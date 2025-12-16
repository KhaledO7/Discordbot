from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable

from storage import AvailabilityStore, GuildConfigStore, WEEK_DAYS


# Role priorities for balanced team composition
ROLE_PRIORITY = ["controller", "sentinel", "initiator", "duelist"]


@dataclass
class PlayerInfo:
    """Information about a player for lineup suggestions."""
    user_id: int
    display_name: str
    team: Optional[str]
    roles: List[str]
    agents: List[str]


@dataclass
class LineupSuggestion:
    """A suggested lineup with role assignments."""
    players: List[PlayerInfo]
    missing_roles: List[str]
    is_complete: bool  # Has 5 players
    has_all_roles: bool  # Has all 4 roles covered


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
    lineup_suggestion: Optional[LineupSuggestion] = None
    locked_lineup_ids: Optional[List[int]] = None

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

    def build_week(self, guild_id: int, include_lineup_suggestions: bool = False) -> List[DaySummary]:
        """Build the weekly schedule summary.

        Args:
            guild_id: The guild to build schedule for.
            include_lineup_suggestions: If True, generate lineup suggestions for each day.
        """
        summaries: List[DaySummary] = []

        for day in WEEK_DAYS:
            users = self.availability_store.users_for_day(day)
            team_counts: Dict[str, int] = {"A": 0, "B": 0}
            names: List[str] = []
            players: List[PlayerInfo] = []

            for info in users:
                team = (str(info.get("team") or "")).upper()
                if team in team_counts:
                    team_counts[team] += 1
                names.append(str(info.get("display_name")))

                # Build player info for lineup suggestions
                players.append(PlayerInfo(
                    user_id=int(info.get("id", 0)),
                    display_name=str(info.get("display_name", "Unknown")),
                    team=team if team in ("A", "B") else None,
                    roles=list(info.get("roles", [])),
                    agents=list(info.get("agents", [])),
                ))

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

            # Generate lineup suggestion if requested
            lineup_suggestion = None
            if include_lineup_suggestions and total >= 5:
                lineup_suggestion = self._suggest_lineup(players, premier_team)

            # Check for locked lineup
            locked_lineup = self.config_store.get_locked_lineup(guild_id, day, "premier")
            locked_lineup_ids = None
            if locked_lineup:
                locked_lineup_ids = locked_lineup.get("player_ids", [])

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
                    lineup_suggestion=lineup_suggestion,
                    locked_lineup_ids=locked_lineup_ids,
                )
            )

        return summaries

    @staticmethod
    def _select_premier_team(team_counts: Dict[str, int]) -> Optional[str]:
        """Select the team to play Premier based on availability."""
        qualified = {team: count for team, count in team_counts.items() if count >= 5}
        if not qualified:
            return None
        # Pick team with highest count (use lambda to satisfy type checker)
        return max(qualified, key=lambda t: qualified[t])

    @staticmethod
    def _suggest_lineup(players: List[PlayerInfo], target_team: Optional[str] = None) -> LineupSuggestion:
        """Generate a lineup suggestion based on player roles.

        Prioritizes:
        1. Players from the target team (if specified)
        2. Players with roles that fill gaps in the composition
        3. Limiting to 5 players
        """
        # Filter by team if specified
        if target_team:
            team_players = [p for p in players if p.team == target_team]
            if len(team_players) >= 5:
                players = team_players

        # Sort players by role coverage priority
        def role_score(player: PlayerInfo) -> int:
            """Higher score = more valuable (has rare roles)."""
            score = 0
            for i, role in enumerate(ROLE_PRIORITY):
                if role in player.roles:
                    score += (len(ROLE_PRIORITY) - i) * 10
            # Bonus for having any roles defined
            if player.roles:
                score += 5
            return score

        sorted_players = sorted(players, key=role_score, reverse=True)

        # Select up to 5 players trying to cover all roles
        selected: List[PlayerInfo] = []
        covered_roles: set = set()

        # First pass: select players that fill missing roles
        for player in sorted_players:
            if len(selected) >= 5:
                break
            player_roles = set(player.roles)
            new_roles = player_roles - covered_roles
            if new_roles or not player_roles:  # Include if fills gaps or has no roles set
                selected.append(player)
                covered_roles.update(player_roles)

        # Second pass: fill remaining slots
        for player in sorted_players:
            if len(selected) >= 5:
                break
            if player not in selected:
                selected.append(player)
                covered_roles.update(player.roles)

        # Determine missing roles
        missing_roles = [r for r in ROLE_PRIORITY if r not in covered_roles]

        return LineupSuggestion(
            players=selected,
            missing_roles=missing_roles,
            is_complete=len(selected) >= 5,
            has_all_roles=len(missing_roles) == 0,
        )

    @staticmethod
    def format_schedule(guild_name: str, summaries: List[DaySummary]) -> str:
        """Format the schedule for display in Discord."""
        header = (
            f"## Weekly Valorant Schedule — {guild_name}\n"
            "_Premier windows, scrim times, practice, and maps are **server-configurable** via `/config`._\n\n"
        )
        lines = [header]
        for summary in summaries:
            lines.append(summary.to_lines())
        return "\n".join(lines)
