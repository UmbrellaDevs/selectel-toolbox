"""
Persistent JSON storage for accounts and settings.
Все чтения/записи защищены asyncio.Lock.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

DATA_FILE = Path("data/config.json")

DEFAULT_SETTINGS: dict[str, Any] = {
    # Скорость намеренно низкая — Selectel не любит шквал запросов.
    "attempts_per_minute": 30,    # 1 попытка каждые 2 секунды
    "error_backoff":       5.0,   # пауза после ошибки API
    "rate_limit_wait":     15.0,  # пауза при 429
    "update_interval":     4.0,   # как часто перерисовывать карточку
}

_lock = asyncio.Lock()


def _load_raw() -> dict:
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"settings": DEFAULT_SETTINGS.copy(), "accounts": []}


def _save_raw(data: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(DATA_FILE)


async def load() -> dict:
    async with _lock:
        return _load_raw()


async def get_accounts() -> list[dict]:
    d = await load()
    return d.get("accounts", [])


async def get_settings() -> dict:
    d = await load()
    merged = DEFAULT_SETTINGS.copy()
    merged.update(d.get("settings", {}))
    return merged


async def upsert_account(account: dict) -> None:
    async with _lock:
        d = _load_raw()
        accounts: list = d.get("accounts", [])
        for i, a in enumerate(accounts):
            if a["name"] == account["name"]:
                accounts[i] = account
                d["accounts"] = accounts
                _save_raw(d)
                return
        accounts.append(account)
        d["accounts"] = accounts
        _save_raw(d)


async def delete_account(name: str) -> None:
    async with _lock:
        d = _load_raw()
        d["accounts"] = [a for a in d.get("accounts", []) if a["name"] != name]
        _save_raw(d)


async def update_setting(key: str, value: Any) -> None:
    async with _lock:
        d = _load_raw()
        s = d.get("settings", {})
        s[key] = value
        d["settings"] = s
        _save_raw(d)
