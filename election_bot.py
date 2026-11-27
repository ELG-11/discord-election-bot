
import os
import re
import json
import math
import asyncio
import logging
import datetime
from pathlib import Path
from threading import Thread
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from flask import Flask


# ============================================================
# CONFIGURATION
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
DEFAULT_DURATION_HOURS = 24
DEFAULT_TIE_DURATION_HOURS = 1

MAX_CANDIDATES = 25


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
        "duration_hours": DEFAULT_DURATION_HOURS,
        "tie_duration_hours": DEFAULT_TIE_DURATION_HOURS,

        "interval_seconds": DEFAULT_INTERVAL_DAYS * 86400,
        "duration_seconds": DEFAULT_DURATION_HOURS * 3600,
        "tie_duration_seconds": DEFAULT_TIE_DURATION_HOURS * 3600,

        "auto_start": True,
        "next_run": None,
    }

    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text())
            defaults.update(saved)

            # Migrate older configuration files automatically.
            if "interval_seconds" not in saved:
                defaults["interval_seconds"] = (
                    defaults["interval_days"] * 86400
                )

            if "duration_seconds" not in saved:
                defaults["duration_seconds"] = (
                    defaults["duration_hours"] * 3600
                )

            if "tie_duration_seconds" not in saved:
                defaults["tie_duration_seconds"] = (
                    defaults["tie_duration_hours"] * 3600
                )

        except Exception:
            logger.exception("Could not load configuration.")

    return defaults


config = load_config()


def save_config():
    CONFIG_FILE.write_text(json.dumps(config, indent=4))


def parse_time(value, default_unit, maximum_seconds):
    """
    Accepts:
    5m = 5 minutes
    2h = 2 hours
    14d = 14 days

    Plain numbers use the field's default unit.
    """

    value = value.strip().lower().replace(" ", "")

    match = re.fullmatch(r"(\d+)(m|h|d)?", value)

    if not match:
        raise ValueError(
            "Use a number followed by m, h, or d. "
            "Examples: 5m, 2h, 14d."
        )

    amount = int(match.group(1))
    unit = match.group(2) or default_unit

    multipliers = {
        "m": 60,
        "h": 3600,
        "d": 86400,
    }

    seconds = amount * multipliers[unit]

    if seconds < 60:
        raise ValueError("The minimum setting is 1 minute.")

    if seconds > maximum_seconds:
        raise ValueError("That duration is too long.")

    return seconds


def format_time(seconds):
    seconds = int(seconds)

    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)

    parts = []

    if days:
        parts.append(f"{days}d")

    if hours:
        parts.append(f"{hours}h")

    if minutes and not days:
        parts.append(f"{minutes}m")

    return " ".join(parts) if parts else "1m"


def get_timezone():
    try:
        return ZoneInfo(config["timezone"])
    except Exception:
        return ZoneInfo(DEFAULT_TIMEZONE)


def get_next_run():
    value = config.get("next_run")

    if value:
        try:
            return datetime.datetime.fromisoformat(value)
        except ValueError:
            pass

    next_run = (
        datetime.datetime.now(get_timezone())
        + datetime.timedelta(seconds=config["interval_seconds"])
    )

    config["next_run"] = next_run.isoformat()
    save_config()

    return next_run


def schedule_next_run():
    next_run = (
        datetime.datetime.now(get_timezone())
        + datetime.timedelta(seconds=config["interval_seconds"])
    )

    config["next_run"] = next_run.isoformat()
    save_config()

    return next_run


# ============================================================
# KEEP-ALIVE
# ============================================================

app = Flask("")


@app.route("/")
def home():
    return "Election bot is running."


def keep_alive():
    port = int(os.environ.get("PORT", 8080))

    Thread(
        target=lambda: app.run(host="0.0.0.0", port=port),
        daemon=True
    ).start()


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
# PERMISSIONS AND CONFIGURATION LOOKUPS
# ============================================================

def is_server_owner(ctx):
    return (
        ctx.guild is not None
        and ctx.author.id == ctx.guild.owner_id
    )


def is_server_owner_from_interaction(interaction):
    return (
        interaction.guild is not None
        and interaction.user.id == interaction.guild.owner_id
    )


