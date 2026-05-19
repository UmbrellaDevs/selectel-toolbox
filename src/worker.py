"""
Selectel IP Hunter — мягкий, последовательный воркер.

Логика на одну итерацию:
  1) создать floating IP
  2) если адрес подходит под цель — сохранить, остановиться (НЕ удалять)
  3) если не подходит — удалить и подождать до следующей попытки
  4) при ошибке — пауза error_backoff и продолжить
  5) при 429 — пауза rate_limit_wait

Скорость регулируется attempts_per_minute. По умолчанию 30 (одна каждые 2с) —
этого хватает для рутинной работы и не нагружает Selectel.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from .selectel_client import SelectelClient, SelectelError, SelectelRateLimit


def _ip_matches(ip: str, patterns: str) -> bool:
    """Проверка IP против списка паттернов через запятую.

    Поддерживается:
      - точный IP: ``5.250.1.2``
      - префикс/подстрока: ``5.250``
      - wildcard на 4 октета: ``5.*.*.2``
    """
    ip = (ip or "").strip()
    if not ip:
        return False
    for pat in patterns.split(","):
        pat = pat.strip()
        if not pat:
            continue
        if "*" in pat:
            pp, ip_p = pat.split("."), ip.split(".")
            if len(pp) == 4 and len(ip_p) == 4:
                if all(p == "*" or p == i for p, i in zip(pp, ip_p)):
                    return True
        elif len(pat.split(".")) == 4 and "*" not in pat:
            if ip == pat:
                return True
        else:
            # «5.250», «154.30», и т.п. — подстрока в IP
            if pat in ip:
                return True
    return False


@dataclass
class WorkerStats:
    account_name:  str
    region:        str
    target_ip:     str
    attempts:      int   = 0
    errors:        int   = 0
    rate_limits:   int   = 0
    last_ip:       str   = "—"
    last_error:    str   = ""
    start_time:    float = field(default_factory=time.time)
    found:         bool  = False
    found_fip_id:  str   = ""
    running:       bool  = False
    paused_until:  float = 0.0


OnFoundCallback = Callable[[WorkerStats], Awaitable[None]]


class IPWorker:
    def __init__(
        self,
        client:              SelectelClient,
        stats:               WorkerStats,
        attempts_per_minute: int   = 30,
        on_found:            Optional[OnFoundCallback] = None,
        error_backoff:       float = 5.0,
        rate_limit_wait:     float = 15.0,
    ):
        self.client          = client
        self.stats           = stats
        self.interval        = 60.0 / max(attempts_per_minute, 1)
        self.on_found        = on_found
        self.error_backoff   = max(error_backoff, 0.5)
        self.rate_limit_wait = max(rate_limit_wait, 1.0)
        self._done           = asyncio.Event()

    def stop(self) -> None:
        self._done.set()

    async def _sleep_or_stop(self, secs: float) -> None:
        """Спать ``secs`` секунд, но мгновенно проснуться, если попросили остановиться."""
        if secs <= 0:
            return
        try:
            await asyncio.wait_for(self._done.wait(), timeout=secs)
        except asyncio.TimeoutError:
            pass

    async def _safe_delete(self, fip_id: str) -> None:
        if not fip_id:
            return
        try:
            await asyncio.wait_for(self.client.delete_floating_ip(fip_id), timeout=15.0)
        except Exception as e:
            print(f"[worker:{self.stats.account_name}] delete failed: {e}")

    async def run(self) -> None:
        self.stats.running    = True
        self.stats.start_time = time.time()
        try:
            while not self._done.is_set():
                tick_start = time.monotonic()
                fip_id, ip = "", ""
                try:
                    fip_id, ip = await self.client.create_floating_ip()
                    self.stats.attempts += 1
                    self.stats.last_ip   = ip
                    self.stats.last_error = ""

                    if _ip_matches(ip, self.stats.target_ip):
                        # Нашли — сохраняем и завершаем работу.
                        self.stats.found        = True
                        self.stats.found_fip_id = fip_id
                        if self.on_found:
                            try:
                                await self.on_found(self.stats)
                            except Exception as e:
                                print(f"[worker:{self.stats.account_name}] on_found error: {e}")
                        self._done.set()
                        break

                    # Не подошёл — удаляем.
                    await self._safe_delete(fip_id)

                except SelectelRateLimit:
                    self.stats.rate_limits += 1
                    self.stats.last_error   = f"Rate limit — пауза {self.rate_limit_wait:.0f}с"
                    self.stats.paused_until = time.time() + self.rate_limit_wait
                    if fip_id:
                        await self._safe_delete(fip_id)
                    await self._sleep_or_stop(self.rate_limit_wait)
                    continue

                except SelectelError as e:
                    self.stats.errors    += 1
                    self.stats.last_error = str(e)[:120]
                    if fip_id:
                        await self._safe_delete(fip_id)
                    await self._sleep_or_stop(self.error_backoff)
                    continue

                except Exception as e:
                    self.stats.errors    += 1
                    self.stats.last_error = f"{type(e).__name__}: {str(e)[:100]}"
                    if fip_id:
                        await self._safe_delete(fip_id)
                    await self._sleep_or_stop(self.error_backoff)
                    continue

                # Пейсинг до целевой частоты.
                elapsed   = time.monotonic() - tick_start
                sleep_for = max(0.0, self.interval - elapsed)
                if sleep_for > 0:
                    await self._sleep_or_stop(sleep_for)
        finally:
            self.stats.running = False
