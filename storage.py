from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional


class AvailabilityStore:
    """Persist user availability for the week.

    Data model (JSON):
    {
        "users": {
            "<user_id>": {
                "display_name": str,
                "team": Optional[str],
                "days": ["monday", "tuesday", ...]
            }
        }
    }
    """

    def __init__(self, path: Path | str = Path("data/availability.json")) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: Dict[str, Dict[str, object]] = {"users": {}}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            self._data = json.loads(self.path.read_text())
        else:
            self._persist()

    def _persist(self) -> None:
        self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True))

    def set_availability(
        self,
        user_id: int,
        display_name: str,
        team: Optional[str],
        days: Iterable[str],
    ) -> None:
        normalized_days = sorted({day.lower() for day in days})
        self._data.setdefault("users", {})[str(user_id)] = {
            "display_name": display_name,
            "team": team,
            "days": normalized_days,
        }
        self._persist()

    def clear_user(self, user_id: int) -> None:
        if str(user_id) in self._data.get("users", {}):
            del self._data["users"][str(user_id)]
            self._persist()

    def users_for_day(self, day: str) -> List[Dict[str, object]]:
        day = day.lower()
        users = self._data.get("users", {})
        return [
            {
                "id": int(user_id),
                "display_name": info.get("display_name", "Unknown"),
                "team": info.get("team"),
            }
            for user_id, info in users.items()
            if day in info.get("days", [])
        ]

    def get_user_days(self, user_id: int) -> List[str]:
        return list(self._data.get("users", {}).get(str(user_id), {}).get("days", []))

    def all_users(self) -> Dict[str, Dict[str, object]]:
        return self._data.get("users", {})


class GuildConfigStore:
    """Track guild-specific config such as announcement channel and ping role."""

    def __init__(self, path: Path | str = Path("data/guild_config.json")) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: Dict[str, Dict[str, int]] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            self._data = json.loads(self.path.read_text())
        else:
            self._persist()

    def _persist(self) -> None:
        self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True))

    def set_announcement_channel(self, guild_id: int, channel_id: int) -> None:
        self._data.setdefault(str(guild_id), {})["announcement_channel_id"] = channel_id
        self._persist()

    def set_ping_role(self, guild_id: int, role_id: int) -> None:
        self._data.setdefault(str(guild_id), {})["ping_role_id"] = role_id
        self._persist()

    def get_announcement_channel(self, guild_id: int) -> Optional[int]:
        return self._data.get(str(guild_id), {}).get("announcement_channel_id")

    def get_ping_role(self, guild_id: int) -> Optional[int]:
        return self._data.get(str(guild_id), {}).get("ping_role_id")

