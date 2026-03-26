# ─────────────────────────────────────────────────────────────────────────────
# Group Management Plugin for AnonXMusic
# Author: Built on top of AnonXMusic by AnonymousX1025
#
# Permission levels used throughout this file:
#   aa  = Group Admins  (Telegram admin / owner)
#   bb  = Bot admins    (aa + users authorised via /auth command)
#
# All significant actions are forwarded to LOGGER_ID channel.
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import random
import re
import string
from datetime import datetime

from pyrogram import enums, filters, types
from pyrogram.errors import (
    ChatAdminRequired,
    UserAdminInvalid,
    UserNotParticipant,
)

from anony import app, config, db
from anony.helpers._admins import is_admin

# ─── URL regex ───────────────────────────────────────────────────────────────
_URL_RE = re.compile(
    r"(https?://|www\.|t\.me/|@\w+\.\w+)"
    r"|(\b\w[\w-]*\.(com|net|org|io|me|ly|co|gg|tv|xyz|info|biz|app|dev)\b)",
    re.IGNORECASE,
)

# ─── In-memory captcha store: {chat_id: {user_id: {"code": str, "msg_id": int}}} ──
_captcha_pending: dict[int, dict[int, dict]] = {}

# ─── Flood tracker (in-memory, per chat) ─────────────────────────────────────
_flood_count: dict[int, dict[int, int]] = {}   # {chat_id: {user_id: count}}

# ─── Helpers ─────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


async def _log(text: str) -> None:
    """Forward an event to the log channel."""
    try:
        await app.send_message(config.LOGGER_ID, text, parse_mode=enums.ParseMode.HTML)
    except Exception:
        pass


async def _get_target(message: types.Message) -> types.User | None:
    """
    Resolve the target user from:
      1. A replied-to message
      2. A @username or user_id argument
    """
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
    """Returns True if the sender is a Telegram admin / owner."""
    if message.from_user.id in app.sudoers:
        return True
    return await is_admin(message.chat.id, message.from_user.id)


async def _assert_bb(message: types.Message) -> bool:
    """Returns True if the sender is aa or is auth-listed (bb)."""
    if await _assert_admin(message):
        return True
    return await db.is_auth(message.chat.id, message.from_user.id)


async def _only_groups(message: types.Message) -> bool:
    if message.chat.type == enums.ChatType.PRIVATE:
        await message.reply_text("❌ This command only works in groups.")
        return False
    return True


