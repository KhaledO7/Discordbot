from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import discord

from storage import AvailabilityStore, GuildConfigStore, WEEK_DAYS


@dataclass
class DaySummary:
    day: str
    total_available: int
    team_counts: Dict[str, int]
    premier_team: Optional[str]
    premier_window: Optional[str]
    scrim_time: Optional[str]
    scrim_ready: bool
    scrim_missing: int
    available_names: List[str]

    def to_lines(self) -> str:
        # Premier line
        if self.premier_window is None:
            premier_status = "Premier: **OFF**"
        elif self.premier_team:
            premier_status = f"Premier: **Team {self.premier_team}** @ `{self.premier_window}`"
        else:
            premier_status = f"Premier: needs **5** from Team A or B @ `{self.premier_window}`"

        # Scrim line
        if self.scrim_time is None:
            scrim_status = "Scrim: **OFF**"
        elif self.scrim_ready:
            scrim_status = f"Scrim: **READY** ({self.total_available} players) @ `{self.scrim_time}`"
        else:
            scrim_status = f"Scrim: needs **{self.scrim_missing}** more for 10 @ `{self.scrim_time}`"

        team_lines = ", ".join(
            f"Team {team}: {count}" for team, count in sorted(self.team_counts.items())
        ) or "No teams set"

        names = ", ".join(self.available_names) if self.available_names else "No signups"

        return (
            f"### {self.day.title()}\n"
            f"- {premier_status}\n"
            f"- {scrim_status}\n"
            f"- Availability: **{self.total_available}** ({names})\n"
            f"- Teams: {team_lines}\n"
        )


class ScheduleBuilder:
    """Builds a weekly schedule summary for a given guild."""

    def __init__(self, availability_store: AvailabilityStore, config_store: GuildConfigStore) -> None:
        self.availability_store = availability_store
        self.config_store = config_store

    def build_week(self, guild: discord.Guild) -> List[DaySummary]:
        summaries: List[DaySummary] = []

        # Get configured team role IDs for this guild
        team_roles = self.config_store.get_team_roles(guild.id)
        team_a_id = team_roles.get("A")
        team_b_id = team_roles.get("B")

        for day in WEEK_DAYS:
            users = self.availability_store.users_for_day(day)
            team_counts: Dict[str, int] = {"A": 0, "B": 0}
            names: List[str] = []

            for info in users:
                user_id = int(info.get("id"))
                member = guild.get_member(user_id)

                team: Optional[str] = None

                # Prefer live Discord roles
                if member is not None:
                    member_role_ids = {r.id for r in member.roles}
                    if team_a_id and team_a_id in member_role_ids:
                        team = "A"
                    elif team_b_id and team_b_id in member_role_ids:
                        team = "B"
                else:
                    # Fallback to stored team if member not visible (left server, etc.)
                    stored_team = (str(info.get("team") or "")).upper()
                    if stored_team in {"A", "B"}:
                        team = stored_team

                if team in {"A", "B"}:
                    team_counts[team] += 1

                names.append(str(info.get("display_name")))

            premier_window = self.config_store.get_premier_window(guild.id, day)
            scrim_time = self.config_store.get_scrim_time(guild.id, day)

            premier_team = self._select_premier_team(team_counts) if premier_window else None

            total = len(users)
            scrim_ready = scrim_time is not None and total >= 10
            scrim_missing = max(0, 10 - total) if scrim_time is not None else 0

            summaries.append(
                DaySummary(
                    day=day,
                    total_available=total,
                    team_counts=team_counts,
                    premier_team=premier_team,
                    premier_window=premier_window,
                    scrim_time=scrim_time,
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
            "_Premier and scrim windows are **server-configurable**. "
            "Use `/config` commands to adjust times._\n\n"
        )
        lines = [header]
        for summary in summaries:
            lines.append(summary.to_lines())
        return "\n".join(lines)