def get_configured_items(guild):
    return (
        guild.get_channel(config["poll_channel_id"]),
        guild.get_channel(config["results_channel_id"]),
        guild.get_role(config["candidate_role_id"]),
        guild.get_role(config["winner_role_id"])
    )


# ============================================================
# ELECTION POLL
# ============================================================

class ElectionView(discord.ui.View):

    def __init__(self, candidates, duration_seconds, round_name="Election"):
        super().__init__(timeout=None)

        self.candidates = {
            member.id: member for member in candidates
        }

        self.votes = {}
        self.closed = False
        self.round_name = round_name

        self.end_time = (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=duration_seconds)
        )

        for candidate in candidates:
            button = discord.ui.Button(
                label=candidate.display_name[:80],
                style=discord.ButtonStyle.primary,
                custom_id=f"election_vote_{candidate.id}"
            )

            button.callback = self.make_callback(candidate.id)
            self.add_item(button)

    def get_counts(self):
        counts = {
            candidate_id: 0
            for candidate_id in self.candidates
        }

        for candidate_id in self.votes.values():
            if candidate_id in counts:
                counts[candidate_id] += 1

        return counts

    def get_remaining(self):
        remaining = (
            self.end_time
            - datetime.datetime.now(datetime.timezone.utc)
        ).total_seconds()

        return max(0, int(remaining))

    def get_leaders(self):
        counts = self.get_counts()
        highest = max(counts.values(), default=0)

        if highest == 0:
            return []

        return [
            candidate_id
            for candidate_id, amount in counts.items()
            if amount == highest
        ]

    def make_embed(self):
        counts = self.get_counts()

        ranked = sorted(
            self.candidates.items(),
            key=lambda item: (
                -counts[item[0]],
                item[1].display_name.casefold()
            )
        )

        highest = max(counts.values(), default=0)

        if highest == 0:
            leader_text = "Waiting for the first vote."
        else:
            leaders = [
                member.display_name
                for candidate_id, member in ranked
                if counts[candidate_id] == highest
            ]

            if len(leaders) == 1:
                leader_text = f"🏆 **{leaders[0]}** is leading"
            else:
                leader_text = "⚖️ **Tie:** " + ", ".join(leaders)

        standings = []
        total_votes = len(self.votes)

        for candidate_id, member in ranked:
            amount = counts[candidate_id]

            marker = (
                "🟢" if amount == highest and amount > 0
                else "▫️"
            )

            name = discord.utils.escape_markdown(
                member.display_name
            )

            percentage = (
                round(amount / total_votes * 100)
                if total_votes else 0
            )

            standings.append(
                f"{marker} **{name}** — {amount} ({percentage}%)"
            )

        if self.closed:
            status = "🔒 VOTING CLOSED"
            color = discord.Color.dark_grey()
            timer_text = "Voting has ended."
        else:
            status = "🟢 VOTING OPEN"
            color = discord.Color.gold()

            seconds = self.get_remaining()
            hours, remainder = divmod(seconds, 3600)
            minutes, seconds = divmod(remainder, 60)

            if hours:
                timer_text = f"⏳ **Time left:** {hours}h {minutes}m"
            elif minutes:
                timer_text = f"⏳ **Time left:** {minutes}m {seconds}s"
            else:
                timer_text = f"⏳ **Time left:** {seconds}s"

        embed = discord.Embed(
            title=f"🗳️ {self.round_name}",
            description=(
                f"{status}\n\n"
                f"**Who's winning?**\n"
                f"{leader_text}\n\n"
                f"{timer_text}\n\n"
                "**Choose a candidate below to vote.**\n"
                "You can change your vote before voting closes."
            ),
            color=color
        )

        embed.add_field(
            name="📊 Current votes",
            value="\n".join(standings) or "No candidates.",
            inline=False
        )

        embed.set_footer(text=f"{total_votes} total vote(s)")

        return embed

    def make_callback(self, candidate_id):

        async def callback(interaction):

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

            # Update the same poll message.
            await interaction.response.edit_message(
                embed=self.make_embed(),
                view=self
            )

        return callback


# ============================================================
# MANUAL WINNER SELECTION
# ============================================================

