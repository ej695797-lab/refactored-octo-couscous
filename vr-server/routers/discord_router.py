"""
Discord slash-command bot via HTTP interactions (no always-on gateway process,
so it runs fine on the same Render web service).

Command:
  /link username:<yeeps username> in-game-mobile-code:<code>
     -> finds that player's account and applies the current default data
        (owned items, roles, currency, ... from the editable Default template),
        so a player can set themselves up straight from Discord.

Setup (one time, in the Discord Developer Portal for your application):
  1. General Information -> copy the PUBLIC KEY -> set env DISCORD_PUBLIC_KEY on Render.
  2. General Information -> set "Interactions Endpoint URL" to
       https://api.your-domain.example/discord/interactions
     (Discord will send a PING to verify it; this endpoint answers it.)
  3. Register the /link command once with register_discord_commands.py
     (needs DISCORD_APP_ID + DISCORD_BOT_TOKEN).
"""

import asyncio
import os
import re

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse, PlainTextResponse

import httpx

from services.db import accounts, globals_col, player_statuses, rooms
from config import YEEPS_PROXY_URL
from datetime import datetime, timezone

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
except Exception:  # pragma: no cover
    Ed25519PublicKey = None

router = APIRouter()

DISCORD_PUBLIC_KEY = os.getenv("DISCORD_PUBLIC_KEY", "")
# App id is needed to edit the deferred reply via the interaction webhook. When
# set, /link and /online DEFER (instant ack, then edit in the result) so a slow
# cold-start of the /data relay can't blow Discord's 3s response window. When
# unset, we fall back to answering synchronously (the old behaviour).
DISCORD_APP_ID = os.getenv("DISCORD_APP_ID", "")

# Full-access secret for the /data relay (server.js: "<FULL_SECRET>" => full account,
# including SECRET_PROPERTIES). Overridable via env so it isn't pinned in code.
DATA_SECRET = os.getenv("DATA_SECRET", "change-me")

# The slash commands this bot exposes. Kept here so the running service can
# (re)register them itself via /discord/register-commands, in addition to the
# standalone register_discord_commands.py script. Keep the two in sync.
_SLASH_COMMANDS = [
    {"name": "link", "description": "Link your Yeeps account and apply the default data", "type": 1,
     "options": [
         {"name": "username", "description": "Your Yeeps username, exactly as in game (upper cases matter)", "type": 3, "required": True},
         {"name": "in-game-mobile-code", "description": "The code on the phone next to the TechWeb in Yeeps", "type": 3, "required": True},
     ]},
    {"name": "online", "description": "Show who's online and which map they're in", "type": 1},
    {"name": "editor", "description": "Data managers: get your one-time sign-in code for the live editor", "type": 1},
    {"name": "unlink", "description": "Unlink your Discord from your Yeeps account (your account and stuff stay)", "type": 1},
    {"name": "discord", "description": "Put your Yeeps stats on your Discord profile (profile widget)", "type": 1},
    {"name": "data", "description": "Edit your Yeeps data: name / skin / eye color (must /link first)", "type": 1,
     "options": [
         {"name": "field", "description": "What to change", "type": 3, "required": True,
          "choices": [
              {"name": "In-game username", "value": "username"},
              {"name": "Skin color", "value": "skin"},
              {"name": "Eye color", "value": "eye"},
              {"name": "Private world map", "value": "pw"},
              {"name": "Private world sandbox map", "value": "pws"},
          ]},
         {"name": "value", "description": "A name, a hex color like #FF0000, or your map code (YT-...) from the Map Maker", "type": 3, "required": False},
         {"name": "file", "description": "Private world: the .json file from your-domain.example/mapmaker", "type": 11, "required": False},
     ]},
    # /color removed 2026-10-19: the hex converter + gradient name maker live on
    # your-domain.example (Colors tab) now.
]

# Interaction / response type enums.
_PING = 1
_APPLICATION_COMMAND = 2
_PONG = 1
_CHANNEL_MESSAGE = 4
_DEFERRED = 5  # DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE: ack now, edit the reply later
_EPHEMERAL = 64  # response flag: only the invoking user sees it

# Account fields the /link apply must NEVER touch (identity / credentials /
# anti-tamper / per-session). Everything else from the default template is
# applied (items, roles, currency, cosmetics, ...).
_PROTECTED = {
    "accountID", "displayName", "displayName_searchable", "oculusID",
    "e_accessKey", "e_secretKey", "mobilePassword", "mobileID", "mobileCode",
    "gameSessionID", "publicSessionID", "challengeNonce", "hash", "banHash",
    "firstLogin", "skinColor", "eyeColor",
}


def _verify(signature: str, timestamp: str, body: bytes) -> bool:
    if not DISCORD_PUBLIC_KEY or Ed25519PublicKey is None:
        return False
    try:
        pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(DISCORD_PUBLIC_KEY))
        pk.verify(bytes.fromhex(signature), timestamp.encode() + body)
        return True
    except Exception:
        return False


def _msg(text: str, ephemeral: bool = True):
    data = {"content": text[:1900]}
    if ephemeral:
        data["flags"] = _EPHEMERAL
    return JSONResponse({"type": _CHANNEL_MESSAGE, "data": data})


async def _edit_deferred(app_id: str, token: str, text: str) -> None:
    """Edit the original (deferred) interaction reply with the real result.
    Uses the app id + interaction token (both from the payload); no bot token."""
    if not app_id or not token:
        return
    url = f"https://discord.com/api/v10/webhooks/{app_id}/{token}/messages/@original"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            await client.patch(url, json={"content": text[:1900]})
    except Exception:
        pass  # best-effort: the user still sees the "thinking…" state otherwise


async def _run_and_edit(coro_fn, app_id: str, token: str, fail_prefix: str) -> None:
    try:
        text = await coro_fn()
    except Exception as e:
        text = f"{fail_prefix} ({e})"
    await _edit_deferred(app_id, token, text)


async def _effective_default() -> dict:
    from handlers.login import DEFAULT_ACCOUNT
    merged = dict(DEFAULT_ACCOUNT)
    ov = await globals_col.find_one({"key": "default_account_override"}, {"_id": 0, "value": 1})
    if ov and isinstance(ov.get("value"), dict):
        merged.update(ov["value"])
    return merged


async def _audit(text: str) -> None:
    """Send a line to the ADMIN audit channel (shared with the editor webhook)."""
    try:
        from routers.editor_router import _discord_notify
        await _discord_notify(text)
    except Exception:
        pass


# One account per Discord (owner 2026-10-18: "people can still link more then one
# account"). The old check only stopped a DIFFERENT Discord from claiming a linked
# account; one Discord could still link any number of alts. Discord IDs listed in
# env MULTI_LINK_DISCORD_IDS (comma-separated) are exempt.
MULTI_LINK_DISCORD_IDS = set(filter(None, (os.getenv("MULTI_LINK_DISCORD_IDS") or "").replace(" ", "").split(",")))


