"""Offline tests: run with `python -m unittest discover -s tests -v` from the project root.

No Discord connection or token is needed. Discord objects are replaced with mocks.
"""
import os
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord  # noqa: E402
from discord import app_commands  # noqa: E402

from bot import EXTENSIONS, CommunityBot  # noqa: E402
from cogs.automod import INVITE_RE  # noqa: E402
from cogs.fun import DICE_RE  # noqa: E402
from cogs.giveaways import GiveawayButton  # noqa: E402
from cogs.roles import RoleButton  # noqa: E402
from cogs import tickets  # noqa: E402
from db import Database  # noqa: E402
from utils import (  # noqa: E402
    level_from_xp, level_progress, parse_duration, progress_bar, render_template, xp_for_level,
)


class PureHelperTests(unittest.TestCase):
    def test_parse_duration(self):
        self.assertEqual(parse_duration("10m"), timedelta(minutes=10))
        self.assertEqual(parse_duration("1h30m"), timedelta(hours=1, minutes=30))
        self.assertEqual(parse_duration("2 d"), timedelta(days=2))
        self.assertEqual(parse_duration("1w"), timedelta(weeks=1))
        for bad in ("", "abc", "10x", "0m", "10", "5m tomorrow"):
            self.assertIsNone(parse_duration(bad), bad)

    def test_level_math(self):
        self.assertEqual(xp_for_level(0), 100)
        self.assertEqual(xp_for_level(1), 155)
        self.assertEqual(level_from_xp(0), 0)
        self.assertEqual(level_from_xp(99), 0)
        self.assertEqual(level_from_xp(100), 1)
        self.assertEqual(level_from_xp(100 + 155), 2)
        level, into, needed = level_progress(150)
        self.assertEqual((level, into, needed), (1, 50, 155))
        # monotonic
        levels = [level_from_xp(x) for x in range(0, 20000, 50)]
        self.assertEqual(levels, sorted(levels))

    def test_progress_bar(self):
        self.assertEqual(progress_bar(0), "░" * 12)
        self.assertEqual(progress_bar(1), "█" * 12)
        self.assertEqual(len(progress_bar(0.5)), 12)
        self.assertEqual(progress_bar(5), "█" * 12)  # clamped

    def test_render_template(self):
        member = SimpleNamespace(
            mention="<@1>", display_name="Sam", __str__=lambda s: "sam#0",
            guild=SimpleNamespace(name="Cool Server", member_count=42),
        )
        out = render_template("Hi {user} ({name}) to {server}! #{count} {unknown} {", member)
        self.assertEqual(out, "Hi <@1> (Sam) to Cool Server! #42 {unknown} {")

    def test_regexes(self):
        self.assertTrue(INVITE_RE.search("join discord.gg/abc123 now"))
        self.assertTrue(INVITE_RE.search("https://discord.com/invite/abc-123"))
        self.assertFalse(INVITE_RE.search("discord is great"))
        self.assertTrue(DICE_RE.match("2d6"))
        self.assertFalse(DICE_RE.match("2d"))

    def test_dynamic_item_templates(self):
        m = RoleButton.__discord_ui_compiled_template__.fullmatch("btnrole:123456")
        self.assertEqual(m["role_id"], "123456")
        self.assertIsNone(RoleButton.__discord_ui_compiled_template__.fullmatch("btnrole:abc"))
        g = GiveawayButton.__discord_ui_compiled_template__.fullmatch("gw:enter:7")
        self.assertEqual(g["id"], "7")


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.dir.name, "t.db"))
        await self.db.init()

    async def asyncTearDown(self):
        self.dir.cleanup()

    async def test_warnings(self):
        self.assertEqual(await self.db.add_warning(1, 2, 3, "spam"), 1)
        self.assertEqual(await self.db.add_warning(1, 2, 3, "again"), 2)
        self.assertEqual(len(await self.db.get_warnings(1, 2)), 2)
        self.assertEqual(await self.db.clear_warnings(1, 2), 2)
        self.assertEqual(await self.db.get_warnings(1, 2), [])

    async def test_config(self):
        self.assertIsNone(await self.db.get_config(1, "x"))
        self.assertEqual(await self.db.get_config(1, "x", "d"), "d")
        await self.db.set_config(1, "x", 5)
        await self.db.set_config(1, "x", 6)  # upsert
        self.assertEqual(await self.db.get_config_int(1, "x"), 6)
        await self.db.set_config(1, "automod_spam", 1)
        await self.db.set_config(1, "automodXother", 1)  # '_' must not act as a wildcard
        self.assertEqual(await self.db.get_configs(1, "automod_"), {"spam": "1"})
        await self.db.delete_config(1, "x")
        self.assertIsNone(await self.db.get_config(1, "x"))
        # per-guild isolation
        self.assertIsNone(await self.db.get_config(2, "automod_spam"))

    async def test_modlog_channel(self):
        await self.db.set_modlog_channel(1, 99)
        self.assertEqual(await self.db.get_modlog_channel(1), 99)
        await self.db.set_modlog_channel(1, None)
        self.assertIsNone(await self.db.get_modlog_channel(1))

    async def test_levels_upsert_and_rank(self):
        sql = """INSERT INTO levels (guild_id, user_id, xp, level, messages) VALUES (?, ?, ?, ?, 1)
                 ON CONFLICT(guild_id, user_id) DO UPDATE
                 SET xp = excluded.xp, level = excluded.level, messages = messages + 1"""
        await self.db.execute(sql, (1, 10, 50, 0))
        await self.db.execute(sql, (1, 10, 120, 1))
        await self.db.execute(sql, (1, 11, 500, 3))
        row = await self.db.fetchone("SELECT * FROM levels WHERE guild_id=1 AND user_id=10")
        self.assertEqual((row["xp"], row["level"], row["messages"]), (120, 1, 2))
        pos = await self.db.fetchone("SELECT COUNT(*) + 1 AS pos FROM levels WHERE guild_id = 1 AND xp > 120")
        self.assertEqual(pos["pos"], 2)

    async def test_legacy_modlog_migration(self):
        import sqlite3
        path = os.path.join(self.dir.name, "legacy.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE settings (guild_id INTEGER PRIMARY KEY, modlog_channel_id INTEGER)")
        conn.execute("INSERT INTO settings VALUES (5, 555), (6, NULL)")
        conn.commit()
        conn.close()
        db = Database(path)
        await db.init()
        self.assertEqual(await db.get_modlog_channel(5), 555)
        self.assertIsNone(await db.get_modlog_channel(6))


