"""Giveaways with a persistent 'Enter' button. They survive bot restarts."""
from __future__ import annotations

import logging
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import parse_duration

log = logging.getLogger(__name__)

MAX_DURATION = timedelta(days=30)
MIN_DURATION = timedelta(seconds=10)


def _ts(unix: int, style: str = "R") -> str:
    return discord.utils.format_dt(datetime.fromtimestamp(unix, tz=timezone.utc), style)


def giveaway_embed(row: sqlite3.Row, *, entries: Optional[int] = None, winners: Optional[list[int]] = None) -> discord.Embed:
    ended = winners is not None
    embed = discord.Embed(
        title=f"\N{PARTY POPPER} {row['prize']}",
        color=discord.Color.dark_grey() if ended else discord.Color.gold(),
    )
    if ended:
        embed.description = (
            "Winner(s): " + ", ".join(f"<@{w}>" for w in winners) if winners else "No valid entries, so no winner."
        )
        embed.add_field(name="Ended", value=_ts(row["end_time"], "f"))
    else:
        embed.description = "Click the button below to enter!"
        embed.add_field(name="Ends", value=_ts(row["end_time"]))
    embed.add_field(name="Winners", value=str(row["winners"]))
    embed.add_field(name="Hosted by", value=f"<@{row['host_id']}>")
    embed.set_footer(text=f"Giveaway ID: {row['id']}")
    return embed


class GiveawayButton(discord.ui.DynamicItem[discord.ui.Button], template=r"gw:enter:(?P<id>[0-9]+)"):
    def __init__(self, giveaway_id: int) -> None:
        super().__init__(
            discord.ui.Button(label="Enter", emoji="\N{PARTY POPPER}", style=discord.ButtonStyle.primary, custom_id=f"gw:enter:{giveaway_id}")
        )
        self.giveaway_id = giveaway_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        db = interaction.client.db
        row = await db.fetchone("SELECT * FROM giveaways WHERE id = ?", (self.giveaway_id,))
        if row is None or row["ended"] or row["end_time"] <= time.time():
            return await interaction.response.send_message("This giveaway has ended.", ephemeral=True)

        existing = await db.fetchone(
            "SELECT 1 FROM giveaway_entries WHERE giveaway_id = ? AND user_id = ?", (self.giveaway_id, interaction.user.id)
        )
        if existing:
            await db.execute("DELETE FROM giveaway_entries WHERE giveaway_id = ? AND user_id = ?", (self.giveaway_id, interaction.user.id))
            msg = "You left the giveaway."
        else:
            await db.execute("INSERT OR IGNORE INTO giveaway_entries (giveaway_id, user_id) VALUES (?, ?)", (self.giveaway_id, interaction.user.id))
            msg = "You're in! Good luck. Click again to leave."
        count = await db.fetchone("SELECT COUNT(*) AS n FROM giveaway_entries WHERE giveaway_id = ?", (self.giveaway_id,))
        await interaction.response.send_message(f"{msg} ({count['n']} entries)", ephemeral=True)


