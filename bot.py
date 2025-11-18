from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, date
from functools import lru_cache
from typing import Dict, List, Optional, Set, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks

from scheduler import ScheduleBuilder, WEEK_DAYS
from storage import AvailabilityStore, GuildConfigStore

logging.basicConfig(level=logging.INFO)


def _safe_int_env(var_name: str) -> Optional[int]:
    raw = os.getenv(var_name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        logging.warning("Ignoring non-numeric value for %s: %s", var_name, raw)
        return None


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
    return (
        TEAM_A_ROLE_ID,
        TEAM_B_ROLE_ID,
    )


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


class AvailabilitySelect(discord.ui.Select):
    def __init__(self, cog: "AvailabilityCog") -> None:
        options = [
            discord.SelectOption(label=day.title(), value=day) for day in WEEK_DAYS
        ]
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
        await self.cog._sync_member_role(member)
        await interaction.response.send_message("Cleared your availability for the week.", ephemeral=True)


class AvailabilityPanelView(discord.ui.View):
    def __init__(self, cog: "AvailabilityCog") -> None:
        super().__init__(timeout=60 * 60)
        self.add_item(AvailabilitySelect(cog))
        self.add_item(AvailabilityClearButton(cog))


class AvailabilityCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        availability_store: AvailabilityStore,
        config_store: GuildConfigStore,
        role_manager: "AvailabilityRoleManager | None" = None,
    ) -> None:
        self.bot = bot
        self.availability_store = availability_store
        self.config_store = config_store
        self.role_manager = role_manager

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
        await self._sync_member_role(member)
        return normalized_days, normalized_team

    async def _sync_member_role(self, member: discord.Member) -> None:
        if not self.role_manager:
            return
        await self.role_manager.sync_member(member)

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
        await self._sync_member_role(member)
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
            await interaction.response.send_message(f"No one has signed up for {normalized.title()} yet.", ephemeral=True)
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
        if self.role_manager:
            await self.role_manager.clear_roles_for_all_guilds()
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
        self.schedule_builder = ScheduleBuilder(availability_store)
        self.config_store = config_store

    schedule = app_commands.Group(name="schedule", description="Build and post weekly schedules")

    @schedule.command(name="preview", description="Preview the current schedule")
    async def schedule_preview(self, interaction: discord.Interaction) -> None:
        summaries = self.schedule_builder.build_week()
        text = ScheduleBuilder.format_schedule(summaries)
        embed = format_embed("Valorant Weekly Schedule", text)
        await interaction.response.send_message(embed=embed)

    @schedule.command(name="post", description="Post the schedule to the announcement channel")
    async def schedule_post(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        summaries = self.schedule_builder.build_week()
        text = ScheduleBuilder.format_schedule(summaries)
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

    def _resolve_announcement_channel_id(self, guild: discord.Guild) -> Optional[int]:
        configured = self.config_store.get_announcement_channel(guild.id)
        if configured:
            return configured
        if ANNOUNCEMENT_CHANNEL_ID:
            return ANNOUNCEMENT_CHANNEL_ID
        return None

    def _resolve_ping_mention(self, guild: discord.Guild) -> Optional[str]:
        configured_role_id = (
            self.config_store.get_ping_role(guild.id)
            or self.config_store.get_availability_role(guild.id)
            or (AVAILABLE_ROLE_ID if AVAILABLE_ROLE_ID else None)
        )
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

    config = app_commands.Group(name="config", description="Configure announcements and pings")

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
    @app_commands.describe(role="Role to mention for availability updates")
    async def config_ping_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        self.config_store.set_ping_role(interaction.guild.id, role.id)
        await interaction.response.send_message(f"Ping role set to {role.mention}", ephemeral=True)

    @config.command(
        name="availablerole",
        description="Set the role granted to members available today",
    )
    @app_commands.describe(role="Role that should be granted to available members")
    async def config_availability_role(
        self, interaction: discord.Interaction, role: discord.Role
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        self.config_store.set_availability_role(interaction.guild.id, role.id)
        await interaction.response.send_message(
            f"Availability role set to {role.mention}", ephemeral=True
        )

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


class AvailabilityRoleManager(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        availability_store: AvailabilityStore,
        config_store: GuildConfigStore,
    ) -> None:
        self.bot = bot
        self.availability_store = availability_store
        self.config_store = config_store

    def cog_unload(self) -> None:
        if self.sync_roles_task.is_running():
            self.sync_roles_task.cancel()

    async def cog_load(self) -> None:
        if not self.sync_roles_task.is_running():
            self.sync_roles_task.start()

    @tasks.loop(minutes=60)
    async def sync_roles_task(self) -> None:
        await self.sync_all_guilds()

    @sync_roles_task.before_loop
    async def before_sync_roles_task(self) -> None:
        await self.bot.wait_until_ready()

    async def sync_all_guilds(self) -> None:
        today_ids = self._today_user_ids()
        for guild in self.bot.guilds:
            await self._sync_guild_roles(guild, today_ids)

    async def sync_member(self, member: discord.Member) -> None:
        today_ids = self._today_user_ids()
        await self._apply_role(member, today_ids)

    async def clear_roles_for_all_guilds(self) -> None:
        for guild in self.bot.guilds:
            role = self._resolve_role(guild)
            if not role:
                continue
            for member in guild.members:
                if role in member.roles:
                    await self._remove_role(member, role)

    async def _sync_guild_roles(self, guild: discord.Guild, today_ids: Set[int]) -> None:
        role = self._resolve_role(guild)
        if not role:
            return
        for member in guild.members:
            await self._apply_role(member, today_ids, role)

    async def _apply_role(
        self,
        member: discord.Member,
        today_ids: Set[int],
        role: Optional[discord.Role] = None,
    ) -> None:
        if not member.guild:
            return
        resolved_role = role or self._resolve_role(member.guild)
        if not resolved_role:
            return
        should_have = member.id in today_ids
        has_role = resolved_role in member.roles
        try:
            if should_have and not has_role:
                await member.add_roles(
                    resolved_role, reason="Marked available for today's schedule"
                )
            elif not should_have and has_role:
                await member.remove_roles(
                    resolved_role, reason="Not marked available for today"
                )
        except discord.Forbidden:
            logging.warning(
                "Missing permissions to manage availability role in guild %s", member.guild
            )
        except discord.HTTPException:
            logging.warning("Failed to update availability role for %s", member.display_name)

    async def _remove_role(self, member: discord.Member, role: discord.Role) -> None:
        if role not in member.roles:
            return
        try:
            await member.remove_roles(role, reason="Availability cleared")
        except discord.Forbidden:
            logging.warning(
                "Missing permissions to remove availability role in guild %s", member.guild
            )
        except discord.HTTPException:
            logging.warning("Failed to remove availability role for %s", member.display_name)

    def _today_user_ids(self) -> Set[int]:
        today = WEEK_DAYS[datetime.now().weekday()]
        return {info["id"] for info in self.availability_store.users_for_day(today)}

    def _resolve_role(self, guild: discord.Guild) -> Optional[discord.Role]:
        role_id = self._resolve_role_id(guild)
        if not role_id:
            return None
        return guild.get_role(role_id)

    def _resolve_role_id(self, guild: discord.Guild) -> Optional[int]:
        configured = self.config_store.get_availability_role(guild.id)
        if configured:
            return configured
        if AVAILABLE_ROLE_ID:
            return AVAILABLE_ROLE_ID
        return self.config_store.get_ping_role(guild.id)


class AutoResetter(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        availability_store: AvailabilityStore,
        config_store: GuildConfigStore,
        role_manager: "AvailabilityRoleManager | None" = None,
    ) -> None:
        self.bot = bot
        self.availability_store = availability_store
        self.config_store = config_store
        self.role_manager = role_manager
        self._target_weekday = self._resolve_reset_weekday()
        self.last_reset_date: Optional[date] = None

    def cog_unload(self) -> None:
        if self.auto_reset_task.is_running():
            self.auto_reset_task.cancel()

    async def cog_load(self) -> None:
        if not self.auto_reset_task.is_running():
            self.auto_reset_task.start()

    def _resolve_reset_weekday(self) -> int:
        try:
            return WEEK_DAYS.index(AUTO_RESET_DAY)
        except ValueError:
            logging.warning("Invalid AUTO_RESET_DAY %s, defaulting to Monday", AUTO_RESET_DAY)
            return 0

    def _resolve_announcement_channel_id(self, guild: discord.Guild) -> Optional[int]:
        configured = self.config_store.get_announcement_channel(guild.id)
        if configured:
            return configured
        if ANNOUNCEMENT_CHANNEL_ID:
            return ANNOUNCEMENT_CHANNEL_ID
        return None

    @tasks.loop(minutes=30)
    async def auto_reset_task(self) -> None:
        await self._maybe_reset()

    @auto_reset_task.before_loop
    async def before_auto_reset_task(self) -> None:
        await self.bot.wait_until_ready()

    async def _maybe_reset(self) -> None:
        now = datetime.now()
        if now.weekday() != self._target_weekday or now.hour < AUTO_RESET_HOUR:
            return
        if self.last_reset_date == now.date():
            return

        cleared = self.availability_store.reset_all()
        self.last_reset_date = now.date()
        logging.info("Auto-reset availability for new week; cleared %d users", cleared)
        if self.role_manager:
            await self.role_manager.clear_roles_for_all_guilds()
        await self._announce_reset(cleared)

    async def _announce_reset(self, cleared: int) -> None:
        if cleared == 0:
            return
        for guild in self.bot.guilds:
            channel_id = self._resolve_announcement_channel_id(guild)
            if not channel_id:
                continue
            channel = guild.get_channel(channel_id)
            if not channel:
                continue
            try:
                await channel.send(
                    f"Weekly reset done: cleared availability for {cleared} players."
                    " Set your days with /availability set or the signup panel!"
                )
            except discord.HTTPException:
                logging.warning("Failed to announce reset in guild %s", guild.name)


class ValorantBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

        self.availability_store = AvailabilityStore()
        self.config_store = GuildConfigStore()

    async def setup_hook(self) -> None:  # type: ignore[override]
        role_manager = AvailabilityRoleManager(
            self, self.availability_store, self.config_store
        )
        await self.add_cog(role_manager)
        await self.add_cog(
            AvailabilityCog(
                self, self.availability_store, self.config_store, role_manager
            )
        )
        await self.add_cog(ScheduleCog(self, self.availability_store, self.config_store))
        await self.add_cog(ConfigCog(self, self.config_store))
        await self.add_cog(
            AutoResetter(
                self, self.availability_store, self.config_store, role_manager
            )
        )

        try:
            synced = await self.tree.sync()
            logging.info("Synced %d app commands", len(synced))
        except discord.HTTPException as exc:
            logging.error("Failed to sync commands: %s", exc)

    async def on_ready(self) -> None:  # type: ignore[override]
        assert self.user is not None
        logging.info("Logged in as %s", self.user)


async def create_bot() -> commands.Bot:
    return ValorantBot()


async def main() -> None:
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is required")

    bot = await create_bot()
    try:
        await bot.start(token)
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
