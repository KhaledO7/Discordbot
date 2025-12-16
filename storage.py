from __future__ import annotations

import json
import logging
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional


def _atomic_write(path: Path, data: Dict) -> None:
    """Write JSON data atomically using a temp file and rename."""
    # Create temp file in same directory to ensure same filesystem
    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.stem}_",
        suffix=".tmp"
    )
    try:
        with open(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, sort_keys=True)
        # Atomic rename
        Path(tmp_path).replace(path)
    except Exception:
        # Clean up temp file on error
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass
        raise


def _backup_file(path: Path) -> None:
    """Create a timestamped backup of a file if it exists."""
    if not path.exists():
        return
    backup_dir = path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"{path.stem}_{timestamp}{path.suffix}"
    try:
        shutil.copy2(path, backup_path)
        # Keep only last 10 backups
        backups = sorted(backup_dir.glob(f"{path.stem}_*{path.suffix}"))
        for old_backup in backups[:-10]:
            old_backup.unlink()
    except Exception as e:
        logging.warning("Failed to create backup: %s", e)


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
                "agents": [str],     # optional: picked agents
                "timezone": str      # optional: user's timezone (e.g. "America/New_York")
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
            # Backup before loading in case we need to recover
            _backup_file(self.path)
            try:
                self._data = json.loads(self.path.read_text(encoding='utf-8'))
                if "users" not in self._data:
                    self._data["users"] = {}
            except Exception as e:
                logging.error("Failed to load availability data: %s", e)
                # Corrupted file → reset
                self._data = {"users": {}}
        else:
            self._persist()

    def _persist(self) -> None:
        _atomic_write(self.path, self._data)

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

    # -------- Timezone preferences --------

    def set_user_timezone(self, user_id: int, timezone: str) -> None:
        """Set a user's timezone (e.g., 'America/New_York')."""
        users = self._data.setdefault("users", {})
        key = str(user_id)
        entry = users.setdefault(key, {})
        entry["timezone"] = timezone
        self._persist()

    def get_user_timezone(self, user_id: int) -> Optional[str]:
        """Get a user's timezone. Returns None if not set."""
        entry = self._data.get("users", {}).get(str(user_id), {})
        return entry.get("timezone")

    def get_user_info(self, user_id: int) -> Dict[str, object]:
        """Get full user info including days, team, roles, agents, and timezone."""
        entry = self._data.get("users", {}).get(str(user_id), {})
        return {
            "display_name": entry.get("display_name", "Unknown"),
            "team": entry.get("team"),
            "days": list(entry.get("days", [])),
            "roles": list(entry.get("roles", [])),
            "agents": list(entry.get("agents", [])),
            "timezone": entry.get("timezone"),
        }


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
            _backup_file(self.path)
            try:
                self._data = json.loads(self.path.read_text(encoding='utf-8'))
            except Exception as e:
                logging.error("Failed to load guild config: %s", e)
                self._data = {}
        else:
            self._persist()

    def _persist(self) -> None:
        _atomic_write(self.path, self._data)

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

    # -------- Lineup Lock --------

    def set_locked_lineup(
        self, guild_id: int, day: str, player_ids: List[int], match_type: str = "premier"
    ) -> None:
        """Lock a lineup for a specific day and match type."""
        g = self._ensure_guild(guild_id)
        day = day.lower()
        if day not in WEEK_DAYS:
            raise ValueError(f"Invalid day: {day}")
        locked = g.setdefault("locked_lineups", {})
        key = f"{day}_{match_type}"
        locked[key] = {
            "player_ids": player_ids,
            "locked_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._persist()

    def get_locked_lineup(
        self, guild_id: int, day: str, match_type: str = "premier"
    ) -> Optional[Dict[str, object]]:
        """Get the locked lineup for a day/match type. Returns None if not locked."""
        g = self._ensure_guild(guild_id)
        locked = g.get("locked_lineups", {})
        key = f"{day.lower()}_{match_type}"
        return locked.get(key)

    def clear_locked_lineup(self, guild_id: int, day: str, match_type: str = "premier") -> bool:
        """Clear the locked lineup for a day/match type. Returns True if one was cleared."""
        g = self._ensure_guild(guild_id)
        locked = g.get("locked_lineups", {})
        key = f"{day.lower()}_{match_type}"
        if key in locked:
            del locked[key]
            self._persist()
            return True
        return False

    def clear_all_locked_lineups(self, guild_id: int) -> int:
        """Clear all locked lineups for a guild. Returns count cleared."""
        g = self._ensure_guild(guild_id)
        locked = g.get("locked_lineups", {})
        count = len(locked)
        if count:
            g["locked_lineups"] = {}
            self._persist()
        return count

    # -------- Reminders configuration --------

    def set_reminder_channel(self, guild_id: int, channel_id: int) -> None:
        """Set the channel for match reminders."""
        g = self._ensure_guild(guild_id)
        g["reminder_channel_id"] = channel_id
        self._persist()

    def get_reminder_channel(self, guild_id: int) -> Optional[int]:
        """Get the reminder channel. Falls back to announcement channel if not set."""
        g = self._ensure_guild(guild_id)
        cid = g.get("reminder_channel_id")
        if isinstance(cid, int):
            return cid
        # Fall back to announcement channel
        return self.get_announcement_channel(guild_id)

    def set_reminders_enabled(self, guild_id: int, enabled: bool) -> None:
        """Enable or disable automatic reminders."""
        g = self._ensure_guild(guild_id)
        g["reminders_enabled"] = enabled
        self._persist()

    def get_reminders_enabled(self, guild_id: int) -> bool:
        """Check if reminders are enabled. Default True."""
        g = self._ensure_guild(guild_id)
        return g.get("reminders_enabled", True)


class GameLogStore:
    """Store match logs (scrim/premier/practice) per guild."""

    def __init__(self, path: Path | str = Path("data/match_logs.json")) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: Dict[str, object] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            _backup_file(self.path)
            try:
                self._data = json.loads(self.path.read_text(encoding='utf-8'))
            except Exception as e:
                logging.error("Failed to load match logs: %s", e)
                self._data = {}
        else:
            self._persist()

    def _persist(self) -> None:
        _atomic_write(self.path, self._data)

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
