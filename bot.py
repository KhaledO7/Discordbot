from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from zoneinfo import ZoneInfo

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
DEFAULT_SCRIM_START_TIME = os.getenv("DEFAULT_SCRIM_START_TIME", "7:00 PM")
SCRIM_TIMEZONE = os.getenv("SCRIM_TIMEZONE", "America/New_York")

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


def parse_time_string(raw: str) -> Optional[time]:
    cleaned = raw.strip()
    for fmt in ("%H:%M", "%I:%M %p", "%I %p", "%I:%M%p"):
        try:
            return datetime.strptime(cleaned, fmt).time()
        except ValueError:
            continue
    return None


def format_time_with_zone(target_date: date, scrim_time: time, tz: ZoneInfo) -> str:
    start_dt = datetime.combine(target_date, scrim_time, tzinfo=tz)
    return start_dt.strftime("%I:%M %p %Z").lstrip("0")


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


def resolve_scrim_timezone(config_store: GuildConfigStore, guild_id: int) -> ZoneInfo:
    timezone_name = config_store.get_scrim_timezone(guild_id) or SCRIM_TIMEZONE
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        logging.warning("Invalid timezone %s, defaulting to UTC", timezone_name)
        return ZoneInfo("UTC")


def resolve_scrim_time(config_store: GuildConfigStore, guild_id: int, day: str) -> time:
    raw_time = config_store.get_scrim_time(guild_id, day) or DEFAULT_SCRIM_START_TIME
    parsed = parse_time_string(raw_time)
    if parsed:
        return parsed
    logging.warning("Invalid scrim time '%s'; defaulting to 19:00", raw_time)
    return time(hour=19, minute=0)


def resolve_announcement_channel_id(
    guild: discord.Guild, config_store: GuildConfigStore
) -> Optional[int]:
    configured = config_store.get_announcement_channel(guild.id)
    if configured:
        return configured
    if ANNOUNCEMENT_CHANNEL_ID:
        return ANNOUNCEMENT_CHANNEL_ID
    return None


def resolve_ping_mention(guild: discord.Guild, config_store: GuildConfigStore) -> Optional[str]:
    configured_role_id = config_store.get_ping_role(guild.id) or (
        AVAILABLE_ROLE_ID if AVAILABLE_ROLE_ID else None
    )
    if not configured_role_id:
        return None
    role = guild.get_role(configured_role_id)
    if role:
        return role.mention
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

        channel_id = resolve_announcement_channel_id(
            interaction.guild, self.config_store
        )
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

        mention = resolve_ping_mention(interaction.guild, self.config_store)
        content = f"{mention} Weekly schedule updated!" if mention else "Weekly schedule updated!"
        await channel.send(content=content, embed=embed)
        await interaction.response.send_message("Schedule posted!", ephemeral=True)


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
        description="Set the scrim start time for a specific weekday (24h or AM/PM)",
    )
    @app_commands.describe(
        day="Day of week (e.g. friday)",
        time_of_day="Start time like 19:00 or 7:00 PM",
        timezone="Optional IANA timezone (e.g. America/New_York)",
    )
    async def config_scrim_time(
        self,
        interaction: discord.Interaction,
        day: str,
        time_of_day: str,
        timezone: Optional[str] = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        normalized_day = normalize_day(day)
        if not normalized_day:
            await interaction.response.send_message("Provide a valid weekday.", ephemeral=True)
            return

        parsed_time = parse_time_string(time_of_day)
        if not parsed_time:
            await interaction.response.send_message(
                "Provide a valid start time like `19:00` or `7:00 PM`.",
                ephemeral=True,
            )
            return

        tz = resolve_scrim_timezone(self.config_store, interaction.guild.id)
        if timezone:
            try:
                tz = ZoneInfo(timezone)
            except Exception:
                await interaction.response.send_message(
                    "Invalid timezone. Use an IANA timezone like `America/New_York`.",
                    ephemeral=True,
                )
                return
            self.config_store.set_scrim_timezone(interaction.guild.id, timezone)

        self.config_store.set_scrim_time(interaction.guild.id, normalized_day, time_of_day)
        formatted_time = format_time_with_zone(date.today(), parsed_time, tz)
        await interaction.response.send_message(
            f"Scrim time for **{normalized_day.title()}** set to {formatted_time}.",
            ephemeral=True,
        )


class AutoResetter(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        availability_store: AvailabilityStore,
        config_store: GuildConfigStore,
    ) -> None:
        self.bot = bot
        self.availability_store = availability_store
        self.config_store = config_store
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


class ScrimNotifier(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        availability_store: AvailabilityStore,
        config_store: GuildConfigStore,
    ) -> None:
        self.bot = bot
        self.availability_store = availability_store
        self.config_store = config_store
        self.sent_notifications: Dict[int, Dict[str, date]] = {}

    def cog_unload(self) -> None:
        if self.scrim_check_task.is_running():
            self.scrim_check_task.cancel()

    async def cog_load(self) -> None:
        if not self.scrim_check_task.is_running():
            self.scrim_check_task.start()

    @tasks.loop(minutes=5)
    async def scrim_check_task(self) -> None:
        for guild in self.bot.guilds:
            await self._maybe_ping_guild(guild)

    @scrim_check_task.before_loop
    async def before_scrim_check_task(self) -> None:
        await self.bot.wait_until_ready()

    async def _maybe_ping_guild(self, guild: discord.Guild) -> None:
        tz = resolve_scrim_timezone(self.config_store, guild.id)
        now = datetime.now(tz)
        day = WEEK_DAYS[now.weekday()]
        start_time = resolve_scrim_time(self.config_store, guild.id, day)
        start_dt = datetime.combine(now.date(), start_time, tzinfo=tz)
        notify_dt = start_dt - timedelta(minutes=30)

        if not (notify_dt <= now < start_dt):
            return
        if self._already_notified(guild.id, day, now.date()):
            return

        users = self.availability_store.users_for_day(day)
        if len(users) < 10:
            return

        channel_id = resolve_announcement_channel_id(guild, self.config_store)
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if not channel:
            return

        mention = resolve_ping_mention(guild, self.config_store)
        mention_prefix = f"{mention} " if mention else ""
        formatted_time = format_time_with_zone(now.date(), start_time, tz)
        try:
            await channel.send(
                f"{mention_prefix}Scrim ready for **{day.title()}**! {len(users)} players signed up. "
                f"Scrim starts at {formatted_time}. This is your 30-minute warning."
            )
            self._mark_notified(guild.id, day, now.date())
        except discord.HTTPException:
            logging.warning("Failed to post scrim reminder in guild %s", guild.name)

    def _already_notified(self, guild_id: int, day: str, today: date) -> bool:
        return self.sent_notifications.get(guild_id, {}).get(day) == today

    def _mark_notified(self, guild_id: int, day: str, today: date) -> None:
        self.sent_notifications.setdefault(guild_id, {})[day] = today


class ValorantBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

        self.availability_store = AvailabilityStore()
        self.config_store = GuildConfigStore()

    async def setup_hook(self) -> None:  # type: ignore[override]
        await self.add_cog(AvailabilityCog(self, self.availability_store, self.config_store))
        await self.add_cog(ScheduleCog(self, self.availability_store, self.config_store))
        await self.add_cog(ConfigCog(self, self.config_store))
        await self.add_cog(AutoResetter(self, self.availability_store, self.config_store))
        await self.add_cog(ScrimNotifier(self, self.availability_store, self.config_store))

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
