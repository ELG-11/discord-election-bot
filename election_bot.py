
import os
import asyncio
import datetime
import logging
from zoneinfo import ZoneInfo
from threading import Thread

from flask import Flask
import discord
from discord.ext import commands, tasks


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

logger = logging.getLogger("election_bot")


# ============================================================
# CONFIGURATION
# ============================================================

TOKEN = os.environ["TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
CANDIDATE_ROLE_ID = int(os.environ["CANDIDATE_ROLE_ID"])
WINNER_ROLE_ID = int(os.environ["WINNER_ROLE_ID"])


# ============================================================
# SCHEDULE SETTINGS
# ============================================================

TIMEZONE = ZoneInfo("America/New_York")

ANCHOR_START = datetime.datetime(
    2026, 11, 30, 23, 0, tzinfo=TIMEZONE
)

INTERVAL_DAYS = 14
VOTING_DURATION_SECONDS = 86400
MAX_EMPTY_RESTARTS = 2


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
    intents=intents
)

active_election_task = None
active_view = None
active_message = None


# ============================================================
# ELECTION VOTING VIEW
# ============================================================

class ElectionView(discord.ui.View):

    def __init__(self, candidates):
        super().__init__(timeout=None)

        self.candidates = {
            candidate.id: candidate
            for candidate in candidates
        }

        self.votes = {}
        self.closed = False

        for candidate in candidates:
            button = discord.ui.Button(
                label=candidate.display_name,
                style=discord.ButtonStyle.primary,
                custom_id=f"vote_{candidate.id}"
            )

            button.callback = self.make_callback(candidate.id)
            self.add_item(button)

    def make_callback(self, candidate_id):

        async def callback(interaction: discord.Interaction):

            if self.closed:
                await interaction.response.send_message(
                    "Voting for this round has already closed.",
                    ephemeral=True
                )
                return

            candidate = self.candidates.get(candidate_id)

            if candidate is None:
                await interaction.response.send_message(
                    "This candidate is no longer available.",
                    ephemeral=True
                )
                return

            previous_vote = self.votes.get(interaction.user.id)

            # User already voted for this candidate
            if previous_vote == candidate_id:

                await interaction.response.send_message(
                    f"You have already voted for **{candidate.display_name}**!",
                    ephemeral=True
                )

                return

            # User is changing their vote
            elif previous_vote is not None:

                self.votes[interaction.user.id] = candidate_id

                await interaction.response.send_message(
                    f"Your vote has been changed to **{candidate.display_name}**.",
                    ephemeral=True
                )

                return

            # First vote
            else:

                self.votes[interaction.user.id] = candidate_id

                await interaction.response.send_message(
                    f"Your vote for **{candidate.display_name}** has been recorded.",
                    ephemeral=True
                )

        return callback


# ============================================================
# ELECTION CYCLE
# ============================================================

