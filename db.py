"""SQLite storage layer shared by every cog.

Uses only the standard-library sqlite3 module, executed in a worker thread so
it never blocks the bot's event loop. Cogs call the generic helpers
(`execute`, `insert`, `fetchone`, `fetchall`) plus the per-server config
helpers (`get_config` / `set_config`).
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import closing
from typing import Any, Optional, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_warnings_user ON warnings (guild_id, user_id);

-- Generic per-server key/value settings (mod-log channel, ticket config, ...)
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (guild_id, key)
);

CREATE TABLE IF NOT EXISTS levels (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    xp INTEGER NOT NULL DEFAULT 0,
    level INTEGER NOT NULL DEFAULT 0,
    messages INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_levels_xp ON levels (guild_id, xp DESC);

CREATE TABLE IF NOT EXISTS level_rewards (
    guild_id INTEGER NOT NULL,
    level INTEGER NOT NULL,
    role_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, level)
);

CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER,
    user_id INTEGER NOT NULL,
    subject TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    claimed_by INTEGER,
    created_at INTEGER NOT NULL,
    closed_at INTEGER,
    closed_by INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tickets_channel ON tickets (channel_id);
CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets (guild_id, user_id, status);

-- Roles that button-role panels are allowed to hand out
CREATE TABLE IF NOT EXISTS panel_roles (
    guild_id INTEGER NOT NULL,
    role_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, role_id)
);

CREATE TABLE IF NOT EXISTS automod_words (
    guild_id INTEGER NOT NULL,
    word TEXT NOT NULL,
    PRIMARY KEY (guild_id, word)
);

CREATE TABLE IF NOT EXISTS giveaways (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER,
    host_id INTEGER NOT NULL,
    prize TEXT NOT NULL,
    winners INTEGER NOT NULL DEFAULT 1,
    end_time INTEGER NOT NULL,
    ended INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_giveaways_due ON giveaways (ended, end_time);

CREATE TABLE IF NOT EXISTS giveaway_entries (
    giveaway_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (giveaway_id, user_id)
);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    channel_id INTEGER,
    message TEXT NOT NULL,
    remind_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders (remind_at);
"""


class Database:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ internals
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _run(self, sql: str, params: Sequence[Any], mode: str):
        with closing(self._connect()) as conn, conn:  # `conn` context = commit/rollback
            cur = conn.execute(sql, params)
            if mode == "all":
                return cur.fetchall()
            if mode == "one":
                return cur.fetchone()
            if mode == "insert":
                return cur.lastrowid
            return cur.rowcount

    def _run_script(self, script: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.executescript(script)

    async def _call(self, sql: str, params: Sequence[Any], mode: str):
        async with self._lock:
            return await asyncio.to_thread(self._run, sql, tuple(params), mode)

    # ------------------------------------------------------------ public API
    async def init(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._run_script, SCHEMA)
        # Carry over the mod-log channel from the first version of the bot.
        try:
            await self.execute(
                """INSERT OR IGNORE INTO guild_config (guild_id, key, value)
                   SELECT guild_id, 'modlog_channel', CAST(modlog_channel_id AS TEXT)
                   FROM settings WHERE modlog_channel_id IS NOT NULL"""
            )
        except sqlite3.OperationalError:
            pass  # no legacy table, nothing to migrate

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run a write statement and return the number of affected rows."""
        return await self._call(sql, params, "exec")

    async def insert(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run an INSERT and return the new row id."""
        return await self._call(sql, params, "insert")

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        return await self._call(sql, params, "one")

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return await self._call(sql, params, "all")

    # ------------------------------------------------------------ config
    async def get_config(self, guild_id: int, key: str, default: Optional[str] = None) -> Optional[str]:
        row = await self.fetchone(
            "SELECT value FROM guild_config WHERE guild_id = ? AND key = ?", (guild_id, key)
        )
        return row["value"] if row else default

    async def get_config_int(self, guild_id: int, key: str) -> Optional[int]:
        value = await self.get_config(guild_id, key)
        return int(value) if value is not None and value.lstrip("-").isdigit() else None

    async def set_config(self, guild_id: int, key: str, value: Any) -> None:
        await self.execute(
            """INSERT INTO guild_config (guild_id, key, value) VALUES (?, ?, ?)
               ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value""",
            (guild_id, key, str(value)),
        )

    async def delete_config(self, guild_id: int, key: str) -> None:
        await self.execute("DELETE FROM guild_config WHERE guild_id = ? AND key = ?", (guild_id, key))

    async def get_configs(self, guild_id: int, prefix: str) -> dict[str, str]:
        """All settings whose key starts with `prefix`, with the prefix stripped."""
        rows = await self.fetchall(
            "SELECT key, value FROM guild_config WHERE guild_id = ? AND key LIKE ? ESCAPE '\\'",
            (guild_id, prefix.replace("_", "\\_") + "%"),
        )
        return {r["key"][len(prefix):]: r["value"] for r in rows}

    # ------------------------------------------------------------ warnings
    async def add_warning(self, guild_id: int, user_id: int, mod_id: int, reason: str) -> int:
        await self.execute(
            "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, user_id, mod_id, reason, int(time.time())),
        )
        row = await self.fetchone(
            "SELECT COUNT(*) AS n FROM warnings WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        )
        return row["n"]

    async def get_warnings(self, guild_id: int, user_id: int):
        return await self.fetchall(
            "SELECT * FROM warnings WHERE guild_id = ? AND user_id = ? ORDER BY id DESC",
            (guild_id, user_id),
        )

    async def clear_warnings(self, guild_id: int, user_id: int) -> int:
        return await self.execute(
            "DELETE FROM warnings WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        )

    # ------------------------------------------------------------ mod-log
    async def set_modlog_channel(self, guild_id: int, channel_id: Optional[int]) -> None:
        if channel_id is None:
            await self.delete_config(guild_id, "modlog_channel")
        else:
            await self.set_config(guild_id, "modlog_channel", channel_id)

    async def get_modlog_channel(self, guild_id: int) -> Optional[int]:
        return await self.get_config_int(guild_id, "modlog_channel")
