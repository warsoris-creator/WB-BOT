#!/usr/bin/env python3
"""
WB-BOT V2 — Ultimate Telegram Moderator Bot

Features:
  • Ban / Kick / Mute (timed) / Warn system with auto-ban
  • Anti-flood, anti-links, forbidden words (regex)
  • Per-chat settings stored in SQLite (aiosqlite)
  • Welcome messages & math captcha for new members
  • Subscription-channel check
  • Night mode (auto-delete messages at night hours)
  • Statistics & report system
  • Inline moderation panels
  • Full owner/admin permission model
"""

import asyncio
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, Filter
from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter, JOIN_TRANSITION
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()

# ─── CONFIG ────────────────────────────────────────────────────────────────────

TOKEN: str = os.getenv("BOT_TOKEN", "")
OWNER_IDS: list[int] = [int(x) for x in os.getenv("OWNER_IDS", "382254550").split(",") if x.strip()]
DB_PATH: str = os.getenv("DB_PATH", "wb_v2.db")
GLOBAL_SUB_CHANNEL: int = -1001605638152  # глобальный канал для проверки подписки

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("wb-bot")

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# In-memory flood tracker: {chat_id: {user_id: [timestamps]}}
flood_tracker: dict[int, dict[int, list[float]]] = {}
# Выбранная группа владельца в личке: {user_id: chat_id}
active_panel: dict[int, int] = {}
FLOOD_LIMIT = 5   # messages in window
FLOOD_WINDOW = 5  # seconds

# URL regex
URL_PATTERN = re.compile(
    r"(https?://|www\.|t\.me/|tg://)[^\s]+",
    re.IGNORECASE,
)


# ─── DATABASE ──────────────────────────────────────────────────────────────────

