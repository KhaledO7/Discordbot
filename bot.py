from __future__ import annotations

import logging
import os
from datetime import datetime, date
from functools import lru_cache
from typing import Dict, List, Optional, Tuple, Set

import discord
from discord import app_commands
from discord.ext import commands, tasks

from scheduler import ScheduleBuilder
from storage import AvailabilityStore, GuildConfigStore, WEEK_DAYS

logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------

def _safe_int_env(var_name: str) -> Optional[int]:
    raw = os.getenv(var_name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        logging.warning("Ignoring non-numeric value for %s: %s", var_name, raw)
        return None


TOKEN = os.getenv("DISCORD_BOT_TOKEN")

ANNOUNCEMENT_CHANNEL_ID = _safe_int_env("ANNOUNCEMENT_CHANNEL_ID")
AVAILABLE_ROLE_ID = _safe_int_env("AVAILABLE_ROLE_ID")
TEAM_A_ROLE_ID = _safe_int_env("TEAM_A_ROLE_ID")
TEAM_B_ROLE_ID = _safe_int_env("TEAM_B_ROLE_ID")

AUTO_RESET_DAY = os.getenv("AUTO_RESET_DAY", "monday").lower()
try:
    parsed_hour = int(os.getenv("AUTO_RESET_HOUR", "8"))
    AUTO_RESET_HOUR = parsed_hour if 0 <= parsed_hour <= 23 else 8
except ValueError:
    logging.warning("AUTO_RESET_HOUR is not a number; defaulting to 8")
    AUTO_RESET_HOUR = 8


# ---------------------------------------------------------
# Utility functions
# ---------------------------------------------------------

def normalize_day(day: str) -> Optional[str]:
    day = day.strip().lower()
    for candidate in WEEK_DAYS:
        if candidate.startswith(day):
            return candidate
    return None


def parse_days(raw: str) -> List[str]:
    segments = (segment.strip() for segment in raw.split(","))
    normalized = (normalize_day(segment) for segment in segments if segment)
    return [day for day in normalized if day]


@lru_cache(maxsize=1)
def env_team_roles() -> Tuple[Optional[int], Optional[int]]:
    return TEAM_A_ROLE_ID, TEAM_B_ROLE_ID


def infer_team(
    member: discord.Member,
    fallback: Optional[str],
    configured_roles: Dict[str, Optional[int]],
    env_roles: Tuple[Optional[int], Optional[int]],
) -> Optional[str]:
    if fallback:
        return fallback.upper()

    member_role_ids = {role.id for role in member.roles}
    team_a_id = configured_roles.get("A") or env_roles[0]
    team_b_id = configured_roles.get("B") or env_roles[1]
    if team_a_id and team_a_id in member_role_ids:
        return "A"
    if team_b_id and team_b_id in member_role_ids:
        return "B"
    return None


def format_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.brand_red())


def parse_time_hhmm(time_str: str) -> Optional[Tuple[int, int]]:
    """Parse 'HH:MM' into (hour, minute). Returns None if invalid."""
    try:
        parts = time_str.strip().split(":")
        if len(parts) != 2:
            return None
        hour = int(parts[0])
        minute = int(parts[1])
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
        return None
    except Exception:
        return None


# ---------------------------------------------------------
# Availability UI (select + clear button)
# ---------------------------------------------------------

