from __future__ import annotations

import logging
import os
from datetime import datetime, date, time, timedelta
from functools import lru_cache
from typing import Dict, List, Optional, Tuple, Set

import discord
from discord import app_commands
from discord.ext import commands, tasks

from scheduler import ScheduleBuilder
from storage import (
    AvailabilityStore,
    GuildConfigStore,
    WEEK_DAYS,
)

logging.basicConfig(level=logging.INFO)

# ---- Environment helpers ----

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
ANNOUNCEMENT_CHANNEL_ID_ENV = _safe_int_env("ANNOUNCEMENT_CHANNEL_ID")
AVAILABLE_ROLE_ID_ENV = _safe_int_env("AVAILABLE_ROLE_ID")
TEAM_A_ROLE_ID_ENV = _safe_int_env("TEAM_A_ROLE_ID")
TEAM_B_ROLE_ID_ENV = _safe_int_env("TEAM_B_ROLE_ID")
AUTO_RESET_DAY_ENV = os.getenv("AUTO_RESET_DAY", "monday").lower()

try:
    parsed_hour = int(os.getenv("AUTO_RESET_HOUR", "8"))
    AUTO_RESET_HOUR_ENV = parsed_hour if 0 <= parsed_hour <= 23 else 8
except ValueError:
    logging.warning("AUTO_RESET_HOUR is not a number; defaulting to 8")
    AUTO_RESET_HOUR_ENV = 8


# Valorant maps for dropdowns
VALORANT_MAPS: List[str] = [
    "Abyss",
    "Ascent",
    "Breeze",
    "Bind",
    "Corrode",
    "Fracture",
    "Haven",
    "Icebox",
    "Lotus",
    "Pearl",
    "Split",
    "Sunset",
]

# Agent catalog grouped by role (based on official Valorant info up to 2024).
# Not every very-new agent may be listed; you can extend this if Riot adds more.
ROLE_AGENTS: Dict[str, List[str]] = {
    "duelist": [
        "Jett",
        "Phoenix",
        "Reyna",
        "Raze",
        "Yoru",
        "Neon",
        "Iso",
        "Waylay",
    ],
    "initiator": [
        "Sova",
        "Breach",
        "Skye",
        "KAY/O",
        "Fade",
        "Gekko",
        "Tejo",
    ],
    "sentinel": [
        "Sage",
        "Cypher",
        "Killjoy",
        "Chamber",
        "Deadlock",
        "Vyse",
        "Veto",
    ],
    "controller": [
        "Brimstone",
        "Viper",
        "Omen",
        "Astra",
        "Harbor",
        "Clove",
    ],
}


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
    return TEAM_A_ROLE_ID_ENV, TEAM_B_ROLE_ID_ENV


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


def _parse_hhmm_to_time(label: str) -> Optional[time]:
    """Parse a 'HH:MM' string into a time object. Returns None on error."""
    try:
        parts = label.split(":")
        if len(parts) != 2:
            return None
        hour = int(parts[0])
        minute = int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return time(hour=hour, minute=minute)
    except Exception:
        return None


# -------------------------- Availability UI --------------------------


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


# -------------------------- Agents UI --------------------------


