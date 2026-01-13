from __future__ import annotations

import asyncio
import logging
import os
import re
import traceback
from datetime import datetime, date, time, timedelta
from functools import lru_cache
from typing import Dict, List, Optional, Tuple, Set, Any, Callable, TypeVar

import discord
from discord import app_commands
from discord.ext import commands, tasks

from scheduler import ScheduleBuilder
from storage import (
    AvailabilityStore,
    GuildConfigStore,
    GameLogStore,
    WEEK_DAYS,
    DAY_TYPES,
)

# Type variable for generic functions
T = TypeVar('T')

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

# ---- Static data ----

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

# Agent catalog grouped by role.
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


def format_embed(title: str, description: str, color: discord.Color = None) -> discord.Embed:
    """Create a standardized embed with consistent styling."""
    if color is None:
        color = discord.Color.brand_red()
    return discord.Embed(title=title, description=description, color=color)


def has_captain_permission(
    member: discord.Member,
    config_store: GuildConfigStore,
) -> bool:
    """Check if a member has captain/admin permissions.

    Returns True if member is admin, has manage_guild, or has the captain role.
    """
    if member.guild_permissions.administrator:
        return True
    if member.guild_permissions.manage_guild:
        return True

    captain_role_id = config_store.get_captain_role(member.guild.id)
    if captain_role_id:
        return any(r.id == captain_role_id for r in member.roles)

    return False


def error_embed(title: str, description: str) -> discord.Embed:
    """Create an error-styled embed."""
    return discord.Embed(title=f"Error: {title}", description=description, color=discord.Color.red())


def success_embed(title: str, description: str) -> discord.Embed:
    """Create a success-styled embed."""
    return discord.Embed(title=title, description=description, color=discord.Color.green())


async def safe_respond(
    interaction: discord.Interaction,
    content: str = None,
    embed: discord.Embed = None,
    view: discord.ui.View = None,
    ephemeral: bool = True,
) -> bool:
    """Safely respond to an interaction, handling already-responded cases.

    Returns True if response succeeded, False otherwise.
    """
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content=content, embed=embed, view=view, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content=content, embed=embed, view=view, ephemeral=ephemeral)
        return True
    except discord.NotFound:
        logging.warning("Interaction expired or not found")
        return False
    except discord.HTTPException as e:
        logging.error("Failed to respond to interaction: %s", e)
        return False


async def safe_defer(interaction: discord.Interaction, ephemeral: bool = True) -> bool:
    """Safely defer an interaction response.

    Returns True if defer succeeded, False otherwise.
    """
    try:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=ephemeral)
        return True
    except discord.NotFound:
        logging.warning("Interaction expired before defer")
        return False
    except discord.HTTPException as e:
        logging.error("Failed to defer interaction: %s", e)
        return False


async def safe_edit_message(
    message: discord.Message,
    content: str = None,
    embed: discord.Embed = None,
    view: discord.ui.View = None,
) -> bool:
    """Safely edit a message, handling missing message cases.

    Returns True if edit succeeded, False otherwise.
    """
    try:
        await message.edit(content=content, embed=embed, view=view)
        return True
    except discord.NotFound:
        logging.warning("Message not found for edit")
        return False
    except discord.HTTPException as e:
        logging.error("Failed to edit message: %s", e)
        return False


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


# -------------------------- Persistent View Base --------------------------


class PersistentView(discord.ui.View):
    """Base class for views that handle timeouts gracefully."""

    def __init__(self, timeout: float = 3600.0) -> None:
        super().__init__(timeout=timeout)
        self.message: Optional[discord.Message] = None

    async def on_timeout(self) -> None:
        """Disable all components when the view times out."""
        for item in self.children:
            if isinstance(item, (discord.ui.Button, discord.ui.Select)):
                item.disabled = True
        if self.message:
            await safe_edit_message(self.message, view=self)

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        """Handle errors in view interactions gracefully."""
        logging.error("View interaction error: %s", error, exc_info=error)
        embed = error_embed("Interaction Error", "Something went wrong. Please try again.")
        await safe_respond(interaction, embed=embed, ephemeral=True)


# -------------------------- Availability UI --------------------------

# Day emoji mapping for better visual presentation
DAY_EMOJIS = {
    "monday": "1️⃣",
    "tuesday": "2️⃣",
    "wednesday": "3️⃣",
    "thursday": "4️⃣",
    "friday": "5️⃣",
    "saturday": "6️⃣",
    "sunday": "7️⃣",
}

# Day type icons for display
DAY_TYPE_ICONS = {
    "FREE": "🟢",
    "PREMIER": "🏆",
    "SCRIM": "⚔️",
    "VOD": "🎬",
    "MIXED": "🔀",
    "OFF": "⛔",
}