# ═════════════════════════════════════════════════════════════════════════════
#  WELCOME / GOODBYE
# ═════════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("setwelcome") & filters.group)
async def cmd_set_welcome(_, message: types.Message):
    """aa: /setwelcome <text>  — optionally reply to a photo to set custom welcome photo."""
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins can set the welcome message.")

    args = message.text.split(None, 1)
    if len(args) < 2:
        return await message.reply_text(
            "📝 Usage:\n"
            "<code>/setwelcome Hello {mention}! Welcome to {title}.</code>\n\n"
            "Variables: <code>{mention}</code> <code>{first}</code> <code>{last}</code> <code>{title}</code> <code>{id}</code>\n\n"
            "To auto-delete the welcome message after N seconds:\n"
            "<code>/setwelcome 60 Hello {mention}!</code>\n\n"
            "To set a custom photo, reply to a photo while running this command."
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

    # Check if replying to a photo
    photo_url = None
    if message.reply_to_message and message.reply_to_message.photo:
        photo_url = message.reply_to_message.photo.file_id

    await db.set_welcome(message.chat.id, text, delete_after, photo_url)
    resp = "✅ Welcome message saved."
    if delete_after:
        resp += f"\n⏳ Auto-delete after <b>{delete_after}s</b>."
    if photo_url:
        resp += "\n🖼 Custom photo saved."
    else:
        resp += f"\n🖼 Using default photo from config."
    await message.reply_text(resp)

    await _log(
        f"<b>🟢 Welcome Set</b>\n"
        f"Chat: <b>{message.chat.title}</b> (<code>{message.chat.id}</code>)\n"
        f"By: {_mention(message.from_user)}\n"
        f"Delete after: {delete_after}s\n"
        f"Text: <code>{text[:200]}</code>\n"
        f"Time: {_now()}"
    )


@app.on_message(filters.command("delwelcome") & filters.group)
async def cmd_del_welcome(_, message: types.Message):
    """aa: Remove the custom welcome message."""
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins can remove the welcome message.")
    await db.del_welcome(message.chat.id)
    await message.reply_text("✅ Welcome message removed.")
    await _log(
        f"<b>🗑 Welcome Removed</b>\n"
        f"Chat: <b>{message.chat.title}</b> (<code>{message.chat.id}</code>)\n"
        f"By: {_mention(message.from_user)}\n"
        f"Time: {_now()}"
    )


@app.on_message(filters.command("setgoodbye") & filters.group)
async def cmd_set_goodbye(_, message: types.Message):
    """aa: /setgoodbye <text> — reply to a photo to set custom goodbye photo."""
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins can set the goodbye message.")

    args = message.text.split(None, 1)
    if len(args) < 2:
        return await message.reply_text(
            "📝 Usage: <code>/setgoodbye Goodbye {first}! We'll miss you.</code>\n\n"
            "Variables: <code>{mention}</code> <code>{first}</code> <code>{last}</code> <code>{title}</code> <code>{id}</code>\n\n"
            "Reply to a photo while running this command to set a custom goodbye photo."
        )

    photo_url = None
    if message.reply_to_message and message.reply_to_message.photo:
        photo_url = message.reply_to_message.photo.file_id

    await db.set_goodbye(message.chat.id, args[1], photo_url)
    resp = "✅ Goodbye message saved."
    if photo_url:
        resp += "\n🖼 Custom photo saved."
    else:
        resp += "\n🖼 Using default photo from config."
    await message.reply_text(resp)

    await _log(
        f"<b>🔴 Goodbye Set</b>\n"
        f"Chat: <b>{message.chat.title}</b> (<code>{message.chat.id}</code>)\n"
        f"By: {_mention(message.from_user)}\n"
        f"Text: <code>{args[1][:200]}</code>\n"
        f"Time: {_now()}"
    )


@app.on_message(filters.command("delgoodbye") & filters.group)
async def cmd_del_goodbye(_, message: types.Message):
    """aa: Remove the custom goodbye message."""
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins can remove the goodbye message.")
    await db.del_goodbye(message.chat.id)
    await message.reply_text("✅ Goodbye message removed.")


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


@app.on_chat_member_updated(filters.group)
async def on_member_update(_, update: types.ChatMemberUpdated):
    chat = update.chat
    if not update.new_chat_member and not update.old_chat_member:
        return

    old_status = getattr(update.old_chat_member, "status", None)
    new_status = getattr(update.new_chat_member, "status", None)

    user = update.new_chat_member.user if update.new_chat_member else update.old_chat_member.user

    # ── User joined ──────────────────────────────────────────────────────────
    if new_status == enums.ChatMemberStatus.MEMBER and old_status not in (
        enums.ChatMemberStatus.MEMBER,
        enums.ChatMemberStatus.ADMINISTRATOR,
        enums.ChatMemberStatus.OWNER,
    ):
        # Captcha check
        if await db.get_captcha(chat.id):
            await _send_captcha(chat, user)

        data = await db.get_welcome(chat.id)
        # Use custom text if set, else fall back to config default
        raw_text = data.get("text") or config.WELCOME_TEXT
        photo = data.get("photo") or config.WELCOME_PHOTO
        text = _format_greeting(raw_text, user, chat)

        try:
            sent = await app.send_photo(
                chat.id,
                photo=photo,
                caption=text,
            )
        except Exception:
            # If photo fails, send plain text
            sent = await app.send_message(chat.id, text)

        delete_after = data.get("delete_after", 0)
        if delete_after:
            await asyncio.sleep(delete_after)
            try:
                await sent.delete()
            except Exception:
                pass

    # ── User left / kicked ───────────────────────────────────────────────────
    elif new_status in (enums.ChatMemberStatus.LEFT, enums.ChatMemberStatus.BANNED) and old_status in (
        enums.ChatMemberStatus.MEMBER,
        enums.ChatMemberStatus.ADMINISTRATOR,
        enums.ChatMemberStatus.OWNER,
    ):
        data = await db.get_goodbye(chat.id)
        raw_text = data.get("text") if isinstance(data, dict) else data
        raw_text = raw_text or config.GOODBYE_TEXT
        photo = (data.get("photo") if isinstance(data, dict) else None) or config.WELCOME_PHOTO
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
        return await message.reply_text("❌ Reply to a user or provide @username/user_id.")

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
        await message.reply_text(resp)
        await _log(
            f"<b>🚫 User Banned</b>\n"
            f"Chat: <b>{message.chat.title}</b> (<code>{message.chat.id}</code>)\n"
            f"User: {_mention(target)} (<code>{target.id}</code>)\n"
            f"By: {_mention(message.from_user)}\n"
            f"Reason: {reason or 'None'}\n"
            f"Time: {_now()}"
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
        return await message.reply_text("❌ Reply to a user or provide @username/user_id.")

    try:
        await app.unban_chat_member(message.chat.id, target.id)
        await message.reply_text(f"✅ {_mention(target)} has been <b>unbanned</b>.")
        await _log(
            f"<b>✅ User Unbanned</b>\n"
            f"Chat: <b>{message.chat.title}</b> (<code>{message.chat.id}</code>)\n"
            f"User: {_mention(target)} (<code>{target.id}</code>)\n"
            f"By: {_mention(message.from_user)}\n"
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
        return await message.reply_text("❌ Reply to a user or provide @username/user_id.")

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
        await message.reply_text(resp)
        await _log(
            f"<b>👢 User Kicked</b>\n"
            f"Chat: <b>{message.chat.title}</b> (<code>{message.chat.id}</code>)\n"
            f"User: {_mention(target)} (<code>{target.id}</code>)\n"
            f"By: {_mention(message.from_user)}\n"
            f"Reason: {reason or 'None'}\n"
            f"Time: {_now()}"
        )
    except ChatAdminRequired:
        await message.reply_text("❌ I need kick permissions.")


# ═════════════════════════════════════════════════════════════════════════════
#  MUTE / UNMUTE / TMUTE
# ═════════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("mute") & filters.group)
async def cmd_mute(_, message: types.Message):
    """bb: Mute a user (restrict messages)."""
    if not await _assert_bb(message):
        return await message.reply_text("❌ You need to be an admin or authorised user to mute.")

    target = await _get_target(message)
    if not target:
        return await message.reply_text("❌ Reply to a user or provide @username/user_id.")

    if await is_admin(message.chat.id, target.id):
        return await message.reply_text("❌ Cannot mute an admin.")

    try:
        await app.restrict_chat_member(
            message.chat.id,
            target.id,
            enums.ChatPermissions(
                can_send_messages=False,
                can_send_media_messages=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False,
            ),
        )
        await db.mute_user(message.chat.id, target.id)
        await message.reply_text(f"🔇 {_mention(target)} has been <b>muted</b>.")
        await _log(
            f"<b>🔇 User Muted</b>\n"
            f"Chat: <b>{message.chat.title}</b> (<code>{message.chat.id}</code>)\n"
            f"User: {_mention(target)} (<code>{target.id}</code>)\n"
            f"By: {_mention(message.from_user)}\n"
            f"Time: {_now()}"
        )
    except ChatAdminRequired:
        await message.reply_text("❌ I need restrict permissions.")


@app.on_message(filters.command("unmute") & filters.group)
async def cmd_unmute(_, message: types.Message):
    """bb: Unmute a user."""
    if not await _assert_bb(message):
        return await message.reply_text("❌ You need to be an admin or authorised user to unmute.")

    target = await _get_target(message)
    if not target:
        return await message.reply_text("❌ Reply to a user or provide @username/user_id.")

    try:
        chat = await app.get_chat(message.chat.id)
        default_perms = chat.permissions
        await app.restrict_chat_member(
            message.chat.id, target.id, default_perms or enums.ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            )
        )
        await db.unmute_user(message.chat.id, target.id)
        await message.reply_text(f"🔊 {_mention(target)} has been <b>unmuted</b>.")
        await _log(
            f"<b>🔊 User Unmuted</b>\n"
            f"Chat: <b>{message.chat.title}</b> (<code>{message.chat.id}</code>)\n"
            f"User: {_mention(target)} (<code>{target.id}</code>)\n"
            f"By: {_mention(message.from_user)}\n"
            f"Time: {_now()}"
        )
    except ChatAdminRequired:
        await message.reply_text("❌ I need restrict permissions.")


@app.on_message(filters.command("tmute") & filters.group)
async def cmd_tmute(_, message: types.Message):
    """bb: /tmute <time> — Mute for a duration. Time: 10m / 2h / 1d"""
    if not await _assert_bb(message):
        return await message.reply_text("❌ Admins/authorised users only.")

    target = await _get_target(message)
    if not target:
        return await message.reply_text("❌ Reply to a user or provide @username/user_id.")

    args = message.text.split()
    # Find the time argument regardless of position (works with reply or @username)
    time_str = ""
    for a in args[1:]:
        if _parse_time(a):
            time_str = a
            break
    seconds = _parse_time(time_str)
    if not seconds:
        return await message.reply_text("❌ Invalid time. Examples: <code>10m</code>, <code>2h</code>, <code>1d</code>")

    if await is_admin(message.chat.id, target.id):
        return await message.reply_text("❌ Cannot mute an admin.")

    try:
        from datetime import timedelta
        until = datetime.utcnow() + timedelta(seconds=seconds)
        await app.restrict_chat_member(
            message.chat.id, target.id,
            enums.ChatPermissions(),
            until_date=until,
        )
        await message.reply_text(
            f"🔇 {_mention(target)} muted for <b>{time_str}</b>."
        )
        await _log(
            f"<b>🔇 User Temp-Muted</b>\n"
            f"Chat: <b>{message.chat.title}</b>\n"
            f"User: {_mention(target)} (<code>{target.id}</code>)\n"
            f"Duration: {time_str}\n"
            f"By: {_mention(message.from_user)}\n"
            f"Time: {_now()}"
        )
    except ChatAdminRequired:
        await message.reply_text("❌ I need restrict permissions.")


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


# ═════════════════════════════════════════════════════════════════════════════
#  DELETE / PURGE
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
        await message.reply_text("❌ Couldn't delete the message.")


@app.on_message(filters.command("purge") & filters.group)
async def cmd_purge(_, message: types.Message):
    """bb: /purge — Delete all messages from replied message up to this one."""
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
    await asyncio.sleep(5)
    try:
        await sent.delete()
    except Exception:
        pass

    await _log(
        f"<b>🗑 Purge</b>\n"
        f"Chat: <b>{message.chat.title}</b> (<code>{message.chat.id}</code>)\n"
        f"Deleted: {deleted} messages\n"
        f"By: {_mention(message.from_user)}\n"
        f"Time: {_now()}"
    )


@app.on_message(filters.command("delall") & filters.group)
async def cmd_delall(_, message: types.Message):
    """bb: /delall — Delete all messages from a specific user (reply or @username)."""
    if not await _assert_bb(message):
        return await message.reply_text("❌ Admins/authorised users only.")

    target = await _get_target(message)
    if not target:
        return await message.reply_text("❌ Reply to a user or provide @username.")

    if await is_admin(message.chat.id, target.id):
        return await message.reply_text("❌ Cannot delete messages of an admin.")

    try:
        await app.delete_user_history(message.chat.id, target.id)
        await message.reply_text(f"🗑 Deleted all messages from {_mention(target)}.")
        await _log(
            f"<b>🗑 Delete All Messages</b>\n"
            f"Chat: <b>{message.chat.title}</b> (<code>{message.chat.id}</code>)\n"
            f"User: {_mention(target)} (<code>{target.id}</code>)\n"
            f"By: {_mention(message.from_user)}\n"
            f"Time: {_now()}"
        )
    except Exception as e:
        await message.reply_text(f"❌ Failed: {e}")


# ═════════════════════════════════════════════════════════════════════════════
#  WARNINGS
# ═════════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("warn") & filters.group)
async def cmd_warn(_, message: types.Message):
    """bb: Warn a user. On reaching warn limit, they are banned."""
    if not await _assert_bb(message):
        return await message.reply_text("❌ Admins/authorised users only.")

    target = await _get_target(message)
    if not target:
        return await message.reply_text("❌ Reply to a user or provide @username/user_id.")

    if await is_admin(message.chat.id, target.id):
        return await message.reply_text("❌ Cannot warn an admin.")

    args = message.text.split(None, 2)
    reason = ""
    if message.reply_to_message and len(args) > 1:
        reason = args[1]
    elif not message.reply_to_message and len(args) > 2:
        reason = args[2]

    count = await db.warn_user(message.chat.id, target.id, reason)
    limit = await db.get_warn_limit(message.chat.id)

    if count >= limit:
        try:
            await app.ban_chat_member(message.chat.id, target.id)
            await db.reset_warns(message.chat.id, target.id)
            await message.reply_text(
                f"⚠️ {_mention(target)} has reached <b>{limit}</b> warnings and has been <b>banned</b>."
            )
            await _log(
                f"<b>⚠️➡️🚫 Warn-Ban</b>\n"
                f"Chat: <b>{message.chat.title}</b>\n"
                f"User: {_mention(target)} (<code>{target.id}</code>)\n"
                f"Warns hit limit ({limit})\n"
                f"By: {_mention(message.from_user)}\n"
                f"Time: {_now()}"
            )
        except Exception:
            await message.reply_text("⚠️ Warn limit reached but I couldn't ban the user.")
    else:
        await message.reply_text(
            f"⚠️ {_mention(target)} has been warned.\n"
            f"Warnings: <b>{count}/{limit}</b>"
            + (f"\nReason: {reason}" if reason else "")
        )
        await _log(
            f"<b>⚠️ User Warned</b>\n"
            f"Chat: <b>{message.chat.title}</b>\n"
            f"User: {_mention(target)} (<code>{target.id}</code>)\n"
            f"Warns: {count}/{limit}\n"
            f"Reason: {reason or 'None'}\n"
            f"By: {_mention(message.from_user)}\n"
            f"Time: {_now()}"
        )


@app.on_message(filters.command("warns") & filters.group)
async def cmd_warns(_, message: types.Message):
    """Check warns for yourself or a replied user."""
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
        return await message.reply_text("❌ Reply to a user or provide @username/user_id.")

    await db.reset_warns(message.chat.id, target.id)
    await message.reply_text(f"✅ Warnings cleared for {_mention(target)}.")
    await _log(
        f"<b>✅ Warns Reset</b>\n"
        f"Chat: <b>{message.chat.title}</b>\n"
        f"User: {_mention(target)} (<code>{target.id}</code>)\n"
        f"By: {_mention(message.from_user)}\n"
        f"Time: {_now()}"
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
    """/get <name> — Retrieve a saved note."""
    args = message.text.split(None, 1)
    if len(args) < 2:
        return await message.reply_text("Usage: /get <name>")

    content = await db.get_note(message.chat.id, args[1])
    if not content:
        return await message.reply_text(f"❌ No note named <b>{args[1]}</b>.")
    await message.reply_text(content)


@app.on_message(filters.command("notes") & filters.group)
async def cmd_list_notes(_, message: types.Message):
    """List all saved notes."""
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


# Hashtag note retrieval: #notename in chat
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


@app.on_message(filters.group & filters.text & ~filters.command([]))
async def on_filter_check(_, message: types.Message):
    """Auto-reply to messages matching a saved filter keyword."""
    if not message.text or message.text.startswith("/"):
        return
    words = message.text.lower().split()
    for word in words:
        reply = await db.get_filter(message.chat.id, word)
        if reply:
            await message.reply_text(reply)
            return


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
    """aa: /lock <type> — Lock a message type. Types: sticker gif link media poll all"""
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
        chat = await app.get_chat(message.chat.id)
        current = chat.permissions
        new_perms = enums.ChatPermissions(
            can_send_messages=current.can_send_messages,
            can_send_media_messages=False if lock_type in ("media", "all") else current.can_send_media_messages,
            can_send_other_messages=False if lock_type in ("sticker", "gif", "poll", "all") else current.can_send_other_messages,
            can_add_web_page_previews=False if lock_type in ("link", "all") else current.can_add_web_page_previews,
        )
        await app.set_chat_permissions(message.chat.id, new_perms)
        await message.reply_text(f"🔒 Locked: <b>{lock_type}</b>")
    except ChatAdminRequired:
        await message.reply_text("❌ I need admin permissions to change chat permissions.")


@app.on_message(filters.command("unlock") & filters.group)
async def cmd_unlock(_, message: types.Message):
    """aa: /unlock <type>"""
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins can unlock.")

    args = message.text.split(None, 1)
    if len(args) < 2:
        return await message.reply_text("Usage: /unlock <type>\nTypes: " + ", ".join(LOCK_TYPES.keys()) + ", all")

    lock_type = args[1].lower()
    to_unlock = list(LOCK_TYPES.keys()) if lock_type == "all" else [lock_type]
    for lt in to_unlock:
        await db.remove_lock(message.chat.id, lt)

    try:
        await app.set_chat_permissions(
            message.chat.id,
            enums.ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
        await message.reply_text(f"🔓 Unlocked: <b>{lock_type}</b>")
    except ChatAdminRequired:
        await message.reply_text("❌ I need admin permissions.")


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
    """aa: /setflood <number|off> — Set max messages before muting."""
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


@app.on_message(filters.group & ~filters.service)
async def antiflood_check(_, message: types.Message):
    """Automatically mutes users who send too many messages rapidly."""
    if not message.from_user:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id

    # Skip admins and bot
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
            await app.restrict_chat_member(chat_id, user_id, enums.ChatPermissions())
            await message.reply_text(
                f"🚦 {_mention(message.from_user)} was <b>muted</b> for flooding."
            )
            await _log(
                f"<b>🚦 Antiflood Mute</b>\n"
                f"Chat: <b>{message.chat.title}</b>\n"
                f"User: {_mention(message.from_user)} (<code>{user_id}</code>)\n"
                f"Exceeded limit of {limit} messages\n"
                f"Time: {_now()}"
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
        return await message.reply_text("❌ Reply to a user or provide @username/user_id.")

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
        await message.reply_text(f"⬆️ {_mention(target)} has been <b>promoted</b>.")
        await _log(
            f"<b>⬆️ User Promoted</b>\n"
            f"Chat: <b>{message.chat.title}</b>\n"
            f"User: {_mention(target)} (<code>{target.id}</code>)\n"
            f"By: {_mention(message.from_user)}\n"
            f"Time: {_now()}"
        )
    except ChatAdminRequired:
        await message.reply_text("❌ I need promote permissions.")


@app.on_message(filters.command("demote") & filters.group)
async def cmd_demote(_, message: types.Message):
    """aa: Demote an admin to regular member."""
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins can demote.")

    target = await _get_target(message)
    if not target:
        return await message.reply_text("❌ Reply to a user or provide @username/user_id.")

    try:
        await app.promote_chat_member(
            message.chat.id, target.id,
            privileges=types.ChatPrivileges(),
        )
        await message.reply_text(f"⬇️ {_mention(target)} has been <b>demoted</b>.")
        await _log(
            f"<b>⬇️ User Demoted</b>\n"
            f"Chat: <b>{message.chat.title}</b>\n"
            f"User: {_mention(target)} (<code>{target.id}</code>)\n"
            f"By: {_mention(message.from_user)}\n"
            f"Time: {_now()}"
        )
    except ChatAdminRequired:
        await message.reply_text("❌ I need promote permissions.")


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
        await message.reply_to_message.pin()
        await message.reply_text("📌 Message pinned.")
    except ChatAdminRequired:
        await message.reply_text("❌ I need pin permissions.")


@app.on_message(filters.command("unpin") & filters.group)
async def cmd_unpin(_, message: types.Message):
    """aa: Unpin the pinned message."""
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins can unpin.")

    try:
        await app.unpin_chat_message(message.chat.id)
        await message.reply_text("📌 Message unpinned.")
    except ChatAdminRequired:
        await message.reply_text("❌ I need pin permissions.")


# ═════════════════════════════════════════════════════════════════════════════
#  CHAT INFO / USER INFO
# ═════════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("info") & filters.group)
async def cmd_info(_, message: types.Message):
    """Get info about yourself or a replied user."""
    target = await _get_target(message) or message.from_user
    chat = message.chat

    try:
        member = await app.get_chat_member(chat.id, target.id)
        status = str(member.status).split(".")[-1].title()
    except UserNotParticipant:
        status = "Not in group"
    except Exception:
        status = "Unknown"

    name = (target.first_name or "") + (" " + target.last_name if target.last_name else "")
    warns = await db.get_warns(chat.id, target.id)
    warn_limit = await db.get_warn_limit(chat.id)
    username = f"@{target.username}" if target.username else "N/A"

    text = (
        f"👤 <b>User Info</b>\n\n"
        f"Name: {_mention(target)}\n"
        f"ID: <code>{target.id}</code>\n"
        f"Username: {username}\n"
        f"Status: {status}\n"
        f"Warnings: {len(warns)}/{warn_limit}\n"
        f"Bot: {'Yes' if target.is_bot else 'No'}"
    )
    await message.reply_text(text)


@app.on_message(filters.command("chatinfo") & filters.group)
async def cmd_chatinfo(_, message: types.Message):
    chat = message.chat
    count = await app.get_chat_members_count(chat.id)
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
    """List all admins in the group."""
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


# ═════════════════════════════════════════════════════════════════════════════
#  HELP
# ═════════════════════════════════════════════════════════════════════════════


# ═════════════════════════════════════════════════════════════════════════════
#  ANTI-LINK  (auto-delete links; admins exempt)
# ═════════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("antilink") & filters.group)
async def cmd_antilink(_, message: types.Message):
    """aa: /antilink on|off — Toggle automatic link removal."""
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins.")

    args = message.text.split()
    if len(args) < 2 or args[1].lower() not in ("on", "off"):
        current = await db.get_antilink(message.chat.id)
        status = "🔴 ON" if current else "🟢 OFF"
        return await message.reply_text(
            f"🔗 Anti-link is currently <b>{status}</b>.\nUse /antilink on or /antilink off."
        )

    enable = args[1].lower() == "on"
    await db.set_antilink(message.chat.id, enable)
    await message.reply_text(
        f"✅ Anti-link <b>{'enabled' if enable else 'disabled'}</b>. "
        f"{'Links will be deleted and non-admins warned.' if enable else 'Links are now allowed.'}"
    )
    await _log(
        f"<b>🔗 Anti-link {'ON' if enable else 'OFF'}</b>\n"
        f"Chat: <b>{message.chat.title}</b> (<code>{message.chat.id}</code>)\n"
        f"By: {_mention(message.from_user)}\n"
        f"Time: {_now()}"
    )


@app.on_message(filters.group & filters.text & ~filters.command([]))
async def antilink_check(_, message: types.Message):
    """Delete messages containing links if antilink is on and sender is not admin."""
    if not message.from_user:
        return
    if not await db.get_antilink(message.chat.id):
        return
    if await is_admin(message.chat.id, message.from_user.id):
        return
    if message.from_user.id in app.sudoers:
        return

    if message.text and _URL_RE.search(message.text):
        try:
            await message.delete()
        except Exception:
            pass
        warn_count = await db.warn_user(message.chat.id, message.from_user.id, "Sent a link")
        limit = await db.get_warn_limit(message.chat.id)
        sent = await message.reply_text(
            f"🔗 {_mention(message.from_user)} links are not allowed here!\n"
            f"⚠️ Warning <b>{warn_count}/{limit}</b>."
        )
        await asyncio.sleep(5)
        try:
            await sent.delete()
        except Exception:
            pass


# ═════════════════════════════════════════════════════════════════════════════
#  ANTI-FORWARD  (delete forwarded messages; admins exempt)
# ═════════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("antiforward") & filters.group)
async def cmd_antiforward(_, message: types.Message):
    """aa: /antiforward on|off — Toggle deletion of forwarded messages."""
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins.")

    args = message.text.split()
    if len(args) < 2 or args[1].lower() not in ("on", "off"):
        current = await db.get_antiforward(message.chat.id)
        status = "🔴 ON" if current else "🟢 OFF"
        return await message.reply_text(
            f"↩️ Anti-forward is currently <b>{status}</b>.\nUse /antiforward on or /antiforward off."
        )

    enable = args[1].lower() == "on"
    await db.set_antiforward(message.chat.id, enable)
    await message.reply_text(
        f"✅ Anti-forward <b>{'enabled' if enable else 'disabled'}</b>."
    )
    await _log(
        f"<b>↩️ Anti-forward {'ON' if enable else 'OFF'}</b>\n"
        f"Chat: <b>{message.chat.title}</b> (<code>{message.chat.id}</code>)\n"
        f"By: {_mention(message.from_user)}\n"
        f"Time: {_now()}"
    )


@app.on_message(filters.group & filters.forwarded)
async def antiforward_check(_, message: types.Message):
    """Delete forwarded messages if antiforward is on and sender is not admin."""
    if not message.from_user:
        return
    if not await db.get_antiforward(message.chat.id):
        return
    if await is_admin(message.chat.id, message.from_user.id):
        return
    if message.from_user.id in app.sudoers:
        return

    try:
        await message.delete()
    except Exception:
        pass
    sent = await message.reply_text(
        f"↩️ {_mention(message.from_user)} forwarded messages are not allowed here!"
    )
    await asyncio.sleep(5)
    try:
        await sent.delete()
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════════════════════
#  ANTI-WORDS  (banned words managed by admin via bot DM)
# ═════════════════════════════════════════════════════════════════════════════
# Admins set banned words by DMing the bot.
# /setantiword <chat_id> <word>
# /delantiword <chat_id> <word>
# /listantiwords <chat_id>

@app.on_message(filters.private & filters.command("setantiword"))
async def cmd_set_antiword(_, message: types.Message):
    """Admin DM: /setantiword <chat_id> <word> — Add a banned word for a group."""
    if message.from_user.id not in app.sudoers:
        if not hasattr(app, "_antiword_admins"):
            app._antiword_admins = set()
        # Allow group admins who have previously verified
        pass

    args = message.text.split(None, 2)
    if len(args) < 3:
        return await message.reply_text(
            "Usage: /setantiword <chat_id> <word>\n"
            "Example: /setantiword -1001234567890 badword"
        )

    try:
        chat_id = int(args[1])
    except ValueError:
        return await message.reply_text("❌ Invalid chat ID.")

    # Verify sender is admin of that group
    if message.from_user.id not in app.sudoers:
        if not await is_admin(chat_id, message.from_user.id):
            return await message.reply_text("❌ You are not an admin of that group.")

    word = args[2].lower().strip()
    await db.add_antiword(chat_id, word)
    await message.reply_text(f"✅ Word <code>{word}</code> added to banned list for chat <code>{chat_id}</code>.")


@app.on_message(filters.private & filters.command("delantiword"))
async def cmd_del_antiword(_, message: types.Message):
    """Admin DM: /delantiword <chat_id> <word> — Remove a banned word."""
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

    word = args[2].lower().strip()
    removed = await db.remove_antiword(chat_id, word)
    if removed:
        await message.reply_text(f"✅ Word <code>{word}</code> removed.")
    else:
        await message.reply_text(f"❌ Word <code>{word}</code> not found in the list.")


@app.on_message(filters.private & filters.command("listantiwords"))
async def cmd_list_antiwords(_, message: types.Message):
    """Admin DM: /listantiwords <chat_id> — List all banned words for a group."""
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
        return await message.reply_text(f"📭 No banned words for chat <code>{chat_id}</code>.")
    await message.reply_text(
        f"🚫 <b>Banned words for {chat_id}:</b>\n" +
        "\n".join(f"• <code>{w}</code>" for w in words)
    )


@app.on_message(filters.private & filters.command("clearantiwords"))
async def cmd_clear_antiwords(_, message: types.Message):
    """Admin DM: /clearantiwords <chat_id> — Clear all banned words."""
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


@app.on_message(filters.group & filters.text & ~filters.command([]))
async def antiword_check(_, message: types.Message):
    """Delete messages containing banned words and warn the user."""
    if not message.from_user:
        return
    if await is_admin(message.chat.id, message.from_user.id):
        return
    if message.from_user.id in app.sudoers:
        return

    words = await db.get_antiwords(message.chat.id)
    if not words:
        return

    text_lower = (message.text or "").lower()
    hit = next((w for w in words if w in text_lower), None)
    if not hit:
        return

    try:
        await message.delete()
    except Exception:
        pass

    warn_count = await db.warn_user(message.chat.id, message.from_user.id, f"Used banned word: {hit}")
    limit = await db.get_warn_limit(message.chat.id)

    # Build message with admin-only remove-warn button
    btn = types.InlineKeyboardMarkup([[
        types.InlineKeyboardButton("✅ Remove Warn", callback_data=f"rmwarn_{message.from_user.id}")
    ]])

    sent = await app.send_message(
        message.chat.id,
        f"🚫 {_mention(message.from_user)} your message contained a banned word.\n"
        f"⚠️ Warning <b>{warn_count}/{limit}</b>.",
        reply_markup=btn,
    )

    if warn_count >= limit:
        try:
            await app.ban_chat_member(message.chat.id, message.from_user.id)
            await db.reset_warns(message.chat.id, message.from_user.id)
            await sent.edit_text(
                f"🚫 {_mention(message.from_user)} reached the warn limit and has been <b>banned</b>.",
            )
        except Exception:
            pass


@app.on_callback_query(filters.regex(r"^rmwarn_(\d+)$"))
async def cb_remove_warn(_, query: types.CallbackQuery):
    """Admin-only button to remove a warning issued for anti-word."""
    chat = query.message.chat
    if not await is_admin(chat.id, query.from_user.id):
        return await query.answer("❌ Only admins can remove warnings.", show_alert=True)

    target_id = int(query.matches[0].group(1))
    warns = await db.get_warns(chat.id, target_id)
    if not warns:
        return await query.answer("✅ User already has no warnings.", show_alert=True)

    # Remove last warning
    warns.pop()
    await db.db.warns.update_one(
        {"_id": f"{chat.id}:{target_id}"},
        {"$set": {"warns": warns}},
        upsert=True,
    )
    await query.answer("✅ One warning removed.", show_alert=True)
    try:
        limit = await db.get_warn_limit(chat.id)
        await query.message.edit_reply_markup(
            types.InlineKeyboardMarkup([[
                types.InlineKeyboardButton(
                    f"✅ Warn removed by {query.from_user.first_name} ({len(warns)}/{limit})",
                    callback_data="noop"
                )
            ]])
        )
    except Exception:
        pass
    await _log(
        f"<b>✅ Warn Removed via Button</b>\n"
        f"Chat: <b>{chat.title}</b> (<code>{chat.id}</code>)\n"
        f"Target ID: <code>{target_id}</code>\n"
        f"By: {_mention(query.from_user)}\n"
        f"Time: {_now()}"
    )


# ═════════════════════════════════════════════════════════════════════════════
#  TAGALL  (mention all members)
# ═════════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("tagall") & filters.group)
async def cmd_tagall(_, message: types.Message):
    """bb: /tagall [message] — Tag all members in the group."""
    if not await _assert_bb(message):
        return await message.reply_text("❌ Admins/authorised users only.")

    args = message.text.split(None, 1)
    header = args[1] if len(args) > 1 else "📣 Attention everyone!"

    members = []
    async for member in app.get_chat_members(message.chat.id):
        if not member.user.is_bot and not member.user.is_deleted:
            members.append(member.user)

    if not members:
        return await message.reply_text("❌ No members found.")

    # Send in chunks of 5 mentions per message to avoid flood
    chunk_size = 5
    await message.reply_text(f"<b>{header}</b>")
    for i in range(0, len(members), chunk_size):
        chunk = members[i:i + chunk_size]
        mentions = "  ".join(
            f'<a href="tg://user?id={u.id}">{u.first_name or u.id}</a>'
            for u in chunk
        )
        await app.send_message(message.chat.id, mentions)
        await asyncio.sleep(0.5)


# ═════════════════════════════════════════════════════════════════════════════
#  CAPTCHA
# ═════════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("captcha") & filters.group)
async def cmd_captcha(_, message: types.Message):
    """aa: /captcha on|off — Toggle captcha verification for new members."""
    if not await _assert_admin(message):
        return await message.reply_text("❌ Only group admins.")

    args = message.text.split()
    if len(args) < 2 or args[1].lower() not in ("on", "off"):
        current = await db.get_captcha(message.chat.id)
        status = "🔴 ON" if current else "🟢 OFF"
        return await message.reply_text(
            f"🔐 Captcha is currently <b>{status}</b>.\nUse /captcha on or /captcha off."
        )

    enable = args[1].lower() == "on"
    await db.set_captcha(message.chat.id, enable)
    await message.reply_text(
        f"✅ Captcha <b>{'enabled' if enable else 'disabled'}</b>."
    )


async def _send_captcha(chat: types.Chat, user: types.User) -> None:
    """Mute the new user and send them a math captcha to solve."""
    # Mute until verified
    try:
        await app.restrict_chat_member(
            chat.id,
            user.id,
            enums.ChatPermissions(
                can_send_messages=False,
                can_send_media_messages=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False,
            ),
        )
    except Exception:
        return

    # Generate a simple math question
    a, b = random.randint(1, 15), random.randint(1, 15)
    answer = a + b
    wrong = [answer + random.randint(1, 5), answer - random.randint(1, 5), answer + random.randint(6, 12)]
    random.shuffle(wrong)
    options = wrong[:3] + [answer]
    random.shuffle(options)

    buttons = types.InlineKeyboardMarkup([[
        types.InlineKeyboardButton(str(opt), callback_data=f"captcha_{user.id}_{opt}_{answer}")
        for opt in options
    ]])

    sent = await app.send_message(
        chat.id,
        f"🔐 Welcome {_mention(user)}!\n"
        f"Please solve this to verify you're human:\n\n"
        f"<b>{a} + {b} = ?</b>\n\n"
        f"You have <b>60 seconds</b> or you will be kicked.",
        reply_markup=buttons,
    )

    if chat.id not in _captcha_pending:
        _captcha_pending[chat.id] = {}
    _captcha_pending[chat.id][user.id] = {"msg_id": sent.id, "answer": answer}

    # Auto-kick after 60s if not verified
    await asyncio.sleep(60)
    if chat.id in _captcha_pending and user.id in _captcha_pending[chat.id]:
        _captcha_pending[chat.id].pop(user.id)
        try:
            await app.ban_chat_member(chat.id, user.id)
            await app.unban_chat_member(chat.id, user.id)  # kick, not ban
            await sent.edit_text(
                f"⏰ {_mention(user)} failed captcha and was kicked."
            )
        except Exception:
            pass


@app.on_callback_query(filters.regex(r"^captcha_(\d+)_(\d+)_(\d+)$"))
async def cb_captcha(_, query: types.CallbackQuery):
    """Handle captcha button presses."""
    user_id = int(query.matches[0].group(1))
    chosen = int(query.matches[0].group(2))
    answer = int(query.matches[0].group(3))
    chat = query.message.chat

    # Only the target user can answer
    if query.from_user.id != user_id:
        return await query.answer("❌ This captcha is not for you!", show_alert=True)

    if chosen == answer:
        # Correct — restore permissions
        if chat.id in _captcha_pending:
            _captcha_pending[chat.id].pop(user_id, None)

        try:
            default = (await app.get_chat(chat.id)).permissions
            await app.restrict_chat_member(
                chat.id,
                user_id,
                default or enums.ChatPermissions(
                    can_send_messages=True,
                    can_send_media_messages=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True,
                ),
            )
        except Exception:
            pass

        await query.message.edit_text(
            f"✅ {_mention(query.from_user)} passed the captcha! Welcome to <b>{chat.title}</b>. 🎉"
        )
    else:
        # Wrong answer
        await query.answer("❌ Wrong answer! Try again.", show_alert=True)


GMGMT_HELP = """
<b>🛡 Group Management Commands</b>

<b>─── Welcome / Goodbye (aa = group admin) ───</b>
/setwelcome &lt;text&gt; — Set welcome message (reply to photo for custom image)
/setwelcome &lt;secs&gt; &lt;text&gt; — Set welcome + auto-delete delay
/delwelcome — Remove welcome message
/setgoodbye &lt;text&gt; — Set goodbye message (reply to photo for custom image)
/delgoodbye — Remove goodbye message

Variables: {mention} {first} {last} {title} {id}

<b>─── Moderation (bb = admin / authorised) ───</b>
/ban — Ban a user (reply or @username)
/unban — Unban a user
/kick — Kick (removable ban)
/mute — Mute a user
/unmute — Unmute a user
/tmute &lt;10m|2h|1d&gt; — Temp mute

<b>─── Messages ───</b>
/del — Delete replied message
/purge — Purge from replied msg to this msg
/delall — Delete all msgs from a user

<b>─── Warnings ───</b>
/warn [reason] — Warn a user (ban at limit)
/warns — Show user's warnings
/resetwarn — Clear all warnings
/setwarnlimit &lt;n&gt; — Set warn limit (aa)

<b>─── Notes ───</b>
/save &lt;name&gt; &lt;text&gt; — Save a note (aa)
/get &lt;name&gt; — Retrieve a note
#notename — Auto-retrieve note by hashtag
/notes — List all notes
/clear &lt;name&gt; — Delete a note (aa)

<b>─── Filters ───</b>
/filter &lt;keyword&gt; &lt;reply&gt; — Add auto-reply (aa)
/filters — List filters
/stopfilter &lt;keyword&gt; — Remove filter (aa)

<b>─── Anti-Link (aa) ───</b>
/antilink on|off — Auto-delete links (admins exempt)

<b>─── Anti-Forward (aa) ───</b>
/antiforward on|off — Auto-delete forwarded messages (admins exempt)

<b>─── Anti-Words (via bot DM) ───</b>
/setantiword &lt;chat_id&gt; &lt;word&gt; — Add banned word
/delantiword &lt;chat_id&gt; &lt;word&gt; — Remove banned word
/listantiwords &lt;chat_id&gt; — List banned words
/clearantiwords &lt;chat_id&gt; — Clear all banned words
⚠️ Violations: message deleted + warn issued + admin remove-warn button shown

<b>─── Locks (aa) ───</b>
/lock &lt;type|all&gt; — Lock message type
/unlock &lt;type|all&gt; — Unlock
/locks — Show active locks
Types: sticker, gif, link, media, poll, all

<b>─── AntiFlood (aa) ───</b>
/setflood &lt;n|off&gt; — Set flood limit
/flood — Show current limit

<b>─── Admin Tools (aa) ───</b>
/promote — Promote user to admin
/demote — Demote admin
/pin — Pin replied message
/unpin — Unpin message
/invitelink — Generate invite link

<b>─── Tag All (bb) ───</b>
/tagall [message] — Mention all members

<b>─── Captcha (aa) ───</b>
/captcha on|off — Math captcha for new members (auto-kick on timeout)

<b>─── Info ───</b>
/info — User info
/chatinfo — Chat info
/adminlist — List all admins
"""


@app.on_message(filters.command("gmhelp"))
async def cmd_gmhelp(_, message: types.Message):
    await message.reply_text(GMGMT_HELP)
