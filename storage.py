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

# Practice: by default OFF for every day
DEFAULT_PRACTICE_TIMES: Dict[str, Optional[str]] = {
    day: None for day in WEEK_DAYS
}


class AvailabilityStore:
    """Persist user availability and agent prefs for the week.

    Data model (JSON):
    {
        "users": {
            "<user_id>": {
                "display_name": str,
                "team": Optional[str],
                "days": ["monday", "tuesday", ...],
                "roles": [str],      # optional: picked Valorant roles
                "agents": [str]      # optional: picked agents
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
                if "users" not in self._data:
                    self._data["users"] = {}
            except Exception:
                # Corrupted file → reset
                self._data = {"users": {}}
        else:
            self._persist()

    def _persist(self) -> None:
        self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True))

    # -------- Availability (days + team) --------

    def set_availability(
        self,
        user_id: int,
        display_name: str,
        team: Optional[str],
        days: Iterable[str],
    ) -> None:
        """Set days + team, but keep any stored roles/agents."""
        normalized_days = sorted({day.lower() for day in days})
        users = self._data.setdefault("users", {})
        key = str(user_id)
        entry = users.get(key, {})
        entry["display_name"] = display_name
        entry["team"] = team
        entry["days"] = normalized_days
        # Preserve existing roles/agents if present
        entry.setdefault("roles", [])
        entry.setdefault("agents", [])
        users[key] = entry
        self._persist()

    def clear_user(self, user_id: int) -> None:
        """Clear this user's availability (days), but leave agents/roles."""
        users = self._data.get("users", {})
        key = str(user_id)
        entry = users.get(key)
        if not entry:
            return
        entry["days"] = []
        # keep team as-is; may still be useful for agents
        self._persist()

    def users_for_day(self, day: str) -> List[Dict[str, object]]:
        day = day.lower()
        users = self._data.get("users", {})
        return [
            {
                "id": int(user_id),
                "display_name": info.get("display_name", "Unknown"),
                "team": info.get("team"),
                "roles": info.get("roles", []),
                "agents": info.get("agents", []),
            }
            for user_id, info in users.items()
            if day in info.get("days", [])
        ]

    def get_user_days(self, user_id: int) -> List[str]:
        return list(self._data.get("users", {}).get(str(user_id), {}).get("days", []))

    def all_users(self) -> Dict[str, Dict[str, object]]:
        return self._data.get("users", {})

    def reset_all(self) -> int:
        """Clear all saved availability entries (days), keep roles/agents.

        Returns the number of users whose availability was cleared.
        """
        users = self._data.get("users", {})
        cleared = len(users)
        for info in users.values():
            info["days"] = []
        self._persist()
        return cleared

    # -------- Agent / role preferences --------

    def set_agents(
        self,
        user_id: int,
        display_name: str,
        roles: Iterable[str],
        agents: Iterable[str],
    ) -> None:
        users = self._data.setdefault("users", {})
        key = str(user_id)
        entry = users.get(key, {})
        entry["display_name"] = display_name
        entry.setdefault("days", [])
        # team is kept as whatever on_member_update / availability set decides
        entry.setdefault("team", None)
        entry["roles"] = sorted({r.lower() for r in roles})
        entry["agents"] = sorted({a for a in agents})
        users[key] = entry
        self._persist()

    def get_user_agents(self, user_id: int) -> Dict[str, List[str]]:
        entry = self._data.get("users", {}).get(str(user_id), {})
        roles = list(entry.get("roles", []) or [])
        agents = list(entry.get("agents", []) or [])
        return {"roles": roles, "agents": agents}

    def clear_agents(self, user_id: int) -> None:
        users = self._data.get("users", {})
        key = str(user_id)
        entry = users.get(key)
        if not entry:
            return
        entry.pop("roles", None)
        entry.pop("agents", None)
        self._persist()


