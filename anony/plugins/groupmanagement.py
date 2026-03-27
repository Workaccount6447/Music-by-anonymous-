# ─────────────────────────────────────────────────────────────────────────────
# Group Management Plugin for AnonXMusic
# Author: Built on top of AnonXMusic by AnonymousX1025
#
# Permission levels:
#   aa  = Group Admins  (Telegram admin / owner)
#   bb  = Bot admins    (aa + users authorised via /auth command)
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import random
import re
import time
from datetime import datetime, timedelta

from pyrogram import enums, filters, types
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

# ─── In-memory captcha store: {chat_id: {user_id: {"answer": int, "msg_id": int}}}
_captcha_pending: dict[int, dict[int, dict]] = {}

# ─── Flood tracker (in-memory, per chat) ─────────────────────────────────────
_flood_count: dict[int, dict[int, int]] = {}


# ═════════════════════════════════════════════════════════════════════════════
#  HELPERS
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


def _muted_perms() -> types.ChatPermissions:
    return types.ChatPermissions(
        can_send_messages=False,
        can_send_media_messages=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
        can_send_polls=False,
    )


def _default_perms() -> types.ChatPermissions:
    return types.ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_send_polls=True,
    )


def _parse_time(time_str: str) -> int:
    """Convert '10m', '2h', '1d' to seconds. Returns 0 on failure."""
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


