
import os
import json
import asyncio
import logging
import datetime
from pathlib import Path
from threading import Thread
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord.ext import commands, tasks
from flask import Flask


# ============================================================
# LOGGING AND CONFIGURATION
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("election_bot")

TOKEN = os.environ["TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

CONFIG_FILE = Path("election_config.json")

DEFAULT_TIMEZONE = "America/New_York"
DEFAULT_INTERVAL_DAYS = 14
DEFAULT_DURATION_MINUTES = 1440

MAX_CANDIDATES = 25
MAX_EMPTY_RESTARTS = 2

def env_int(name, default=0):
    try:
        return int(os.environ.get(name, default))
    except (ValueError, TypeError):
        return default


def load_config():
    defaults = {
        "guild_id": GUILD_ID,
        "poll_channel_id": env_int("CHANNEL_ID"),
        "results_channel_id": env_int("CHANNEL_ID"),
        "candidate_role_id": env_int("CANDIDATE_ROLE_ID"),
        "winner_role_id": env_int("WINNER_ROLE_ID"),
        "timezone": DEFAULT_TIMEZONE,
        "interval_days": DEFAULT_INTERVAL_DAYS,
        "duration_minutes": DEFAULT_DURATION_MINUTES,
        "next_run": None,
    }

    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text())
            defaults.update(saved)
        except Exception:
            logger.exception("Could not read saved configuration.")

    return defaults


config = load_config()


def save_config():
    CONFIG_FILE.write_text(json.dumps(config, indent=4))


def get_timezone():
    try:
        return ZoneInfo(config["timezone"])
    except (ZoneInfoNotFoundError, KeyError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def get_next_run():
    value = config.get("next_run")

    if value:
        try:
            return datetime.datetime.fromisoformat(value)
        except ValueError:
            pass

    now = datetime.datetime.now(get_timezone())
    next_run = now + datetime.timedelta(days=config["interval_days"])

    config["next_run"] = next_run.isoformat()
    save_config()

    return next_run


def schedule_next_run():
    now = datetime.datetime.now(get_timezone())
    next_run = now + datetime.timedelta(days=config["interval_days"])
    config["next_run"] = next_run.isoformat()
    save_config()
    return next_run


# ============================================================
# KEEP-ALIVE WEB SERVER
# ============================================================

app = Flask("")


@app.route("/")
def home():
    return "Election bot is running."


def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


def keep_alive():
    Thread(target=run_web, daemon=True).start()


# ============================================================
# DISCORD BOT
# ============================================================

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None
)

active_election_task = None
active_view = None
active_message = None


# ============================================================
# PERMISSIONS AND LOOKUPS
# ============================================================

def is_server_owner(ctx):
    return ctx.guild is not None and ctx.author.id == ctx.guild.owner_id


def get_configured_items(guild):
    poll_channel = guild.get_channel(config["poll_channel_id"])
    results_channel = guild.get_channel(config["results_channel_id"])
    candidate_role = guild.get_role(config["candidate_role_id"])
    winner_role = guild.get_role(config["winner_role_id"])

    return poll_channel, results_channel, candidate_role, winner_role


async def get_guild():
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        try:
            guild = await bot.fetch_guild(GUILD_ID)
        except discord.HTTPException:
            return None
    return guild


# ============================================================
# LIVE ELECTION VIEW
# ============================================================

