"""Button role panels: members click a button to toggle a role on or off."""
from __future__ import annotations

import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import role_assign_error

log = logging.getLogger(__name__)


class RoleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"btnrole:(?P<role_id>[0-9]+)"):
    """A persistent button whose custom_id carries the role it toggles."""

    def __init__(self, role_id: int, label: str = "Role") -> None:
        super().__init__(
            discord.ui.Button(label=label[:80], style=discord.ButtonStyle.secondary, custom_id=f"btnrole:{role_id}")
        )
        self.role_id = role_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(int(match["role_id"]), item.label or "Role")

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            return

        # Only roles an admin explicitly put on a panel may ever be handed out.
        allowed = await interaction.client.db.fetchone(
            "SELECT 1 FROM panel_roles WHERE guild_id = ? AND role_id = ?", (guild.id, self.role_id)
        )
        role = guild.get_role(self.role_id)
        if allowed is None or role is None:
            return await interaction.response.send_message("That role is no longer available.", ephemeral=True)
        if (err := role_assign_error(guild, None, role)):
            return await interaction.response.send_message(err, ephemeral=True)

        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Role panel")
                await interaction.response.send_message(f"Removed {role.mention}.", ephemeral=True)
            else:
                await member.add_roles(role, reason="Role panel")
                await interaction.response.send_message(f"You now have {role.mention}.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("I don't have permission to change that role.", ephemeral=True)


class Roles(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(RoleButton)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(RoleButton)

    rolepanel = app_commands.Group(
        name="rolepanel", description="Self-assign role panels with buttons.",
        default_permissions=discord.Permissions(manage_roles=True), guild_only=True,
    )

    @rolepanel.command(name="create", description="Post a panel where members click buttons to get roles.")
    @app_commands.describe(
        title="Panel title", description="Text shown above the buttons",
        role1="First role", role2="Second role", role3="Third role", role4="Fourth role", role5="Fifth role",
        channel="Where to post it (default: here)",
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def create(
        self,
        interaction: discord.Interaction,
        title: app_commands.Range[str, 1, 100],
        description: app_commands.Range[str, 1, 1000],
        role1: discord.Role,
        role2: Optional[discord.Role] = None,
        role3: Optional[discord.Role] = None,
        role4: Optional[discord.Role] = None,
        role5: Optional[discord.Role] = None,
        channel: Optional[discord.TextChannel] = None,
    ):
        roles: list[discord.Role] = []
        for r in (role1, role2, role3, role4, role5):
            if r is not None and r not in roles:
                roles.append(r)
        for r in roles:
            if (err := role_assign_error(interaction.guild, interaction.user, r)):
                return await interaction.response.send_message(err, ephemeral=True)

        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            return await interaction.response.send_message("Pick a normal text channel.", ephemeral=True)

        view = discord.ui.View(timeout=None)
        for r in roles:
            view.add_item(RoleButton(r.id, r.name))
        embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
        embed.set_footer(text="Click a button to get the role. Click again to remove it.")
        try:
            await target.send(embed=embed, view=view)
        except discord.Forbidden:
            return await interaction.response.send_message(f"I can't send messages in {target.mention}.", ephemeral=True)

        for r in roles:
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO panel_roles (guild_id, role_id) VALUES (?, ?)", (interaction.guild_id, r.id)
            )
        await interaction.response.send_message(f"Role panel posted in {target.mention}.", ephemeral=True)

    @rolepanel.command(name="revoke", description="Stop panel buttons from handing out a role.")
    @app_commands.describe(role="Role to disable on all panels")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def revoke(self, interaction: discord.Interaction, role: discord.Role):
        count = await self.bot.db.execute(
            "DELETE FROM panel_roles WHERE guild_id = ? AND role_id = ?", (interaction.guild_id, role.id)
        )
        msg = f"{role.mention} can no longer be claimed from panels." if count else f"{role.mention} wasn't on any panel."
        await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Roles(bot))