class GuildConfigStore:
    """Track guild-specific config such as announcement channel, ping role,
    team roles, scrim times, premier windows, practice, and maps.
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
                "practice_times": {d: DEFAULT_PRACTICE_TIMES[d] for d in WEEK_DAYS},
                "scrim_maps": {d: None for d in WEEK_DAYS},
                "premier_maps": {d: None for d in WEEK_DAYS},
                "practice_maps": {d: None for d in WEEK_DAYS},
            }
        else:
            g = self._data[gid]
            g.setdefault("announcement_channel_id", None)
            g.setdefault("ping_role_id", None)
            g.setdefault("team_a_role_id", None)
            g.setdefault("team_b_role_id", None)

            scrim = g.setdefault("scrim_times", {})
            premier = g.setdefault("premier_windows", {})
            practice = g.setdefault("practice_times", {})
            scrim_maps = g.setdefault("scrim_maps", {})
            premier_maps = g.setdefault("premier_maps", {})
            practice_maps = g.setdefault("practice_maps", {})

            for d in WEEK_DAYS:
                scrim.setdefault(d, DEFAULT_SCRIM_TIMES[d])
                premier.setdefault(d, DEFAULT_PREMIER_WINDOWS[d])
                practice.setdefault(d, DEFAULT_PRACTICE_TIMES[d])
                scrim_maps.setdefault(d, None)
                premier_maps.setdefault(d, None)
                practice_maps.setdefault(d, None)
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

    # Practice time configuration
    def set_practice_time(self, guild_id: int, day: str, time_str: Optional[str]) -> None:
        """time_str examples:
        - "18:00"
        - None (turn off practice for that day)
        """
        g = self._ensure_guild(guild_id)
        day = day.lower()
        if day not in WEEK_DAYS:
            raise ValueError(f"Invalid day: {day}")
        g["practice_times"][day] = time_str
        self._persist()

    def get_practice_time(self, guild_id: int, day: str) -> Optional[str]:
        g = self._ensure_guild(guild_id)
        return g["practice_times"].get(day.lower())

    def reset_practice_times(self, guild_id: int) -> None:
        g = self._ensure_guild(guild_id)
        g["practice_times"] = {d: DEFAULT_PRACTICE_TIMES[d] for d in WEEK_DAYS}
        self._persist()

    # Maps configuration
    def set_scrim_map(self, guild_id: int, day: str, map_name: Optional[str]) -> None:
        g = self._ensure_guild(guild_id)
        day = day.lower()
        if day not in WEEK_DAYS:
            raise ValueError(f"Invalid day: {day}")
        g["scrim_maps"][day] = map_name
        self._persist()

    def get_scrim_map(self, guild_id: int, day: str) -> Optional[str]:
        g = self._ensure_guild(guild_id)
        return g["scrim_maps"].get(day.lower())

    def set_premier_map(self, guild_id: int, day: str, map_name: Optional[str]) -> None:
        g = self._ensure_guild(guild_id)
        day = day.lower()
        if day not in WEEK_DAYS:
            raise ValueError(f"Invalid day: {day}")
        g["premier_maps"][day] = map_name
        self._persist()

    def get_premier_map(self, guild_id: int, day: str) -> Optional[str]:
        g = self._ensure_guild(guild_id)
        return g["premier_maps"].get(day.lower())

    def set_practice_map(self, guild_id: int, day: str, map_name: Optional[str]) -> None:
        g = self._ensure_guild(guild_id)
        day = day.lower()
        if day not in WEEK_DAYS:
            raise ValueError(f"Invalid day: {day}")
        g["practice_maps"][day] = map_name
        self._persist()

    def get_practice_map(self, guild_id: int, day: str) -> Optional[str]:
        g = self._ensure_guild(guild_id)
        return g["practice_maps"].get(day.lower())

    # Full schedule reset
    def reset_entire_schedule(self, guild_id: int) -> None:
        """Reset scrim, premier, and practice times to defaults."""
        g = self._ensure_guild(guild_id)
        g["scrim_times"] = {d: DEFAULT_SCRIM_TIMES[d] for d in WEEK_DAYS}
        g["premier_windows"] = {d: DEFAULT_PREMIER_WINDOWS[d] for d in WEEK_DAYS}
        g["practice_times"] = {d: DEFAULT_PRACTICE_TIMES[d] for d in WEEK_DAYS}
        # Do NOT touch maps here so you can keep maps if you want;
        # uncomment if you ever want maps reset too.
        # g["scrim_maps"] = {d: None for d in WEEK_DAYS}
        # g["premier_maps"] = {d: None for d in WEEK_DAYS}
        # g["practice_maps"] = {d: None for d in WEEK_DAYS}
        self._persist()


class GameLogStore:
    """Store match logs (scrim/premier/practice) per guild."""

    def __init__(self, path: Path | str = Path("data/match_logs.json")) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: Dict[str, object] = {}
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

    def _guild_logs(self, guild_id: int) -> List[Dict[str, object]]:
        guilds = self._data.setdefault("guilds", {})
        if not isinstance(guilds, dict):
            guilds = {}
            self._data["guilds"] = guilds
        gid = str(guild_id)
        logs = guilds.get(gid)
        if not isinstance(logs, list):
            logs = []
            guilds[gid] = logs
        return logs

    def add_log(self, guild_id: int, entry: Dict[str, object]) -> int:
        """Append a log entry and return its ID."""
        logs = self._guild_logs(guild_id)
        new_entry = dict(entry)
        new_entry["id"] = len(logs) + 1
        logs.append(new_entry)
        self._persist()
        return int(new_entry["id"])

    def logs_for_date(self, guild_id: int, date_str: str) -> List[Dict[str, object]]:
        logs = self._guild_logs(guild_id)
        return [log for log in logs if log.get("date") == date_str]

    def recent_logs(self, guild_id: int, limit: int = 10) -> List[Dict[str, object]]:
        logs = self._guild_logs(guild_id)
        if limit <= 0:
            return []
        return logs[-limit:]

    def clear_logs_for_date(self, guild_id: int, date_str: str) -> int:
        logs = self._guild_logs(guild_id)
        before = len(logs)
        logs[:] = [log for log in logs if log.get("date") != date_str]
        removed = before - len(logs)
        if removed:
            self._persist()
        return removed

    def clear_all_logs(self, guild_id: int) -> int:
        logs = self._guild_logs(guild_id)
        removed = len(logs)
        if removed:
            guilds = self._data.setdefault("guilds", {})
            guilds[str(guild_id)] = []
            self._persist()
        return removed
