"""XP and levels: earn XP by chatting, climb the leaderboard, unlock role rewards."""
from __future__ import annotations

import logging
import random
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import level_from_xp, level_progress, progress_bar, role_assign_error

log = logging.getLogger(__name__)

XP_COOLDOWN_SECONDS = 60
XP_MIN, XP_MAX = 15, 25


class Levels(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._cooldowns: dict[tuple[int, int], float] = {}
        self._cfg_cache: dict[int, dict[str, str]] = {}

    # ------------------------------------------------------------------ config
    async def _cfg(self, guild_id: int) -> dict[str, str]:
        if guild_id not in self._cfg_cache:
            self._cfg_cache[guild_id] = await self.bot.db.get_configs(guild_id, "levels_")
        return self._cfg_cache[guild_id]

    async def _set_cfg(self, guild_id: int, key: str, value) -> None:
        await self.bot.db.set_config(guild_id, f"levels_{key}", value)
        self._cfg_cache.pop(guild_id, None)

    # ------------------------------------------------------------------ rewards
    async def _grant_rewards(self, member: discord.Member, level: int) -> list[discord.Role]:
        rows = await self.bot.db.fetchall(
            "SELECT role_id FROM level_rewards WHERE guild_id = ? AND level <= ?", (member.guild.id, level)
        )
        granted: list[discord.Role] = []
        for row in rows:
            role = member.guild.get_role(row["role_id"])
            if role is None or role in member.roles or role_assign_error(member.guild, None, role):
                continue
            try:
                await member.add_roles(role, reason=f"Level {level} reward")
                granted.append(role)
            except discord.HTTPException:
                log.warning("Could not grant reward role %s in guild %s", role.id, member.guild.id)
        return granted

    # ------------------------------------------------------------------ xp gain
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or not isinstance(message.author, discord.Member):
            return

        key = (message.guild.id, message.author.id)
        now = time.monotonic()
        if now - self._cooldowns.get(key, -XP_COOLDOWN_SECONDS) < XP_COOLDOWN_SECONDS:
            return
        cfg = await self._cfg(message.guild.id)
        if cfg.get("enabled", "1") != "1":
            return
        self._cooldowns[key] = now

        db = self.bot.db
        row = await db.fetchone(
            "SELECT xp, level FROM levels WHERE guild_id = ? AND user_id = ?", key
        )
        old_level = row["level"] if row else 0
        xp = (row["xp"] if row else 0) + random.randint(XP_MIN, XP_MAX)
        new_level = level_from_xp(xp)
        await db.execute(
            """INSERT INTO levels (guild_id, user_id, xp, level, messages) VALUES (?, ?, ?, ?, 1)
               ON CONFLICT(guild_id, user_id) DO UPDATE
               SET xp = excluded.xp, level = excluded.level, messages = messages + 1""",
            (*key, xp, new_level),
        )

        if new_level > old_level:
            granted = await self._grant_rewards(message.author, new_level)
            if cfg.get("announce", "1") == "1":
                text = f"\N{PARTY POPPER} {message.author.mention} reached **level {new_level}**!"
                if granted:
                    text += " New role: " + ", ".join(r.mention for r in granted)
                channel = message.channel
                if cfg.get("channel"):
                    channel = message.guild.get_channel(int(cfg["channel"])) or channel
                try:
                    await channel.send(text, allowed_mentions=discord.AllowedMentions(users=[message.author]))
                except discord.HTTPException:
                    pass

    # ------------------------------------------------------------------ commands
    @app_commands.command(name="rank", description="Show your level and XP (or someone else's).")
    @app_commands.describe(member="Member to look up")
    @app_commands.guild_only()
    async def rank(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        member = member or interaction.user
        if member.bot:
            return await interaction.response.send_message("Bots don't earn XP.", ephemeral=True)
        db = self.bot.db
        row = await db.fetchone(
            "SELECT xp, messages FROM levels WHERE guild_id = ? AND user_id = ?", (interaction.guild_id, member.id)
        )
        xp = row["xp"] if row else 0
        level, into, needed = level_progress(xp)
        pos = await db.fetchone(
            "SELECT COUNT(*) + 1 AS pos FROM levels WHERE guild_id = ? AND xp > ?", (interaction.guild_id, xp)
        )
        embed = discord.Embed(title=member.display_name, color=member.color if member.color.value else discord.Color.blurple())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Level", value=str(level))
        embed.add_field(name="Rank", value=f"#{pos['pos']}")
        embed.add_field(name="Messages", value=str(row["messages"] if row else 0))
        embed.add_field(
            name=f"Progress to level {level + 1}",
            value=f"`{progress_bar(into / needed)}` {into:,} / {needed:,} XP\nTotal: {xp:,} XP",
            inline=False,
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leaderboard", description="Show the top 10 most active members.")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall(
            "SELECT user_id, xp FROM levels WHERE guild_id = ? ORDER BY xp DESC LIMIT 10", (interaction.guild_id,)
        )
        if not rows:
            return await interaction.response.send_message("Nobody has earned any XP yet. Start chatting!")
        medals = {1: "\N{FIRST PLACE MEDAL}", 2: "\N{SECOND PLACE MEDAL}", 3: "\N{THIRD PLACE MEDAL}"}
        lines = [
            f"{medals.get(i, f'`{i}.`')} <@{r['user_id']}> · level {level_from_xp(r['xp'])} · {r['xp']:,} XP"
            for i, r in enumerate(rows, start=1)
        ]
        embed = discord.Embed(title=f"{interaction.guild.name} leaderboard", description="\n".join(lines), color=discord.Color.gold())
        await interaction.response.send_message(embed=embed)

    # ---- settings
    levels = app_commands.Group(
        name="levels", description="Configure the levels system.",
        default_permissions=discord.Permissions(manage_guild=True), guild_only=True,
    )

    @levels.command(name="config", description="Turn levels or level-up announcements on/off, or pick an announce channel.")
    @app_commands.describe(enabled="Earn XP at all?", announce="Announce level-ups?", channel="Send announcements here (default: the chat channel)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def levels_config(
        self,
        interaction: discord.Interaction,
        enabled: Optional[bool] = None,
        announce: Optional[bool] = None,
        channel: Optional[discord.TextChannel] = None,
    ):
        gid = interaction.guild_id
        if enabled is not None:
            await self._set_cfg(gid, "enabled", int(enabled))
        if announce is not None:
            await self._set_cfg(gid, "announce", int(announce))
        if channel is not None:
            await self._set_cfg(gid, "channel", channel.id)
        cfg = await self._cfg(gid)
        ch = f"<#{cfg['channel']}>" if cfg.get("channel") else "the channel where they chat"
        await interaction.response.send_message(
            f"Levels: **{'on' if cfg.get('enabled', '1') == '1' else 'off'}** · "
            f"Announcements: **{'on' if cfg.get('announce', '1') == '1' else 'off'}** in {ch}",
            ephemeral=True,
        )

    # ---- role rewards
    levelreward = app_commands.Group(
        name="levelreward", description="Roles given when members reach a level.",
        default_permissions=discord.Permissions(manage_roles=True), guild_only=True,
    )

    @levelreward.command(name="add", description="Give a role when members reach a level.")
    @app_commands.describe(level="Level that unlocks the role", role="Role to give")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def reward_add(self, interaction: discord.Interaction, level: app_commands.Range[int, 1, 500], role: discord.Role):
        if (err := role_assign_error(interaction.guild, interaction.user, role)):
            return await interaction.response.send_message(err, ephemeral=True)
        await self.bot.db.execute(
            """INSERT INTO level_rewards (guild_id, level, role_id) VALUES (?, ?, ?)
               ON CONFLICT(guild_id, level) DO UPDATE SET role_id = excluded.role_id""",
            (interaction.guild_id, level, role.id),
        )
        await interaction.response.send_message(f"Members reaching level **{level}** will now get {role.mention}.")

    @levelreward.command(name="remove", description="Remove the reward for a level.")
    @app_commands.describe(level="Level whose reward to remove")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def reward_remove(self, interaction: discord.Interaction, level: app_commands.Range[int, 1, 500]):
        count = await self.bot.db.execute(
            "DELETE FROM level_rewards WHERE guild_id = ? AND level = ?", (interaction.guild_id, level)
        )
        await interaction.response.send_message("Reward removed." if count else "There's no reward at that level.", ephemeral=True)

    @levelreward.command(name="list", description="List all level rewards.")
    async def reward_list(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall(
            "SELECT level, role_id FROM level_rewards WHERE guild_id = ? ORDER BY level", (interaction.guild_id,)
        )
        if not rows:
            return await interaction.response.send_message("No level rewards set. Add one with `/levelreward add`.", ephemeral=True)
        await interaction.response.send_message("\n".join(f"Level **{r['level']}** → <@&{r['role_id']}>" for r in rows), ephemeral=True)

    # ---- admin xp tools
    xp = app_commands.Group(
        name="xp", description="Adjust members' XP.",
        default_permissions=discord.Permissions(administrator=True), guild_only=True,
    )

    @xp.command(name="give", description="Give a member XP.")
    @app_commands.describe(member="Member to give XP to", amount="How much XP")
    @app_commands.checks.has_permissions(administrator=True)
    async def xp_give(self, interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 1_000_000]):
        if member.bot:
            return await interaction.response.send_message("Bots don't earn XP.", ephemeral=True)
        db = self.bot.db
        row = await db.fetchone("SELECT xp FROM levels WHERE guild_id = ? AND user_id = ?", (interaction.guild_id, member.id))
        total = (row["xp"] if row else 0) + amount
        level = level_from_xp(total)
        await db.execute(
            """INSERT INTO levels (guild_id, user_id, xp, level, messages) VALUES (?, ?, ?, ?, 0)
               ON CONFLICT(guild_id, user_id) DO UPDATE SET xp = excluded.xp, level = excluded.level""",
            (interaction.guild_id, member.id, total, level),
        )
        await self._grant_rewards(member, level)
        await interaction.response.send_message(f"Gave {amount:,} XP to {member.mention}. They're now level **{level}** ({total:,} XP).")

    @xp.command(name="reset", description="Reset a member's XP to zero.")
    @app_commands.describe(member="Member to reset")
    @app_commands.checks.has_permissions(administrator=True)
    async def xp_reset(self, interaction: discord.Interaction, member: discord.Member):
        await self.bot.db.execute("DELETE FROM levels WHERE guild_id = ? AND user_id = ?", (interaction.guild_id, member.id))
        await interaction.response.send_message(f"Reset {member.mention}'s XP.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Levels(bot))
