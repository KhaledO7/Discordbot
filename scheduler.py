from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from storage import AvailabilityStore, GuildConfigStore, WEEK_DAYS


@dataclass
class DaySummary:
    day: str
    total_available: int
    team_counts: Dict[str, int]
    premier_team: Optional[str]
    premier_window: Optional[str]
    premier_map: Optional[str]
    practice_time: Optional[str]
    practice_map: Optional[str]
    scrim_time: Optional[str]
    scrim_map: Optional[str]
    practice_ready: bool
    practice_missing: int
    scrim_ready: bool
    scrim_missing: int
    available_names: List[str]

    def to_lines(self) -> str:
        # Premier line
        if self.premier_window is None:
            premier_status = "Premier: **OFF**"
        else:
            premier_map_suffix = f" · Map: **{self.premier_map}**" if self.premier_map else ""
            if self.premier_team:
                premier_status = (
                    f"Premier: **Team {self.premier_team}** @ `{self.premier_window}`"
                    f"{premier_map_suffix}"
                )
            else:
                premier_status = (
                    f"Premier: needs **5** from Team A or B @ `{self.premier_window}`"
                    f"{premier_map_suffix}"
                )

        # Practice line
        if self.practice_time is None:
            practice_status = "Practice: **OFF**"
        else:
            practice_map_suffix = f" · Map: **{self.practice_map}**" if self.practice_map else ""
            if self.practice_ready:
                practice_status = (
                    f"Practice: **READY** ({self.total_available} players) "
                    f"@ `{self.practice_time}`{practice_map_suffix}"
                )
            else:
                practice_status = (
                    f"Practice: needs **{self.practice_missing}** more for 5 "
                    f"@ `{self.practice_time}`{practice_map_suffix}"
                )

        # Scrim line
        if self.scrim_time is None:
            scrim_status = "Scrim: **OFF**"
        else:
            scrim_map_suffix = f" · Map: **{self.scrim_map}**" if self.scrim_map else ""
            if self.scrim_ready:
                scrim_status = (
                    f"Scrim: **READY** ({self.total_available} players) "
                    f"@ `{self.scrim_time}`{scrim_map_suffix}"
                )
            else:
                scrim_status = (
                    f"Scrim: needs **{self.scrim_missing}** more for 10 "
                    f"@ `{self.scrim_time}`{scrim_map_suffix}"
                )

        team_lines = ", ".join(
            f"Team {team}: {count}" for team, count in sorted(self.team_counts.items())
        ) or "No teams set"

        names = ", ".join(self.available_names) if self.available_names else "No signups"

        return (
            f"### {self.day.title()}\n"
            f"- {premier_status}\n"
            f"- {practice_status}\n"
            f"- {scrim_status}\n"
            f"- Availability: **{self.total_available}** ({names})\n"
            f"- Teams: {team_lines}\n"
        )


class ScheduleBuilder:
    """Builds a weekly schedule summary for a given guild."""

    def __init__(self, availability_store: AvailabilityStore, config_store: GuildConfigStore) -> None:
        self.availability_store = availability_store
        self.config_store = config_store

    def build_week(self, guild_id: int) -> List[DaySummary]:
        summaries: List[DaySummary] = []

        for day in WEEK_DAYS:
            users = self.availability_store.users_for_day(day)
            team_counts: Dict[str, int] = {"A": 0, "B": 0}
            names: List[str] = []

            for info in users:
                team = (str(info.get("team") or "")).upper()
                if team in team_counts:
                    team_counts[team] += 1
                names.append(str(info.get("display_name")))

            premier_window = self.config_store.get_premier_window(guild_id, day)
            scrim_time = self.config_store.get_scrim_time(guild_id, day)
            practice_time = self.config_store.get_practice_time(guild_id, day)

            premier_map = self.config_store.get_premier_map(guild_id, day)
            scrim_map = self.config_store.get_scrim_map(guild_id, day)
            practice_map = self.config_store.get_practice_map(guild_id, day)

            premier_team = self._select_premier_team(team_counts) if premier_window else None

            total = len(users)

            practice_ready = practice_time is not None and total >= 5
            practice_missing = max(0, 5 - total) if practice_time is not None else 0

            scrim_ready = scrim_time is not None and total >= 10
            scrim_missing = max(0, 10 - total) if scrim_time is not None else 0

            summaries.append(
                DaySummary(
                    day=day,
                    total_available=total,
                    team_counts=team_counts,
                    premier_team=premier_team,
                    premier_window=premier_window,
                    premier_map=premier_map,
                    practice_time=practice_time,
                    practice_map=practice_map,
                    scrim_time=scrim_time,
                    scrim_map=scrim_map,
                    practice_ready=practice_ready,
                    practice_missing=practice_missing,
                    scrim_ready=scrim_ready,
                    scrim_missing=scrim_missing,
                    available_names=names,
                )
            )

        return summaries

    @staticmethod
    def _select_premier_team(team_counts: Dict[str, int]) -> Optional[str]:
        qualified = {team: count for team, count in team_counts.items() if count >= 5}
        if not qualified:
            return None
        # Pick team with highest count
        return max(qualified, key=qualified.get)

    @staticmethod
    def format_schedule(guild_name: str, summaries: List[DaySummary]) -> str:
        header = (
            f"## Weekly Valorant Schedule — {guild_name}\n"
            "_Premier windows, scrim times, practice, and maps are **server-configurable** via `/config`._\n\n"
        )
        lines = [header]
        for summary in summaries:
            lines.append(summary.to_lines())
        return "\n".join(lines)