def _ban_buttons(chat_id: int, target_id: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup([[
        types.InlineKeyboardButton("🔓 Unban", callback_data=f"unban_{chat_id}_{target_id}"),
    ]])


def _mute_buttons(chat_id: int, target_id: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup([[
        types.InlineKeyboardButton("🔊 Unmute", callback_data=f"unmute_{chat_id}_{target_id}"),
    ]])


def _lock_buttons(chat_id: int, lock_type: str) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup([[
        types.InlineKeyboardButton(f"🔓 Unlock {lock_type}", callback_data=f"unlock_{chat_id}_{lock_type}"),
    ]])


def _promote_buttons(chat_id: int, target_id: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup([[
        types.InlineKeyboardButton("⬇️ Demote", callback_data=f"demote_{chat_id}_{target_id}"),
    ]])


def _pin_buttons(chat_id: int, msg_id: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup([[
        types.InlineKeyboardButton("📌 Unpin", callback_data=f"unpin_{chat_id}_{msg_id}"),
    ]])


async def _auto_delete(msg: types.Message, delay: int = 8) -> None:
    """Delete a message after delay seconds — run as asyncio.create_task()."""
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass


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
#  WELCOME / GOODBYE
# ═════════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("setwelcome") & filters.group)
async def cmd_set_welcome(_, message: types.Message):
    """aa: /setwelcome <text> — reply to a photo to set custom welcome image."""
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins can set the welcome message.")

    args = message.text.split(None, 1)
    if len(args) < 2:
        return await message.reply_text(
            "📝 <b>Usage:</b>\n"
            "<code>/setwelcome Hello {mention}! Welcome to {title}.</code>\n\n"
            "Variables: <code>{mention}</code> <code>{first}</code> <code>{last}</code> "
            "<code>{title}</code> <code>{id}</code>\n\n"
            "Auto-delete timer: <code>/setwelcome 60 Hello {mention}!</code>\n"
            "Custom photo: reply to a photo while running this command."
        )

    parts = args[1].split(None, 1)
    delete_after = 0
    if parts[0].isdigit():
        delete_after = int(parts[0])
        text = parts[1] if len(parts) > 1 else ""
    else:
        text = args[1]

    if not text:
        return await message.reply_text("❌ Please provide the welcome message text.")

    photo_id = None
    if message.reply_to_message and message.reply_to_message.photo:
        photo_id = message.reply_to_message.photo.file_id

    await db.set_welcome(message.chat.id, text, delete_after, photo_id)

    resp = "✅ Welcome message saved."
    if delete_after:
        resp += f"\n⏳ Auto-delete after <b>{delete_after}s</b>."
    resp += f"\n🖼 Photo: {'custom' if photo_id else 'default from config'}."
    await message.reply_text(resp)
    await _log(
        f"<b>🟢 Welcome Set</b>\n"
        f"Chat: <b>{message.chat.title}</b> (<code>{message.chat.id}</code>)\n"
        f"By: {_mention(message.from_user)}\nDelete after: {delete_after}s\n"
        f"Text: <code>{text[:200]}</code>\nTime: {_now()}"
    )


@app.on_message(filters.command("delwelcome") & filters.group)
async def cmd_del_welcome(_, message: types.Message):
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins can remove the welcome message.")
    await db.del_welcome(message.chat.id)
    await message.reply_text("✅ Welcome message removed. Default config message will be used.")
    await _log(
        f"<b>🗑 Welcome Removed</b>\n"
        f"Chat: <b>{message.chat.title}</b> (<code>{message.chat.id}</code>)\n"
        f"By: {_mention(message.from_user)}\nTime: {_now()}"
    )


@app.on_message(filters.command("setgoodbye") & filters.group)
async def cmd_set_goodbye(_, message: types.Message):
    """aa: /setgoodbye <text> — reply to a photo to set custom goodbye image."""
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins can set the goodbye message.")

    args = message.text.split(None, 1)
    if len(args) < 2:
        return await message.reply_text(
            "📝 <b>Usage:</b> <code>/setgoodbye Goodbye {first}! We'll miss you.</code>\n\n"
            "Variables: <code>{mention}</code> <code>{first}</code> <code>{last}</code> "
            "<code>{title}</code> <code>{id}</code>\n"
            "Reply to a photo to set a custom goodbye image."
        )

    photo_id = None
    if message.reply_to_message and message.reply_to_message.photo:
        photo_id = message.reply_to_message.photo.file_id

    await db.set_goodbye(message.chat.id, args[1], photo_id)
    resp = "✅ Goodbye message saved."
    resp += f"\n🖼 Photo: {'custom' if photo_id else 'default from config'}."
    await message.reply_text(resp)
    await _log(
        f"<b>🔴 Goodbye Set</b>\n"
        f"Chat: <b>{message.chat.title}</b> (<code>{message.chat.id}</code>)\n"
        f"By: {_mention(message.from_user)}\n"
        f"Text: <code>{args[1][:200]}</code>\nTime: {_now()}"
    )


@app.on_message(filters.command("delgoodbye") & filters.group)
async def cmd_del_goodbye(_, message: types.Message):
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins can remove the goodbye message.")
    await db.del_goodbye(message.chat.id)
    await message.reply_text("✅ Goodbye message removed.")


@app.on_chat_member_updated(filters.group)
async def on_member_update(_, update: types.ChatMemberUpdated):
    chat = update.chat
    if not update.new_chat_member and not update.old_chat_member:
        return

    old_status = getattr(update.old_chat_member, "status", None)
    new_status = getattr(update.new_chat_member, "status", None)
    user = update.new_chat_member.user if update.new_chat_member else update.old_chat_member.user

    # ── User joined ──────────────────────────────────────────────────────────
    joined = (
        new_status == enums.ChatMemberStatus.MEMBER
        and old_status not in (
            enums.ChatMemberStatus.MEMBER,
            enums.ChatMemberStatus.ADMINISTRATOR,
            enums.ChatMemberStatus.OWNER,
        )
    )
    if joined:
        # Trigger captcha — run as background task so it doesn't block this handler
        if await db.get_captcha(chat.id):
            asyncio.create_task(_send_captcha(chat, user))

        data = await db.get_welcome(chat.id)
        raw_text = data.get("text") or config.WELCOME_TEXT
        photo = data.get("photo") or config.WELCOME_PHOTO
        text = _format_greeting(raw_text, user, chat)

        try:
            sent = await app.send_photo(chat.id, photo=photo, caption=text)
        except Exception:
            sent = await app.send_message(chat.id, text)

        delete_after = data.get("delete_after", 0)
        if delete_after:
            asyncio.create_task(_auto_delete(sent, delete_after))
        return

    # ── User left / kicked ───────────────────────────────────────────────────
    left = (
        new_status in (enums.ChatMemberStatus.LEFT, enums.ChatMemberStatus.BANNED)
        and old_status in (
            enums.ChatMemberStatus.MEMBER,
            enums.ChatMemberStatus.ADMINISTRATOR,
            enums.ChatMemberStatus.OWNER,
        )
    )
    if left:
        data = await db.get_goodbye(chat.id)
        raw_text = data.get("text") or config.GOODBYE_TEXT
        photo = data.get("photo") or config.WELCOME_PHOTO
        text = _format_greeting(raw_text, user, chat)
        try:
            await app.send_photo(chat.id, photo=photo, caption=text)
        except Exception:
            await app.send_message(chat.id, text)


# ═════════════════════════════════════════════════════════════════════════════
#  BAN / UNBAN
# ═════════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("ban") & filters.group)
async def cmd_ban(_, message: types.Message):
    """bb: Ban a user from the group."""
    if not await _assert_bb(message):
        return await message.reply_text("❌ You need to be an admin or authorised user to ban.")

    target = await _get_target(message)
    if not target:
        return await message.reply_text("❌ Reply to a user or provide @username / user_id.")

    if await is_admin(message.chat.id, target.id):
        return await message.reply_text("❌ Cannot ban an admin.")

    reason = ""
    args = message.text.split(None, 2)
    if len(args) > 2 and not message.reply_to_message:
        reason = args[2]
    elif len(args) > 1 and message.reply_to_message:
        reason = args[1]

    try:
        await app.ban_chat_member(message.chat.id, target.id)
        resp = f"🚫 {_mention(target)} has been <b>banned</b>."
        if reason:
            resp += f"\n📋 Reason: {reason}"
        await message.reply_text(resp, reply_markup=_ban_buttons(message.chat.id, target.id))
        await _log(
            f"<b>🚫 User Banned</b>\nChat: <b>{message.chat.title}</b> (<code>{message.chat.id}</code>)\n"
            f"User: {_mention(target)} (<code>{target.id}</code>)\nBy: {_mention(message.from_user)}\n"
            f"Reason: {reason or 'None'}\nTime: {_now()}"
        )
    except ChatAdminRequired:
        await message.reply_text("❌ I need ban permissions to do this.")
    except UserAdminInvalid:
        await message.reply_text("❌ Cannot ban that user.")


@app.on_message(filters.command("unban") & filters.group)
async def cmd_unban(_, message: types.Message):
    """bb: Unban a user."""
    if not await _assert_bb(message):
        return await message.reply_text("❌ You need to be an admin or authorised user to unban.")

    target = await _get_target(message)
    if not target:
        return await message.reply_text("❌ Reply to a user or provide @username / user_id.")

    try:
        await app.unban_chat_member(message.chat.id, target.id)
        await message.reply_text(f"✅ {_mention(target)} has been <b>unbanned</b>.")
        await _log(
            f"<b>✅ User Unbanned</b>\nChat: <b>{message.chat.title}</b> (<code>{message.chat.id}</code>)\n"
            f"User: {_mention(target)} (<code>{target.id}</code>)\nBy: {_mention(message.from_user)}\n"
            f"Time: {_now()}"
        )
    except ChatAdminRequired:
        await message.reply_text("❌ I need ban permissions to do this.")


# ═════════════════════════════════════════════════════════════════════════════
#  KICK
# ═════════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("kick") & filters.group)
async def cmd_kick(_, message: types.Message):
    """bb: Kick (remove) a user — they can rejoin via invite link."""
    if not await _assert_bb(message):
        return await message.reply_text("❌ You need to be an admin or authorised user to kick.")

    target = await _get_target(message)
    if not target:
        return await message.reply_text("❌ Reply to a user or provide @username / user_id.")

    if await is_admin(message.chat.id, target.id):
        return await message.reply_text("❌ Cannot kick an admin.")

    reason = ""
    args = message.text.split(None, 2)
    if len(args) > 2 and not message.reply_to_message:
        reason = args[2]
    elif len(args) > 1 and message.reply_to_message:
        reason = args[1]

    try:
        await app.ban_chat_member(message.chat.id, target.id)
        await app.unban_chat_member(message.chat.id, target.id)
        resp = f"👢 {_mention(target)} has been <b>kicked</b>."
        if reason:
            resp += f"\n📋 Reason: {reason}"
        # Try to attach a re-invite link button
        try:
            link = await app.export_chat_invite_link(message.chat.id)
            await message.reply_text(resp, reply_markup=types.InlineKeyboardMarkup([[
                types.InlineKeyboardButton("🔗 Re-invite Link", url=link)
            ]]))
        except Exception:
            await message.reply_text(resp)
        await _log(
            f"<b>👢 User Kicked</b>\nChat: <b>{message.chat.title}</b> (<code>{message.chat.id}</code>)\n"
            f"User: {_mention(target)} (<code>{target.id}</code>)\nBy: {_mention(message.from_user)}\n"
            f"Reason: {reason or 'None'}\nTime: {_now()}"
        )
    except ChatAdminRequired:
        await message.reply_text("❌ I need kick permissions.")


# ═════════════════════════════════════════════════════════════════════════════
#  MUTE / UNMUTE / TMUTE
# ═════════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("mute") & filters.group)
async def cmd_mute(_, message: types.Message):
    """bb: Mute a user (restrict all messages)."""
    if not await _assert_bb(message):
        return await message.reply_text("❌ You need to be an admin or authorised user to mute.")

    target = await _get_target(message)
    if not target:
        return await message.reply_text("❌ Reply to a user or provide @username / user_id.")

    if await is_admin(message.chat.id, target.id):
        return await message.reply_text("❌ Cannot mute an admin.")

    try:
        await app.restrict_chat_member(message.chat.id, target.id, _muted_perms())
        await db.mute_user(message.chat.id, target.id)
        await message.reply_text(
            f"🔇 {_mention(target)} has been <b>muted</b>.",
            reply_markup=_mute_buttons(message.chat.id, target.id),
        )
        await _log(
            f"<b>🔇 User Muted</b>\nChat: <b>{message.chat.title}</b> (<code>{message.chat.id}</code>)\n"
            f"User: {_mention(target)} (<code>{target.id}</code>)\nBy: {_mention(message.from_user)}\n"
            f"Time: {_now()}"
        )
    except ChatAdminRequired:
        await message.reply_text("❌ I need restrict permissions. Make sure I'm admin with 'Restrict Members'.")
    except Exception as e:
        await message.reply_text(f"❌ Failed: {e}")


@app.on_message(filters.command("unmute") & filters.group)
async def cmd_unmute(_, message: types.Message):
    """bb: Unmute a user."""
    if not await _assert_bb(message):
        return await message.reply_text("❌ You need to be an admin or authorised user to unmute.")

    target = await _get_target(message)
    if not target:
        return await message.reply_text("❌ Reply to a user or provide @username / user_id.")

    try:
        # Try to get chat's default permissions, fall back to full perms
        try:
            chat_obj = await app.get_chat(message.chat.id)
            default_perms = chat_obj.permissions or _default_perms()
        except Exception:
            default_perms = _default_perms()

        await app.restrict_chat_member(message.chat.id, target.id, default_perms)
        await db.unmute_user(message.chat.id, target.id)
        await message.reply_text(f"🔊 {_mention(target)} has been <b>unmuted</b>.")
        await _log(
            f"<b>🔊 User Unmuted</b>\nChat: <b>{message.chat.title}</b> (<code>{message.chat.id}</code>)\n"
            f"User: {_mention(target)} (<code>{target.id}</code>)\nBy: {_mention(message.from_user)}\n"
            f"Time: {_now()}"
        )
    except ChatAdminRequired:
        await message.reply_text("❌ I need restrict permissions.")
    except Exception as e:
        await message.reply_text(f"❌ Failed: {e}")


@app.on_message(filters.command("tmute") & filters.group)
async def cmd_tmute(_, message: types.Message):
    """bb: /tmute <time> [@user] — Temp mute. Time: 10m / 2h / 1d"""
    if not await _assert_bb(message):
        return await message.reply_text("❌ Admins/authorised users only.")

    target = await _get_target(message)
    if not target:
        return await message.reply_text("❌ Reply to a user or provide @username / user_id.")

    if await is_admin(message.chat.id, target.id):
        return await message.reply_text("❌ Cannot mute an admin.")

    # Find time arg from any position in the command
    args = message.text.split()
    time_str = ""
    for a in args[1:]:
        if _parse_time(a):
            time_str = a
            break
    seconds = _parse_time(time_str)
    if not seconds:
        return await message.reply_text(
            "❌ Invalid or missing time.\nExamples: <code>/tmute 10m</code>, <code>/tmute 2h</code>, <code>/tmute 1d</code>"
        )

    try:
        until = datetime.utcnow() + timedelta(seconds=seconds)
        await app.restrict_chat_member(
            message.chat.id, target.id, _muted_perms(), until_date=until
        )
        await message.reply_text(
            f"🔇 {_mention(target)} muted for <b>{time_str}</b>.",
            reply_markup=_mute_buttons(message.chat.id, target.id),
        )
        await _log(
            f"<b>🔇 Temp-Muted</b>\nChat: <b>{message.chat.title}</b>\n"
            f"User: {_mention(target)} (<code>{target.id}</code>)\n"
            f"Duration: {time_str}\nBy: {_mention(message.from_user)}\nTime: {_now()}"
        )
    except ChatAdminRequired:
        await message.reply_text("❌ I need restrict permissions.")
    except Exception as e:
        await message.reply_text(f"❌ Failed: {e}")


# ═════════════════════════════════════════════════════════════════════════════
#  DELETE / PURGE / DELALL
# ═════════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("del") & filters.group)
async def cmd_delete(_, message: types.Message):
    """bb: Delete the replied-to message."""
    if not await _assert_bb(message):
        return await message.reply_text("❌ Admins/authorised users only.")

    if not message.reply_to_message:
        return await message.reply_text("❌ Reply to a message to delete it.")

    try:
        await message.reply_to_message.delete()
        await message.delete()
    except Exception:
        await message.reply_text("❌ Couldn't delete the message. Check my permissions.")


@app.on_message(filters.command("purge") & filters.group)
async def cmd_purge(_, message: types.Message):
    """bb: Delete all messages from replied message up to this one."""
    if not await _assert_bb(message):
        return await message.reply_text("❌ Admins/authorised users only.")

    if not message.reply_to_message:
        return await message.reply_text("❌ Reply to the message you want to start purging from.")

    start_id = message.reply_to_message.id
    end_id = message.id
    ids = list(range(start_id, end_id + 1))

    deleted = 0
    for i in range(0, len(ids), 100):
        try:
            await app.delete_messages(message.chat.id, ids[i:i + 100])
            deleted += len(ids[i:i + 100])
        except Exception:
            pass

    sent = await app.send_message(message.chat.id, f"🗑 Purged <b>{deleted}</b> messages.")
    asyncio.create_task(_auto_delete(sent, 5))
    await _log(
        f"<b>🗑 Purge</b>\nChat: <b>{message.chat.title}</b> (<code>{message.chat.id}</code>)\n"
        f"Deleted: {deleted} messages\nBy: {_mention(message.from_user)}\nTime: {_now()}"
    )


@app.on_message(filters.command("delall") & filters.group)
async def cmd_delall(_, message: types.Message):
    """bb: Delete all messages from a user by iterating history (bot-compatible)."""
    if not await _assert_bb(message):
        return await message.reply_text("❌ Admins/authorised users only.")

    target = await _get_target(message)
    if not target:
        return await message.reply_text("❌ Reply to a user or provide @username.")

    if await is_admin(message.chat.id, target.id):
        return await message.reply_text("❌ Cannot delete messages of an admin.")

    sent = await message.reply_text(f"🗑 Deleting messages from {_mention(target)}... please wait.")

    # Bots cannot use channels.DeleteParticipantHistory, so we iterate history
    deleted = 0
    ids_to_delete = []
    try:
        async for msg in app.get_chat_history(message.chat.id, limit=3000):
            if msg.from_user and msg.from_user.id == target.id:
                ids_to_delete.append(msg.id)
            if len(ids_to_delete) >= 100:
                await app.delete_messages(message.chat.id, ids_to_delete)
                deleted += len(ids_to_delete)
                ids_to_delete = []
                await asyncio.sleep(0.3)

        if ids_to_delete:
            await app.delete_messages(message.chat.id, ids_to_delete)
            deleted += len(ids_to_delete)

    except ChatAdminRequired:
        return await sent.edit_text("❌ I need 'Delete Messages' admin permission.")
    except Exception as e:
        return await sent.edit_text(f"❌ Error: {e}")

    await sent.edit_text(f"✅ Deleted <b>{deleted}</b> messages from {_mention(target)}.")
    await _log(
        f"<b>🗑 Delete All Messages</b>\nChat: <b>{message.chat.title}</b> (<code>{message.chat.id}</code>)\n"
        f"User: {_mention(target)} (<code>{target.id}</code>)\n"
        f"Deleted: {deleted} msgs\nBy: {_mention(message.from_user)}\nTime: {_now()}"
    )


# ═════════════════════════════════════════════════════════════════════════════
#  WARNINGS
# ═════════════════════════════════════════════════════════════════════════════

async def _issue_warn(chat_id: int, chat_title: str, target: types.User,
                      by: types.User, reason: str = "") -> tuple[int, int]:
    """Add a warn. Returns (new_count, limit)."""
    count = await db.warn_user(chat_id, target.id, reason or "No reason")
    limit = await db.get_warn_limit(chat_id)
    await _log(
        f"<b>⚠️ User Warned</b>\nChat: <b>{chat_title}</b>\n"
        f"User: {_mention(target)} (<code>{target.id}</code>)\n"
        f"Warns: {count}/{limit}\nReason: {reason or 'None'}\n"
        f"By: {_mention(by)}\nTime: {_now()}"
    )
    return count, limit


@app.on_message(filters.command("warn") & filters.group)
async def cmd_warn(_, message: types.Message):
    """bb: Warn a user. Auto-ban on reaching warn limit."""
    if not await _assert_bb(message):
        return await message.reply_text("❌ Admins/authorised users only.")

    target = await _get_target(message)
    if not target:
        return await message.reply_text("❌ Reply to a user or provide @username / user_id.")

    if await is_admin(message.chat.id, target.id):
        return await message.reply_text("❌ Cannot warn an admin.")

    args = message.text.split(None, 2)
    reason = ""
    if message.reply_to_message and len(args) > 1:
        reason = args[1]
    elif not message.reply_to_message and len(args) > 2:
        reason = args[2]

    count, limit = await _issue_warn(
        message.chat.id, message.chat.title, target, message.from_user, reason
    )

    if count >= limit:
        try:
            await app.ban_chat_member(message.chat.id, target.id)
            await db.reset_warns(message.chat.id, target.id)
            await message.reply_text(
                f"⚠️ {_mention(target)} reached <b>{limit}</b> warnings and has been <b>banned</b>."
            )
        except Exception:
            await message.reply_text("⚠️ Warn limit reached but I couldn't ban the user.")
    else:
        text = (
            f"⚠️ {_mention(target)} has been warned.\n"
            f"Warnings: <b>{count}/{limit}</b>"
            + (f"\nReason: {reason}" if reason else "")
        )
        await message.reply_text(text, reply_markup=_warn_buttons(target.id))


@app.on_message(filters.command("warns") & filters.group)
async def cmd_warns(_, message: types.Message):
    target = await _get_target(message) or message.from_user
    warns = await db.get_warns(message.chat.id, target.id)
    limit = await db.get_warn_limit(message.chat.id)

    if not warns:
        return await message.reply_text(f"✅ {_mention(target)} has no warnings.")

    text = f"⚠️ Warnings for {_mention(target)}: <b>{len(warns)}/{limit}</b>\n\n"
    for i, w in enumerate(warns, 1):
        text += f"{i}. {w}\n"
    await message.reply_text(text)


@app.on_message(filters.command("resetwarn") & filters.group)
async def cmd_reset_warn(_, message: types.Message):
    """bb: Clear all warnings for a user."""
    if not await _assert_bb(message):
        return await message.reply_text("❌ Admins/authorised users only.")

    target = await _get_target(message)
    if not target:
        return await message.reply_text("❌ Reply to a user or provide @username / user_id.")

    await db.reset_warns(message.chat.id, target.id)
    await message.reply_text(f"✅ Warnings cleared for {_mention(target)}.")
    await _log(
        f"<b>✅ Warns Reset</b>\nChat: <b>{message.chat.title}</b>\n"
        f"User: {_mention(target)} (<code>{target.id}</code>)\n"
        f"By: {_mention(message.from_user)}\nTime: {_now()}"
    )


@app.on_message(filters.command("setwarnlimit") & filters.group)
async def cmd_set_warn_limit(_, message: types.Message):
    """aa: /setwarnlimit <number>"""
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins.")
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.reply_text("Usage: /setwarnlimit <number>")
    await db.set_warn_limit(message.chat.id, int(args[1]))
    await message.reply_text(f"✅ Warn limit set to <b>{args[1]}</b>.")


# ─── Warn inline buttons ─────────────────────────────────────────────────────

@app.on_callback_query(filters.regex(r"^rmwarn_(\d+)$"))
async def cb_remove_warn(_, query: types.CallbackQuery):
    """Admin-only: remove one warning from the button on a warn message."""
    chat = query.message.chat
    if not await is_admin(chat.id, query.from_user.id):
        return await query.answer("❌ Only admins can remove warnings.", show_alert=True)

    target_id = int(query.matches[0].group(1))
    warns = await db.get_warns(chat.id, target_id)
    if not warns:
        return await query.answer("✅ This user already has no warnings.", show_alert=True)

    warns.pop()
    await db.db.warns.update_one(
        {"_id": f"{chat.id}:{target_id}"}, {"$set": {"warns": warns}}, upsert=True
    )
    limit = await db.get_warn_limit(chat.id)
    await query.answer(f"✅ Warning removed. Now {len(warns)}/{limit}.", show_alert=True)
    try:
        new_markup = _warn_buttons(target_id) if warns else None
        await query.message.edit_reply_markup(new_markup)
    except Exception:
        pass
    await _log(
        f"<b>✅ Warn Removed (button)</b>\nChat: <b>{chat.title}</b>\n"
        f"Target ID: <code>{target_id}</code>\nBy: {_mention(query.from_user)}\n"
        f"Remaining: {len(warns)}/{limit}\nTime: {_now()}"
    )


@app.on_callback_query(filters.regex(r"^resetwarn_(\d+)$"))
async def cb_reset_warn(_, query: types.CallbackQuery):
    """Admin-only: reset all warnings from the button on a warn message."""
    chat = query.message.chat
    if not await is_admin(chat.id, query.from_user.id):
        return await query.answer("❌ Only admins can reset warnings.", show_alert=True)

    target_id = int(query.matches[0].group(1))
    await db.reset_warns(chat.id, target_id)
    await query.answer("✅ All warnings reset.", show_alert=True)
    try:
        await query.message.edit_reply_markup(None)
    except Exception:
        pass
    await _log(
        f"<b>🗑 All Warns Reset (button)</b>\nChat: <b>{chat.title}</b>\n"
        f"Target ID: <code>{target_id}</code>\nBy: {_mention(query.from_user)}\nTime: {_now()}"
    )


@app.on_callback_query(filters.regex(r"^noop$"))
async def cb_noop(_, query: types.CallbackQuery):
    await query.answer()


# ─── Moderation inline callbacks ─────────────────────────────────────────────

@app.on_callback_query(filters.regex(r"^unban_(-?\d+)_(\d+)$"))
async def cb_unban(_, query: types.CallbackQuery):
    """Admin-only: unban a user via button on the ban message."""
    chat_id = int(query.matches[0].group(1))
    target_id = int(query.matches[0].group(2))

    if not await is_admin(chat_id, query.from_user.id):
        return await query.answer("❌ Only admins can unban.", show_alert=True)

    try:
        await app.unban_chat_member(chat_id, target_id)
        await query.answer("✅ User unbanned.", show_alert=True)
        await query.message.edit_reply_markup(None)
        await query.message.edit_text(
            query.message.text.html + f"\n\n✅ <b>Unbanned</b> by {_mention(query.from_user)}",
            reply_markup=None,
        )
        await _log(
            f"<b>✅ Unban (button)</b>\nChat ID: <code>{chat_id}</code>\n"
            f"Target ID: <code>{target_id}</code>\nBy: {_mention(query.from_user)}\n"
            f"Time: {_now()}"
        )
    except Exception as e:
        await query.answer(f"❌ Failed: {e}", show_alert=True)


@app.on_callback_query(filters.regex(r"^unmute_(-?\d+)_(\d+)$"))
async def cb_unmute(_, query: types.CallbackQuery):
    """Admin-only: unmute a user via button on the mute message."""
    chat_id = int(query.matches[0].group(1))
    target_id = int(query.matches[0].group(2))

    if not await is_admin(chat_id, query.from_user.id):
        return await query.answer("❌ Only admins can unmute.", show_alert=True)

    try:
        try:
            chat_obj = await app.get_chat(chat_id)
            perms = chat_obj.permissions or _default_perms()
        except Exception:
            perms = _default_perms()

        await app.restrict_chat_member(chat_id, target_id, perms)
        await db.unmute_user(chat_id, target_id)
        await query.answer("✅ User unmuted.", show_alert=True)
        await query.message.edit_reply_markup(None)
        await _log(
            f"<b>🔊 Unmute (button)</b>\nChat ID: <code>{chat_id}</code>\n"
            f"Target ID: <code>{target_id}</code>\nBy: {_mention(query.from_user)}\n"
            f"Time: {_now()}"
        )
    except Exception as e:
        await query.answer(f"❌ Failed: {e}", show_alert=True)


@app.on_callback_query(filters.regex(r"^unlock_(-?\d+)_(\w+)$"))
async def cb_unlock(_, query: types.CallbackQuery):
    """Admin-only: unlock a message type via button on the lock message."""
    chat_id = int(query.matches[0].group(1))
    lock_type = query.matches[0].group(2)

    if not await is_admin(chat_id, query.from_user.id):
        return await query.answer("❌ Only admins can unlock.", show_alert=True)

    try:
        to_unlock = list(LOCK_TYPES.keys()) if lock_type == "all" else [lock_type]
        for lt in to_unlock:
            await db.remove_lock(chat_id, lt)
        await app.set_chat_permissions(chat_id, _default_perms())
        await query.answer(f"✅ Unlocked: {lock_type}", show_alert=True)
        await query.message.edit_reply_markup(None)
        await _log(
            f"<b>🔓 Unlock (button)</b>\nChat ID: <code>{chat_id}</code>\n"
            f"Type: {lock_type}\nBy: {_mention(query.from_user)}\nTime: {_now()}"
        )
    except Exception as e:
        await query.answer(f"❌ Failed: {e}", show_alert=True)


@app.on_callback_query(filters.regex(r"^demote_(-?\d+)_(\d+)$"))
async def cb_demote(_, query: types.CallbackQuery):
    """Admin-only: demote a promoted user via button on the promote message."""
    chat_id = int(query.matches[0].group(1))
    target_id = int(query.matches[0].group(2))

    if not await is_admin(chat_id, query.from_user.id):
        return await query.answer("❌ Only admins can demote.", show_alert=True)

    try:
        await app.promote_chat_member(
            chat_id, target_id,
            privileges=types.ChatPrivileges(
                can_manage_chat=False,
                can_delete_messages=False,
                can_restrict_members=False,
                can_invite_users=False,
                can_pin_messages=False,
            ),
        )
        await query.answer("✅ User demoted.", show_alert=True)
        await query.message.edit_reply_markup(None)
        await _log(
            f"<b>⬇️ Demote (button)</b>\nChat ID: <code>{chat_id}</code>\n"
            f"Target ID: <code>{target_id}</code>\nBy: {_mention(query.from_user)}\n"
            f"Time: {_now()}"
        )
    except Exception as e:
        await query.answer(f"❌ Failed: {e}", show_alert=True)


@app.on_callback_query(filters.regex(r"^unpin_(-?\d+)_(\d+)$"))
async def cb_unpin(_, query: types.CallbackQuery):
    """Admin-only: unpin a message via button on the pin confirmation."""
    chat_id = int(query.matches[0].group(1))
    msg_id = int(query.matches[0].group(2))

    if not await is_admin(chat_id, query.from_user.id):
        return await query.answer("❌ Only admins can unpin.", show_alert=True)

    try:
        await app.unpin_chat_message(chat_id, message_id=msg_id)
        await query.answer("✅ Message unpinned.", show_alert=True)
        await query.message.edit_reply_markup(None)
        await _log(
            f"<b>📌 Unpin (button)</b>\nChat ID: <code>{chat_id}</code>\n"
            f"Msg ID: <code>{msg_id}</code>\nBy: {_mention(query.from_user)}\n"
            f"Time: {_now()}"
        )
    except Exception as e:
        await query.answer(f"❌ Failed: {e}", show_alert=True)


# ═════════════════════════════════════════════════════════════════════════════
#  NOTES
# ═════════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("save") & filters.group)
async def cmd_save_note(_, message: types.Message):
    """aa: /save <name> <content> — Save a group note."""
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins can save notes.")

    args = message.text.split(None, 2)
    if len(args) < 3:
        return await message.reply_text("Usage: /save <name> <content>")

    await db.set_note(message.chat.id, args[1], args[2])
    await message.reply_text(f"📌 Note <b>{args[1]}</b> saved.")


@app.on_message(filters.command("get") & filters.group)
async def cmd_get_note(_, message: types.Message):
    args = message.text.split(None, 1)
    if len(args) < 2:
        return await message.reply_text("Usage: /get <name>")

    content = await db.get_note(message.chat.id, args[1])
    if not content:
        return await message.reply_text(f"❌ No note named <b>{args[1]}</b>.")
    await message.reply_text(content)


@app.on_message(filters.command("notes") & filters.group)
async def cmd_list_notes(_, message: types.Message):
    notes = await db.get_all_notes(message.chat.id)
    if not notes:
        return await message.reply_text("📭 No notes saved in this group.")
    await message.reply_text(
        "📌 <b>Saved Notes:</b>\n" + "\n".join(f"• <code>{n}</code>" for n in notes)
    )


@app.on_message(filters.command("clear") & filters.group)
async def cmd_clear_note(_, message: types.Message):
    """aa: /clear <name> — Delete a note."""
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins can delete notes.")

    args = message.text.split(None, 1)
    if len(args) < 2:
        return await message.reply_text("Usage: /clear <name>")

    deleted = await db.del_note(message.chat.id, args[1])
    if deleted:
        await message.reply_text(f"🗑 Note <b>{args[1]}</b> deleted.")
    else:
        await message.reply_text(f"❌ No note named <b>{args[1]}</b>.")


@app.on_message(filters.group & filters.regex(r"^#(\w+)"))
async def on_hashtag_note(_, message: types.Message):
    name = message.matches[0].group(1)
    content = await db.get_note(message.chat.id, name)
    if content:
        await message.reply_text(content)


# ═════════════════════════════════════════════════════════════════════════════
#  FILTERS (auto-reply keywords)
# ═════════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("filter") & filters.group)
async def cmd_set_filter(_, message: types.Message):
    """aa: /filter <keyword> <reply>"""
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins can add filters.")

    args = message.text.split(None, 2)
    if len(args) < 3:
        return await message.reply_text("Usage: /filter <keyword> <reply text>")

    await db.set_filter(message.chat.id, args[1], args[2])
    await message.reply_text(f"✅ Filter for <b>{args[1]}</b> added.")


@app.on_message(filters.command("filters") & filters.group)
async def cmd_list_filters(_, message: types.Message):
    kws = await db.get_all_filters(message.chat.id)
    if not kws:
        return await message.reply_text("📭 No active filters.")
    await message.reply_text(
        "🔍 <b>Active Filters:</b>\n" + "\n".join(f"• <code>{k}</code>" for k in kws)
    )


@app.on_message(filters.command("stopfilter") & filters.group)
async def cmd_stop_filter(_, message: types.Message):
    """aa: /stopfilter <keyword> — Remove a filter."""
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins can remove filters.")

    args = message.text.split(None, 1)
    if len(args) < 2:
        return await message.reply_text("Usage: /stopfilter <keyword>")

    deleted = await db.del_filter(message.chat.id, args[1])
    if deleted:
        await message.reply_text(f"🗑 Filter <b>{args[1]}</b> removed.")
    else:
        await message.reply_text(f"❌ No filter for <b>{args[1]}</b>.")


@app.on_message(filters.group & (filters.text | filters.forwarded) & ~filters.command([]))
async def on_group_message_check(_, message: types.Message):
    """
    Unified handler for all passive group-message checks:
      1. Anti-forward  (forwarded messages from non-admins)
      2. Anti-link     (URLs from non-admins)
      3. Anti-words    (banned words from non-admins)
      4. Filters       (keyword auto-replies for everyone)

    Having one handler prevents Pyrogram from silently
    dropping duplicate filter registrations.
    """
    if not message.from_user:
        return

    chat_id = message.chat.id
    user = message.from_user
    is_sudo = user.id in app.sudoers
    admin = is_sudo or await is_admin(chat_id, user.id)
    text = message.text or ""

    # ── 1. Anti-forward ──────────────────────────────────────────────────────
    if message.forward_date is not None and not admin:
        if await db.get_antiforward(chat_id):
            try:
                await message.delete()
            except Exception:
                pass
            sent = await app.send_message(
                chat_id,
                f"↩️ {_mention(user)}, forwarded messages are not allowed here!"
            )
            asyncio.create_task(_auto_delete(sent, 5))
            return   # stop — no point checking links/words on a deleted message

    # ── 2. Anti-link ─────────────────────────────────────────────────────────
    if text and not admin and await db.get_antilink(chat_id):
        if _URL_RE.search(text):
            try:
                await message.delete()
            except Exception:
                pass
            count, limit = await _issue_warn(
                chat_id, message.chat.title, user, user, "Sent a link"
            )
            if count >= limit:
                try:
                    await app.ban_chat_member(chat_id, user.id)
                    await db.reset_warns(chat_id, user.id)
                    await app.send_message(
                        chat_id,
                        f"🚫 {_mention(user)} was <b>banned</b> for repeatedly sending links."
                    )
                except Exception:
                    pass
            else:
                sent = await app.send_message(
                    chat_id,
                    f"🔗 {_mention(user)}, links are not allowed here!\n"
                    f"⚠️ Warning <b>{count}/{limit}</b>.",
                    reply_markup=_warn_buttons(user.id),
                )
                asyncio.create_task(_auto_delete(sent, 10))
            return   # stop after deleting

    # ── 3. Anti-words ────────────────────────────────────────────────────────
    if text and not admin:
        banned_words = await db.get_antiwords(chat_id)
        if banned_words:
            text_lower = text.lower()
            hit = next((w for w in banned_words if w in text_lower), None)
            if hit:
                try:
                    await message.delete()
                except Exception:
                    pass
                count, limit = await _issue_warn(
                    chat_id, message.chat.title, user, user,
                    f"Used banned word: {hit}"
                )
                if count >= limit:
                    try:
                        await app.ban_chat_member(chat_id, user.id)
                        await db.reset_warns(chat_id, user.id)
                        await app.send_message(
                            chat_id,
                            f"🚫 {_mention(user)} was <b>banned</b> for using banned words repeatedly."
                        )
                    except Exception:
                        pass
                else:
                    await app.send_message(
                        chat_id,
                        f"🚫 {_mention(user)}, that word is not allowed here!\n"
                        f"⚠️ Warning <b>{count}/{limit}</b>.",
                        reply_markup=_warn_buttons(user.id),
                    )
                return   # stop after deleting

    # ── 4. Keyword filters (auto-reply) ──────────────────────────────────────
    if text and not text.startswith("/"):
        text_lower = text.lower()
        # Check full text phrase first, then word-by-word
        all_filters = await db.get_all_filters(chat_id)
        matched_reply = None
        for kw in all_filters:
            if kw in text_lower:
                matched_reply = await db.get_filter(chat_id, kw)
                if matched_reply:
                    break
        if matched_reply:
            await message.reply_text(matched_reply)


# ═════════════════════════════════════════════════════════════════════════════
#  LOCKS
# ═════════════════════════════════════════════════════════════════════════════

LOCK_TYPES = {
    "sticker": "can_send_other_messages",
    "gif": "can_send_other_messages",
    "link": "can_add_web_page_previews",
    "media": "can_send_media_messages",
    "poll": "can_send_other_messages",
}


@app.on_message(filters.command("lock") & filters.group)
async def cmd_lock(_, message: types.Message):
    """aa: /lock <type|all>"""
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins can lock.")

    args = message.text.split(None, 1)
    if len(args) < 2:
        return await message.reply_text(
            "Usage: /lock <type>\nTypes: " + ", ".join(LOCK_TYPES.keys()) + ", all"
        )

    lock_type = args[1].lower()
    if lock_type not in LOCK_TYPES and lock_type != "all":
        return await message.reply_text("❌ Unknown lock type.")

    to_lock = list(LOCK_TYPES.keys()) if lock_type == "all" else [lock_type]
    for lt in to_lock:
        await db.add_lock(message.chat.id, lt)

    try:
        chat_obj = await app.get_chat(message.chat.id)
        current = chat_obj.permissions
        new_perms = types.ChatPermissions(
            can_send_messages=current.can_send_messages if current else True,
            can_send_media_messages=False if lock_type in ("media", "all") else (current.can_send_media_messages if current else True),
            can_send_other_messages=False if lock_type in ("sticker", "gif", "poll", "all") else (current.can_send_other_messages if current else True),
            can_add_web_page_previews=False if lock_type in ("link", "all") else (current.can_add_web_page_previews if current else True),
        )
        await app.set_chat_permissions(message.chat.id, new_perms)
        await message.reply_text(
            f"🔒 Locked: <b>{lock_type}</b>",
            reply_markup=_lock_buttons(message.chat.id, lock_type),
        )
    except ChatAdminRequired:
        await message.reply_text("❌ I need admin permissions to change chat permissions.")
    except Exception as e:
        await message.reply_text(f"❌ Failed: {e}")


@app.on_message(filters.command("unlock") & filters.group)
async def cmd_unlock(_, message: types.Message):
    """aa: /unlock <type|all>"""
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins can unlock.")

    args = message.text.split(None, 1)
    if len(args) < 2:
        return await message.reply_text(
            "Usage: /unlock <type>\nTypes: " + ", ".join(LOCK_TYPES.keys()) + ", all"
        )

    lock_type = args[1].lower()
    to_unlock = list(LOCK_TYPES.keys()) if lock_type == "all" else [lock_type]
    for lt in to_unlock:
        await db.remove_lock(message.chat.id, lt)

    try:
        await app.set_chat_permissions(message.chat.id, _default_perms())
        await message.reply_text(f"🔓 Unlocked: <b>{lock_type}</b>")
    except ChatAdminRequired:
        await message.reply_text("❌ I need admin permissions.")
    except Exception as e:
        await message.reply_text(f"❌ Failed: {e}")


@app.on_message(filters.command("locks") & filters.group)
async def cmd_list_locks(_, message: types.Message):
    locked = await db.get_locks(message.chat.id)
    if not locked:
        return await message.reply_text("🔓 No active locks.")
    await message.reply_text(
        "🔒 <b>Active Locks:</b>\n" + "\n".join(f"• {lt}" for lt in locked)
    )


# ═════════════════════════════════════════════════════════════════════════════
#  ANTIFLOOD
# ═════════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("setflood") & filters.group)
async def cmd_setflood(_, message: types.Message):
    """aa: /setflood <number|off>"""
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins.")

    args = message.text.split(None, 1)
    if len(args) < 2:
        return await message.reply_text("Usage: /setflood <number> or /setflood off")

    if args[1].lower() == "off":
        await db.set_flood_limit(message.chat.id, 0)
        return await message.reply_text("✅ Antiflood disabled.")

    if not args[1].isdigit():
        return await message.reply_text("❌ Provide a number or 'off'.")

    await db.set_flood_limit(message.chat.id, int(args[1]))
    await message.reply_text(f"✅ Antiflood set to <b>{args[1]}</b> messages.")


@app.on_message(filters.command("flood") & filters.group)
async def cmd_flood(_, message: types.Message):
    limit = await db.get_flood_limit(message.chat.id)
    if not limit:
        await message.reply_text("🟢 Antiflood is <b>disabled</b>.")
    else:
        await message.reply_text(f"🔴 Antiflood: mute after <b>{limit}</b> consecutive messages.")


@app.on_message(filters.group & ~filters.service, group=1)
async def antiflood_check(_, message: types.Message):
    """Auto-mute users who flood."""
    if not message.from_user:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id

    if user_id == app.id:
        return
    if await is_admin(chat_id, user_id):
        return

    limit = await db.get_flood_limit(chat_id)
    if not limit:
        return

    if chat_id not in _flood_count:
        _flood_count[chat_id] = {}

    _flood_count[chat_id][user_id] = _flood_count[chat_id].get(user_id, 0) + 1

    if _flood_count[chat_id][user_id] >= limit:
        _flood_count[chat_id][user_id] = 0
        try:
            await app.restrict_chat_member(chat_id, user_id, _muted_perms())
            await message.reply_text(
                f"🚦 {_mention(message.from_user)} was <b>muted</b> for flooding."
            )
        except Exception:
            pass


# ═════════════════════════════════════════════════════════════════════════════
#  PROMOTE / DEMOTE
# ═════════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("promote") & filters.group)
async def cmd_promote(_, message: types.Message):
    """aa: Promote a user to admin."""
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins can promote.")

    target = await _get_target(message)
    if not target:
        return await message.reply_text("❌ Reply to a user or provide @username / user_id.")

    try:
        await app.promote_chat_member(
            message.chat.id, target.id,
            privileges=types.ChatPrivileges(
                can_manage_chat=True,
                can_delete_messages=True,
                can_restrict_members=True,
                can_invite_users=True,
                can_pin_messages=True,
            ),
        )
        await message.reply_text(
            f"⬆️ {_mention(target)} has been <b>promoted</b>.",
            reply_markup=_promote_buttons(message.chat.id, target.id),
        )
        await _log(
            f"<b>⬆️ User Promoted</b>\nChat: <b>{message.chat.title}</b>\n"
            f"User: {_mention(target)} (<code>{target.id}</code>)\n"
            f"By: {_mention(message.from_user)}\nTime: {_now()}"
        )
    except ChatAdminRequired:
        await message.reply_text("❌ I need promote permissions.")
    except Exception as e:
        await message.reply_text(f"❌ Failed: {e}")


@app.on_message(filters.command("demote") & filters.group)
async def cmd_demote(_, message: types.Message):
    """aa: Demote an admin."""
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins can demote.")

    target = await _get_target(message)
    if not target:
        return await message.reply_text("❌ Reply to a user or provide @username / user_id.")

    try:
        await app.promote_chat_member(
            message.chat.id, target.id,
            privileges=types.ChatPrivileges(
                can_manage_chat=False,
                can_delete_messages=False,
                can_restrict_members=False,
                can_invite_users=False,
                can_pin_messages=False,
            ),
        )
        await message.reply_text(f"⬇️ {_mention(target)} has been <b>demoted</b>.")
        await _log(
            f"<b>⬇️ User Demoted</b>\nChat: <b>{message.chat.title}</b>\n"
            f"User: {_mention(target)} (<code>{target.id}</code>)\n"
            f"By: {_mention(message.from_user)}\nTime: {_now()}"
        )
    except ChatAdminRequired:
        await message.reply_text("❌ I need promote permissions.")
    except Exception as e:
        await message.reply_text(f"❌ Failed: {e}")


# ═════════════════════════════════════════════════════════════════════════════
#  PIN / UNPIN
# ═════════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("pin") & filters.group)
async def cmd_pin(_, message: types.Message):
    """aa: Pin the replied message."""
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins can pin.")

    if not message.reply_to_message:
        return await message.reply_text("❌ Reply to a message to pin it.")

    try:
        await app.pin_chat_message(
            message.chat.id,
            message.reply_to_message.id,
            disable_notification=False,
        )
        await message.reply_text(
            "📌 Message pinned.",
            reply_markup=_pin_buttons(message.chat.id, message.reply_to_message.id),
        )
    except ChatAdminRequired:
        await message.reply_text("❌ I need 'Pin Messages' admin permission.")
    except Exception as e:
        await message.reply_text(f"❌ Failed: {e}")


@app.on_message(filters.command("unpin") & filters.group)
async def cmd_unpin(_, message: types.Message):
    """aa: Unpin — reply to unpin a specific message, or use alone to unpin the last one."""
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins can unpin.")

    try:
        if message.reply_to_message:
            await app.unpin_chat_message(
                message.chat.id, message_id=message.reply_to_message.id
            )
        else:
            await app.unpin_chat_message(message.chat.id)
        await message.reply_text("📌 Message unpinned.")
    except ChatAdminRequired:
        await message.reply_text("❌ I need 'Pin Messages' admin permission.")
    except Exception as e:
        await message.reply_text(f"❌ Failed: {e}")


@app.on_message(filters.command("unpinall") & filters.group)
async def cmd_unpinall(_, message: types.Message):
    """aa: Unpin all messages."""
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins can unpin all.")
    try:
        await app.unpin_all_chat_messages(message.chat.id)
        await message.reply_text("📌 All messages unpinned.")
    except ChatAdminRequired:
        await message.reply_text("❌ I need 'Pin Messages' admin permission.")
    except Exception as e:
        await message.reply_text(f"❌ Failed: {e}")


# ═════════════════════════════════════════════════════════════════════════════
#  USER INFO / CHAT INFO
# ═════════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("info") & filters.group)
async def cmd_info(_, message: types.Message):
    """Get info about yourself or a replied/mentioned user."""
    target = await _get_target(message) or message.from_user
    if not target:
        return await message.reply_text("❌ Could not find that user.")

    chat = message.chat

    status = "Unknown"
    try:
        member = await app.get_chat_member(chat.id, target.id)
        status = str(member.status).split(".")[-1].title()
    except UserNotParticipant:
        status = "Not in group"
    except Exception:
        status = "Unknown"

    warns = await db.get_warns(chat.id, target.id)
    warn_limit = await db.get_warn_limit(chat.id)
    username = f"@{target.username}" if target.username else "N/A"
    is_auth = await db.is_auth(chat.id, target.id)

    text = (
        f"👤 <b>User Info</b>\n\n"
        f"Name: {_mention(target)}\n"
        f"ID: <code>{target.id}</code>\n"
        f"Username: {username}\n"
        f"Status: {status}\n"
        f"Authorised: {'✅' if is_auth else '❌'}\n"
        f"Warnings: {len(warns)}/{warn_limit}\n"
        f"Bot: {'Yes' if target.is_bot else 'No'}"
    )
    await message.reply_text(text)


@app.on_message(filters.command("chatinfo") & filters.group)
async def cmd_chatinfo(_, message: types.Message):
    chat = message.chat
    try:
        count = await app.get_chat_members_count(chat.id)
    except Exception:
        count = "Unknown"
    username = f"@{chat.username}" if chat.username else "Private"
    text = (
        f"💬 <b>Chat Info</b>\n\n"
        f"Name: <b>{chat.title}</b>\n"
        f"ID: <code>{chat.id}</code>\n"
        f"Type: {str(chat.type).split('.')[-1].title()}\n"
        f"Members: {count}\n"
        f"Username: {username}"
    )
    await message.reply_text(text)


@app.on_message(filters.command("adminlist") & filters.group)
async def cmd_adminlist(_, message: types.Message):
    admins = await db.get_admins(message.chat.id, reload=True)
    if not admins:
        return await message.reply_text("❌ Couldn't fetch admin list.")
    lines = []
    for uid in admins:
        try:
            u = await app.get_users(uid)
            name = (u.first_name or "") + (" " + u.last_name if u.last_name else "")
            lines.append(f"• <a href='tg://user?id={uid}'>{name.strip() or uid}</a>")
        except Exception:
            lines.append(f"• <code>{uid}</code>")
    await message.reply_text("👑 <b>Admins:</b>\n" + "\n".join(lines))


# ═════════════════════════════════════════════════════════════════════════════
#  INVITE LINK
# ═════════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("invitelink") & filters.group)
async def cmd_invite(_, message: types.Message):
    """aa: Generate a new invite link."""
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins.")
    try:
        link = await app.export_chat_invite_link(message.chat.id)
        await message.reply_text(f"🔗 Invite link:\n{link}")
    except ChatAdminRequired:
        await message.reply_text("❌ I need invite link permission.")
    except Exception as e:
        await message.reply_text(f"❌ Failed: {e}")



# ═════════════════════════════════════════════════════════════════════════════
#  ANTI-LINK
# ═════════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("antilink") & filters.group)
async def cmd_antilink(_, message: types.Message):
    """aa: /antilink on|off"""
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins.")

    args = message.text.split()
    if len(args) < 2 or args[1].lower() not in ("on", "off"):
        current = await db.get_antilink(message.chat.id)
        return await message.reply_text(
            f"🔗 Anti-link is currently <b>{'ON 🔴' if current else 'OFF 🟢'}</b>.\n"
            f"Use /antilink on or /antilink off."
        )

    enable = args[1].lower() == "on"
    await db.set_antilink(message.chat.id, enable)
    await message.reply_text(
        f"✅ Anti-link <b>{'enabled 🔴' if enable else 'disabled 🟢'}</b>.\n"
        + ("Links from non-admins will be deleted + warned." if enable else "Links are now allowed.")
    )
    await _log(
        f"<b>🔗 Anti-link {'ON' if enable else 'OFF'}</b>\n"
        f"Chat: <b>{message.chat.title}</b> (<code>{message.chat.id}</code>)\n"
        f"By: {_mention(message.from_user)}\nTime: {_now()}"
    )




# ═════════════════════════════════════════════════════════════════════════════
#  ANTI-FORWARD
# ═════════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("antiforward") & filters.group)
async def cmd_antiforward(_, message: types.Message):
    """aa: /antiforward on|off"""
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins.")

    args = message.text.split()
    if len(args) < 2 or args[1].lower() not in ("on", "off"):
        current = await db.get_antiforward(message.chat.id)
        return await message.reply_text(
            f"↩️ Anti-forward is currently <b>{'ON 🔴' if current else 'OFF 🟢'}</b>.\n"
            f"Use /antiforward on or /antiforward off."
        )

    enable = args[1].lower() == "on"
    await db.set_antiforward(message.chat.id, enable)
    await message.reply_text(f"✅ Anti-forward <b>{'enabled 🔴' if enable else 'disabled 🟢'}</b>.")
    await _log(
        f"<b>↩️ Anti-forward {'ON' if enable else 'OFF'}</b>\n"
        f"Chat: <b>{message.chat.title}</b> (<code>{message.chat.id}</code>)\n"
        f"By: {_mention(message.from_user)}\nTime: {_now()}"
    )



# ═════════════════════════════════════════════════════════════════════════════
#  ANTI-WORDS  (managed by admin via bot DM)
# ═════════════════════════════════════════════════════════════════════════════

@app.on_message(filters.private & filters.command("setantiword"))
async def cmd_set_antiword(_, message: types.Message):
    """DM: /setantiword <chat_id> <word>"""
    args = message.text.split(None, 2)
    if len(args) < 3:
        return await message.reply_text(
            "📝 Usage: /setantiword <chat_id> <word>\nExample: /setantiword -1001234567890 badword"
        )
    try:
        chat_id = int(args[1])
    except ValueError:
        return await message.reply_text("❌ Invalid chat ID.")

    if message.from_user.id not in app.sudoers:
        if not await is_admin(chat_id, message.from_user.id):
            return await message.reply_text("❌ You are not an admin of that group.")

    word = args[2].lower().strip()
    await db.add_antiword(chat_id, word)
    await message.reply_text(f"✅ Word <code>{word}</code> added to banned list for <code>{chat_id}</code>.")


@app.on_message(filters.private & filters.command("delantiword"))
async def cmd_del_antiword(_, message: types.Message):
    """DM: /delantiword <chat_id> <word>"""
    args = message.text.split(None, 2)
    if len(args) < 3:
        return await message.reply_text("Usage: /delantiword <chat_id> <word>")
    try:
        chat_id = int(args[1])
    except ValueError:
        return await message.reply_text("❌ Invalid chat ID.")

    if message.from_user.id not in app.sudoers:
        if not await is_admin(chat_id, message.from_user.id):
            return await message.reply_text("❌ You are not an admin of that group.")

    removed = await db.remove_antiword(chat_id, args[2].lower().strip())
    if removed:
        await message.reply_text(f"✅ Word removed.")
    else:
        await message.reply_text(f"❌ Word not found in the list.")


@app.on_message(filters.private & filters.command("listantiwords"))
async def cmd_list_antiwords(_, message: types.Message):
    """DM: /listantiwords <chat_id>"""
    args = message.text.split(None, 1)
    if len(args) < 2:
        return await message.reply_text("Usage: /listantiwords <chat_id>")
    try:
        chat_id = int(args[1])
    except ValueError:
        return await message.reply_text("❌ Invalid chat ID.")

    if message.from_user.id not in app.sudoers:
        if not await is_admin(chat_id, message.from_user.id):
            return await message.reply_text("❌ You are not an admin of that group.")

    words = await db.get_antiwords(chat_id)
    if not words:
        return await message.reply_text(f"📭 No banned words for <code>{chat_id}</code>.")
    await message.reply_text(
        f"🚫 <b>Banned words for {chat_id}:</b>\n" +
        "\n".join(f"• <code>{w}</code>" for w in words)
    )


@app.on_message(filters.private & filters.command("clearantiwords"))
async def cmd_clear_antiwords(_, message: types.Message):
    """DM: /clearantiwords <chat_id>"""
    args = message.text.split(None, 1)
    if len(args) < 2:
        return await message.reply_text("Usage: /clearantiwords <chat_id>")
    try:
        chat_id = int(args[1])
    except ValueError:
        return await message.reply_text("❌ Invalid chat ID.")

    if message.from_user.id not in app.sudoers:
        if not await is_admin(chat_id, message.from_user.id):
            return await message.reply_text("❌ You are not an admin of that group.")

    await db.clear_antiwords(chat_id)
    await message.reply_text(f"✅ All banned words cleared for <code>{chat_id}</code>.")



# ═════════════════════════════════════════════════════════════════════════════
#  TAG ALL
# ═════════════════════════════════════════════════════════════════════════════

# In-memory store for active tagall tasks: {chat_id: asyncio.Task}
# {chat_id: {"task": asyncio.Task, "invoker": int}}
_tagall_tasks: dict[int, dict] = {}


@app.on_message(filters.command("tagall") & filters.group)
async def cmd_tagall(_, message: types.Message):
    """bb: /tagall [message] — Mention all members. Shows a Stop button."""
    if not await _assert_bb(message):
        return await message.reply_text("❌ Admins/authorised users only.")

    chat_id = message.chat.id
    invoker_id = message.from_user.id

    # Cancel any already-running tagall in this chat
    existing = _tagall_tasks.get(chat_id)
    if existing and not existing["task"].done():
        existing["task"].cancel()
        await message.reply_text("⏹ Previous /tagall stopped. Starting new one...")

    args = message.text.split(None, 1)
    header = args[1] if len(args) > 1 else "📣 Attention everyone!"

    members = []
    try:
        async for member in app.get_chat_members(chat_id):
            if not member.user.is_bot and not member.user.is_deleted:
                members.append(member.user)
    except Exception as e:
        return await message.reply_text(f"❌ Couldn't fetch members: {e}")

    if not members:
        return await message.reply_text("❌ No members found.")

    stop_markup = types.InlineKeyboardMarkup([[
        types.InlineKeyboardButton("⏹ Stop Tagging", callback_data=f"tagall_stop_{chat_id}")
    ]])
    header_msg = await message.reply_text(
        f"<b>{header}</b>\n<i>Tagging {len(members)} members…</i>",
        reply_markup=stop_markup,
    )

    async def _do_tagall():
        chunk_size = 5
        try:
            for i in range(0, len(members), chunk_size):
                chunk = members[i:i + chunk_size]
                mentions = "  ".join(
                    f'<a href="tg://user?id={u.id}">{u.first_name or u.id}</a>'
                    for u in chunk
                )
                try:
                    await app.send_message(chat_id, mentions)
                except FloodWait as fw:
                    await asyncio.sleep(fw.value)
                    await app.send_message(chat_id, mentions)
                await asyncio.sleep(0.4)

            try:
                await header_msg.edit_reply_markup(None)
                await header_msg.edit_text(
                    f"<b>{header}</b>\n✅ Tagged <b>{len(members)}</b> members."
                )
            except Exception:
                pass
        except asyncio.CancelledError:
            try:
                await header_msg.edit_text(
                    f"<b>{header}</b>\n⏹ Tagging was stopped.",
                    reply_markup=None,
                )
            except Exception:
                pass
        finally:
            _tagall_tasks.pop(chat_id, None)

    task = asyncio.create_task(_do_tagall())
    _tagall_tasks[chat_id] = {"task": task, "invoker": invoker_id}


@app.on_callback_query(filters.regex(r"^tagall_stop_(-?\d+)$"))
async def cb_tagall_stop(_, query: types.CallbackQuery):
    """Admin or the user who started it can stop tagall."""
    chat_id = int(query.matches[0].group(1))
    entry = _tagall_tasks.get(chat_id)

    # Allow: admin OR the person who started the tagall
    is_invoker = entry and entry.get("invoker") == query.from_user.id
    if not is_invoker and not await is_admin(chat_id, query.from_user.id):
        return await query.answer("❌ Only admins or the person who started tagall can stop it.", show_alert=True)

    if entry and not entry["task"].done():
        entry["task"].cancel()
        await query.answer("⏹ Tagall stopped.", show_alert=True)
    else:
        await query.answer("ℹ️ No tagall is running.", show_alert=True)


# ═════════════════════════════════════════════════════════════════════════════
#  CAPTCHA
# ═════════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("captcha") & filters.group)
async def cmd_captcha(_, message: types.Message):
    """aa: /captcha on|off"""
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins.")

    args = message.text.split()
    if len(args) < 2 or args[1].lower() not in ("on", "off"):
        current = await db.get_captcha(message.chat.id)
        return await message.reply_text(
            f"🔐 Captcha is currently <b>{'ON 🔴' if current else 'OFF 🟢'}</b>.\n"
            f"Use /captcha on or /captcha off."
        )

    enable = args[1].lower() == "on"
    await db.set_captcha(message.chat.id, enable)
    await message.reply_text(
        f"✅ Captcha <b>{'enabled 🔴' if enable else 'disabled 🟢'}</b>."
        + ("\nNew members will be muted until they solve a math question." if enable else "")
    )


async def _send_captcha(chat: types.Chat, user: types.User) -> None:
    """Mute new user and send them a math captcha."""
    try:
        await app.restrict_chat_member(chat.id, user.id, _muted_perms())
    except Exception:
        return  # If we can't restrict, skip captcha

    a, b = random.randint(1, 15), random.randint(1, 15)
    answer = a + b

    # 3 wrong answers + 1 correct, shuffled
    wrong = set()
    while len(wrong) < 3:
        w = answer + random.randint(-10, 10)
        if w != answer and w > 0:
            wrong.add(w)
    options = list(wrong) + [answer]
    random.shuffle(options)

    buttons = types.InlineKeyboardMarkup([[
        types.InlineKeyboardButton(
            str(opt),
            callback_data=f"cap_{user.id}_{opt}_{answer}"
        )
        for opt in options
    ]])

    try:
        sent = await app.send_message(
            chat.id,
            f"🔐 Welcome {_mention(user)}!\n\n"
            f"Please solve this to verify you're human and gain access:\n\n"
            f"<b>{a} + {b} = ?</b>\n\n"
            f"⏳ You have <b>60 seconds</b> or you will be kicked.",
            reply_markup=buttons,
        )
    except Exception:
        return

    if chat.id not in _captcha_pending:
        _captcha_pending[chat.id] = {}
    _captcha_pending[chat.id][user.id] = {"msg_id": sent.id, "answer": answer}

    # Auto-kick after 60s if not verified
    await asyncio.sleep(60)
    if chat.id in _captcha_pending and user.id in _captcha_pending[chat.id]:
        _captcha_pending[chat.id].pop(user.id, None)
        try:
            await app.ban_chat_member(chat.id, user.id)
            await app.unban_chat_member(chat.id, user.id)
            await sent.edit_text(
                f"⏰ {_mention(user)} failed to solve the captcha and was <b>kicked</b>."
            )
        except Exception:
            pass


@app.on_callback_query(filters.regex(r"^cap_(\d+)_(\d+)_(\d+)$"))
async def cb_captcha(_, query: types.CallbackQuery):
    """Handle captcha button presses."""
    user_id = int(query.matches[0].group(1))
    chosen = int(query.matches[0].group(2))
    answer = int(query.matches[0].group(3))
    chat = query.message.chat

    if query.from_user.id != user_id:
        return await query.answer("❌ This captcha is not for you!", show_alert=True)

    if chosen == answer:
        # Correct — restore permissions
        if chat.id in _captcha_pending:
            _captcha_pending[chat.id].pop(user_id, None)

        try:
            chat_obj = await app.get_chat(chat.id)
            perms = chat_obj.permissions or _default_perms()
            await app.restrict_chat_member(chat.id, user_id, perms)
        except Exception:
            pass

        try:
            await query.message.edit_text(
                f"✅ {_mention(query.from_user)} passed the captcha! "
                f"Welcome to <b>{chat.title}</b>! 🎉"
            )
        except Exception:
            pass
        await query.answer("✅ Correct! Welcome!", show_alert=True)
    else:
        await query.answer("❌ Wrong answer! Try again.", show_alert=True)


# ═════════════════════════════════════════════════════════════════════════════
#  HELP
# ═════════════════════════════════════════════════════════════════════════════

GMGMT_HELP = """
<b>🛡 Group Management Commands</b>

<b>── Welcome / Goodbye (aa) ──</b>
/setwelcome &lt;text&gt; — Set welcome message (reply to photo for custom image)
/setwelcome &lt;secs&gt; &lt;text&gt; — With auto-delete timer
/delwelcome — Remove welcome message
/setgoodbye &lt;text&gt; — Set goodbye message (reply to photo for custom image)
/delgoodbye — Remove goodbye message
Variables: {mention} {first} {last} {title} {id}

<b>── Moderation (bb = admin or authorised) ──</b>
/ban [reason] — Ban a user (inline Unban button)
/unban — Unban a user
/kick [reason] — Kick a user (inline Re-invite button)
/mute — Mute a user (inline Unmute button)
/unmute — Unmute a user
/tmute &lt;10m|2h|1d&gt; — Temp mute (inline Unmute button)

<b>── Auth / Permissions (aa) ──</b>
/auth — Authorise a user to use moderation commands
/unauth — Remove a user's authorisation
/authlist — List all authorised users
/admincache — Reload admin list (also: /reload)

<b>── Messages (bb) ──</b>
/del — Delete replied message
/purge — Purge from replied to current
/delall — Delete last 3000 msgs from a user

<b>── Warnings (bb) ──</b>
/warn [reason] — Warn (auto-ban at limit) + inline Remove/Reset buttons
/warns — Show warnings
/resetwarn — Clear all warnings
/setwarnlimit &lt;n&gt; — Set warn limit (aa)

<b>── Notes (aa) ──</b>
/save &lt;name&gt; &lt;text&gt; — Save a note
/get &lt;name&gt; — Get a note
#notename — Auto-get by hashtag
/notes — List all notes
/clear &lt;name&gt; — Delete a note

<b>── Filters (aa) ──</b>
/filter &lt;keyword&gt; &lt;reply&gt; — Add auto-reply
/filters — List filters
/stopfilter &lt;keyword&gt; — Remove filter

<b>── Anti-Link (aa) ──</b>
/antilink on|off — Delete links from non-admins + warn

<b>── Anti-Forward (aa) ──</b>
/antiforward on|off — Delete forwarded messages from non-admins

<b>── Anti-Words (bot DM only) ──</b>
/setantiword &lt;chat_id&gt; &lt;word&gt;
/delantiword &lt;chat_id&gt; &lt;word&gt;
/listantiwords &lt;chat_id&gt;
/clearantiwords &lt;chat_id&gt;

<b>── Locks (aa) ──</b>
/lock &lt;type|all&gt; — Lock: sticker gif link media poll all (inline Unlock button)
/unlock &lt;type|all&gt;
/locks — Show active locks

<b>── AntiFlood (aa) ──</b>
/setflood &lt;n|off&gt;
/flood — Show limit

<b>── Admin Tools (aa) ──</b>
/promote — Promote to admin (inline Demote button)
/demote — Demote admin
/pin — Pin replied message (inline Unpin button)
/unpin — Unpin (reply = specific, alone = last)
/unpinall — Unpin all messages
/invitelink — Generate invite link

<b>── Tag All (bb) ──</b>
/tagall [message] — Mention all members (inline Stop button)

<b>── Captcha (aa) ──</b>
/captcha on|off — Math captcha for new members (auto-kick on timeout)

<b>── Info ──</b>
/info — User info (name, ID, status, warns, auth)
/chatinfo — Chat info
/adminlist — List admins
/authlist — List authorised users
/gmhelp — This help message
"""


@app.on_message(filters.command("gmhelp"))
async def cmd_gmhelp(_, message: types.Message):
    await message.reply_text(GMGMT_HELP)
