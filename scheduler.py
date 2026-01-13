from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from typing import Dict, List, Optional, Callable, Any, Tuple

from storage import AvailabilityStore, GuildConfigStore, WEEK_DAYS, DAY_TYPES


# Role priorities for balanced team composition
ROLE_PRIORITY = ["controller", "sentinel", "initiator", "duelist"]

# Day type icons for display
DAY_TYPE_ICONS = {
    "FREE": "🟢",
    "PREMIER": "🏆",
    "SCRIM": "⚔️",
    "VOD": "🎬",
    "MIXED": "🔀",
    "OFF": "⛔",
}


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
class ScrimSlot:
    """Information about a scrim slot."""
    slot_number: int
    start_time: str
    available_count: int
    available_names: List[str]

    def format_display(self, max_names: int = 5) -> str:
        """Format for display, truncating names if needed."""
        if not self.available_names:
            names_str = "No signups"
        elif len(self.available_names) <= max_names:
            names_str = ", ".join(self.available_names)
        else:
            shown = self.available_names[:max_names]
            remaining = len(self.available_names) - max_names
            names_str = f"{', '.join(shown)} +{remaining} more"
        return f"Scrim #{self.slot_number} @ `{self.start_time}` — **{self.available_count}** ({names_str})"


@dataclass
class VodSession:
    """Information about a VOD review session."""
    start_time: str
    duration_minutes: int
    available_count: int
    available_names: List[str]

    def format_display(self, max_names: int = 5) -> str:
        """Format for display."""
        if not self.available_names:
            names_str = "No signups"
        elif len(self.available_names) <= max_names:
            names_str = ", ".join(self.available_names)
        else:
            shown = self.available_names[:max_names]
            remaining = len(self.available_names) - max_names
            names_str = f"{', '.join(shown)} +{remaining} more"
        return f"VOD @ `{self.start_time}` ({self.duration_minutes}min) — **{self.available_count}** ({names_str})"