class ElectionView(discord.ui.View):

    def __init__(self, candidates):
        super().__init__(timeout=None)

        self.candidates = {
            member.id: member for member in candidates
        }

        self.votes = {}
        self.voter_names = {}
        self.closed = False

        for candidate in candidates:
            button = discord.ui.Button(
                label=candidate.display_name[:80],
                style=discord.ButtonStyle.primary,
                custom_id=f"election_vote_{candidate.id}"
            )
            button.callback = self.make_callback(candidate.id)
            self.add_item(button)

    def make_embed(self, remaining=None):
        counts = {
            candidate_id: 0
            for candidate_id in self.candidates
        }

        for candidate_id in self.votes.values():
            if candidate_id in counts:
                counts[candidate_id] += 1

        ranked = sorted(
            self.candidates.items(),
            key=lambda item: (
                -counts[item[0]],
                item[1].display_name.casefold()
            )
        )

        if self.votes:
            highest = max(counts.values())
            leaders = [
                member.display_name
                for candidate_id, member in ranked
                if counts[candidate_id] == highest
            ]

            if len(leaders) == 1:
                leader_text = f"🏆 Currently leading: **{leaders[0]}**"
            else:
                leader_text = (
                    "⚖️ Currently tied: **"
                    + ", ".join(leaders)
                    + "**"
                )
        else:
            leader_text = "Nobody has voted yet!"

        standings = []

        for candidate_id, member in ranked:
            amount = counts[candidate_id]
            name = discord.utils.escape_markdown(
                member.display_name
            )

            marker = "🟢" if amount == max(
                counts.values(), default=0
            ) and amount > 0 else "▫️"

            standings.append(
                f"{marker} **{name}** — **{amount}** vote(s)"
            )

        standings_text = "\n".join(standings)

        voter_lines = []

        for user_id, candidate_id in self.votes.items():
            candidate = self.candidates.get(candidate_id)

            if candidate is None:
                continue

            voter = self.voter_names.get(
                user_id, f"User {user_id}"
            )

            voter_lines.append(
                f"• **{discord.utils.escape_markdown(voter)}** "
                f"→ **{discord.utils.escape_markdown(candidate.display_name)}**"
            )

        voter_text = (
            "\n".join(voter_lines)
            if voter_lines else "Nobody has voted yet."
        )

        if len(standings_text) > 1024:
            standings_text = standings_text[:1000] + "\n... shortened"

        if len(voter_text) > 1024:
            visible = []
            used = 0

            for line in voter_lines:
                if used + len(line) + 1 > 980:
                    break
                visible.append(line)
                used += len(line) + 1

            remaining_voters = len(voter_lines) - len(visible)
            voter_text = "\n".join(visible)

            if remaining_voters:
                voter_text += (
                    f"\n...and {remaining_voters} more vote(s)"
                )

        status = (
            "🟢 VOTING OPEN"
            if not self.closed
            else "🔒 VOTING CLOSED"
        )

        description = (
            f"{status}\n"
            f"{leader_text}\n"
        )

        if remaining is not None and not self.closed:
            seconds = max(0, int(remaining))
            hours, remainder = divmod(seconds, 3600)
            minutes, seconds = divmod(remainder, 60)

            description += (
                f"\n⏳ **Time remaining:** "
                f"{hours:02d}:{minutes:02d}:{seconds:02d}\n"
            )

        description += (
            "\nChoose a candidate below to vote. "
            "You may change your vote before voting closes."
        )

        embed = discord.Embed(
            title="🗳️ Server Leader Election",
            description=description,
            color=(
                discord.Color.gold()
                if not self.closed
                else discord.Color.dark_grey()
            )
        )

        embed.add_field(
            name="📊 Current standings",
            value=standings_text or "No candidates.",
            inline=False
        )

        embed.add_field(
            name="👥 Who voted for whom",
            value=voter_text,
            inline=False
        )

        embed.set_footer(
            text=(
                f"{len(self.votes)} vote(s) • "
                "Vote details are visible to everyone who can view this poll"
            )
        )

        return embed

    def make_callback(self, candidate_id):

        async def callback(interaction: discord.Interaction):

            if self.closed:
                await interaction.response.send_message(
                    "Voting has closed.",
                    ephemeral=True
                )
                return

            if candidate_id not in self.candidates:
                await interaction.response.send_message(
                    "This candidate is no longer available.",
                    ephemeral=True
                )
                return

            self.votes[interaction.user.id] = candidate_id
            self.voter_names[interaction.user.id] = (
                interaction.user.display_name
            )

            # Update the existing poll message, not a new message.
            await interaction.response.edit_message(
                embed=self.make_embed(),
                view=self
            )

        return callback


# ============================================================
# RESULTS AND WINNER ROLE
# ============================================================

