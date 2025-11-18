from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional


WEEK_DAYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]

# Default scrim times (server ET) and premier windows
DEFAULT_SCRIM_TIMES: Dict[str, Optional[str]] = {
    "monday": None,
    "tuesday": None,
    "wednesday": "19:00",  # 7 PM ET
    "thursday": "19:00",
    "friday": "20:00",     # 8 PM ET
    "saturday": "20:00",
    "sunday": "19:00",
}

DEFAULT_PREMIER_WINDOWS: Dict[str, Optional[str]] = {
    "monday": None,
    "tuesday": None,
    "wednesday": "19:00-20:00",
    "thursday": "19:00-20:00",
    "friday": "20:00-21:00",
    "saturday": "20:00-21:00",
    "sunday": "19:00-20:00",
}


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
            try:
                self._data = json.loads(self.path.read_text())
            except Exception:
                # Corrupted file → reset
                self._data = {"users": {}}
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
    """Track guild-specific config such as announcement channel, ping role,
    team roles, scrim times, and premier windows.
    """

    def __init__(self, path: Path | str = Path("data/guild_config.json")) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: Dict[str, Dict[str, object]] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except Exception:
                self._data = {}
        else:
            self._persist()

    def _persist(self) -> None:
        self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True))

    def _ensure_guild(self, guild_id: int) -> Dict[str, object]:
        gid = str(guild_id)
        if gid not in self._data:
            self._data[gid] = {
                "announcement_channel_id": None,
                "ping_role_id": None,
                "team_a_role_id": None,
                "team_b_role_id": None,
                "scrim_times": {d: DEFAULT_SCRIM_TIMES[d] for d in WEEK_DAYS},
                "premier_windows": {d: DEFAULT_PREMIER_WINDOWS[d] for d in WEEK_DAYS},
            }
        else:
            # Ensure keys exist even if file is from an older version
            g = self._data[gid]
            g.setdefault("announcement_channel_id", None)
            g.setdefault("ping_role_id", None)
            g.setdefault("team_a_role_id", None)
            g.setdefault("team_b_role_id", None)
            scrim = g.setdefault("scrim_times", {})
            premier = g.setdefault("premier_windows", {})
            for d in WEEK_DAYS:
                scrim.setdefault(d, DEFAULT_SCRIM_TIMES[d])
                premier.setdefault(d, DEFAULT_PREMIER_WINDOWS[d])
        return self._data[gid]

    # Announcement channel
    def set_announcement_channel(self, guild_id: int, channel_id: int) -> None:
        g = self._ensure_guild(guild_id)
        g["announcement_channel_id"] = channel_id
        self._persist()

    def get_announcement_channel(self, guild_id: int) -> Optional[int]:
        g = self._ensure_guild(guild_id)
        cid = g.get("announcement_channel_id")
        return int(cid) if isinstance(cid, int) else None

    # Ping role
    def set_ping_role(self, guild_id: int, role_id: int) -> None:
        g = self._ensure_guild(guild_id)
        g["ping_role_id"] = role_id
        self._persist()

    def get_ping_role(self, guild_id: int) -> Optional[int]:
        g = self._ensure_guild(guild_id)
        rid = g.get("ping_role_id")
        return int(rid) if isinstance(rid, int) else None

    # Team roles
    def set_team_roles(
        self,
        guild_id: int,
        team_a_role_id: int | None,
        team_b_role_id: int | None,
    ) -> None:
        g = self._ensure_guild(guild_id)
        if team_a_role_id is not None:
            g["team_a_role_id"] = team_a_role_id
        if team_b_role_id is not None:
            g["team_b_role_id"] = team_b_role_id
        self._persist()

    def get_team_roles(self, guild_id: int) -> Dict[str, Optional[int]]:
        g = self._ensure_guild(guild_id)
        a = g.get("team_a_role_id")
        b = g.get("team_b_role_id")
        return {
            "A": int(a) if isinstance(a, int) else None,
            "B": int(b) if isinstance(b, int) else None,
        }

    # Scrim time configuration
    def set_scrim_time(self, guild_id: int, day: str, time_str: Optional[str]) -> None:
        """time_str examples:
        - "19:00"
        - None (turn off scrims for that day)
        """
        g = self._ensure_guild(guild_id)
        day = day.lower()
        if day not in WEEK_DAYS:
            raise ValueError(f"Invalid day: {day}")
        g["scrim_times"][day] = time_str
        self._persist()

    def get_scrim_time(self, guild_id: int, day: str) -> Optional[str]:
        g = self._ensure_guild(guild_id)
        return g["scrim_times"].get(day.lower())

    def reset_scrim_times(self, guild_id: int) -> None:
        g = self._ensure_guild(guild_id)
        g["scrim_times"] = {d: DEFAULT_SCRIM_TIMES[d] for d in WEEK_DAYS}
        self._persist()

    # Premier windows configuration
    def set_premier_window(self, guild_id: int, day: str, window: Optional[str]) -> None:
        """window examples:
        - "19:00-20:00"
        - None (no premier that day)
        """
        g = self._ensure_guild(guild_id)
        day = day.lower()
        if day not in WEEK_DAYS:
            raise ValueError(f"Invalid day: {day}")
        g["premier_windows"][day] = window
        self._persist()

    def get_premier_window(self, guild_id: int, day: str) -> Optional[str]:
        g = self._ensure_guild(guild_id)
        return g["premier_windows"].get(day.lower())

    def reset_premier_windows(self, guild_id: int) -> None:
        g = self._ensure_guild(guild_id)
        g["premier_windows"] = {d: DEFAULT_PREMIER_WINDOWS[d] for d in WEEK_DAYS}
        self._persist()

    # Full schedule reset
    def reset_entire_schedule(self, guild_id: int) -> None:
        g = self._ensure_guild(guild_id)
        g["scrim_times"] = {d: DEFAULT_SCRIM_TIMES[d] for d in WEEK_DAYS}
        g["premier_windows"] = {d: DEFAULT_PREMIER_WINDOWS[d] for d in WEEK_DAYS}
        self._persist()