@dataclass
class DaySummary:
    day: str
    day_type: str
    day_label: Optional[str]
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
    scrim_slots: List[ScrimSlot] = field(default_factory=list)
    vod_session: Optional[VodSession] = None

    def to_lines(self) -> str:
        """Format day summary for Discord display."""
        # Day header with type icon
        icon = DAY_TYPE_ICONS.get(self.day_type, "")
        label_suffix = f" — {self.day_label}" if self.day_label else ""
        header = f"### {icon} {self.day.title()}{label_suffix}"

        lines = [header]

        # Show content based on day type
        if self.day_type == "FREE":
            lines.append("_Free day — no activities scheduled_")
            return "\n".join(lines) + "\n"

        if self.day_type == "OFF":
            lines.append("_Day off — no activities scheduled_")
            return "\n".join(lines) + "\n"

        # Premier section (show for PREMIER or MIXED)
        if self.day_type in ("PREMIER", "MIXED"):
            if self.premier_window is None:
                premier_status = "🏆 Premier: **OFF**"
            else:
                premier_map_suffix = f" · Map: **{self.premier_map}**" if self.premier_map else ""
                if self.premier_team:
                    premier_status = (
                        f"🏆 Premier: **Team {self.premier_team}** @ `{self.premier_window}`"
                        f"{premier_map_suffix}"
                    )
                else:
                    premier_status = (
                        f"🏆 Premier: needs **5** from Team A or B @ `{self.premier_window}`"
                        f"{premier_map_suffix}"
                    )
            lines.append(f"- {premier_status}")

        # Scrim section (show for SCRIM or MIXED)
        if self.day_type in ("SCRIM", "MIXED"):
            if self.scrim_slots:
                lines.append("- ⚔️ **Scrims:**")
                for slot in self.scrim_slots:
                    lines.append(f"  - {slot.format_display()}")
            elif self.scrim_time is not None:
                scrim_map_suffix = f" · Map: **{self.scrim_map}**" if self.scrim_map else ""
                if self.scrim_ready:
                    scrim_status = (
                        f"⚔️ Scrim: **READY** ({self.total_available} players) "
                        f"@ `{self.scrim_time}`{scrim_map_suffix}"
                    )
                else:
                    scrim_status = (
                        f"⚔️ Scrim: needs **{self.scrim_missing}** more for 10 "
                        f"@ `{self.scrim_time}`{scrim_map_suffix}"
                    )
                lines.append(f"- {scrim_status}")
            else:
                lines.append("- ⚔️ Scrim: **OFF**")

        # VOD section (show for VOD or MIXED)
        if self.day_type in ("VOD", "MIXED"):
            if self.vod_session:
                lines.append(f"- 🎬 {self.vod_session.format_display()}")
            else:
                lines.append("- 🎬 VOD: **OFF**")

        # Practice section (show for any activity day)
        if self.day_type not in ("FREE", "OFF"):
            if self.practice_time is not None:
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
                lines.append(f"- {practice_status}")

        # Availability summary
        team_lines = ", ".join(
            f"Team {team}: {count}" for team, count in sorted(self.team_counts.items())
        ) or "No teams set"

        names = ", ".join(self.available_names[:8]) if self.available_names else "No signups"
        if len(self.available_names) > 8:
            names += f" +{len(self.available_names) - 8} more"

        lines.append(f"- 👥 **{self.total_available}** available ({names})")
        lines.append(f"- Teams: {team_lines}")

        return "\n".join(lines) + "\n"


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

        # Get scrim slot config
        scrims_per_day = self.config_store.get_scrims_per_day(guild_id)
        scrim_spacing = self.config_store.get_scrim_spacing(guild_id)

        # Get VOD config
        vod_start_time = self.config_store.get_vod_start_time(guild_id)
        vod_duration = self.config_store.get_vod_session_minutes(guild_id)

        for day in WEEK_DAYS:
            # Get day type and label
            day_type = self.config_store.get_day_type(guild_id, day)
            day_label = self.config_store.get_day_label(guild_id, day)

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

            # Build scrim slots if day type allows scrims
            scrim_slots: List[ScrimSlot] = []
            if day_type in ("SCRIM", "MIXED") and scrim_time:
                scrim_slots = self._build_scrim_slots(
                    guild_id, day, scrim_time, scrims_per_day, scrim_spacing
                )

            # Build VOD session if day type allows
            vod_session: Optional[VodSession] = None
            if day_type in ("VOD", "MIXED"):
                vod_users = self.availability_store.users_for_vod_day(day)
                if vod_users or vod_start_time:
                    vod_session = VodSession(
                        start_time=vod_start_time,
                        duration_minutes=vod_duration,
                        available_count=len(vod_users),
                        available_names=[u["display_name"] for u in vod_users],
                    )

            summaries.append(
                DaySummary(
                    day=day,
                    day_type=day_type,
                    day_label=day_label,
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
                    scrim_slots=scrim_slots,
                    vod_session=vod_session,
                )
            )

        return summaries

    def _build_scrim_slots(
        self,
        guild_id: int,
        day: str,
        base_time: str,
        num_slots: int,
        spacing_minutes: int,
    ) -> List[ScrimSlot]:
        """Build scrim slot information for a day."""
        slots = []

        # Parse base time
        try:
            parts = base_time.split(":")
            base_hour = int(parts[0])
            base_minute = int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            return slots

        for i in range(num_slots):
            slot_num = i + 1
            # Calculate slot time
            total_minutes = base_hour * 60 + base_minute + (i * spacing_minutes)
            slot_hour = (total_minutes // 60) % 24
            slot_minute = total_minutes % 60
            slot_time = f"{slot_hour:02d}:{slot_minute:02d}"

            # Get users available for this slot
            slot_users = self.availability_store.users_for_scrim_slot(day, slot_num)

            slots.append(ScrimSlot(
                slot_number=slot_num,
                start_time=slot_time,
                available_count=len(slot_users),
                available_names=[u["display_name"] for u in slot_users],
            ))

        return slots

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
        # Count stats
        total_premier_days = sum(1 for s in summaries if s.day_type == "PREMIER")
        total_scrim_days = sum(1 for s in summaries if s.day_type in ("SCRIM", "MIXED") and s.scrim_time)
        total_vod_days = sum(1 for s in summaries if s.day_type in ("VOD", "MIXED"))
        total_available = sum(s.total_available for s in summaries)

        header = (
            f"## 📅 Weekly Schedule — {guild_name}\n"
            f"_🏆 {total_premier_days} Premier days · ⚔️ {total_scrim_days} Scrim days · 🎬 {total_vod_days} VOD days_\n"
            f"_👥 {total_available} total signups this week_\n\n"
        )
        lines = [header]
        for summary in summaries:
            lines.append(summary.to_lines())

        footer = (
            "\n---\n"
            "_Use `/availability panel` to sign up · `/premier days view` to see day types_"
        )
        lines.append(footer)
        return "\n".join(lines)

    def build_dashboard_embed(
        self,
        guild_id: int,
        guild_name: str,
        view_mode: str = "all"
    ) -> Tuple[str, str]:
        """Build dashboard content with optional view filtering.

        Args:
            guild_id: The guild ID
            guild_name: The guild name
            view_mode: 'all', 'premier', 'scrim', or 'vod'

        Returns:
            Tuple of (title, description)
        """
        summaries = self.build_week(guild_id)

        # Filter based on view mode
        if view_mode == "premier":
            summaries = [s for s in summaries if s.day_type == "PREMIER"]
            title = f"🏆 Premier Schedule — {guild_name}"
        elif view_mode == "scrim":
            summaries = [s for s in summaries if s.day_type in ("SCRIM", "MIXED")]
            title = f"⚔️ Scrim Schedule — {guild_name}"
        elif view_mode == "vod":
            summaries = [s for s in summaries if s.day_type in ("VOD", "MIXED")]
            title = f"🎬 VOD Schedule — {guild_name}"
        else:
            title = f"📅 Weekly Schedule — {guild_name}"

        if not summaries:
            description = "_No days configured for this view._\n\nUse `/premier days set` to configure day types."
        else:
            lines = []
            for summary in summaries:
                lines.append(summary.to_lines())
            description = "\n".join(lines)

        return title, description