async def run_election_cycle(
    guild,
    channel,
    candidate_role,
    winner_role,
    duration
):

    global active_view, active_message

    empty_restarts = 0

    try:

        while True:

            # Get eligible candidates
            candidates = [
                member
                for member in guild.members
                if candidate_role in member.roles
                and not member.bot
            ]

            if not candidates:

                await channel.send(
                    "⚠️ Election skipped: no members currently have the Candidate role."
                )

                return

            # Discord supports a maximum of 25 buttons per view.
            if len(candidates) > 25:

                await channel.send(
                    f"⚠️ Election cancelled: **{len(candidates)} candidates** "
                    "were found, but Discord only supports 25 buttons per poll. "
                    "Please reduce the number of candidates before restarting."
                )

                logger.warning(
                    "Election cancelled: %s candidates exceeds Discord's 25-button limit.",
                    len(candidates)
                )

                return

            # Create voting view
            view = ElectionView(candidates)

            active_view = view

            hours_text = (
                "24 hours"
                if duration == 86400
                else f"{duration} seconds"
            )

            embed = discord.Embed(
                title="🗳️ Server Leader Election",
                description=(
                    "Click a button below to cast your vote.\n"
                    f"Voting closes in **{hours_text}**.\n"
                    "You can change your vote anytime before it ends."
                ),
                color=discord.Color.gold()
            )

            msg = await channel.send(
                embed=embed,
                view=view
            )

            active_message = msg

            logger.info(
                "Election started in %s with %s candidates.",
                guild.name,
                len(candidates)
            )

            # Wait for voting period
            await asyncio.sleep(duration)

            # Close voting
            view.closed = True

            for item in view.children:
                item.disabled = True

            await msg.edit(view=view)

            view.stop()

            logger.info(
                "Election ended with %s votes.",
                len(view.votes)
            )

            # ------------------------------------------------
            # EMPTY POLL RESTART
            # ------------------------------------------------

            if not view.votes:

                empty_restarts += 1

                if empty_restarts <= MAX_EMPTY_RESTARTS:

                    await channel.send(
                        f"🔄 No votes were cast! Restarting the poll "
                        f"(Attempt {empty_restarts}/{MAX_EMPTY_RESTARTS})..."
                    )

                    continue

                else:

                    await channel.send(
                        "❌ No votes were cast after restarting. "
                        "No role changes were made."
                    )

                    return

            # ------------------------------------------------
            # TALLY VOTES
            # ------------------------------------------------

            counts = {}

            for candidate_id in view.votes.values():

                counts[candidate_id] = (
                    counts.get(candidate_id, 0) + 1
                )

            max_votes = max(counts.values())

            top_candidates = [
                candidate_id
                for candidate_id, count in counts.items()
                if count == max_votes
            ]

            # ------------------------------------------------
            # TIE BREAKER
            # ------------------------------------------------

            if len(top_candidates) > 1:

                await channel.send(
                    "⚖️ It's a tie! Restarting the poll to break the tie..."
                )

                continue

            # ------------------------------------------------
            # DECLARE WINNER
            # ------------------------------------------------

            winner_id = top_candidates[0]

            winner = guild.get_member(winner_id)

            if not winner:

                try:
                    winner = await guild.fetch_member(winner_id)

                except discord.NotFound:

                    await channel.send(
                        "⚠️ The winning candidate left the server "
                        "before the poll ended. No roles were changed."
                    )

                    return

            # ------------------------------------------------
            # ROLE REMOVAL AND ASSIGNMENT
            # ------------------------------------------------

            role_errors = []

            # Remove winner role from previous holders
            for member in guild.members:

                if (
                    winner_role in member.roles
                    and member.id != winner.id
                ):

                    try:

                        await member.remove_roles(
                            winner_role,
                            reason="Election ended"
                        )

                        logger.info(
                            "Removed winner role from %s.",
                            member.display_name
                        )

                    except discord.Forbidden as e:

                        logger.error(
                            "Missing permissions removing winner role from %s: %s",
                            member.display_name,
                            e
                        )

                        role_errors.append(
                            f"Could not remove the role from {member.display_name}."
                        )

                        break

                    except discord.HTTPException as e:

                        logger.exception(
                            "Discord HTTP error removing winner role."
                        )

                        role_errors.append(
                            "Discord API error while removing an existing winner role."
                        )

                        break

            # Attempt to assign the winner role even if removal failed
            try:

                await winner.add_roles(
                    winner_role,
                    reason="Won election"
                )

                logger.info(
                    "Successfully assigned winner role to %s.",
                    winner.display_name
                )

            except discord.Forbidden as e:

                logger.error(
                    "Missing permissions assigning winner role to %s: %s",
                    winner.display_name,
                    e
                )

                role_errors.append(
                    "Could not assign the winner role."
                )

            except discord.HTTPException as e:

                logger.exception(
                    "Discord HTTP error assigning winner role."
                )

                role_errors.append(
                    "Discord API error while assigning the winner role."
                )

            # ------------------------------------------------
            # WINNER ANNOUNCEMENT
            # ------------------------------------------------

            # Always attempt to announce the winner,
            # regardless of role assignment errors.

            await channel.send(
                f"🎉 **{winner.mention}** won the election "
                f"with **{max_votes}** vote(s)!"
            )

            if not role_errors:

                await channel.send(
                    f"✅ {winner.mention} has been assigned "
                    f"the **{winner_role.name}** role!"
                )

            else:

                await channel.send(
                    "⚠️ **Role assignment warning!**\n"
                    + "\n".join(role_errors)
                    + "\n\n"
                    "**Administrator action required:**\n"
                    "1. Open Server Settings → Roles.\n"
                    "2. Move the bot's role ABOVE the Winner role.\n"
                    "3. Make sure the bot has the **Manage Roles** permission.\n"
                    "4. Check that the Winner role is not managed by an integration.\n"
                    "5. Manually correct any incomplete role changes."
                )

            break

    except asyncio.CancelledError:

        logger.info("Election was cancelled by an administrator.")

        if active_view:

            active_view.closed = True

            for item in active_view.children:
                item.disabled = True

            active_view.stop()

        if active_message:

            try:

                await active_message.edit(
                    view=active_view
                )

            except Exception:

                logger.exception(
                    "Could not disable election buttons during cancellation."
                )

        await channel.send(
            "🛑 **Election force-stopped by an admin.** "
            "No further role changes were made."
        )

        raise

    except Exception as e:

        logger.exception(
            "UNEXPECTED ERROR in election cycle: %s",
            e
        )

        try:

            await channel.send(
                "🚨 **Election error!** An unexpected error occurred "
                "during the election. Please check the bot logs. "
                "The election may not have completed."
            )

        except Exception:

            logger.exception(
                "Could not send election error notification."
            )

    finally:

        active_view = None
        active_message = None

        logger.info("Election cycle finished.")


