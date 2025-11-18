from __future__ import annotations

import logging
import os
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from scheduler import ScheduleBuilder, WEEK_DAYS
from storage import AvailabilityStore, GuildConfigStore

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ANNOUNCEMENT_CHANNEL_ID = os.getenv("ANNOUNCEMENT_CHANNEL_ID")
AVAILABLE_ROLE_ID = os.getenv("AVAILABLE_ROLE_ID")
TEAM_A_ROLE_NAMES = {"team a", "a"}
TEAM_B_ROLE_NAMES = {"team b", "b"}


def normalize_day(day: str) -> Optional[str]:
    day = day.strip().lower()
    for candidate in WEEK_DAYS:
        if candidate.startswith(day):
            return candidate
    return None


def parse_days(raw: str) -> List[str]:
    segments = [segment.strip() for segment in raw.split(",")]
    normalized = [normalize_day(segment) for segment in segments if segment]
    return [day for day in normalized if day]


def infer_team(member: discord.Member, fallback: Optional[str]) -> Optional[str]:
    if fallback:
        return fallback.upper()
    role_names = {role.name.lower() for role in member.roles}
    if role_names & TEAM_A_ROLE_NAMES:
        return "A"
    if role_names & TEAM_B_ROLE_NAMES:
        return "B"
    return None


def format_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.brand_red())


class AvailabilityCog(commands.Cog):
    def __init__(self, bot: commands.Bot, availability_store: AvailabilityStore) -> None:
        self.bot = bot
        self.availability_store = availability_store

    availability = app_commands.Group(name="availability", description="Manage Valorant availability")

    @availability.command(name="set", description="Set the days you can play this week")
    @app_commands.describe(days="Comma-separated days (e.g. wed, thu, sat)", team="Optional team override (A or B)")
    async def availability_set(self, interaction: discord.Interaction, days: str, team: Optional[str] = None) -> None:
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

        normalized_team = infer_team(member, team)
        self.availability_store.set_availability(
            user_id=member.id,
            display_name=member.display_name,
            team=normalized_team,
            days=normalized_days,
        )

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
            return int(ANNOUNCEMENT_CHANNEL_ID)
        return None

    def _resolve_ping_mention(self, guild: discord.Guild) -> Optional[str]:
        configured_role_id = self.config_store.get_ping_role(guild.id) or (
            int(AVAILABLE_ROLE_ID) if AVAILABLE_ROLE_ID else None
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


def build_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.members = True
    bot = commands.Bot(command_prefix="!", intents=intents)

    availability_store = AvailabilityStore()
    config_store = GuildConfigStore()

    bot.add_cog(AvailabilityCog(bot, availability_store))
    bot.add_cog(ScheduleCog(bot, availability_store, config_store))
    bot.add_cog(ConfigCog(bot, config_store))

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

