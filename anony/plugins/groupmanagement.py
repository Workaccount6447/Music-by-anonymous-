# ─────────────────────────────────────────────────────────────────────────────
# Group Management Plugin for AnonXMusic
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import random
import re
from datetime import datetime, timedelta

from pyrogram import filters, types, enums
from pyrogram.types import ChatPermissions
from pyrogram.errors import (
    ChatAdminRequired,
    FloodWait,
    UserAdminInvalid,
    UserNotParticipant,
)

from anony import app, config, db
from anony.helpers._admins import is_admin

# ─── URL regex ───────────────────────────────────────────────────────────────
_URL_RE = re.compile(
    r"(https?://|www\.|t\.me/)"
    r"|(\b[\w-]+\.(com|net|org|io|me|ly|co|gg|tv|xyz|info|biz|app|dev)\b)",
    re.IGNORECASE,
)

_captcha_pending: dict[int, dict[int, dict]] = {}
_flood_count: dict[int, dict[int, int]] = {}


# ═════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


async def _log(text: str) -> None:
    try:
        await app.send_message(config.LOGGER_ID, text, parse_mode=enums.ParseMode.HTML)
    except Exception:
        pass


async def _get_target(message: types.Message) -> types.User | None:
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user
    args = message.text.split(None, 1)
    if len(args) < 2:
        return None
    target_str = args[1].split()[0]
    try:
        return await app.get_users(
            int(target_str) if target_str.lstrip("-").isdigit() else target_str
        )
    except Exception:
        return None


def _mention(user: types.User) -> str:
    name = (user.first_name or "") + (" " + user.last_name if user.last_name else "")
    return f'<a href="tg://user?id={user.id}">{name.strip() or user.id}</a>'


async def _assert_admin(message: types.Message) -> bool:
    if message.from_user.id in app.sudoers:
        return True
    return await is_admin(message.chat.id, message.from_user.id)


async def _assert_bb(message: types.Message) -> bool:
    if await _assert_admin(message):
        return True
    return await db.is_auth(message.chat.id, message.from_user.id)


# ✅ FIXED
def _muted_perms() -> ChatPermissions:
    return ChatPermissions(
        can_send_messages=False,
        can_send_media_messages=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
        can_send_polls=False,
    )


# ✅ FIXED
def _default_perms() -> ChatPermissions:
    return ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_send_polls=True,
    )


def _parse_time(time_str: str) -> int:
    if not time_str:
        return 0
    unit = time_str[-1].lower()
    num = time_str[:-1]
    if not num.isdigit():
        return 0
    n = int(num)
    return {"s": n, "m": n * 60, "h": n * 3600, "d": n * 86400}.get(unit, 0)


def _warn_buttons(target_id: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup([[
        types.InlineKeyboardButton("✅ Remove Last Warn", callback_data=f"rmwarn_{target_id}"),
        types.InlineKeyboardButton("🗑 Reset All Warns", callback_data=f"resetwarn_{target_id}"),
    ]])


def _format_greeting(template: str, user: types.User, chat: types.Chat) -> str:
    full = (user.first_name or "") + (" " + user.last_name if user.last_name else "")
    mention = f'<a href="tg://user?id={user.id}">{user.first_name or user.id}</a>'
    return (
        template
        .replace("{mention}", mention)
        .replace("{first}", user.first_name or "")
        .replace("{last}", user.last_name or "")
        .replace("{title}", chat.title or "")
        .replace("{id}", str(user.id))
        .replace("{full}", full.strip())
    )


# ═════════════════════════════════════════════════════════════════════════════
# MUTE / UNMUTE / TMUTE (TESTED FIXED PART)
# ═════════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("mute") & filters.group)
async def cmd_mute(_, message: types.Message):
    if not await _assert_bb(message):
        return await message.reply_text("❌ Not allowed.")

    target = await _get_target(message)
    if not target:
        return await message.reply_text("❌ Reply to user.")

    if await is_admin(message.chat.id, target.id):
        return await message.reply_text("❌ Cannot mute admin.")

    try:
        await app.restrict_chat_member(message.chat.id, target.id, _muted_perms())
        await message.reply_text(f"🔇 {_mention(target)} muted.")
    except ChatAdminRequired:
        await message.reply_text("❌ Need restrict permission.")


@app.on_message(filters.command("unmute") & filters.group)
async def cmd_unmute(_, message: types.Message):
    if not await _assert_bb(message):
        return await message.reply_text("❌ Not allowed.")

    target = await _get_target(message)
    if not target:
        return await message.reply_text("❌ Reply to user.")

    try:
        await app.restrict_chat_member(message.chat.id, target.id, _default_perms())
        await message.reply_text(f"🔊 {_mention(target)} unmuted.")
    except ChatAdminRequired:
        await message.reply_text("❌ Need restrict permission.")


@app.on_message(filters.command("tmute") & filters.group)
async def cmd_tmute(_, message: types.Message):
    if not await _assert_bb(message):
        return await message.reply_text("❌ Not allowed.")

    target = await _get_target(message)
    if not target:
        return await message.reply_text("❌ Reply to user.")

    args = message.text.split()
    seconds = _parse_time(args[1]) if len(args) > 1 else 0

    if not seconds:
        return await message.reply_text("❌ Invalid time (10m / 2h / 1d).")

    try:
        until = datetime.utcnow() + timedelta(seconds=seconds)
        await app.restrict_chat_member(
            message.chat.id, target.id, _muted_perms(), until_date=until
        )
        await message.reply_text(f"🔇 {_mention(target)} muted temporarily.")
    except ChatAdminRequired:
        await message.reply_text("❌ Need restrict permission.")