class AvailabilitySelect(discord.ui.Select):
    def __init__(self, cog: "AvailabilityCog") -> None:
        options = [discord.SelectOption(label=day.title(), value=day) for day in WEEK_DAYS]
        super().__init__(
            placeholder="Pick the days you can play",
            min_values=1,
            max_values=len(options),
            options=options,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        saved_days, team = await self.cog._save_availability(member, list(self.values), None)
        pretty_days = ", ".join(day.title() for day in saved_days)
        await interaction.response.send_message(
            f"Saved availability for **{member.display_name}**: {pretty_days} | Team: {team or 'Not set'}",
            ephemeral=True,
        )


class AvailabilityClearButton(discord.ui.Button):
    def __init__(self, cog: "AvailabilityCog") -> None:
        super().__init__(style=discord.ButtonStyle.secondary, label="Clear my week")
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        self.cog.availability_store.clear_user(member.id)
        await interaction.response.send_message("Cleared your availability for the week.", ephemeral=True)


class AvailabilityPanelView(discord.ui.View):
    def __init__(self, cog: "AvailabilityCog") -> None:
        super().__init__(timeout=60 * 60)
        self.add_item(AvailabilitySelect(cog))
        self.add_item(AvailabilityClearButton(cog))


# ---------------------------------------------------------
# Availability Commands
# ---------------------------------------------------------

class AvailabilityCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        availability_store: AvailabilityStore,
        config_store: GuildConfigStore,
    ) -> None:
        self.bot = bot
        self.availability_store = availability_store
        self.config_store = config_store

    availability = app_commands.Group(name="availability", description="Manage Valorant availability")

    async def _save_availability(
        self, member: discord.Member, days: List[str], team_override: Optional[str]
    ) -> Tuple[List[str], Optional[str]]:
        guild_id = member.guild.id if member.guild else None
        configured_roles = (
            self.config_store.get_team_roles(guild_id) if guild_id else {"A": None, "B": None}
        )
        normalized_team = infer_team(member, team_override, configured_roles, env_team_roles())
        normalized_days = sorted({day.lower() for day in days if normalize_day(day)})

        self.availability_store.set_availability(
            user_id=member.id,
            display_name=member.display_name,
            team=normalized_team,
            days=normalized_days,
        )
        return normalized_days, normalized_team

    async def _sync_member_role_today(self, member: discord.Member, days: List[str]) -> None:
        """Grant/remove availability role for this member based on today's availability."""
        if not member.guild:
            return

        today = WEEK_DAYS[datetime.now().weekday()]
        guild_id = member.guild.id

        role_id = self.config_store.get_ping_role(guild_id) or AVAILABLE_ROLE_ID
        if not role_id:
            return

        role = member.guild.get_role(role_id)
        if not role:
            return

        has_role = role in member.roles
        is_available_today = today in [d.lower() for d in days]

        try:
            if is_available_today and not has_role:
                await member.add_roles(role, reason="Marked available today")
            elif not is_available_today and has_role:
                await member.remove_roles(role, reason="No longer available today")
        except discord.HTTPException:
            logging.warning("Failed to update availability role for %s", member)

    @availability.command(name="set", description="Set the days you can play this week")
    @app_commands.describe(days="Comma-separated days (e.g. wed, thu, sat)", team="Optional team override (A or B)")
    async def availability_set(
        self, interaction: discord.Interaction, days: str, team: Optional[str] = None
    ) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        normalized_days = parse_days(days)
        if not normalized_days:
            await interaction.response.send_message(
                "No valid days provided. Try `wed, thu, sat`.", ephemeral=True
            )
            return

        normalized_days, normalized_team = await self._save_availability(member, normalized_days, team)

        # Try to instantly sync today's role
        await self._sync_member_role_today(member, normalized_days)

        pretty_days = ", ".join(day.title() for day in normalized_days)
        team_message = normalized_team or "Not set"
        await interaction.response.send_message(
            f"Saved availability for **{member.display_name}**: {pretty_days} | Team: {team_message}",
            ephemeral=True,
        )

    @availability.command(name="clear", description="Clear your saved availability")
    async def availability_clear(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        self.availability_store.clear_user(member.id)
        await interaction.response.send_message("Availability cleared!", ephemeral=True)

    @availability.command(name="mine", description="View your saved availability")
    async def availability_mine(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        days = self.availability_store.get_user_days(member.id)
        if not days:
            await interaction.response.send_message("No availability saved yet.", ephemeral=True)
            return

        pretty_days = ", ".join(day.title() for day in days)
        await interaction.response.send_message(f"You are marked available on: {pretty_days}", ephemeral=True)

    @availability.command(name="day", description="See everyone available for a given day")
    @app_commands.describe(day="Day of week (e.g. friday)")
    async def availability_day(self, interaction: discord.Interaction, day: str) -> None:
        normalized = normalize_day(day)
        if not normalized:
            await interaction.response.send_message("Please provide a valid day.", ephemeral=True)
            return

        users = self.availability_store.users_for_day(normalized)
        if not users:
            await interaction.response.send_message(
                f"No one has signed up for {normalized.title()} yet.", ephemeral=True
            )
            return

        lines = [f"{user['display_name']} (Team {user.get('team') or 'Not set'})" for user in users]
        embed = format_embed(
            title=f"Availability for {normalized.title()}",
            description="\n".join(lines),
        )
        await interaction.response.send_message(embed=embed)

    @availability.command(
        name="panel",
        description="Post a signup panel with a select menu + clear button for quick updates",
    )
    async def availability_panel(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not interaction.channel:
            await interaction.response.send_message("Run this in a server channel.", ephemeral=True)
            return

        embed = format_embed(
            "Weekly Signup Panel",
            (
                "Pick your days below to save availability quickly. "
                "Use the clear button to wipe your week and re-select."
            ),
        )
        view = AvailabilityPanelView(self)
        await interaction.channel.send(embed=embed, view=view)
        await interaction.response.send_message("Signup panel posted!", ephemeral=True)

    @availability.command(
        name="resetweek", description="Admins: clear all saved availability for a fresh week"
    )
    async def availability_resetweek(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        if not member.guild_permissions.manage_guild and not member.guild_permissions.administrator:
            await interaction.response.send_message(
                "You need Manage Server permissions to reset the week.", ephemeral=True
            )
            return

        cleared = self.availability_store.reset_all()
        await interaction.response.send_message(
            f"Cleared availability for {cleared} players. Fresh week ready!", ephemeral=True
        )


# ---------------------------------------------------------
# Schedule Commands
# ---------------------------------------------------------

class ScheduleCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        availability_store: AvailabilityStore,
        config_store: GuildConfigStore,
    ) -> None:
        self.bot = bot
        self.availability_store = availability_store
        self.config_store = config_store
        self.builder = ScheduleBuilder(availability_store, config_store)

    schedule = app_commands.Group(name="schedule", description="Build and post weekly schedules")

    @schedule.command(name="preview", description="Preview the current weekly schedule")
    async def schedule_preview(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        summaries = self.builder.build_week(interaction.guild.id)
        text = ScheduleBuilder.format_schedule(interaction.guild.name, summaries)
        embed = format_embed("Valorant Weekly Schedule", text)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @schedule.command(name="post", description="Post the schedule to the announcement channel")
    async def schedule_post(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        summaries = self.builder.build_week(interaction.guild.id)
        text = ScheduleBuilder.format_schedule(interaction.guild.name, summaries)
        embed = format_embed("Valorant Weekly Schedule", text)

        channel_id = self._resolve_announcement_channel_id(interaction.guild)
        if not channel_id:
            await interaction.response.send_message(
                "No announcement channel configured. Use `/config announcement` first.",
                ephemeral=True,
            )
            return

        channel = interaction.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Announcement channel not found.", ephemeral=True)
            return

        mention = self._resolve_ping_mention(interaction.guild)
        content = f"{mention} Weekly schedule updated!" if mention else "Weekly schedule updated!"
        await channel.send(content=content, embed=embed)
        await interaction.response.send_message("Schedule posted!", ephemeral=True)

    @schedule.command(name="check_times", description="Show configured scrim and premier times")
    async def schedule_check_times(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        guild_id = interaction.guild.id
        lines: List[str] = ["**Scrim Times (HH:MM ET)**", ""]
        for day in WEEK_DAYS:
            scrim = self.config_store.get_scrim_time(guild_id, day)
            lines.append(f"- {day.title()}: `{scrim}`" if scrim else f"- {day.title()}: Off")

        lines.append("\n**Premier Windows**\n")
        for day in WEEK_DAYS:
            window = self.config_store.get_premier_window(guild_id, day)
            lines.append(f"- {day.title()}: `{window}`" if window else f"- {day.title()}: Off")

        embed = format_embed("Configured Times", "\n".join(lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @schedule.command(
        name="reset_schedule",
        description="Reset scrim and premier times back to their default schedule",
    )
    async def schedule_reset_schedule(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        if not member.guild_permissions.manage_guild and not member.guild_permissions.administrator:
            await interaction.response.send_message(
                "You need Manage Server permissions to reset the schedule.", ephemeral=True
            )
            return

        self.config_store.reset_entire_schedule(interaction.guild.id)
        await interaction.response.send_message(
            "Scrim times and premier windows reset to defaults for this server.",
            ephemeral=True,
        )

    def _resolve_announcement_channel_id(self, guild: discord.Guild) -> Optional[int]:
        configured = self.config_store.get_announcement_channel(guild.id)
        if configured:
            return configured
        if ANNOUNCEMENT_CHANNEL_ID:
            return ANNOUNCEMENT_CHANNEL_ID
        return None

    def _resolve_ping_mention(self, guild: discord.Guild) -> Optional[str]:
        configured_role_id = self.config_store.get_ping_role(guild.id) or AVAILABLE_ROLE_ID
        if not configured_role_id:
            return None
        role = guild.get_role(configured_role_id)
        if role:
            return role.mention
        return None


# ---------------------------------------------------------
# Config Commands
# ---------------------------------------------------------

class ConfigCog(commands.Cog):
    def __init__(self, bot: commands.Bot, config_store: GuildConfigStore) -> None:
        self.bot = bot
        self.config_store = config_store

    config = app_commands.Group(name="config", description="Configure announcements, roles, and times")

    @config.command(name="announcement", description="Set the channel for weekly announcements")
    @app_commands.describe(channel="Channel to post schedules to")
    async def config_announcement(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        self.config_store.set_announcement_channel(interaction.guild.id, channel.id)
        await interaction.response.send_message(
            f"Announcement channel set to {channel.mention}", ephemeral=True
        )

    @config.command(name="pingrole", description="Set the role to ping / assign for availability")
    @app_commands.describe(role="Role to mention and grant to available players")
    async def config_ping_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        self.config_store.set_ping_role(interaction.guild.id, role.id)
        await interaction.response.send_message(f"Ping / availability role set to {role.mention}", ephemeral=True)

    @config.command(
        name="teamroles",
        description="Set the Discord roles that map to Team A and Team B",
    )
    @app_commands.describe(team_a="Role for Team A", team_b="Role for Team B")
    async def config_team_roles(
        self,
        interaction: discord.Interaction,
        team_a: Optional[discord.Role] = None,
        team_b: Optional[discord.Role] = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        if not team_a and not team_b:
            await interaction.response.send_message(
                "Provide at least one role for Team A or Team B.", ephemeral=True
            )
            return

        self.config_store.set_team_roles(
            interaction.guild.id,
            team_a_role_id=team_a.id if team_a else None,
            team_b_role_id=team_b.id if team_b else None,
        )

        parts = []
        if team_a:
            parts.append(f"Team A → {team_a.mention}")
        if team_b:
            parts.append(f"Team B → {team_b.mention}")
        await interaction.response.send_message(
            "Saved team roles: " + ", ".join(parts), ephemeral=True
        )

    # ---- Scrim time config commands ----

    @config.command(
        name="scrim_set",
        description="Set scrim start time for a given day (HH:MM ET, or 'off')",
    )
    @app_commands.describe(day="Day of week (e.g. wed, friday)", time="Time in HH:MM, or 'off'")
    async def config_scrim_set(self, interaction: discord.Interaction, day: str, time: str) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        normalized = normalize_day(day)
        if not normalized:
            await interaction.response.send_message("Invalid day. Try 'wed', 'friday', etc.", ephemeral=True)
            return

        time = time.strip().lower()
        if time in {"off", "none"}:
            self.config_store.set_scrim_time(interaction.guild.id, normalized, None)
            await interaction.response.send_message(
                f"Scrims turned **off** for {normalized.title()}.", ephemeral=True
            )
            return

        parsed = parse_time_hhmm(time)
        if not parsed:
            await interaction.response.send_message(
                "Invalid time. Use 24h format like `19:00` for 7 PM.", ephemeral=True
            )
            return

        self.config_store.set_scrim_time(interaction.guild.id, normalized, f"{parsed[0]:02d}:{parsed[1]:02d}")
        await interaction.response.send_message(
            f"Scrim time for {normalized.title()} set to `{parsed[0]:02d}:{parsed[1]:02d}` ET.",
            ephemeral=True,
        )

    @config.command(
        name="scrim_reset_day",
        description="Turn off scrims for a specific day",
    )
    @app_commands.describe(day="Day of week (e.g. monday)")
    async def config_scrim_reset_day(self, interaction: discord.Interaction, day: str) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        normalized = normalize_day(day)
        if not normalized:
            await interaction.response.send_message("Invalid day.", ephemeral=True)
            return

        self.config_store.set_scrim_time(interaction.guild.id, normalized, None)
        await interaction.response.send_message(
            f"Scrims turned **off** for {normalized.title()}.", ephemeral=True
        )

    @config.command(
        name="scrim_reset_all",
        description="Reset scrim times back to default schedule (Wed–Sun active)",
    )
    async def config_scrim_reset_all(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        self.config_store.reset_scrim_times(interaction.guild.id)
        await interaction.response.send_message(
            "Scrim times reset to **default** for this server.", ephemeral=True
        )

    @config.command(
        name="scrim_check",
        description="List scrim times for each day",
    )
    async def config_scrim_check(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        guild_id = interaction.guild.id
        lines: List[str] = ["**Scrim Times (HH:MM ET)**", ""]
        for d in WEEK_DAYS:
            t = self.config_store.get_scrim_time(guild_id, d)
            lines.append(f"- {d.title()}: `{t}`" if t else f"- {d.title()}: Off")

        embed = format_embed("Scrim Times", "\n".join(lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---- Premier window config commands ----

    @config.command(
        name="premier_set",
        description="Set premier window for a given day (e.g. 19:00-20:00, or 'off')",
    )
    @app_commands.describe(day="Day of week (e.g. wed)", window="Time window like 19:00-20:00, or 'off'")
    async def config_premier_set(self, interaction: discord.Interaction, day: str, window: str) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        normalized = normalize_day(day)
        if not normalized:
            await interaction.response.send_message("Invalid day.", ephemeral=True)
            return

        window = window.strip().lower()
        if window in {"off", "none"}:
            self.config_store.set_premier_window(interaction.guild.id, normalized, None)
            await interaction.response.send_message(
                f"Premier turned **off** for {normalized.title()}.", ephemeral=True
            )
            return

        # Minimal validation: expect "HH:MM-HH:MM"
        parts = window.split("-")
        if len(parts) != 2 or not (parse_time_hhmm(parts[0]) and parse_time_hhmm(parts[1])):
            await interaction.response.send_message(
                "Invalid window. Use `19:00-20:00` format or 'off'.", ephemeral=True
            )
            return

        self.config_store.set_premier_window(interaction.guild.id, normalized, window)
        await interaction.response.send_message(
            f"Premier window for {normalized.title()} set to `{window}`.", ephemeral=True
        )

    @config.command(
        name="premier_reset_day",
        description="Turn off premier for a specific day",
    )
    @app_commands.describe(day="Day of week (e.g. sunday)")
    async def config_premier_reset_day(self, interaction: discord.Interaction, day: str) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        normalized = normalize_day(day)
        if not normalized:
            await interaction.response.send_message("Invalid day.", ephemeral=True)
            return

        self.config_store.set_premier_window(interaction.guild.id, normalized, None)
        await interaction.response.send_message(
            f"Premier turned **off** for {normalized.title()}.", ephemeral=True
        )

    @config.command(
        name="premier_reset_all",
        description="Reset premier windows back to default schedule (Wed–Sun active)",
    )
    async def config_premier_reset_all(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        self.config_store.reset_premier_windows(interaction.guild.id)
        await interaction.response.send_message(
            "Premier windows reset to **default** for this server.", ephemeral=True
        )

    @config.command(
        name="premier_check",
        description="List premier windows for each day",
    )
    async def config_premier_check(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        guild_id = interaction.guild.id
        lines: List[str] = ["**Premier Windows**", ""]
        for d in WEEK_DAYS:
            w = self.config_store.get_premier_window(guild_id, d)
            lines.append(f"- {d.title()}: `{w}`" if w else f"- {d.title()}: Off")

        embed = format_embed("Premier Windows", "\n".join(lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------
# Background Tasks:
# - Weekly auto-reset of availability
# - Daily role sync for "available" role
# - 30-minute pre-scrim pings
# ---------------------------------------------------------

class BackgroundTasksCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        availability_store: AvailabilityStore,
        config_store: GuildConfigStore,
    ) -> None:
        self.bot = bot
        self.availability_store = availability_store
        self.config_store = config_store

        # Determine which weekday to reset on (0=Monday, 6=Sunday)
        try:
            self._reset_weekday_index = WEEK_DAYS.index(AUTO_RESET_DAY)
        except ValueError:
            logging.warning("Invalid AUTO_RESET_DAY %s, defaulting to monday", AUTO_RESET_DAY)
            self._reset_weekday_index = 0

        self.last_reset_date: Optional[date] = None
        self._last_scrim_ping: Dict[Tuple[int, str], date] = {}

    async def cog_load(self) -> None:
        if not self.auto_reset_task.is_running():
            self.auto_reset_task.start()
        if not self.role_sync_task.is_running():
            self.role_sync_task.start()
        if not self.scrim_ping_task.is_running():
            self.scrim_ping_task.start()

    def cog_unload(self) -> None:
        for task in (self.auto_reset_task, self.role_sync_task, self.scrim_ping_task):
            if task.is_running():
                task.cancel()

    # ---- Helpers ----

    def _resolve_announcement_channel_id(self, guild: discord.Guild) -> Optional[int]:
        configured = self.config_store.get_announcement_channel(guild.id)
        if configured:
            return configured
        if ANNOUNCEMENT_CHANNEL_ID:
            return ANNOUNCEMENT_CHANNEL_ID
        return None

    def _resolve_availability_role_id(self, guild: discord.Guild) -> Optional[int]:
        configured = self.config_store.get_ping_role(guild.id)
        if configured:
            return configured
        return AVAILABLE_ROLE_ID

    # ---- Auto-reset availability weekly ----

    @tasks.loop(minutes=30)
    async def auto_reset_task(self) -> None:
        now = datetime.now()
        if now.weekday() != self._reset_weekday_index or now.hour < AUTO_RESET_HOUR:
            return
        if self.last_reset_date == now.date():
            return

        cleared = self.availability_store.reset_all()
        self.last_reset_date = now.date()
        logging.info("Auto-reset availability for new week; cleared %d users", cleared)

        # Announce reset in each guild that has an announcement channel
        for guild in self.bot.guilds:
            channel_id = self._resolve_announcement_channel_id(guild)
            if not channel_id:
                continue
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue
            try:
                await channel.send(
                    f"Weekly reset done: cleared availability for {cleared} players. "
                    "Set your new days with `/availability set` or the signup panel!"
                )
            except discord.HTTPException:
                logging.warning("Failed to announce weekly reset in guild %s", guild.name)

    @auto_reset_task.before_loop
    async def before_auto_reset_task(self) -> None:
        await self.bot.wait_until_ready()

    # ---- Daily role sync for "available" role ----

    @tasks.loop(minutes=30)
    async def role_sync_task(self) -> None:
        now = datetime.now()
        today = WEEK_DAYS[now.weekday()]
        users_today = self.availability_store.users_for_day(today)
        available_ids: Set[int] = {int(info["id"]) for info in users_today}

        for guild in self.bot.guilds:
            role_id = self._resolve_availability_role_id(guild)
            if not role_id:
                continue
            role = guild.get_role(role_id)
            if not role:
                continue

            # Remove from members who should not have it
            for member in guild.members:
                has_role = role in member.roles
                should_have = member.id in available_ids
                if has_role and not should_have:
                    try:
                        await member.remove_roles(role, reason="Not available today")
                    except discord.HTTPException:
                        logging.warning("Failed to remove availability role from %s", member)

            # Grant to members who should have it
            for uid in available_ids:
                member = guild.get_member(uid)
                if not member:
                    continue
                if role not in member.roles:
                    try:
                        await member.add_roles(role, reason="Available today")
                    except discord.HTTPException:
                        logging.warning("Failed to add availability role to %s", member)

    @role_sync_task.before_loop
    async def before_role_sync_task(self) -> None:
        await self.bot.wait_until_ready()

    # ---- Pre-scrim pings (30 minutes before scrim start) ----

    @tasks.loop(minutes=1)
    async def scrim_ping_task(self) -> None:
        now = datetime.now()
        today = WEEK_DAYS[now.weekday()]
        minutes_now = now.hour * 60 + now.minute

        for guild in self.bot.guilds:
            scrim_time = self.config_store.get_scrim_time(guild.id, today)
            if not scrim_time:
                continue

            parsed = parse_time_hhmm(scrim_time)
            if not parsed:
                continue

            scrim_minutes = parsed[0] * 60 + parsed[1]
            pre_minutes = scrim_minutes - 30
            if pre_minutes < 0:
                continue

            if minutes_now != pre_minutes:
                continue

            key = (guild.id, today)
            if self._last_scrim_ping.get(key) == now.date():
                continue  # already pinged today

            # Check if there are enough players
            users_today = self.availability_store.users_for_day(today)
            if len(users_today) < 10:
                continue

            channel_id = self._resolve_announcement_channel_id(guild)
            if not channel_id:
                continue
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue

            role_id = self._resolve_availability_role_id(guild)
            mention = ""
            if role_id:
                role = guild.get_role(role_id)
                if role:
                    mention = role.mention

            try:
                await channel.send(
                    f"{mention} Scrims start in **30 minutes** at `{scrim_time}` ET! "
                    f"We have **{len(users_today)}** players marked available."
                )
                self._last_scrim_ping[key] = now.date()
            except discord.HTTPException:
                logging.warning("Failed to send scrim ping in guild %s", guild.name)

    @scrim_ping_task.before_loop
    async def before_scrim_ping_task(self) -> None:
        await self.bot.wait_until_ready()


# ---------------------------------------------------------
# Bot setup
# ---------------------------------------------------------

def build_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.members = True

    bot = commands.Bot(command_prefix="!", intents=intents)

    availability_store = AvailabilityStore()
    config_store = GuildConfigStore()

    # Attach stores so other extensions (if any) can access them
    bot.availability_store = availability_store  # type: ignore[attr-defined]
    bot.config_store = config_store  # type: ignore[attr-defined]

    bot.add_cog(AvailabilityCog(bot, availability_store, config_store))
    bot.add_cog(ScheduleCog(bot, availability_store, config_store))
    bot.add_cog(ConfigCog(bot, config_store))
    bot.add_cog(BackgroundTasksCog(bot, availability_store, config_store))

    @bot.event
    async def on_ready() -> None:
        assert bot.user is not None
        logging.info("Logged in as %s", bot.user)
        try:
            synced = await bot.tree.sync()
            logging.info("Synced %d app commands", len(synced))
        except discord.HTTPException as exc:
            logging.error("Failed to sync commands: %s", exc)

    return bot


def main() -> None:
    if not TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN is required")

    bot = build_bot()
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
