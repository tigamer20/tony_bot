"""Auto-moderation: spam, invite links, banned words and mass mentions.

Everything is OFF by default. Turn features on with /automod toggle.
Members with Manage Messages (or Administrator) are always exempt.
"""
from __future__ import annotations

import logging
import re
import time
from collections import defaultdict, deque
from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import send_modlog

log = logging.getLogger(__name__)

INVITE_RE = re.compile(r"(?:discord\.gg|discord(?:app)?\.com/invite)/[\w-]+", re.IGNORECASE)
FEATURES = ("spam", "invites", "words", "mentions")
DEFAULTS = {
    "spam": "0", "invites": "0", "words": "0", "mentions": "0",
    "spam_limit": "5", "spam_seconds": "5", "mention_limit": "5",
}
SPAM_TIMEOUT = timedelta(minutes=5)
MAX_WORDS = 200


class AutoMod(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._cfg_cache: dict[int, dict[str, str]] = {}
        self._word_cache: dict[int, Optional[re.Pattern]] = {}
        self._recent: dict[tuple[int, int], deque[float]] = defaultdict(deque)

    # ------------------------------------------------------------------ config
    async def _cfg(self, guild_id: int) -> dict[str, str]:
        if guild_id not in self._cfg_cache:
            stored = await self.bot.db.get_configs(guild_id, "automod_")
            self._cfg_cache[guild_id] = {**DEFAULTS, **stored}
        return self._cfg_cache[guild_id]

    async def _set(self, guild_id: int, key: str, value) -> None:
        await self.bot.db.set_config(guild_id, f"automod_{key}", value)
        self._cfg_cache.pop(guild_id, None)

    async def _word_pattern(self, guild_id: int) -> Optional[re.Pattern]:
        if guild_id not in self._word_cache:
            rows = await self.bot.db.fetchall("SELECT word FROM automod_words WHERE guild_id = ?", (guild_id,))
            words = [r["word"] for r in rows]
            self._word_cache[guild_id] = (
                re.compile(r"(?<!\w)(?:" + "|".join(re.escape(w) for w in words) + r")(?!\w)", re.IGNORECASE)
                if words else None
            )
        return self._word_cache[guild_id]

    # ------------------------------------------------------------------ enforcement
    async def _punish(self, message: discord.Message, reason: str, *, timeout: Optional[timedelta] = None) -> None:
        author = message.author
        guild = message.guild
        assert guild is not None and isinstance(author, discord.Member)

        try:
            await message.delete()
        except discord.HTTPException:
            pass
        try:
            await message.channel.send(
                f"{author.mention}, your message was removed: {reason}.",
                delete_after=6, allowed_mentions=discord.AllowedMentions(users=[author]),
            )
        except discord.HTTPException:
            pass

        total = await self.bot.db.add_warning(guild.id, author.id, self.bot.user.id, f"[AutoMod] {reason}")
        extra = f"Warnings on record: {total}"
        if timeout is not None:
            try:
                await author.timeout(timeout, reason=f"AutoMod: {reason}")
                extra += f"\nTimed out for {int(timeout.total_seconds() // 60)} minutes"
            except discord.HTTPException:
                extra += "\n(Could not time out: check my role and Moderate Members permission)"
        await send_modlog(self.bot, guild, "automod", self.bot.user, author, reason, extra)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or not isinstance(message.author, discord.Member):
            return
        perms = message.author.guild_permissions
        if perms.manage_messages or perms.administrator:
            return

        guild_id = message.guild.id
        cfg = await self._cfg(guild_id)
        if not any(cfg[f] == "1" for f in FEATURES):
            return

        if cfg["invites"] == "1" and INVITE_RE.search(message.content):
            return await self._punish(message, "invite links aren't allowed here")

        if cfg["words"] == "1":
            pattern = await self._word_pattern(guild_id)
            if pattern and pattern.search(message.content):
                return await self._punish(message, "that language isn't allowed here")

        if cfg["mentions"] == "1":
            unique = {m.id for m in message.mentions} | {r.id for r in message.role_mentions}
            if len(unique) >= int(cfg["mention_limit"]):
                return await self._punish(message, "too many mentions in one message")

        if cfg["spam"] == "1":
            key = (guild_id, message.author.id)
            now = time.monotonic()
            window = float(cfg["spam_seconds"])
            recent = self._recent[key]
            recent.append(now)
            while recent and now - recent[0] > window:
                recent.popleft()
            if len(recent) >= int(cfg["spam_limit"]):
                recent.clear()
                try:  # sweep the burst that triggered it
                    await message.channel.purge(
                        limit=25, check=lambda m: m.author.id == message.author.id,
                        after=discord.utils.utcnow() - timedelta(seconds=window + 5),
                    )
                except discord.HTTPException:
                    pass
                await self._punish(message, "please don't spam", timeout=SPAM_TIMEOUT)

    # ------------------------------------------------------------------ commands
    automod = app_commands.Group(
        name="automod", description="Configure automatic moderation.",
        default_permissions=discord.Permissions(manage_guild=True), guild_only=True,
    )

    @automod.command(name="toggle", description="Turn an auto-moderation feature on or off.")
    @app_commands.describe(feature="Which filter", enabled="On or off")
    @app_commands.choices(feature=[
        app_commands.Choice(name="Spam (delete + 5 minute timeout)", value="spam"),
        app_commands.Choice(name="Invite links", value="invites"),
        app_commands.Choice(name="Banned words", value="words"),
        app_commands.Choice(name="Mass mentions", value="mentions"),
    ])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def toggle(self, interaction: discord.Interaction, feature: app_commands.Choice[str], enabled: bool):
        await self._set(interaction.guild_id, feature.value, int(enabled))
        await interaction.response.send_message(
            f"**{feature.value}** filter is now **{'on' if enabled else 'off'}**.", ephemeral=True
        )

    @automod.command(name="spam_limit", description="Set how many messages within how many seconds count as spam.")
    @app_commands.describe(messages="Messages allowed (2-20)", seconds="...within this many seconds (1-30)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def spam_limit(self, interaction: discord.Interaction, messages: app_commands.Range[int, 2, 20], seconds: app_commands.Range[int, 1, 30]):
        await self._set(interaction.guild_id, "spam_limit", messages)
        await self._set(interaction.guild_id, "spam_seconds", seconds)
        await interaction.response.send_message(f"Spam = {messages} messages in {seconds}s.", ephemeral=True)

    @automod.command(name="mention_limit", description="Set how many unique mentions in one message trigger the filter.")
    @app_commands.describe(count="Unique user/role mentions (2-50)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def mention_limit(self, interaction: discord.Interaction, count: app_commands.Range[int, 2, 50]):
        await self._set(interaction.guild_id, "mention_limit", count)
        await interaction.response.send_message(f"Messages with {count} or more unique mentions will be removed.", ephemeral=True)

    @automod.command(name="word_add", description="Add a word or phrase to the banned list.")
    @app_commands.describe(word="Word or phrase (matched as whole words, case-insensitive)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def word_add(self, interaction: discord.Interaction, word: app_commands.Range[str, 2, 50]):
        word = word.strip().lower()
        count = await self.bot.db.fetchone("SELECT COUNT(*) AS n FROM automod_words WHERE guild_id = ?", (interaction.guild_id,))
        if count["n"] >= MAX_WORDS:
            return await interaction.response.send_message(f"The banned list is full ({MAX_WORDS} words).", ephemeral=True)
        await self.bot.db.execute("INSERT OR IGNORE INTO automod_words (guild_id, word) VALUES (?, ?)", (interaction.guild_id, word))
        self._word_cache.pop(interaction.guild_id, None)
        await interaction.response.send_message("Added. (Remember to turn the **words** filter on with `/automod toggle`.)", ephemeral=True)

    @automod.command(name="word_remove", description="Remove a word from the banned list.")
    @app_commands.describe(word="Word or phrase to remove")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def word_remove(self, interaction: discord.Interaction, word: app_commands.Range[str, 2, 50]):
        count = await self.bot.db.execute("DELETE FROM automod_words WHERE guild_id = ? AND word = ?", (interaction.guild_id, word.strip().lower()))
        self._word_cache.pop(interaction.guild_id, None)
        await interaction.response.send_message("Removed." if count else "That word isn't on the list.", ephemeral=True)

    @automod.command(name="word_list", description="Show the banned words (only visible to you).")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def word_list(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall("SELECT word FROM automod_words WHERE guild_id = ? ORDER BY word", (interaction.guild_id,))
        text = ", ".join(f"||{r['word']}||" for r in rows) or "No banned words yet."
        await interaction.response.send_message(text[:1900], ephemeral=True)

    @automod.command(name="status", description="Show which filters are on.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction):
        cfg = await self._cfg(interaction.guild_id)
        on = lambda k: "on" if cfg[k] == "1" else "off"
        embed = discord.Embed(title="AutoMod status", color=discord.Color.dark_orange())
        embed.add_field(name="Spam", value=f"{on('spam')} ({cfg['spam_limit']} msgs / {cfg['spam_seconds']}s)")
        embed.add_field(name="Invite links", value=on("invites"))
        embed.add_field(name="Banned words", value=on("words"))
        embed.add_field(name="Mass mentions", value=f"{on('mentions')} ({cfg['mention_limit']}+)")
        embed.set_footer(text="Members with Manage Messages are exempt.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AutoMod(bot))
