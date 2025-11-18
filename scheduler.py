from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional

from storage import AvailabilityStore, GuildConfigStore
from time_utils import format_time_with_zone, parse_time_string

WEEK_DAYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]

# Default premier windows – can later be overridden by config
PREMIER_WINDOWS = {
    "wednesday": "7:00-8:00 PM ET",
    "thursday": "7:00-8:00 PM ET",
    "friday": "8:00-9:00 PM ET",
    "saturday": "8:00-9:00 PM ET",
    "sunday": "7:00-8:00 PM ET",
}

DEFAULT_SCRIM_LABEL = "7:00 PM ET"


@dataclass
class DaySummary:
    day: str
    total_available: int
    team_counts: Dict[str, int]
    premier_team: Optional[str]
    premier_window: Optional[str]
    scrim_ready: bool
    scrim_time_display: str
    available_names: List[str]

    def to_line(self) -> str:
        premier_status = "Premier off" if not self.premier_window else "Premier TBD"
        if self.premier_team:
            premier_status = f"Premier ({self.premier_team}) @ {self.premier_window}"
        elif self.premier_window:
            premier_status = f"Premier needs 5 from Team A or B @ {self.premier_window}"

        if self.scrim_ready:
            scrim_status = "Scrim ready"
        else:
            scrim_status = f"Scrim needs {max(0, 10 - self.total_available)} more"

        team_lines = ", ".join(
            f"Team {team}: {count}" for team, count in sorted(self.team_counts.items())
        ) or "No teams set"

        names = ", ".join(self.available_names) if self.available_names else "No signups"
        return (
            f"**{self.day.title()}** — {premier_status} — {scrim_status}\n"
            f"• Availability: {self.total_available} ({names})\n"
            f"• {team_lines}"
        )


class ScheduleBuilder:
    def __init__(
        self, availability_store: AvailabilityStore, config_store: GuildConfigStore
    ) -> None:
        self.availability_store = availability_store
        self.config_store = config_store

    def build_week(
        self,
        premier_windows: Optional[Dict[str, str]] = None,
    ) -> List[DaySummary]:
        """Build summaries for the entire week.

        premier_windows:
            Optional override per day (e.g. from guild config). If not provided,
            uses the defaults in PREMIER_WINDOWS.
        """
        windows = premier_windows or PREMIER_WINDOWS

        summaries: List[DaySummary] = []
        timezone = self.config_store.resolve_timezone(guild_id, DEFAULT_TIMEZONE)
        today = date.today()

        for day in WEEK_DAYS:
            users = self.availability_store.users_for_day(day)
            team_counts: Dict[str, int] = {"A": 0, "B": 0}
            names: List[str] = []

            for info in users:
                team = (info.get("team") or "").upper()
                if team in team_counts:
                    team_counts[team] += 1
                names.append(str(info.get("display_name")))

            premier_window = windows.get(day)
            premier_team = self._select_premier_team(team_counts) if premier_window else None
            scrim_ready = len(users) >= 10

            scrim_time_str = self.config_store.get_scrim_time(guild_id, day) or DEFAULT_SCRIM_TIME
            parsed_time = parse_time_string(scrim_time_str) or parse_time_string(DEFAULT_SCRIM_TIME)
            scrim_time_display = scrim_time_str
            if parsed_time:
                scrim_time_display = format_time_with_zone(today, parsed_time, timezone)

            summaries.append(
                DaySummary(
                    day=day,
                    total_available=len(users),
                    team_counts=team_counts,
                    premier_team=premier_team,
                    premier_window=premier_window,
                    scrim_ready=scrim_ready,
                    scrim_time_display=scrim_time_display,
                    available_names=names,
                )
            )
        return summaries

    @staticmethod
    def _select_premier_team(team_counts: Dict[str, int]) -> Optional[str]:
        qualified = {team: count for team, count in team_counts.items() if count >= 5}
        if not qualified:
            return None
        return max(qualified, key=qualified.get)

    @staticmethod
    def format_schedule(
        summaries: List[DaySummary],
        scrim_label: str = DEFAULT_SCRIM_LABEL,
    ) -> str:
        header = (
            "Valorant Availability — Premier Wed-Sun (configurable windows) | "
            f"Scrims target {scrim_label} if 10+ players"
        )
        lines = [header, ""]
        for summary in summaries:
            lines.append(summary.to_line())
        return "\n".join(lines)