async def _discord_linked_accounts(discord_id: str) -> list:
    """Every account this Discord is linked to, on 1.31 and live (incl. live's
    discord_links records): [{accountID, displayName, oculusID}]."""
    found = {}
    try:
        from services.db import client
        cols = [accounts, client[LIVE_DB]["accounts"]]
        for col in cols:
            async for a in col.find({"discordID": discord_id},
                                    {"_id": 0, "accountID": 1, "displayName": 1, "oculusID": 1}):
                if a.get("accountID"):
                    found.setdefault(a["accountID"], a)
        async for l in client[LIVE_DB]["discord_links"].find({"discordID": discord_id}, {"_id": 0, "accountID": 1}):
            if l.get("accountID"):
                found.setdefault(l["accountID"], {"accountID": l["accountID"]})
    except Exception as e:
        print(f"[discord] linked-accounts lookup failed for {discord_id}: {e}")
    return list(found.values())


async def _do_link(username: str, code: str, discord_id: str = "", discord_name: str = "") -> str:
    username = (username or "").strip()
    code = (code or "").strip()
    if not username:
        return "Give me a username: `/link username:<your Yeeps username> in-game-mobile-code:<code>`"
    if not re.fullmatch(r"\d{4}", code):
        return ("Your in-game mobile code must be the 4 digits on the phone next to the TechWeb in Yeeps. "
                "Make sure you put exactly what's in real Yeeps, upper cases matter.")

    # A Discord whose account was banned can't /link an alt (owner 2026-11-02).
    if discord_id:
        from services.discord_bans import banned_discord
        _b = await banned_discord(discord_id)
        if _b:
            await _audit(f"BLOCKED /link: **{discord_name}** (`{discord_id}`) is banned "
                         f"(account `{_b.get('accountID')}`), tried **{username}**.")
            return "This Discord is banned from linking Yeeps accounts."

    # Block unverified Discord users (the @None Verified role) BEFORE the Yeeps
    # login call below, so a blocked user never burns a mobile-code attempt.
    if discord_id:
        try:
            from services.discord_roles import has_role
            _nv_role = os.getenv("NONE_VERIFIED_ROLE_NAME", "None Verified")
            if await has_role(discord_id, _nv_role):
                await _audit(f"⛔ **{discord_name}** (`{discord_id}`) tried to /link but has the "
                             f"**{_nv_role}** role — blocked.")
                return ("You need to get verified in the server before you can `/link`. "
                        "Once an admin removes your **None Verified** role, try again.")
        except Exception as _e:
            print(f"[discord] None-Verified check failed for {discord_id}: {_e}")
        # Owner 2026-10-28: anyone with this role can't /link (comma list in env to change).
        try:
            from services.discord_roles import has_role_id
            for _rid in os.getenv("NO_LINK_ROLE_IDS", "1000000000000000006").split(","):
                if _rid.strip() and await has_role_id(discord_id, _rid.strip()):
                    await _audit(f"**{discord_name}** (`{discord_id}`) tried to /link but has the "
                                 f"no-link role ({_rid.strip()}) - blocked.")
                    return "You can't use `/link` with the role you have."
        except Exception as _e:
            print(f"[discord] no-link role check failed for {discord_id}: {_e}")

    # One account per Discord, checked BEFORE the Yeeps login so a refused /link
    # doesn't burn one of the player's limited mobile-code attempts. Re-linking the
    # same account is fine (matched by Meta name here, by accountID after login).
    _mine = []
    if discord_id and discord_id not in MULTI_LINK_DISCORD_IDS:
        _mine = await _discord_linked_accounts(discord_id)
        _u = username.lower()
        if _mine and not any(_u in {str(a.get("oculusID") or "").lower(), str(a.get("displayName") or "").lower()}
                             for a in _mine):
            _names = ", ".join(f"{a.get('displayName') or '?'} (`{a['accountID']}`)" for a in _mine)
            await _audit(f"⛔ **{discord_name}** (`{discord_id}`) tried to /link a second account "
                         f"(**{username}**) — already linked to {_names}. Rejected before login.")
            return ("Your Discord is already linked to a Yeeps account. One account per Discord — "
                    "if you need to switch accounts, contact an admin.")

    # 1. Fetch the player's REAL account data from yeeps-proxy's /data relay
    #    (same login the editor's import uses), so /link works even before the
    #    player has ever been seen by this server.
    #
    #    The relay runs on Render's free tier and spins down when idle. The first
    #    request after a sleep gets a cold-start gateway error, which used to blow
    #    up resp.json() as the opaque "Expecting value: line 1 column 1 (char 0)".
    #    We're inside a DEFERRED interaction (15-min budget, not Discord's 3s), so
    #    we can wait the relay out.
    #
    #    ATTEMPT SAFETY: each /data call makes a REAL mobile-code link attempt on
    #    Yeeps, and Yeeps locks the account after a few (OUT_OF_ATTEMPTS). So we
    #    only retry when we're sure the relay did NOT forward upstream: a cold-
    #    start 502/503 (app not even up) or a failure to establish the connection.
    #    A 504 or a read timeout means the request WAS forwarded and may have
    #    already burned an attempt -> never retry those.
    data = None
    for attempt in range(4):
        try:
            async with httpx.AsyncClient(timeout=40) as client:
                resp = await client.post(
                    f"{YEEPS_PROXY_URL}/data",
                    json={"oculusID": username, "mobileCode": code,
                          "secret": DATA_SECRET},
                )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            await asyncio.sleep(2)   # never connected -> no upstream attempt, safe
            continue
        except Exception:
            return ("The login service didn't answer in time. Give it a minute and "
                    "run `/link` **once** more — each try uses one of Yeeps' limited "
                    "code attempts, so don't spam it.")
        try:
            data = resp.json()
            break
        except Exception:
            if resp.status_code in (502, 503):
                await asyncio.sleep(3)   # app still waking, not forwarded yet
                continue
            return ("The login service hiccuped. Give it a minute and run `/link` "
                    "**once** more (don't spam it — each try uses a code attempt).")
    if data is None:
        return ("The login service was waking up and didn't answer in time — "
                "give it a minute and run `/link` again.")
    if resp.status_code != 200 or not data.get("ok"):
        # Show exactly what Yeeps returned (no rate-limit reinterpretation).
        err = str(data.get("error") or "").strip()
        return ("Login failed, double check your username and in-game mobile code (the phone "
                "next to the TechWeb in Yeeps). Make sure you put exactly what's in real Yeeps, "
                "upper cases matter. "
                f"({err or 'wrong username or in-game mobile code'})")
    account_id = data.get("accountID")
    if not account_id:
        return "Login worked but no account id came back — try again."

    existing = await accounts.find_one(
        {"accountID": account_id},
        {"_id": 0, "displayName": 1, "discordID": 1, "discordUsername": 1})
    prior = (existing or {}).get("discordID")
    disp = (existing or {}).get("displayName") or data.get("oculusID") or username

    # One Discord per account: a DIFFERENT Discord user can't claim an account
    # that's already linked. Log the attempt so account-takeover tries are seen.
    if prior and discord_id and prior != discord_id:
        await _audit(
            f"⚠️ **{discord_name}** (`{discord_id}`) tried to /link **{disp}** (`{account_id}`) — "
            f"already linked to Discord `{prior}` ({(existing or {}).get('discordUsername') or '?'}). Rejected.")
        return ("That Yeeps account is already linked to a different Discord account. "
                "If you need to link another account do /unlink.")

    if _mine and account_id not in {a["accountID"] for a in _mine}:
        _names = ", ".join(f"{a.get('displayName') or '?'} (`{a['accountID']}`)" for a in _mine)
        await _audit(f"⛔ **{discord_name}** (`{discord_id}`) tried to /link a second account "
                     f"**{disp}** (`{account_id}`) — already linked to {_names}. Rejected.")
        return ("Your Discord is already linked to a Yeeps account. One account per Discord — "
                "if you need to switch accounts, contact an admin.")

    # No VPN: /link comes from Discord, so check the network this account last
    # played from on our servers (owner 2026-11-02). Brand-new accounts have none.
    try:
        from services import geo
        from services.db import client as _vc
        _last = None
        for _col in (accounts, _vc[LIVE_DB]["accounts"]):
            _a = await _col.find_one({"accountID": account_id}, {"_id": 0, "lastIP": 1})
            if _a and _a.get("lastIP"):
                _last = _a["lastIP"]
                if _col is not accounts:
                    break   # prefer the live (newest) game's IP
        _vpn = await geo.vpn_check(_last) if _last else ""
        if _vpn:
            await _audit(f"BLOCKED /link: **{discord_name}** (`{discord_id}`) -> **{disp}** (`{account_id}`): "
                         f"account last played from a VPN (`{_last}`, {_vpn}).")
            return ("Your Yeeps account last played from a VPN. Turn the VPN off, open Yeeps once, "
                    "then run `/link` again.")
    except Exception as _e:
        print(f"[discord] /link VPN check failed for {account_id}: {_e}")

    # 2. Base = their real player data; 3. default data applied on top (items,
    #    roles, currency, cosmetics, ...), never touching identity/credentials.
    # Never store the player's REAL Yeeps mobile code / login secrets that the
    # official account data carries.
    from services.account_privacy import public_view
    doc = {"accountID": account_id,
           "oculusID": data.get("oculusID", username),
           **public_view(data.get("stats") or {})}
    # Remember who linked from Discord, so /name can find this account later by
    # the caller's Discord id without asking for the Meta name + code again.
    if discord_id:
        doc["discordID"] = discord_id
        doc["discordUsername"] = discord_name
    # Preserve a display name the player set with /name: don't let a re-link
    # overwrite it with the Meta/backend name once the account already exists.
    if existing and existing.get("displayName"):
        doc.pop("displayName", None)
        doc.pop("displayName_searchable", None)
    # Guarantee a display name so the player is identifiable in logs and in-game.
    elif not doc.get("displayName"):
        doc["displayName"] = data.get("oculusID") or username
    # No invisible names (blank, invisible characters or hidden rich text).
    from services.names import is_visible_name, fallback_name
    if "displayName" in doc and not is_visible_name(doc["displayName"]):
        doc["displayName"] = fallback_name(data.get("oculusID") or username, account_id)
        doc["displayName_searchable"] = doc["displayName"].lower()
    # No two players with the same name (Nathan -> Nathan2).
    if "displayName" in doc:
        from services.names import unique_name
        doc["displayName"] = await unique_name(doc["displayName"], account_id)
        doc["displayName_searchable"] = doc["displayName"].lower()
    for k, v in (await _effective_default()).items():
        if k not in _PROTECTED:
            doc[k] = v
    await accounts.update_one({"accountID": account_id}, {"$set": doc}, upsert=True)
    on_2314 = await _verify_2314(account_id, discord_id, discord_name)

    final_name = (existing or {}).get("displayName") or doc.get("displayName") or data.get("oculusID", username)
    await _audit(f"✅ **{discord_name}** (`{discord_id}`) "
                 f"{'re-linked' if existing else 'linked'} **{final_name}** (`{account_id}`)"
                 + (" — verified on 2.31.4" if on_2314 else ""))
    return (f"Linked **{data.get('oculusID', username)}** — launch Yeeps and your stuff will be there."
            "\nYou're verified, so you can play 2.31.4 too (restart it if it was open).")


