"""Moderation slash commands: kick, ban, unban, timeout, purge, warnings, mod-log."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import COLORS, audit_reason, parse_duration, send_modlog

log = logging.getLogger(__name__)

MAX_TIMEOUT = timedelta(days=28)  # Discord's hard limit


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ helpers
    def _hierarchy_error(
        self, interaction: discord.Interaction, target: discord.Member
    ) -> Optional[str]:
        """Return a message explaining why the action is not allowed, or None if OK."""
        guild = interaction.guild
        assert guild is not None
        moderator = interaction.user
        assert isinstance(moderator, discord.Member)

        if target.id == moderator.id:
            return "You can't do that to yourself."
        if target.id == guild.me.id:
            return "I can't do that to myself."
        if target.id == guild.owner_id:
            return "You can't moderate the server owner."
        if moderator.id != guild.owner_id and target.top_role >= moderator.top_role:
            return "That member's highest role is equal to or above yours."
        if target.top_role >= guild.me.top_role:
            return "That member's highest role is equal to or above mine. Move my role higher in Server Settings > Roles."
        return None

    async def _dm(self, member: discord.abc.User, action: str, guild: discord.Guild, reason: str, extra: str = "") -> None:
        """Best-effort DM; users with DMs closed are simply skipped."""
        try:
            embed = discord.Embed(
                title=f"You were {action} in {guild.name}",
                description=f"**Reason:** {reason}{extra}",
                color=COLORS.get(action.split()[0], discord.Color.greyple()),
            )
            await member.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _log(
        self,
        guild: discord.Guild,
        action: str,
        moderator: discord.abc.User,
        target: Optional[discord.abc.User] = None,
        reason: Optional[str] = None,
        extra: Optional[str] = None,
    ) -> None:
        await send_modlog(self.bot, guild, action, moderator, target, reason, extra)

    # ------------------------------------------------------------------ kick
    @app_commands.command(name="kick", description="Kick a member from the server.")
    @app_commands.describe(member="Member to kick", reason="Why they're being kicked")
    @app_commands.guild_only()
    @app_commands.default_permissions(kick_members=True)
    @app_commands.checks.has_permissions(kick_members=True)
    @app_commands.checks.bot_has_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        if (err := self._hierarchy_error(interaction, member)):
            return await interaction.response.send_message(err, ephemeral=True)

        await interaction.response.defer()
        await self._dm(member, "kicked", interaction.guild, reason)  # type: ignore[arg-type]
        await member.kick(reason=audit_reason(interaction.user, reason))
        await interaction.followup.send(f"Kicked **{member}**. Reason: {reason}")
        await self._log(interaction.guild, "kick", interaction.user, member, reason)  # type: ignore[arg-type]

    # ------------------------------------------------------------------ ban
    @app_commands.command(name="ban", description="Ban a user from the server.")
    @app_commands.describe(
        user="User to ban (can be someone who already left)",
        reason="Why they're being banned",
        delete_days="Days of their messages to delete (0-7)",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def ban(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        reason: str = "No reason provided",
        delete_days: app_commands.Range[int, 0, 7] = 0,
    ):
        guild = interaction.guild
        assert guild is not None
        member = guild.get_member(user.id)
        if member is not None:
            if (err := self._hierarchy_error(interaction, member)):
                return await interaction.response.send_message(err, ephemeral=True)
        elif user.id in (interaction.user.id, guild.me.id):
            return await interaction.response.send_message("Nice try.", ephemeral=True)

        await interaction.response.defer()
        if member is not None:
            await self._dm(user, "banned", guild, reason)
        await guild.ban(
            user,
            reason=audit_reason(interaction.user, reason),
            delete_message_seconds=delete_days * 86400,
        )
        await interaction.followup.send(f"Banned **{user}**. Reason: {reason}")
        await self._log(guild, "ban", interaction.user, user, reason)

    @app_commands.command(name="unban", description="Unban a user by their ID.")
    @app_commands.describe(user_id="The user's ID (Server Settings > Bans)", reason="Why they're being unbanned")
    @app_commands.guild_only()
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str = "No reason provided"):
        guild = interaction.guild
        assert guild is not None
        if not user_id.isdigit():
            return await interaction.response.send_message("That doesn't look like a valid user ID.", ephemeral=True)

        try:
            entry = await guild.fetch_ban(discord.Object(id=int(user_id)))
        except discord.NotFound:
            return await interaction.response.send_message("That user isn't banned.", ephemeral=True)

        await guild.unban(entry.user, reason=audit_reason(interaction.user, reason))
        await interaction.response.send_message(f"Unbanned **{entry.user}**.")
        await self._log(guild, "unban", interaction.user, entry.user, reason)

    # ------------------------------------------------------------------ timeout
    @app_commands.command(name="timeout", description="Temporarily mute a member (e.g. 10m, 2h, 1d).")
    @app_commands.describe(member="Member to time out", duration="How long: 30s, 10m, 2h30m, 1d, 1w (max 28d)", reason="Why")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def timeout(
        self, interaction: discord.Interaction, member: discord.Member, duration: str, reason: str = "No reason provided"
    ):
        delta = parse_duration(duration)
        if delta is None:
            return await interaction.response.send_message(
                "Couldn't read that duration. Try something like `10m`, `2h`, `1d`, or `1h30m`.", ephemeral=True
            )
        if delta > MAX_TIMEOUT:
            return await interaction.response.send_message("The maximum timeout is 28 days.", ephemeral=True)
        if (err := self._hierarchy_error(interaction, member)):
            return await interaction.response.send_message(err, ephemeral=True)

        await member.timeout(delta, reason=audit_reason(interaction.user, reason))
        until = discord.utils.format_dt(discord.utils.utcnow() + delta, style="R")
        await interaction.response.send_message(f"Timed out **{member}** (ends {until}). Reason: {reason}")
        await self._dm(member, "timed out", interaction.guild, reason, f"\n**Ends:** {until}")  # type: ignore[arg-type]
        await self._log(interaction.guild, "timeout", interaction.user, member, reason, f"Duration: {duration}")  # type: ignore[arg-type]

    @app_commands.command(name="untimeout", description="Remove a member's timeout.")
    @app_commands.describe(member="Member to un-timeout")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member):
        if not member.is_timed_out():
            return await interaction.response.send_message("That member isn't timed out.", ephemeral=True)
        await member.timeout(None, reason=audit_reason(interaction.user, "Timeout removed"))
        await interaction.response.send_message(f"Removed the timeout from **{member}**.")
        await self._log(interaction.guild, "untimeout", interaction.user, member)  # type: ignore[arg-type]

    # ------------------------------------------------------------------ purge
    @app_commands.command(name="purge", description="Bulk-delete recent messages in this channel.")
    @app_commands.describe(amount="How many recent messages to check (1-100)", member="Only delete this member's messages")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.checks.bot_has_permissions(manage_messages=True, read_message_history=True)
    async def purge(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 100],
        member: Optional[discord.Member] = None,
    ):
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
            return await interaction.response.send_message("I can't purge this kind of channel.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        check = (lambda m: m.author.id == member.id) if member else None
        deleted = await channel.purge(limit=amount, check=check, reason=audit_reason(interaction.user, "purge"))
        await interaction.followup.send(f"Deleted {len(deleted)} message(s).", ephemeral=True)
        await self._log(
            interaction.guild, "purge", interaction.user, member,  # type: ignore[arg-type]
            extra=f"{len(deleted)} message(s) in {channel.mention}",
        )

    # ------------------------------------------------------------------ warnings
    @app_commands.command(name="warn", description="Issue a warning to a member.")
    @app_commands.describe(member="Member to warn", reason="Why they're being warned")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        if (err := self._hierarchy_error(interaction, member)):
            return await interaction.response.send_message(err, ephemeral=True)

        total = await self.bot.db.add_warning(interaction.guild_id, member.id, interaction.user.id, reason)  # type: ignore[attr-defined]
        await interaction.response.send_message(f"Warned **{member}** (warning #{total}). Reason: {reason}")
        await self._dm(member, "warned", interaction.guild, reason, f"\n**Total warnings:** {total}")  # type: ignore[arg-type]
        await self._log(interaction.guild, "warn", interaction.user, member, reason, f"Total warnings: {total}")  # type: ignore[arg-type]

    @app_commands.command(name="warnings", description="List a member's warnings.")
    @app_commands.describe(member="Member to look up")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warnings(self, interaction: discord.Interaction, member: discord.Member):
        rows = await self.bot.db.get_warnings(interaction.guild_id, member.id)  # type: ignore[attr-defined]
        if not rows:
            return await interaction.response.send_message(f"**{member}** has no warnings.", ephemeral=True)

        embed = discord.Embed(title=f"Warnings for {member}", color=COLORS["warn"])
        for row in rows[:10]:
            when = discord.utils.format_dt(datetime.fromtimestamp(row["created_at"], tz=timezone.utc), style="d")
            embed.add_field(
                name=f"#{row['id']} · {when}",
                value=f"{row['reason'][:200]}\n— <@{row['moderator_id']}>",
                inline=False,
            )
        if len(rows) > 10:
            embed.set_footer(text=f"Showing latest 10 of {len(rows)}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="clearwarnings", description="Delete all of a member's warnings.")
    @app_commands.describe(member="Member whose warnings to clear")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def clearwarnings(self, interaction: discord.Interaction, member: discord.Member):
        count = await self.bot.db.clear_warnings(interaction.guild_id, member.id)  # type: ignore[attr-defined]
        await interaction.response.send_message(f"Cleared {count} warning(s) for **{member}**.")

    # ------------------------------------------------------------------ mod-log
    @app_commands.command(name="modlog", description="Set (or disable) the channel where moderation actions are logged.")
    @app_commands.describe(channel="Channel for mod logs. Leave empty to turn logging off.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def modlog(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        await self.bot.db.set_modlog_channel(interaction.guild_id, channel.id if channel else None)  # type: ignore[attr-defined]
        if channel:
            await interaction.response.send_message(f"Moderation actions will be logged in {channel.mention}.")
        else:
            await interaction.response.send_message("Mod logging turned off.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