# ============================================================
# LAUNCH ELECTION
# ============================================================

def launch_election(
    guild,
    channel,
    candidate_role,
    winner_role,
    duration
):

    global active_election_task

    if active_election_task and not active_election_task.done():
        return False

    if not guild or not channel or not candidate_role or not winner_role:
        return False

    active_election_task = asyncio.create_task(
        run_election_cycle(
            guild,
            channel,
            candidate_role,
            winner_role,
            duration
        )
    )

    return True


# ============================================================
# NEXT ELECTION TIME
# ============================================================

def get_next_election_time(now):

    next_time = ANCHOR_START

    while next_time <= now:

        next_time += datetime.timedelta(
            days=INTERVAL_DAYS
        )

    return next_time


# ============================================================
# AUTOMATIC SCHEDULE
# ============================================================

@tasks.loop(minutes=1)
async def scheduled_election():

    try:

        now = datetime.datetime.now(TIMEZONE)

        days_since_anchor = (
            now.date() - ANCHOR_START.date()
        ).days

        if (
            days_since_anchor >= 0
            and days_since_anchor % INTERVAL_DAYS == 0
        ):

            if (
                now.hour == ANCHOR_START.hour
                and now.minute == ANCHOR_START.minute
            ):

                guild = bot.get_guild(GUILD_ID)

                if not guild:

                    logger.error(
                        "Scheduled election failed: guild not found."
                    )

                    return

                channel = guild.get_channel(CHANNEL_ID)

                candidate_role = guild.get_role(
                    CANDIDATE_ROLE_ID
                )

                winner_role = guild.get_role(
                    WINNER_ROLE_ID
                )

                if not channel or not candidate_role or not winner_role:

                    logger.error(
                        "Scheduled election failed: missing channel or role configuration."
                    )

                    return

                launched = launch_election(
                    guild,
                    channel,
                    candidate_role,
                    winner_role,
                    VOTING_DURATION_SECONDS
                )

                if launched:

                    logger.info(
                        "Scheduled election launched successfully."
                    )

    except Exception:

        logger.exception(
            "Unexpected error in scheduled election task."
        )


# ============================================================
# ADMIN COMMAND: START ELECTION
# ============================================================

@bot.command()
@commands.has_permissions(administrator=True)
async def startelection(ctx):

    """Force-starts a full 24-hour election right now."""

    guild = bot.get_guild(GUILD_ID)

    channel = guild.get_channel(CHANNEL_ID) if guild else None

    candidate_role = (
        guild.get_role(CANDIDATE_ROLE_ID)
        if guild else None
    )

    winner_role = (
        guild.get_role(WINNER_ROLE_ID)
        if guild else None
    )

    if launch_election(
        guild,
        channel,
        candidate_role,
        winner_role,
        VOTING_DURATION_SECONDS
    ):

        await ctx.send(
            "▶️ Force-started a full **24-hour** election!"
        )

    else:

        await ctx.send(
            "⚠️ An election is already running, or the server/channel/role "
            "configuration is invalid. Check `!debug`."
        )


# ============================================================
# ADMIN COMMAND: TEST POLL
# ============================================================

