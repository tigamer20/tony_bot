"""Helpers shared between cogs: durations, level math, templates, mod-log, error handling."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands

log = logging.getLogger(__name__)

COLORS = {
    "kick": discord.Color.orange(),
    "ban": discord.Color.red(),
    "unban": discord.Color.green(),
    "timeout": discord.Color.gold(),
    "untimeout": discord.Color.green(),
    "warn": discord.Color.yellow(),
    "purge": discord.Color.blurple(),
    "automod": discord.Color.dark_orange(),
}

# ---------------------------------------------------------------- durations
DURATION_RE = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> Optional[timedelta]:
    """Turn '10m', '2h30m', '1d' into a timedelta. Returns None if invalid."""
    text = text.strip()
    matches = DURATION_RE.findall(text)
    # Make sure the whole string was consumed by valid parts.
    if not matches or DURATION_RE.sub("", text).strip():
        return None
    seconds = sum(int(n) * UNIT_SECONDS[u.lower()] for n, u in matches)
    return timedelta(seconds=seconds) if seconds > 0 else None


def audit_reason(moderator: discord.abc.User, reason: str) -> str:
    return f"{moderator} ({moderator.id}): {reason}"[:512]


# ---------------------------------------------------------------- levels
def xp_for_level(level: int) -> int:
    """XP needed to go from `level` to `level + 1`."""
    return 5 * level * level + 50 * level + 100


def level_from_xp(xp: int) -> int:
    level = 0
    while xp >= xp_for_level(level):
        xp -= xp_for_level(level)
        level += 1
    return level


def level_progress(xp: int) -> tuple[int, int, int]:
    """Return (level, xp earned inside the current level, xp needed for the next level)."""
    level = 0
    while xp >= xp_for_level(level):
        xp -= xp_for_level(level)
        level += 1
    return level, xp, xp_for_level(level)


def progress_bar(fraction: float, width: int = 12) -> str:
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * width)
    return "█" * filled + "░" * (width - filled)


# ---------------------------------------------------------------- templates
_PLACEHOLDER_RE = re.compile(r"\{(user|name|server|count)\}")


def render_template(template: str, member: discord.Member, *, mention: bool = True) -> str:
    """Fill {user} {name} {server} {count} in an admin-written message.

    Single-pass regex replacement (not str.format) so stray braces in the
    template can never raise or leak attributes.
    """
    values = {
        "user": member.mention if mention else str(member),
        "name": member.display_name,
        "server": member.guild.name,
        "count": str(member.guild.member_count or 0),
    }
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template)


# ---------------------------------------------------------------- roles
def role_assign_error(guild: discord.Guild, invoker: Optional[discord.Member], role: discord.Role) -> Optional[str]:
    """Why `role` can't be handed out by the bot (or by `invoker`), or None if it's fine."""
    if role.is_default():
        return "You can't use @everyone."
    if role.managed:
        return f"{role.mention} is managed by an integration and can't be assigned manually."
    if role >= guild.me.top_role:
        return f"{role.mention} is above my highest role. Move my role higher in Server Settings > Roles."
    if invoker is not None and invoker.id != guild.owner_id and role >= invoker.top_role:
        return f"{role.mention} is equal to or above your highest role."
    return None


# ---------------------------------------------------------------- mod-log
async def send_modlog(
    bot,
    guild: discord.Guild,
    action: str,
    moderator: discord.abc.User,
    target: Optional[discord.abc.User] = None,
    reason: Optional[str] = None,
    extra: Optional[str] = None,
) -> None:
    channel_id = await bot.db.get_modlog_channel(guild.id)
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        return
    embed = discord.Embed(
        title=action.title(),
        color=COLORS.get(action, discord.Color.greyple()),
        timestamp=datetime.now(timezone.utc),
    )
    if target:
        embed.add_field(name="User", value=f"{target} (`{target.id}`)", inline=True)
    embed.add_field(name="Moderator", value=f"{moderator} (`{moderator.id}`)", inline=True)
    if reason:
        embed.add_field(name="Reason", value=reason[:1024], inline=False)
    if extra:
        embed.add_field(name="Details", value=extra[:1024], inline=False)
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        log.warning("Could not post to mod-log channel %s in guild %s", channel_id, guild.id)


# ---------------------------------------------------------------- errors
async def handle_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    """Global slash-command error handler (registered on the command tree)."""
    original = getattr(error, "original", None)

    if isinstance(error, app_commands.MissingPermissions):
        msg = "You don't have permission to use this command."
    elif isinstance(error, app_commands.BotMissingPermissions):
        perms = ", ".join(p.replace("_", " ") for p in error.missing_permissions)
        msg = f"I'm missing the permission(s) I need: {perms}."
    elif isinstance(error, app_commands.NoPrivateMessage):
        msg = "This command only works in a server."
    elif isinstance(error, app_commands.CommandOnCooldown):
        msg = f"Slow down! Try again in {error.retry_after:.0f}s."
    elif isinstance(error, app_commands.CheckFailure):
        msg = "You can't use this command here."
    elif isinstance(original, discord.Forbidden):
        msg = "Discord refused that action. Check that my role is high enough and I have the right permissions."
    else:
        log.error("Unhandled command error in /%s", getattr(interaction.command, "qualified_name", "?"), exc_info=error)
        msg = "Something went wrong running that command."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass  # interaction expired; nothing more we can do
