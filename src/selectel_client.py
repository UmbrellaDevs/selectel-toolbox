"""
Async Selectel Cloud Platform client.

Аутентификация через Keystone v3 (сервисный пользователь + пароль),
эндпоинт Neutron берётся из service catalog. Управление floating IP
через стандартный Neutron API.

Сделан намеренно «спокойным»: маленький пул соединений, ретраи без
агрессии, мягкая обработка 401 (повторная авторизация) и 429 (исключение
наверх — воркер сделает паузу).
"""

import asyncio
import json
import time
from typing import Optional, Tuple

import aiohttp

_DEFAULT_AUTH_URL = "https://cloud.api.selcloud.ru/identity/v3"


class SelectelError(Exception):
    """Любая ошибка взаимодействия с API Selectel."""


class SelectelRateLimit(SelectelError):
    """HTTP 429 — слишком частые запросы."""


class SelectelClient:
    def __init__(
        self,
        user_name: str,
        password: str,
        account_id: str,        # имя домена = ID аккаунта Selectel (например "123456")
        project_name: str,      # имя проекта в Selectel Cloud
        region: str = "ru-1",   # ru-1, ru-2, ru-3, ru-7, ru-8, ru-9
        auth_url: str = _DEFAULT_AUTH_URL,
    ):
        self._user_name    = user_name
        self._password     = password
        self._account_id   = account_id
        self._project_name = project_name
        self._region       = region
        self._auth_url     = auth_url.rstrip("/")

        self._token: Optional[str] = None
        self._token_expires: float = 0.0
        self._neutron_url: Optional[str] = None
        self._external_net_id: Optional[str] = None

        self._session: Optional[aiohttp.ClientSession] = None
        self._auth_lock    = asyncio.Lock()
        self._session_lock = asyncio.Lock()

    # ── Session ───────────────────────────────────────────────────────

    async def _make_session(self) -> aiohttp.ClientSession:
        connector = aiohttp.TCPConnector(
            limit=20,
            limit_per_host=10,
            ttl_dns_cache=600,
            enable_cleanup_closed=True,
        )
        return aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=30, connect=8, sock_read=20),
        )

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            async with self._session_lock:
                if self._session is None or self._session.closed:
                    self._session = await self._make_session()
        return self._session

    async def _reset_session(self) -> None:
        async with self._session_lock:
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = await self._make_session()

    # ── Keystone v3 auth ──────────────────────────────────────────────

    async def _authenticate(self) -> None:
        payload = {
            "auth": {
                "identity": {
                    "methods": ["password"],
                    "password": {
                        "user": {
                            "name":     self._user_name,
                            "domain":   {"name": self._account_id},
                            "password": self._password,
                        }
                    },
                },
                "scope": {
                    "project": {
                        "name":   self._project_name,
                        "domain": {"name": self._account_id},
                    }
                },
            }
        }
        sess = await self._sess()
        url = f"{self._auth_url}/auth/tokens"
        try:
            async with sess.post(url, json=payload) as r:
                text = await r.text()
                if r.status not in (200, 201):
                    raise SelectelError(f"Auth {r.status}: {text[:200]}")
                token = r.headers.get("X-Subject-Token")
                if not token:
                    raise SelectelError("В ответе нет X-Subject-Token")
                try:
                    data = json.loads(text)
                except json.JSONDecodeError as e:
                    raise SelectelError(f"Auth JSON decode error: {e}")
        except aiohttp.ClientError as e:
            raise SelectelError(f"Auth connection error: {e}") from e

        self._token = token
        # Токен выдают на ~24 часа, но мы перевыпускаем заранее.
        self._token_expires = time.time() + 10 * 3600

        # Берём публичный endpoint Neutron для нужного региона из каталога.
        token_info = data.get("token", {})
        neutron_url = None
        for svc in token_info.get("catalog", []):
            if svc.get("type") != "network":
                continue
            for ep in svc.get("endpoints", []):
                if ep.get("interface") == "public" and ep.get("region") == self._region:
                    neutron_url = (ep.get("url") or "").rstrip("/")
                    break
            if neutron_url:
                break
        if not neutron_url:
            raise SelectelError(
                f"Endpoint Neutron для региона '{self._region}' не найден в каталоге. "
                f"Проверьте, что в выбранном регионе у проекта есть сетевая квота."
            )
        self._neutron_url = neutron_url
        self._external_net_id = None  # сбрасываем кэш — на случай смены проекта

    async def _ensure_token(self) -> str:
        async with self._auth_lock:
            if self._token and time.time() < self._token_expires - 300:
                return self._token  # type: ignore[return-value]
            await self._authenticate()
            return self._token  # type: ignore[return-value]

    async def _hdrs(self) -> dict:
        return {
            "X-Auth-Token": await self._ensure_token(),
            "Content-Type": "application/json",
            "Accept":       "application/json",
        }

    # ── HTTP helper ──────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        url: str,
        payload: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> dict:
        for attempt in range(2):
            try:
                sess = await self._sess()
                kwargs: dict = {"headers": await self._hdrs()}
                if payload is not None:
                    kwargs["json"] = payload
                if timeout is not None:
                    kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout)
                async with sess.request(method, url, **kwargs) as r:
                    text = await r.text()
                    if r.status == 401:
                        # Токен протух — сбрасываем и пробуем ещё раз
                        async with self._auth_lock:
                            self._token = None
                        if attempt == 0:
                            continue
                        raise SelectelError("401 Unauthorized после повторной авторизации")
                    if r.status == 429:
                        raise SelectelRateLimit("429 Too Many Requests")
                    if r.status == 404:
                        return {}
                    if r.status not in (200, 201, 202, 204):
                        raise SelectelError(f"{method} {r.status}: {text[:200]}")
                    if not text:
                        return {}
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        return {}
            except SelectelError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt == 0:
                    await self._reset_session()
                    continue
                raise SelectelError(f"Connection error: {e}") from e
        raise SelectelError("Запрос не выполнен после ретраев")

    # ── Neutron: external networks ───────────────────────────────────

    async def _get_external_net_id(self) -> str:
        if self._external_net_id:
            return self._external_net_id
        await self._ensure_token()
        url = f"{self._neutron_url}/v2.0/networks?router:external=true"
        data = await self._request("GET", url)
        nets = data.get("networks", [])
        if not nets:
            raise SelectelError(
                "В регионе нет внешних сетей (router:external=true). "
                "Создайте роутер с внешним шлюзом в панели Selectel."
            )
        self._external_net_id = nets[0]["id"]
        return self._external_net_id  # type: ignore[return-value]

    # ── Neutron: floating IPs ────────────────────────────────────────

    async def create_floating_ip(self) -> Tuple[str, str]:
        """Создать floating IP. Возвращает (id, ip_address)."""
        await self._ensure_token()
        net_id = await self._get_external_net_id()
        url = f"{self._neutron_url}/v2.0/floatingips"
        data = await self._request("POST", url, {
            "floatingip": {"floating_network_id": net_id}
        })
        fip = data.get("floatingip", {})
        fip_id = fip.get("id", "")
        addr   = fip.get("floating_ip_address", "")
        if not fip_id or not addr:
            raise SelectelError(f"Некорректный ответ при создании FIP: {data}")
        return fip_id, addr

    async def delete_floating_ip(self, fip_id: str) -> None:
        await self._ensure_token()
        url = f"{self._neutron_url}/v2.0/floatingips/{fip_id}"
        try:
            await self._request("DELETE", url)
        except SelectelError as e:
            # 404 уже свернули в {} в _request, но на всякий случай
            if "404" in str(e):
                return
            raise

    async def list_floating_ips(self) -> list[dict]:
        await self._ensure_token()
        url = f"{self._neutron_url}/v2.0/floatingips"
        data = await self._request("GET", url)
        result = []
        for f in data.get("floatingips", []):
            result.append({
                "id":      f.get("id", ""),
                "ip":      f.get("floating_ip_address", ""),
                "status":  f.get("status", ""),
                "port_id": f.get("port_id") or "",
            })
        return result

    # ── Cleanup ───────────────────────────────────────────────────────

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