class ManualWinnerView(discord.ui.View):

    def __init__(self, guild, tied_candidates):
        super().__init__(timeout=86400)

        self.guild = guild
        self.owner_id = guild.owner_id
        self.future = asyncio.get_running_loop().create_future()

        for candidate in tied_candidates:
            button = discord.ui.Button(
                label=candidate.display_name[:80],
                style=discord.ButtonStyle.success
            )

            button.callback = self.make_callback(candidate)
            self.add_item(button)

    async def interaction_check(self, interaction):

        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the server owner can select the winner.",
                ephemeral=True
            )
            return False

        return True

    def make_callback(self, candidate):

        async def callback(interaction):

            if self.future.done():
                await interaction.response.send_message(
                    "A winner has already been selected.",
                    ephemeral=True
                )
                return

            self.future.set_result(candidate.id)

            for item in self.children:
                item.disabled = True

            await interaction.response.edit_message(
                content=(
                    f"✅ {candidate.mention} was selected "
                    "as the election winner by the server owner."
                ),
                view=self
            )

            self.stop()

        return callback

    async def on_timeout(self):

        if not self.future.done():
            self.future.set_result(None)

        for item in self.children:
            item.disabled = True


async def ask_owner_to_choose(guild, results_channel, tied_candidates):

    view = ManualWinnerView(guild, tied_candidates)

    mentions = ", ".join(
        candidate.mention for candidate in tied_candidates
    )

    message = await results_channel.send(
        content=(
            "⚖️ **The tie-break poll also ended in a tie!**\n\n"
            f"The tied candidates are: {mentions}\n\n"
            "**Server owner:** Please choose the winner below."
        ),
        view=view
    )

    try:
        winner_id = await asyncio.wait_for(
            asyncio.shield(view.future),
            timeout=86400
        )
    except asyncio.TimeoutError:
        winner_id = None

    if winner_id is None:
        for item in view.children:
            item.disabled = True

        try:
            await message.edit(
                content=(
                    "⌛ No winner was selected within 24 hours. "
                    "No Winner role was changed."
                ),
                view=view
            )
        except discord.HTTPException:
            pass

        return None

    return winner_id


# ============================================================
# RESULTS AND WINNER ROLE
# ============================================================

async def announce_results(
    guild,
    results_channel,
    winner_role,
    view,
    winner_id=None,
    manual_selection=False
):

    if not view.votes:
        await results_channel.send(
            "No votes were cast. No winner was selected."
        )
        return

    counts = view.get_counts()
    highest = max(counts.values())

    top_candidates = [
        candidate_id
        for candidate_id, amount in counts.items()
        if amount == highest
    ]

    if winner_id is None:
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

    try:
        await guild.chunk(cache=True)
    except discord.HTTPException:
        logger.warning("Could not refresh the member list.")

    # Remove the Winner role from previous holders.
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

    # Give the Winner role to the new winner.
    try:
        if winner_role not in winner.roles:
            await winner.add_roles(
                winner_role,
                reason="Won the server election"
            )
    except discord.Forbidden:
        role_errors.append("Could not assign the Winner role.")
    except discord.HTTPException:
        role_errors.append("Discord error assigning the Winner role.")

    description = (
        f"Congratulations to {winner.mention}!\n"
        f"**Winning votes:** {counts.get(winner_id, 0)}"
    )

    if manual_selection:
        description += "\n\n🧑‍⚖️ **Winner selected by the server owner.**"

    embed = discord.Embed(
        title="🏆 Election Results",
        description=description,
        color=discord.Color.gold()
    )

    lines = []

    for candidate_id, amount in sorted(
        counts.items(),
        key=lambda item: -item[1]
    ):
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
                + "\n\nCheck that the bot has Manage Roles "
                "and its role is above the Winner role."
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
# RUN ONE POLL ROUND
# ============================================================