async def announce_results(guild, results_channel, winner_role, view):
    if not view.votes:
        await results_channel.send(
            "No votes were cast. No winner was selected."
        )
        return

    counts = {}

    for candidate_id in view.votes.values():
        counts[candidate_id] = counts.get(candidate_id, 0) + 1

    highest = max(counts.values())

    top_candidates = [
        candidate_id
        for candidate_id, amount in counts.items()
        if amount == highest
    ]

    if len(top_candidates) != 1:
        await results_channel.send(
            "⚖️ The election ended in a tie. "
            "No winner role was changed."
        )
        return

    winner_id = top_candidates[0]
    winner = guild.get_member(winner_id)

    if winner is None:
        try:
            winner = await guild.fetch_member(winner_id)
        except discord.NotFound:
            await results_channel.send(
                "The winning candidate left the server. "
                "No role changes were made."
            )
            return

    role_errors = []

    # Refresh member cache before checking old role holders.
    try:
        await guild.chunk(cache=True)
    except discord.HTTPException:
        logger.warning("Could not refresh all server members.")

    for member in guild.members:
        if winner_role in member.roles and member.id != winner.id:
            try:
                await member.remove_roles(
                    winner_role,
                    reason="Election winner changed"
                )
            except discord.Forbidden:
                role_errors.append(
                    f"Could not remove the role from {member.display_name}."
                )
                break
            except discord.HTTPException:
                role_errors.append(
                    "Discord error removing an old winner role."
                )
                break

    try:
        if winner_role not in winner.roles:
            await winner.add_roles(
                winner_role,
                reason="Won the server election"
            )
    except discord.Forbidden:
        role_errors.append("Could not assign the winner role.")
    except discord.HTTPException:
        role_errors.append("Discord error assigning the winner role.")

    embed = discord.Embed(
        title="🏆 Election Results",
        description=(
            f"Congratulations to {winner.mention}!\n"
            f"**Winning votes:** {highest}"
        ),
        color=discord.Color.gold()
    )

    sorted_counts = sorted(
        counts.items(),
        key=lambda item: -item[1]
    )

    lines = []

    for candidate_id, amount in sorted_counts:
        member = guild.get_member(candidate_id)
        name = member.display_name if member else str(candidate_id)
        lines.append(f"• **{name}** — {amount} vote(s)")

    embed.add_field(
        name="Final standings",
        value="\n".join(lines)[:1024],
        inline=False
    )

    if role_errors:
        embed.add_field(
            name="⚠️ Role assignment warning",
            value=(
                "\n".join(role_errors)
                + "\n\nMake sure the bot has Manage Roles and "
                "its role is above the Winner role."
            ),
            inline=False
        )
    else:
        embed.add_field(
            name="Winner role",
            value=f"✅ {winner_role.name} assigned to {winner.mention}",
            inline=False
        )

    await results_channel.send(embed=embed)


# ============================================================
# RUN AN ELECTION
# ============================================================

async def run_election(guild, duration_seconds):

    global active_view, active_message

    poll_channel, results_channel, candidate_role, winner_role = (
        get_configured_items(guild)
    )

    if not all([
        poll_channel,
        results_channel,
        candidate_role,
        winner_role
    ]):
        logger.error("Election configuration is incomplete.")
        return

    try:
        try:
            await guild.chunk(cache=True)
        except discord.HTTPException:
            pass

        candidates = [
            member
            for member in guild.members
            if candidate_role in member.roles and not member.bot
        ]

        if not candidates:
            await poll_channel.send(
                "⚠️ Election skipped: no members have the Candidate role."
            )
            return

        if len(candidates) > MAX_CANDIDATES:
            await poll_channel.send(
                f"⚠️ There are {len(candidates)} candidates. "
                f"Discord supports at most {MAX_CANDIDATES} voting buttons."
            )
            return

        view = ElectionView(candidates)
        active_view = view

        remaining = duration_seconds

        embed = view.make_embed(remaining)

        msg = await poll_channel.send(
            embed=embed,
            view=view
        )

        active_message = msg

        logger.info("Election started in %s.", guild.name)

        # Refresh countdown and scoreboard every 60 seconds.
        while remaining > 0:
            await asyncio.sleep(min(60, remaining))
            remaining = max(0, remaining - 60)

            if remaining > 0:
                try:
                    await msg.edit(
                        embed=view.make_embed(remaining),
                        view=view
                    )
                except discord.HTTPException:
                    logger.exception("Could not update poll message.")

        view.closed = True

        for item in view.children:
            item.disabled = True

        try:
            await msg.edit(
                embed=view.make_embed(),
                view=view
            )
        except discord.HTTPException:
            logger.exception("Could not close poll message.")

        view.stop()

        await announce_results(
            guild,
            results_channel,
            winner_role,
            view
        )

    except asyncio.CancelledError:
        logger.info("Election stopped by an administrator.")

        if active_view:
            active_view.closed = True

            for item in active_view.children:
                item.disabled = True

            active_view.stop()

        if active_message and active_view:
            try:
                await active_message.edit(
                    embed=active_view.make_embed(),
                    view=active_view
                )
            except discord.HTTPException:
                pass

        if poll_channel:
            await poll_channel.send(
                "🛑 Election stopped by an administrator. "
                "No winner was selected."
            )

        raise

    except Exception:
        logger.exception("Unexpected election error.")

        try:
            await poll_channel.send(
                "🚨 An election error occurred. Check the bot logs."
            )
        except Exception:
            pass

    finally:
        active_view = None
        active_message = None


