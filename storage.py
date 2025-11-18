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

    def reset_all(self) -> int:
        """Clear all saved availability entries.

        Returns the number of users that were cleared.
        """

        cleared = len(self._data.get("users", {}))
        self._data["users"] = {}
        self._persist()
        return cleared


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

    def set_team_roles(
        self, guild_id: int, team_a_role_id: int | None, team_b_role_id: int | None
    ) -> None:
        guild_data = self._data.setdefault(str(guild_id), {})
        if team_a_role_id is not None:
            guild_data["team_a_role_id"] = team_a_role_id
        if team_b_role_id is not None:
            guild_data["team_b_role_id"] = team_b_role_id
        self._persist()

    def get_announcement_channel(self, guild_id: int) -> Optional[int]:
        return self._data.get(str(guild_id), {}).get("announcement_channel_id")

    def get_ping_role(self, guild_id: int) -> Optional[int]:
        return self._data.get(str(guild_id), {}).get("ping_role_id")

    def get_team_roles(self, guild_id: int) -> Dict[str, Optional[int]]:
        data = self._data.get(str(guild_id), {})
        return {
            "A": data.get("team_a_role_id"),
            "B": data.get("team_b_role_id"),
        }