@bot.command()
@commands.has_permissions(administrator=True)
async def testpoll(ctx):

    """Force-starts a quick 30-second test election."""

    guild = bot.get_guild(GUILD_ID)

    channel = guild.get_channel(CHANNEL_ID) if guild else None

    candidate_role = (
        guild.get_role(CANDIDATE_ROLE_ID)
        if guild else None
    )

    winner_role = (
        guild.get_role(WINNER_ROLE_ID)
        if guild else None
    )

    if launch_election(
        guild,
        channel,
        candidate_role,
        winner_role,
        duration=30
    ):

        await ctx.send(
            "🧪 Started a **30-second** test election!"
        )

    else:

        await ctx.send(
            "⚠️ An election is already running, or the server/channel/role "
            "configuration is invalid. Check `!debug`."
        )


# ============================================================
# ADMIN COMMAND: STOP ELECTION
# ============================================================

@bot.command()
@commands.has_permissions(administrator=True)
async def stopelection(ctx):

    """Force-stops any currently running election or test poll."""

    global active_election_task

    if active_election_task and not active_election_task.done():

        active_election_task.cancel()

        await ctx.send(
            "🛑 Election cancellation requested."
        )

    else:

        await ctx.send(
            "ℹ️ No election is currently running."
        )


# ============================================================
# ADMIN COMMAND: DEBUG
# ============================================================

@bot.command()
@commands.has_permissions(administrator=True)
async def debug(ctx):

    """Checks bot permissions, role hierarchy, candidates, and next scheduled date."""

    guild = bot.get_guild(GUILD_ID)

    if not guild:

        await ctx.send(
            "❌ **Error:** Bot cannot find the server "
            "(`GUILD_ID` is wrong or bot is not in server)."
        )

        return

    channel = guild.get_channel(CHANNEL_ID)

    candidate_role = guild.get_role(
        CANDIDATE_ROLE_ID
    )

    winner_role = guild.get_role(
        WINNER_ROLE_ID
    )

    bot_member = (
        guild.get_member(bot.user.id)
        or await guild.fetch_member(bot.user.id)
    )

    candidates = [
        member.display_name
        for member in guild.members
        if candidate_role
        and candidate_role in member.roles
        and not member.bot
    ]

    candidate_names = (
        ", ".join(candidates)
        if candidates
        else "None"
    )

    has_manage_roles = bot_member.guild_permissions.manage_roles

    hierarchy_ok = bool(
        winner_role
        and bot_member.top_role > winner_role
    )

    role_ok = has_manage_roles and hierarchy_ok

    next_run = get_next_election_time(
        datetime.datetime.now(TIMEZONE)
    )

    is_running = (
        active_election_task is not None
        and not active_election_task.done()
    )

    channel_text = (
        channel.mention
        if channel
        else "❌ Not found (check CHANNEL_ID)"
    )

    cand_role_text = (
        candidate_role.name
        if candidate_role
        else "❌ Not found"
    )

    win_role_text = (
        winner_role.name
        if winner_role
        else "❌ Not found"
    )

    if role_ok:

        perm_text = "✅ Yes"

    else:

        perm_text = (
            "❌ NO — Check Manage Roles permission and move "
            "the bot role ABOVE the Winner role!"
        )

    running_text = "Yes" if is_running else "No"

    next_run_text = next_run.strftime(
        "%A, %b %d, %Y at %I:%M %p %Z"
    )

    report = (
        f"**🛠️ Election Bot Debug Report**\n"
        f"• **Server:** {guild.name} ✅\n"
        f"• **Channel:** {channel_text}\n"
        f"• **Candidate Role:** {cand_role_text} "
        f"({len(candidates)} candidate(s): {candidate_names})\n"
        f"• **Winner Role:** {win_role_text}\n"
        f"• **Bot Can Assign Winner Role:** {perm_text}\n"
        f"• **Manage Roles Permission:** "
        f"{'✅ Yes' if has_manage_roles else '❌ No'}\n"
        f"• **Role Hierarchy:** "
        f"{'✅ Yes' if hierarchy_ok else '❌ No'}\n"
        f"• **Election Currently Running:** {running_text}\n"
        f"• **Next Auto Election:** {next_run_text}\n"
        f"• **Admin Commands:** "
        "`!debug` | `!testpoll` | `!startelection` | `!stopelection`"
    )

    await ctx.send(report)


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

        logger.info(
            "Automatic election scheduler started."
        )


# ============================================================
# START BOT
# ============================================================

keep_alive()

bot.run(TOKEN)