# ============================================================
# LAUNCH ELECTION
# ============================================================

def launch_election(guild, duration_seconds):

    global active_election_task

    if active_election_task and not active_election_task.done():
        return False

    active_election_task = asyncio.create_task(
        run_election(guild, duration_seconds)
    )

    return True


# ============================================================
# DASHBOARD: SCHEDULE SETTINGS
# ============================================================

class ScheduleModal(discord.ui.Modal, title="Election Schedule Settings"):

    interval = discord.ui.TextInput(
        label="Repeat every how many days?",
        placeholder="14",
        default=str(config["interval_days"]),
        max_length=5
    )

    date = discord.ui.TextInput(
        label="Next election date (YYYY-MM-DD)",
        placeholder="2026-12-14",
        required=True,
        max_length=10
    )

    time = discord.ui.TextInput(
        label="Start time (24-hour HH:MM)",
        placeholder="23:00",
        required=True,
        max_length=5
    )

    timezone = discord.ui.TextInput(
        label="Timezone",
        placeholder="America/New_York",
        default=config["timezone"],
        max_length=60
    )

    duration = discord.ui.TextInput(
        label="Voting duration in minutes",
        placeholder="1440",
        default=str(config["duration_minutes"]),
        max_length=7
    )

    async def on_submit(self, interaction):

        try:
            interval_days = int(self.interval.value)
            duration_minutes = int(self.duration.value)

            if interval_days < 1 or interval_days > 3650:
                raise ValueError("Repeat interval must be between 1 and 3650 days.")

            if duration_minutes < 1:
                raise ValueError("Duration must be at least one minute.")

            timezone = ZoneInfo(self.timezone.value.strip())

            date_value = datetime.date.fromisoformat(self.date.value.strip())
            time_value = datetime.time.fromisoformat(self.time.value.strip())

            next_run = datetime.datetime.combine(
                date_value,
                time_value,
                tzinfo=timezone
            )

            if next_run <= datetime.datetime.now(timezone):
                raise ValueError("The next election date and time must be in the future.")

            config["interval_days"] = interval_days
            config["duration_minutes"] = duration_minutes
            config["timezone"] = self.timezone.value.strip()
            config["next_run"] = next_run.isoformat()

            save_config()

            await interaction.response.send_message(
                "✅ Election schedule saved!\n"
                f"Repeats every **{interval_days} days**.\n"
                f"Next election: **{next_run.strftime('%B %d, %Y at %I:%M %p %Z')}**.\n"
                f"Voting duration: **{duration_minutes} minutes**.",
                ephemeral=True
            )

        except (ValueError, ZoneInfoNotFoundError) as error:
            await interaction.response.send_message(
                f"⚠️ Invalid setting: {error}",
                ephemeral=True
            )


# ============================================================
# DASHBOARD: CHANNEL SELECTORS
# ============================================================

class PollChannelSelect(discord.ui.ChannelSelect):

    def __init__(self):
        super().__init__(
            placeholder="Choose the poll channel",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction):
        config["poll_channel_id"] = self.values[0].id
        save_config()

        await interaction.response.send_message(
            f"✅ Poll channel set to {self.values[0].mention}.",
            ephemeral=True
        )


