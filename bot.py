"""Entry point for the community bot.

Run with:  python bot.py
"""
import asyncio
import logging
import os
from pathlib import Path

import discord
from discord.ext import commands

from db import Database
from utils import handle_app_error

EXTENSIONS = (
    "cogs.moderation",
    "cogs.automod",
    "cogs.tickets",
    "cogs.levels",
    "cogs.welcome",
    "cogs.roles",
    "cogs.giveaways",
    "cogs.reminders",
    "cogs.fun",
)


def load_env(path: str = ".env") -> None:
    """Minimal .env loader (avoids an extra third-party dependency)."""
    env_file = Path(path)
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class CommunityBot(commands.Bot):
    def __init__(self) -> None:
        # Two privileged intents must be switched on in the Developer Portal:
        #   * Server Members Intent  (welcome messages, moderation)
        #   * Message Content Intent (levels + auto-moderation read messages)
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            # Never let user-supplied text ping @everyone or roles. Cogs that
            # need a role ping (tickets) opt in explicitly per message.
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
        )
        self.db = Database(os.getenv("DATABASE_PATH", "bot.db"))
        self.tree.error(handle_app_error)

    async def setup_hook(self) -> None:
        await self.db.init()
        for extension in EXTENSIONS:
            await self.load_extension(extension)

        # Optional: set DEV_GUILD_ID to sync commands instantly to one server
        # while developing. Without it, commands sync globally (can take ~1h
        # to appear the first time).
        dev_guild = os.getenv("DEV_GUILD_ID")
        if dev_guild:
            guild = discord.Object(id=int(dev_guild))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self) -> None:
        logging.info("Logged in as %s (id=%s) in %d server(s)", self.user, self.user.id, len(self.guilds))


async def main() -> None:
    load_env()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")

    discord.utils.setup_logging(level=logging.INFO)
    async with CommunityBot() as bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