class AgentRoleSelect(discord.ui.Select):
    def __init__(self, cog: "AgentsCog") -> None:
        options = [
            discord.SelectOption(label=role.title(), value=role)
            for role in ROLE_AGENTS.keys()
        ]
        super().__init__(
            placeholder="Pick one or more roles",
            min_values=1,
            max_values=len(options),
            options=options,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        view = self.view
        if not isinstance(view, AgentSelectView):
            await interaction.response.send_message("Internal view error.", ephemeral=True)
            return

        view.selected_roles = list(self.values)
        view.refresh_agent_options()

        await interaction.response.edit_message(
            content="Now pick which agents you play on those roles:",
            view=view,
        )


class AgentSelect(discord.ui.Select):
    def __init__(self, cog: "AgentsCog") -> None:
        super().__init__(
            placeholder="Pick your agents (multi-select)",
            min_values=1,
            max_values=25,
            options=[],
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        view = self.view
        if not isinstance(view, AgentSelectView):
            await interaction.response.send_message("Internal view error.", ephemeral=True)
            return

        roles = view.selected_roles
        agents = list(self.values)

        if not roles:
            await interaction.response.send_message(
                "Pick at least one role first, then choose agents.", ephemeral=True
            )
            return

        self.cog.availability_store.set_agents(
            user_id=member.id,
            display_name=member.display_name,
            roles=roles,
            agents=agents,
        )

        pretty_roles = ", ".join(r.title() for r in roles)
        pretty_agents = ", ".join(agents)
        await interaction.response.send_message(
            f"Saved your agents!\nRoles: **{pretty_roles}**\nAgents: **{pretty_agents}**",
            ephemeral=True,
        )


class AgentSelectView(discord.ui.View):
    def __init__(self, cog: "AgentsCog") -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.selected_roles: List[str] = []
        self.role_select = AgentRoleSelect(cog)
        self.agent_select = AgentSelect(cog)
        self.add_item(self.role_select)
        self.add_item(self.agent_select)

    def refresh_agent_options(self) -> None:
        agents: List[str] = []
        for role in self.selected_roles:
            agents.extend(ROLE_AGENTS.get(role, []))
        unique_agents = sorted(set(agents))
        self.agent_select.options = [
            discord.SelectOption(label=name, value=name) for name in unique_agents
        ]


# -------------------------- Cogs --------------------------


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
        self.schedule_builder = ScheduleBuilder(availability_store, config_store)

    schedule = app_commands.Group(name="schedule", description="Build and post weekly schedules")

    @schedule.command(name="preview", description="Preview the current schedule")
    async def schedule_preview(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        summaries = self.schedule_builder.build_week(interaction.guild.id)
        text = self.schedule_builder.format_schedule(interaction.guild.name, summaries)
        embed = format_embed("Valorant Weekly Schedule", text)
        await interaction.response.send_message(embed=embed)

    @schedule.command(name="post", description="Post the schedule to the announcement channel")
    async def schedule_post(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        summaries = self.schedule_builder.build_week(interaction.guild.id)
        text = self.schedule_builder.format_schedule(interaction.guild.name, summaries)
        embed = format_embed("Valorant Weekly Schedule", text)

        channel_id = self._resolve_announcement_channel_id(interaction.guild)
        if not channel_id:
            await interaction.response.send_message(
                "No announcement channel configured. Use /config announcement first.",
                ephemeral=True,
            )
            return

        channel = interaction.guild.get_channel(channel_id)
        if not channel:
            await interaction.response.send_message("Announcement channel not found.", ephemeral=True)
            return

        mention = self._resolve_ping_mention(interaction.guild)
        content = f"{mention} Weekly schedule updated!" if mention else "Weekly schedule updated!"
        await channel.send(content=content, embed=embed)
        await interaction.response.send_message("Schedule posted!", ephemeral=True)

    @schedule.command(
        name="pingcheck",
        description="Check if current numbers would trigger a scrim ping for a given day",
    )
    @app_commands.describe(day="Day of week (e.g. friday)")
    async def schedule_pingcheck(self, interaction: discord.Interaction, day: str) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        norm_day = normalize_day(day)
        if not norm_day:
            await interaction.response.send_message("Invalid day. Try monday, tuesday, etc.", ephemeral=True)
            return

        users = self.availability_store.users_for_day(norm_day)
        total = len(users)
        team_counts: Dict[str, int] = {"A": 0, "B": 0}
        for info in users:
            t = str(info.get("team") or "").upper()
            if t in team_counts:
                team_counts[t] += 1

        scrim_time = self.config_store.get_scrim_time(interaction.guild.id, norm_day)
        premier_window = self.config_store.get_premier_window(interaction.guild.id, norm_day)

        by_team = any(c >= 5 for c in team_counts.values())
        by_total = total >= 10

        will_ping = scrim_time is not None and (by_team or by_total)

        desc_lines = [
            f"**Day:** {norm_day.title()}",
            f"**Total available:** {total}",
            f"**Team A:** {team_counts['A']} · **Team B:** {team_counts['B']}",
            f"**Scrim time:** `{scrim_time}`" if scrim_time else "**Scrim time:** OFF",
            f"**Premier window:** `{premier_window}`" if premier_window else "**Premier:** OFF",
            "",
            "A scrim ping will trigger 30 minutes before the scrim time if:",
            "- At least **10 total** players are available, **or**",
            "- At least **5 players** from **one team** are available.",
            "",
            f"**Would ping with current numbers?** {'✅ Yes' if will_ping else '❌ No'}",
        ]

        embed = format_embed("Scrim Ping Check", "\n".join(desc_lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    def _resolve_announcement_channel_id(self, guild: discord.Guild) -> Optional[int]:
        configured = self.config_store.get_announcement_channel(guild.id)
        if configured:
            return configured
        if ANNOUNCEMENT_CHANNEL_ID_ENV:
            return ANNOUNCEMENT_CHANNEL_ID_ENV
        return None

    def _resolve_ping_mention(self, guild: discord.Guild) -> Optional[str]:
        configured_role_id = self.config_store.get_ping_role(guild.id) or AVAILABLE_ROLE_ID_ENV
        if not configured_role_id:
            return None
        role = guild.get_role(configured_role_id)
        if role:
            return role.mention
        return None


class ConfigCog(commands.Cog):
    def __init__(self, bot: commands.Bot, config_store: GuildConfigStore) -> None:
        self.bot = bot
        self.config_store = config_store

    config = app_commands.Group(name="config", description="Configure announcements, pings, times, maps")

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

    @config.command(name="pingrole", description="Set the role to ping when posting schedules")
    @app_commands.describe(role="Role to mention for availability updates and scrim pings")
    async def config_ping_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        self.config_store.set_ping_role(interaction.guild.id, role.id)
        await interaction.response.send_message(f"Ping role set to {role.mention}", ephemeral=True)

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

    @config.command(
        name="scrimtime",
        description="Set or clear the scrim start time for a given day (HH:MM in 24h, or 'off')",
    )
    @app_commands.describe(
        day="Day of week (e.g. monday)",
        time_label="24h time like '19:00' for 7 PM, or 'off' to disable scrims that day",
    )
    async def config_scrim_time(
        self,
        interaction: discord.Interaction,
        day: str,
        time_label: str,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        norm_day = normalize_day(day)
        if not norm_day:
            await interaction.response.send_message("Invalid day. Try monday, tuesday, etc.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not (
            member.guild_permissions.manage_guild or member.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to change scrim times.", ephemeral=True
            )
            return

        if time_label.lower() in {"off", "none", "disable"}:
            self.config_store.set_scrim_time(interaction.guild.id, norm_day, None)
            await interaction.response.send_message(
                f"Scrims for **{norm_day.title()}** turned **OFF**.", ephemeral=True
            )
            return

        t = _parse_hhmm_to_time(time_label)
        if t is None:
            await interaction.response.send_message(
                "Invalid time format. Use 24h `HH:MM`, e.g. `19:00` for 7 PM.", ephemeral=True
            )
            return

        self.config_store.set_scrim_time(interaction.guild.id, norm_day, time_label)
        await interaction.response.send_message(
            f"Scrim time for **{norm_day.title()}** set to `{time_label}`.", ephemeral=True
        )

    @config.command(name="check_scrimtimes", description="Show current scrim times for all days")
    async def config_check_scrimtimes(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        lines: List[str] = []
        for d in WEEK_DAYS:
            t = self.config_store.get_scrim_time(interaction.guild.id, d)
            label = t if t is not None else "OFF"
            lines.append(f"**{d.title()}**: `{label}`")
        embed = format_embed("Current Scrim Times", "\n".join(lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @config.command(name="reset_scrimtimes", description="Reset all scrim times to defaults")
    async def config_reset_scrimtimes(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not (
            member.guild_permissions.manage_guild or member.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to reset scrim times.", ephemeral=True
            )
            return

        self.config_store.reset_scrim_times(interaction.guild.id)
        await interaction.response.send_message(
            "All scrim times reset to default.", ephemeral=True
        )

    @config.command(
        name="premier_window",
        description="Set or clear the Premier window for a given day (e.g. '19:00-20:00' or 'off')",
    )
    @app_commands.describe(
        day="Day of week (e.g. wednesday)",
        window="Time window like '19:00-20:00', or 'off' to disable premier that day",
    )
    async def config_premier_window(
        self,
        interaction: discord.Interaction,
        day: str,
        window: str,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        norm_day = normalize_day(day)
        if not norm_day:
            await interaction.response.send_message("Invalid day. Try wednesday, thursday, etc.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not (
            member.guild_permissions.manage_guild or member.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to change premier windows.", ephemeral=True
            )
            return

        if window.lower() in {"off", "none", "disable"}:
            self.config_store.set_premier_window(interaction.guild.id, norm_day, None)
            await interaction.response.send_message(
                f"Premier window for **{norm_day.title()}** turned **OFF**.", ephemeral=True
            )
            return

        parts = window.split("-")
        if len(parts) != 2 or _parse_hhmm_to_time(parts[0]) is None or _parse_hhmm_to_time(parts[1]) is None:
            await interaction.response.send_message(
                "Invalid window format. Use `HH:MM-HH:MM`, e.g. `19:00-20:00`.", ephemeral=True
            )
            return

        self.config_store.set_premier_window(interaction.guild.id, norm_day, window)
        await interaction.response.send_message(
            f"Premier window for **{norm_day.title()}** set to `{window}`.", ephemeral=True
        )

    @config.command(name="reset_premier", description="Reset all Premier windows to defaults")
    async def config_reset_premier(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not (
            member.guild_permissions.manage_guild or member.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to reset premier windows.", ephemeral=True
            )
            return

        self.config_store.reset_premier_windows(interaction.guild.id)
        await interaction.response.send_message(
            "All premier windows reset to default.", ephemeral=True
        )

    @config.command(name="reset_schedule", description="Reset scrim times and premier windows to defaults")
    async def config_reset_schedule(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not (
            member.guild_permissions.manage_guild or member.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to reset the schedule.", ephemeral=True
            )
            return

        self.config_store.reset_entire_schedule(interaction.guild.id)
        await interaction.response.send_message(
            "Entire schedule (scrim times + premier windows) reset to defaults.", ephemeral=True
        )

    @config.command(name="check_premier", description="Show current Premier windows for all days")
    async def config_check_premier(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        lines: List[str] = []
        for d in WEEK_DAYS:
            w = self.config_store.get_premier_window(interaction.guild.id, d)
            label = w if w is not None else "OFF"
            lines.append(f"**{d.title()}**: `{label}`")
        embed = format_embed("Current Premier Windows", "\n".join(lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ----- Map configuration -----

    @config.command(
        name="map_premier",
        description="Set the Premier map for a given day",
    )
    @app_commands.describe(
        day="Day of week (e.g. wednesday)",
        map_name="Map to play for Premier on that day",
    )
    @app_commands.choices(
        map_name=[app_commands.Choice(name=m, value=m) for m in VALORANT_MAPS]
    )
    async def config_map_premier(
        self,
        interaction: discord.Interaction,
        day: str,
        map_name: app_commands.Choice[str],
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        norm_day = normalize_day(day)
        if not norm_day:
            await interaction.response.send_message("Invalid day. Try wednesday, thursday, etc.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not (
            member.guild_permissions.manage_guild or member.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to set maps.", ephemeral=True
            )
            return

        self.config_store.set_premier_map(interaction.guild.id, norm_day, map_name.value)
        await interaction.response.send_message(
            f"Premier map for **{norm_day.title()}** set to **{map_name.value}**.",
            ephemeral=True,
        )

    @config.command(
        name="map_scrim",
        description="Set the Scrim map for a given day",
    )
    @app_commands.describe(
        day="Day of week (e.g. wednesday)",
        map_name="Map to scrim on that day",
    )
    @app_commands.choices(
        map_name=[app_commands.Choice(name=m, value=m) for m in VALORANT_MAPS]
    )
    async def config_map_scrim(
        self,
        interaction: discord.Interaction,
        day: str,
        map_name: app_commands.Choice[str],
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        norm_day = normalize_day(day)
        if not norm_day:
            await interaction.response.send_message("Invalid day. Try wednesday, thursday, etc.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not (
            member.guild_permissions.manage_guild or member.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to set maps.", ephemeral=True
            )
            return

        self.config_store.set_scrim_map(interaction.guild.id, norm_day, map_name.value)
        await interaction.response.send_message(
            f"Scrim map for **{norm_day.title()}** set to **{map_name.value}**.",
            ephemeral=True,
        )

    @config.command(name="check_maps", description="Show current Premier/Scrim maps for all days")
    async def config_check_maps(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        lines: List[str] = []
        for d in WEEK_DAYS:
            p = self.config_store.get_premier_map(interaction.guild.id, d)
            s = self.config_store.get_scrim_map(interaction.guild.id, d)
            p_label = p if p is not None else "—"
            s_label = s if s is not None else "—"
            lines.append(f"**{d.title()}** · Premier: `{p_label}` · Scrim: `{s_label}`")

        embed = format_embed("Current Maps", "\n".join(lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)


class AgentsCog(commands.Cog):
    """Let players declare the roles/agents they play and inspect team agent comps."""

    def __init__(self, bot: commands.Bot, availability_store: AvailabilityStore, config_store: GuildConfigStore) -> None:
        self.bot = bot
        self.availability_store = availability_store
        self.config_store = config_store

    agents = app_commands.Group(name="agents", description="Pick and inspect Valorant agents")

    @agents.command(name="set", description="Pick your roles and agents via dropdowns")
    async def agents_set(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        view = AgentSelectView(self)
        await interaction.response.send_message(
            "Pick your **roles** first, then pick your **agents**.",
            view=view,
            ephemeral=True,
        )

    @agents.command(name="clear", description="Clear your saved roles/agents")
    async def agents_clear(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        self.availability_store.clear_agents(member.id)
        await interaction.response.send_message("Cleared your saved agents.", ephemeral=True)

    @agents.command(name="mine", description="View your saved roles/agents")
    async def agents_mine(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        info = self.availability_store.get_user_agents(member.id)
        roles = info["roles"]
        agents = info["agents"]

        if not roles and not agents:
            await interaction.response.send_message("You don't have any agents saved yet.", ephemeral=True)
            return

        lines = [
            f"**Roles:** {', '.join(r.title() for r in roles) if roles else '—'}",
            f"**Agents:** {', '.join(agents) if agents else '—'}",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @agents.command(
        name="team",
        description="Show agents played by Team A, Team B, or all players",
    )
    @app_commands.describe(team="Which team to inspect")
    @app_commands.choices(
        team=[
            app_commands.Choice(name="Team A", value="A"),
            app_commands.Choice(name="Team B", value="B"),
            app_commands.Choice(name="All", value="ALL"),
        ]
    )
    async def agents_team(
        self,
        interaction: discord.Interaction,
        team: app_commands.Choice[str],
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        target = team.value  # "A", "B", or "ALL"
        users = self.availability_store.all_users()
        # Team membership is stored in availability_store (kept in sync via on_member_update)
        bucket: Dict[str, List[str]] = {}

        for user_id, info in users.items():
            user_team = (str(info.get("team") or "")).upper()
            if target != "ALL" and user_team != target:
                continue
            agents = info.get("agents") or []
            if not agents:
                continue
            name = str(info.get("display_name", f"User {user_id}"))
            bucket[name] = list(agents)

        if not bucket:
            label = "all teams" if target == "ALL" else f"Team {target}"
            await interaction.response.send_message(
                f"No saved agents found for {label}.", ephemeral=True
            )
            return

        lines: List[str] = []
        for name, agents in bucket.items():
            lines.append(f"**{name}**: {', '.join(agents)}")

        label = "all teams" if target == "ALL" else f"Team {target}"
        embed = format_embed(f"Agents for {label}", "\n".join(lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)


class RoleSyncCog(commands.Cog):
    """Keeps the 'available' role in sync with today's availability
    and handles weekly reset and scrim pre-pings.
    """

    def __init__(
        self,
        bot: commands.Bot,
        availability_store: AvailabilityStore,
        config_store: GuildConfigStore,
    ) -> None:
        self.bot = bot
        self.availability_store = availability_store
        self.config_store = config_store
        self._target_weekday_index: int = self._resolve_reset_weekday()
        self._last_reset_date: Optional[date] = None
        self.role_sync_task.start()
        self.scrim_ping_task.start()

    def cog_unload(self) -> None:
        self.role_sync_task.cancel()
        self.scrim_ping_task.cancel()

    def _resolve_reset_weekday(self) -> int:
        try:
            return WEEK_DAYS.index(AUTO_RESET_DAY_ENV)
        except ValueError:
            logging.warning("Invalid AUTO_RESET_DAY %s, defaulting to Monday", AUTO_RESET_DAY_ENV)
            return 0

    def _resolve_today_label(self) -> str:
        idx = datetime.now().weekday()
        return WEEK_DAYS[idx]

    def _resolve_ping_role_id(self, guild: discord.Guild) -> Optional[int]:
        # guild-specific ping role beats env AVAILABLE_ROLE_ID
        return self.config_store.get_ping_role(guild.id) or AVAILABLE_ROLE_ID_ENV

    async def _sync_roles_for_guild(self, guild: discord.Guild) -> None:
        """Give/remove the 'available' role based on today's availability."""
        role_id = self._resolve_ping_role_id(guild)
        if not role_id:
            return

        role = guild.get_role(role_id)
        if role is None:
            return

        today_label = self._resolve_today_label()
        today_users = self.availability_store.users_for_day(today_label)
        allowed_ids: Set[int] = {int(u["id"]) for u in today_users}

        # Add role to those who should have it
        for user_info in today_users:
            member = guild.get_member(int(user_info["id"]))
            if member and role not in member.roles:
                try:
                    await member.add_roles(role, reason="Marked available for today's games")
                except discord.HTTPException:
                    logging.warning("Failed to add role to %s in %s", member, guild.name)

        # Remove role from others
        for member in guild.members:
            if role in member.roles and member.id not in allowed_ids:
                try:
                    await member.remove_roles(role, reason="No longer available today")
                except discord.HTTPException:
                    logging.warning("Failed to remove role from %s in %s", member, guild.name)

    @tasks.loop(minutes=30)
    async def role_sync_task(self) -> None:
        """Periodically sync roles and run weekly reset if configured."""
        now = datetime.now()

        # Weekly reset of availability (optional; uses env vars)
        should_reset = (
            now.weekday() == self._target_weekday_index
            and now.hour >= AUTO_RESET_HOUR_ENV
            and (self._last_reset_date is None or self._last_reset_date != now.date())
        )
        if should_reset:
            cleared = self.availability_store.reset_all()
            self._last_reset_date = now.date()
            logging.info("Auto-reset availability for new week; cleared %d users", cleared)

        # Always keep today's availability role in sync
        for guild in self.bot.guilds:
            await self._sync_roles_for_guild(guild)

    @role_sync_task.before_loop
    async def before_role_sync(self) -> None:
        await self.bot.wait_until_ready()

    # ---- Scrim pre-ping logic ----

    async def _maybe_ping_scrim_for_guild(self, guild: discord.Guild) -> None:
        today_label = self._resolve_today_label()
        scrim_time_label = self.config_store.get_scrim_time(guild.id, today_label)
        if scrim_time_label is None:
            return

        scrim_time = _parse_hhmm_to_time(scrim_time_label)
        if scrim_time is None:
            return

        now = datetime.now()
        target_dt = datetime.combine(now.date(), scrim_time)
        delta = target_dt - now

        # We want to ping between 25 and 35 minutes before start time
        if not (timedelta(minutes=25) <= delta <= timedelta(minutes=35)):
            return

        users = self.availability_store.users_for_day(today_label)
        if not users:
            return

        team_counts: Dict[str, int] = {"A": 0, "B": 0}
        for info in users:
            t = str(info.get("team") or "").upper()
            if t in team_counts:
                team_counts[t] += 1

        total = len(users)
        by_team = any(c >= 5 for c in team_counts.values())
        by_total = total >= 10

        # New rule: ping if 10 total OR >=5 from one team
        if not (by_team or by_total):
            return

        channel_id = self.config_store.get_announcement_channel(guild.id) or ANNOUNCEMENT_CHANNEL_ID_ENV
        if not channel_id:
            return

        channel = guild.get_channel(channel_id)
        if channel is None:
            return

        role_id = self._resolve_ping_role_id(guild)
        mention = ""
        if role_id:
            role = guild.get_role(role_id)
            if role:
                mention = role.mention + " "

        try:
            await channel.send(
                f"{mention}Scrim is **today** at `{scrim_time_label}` "
                f"({total} players signed, Team A: {team_counts['A']}, Team B: {team_counts['B']}). "
                f"This is your 30-minute reminder."
            )
        except discord.HTTPException:
            logging.warning("Failed to send scrim reminder in %s", guild.name)

    @tasks.loop(minutes=5)
    async def scrim_ping_task(self) -> None:
        """Check frequently if we are ~30 minutes before today's scrim and ping if ready."""
        for guild in self.bot.guilds:
            await self._maybe_ping_scrim_for_guild(guild)

    @scrim_ping_task.before_loop
    async def before_scrim_ping(self) -> None:
        await self.bot.wait_until_ready()


class ValorantBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.guilds = True
        super().__init__(command_prefix="!", intents=intents)

        self.availability_store = AvailabilityStore()
        self.config_store = GuildConfigStore()

    async def setup_hook(self) -> None:  # type: ignore[override]
        await self.add_cog(AvailabilityCog(self, self.availability_store, self.config_store))
        await self.add_cog(ScheduleCog(self, self.availability_store, self.config_store))
        await self.add_cog(ConfigCog(self, self.config_store))
        await self.add_cog(AgentsCog(self, self.availability_store, self.config_store))
        await self.add_cog(RoleSyncCog(self, self.availability_store, self.config_store))

        try:
            synced = await self.tree.sync()
            logging.info("Synced %d app commands", len(synced))
        except discord.HTTPException as exc:
            logging.error("Failed to sync commands: %s", exc)

    async def on_ready(self) -> None:  # type: ignore[override]
        assert self.user is not None
        logging.info("Logged in as %s", self.user)

    async def on_member_update(  # type: ignore[override]
        self,
        before: discord.Member,
        after: discord.Member,
    ) -> None:
        """Keep stored 'team' in AvailabilityStore in sync with current Discord roles.

        This fixes the issue where schedule would still show the old team
        unless the player re-ran /availability.
        """
        if before.guild is None or after.guild is None:
            return

        before_ids = {r.id for r in before.roles}
        after_ids = {r.id for r in after.roles}
        if before_ids == after_ids:
            return  # no role change

        configured_roles = self.config_store.get_team_roles(after.guild.id)
        new_team = infer_team(after, None, configured_roles, env_team_roles())

        days = self.availability_store.get_user_days(after.id)
        if not days:
            # No availability saved; nothing to update
            return

        self.availability_store.set_availability(
            user_id=after.id,
            display_name=after.display_name,
            team=new_team,
            days=days,
        )


def main() -> None:
    if not TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN is required")

    bot = ValorantBot()
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
