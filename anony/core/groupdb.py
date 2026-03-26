# Group Management Database Methods
# Extends MongoDB class with all group management data operations


class GroupDB:
    """
    Mixin class that adds group management database methods.
    These are merged into the MongoDB class at runtime.
    """

    # ─────────────────────────────────────────────────────────────────────────
    # WELCOME / GOODBYE MESSAGES
    # ─────────────────────────────────────────────────────────────────────────

    async def set_welcome(self, chat_id: int, text: str, delete_after: int = 0, photo_url: str = None) -> None:
        """Store a custom welcome message, optional auto-delete delay (seconds), and optional photo."""
        update_data = {"welcome": text, "welcome_delete": delete_after}
        if photo_url is not None:
            update_data["welcome_photo"] = photo_url
        await self.db.greetings.update_one(
            {"_id": chat_id},
            {"$set": update_data},
            upsert=True,
        )

    async def get_welcome(self, chat_id: int) -> dict:
        """Return {'text': str, 'delete_after': int, 'photo': str|None} or empty dict."""
        doc = await self.db.greetings.find_one({"_id": chat_id})
        if not doc:
            return {}
        return {
            "text": doc.get("welcome", ""),
            "delete_after": doc.get("welcome_delete", 0),
            "photo": doc.get("welcome_photo", None),
        }

    async def del_welcome(self, chat_id: int) -> None:
        await self.db.greetings.update_one(
            {"_id": chat_id}, {"$unset": {"welcome": "", "welcome_delete": ""}}
        )

    async def set_goodbye(self, chat_id: int, text: str, photo_url: str = None) -> None:
        update_data = {"goodbye": text}
        if photo_url is not None:
            update_data["goodbye_photo"] = photo_url
        await self.db.greetings.update_one(
            {"_id": chat_id}, {"$set": update_data}, upsert=True
        )

    async def get_goodbye(self, chat_id: int) -> dict:
        doc = await self.db.greetings.find_one({"_id": chat_id})
        if not doc:
            return {}
        return {
            "text": doc.get("goodbye", ""),
            "photo": doc.get("goodbye_photo", None),
        }

    async def del_goodbye(self, chat_id: int) -> None:
        await self.db.greetings.update_one(
            {"_id": chat_id}, {"$unset": {"goodbye": ""}}
        )

    # ─────────────────────────────────────────────────────────────────────────
    # MUTE / UNMUTE
    # ─────────────────────────────────────────────────────────────────────────

    async def mute_user(self, chat_id: int, user_id: int) -> None:
        await self.db.mutes.update_one(
            {"_id": chat_id},
            {"$addToSet": {"user_ids": user_id}},
            upsert=True,
        )

    async def unmute_user(self, chat_id: int, user_id: int) -> None:
        await self.db.mutes.update_one(
            {"_id": chat_id}, {"$pull": {"user_ids": user_id}}
        )

    async def is_muted(self, chat_id: int, user_id: int) -> bool:
        doc = await self.db.mutes.find_one({"_id": chat_id})
        return user_id in (doc.get("user_ids", []) if doc else [])

    # ─────────────────────────────────────────────────────────────────────────
    # WARNINGS
    # ─────────────────────────────────────────────────────────────────────────

    async def warn_user(self, chat_id: int, user_id: int, reason: str = "") -> int:
        """Add a warning; returns new warn count."""
        doc = await self.db.warns.find_one({"_id": f"{chat_id}:{user_id}"})
        warns = doc.get("warns", []) if doc else []
        warns.append(reason or "No reason provided")
        await self.db.warns.update_one(
            {"_id": f"{chat_id}:{user_id}"},
            {"$set": {"warns": warns}},
            upsert=True,
        )
        return len(warns)

    async def get_warns(self, chat_id: int, user_id: int) -> list:
        doc = await self.db.warns.find_one({"_id": f"{chat_id}:{user_id}"})
        return doc.get("warns", []) if doc else []

    async def reset_warns(self, chat_id: int, user_id: int) -> None:
        await self.db.warns.delete_one({"_id": f"{chat_id}:{user_id}"})

    async def get_warn_limit(self, chat_id: int) -> int:
        doc = await self.db.warn_settings.find_one({"_id": chat_id})
        return doc.get("limit", 3) if doc else 3

    async def set_warn_limit(self, chat_id: int, limit: int) -> None:
        await self.db.warn_settings.update_one(
            {"_id": chat_id}, {"$set": {"limit": limit}}, upsert=True
        )

    # ─────────────────────────────────────────────────────────────────────────
    # NOTES
    # ─────────────────────────────────────────────────────────────────────────

    async def set_note(self, chat_id: int, name: str, content: str) -> None:
        await self.db.notes.update_one(
            {"_id": f"{chat_id}:{name.lower()}"},
            {"$set": {"content": content, "chat_id": chat_id, "name": name.lower()}},
            upsert=True,
        )

    async def get_note(self, chat_id: int, name: str) -> str:
        doc = await self.db.notes.find_one({"_id": f"{chat_id}:{name.lower()}"})
        return doc.get("content", "") if doc else ""

    async def del_note(self, chat_id: int, name: str) -> bool:
        result = await self.db.notes.delete_one({"_id": f"{chat_id}:{name.lower()}"})
        return result.deleted_count > 0

    async def get_all_notes(self, chat_id: int) -> list:
        return [
            doc["name"]
            async for doc in self.db.notes.find({"chat_id": chat_id})
        ]

    # ─────────────────────────────────────────────────────────────────────────
    # FILTERS (auto-reply)
    # ─────────────────────────────────────────────────────────────────────────

    async def set_filter(self, chat_id: int, keyword: str, reply: str) -> None:
        await self.db.filters.update_one(
            {"_id": f"{chat_id}:{keyword.lower()}"},
            {"$set": {"reply": reply, "chat_id": chat_id, "keyword": keyword.lower()}},
            upsert=True,
        )

    async def get_filter(self, chat_id: int, keyword: str) -> str:
        doc = await self.db.filters.find_one({"_id": f"{chat_id}:{keyword.lower()}"})
        return doc.get("reply", "") if doc else ""

    async def del_filter(self, chat_id: int, keyword: str) -> bool:
        result = await self.db.filters.delete_one({"_id": f"{chat_id}:{keyword.lower()}"})
        return result.deleted_count > 0

    async def get_all_filters(self, chat_id: int) -> list:
        return [
            doc["keyword"]
            async for doc in self.db.filters.find({"chat_id": chat_id})
        ]

    # ─────────────────────────────────────────────────────────────────────────
    # ANTIFLOOD
    # ─────────────────────────────────────────────────────────────────────────

    async def set_flood_limit(self, chat_id: int, limit: int) -> None:
        await self.db.flood.update_one(
            {"_id": chat_id}, {"$set": {"limit": limit}}, upsert=True
        )

    async def get_flood_limit(self, chat_id: int) -> int:
        doc = await self.db.flood.find_one({"_id": chat_id})
        return doc.get("limit", 0) if doc else 0  # 0 = disabled

    # ─────────────────────────────────────────────────────────────────────────
    # LOCKED TOPICS (lock specific message types)
    # ─────────────────────────────────────────────────────────────────────────

    async def get_locks(self, chat_id: int) -> list:
        doc = await self.db.locks.find_one({"_id": chat_id})
        return doc.get("types", []) if doc else []

    async def add_lock(self, chat_id: int, lock_type: str) -> None:
        await self.db.locks.update_one(
            {"_id": chat_id},
            {"$addToSet": {"types": lock_type}},
            upsert=True,
        )

    async def remove_lock(self, chat_id: int, lock_type: str) -> None:
        await self.db.locks.update_one(
            {"_id": chat_id}, {"$pull": {"types": lock_type}}
        )

    # ─────────────────────────────────────────────────────────────────────────
    # ANTI-WORDS  (per-group badword list, managed via bot DM)
    # ─────────────────────────────────────────────────────────────────────────

    async def add_antiword(self, chat_id: int, word: str) -> None:
        await self.db.antiwords.update_one(
            {"_id": chat_id},
            {"$addToSet": {"words": word.lower()}},
            upsert=True,
        )

    async def remove_antiword(self, chat_id: int, word: str) -> bool:
        result = await self.db.antiwords.update_one(
            {"_id": chat_id},
            {"$pull": {"words": word.lower()}},
        )
        return result.modified_count > 0

    async def get_antiwords(self, chat_id: int) -> list:
        doc = await self.db.antiwords.find_one({"_id": chat_id})
        return doc.get("words", []) if doc else []

    async def clear_antiwords(self, chat_id: int) -> None:
        await self.db.antiwords.delete_one({"_id": chat_id})

    # ─────────────────────────────────────────────────────────────────────────
    # ANTI-LINK toggle
    # ─────────────────────────────────────────────────────────────────────────

    async def set_antilink(self, chat_id: int, enabled: bool) -> None:
        await self.db.chat_settings.update_one(
            {"_id": chat_id}, {"$set": {"antilink": enabled}}, upsert=True
        )

    async def get_antilink(self, chat_id: int) -> bool:
        doc = await self.db.chat_settings.find_one({"_id": chat_id})
        return bool(doc.get("antilink", False)) if doc else False

    # ─────────────────────────────────────────────────────────────────────────
    # ANTI-FORWARD toggle
    # ─────────────────────────────────────────────────────────────────────────

    async def set_antiforward(self, chat_id: int, enabled: bool) -> None:
        await self.db.chat_settings.update_one(
            {"_id": chat_id}, {"$set": {"antiforward": enabled}}, upsert=True
        )

    async def get_antiforward(self, chat_id: int) -> bool:
        doc = await self.db.chat_settings.find_one({"_id": chat_id})
        return bool(doc.get("antiforward", False)) if doc else False

    # ─────────────────────────────────────────────────────────────────────────
    # CAPTCHA toggle
    # ─────────────────────────────────────────────────────────────────────────

    async def set_captcha(self, chat_id: int, enabled: bool) -> None:
        await self.db.chat_settings.update_one(
            {"_id": chat_id}, {"$set": {"captcha": enabled}}, upsert=True
        )

    async def get_captcha(self, chat_id: int) -> bool:
        doc = await self.db.chat_settings.find_one({"_id": chat_id})
        return bool(doc.get("captcha", False)) if doc else False

    # ─────────────────────────────────────────────────────────────────────────
    # CAPTCHA pending verifications  {chat_id: {user_id: message_id}}
    # stored in-memory; no need to persist across restarts
    # ─────────────────────────────────────────────────────────────────────────
    # (handled entirely in-memory inside groupmanagement.py)