async def db_init() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id     INTEGER PRIMARY KEY,
                welcome_msg TEXT    DEFAULT 'Добро пожаловать, {name}! 👋',
                rules       TEXT    DEFAULT '',
                max_warns   INTEGER DEFAULT 3,
                anti_links  INTEGER DEFAULT 1,
                sub_check   INTEGER DEFAULT 0,
                sub_channel TEXT    DEFAULT '',
                night_mode  INTEGER DEFAULT 0,
                night_start INTEGER DEFAULT 23,
                night_end   INTEGER DEFAULT 8,
                captcha     INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS warnings (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    INTEGER,
                user_id    INTEGER,
                reason     TEXT,
                issued_by  INTEGER,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS forbidden_words (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id  INTEGER,
                word     TEXT,
                is_regex INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS mutes (
                chat_id   INTEGER,
                user_id   INTEGER,
                unmute_at TEXT,
                PRIMARY KEY (chat_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS subscribed_users (
                chat_id INTEGER,
                user_id INTEGER,
                PRIMARY KEY (chat_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS stats (
                chat_id    INTEGER,
                user_id    INTEGER,
                messages   INTEGER DEFAULT 0,
                violations INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS reports (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id      INTEGER,
                reporter_id  INTEGER,
                message_id   INTEGER,
                reported_uid INTEGER,
                reason       TEXT,
                created_at   TEXT DEFAULT (datetime('now')),
                resolved     INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS captcha_pending (
                chat_id    INTEGER,
                user_id    INTEGER,
                answer     TEXT,
                message_id INTEGER,
                PRIMARY KEY (chat_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS known_chats (
                chat_id INTEGER PRIMARY KEY,
                title   TEXT DEFAULT ''
            );
        """)
        await db.commit()


async def get_setting(chat_id: int, key: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (chat_id,)
        )
        await db.commit()
        async with db.execute(
            f"SELECT {key} FROM chat_settings WHERE chat_id=?", (chat_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def set_setting(chat_id: int, key: str, value) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (chat_id,)
        )
        await db.execute(
            f"UPDATE chat_settings SET {key}=? WHERE chat_id=?", (value, chat_id)
        )
        await db.commit()


# ─── HELPERS ───────────────────────────────────────────────────────────────────

def parse_duration(text: str) -> Optional[timedelta]:
    """Parse duration string: 30s, 10m, 2h, 1d, 1w"""
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    m = re.fullmatch(r"(\d+)([smhdw])", text.strip().lower())
    if m:
        return timedelta(seconds=int(m.group(1)) * units[m.group(2)])
    return None


def fmt_duration(td: timedelta) -> str:
    total = int(td.total_seconds())
    if total < 60:
        return f"{total}с"
    if total < 3600:
        return f"{total // 60}м"
    if total < 86400:
        return f"{total // 3600}ч"
    return f"{total // 86400}д"


def user_mention(user) -> str:
    name = (user.full_name or str(user.id)).replace("<", "&lt;").replace(">", "&gt;")
    return f'<a href="tg://user?id={user.id}">{name}</a>'


async def is_admin(chat_id: int, user_id: int) -> bool:
    if user_id in OWNER_IDS:
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except Exception as e:
        log.warning("is_admin(%s, %s) failed: %s", chat_id, user_id, e)
        return False


async def get_target(message: Message):
    if message.reply_to_message:
        return message.reply_to_message.from_user
    return None


async def safe_delete(chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id, message_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        pass


async def auto_delete(message: Message, delay: int = 15) -> None:
    await asyncio.sleep(delay)
    await safe_delete(message.chat.id, message.message_id)


async def notify(chat_id: int, text: str, delay: int = 15, **kwargs) -> Message:
    msg = await bot.send_message(chat_id, text, **kwargs)
    asyncio.create_task(auto_delete(msg, delay))
    return msg


async def get_warn_count(chat_id: int, user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM warnings WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def increment_stat(chat_id: int, user_id: int, col: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"INSERT INTO stats (chat_id, user_id, {col}) VALUES (?,?,1) "
            f"ON CONFLICT(chat_id,user_id) DO UPDATE SET {col}={col}+1",
            (chat_id, user_id),
        )
        await db.commit()


def build_mod_keyboard(target_id: int, chat_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⚠️ Варн",   callback_data=f"mod:warn:{chat_id}:{target_id}")
    kb.button(text="🔇 Мут 1ч", callback_data=f"mod:mute:{chat_id}:{target_id}")
    kb.button(text="👢 Кик",    callback_data=f"mod:kick:{chat_id}:{target_id}")
    kb.button(text="🚫 Бан",    callback_data=f"mod:ban:{chat_id}:{target_id}")
    kb.adjust(2)
    return kb.as_markup()


# ─── MODERATION CORE ───────────────────────────────────────────────────────────

async def do_warn(chat_id: int, user_id: int, issued_by: int, reason: str = "—") -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO warnings (chat_id, user_id, reason, issued_by) VALUES (?,?,?,?)",
            (chat_id, user_id, reason, issued_by),
        )
        await db.commit()
    await increment_stat(chat_id, user_id, "violations")
    return await get_warn_count(chat_id, user_id)


async def do_ban(chat_id: int, user_id: int, reason: str = "—") -> None:
    """Банит пользователя во всех известных группах."""
    chats = await get_known_chats()
    targets = {c[0] for c in chats} | {chat_id}
    for cid in targets:
        try:
            await bot.ban_chat_member(cid, user_id)
        except Exception as e:
            log.warning(f"[global ban] {user_id} in {cid}: {e}")
    log.info(f"[global] Banned {user_id} across {len(targets)} chats: {reason}")


async def do_kick(chat_id: int, user_id: int) -> None:
    await bot.ban_chat_member(chat_id, user_id)
    await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)


async def do_mute(
    chat_id: int,
    user_id: int,
    duration: Optional[timedelta] = None,
    reason: str = "—",
) -> None:
    until = datetime.now(timezone.utc) + duration if duration else None
    await bot.restrict_chat_member(
        chat_id,
        user_id,
        permissions=ChatPermissions(can_send_messages=False),
        until_date=until,
    )
    if until:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO mutes (chat_id, user_id, unmute_at) VALUES (?,?,?)",
                (chat_id, user_id, until.isoformat()),
            )
            await db.commit()
    log.info(f"[{chat_id}] Muted {user_id} until {until}: {reason}")


async def do_unmute(chat_id: int, user_id: int) -> None:
    try:
        chat = await bot.get_chat(chat_id)
        perms = chat.permissions or ChatPermissions(
            can_send_messages=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
            can_invite_users=True,
        )
    except Exception:
        perms = ChatPermissions(
            can_send_messages=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
        )
    await bot.restrict_chat_member(chat_id, user_id, permissions=perms)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM mutes WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        )
        await db.commit()


async def register_chat(chat_id: int, title: str = "") -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO known_chats (chat_id, title) VALUES (?,?)",
            (chat_id, title),
        )
        if title:
            await db.execute(
                "UPDATE known_chats SET title=? WHERE chat_id=?", (title, chat_id)
            )
        await db.commit()


async def get_known_chats() -> list[tuple[int, str]]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT chat_id, title FROM known_chats") as cur:
            return await cur.fetchall()


# ─── FILTERS ───────────────────────────────────────────────────────────────────

class IsGroupAdmin(Filter):
    async def __call__(self, message: Message) -> bool:
        if message.chat.type == ChatType.PRIVATE:
            await message.reply("Эта команда работает только в группах.")
            return False
        result = await is_admin(message.chat.id, message.from_user.id)
        if not result:
            await message.reply("⛔ Недостаточно прав.")
        return result


async def get_panel_chat_id(message: Message) -> Optional[int]:
    """
    Возвращает effective chat_id для команды.
    В группе — ID группы (если пользователь — admin).
    В личке — ID выбранной группы из active_panel (только для владельцев).
    """
    uid = message.from_user.id
    if message.chat.type == ChatType.PRIVATE:
        if uid not in OWNER_IDS:
            await message.answer("⛔ Эта команда доступна только владельцам.")
            return None
        chat_id = active_panel.get(uid)
        if not chat_id:
            await message.answer(
                "Сначала выберите группу через /panel"
            )
            return None
        return chat_id
    else:
        if not await is_admin(message.chat.id, uid):
            await message.reply("⛔ Недостаточно прав.")
            return None
        return message.chat.id


def build_panel_keyboard(chat_id: int, anti_links: int, sub_check: int,
                         captcha: int, night_mode: int) -> InlineKeyboardMarkup:
    def toggle(val: int) -> str:
        return "✅" if val else "❌"

    kb = InlineKeyboardBuilder()
    kb.button(text=f"🔗 Анти-ссылки {toggle(anti_links)}",
              callback_data=f"ptoggle:anti_links:{chat_id}")
    kb.button(text=f"📡 Подписка {toggle(sub_check)}",
              callback_data=f"ptoggle:sub_check:{chat_id}")
    kb.button(text=f"🤖 Капча {toggle(captcha)}",
              callback_data=f"ptoggle:captcha:{chat_id}")
    kb.button(text=f"🌙 Ночной режим {toggle(night_mode)}",
              callback_data=f"ptoggle:night_mode:{chat_id}")
    kb.button(text="📜 Правила", callback_data=f"pview:rules:{chat_id}")
    kb.button(text="👋 Приветствие", callback_data=f"pview:welcome:{chat_id}")
    kb.button(text="↩️ К группам", callback_data="panel:list")
    kb.adjust(2, 2, 2, 1)
    return kb.as_markup()


# ─── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    if message.chat.type == ChatType.PRIVATE:
        owner_hint = "\n\n👑 Вы — владелец. Используйте /panel для управления группами." \
            if message.from_user.id in OWNER_IDS else ""
        await message.answer(
            "👋 <b>WB-BOT V2</b> — Умный модератор для вашей группы!\n\n"
            "Добавьте меня в группу и назначьте администратором.\n"
            f"Напишите /help для списка команд.{owner_hint}"
        )
    else:
        uid = message.from_user.id
        if uid in OWNER_IDS:
            role = "👑 Владелец"
        elif await is_admin(message.chat.id, uid):
            role = "🛡️ Администратор"
        else:
            role = "👤 Пользователь"
        msg = await bot.send_message(
            message.chat.id,
            f"Привет, {user_mention(message.from_user)}! ({role})"
        )
        asyncio.create_task(auto_delete(msg, 10))
        await safe_delete(message.chat.id, message.message_id)


# ─── /help ─────────────────────────────────────────────────────────────────────

HELP_USER = """📖 <b>Команды для пользователей:</b>

/start — приветствие
/help — эта справка
/rules — правила чата
/status — мой статус
/id — узнать ID
/warnings — мои предупреждения
/report — пожаловаться (ответьте на сообщение)
/panel — панель управления группами (личка, только владельцы)""".strip()

HELP_ADMIN = """
🛡️ <b>Команды администратора:</b>

<b>Модерация</b> (ответьте на сообщение):
/ban [причина] — заблокировать
/unban [id] — разбанить
/kick [причина] — выгнать
/mute [время] [причина] — мут (1m/1h/1d/1w)
/unmute — снять мут
/warn [причина] — предупреждение
/unwarn — снять последнее предупреждение
/warnings — предупреждения пользователя
/clearwarns — сбросить все предупреждения
/info — профиль пользователя

<b>Фильтры:</b>
/add_word слово — добавить запрещённое слово (/regex/ для регулярок)
/del_word слово — удалить слово
/words — список запрещённых слов

<b>Настройки:</b>
/settings — текущие настройки
/setrules текст — установить правила
/setwelcome текст — приветствие ({name} = имя)
/setmaxwarns n — авто-бан после n предупреждений
/antilinks on|off — фильтр ссылок
/setsub @channel — канал для проверки подписки
/sub on|off — проверка подписки
/nightmode on|off [начало] [конец] — ночной режим
/captcha on|off — капча для новых участников

<b>Инфо:</b>
/stats — статистика чата
/admins — список администраторов""".strip()


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    uid = message.from_user.id
    if uid in OWNER_IDS or (
        message.chat.type != ChatType.PRIVATE
        and await is_admin(message.chat.id, uid)
    ):
        text = HELP_USER + "\n\n" + HELP_ADMIN
    else:
        text = HELP_USER

    if message.chat.type != ChatType.PRIVATE:
        await safe_delete(message.chat.id, message.message_id)
        msg = await bot.send_message(message.chat.id, text)
        asyncio.create_task(auto_delete(msg, 45))
    else:
        await message.answer(text)


# ─── /panel ────────────────────────────────────────────────────────────────────

@router.message(Command("panel"))
async def cmd_panel(message: Message) -> None:
    uid = message.from_user.id
    if uid not in OWNER_IDS:
        if message.chat.type != ChatType.PRIVATE:
            await safe_delete(message.chat.id, message.message_id)
        return
    if message.chat.type != ChatType.PRIVATE:
        await safe_delete(message.chat.id, message.message_id)
        await notify(message.chat.id, "Панель управления доступна только в личке с ботом.", 10)
        return
    chats = await get_known_chats()
    if not chats:
        await message.answer("Бот ещё не добавлен ни в одну группу.")
        return
    kb = InlineKeyboardBuilder()
    for cid, title in chats:
        kb.button(text=title or str(cid), callback_data=f"panel:select:{cid}")
    kb.adjust(1)
    await message.answer("🎛 <b>Выберите группу для управления:</b>", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("panel:"))
async def panel_callback(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    if uid not in OWNER_IDS:
        await callback.answer("Только для владельцев.", show_alert=True)
        return
    parts = callback.data.split(":")
    action = parts[1]

    if action == "list":
        chats = await get_known_chats()
        if not chats:
            await callback.answer("Нет известных групп.", show_alert=True)
            return
        kb = InlineKeyboardBuilder()
        for cid, title in chats:
            kb.button(text=title or str(cid), callback_data=f"panel:select:{cid}")
        kb.adjust(1)
        await callback.message.edit_text(
            "🎛 <b>Выберите группу для управления:</b>",
            reply_markup=kb.as_markup(),
        )
        return

    if action == "select":
        chat_id = int(parts[2])
        active_panel[uid] = chat_id
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (chat_id,))
            await db.commit()
            async with db.execute(
                "SELECT anti_links, sub_check, max_warns, night_mode, night_start, night_end, captcha "
                "FROM chat_settings WHERE chat_id=?", (chat_id,)
            ) as cur:
                row = await cur.fetchone()
        if not row:
            await callback.answer("Ошибка получения настроек.", show_alert=True)
            return
        anti_links, sub_check, max_warns, night_mode, night_start, night_end, captcha = row
        chats = await get_known_chats()
        title = next((t for c, t in chats if c == chat_id), str(chat_id))
        text = (
            f"⚙️ <b>Управление: {title}</b>\n\n"
            f"⚠️ Авто-бан после: <b>{max_warns}</b> предупреждений\n"
            f"📡 Канал подписки: <code>{GLOBAL_SUB_CHANNEL}</code>\n"
            f"🌙 Ночной режим: {night_start}:00–{night_end}:00\n\n"
            f"Используйте /setrules, /setwelcome, /setmaxwarns, /nightmode в этом чате."
        )
        await callback.message.edit_text(
            text,
            reply_markup=build_panel_keyboard(chat_id, anti_links, sub_check, captcha, night_mode),
        )
        await callback.answer()


@router.callback_query(F.data.startswith("ptoggle:"))
async def ptoggle_callback(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    if uid not in OWNER_IDS:
        await callback.answer("Только для владельцев.", show_alert=True)
        return
    _, setting, chat_id_str = callback.data.split(":")
    chat_id = int(chat_id_str)
    current = await get_setting(chat_id, setting)
    new_val = 0 if current else 1
    await set_setting(chat_id, setting, new_val)
    # Refresh panel
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT anti_links, sub_check, captcha, night_mode "
            "FROM chat_settings WHERE chat_id=?", (chat_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        await callback.answer("Ошибка.", show_alert=True)
        return
    anti_links, sub_check, captcha, night_mode = row
    await callback.message.edit_reply_markup(
        reply_markup=build_panel_keyboard(chat_id, anti_links, sub_check, captcha, night_mode)
    )
    labels = {
        "anti_links": "Анти-ссылки",
        "sub_check": "Проверка подписки",
        "captcha": "Капча",
        "night_mode": "Ночной режим",
    }
    state = "включён" if new_val else "отключён"
    await callback.answer(f"{labels.get(setting, setting)}: {state}")


@router.callback_query(F.data.startswith("pview:"))
async def pview_callback(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    if uid not in OWNER_IDS:
        await callback.answer("Только для владельцев.", show_alert=True)
        return
    _, what, chat_id_str = callback.data.split(":")
    chat_id = int(chat_id_str)
    if what == "rules":
        rules = await get_setting(chat_id, "rules")
        text = f"📜 <b>Текущие правила:</b>\n\n{rules or '(не установлены)'}\n\n" \
               f"Чтобы изменить: /setrules <текст>"
    else:
        welcome = await get_setting(chat_id, "welcome_msg")
        text = f"👋 <b>Текущее приветствие:</b>\n\n{welcome or '(не установлено)'}\n\n" \
               f"Чтобы изменить: /setwelcome <текст> (используйте {{name}})"
    await callback.answer(text[:200], show_alert=True)


# ─── /rules ────────────────────────────────────────────────────────────────────

@router.message(Command("rules"))
async def cmd_rules(message: Message) -> None:
    if message.chat.type == ChatType.PRIVATE:
        chat_id = await get_panel_chat_id(message)
        if not chat_id:
            return
        rules = await get_setting(chat_id, "rules")
        text = f"📜 <b>Правила чата:</b>\n\n{rules}" if rules else "Правила ещё не установлены."
        await message.answer(text)
        return
    rules = await get_setting(message.chat.id, "rules")
    text = f"📜 <b>Правила чата:</b>\n\n{rules}" if rules else "Правила ещё не установлены."
    await safe_delete(message.chat.id, message.message_id)
    msg = await bot.send_message(message.chat.id, text)
    asyncio.create_task(auto_delete(msg, 60))


@router.message(Command("setrules"))
async def cmd_setrules(message: Message, command: CommandObject) -> None:
    chat_id = await get_panel_chat_id(message)
    if not chat_id:
        return
    if not command.args:
        text = "Укажите текст: /setrules <текст>"
        if message.chat.type == ChatType.PRIVATE:
            await message.answer(text)
        else:
            await notify(chat_id, text, 10)
        return
    await set_setting(chat_id, "rules", command.args)
    if message.chat.type == ChatType.PRIVATE:
        await message.answer("✅ Правила обновлены.")
    else:
        await safe_delete(chat_id, message.message_id)
        await notify(chat_id, "✅ Правила обновлены.", 10)


@router.message(Command("setwelcome"))
async def cmd_setwelcome(message: Message, command: CommandObject) -> None:
    chat_id = await get_panel_chat_id(message)
    if not chat_id:
        return
    if not command.args:
        text = "Укажите текст: /setwelcome <текст> (используйте {name})"
        if message.chat.type == ChatType.PRIVATE:
            await message.answer(text)
        else:
            await notify(chat_id, text, 10)
        return
    await set_setting(chat_id, "welcome_msg", command.args)
    if message.chat.type == ChatType.PRIVATE:
        await message.answer("✅ Приветствие обновлено.")
    else:
        await safe_delete(chat_id, message.message_id)
        await notify(chat_id, "✅ Приветствие обновлено.", 10)


# ─── /status ───────────────────────────────────────────────────────────────────

@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    uid = message.from_user.id
    if uid in OWNER_IDS:
        role = "👑 Владелец"
    elif message.chat.type != ChatType.PRIVATE and await is_admin(message.chat.id, uid):
        role = "🛡️ Администратор"
    else:
        role = "👤 Пользователь"
    warns = 0
    if message.chat.type != ChatType.PRIVATE:
        warns = await get_warn_count(message.chat.id, uid)
    text = (
        f"ℹ️ <b>Статус:</b> {role}\n"
        f"🆔 <b>ID:</b> <code>{uid}</code>\n"
        f"⚠️ <b>Предупреждений:</b> {warns}"
    )
    if message.chat.type != ChatType.PRIVATE:
        await safe_delete(message.chat.id, message.message_id)
        await notify(message.chat.id, text, 15)
    else:
        await message.answer(text)


# ─── /id ───────────────────────────────────────────────────────────────────────

@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    if message.reply_to_message:
        u = message.reply_to_message.from_user
        text = f"🆔 {user_mention(u)}: <code>{u.id}</code>"
    else:
        text = (
            f"🆔 Ваш ID: <code>{message.from_user.id}</code>\n"
            f"💬 ID чата: <code>{message.chat.id}</code>"
        )
    if message.chat.type != ChatType.PRIVATE:
        await safe_delete(message.chat.id, message.message_id)
        await notify(message.chat.id, text, 15)
    else:
        await message.answer(text)


# ─── /info ─────────────────────────────────────────────────────────────────────

@router.message(Command("info"), IsGroupAdmin())
async def cmd_info(message: Message) -> None:
    target = await get_target(message)
    if not target:
        await notify(message.chat.id, "Ответьте на сообщение пользователя.", 10)
        return
    warns = await get_warn_count(message.chat.id, target.id)
    try:
        member = await bot.get_chat_member(message.chat.id, target.id)
        status_map = {
            "creator": "👑 Создатель",
            "administrator": "🛡️ Администратор",
            "member": "👤 Участник",
            "restricted": "🔇 Ограничен",
            "left": "🚪 Покинул",
            "kicked": "🚫 Забанен",
        }
        status = status_map.get(member.status, member.status)
    except Exception:
        status = "❓ Неизвестно"
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT messages, violations FROM stats WHERE chat_id=? AND user_id=?",
            (message.chat.id, target.id),
        ) as cur:
            row = await cur.fetchone()
            msgs, viols = (row[0], row[1]) if row else (0, 0)
    max_warns = int(await get_setting(message.chat.id, "max_warns") or 3)
    text = (
        f"👤 <b>Пользователь:</b> {user_mention(target)}\n"
        f"🆔 <b>ID:</b> <code>{target.id}</code>\n"
        f"📊 <b>Статус:</b> {status}\n"
        f"⚠️ <b>Предупреждений:</b> {warns}/{max_warns}\n"
        f"💬 <b>Сообщений:</b> {msgs}\n"
        f"🚫 <b>Нарушений:</b> {viols}"
    )
    await safe_delete(message.chat.id, message.message_id)
    await notify(
        message.chat.id, text, 30,
        reply_markup=build_mod_keyboard(target.id, message.chat.id)
    )


# ─── /admins ───────────────────────────────────────────────────────────────────

@router.message(Command("admins"))
async def cmd_admins(message: Message) -> None:
    if message.chat.type == ChatType.PRIVATE:
        await message.answer("Только для групп.")
        return
    try:
        admins = await bot.get_chat_administrators(message.chat.id)
    except Exception:
        await notify(message.chat.id, "Не удалось получить список администраторов.", 10)
        return
    lines = []
    for a in admins:
        if a.user.is_bot:
            continue
        title = getattr(a, "custom_title", "") or ""
        prefix = "👑" if a.status == "creator" else "🛡️"
        lines.append(f"{prefix} {user_mention(a.user)}{' — ' + title if title else ''}")
    await safe_delete(message.chat.id, message.message_id)
    await notify(message.chat.id, "👮 <b>Администраторы:</b>\n\n" + "\n".join(lines), 30)


# ─── /stats ────────────────────────────────────────────────────────────────────

@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if message.chat.type == ChatType.PRIVATE:
        await message.answer("Только для групп.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT SUM(messages), SUM(violations) FROM stats WHERE chat_id=?",
            (message.chat.id,),
        ) as cur:
            row = await cur.fetchone()
            total_msgs = row[0] or 0
            total_viols = row[1] or 0
        async with db.execute(
            "SELECT COUNT(*) FROM warnings WHERE chat_id=?", (message.chat.id,)
        ) as cur:
            total_warns = (await cur.fetchone())[0] or 0
        async with db.execute(
            "SELECT COUNT(*) FROM reports WHERE chat_id=? AND resolved=0",
            (message.chat.id,),
        ) as cur:
            open_reports = (await cur.fetchone())[0] or 0
        async with db.execute(
            "SELECT user_id, messages FROM stats WHERE chat_id=? ORDER BY messages DESC LIMIT 5",
            (message.chat.id,),
        ) as cur:
            top = await cur.fetchall()
    top_lines = []
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    for i, (uid, cnt) in enumerate(top):
        try:
            m = await bot.get_chat_member(message.chat.id, uid)
            name = m.user.full_name
        except Exception:
            name = str(uid)
        top_lines.append(f"{medals[i]} {name} — {cnt} сообщ.")
    text = (
        f"📊 <b>Статистика чата</b>\n\n"
        f"💬 Сообщений: <b>{total_msgs}</b>\n"
        f"🚫 Нарушений: <b>{total_viols}</b>\n"
        f"⚠️ Варнов выдано: <b>{total_warns}</b>\n"
        f"📢 Открытых жалоб: <b>{open_reports}</b>\n\n"
        f"🏆 <b>Топ активных:</b>\n"
        + ("\n".join(top_lines) if top_lines else "—")
    )
    await safe_delete(message.chat.id, message.message_id)
    await notify(message.chat.id, text, 30)


# ─── /report ───────────────────────────────────────────────────────────────────

@router.message(Command("report"))
async def cmd_report(message: Message, command: CommandObject) -> None:
    if message.chat.type == ChatType.PRIVATE:
        await message.answer("Только для групп.")
        return
    if not message.reply_to_message:
        await notify(message.chat.id, "Ответьте на сообщение, которое хотите пожаловаться.", 10)
        await safe_delete(message.chat.id, message.message_id)
        return
    target = message.reply_to_message.from_user
    reason = command.args or "без причины"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO reports (chat_id, reporter_id, message_id, reported_uid, reason) "
            "VALUES (?,?,?,?,?)",
            (message.chat.id, message.from_user.id,
             message.reply_to_message.message_id, target.id, reason),
        )
        await db.commit()
    await safe_delete(message.chat.id, message.message_id)
    await notify(
        message.chat.id,
        f"📢 <b>Жалоба отправлена</b>\n"
        f"👤 На: {user_mention(target)}\n"
        f"📝 Причина: {reason}\n\n"
        f"Администраторы рассмотрят обращение.",
        20,
        reply_markup=build_mod_keyboard(target.id, message.chat.id),
    )


# ─── /ban ──────────────────────────────────────────────────────────────────────

@router.message(Command("ban"), IsGroupAdmin())
async def cmd_ban(message: Message, command: CommandObject) -> None:
    target = await get_target(message)
    if not target:
        await notify(message.chat.id, "Ответьте на сообщение пользователя.", 10)
        return
    if target.id in OWNER_IDS or await is_admin(message.chat.id, target.id):
        await notify(message.chat.id, "Нельзя забанить администратора.", 10)
        return
    reason = command.args or "нарушение правил"
    await do_ban(message.chat.id, target.id, reason)
    await safe_delete(message.chat.id, message.message_id)
    await notify(
        message.chat.id,
        f"🚫 {user_mention(target)} <b>заблокирован</b>\n📝 Причина: {reason}",
        30,
    )


@router.message(Command("unban"), IsGroupAdmin())
async def cmd_unban(message: Message, command: CommandObject) -> None:
    user_id = None
    if message.reply_to_message:
        user_id = message.reply_to_message.from_user.id
    elif command.args and command.args.strip().isdigit():
        user_id = int(command.args.strip())
    if not user_id:
        await notify(message.chat.id, "Укажите ID или ответьте на сообщение.", 10)
        return
    await bot.unban_chat_member(message.chat.id, user_id, only_if_banned=True)
    await safe_delete(message.chat.id, message.message_id)
    await notify(message.chat.id, f"✅ Пользователь <code>{user_id}</code> разбанен.", 15)


# ─── /kick ─────────────────────────────────────────────────────────────────────

@router.message(Command("kick"), IsGroupAdmin())
async def cmd_kick(message: Message, command: CommandObject) -> None:
    target = await get_target(message)
    if not target:
        await notify(message.chat.id, "Ответьте на сообщение пользователя.", 10)
        return
    if target.id in OWNER_IDS or await is_admin(message.chat.id, target.id):
        await notify(message.chat.id, "Нельзя кикнуть администратора.", 10)
        return
    reason = command.args or "нарушение правил"
    await do_kick(message.chat.id, target.id)
    await safe_delete(message.chat.id, message.message_id)
    await notify(
        message.chat.id,
        f"👢 {user_mention(target)} <b>выгнан</b>\n📝 Причина: {reason}",
        30,
    )


# ─── /mute ─────────────────────────────────────────────────────────────────────

@router.message(Command("mute"), IsGroupAdmin())
async def cmd_mute(message: Message, command: CommandObject) -> None:
    target = await get_target(message)
    if not target:
        await notify(message.chat.id, "Ответьте на сообщение пользователя.", 10)
        return
    if target.id in OWNER_IDS or await is_admin(message.chat.id, target.id):
        await notify(message.chat.id, "Нельзя замутить администратора.", 10)
        return
    args = (command.args or "").split(maxsplit=1)
    duration = parse_duration(args[0]) if args and args[0] else None
    if duration:
        reason = args[1] if len(args) > 1 else "нарушение правил"
    else:
        reason = command.args or "нарушение правил"
    await do_mute(message.chat.id, target.id, duration, reason)
    dur_text = fmt_duration(duration) if duration else "навсегда"
    await safe_delete(message.chat.id, message.message_id)
    await notify(
        message.chat.id,
        f"🔇 {user_mention(target)} <b>замучен</b> на {dur_text}\n📝 Причина: {reason}",
        30,
    )


@router.message(Command("unmute"), IsGroupAdmin())
async def cmd_unmute(message: Message) -> None:
    target = await get_target(message)
    if not target:
        await notify(message.chat.id, "Ответьте на сообщение пользователя.", 10)
        return
    await do_unmute(message.chat.id, target.id)
    await safe_delete(message.chat.id, message.message_id)
    await notify(message.chat.id, f"🔊 {user_mention(target)} <b>размучен</b>.", 15)


# ─── /warn ─────────────────────────────────────────────────────────────────────

@router.message(Command("warn"), IsGroupAdmin())
async def cmd_warn(message: Message, command: CommandObject) -> None:
    target = await get_target(message)
    if not target:
        await notify(message.chat.id, "Ответьте на сообщение пользователя.", 10)
        return
    if target.id in OWNER_IDS or await is_admin(message.chat.id, target.id):
        await notify(message.chat.id, "Нельзя варнить администратора.", 10)
        return
    reason = command.args or "нарушение правил"
    count = await do_warn(message.chat.id, target.id, message.from_user.id, reason)
    max_warns = int(await get_setting(message.chat.id, "max_warns") or 3)
    await safe_delete(message.chat.id, message.message_id)
    if count >= max_warns:
        await do_ban(message.chat.id, target.id, f"Авто-бан: {count} предупреждений")
        await notify(
            message.chat.id,
            f"🚫 {user_mention(target)} <b>забанен</b> ({count}/{max_warns} предупреждений).",
            30,
        )
    else:
        await notify(
            message.chat.id,
            f"⚠️ {user_mention(target)} — предупреждение <b>{count}/{max_warns}</b>\n📝 {reason}",
            30,
        )


@router.message(Command("unwarn"), IsGroupAdmin())
async def cmd_unwarn(message: Message) -> None:
    target = await get_target(message)
    if not target:
        await notify(message.chat.id, "Ответьте на сообщение пользователя.", 10)
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM warnings WHERE chat_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
            (message.chat.id, target.id),
        ) as cur:
            row = await cur.fetchone()
        if row:
            await db.execute("DELETE FROM warnings WHERE id=?", (row[0],))
            await db.commit()
            remaining = await get_warn_count(message.chat.id, target.id)
            await safe_delete(message.chat.id, message.message_id)
            await notify(
                message.chat.id,
                f"✅ Последнее предупреждение {user_mention(target)} снято. Осталось: {remaining}",
                15,
            )
        else:
            await notify(message.chat.id, "У этого пользователя нет предупреждений.", 10)


@router.message(Command("warnings"))
async def cmd_warnings(message: Message) -> None:
    target = await get_target(message) or message.from_user
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT reason, created_at FROM warnings "
            "WHERE chat_id=? AND user_id=? ORDER BY id DESC",
            (message.chat.id, target.id),
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        await notify(message.chat.id, f"У {user_mention(target)} нет предупреждений. ✅", 15)
        return
    max_warns = int(await get_setting(message.chat.id, "max_warns") or 3)
    lines = [f"⚠️ <b>Предупреждения {user_mention(target)} ({len(rows)}/{max_warns}):</b>\n"]
    for i, (reason, created_at) in enumerate(rows, 1):
        lines.append(f"{i}. {reason} — <code>{created_at[:10]}</code>")
    await safe_delete(message.chat.id, message.message_id)
    await notify(message.chat.id, "\n".join(lines), 30)


@router.message(Command("clearwarns"), IsGroupAdmin())
async def cmd_clearwarns(message: Message) -> None:
    target = await get_target(message)
    if not target:
        await notify(message.chat.id, "Ответьте на сообщение пользователя.", 10)
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM warnings WHERE chat_id=? AND user_id=?",
            (message.chat.id, target.id),
        )
        await db.commit()
    await safe_delete(message.chat.id, message.message_id)
    await notify(message.chat.id, f"✅ Все предупреждения {user_mention(target)} сброшены.", 15)


# ─── FORBIDDEN WORDS ───────────────────────────────────────────────────────────

@router.message(Command("add_word"), IsGroupAdmin())
async def cmd_add_word(message: Message, command: CommandObject) -> None:
    if not command.args:
        await notify(message.chat.id, "Укажите слово: /add_word <слово|/regex/>", 10)
        return
    word = command.args.strip()
    is_regex = word.startswith("/") and word.endswith("/") and len(word) > 2
    if is_regex:
        word = word[1:-1]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO forbidden_words (chat_id, word, is_regex) VALUES (?,?,?)",
            (message.chat.id, word, 1 if is_regex else 0),
        )
        await db.commit()
    await safe_delete(message.chat.id, message.message_id)
    await notify(
        message.chat.id,
        f"✅ Добавлено: <code>{word}</code>{'  (regex)' if is_regex else ''}",
        10,
    )


@router.message(Command("del_word"), IsGroupAdmin())
async def cmd_del_word(message: Message, command: CommandObject) -> None:
    if not command.args:
        await notify(message.chat.id, "Укажите слово: /del_word <слово>", 10)
        return
    word = command.args.strip()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM forbidden_words WHERE chat_id=? AND word=?",
            (message.chat.id, word),
        )
        await db.commit()
    await safe_delete(message.chat.id, message.message_id)
    await notify(message.chat.id, f"✅ Удалено: <code>{word}</code>", 10)


@router.message(Command("words"))
async def cmd_words(message: Message) -> None:
    if message.chat.type == ChatType.PRIVATE:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT word, is_regex FROM forbidden_words WHERE chat_id=?",
            (message.chat.id,),
        ) as cur:
            rows = await cur.fetchall()
    await safe_delete(message.chat.id, message.message_id)
    if not rows:
        await notify(message.chat.id, "Запрещённых слов нет.", 10)
        return
    lines = ["🚫 <b>Запрещённые слова:</b>\n"]
    for word, is_regex in rows:
        lines.append(f"• <code>{word}</code>{'  (regex)' if is_regex else ''}")
    await notify(message.chat.id, "\n".join(lines), 20)


# ─── SETTINGS ──────────────────────────────────────────────────────────────────

@router.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    chat_id = await get_panel_chat_id(message)
    if not chat_id:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (chat_id,)
        )
        await db.commit()
        async with db.execute(
            "SELECT anti_links, sub_check, max_warns, "
            "night_mode, night_start, night_end, captcha "
            "FROM chat_settings WHERE chat_id=?",
            (chat_id,),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        if message.chat.type == ChatType.PRIVATE:
            await message.answer("Ошибка получения настроек.")
        else:
            await notify(chat_id, "Ошибка получения настроек.", 10)
        return
    anti_links, sub_check, max_warns, night_mode, night_start, night_end, captcha = row
    text = (
        f"⚙️ <b>Настройки чата</b>\n\n"
        f"🔗 Фильтр ссылок: {'✅' if anti_links else '❌'}\n"
        f"📡 Проверка подписки: {'✅' if sub_check else '❌'} (канал: <code>{GLOBAL_SUB_CHANNEL}</code>)\n"
        f"⚠️ Авто-бан после: {max_warns} предупреждений\n"
        f"🌙 Ночной режим: {'✅' if night_mode else '❌'}"
        f"{f' ({night_start}:00–{night_end}:00)' if night_mode else ''}\n"
        f"🤖 Капча для новых: {'✅' if captcha else '❌'}"
    )
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(text, reply_markup=build_panel_keyboard(
            chat_id, anti_links, sub_check, captcha, night_mode))
    else:
        await safe_delete(chat_id, message.message_id)
        await notify(chat_id, text, 30)


@router.message(Command("setmaxwarns"))
async def cmd_setmaxwarns(message: Message, command: CommandObject) -> None:
    chat_id = await get_panel_chat_id(message)
    if not chat_id:
        return
    if not command.args or not command.args.strip().isdigit():
        text = "Укажите число: /setmaxwarns <n>"
        await (message.answer(text) if message.chat.type == ChatType.PRIVATE else notify(chat_id, text, 10))
        return
    n = int(command.args.strip())
    if not 1 <= n <= 20:
        text = "Допустимый диапазон: 1–20"
        await (message.answer(text) if message.chat.type == ChatType.PRIVATE else notify(chat_id, text, 10))
        return
    await set_setting(chat_id, "max_warns", n)
    text = f"✅ Авто-бан после <b>{n}</b> предупреждений."
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(text)
    else:
        await safe_delete(chat_id, message.message_id)
        await notify(chat_id, text, 10)


@router.message(Command("antilinks"))
async def cmd_antilinks(message: Message, command: CommandObject) -> None:
    chat_id = await get_panel_chat_id(message)
    if not chat_id:
        return
    if not command.args or command.args.lower() not in ("on", "off"):
        text = "Используйте: /antilinks on|off"
        await (message.answer(text) if message.chat.type == ChatType.PRIVATE else notify(chat_id, text, 10))
        return
    val = 1 if command.args.lower() == "on" else 0
    await set_setting(chat_id, "anti_links", val)
    text = f"🔗 Фильтр ссылок: {'✅ включён' if val else '❌ отключён'}."
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(text)
    else:
        await safe_delete(chat_id, message.message_id)
        await notify(chat_id, text, 10)


@router.message(Command("setsub"))
async def cmd_setsub(message: Message) -> None:
    # Канал подписки теперь глобальный — команда информирует об этом
    text = f"📡 Используется глобальный канал подписки: <code>{GLOBAL_SUB_CHANNEL}</code>\nИзменить его можно только в конфигурации бота."
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(text)
    else:
        await safe_delete(message.chat.id, message.message_id)
        await notify(message.chat.id, text, 15)


@router.message(Command("sub"))
async def cmd_sub(message: Message, command: CommandObject) -> None:
    chat_id = await get_panel_chat_id(message)
    if not chat_id:
        return
    if not command.args or command.args.lower() not in ("on", "off"):
        text = "Используйте: /sub on|off"
        await (message.answer(text) if message.chat.type == ChatType.PRIVATE else notify(chat_id, text, 10))
        return
    val = 1 if command.args.lower() == "on" else 0
    await set_setting(chat_id, "sub_check", val)
    text = f"📡 Проверка подписки: {'✅ включена' if val else '❌ отключена'}."
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(text)
    else:
        await safe_delete(chat_id, message.message_id)
        await notify(chat_id, text, 10)


@router.message(Command("nightmode"))
async def cmd_nightmode(message: Message, command: CommandObject) -> None:
    chat_id = await get_panel_chat_id(message)
    if not chat_id:
        return
    if not command.args:
        text = "Используйте: /nightmode on|off [начало] [конец]\nПример: /nightmode on 23 8"
        await (message.answer(text) if message.chat.type == ChatType.PRIVATE else notify(chat_id, text, 10))
        return
    parts = command.args.split()
    mode = parts[0].lower()
    if mode not in ("on", "off"):
        text = "Используйте on или off."
        await (message.answer(text) if message.chat.type == ChatType.PRIVATE else notify(chat_id, text, 10))
        return
    val = 1 if mode == "on" else 0
    await set_setting(chat_id, "night_mode", val)
    if val and len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
        await set_setting(chat_id, "night_start", int(parts[1]))
        await set_setting(chat_id, "night_end", int(parts[2]))
    text = f"🌙 Ночной режим: {'✅ включён' if val else '❌ отключён'}."
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(text)
    else:
        await safe_delete(chat_id, message.message_id)
        await notify(chat_id, text, 10)


@router.message(Command("captcha"))
async def cmd_captcha(message: Message, command: CommandObject) -> None:
    chat_id = await get_panel_chat_id(message)
    if not chat_id:
        return
    if not command.args or command.args.lower() not in ("on", "off"):
        text = "Используйте: /captcha on|off"
        await (message.answer(text) if message.chat.type == ChatType.PRIVATE else notify(chat_id, text, 10))
        return
    val = 1 if command.args.lower() == "on" else 0
    await set_setting(chat_id, "captcha", val)
    text = f"🤖 Капча: {'✅ включена' if val else '❌ отключена'}."
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(text)
    else:
        await safe_delete(chat_id, message.message_id)
        await notify(chat_id, text, 10)


# ─── NEW MEMBER / CAPTCHA ──────────────────────────────────────────────────────

@router.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_new_member(event: ChatMemberUpdated) -> None:
    chat_id = event.chat.id
    user = event.new_chat_member.user
    if user.is_bot:
        return

    await register_chat(chat_id, event.chat.title or "")

    captcha_enabled = await get_setting(chat_id, "captcha")
    welcome_msg = await get_setting(chat_id, "welcome_msg") or "Добро пожаловать, {name}! 👋"

    if captcha_enabled:
        a, b = random.randint(1, 10), random.randint(1, 10)
        answer = str(a + b)
        try:
            await do_mute(chat_id, user.id, timedelta(minutes=5))
        except Exception:
            pass
        options = list({answer, str(a + b + 1), str(max(1, abs(a - b))), str((a * b) % 10 + 1)})
        random.shuffle(options)
        kb = InlineKeyboardBuilder()
        for opt in options[:4]:
            kb.button(text=opt, callback_data=f"captcha:{chat_id}:{user.id}:{answer}:{opt}")
        kb.adjust(2)
        msg = await bot.send_message(
            chat_id,
            f"👋 {user_mention(user)}, добро пожаловать!\n\n"
            f"🤖 <b>Подтверди, что ты не бот:</b>\n"
            f"Сколько будет <b>{a} + {b}</b>?\n\n"
            f"⏱ У тебя 5 минут. При неверном ответе — кик.",
            reply_markup=kb.as_markup(),
        )
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO captcha_pending "
                "(chat_id, user_id, answer, message_id) VALUES (?,?,?,?)",
                (chat_id, user.id, answer, msg.message_id),
            )
            await db.commit()

        async def captcha_timeout() -> None:
            await asyncio.sleep(300)
            async with aiosqlite.connect(DB_PATH) as db2:
                async with db2.execute(
                    "SELECT 1 FROM captcha_pending WHERE chat_id=? AND user_id=?",
                    (chat_id, user.id),
                ) as cur:
                    still_pending = await cur.fetchone()
            if still_pending:
                try:
                    await do_kick(chat_id, user.id)
                    await safe_delete(chat_id, msg.message_id)
                    await notify(
                        chat_id,
                        f"👢 {user_mention(user)} не прошёл капчу и был кикнут.",
                        15,
                    )
                except Exception:
                    pass
                async with aiosqlite.connect(DB_PATH) as db2:
                    await db2.execute(
                        "DELETE FROM captcha_pending WHERE chat_id=? AND user_id=?",
                        (chat_id, user.id),
                    )
                    await db2.commit()

        asyncio.create_task(captcha_timeout())
    else:
        name = user.full_name or str(user.id)
        text = welcome_msg.replace("{name}", user_mention(user))
        rules = await get_setting(chat_id, "rules")
        if rules:
            text += "\n\n📜 Ознакомься с /rules"
        await bot.send_message(chat_id, text)


@router.callback_query(F.data.startswith("captcha:"))
async def captcha_callback(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    _, chat_id, user_id, correct, chosen = parts
    chat_id, user_id = int(chat_id), int(user_id)
    if callback.from_user.id != user_id:
        await callback.answer("Это не ваша капча!", show_alert=True)
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM captcha_pending WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ) as cur:
            pending = await cur.fetchone()
    if not pending:
        await callback.answer("Капча уже решена или истекла.")
        return
    if chosen == correct:
        await do_unmute(chat_id, user_id)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM captcha_pending WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            )
            await db.commit()
        await callback.message.delete()
        welcome_msg = await get_setting(chat_id, "welcome_msg") or "Добро пожаловать, {name}! 👋"
        user = callback.from_user
        text = welcome_msg.replace("{name}", user_mention(user))
        await bot.send_message(chat_id, f"✅ {text}")
        await callback.answer("Добро пожаловать! ✅")
    else:
        await callback.answer("❌ Неверно! Попробуй ещё раз.", show_alert=True)


# ─── INLINE MOD PANEL ──────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("mod:"))
async def mod_callback(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    _, action, chat_id, target_id = parts
    chat_id, target_id = int(chat_id), int(target_id)
    if not await is_admin(chat_id, callback.from_user.id):
        await callback.answer("Только администраторы.", show_alert=True)
        return
    try:
        member = await bot.get_chat_member(chat_id, target_id)
        target = member.user
    except Exception:
        await callback.answer("Пользователь не найден.", show_alert=True)
        return
    if action == "warn":
        count = await do_warn(chat_id, target_id, callback.from_user.id, "через панель")
        max_warns = int(await get_setting(chat_id, "max_warns") or 3)
        if count >= max_warns:
            await do_ban(chat_id, target_id, f"Авто-бан: {count} предупреждений")
            await callback.answer(f"🚫 Забанен ({count}/{max_warns} варнов)!")
        else:
            await callback.answer(f"⚠️ Варн выдан ({count}/{max_warns})")
    elif action == "mute":
        await do_mute(chat_id, target_id, timedelta(hours=1), "через панель")
        await callback.answer("🔇 Замучен на 1 час")
    elif action == "kick":
        await do_kick(chat_id, target_id)
        await callback.answer("👢 Кикнут")
    elif action == "ban":
        await do_ban(chat_id, target_id, "через панель")
        await callback.answer("🚫 Забанен")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await notify(
        chat_id,
        f"🛡️ {user_mention(callback.from_user)} применил <b>{action}</b> к {user_mention(target)}",
        20,
    )


# ─── MESSAGE FILTER ────────────────────────────────────────────────────────────

@router.message(F.text, F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def filter_messages(message: Message) -> None:
    if not message.from_user or message.from_user.is_bot:
        return
    uid = message.from_user.id
    chat_id = message.chat.id

    await register_chat(chat_id, message.chat.title or "")

    # Admins are exempt from all filters
    if await is_admin(chat_id, uid):
        await increment_stat(chat_id, uid, "messages")
        return

    await increment_stat(chat_id, uid, "messages")

    # ── Flood check ────────────────────────────────────────────────────────────
    now = time.time()
    flood_tracker.setdefault(chat_id, {}).setdefault(uid, [])
    flood_tracker[chat_id][uid] = [t for t in flood_tracker[chat_id][uid] if now - t < FLOOD_WINDOW]
    flood_tracker[chat_id][uid].append(now)
    if len(flood_tracker[chat_id][uid]) > FLOOD_LIMIT:
        await safe_delete(chat_id, message.message_id)
        await do_mute(chat_id, uid, timedelta(minutes=5), "флуд")
        await increment_stat(chat_id, uid, "violations")
        await notify(
            chat_id,
            f"🔇 {user_mention(message.from_user)} замучен на 5 минут за флуд.",
            15,
        )
        return

    # ── Night mode ─────────────────────────────────────────────────────────────
    night_mode = await get_setting(chat_id, "night_mode")
    if night_mode:
        night_start = int(await get_setting(chat_id, "night_start") or 23)
        night_end = int(await get_setting(chat_id, "night_end") or 8)
        hour = datetime.now().hour
        is_night = (
            (night_start > night_end and (hour >= night_start or hour < night_end))
            or (night_start <= night_end and night_start <= hour < night_end)
        )
        if is_night:
            await safe_delete(chat_id, message.message_id)
            await notify(
                chat_id,
                f"🌙 {user_mention(message.from_user)}, ночной режим активен "
                f"({night_start}:00–{night_end}:00).",
                10,
            )
            return

    # ── Anti-links ─────────────────────────────────────────────────────────────
    anti_links = await get_setting(chat_id, "anti_links")
    if anti_links:
        has_link = bool(URL_PATTERN.search(message.text or ""))
        if not has_link:
            for e in (message.entities or []):
                if e.type in ("url", "text_link"):
                    has_link = True
                    break
        if has_link:
            await safe_delete(chat_id, message.message_id)
            await increment_stat(chat_id, uid, "violations")
            await notify(
                chat_id,
                f"🔗 {user_mention(message.from_user)}, ссылки запрещены.\n"
                f"Для рекламы обратитесь к администратору.",
                15,
            )
            return

    # ── Forbidden words ────────────────────────────────────────────────────────
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT word, is_regex FROM forbidden_words WHERE chat_id=?", (chat_id,)
        ) as cur:
            words = await cur.fetchall()
    text_lower = (message.text or "").lower()
    for word, is_regex in words:
        matched = False
        if is_regex:
            try:
                matched = bool(re.search(word, message.text or "", re.IGNORECASE))
            except re.error:
                pass
        else:
            matched = word.lower() in text_lower
        if matched:
            await safe_delete(chat_id, message.message_id)
            await increment_stat(chat_id, uid, "violations")
            count = await do_warn(chat_id, uid, 0, f"запрещённое слово")
            max_warns = int(await get_setting(chat_id, "max_warns") or 3)
            if count >= max_warns:
                await do_ban(chat_id, uid, f"Авто-бан: {count} предупреждений")
                await notify(
                    chat_id,
                    f"🚫 {user_mention(message.from_user)} забанен за систематическое нарушение.",
                    30,
                )
            else:
                await notify(
                    chat_id,
                    f"⚠️ {user_mention(message.from_user)}, сообщение удалено. "
                    f"Предупреждение {count}/{max_warns}.",
                    15,
                )
            return

    # ── Subscription check ─────────────────────────────────────────────────────
    sub_check = await get_setting(chat_id, "sub_check")
    if sub_check:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT 1 FROM subscribed_users WHERE chat_id=? AND user_id=?",
                (chat_id, uid),
            ) as cur:
                already_subbed = await cur.fetchone()
        if not already_subbed:
            try:
                member = await bot.get_chat_member(GLOBAL_SUB_CHANNEL, uid)
                if member.status not in ("left", "kicked"):
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute(
                            "INSERT OR IGNORE INTO subscribed_users (chat_id, user_id) VALUES (?,?)",
                            (chat_id, uid),
                        )
                        await db.commit()
                else:
                    await safe_delete(chat_id, message.message_id)
                    await notify(
                        chat_id,
                        f"📡 {user_mention(message.from_user)}, подпишитесь на наш канал "
                        f"для участия в чате!",
                        20,
                    )
                    return
            except Exception:
                pass


# ─── SCHEDULED TASKS ───────────────────────────────────────────────────────────

async def unmute_scheduler() -> None:
    """Periodically remove expired mutes."""
    while True:
        await asyncio.sleep(60)
        try:
            now = datetime.now(timezone.utc).isoformat()
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT chat_id, user_id FROM mutes WHERE unmute_at < ?", (now,)
                ) as cur:
                    expired = await cur.fetchall()
            for chat_id, user_id in expired:
                try:
                    await do_unmute(chat_id, user_id)
                    log.info(f"Auto-unmuted {user_id} in {chat_id}")
                except Exception as e:
                    log.warning(f"Auto-unmute failed {chat_id}/{user_id}: {e}")
        except Exception as e:
            log.error(f"unmute_scheduler error: {e}")


# ─── STARTUP / MAIN ────────────────────────────────────────────────────────────

async def on_startup() -> None:
    await db_init()
    asyncio.create_task(unmute_scheduler())
    me = await bot.get_me()
    log.info(f"✅ WB-BOT V2 запущен: @{me.username}")


async def main() -> None:
    dp.startup.register(on_startup)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
