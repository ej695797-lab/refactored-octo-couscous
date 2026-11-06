"""
Auto-replies for the Yeeps-Tools Discord bot.

The slash commands come in over HTTP (discord_router.py); this small gateway
client keeps the bot connected so it can read normal messages and answer
common questions by replying to them.

Needs the bot's "Message Content Intent" switched on in the Developer Portal
(Bot -> Privileged Gateway Intents). Token: DISCORD_BOT_TOKEN in
~/.config/yeeps/vr-server.env. Run as the user service yeeps-autoreply.
"""
import asyncio
import json
import os
import re
import time

import httpx
import websockets

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
API = "https://discord.com/api/v10"
HEADERS = {"Authorization": f"Bot {TOKEN}", "User-Agent": "DiscordBot (https://your-domain.example, 1.0)"}
INTENTS = (1 << 0) | (1 << 9) | (1 << 12) | (1 << 15)   # guilds, guild messages, DMs, message content
CHANNEL_COOLDOWN = 15                                    # seconds between replies in one channel
# Only answer in these channels (main-chat, issues). Forums, threads and other channels are ignored.
REPLY_CHANNELS = set(os.getenv("AUTOREPLY_CHANNELS", "1000000000000000008,1000000000000000009").replace(" ", "").split(","))

BAN_REPLY = ("No, using this APK is not bannable. Please read "
             "https://discord.com/channels/1000000000000000001/1000000000000000005 - "
             "if you have questions, please ask blazes.")
# Replies that go out with a picture attached (screenshots for the ban answer).
REPLY_FILES = {}   # no picture with any reply (owner 2026-11-03)

