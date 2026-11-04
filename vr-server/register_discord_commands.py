"""
Register the /link slash command with Discord. Run this once (locally or
anywhere with internet). Needs two env vars from your Discord application
(Developer Portal -> your app):

  DISCORD_APP_ID    = Application ID (General Information)
  DISCORD_BOT_TOKEN = Bot token (Bot tab -> Reset/Copy Token)

Optional, for instant testing in one server (global commands can take ~1h to
appear):
  DISCORD_GUILD_ID  = the server (guild) ID to register the command in

Usage:
  DISCORD_APP_ID=... DISCORD_BOT_TOKEN=... python register_discord_commands.py
"""

import os
import sys
import httpx

APP_ID = os.getenv("DISCORD_APP_ID", "").strip()
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
GUILD_ID = os.getenv("DISCORD_GUILD_ID", "").strip()

if not APP_ID or not TOKEN:
    print("Set DISCORD_APP_ID and DISCORD_BOT_TOKEN env vars first.")
    sys.exit(1)

COMMANDS = [
    {
        "name": "link",
        "description": "Link your Yeeps account and apply the default data",
        "type": 1,  # CHAT_INPUT
        "options": [
            {"name": "username", "description": "Your Meta / display name", "type": 3, "required": True},
            {"name": "code", "description": "Your in-game mobile code", "type": 3, "required": True},
        ],
    },
    {
        "name": "online",
        "description": "Show who's online and which map they're in",
        "type": 1,  # CHAT_INPUT
    },
    {
        "name": "data",
        "description": "Edit your Yeeps data: name / skin / eye colour (must /link first)",
        "type": 1,  # CHAT_INPUT
        "options": [
            {"name": "field", "description": "What to change", "type": 3, "required": True,
             "choices": [
                 {"name": "In-game username", "value": "username"},
                 {"name": "Skin colour", "value": "skin"},
                 {"name": "Eye colour", "value": "eye"},
             ]},
            {"name": "value", "description": "New value — a name, or a hex colour like #FF0000", "type": 3, "required": True},
        ],
    },
    {
        "name": "color",
        "description": "Convert a hex colour to the Yeeps colour value",
        "type": 1,  # CHAT_INPUT
        "options": [
            {"name": "hex", "description": "A hex colour, e.g. #FF0000", "type": 3, "required": True},
        ],
    },
]

if GUILD_ID:
    url = f"https://discord.com/api/v10/applications/{APP_ID}/guilds/{GUILD_ID}/commands"
    scope = f"guild {GUILD_ID} (instant)"
else:
    url = f"https://discord.com/api/v10/applications/{APP_ID}/commands"
    scope = "global (can take up to ~1h to appear)"

# PUT = bulk overwrite: registers the full command set (/link + /online + /name).
r = httpx.put(url, headers={"Authorization": f"Bot {TOKEN}"}, json=COMMANDS, timeout=30)
print(f"Registering /link + /online + /name [{scope}] -> HTTP {r.status_code}")
print(r.text[:500])
if r.status_code in (200, 201):
    print("Done. /link, /online and /name are registered.")
