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

    async def set_welcome(self, chat_id: int, text: str, delete_after: int = 0) -> None:
        """Store a custom welcome message and optional auto-delete delay (seconds)."""
        await self.db.greetings.update_one(
            {"_id": chat_id},
            {"$set": {"welcome": text, "welcome_delete": delete_after}},
            upsert=True,
        )

    async def get_welcome(self, chat_id: int) -> dict:
        """Return {'text': str, 'delete_after': int} or empty dict."""
        doc = await self.db.greetings.find_one({"_id": chat_id})
        if not doc:
            return {}
        return {
            "text": doc.get("welcome", ""),
            "delete_after": doc.get("welcome_delete", 0),
        }

    async def del_welcome(self, chat_id: int) -> None:
        await self.db.greetings.update_one(
            {"_id": chat_id}, {"$unset": {"welcome": "", "welcome_delete": ""}}
        )

    async def set_goodbye(self, chat_id: int, text: str) -> None:
        await self.db.greetings.update_one(
            {"_id": chat_id}, {"$set": {"goodbye": text}}, upsert=True
        )

    async def get_goodbye(self, chat_id: int) -> str:
        doc = await self.db.greetings.find_one({"_id": chat_id})
        return doc.get("goodbye", "") if doc else ""

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
