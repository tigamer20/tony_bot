"""Personal reminders that survive restarts."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import parse_duration

log = logging.getLogger(__name__)

MAX_PER_USER = 25
MIN_DURATION = timedelta(seconds=10)
MAX_DURATION = timedelta(days=365)


class Reminders(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.check_reminders.start()

    async def cog_unload(self) -> None:
        self.check_reminders.cancel()

    @tasks.loop(seconds=15)
    async def check_reminders(self) -> None:
        due = await self.bot.db.fetchall("SELECT * FROM reminders WHERE remind_at <= ?", (int(time.time()),))
        for row in due:
            # Delete first so a slow send can never cause a duplicate.
            await self.bot.db.execute("DELETE FROM reminders WHERE id = ?", (row["id"],))
            text = f"\N{ALARM CLOCK} <@{row['user_id']}> reminder: {row['message']}"
            allowed = discord.AllowedMentions(users=[discord.Object(id=row["user_id"])])
            delivered = False
            channel = self.bot.get_channel(row["channel_id"]) if row["channel_id"] else None
            if isinstance(channel, discord.abc.Messageable):
                try:
                    await channel.send(text, allowed_mentions=allowed)
                    delivered = True
                except discord.HTTPException:
                    pass
            if not delivered:
                try:
                    user = await self.bot.fetch_user(row["user_id"])
                    await user.send(text)
                except discord.HTTPException:
                    log.info("Could not deliver reminder %s", row["id"])

    @check_reminders.before_loop
    async def _wait(self) -> None:
        await self.bot.wait_until_ready()

    reminder = app_commands.Group(name="reminder", description="Set personal reminders.")

    @reminder.command(name="add", description="Remind me of something later.")
    @app_commands.describe(when="In how long: 10m, 2h, 1d, 1w", message="What to remind you about")
    async def add(self, interaction: discord.Interaction, when: str, message: app_commands.Range[str, 1, 500]):
        delta = parse_duration(when)
        if delta is None or not (MIN_DURATION <= delta <= MAX_DURATION):
            return await interaction.response.send_message("Time must look like `10m`, `2h30m` or `1d` (10 seconds to 1 year).", ephemeral=True)
        count = await self.bot.db.fetchone("SELECT COUNT(*) AS n FROM reminders WHERE user_id = ?", (interaction.user.id,))
        if count["n"] >= MAX_PER_USER:
            return await interaction.response.send_message(f"You already have {MAX_PER_USER} reminders. Cancel one first.", ephemeral=True)
        remind_at = int(time.time() + delta.total_seconds())
        rid = await self.bot.db.insert(
            "INSERT INTO reminders (user_id, channel_id, message, remind_at) VALUES (?, ?, ?, ?)",
            (interaction.user.id, interaction.channel_id, message, remind_at),
        )
        when_text = discord.utils.format_dt(datetime.fromtimestamp(remind_at, tz=timezone.utc), "R")
        await interaction.response.send_message(f"Okay! I'll remind you {when_text} (reminder #{rid}).", ephemeral=True)

    @reminder.command(name="list", description="Show your upcoming reminders.")
    async def list_reminders(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall("SELECT * FROM reminders WHERE user_id = ? ORDER BY remind_at LIMIT 25", (interaction.user.id,))
        if not rows:
            return await interaction.response.send_message("You have no reminders.", ephemeral=True)
        lines = [
            f"`#{r['id']}` {discord.utils.format_dt(datetime.fromtimestamp(r['remind_at'], tz=timezone.utc), 'R')} · {r['message'][:80]}"
            for r in rows
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @reminder.command(name="cancel", description="Cancel one of your reminders.")
    @app_commands.describe(reminder_id="The # shown in /reminder list")
    async def cancel(self, interaction: discord.Interaction, reminder_id: int):
        count = await self.bot.db.execute("DELETE FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, interaction.user.id))
        await interaction.response.send_message("Cancelled." if count else "You have no reminder with that ID.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Reminders(bot))
