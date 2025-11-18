# Valorant Team Discord Bot Scheduler

A lightweight Discord bot for managing Valorant Premier nights and 5v5 scrim availability. Players can set which days they can play, and staff can generate and post a weekly schedule that pings an availability role.

## Features
- Slash commands for collecting player availability (with optional Team A/B tagging).
- Automatic weekly summary showing Premier readiness (needs 5 from a single team) and scrim readiness (needs 10 total).
- Posts the schedule into an announcement channel and optionally pings a configured role.
- Automatic scrim reminders that fire 30 minutes before the configured start time when 10+ players are available.
- Optional daily role sync that grants an "available" role to everyone signed up for the current day and removes it after.
- Quick signup select menu with a built-in clear button so players can update availability without typing commands.
- Optional weekly auto-reset so availability clears on a chosen day/hour (default: Mondays at 8 AM server time).
- Simple JSON persistence—no external database required.

## Premier & Scrim Rules
- Premier defaults to **Wednesday–Sunday** with the windows listed below, but you can override each day with `/config premierwindow`.
  - **Wednesday/Thursday/Sunday:** 7–8 PM ET (default)
  - **Friday/Saturday:** 8–9 PM ET (default)
- Scrims target **the configured start time** (default 7 PM in your configured timezone) on any day where 10+ players are available.

## Quick start (Version 2)
Follow these steps to get the bot running—no code moves required. Run all commands from the repository root.

### 1) Prepare Discord resources
1. In the [Discord Developer Portal](https://discord.com/developers/applications), create an application and add a bot user.
2. Under **Bot → Privileged Gateway Intents**, enable **Server Members Intent** (needed to read member roles for Team A/B detection).
3. Under **OAuth2 → URL Generator**, select `bot` and `applications.commands`, then invite the bot to your server with permission to read messages, send messages, and manage messages in the announcement channel.

### 2) Configure environment variables
Create a `.env` file (or export variables in your shell) with your token and optional defaults for announcement/ping targets:
```bash
DISCORD_BOT_TOKEN=YOUR_TOKEN
# Optional fallbacks if you prefer environment defaults over using /config commands
ANNOUNCEMENT_CHANNEL_ID=123456789012345678
AVAILABLE_ROLE_ID=234567890123456789
# Optional: map Team A/B to Discord role IDs (e.g., if your roles are named "Group A/B")
TEAM_A_ROLE_ID=345678901234567890
TEAM_B_ROLE_ID=456789012345678901
# Optional: weekly availability reset cadence (server-local time)
AUTO_RESET_DAY=monday
AUTO_RESET_HOUR=8
# Optional: default scrim start time and timezone (used unless overridden via /config scrimtime)
DEFAULT_SCRIM_START_TIME="7:00 PM"
SCRIM_TIMEZONE="America/New_York"
```

### 3) Install and run
1. Use Python 3.10+.
2. Install dependencies and start the bot:
   ```bash
   pip install -r requirements.txt
   python bot.py
   ```
3. Leave the process running (or use a process manager like `pm2`, `screen`, or systemd on your host).

### 4) Configure inside Discord
Once the bot is online, run these slash commands in your server:
- `/config announcement channel:<#channel>` — channel where schedules should be posted.
- `/config pingrole role:<@role>` — role to ping when posting schedules (optional; overrides `AVAILABLE_ROLE_ID`).
- `/config teamroles [team_a:<@role>] [team_b:<@role>]` — explicitly map Team A/B to role IDs (useful if your roles are named differently, e.g., **Group A/B**).
- Players run `/availability set days:<wed, thu, sat> [team:<A|B>]` to register for the week.
- Post a quick signup UI with `/availability panel` so anyone can click-select their days or clear their week.
- Staff run `/schedule preview` to see availability and `/schedule post` to send the embed (and ping) to announcements.

### 5) Data and resets
- JSON data lives under `data/` (created automatically). Delete the folder to reset availability and config.
- Availability auto-clears weekly on `AUTO_RESET_DAY`/`AUTO_RESET_HOUR` and announces the reset in the announcement channel if configured.
- Staff can manually clear the week with `/availability resetweek`.

## Commands
- `/availability set days:<wed, thu, sat> [team:<A|B>]` — save your availability (team inferred from configured roles if omitted).
- `/availability mine` — view your saved days.
- `/availability clear` — remove your availability.
- `/availability day day:<weekday>` — list who is available on a given day.
- `/availability panel` — post a select menu + clear button for quick signups in-channel.
- `/availability resetweek` — (admins) clear all saved availability for a fresh week.
- `/schedule preview` — view the current Premier/scrim readiness summary.
- `/schedule post` — send the schedule to the configured announcement channel and ping the availability role if set.
- `/config announcement channel:<#channel>` — set the channel where schedules are posted.
- `/config pingrole role:<@role>` — set the role to mention when posting schedules.
- `/config availablerole role:<@role>` — set the role to grant players who are marked available today.
- `/config teamroles [team_a:<@role>] [team_b:<@role>]` — set Team A/B role IDs for accurate detection.
- `/config scrimtime day:<weekday> time:<19:00|7:00 PM> [timezone:<America/New_York>]` — set the scrim start time and optional timezone for a specific day.
- `/config premierwindow day:<weekday> window:<7:00-8:00 PM ET>` — override the Premier window text for a given day.

## Data Storage
Availability and guild configuration are stored as JSON under `data/`. The directory is created automatically on first run; the files can be safely deleted to reset state.

## Notes & Ideas
- Team detection prioritizes configured role IDs (`/config teamroles` or `TEAM_A_ROLE_ID`/`TEAM_B_ROLE_ID`). Users can also override with the `team` argument if needed.
- The schedule output highlights whether each Premier slot has enough from Team A or B and how many more players are needed for scrims.
- Quick signup select menus and optional weekly auto-resets help the roster stay current without manual cleanup.