class Giveaways(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(GiveawayButton)
        self.check_giveaways.start()

    async def cog_unload(self) -> None:
        self.check_giveaways.cancel()
        self.bot.remove_dynamic_items(GiveawayButton)

    # ------------------------------------------------------------------ ending logic
    async def _end(self, giveaway_id: int, *, reroll: bool = False) -> Optional[list[int]]:
        """Pick winners, update the message and announce. Returns winner ids (None if not found)."""
        db = self.bot.db
        row = await db.fetchone("SELECT * FROM giveaways WHERE id = ?", (giveaway_id,))
        if row is None:
            return None
        await db.execute("UPDATE giveaways SET ended = 1 WHERE id = ?", (giveaway_id,))

        guild = self.bot.get_guild(row["guild_id"])
        entries = [e["user_id"] for e in await db.fetchall("SELECT user_id FROM giveaway_entries WHERE giveaway_id = ?", (giveaway_id,))]
        if guild is not None:  # drop people who left the server
            entries = [uid for uid in entries if guild.get_member(uid) is not None]
        winners = random.sample(entries, min(row["winners"], len(entries)))

        channel = self.bot.get_channel(row["channel_id"])
        if not isinstance(channel, discord.abc.Messageable) or row["message_id"] is None:
            return winners
        try:
            message = await channel.fetch_message(row["message_id"])
            if not reroll:
                await message.edit(embed=giveaway_embed(row, winners=winners), view=None)
            text = (
                f"\N{PARTY POPPER} Congratulations {', '.join(f'<@{w}>' for w in winners)}! You won **{row['prize']}**."
                if winners else f"No valid entries for **{row['prize']}**, so nobody won."
            )
            await message.reply(text, allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException:
            log.warning("Could not update giveaway message %s", row["message_id"])
        return winners

    @tasks.loop(seconds=15)
    async def check_giveaways(self) -> None:
        due = await self.bot.db.fetchall("SELECT id FROM giveaways WHERE ended = 0 AND end_time <= ?", (int(time.time()),))
        for row in due:
            try:
                await self._end(row["id"])
            except Exception:
                log.exception("Failed to end giveaway %s", row["id"])

    @check_giveaways.before_loop
    async def _wait(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ commands
    giveaway = app_commands.Group(
        name="giveaway", description="Run giveaways.",
        default_permissions=discord.Permissions(manage_guild=True), guild_only=True,
    )

    @giveaway.command(name="start", description="Start a giveaway.")
    @app_commands.describe(prize="What's being given away", duration="How long: 10m, 2h, 1d, 1w (max 30d)", winners="Number of winners", channel="Where to post (default: here)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def start(
        self,
        interaction: discord.Interaction,
        prize: app_commands.Range[str, 1, 200],
        duration: str,
        winners: app_commands.Range[int, 1, 10] = 1,
        channel: Optional[discord.TextChannel] = None,
    ):
        delta = parse_duration(duration)
        if delta is None or not (MIN_DURATION <= delta <= MAX_DURATION):
            return await interaction.response.send_message("Duration must look like `10m`, `2h` or `1d` (10 seconds to 30 days).", ephemeral=True)
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            return await interaction.response.send_message("Pick a normal text channel.", ephemeral=True)

        end_time = int(time.time() + delta.total_seconds())
        gid = await self.bot.db.insert(
            "INSERT INTO giveaways (guild_id, channel_id, host_id, prize, winners, end_time) VALUES (?, ?, ?, ?, ?, ?)",
            (interaction.guild_id, target.id, interaction.user.id, prize, winners, end_time),
        )
        row = await self.bot.db.fetchone("SELECT * FROM giveaways WHERE id = ?", (gid,))
        view = discord.ui.View(timeout=None)
        view.add_item(GiveawayButton(gid))
        try:
            message = await target.send(embed=giveaway_embed(row), view=view)
        except discord.HTTPException:
            await self.bot.db.execute("DELETE FROM giveaways WHERE id = ?", (gid,))
            return await interaction.response.send_message(f"I can't post in {target.mention}.", ephemeral=True)
        await self.bot.db.execute("UPDATE giveaways SET message_id = ? WHERE id = ?", (message.id, gid))
        await interaction.response.send_message(f"Giveaway #{gid} started in {target.mention}.", ephemeral=True)

    async def _own_giveaway(self, interaction: discord.Interaction, giveaway_id: int) -> Optional[sqlite3.Row]:
        row = await self.bot.db.fetchone("SELECT * FROM giveaways WHERE id = ? AND guild_id = ?", (giveaway_id, interaction.guild_id))
        if row is None:
            await interaction.response.send_message("No giveaway with that ID in this server.", ephemeral=True)
        return row

    @giveaway.command(name="end", description="End a giveaway right now and pick winners.")
    @app_commands.describe(giveaway_id="ID shown in the giveaway's footer")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def end(self, interaction: discord.Interaction, giveaway_id: int):
        row = await self._own_giveaway(interaction, giveaway_id)
        if row is None:
            return
        if row["ended"]:
            return await interaction.response.send_message("That giveaway already ended. Use `/giveaway reroll`.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await self.bot.db.execute("UPDATE giveaways SET end_time = ? WHERE id = ?", (int(time.time()), giveaway_id))
        await self._end(giveaway_id)
        await interaction.followup.send("Giveaway ended.", ephemeral=True)

    @giveaway.command(name="reroll", description="Pick new winner(s) for a finished giveaway.")
    @app_commands.describe(giveaway_id="ID shown in the giveaway's footer")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reroll(self, interaction: discord.Interaction, giveaway_id: int):
        row = await self._own_giveaway(interaction, giveaway_id)
        if row is None:
            return
        if not row["ended"]:
            return await interaction.response.send_message("That giveaway is still running.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await self._end(giveaway_id, reroll=True)
        await interaction.followup.send("Rerolled.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Giveaways(bot))