# (pattern, reply). A message must match every regex in the pattern list.
RULES = [
    ([r"\bclone_maker\s+is\s+a\s+bum\b"], "clone_maker you dumb as hell"),
    # "can I get banned (for playing)?", "can the app get you banned on normal yeeps?" (first,
    # so "normal yeeps" doesn't send the reinstall answer). "got banned" is left out on purpose.
    ([r"\b(get|gets|getting|be|being)\s+(me\s+|you\s+|u\s+|us\s+|people\s+|someone\s+|them\s+)?banned\b"
      r"|\bbannable\b"],
     BAN_REPLY),
    # "where is the mod menu?", "where do I get BNM?" (before the APK download rule).
    ([r"\b(mod\s*-?\s*menus?|bnm)\b",
      r"\b(where|wheres|where's|find|get|download|dl|link|links|install|any|send|is\s+there|how)\b"],
     "You can find the mod menus here: https://discord.com/channels/1000000000000000001/1000000000000000002\n"
     "If you want player made ones, check here: https://discord.com/channels/1000000000000000001/1000000000000000003\n\n"
     "If you're looking for BNM, please read here: https://discord.com/channels/1000000000000000001/1000000000000000004"),
    # "my code isn't loading" is the mobile code, not the game (before the load rule).
    ([r"\b(my|the|mobile|phone)\s+(number\s+|phone\s+|in-?game\s+)?codes?\b",
      r"\b(cant|can't|cannot|wont|won't|not|doesnt|doesn't|isnt|isn't|never)\b.*"
      r"\b(load|loads|loading|show|shows|showing|appear|appears|appearing|work|works|working|come\s+up|there)\b",
      r"^(?!.*\d{3})"],
     "Your in-game mobile code is in Yeeps, on the phone next to the TechWeb. Make sure you type exactly "
     "what's there, upper cases matter.\n\nIf your code is blank, that's because you're in my APK, not real Yeeps."),
    ([r"\b(cant|can't|cannot|wont|won't|not|doesnt|doesn't|isnt|isn't|stuck|never|keeps?)\b",
      r"\b(load|loads|loading|join|joining|connect|connecting|get\s+in)\b"],
     "Run `/online` to check how many people are playing. Each version allows 20 players max, so if a "
     "version is full you can't get in. Try a different version with fewer than 20 players, or just wait "
     "for a spot to open up."),
    ([r"\b(how|change|set|make|get|do|can)\b", r"\b(name|username)\b", r"\b(gradient|color|colour|colors|colored|fade|rainbow)\b"],
     "Go to the Colors tab on https://your-domain.example. Use the gradient name maker for a fade, or pick a color "
     "from the palette for one solid color, then copy the name it gives you. In Discord run `/data`, pick "
     "**In-game username** as the field and paste it into **value**. You need to `/link` first."),
    ([r"\b(how|chang\w*|set|make|get|pick|can)\b", r"\b(colou?rs?|skin|eyes?)\b"],
     "In Discord run `/data` and pick a field:\n- **Skin color** or **Eye color**: put a hex color like `#FF0000` "
     "in **value** (the Colors tab on https://your-domain.example has the Yeeps palette).\n- **In-game username**: "
     "for a colored or gradient name, make it on the Colors tab and paste it here.\nYou need to `/link` first."),
    ([r"\b(is|isnt|isn't|are)\s+(this|it|that|the\s+apk|the\s+app)\s+(the\s+)?(real|normal|actual|official|original|regular)\s+yeeps\b"],
     "No, this is a modded version of Yeeps that runs on its own separate servers. Nothing in it touches "
     "your real Yeeps account, progress or items."),
    ([r"\b(how|can|where|go|get|back|switch|play)\b", r"\b(normal|real|regular|original|official|vanilla|actual)\s+yeeps\b"],
     "Uninstall this APK and reinstall Yeeps from the Meta Store, or just click Restore when you open the APK."),
    ([r"\b(where|wheres|where's|find|see|get|cant|can't|cannot|how|empty|blank|nothing|no)\b",
      r"\b(my|the|mobile)\s+(number\s+|phone\s+|in-?game\s+)?codes?\b", r"^(?!.*\d{3})",
      r"^(?!.*\b(need|needed|needs)\b)"],
     "Your in-game mobile code is in Yeeps, on the phone next to the TechWeb. Make sure you type exactly "
     "what's there, upper cases matter.\n\nIf your code is blank, that's because you're in my APK, not real Yeeps."),
    ([r"\b(why|what|is|do|does|only|once|again|every)\b", r"\bmobile\s*codes?\b", r"^(?!.*\d{3})"],   # not someone posting their code
     "The mobile code is only needed once, so we know which account to add. It also lets us tie each player to "
     "their account, so we know who to ban for hacking or racism."),
    ([r"\bbannable\b"],
     BAN_REPLY),
    ([r"\b(ban+ed|ban|bans|get\s+(me\s+)?banned|banning)\b",
      r"\b(apk|mod|mods|modded|this|it|blaze)\b"],
     BAN_REPLY),
    ([r"\b(real|main|actual|normal|original|official)\b", r"\b(account|accounts|acc|data|progress|stuff|items|inventory|save|yeeps)\b",
      r"\b(change|changes|changing|affect|affects|effect|mess|edit|edits|reset|wipe|lose|delete|touch|ruin|safe|remove|removes|take|takes|erase|erases|clear|clears|gone)\b"],
     "No, nothing in this APK touches your real Yeeps account. It runs on its own separate servers, so your "
     "real account, progress and items stay exactly the same."),
    ([r"\b(change|changes|changing|affect|affects|effect|mess|reset|wipe|lose|delete|ruin|remove|removes|take|takes|erase|erases|clear|clears)\b",
      r"\bmy\s+(account|acc|data|progress|stuff|items|inventory|save)\b", r"\b(apk|mod|mods|modded|this|it)\b"],
     "No, nothing in this APK touches your real Yeeps account. It runs on its own separate servers, so your "
     "real account, progress and items stay exactly the same."),
    ([r"\bcards?\b", r"\bcrash(es|ed|ing)?\b"],
     "If you're talking about the debug pack in your inventory that spawns all cards at once, it lags for a "
     "very long time. Just get your cards from the card buyer."),
    ([r"\b(where|wheres|download|downloading|dl|link|install|installing|get|find)\b", r"\b(apk|apks|mods?|modded)\b"],
     "You can get the APK at https://discord.com/channels/1000000000000000001/1000000000000000007 "
     "make sure you follow the tutorial!"),
]
_RULES = [([re.compile(p, re.I) for p in pats], reply) for pats, reply in RULES]
_last_reply: dict = {}


# Messages about a new version / update get no auto-reply at all (owner 2026-10-28): "it said
# to upgrade, I got banned cuz there's a new version" is the version gate, not a ban question.
_SKIP = re.compile(r"\b(updates?|updated|updating|upgrades?|upgraded|upgrading|new\s+(version|update|apk|build))\b", re.I)


def match(text: str):
    if _SKIP.search(text or ""):
        return None
    for pats, reply in _RULES:
        if all(p.search(text) for p in pats):
            return reply
    return None