async def _do_unlink(discord_id: str, discord_name: str) -> str:
    """Remove this Discord's link from every account it is linked to (1.31 + live
    accounts and live discord_links). The account itself is never touched beyond
    the link fields. The old Discord is kept on the account (oldDiscordID /
    oldDiscordUsername + an oldDiscords history list) and in Yeeps240.discord_unlinks."""
    if not discord_id:
        return "I couldn't read your Discord account - try again."
    linked = await _discord_linked_accounts(discord_id)
    if not linked:
        return "Your Discord isn't linked to a Yeeps account."
    from services.db import client
    # A banned account keeps its Discord: unlinking would let them /link an alt.
    for col in (accounts, client[LIVE_DB]["accounts"]):
        if await col.find_one({"accountID": {"$in": [a["accountID"] for a in linked]},
                               "adminBlocked": 1}, {"_id": 1}):   # adminBlocked = a real ban (login uses only this)
            await _audit(f"BLOCKED /unlink: **{discord_name}** (`{discord_id}`) is linked to a banned account.")
            return "You can't unlink a banned account."
    at = datetime.now(timezone.utc).strftime("%Y %m/%d %H:%M:%S")
    names = []
    for a in linked:
        aid = a["accountID"]
        old_name = discord_name
        disp = a.get("displayName") or ""
        for col in (accounts, client[LIVE_DB]["accounts"]):
            d = await col.find_one({"accountID": aid}, {"_id": 0, "displayName": 1, "discordID": 1, "discordUsername": 1})
            if not d or str(d.get("discordID") or "") != discord_id:
                continue
            old_name = d.get("discordUsername") or old_name
            disp = disp or d.get("displayName") or ""
            await col.update_one(
                {"accountID": aid},
                {"$set": {"oldDiscordID": discord_id, "oldDiscordUsername": old_name},
                 "$push": {"oldDiscords": {"discordID": discord_id, "discordUsername": old_name,
                                           "unlinkedAt": at}},
                 "$unset": {"discordID": "", "discordUsername": ""}})
        ldb = client[LIVE_DB]
        async for l in ldb["discord_links"].find({"accountID": aid, "discordID": discord_id}, {"_id": 0}):
            await ldb["discord_unlinks"].insert_one({**l, "unlinkedAt": at, "by": "self"})
        await ldb["discord_links"].delete_many({"accountID": aid, "discordID": discord_id})
        names.append(f"**{disp or aid}** (`{aid}`)")
    await _audit(f"**{discord_name}** (`{discord_id}`) unlinked their Discord from {', '.join(names)}. "
                 f"Old Discord saved on the account.")
    return (f"Unlinked your Discord from {', '.join(n.split(' (')[0] for n in names)}. "
            "Your account and everything on it are still there. "
            "You can `/link` again any time (the same account or a different one).")


