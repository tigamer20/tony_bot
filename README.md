# Community Discord Bot

An all-in-one bot for a community server, built with discord.py 2.7.1 and slash commands.

| Module | What it gives you |
|---|---|
| **Moderation** | kick, ban, unban, timeout, purge, warnings, mod-log |
| **Auto-moderation** | spam, invite-link, banned-word and mass-mention filters |
| **Tickets** | button panel, private channels, claim/close, transcripts |
| **Levels** | XP from chatting, `/rank`, leaderboard, role rewards |
| **Welcome** | welcome/goodbye messages and an auto-role |
| **Role panels** | members click buttons to give themselves roles |
| **Giveaways** | timed giveaways with an Enter button, end/reroll |
| **Reminders** | personal reminders that survive restarts |
| **Fun & utility** | polls, dice, 8-ball, coinflip, user/server info, `/help` |

## Setup

1. **Create the bot** at https://discord.com/developers/applications → New Application → **Bot** tab → *Reset Token* and copy it.
2. On the same **Bot** tab, switch on both privileged intents:
   - **Server Members Intent** (welcome messages, moderation)
   - **Message Content Intent** (levels and auto-moderation need to read messages)
3. **Invite it**: OAuth2 → URL Generator → scopes `bot` and `applications.commands`. Bot permissions: View Channels, Send Messages, Embed Links, Attach Files, Read Message History, Add Reactions, Manage Messages, Manage Channels, Manage Roles, Kick Members, Ban Members, Moderate Members.
4. In **Server Settings → Roles**, drag the bot's role **above** every role it should manage (auto-role, level rewards, role panels, and members it moderates).
5. Install and run (Python 3.9 or newer):

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env           # Windows: copy .env.example .env  — then paste your token into .env
python bot.py
```

Put your test server's ID in `DEV_GUILD_ID` in `.env` and the slash commands appear instantly. Without it they roll out globally, which can take up to an hour the first time.

## First-time configuration (in Discord)

Most of this is one-time admin setup. Everything is off or empty until you configure it.

```
/modlog channel:#mod-log                         where moderation actions get logged
/ticket setup staff_role:@Support category:... log_channel:#ticket-logs
/ticket panel channel:#support                   posts the "Open a Ticket" button
/welcome channel:#welcome                        then /welcome message, /welcome autorole
/levels config announce:True channel:#level-ups  optional
/levelreward add level:5 role:@Regular           role handed out at level 5
/rolepanel create title:"Pick roles" description:"..." role1:@Gamer role2:@Artist
/automod toggle feature:Spam enabled:True        also Invites, Banned words, Mass mentions
/automod word_add word:someword
```

## All commands

**Moderation** (permissions are enforced by Discord *and* by the bot; role hierarchy is always respected)
`/kick` `/ban` `/unban` `/timeout` `/untimeout` `/purge` `/warn` `/warnings` `/clearwarnings` `/modlog`

**Auto-moderation** (Manage Server; members with Manage Messages are exempt)
`/automod toggle` `/automod spam_limit` `/automod mention_limit` `/automod word_add` `/automod word_remove` `/automod word_list` `/automod status`
Offences delete the message, add a warning (visible in `/warnings`) and are logged to the mod-log. Spam also gives a 5-minute timeout.

**Tickets**
`/ticket setup` `/ticket panel` (Manage Server) · `/ticket add` `/ticket remove` `/ticket list` (staff) · `/ticket close` (staff or ticket owner)
Members press **Open a Ticket**, fill in a short form, and get a private channel visible only to them and the staff role. Staff can **Claim** it; **Close** saves a text transcript to the log channel and DMs a copy to the member, then deletes the channel. One open ticket per member by default (`max_open` in `/ticket setup` changes it). Buttons keep working after restarts.

**Levels**
`/rank [member]` `/leaderboard` · `/levels config` · `/levelreward add|remove|list` · `/xp give|reset` (admin)
Members earn 15–25 XP per message, at most once a minute.

**Welcome** (Manage Server)
`/welcome channel|message|goodbye_channel|goodbye_message|autorole|test`
Message placeholders: `{user}` (mention), `{name}`, `{server}`, `{count}`.

**Role panels** (Manage Roles)
`/rolepanel create` (up to 5 roles per panel) · `/rolepanel revoke`
You can only put roles on a panel that are below your own top role, and the bot only ever hands out roles that were placed on a panel.

**Giveaways** (Manage Server)
`/giveaway start` `/giveaway end` `/giveaway reroll`

**Reminders & fun**
`/reminder add|list|cancel` · `/poll` `/8ball` `/coinflip` `/roll` `/avatar` `/userinfo` `/serverinfo` `/ping` `/help`

## Project layout

```
bot.py            startup, intents, command sync
db.py             SQLite storage (file: bot.db, created automatically)
utils.py          shared helpers (durations, level math, mod-log, error handler)
cogs/             one file per module listed above
tests/            offline unit tests (no token needed)
```

Run the tests with `python -m unittest discover -s tests -v`.

## Notes

- **Keep your token private.** `.env` is in `.gitignore`. If it ever leaks, reset it in the Developer Portal.
- **Backups:** all data lives in `bot.db`. Copy that file to back everything up. Set `DATABASE_PATH` in `.env` to move it.
- **Hosting:** the bot must keep running to respond. Any always-on machine, VPS or container works; there's no web server or open port to configure.
- **Dependencies:** only `discord.py` and `aiohttp`, pinned to exact versions that were security-reviewed before use (source scanned, and hashes matched PyPI). Re-review them before bumping versions.
- `/purge amount` counts messages *checked*, so with a member filter it may delete fewer. Discord can't bulk-delete messages older than 14 days.
- Levels and auto-moderation ignore DMs and other bots. XP is per server.