class ResultsChannelSelect(discord.ui.ChannelSelect):

    def __init__(self):
        super().__init__(
            placeholder="Choose the results channel",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction):
        config["results_channel_id"] = self.values[0].id
        save_config()

        await interaction.response.send_message(
            f"✅ Results channel set to {self.values[0].mention}.",
            ephemeral=True
        )


# ============================================================
# DASHBOARD: ROLE SELECTORS
# ============================================================

class CandidateRoleSelect(discord.ui.RoleSelect):

    def __init__(self):
        super().__init__(
            placeholder="Choose the Candidate role",
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction):
        config["candidate_role_id"] = self.values[0].id
        save_config()

        await interaction.response.send_message(
            f"✅ Candidate role set to **{self.values[0].name}**.",
            ephemeral=True
        )


class WinnerRoleSelect(discord.ui.RoleSelect):

    def __init__(self):
        super().__init__(
            placeholder="Choose the Winner role",
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction):
        config["winner_role_id"] = self.values[0].id
        save_config()

        await interaction.response.send_message(
            f"✅ Winner role set to **{self.values[0].name}**.",
            ephemeral=True
        )


# ============================================================
# DASHBOARD
# ============================================================

class ElectionDashboard(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=300)

        self.add_item(PollChannelSelect())
        self.add_item(ResultsChannelSelect())
        self.add_item(CandidateRoleSelect())
        self.add_item(WinnerRoleSelect())

    @discord.ui.button(
        label="Schedule & Timer",
        style=discord.ButtonStyle.primary,
        emoji="⏰",
        row=4
    )
    async def schedule_button(self, interaction, button):
        await interaction.response.send_modal(ScheduleModal())

    @discord.ui.button(
        label="Start Test Poll",
        style=discord.ButtonStyle.success,
        emoji="🧪",
        row=4
    )
    async def test_button(self, interaction, button):
        if not is_server_owner_from_interaction(interaction):
            await interaction.response.send_message(
                "Only the server owner can use this dashboard.",
                ephemeral=True
            )
            return

        guild = interaction.guild

        if launch_election(guild, 30):
            await interaction.response.send_message(
                "🧪 A 30-second test poll has started.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "An election is already running.",
                ephemeral=True
            )

    @discord.ui.button(
        label="Start Election Now",
        style=discord.ButtonStyle.primary,
        emoji="▶️",
        row=4
    )
    async def start_button(self, interaction, button):
        if not is_server_owner_from_interaction(interaction):
            await interaction.response.send_message(
                "Only the server owner can use this dashboard.",
                ephemeral=True
            )
            return

        duration = config["duration_minutes"] * 60

        if launch_election(interaction.guild, duration):
            await interaction.response.send_message(
                "▶️ Election started using your configured voting duration.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "An election is already running.",
                ephemeral=True
            )

    @discord.ui.button(
        label="Stop Election",
        style=discord.ButtonStyle.danger,
        emoji="🛑",
        row=4
    )
    async def stop_button(self, interaction, button):
        global active_election_task

        if not is_server_owner_from_interaction(interaction):
            await interaction.response.send_message(
                "Only the server owner can use this dashboard.",
                ephemeral=True
            )
            return

        if active_election_task and not active_election_task.done():
            active_election_task.cancel()

            await interaction.response.send_message(
                "🛑 Election stop requested.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "No election is currently running.",
                ephemeral=True
            )


def is_server_owner_from_interaction(interaction):
    return (
        interaction.guild is not None
        and interaction.user.id == interaction.guild.owner_id
    )


# ============================================================
# !HELP DASHBOARD
# ============================================================

