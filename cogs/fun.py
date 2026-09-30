"""Fun and utility commands: polls, dice, 8ball, info lookups, and /help."""
from __future__ import annotations

import random
import re
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

NUMBER_EMOJIS = ["1\N{COMBINING ENCLOSING KEYCAP}", "2\N{COMBINING ENCLOSING KEYCAP}", "3\N{COMBINING ENCLOSING KEYCAP}",
                 "4\N{COMBINING ENCLOSING KEYCAP}", "5\N{COMBINING ENCLOSING KEYCAP}"]
EIGHT_BALL = [
    "It is certain.", "Without a doubt.", "Yes, definitely.", "You may rely on it.", "Most likely.",
    "Signs point to yes.", "Reply hazy, try again.", "Ask again later.", "Better not tell you now.",
    "Don't count on it.", "My reply is no.", "Very doubtful.",
]
DICE_RE = re.compile(r"^(\d{1,3})d(\d{1,4})$", re.IGNORECASE)


class Fun(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ polls
    @app_commands.command(name="poll", description="Create a reaction poll (2 to 5 options).")
    @app_commands.describe(question="What are you asking?", option1="First option", option2="Second option",
                           option3="Third option", option4="Fourth option", option5="Fifth option")
    @app_commands.guild_only()
    async def poll(
        self,
        interaction: discord.Interaction,
        question: app_commands.Range[str, 1, 250],
        option1: app_commands.Range[str, 1, 100],
        option2: app_commands.Range[str, 1, 100],
        option3: Optional[app_commands.Range[str, 1, 100]] = None,
        option4: Optional[app_commands.Range[str, 1, 100]] = None,
        option5: Optional[app_commands.Range[str, 1, 100]] = None,
    ):
        options = [o for o in (option1, option2, option3, option4, option5) if o]
        embed = discord.Embed(
            title=question,
            description="\n".join(f"{NUMBER_EMOJIS[i]} {text}" for i, text in enumerate(options)),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Poll by {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
        message = await interaction.original_response()
        for i in range(len(options)):
            await message.add_reaction(NUMBER_EMOJIS[i])

    # ------------------------------------------------------------------ games
    @app_commands.command(name="8ball", description="Ask the magic 8-ball a question.")
    @app_commands.describe(question="Your yes/no question")
    async def eight_ball(self, interaction: discord.Interaction, question: app_commands.Range[str, 1, 200]):
        await interaction.response.send_message(f"\N{BILLIARDS} **{question}**\n{random.choice(EIGHT_BALL)}")

    @app_commands.command(name="coinflip", description="Flip a coin.")
    async def coinflip(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"\N{COIN} {random.choice(['Heads', 'Tails'])}!")

    @app_commands.command(name="roll", description="Roll dice, like 2d6 or 1d20.")
    @app_commands.describe(dice="Format NdM, e.g. 2d6 (up to 100 dice, 1000 sides)")
    async def roll(self, interaction: discord.Interaction, dice: str = "1d6"):
        match = DICE_RE.match(dice.strip())
        if not match:
            return await interaction.response.send_message("Use the format `NdM`, like `2d6` or `1d20`.", ephemeral=True)
        count, sides = int(match.group(1)), int(match.group(2))
        if not (1 <= count <= 100 and 2 <= sides <= 1000):
            return await interaction.response.send_message("Roll 1 to 100 dice with 2 to 1000 sides.", ephemeral=True)
        rolls = [random.randint(1, sides) for _ in range(count)]
        detail = f" ({', '.join(map(str, rolls))})" if count <= 20 else ""
        await interaction.response.send_message(f"\N{GAME DIE} **{dice}** → **{sum(rolls)}**{detail}")

    # ------------------------------------------------------------------ info
    @app_commands.command(name="ping", description="Check the bot's latency.")
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"Pong! {round(self.bot.latency * 1000)} ms")

    @app_commands.command(name="avatar", description="Show a member's avatar.")
    @app_commands.describe(member="Member (default: you)")
    async def avatar(self, interaction: discord.Interaction, member: Optional[discord.User] = None):
        member = member or interaction.user
        embed = discord.Embed(title=f"{member.display_name}'s avatar", color=discord.Color.blurple())
        embed.set_image(url=member.display_avatar.with_size(1024).url)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="userinfo", description="Show info about a member.")
    @app_commands.describe(member="Member (default: you)")
    @app_commands.guild_only()
    async def userinfo(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        member = member or interaction.user
        embed = discord.Embed(title=str(member), color=member.color if member.color.value else discord.Color.blurple())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="ID", value=str(member.id))
        embed.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "D"))
        if member.joined_at:
            embed.add_field(name="Joined server", value=discord.utils.format_dt(member.joined_at, "D"))
        roles = [r.mention for r in reversed(member.roles[1:])]
        embed.add_field(name=f"Roles ({len(roles)})", value=" ".join(roles[:15]) or "None", inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="serverinfo", description="Show info about this server.")
    @app_commands.guild_only()
    async def serverinfo(self, interaction: discord.Interaction):
        g = interaction.guild
        embed = discord.Embed(title=g.name, description=g.description or None, color=discord.Color.blurple())
        if g.icon:
            embed.set_thumbnail(url=g.icon.url)
        embed.add_field(name="Owner", value=f"<@{g.owner_id}>")
        embed.add_field(name="Members", value=str(g.member_count))
        embed.add_field(name="Created", value=discord.utils.format_dt(g.created_at, "D"))
        embed.add_field(name="Channels", value=f"{len(g.text_channels)} text · {len(g.voice_channels)} voice")
        embed.add_field(name="Roles", value=str(len(g.roles) - 1))
        embed.add_field(name="Boosts", value=f"{g.premium_subscription_count} (tier {g.premium_tier})")
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------ help
    @app_commands.command(name="help", description="List everything the bot can do.")
    async def help(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="Bot commands",
            description="Commands you don't have permission for won't do anything, so ask a mod if you're unsure.",
            color=discord.Color.blurple(),
        )
        for cog_name, cog in sorted(self.bot.cogs.items()):
            lines: list[str] = []
            for cmd in cog.get_app_commands():
                if isinstance(cmd, app_commands.Group):
                    leaves = [c for c in cmd.walk_commands() if isinstance(c, app_commands.Command)]
                else:
                    leaves = [cmd]
                lines.extend(f"`/{c.qualified_name}` {c.description}" for c in leaves)
            if lines:
                value = "\n".join(lines)
                embed.add_field(name=cog_name, value=value[:1020] + ("…" if len(value) > 1020 else ""), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Fun(bot))
