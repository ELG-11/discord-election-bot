import os
import asyncio
import datetime
from zoneinfo import ZoneInfo
from threading import Thread
from flask import Flask
import discord
from discord.ext import commands, tasks

# --- CONFIGURATION (Read safely from Render environment variables) ---
TOKEN = os.environ["TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
CANDIDATE_ROLE_ID = int(os.environ["CANDIDATE_ROLE_ID"])
WINNER_ROLE_ID = int(os.environ["WINNER_ROLE_ID"])

# --- SCHEDULE SETTINGS (Every other Monday at 11:00 PM NYC Time) ---
TIMEZONE = ZoneInfo("America/New_York")
ANCHOR_START = datetime.datetime(2026, 11, 30, 23, 0, tzinfo=TIMEZONE)
INTERVAL_DAYS = 14                 # Every 2 weeks
VOTING_DURATION_SECONDS = 86400    # Voting stays open for 24 hours
MAX_EMPTY_RESTARTS = 2             # Restarts up to 2 times if nobody votes

# --- KEEP-ALIVE WEB SERVER ---
app = Flask("")

@app.route("/")
def home():
    return "Election bot is running."

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def keep_alive():
    Thread(target=run_web, daemon=True).start()

# --- DISCORD BOT ---
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

active_election_task = None
active_view = None
active_message = None

class ElectionView(discord.ui.View):
    def __init__(self, candidates):
        super().__init__(timeout=None)
        self.candidates = {c.id: c for c in candidates}
        self.votes = {}
        self.closed = False

        for candidate in candidates[:25]:
            btn = discord.ui.Button(
                label=candidate.display_name,
                style=discord.ButtonStyle.primary,
                custom_id=f"vote_{candidate.id}"
            )
            btn.callback = self.make_callback(candidate.id)
            self.add_item(btn)

    def make_callback(self, candidate_id):
        async def callback(interaction: discord.Interaction):
            if self.closed:
                await interaction.response.send_message(
                    "Voting for this round has already closed.", ephemeral=True
                )
                return
            self.votes[interaction.user.id] = candidate_id
            candidate = self.candidates[candidate_id]
            await interaction.response.send_message(
                f"Your vote for **{candidate.display_name}** has been recorded.",
                ephemeral=True
            )
        return callback

async def run_election_cycle(guild, channel, candidate_role, winner_role, duration):
    global active_view, active_message
    empty_restarts = 0

    try:
        while True:
            candidates = [m for m in guild.members if candidate_role in m.roles and not m.bot]
            if not candidates:
                await channel.send("⚠️ Election skipped: no members currently have the Candidate role.")
                return

            view = ElectionView(candidates)
            active_view = view
            hours_text = "24 hours" if duration == 86400 else f"{duration} seconds"
            embed = discord.Embed(
                title="🗳️ Server Leader Election",
                description=(
                    f"Click a button below to cast your vote.\n"
                    f"Voting closes in **{hours_text}**. You can change your vote anytime before it ends."
                ),
                color=discord.Color.gold()
            )
            msg = await channel.send(embed=embed, view=view)
            active_message = msg

            # Wait for the full voting period
            await asyncio.sleep(duration)

            # Lock voting and disable buttons
            view.closed = True
            for item in view.children:
                item.disabled = True
            await msg.edit(view=view)
            view.stop()

            # Check for zero votes
            if not view.votes:
                empty_restarts += 1
                if empty_restarts <= MAX_EMPTY_RESTARTS:
                    await channel.send(f"🔄 No votes were cast! Restarting the poll (Attempt {empty_restarts}/{MAX_EMPTY_RESTARTS})...")
                    continue
                else:
                    await channel.send("❌ No votes were cast after restarting. No role changes were made.")
                    return

            # Tally votes
            counts = {}
            for cid in view.votes.values():
                counts[cid] = counts.get(cid, 0) + 1

            max_votes = max(counts.values())
            top_candidates = [cid for cid, count in counts.items() if count == max_votes]

            # Restart if tied
            if len(top_candidates) > 1:
                await channel.send("⚖️ It's a tie! Restarting the poll to break the tie...")
                continue

            # Declare winner and swap roles
            winner_id = top_candidates.pop(0)
            winner = guild.get_member(winner_id)
            if not winner:
                await channel.send("⚠️ The winning candidate left the server before the poll ended. No roles were changed.")
                return

            for member in guild.members:
                if winner_role in member.roles and member.id != winner.id:
                    await member.remove_roles(winner_role, reason="Election ended")

            await winner.add_roles(winner_role, reason="Won election")
            await channel.send(
                f"🎉 **{winner.mention}** won the election with **{max_votes}** vote(s) "
                f"and is now the **{winner_role.name}**!"
            )
            break

    except asyncio.CancelledError:
        if active_view:
            active_view.closed = True
            for item in active_view.children:
                item.disabled = True
            active_view.stop()
        if active_message:
            try:
                await active_message.edit(view=active_view)
            except Exception:
                pass
        await channel.send("🛑 **Election force-stopped by an admin.** No roles were changed.")
        raise
    finally:
        active_view = None
        active_message = None

def launch_election(guild, channel, candidate_role, winner_role, duration):
    global active_election_task
    if active_election_task and not active_election_task.done():
        return False
    active_election_task = asyncio.create_task(
        run_election_cycle(guild, channel, candidate_role, winner_role, duration)
    )
    return True

def get_next_election_time(now):
    next_time = ANCHOR_START
    while next_time <= now:
        next_time += datetime.timedelta(days=INTERVAL_DAYS)
    return next_time

@tasks.loop(minutes=1)
async def scheduled_election():
    now = datetime.datetime.now(TIMEZONE)
    days_since_anchor = (now.date() - ANCHOR_START.date()).days

    if days_since_anchor >= 0 and days_since_anchor % INTERVAL_DAYS == 0:
        if now.hour == ANCHOR_START.hour and now.minute == ANCHOR_START.minute:
            guild = bot.get_guild(GUILD_ID)
            channel = guild.get_channel(CHANNEL_ID)
            candidate_role = guild.get_role(CANDIDATE_ROLE_ID)
            winner_role = guild.get_role(WINNER_ROLE_ID)
            launch_election(guild, channel, candidate_role, winner_role, VOTING_DURATION_SECONDS)

@bot.command()
@commands.has_permissions(administrator=True)
async def startelection(ctx):
    """Force-starts a full 24-hour election right now."""
    guild = bot.get_guild(GUILD_ID)
    channel = guild.get_channel(CHANNEL_ID)
    candidate_role = guild.get_role(CANDIDATE_ROLE_ID)
    winner_role = guild.get_role(WINNER_ROLE_ID)

    if launch_election(guild, channel, candidate_role, winner_role, VOTING_DURATION_SECONDS):
        await ctx.send("▶️ Force-started a full **24-hour** election!")
    else:
        await ctx.send("⚠️ An election is already running! Use `!stopelection` first if you want to cancel it.")

@bot.command()
@commands.has_permissions(administrator=True)
async def testpoll(ctx):
    """Force-starts a quick 30-second test election."""
    guild = bot.get_guild(GUILD_ID)
    channel = guild.get_channel(CHANNEL_ID)
    candidate_role = guild.get_role(CANDIDATE_ROLE_ID)
    winner_role = guild.get_role(WINNER_ROLE_ID)

    if launch_election(guild, channel, candidate_role, winner_role, duration=30):
        await ctx.send("🧪 Started a **30-second** test election!")
    else:
        await ctx.send("⚠️ An election is already running! Use `!stopelection` first if you want to cancel it.")

@bot.command()
@commands.has_permissions(administrator=True)
async def stopelection(ctx):
    """Force-stops any currently running election or test poll."""
    global active_election_task
    if active_election_task and not active_election_task.done():
        active_election_task.cancel()
    else:
        await ctx.send("ℹ️ No election is currently running.")

@bot.command()
@commands.has_permissions(administrator=True)
async def debug(ctx):
    """Checks bot permissions, role hierarchy, candidates, and next scheduled date."""
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        await ctx.send("❌ **Error:** Bot cannot find the server (`GUILD_ID` is wrong or bot is not in server).")
        return

    channel = guild.get_channel(CHANNEL_ID)
    candidate_role = guild.get_role(CANDIDATE_ROLE_ID)
    winner_role = guild.get_role(WINNER_ROLE_ID)
    bot_member = guild.get_member(bot.user.id) or await guild.fetch_member(bot.user.id)

    candidates = [m.display_name for m in guild.members if candidate_role and candidate_role in m.roles and not m.bot]
    candidate_names = ", ".join(candidates) if candidates else "None"
    role_ok = bool(winner_role and (bot_member.top_role > winner_role))
    next_run = get_next_election_time(datetime.datetime.now(TIMEZONE))
    is_running = active_election_task is not None and not active_election_task.done()

    channel_text = channel.mention if channel else "❌ Not found (check CHANNEL_ID)"
    cand_role_text = candidate_role.name if candidate_role else "❌ Not found"
    win_role_text = winner_role.name if winner_role else "❌ Not found"
    perm_text = "✅ Yes" if role_ok else "❌ NO — Drag the bot role ABOVE the winner role in Server Settings > Roles!"
    running_text = "Yes" if is_running else "No"
    next_run_text = next_run.strftime("%A, %b %d, %Y at %I:%M %p %Z")

    report = (
        f"**🛠️ Election Bot Debug Report**\n"
        f"• **Server:** {guild.name} ✅\n"
        f"• **Channel:** {channel_text}\n"
        f"• **Candidate Role:** {cand_role_text} ({len(candidates)} candidate(s): {candidate_names})\n"
        f"• **Winner Role:** {win_role_text}\n"
        f"• **Bot Can Assign Winner Role:** {perm_text}\n"
        f"• **Election Currently Running:** {running_text}\n"
        f"• **Next Auto Election:** {next_run_text}\n"
        f"• **Admin Commands:** `!debug` | `!testpoll` | `!startelection` | `!stopelection`"
    )
    await ctx.send(report)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    if not scheduled_election.is_running():
        scheduled_election.start()

keep_alive()
bot.run(TOKEN)