@bot.command(name="help")
async def help_command(ctx):

    if not is_server_owner(ctx):
        await ctx.send(
            "Only the server owner can open the election dashboard."
        )
        return

    next_run = get_next_run()
    poll_channel, results_channel, candidate_role, winner_role = (
        get_configured_items(ctx.guild)
    )

    embed = discord.Embed(
        title="🛠️ Election Control Panel",
        description=(
            "Manage your election using the menus and buttons below.\n\n"
            "**Schedule:**\n"
            f"Repeats every **{config['interval_days']} days**\n"
            f"Next election: **{next_run.strftime('%b %d, %Y %I:%M %p %Z')}**\n"
            f"Voting duration: **{config['duration_minutes']} minutes**\n\n"
            "**Channels:**\n"
            f"Poll: {poll_channel.mention if poll_channel else 'Not configured'}\n"
            f"Results: {results_channel.mention if results_channel else 'Not configured'}\n\n"
            "**Roles:**\n"
            f"Candidate: {candidate_role.name if candidate_role else 'Not configured'}\n"
            f"Winner: {winner_role.name if winner_role else 'Not configured'}"
        ),
        color=discord.Color.blurple()
    )

    await ctx.send(
        embed=embed,
        view=ElectionDashboard()
    )


# ============================================================
# OPTIONAL COMMANDS
# ============================================================

@bot.command()
async def testpoll(ctx):
    if not is_server_owner(ctx):
        return

    if launch_election(ctx.guild, 30):
        await ctx.send("🧪 Started a 30-second test election.")
    else:
        await ctx.send("An election is already running.")


@bot.command()
async def startelection(ctx):
    if not is_server_owner(ctx):
        return

    if launch_election(
        ctx.guild,
        config["duration_minutes"] * 60
    ):
        await ctx.send("▶️ Election started.")
    else:
        await ctx.send("An election is already running.")


@bot.command()
async def stopelection(ctx):
    global active_election_task

    if not is_server_owner(ctx):
        return

    if active_election_task and not active_election_task.done():
        active_election_task.cancel()
        await ctx.send("🛑 Election stop requested.")
    else:
        await ctx.send("No election is currently running.")


@bot.command()
async def debug(ctx):
    if not is_server_owner(ctx):
        return

    guild = ctx.guild
    poll, results, candidate, winner = get_configured_items(guild)

    bot_member = guild.get_member(bot.user.id)

    hierarchy_ok = bool(
        bot_member
        and winner
        and bot_member.top_role > winner
    )

    manage_roles = bool(
        bot_member
        and bot_member.guild_permissions.manage_roles
    )

    next_run = get_next_run()

    embed = discord.Embed(
        title="🛠️ Election Bot Debug",
        color=discord.Color.blue()
    )

    embed.add_field(
        name="Poll channel",
        value=poll.mention if poll else "Missing",
        inline=True
    )

    embed.add_field(
        name="Results channel",
        value=results.mention if results else "Missing",
        inline=True
    )

    embed.add_field(
        name="Candidate role",
        value=candidate.name if candidate else "Missing",
        inline=True
    )

    embed.add_field(
        name="Winner role",
        value=winner.name if winner else "Missing",
        inline=True
    )

    embed.add_field(
        name="Bot can assign roles",
        value=(
            "✅ Yes"
            if manage_roles and hierarchy_ok
            else "❌ Check Manage Roles and role hierarchy"
        ),
        inline=False
    )

    embed.add_field(
        name="Next election",
        value=next_run.strftime("%A, %B %d at %I:%M %p %Z"),
        inline=False
    )

    await ctx.send(embed=embed)


# ============================================================
# AUTOMATIC REPEATING SCHEDULER
# ============================================================

@tasks.loop(seconds=30)
async def scheduled_election():

    try:
        next_run = get_next_run()
        now = datetime.datetime.now(next_run.tzinfo)

        if now >= next_run:
            guild = bot.get_guild(GUILD_ID)

            if guild is None:
                logger.error("Scheduled election: server not found.")
                return

            # Advance the schedule before starting the poll.
            # This prevents the scheduler from launching it repeatedly.
            schedule_next_run()

            duration = config["duration_minutes"] * 60

            if not launch_election(guild, duration):
                logger.warning(
                    "Scheduled election was due, but another election is active."
                )

    except Exception:
        logger.exception("Scheduler error.")


@scheduled_election.before_loop
async def before_scheduled_election():
    await bot.wait_until_ready()


# ============================================================
# BOT READY
# ============================================================

@bot.event
async def on_ready():

    logger.info(
        "Logged in as %s (ID: %s)",
        bot.user,
        bot.user.id
    )

    if not scheduled_election.is_running():
        scheduled_election.start()
        logger.info("Election scheduler started.")


# ============================================================
# START
# ============================================================

keep_alive()
bot.run(TOKEN)