async def _do_editor_code(discord_id: str, discord_name: str) -> str:
    """One-time live-editor sign-in code for a data manager (owner 2026-11-02). The
    shared code + a typed name let anyone sign in as any data manager; this code is
    only given to the Discord linked to the account, so it proves who they are.
    Stored hashed in Yeeps240.editor_otp; the live editor checks and burns it."""
    import hashlib, secrets, time as _t
    if not discord_id:
        return "I couldn't read your Discord account - try again."
    from services.discord_bans import banned_discord
    if await banned_discord(discord_id):
        return "This Discord is banned."
    from services.discord_roles import has_editor_role
    if not await has_editor_role(discord_id):
        await _audit(f"/editor refused: **{discord_name}** (`{discord_id}`) doesn't have the Data Manager role.")
        return "You need the Data Manager role to use the editor."
    linked = await _discord_linked_accounts(discord_id)
    if not linked:
        return "Link your Yeeps account first with `/link`, then run `/editor` again."
    from services.db import client
    code = f"{secrets.randbelow(10**8):08d}"
    await client[LIVE_DB]["editor_otp"].insert_one({
        "h": hashlib.sha256(code.encode()).hexdigest(), "discordID": discord_id,
        "discordUsername": discord_name, "accountIDs": [a["accountID"] for a in linked],
        "exp": int(_t.time()) + 600, "used": False})
    names = ", ".join(a.get("displayName") or a["accountID"] for a in linked)
    await _audit(f"**{discord_name}** (`{discord_id}`) got a one-time editor code for {names}.")
    return (f"Your editor sign-in code: **{code}**\n"
            "It works once, for 10 minutes. Go to https://your-domain.example/editor/live, type your "
            "in-game name and put this code in the password box. Don't share it.")


async def _verify_2314(account_id: str, discord_id: str, discord_name: str) -> bool:
    """2.31.4 (the biglive APKs, on the 2.4 server / Yeeps240) only lets in accounts
    with the "verified" role (owner 2026-10-13). A player's accountID is the same in
    both databases, so a /link here verifies them there too: the role on their 2.31.4
    account if they have one, and a Yeeps240.discord_links record so the 2.4 server
    can verify someone who links before they first open 2.31.4. True if an existing
    2.31.4 account got the role."""
    try:
        from services.db import client
        ldb = client[LIVE_DB]
        at = datetime.now(timezone.utc).strftime("%Y %m/%d %H:%M:%S")
        await ldb["discord_links"].update_one(
            {"accountID": account_id},
            {"$set": {"accountID": account_id, "discordID": discord_id,
                      "discordUsername": discord_name, "linkedAt": at}}, upsert=True)
        r = await ldb["accounts"].update_one(
            {"accountID": account_id},
            {"$addToSet": {"roleKeys": "verified"},
             "$set": {"discordID": discord_id, "discordUsername": discord_name}})
        return r.matched_count > 0
    except Exception as e:
        print(f"[discord] 2.31.4 verify failed for {account_id}: {e}")
        return False


_HEX6_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")
_HEX3_RE = re.compile(r"^#?([0-9a-fA-F]{3})$")


def _parse_hex(s: str):
    """'#RRGGBB' / '#RGB' (leading # optional) -> (R, G, B), or None if invalid."""
    s = (s or "").strip()
    if _HEX6_RE.match(s):
        h = _HEX6_RE.match(s).group(1)
    elif _HEX3_RE.match(s):
        h = "".join(c * 2 for c in _HEX3_RE.match(s).group(1))
    else:
        return None
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _hex_to_yeeps(r: int, g: int, b: int) -> int:
    """Packed Yeeps color: signed int32 = (R<<24)|(G<<16)|(B<<8)|0xFF."""
    v = (r << 24) | (g << 16) | (b << 8) | 0xFF
    return v - 0x100000000 if v >= 0x80000000 else v


async def _do_color(hex_str: str) -> str:
    """Pure converter: hex color -> the packed Yeeps color value."""
    rgb = _parse_hex(hex_str)
    if rgb is None:
        return "Give me a hex color like `/color hex:#FF0000` (3 or 6 hex digits)."
    r, g, b = rgb
    return (f"**#{r:02X}{g:02X}{b:02X}**  ·  R {r}, G {g}, B {b}\n"
            f"Yeeps color value: **`{_hex_to_yeeps(r, g, b)}`**")


_NAME_CAP = 20          # visible characters in a plain (no gradient) name
_GRADIENT_CAP = 300     # visible characters in a gradient name (long names are fine: the game shows ~14k-char names)
_GRADIENT_RAW = 12000   # raw length of a gradient name, tags included
_HEX_TOKEN = re.compile(r"#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9a-fA-F])")


def _parse_hex_list(text: str) -> list:
    """'#FF0000 #00f' / 'FF0000,0000FF' -> [(r,g,b), ...] (skips anything that isn't hex)."""
    out = []
    for m in _HEX_TOKEN.finditer(text or ""):
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        out.append((int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)))
    return out


def _gradient_color(f: float, stops: list) -> tuple:
    """Color at position f (0..1) along the stops: 2 colors = start..end,
    3 colors = start, middle, end (the middle color is at the centre)."""
    def lerp(a, b, t):
        return tuple(round(a[k] + (b[k] - a[k]) * t) for k in range(3))
    if len(stops) == 2:
        return lerp(stops[0], stops[1], f)
    if f <= 0.5:
        return lerp(stops[0], stops[1], f * 2)
    return lerp(stops[1], stops[2], (f - 0.5) * 2)


