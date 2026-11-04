"""
Discord slash-command bot via HTTP interactions (no always-on gateway process,
so it runs fine on the same Render web service).

Command:
  /link username:<meta name> code:<mobile code>
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
         {"name": "username", "description": "Your Meta / display name", "type": 3, "required": True},
         {"name": "code", "description": "Your in-game mobile code", "type": 3, "required": True},
     ]},
    {"name": "online", "description": "Show who's online and which map they're in", "type": 1},
    {"name": "data", "description": "Edit your Yeeps data: name / skin / eye colour (must /link first)", "type": 1,
     "options": [
         {"name": "field", "description": "What to change", "type": 3, "required": True,
          "choices": [
              {"name": "In-game username", "value": "username"},
              {"name": "Skin colour", "value": "skin"},
              {"name": "Eye colour", "value": "eye"},
          ]},
         {"name": "value", "description": "New value — a name, or a hex colour like #FF0000", "type": 3, "required": True},
     ]},
    {"name": "color", "description": "Convert a hex colour to the Yeeps colour value", "type": 1,
     "options": [
         {"name": "hex", "description": "A hex colour, e.g. #FF0000", "type": 3, "required": True},
     ]},
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


async def _do_link(username: str, code: str, discord_id: str = "", discord_name: str = "") -> str:
    username = (username or "").strip()
    code = (code or "").strip()
    if not username:
        return "Give me a username: `/link username:<your Meta name> code:<mobile code>`"
    if not re.fullmatch(r"\d{4}", code):
        return "Your mobile code must be the 4 digits from the game (Settings → Mobile)."

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
        return ("Login failed — double check your Meta username and the 4-digit "
                "mobile code (the phone in game). "
                f"({err or 'wrong username or code'})")
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
                "If it's yours, contact an admin.")

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
    for k, v in (await _effective_default()).items():
        if k not in _PROTECTED:
            doc[k] = v
    # Grant the in-game "verified" role: only Discord-verified users reach here
    # (the "None Verified" check above blocks everyone else), and that role is
    # what gates the 2.31.x-blaze APK at login. Merge so the default's roles
    # (and any the account already had) are kept.
    _roles = list(doc.get("roleKeys") or (existing or {}).get("roleKeys") or [])
    if "verified" not in _roles:
        _roles.append("verified")
    doc["roleKeys"] = _roles
    await accounts.update_one({"accountID": account_id}, {"$set": doc}, upsert=True)

    final_name = (existing or {}).get("displayName") or doc.get("displayName") or data.get("oculusID", username)
    await _audit(f"✅ **{discord_name}** (`{discord_id}`) "
                 f"{'re-linked' if existing else 'linked'} **{final_name}** (`{account_id}`)")
    return f"Linked **{data.get('oculusID', username)}** — launch Yeeps and your stuff will be there."


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
    """Packed Yeeps colour: signed int32 = (R<<24)|(G<<16)|(B<<8)|0xFF."""
    v = (r << 24) | (g << 16) | (b << 8) | 0xFF
    return v - 0x100000000 if v >= 0x80000000 else v


async def _do_color(hex_str: str) -> str:
    """Pure converter: hex colour -> the packed Yeeps colour value."""
    rgb = _parse_hex(hex_str)
    if rgb is None:
        return "Give me a hex colour like `/color hex:#FF0000` (3 or 6 hex digits)."
    r, g, b = rgb
    return (f"**#{r:02X}{g:02X}{b:02X}**  ·  R {r}, G {g}, B {b}\n"
            f"Yeeps colour value: **`{_hex_to_yeeps(r, g, b)}`**")


async def _do_data(discord_id: str, discord_name: str, field: str, value: str) -> str:
    """Edit the account linked to this Discord user: username / skin / eye colour.
    Requires the caller's Discord to already be linked (/link)."""
    if not discord_id:
        return "I couldn't read your Discord account — try again."
    acct = await accounts.find_one({"discordID": discord_id}, {"_id": 0, "accountID": 1})
    if not acct:
        return ("You have to relink! I don't have your Discord tied to a Yeeps account yet — "
                "run `/link username:<Meta name> code:<mobile code>` first.")
    value = (value or "").strip()
    if not value:
        return "Give me a value to set (a name, or a hex colour like `#FF0000`)."

    if field == "username":
        # No length limit on names (color-tagged names exceed any short cap).
        if any(ord(c) < 32 for c in value):
            return "That name has control characters I can't use — no newlines/tabs."
        from services.names import is_visible_name
        if not is_visible_name(value):
            return ("That name wouldn't show up in game (only spaces, invisible characters "
                    "or hidden text). Use at least one visible character.")
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
        await accounts.update_one(
            {"discordID": discord_id},
            {"$set": {"displayName": value, "displayName_searchable": value.lower(),
                      "discordUsername": discord_name}})
        await _audit(f"✏️ **{discord_name}** (`{discord_id}`) set their name to "
                     f"**{value}** (`{acct_id}`)")
        return f"Your in-game name is now **{value}** — relaunch Yeeps to see it."

    if field in ("skin", "eye"):
        rgb = _parse_hex(value)
        if rgb is None:
            return "Give me a hex colour like `#FF0000` for the colour."
        r, g, b = rgb
        col = _hex_to_yeeps(r, g, b)
        key = "skinColor" if field == "skin" else "eyeColor"
        acct_id = acct.get("accountID")
        await accounts.update_one(
            {"discordID": discord_id},
            {"$set": {key: col, "discordUsername": discord_name}})
        label = "skin" if field == "skin" else "eye"
        await _audit(f"✏️ **{discord_name}** (`{discord_id}`) set their {label} colour to "
                     f"**#{r:02X}{g:02X}{b:02X}** (`{col}`) (`{acct_id}`)")
        return (f"Your {label} colour is now **#{r:02X}{g:02X}{b:02X}** "
                f"(`{col}`) — relaunch Yeeps to see it.")

    return "Pick what to edit: `username`, `skin`, or `eye`."


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


async def _do_online() -> str:
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
        return "Nobody's online right now."
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
    out = f"**{len(lines)} online:**\n" + "\n".join(lines[:50])
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

        work = None
        if name == "link":
            work = (lambda: _do_link(str(opts.get("username", "")), str(opts.get("code", "")),
                                     discord_id, discord_name),
                    "Something went wrong")
        elif name == "data":
            work = (lambda: _do_data(discord_id, discord_name,
                                     str(opts.get("field", "")), str(opts.get("value", ""))),
                    "Couldn't edit your data")
        elif name == "color":
            work = (lambda: _do_color(str(opts.get("hex", ""))),
                    "Couldn't convert that colour")
        elif name == "online":
            work = (_do_online, "Couldn't fetch online players")
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