async def reply_to(client: httpx.AsyncClient, msg: dict, text: str):
    ch = msg["channel_id"]
    now = time.time()
    if now - _last_reply.get(ch, 0) < CHANNEL_COOLDOWN:
        return
    _last_reply[ch] = now
    body = {"content": text, "message_reference": {"message_id": msg["id"], "channel_id": ch, "fail_if_not_exists": False},
            "allowed_mentions": {"replied_user": True, "parse": []}}
    path = REPLY_FILES.get(text)
    if path and os.path.exists(path):
        with open(path, "rb") as f:
            r = await client.post(f"{API}/channels/{ch}/messages", headers=HEADERS,
                                  data={"payload_json": json.dumps(body)},
                                  files={"files[0]": (os.path.basename(path), f.read(), "image/gif")})
    else:
        r = await client.post(f"{API}/channels/{ch}/messages", json=body, headers=HEADERS)
    if r.status_code >= 300:
        print(f"[autoreply] reply failed {r.status_code}: {r.text[:200]}", flush=True)


# Someone typed "/link", "/unlink" or "/data" as plain text, so the slash command never ran. Discord only
# allows "only you can see this" on slash-command responses, so this is a normal channel reply.
_TYPED_CMD = re.compile(r"^\s*/(link|unlink|data)\b", re.I)
_TYPED_CMD_IMG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "link-command-help.webp")
_TYPED_CMD_TEXT = ("hm seems your having issues... (or just a little dumb)\n"
                   "if your on vr try and click the button shown on the image above\n"
                   "and if your not please look it up or ask before pinging ownername")
USER_COOLDOWN = 300
_last_user_help: dict = {}


async def help_typed_cmd(client: httpx.AsyncClient, msg: dict):
    uid = (msg.get("author") or {}).get("id")
    now = time.time()
    if not uid or now - _last_user_help.get(uid, 0) < USER_COOLDOWN:
        return
    _last_user_help[uid] = now
    with open(_TYPED_CMD_IMG, "rb") as f:
        img = f.read()
    ch = msg["channel_id"]
    payload = {"content": _TYPED_CMD_TEXT, "allowed_mentions": {"replied_user": True, "parse": []},
               "message_reference": {"message_id": msg["id"], "channel_id": ch, "fail_if_not_exists": False}}
    r = await client.post(f"{API}/channels/{ch}/messages", headers=HEADERS,
                          data={"payload_json": json.dumps(payload)},
                          files={"files[0]": ("link-command-help.webp", img, "image/webp")})
    if r.status_code >= 300:
        print(f"[autoreply] typed-cmd help failed {r.status_code}: {r.text[:200]}", flush=True)
        return


async def run_once():
    async with httpx.AsyncClient(timeout=20) as client:
        g = await client.get(f"{API}/gateway/bot", headers=HEADERS)
        g.raise_for_status()
        url = g.json()["url"] + "/?v=10&encoding=json"
        async with websockets.connect(url, max_size=2**22) as ws:
            hello = json.loads(await ws.recv())
            interval = hello["d"]["heartbeat_interval"] / 1000
            seq = None

            async def heartbeat():
                while True:
                    await asyncio.sleep(interval)
                    await ws.send(json.dumps({"op": 1, "d": seq}))
            hb = asyncio.create_task(heartbeat())
            await ws.send(json.dumps({"op": 2, "d": {
                "token": TOKEN, "intents": INTENTS,
                "properties": {"os": "linux", "browser": "yeeps-tools", "device": "yeeps-tools"}}}))
            try:
                async for raw in ws:
                    ev = json.loads(raw)
                    if ev.get("s") is not None:
                        seq = ev["s"]
                    op, t, d = ev.get("op"), ev.get("t"), ev.get("d")
                    if op == 1:
                        await ws.send(json.dumps({"op": 1, "d": seq}))
                    elif op in (7, 9):            # reconnect / invalid session
                        return
                    elif t == "READY":
                        print(f"[autoreply] connected as {d['user']['username']}", flush=True)
                    elif t == "MESSAGE_CREATE":
                        if (d.get("author") or {}).get("bot"):
                            continue
                        if d.get("channel_id") not in REPLY_CHANNELS:
                            continue
                        if _TYPED_CMD.match(d.get("content") or "") and d.get("guild_id"):
                            asyncio.create_task(help_typed_cmd(client, d))
                            continue
                        reply = match(d.get("content") or "")
                        if reply:
                            await reply_to(client, d, reply)
            finally:
                hb.cancel()


async def main():
    if not TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN is not set")
    while True:
        try:
            await run_once()
        except websockets.ConnectionClosed as e:
            print(f"[autoreply] gateway closed: {e.code} {e.reason}", flush=True)
            if e.code == 4014:
                print("[autoreply] Message Content Intent is OFF: turn it on in the Developer Portal "
                      "(Bot -> Privileged Gateway Intents).", flush=True)
                await asyncio.sleep(300)
        except Exception as e:
            print(f"[autoreply] error: {e}", flush=True)
        await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
