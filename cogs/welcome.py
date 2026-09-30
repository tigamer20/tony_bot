"""Welcome / goodbye messages and an optional auto-role for new members."""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import render_template, role_assign_error

log = logging.getLogger(__name__)

DEFAULT_WELCOME = "Welcome to **{server}**, {user}! You are member #{count}."
DEFAULT_GOODBYE = "**{name}** has left {server}."


class Welcome(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _send(self, guild: discord.Guild, channel_key: str, message_key: str, default: str, member: discord.Member, *, mention: bool) -> None:
        db = self.bot.db
        channel_id = await db.get_config_int(guild.id, channel_key)
        channel = guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            return
        template = await db.get_config(guild.id, message_key, default)
        try:
            await channel.send(
                render_template(template, member, mention=mention),
                allowed_mentions=discord.AllowedMentions(users=[member]) if mention else discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.warning("Could not send %s in guild %s", message_key, guild.id)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        guild = member.guild
        role_id = await self.bot.db.get_config_int(guild.id, "autorole")
        role = guild.get_role(role_id) if role_id else None
        if role is not None and not role_assign_error(guild, None, role):
            try:
                await member.add_roles(role, reason="Autorole")
            except discord.HTTPException:
                log.warning("Could not assign autorole in guild %s", guild.id)
        await self._send(guild, "welcome_channel", "welcome_message", DEFAULT_WELCOME, member, mention=True)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        await self._send(member.guild, "goodbye_channel", "goodbye_message", DEFAULT_GOODBYE, member, mention=False)

    # ------------------------------------------------------------------ commands
    welcome = app_commands.Group(
        name="welcome", description="Configure welcome/goodbye messages and the auto-role.",
        default_permissions=discord.Permissions(manage_guild=True), guild_only=True,
    )

    @welcome.command(name="channel", description="Set the channel for welcome messages (leave empty to turn off).")
    @app_commands.describe(channel="Welcome channel")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def welcome_channel(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        await self._set_channel(interaction, "welcome_channel", channel, "Welcome messages")

    @welcome.command(name="message", description="Set the welcome text. Use {user} {name} {server} {count}.")
    @app_commands.describe(text="Message text")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def welcome_message(self, interaction: discord.Interaction, text: app_commands.Range[str, 1, 1000]):
        await self.bot.db.set_config(interaction.guild_id, "welcome_message", text)
        await interaction.response.send_message("Welcome message updated. Try it with `/welcome test`.", ephemeral=True)

    @welcome.command(name="goodbye_channel", description="Set the channel for goodbye messages (leave empty to turn off).")
    @app_commands.describe(channel="Goodbye channel")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def goodbye_channel(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        await self._set_channel(interaction, "goodbye_channel", channel, "Goodbye messages")

    @welcome.command(name="goodbye_message", description="Set the goodbye text. Use {name} {server} {count}.")
    @app_commands.describe(text="Message text")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def goodbye_message(self, interaction: discord.Interaction, text: app_commands.Range[str, 1, 1000]):
        await self.bot.db.set_config(interaction.guild_id, "goodbye_message", text)
        await interaction.response.send_message("Goodbye message updated.", ephemeral=True)

    @welcome.command(name="autorole", description="Give new members a role automatically (leave empty to turn off).")
    @app_commands.describe(role="Role to give on join")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def autorole(self, interaction: discord.Interaction, role: Optional[discord.Role] = None):
        if role is None:
            await self.bot.db.delete_config(interaction.guild_id, "autorole")
            return await interaction.response.send_message("Auto-role turned off.", ephemeral=True)
        if (err := role_assign_error(interaction.guild, interaction.user, role)):
            return await interaction.response.send_message(err, ephemeral=True)
        await self.bot.db.set_config(interaction.guild_id, "autorole", role.id)
        await interaction.response.send_message(f"New members will get {role.mention}.", ephemeral=True)

    @welcome.command(name="test", description="Preview the welcome message with yourself as the new member.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def test(self, interaction: discord.Interaction):
        template = await self.bot.db.get_config(interaction.guild_id, "welcome_message", DEFAULT_WELCOME)
        await interaction.response.send_message(
            render_template(template, interaction.user), ephemeral=True,
        )

    async def _set_channel(self, interaction: discord.Interaction, key: str, channel: Optional[discord.TextChannel], label: str) -> None:
        if channel is None:
            await self.bot.db.delete_config(interaction.guild_id, key)
            return await interaction.response.send_message(f"{label} turned off.", ephemeral=True)
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages):
            return await interaction.response.send_message(f"I can't send messages in {channel.mention}.", ephemeral=True)
        await self.bot.db.set_config(interaction.guild_id, key, channel.id)
        await interaction.response.send_message(f"{label} will be sent in {channel.mention}.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Welcome(bot))