class BotWiringTests(unittest.IsolatedAsyncioTestCase):
    """Load every cog into a real (but never-connected) bot and validate the command payloads."""

    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["DATABASE_PATH"] = os.path.join(self.dir.name, "bot.db")
        self.bot = CommunityBot()
        await self.bot.__aenter__()  # what `async with bot:` does in bot.py: prepares internal state
        await self.bot.db.init()
        for ext in EXTENSIONS:
            await self.bot.load_extension(ext)

    async def asyncTearDown(self):
        await self.bot.__aexit__(None, None, None)
        self.dir.cleanup()

    def test_all_cogs_loaded_and_payloads_valid(self):
        self.assertEqual(len(self.bot.cogs), len(EXTENSIONS))
        cmds = self.bot.tree.get_commands()
        self.assertLess(len(cmds), 100)
        names = [c.name for c in cmds]
        self.assertEqual(len(names), len(set(names)), "duplicate top-level command names")
        for expected in ("kick", "ban", "ticket", "rank", "leaderboard", "levels", "levelreward", "xp",
                         "welcome", "rolepanel", "automod", "giveaway", "reminder", "poll", "help"):
            self.assertIn(expected, names)
        for c in cmds:
            payload = c.to_dict(self.bot.tree)  # raises if the definition is invalid
            self.assertRegex(payload["name"], r"^[-_\w]{1,32}$")
            self.assertLessEqual(len(payload["description"]), 100)
            self.assertLessEqual(len(payload.get("options", [])), 25)

    def test_persistent_views_registered(self):
        # tickets: 2 static views; roles + giveaways: dynamic items
        custom_ids = {
            item.custom_id
            for view in self.bot.persistent_views
            for item in view.children
            if hasattr(item, "custom_id")
        }
        self.assertTrue({"ticket:open", "ticket:close", "ticket:claim"} <= custom_ids)

    async def test_help_fits_in_one_embed(self):
        interaction = MagicMock()
        interaction.response.send_message = AsyncMock()
        fun = self.bot.get_cog("Fun")
        await fun.help.callback(fun, interaction)
        embed = interaction.response.send_message.call_args.kwargs["embed"]
        self.assertLessEqual(len(embed), 6000)
        self.assertLessEqual(len(embed.fields), 25)
        for f in embed.fields:
            self.assertLessEqual(len(f.value), 1024)
        listed = "\n".join(f.value for f in embed.fields)
        for cmd in ("/kick", "/ticket setup", "/ticket panel", "/rank", "/rolepanel create",
                    "/giveaway start", "/automod toggle", "/welcome autorole", "/reminder add"):
            self.assertIn(cmd, listed)

    async def test_level_gain_and_cooldown(self):
        levels = self.bot.get_cog("Levels")
        member = MagicMock(spec=discord.Member)
        member.bot = False
        member.id = 42
        member.roles = []
        message = MagicMock()
        message.author = member
        message.guild = SimpleNamespace(id=7, get_role=lambda _id: None, get_channel=lambda _id: None)
        message.channel.send = AsyncMock()

        await levels.on_message(message)
        row = await self.bot.db.fetchone("SELECT * FROM levels WHERE guild_id=7 AND user_id=42")
        self.assertTrue(15 <= row["xp"] <= 25)
        first_xp = row["xp"]
        await levels.on_message(message)  # inside the 60 s cooldown: no change
        row = await self.bot.db.fetchone("SELECT * FROM levels WHERE guild_id=7 AND user_id=42")
        self.assertEqual(row["xp"], first_xp)
        self.assertEqual(row["messages"], 1)

        # disabling the system stops XP for other users
        await levels._set_cfg(7, "enabled", 0)
        other = MagicMock(spec=discord.Member)
        other.bot = False
        other.id = 43
        message.author = other
        await levels.on_message(message)
        self.assertIsNone(await self.bot.db.fetchone("SELECT * FROM levels WHERE user_id=43"))

    async def test_levelup_announces(self):
        levels = self.bot.get_cog("Levels")
        await self.bot.db.execute("INSERT INTO levels (guild_id, user_id, xp, level, messages) VALUES (7, 50, 99, 0, 1)")
        member = MagicMock(spec=discord.Member)
        member.bot = False
        member.id = 50
        member.mention = "<@50>"
        member.roles = []
        message = MagicMock()
        message.author = member
        message.guild = SimpleNamespace(id=7, get_role=lambda _id: None, get_channel=lambda _id: None)
        member.guild = message.guild
        message.channel.send = AsyncMock()
        await levels.on_message(message)  # 99 + 15..25 crosses 100
        message.channel.send.assert_awaited_once()
        self.assertIn("level 1", message.channel.send.call_args.args[0])

    async def test_automod_filters(self):
        automod = self.bot.get_cog("AutoMod")
        gid = 9
        await self.bot.db.execute("INSERT INTO automod_words VALUES (9, 'badword'), (9, 'two words')")
        pattern = await automod._word_pattern(gid)
        self.assertTrue(pattern.search("this is a BADWORD!"))
        self.assertTrue(pattern.search("has two words in it"))
        self.assertFalse(pattern.search("badwords"))      # whole-word only
        self.assertFalse(pattern.search("notbadword"))
        self.assertIsNone(await automod._word_pattern(1234))  # no words configured

        # feature flags default off, then respect toggles
        self.assertEqual((await automod._cfg(gid))["invites"], "0")
        await automod._set(gid, "invites", 1)
        self.assertEqual((await automod._cfg(gid))["invites"], "1")

        def fake_message(content, perms=None):
            author = MagicMock(spec=discord.Member)
            author.bot = False
            author.id = 77
            author.guild_permissions = perms or discord.Permissions.none()
            msg = MagicMock()
            msg.author = author
            msg.guild = SimpleNamespace(id=gid)
            msg.content = content
            msg.mentions = []
            msg.role_mentions = []
            return msg

        automod._punish = AsyncMock()
        await automod.on_message(fake_message("hey discord.gg/xyz"))
        automod._punish.assert_awaited_once()
        automod._punish.reset_mock()
        await automod.on_message(fake_message("discord.gg/xyz", discord.Permissions(manage_messages=True)))
        automod._punish.assert_not_awaited()  # moderators are exempt
        await automod.on_message(fake_message("hello there"))
        automod._punish.assert_not_awaited()

    async def test_spam_detection(self):
        automod = self.bot.get_cog("AutoMod")
        gid = 11
        await automod._set(gid, "spam", 1)
        await automod._set(gid, "spam_limit", 3)
        automod._punish = AsyncMock()
        author = MagicMock(spec=discord.Member)
        author.bot = False
        author.id = 5
        author.guild_permissions = discord.Permissions.none()
        msg = MagicMock()
        msg.author = author
        msg.guild = SimpleNamespace(id=gid)
        msg.content = "hi"
        msg.mentions = []
        msg.role_mentions = []
        msg.channel.purge = AsyncMock()
        for _ in range(2):
            await automod.on_message(msg)
        automod._punish.assert_not_awaited()
        await automod.on_message(msg)  # third message inside the window
        automod._punish.assert_awaited_once()
        self.assertIsNotNone(automod._punish.call_args.kwargs.get("timeout"))

    async def test_role_button_toggle_and_allowlist(self):
        from unittest.mock import patch
        # load_extension() executes a fresh copy of the module, so patch/instantiate from that copy
        button = sys.modules["cogs.roles"].RoleButton(555, "Gamer")
        role = SimpleNamespace(id=555, mention="<@&555>")
        member = MagicMock(spec=discord.Member)
        member.roles = []
        member.add_roles = AsyncMock()
        member.remove_roles = AsyncMock()
        guild = SimpleNamespace(id=3, get_role=lambda rid: role if rid == 555 else None)
        interaction = MagicMock()
        interaction.guild = guild
        interaction.user = member
        interaction.client = self.bot
        interaction.response.send_message = AsyncMock()

        # Not on any panel yet: refuse even though the role exists.
        await button.callback(interaction)
        member.add_roles.assert_not_awaited()
        self.assertIn("no longer available", interaction.response.send_message.call_args.args[0])

        await self.bot.db.execute("INSERT INTO panel_roles VALUES (3, 555)")
        with patch("cogs.roles.role_assign_error", return_value=None):
            await button.callback(interaction)
            member.add_roles.assert_awaited_once()
            member.roles = [role]
            await button.callback(interaction)
            member.remove_roles.assert_awaited_once()
        # a role the bot can't manage is refused
        member.add_roles.reset_mock()
        member.roles = []
        with patch("cogs.roles.role_assign_error", return_value="too high"):
            await button.callback(interaction)
        member.add_roles.assert_not_awaited()

    async def test_giveaway_lifecycle(self):
        from unittest.mock import patch
        from cogs.giveaways import giveaway_embed
        giveaways = self.bot.get_cog("Giveaways")
        gid = await self.bot.db.insert(
            "INSERT INTO giveaways (guild_id, channel_id, message_id, host_id, prize, winners, end_time) "
            "VALUES (1, 2, 3, 4, 'Nitro', 2, ?)", (int(time.time()) - 1,))
        for uid in (10, 11, 12, 13):
            await self.bot.db.execute("INSERT INTO giveaway_entries VALUES (?, ?)", (gid, uid))

        message = MagicMock()
        message.edit = AsyncMock()
        message.reply = AsyncMock()
        channel = MagicMock(spec=discord.TextChannel)
        channel.fetch_message = AsyncMock(return_value=message)
        guild = SimpleNamespace(get_member=lambda uid: object() if uid != 13 else None)  # 13 left the server

        with patch.object(self.bot, "get_guild", return_value=guild), patch.object(self.bot, "get_channel", return_value=channel):
            await giveaways.check_giveaways.coro(giveaways)
            row = await self.bot.db.fetchone("SELECT * FROM giveaways WHERE id = ?", (gid,))
            self.assertEqual(row["ended"], 1)
            message.edit.assert_awaited_once()
            reply = message.reply.call_args.args[0]
            self.assertIn("Nitro", reply)
            self.assertNotIn("<@13>", reply)          # ineligible entrant can't win
            self.assertEqual(reply.count("<@1"), 2)   # two winners picked
            message.reply.reset_mock()
            await giveaways.check_giveaways.coro(giveaways)  # already ended: must not fire twice
            message.reply.assert_not_awaited()
            winners = await giveaways._end(gid, reroll=True)
            self.assertEqual(len(winners), 2)

        embed = giveaway_embed(row, winners=[])
        self.assertIn("No valid entries", embed.description)

    async def test_reminder_delivery(self):
        from unittest.mock import patch
        reminders = self.bot.get_cog("Reminders")
        await self.bot.db.execute("INSERT INTO reminders (user_id, channel_id, message, remind_at) VALUES (9, 2, 'take a break', ?)", (int(time.time()) - 1,))
        await self.bot.db.execute("INSERT INTO reminders (user_id, channel_id, message, remind_at) VALUES (9, 2, 'later', ?)", (int(time.time()) + 3600,))
        channel = MagicMock(spec=discord.TextChannel)
        channel.send = AsyncMock()
        with patch.object(self.bot, "get_channel", return_value=channel):
            await reminders.check_reminders.coro(reminders)
        channel.send.assert_awaited_once()
        self.assertIn("take a break", channel.send.call_args.args[0])
        left = await self.bot.db.fetchall("SELECT message FROM reminders")
        self.assertEqual([r["message"] for r in left], ["later"])


class TicketLogicTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.dir.name, "t.db"))
        await self.db.init()

    async def asyncTearDown(self):
        self.dir.cleanup()

    async def test_is_staff(self):
        await self.db.set_config(1, "ticket_staff_role", 500)
        member = MagicMock()
        member.guild.id = 1
        member.guild_permissions = discord.Permissions.none()
        member.roles = [SimpleNamespace(id=1), SimpleNamespace(id=500)]
        self.assertTrue(await tickets.is_staff(self.db, member))
        member.roles = [SimpleNamespace(id=1)]
        self.assertFalse(await tickets.is_staff(self.db, member))
        member.guild_permissions = discord.Permissions(manage_guild=True)
        self.assertTrue(await tickets.is_staff(self.db, member))

    async def test_open_ticket_count_cleans_stale_rows(self):
        now = int(time.time())
        await self.db.execute("INSERT INTO tickets (guild_id, channel_id, user_id, subject, created_at) VALUES (1, 100, 9, 'a', ?)", (now,))
        await self.db.execute("INSERT INTO tickets (guild_id, channel_id, user_id, subject, created_at) VALUES (1, 101, 9, 'b', ?)", (now,))
        guild = SimpleNamespace(id=1, get_channel=lambda cid: object() if cid == 100 else None)  # 101 was deleted
        self.assertEqual(await tickets.open_ticket_count(self.db, guild, 9), 1)
        stale = await self.db.fetchone("SELECT status FROM tickets WHERE channel_id = 101")
        self.assertEqual(stale["status"], "closed")

    async def test_transcript(self):
        from datetime import datetime, timezone

        class Author:
            def __init__(self, name, uid):
                self.name, self.id = name, uid

            def __str__(self):
                return self.name

        def msg(author, text, attachments=()):
            return SimpleNamespace(
                created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc), author=author,
                clean_content=text, embeds=[], attachments=[SimpleNamespace(url=u) for u in attachments],
            )

        async def history(limit, oldest_first):
            for m in (msg(Author("alice", 1), "hello"), msg(Author("bob", 2), "see file", ["http://x/y.png"])):
                yield m

        channel = SimpleNamespace(name="ticket-0001", id=1, history=history)
        text = (await tickets.build_transcript(channel)).decode()
        self.assertIn("ticket-0001", text)
        self.assertIn("[2026-01-02 03:04:05] alice (1): hello", text)
        self.assertIn("see file http://x/y.png", text)

    async def test_create_and_close_ticket_flow(self):
        from unittest.mock import patch
        db = self.db
        await db.set_config(1, "ticket_staff_role", 500)
        await db.set_config(1, "ticket_log_channel", 900)

        staff_role = MagicMock()
        staff_role.id = 500
        staff_role.mention = "<@&500>"
        ticket_channel = MagicMock(spec=discord.TextChannel)
        ticket_channel.id = 777
        ticket_channel.name = "ticket-0001"
        ticket_channel.mention = "<#777>"
        ticket_channel.send = AsyncMock()
        ticket_channel.delete = AsyncMock()

        async def history(limit, oldest_first):
            return
            yield  # empty async generator
        ticket_channel.history = history

        log_channel = MagicMock(spec=discord.TextChannel)
        log_channel.send = AsyncMock()
        guild = MagicMock()
        guild.id = 1
        guild.name = "Guild"
        guild.get_role = lambda rid: staff_role if rid == 500 else None
        guild.get_channel = lambda cid: {900: log_channel, 777: ticket_channel}.get(cid)
        guild.get_member = lambda uid: None
        guild.create_text_channel = AsyncMock(return_value=ticket_channel)
        ticket_channel.guild = guild

        user = MagicMock(spec=discord.Member)
        user.id = 42
        user.mention = "<@42>"
        user.display_avatar.url = "http://avatar"
        interaction = MagicMock()
        interaction.guild = guild
        interaction.user = user
        interaction.client = SimpleNamespace(db=db)
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()

        await tickets.create_ticket(interaction, "Login broken", "Details here")
        kwargs = guild.create_text_channel.call_args.kwargs
        self.assertEqual(kwargs["name"], "ticket-0001")
        self.assertFalse(kwargs["overwrites"][guild.default_role].view_channel)   # hidden from @everyone
        self.assertTrue(kwargs["overwrites"][user].view_channel)                  # visible to opener
        self.assertTrue(kwargs["overwrites"][staff_role].manage_messages)         # staff can moderate it
        row = await db.fetchone("SELECT * FROM tickets WHERE id = 1")
        self.assertEqual((row["channel_id"], row["user_id"], row["status"]), (777, 42, "open"))
        self.assertIn("<#777>", interaction.followup.send.call_args.args[0])
        ticket_channel.send.assert_awaited_once()

        # second attempt is blocked by the per-user limit (default 1)
        interaction.followup.send.reset_mock()
        await tickets.create_ticket(interaction, "Another", "")
        guild.create_text_channel.assert_awaited_once()
        self.assertIn("already have", interaction.followup.send.call_args.args[0])

        # closing posts a transcript to the log channel, marks it closed, deletes the channel
        closer = MagicMock()
        closer.id = 8
        closer.mention = "<@8>"
        with patch("cogs.tickets.asyncio.sleep", new=AsyncMock()):
            await tickets.close_ticket(SimpleNamespace(db=db), ticket_channel, row, closer, "Resolved")
        log_channel.send.assert_awaited_once()
        self.assertIn("file", log_channel.send.call_args.kwargs)
        ticket_channel.delete.assert_awaited_once()
        closed = await db.fetchone("SELECT status, closed_by FROM tickets WHERE id = 1")
        self.assertEqual((closed["status"], closed["closed_by"]), ("closed", 8))
        self.assertIsNone(await tickets.get_open_ticket(db, 777))

    async def test_create_ticket_requires_setup(self):
        interaction = MagicMock()
        interaction.guild = MagicMock()
        interaction.guild.id = 1
        interaction.guild.get_role = lambda rid: None
        interaction.user = MagicMock(spec=discord.Member)
        interaction.client = SimpleNamespace(db=self.db)
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        await tickets.create_ticket(interaction, "x", "")
        self.assertIn("isn't set up", interaction.followup.send.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
