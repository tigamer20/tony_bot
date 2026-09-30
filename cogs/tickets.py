"""Ticket system: panel button -> modal -> private channel, with claim/close/transcript.

Setup (server admin):
    /ticket setup staff_role:@Support [category] [log_channel] [max_open]
    /ticket panel [channel]
"""
from __future__ import annotations

import asyncio
import io
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)

MAX_TRANSCRIPT_MESSAGES = 5000
DEFAULT_MAX_OPEN = 1


# ------------------------------------------------------------------ helpers
async def get_open_ticket(db, channel_id: int) -> Optional[sqlite3.Row]:
    return await db.fetchone(
        "SELECT * FROM tickets WHERE channel_id = ? AND status = 'open'", (channel_id,)
    )


async def is_staff(db, member: discord.Member) -> bool:
    """Staff = holds the configured support role, or has Manage Server."""
    if member.guild_permissions.manage_guild:
        return True
    role_id = await db.get_config_int(member.guild.id, "ticket_staff_role")
    return role_id is not None and any(r.id == role_id for r in member.roles)


async def build_transcript(channel: discord.TextChannel) -> bytes:
    lines = [f"Transcript of #{channel.name} ({channel.id})", f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC", ""]
    async for msg in channel.history(limit=MAX_TRANSCRIPT_MESSAGES, oldest_first=True):
        stamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
        text = msg.clean_content or ("[embed]" if msg.embeds else "")
        attachments = " ".join(a.url for a in msg.attachments)
        lines.append(f"[{stamp}] {msg.author} ({msg.author.id}): {text} {attachments}".rstrip())
    return "\n".join(lines).encode("utf-8")


async def close_ticket(bot, channel: discord.TextChannel, ticket: sqlite3.Row, closer: discord.abc.User, reason: str) -> None:
    db = bot.db
    guild = channel.guild
    # Mark closed first so a double-click can't run this twice.
    await db.execute(
        "UPDATE tickets SET status = 'closed', closed_at = ?, closed_by = ? WHERE id = ?",
        (int(time.time()), closer.id, ticket["id"]),
    )

    data = await build_transcript(channel)
    filename = f"transcript-{channel.name}.txt"

    embed = discord.Embed(
        title=f"Ticket #{ticket['id']:04d} closed",
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Opened by", value=f"<@{ticket['user_id']}>")
    embed.add_field(name="Closed by", value=closer.mention)
    if ticket["claimed_by"]:
        embed.add_field(name="Claimed by", value=f"<@{ticket['claimed_by']}>")
    embed.add_field(name="Subject", value=ticket["subject"][:1024], inline=False)
    embed.add_field(name="Reason", value=reason[:1024], inline=False)

    log_channel_id = await db.get_config_int(guild.id, "ticket_log_channel")
    log_channel = guild.get_channel(log_channel_id) if log_channel_id else None
    if isinstance(log_channel, discord.abc.Messageable):
        try:
            await log_channel.send(embed=embed, file=discord.File(io.BytesIO(data), filename=filename))
        except discord.HTTPException:
            log.warning("Could not post ticket transcript to log channel in guild %s", guild.id)

    opener = guild.get_member(ticket["user_id"])
    if opener is not None:
        try:
            embed.title = f"Your ticket in {guild.name} was closed"
            await opener.send(embed=embed, file=discord.File(io.BytesIO(data), filename=filename))
        except discord.HTTPException:
            pass  # DMs closed

    try:
        await channel.send("This ticket will be deleted in 5 seconds.")
        await asyncio.sleep(5)
        await channel.delete(reason=f"Ticket #{ticket['id']} closed by {closer}")
    except discord.HTTPException:
        log.warning("Could not delete ticket channel %s", channel.id)


async def open_ticket_count(db, guild: discord.Guild, user_id: int) -> int:
    """Count a user's genuinely open tickets, cleaning up rows whose channel vanished."""
    rows = await db.fetchall(
        "SELECT id, channel_id FROM tickets WHERE guild_id = ? AND user_id = ? AND status = 'open'",
        (guild.id, user_id),
    )
    count = 0
    for row in rows:
        if row["channel_id"] and guild.get_channel(row["channel_id"]):
            count += 1
        else:
            await db.execute("UPDATE tickets SET status = 'closed', closed_at = ? WHERE id = ?", (int(time.time()), row["id"]))
    return count


async def create_ticket(interaction: discord.Interaction, subject: str, details: str) -> None:
    guild = interaction.guild
    user = interaction.user
    bot = interaction.client
    db = bot.db
    assert guild is not None and isinstance(user, discord.Member)
    await interaction.response.defer(ephemeral=True)

    staff_role_id = await db.get_config_int(guild.id, "ticket_staff_role")
    staff_role = guild.get_role(staff_role_id) if staff_role_id else None
    if staff_role is None:
        return await interaction.followup.send("The ticket system isn't set up yet. Ask an admin to run `/ticket setup`.", ephemeral=True)

    max_open = await db.get_config_int(guild.id, "ticket_max_open") or DEFAULT_MAX_OPEN
    if await open_ticket_count(db, guild, user.id) >= max_open:
        return await interaction.followup.send(f"You already have {max_open} open ticket(s). Please use those or close one first.", ephemeral=True)

    category_id = await db.get_config_int(guild.id, "ticket_category")
    category = guild.get_channel(category_id) if category_id else None
    if not isinstance(category, discord.CategoryChannel):
        category = None

    ticket_id = await db.insert(
        "INSERT INTO tickets (guild_id, user_id, subject, status, created_at) VALUES (?, ?, ?, 'open', ?)",
        (guild.id, user.id, subject, int(time.time())),
    )

    member_perms = discord.PermissionOverwrite(
        view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True
    )
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: member_perms,
        staff_role: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            attach_files=True, embed_links=True, manage_messages=True,
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            attach_files=True, embed_links=True, manage_channels=True, manage_messages=True,
        ),
    }
    try:
        channel = await guild.create_text_channel(
            name=f"ticket-{ticket_id:04d}",
            category=category,
            overwrites=overwrites,
            topic=f"Ticket #{ticket_id:04d} | {user} ({user.id}) | {subject}"[:1024],
            reason=f"Ticket opened by {user}",
        )
    except discord.HTTPException:
        await db.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
        return await interaction.followup.send(
            "I couldn't create the ticket channel. I need the **Manage Channels** permission (and the category may be full).",
            ephemeral=True,
        )

    await db.execute("UPDATE tickets SET channel_id = ? WHERE id = ?", (channel.id, ticket_id))

    embed = discord.Embed(
        title=f"Ticket #{ticket_id:04d}: {subject}",
        description=details or "No extra details provided.",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_author(name=str(user), icon_url=user.display_avatar.url)
    embed.set_footer(text="Staff can claim this ticket. Use the buttons below to claim or close it.")
    await channel.send(
        content=f"{user.mention} {staff_role.mention}",
        embed=embed,
        view=TicketControlsView(),
        allowed_mentions=discord.AllowedMentions(users=[user], roles=[staff_role]),
    )
    await interaction.followup.send(f"Your ticket is open: {channel.mention}", ephemeral=True)


# ------------------------------------------------------------------ UI
class TicketModal(discord.ui.Modal, title="Open a ticket"):
    subject = discord.ui.TextInput(label="Subject", placeholder="Briefly, what do you need help with?", max_length=100)
    details = discord.ui.TextInput(
        label="Details", style=discord.TextStyle.paragraph, required=False, max_length=1000,
        placeholder="Give us as much information as you can.",
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await create_ticket(interaction, self.subject.value.strip(), self.details.value.strip())


class TicketPanelView(discord.ui.View):
    """The persistent 'Open a ticket' button posted by /ticket panel."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Open a Ticket", style=discord.ButtonStyle.primary, emoji="\N{TICKET}", custom_id="ticket:open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild = interaction.guild
        if guild is None:
            return
        db = interaction.client.db
        max_open = await db.get_config_int(guild.id, "ticket_max_open") or DEFAULT_MAX_OPEN
        if await open_ticket_count(db, guild, interaction.user.id) >= max_open:
            return await interaction.response.send_message(
                f"You already have {max_open} open ticket(s). Please use those or close one first.", ephemeral=True
            )
        await interaction.response.send_modal(TicketModal())


class ConfirmCloseView(discord.ui.View):
    def __init__(self, ticket: sqlite3.Row, closer: discord.Member) -> None:
        super().__init__(timeout=30)
        self.ticket = ticket
        self.closer = closer

    @discord.ui.button(label="Yes, close it", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.closer.id:
            return await interaction.response.send_message("This confirmation isn't for you.", ephemeral=True)
        self.stop()
        await interaction.response.edit_message(content="Closing ticket...", view=None)
        await close_ticket(interaction.client, interaction.channel, self.ticket, interaction.user, "Closed via button")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", view=None)


class TicketControlsView(discord.ui.View):
    """Close / Claim buttons inside each ticket channel (persistent)."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, emoji="\N{LOCK}", custom_id="ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        db = interaction.client.db
        ticket = await get_open_ticket(db, interaction.channel_id)
        if ticket is None:
            return await interaction.response.send_message("This ticket is already closed.", ephemeral=True)
        member = interaction.user
        if member.id != ticket["user_id"] and not await is_staff(db, member):
            return await interaction.response.send_message("Only the ticket owner or staff can close this.", ephemeral=True)
        await interaction.response.send_message("Close this ticket? A transcript will be saved.", view=ConfirmCloseView(ticket, member), ephemeral=True)

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success, emoji="\N{RAISED HAND}", custom_id="ticket:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        db = interaction.client.db
        ticket = await get_open_ticket(db, interaction.channel_id)
        if ticket is None:
            return await interaction.response.send_message("This ticket is already closed.", ephemeral=True)
        if not await is_staff(db, interaction.user):
            return await interaction.response.send_message("Only staff can claim tickets.", ephemeral=True)
        if ticket["claimed_by"]:
            return await interaction.response.send_message(f"Already claimed by <@{ticket['claimed_by']}>.", ephemeral=True)
        await db.execute("UPDATE tickets SET claimed_by = ? WHERE id = ?", (interaction.user.id, ticket["id"]))
        await interaction.response.send_message(f"{interaction.user.mention} has claimed this ticket and will help you.")


# ------------------------------------------------------------------ cog
class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        # Re-attach the buttons of messages posted before a restart.
        self.bot.add_view(TicketPanelView())
        self.bot.add_view(TicketControlsView())

    ticket = app_commands.Group(name="ticket", description="Support ticket system.", guild_only=True)

    @ticket.command(name="setup", description="Configure the ticket system.")
    @app_commands.describe(
        staff_role="Role that can see and manage tickets",
        category="Category new ticket channels are created in",
        log_channel="Where transcripts are posted when tickets close",
        max_open="Max open tickets per member (default 1)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup(
        self,
        interaction: discord.Interaction,
        staff_role: discord.Role,
        category: Optional[discord.CategoryChannel] = None,
        log_channel: Optional[discord.TextChannel] = None,
        max_open: Optional[app_commands.Range[int, 1, 10]] = None,
    ):
        db = self.bot.db
        gid = interaction.guild_id
        await db.set_config(gid, "ticket_staff_role", staff_role.id)
        lines = [f"Staff role: {staff_role.mention}"]
        for key, value, label in (
            ("ticket_category", category, "Category"),
            ("ticket_log_channel", log_channel, "Log channel"),
        ):
            if value is not None:
                await db.set_config(gid, key, value.id)
                lines.append(f"{label}: {value.mention}")
        if max_open is not None:
            await db.set_config(gid, "ticket_max_open", max_open)
            lines.append(f"Max open tickets per member: {max_open}")
        lines.append("\nNext: run `/ticket panel` in the channel where members should open tickets.")
        await interaction.response.send_message("Ticket system configured.\n" + "\n".join(lines), ephemeral=True)

    @ticket.command(name="panel", description="Post the 'Open a Ticket' button panel.")
    @app_commands.describe(channel="Where to post the panel (default: here)", title="Panel title", description="Panel text")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def panel(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
        title: app_commands.Range[str, 1, 100] = "Need help?",
        description: app_commands.Range[str, 1, 1000] = "Click the button below to open a private ticket with our staff team.",
    ):
        if await self.bot.db.get_config_int(interaction.guild_id, "ticket_staff_role") is None:
            return await interaction.response.send_message("Run `/ticket setup` first so I know who your staff are.", ephemeral=True)
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            return await interaction.response.send_message("Pick a normal text channel.", ephemeral=True)
        embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
        try:
            await target.send(embed=embed, view=TicketPanelView())
        except discord.Forbidden:
            return await interaction.response.send_message(f"I can't send messages in {target.mention}.", ephemeral=True)
        await interaction.response.send_message(f"Panel posted in {target.mention}.", ephemeral=True)

    async def _staff_ticket(self, interaction: discord.Interaction):
        """Shared guard for in-ticket commands. Returns the ticket row or None (after replying)."""
        ticket = await get_open_ticket(self.bot.db, interaction.channel_id)
        if ticket is None:
            await interaction.response.send_message("This isn't an open ticket channel.", ephemeral=True)
            return None
        if not await is_staff(self.bot.db, interaction.user):
            await interaction.response.send_message("Only staff can do that.", ephemeral=True)
            return None
        return ticket

    @ticket.command(name="add", description="Add a member to this ticket.")
    @app_commands.describe(member="Member to add")
    async def add(self, interaction: discord.Interaction, member: discord.Member):
        if await self._staff_ticket(interaction) is None:
            return
        await interaction.channel.set_permissions(
            member, view_channel=True, send_messages=True, read_message_history=True, attach_files=True,
            reason=f"Added to ticket by {interaction.user}",
        )
        await interaction.response.send_message(f"Added {member.mention} to the ticket.")

    @ticket.command(name="remove", description="Remove a member from this ticket.")
    @app_commands.describe(member="Member to remove")
    async def remove(self, interaction: discord.Interaction, member: discord.Member):
        ticket = await self._staff_ticket(interaction)
        if ticket is None:
            return
        if member.id == ticket["user_id"]:
            return await interaction.response.send_message("You can't remove the ticket owner. Close the ticket instead.", ephemeral=True)
        await interaction.channel.set_permissions(member, overwrite=None, reason=f"Removed from ticket by {interaction.user}")
        await interaction.response.send_message(f"Removed {member.mention} from the ticket.")

    @ticket.command(name="close", description="Close this ticket and save a transcript.")
    @app_commands.describe(reason="Why the ticket is being closed")
    async def close(self, interaction: discord.Interaction, reason: app_commands.Range[str, 1, 500] = "Resolved"):
        ticket = await get_open_ticket(self.bot.db, interaction.channel_id)
        if ticket is None:
            return await interaction.response.send_message("This isn't an open ticket channel.", ephemeral=True)
        if interaction.user.id != ticket["user_id"] and not await is_staff(self.bot.db, interaction.user):
            return await interaction.response.send_message("Only the ticket owner or staff can close this.", ephemeral=True)
        await interaction.response.send_message("Closing ticket...")
        await close_ticket(self.bot, interaction.channel, ticket, interaction.user, reason)

    @ticket.command(name="list", description="List open tickets (staff only).")
    async def list_tickets(self, interaction: discord.Interaction):
        if not await is_staff(self.bot.db, interaction.user):
            return await interaction.response.send_message("Only staff can do that.", ephemeral=True)
        rows = await self.bot.db.fetchall(
            "SELECT * FROM tickets WHERE guild_id = ? AND status = 'open' ORDER BY id LIMIT 25", (interaction.guild_id,)
        )
        if not rows:
            return await interaction.response.send_message("No open tickets.", ephemeral=True)
        lines = []
        for r in rows:
            claimed = f" (claimed by <@{r['claimed_by']}>)" if r["claimed_by"] else ""
            lines.append(f"<#{r['channel_id']}> · <@{r['user_id']}> · {r['subject'][:60]}{claimed}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        # If a ticket channel is deleted by hand, don't leave the ticket "open" forever.
        await self.bot.db.execute(
            "UPDATE tickets SET status = 'closed', closed_at = ? WHERE channel_id = ? AND status = 'open'",
            (int(time.time()), channel.id),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tickets(bot))