class AvailabilitySelect(discord.ui.Select):
    def __init__(self, cog: "AvailabilityCog", guild_id: Optional[int] = None) -> None:
        self.cog = cog
        self.guild_id = guild_id

        # Build options filtering out FREE and OFF days
        options = []
        selectable_days = []

        for day in WEEK_DAYS:
            day_type = "MIXED"  # Default if no guild
            if guild_id:
                day_type = cog.config_store.get_day_type(guild_id, day)

            # Skip FREE and OFF days
            if day_type in ("FREE", "OFF"):
                continue

            icon = DAY_TYPE_ICONS.get(day_type, "")
            options.append(discord.SelectOption(
                label=day.title(),
                value=day,
                emoji=DAY_EMOJIS.get(day, None),
                description=f"{icon} {day_type} day"
            ))
            selectable_days.append(day)

        # If all days are filtered out, show a placeholder option
        if not options:
            options.append(discord.SelectOption(
                label="No days available",
                value="none",
                description="All days are marked as FREE or OFF"
            ))

        super().__init__(
            placeholder="Select days you can play (multi-select)",
            min_values=1,
            max_values=max(1, len(options)),
            options=options,
        )
        self.selectable_days = selectable_days

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        # Handle "none" placeholder
        if "none" in self.values:
            await interaction.response.send_message(
                "No days are available for signup. All days are marked as FREE or OFF.",
                ephemeral=True
            )
            return

        # Filter to only valid selectable days
        valid_days = [d for d in self.values if d in self.selectable_days]
        if not valid_days:
            await interaction.response.send_message(
                "Please select at least one valid day.", ephemeral=True
            )
            return

        saved_days, team = await self.cog._save_availability(member, valid_days, None)
        pretty_days = ", ".join(day.title() for day in saved_days)

        embed = success_embed(
            "Availability Saved",
            f"**Player:** {member.display_name}\n"
            f"**Days:** {pretty_days}\n"
            f"**Team:** {team or 'Not assigned'}"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class AvailabilityClearButton(discord.ui.Button):
    def __init__(self, cog: "AvailabilityCog") -> None:
        super().__init__(style=discord.ButtonStyle.danger, label="Clear My Week", emoji="🗑️")
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        self.cog.availability_store.clear_user(member.id)
        embed = success_embed("Availability Cleared", "Your availability for this week has been reset.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class AvailabilityAllWeekButton(discord.ui.Button):
    """Quick button to mark available for all active days."""
    def __init__(self, cog: "AvailabilityCog") -> None:
        super().__init__(style=discord.ButtonStyle.success, label="Available All Active Days", emoji="✅")
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        member = interaction.user
        if not isinstance(member, discord.Member) or not interaction.guild:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        # Get only active days (not FREE or OFF)
        day_types = self.cog.config_store.get_all_day_types(interaction.guild.id)
        active_days = [d for d, t in day_types.items() if t not in ("FREE", "OFF")]

        if not active_days:
            await interaction.response.send_message(
                "No active days configured. Ask an admin to set day types.",
                ephemeral=True
            )
            return

        saved_days, team = await self.cog._save_availability(member, active_days, None)
        pretty_days = ", ".join(d.title() for d in saved_days)
        embed = success_embed(
            "Availability Saved",
            f"**Player:** {member.display_name}\n"
            f"**Days:** {pretty_days}\n"
            f"**Team:** {team or 'Not assigned'}"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class AvailabilityViewMineButton(discord.ui.Button):
    """Button to view current availability."""
    def __init__(self, cog: "AvailabilityCog") -> None:
        super().__init__(style=discord.ButtonStyle.secondary, label="View My Status", emoji="👤")
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        days = self.cog.availability_store.get_user_days(member.id)
        user_info = self.cog.availability_store.get_user_info(member.id)

        if not days:
            embed = format_embed("Your Status", "No availability saved yet. Use the dropdown above to sign up!")
        else:
            pretty_days = ", ".join(day.title() for day in days)
            roles = user_info.get("roles", [])
            agents = user_info.get("agents", [])
            tz = user_info.get("timezone", "Not set")

            embed = format_embed(
                f"Status: {member.display_name}",
                f"**Available:** {pretty_days}\n"
                f"**Team:** {user_info.get('team') or 'Not assigned'}\n"
                f"**Roles:** {', '.join(r.title() for r in roles) if roles else 'Not set'}\n"
                f"**Agents:** {', '.join(agents) if agents else 'Not set'}\n"
                f"**Timezone:** {tz}"
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class AvailabilityPanelView(PersistentView):
    def __init__(self, cog: "AvailabilityCog", guild_id: Optional[int] = None) -> None:
        super().__init__(timeout=3600.0)  # 1 hour timeout
        self.cog = cog
        self.guild_id = guild_id
        self.add_item(AvailabilitySelect(cog, guild_id))
        self.add_item(AvailabilityAllWeekButton(cog))
        self.add_item(AvailabilityViewMineButton(cog))
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
        # Options + min/max will be overridden by AgentSelectView.refresh_agent_options
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


class AgentSelectView(PersistentView):
    def __init__(self, cog: "AgentsCog") -> None:
        super().__init__(timeout=300.0)  # 5 minute timeout
        self.cog = cog
        self.selected_roles: List[str] = []
        self.role_select = AgentRoleSelect(cog)
        self.agent_select: Optional[AgentSelect] = None  # created on demand
        self.add_item(self.role_select)

    def refresh_agent_options(self) -> None:
        agents: List[str] = []
        for role in self.selected_roles:
            agents.extend(ROLE_AGENTS.get(role, []))
        unique_agents = sorted(set(agents))

        if self.agent_select is None:
            # First time: create the select and add to the view
            self.agent_select = AgentSelect(self.cog)
            self.add_item(self.agent_select)

        self.agent_select.options = [
            discord.SelectOption(label=name, value=name) for name in unique_agents
        ]

        # Make sure min/max are valid given the options
        if unique_agents:
            self.agent_select.min_values = 1
            self.agent_select.max_values = min(25, len(unique_agents))
            self.agent_select.disabled = False
        else:
            # Failsafe; should never really happen because each role has agents
            self.agent_select.min_values = 1
            self.agent_select.max_values = 1
            self.agent_select.disabled = True


# -------------------------- Cogs --------------------------


class ScrimSlotSelect(discord.ui.Select):
    """Select menu for scrim slot availability."""

    def __init__(self, cog: "AvailabilityCog", guild_id: int, day: str) -> None:
        self.cog = cog
        self.guild_id = guild_id
        self.day = day

        # Get scrim config
        num_slots = cog.config_store.get_scrims_per_day(guild_id)
        scrim_time = cog.config_store.get_scrim_time(guild_id, day)
        spacing = cog.config_store.get_scrim_spacing(guild_id)

        options = []
        if scrim_time:
            try:
                parts = scrim_time.split(":")
                base_hour = int(parts[0])
                base_minute = int(parts[1]) if len(parts) > 1 else 0

                for i in range(num_slots):
                    total_min = base_hour * 60 + base_minute + (i * spacing)
                    slot_time = f"{(total_min // 60) % 24:02d}:{total_min % 60:02d}"
                    options.append(discord.SelectOption(
                        label=f"Scrim #{i+1} @ {slot_time}",
                        value=str(i + 1),
                        description=f"Slot {i+1} starting at {slot_time}"
                    ))
            except (ValueError, IndexError):
                pass

        if not options:
            options.append(discord.SelectOption(
                label="No scrim slots configured",
                value="none",
                description="Ask an admin to configure scrim times"
            ))

        super().__init__(
            placeholder=f"Select scrim slots for {day.title()}",
            min_values=1,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        if "none" in self.values:
            await interaction.response.send_message(
                "No scrim slots are configured for this day.", ephemeral=True
            )
            return

        slots = [int(v) for v in self.values]
        self.cog.availability_store.set_scrim_slot_availability(
            member.id, member.display_name, self.day, slots
        )

        slot_names = [f"Scrim #{s}" for s in sorted(slots)]
        embed = success_embed(
            "Scrim Availability Saved",
            f"**Player:** {member.display_name}\n"
            f"**Day:** {self.day.title()}\n"
            f"**Slots:** {', '.join(slot_names)}"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class ScrimSlotView(PersistentView):
    """View for selecting scrim slot availability."""

    def __init__(self, cog: "AvailabilityCog", guild_id: int, day: str) -> None:
        super().__init__(timeout=300.0)
        self.add_item(ScrimSlotSelect(cog, guild_id, day))


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
    scrim = app_commands.Group(name="scrim", description="Manage scrim slot availability", parent=availability)
    vod = app_commands.Group(name="vod", description="Manage VOD review availability", parent=availability)

    async def _save_availability(
        self, member: discord.Member, days: List[str], team_override: Optional[str]
    ) -> Tuple[List[str], Optional[str]]:
        guild_id = member.guild.id if member.guild else None
        configured_roles = (
            self.config_store.get_team_roles(guild_id) if guild_id else {"A": None, "B": None}
        )
        normalized_team = infer_team(member, team_override, configured_roles, env_team_roles())
        normalized_days = sorted({normalize_day(day) or day.lower() for day in days if normalize_day(day)})

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

    @availability.command(name="day", description="See everyone available for one or more days")
    @app_commands.describe(days="Day or comma-separated days (e.g. friday, saturday)")
    async def availability_day(self, interaction: discord.Interaction, days: str) -> None:
        norm_days = parse_days(days)
        if not norm_days:
            await interaction.response.send_message("Please provide at least one valid day.", ephemeral=True)
            return

        chunks: List[str] = []
        for d in norm_days:
            users = self.availability_store.users_for_day(d)
            if not users:
                chunks.append(f"**{d.title()}** — no one has signed up.\n")
                continue

            lines = [f"{user['display_name']} (Team {user.get('team') or 'Not set'})" for user in users]
            chunks.append(f"**{d.title()}**\n" + "\n".join(lines) + "\n")

        embed = format_embed(
            title="Availability by Day",
            description="\n".join(chunks),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @availability.command(
        name="panel",
        description="Post a signup panel with a select menu + clear button for quick updates",
    )
    async def availability_panel(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not interaction.channel:
            await interaction.response.send_message("Run this in a server channel.", ephemeral=True)
            return

        # Defer first to avoid "Interaction failed" - channel.send may take time
        await safe_defer(interaction, ephemeral=True)

        # Get day types info for the description
        day_types = self.config_store.get_all_day_types(interaction.guild.id)
        active_days = [d for d, t in day_types.items() if t not in ("FREE", "OFF")]

        if active_days:
            days_info = f"Active days: {', '.join(d.title() for d in active_days)}"
        else:
            days_info = "No active days configured. Ask an admin to set day types."

        embed = format_embed(
            "Weekly Signup Panel",
            f"Pick your days below to save availability quickly.\n"
            f"Use the clear button to wipe your week and re-select.\n\n"
            f"_{days_info}_"
        )
        view = AvailabilityPanelView(self, interaction.guild.id)
        msg = await interaction.channel.send(embed=embed, view=view)
        view.message = msg  # Store reference for timeout cleanup
        await interaction.followup.send("Signup panel posted!", ephemeral=True)

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

    # -------- Scrim slot availability commands --------

    @scrim.command(name="set", description="Set your availability for scrim slots on a specific day")
    @app_commands.describe(day="Day to set scrim availability for")
    @app_commands.choices(
        day=[app_commands.Choice(name=d.title(), value=d) for d in WEEK_DAYS]
    )
    async def scrim_set(
        self, interaction: discord.Interaction, day: app_commands.Choice[str]
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        # Check if this day supports scrims
        day_type = self.config_store.get_day_type(interaction.guild.id, day.value)
        if day_type not in ("SCRIM", "MIXED"):
            embed = error_embed(
                "Not a Scrim Day",
                f"**{day.name}** is configured as **{day_type}** and doesn't support scrims.\n"
                "Ask an admin to change the day type to SCRIM or MIXED."
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        view = ScrimSlotView(self, interaction.guild.id, day.value)
        await interaction.response.send_message(
            f"Select which scrim slots you can play on **{day.name}**:",
            view=view,
            ephemeral=True,
        )

    @scrim.command(name="view", description="View your scrim slot availability")
    async def scrim_view(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        lines = []
        for day in WEEK_DAYS:
            slots = self.availability_store.get_scrim_slot_availability(member.id, day)
            if slots:
                slot_names = ", ".join(f"#{s}" for s in sorted(slots))
                lines.append(f"**{day.title()}:** {slot_names}")

        if not lines:
            embed = format_embed(
                "Your Scrim Availability",
                "No scrim slot availability set.\nUse `/availability scrim set` to sign up for specific scrim slots."
            )
        else:
            embed = format_embed(
                f"Scrim Availability: {member.display_name}",
                "\n".join(lines)
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @scrim.command(name="clear", description="Clear your scrim slot availability")
    async def scrim_clear(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        self.availability_store.clear_scrim_slots(member.id)
        embed = success_embed(
            "Scrim Availability Cleared",
            "All your scrim slot availability has been cleared."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # -------- VOD availability commands --------

    @vod.command(name="set", description="Set your availability for VOD review sessions")
    @app_commands.describe(days="Comma-separated days you can attend VOD review (e.g., sunday, monday)")
    async def vod_set(self, interaction: discord.Interaction, days: str) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        # Parse days
        norm_days = parse_days(days)
        if not norm_days:
            await interaction.response.send_message(
                "No valid days provided. Try `sunday, monday`.", ephemeral=True
            )
            return

        # Filter to only VOD or MIXED days
        valid_days = []
        invalid_days = []
        for day in norm_days:
            day_type = self.config_store.get_day_type(interaction.guild.id, day)
            if day_type in ("VOD", "MIXED"):
                valid_days.append(day)
            else:
                invalid_days.append(day)

        if not valid_days:
            embed = error_embed(
                "No Valid VOD Days",
                f"None of the selected days ({', '.join(d.title() for d in norm_days)}) are configured for VOD.\n"
                "Ask an admin to set day types to VOD or MIXED."
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        self.availability_store.set_vod_availability(
            member.id, member.display_name, valid_days
        )

        msg = f"**Player:** {member.display_name}\n**VOD Days:** {', '.join(d.title() for d in valid_days)}"
        if invalid_days:
            msg += f"\n\n_Skipped non-VOD days: {', '.join(d.title() for d in invalid_days)}_"

        embed = success_embed("VOD Availability Saved", msg)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @vod.command(name="view", description="View your VOD review availability")
    async def vod_view(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        days = self.availability_store.get_vod_availability(member.id)
        if not days:
            embed = format_embed(
                "Your VOD Availability",
                "No VOD availability set.\nUse `/availability vod set` to sign up for VOD review sessions."
            )
        else:
            embed = format_embed(
                f"VOD Availability: {member.display_name}",
                f"**Days:** {', '.join(d.title() for d in days)}"
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @vod.command(name="clear", description="Clear your VOD review availability")
    async def vod_clear(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        self.availability_store.clear_vod_availability(member.id)
        embed = success_embed(
            "VOD Availability Cleared",
            "Your VOD review availability has been cleared."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class DashboardRefreshButton(discord.ui.Button):
    """Button to refresh the dashboard."""

    def __init__(self, cog: "ScheduleCog") -> None:
        super().__init__(style=discord.ButtonStyle.primary, label="Refresh", emoji="🔄")
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return

        await safe_defer(interaction, ephemeral=True)
        await self.cog._refresh_dashboard(interaction.guild)
        await interaction.followup.send("Dashboard refreshed!", ephemeral=True)


class DashboardViewSelect(discord.ui.Select):
    """Select menu to change dashboard view mode."""

    def __init__(self, cog: "ScheduleCog", current_mode: str = "all") -> None:
        self.cog = cog
        options = [
            discord.SelectOption(label="All Days", value="all", emoji="📅", default=current_mode == "all"),
            discord.SelectOption(label="Premier Only", value="premier", emoji="🏆", default=current_mode == "premier"),
            discord.SelectOption(label="Scrims Only", value="scrim", emoji="⚔️", default=current_mode == "scrim"),
            discord.SelectOption(label="VOD Only", value="vod", emoji="🎬", default=current_mode == "vod"),
        ]
        super().__init__(placeholder="Change view", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return

        view_mode = self.values[0]
        await safe_defer(interaction, ephemeral=True)

        title, description = self.cog.schedule_builder.build_dashboard_embed(
            interaction.guild.id, interaction.guild.name, view_mode
        )
        embed = format_embed(title, description)
        embed.set_footer(text=f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M')} · View: {view_mode}")

        # Get dashboard message and update it
        dashboard = self.cog.config_store.get_dashboard(interaction.guild.id)
        if dashboard:
            channel = interaction.guild.get_channel(dashboard["channel_id"])
            if isinstance(channel, discord.TextChannel):
                try:
                    msg = await channel.fetch_message(dashboard["message_id"])
                    # Update view with new selection
                    new_view = DashboardView(self.cog, view_mode)
                    await msg.edit(embed=embed, view=new_view)
                except discord.NotFound:
                    pass

        await interaction.followup.send(f"Dashboard view changed to **{view_mode}**!", ephemeral=True)


class DashboardView(PersistentView):
    """View with dashboard controls."""

    def __init__(self, cog: "ScheduleCog", view_mode: str = "all") -> None:
        super().__init__(timeout=None)  # No timeout for dashboard
        self.cog = cog
        self.view_mode = view_mode
        self.add_item(DashboardRefreshButton(cog))
        self.add_item(DashboardViewSelect(cog, view_mode))


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
        self.dashboard_refresh_task.start()

    def cog_unload(self) -> None:
        self.dashboard_refresh_task.cancel()

    schedule = app_commands.Group(name="schedule", description="Build and post weekly schedules")
    dashboard = app_commands.Group(name="dashboard", description="Manage schedule dashboard", parent=schedule)

    @schedule.command(name="preview", description="Preview the current schedule")
    async def schedule_preview(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        # Defer for potentially long schedule computation
        await safe_defer(interaction, ephemeral=False)

        summaries = self.schedule_builder.build_week(interaction.guild.id)
        text = self.schedule_builder.format_schedule(interaction.guild.name, summaries)
        embed = format_embed("Valorant Weekly Schedule", text)
        await interaction.followup.send(embed=embed)

    @schedule.command(name="post", description="Post the schedule to the announcement channel")
    async def schedule_post(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

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

        # Defer first to avoid "Interaction failed" - channel.send may take time
        await interaction.response.defer(ephemeral=True)

        summaries = self.schedule_builder.build_week(interaction.guild.id)
        text = self.schedule_builder.format_schedule(interaction.guild.name, summaries)
        embed = format_embed("Valorant Weekly Schedule", text)

        mention = self._resolve_ping_mention(interaction.guild)
        content = f"{mention} Weekly schedule updated!" if mention else "Weekly schedule updated!"
        await channel.send(content=content, embed=embed)
        await interaction.followup.send("Schedule posted!", ephemeral=True)

    @schedule.command(
        name="pingcheck",
        description="Check if current numbers would trigger scrim/practice pings for one or more days",
    )
    @app_commands.describe(days="Day or comma-separated days (e.g. friday, saturday)")
    async def schedule_pingcheck(self, interaction: discord.Interaction, days: str) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        norm_days = parse_days(days)
        if not norm_days:
            await interaction.response.send_message("Invalid days. Try `monday, friday` etc.", ephemeral=True)
            return

        guild_id = interaction.guild.id
        blocks: List[str] = []

        for norm_day in norm_days:
            users = self.availability_store.users_for_day(norm_day)
            total = len(users)
            team_counts: Dict[str, int] = {"A": 0, "B": 0}
            for info in users:
                t = str(info.get("team") or "").upper()
                if t in team_counts:
                    team_counts[t] += 1

            scrim_time = self.config_store.get_scrim_time(guild_id, norm_day)
            premier_window = self.config_store.get_premier_window(guild_id, norm_day)
            practice_time = self.config_store.get_practice_time(guild_id, norm_day)

            by_team = any(c >= 5 for c in team_counts.values())
            by_total = total >= 10
            scrim_will_ping = scrim_time is not None and (by_team or by_total)
            practice_will_ping = practice_time is not None and total >= 5

            lines = [
                f"**{norm_day.title()}**",
                f"- Total available: **{total}**",
                f"- Team A: **{team_counts['A']}** · Team B: **{team_counts['B']}**",
                f"- Scrim time: `{scrim_time}`" if scrim_time else "- Scrim time: **OFF**",
                f"- Practice time: `{practice_time}`" if practice_time else "- Practice time: **OFF**",
                f"- Premier window: `{premier_window}`" if premier_window else "- Premier: **OFF**",
                f"- Scrim ping now? {'✅ Yes' if scrim_will_ping else '❌ No'}",
                f"- Practice ping now? {'✅ Yes' if practice_will_ping else '❌ No'}",
                "",
            ]
            blocks.extend(lines)

        blocks.extend([
            "Scrim ping (30 minutes before) triggers if:",
            "- At least **10 total** players are available, **or**",
            "- At least **5 players** from **one team** are available.",
            "",
            "Practice ping (30 minutes before) triggers if:",
            "- At least **5 total** players are available.",
        ])

        embed = format_embed("Scrim / Practice Ping Check", "\n".join(blocks))
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

    # -------- Dashboard commands --------

    @dashboard.command(name="post", description="Post a tracked schedule dashboard that auto-updates")
    async def dashboard_post(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not interaction.channel:
            await interaction.response.send_message("Run this in a server channel.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not (
            member.guild_permissions.manage_guild or member.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to post a dashboard.", ephemeral=True
            )
            return

        await safe_defer(interaction, ephemeral=True)

        # Build and post dashboard
        title, description = self.schedule_builder.build_dashboard_embed(
            interaction.guild.id, interaction.guild.name, "all"
        )
        embed = format_embed(title, description)
        embed.set_footer(text=f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M')} · View: all")

        view = DashboardView(self, "all")

        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.followup.send("Dashboard can only be posted in text channels.", ephemeral=True)
            return

        msg = await interaction.channel.send(embed=embed, view=view)

        # Save dashboard tracking
        self.config_store.set_dashboard(interaction.guild.id, interaction.channel.id, msg.id)

        await interaction.followup.send(
            f"Dashboard posted! It will auto-update when availability changes.\n"
            f"Use the buttons on the dashboard to refresh or change views.",
            ephemeral=True
        )

    @dashboard.command(name="refresh", description="Force refresh the tracked dashboard")
    async def dashboard_refresh(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        await safe_defer(interaction, ephemeral=True)

        success = await self._refresh_dashboard(interaction.guild)
        if success:
            await interaction.followup.send("Dashboard refreshed!", ephemeral=True)
        else:
            await interaction.followup.send(
                "Could not find the dashboard. Use `/schedule dashboard post` to create a new one.",
                ephemeral=True
            )

    @dashboard.command(name="clear", description="Remove dashboard tracking")
    async def dashboard_clear(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not (
            member.guild_permissions.manage_guild or member.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to clear the dashboard.", ephemeral=True
            )
            return

        self.config_store.clear_dashboard(interaction.guild.id)
        embed = success_embed(
            "Dashboard Cleared",
            "Dashboard tracking has been removed.\n"
            "The message will remain but won't auto-update anymore."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _refresh_dashboard(self, guild: discord.Guild, view_mode: str = "all") -> bool:
        """Refresh the dashboard message for a guild. Returns True if successful."""
        dashboard = self.config_store.get_dashboard(guild.id)
        if not dashboard:
            return False

        channel = guild.get_channel(dashboard["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return False

        try:
            msg = await channel.fetch_message(dashboard["message_id"])
        except discord.NotFound:
            # Message deleted, clear tracking
            self.config_store.clear_dashboard(guild.id)
            return False
        except discord.HTTPException:
            return False

        title, description = self.schedule_builder.build_dashboard_embed(guild.id, guild.name, view_mode)
        embed = format_embed(title, description)
        embed.set_footer(text=f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M')} · View: {view_mode}")

        view = DashboardView(self, view_mode)
        return await safe_edit_message(msg, embed=embed, view=view)

    @tasks.loop(minutes=15)
    async def dashboard_refresh_task(self) -> None:
        """Periodically refresh all dashboards."""
        for guild in self.bot.guilds:
            dashboard = self.config_store.get_dashboard(guild.id)
            if dashboard:
                await self._refresh_dashboard(guild)
                await asyncio.sleep(1)  # Rate limit protection

    @dashboard_refresh_task.before_loop
    async def before_dashboard_refresh(self) -> None:
        await self.bot.wait_until_ready()


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
    @app_commands.describe(role="Role to mention for availability updates and scrim/practice pings")
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

    # ----- Time / window configuration (supports multiple days) -----

    @config.command(
        name="scrimtime",
        description="Set or clear the scrim start time for one or more days (HH:MM in 24h, or 'off')",
    )
    @app_commands.describe(
        days="Day or comma-separated days (e.g. monday, wednesday, friday)",
        time_label="24h time like '19:00' for 7 PM, or 'off' to disable scrims those days",
    )
    async def config_scrim_time(
        self,
        interaction: discord.Interaction,
        days: str,
        time_label: str,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        norm_days = parse_days(days)
        if not norm_days:
            await interaction.response.send_message(
                "No valid days provided. Try `monday, wednesday, friday`.", ephemeral=True
            )
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
            for d in norm_days:
                self.config_store.set_scrim_time(interaction.guild.id, d, None)
            pretty_days = ", ".join(d.title() for d in norm_days)
            await interaction.response.send_message(
                f"Scrims for **{pretty_days}** turned **OFF**.", ephemeral=True
            )
            return

        t = _parse_hhmm_to_time(time_label)
        if t is None:
            await interaction.response.send_message(
                "Invalid time format. Use 24h `HH:MM`, e.g. `19:00` for 7 PM.", ephemeral=True
            )
            return

        for d in norm_days:
            self.config_store.set_scrim_time(interaction.guild.id, d, time_label)
        pretty_days = ", ".join(d.title() for d in norm_days)
        await interaction.response.send_message(
            f"Scrim time for **{pretty_days}** set to `{time_label}`.", ephemeral=True
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
        description="Set or clear the Premier window for one or more days (e.g. '19:00-20:00' or 'off')",
    )
    @app_commands.describe(
        days="Day or comma-separated days (e.g. wednesday, thursday)",
        window="Time window like '19:00-20:00', or 'off' to disable premier those days",
    )
    async def config_premier_window(
        self,
        interaction: discord.Interaction,
        days: str,
        window: str,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        norm_days = parse_days(days)
        if not norm_days:
            await interaction.response.send_message(
                "No valid days provided. Try `wednesday, thursday`.", ephemeral=True
            )
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
            for d in norm_days:
                self.config_store.set_premier_window(interaction.guild.id, d, None)
            pretty_days = ", ".join(d.title() for d in norm_days)
            await interaction.response.send_message(
                f"Premier window for **{pretty_days}** turned **OFF**.", ephemeral=True
            )
            return

        parts = window.split("-")
        if len(parts) != 2 or _parse_hhmm_to_time(parts[0]) is None or _parse_hhmm_to_time(parts[1]) is None:
            await interaction.response.send_message(
                "Invalid window format. Use `HH:MM-HH:MM`, e.g. `19:00-20:00`.", ephemeral=True
            )
            return

        for d in norm_days:
            self.config_store.set_premier_window(interaction.guild.id, d, window)
        pretty_days = ", ".join(d.title() for d in norm_days)
        await interaction.response.send_message(
            f"Premier window for **{pretty_days}** set to `{window}`.", ephemeral=True
        )

    @config.command(
        name="practicetime",
        description="Set or clear the practice time for one or more days (HH:MM in 24h, or 'off')",
    )
    @app_commands.describe(
        days="Day or comma-separated days (e.g. monday, wednesday, friday)",
        time_label="24h time like '18:00', or 'off' to disable practice those days",
    )
    async def config_practice_time(
        self,
        interaction: discord.Interaction,
        days: str,
        time_label: str,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        norm_days = parse_days(days)
        if not norm_days:
            await interaction.response.send_message(
                "No valid days provided. Try `monday, wednesday, friday`.", ephemeral=True
            )
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not (
            member.guild_permissions.manage_guild or member.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to change practice times.", ephemeral=True
            )
            return

        if time_label.lower() in {"off", "none", "disable"}:
            for d in norm_days:
                self.config_store.set_practice_time(interaction.guild.id, d, None)
            pretty_days = ", ".join(d.title() for d in norm_days)
            await interaction.response.send_message(
                f"Practice for **{pretty_days}** turned **OFF**.", ephemeral=True
            )
            return

        t = _parse_hhmm_to_time(time_label)
        if t is None:
            await interaction.response.send_message(
                "Invalid time format. Use 24h `HH:MM`, e.g. `18:00`.", ephemeral=True
            )
            return

        for d in norm_days:
            self.config_store.set_practice_time(interaction.guild.id, d, time_label)
        pretty_days = ", ".join(d.title() for d in norm_days)
        await interaction.response.send_message(
            f"Practice time for **{pretty_days}** set to `{time_label}`.", ephemeral=True
        )

    @config.command(name="check_practice", description="Show current practice times for all days")
    async def config_check_practice(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        lines: List[str] = []
        for d in WEEK_DAYS:
            t = self.config_store.get_practice_time(interaction.guild.id, d)
            label = t if t is not None else "OFF"
            lines.append(f"**{d.title()}**: `{label}`")
        embed = format_embed("Current Practice Times", "\n".join(lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @config.command(name="reset_practice", description="Reset all practice times to defaults (OFF)")
    async def config_reset_practice(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not (
            member.guild_permissions.manage_guild or member.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to reset practice times.", ephemeral=True
            )
            return

        self.config_store.reset_practice_times(interaction.guild.id)
        await interaction.response.send_message(
            "All practice times reset to default (OFF).", ephemeral=True
        )

    @config.command(name="reset_schedule", description="Reset scrim, premier, and practice times to defaults")
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
            "Entire schedule (scrim, premier, practice) reset to defaults.", ephemeral=True
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

    # ----- Map configuration (now supports multiple days per command) -----

    @config.command(
        name="map_premier",
        description="Set the Premier map for one or more days",
    )
    @app_commands.describe(
        days="Day or comma-separated days (e.g. wednesday, thursday)",
        map_name="Map to play for Premier on those days",
    )
    @app_commands.choices(
        map_name=[app_commands.Choice(name=m, value=m) for m in VALORANT_MAPS]
    )
    async def config_map_premier(
        self,
        interaction: discord.Interaction,
        days: str,
        map_name: app_commands.Choice[str],
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        norm_days = parse_days(days)
        if not norm_days:
            await interaction.response.send_message("Invalid day(s). Try `wednesday, thursday`.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not (
            member.guild_permissions.manage_guild or member.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to set maps.", ephemeral=True
            )
            return

        for d in norm_days:
            self.config_store.set_premier_map(interaction.guild.id, d, map_name.value)

        pretty_days = ", ".join(d.title() for d in norm_days)
        await interaction.response.send_message(
            f"Premier map for **{pretty_days}** set to **{map_name.value}**.",
            ephemeral=True,
        )

    @config.command(
        name="map_scrim",
        description="Set the Scrim map for one or more days",
    )
    @app_commands.describe(
        days="Day or comma-separated days (e.g. wednesday, friday)",
        map_name="Map to scrim on those days",
    )
    @app_commands.choices(
        map_name=[app_commands.Choice(name=m, value=m) for m in VALORANT_MAPS]
    )
    async def config_map_scrim(
        self,
        interaction: discord.Interaction,
        days: str,
        map_name: app_commands.Choice[str],
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        norm_days = parse_days(days)
        if not norm_days:
            await interaction.response.send_message("Invalid day(s). Try `wednesday, friday`.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not (
            member.guild_permissions.manage_guild or member.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to set maps.", ephemeral=True
            )
            return

        for d in norm_days:
            self.config_store.set_scrim_map(interaction.guild.id, d, map_name.value)

        pretty_days = ", ".join(d.title() for d in norm_days)
        await interaction.response.send_message(
            f"Scrim map for **{pretty_days}** set to **{map_name.value}**.",
            ephemeral=True,
        )

    @config.command(
        name="map_practice",
        description="Set the Practice map for one or more days",
    )
    @app_commands.describe(
        days="Day or comma-separated days (e.g. tuesday, thursday)",
        map_name="Map to practice on those days",
    )
    @app_commands.choices(
        map_name=[app_commands.Choice(name=m, value=m) for m in VALORANT_MAPS]
    )
    async def config_map_practice(
        self,
        interaction: discord.Interaction,
        days: str,
        map_name: app_commands.Choice[str],
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        norm_days = parse_days(days)
        if not norm_days:
            await interaction.response.send_message("Invalid day(s). Try `tuesday, thursday`.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not (
            member.guild_permissions.manage_guild or member.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to set maps.", ephemeral=True
            )
            return

        for d in norm_days:
            self.config_store.set_practice_map(interaction.guild.id, d, map_name.value)

        pretty_days = ", ".join(d.title() for d in norm_days)
        await interaction.response.send_message(
            f"Practice map for **{pretty_days}** set to **{map_name.value}**.",
            ephemeral=True,
        )

    @config.command(name="check_maps", description="Show current Premier/Scrim/Practice maps for all days")
    async def config_check_maps(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        lines: List[str] = []
        for d in WEEK_DAYS:
            p = self.config_store.get_premier_map(interaction.guild.id, d)
            s = self.config_store.get_scrim_map(interaction.guild.id, d)
            pr = self.config_store.get_practice_map(interaction.guild.id, d)
            p_label = p if p is not None else "—"
            s_label = s if s is not None else "—"
            pr_label = pr if pr is not None else "—"
            lines.append(
                f"**{d.title()}** · Premier: `{p_label}` · Scrim: `{s_label}` · Practice: `{pr_label}`"
            )

        embed = format_embed("Current Maps", "\n".join(lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ----- Scrim slots configuration -----

    @config.command(name="scrim_slots", description="Configure scrim slots per day")
    @app_commands.describe(
        slots_per_day="Number of scrim slots per day (1-5)",
        spacing_minutes="Minutes between scrim slots (30-180)"
    )
    async def config_scrim_slots(
        self,
        interaction: discord.Interaction,
        slots_per_day: int,
        spacing_minutes: int = 90,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not (
            member.guild_permissions.manage_guild or member.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to configure scrim slots.", ephemeral=True
            )
            return

        # Validate
        if slots_per_day < 1 or slots_per_day > 5:
            await interaction.response.send_message(
                "Slots per day must be between 1 and 5.", ephemeral=True
            )
            return

        if spacing_minutes < 30 or spacing_minutes > 180:
            await interaction.response.send_message(
                "Spacing must be between 30 and 180 minutes.", ephemeral=True
            )
            return

        self.config_store.set_scrims_per_day(interaction.guild.id, slots_per_day)
        self.config_store.set_scrim_spacing(interaction.guild.id, spacing_minutes)

        embed = success_embed(
            "Scrim Slots Configured",
            f"**Slots per day:** {slots_per_day}\n"
            f"**Spacing:** {spacing_minutes} minutes between slots\n\n"
            f"_Example: If scrim time is 19:00 with {slots_per_day} slots and {spacing_minutes}min spacing:_\n"
            + "\n".join(
                f"• Scrim #{i+1}: `{19 + (i * spacing_minutes) // 60:02d}:{(i * spacing_minutes) % 60:02d}`"
                for i in range(slots_per_day)
            )
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @config.command(name="check_scrim_slots", description="Show current scrim slot configuration")
    async def config_check_scrim_slots(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        slots = self.config_store.get_scrims_per_day(interaction.guild.id)
        spacing = self.config_store.get_scrim_spacing(interaction.guild.id)

        embed = format_embed(
            "Scrim Slot Configuration",
            f"**Slots per day:** {slots}\n"
            f"**Spacing:** {spacing} minutes\n\n"
            "_Scrim slots are generated for days with SCRIM or MIXED day type._"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ----- VOD configuration -----

    @config.command(name="vod", description="Configure VOD review sessions")
    @app_commands.describe(
        start_time="VOD session start time (HH:MM in 24h, e.g., 19:00)",
        duration_minutes="Session duration in minutes (30-180)"
    )
    async def config_vod(
        self,
        interaction: discord.Interaction,
        start_time: str,
        duration_minutes: int = 60,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not (
            member.guild_permissions.manage_guild or member.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to configure VOD sessions.", ephemeral=True
            )
            return

        # Validate time
        t = _parse_hhmm_to_time(start_time)
        if t is None:
            await interaction.response.send_message(
                "Invalid time format. Use 24h `HH:MM`, e.g. `19:00`.", ephemeral=True
            )
            return

        if duration_minutes < 30 or duration_minutes > 180:
            await interaction.response.send_message(
                "Duration must be between 30 and 180 minutes.", ephemeral=True
            )
            return

        self.config_store.set_vod_start_time(interaction.guild.id, start_time)
        self.config_store.set_vod_session_minutes(interaction.guild.id, duration_minutes)

        embed = success_embed(
            "VOD Configuration Updated",
            f"**Start time:** `{start_time}`\n"
            f"**Duration:** {duration_minutes} minutes\n\n"
            "_VOD sessions are shown for days with VOD or MIXED day type._"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @config.command(name="check_vod", description="Show current VOD configuration")
    async def config_check_vod(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        start_time = self.config_store.get_vod_start_time(interaction.guild.id)
        duration = self.config_store.get_vod_session_minutes(interaction.guild.id)

        embed = format_embed(
            "VOD Configuration",
            f"**Start time:** `{start_time}`\n"
            f"**Duration:** {duration} minutes\n\n"
            "_VOD sessions are shown for days with VOD or MIXED day type._"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ----- Guild timezone -----

    @config.command(name="timezone", description="Set the guild's timezone for schedule display")
    @app_commands.describe(timezone="Timezone for the guild")
    @app_commands.choices(
        timezone=[
            app_commands.Choice(name=label, value=tz) for tz, label in COMMON_TIMEZONES
        ]
    )
    async def config_timezone(
        self,
        interaction: discord.Interaction,
        timezone: app_commands.Choice[str],
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not (
            member.guild_permissions.manage_guild or member.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to set the guild timezone.", ephemeral=True
            )
            return

        self.config_store.set_guild_timezone(interaction.guild.id, timezone.value)
        embed = success_embed(
            "Timezone Updated",
            f"Guild timezone set to **{timezone.name}** (`{timezone.value}`).\n"
            "All schedule times will be displayed in this timezone."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @config.command(name="captain_role", description="Set the captain role for lineup management permissions")
    @app_commands.describe(role="Role that can lock lineups and manage schedules")
    async def config_captain_role(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not (
            member.guild_permissions.manage_guild or member.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to set the captain role.", ephemeral=True
            )
            return

        self.config_store.set_captain_role(interaction.guild.id, role.id)
        embed = success_embed(
            "Captain Role Set",
            f"Captain role set to {role.mention}.\n"
            f"Members with this role can lock lineups and manage schedules."
        )
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
        bucket: Dict[str, List[str]] = {}

        configured_roles = self.config_store.get_team_roles(interaction.guild.id)
        env_roles_tuple = env_team_roles()

        for user_id, info in users.items():
            member = interaction.guild.get_member(int(user_id))
            if member is None:
                continue

            agents = info.get("agents") or []
            if not agents:
                continue

            if target == "ALL":
                # No team filter, just show everyone with agents
                name = str(info.get("display_name", member.display_name))
                bucket[name] = list(agents)
                continue

            stored_team = str(info.get("team") or "") or None
            effective_team = infer_team(member, stored_team, configured_roles, env_roles_tuple)
            if effective_team != target:
                continue

            name = str(info.get("display_name", member.display_name))
            bucket[name] = list(agents)

        if not bucket:
            label = "all teams" if target == "ALL" else f"Team {target}"
            await interaction.response.send_message(
                f"No saved agents found for {label}.", ephemeral=True
            )
            return

        lines: List[str] = []
        for name, agents_list in bucket.items():
            lines.append(f"**{name}**: {', '.join(agents_list)}")

        label = "all teams" if target == "ALL" else f"Team {target}"
        embed = format_embed(f"Agents for {label}", "\n".join(lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)


class RoleSyncCog(commands.Cog):
    """Keeps the 'available' role in sync with today's availability
    and handles weekly reset and enhanced reminder system.
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
        self.reminder_task.start()

    def cog_unload(self) -> None:
        self.role_sync_task.cancel()
        self.reminder_task.cancel()

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

        should_reset = (
            now.weekday() == self._target_weekday_index
            and now.hour >= AUTO_RESET_HOUR_ENV
            and (self._last_reset_date is None or self._last_reset_date != now.date())
        )
        if should_reset:
            cleared = self.availability_store.reset_all()
            self._last_reset_date = now.date()
            logging.info("Auto-reset availability for new week; cleared %d users", cleared)

        for guild in self.bot.guilds:
            await self._sync_roles_for_guild(guild)

    @role_sync_task.before_loop
    async def before_role_sync(self) -> None:
        await self.bot.wait_until_ready()

    # ---- Enhanced reminder system ----

    def _get_reminder_mention(self, guild: discord.Guild) -> str:
        """Get the appropriate mention string based on ping style."""
        ping_style = self.config_store.get_reminder_ping_style(guild.id)

        if ping_style == "none":
            return ""
        elif ping_style == "here":
            return "@here "
        else:  # "role"
            role_id = self._resolve_ping_role_id(guild)
            if role_id:
                role = guild.get_role(role_id)
                if role:
                    return f"{role.mention} "
        return ""

    async def _send_reminder(
        self,
        guild: discord.Guild,
        reminder_type: str,
        day: str,
        time_str: str,
        hours_before: int,
        extra_info: str = "",
    ) -> bool:
        """Send a reminder if not already sent. Returns True if sent."""
        # Check if reminders are enabled
        if not self.config_store.get_reminders_enabled(guild.id):
            return False

        # Build reminder key for dedup
        today = datetime.now().strftime("%Y-%m-%d")
        reminder_key = f"{today}_{reminder_type}_{day}_{hours_before}h"

        # Check if already sent
        if self.config_store.is_reminder_sent(guild.id, reminder_key):
            return False

        # Get channel
        channel_id = self.config_store.get_reminder_channel(guild.id)
        if not channel_id:
            return False

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return False

        mention = self._get_reminder_mention(guild)

        # Build message based on type
        type_icons = {"premier": "🏆", "scrim": "⚔️", "vod": "🎬", "practice": "🎯"}
        icon = type_icons.get(reminder_type, "📅")

        if hours_before >= 24:
            time_desc = f"{hours_before // 24} day(s)"
        else:
            time_desc = f"{hours_before} hour(s)"

        message = (
            f"{mention}{icon} **{reminder_type.title()} Reminder**\n"
            f"**{day.title()}** at `{time_str}` — {time_desc} away!\n"
            f"{extra_info}"
        )

        try:
            await channel.send(message)
            self.config_store.mark_reminder_sent(guild.id, reminder_key)
            logging.info("Sent %s reminder for %s in %s", reminder_type, day, guild.name)
            return True
        except discord.HTTPException as e:
            logging.warning("Failed to send %s reminder in %s: %s", reminder_type, guild.name, e)
            return False

    async def _check_reminders_for_guild(self, guild: discord.Guild) -> None:
        """Check and send any due reminders for a guild."""
        now = datetime.now()
        today_idx = now.weekday()
        today_label = WEEK_DAYS[today_idx]

        # Get lead times
        lead_times = self.config_store.get_reminder_lead_times(guild.id)

        for hours_before in lead_times:
            # Calculate target time
            target_time = now + timedelta(hours=hours_before)
            target_day_idx = target_time.weekday()
            target_day = WEEK_DAYS[target_day_idx]

            # Get day type
            day_type = self.config_store.get_day_type(guild.id, target_day)

            # Check Premier
            if day_type in ("PREMIER", "MIXED"):
                premier_window = self.config_store.get_premier_window(guild.id, target_day)
                if premier_window:
                    premier_time = premier_window.split("-")[0] if "-" in premier_window else premier_window
                    # Check if time matches
                    if self._time_matches(target_time, premier_time, hours_before):
                        users = self.availability_store.users_for_day(target_day)
                        extra = f"**{len(users)}** players signed up"
                        await self._send_reminder(guild, "premier", target_day, premier_window, hours_before, extra)

            # Check Scrim
            if day_type in ("SCRIM", "MIXED"):
                scrim_time = self.config_store.get_scrim_time(guild.id, target_day)
                if scrim_time:
                    if self._time_matches(target_time, scrim_time, hours_before):
                        users = self.availability_store.users_for_day(target_day)
                        extra = f"**{len(users)}** players signed up"
                        await self._send_reminder(guild, "scrim", target_day, scrim_time, hours_before, extra)

            # Check VOD
            if day_type in ("VOD", "MIXED"):
                vod_time = self.config_store.get_vod_start_time(guild.id)
                if vod_time:
                    if self._time_matches(target_time, vod_time, hours_before):
                        vod_users = self.availability_store.users_for_vod_day(target_day)
                        extra = f"**{len(vod_users)}** players signed up for VOD"
                        await self._send_reminder(guild, "vod", target_day, vod_time, hours_before, extra)

    def _time_matches(self, target_time: datetime, time_str: str, hours_before: int) -> bool:
        """Check if target_time approximately matches time_str with given lead time."""
        try:
            parts = time_str.split(":")
            event_hour = int(parts[0])
            event_minute = int(parts[1]) if len(parts) > 1 else 0

            event_dt = target_time.replace(hour=event_hour, minute=event_minute, second=0, microsecond=0)

            # Check if we're within 10 minutes of when the reminder should fire
            now = datetime.now()
            reminder_fire_time = event_dt - timedelta(hours=hours_before)

            delta = abs((now - reminder_fire_time).total_seconds())
            return delta < 600  # Within 10 minutes
        except (ValueError, IndexError):
            return False

    @tasks.loop(minutes=10)
    async def reminder_task(self) -> None:
        """Check and send reminders for all guilds."""
        for guild in self.bot.guilds:
            try:
                await self._check_reminders_for_guild(guild)
            except Exception as e:
                logging.error("Error checking reminders for %s: %s", guild.name, e)
            await asyncio.sleep(1)  # Rate limit protection

        # Clean up old reminder records (older than 7 days)
        cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        for guild in self.bot.guilds:
            self.config_store.clear_old_reminders(guild.id, cutoff)

    @reminder_task.before_loop
    async def before_reminder_task(self) -> None:
        await self.bot.wait_until_ready()


class GameLogCog(commands.Cog):
    """Log and review scrims/premier/practice days."""

    def __init__(self, bot: commands.Bot, log_store: GameLogStore) -> None:
        self.bot = bot
        self.log_store = log_store

    log = app_commands.Group(name="log", description="Log scrims, premier, and practice matches")
    check = app_commands.Group(name="check", description="Check logged matches")

    # ---- Logging ----

    @log.command(name="day", description="Log a scrim/premier/practice match for a specific date")
    @app_commands.describe(
        date_str="Date in YYYY-MM-DD (e.g. 2025-11-20)",
        match_type="Type of match",
        time_label="Start time in 24h HH:MM (server time)",
        agents="Agents played (comma-separated)",
        result="Result and/or score (e.g. 'W 13-11', 'L 11-13')",
        vod_url="Optional VOD / video link",
        comments="Optional comments/notes",
    )
    @app_commands.choices(
        match_type=[
            app_commands.Choice(name="Premier", value="premier"),
            app_commands.Choice(name="Scrim", value="scrim"),
            app_commands.Choice(name="Practice", value="practice"),
        ]
    )
    async def log_day(
        self,
        interaction: discord.Interaction,
        date_str: str,
        match_type: app_commands.Choice[str],
        time_label: str,
        agents: str,
        result: str,
        vod_url: Optional[str] = None,
        comments: Optional[str] = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        # Validate date
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            await interaction.response.send_message(
                "Invalid date format. Use `YYYY-MM-DD`, e.g. `2025-11-20`.", ephemeral=True
            )
            return

        # Validate time
        if _parse_hhmm_to_time(time_label) is None:
            await interaction.response.send_message(
                "Invalid time format. Use 24h `HH:MM`, e.g. `19:00`.", ephemeral=True
            )
            return

        agents_list = [a.strip() for a in agents.split(",") if a.strip()]
        entry = {
            "date": date_str,
            "type": match_type.value,
            "time": time_label,
            "agents": agents_list,
            "result": result,
            "vod_url": vod_url,
            "comments": comments,
            "logged_by_id": interaction.user.id,
            "logged_by_name": getattr(interaction.user, "display_name", str(interaction.user)),
            "created_at": datetime.utcnow().isoformat(timespec="seconds"),
        }

        log_id = self.log_store.add_log(interaction.guild.id, entry)

        lines = [
            f"**Log ID:** {log_id}",
            f"**Date:** {date_str}",
            f"**Type:** {match_type.name}",
            f"**Time:** `{time_label}`",
            f"**Agents:** {', '.join(agents_list) if agents_list else '—'}",
            f"**Result:** {result}",
        ]
        if vod_url:
            lines.append(f"**VOD:** {vod_url}")
        if comments:
            lines.append(f"**Comments:** {comments}")

        embed = format_embed("Match logged ✅", "\n".join(lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @log.command(name="clear_date", description="Admin: clear all logs for a specific date")
    @app_commands.describe(date_str="Date in YYYY-MM-DD (e.g. 2025-11-20)")
    async def log_clear_date(self, interaction: discord.Interaction, date_str: str) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not (
            member.guild_permissions.manage_guild or member.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to clear logs.", ephemeral=True
            )
            return

        removed = self.log_store.clear_logs_for_date(interaction.guild.id, date_str)
        await interaction.response.send_message(
            f"Removed **{removed}** logs for `{date_str}`.", ephemeral=True
        )

    @log.command(name="clear_all", description="Admin: clear ALL logs for this server")
    async def log_clear_all(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not (
            member.guild_permissions.manage_guild or member.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to clear logs.", ephemeral=True
            )
            return

        removed = self.log_store.clear_all_logs(interaction.guild.id)
        await interaction.response.send_message(
            f"Removed **{removed}** logged matches for this server.", ephemeral=True
        )

    # ---- Checking ----

    @check.command(name="day", description="Show all logged matches for a specific date")
    @app_commands.describe(date_str="Date in YYYY-MM-DD (e.g. 2025-11-20)")
    async def check_day(self, interaction: discord.Interaction, date_str: str) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        logs = self.log_store.logs_for_date(interaction.guild.id, date_str)
        if not logs:
            await interaction.response.send_message(
                f"No matches logged for `{date_str}`.", ephemeral=True
            )
            return

        lines: List[str] = []
        for log in logs:
            lid = log.get("id")
            mtype = str(log.get("type", "unknown")).title()
            time_label = log.get("time", "—")
            agents_list = ", ".join(log.get("agents", [])) or "—"
            result = log.get("result", "—")
            vod = log.get("vod_url")
            comments = log.get("comments")

            lines.append(f"**#{lid}** — {mtype} at `{time_label}` · Result: **{result}**")
            lines.append(f"• Agents: {agents_list}")
            if vod:
                lines.append(f"• VOD: {vod}")
            if comments:
                lines.append(f"• Notes: {comments}")
            lines.append("")  # blank line between logs

        description = "\n".join(lines[:600])  # basic safety against over-long output
        embed = format_embed(f"Logged matches for {date_str}", description)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @check.command(name="recent", description="Show the most recent logged matches")
    @app_commands.describe(limit="How many recent matches to show (default 5, max 20)")
    async def check_recent(self, interaction: discord.Interaction, limit: Optional[int] = 5) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        if limit is None:
            limit = 5
        limit = max(1, min(20, limit))

        logs = self.log_store.recent_logs(interaction.guild.id, limit=limit)
        if not logs:
            await interaction.response.send_message("No matches logged yet.", ephemeral=True)
            return

        lines: List[str] = []
        for log in logs:
            lid = log.get("id")
            date_str = log.get("date", "—")
            mtype = str(log.get("type", "unknown")).title()
            time_label = log.get("time", "—")
            result = log.get("result", "—")
            lines.append(f"**#{lid}** — {date_str} · {mtype} at `{time_label}` · Result: **{result}**")

        embed = format_embed("Recent logged matches", "\n".join(lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)


# Common timezones for the dropdown
COMMON_TIMEZONES = [
    ("US/Eastern", "Eastern Time (ET)"),
    ("US/Central", "Central Time (CT)"),
    ("US/Mountain", "Mountain Time (MT)"),
    ("US/Pacific", "Pacific Time (PT)"),
    ("Europe/London", "London (GMT/BST)"),
    ("Europe/Paris", "Central European (CET)"),
    ("Europe/Berlin", "Berlin (CET)"),
    ("Asia/Tokyo", "Tokyo (JST)"),
    ("Asia/Seoul", "Seoul (KST)"),
    ("Australia/Sydney", "Sydney (AEST)"),
    ("America/Sao_Paulo", "Sao Paulo (BRT)"),
    ("UTC", "UTC"),
]


class ProfileCog(commands.Cog):
    """Manage player profile settings including timezone."""

    def __init__(self, bot: commands.Bot, availability_store: AvailabilityStore) -> None:
        self.bot = bot
        self.availability_store = availability_store

    profile = app_commands.Group(name="profile", description="Manage your player profile")

    @profile.command(name="timezone", description="Set your timezone for schedule display")
    @app_commands.describe(timezone="Your timezone")
    @app_commands.choices(
        timezone=[
            app_commands.Choice(name=label, value=tz) for tz, label in COMMON_TIMEZONES
        ]
    )
    async def profile_timezone(
        self, interaction: discord.Interaction, timezone: app_commands.Choice[str]
    ) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        self.availability_store.set_user_timezone(member.id, timezone.value)
        embed = success_embed(
            "Timezone Updated",
            f"Your timezone is now set to **{timezone.name}** (`{timezone.value}`).\n"
            "Schedule times will be shown in your local time when possible."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @profile.command(name="view", description="View your full profile")
    async def profile_view(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        info = self.availability_store.get_user_info(member.id)
        days = info.get("days", [])
        roles = info.get("roles", [])
        agents = info.get("agents", [])
        tz = info.get("timezone")
        status = info.get("status", "starter")
        scrim_slots = info.get("scrim_slots", {})
        vod_days = info.get("vod_days", [])

        # Format scrim slots
        scrim_str = "None set"
        if scrim_slots:
            parts = []
            for day, slots in scrim_slots.items():
                if slots:
                    parts.append(f"{day.title()}: #{', #'.join(str(s) for s in slots)}")
            scrim_str = "; ".join(parts) if parts else "None set"

        lines = [
            f"**Player:** {member.display_name}",
            f"**Team:** {info.get('team') or 'Not assigned'}",
            f"**Status:** {status.title()}",
            f"**Available Days:** {', '.join(d.title() for d in days) if days else 'None set'}",
            f"**Scrim Slots:** {scrim_str}",
            f"**VOD Days:** {', '.join(d.title() for d in vod_days) if vod_days else 'None set'}",
            f"**Roles:** {', '.join(r.title() for r in roles) if roles else 'Not set'}",
            f"**Agents:** {', '.join(agents) if agents else 'Not set'}",
            f"**Timezone:** {tz or 'Not set (use /profile timezone)'}",
        ]

        embed = format_embed(f"Profile: {member.display_name}", "\n".join(lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @profile.command(name="status", description="Set your player status (starter/sub/flex)")
    @app_commands.describe(status="Your team status")
    @app_commands.choices(
        status=[
            app_commands.Choice(name="Starter - Primary roster", value="starter"),
            app_commands.Choice(name="Sub - Backup/substitute", value="sub"),
            app_commands.Choice(name="Flex - Can play either role", value="flex"),
        ]
    )
    async def profile_status(
        self, interaction: discord.Interaction, status: app_commands.Choice[str]
    ) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        self.availability_store.set_player_status(member.id, status.value)
        embed = success_embed(
            "Status Updated",
            f"Your status is now set to **{status.name}**.\n"
            "This helps captains understand your availability for lineups."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @profile.command(name="role", description="Set your primary Valorant role")
    @app_commands.describe(role="Your primary role")
    @app_commands.choices(
        role=[
            app_commands.Choice(name="Duelist", value="duelist"),
            app_commands.Choice(name="Initiator", value="initiator"),
            app_commands.Choice(name="Controller", value="controller"),
            app_commands.Choice(name="Sentinel", value="sentinel"),
        ]
    )
    async def profile_role(
        self, interaction: discord.Interaction, role: app_commands.Choice[str]
    ) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        # Get current agents and add the role
        current = self.availability_store.get_user_agents(member.id)
        roles = set(current.get("roles", []))
        roles.add(role.value)

        self.availability_store.set_agents(
            member.id,
            member.display_name,
            list(roles),
            current.get("agents", [])
        )

        embed = success_embed(
            "Role Added",
            f"Added **{role.name}** to your roles.\n"
            f"Current roles: {', '.join(r.title() for r in roles)}"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class LineupCog(commands.Cog):
    """Manage lineup locks and suggestions."""

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

    lineup = app_commands.Group(name="lineup", description="Manage match lineups")

    @lineup.command(name="suggest", description="Get a suggested lineup for a day based on roles")
    @app_commands.describe(day="Day to get lineup suggestion for")
    @app_commands.choices(
        day=[app_commands.Choice(name=d.title(), value=d) for d in WEEK_DAYS]
    )
    async def lineup_suggest(
        self, interaction: discord.Interaction, day: app_commands.Choice[str]
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        # Defer for lineup computation
        await safe_defer(interaction, ephemeral=True)

        # Build with lineup suggestions enabled
        summaries = self.schedule_builder.build_week(interaction.guild.id, include_lineup_suggestions=True)

        # Find the day
        day_summary = next((s for s in summaries if s.day == day.value), None)
        if not day_summary:
            await interaction.followup.send("Day not found.", ephemeral=True)
            return

        if day_summary.total_available < 5:
            embed = error_embed(
                "Not Enough Players",
                f"Only **{day_summary.total_available}** players available on {day.name}.\n"
                f"Need at least 5 for a lineup suggestion."
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        suggestion = day_summary.lineup_suggestion
        if not suggestion:
            embed = error_embed("No Suggestion", "Could not generate a lineup suggestion.")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # Format the suggestion
        player_lines = []
        for p in suggestion.players:
            role_str = f" ({', '.join(r.title() for r in p.roles)})" if p.roles else ""
            player_lines.append(f"• **{p.display_name}**{role_str}")

        status = "Complete" if suggestion.is_complete else "Incomplete"
        role_status = "All roles covered" if suggestion.has_all_roles else f"Missing: {', '.join(r.title() for r in suggestion.missing_roles)}"

        embed = format_embed(
            f"Lineup Suggestion: {day.name}",
            f"**Status:** {status} ({len(suggestion.players)}/5 players)\n"
            f"**Roles:** {role_status}\n\n"
            f"**Suggested Players:**\n" + "\n".join(player_lines),
            color=discord.Color.green() if suggestion.is_complete and suggestion.has_all_roles else discord.Color.orange()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @lineup.command(name="lock", description="Lock the lineup for a specific day (captain/admin)")
    @app_commands.describe(
        day="Day to lock lineup for",
        players="Mention up to 5 players to lock in the lineup"
    )
    @app_commands.choices(
        day=[app_commands.Choice(name=d.title(), value=d) for d in WEEK_DAYS]
    )
    async def lineup_lock(
        self,
        interaction: discord.Interaction,
        day: app_commands.Choice[str],
        players: str,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not has_captain_permission(member, self.config_store):
            await interaction.response.send_message(
                "You need Captain role or Manage Server permission to lock lineups.", ephemeral=True
            )
            return

        # Parse player mentions from the string
        mention_pattern = r"<@!?(\d+)>"
        matches = re.findall(mention_pattern, players)

        if not matches:
            embed = error_embed(
                "No Players Found",
                "Please mention players using @username. Example:\n"
                "`/lineup lock day:Wednesday players:@player1 @player2 @player3 @player4 @player5`"
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        player_ids = [int(m) for m in matches[:5]]  # Max 5 players

        if len(player_ids) != 5:
            embed = error_embed(
                "Invalid Lineup",
                f"Found {len(player_ids)} player(s). A lineup must have exactly 5 players."
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Lock the lineup
        self.config_store.set_locked_lineup(interaction.guild.id, day.value, player_ids, "premier")

        # Get player names
        player_names = []
        for pid in player_ids:
            m = interaction.guild.get_member(pid)
            player_names.append(m.display_name if m else f"Unknown ({pid})")

        embed = success_embed(
            f"Lineup Locked: {day.name}",
            f"**Players:**\n" + "\n".join(f"• {name}" for name in player_names)
        )
        await interaction.response.send_message(embed=embed)

    @lineup.command(name="view", description="View the locked lineup for a day")
    @app_commands.describe(day="Day to view lineup for")
    @app_commands.choices(
        day=[app_commands.Choice(name=d.title(), value=d) for d in WEEK_DAYS]
    )
    async def lineup_view(
        self, interaction: discord.Interaction, day: app_commands.Choice[str]
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        locked = self.config_store.get_locked_lineup(interaction.guild.id, day.value, "premier")

        if not locked:
            embed = format_embed(
                f"Lineup: {day.name}",
                "No lineup locked for this day yet.\n"
                "Use `/lineup lock` to set one, or `/lineup suggest` for a recommendation."
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        player_ids = locked.get("player_ids", [])
        locked_at = locked.get("locked_at", "Unknown")

        player_lines = []
        for pid in player_ids:
            m = interaction.guild.get_member(int(pid))
            if m:
                info = self.availability_store.get_user_info(m.id)
                roles = info.get("roles", [])
                role_str = f" - {', '.join(r.title() for r in roles)}" if roles else ""
                player_lines.append(f"• **{m.display_name}**{role_str}")
            else:
                player_lines.append(f"• Unknown player ({pid})")

        embed = format_embed(
            f"Locked Lineup: {day.name}",
            f"**Locked at:** {locked_at}\n\n"
            f"**Players:**\n" + "\n".join(player_lines),
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @lineup.command(name="unlock", description="Unlock/clear the lineup for a day (captain/admin)")
    @app_commands.describe(day="Day to unlock lineup for")
    @app_commands.choices(
        day=[app_commands.Choice(name=d.title(), value=d) for d in WEEK_DAYS]
    )
    async def lineup_unlock(
        self, interaction: discord.Interaction, day: app_commands.Choice[str]
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not has_captain_permission(member, self.config_store):
            await interaction.response.send_message(
                "You need Captain role or Manage Server permission to unlock lineups.", ephemeral=True
            )
            return

        cleared = self.config_store.clear_locked_lineup(interaction.guild.id, day.value, "premier")
        if cleared:
            embed = success_embed("Lineup Unlocked", f"The lineup for **{day.name}** has been cleared.")
        else:
            embed = format_embed("No Lineup", f"There was no locked lineup for **{day.name}**.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class DayTypeSelect(discord.ui.Select):
    """Select menu for bulk day type editing."""

    def __init__(self, config_store: GuildConfigStore, guild_id: int) -> None:
        self.config_store = config_store
        self.guild_id = guild_id
        options = [
            discord.SelectOption(
                label=day.title(),
                value=day,
                emoji=DAY_EMOJIS.get(day),
                description=f"Currently: {config_store.get_day_type(guild_id, day)}"
            )
            for day in WEEK_DAYS
        ]
        super().__init__(
            placeholder="Select days to edit",
            min_values=1,
            max_values=len(WEEK_DAYS),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, DayTypeBulkView):
            view.selected_days = list(self.values)
            # Enable the type selector
            view.type_select.disabled = False
            await interaction.response.edit_message(
                content=f"Selected days: **{', '.join(d.title() for d in self.values)}**\nNow select a day type:",
                view=view,
            )


class DayTypeTypeSelect(discord.ui.Select):
    """Select menu for choosing day type."""

    def __init__(self, config_store: GuildConfigStore, guild_id: int) -> None:
        self.config_store = config_store
        self.guild_id = guild_id
        options = [
            discord.SelectOption(
                label=dt,
                value=dt,
                emoji=DAY_TYPE_ICONS.get(dt, ""),
                description=self._get_description(dt),
            )
            for dt in DAY_TYPES
        ]
        super().__init__(
            placeholder="Select day type to apply",
            min_values=1,
            max_values=1,
            options=options,
            disabled=True,
        )

    @staticmethod
    def _get_description(dt: str) -> str:
        descriptions = {
            "FREE": "No activities - free day",
            "PREMIER": "Premier match day",
            "SCRIM": "Scrim practice day",
            "VOD": "VOD review day",
            "MIXED": "Multiple activity types",
            "OFF": "Team day off",
        }
        return descriptions.get(dt, "")

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, DayTypeBulkView):
            return

        day_type = self.values[0]
        changed_days = []

        for day in view.selected_days:
            self.config_store.set_day_type(self.guild_id, day, day_type)
            changed_days.append(day.title())

        embed = success_embed(
            "Day Types Updated",
            f"Set **{', '.join(changed_days)}** to **{day_type}** {DAY_TYPE_ICONS.get(day_type, '')}"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class DayTypeBulkView(PersistentView):
    """View for bulk editing day types."""

    def __init__(self, config_store: GuildConfigStore, guild_id: int) -> None:
        super().__init__(timeout=300.0)
        self.config_store = config_store
        self.guild_id = guild_id
        self.selected_days: List[str] = []

        self.day_select = DayTypeSelect(config_store, guild_id)
        self.type_select = DayTypeTypeSelect(config_store, guild_id)

        self.add_item(self.day_select)
        self.add_item(self.type_select)


class PremierCog(commands.Cog):
    """Admin commands for Premier bot management."""

    def __init__(
        self,
        bot: commands.Bot,
        availability_store: AvailabilityStore,
        config_store: GuildConfigStore,
        log_store: GameLogStore,
    ) -> None:
        self.bot = bot
        self.availability_store = availability_store
        self.config_store = config_store
        self.log_store = log_store

    premier = app_commands.Group(name="premier", description="Premier bot admin commands")
    days = app_commands.Group(name="days", description="Manage day types", parent=premier)

    # -------- Day Types Commands --------

    @days.command(name="view", description="View current day types for all days")
    async def days_view(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        lines = []
        for day in WEEK_DAYS:
            day_type = self.config_store.get_day_type(interaction.guild.id, day)
            day_label = self.config_store.get_day_label(interaction.guild.id, day)
            icon = DAY_TYPE_ICONS.get(day_type, "")
            label_str = f" — _{day_label}_" if day_label else ""
            lines.append(f"{icon} **{day.title()}**: {day_type}{label_str}")

        embed = format_embed("Day Types Configuration", "\n".join(lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @days.command(name="set", description="Set the day type for a specific day")
    @app_commands.describe(
        day="Day of the week",
        day_type="Type of activity for this day"
    )
    @app_commands.choices(
        day=[app_commands.Choice(name=d.title(), value=d) for d in WEEK_DAYS],
        day_type=[app_commands.Choice(name=f"{DAY_TYPE_ICONS.get(dt, '')} {dt}", value=dt) for dt in DAY_TYPES]
    )
    async def days_set(
        self,
        interaction: discord.Interaction,
        day: app_commands.Choice[str],
        day_type: app_commands.Choice[str],
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not (
            member.guild_permissions.manage_guild or member.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to change day types.", ephemeral=True
            )
            return

        self.config_store.set_day_type(interaction.guild.id, day.value, day_type.value)
        icon = DAY_TYPE_ICONS.get(day_type.value, "")
        embed = success_embed(
            "Day Type Updated",
            f"**{day.name}** is now set to **{day_type.value}** {icon}"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @days.command(name="bulk", description="Edit multiple day types at once")
    async def days_bulk(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not (
            member.guild_permissions.manage_guild or member.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to change day types.", ephemeral=True
            )
            return

        view = DayTypeBulkView(self.config_store, interaction.guild.id)
        await interaction.response.send_message(
            "Select the days you want to modify:",
            view=view,
            ephemeral=True,
        )

    @days.command(name="reset", description="Reset all day types to defaults")
    async def days_reset(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not (
            member.guild_permissions.manage_guild or member.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to reset day types.", ephemeral=True
            )
            return

        self.config_store.reset_day_types(interaction.guild.id)
        embed = success_embed(
            "Day Types Reset",
            "All day types have been reset to defaults."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @days.command(name="label", description="Set a custom label for a day (e.g., 'Gym Day')")
    @app_commands.describe(
        day="Day of the week",
        label="Custom label (leave empty to clear)"
    )
    @app_commands.choices(
        day=[app_commands.Choice(name=d.title(), value=d) for d in WEEK_DAYS]
    )
    async def days_label(
        self,
        interaction: discord.Interaction,
        day: app_commands.Choice[str],
        label: Optional[str] = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not (
            member.guild_permissions.manage_guild or member.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to set day labels.", ephemeral=True
            )
            return

        self.config_store.set_day_label(interaction.guild.id, day.value, label)
        if label:
            embed = success_embed(
                "Day Label Set",
                f"**{day.name}** now has the label: _{label}_"
            )
        else:
            embed = success_embed(
                "Day Label Cleared",
                f"**{day.name}** label has been cleared."
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # -------- Main Premier Commands --------

    @premier.command(name="status", description="Show bot status and configuration summary (admin only)")
    async def premier_status(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not (
            member.guild_permissions.manage_guild or member.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to view bot status.", ephemeral=True
            )
            return

        await safe_defer(interaction, ephemeral=True)
        guild_id = interaction.guild.id

        # Get configuration summary
        ann_channel = self.config_store.get_announcement_channel(guild_id)
        ping_role = self.config_store.get_ping_role(guild_id)
        team_roles = self.config_store.get_team_roles(guild_id)
        reminders_enabled = self.config_store.get_reminders_enabled(guild_id)

        # Count availability
        all_users = self.availability_store.all_users()
        users_with_days = sum(1 for u in all_users.values() if u.get("days"))
        users_with_agents = sum(1 for u in all_users.values() if u.get("agents"))

        # Count logs
        recent_logs = self.log_store.recent_logs(guild_id, limit=100)

        # Get today's schedule
        today_idx = datetime.now().weekday()
        today_label = WEEK_DAYS[today_idx]
        today_users = self.availability_store.users_for_day(today_label)

        # Build status
        ann_str = f"<#{ann_channel}>" if ann_channel else "Not set"
        ping_str = f"<@&{ping_role}>" if ping_role else "Not set"
        team_a_str = f"<@&{team_roles['A']}>" if team_roles.get("A") else "Not set"
        team_b_str = f"<@&{team_roles['B']}>" if team_roles.get("B") else "Not set"

        lines = [
            "## Configuration",
            f"**Announcement Channel:** {ann_str}",
            f"**Ping Role:** {ping_str}",
            f"**Team A Role:** {team_a_str}",
            f"**Team B Role:** {team_b_str}",
            f"**Reminders:** {'Enabled' if reminders_enabled else 'Disabled'}",
            "",
            "## Statistics",
            f"**Players with availability:** {users_with_days}",
            f"**Players with agents set:** {users_with_agents}",
            f"**Logged matches:** {len(recent_logs)}",
            "",
            "## Today ({})".format(today_label.title()),
            f"**Available players:** {len(today_users)}",
        ]

        embed = format_embed(f"Premier Bot Status — {interaction.guild.name}", "\n".join(lines))
        await interaction.followup.send(embed=embed, ephemeral=True)

    @premier.command(name="reminders", description="Enable or disable automatic match reminders")
    @app_commands.describe(enabled="Enable or disable reminders")
    @app_commands.choices(
        enabled=[
            app_commands.Choice(name="Enable", value="true"),
            app_commands.Choice(name="Disable", value="false"),
        ]
    )
    async def premier_reminders(
        self, interaction: discord.Interaction, enabled: app_commands.Choice[str]
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not (
            member.guild_permissions.manage_guild or member.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to change reminder settings.", ephemeral=True
            )
            return

        is_enabled = enabled.value == "true"
        self.config_store.set_reminders_enabled(interaction.guild.id, is_enabled)

        lead_times = self.config_store.get_reminder_lead_times(interaction.guild.id)
        times_str = ", ".join(f"{h}h" for h in lead_times)

        status = "enabled" if is_enabled else "disabled"
        embed = success_embed(
            "Reminders Updated",
            f"Automatic match reminders are now **{status}**.\n"
            f"Reminder times: {times_str} before events\n"
            f"_Use `/premier reminder_times` to change when reminders are sent._"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @premier.command(name="reminder_times", description="Set when reminders are sent (hours before events)")
    @app_commands.describe(
        hours="Comma-separated hours before event (e.g., '24,1' for 24h and 1h before)"
    )
    async def premier_reminder_times(
        self, interaction: discord.Interaction, hours: str
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not (
            member.guild_permissions.manage_guild or member.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to change reminder times.", ephemeral=True
            )
            return

        # Parse hours
        try:
            hour_list = [int(h.strip()) for h in hours.split(",") if h.strip()]
            hour_list = [h for h in hour_list if 1 <= h <= 168]  # 1h to 7 days
        except ValueError:
            await interaction.response.send_message(
                "Invalid format. Use comma-separated numbers like `24,1` for 24h and 1h.", ephemeral=True
            )
            return

        if not hour_list:
            await interaction.response.send_message(
                "Please provide at least one valid hour (1-168).", ephemeral=True
            )
            return

        self.config_store.set_reminder_lead_times(interaction.guild.id, hour_list)

        times_str = ", ".join(f"{h}h" for h in sorted(hour_list, reverse=True))
        embed = success_embed(
            "Reminder Times Updated",
            f"Reminders will now be sent at: **{times_str}** before events.\n"
            f"This applies to Premier, Scrim, and VOD reminders."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @premier.command(name="reminder_ping", description="Set how reminders ping (role/@here/none)")
    @app_commands.describe(style="Ping style for reminders")
    @app_commands.choices(
        style=[
            app_commands.Choice(name="Ping Role", value="role"),
            app_commands.Choice(name="@here", value="here"),
            app_commands.Choice(name="No Ping", value="none"),
        ]
    )
    async def premier_reminder_ping(
        self, interaction: discord.Interaction, style: app_commands.Choice[str]
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not (
            member.guild_permissions.manage_guild or member.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to change reminder ping style.", ephemeral=True
            )
            return

        self.config_store.set_reminder_ping_style(interaction.guild.id, style.value)

        desc_map = {
            "role": "the configured ping role",
            "here": "@here",
            "none": "no ping"
        }
        embed = success_embed(
            "Reminder Ping Style Updated",
            f"Reminders will now use **{desc_map[style.value]}**."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @premier.command(name="config", description="View current bot configuration summary")
    async def premier_config(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        await safe_defer(interaction, ephemeral=True)

        guild_id = interaction.guild.id

        # Gather configuration
        ann_channel = self.config_store.get_announcement_channel(guild_id)
        ping_role = self.config_store.get_ping_role(guild_id)
        captain_role = self.config_store.get_captain_role(guild_id)
        team_roles = self.config_store.get_team_roles(guild_id)
        timezone = self.config_store.get_guild_timezone(guild_id)
        reminders_enabled = self.config_store.get_reminders_enabled(guild_id)
        lead_times = self.config_store.get_reminder_lead_times(guild_id)
        ping_style = self.config_store.get_reminder_ping_style(guild_id)
        scrims_per_day = self.config_store.get_scrims_per_day(guild_id)
        scrim_spacing = self.config_store.get_scrim_spacing(guild_id)
        vod_time = self.config_store.get_vod_start_time(guild_id)
        vod_duration = self.config_store.get_vod_session_minutes(guild_id)
        dashboard = self.config_store.get_dashboard(guild_id)

        ann_str = f"<#{ann_channel}>" if ann_channel else "Not set"
        ping_str = f"<@&{ping_role}>" if ping_role else "Not set"
        captain_str = f"<@&{captain_role}>" if captain_role else "Not set"
        team_a_str = f"<@&{team_roles['A']}>" if team_roles.get("A") else "Not set"
        team_b_str = f"<@&{team_roles['B']}>" if team_roles.get("B") else "Not set"
        dashboard_str = "Active" if dashboard else "Not set"

        lines = [
            "## Channels & Roles",
            f"**Announcement Channel:** {ann_str}",
            f"**Ping Role:** {ping_str}",
            f"**Captain Role:** {captain_str}",
            f"**Team A Role:** {team_a_str}",
            f"**Team B Role:** {team_b_str}",
            "",
            "## Schedule Settings",
            f"**Timezone:** {timezone}",
            f"**Scrims/Day:** {scrims_per_day} slots",
            f"**Scrim Spacing:** {scrim_spacing} minutes",
            f"**VOD Time:** `{vod_time}` ({vod_duration}min)",
            f"**Dashboard:** {dashboard_str}",
            "",
            "## Reminders",
            f"**Status:** {'Enabled' if reminders_enabled else 'Disabled'}",
            f"**Lead Times:** {', '.join(f'{h}h' for h in lead_times)}",
            f"**Ping Style:** {ping_style}",
        ]

        embed = format_embed(f"Bot Configuration — {interaction.guild.name}", "\n".join(lines))
        await interaction.followup.send(embed=embed, ephemeral=True)

    @premier.command(name="doctor", description="Check bot health and identify configuration issues")
    async def premier_doctor(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not (
            member.guild_permissions.manage_guild or member.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to run diagnostics.", ephemeral=True
            )
            return

        await safe_defer(interaction, ephemeral=True)

        guild_id = interaction.guild.id
        issues: List[str] = []
        warnings: List[str] = []
        good: List[str] = []

        # Check announcement channel
        ann_channel = self.config_store.get_announcement_channel(guild_id)
        if not ann_channel:
            issues.append("⚠️ No announcement channel set — use `/config announcement`")
        elif not interaction.guild.get_channel(ann_channel):
            issues.append("❌ Announcement channel not found — update with `/config announcement`")
        else:
            good.append("✅ Announcement channel configured")

        # Check ping role
        ping_role = self.config_store.get_ping_role(guild_id)
        if not ping_role:
            warnings.append("⚠️ No ping role set — schedule pings won't mention anyone")
        elif not interaction.guild.get_role(ping_role):
            issues.append("❌ Ping role not found — update with `/config pingrole`")
        else:
            good.append("✅ Ping role configured")

        # Check day types
        day_types = self.config_store.get_all_day_types(guild_id)
        active_days = [d for d, t in day_types.items() if t not in ("FREE", "OFF")]
        if not active_days:
            issues.append("⚠️ All days are FREE/OFF — users can't sign up")
        else:
            good.append(f"✅ {len(active_days)} active days configured")

        # Check for Premier days without windows
        for day in WEEK_DAYS:
            if day_types.get(day) == "PREMIER":
                window = self.config_store.get_premier_window(guild_id, day)
                if not window:
                    warnings.append(f"⚠️ {day.title()} is PREMIER but has no window set")

        # Check for Scrim days without times
        for day in WEEK_DAYS:
            if day_types.get(day) in ("SCRIM", "MIXED"):
                scrim_time = self.config_store.get_scrim_time(guild_id, day)
                if not scrim_time:
                    warnings.append(f"⚠️ {day.title()} is {day_types[day]} but has no scrim time")

        # Check dashboard
        dashboard = self.config_store.get_dashboard(guild_id)
        if dashboard:
            channel = interaction.guild.get_channel(dashboard["channel_id"])
            if not channel:
                issues.append("❌ Dashboard channel not found — use `/schedule dashboard post`")
            else:
                try:
                    await channel.fetch_message(dashboard["message_id"])
                    good.append("✅ Dashboard message found")
                except discord.NotFound:
                    issues.append("❌ Dashboard message deleted — use `/schedule dashboard post`")
                except discord.HTTPException:
                    warnings.append("⚠️ Could not verify dashboard message")
        else:
            warnings.append("⚠️ No dashboard configured — use `/schedule dashboard post`")

        # Check availability data
        all_users = self.availability_store.all_users()
        users_with_days = sum(1 for u in all_users.values() if u.get("days"))
        if users_with_days == 0:
            warnings.append("⚠️ No players have signed up yet")
        else:
            good.append(f"✅ {users_with_days} players with availability")

        # Build result
        lines = []
        if issues:
            lines.append("## Issues")
            lines.extend(issues)
            lines.append("")
        if warnings:
            lines.append("## Warnings")
            lines.extend(warnings)
            lines.append("")
        if good:
            lines.append("## Good")
            lines.extend(good)

        if not issues and not warnings:
            lines.insert(0, "**All systems healthy!** 🎉\n")

        embed = format_embed("Bot Health Check", "\n".join(lines))
        await interaction.followup.send(embed=embed, ephemeral=True)

    @premier.command(name="conflicts", description="Show players missing availability and low-coverage days")
    async def premier_conflicts(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        await safe_defer(interaction, ephemeral=True)

        guild_id = interaction.guild.id
        day_types = self.config_store.get_all_day_types(guild_id)
        active_days = [d for d, t in day_types.items() if t not in ("FREE", "OFF")]

        lines = []

        # Find days with low coverage
        lines.append("## Coverage by Day")
        for day in active_days:
            users = self.availability_store.users_for_day(day)
            count = len(users)
            day_type = day_types[day]
            icon = DAY_TYPE_ICONS.get(day_type, "")

            if count == 0:
                status = "❌ No signups"
            elif count < 5:
                status = f"⚠️ {count}/5 (need {5 - count} more)"
            elif count < 10 and day_type in ("SCRIM", "MIXED"):
                status = f"⚠️ {count}/10 for scrims (need {10 - count} more)"
            else:
                status = f"✅ {count} players"

            lines.append(f"{icon} **{day.title()}**: {status}")

        lines.append("")

        # Find players without availability
        lines.append("## Players Without Availability")
        all_users = self.availability_store.all_users()
        missing_availability = []

        for user_id, info in all_users.items():
            # User exists but has no days set
            if not info.get("days"):
                name = info.get("display_name", f"Unknown ({user_id})")
                missing_availability.append(name)

        if missing_availability:
            if len(missing_availability) <= 10:
                lines.append(", ".join(missing_availability))
            else:
                lines.append(f"{', '.join(missing_availability[:10])} +{len(missing_availability) - 10} more")
        else:
            lines.append("_All registered players have availability set_")

        embed = format_embed("Availability Conflicts", "\n".join(lines))
        await interaction.followup.send(embed=embed, ephemeral=True)

    @premier.command(name="export", description="Export schedule as a text block for sharing")
    async def premier_export(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        await safe_defer(interaction, ephemeral=True)

        summaries = ScheduleBuilder(
            self.availability_store, self.config_store
        ).build_week(interaction.guild.id)

        lines = [f"VALORANT SCHEDULE — {interaction.guild.name}", "=" * 40, ""]

        for s in summaries:
            if s.day_type in ("FREE", "OFF"):
                lines.append(f"{s.day.upper()}: {s.day_type}")
            else:
                lines.append(f"{s.day.upper()} [{s.day_type}]:")
                if s.premier_window:
                    lines.append(f"  Premier: {s.premier_window}")
                if s.scrim_time:
                    lines.append(f"  Scrim: {s.scrim_time}")
                if s.vod_session:
                    lines.append(f"  VOD: {s.vod_session.start_time}")
                lines.append(f"  Available: {s.total_available} players")
                if s.available_names:
                    names = ", ".join(s.available_names[:5])
                    if len(s.available_names) > 5:
                        names += f" +{len(s.available_names) - 5} more"
                    lines.append(f"  Players: {names}")
            lines.append("")

        text = "\n".join(lines)
        # Send as code block
        await interaction.followup.send(f"```\n{text}\n```", ephemeral=True)

    @premier.command(name="help", description="Show help information for all Premier bot commands")
    async def premier_help(self, interaction: discord.Interaction) -> None:
        help_text = """
## Availability Commands
- `/availability set` - Set your available days
- `/availability clear` - Clear your availability
- `/availability mine` - View your current availability
- `/availability day` - See who's available on specific days
- `/availability panel` - Post an interactive signup panel
- `/availability scrim set/view/clear` - Manage scrim slot availability
- `/availability vod set/view/clear` - Manage VOD availability
- `/availability resetweek` - Admin: reset all availability

## Schedule Commands
- `/schedule preview` - Preview the weekly schedule
- `/schedule post` - Post schedule to announcement channel
- `/schedule pingcheck` - Check if ping thresholds are met
- `/schedule dashboard post/refresh/clear` - Manage auto-updating dashboard

## Lineup Commands
- `/lineup suggest` - Get AI-suggested lineup based on roles
- `/lineup lock` - Captain: lock a lineup for a day
- `/lineup view` - View locked lineup for a day
- `/lineup unlock` - Captain: clear a locked lineup

## Profile Commands
- `/profile timezone` - Set your timezone
- `/profile status` - Set starter/sub/flex status
- `/profile role` - Set your Valorant role
- `/profile view` - View your full profile

## Agent Commands
- `/agents set` - Set your roles and agents
- `/agents mine` - View your saved agents
- `/agents team` - View team agent compositions

## Config Commands
- `/config announcement` - Set announcement channel
- `/config pingrole` - Set ping role
- `/config teamroles` - Set team A/B roles
- `/config captain_role` - Set captain role
- `/config timezone` - Set guild timezone
- `/config scrimtime` - Set scrim times
- `/config scrim_slots` - Configure scrim slots per day
- `/config vod` - Configure VOD sessions
- `/config practicetime` - Set practice times
- `/config premier_window` - Set Premier windows
- `/config map_*` - Set maps for each day

## Day Types Commands
- `/premier days view` - View day types
- `/premier days set` - Set day type for a day
- `/premier days bulk` - Edit multiple days
- `/premier days label` - Set custom day label
- `/premier days reset` - Reset to defaults

## Admin Commands
- `/premier status` - View bot statistics
- `/premier config` - View full configuration
- `/premier doctor` - Check bot health
- `/premier conflicts` - Find availability gaps
- `/premier export` - Export schedule as text
- `/premier reminders` - Toggle reminders
- `/premier reminder_times` - Set reminder lead times
- `/premier reminder_ping` - Set reminder ping style
"""
        embed = format_embed("Premier Bot Help", help_text)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class ValorantBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.guilds = True
        super().__init__(command_prefix="!", intents=intents)

        self.availability_store = AvailabilityStore()
        self.config_store = GuildConfigStore()
        self.log_store = GameLogStore()

    async def setup_hook(self) -> None:  # type: ignore[override]
        # Set up global error handler for app commands
        self.tree.on_error = self._on_app_command_error

        # Core cogs
        await self.add_cog(AvailabilityCog(self, self.availability_store, self.config_store))
        await self.add_cog(ScheduleCog(self, self.availability_store, self.config_store))
        await self.add_cog(ConfigCog(self, self.config_store))
        await self.add_cog(AgentsCog(self, self.availability_store, self.config_store))
        await self.add_cog(RoleSyncCog(self, self.availability_store, self.config_store))
        await self.add_cog(GameLogCog(self, self.log_store))

        # New feature cogs
        await self.add_cog(ProfileCog(self, self.availability_store))
        await self.add_cog(LineupCog(self, self.availability_store, self.config_store))
        await self.add_cog(PremierCog(self, self.availability_store, self.config_store, self.log_store))

        try:
            synced = await self.tree.sync()
            logging.info("Synced %d app commands", len(synced))
        except discord.HTTPException as exc:
            logging.error("Failed to sync commands: %s", exc)

    async def _on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """Global error handler for all app commands."""
        logging.error("App command error: %s", error, exc_info=error)

        # Determine user-friendly message
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"This command is on cooldown. Try again in {error.retry_after:.1f}s."
        elif isinstance(error, app_commands.MissingPermissions):
            msg = "You don't have permission to use this command."
        elif isinstance(error, app_commands.BotMissingPermissions):
            msg = "I don't have the required permissions to do that."
        elif isinstance(error, app_commands.CheckFailure):
            msg = "You can't use this command right now."
        else:
            msg = "Something went wrong. Please try again."
            # Log full traceback for unexpected errors
            logging.error("Full traceback:\n%s", traceback.format_exc())

        # Try to respond to the user
        embed = error_embed("Command Error", msg)
        await safe_respond(interaction, embed=embed, ephemeral=True)

    async def on_ready(self) -> None:  # type: ignore[override]
        assert self.user is not None
        logging.info("Logged in as %s", self.user)

    async def on_member_update(  # type: ignore[override]
        self,
        before: discord.Member,
        after: discord.Member,
    ) -> None:
        """Keep stored 'team' in AvailabilityStore in sync with current Discord roles."""

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