def _gradient_name(text: str, stops: list) -> str:
    """Color each visible character. One sweep (start -> end, or start -> middle -> end)
    per 20 visible letters, bouncing back and forth, so longer names get more color
    changes. Spaces stay plain. Plain text only."""
    n = sum(1 for c in text if not c.isspace())
    sweeps = max(1, n // 20)
    per = n / sweeps if n else 1
    out, k = [], 0
    for c in text:
        if c.isspace():
            out.append(c)
            continue
        x = (k / per) % 2          # position in the bounce, 0..2
        f = x if x <= 1 else 2 - x
        r, g, b = _gradient_color(f, stops)
        out.append(f"<color=#{r:02X}{g:02X}{b:02X}>{c}</color>")
        k += 1
    return "".join(out)


async def _do_gradient(discord_id: str, discord_name: str, name: str, colors: str) -> str:
    """/color mode:gradient - set the in-game name to a fade across 2 or 3 colors,
    long names allowed (up to 300 visible characters)."""
    if not discord_id:
        return "I couldn't read your Discord account - try again."
    acct = await accounts.find_one({"discordID": discord_id}, {"_id": 0, "accountID": 1})
    if not acct:
        return ("You have to relink! I don't have your Discord tied to a Yeeps account yet - "
                "run `/link username:<Yeeps username> in-game-mobile-code:<code>` first.")
    name = (name or "").strip()
    if not name:
        return "Give me the name to fade, e.g. `/color mode:gradient name:Blaze colors:#FF0000 #0000FF`."
    if any(ord(c) < 32 for c in name):
        return "That name has control characters I can't use - no newlines or tabs."
    if "<" in name or ">" in name:
        return "Gradient names are plain text - type the name without <color> tags."
    stops = _parse_hex_list(colors or "")
    if len(stops) not in (2, 3):
        return ("Give 2 or 3 colors: `colors:#FF0000 #0000FF`, or 3 for start, middle, end: "
                "`colors:#FF0000 #FFFF00 #0000FF`.")
    from services.names import is_visible_name, visible_count
    if not is_visible_name(name):
        return "That name wouldn't show up in game. Use at least one visible character."
    if visible_count(name) > _GRADIENT_CAP:
        return f"Gradient names can be at most {_GRADIENT_CAP} characters (yours is {visible_count(name)})."
    value = "<b>" + _gradient_name(name, stops) + "</b>"   # bold, like the long names in game
    if len(value) > _GRADIENT_RAW:
        return "That gradient name is too long - use fewer characters."
    try:
        from handlers.login import _is_banned_name
        if _is_banned_name(name):
            return "That name isn't allowed."
    except Exception:
        pass
    acct_id = acct.get("accountID")
    await accounts.update_one(
        {"discordID": discord_id},
        {"$set": {"displayName": value, "displayName_searchable": name.lower(),
                  "discordUsername": discord_name}})
    await _audit(f"✏️ **{discord_name}** (`{discord_id}`) set a {len(stops)}-color gradient name "
                 f"**{name}** (`{acct_id}`)")
    return (f"Your in-game name is now a {len(stops)}-color fade: **{name}** - relaunch Yeeps to see it.")


async def _set_account_both(account_id: str, fields: dict) -> None:
    """Apply an account edit to BOTH versions so /data shows on each: the 1.31 DB
    (Yeeps119, this server's accounts) and the 2.31.4 DB (Yeeps240), matched by
    accountID. Yeeps240 is updated only where that account exists (no partial doc);
    a player who hasn't played 2.31.4 yet just has nothing to update there."""
    await accounts.update_one({"accountID": account_id}, {"$set": fields})
    try:
        from services.db import client as _mc
        await _mc[LIVE_DB]["accounts"].update_one({"accountID": account_id}, {"$set": fields})
    except Exception as e:
        print(f"[data] {LIVE_DB} update failed for {account_id}: {e}")


async def _do_data(discord_id: str, discord_name: str, field: str, value: str) -> str:
    """Edit the account linked to this Discord user: username / skin / eye color.
    Requires the caller's Discord to already be linked (/link)."""
    if not discord_id:
        return "I couldn't read your Discord account — try again."
    acct = await accounts.find_one({"discordID": discord_id}, {"_id": 0, "accountID": 1})
    if not acct:
        return ("You have to relink! I don't have your Discord tied to a Yeeps account yet — "
                "run `/link username:<Yeeps username> in-game-mobile-code:<code>` first.")
    value = (value or "").strip()
    if not value:
        return "Give me a value to set (a name, or a hex color like `#FF0000`)."

    if field == "username":
        if any(ord(c) < 32 for c in value):
            return "That name has control characters I can't use — no newlines/tabs."
        from services.names import is_visible_name, visible_count
        if not is_visible_name(value):
            return ("That name wouldn't show up in game (only spaces, invisible characters "
                    "or hidden text). Use at least one visible character.")
        if visible_count(value) > _NAME_CAP:
            return (f"Names can be at most {_NAME_CAP} characters (yours is {visible_count(value)}). "
                    "For a color fade, make one in the Colors tab on https://your-domain.example.")
        if len(value) > 400:
            return "That name is too long. Use fewer characters."
        try:
            from handlers.login import _is_banned_name
            if _is_banned_name(value):
                return "That name isn't allowed."
        except Exception:
            pass
        acct_id = acct.get("accountID")
        from services.names import name_owner
        if await name_owner(value, acct_id):
            return "That name is already taken by another player. Pick a different one (names have to be unique so we can tell players apart)."
        await _set_account_both(acct_id, {"displayName": value, "displayName_searchable": value.lower(),
                                          "discordUsername": discord_name})
        await _audit(f"✏️ **{discord_name}** (`{discord_id}`) set their name to "
                     f"**{value}** (`{acct_id}`)")
        return f"Your in-game name is now **{value}** — relaunch Yeeps to see it (both versions)."

    if field in ("skin", "eye"):
        rgb = _parse_hex(value)
        if rgb is None:
            return "Give me a hex color like `#FF0000` for the color."
        r, g, b = rgb
        col = _hex_to_yeeps(r, g, b)
        key = "skinColor" if field == "skin" else "eyeColor"
        acct_id = acct.get("accountID")
        await _set_account_both(acct_id, {key: col, "discordUsername": discord_name})
        label = "skin" if field == "skin" else "eye"
        await _audit(f"✏️ **{discord_name}** (`{discord_id}`) set their {label} color to "
                     f"**#{r:02X}{g:02X}{b:02X}** (`{col}`) (`{acct_id}`)")
        return (f"Your {label} color is now **#{r:02X}{g:02X}{b:02X}** "
                f"(`{col}`) — relaunch Yeeps to see it (both versions).")

    return "Pick what to edit: `username`, `skin`, or `eye`."


_PW_COOLDOWN: dict = {}


async def _do_pw_map(discord_id: str, discord_name: str, which: str, value: str, att: dict) -> str:
    """/data field:pw|pws — put a Map Maker map into the caller's private world
    (main or sandbox). The map JSON comes from an attached .json file or the
    value text. It is written to the game the map was made for (live by default),
    owned by the player, and loads the next time they enter that world."""
    import json as _json, time as _t, base64 as _b64
    if not discord_id:
        return "I couldn't read your Discord account — try again."
    acct = await accounts.find_one({"discordID": discord_id}, {"_id": 0, "accountID": 1})
    if not acct:
        return ("You have to relink! I don't have your Discord tied to a Yeeps account yet — "
                "run `/link username:<Yeeps username> in-game-mobile-code:<code>` first.")
    now = _t.time()
    if now - _PW_COOLDOWN.get(discord_id, 0) < 60:
        return "Wait a minute before loading another map."
    raw = (value or "").strip()
    import re as _re
    if _re.fullmatch(r"(?i)YT-[A-Z0-9]{6}", raw):          # share code from the Map Maker
        raw = ""   # Map Maker share codes need the website (not included)
        if not raw:
            return "That map code doesn't exist or has expired (codes last a week). Make a new one on the Map Maker."
    if att:
        url = str(att.get("url") or "")
        host = url.split("/")[2] if url.count("/") >= 3 else ""
        if host not in ("cdn.discordapp.com", "media.discordapp.net"):
            return "That attachment didn't come from Discord."
        if int(att.get("size") or 0) > 8_000_000:
            return "That file is too big (8 MB max)."
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(url)
            raw = r.text
        except Exception as e:
            return f"I couldn't download that file ({e})."
    if not raw:
        return ("Paste your map code from your-domain.example/mapmaker (**Copy code for /data**, looks like `YT-AB12CD`) "
                "into `value`.")
    try:
        o = _json.loads(raw)
    except Exception:
        return "That isn't valid map JSON. Use **Copy raw JSON** or **Download .json** from the Map Maker."
    blocks = o if isinstance(o, list) else (o.get("blocks") if isinstance(o, dict) else None)
    if not isinstance(blocks, list) or not blocks:
        return "I couldn't find any blocks in that map."
    if len(blocks) > 30000:
        return "That map has too many blocks (30,000 max)."
    game = (o.get("game") if isinstance(o, dict) else None) or "live"
    acct_id = acct["accountID"]
    room_key = f"p_{acct_id}" + ("_sandbox" if which == "pws" else "")
    # Private worlds are ONE shared copy in the 1.31 DB: live reads them from there
    # too (CARRYOVER_DB / services.db.maps_for on the live server), so a copy in the
    # live DB is never seen.
    from services.db import client as _mc, db as target
    room = (await target["rooms"].find_one({"roomKey": room_key}, {"_id": 0, "dimensions": 1})
            or await _mc[LIVE_DB]["rooms"].find_one({"roomKey": room_key}, {"_id": 0, "dimensions": 1}))
    dims = (room or {}).get("dimensions") or ([32, 80, 32] if which == "pws" else [80, 80, 80])
    from services.role_locks import DISABLED_PATTERNS
    norm, dropped = [], 0
    for b in blocks:
        if not isinstance(b, dict) or not b.get("name") or b.get("name") in DISABLED_PATTERNS:
            dropped += 1
            continue
        try:
            x, y, z = int(b.get("x", 0)), int(b.get("y", 0)), int(b.get("z", 0))
        except Exception:
            dropped += 1
            continue
        if not (0 <= x < dims[0] and 0 <= y < dims[1] and 0 <= z < dims[2]):
            dropped += 1
            continue
        nb = {"name": str(b["name"]), "x": x, "y": y, "z": z,
              "fwd": int(b.get("fwd", 4) or 0), "up": int(b.get("up", 2) or 0),
              "color": str(b.get("color", "") or ""), "owner": acct_id}     # it's their world
        if b.get("state"):
            nb["state"] = str(b["state"])[:2000]
        norm.append(nb)
    if not norm:
        return f"None of those blocks fit in your private world ({dims[0]}×{dims[1]}×{dims[2]})."
    from services.mapsync_serializer import serialize_blocks_spam
    try:
        save_data = serialize_blocks_spam(norm)
    except Exception as e:
        return f"I couldn't encode that map ({e})."
    _PW_COOLDOWN[discord_id] = now
    maps_col = target["maps"]
    map_key = f"{room_key}$current"
    cur = await maps_col.find_one({"mapKey": map_key}, {"_id": 0}) or {}
    if cur.get("saveData"):     # snapshot so the editor's Revert can undo it
        await target["map_history"].insert_one({"roomKey": room_key, "ts": int(now), "savedBy": cur.get("savedBy"),
                                               "savedAt": cur.get("savedAt"), "bytes": len(cur.get("saveData") or ""),
                                               "doc": cur, "preDiscordLoad": True})
    states = [((b["x"], b["y"], b["z"]), b["state"]) for b in norm if b.get("state")]
    from services.dynamo_ops import _build_spam_states
    await maps_col.update_one({"mapKey": map_key}, {"$set": {
        "mapKey": map_key, "roomKey": room_key, "saveData": save_data, "editorBlocks": norm,
        "saveInternalStates": _build_spam_states(states), "mapDataVersion": 3,
        "savedBy": f"discord:{discord_name}", "applicationVersion": "1.31.0",
        "savedAt": datetime.now(timezone.utc).strftime("%Y %m/%d %H:%M:%S")},
        "$unset": {"saveInternalStatesFull": ""}}, upsert=True)
    for _rdb in (target, _mc[LIVE_DB]):   # int, never a string (see editor save_map_blocks)
        await _rdb["rooms"].update_one({"roomKey": room_key}, {"$set": {"lastEditedSessionID": int(now)}})
    where = "private world sandbox" if which == "pws" else "private world"
    ver = "1.31 and live"
    await _audit(f"🗺️ **{discord_name}** (`{discord_id}`) loaded a Map Maker map into their {where} "
                 f"({len(norm)} blocks, {ver}) (`{acct_id}`)")
    extra = f" ({dropped} blocks didn't fit and were left out)" if dropped else ""
    return (f"Loaded **{len(norm)} blocks** into your {where} on Yeeps {ver}{extra}. "
            "Walk into your private world (or rejoin it) to see it. If you were already inside it, "
            "leave first and run this again, or the game saves your old world over it.")


async def _discord_widget_reply(discord_id: str):
    """/discord: steps + Connect button for the Discord profile widget (answered at once)."""
    _ready = lambda: False   # profile widget is part of the website (not included)
    widget_snippet = lambda *a, **k: ""
    if not _ready():
        return _msg("The Discord profile widget isn't switched on yet. Check back soon!")
    linked = (await _discord_linked_accounts(discord_id)) if discord_id else []
    if not linked:
        print(f"[widget] /discord from unlinked Discord user {discord_id}")
    first = ("" if linked else "**First run `/link`** with your Yeeps username and in-game mobile code so we know your Yeeps account.\n\n")
    text = (first +
            "**Put your Yeeps stats on your Discord profile** (needs a computer):\n"
            "1. Press **Connect Discord** below and click **Authorize**.\n"
            "2. Open **discord.com/app** in a web browser (not the app), press **Ctrl+Shift+I** (Mac: Cmd+Option+I), "
            "open **Console**, paste the code below and press Enter (type `allow pasting` first if it asks).\n"
            "3. **User Settings > Edit Profile > Add widget** and pick the Yeeps-Tools widget.\n\n"
            "```js\n" + widget_snippet() + "\n```\n"
            "Only paste code from Yeeps-Tools. Never paste console code anyone else sends you.")
    return JSONResponse({"type": _CHANNEL_MESSAGE, "data": {
        "flags": _EPHEMERAL, "content": text[:1990],
        "components": [{"type": 1, "components": [
            {"type": 2, "style": 5, "label": "Connect Discord", "url": "https://your-domain.example/widget/connect"}]}]}})


def _room_label(room_key: str, name_by_key: dict) -> str:
    """Friendly room name for the /online list."""
    if not room_key:
        return "?"
    nm = name_by_key.get(room_key)
    if room_key.startswith("p_o_"):
        return (nm + " (private world)") if nm else "a private world"
    if room_key.startswith("c_"):
        return (nm + " (community world)") if nm else "a community world"
    return nm or room_key


# 2.31.4 (the Yeeps BIG "biglive" APKs, owner 2026-10-13: "/online shows the players
# playing 2.31.4", "no 2.4.0"). Those players are on the 2.4 server, whose data is the
# Yeeps240 database on this same cluster; only accounts whose APK build is a biglive
# build are listed, so 2.4.0 players there (240bN, bigN) never show.
LIVE_DB = os.getenv("ONLINE_2314_DB", "Yeeps240")
# live APK build tags: "biglive*" (old), "live*" (renamed 2026-10-17), "play*".
# The 2nd tag part is the game binary's hash -> which game version they run.
LIVE_BUILD_RE = r"^(biglive|live|play)"
LIVE_GAME_BY_HASH = {"2c82ef438a582938": "2.31.4", "2f1cef2dfe1a943c": "2.32"}


async def _online_2314() -> str:
    from services.db import client
    ldb = client[LIVE_DB]
    now = datetime.now(timezone.utc).strftime("%Y %m/%d %H:%M:%S")
    sts = [s_ async for s_ in ldb["player_statuses"].find(
        {"expireTimestamp": {"$gt": now}}, {"_id": 0, "accountID": 1, "roomKey": 1})]
    ids = [s_["accountID"] for s_ in sts if s_.get("accountID")]
    accts = {a["accountID"]: a async for a in ldb["accounts"].find(
        {"accountID": {"$in": ids}, "clientBuildTag": {"$regex": LIVE_BUILD_RE}},
        {"_id": 0, "accountID": 1, "displayName": 1, "clientBuildTag": 1, "photonShard": 1})}
    sts = [s_ for s_ in sts if s_.get("accountID") in accts]
    if not sts:
        return "Nobody's online on live right now."
    room_names = {r["roomKey"]: r.get("roomName") async for r in ldb["rooms"].find(
        {"roomKey": {"$in": [s_["roomKey"] for s_ in sts if s_.get("roomKey")]}},
        {"_id": 0, "roomKey": 1, "roomName": 1})}
    # owner 2026-10-31: server2 players get their own section under server1's
    by_server = {"1": [], "2": []}
    for s_ in sts:
        who = re.sub(r"<[^>]+>", "", accts[s_["accountID"]].get("displayName") or "").strip() or s_["accountID"]
        who = re.sub(r"([*_`~|\\])", r"\\\1", who)
        parts = (accts[s_["accountID"]].get("clientBuildTag") or "").split(".")
        # owner 2026-10-18: show the APK build (live65), not just the game version
        build = parts[0] if parts and parts[0] else LIVE_GAME_BY_HASH.get(parts[1] if len(parts) > 1 else "", "")
        _sh = "2" if accts[s_["accountID"]].get("photonShard") == "2" else "1"
        by_server[_sh].append(f"• **{who}** — {_room_label(s_.get('roomKey'), room_names)}"
                     + (f" ({build})" if build else ""))
    out = []
    for _sh in ("1", "2"):
        if by_server[_sh]:
            out.append(f"**{len(by_server[_sh])} online on live server {_sh}:**\n" + "\n".join(by_server[_sh]))
        elif _sh == "2":
            out.append("**0 online on live server 2.**")
    return "\n\n".join(out)


async def _do_online() -> str:
    """1.31.0 players online, plus the 2.31.4 players (not 2.4.0)."""
    main = await _online_131()
    try:
        extra = await _online_2314()
    except Exception as e:
        print(f"[discord] 2.31.4 online list failed: {e}")
        extra = ""
    out = main + (f"\n\n{extra}" if extra else "")
    if len(out) > 1990:          # Discord's 2000-char limit: drop room names, keep every player
        out = re.sub(r" — [^\n(]*", "", out)
    return out[:1990]


async def _online_131() -> str:
    """List players whose presence hasn't expired, with the room they're in."""
    now = datetime.now(timezone.utc)
    online = []
    async for s in player_statuses.find({}, {"_id": 0, "accountID": 1, "roomKey": 1, "expireTimestamp": 1}):
        exp = s.get("expireTimestamp") or ""
        try:
            if datetime.strptime(exp, "%Y %m/%d %H:%M:%S").replace(tzinfo=timezone.utc) <= now:
                continue   # presence expired = offline
        except Exception:
            continue       # unparseable timestamp -> skip
        online.append(s)
    if not online:
        return "Nobody's online on 1.31.0 right now."
    from config import REQUIRED_CLIENT_VERSION
    # resolve display names + APK build + room names in bulk.
    acct_ids = [s["accountID"] for s in online if s.get("accountID")]
    room_keys = [s["roomKey"] for s in online if s.get("roomKey")]
    accts = {a["accountID"]: a async for a in
             accounts.find({"accountID": {"$in": acct_ids}},
                           {"_id": 0, "accountID": 1, "displayName": 1,
                            "clientBuildTag": 1, "clientBuildStatus": 1})}
    room_names = {r["roomKey"]: r.get("roomName") async for r in
                  rooms.find({"roomKey": {"$in": room_keys}}, {"_id": 0, "roomKey": 1, "roomName": 1})}
    from services.build_gate import current_status
    from config import ALLOWED_CLIENT_BUILDS
    _latest = (f"{REQUIRED_CLIENT_VERSION} build {max(ALLOWED_CLIENT_BUILDS)}"
               if ALLOWED_CLIENT_BUILDS else REQUIRED_CLIENT_VERSION)
    lines = []
    outdated = []
    for s in online:
        acct = accts.get(s.get("accountID"), {})
        who = acct.get("displayName") or s.get("accountID") or "someone"
        # strip Unity rich-text tags (<b>, <color=#...>, ...) players put in names
        who = re.sub(r"<[^>]+>", "", who).strip() or (s.get("accountID") or "someone")
        # neutralize Discord markdown so names can't break formatting
        who = re.sub(r"([*_`~|\\])", r"\\\1", who)
        # APK build check (services/build_gate.py): the build tag the 1.31 APK
        # sends at login, classified against the CURRENT allowlist. (The old
        # 1.19 attestHash is stale on many accounts and no longer meaningful.)
        if current_status(acct) == "outdated":
            tag = f"  [OUTDATED - must install {_latest}]"
            outdated.append(who)
        else:
            tag = ""
        lines.append(f"• **{who}** — {_room_label(s.get('roomKey'), room_names)}{tag}")
    out = f"**{len(lines)} online on 1.31.0:**\n" + "\n".join(lines)
    if outdated:
        who_list = ", ".join(f"**{w}**" for w in outdated[:20])
        out += (f"\n\n{len(outdated)} on an old build — someone let them know to "
                f"install the latest ({_latest}): {who_list}")
    return out


@router.get("/discord/health")
async def health():
    """Diagnostic only: is the public key configured and does it look valid?
    Never returns the key itself."""
    k = (DISCORD_PUBLIC_KEY or "").strip()
    ok_len = len(k) == 64
    valid_hex = False
    try:
        bytes.fromhex(k); valid_hex = len(k) == 64
    except Exception:
        valid_hex = False
    return JSONResponse({
        "public_key_set": bool(k),
        "length": len(k),
        "looks_like_valid_key": ok_len and valid_hex,
        "crypto_available": Ed25519PublicKey is not None,
    })


@router.post("/discord/register-commands")
async def register_commands(request: Request):
    """Register/refresh the slash commands using THIS service's own env vars, so
    commands can be (re)registered without a laptop. Guarded by DATA_SECRET.
    Registers to a guild (instant) when DISCORD_GUILD_ID is set, else globally
    (can take ~1h). Only ever registers the fixed _SLASH_COMMANDS set."""
    secret = request.query_params.get("secret", "")
    if not DATA_SECRET or secret != DATA_SECRET:
        return JSONResponse({"ok": False, "error": "bad or missing secret"}, status_code=403)
    app_id = os.getenv("DISCORD_APP_ID", "").strip()
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    guild = os.getenv("DISCORD_GUILD_ID", "").strip()
    if not app_id or not token:
        # Report what's missing (never the values) so we know whether the token
        # lives on Render or still needs the local register script.
        return JSONResponse({"ok": False, "has_app_id": bool(app_id),
                             "has_bot_token": bool(token),
                             "error": "DISCORD_APP_ID and/or DISCORD_BOT_TOKEN not set on this service"},
                            status_code=400)
    if guild:
        url = f"https://discord.com/api/v10/applications/{app_id}/guilds/{guild}/commands"
        scope = f"guild {guild} (instant)"
    else:
        url = f"https://discord.com/api/v10/applications/{app_id}/commands"
        scope = "global (~1h to appear)"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.put(url, headers={"Authorization": f"Bot {token}"},
                                  json=_SLASH_COMMANDS)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"discord request failed ({e})"}, status_code=502)
    ok = r.status_code in (200, 201)
    registered = None
    try:
        registered = sorted(c.get("name", "?") for c in r.json())
    except Exception:
        pass
    return JSONResponse({"ok": ok, "status": r.status_code, "scope": scope,
                         "registered": registered,
                         "sent": [c["name"] for c in _SLASH_COMMANDS],
                         "body": None if registered else r.text[:400]},
                        status_code=200 if ok else 502)


