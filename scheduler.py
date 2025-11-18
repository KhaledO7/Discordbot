from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from storage import AvailabilityStore, GuildConfigStore

WEEK_DAYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]

DEFAULT_PREMIER_WINDOWS = {
    "wednesday": "7:00-8:00 PM ET",
    "thursday": "7:00-8:00 PM ET",
    "friday": "8:00-9:00 PM ET",
    "saturday": "8:00-9:00 PM ET",
    "sunday": "7:00-8:00 PM ET",
}

DEFAULT_SCRIM_TIME = "7:00 PM ET"


@dataclass
class DaySummary:
    day: str
    total_available: int
    team_counts: Dict[str, int]
    premier_team: Optional[str]
    premier_window: Optional[str]
    scrim_ready: bool
    scrim_time: str
    available_names: List[str]

    def to_line(self) -> str:
        premier_status = "Premier off" if not self.premier_window else "Premier TBD"
        if self.premier_team:
            premier_status = f"Premier ({self.premier_team}) @ {self.premier_window}"
        elif self.premier_window:
            premier_status = f"Premier needs 5 from Team A or B @ {self.premier_window}"

        scrim_status = (
            f"Scrim ready @ {self.scrim_time}"
            if self.scrim_ready
            else f"Scrim needs {max(0, 10 - self.total_available)} more (target {self.scrim_time})"
        )

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
        self, guild_id: int | None = None
    ) -> tuple[List[DaySummary], Dict[str, Optional[str]], str]:
        summaries: List[DaySummary] = []
        premier_windows = self._resolve_premier_windows(guild_id)
        scrim_time = self._resolve_scrim_time(guild_id)

        for day in WEEK_DAYS:
            users = self.availability_store.users_for_day(day)
            team_counts: Dict[str, int] = {"A": 0, "B": 0}
            names: List[str] = []
            for info in users:
                team = (info.get("team") or "").upper()
                if team in team_counts:
                    team_counts[team] += 1
                names.append(str(info.get("display_name")))

            premier_window = premier_windows.get(day)
            premier_team = self._select_premier_team(team_counts) if premier_window else None
            scrim_ready = len(users) >= 10

            summaries.append(
                DaySummary(
                    day=day,
                    total_available=len(users),
                    team_counts=team_counts,
                    premier_team=premier_team,
                    premier_window=premier_window,
                    scrim_ready=scrim_ready,
                    scrim_time=scrim_time,
                    available_names=names,
                )
            )
        return summaries, premier_windows, scrim_time

    def _resolve_premier_windows(
        self, guild_id: int | None
    ) -> Dict[str, Optional[str]]:
        overrides: Dict[str, Optional[str]] = {}
        if guild_id is not None:
            overrides = self.config_store.get_premier_windows(guild_id)
        return self.merge_premier_windows(overrides)

    def _resolve_scrim_time(self, guild_id: int | None) -> str:
        if guild_id is not None:
            configured = self.config_store.get_scrim_time(guild_id)
            if configured:
                return configured
        return DEFAULT_SCRIM_TIME

    @staticmethod
    def _select_premier_team(team_counts: Dict[str, int]) -> Optional[str]:
        qualified = {team: count for team, count in team_counts.items() if count >= 5}
        if not qualified:
            return None
        return max(qualified, key=qualified.get)

    @staticmethod
    def merge_premier_windows(
        overrides: Dict[str, Optional[str]]
    ) -> Dict[str, Optional[str]]:
        merged: Dict[str, Optional[str]] = dict(DEFAULT_PREMIER_WINDOWS)
        for day, window in overrides.items():
            normalized = day.lower()
            if normalized in WEEK_DAYS:
                merged[normalized] = window
        return merged

    @staticmethod
    def describe_premier_windows(premier_windows: Dict[str, Optional[str]]) -> str:
        grouped: Dict[str, List[str]] = {}
        for day in WEEK_DAYS:
            window = premier_windows.get(day)
            if not window:
                continue
            grouped.setdefault(window, []).append(day[:3].title())

        if not grouped:
            return "Premier off"

        segments = ["/".join(days) + f" @ {window}" for window, days in grouped.items()]
        return "; ".join(segments)

    @staticmethod
    def format_schedule(
        summaries: List[DaySummary],
        premier_windows: Dict[str, Optional[str]],
        scrim_time: str,
    ) -> str:
        premier_description = ScheduleBuilder.describe_premier_windows(premier_windows)
        header = (
            f"Valorant Availability — Premier windows: {premier_description} | "
            f"Scrims target {scrim_time} if 10+ players"
        )
        lines = [header, ""]
        for summary in summaries:
            lines.append(summary.to_line())
        return "\n".join(lines)

