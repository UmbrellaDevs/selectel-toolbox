"""
Telegram UI для Selectel IP Hunter.

Возможности:
  • Добавление/редактирование/удаление аккаунтов Selectel
  • Настройка скорости и пауз
  • Запуск/остановка охоты по выбранным аккаунтам
  • Живая карточка прогресса с обновлением
  • Алерт при найденном IP
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import storage
from .selectel_client import SelectelClient
from .worker import IPWorker, WorkerStats

REGIONS = ["ru-1", "ru-2", "ru-3", "ru-7", "ru-8", "ru-9"]

SETTINGS_META: dict[str, tuple[str, type, str]] = {
    "attempts_per_minute": ("🔄 Попыток/мин",       int,
                            "Попыток создания IP в минуту на аккаунт.\n"
                            "💡 По умолчанию 30 (одна каждые 2 сек) — мягкая нагрузка."),
    "error_backoff":       ("⚡ Пауза при ошибке",   float,
                            "Сколько секунд ждать после ошибки API."),
    "rate_limit_wait":     ("🚫 Пауза Rate Limit",   float,
                            "Сколько секунд ждать при HTTP 429."),
    "update_interval":     ("🕐 Обновление, сек",    float,
                            "Как часто перерисовывать карточку (мин. 2 сек)."),
}

IP_HINT = (
    "Введите IP-адрес(а) для поиска:\n\n"
    "<code>5.250.1.2</code>   — точный IP\n"
    "<code>5.250</code>        — начинается с 5.250\n"
    "<code>5.*.*.2</code>     — wildcard (ровно 4 октета)\n\n"
    "💡 <b>Несколько целей</b> — через запятую:\n"
    "<code>5.250, 95.213</code>"
)


# ── FSM ───────────────────────────────────────────────────────────────

class AddAccount(StatesGroup):
    name         = State()
    user_name    = State()
    password     = State()
    account_id   = State()
    project_name = State()
    region       = State()
    target_ip    = State()


class EditField(StatesGroup):
    waiting = State()


class EditSetting(StatesGroup):
    waiting = State()


# ── Hunt state ────────────────────────────────────────────────────────

@dataclass
class _Hunt:
    active:  bool = False
    workers: list[IPWorker]    = field(default_factory=list)
    tasks:   list[asyncio.Task] = field(default_factory=list)
    stats:   list[WorkerStats] = field(default_factory=list)
    clients: list[SelectelClient] = field(default_factory=list)
    updater:    Optional[asyncio.Task] = None
    supervisor: Optional[asyncio.Task] = None
    chat_id:    Optional[int] = None
    msg_id:     Optional[int] = None
    update_interval: float = 4.0


_hunt = _Hunt()
_OWNER: int = 0


def _ok(event: Message | CallbackQuery) -> bool:
    uid = event.from_user.id if event.from_user else 0
    if uid != _OWNER:
        print(f"[bot] ACCESS DENIED: uid={uid} owner={_OWNER}")
        return False
    return True


# ── Keyboards ─────────────────────────────────────────────────────────

def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def kb_main(active: bool) -> InlineKeyboardMarkup:
    hunt_btn = (_btn("⏹ Остановить охоту", "hunt:stop") if active
                else _btn("🎯 Запустить охоту", "hunt:start"))
    return InlineKeyboardMarkup(inline_keyboard=[
        [hunt_btn],
        [_btn("👥 Аккаунты", "menu:accounts"),
         _btn("⚙️ Настройки", "menu:settings")],
    ])


def kb_accounts(accounts: list, active: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i, a in enumerate(accounts):
        rows.append([
            _btn(f"👤 {a['name']}  ·  {a['region']}", f"acc:view:{i}"),
            _btn("🗑", f"acc:del:{i}"),
        ])
    if not active:
        rows.append([_btn("➕ Добавить аккаунт", "acc:add")])
    rows.append([_btn("⬅️ Главное меню", "menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_account_detail(idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("📛 Название",   f"acc:edit:{idx}:name"),
         _btn("🎯 Цель IP",    f"acc:edit:{idx}:target_ip")],
        [_btn("👤 Логин",      f"acc:edit:{idx}:user_name"),
         _btn("🔑 Пароль",     f"acc:edit:{idx}:password")],
        [_btn("🏷 ID аккаунта", f"acc:edit:{idx}:account_id"),
         _btn("📁 Проект",     f"acc:edit:{idx}:project_name")],
        [_btn("📍 Регион",     f"acc:edit:{idx}:region")],
        [_btn("🗂 IP в проекте", f"ips:list:{idx}")],
        [_btn("🗑 Удалить аккаунт", f"acc:del:{idx}"),
         _btn("⬅️ Назад", "menu:accounts")],
    ])


def kb_regions(prefix: str, cancel_cb: str) -> InlineKeyboardMarkup:
    rows = []
    row: list[InlineKeyboardButton] = []
    for r in REGIONS:
        row.append(_btn(r, f"{prefix}:{r}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([_btn("❌ Отмена", cancel_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_settings(s: dict) -> InlineKeyboardMarkup:
    rows = [[_btn(f"{meta[0]}:  {s.get(k, '—')}", f"set:{k}")]
            for k, meta in SETTINGS_META.items()]
    rows.append([_btn("⬅️ Главное меню", "menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_confirm_del_account(idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        _btn("✅ Да, удалить", f"acc:del:{idx}:yes"),
        _btn("❌ Отмена",      "menu:accounts"),
    ]])


def kb_cancel_add() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        _btn("❌ Отмена", "menu:accounts"),
    ]])


def kb_cancel_edit(idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        _btn("❌ Отмена", f"acc:view:{idx}"),
    ]])


def kb_cancel_setting() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        _btn("❌ Отмена", "menu:settings"),
    ]])


def kb_hunt_card() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("⏹ Остановить охоту", "hunt:stop")],
        [_btn("⬅️ Главное меню", "menu:main")],
    ])


def kb_ip_list(ips: list[dict], acc_idx: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for ip_item in ips[:20]:
        icon  = "🔗" if ip_item.get("port_id") else "🆓"
        label = f"{icon} {ip_item['ip']}"
        rows.append([
            _btn(label, "ip:noop"),
            _btn("🗑", f"ip:del:{acc_idx}:{ip_item['id']}"),
        ])
    rows.append([
        _btn("🔄 Обновить",     f"ips:list:{acc_idx}"),
        _btn("⬅️ К аккаунту",  f"acc:view:{acc_idx}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Text builders ─────────────────────────────────────────────────────

def _mask(s: str, keep: int = 3) -> str:
    if not s:
        return "—"
    if len(s) <= keep * 2:
        return "***"
    return s[:keep] + "…" + s[-keep:]


def _dur(secs: float) -> str:
    s = int(secs)
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def text_main(accounts: list, s: dict, active: bool) -> str:
    status = "🟢 Охота активна" if active else "⚪ Ожидание"
    lines = []
    for a in accounts[:6]:
        lines.append(
            f"  <code>{a['name']}</code> · {a['region']} · 🎯 <code>{a['target_ip']}</code>"
        )
    acc_preview = ("\n" + "\n".join(lines)) if lines else ""
    if len(accounts) > 6:
        acc_preview += f"\n  <i>…и ещё {len(accounts) - 6}</i>"
    return (
        f"☁️ <b>Selectel IP Hunter</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{status}\n"
        f"👤 <b>{len(accounts)}</b> акк  ·  ⚡ <code>{s.get('attempts_per_minute', '—')}</code> rpm"
        f"{acc_preview}"
    )


def text_accounts(accounts: list) -> str:
    if not accounts:
        return ("👥 <b>Аккаунты</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "<i>Пусто — добавьте первый аккаунт</i>")
    lines = [
        f"👤 <b>{a['name']}</b>  ·  {a['region']}  ·  🎯 <code>{a['target_ip']}</code>"
        for a in accounts
    ]
    return (f"👥 <b>Аккаунты</b>  ·  {len(accounts)}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines))


def text_account_detail(a: dict) -> str:
    return (
        f"👤 <b>{a['name']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Логин:    <code>{a['user_name']}</code>\n"
        f"🔑 Пароль:   <code>{_mask(a['password'])}</code>\n"
        f"🏷 Аккаунт:  <code>{a['account_id']}</code>\n"
        f"📁 Проект:   <code>{a['project_name']}</code>\n"
        f"📍 Регион:   <code>{a['region']}</code>\n"
        f"🎯 Цель:     <code>{a['target_ip']}</code>"
    )


def text_settings(s: dict) -> str:
    return (
        "⚙️ <b>Настройки</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Управляйте нагрузкой на API Selectel.\n"
        "<i>Меньше rpm = мягче к API.</i>"
    )


def text_ip_list(ips: list[dict], acc_name: str) -> str:
    if not ips:
        return (f"🗂 <b>IP — {acc_name}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<i>Floating IP в проекте нет</i>")
    free = sum(1 for ip in ips if not ip.get("port_id"))
    return (
        f"🗂 <b>IP — {acc_name}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Всего: <code>{len(ips)}</code>  "
        f"🆓 <code>{free}</code>  🔗 <code>{len(ips) - free}</code>\n\n"
        f"<i>🔗 — IP привязан к порту (нельзя удалить)</i>"
    )


def _account_card(stats: WorkerStats) -> str:
    elapsed = time.time() - stats.start_time
    if stats.found:
        badge = "✅ НАЙДЕН!"
    elif stats.paused_until > time.time():
        badge = f"⏸ ({int(stats.paused_until - time.time())}с)"
    elif stats.running:
        badge = "🟢"
    else:
        badge = "🔴"
    rl  = f" 🚫{stats.rate_limits}" if stats.rate_limits else ""
    err = f"\n  ⚠️ <code>{stats.last_error[:70]}</code>" if stats.last_error else ""
    return (
        f"{badge} <b>{stats.account_name}</b>\n"
        f"  🎯 <code>{stats.target_ip}</code>  📍 <code>{stats.region}</code>\n"
        f"  📡 <code>{stats.last_ip}</code>  🔄 <code>{stats.attempts}</code>"
        f"  ❌ <code>{stats.errors}</code>{rl}\n"
        f"  ⏱ <code>{_dur(elapsed)}</code>"
        f"{err}"
    )


def build_hunt_card(all_stats: list[WorkerStats]) -> str:
    blocks = [_account_card(s) for s in all_stats]
    total_att = sum(s.attempts for s in all_stats)
    elapsed = time.time() - min(
        (s.start_time for s in all_stats), default=time.time()
    )
    found = any(s.found for s in all_stats)
    header = ("✅ <b>Охота завершена</b>" if found
              else f"🔍 <b>Охота</b>  ·  {len(all_stats)} акк")
    return (
        f"{header}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + "\n\n".join(blocks)
        + f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 Всего попыток: <code>{total_att}</code>  "
        f"⏱ <code>{_dur(elapsed)}</code>\n"
        f"🕐 <i>{time.strftime('%H:%M:%S')}</i>"
    )


def build_found_alert(stats: WorkerStats) -> str:
    elapsed = time.time() - stats.start_time
    return (
        f"🎉  <b>IP НАЙДЕН!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐  <code>{stats.last_ip}</code>\n"
        f"🎯  Цель:    <code>{stats.target_ip}</code>\n"
        f"👤  Аккаунт: <code>{stats.account_name}</code>\n"
        f"📍  Регион:  <code>{stats.region}</code>\n"
        f"🔄  Попыток: <code>{stats.attempts}</code>  "
        f"⏱ <code>{_dur(elapsed)}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔑 ID: <code>{stats.found_fip_id}</code>\n\n"
        f"💡 <i>Floating IP зарезервирован. Привяжите к ресурсу в панели Selectel.</i>"
    )


# ── Router + middleware ───────────────────────────────────────────────

router = Router()


class _CbDebugMw(BaseMiddleware):
    """Логируем callback-и и не даём упасть бот-процессу."""

    async def __call__(self, handler, event, data):
        cb_data = getattr(event, "data", "?")
        try:
            return await handler(event, data)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[cb] ERROR ({cb_data}): {e}")
            try:
                await event.answer(f"⚠️ {str(e)[:150]}", show_alert=True)
            except Exception:
                pass


router.callback_query.middleware(_CbDebugMw())


async def _safe_edit(
    bot: Bot, chat_id: int, msg_id: int, text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    try:
        await bot.edit_message_text(
            text, chat_id=chat_id, message_id=msg_id,
            parse_mode="HTML", reply_markup=reply_markup,
        )
    except TelegramRetryAfter as e:
        if e.retry_after <= 30:
            await asyncio.sleep(e.retry_after)
            try:
                await bot.edit_message_text(
                    text, chat_id=chat_id, message_id=msg_id,
                    parse_mode="HTML", reply_markup=reply_markup,
                )
            except Exception:
                pass
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            print(f"[bot] edit: {e}")
    except Exception as e:
        print(f"[bot] edit: {e}")


async def _cb_edit(callback: CallbackQuery, text: str, **kwargs) -> None:
    """Edit callback message; на ошибке — отправить новое сообщение."""
    kwargs.setdefault("parse_mode", "HTML")
    try:
        await callback.message.edit_text(text, **kwargs)
    except TelegramBadRequest as e:
        if "not modified" in str(e).lower():
            return
        try:
            await callback.message.answer(text, **kwargs)
        except Exception:
            pass
    except TelegramRetryAfter:
        try:
            await callback.message.answer(text, **kwargs)
        except Exception:
            pass


# ── Menu handlers ─────────────────────────────────────────────────────

@router.message(CommandStart())
async def on_start(m: Message, state: FSMContext) -> None:
    if not _ok(m):
        return
    await state.clear()
    accounts = await storage.get_accounts()
    s = await storage.get_settings()
    await m.answer(
        text_main(accounts, s, _hunt.active),
        parse_mode="HTML",
        reply_markup=kb_main(_hunt.active),
    )


@router.callback_query(F.data == "menu:main")
async def cb_main(cb: CallbackQuery, state: FSMContext) -> None:
    if not _ok(cb):
        return
    await state.clear()
    accounts = await storage.get_accounts()
    s = await storage.get_settings()
    await _cb_edit(
        cb, text_main(accounts, s, _hunt.active),
        reply_markup=kb_main(_hunt.active),
    )
    await cb.answer()


@router.callback_query(F.data == "menu:accounts")
async def cb_accounts(cb: CallbackQuery, state: FSMContext) -> None:
    if not _ok(cb):
        return
    await state.clear()
    accounts = await storage.get_accounts()
    await _cb_edit(
        cb, text_accounts(accounts),
        reply_markup=kb_accounts(accounts, _hunt.active),
    )
    await cb.answer()


@router.callback_query(F.data == "menu:settings")
async def cb_settings(cb: CallbackQuery, state: FSMContext) -> None:
    if not _ok(cb):
        return
    s = await storage.get_settings()
    await _cb_edit(cb, text_settings(s), reply_markup=kb_settings(s))
    await cb.answer()


# ── Add account flow ──────────────────────────────────────────────────

@router.callback_query(F.data == "acc:add")
async def cb_acc_add(cb: CallbackQuery, state: FSMContext) -> None:
    if not _ok(cb):
        return
    if _hunt.active:
        await cb.answer("Сначала остановите охоту", show_alert=True)
        return
    await state.set_state(AddAccount.name)
    await _cb_edit(
        cb,
        "📛 <b>Название аккаунта</b>\n\nКороткое имя для отображения.",
        reply_markup=kb_cancel_add(),
    )
    await cb.answer()


@router.message(AddAccount.name)
async def st_name(m: Message, state: FSMContext) -> None:
    if not _ok(m):
        return
    name = (m.text or "").strip()
    if not name:
        return
    await state.update_data(name=name)
    await state.set_state(AddAccount.user_name)
    await m.answer(
        "👤 <b>Сервисный пользователь</b>\n\n"
        "Имя сервисного пользователя Selectel.\n"
        "<i>Создаётся в панели: Управление → Пользователи.</i>",
        parse_mode="HTML",
        reply_markup=kb_cancel_add(),
    )


@router.message(AddAccount.user_name)
async def st_user(m: Message, state: FSMContext) -> None:
    if not _ok(m):
        return
    user = (m.text or "").strip()
    if not user:
        return
    await state.update_data(user_name=user)
    await state.set_state(AddAccount.password)
    await m.answer(
        "🔑 <b>Пароль</b>\n\n"
        "Пароль сервисного пользователя.\n"
        "<i>Сообщение с паролем будет удалено сразу после ввода.</i>",
        parse_mode="HTML",
        reply_markup=kb_cancel_add(),
    )


@router.message(AddAccount.password)
async def st_pwd(m: Message, state: FSMContext) -> None:
    if not _ok(m):
        return
    pwd = (m.text or "").strip()
    if not pwd:
        return
    await state.update_data(password=pwd)
    try:
        await m.delete()
    except Exception:
        pass
    await state.set_state(AddAccount.account_id)
    await m.answer(
        "🏷 <b>ID аккаунта Selectel</b>\n\n"
        "Номер вашего аккаунта (например <code>123456</code>) — он же доменное имя в Keystone.",
        parse_mode="HTML",
        reply_markup=kb_cancel_add(),
    )


@router.message(AddAccount.account_id)
async def st_acc(m: Message, state: FSMContext) -> None:
    if not _ok(m):
        return
    acc = (m.text or "").strip()
    if not acc:
        return
    await state.update_data(account_id=acc)
    await state.set_state(AddAccount.project_name)
    await m.answer(
        "📁 <b>Имя проекта</b>\n\nНазвание проекта в Selectel Cloud.",
        parse_mode="HTML",
        reply_markup=kb_cancel_add(),
    )


@router.message(AddAccount.project_name)
async def st_proj(m: Message, state: FSMContext) -> None:
    if not _ok(m):
        return
    proj = (m.text or "").strip()
    if not proj:
        return
    await state.update_data(project_name=proj)
    await state.set_state(AddAccount.region)
    await m.answer(
        "📍 <b>Регион</b>\n\nВыберите регион Selectel:",
        parse_mode="HTML",
        reply_markup=kb_regions("addreg", "menu:accounts"),
    )


@router.callback_query(AddAccount.region, F.data.startswith("addreg:"))
async def cb_addreg(cb: CallbackQuery, state: FSMContext) -> None:
    if not _ok(cb):
        return
    region = cb.data.split(":", 1)[1]
    await state.update_data(region=region)
    await state.set_state(AddAccount.target_ip)
    await _cb_edit(cb, "🎯 <b>Цель поиска</b>\n\n" + IP_HINT,
                   reply_markup=kb_cancel_add())
    await cb.answer()


@router.message(AddAccount.target_ip)
async def st_target(m: Message, state: FSMContext) -> None:
    if not _ok(m):
        return
    target = (m.text or "").strip()
    if not target:
        return
    data = await state.get_data()
    account = {
        "name":         data["name"],
        "user_name":    data["user_name"],
        "password":     data["password"],
        "account_id":   data["account_id"],
        "project_name": data["project_name"],
        "region":       data["region"],
        "target_ip":    target,
    }
    await storage.upsert_account(account)
    await state.clear()
    accounts = await storage.get_accounts()
    await m.answer(
        f"✅ Аккаунт <b>{account['name']}</b> добавлен.\n\n" + text_accounts(accounts),
        parse_mode="HTML",
        reply_markup=kb_accounts(accounts, _hunt.active),
    )


# ── View / edit / delete account ──────────────────────────────────────

@router.callback_query(F.data.startswith("acc:view:"))
async def cb_acc_view(cb: CallbackQuery, state: FSMContext) -> None:
    if not _ok(cb):
        return
    await state.clear()
    idx = int(cb.data.split(":")[2])
    accounts = await storage.get_accounts()
    if idx >= len(accounts):
        await cb.answer("Нет такого аккаунта", show_alert=True)
        return
    a = accounts[idx]
    await _cb_edit(cb, text_account_detail(a), reply_markup=kb_account_detail(idx))
    await cb.answer()


@router.callback_query(F.data.startswith("acc:edit:"))
async def cb_acc_edit(cb: CallbackQuery, state: FSMContext) -> None:
    if not _ok(cb):
        return
    if _hunt.active:
        await cb.answer("Сначала остановите охоту", show_alert=True)
        return
    parts = cb.data.split(":")
    idx   = int(parts[2])
    field = parts[3]
    accounts = await storage.get_accounts()
    if idx >= len(accounts):
        await cb.answer("Нет такого аккаунта", show_alert=True)
        return
    if field == "region":
        await _cb_edit(
            cb, "📍 <b>Регион</b>\n\nВыберите новый регион:",
            reply_markup=kb_regions(f"editreg:{idx}", f"acc:view:{idx}"),
        )
        await cb.answer()
        return
    await state.set_state(EditField.waiting)
    await state.update_data(idx=idx, field=field)
    prompts = {
        "name":         "📛 Введите новое название.",
        "user_name":    "👤 Введите имя сервисного пользователя.",
        "password":     "🔑 Введите новый пароль.\n<i>Сообщение будет удалено.</i>",
        "account_id":   "🏷 Введите ID аккаунта.",
        "project_name": "📁 Введите имя проекта.",
        "target_ip":    f"🎯 <b>Новая цель</b>\n\n{IP_HINT}",
    }
    await _cb_edit(
        cb, prompts.get(field, "Введите значение:"),
        reply_markup=kb_cancel_edit(idx),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("editreg:"))
async def cb_edit_region(cb: CallbackQuery, state: FSMContext) -> None:
    if not _ok(cb):
        return
    parts = cb.data.split(":")
    idx    = int(parts[1])
    region = parts[2]
    accounts = await storage.get_accounts()
    if idx >= len(accounts):
        await cb.answer("Нет такого аккаунта", show_alert=True)
        return
    a = dict(accounts[idx])
    a["region"] = region
    await storage.upsert_account(a)
    await _cb_edit(cb, text_account_detail(a), reply_markup=kb_account_detail(idx))
    await cb.answer("Регион обновлён")


@router.message(EditField.waiting)
async def st_edit(m: Message, state: FSMContext) -> None:
    if not _ok(m):
        return
    val = (m.text or "").strip()
    if not val:
        return
    data = await state.get_data()
    idx   = data.get("idx")
    field = data.get("field")
    accounts = await storage.get_accounts()
    if idx is None or idx >= len(accounts) or not field:
        await state.clear()
        return
    a = dict(accounts[idx])
    a[field] = val
    await storage.upsert_account(a)
    if field == "password":
        try:
            await m.delete()
        except Exception:
            pass
    await state.clear()
    await m.answer(
        text_account_detail(a),
        parse_mode="HTML",
        reply_markup=kb_account_detail(idx),
    )


@router.callback_query(F.data.startswith("acc:del:"))
async def cb_acc_del(cb: CallbackQuery, state: FSMContext) -> None:
    if not _ok(cb):
        return
    if _hunt.active:
        await cb.answer("Сначала остановите охоту", show_alert=True)
        return
    parts = cb.data.split(":")
    idx = int(parts[2])
    accounts = await storage.get_accounts()
    if idx >= len(accounts):
        await cb.answer("Нет такого аккаунта", show_alert=True)
        return
    if len(parts) == 4 and parts[3] == "yes":
        name = accounts[idx]["name"]
        await storage.delete_account(name)
        accounts = await storage.get_accounts()
        await _cb_edit(
            cb, text_accounts(accounts),
            reply_markup=kb_accounts(accounts, _hunt.active),
        )
        await cb.answer(f"Удалён: {name}")
        return
    await _cb_edit(
        cb,
        f"🗑 Удалить аккаунт <b>{accounts[idx]['name']}</b>?",
        reply_markup=kb_confirm_del_account(idx),
    )
    await cb.answer()


# ── IP list / delete ──────────────────────────────────────────────────

@router.callback_query(F.data.startswith("ips:list:"))
async def cb_ips_list(cb: CallbackQuery, state: FSMContext) -> None:
    if not _ok(cb):
        return
    idx = int(cb.data.split(":")[2])
    accounts = await storage.get_accounts()
    if idx >= len(accounts):
        await cb.answer("Нет аккаунта", show_alert=True)
        return
    a = accounts[idx]
    client = SelectelClient(
        user_name=a["user_name"], password=a["password"],
        account_id=a["account_id"], project_name=a["project_name"],
        region=a["region"],
    )
    try:
        await cb.answer("Загружаю…")
        ips = await client.list_floating_ips()
    except Exception as e:
        await _cb_edit(
            cb,
            f"⚠️ Ошибка получения IP:\n<code>{str(e)[:200]}</code>",
            reply_markup=kb_account_detail(idx),
        )
        return
    finally:
        await client.close()
    await _cb_edit(
        cb, text_ip_list(ips, a["name"]),
        reply_markup=kb_ip_list(ips, idx),
    )


@router.callback_query(F.data.startswith("ip:del:"))
async def cb_ip_del(cb: CallbackQuery, state: FSMContext) -> None:
    if not _ok(cb):
        return
    parts = cb.data.split(":")
    idx    = int(parts[2])
    fip_id = parts[3]
    accounts = await storage.get_accounts()
    if idx >= len(accounts):
        await cb.answer("Нет аккаунта", show_alert=True)
        return
    a = accounts[idx]
    client = SelectelClient(
        user_name=a["user_name"], password=a["password"],
        account_id=a["account_id"], project_name=a["project_name"],
        region=a["region"],
    )
    try:
        await client.delete_floating_ip(fip_id)
        await cb.answer("Удалён")
    except Exception as e:
        await cb.answer(f"⚠️ {str(e)[:150]}", show_alert=True)
    finally:
        await client.close()
    # Перерисовать список
    cb.data = f"ips:list:{idx}"
    await cb_ips_list(cb, state)


@router.callback_query(F.data == "ip:noop")
async def cb_ip_noop(cb: CallbackQuery) -> None:
    await cb.answer()


# ── Settings edit ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("set:"))
async def cb_set(cb: CallbackQuery, state: FSMContext) -> None:
    if not _ok(cb):
        return
    key = cb.data.split(":", 1)[1]
    if key not in SETTINGS_META:
        await cb.answer("Неизвестный параметр", show_alert=True)
        return
    label, _typ, help_text = SETTINGS_META[key]
    await state.set_state(EditSetting.waiting)
    await state.update_data(key=key)
    s = await storage.get_settings()
    await _cb_edit(
        cb,
        f"<b>{label}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Текущее: <code>{s.get(key, '—')}</code>\n\n"
        f"{help_text}\n\n"
        f"Введите новое значение:",
        reply_markup=kb_cancel_setting(),
    )
    await cb.answer()


@router.message(EditSetting.waiting)
async def st_setting(m: Message, state: FSMContext) -> None:
    if not _ok(m):
        return
    val = (m.text or "").strip()
    data = await state.get_data()
    key = data.get("key", "")
    meta = SETTINGS_META.get(key)
    if not meta:
        await state.clear()
        return
    _label, typ, _help = meta
    try:
        parsed = typ(val)
        if key == "update_interval" and parsed < 2:
            parsed = 2
        if key == "attempts_per_minute" and parsed < 1:
            parsed = 1
    except Exception:
        await m.answer(
            f"⚠️ Не удалось преобразовать «{val}» к типу {typ.__name__}",
            parse_mode="HTML",
        )
        return
    await storage.update_setting(key, parsed)
    await state.clear()
    s = await storage.get_settings()
    await m.answer(text_settings(s), parse_mode="HTML", reply_markup=kb_settings(s))


# ── Hunt control ──────────────────────────────────────────────────────

async def _on_found(_stats: WorkerStats) -> None:
    """Callback на найденный IP — останавливаем все воркеры."""
    for w in _hunt.workers:
        w.stop()


async def _hunt_updater(bot: Bot) -> None:
    """Периодически перерисовывает карточку охоты."""
    try:
        while _hunt.active and _hunt.chat_id and _hunt.msg_id:
            try:
                await _safe_edit(
                    bot, _hunt.chat_id, _hunt.msg_id,
                    build_hunt_card(_hunt.stats),
                    reply_markup=kb_hunt_card(),
                )
            except Exception as e:
                print(f"[updater] {e}")
            await asyncio.sleep(_hunt.update_interval)
    except asyncio.CancelledError:
        pass


async def _hunt_supervisor(bot: Bot) -> None:
    """Ждёт завершения всех воркеров, шлёт алерты, очищает состояние."""
    try:
        if _hunt.tasks:
            await asyncio.gather(*_hunt.tasks, return_exceptions=True)
    finally:
        # Алерты по найденным
        for s in _hunt.stats:
            if s.found and _hunt.chat_id:
                try:
                    await bot.send_message(
                        _hunt.chat_id, build_found_alert(s), parse_mode="HTML",
                    )
                except Exception:
                    pass
        # Закрыть клиенты
        for c in _hunt.clients:
            try:
                await c.close()
            except Exception:
                pass
        # Финальная карточка (статичная)
        if _hunt.chat_id and _hunt.msg_id:
            try:
                await _safe_edit(
                    bot, _hunt.chat_id, _hunt.msg_id,
                    build_hunt_card(_hunt.stats),
                    reply_markup=kb_hunt_card(),
                )
            except Exception:
                pass
        # Сброс
        _hunt.active  = False
        _hunt.workers = []
        _hunt.tasks   = []
        _hunt.stats   = []
        _hunt.clients = []
        if _hunt.updater:
            _hunt.updater.cancel()
            _hunt.updater = None
        # Вернём главное меню сообщением
        if _hunt.chat_id:
            try:
                accounts = await storage.get_accounts()
                s = await storage.get_settings()
                await bot.send_message(
                    _hunt.chat_id,
                    text_main(accounts, s, False),
                    parse_mode="HTML",
                    reply_markup=kb_main(False),
                )
            except Exception:
                pass
        _hunt.chat_id = None
        _hunt.msg_id  = None
        _hunt.supervisor = None


@router.callback_query(F.data == "hunt:start")
async def cb_hunt_start(cb: CallbackQuery, state: FSMContext) -> None:
    if not _ok(cb):
        return
    if _hunt.active:
        await cb.answer("Уже идёт", show_alert=True)
        return
    accounts = await storage.get_accounts()
    if not accounts:
        await cb.answer("Сначала добавьте аккаунт", show_alert=True)
        return
    s = await storage.get_settings()
    rpm     = int(s.get("attempts_per_minute", 30))
    backoff = float(s.get("error_backoff", 5.0))
    rl_wait = float(s.get("rate_limit_wait", 15.0))
    _hunt.update_interval = max(float(s.get("update_interval", 4.0)), 2.0)

    _hunt.workers = []
    _hunt.stats   = []
    _hunt.clients = []
    _hunt.tasks   = []

    for a in accounts:
        client = SelectelClient(
            user_name=a["user_name"],
            password=a["password"],
            account_id=a["account_id"],
            project_name=a["project_name"],
            region=a["region"],
        )
        stats = WorkerStats(
            account_name=a["name"],
            region=a["region"],
            target_ip=a["target_ip"],
        )
        worker = IPWorker(
            client=client,
            stats=stats,
            attempts_per_minute=rpm,
            on_found=_on_found,
            error_backoff=backoff,
            rate_limit_wait=rl_wait,
        )
        _hunt.clients.append(client)
        _hunt.stats.append(stats)
        _hunt.workers.append(worker)

    _hunt.active  = True
    _hunt.chat_id = cb.message.chat.id
    _hunt.msg_id  = cb.message.message_id

    # Стартовая карточка
    await _safe_edit(
        cb.bot, _hunt.chat_id, _hunt.msg_id,
        build_hunt_card(_hunt.stats),
        reply_markup=kb_hunt_card(),
    )

    # Запускаем воркеры
    for w in _hunt.workers:
        _hunt.tasks.append(asyncio.create_task(w.run()))

    _hunt.updater    = asyncio.create_task(_hunt_updater(cb.bot))
    _hunt.supervisor = asyncio.create_task(_hunt_supervisor(cb.bot))
    await cb.answer("Охота запущена")


@router.callback_query(F.data == "hunt:stop")
async def cb_hunt_stop(cb: CallbackQuery, state: FSMContext) -> None:
    if not _ok(cb):
        return
    if not _hunt.active:
        await cb.answer("Не активна", show_alert=True)
        return
    for w in _hunt.workers:
        w.stop()
    await cb.answer("Останавливаю…")


# ── Public API for main.py ────────────────────────────────────────────

async def stop_hunt(_bot: Bot) -> None:
    """Вызывается при завершении процесса (Ctrl+C / docker stop)."""
    if not _hunt.active:
        return
    for w in _hunt.workers:
        w.stop()
    if _hunt.tasks:
        try:
            await asyncio.wait_for(
                asyncio.gather(*_hunt.tasks, return_exceptions=True),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            pass
    for c in _hunt.clients:
        try:
            await c.close()
        except Exception:
            pass


def build(token: str, user_id: int) -> tuple[Bot, Dispatcher]:
    global _OWNER
    _OWNER = user_id
    bot = Bot(token)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    return bot, dp