@router.post("/discord/interactions")
async def interactions(request: Request, background: BackgroundTasks):
    body = await request.body()
    sig = request.headers.get("x-signature-ed25519", "")
    ts = request.headers.get("x-signature-timestamp", "")
    if not _verify(sig, ts, body):
        return PlainTextResponse("invalid request signature", status_code=401)

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    if payload.get("type") == _PING:
        return JSONResponse({"type": _PONG})

    if payload.get("type") == _APPLICATION_COMMAND:
        data = payload.get("data", {}) or {}
        name = data.get("name")
        token = payload.get("token", "")
        # The payload carries the application id, so deferral needs no extra env.
        app_id = str(payload.get("application_id") or DISCORD_APP_ID or "")
        opts = {o["name"]: o.get("value") for o in (data.get("options") or [])}
        # Who ran it: member.user in a guild, user in a DM.
        du = ((payload.get("member") or {}).get("user")) or payload.get("user") or {}
        discord_id = str(du.get("id") or "")
        discord_name = du.get("username") or du.get("global_name") or ""

        if name == "discord":
            return await _discord_widget_reply(discord_id)
        work = None
        if name == "link":
            work = (lambda: _do_link(str(opts.get("username", "")), str(opts.get("in-game-mobile-code") or opts.get("code") or ""),
                                     discord_id, discord_name),
                    "Something went wrong")
        elif name == "data":
            field = str(opts.get("field", ""))
            if field in ("pw", "pws"):
                att = ((data.get("resolved") or {}).get("attachments") or {}).get(str(opts.get("file") or "")) or {}
                work = (lambda: _do_pw_map(discord_id, discord_name, field, str(opts.get("value") or ""), att),
                        "Couldn't load your map")
            else:
                work = (lambda: _do_data(discord_id, discord_name, field, str(opts.get("value", ""))),
                        "Couldn't edit your data")
        elif name == "online":
            work = (_do_online, "Couldn't fetch online players")
        elif name == "editor":
            work = (lambda: _do_editor_code(discord_id, discord_name), "Couldn't make your editor code")
        elif name == "unlink":
            work = (lambda: _do_unlink(discord_id, discord_name), "Couldn't unlink your Discord")
        if work is None:
            return _msg("Unknown command.")

        coro_fn, fail_prefix = work
        # Preferred path: DEFER so a slow /data cold-start can't blow the 3s
        # window; the real answer is edited in once the background task finishes.
        if app_id and token:
            background.add_task(_run_and_edit, coro_fn, app_id, token, fail_prefix)
            return JSONResponse({"type": _DEFERRED, "data": {"flags": _EPHEMERAL}})
        # Fallback (no app id available): answer synchronously, old behaviour.
        try:
            text = await coro_fn()
        except Exception as e:
            text = f"{fail_prefix} ({e})"
        return _msg(text)

    return JSONResponse({"type": _PONG})