async def run_poll(
    guild,
    poll_channel,
    candidates,
    duration_seconds,
    round_name
):

    global active_view, active_message

    view = ElectionView(
        candidates,
        duration_seconds,
        round_name=round_name
    )

    active_view = view

    closing_timestamp = int(view.end_time.timestamp())

    # The @everyone announcement is part of the poll message.
    # This keeps the announcement and poll together.
    message = await poll_channel.send(
        content=(
            "@everyone 🗳️ **Election started!**\n"
            f"Voting closes <t:{closing_timestamp}:R>."
        ),
        embed=view.make_embed(),
        view=view,
        allowed_mentions=discord.AllowedMentions(everyone=True)
    )

    active_message = message

    logger.info("%s started in %s.", round_name, guild.name)

    while view.get_remaining() > 0:
        await asyncio.sleep(min(30, view.get_remaining()))

        if view.get_remaining() > 0:
            try:
                await message.edit(
                    embed=view.make_embed(),
                    view=view
                )
            except discord.HTTPException:
                logger.exception("Could not update poll.")

    view.closed = True

    for item in view.children:
        item.disabled = True

    try:
        await message.edit(
            embed=view.make_embed(),
            view=view
        )
    except discord.HTTPException:
        logger.exception("Could not close poll.")

    view.stop()

    return view


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
                f"The current button layout supports up to "
                f"{MAX_CANDIDATES} candidates."
            )
            return

        # ROUND 1: Normal election.
        first_view = await run_poll(
            guild,
            poll_channel,
            candidates,
            duration_seconds,
            "Server Election"
        )

        if not first_view.votes:
            await results_channel.send(
                "No votes were cast. No winner was selected."
            )
            return

        first_leaders = first_view.get_leaders()

        # A single winner: finish normally.
        if len(first_leaders) == 1:
            await announce_results(
                guild,
                results_channel,
                winner_role,
                first_view
            )
            return

        # ROUND 2: Tie-break election.
        tied_candidates = [
            first_view.candidates[candidate_id]
            for candidate_id in first_leaders
        ]

        await results_channel.send(
            "⚖️ **The election ended in a tie!**\n"
            "A tie-break poll is starting with only the tied candidates."
        )

        tie_view = await run_poll(
            guild,
            poll_channel,
            tied_candidates,
            config["tie_duration_seconds"],
            "⚖️ Tie-Break Election"
        )

        if not tie_view.votes:
            await results_channel.send(
                "No votes were cast in the tie-break poll. "
                "No winner was selected."
            )
            return

        tie_leaders = tie_view.get_leaders()

        # Tie-break produced a clear winner.
        if len(tie_leaders) == 1:
            await announce_results(
                guild,
                results_channel,
                winner_role,
                tie_view
            )
            return

        # ROUND 3: Ask the server owner to choose.
        final_tied_candidates = [
            tie_view.candidates[candidate_id]
            for candidate_id in tie_leaders
        ]

        await results_channel.send(
            "⚖️ **The tie-break poll also ended in a tie.**\n"
            "The server owner must now choose the winner."
        )

        winner_id = await ask_owner_to_choose(
            guild,
            results_channel,
            final_tied_candidates
        )

        if winner_id is None:
            return

        await announce_results(
            guild,
            results_channel,
            winner_role,
            tie_view,
            winner_id=winner_id,
            manual_selection=True
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

        try:
            await poll_channel.send(
                "🛑 Election stopped. No winner was selected."
            )
        except Exception:
            pass

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
# START AND STOP ELECTIONS
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
# SCHEDULE SETTINGS
# ============================================================

class ScheduleModal(discord.ui.Modal, title="Election Settings"):

    interval = discord.ui.TextInput(
        label="Repeat interval (e.g. 14d, 2h, 5m)",
        placeholder="14d",
        default=format_time(config["interval_seconds"]),
        max_length=20
    )

    duration = discord.ui.TextInput(
        label="Normal voting duration (e.g. 24h, 5m)",
        placeholder="24h",
        default=format_time(config["duration_seconds"]),
        max_length=20
    )

    tie_duration = discord.ui.TextInput(
        label="Tie-break duration (e.g. 1h, 2m)",
        placeholder="1h",
        default=format_time(config["tie_duration_seconds"]),
        max_length=20
    )

    async def on_submit(self, interaction):

        if not is_server_owner_from_interaction(interaction):
            await interaction.response.send_message(
                "Only the server owner can change settings.",
                ephemeral=True
            )
            return

        try:
            interval_seconds = parse_time(
                self.interval.value,
                "d",
                3650 * 86400
            )

            duration_seconds = parse_time(
                self.duration.value,
                "h",
                8760 * 3600
            )

            tie_duration_seconds = parse_time(
                self.tie_duration.value,
                "h",
                8760 * 3600
            )

            config["interval_seconds"] = interval_seconds
            config["duration_seconds"] = duration_seconds
            config["tie_duration_seconds"] = tie_duration_seconds

            # Keep older configuration values compatible.
            config["interval_days"] = math.ceil(
                interval_seconds / 86400
            )
            config["duration_hours"] = math.ceil(
                duration_seconds / 3600
            )
            config["tie_duration_hours"] = math.ceil(
                tie_duration_seconds / 3600
            )

            next_run = (
                datetime.datetime.now(get_timezone())
                + datetime.timedelta(seconds=interval_seconds)
            )

            config["next_run"] = next_run.isoformat()
            save_config()

            await interaction.response.send_message(
                "✅ Settings saved!\n"
                f"Repeats every **{format_time(interval_seconds)}**.\n"
                f"Normal voting lasts **{format_time(duration_seconds)}**.\n"
                f"Tie-break voting lasts **{format_time(tie_duration_seconds)}**.\n"
                f"Next election: **{next_run.strftime('%b %d at %I:%M %p')}**.",
                ephemeral=True
            )

        except ValueError as error:
            await interaction.response.send_message(
                f"⚠️ {error}",
                ephemeral=True
            )


# ============================================================
# CHANNEL SELECTORS
# ============================================================

class PollChannelSelect(discord.ui.ChannelSelect):

    def __init__(self):
        super().__init__(
            placeholder="Poll channel",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction):

        if not is_server_owner_from_interaction(interaction):
            await interaction.response.send_message(
                "Only the server owner can change settings.",
                ephemeral=True
            )
            return

        config["poll_channel_id"] = self.values[0].id
        save_config()

        await interaction.response.send_message(
            f"✅ Poll channel: {self.values[0].mention}",
            ephemeral=True
        )


class ResultsChannelSelect(discord.ui.ChannelSelect):

    def __init__(self):
        super().__init__(
            placeholder="Results channel",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction):

        if not is_server_owner_from_interaction(interaction):
            await interaction.response.send_message(
                "Only the server owner can change settings.",
                ephemeral=True
            )
            return

        config["results_channel_id"] = self.values[0].id
        save_config()

        await interaction.response.send_message(
            f"✅ Results channel: {self.values[0].mention}",
            ephemeral=True
        )


# ============================================================
# ROLE SELECTORS
# ============================================================

class CandidateRoleSelect(discord.ui.RoleSelect):

    def __init__(self):
        super().__init__(
            placeholder="Candidate role",
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction):

        if not is_server_owner_from_interaction(interaction):
            await interaction.response.send_message(
                "Only the server owner can change settings.",
                ephemeral=True
            )
            return

        role = self.values[0]

        if role.is_default() or role.managed:
            await interaction.response.send_message(
                "That role cannot be used as the Candidate role.",
                ephemeral=True
            )
            return

        config["candidate_role_id"] = role.id
        save_config()

        await interaction.response.send_message(
            f"✅ Candidate role: **{role.name}**",
            ephemeral=True
        )


class WinnerRoleSelect(discord.ui.RoleSelect):

    def __init__(self):
        super().__init__(
            placeholder="Winner role",
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction):

        if not is_server_owner_from_interaction(interaction):
            await interaction.response.send_message(
                "Only the server owner can change settings.",
                ephemeral=True
            )
            return

        role = self.values[0]

        if role.is_default() or role.managed:
            await interaction.response.send_message(
                "That role cannot be used as the Winner role.",
                ephemeral=True
            )
            return

        config["winner_role_id"] = role.id
        save_config()

        await interaction.response.send_message(
            f"✅ Winner role: **{role.name}**",
            ephemeral=True
        )


# ============================================================
# ADMIN DASHBOARD
# ============================================================

class ElectionDashboard(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=300)

        self.add_item(PollChannelSelect())
        self.add_item(ResultsChannelSelect())
        self.add_item(CandidateRoleSelect())
        self.add_item(WinnerRoleSelect())

    @discord.ui.button(
        label="Schedule",
        style=discord.ButtonStyle.primary,
        emoji="⏰",
        row=4
    )
    async def schedule_button(self, interaction, button):

        if not is_server_owner_from_interaction(interaction):
            await interaction.response.send_message(
                "Only the server owner can use this dashboard.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(ScheduleModal())

    @discord.ui.button(
        label="Auto-start",
        style=discord.ButtonStyle.secondary,
        emoji="🔁",
        row=4
    )
    async def auto_button(self, interaction, button):

        if not is_server_owner_from_interaction(interaction):
            await interaction.response.send_message(
                "Only the server owner can use this dashboard.",
                ephemeral=True
            )
            return

        config["auto_start"] = not config["auto_start"]
        save_config()

        status = "ON" if config["auto_start"] else "OFF"

        await interaction.response.send_message(
            f"🔁 Automatic elections are now **{status}**.",
            ephemeral=True
        )

    @discord.ui.button(
        label="Start Election",
        style=discord.ButtonStyle.success,
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

        duration = config["duration_seconds"]

        if launch_election(interaction.guild, duration):
            await interaction.response.send_message(
                "▶️ Election started.",
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
                "No election is running.",
                ephemeral=True
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

    poll, results, candidate, winner = get_configured_items(ctx.guild)

    auto_status = (
        "🟢 ON" if config["auto_start"] else "🔴 OFF"
    )

    if active_election_task and not active_election_task.done():
        election_status = "🟢 Election running"
    else:
        election_status = "⚪ No election running"

    embed = discord.Embed(
        title="Election Dashboard",
        description=(
            f"{election_status}\n\n"
            f"**Automatic elections:** {auto_status}\n"
            f"**Repeats:** Every {format_time(config['interval_seconds'])}\n"
            f"**Voting duration:** {format_time(config['duration_seconds'])}\n"
            f"**Tie-break duration:** {format_time(config['tie_duration_seconds'])}\n"
            f"**Next election:** "
            f"{next_run.strftime('%b %d, %Y at %I:%M %p')}\n\n"
            f"**Poll channel:** "
            f"{poll.mention if poll else 'Not set'}\n"
            f"**Results channel:** "
            f"{results.mention if results else 'Not set'}\n\n"
            f"**Candidate role:** "
            f"{candidate.name if candidate else 'Not set'}\n"
            f"**Winner role:** "
            f"{winner.name if winner else 'Not set'}"
        ),
        color=discord.Color.blurple()
    )

    await ctx.send(
        embed=embed,
        view=ElectionDashboard()
    )


# ============================================================
# COMMANDS
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
        config["duration_seconds"]
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
        await ctx.send("No election is running.")


@bot.command()
async def debug(ctx):

    if not is_server_owner(ctx):
        return

    poll, results, candidate, winner = get_configured_items(ctx.guild)

    bot_member = ctx.guild.get_member(bot.user.id)

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
        title="Election Bot Debug",
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
        name="Role permissions",
        value=(
            "✅ Bot can manage Winner role"
            if manage_roles and hierarchy_ok
            else "❌ Check Manage Roles and role hierarchy"
        ),
        inline=False
    )

    embed.add_field(
        name="Automatic elections",
        value="ON" if config["auto_start"] else "OFF",
        inline=True
    )

    embed.add_field(
        name="Repeat interval",
        value=format_time(config["interval_seconds"]),
        inline=True
    )

    embed.add_field(
        name="Normal voting duration",
        value=format_time(config["duration_seconds"]),
        inline=True
    )

    embed.add_field(
        name="Tie-break duration",
        value=format_time(config["tie_duration_seconds"]),
        inline=True
    )

    embed.add_field(
        name="Next election",
        value=next_run.strftime("%A, %B %d at %I:%M %p %Z"),
        inline=False
    )

    await ctx.send(embed=embed)


# ============================================================
# AUTOMATIC SCHEDULER
# ============================================================

@tasks.loop(seconds=30)
async def scheduled_election():

    try:
        if not config["auto_start"]:
            return

        next_run = get_next_run()
        now = datetime.datetime.now(next_run.tzinfo)

        if now >= next_run:

            guild = bot.get_guild(GUILD_ID)

            if guild is None:
                logger.error(
                    "Scheduled election: server not found. Will retry."
                )
                return

            duration = config["duration_seconds"]

            if launch_election(guild, duration):
                schedule_next_run()
            else:
                logger.warning(
                    "An election was already running. "
                    "The scheduled election will be retried."
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
