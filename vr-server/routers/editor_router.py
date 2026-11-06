"""
Player data editor — runs on port 8001 (http://localhost:8001)
Provides a web UI + JSON API for browsing and editing player accounts.
"""

import json
import uvicorn
import httpx
from fastapi import APIRouter, FastAPI, Request, HTTPException, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse

from services.db import accounts, rooms, debug_logs, globals_col, db as _db
from config import YEEPS_PROXY_URL, EDITOR_PW_WEBHOOK, EDITOR_PW_ROTATE_SECONDS

import os
import re
import hmac
from datetime import datetime, timedelta, timezone
import hashlib
import time
from datetime import datetime, timezone
from fastapi import Depends
from fastapi.responses import RedirectResponse

# ─── Auth: password + Meta-name gate, with Discord audit webhook ──────────────
# Config via env (set these on Render): EDITOR_PASSWORD, EDITOR_SECRET (cookie
# signing), EDITOR_DISCORD_WEBHOOK (who's-using-it notifications).
# Password changed after a raid where the old EDITOR_PASSWORD leaked.
# New env key so the old leaked Render env var no longer applies; defaults here.
# The editor password auto-rotates every EDITOR_PW_ROTATE_SECONDS. The current
# code is derived from EDITOR_SECRET + the time window (so it needs no storage
# and survives restarts), and is posted to the password webhook on each change.
# An optional MASTER password (set EDITOR_PW on Render) is accepted as a backup;
# there is no hardcoded fallback password anymore.
_MASTER_PW = os.getenv("EDITOR_PW", "")


def _pw_window() -> int:
    return int(time.time() // EDITOR_PW_ROTATE_SECONDS)


# EDITOR_PW_SALT changes every code without touching EDITOR_SECRET (which also
# signs sessions, device cookies and the owner password hash). Set on
# 2026-10-10 after the rotating code leaked to a raider.
_PW_SALT = os.getenv("EDITOR_PW_SALT", "")
# EDITOR_PW_NOTIFY=0: don't post new codes to the password webhook.
_PW_NOTIFY = os.getenv("EDITOR_PW_NOTIFY", "1") != "0"


def _editor_password(window: int = None) -> str:
    w = _pw_window() if window is None else window
    msg = f"editorpw|{w}" + (f"|{_PW_SALT}" if _PW_SALT else "")
    mac = hmac.new(EDITOR_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{int(mac[:8], 16) % 1000000:06d}"

# Admin allowlist: ONLY these accountIDs may open the editor, even with the right
# password. Empty set = no restriction. Override via env EDITOR_ADMINS (comma-sep).
EDITOR_ADMINS = set(filter(None, (os.getenv("EDITOR_ADMINS") or
    "o_OWNER0000000001").replace(" ", "").split(",")))
# Only these editor usernames may SEE player IPs (lastIP/recentIPs and the IP-ban
# list). Player IPs are sensitive, so for everyone else they're stripped/masked.
# Override/extend via env IP_VIEW_NAMES (comma-separated display names).
IP_VIEW_NAMES = set(filter(None, (n.strip() for n in
    (os.getenv("IP_VIEW_NAMES") or "OwnerName,o_OWNER0000000001").split(","))))
EDITOR_SECRET          = os.getenv("EDITOR_SECRET", "yeeps-editor-secret-change-me")
# Discord audit webhooks. Editor login + player/data edits go to the ADMIN
# channel; in-game activity goes to the INGAME channel. Both live in config so
# they can be overridden by env and shared with the other routers.
from config import ADMIN_DISCORD_WEBHOOK, INGAME_DISCORD_WEBHOOK
# Back-compat: an explicit EDITOR_DISCORD_WEBHOOK env still wins if set.
EDITOR_DISCORD_WEBHOOK = os.getenv("EDITOR_DISCORD_WEBHOOK", ADMIN_DISCORD_WEBHOOK)
_COOKIE = "yeeps_editor"
# How long a signed-in browser stays logged in before it must re-enter the
# password. Shorter = a smaller window where an open/shared/stolen browser can
# be used. Env EDITOR_SESSION_TTL_HOURS (default 12).
_TTL = int(float(os.getenv("EDITOR_SESSION_TTL_HOURS", "12")) * 3600)

# Session cutoff: any editor cookie (session OR 1-year owner-device) issued before
# this unix time is rejected, so bumping it logs EVERYONE out at once. Set env
# EDITOR_SESSION_CUTOFF to `date +%s` to invalidate all existing sessions.
_SESSION_CUTOFF = int(os.getenv("EDITOR_SESSION_CUTOFF", "0") or "0")


def _ip_net(ip: str) -> str:
    from services.device_auth import _net
    return _net(ip or "")


def _sign(name: str, ip: str = "") -> str:
    # Bind the session to the current password window (rotation boots everyone).
    # Not locked to a network (removed 2026-10-09 at the owner's request, so the
    # editor works on mobile data / other Wi-Fi). The login network is signed
    # into the cookie only so a session used from somewhere else gets reported.
    w = _pw_window()
    exp = str(int(time.time()) + _TTL)
    net = _ip_net(ip)
    mac = hmac.new(EDITOR_SECRET.encode(), f"{name}|{w}|{exp}|{net}".encode(), hashlib.sha256).hexdigest()
    return f"{name}|{w}|{exp}|{net}|{mac}"


# ---- editor IP log + "different network" alerts --------------------------
# Every sign-in attempt is stored in "editor_logins" with its IP and rough
# location. The admin webhook is told when someone signs in from a network the
# account doesn't play Yeeps from, or when a session is used from a different
# network than it signed in on.
editor_logins = _db["editor_logins"]
_session_nets: dict = {}      # cookie mac -> networks already seen/reported
_bg_tasks: set = set()


# Every change made in the editor posts a line to the admin webhook (owner
# 2026-10-27). Handlers that already post their own detailed message mark the
# request through this, and _guard posts a short catch-all line for the rest.
import contextvars as _cv
_notified_cv: _cv.ContextVar = _cv.ContextVar("editor_notified", default=None)
_ACTION_NAMES = {
    "photon/cmd": "sent the Photon bot a command",
    "photon/record/start": "started a Photon bot recording",
    "photon/record/stop": "stopped the Photon bot recording",
}


def _action_line(request: Request, name: str, detail: str) -> str:
    from services.ip_bans import client_ip
    path = request.url.path
    i = path.find("/api/")
    what = path[i + 5:].strip("/") if i >= 0 else path
    label = _ACTION_NAMES.get(what) or f"used the editor: `{request.method} {what}`"
    return (f"**{name}** {label}" + (f" ({detail})" if detail else "")
            + f" [{client_ip(request) or '?'}]")


def _audit_summary(body) -> str:
    """One short, safe field value from a request body for the action webhook."""
    if not isinstance(body, dict):
        return ""
    for k in ("text", "message", "cmd", "command", "reason", "role", "roleKey",
              "build", "requireBuild", "record", "name", "username"):
        v = body.get(k)
        if v not in (None, "", []):
            return f"{k}={str(v)[:140]}"
    return ""


def _ip_info(ip: str) -> dict:
    from services import geo
    return {"ip": ip, "net": _ip_net(ip), "place": geo.describe(ip), "vpn": geo.vpn_hint(ip)}


def _where(info: dict) -> str:
    return info["place"] + (f", {info['vpn']} (VPN)" if info.get("vpn") else "")


async def _play_ips(acct_id) -> set:
    """IPs this account has played Yeeps from (EDITOR_ADMINS pool theirs: they're
    the same person's accounts)."""
    ids = list(EDITOR_ADMINS) if acct_id in EDITOR_ADMINS else [acct_id]
    known = set()
    async for a in accounts.find({"accountID": {"$in": [x for x in ids if x]}},
                                 {"_id": 0, "lastIP": 1, "recentIPs": 1}):
        known.update(filter(None, [a.get("lastIP")] + list(a.get("recentIPs") or [])))
    return known


def _km_from_play(ip: str, known: set):
    try:
        from services import geo
        km = geo.nearest_km(ip, known)
        return None if km is None else int(round(km))
    except Exception:
        return None


def _typed_view(pw: str) -> str:
    """What a wrong editor-password attempt typed, for the owner's IP-ban list (2026-10-18).
    Number codes are kept as typed (tagged when it was the previous code); anything else
    is NOT stored - people sometimes type a real password from another site here."""
    p = (pw or "").strip()
    if not p:
        return "(empty)"
    if p.isdigit() and len(p) <= 8:
        try:
            if hmac.compare_digest(p, _editor_password(_pw_window() - 1)):
                return p + " (last code)"
        except Exception:
            pass
        return p
    return f"(text, {len(p)} chars)"


async def _log_editor_ip(kind: str, name: str, acct_id, info: dict, **extra) -> None:
    try:
        await editor_logins.insert_one({
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "kind": kind, "name": name, "accountID": acct_id, **info, **extra})
    except Exception as e:
        print(f"[editor] ip log failed: {e}")


def _session_hash(token: str) -> str:
    """Stable id of a session (its cookie MAC, hashed) for editor_logins lookups."""
    return hashlib.sha256((token or "").rsplit("|", 1)[-1].encode()).hexdigest()[:32]


async def _session_account(request: Request):
    """accountID this editor session signed in as (from its login record), or None.
    Display names aren't unique, so owner-only features check this, not the name."""
    if not _authed_name(request):
        return None
    tok = request.cookies.get(_COOKIE, "")
    doc = await editor_logins.find_one({"kind": "login", "sessionHash": _session_hash(tok)},
                                       {"_id": 0, "accountID": 1})
    if not doc:
        # Sessions signed in before login records kept a sessionHash: match the
        # cookie's own signed fields (name, login network, login time = exp - TTL)
        # to that sign-in's record instead.
        parts = tok.rsplit("|", 4)
        if len(parts) == 5:
            name, _w, exp, net, _mac = parts
            t0 = int(exp) - _TTL
            fmt = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            async for d in editor_logins.find(
                    {"kind": "login", "net": net, "sessionHash": {"$exists": False},
                     "at": {"$gte": fmt(t0 - 5), "$lte": fmt(t0 + 5)}},
                    {"_id": 0, "accountID": 1, "name": 1}):
                if str(d.get("name", "")).lower() == name.lower():
                    doc = d
                    break
    return (doc or {}).get("accountID")


async def _report_session_move(name: str, login_net: str, ip: str) -> None:
    info = _ip_info(ip)
    doc = await _name_exists(name)
    acct_id = doc.get("accountID") if doc else None
    known = await _play_ips(acct_id)
    plays_here = ip in known or info["net"] in {_ip_net(x) for x in known}
    await _log_editor_ip("session-moved", name, acct_id, info, loginNet=login_net, playsHere=plays_here)
    km = _km_from_play(ip, known)
    far = f", ~{km} km from where they play" if km is not None and not plays_here else ""
    await _discord_notify(
        f"EDITOR ALERT: **{name}**'s editor session is now being used from a different network "
        f"[{ip}] ({_where(info)}{far}). It signed in from {login_net}."
        + ("" if plays_here else " That account doesn't play Yeeps from this network."))


def _check_session_net(request: Request, name: str, mac: str, login_net: str) -> None:
    """Report (once per session per network) a session used away from its login network."""
    try:
        from services.ip_bans import client_ip
        ip = client_ip(request) or ""
        net = _ip_net(ip)
        if not net or net == login_net:
            return
        seen = _session_nets.setdefault(mac, set())
        if net in seen:
            return
        seen.add(net)
        if len(_session_nets) > 500:
            _session_nets.pop(next(iter(_session_nets)))
        import asyncio
        t = asyncio.get_running_loop().create_task(_report_session_move(name, login_net, ip))
        _bg_tasks.add(t)
        t.add_done_callback(_bg_tasks.discard)
    except Exception:
        pass


# KILL SWITCH: when True the whole editor is locked -- every session is treated
# as logged out, every API call is unauthorized, and login is refused. Flip to
# False (and redeploy) to bring the editor back. Now protected by EDITOR_ADMINS
# allowlist + rotated password, so it's safe to leave unlocked.
EDITOR_LOCKED = False


def _authed_name(request: Request):
    """Return the Meta name from a valid signed cookie, else None."""
    if EDITOR_LOCKED:
        return None
    tok = request.cookies.get(_COOKIE, "")
    try:
        # name|window|exp|loginNet|mac (sessions signed before the IP log
        # existed have no loginNet and are accepted without move reports).
        parts = tok.rsplit("|", 4)
        if len(parts) == 5:
            name, w, exp, net, mac = parts
            good = hmac.new(EDITOR_SECRET.encode(), f"{name}|{w}|{exp}|{net}".encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(good, mac):
                name, w, exp, mac = tok.rsplit("|", 3)
                net = None
        else:
            name, w, exp, mac = tok.rsplit("|", 3)
            net = None
        if net is None:
            good = hmac.new(EDITOR_SECRET.encode(), f"{name}|{w}|{exp}".encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(good, mac):
                return None
        if int(exp) < time.time():
            return None
        if int(exp) - _TTL < _SESSION_CUTOFF:
            return None  # issued before the cutoff -> booted (log-everyone-out)
        if int(w) != _pw_window():
            return None  # password rotated since login -> booted, must re-sign-in
        if net is not None:
            _check_session_net(request, name, mac, net)
        return name
    except Exception:
        return None


def _can_see_ips(request: Request) -> bool:
    """True only for editor users allowed to view player IPs (IP_VIEW_NAMES)."""
    return (_authed_name(request) or "") in IP_VIEW_NAMES


async def _name_exists(name: str):
    """Look the Meta name up in the accounts collection (displayName / oculusID /
    accountID, case-insensitive). Returns the matched doc or None."""
    n = (name or "").strip()
    if not n:
        return None
    esc = re.escape(n)
    return await accounts.find_one(
        {"$or": [
            {"displayName": {"$regex": f"^{esc}$", "$options": "i"}},
            {"oculusID": {"$regex": f"^{esc}$", "$options": "i"}},
            {"accountID": n},
        ]},
        {"_id": 0, "accountID": 1, "displayName": 1, "oculusID": 1, "discordID": 1},
    )


async def _name_matches(name: str) -> list:
    """Accounts a typed editor name can mean: in-game name, Meta name, Discord
    username (case-insensitive) or account id. Up to 10."""
    n = (name or "").strip()
    if not n:
        return []
    esc = re.escape(n)
    rx = {"$regex": f"^{esc}$", "$options": "i"}
    cur = accounts.find({"$or": [{"displayName": rx}, {"oculusID": rx}, {"discordUsername": rx}, {"accountID": n}]},
                        {"_id": 0, "accountID": 1, "displayName": 1, "oculusID": 1, "discordID": 1, "deviceKeys": 1})
    return [d async for d in cur.limit(10)]


async def _post_webhook(url: str, text: str) -> None:
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            from config import labeled
            await c.post(url, json={"content": labeled(text)[:1900]})
    except Exception as e:
        print(f"[editor] discord webhook failed: {e}")


async def _discord_notify(text: str) -> None:
    """ADMIN channel: editor login + player/data edits (+ /link via discord_router)."""
    _m = _notified_cv.get()
    if _m is not None:
        _m.append(1)
    await _post_webhook(EDITOR_DISCORD_WEBHOOK, text)


async def _ingame_notify(text: str) -> None:
    """INGAME channel: things players do in-game (e.g. in-game map saves)."""
    await _post_webhook(INGAME_DISCORD_WEBHOOK, text)


async def _maybe_notify_new_password() -> None:
    """When the password window rolls over, post the new code to the password
    webhook exactly once. Called from a background loop and lazily on page load,
    so it fires even on Render's free tier (which sleeps when idle)."""
    if not EDITOR_PW_WEBHOOK or not _PW_NOTIFY:
        return
    w = _pw_window()
    try:
        from pymongo import ReturnDocument
        prev = await globals_col.find_one_and_update(
            {"key": "editorPwWindow"}, {"$set": {"value": w}},
            upsert=True, return_document=ReturnDocument.BEFORE)
        if (prev or {}).get("value") == w:
            return  # already announced this window
        await _post_webhook(EDITOR_PW_WEBHOOK, f"New editor password: `{_editor_password(w)}`")
        print(f"[editor] rotated password, window={w}")
    except Exception as e:
        print(f"[editor] password rotate-notify failed: {e}")


def _short_val(v) -> str:
    """Compact one-line string for a field value in an audit log."""
    try:
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    except Exception:
        s = str(v)
    s = str(s).replace("\n", " ")
    return s if len(s) <= 60 else s[:57] + "…"


def _summarize_changes(before: dict, patch: dict) -> str:
    """Readable 'field: old → new' lines for exactly what an edit changed."""
    lines = []
    for k, new in patch.items():
        old = before.get(k, "∅")
        o, n = _short_val(old), _short_val(new)
        if o == n:
            continue
        lines.append(f"• `{k}`: {o} → {n}")
    return "\n".join(lines) if lines else "• (no value changes)"


# ---- owner lock (2026-10-10, owner: "make sure it's me only") --------------
# A normal sign-in is a name + the shared rotating code, so anyone who sees the
# code could sign in AS the owner (it happened: VPN/Oslo sessions under
# OwnerName). The owner's accounts (EDITOR_ADMINS) instead need the owner's
# own password (OWNER_EDITOR_PW_HASH = sha256(EDITOR_SECRET|owner-pw|password))
# AND a trusted place: a network the owner's headset plays from, or a browser
# that has signed in from there before (long-lived signed device cookie).
OWNER_EDITOR_PW_HASH = os.getenv("OWNER_EDITOR_PW_HASH", "")
_OWNER_DEVICE_COOKIE = "yeeps_owner_device"
_OWNER_DEVICE_TTL = 365 * 24 * 3600


def _owner_pw_ok(pw: str) -> bool:
    if not OWNER_EDITOR_PW_HASH:
        return False
    h = hashlib.sha256(f"{EDITOR_SECRET}|owner-pw|{pw}".encode()).hexdigest()
    return hmac.compare_digest(h, OWNER_EDITOR_PW_HASH)


def _owner_device_token(acct: str) -> str:
    t = str(int(time.time()))
    mac = hmac.new(EDITOR_SECRET.encode(), f"owner-device|{acct}|{t}".encode(), hashlib.sha256).hexdigest()
    return f"{acct}|{t}|{mac}"


def _owner_device_ok(request: Request) -> bool:
    try:
        acct, t, mac = request.cookies.get(_OWNER_DEVICE_COOKIE, "").split("|")
        good = hmac.new(EDITOR_SECRET.encode(), f"owner-device|{acct}|{t}".encode(), hashlib.sha256).hexdigest()
        return (hmac.compare_digest(good, mac) and acct in EDITOR_ADMINS
                and int(t) >= _SESSION_CUTOFF
                and time.time() - int(t) < _OWNER_DEVICE_TTL)
    except Exception:
        return False


async def _on_owner_network(request: Request, acct) -> bool:
    from services.ip_bans import client_ip
    ip = client_ip(request)
    known = await _play_ips(acct)
    return bool(ip) and (ip in known or _ip_net(ip) in {_ip_net(x) for x in known})


# Owner names: "must sign in from home or a trusted device" rule. Off by default
# (owner, 2026-10-11) - the owner password is still required. EDITOR_OWNER_DEVICE_RULE=1 turns it on.
EDITOR_OWNER_DEVICE_RULE = os.getenv("EDITOR_OWNER_DEVICE_RULE", "0") == "1"


async def _owner_trusted(request: Request, acct) -> bool:
    if not EDITOR_OWNER_DEVICE_RULE:
        return True
    return _owner_device_ok(request) or await _on_owner_network(request, acct)


def _device_ok_for(request: Request, acct) -> bool:
    """This browser holds a trusted-device cookie for acct (the owner's accounts
    share one)."""
    try:
        cacct, t, mac = request.cookies.get(_OWNER_DEVICE_COOKIE, "").split("|")
        good = hmac.new(EDITOR_SECRET.encode(), f"owner-device|{cacct}|{t}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, mac) or time.time() - int(t) >= _OWNER_DEVICE_TTL:
            return False
        return cacct == acct or (cacct in EDITOR_ADMINS and acct in EDITOR_ADMINS)
    except Exception:
        return False


async def _trusted_for(request: Request, acct) -> bool:
    """Every editor account (2026-10-11): used from the network its headset plays
    from, or from a device that signed in there before."""
    if not acct:
        return False
    if acct in EDITOR_ADMINS:
        return await _owner_trusted(request, acct)
    return True     # home-network/trusted-device rule removed (owner 2026-10-18)


async def _owner_account_for(name: str):
    """The owner (EDITOR_ADMINS) account a typed editor name refers to, or None."""
    n = (name or "").strip()
    if not n or not EDITOR_ADMINS:
        return None
    if n in EDITOR_ADMINS:
        return n
    esc = re.escape(n)
    doc = await accounts.find_one(
        {"accountID": {"$in": list(EDITOR_ADMINS)},
         "$or": [{"displayName": {"$regex": f"^{esc}$", "$options": "i"}},
                 {"oculusID": {"$regex": f"^{esc}$", "$options": "i"}},
                 {"discordUsername": {"$regex": f"^{esc}$", "$options": "i"}}]},
        {"_id": 0, "accountID": 1})
    return (doc or {}).get("accountID")


# Emergency switch (2026-10-10): a raider had the rotating code and signed in
# as Data Managers (NatorGator, StaffMemberA) to post an announcement and play an
# inappropriate video. While True, only the owner's accounts can use the editor.
EDITOR_OWNER_ONLY = os.getenv("EDITOR_OWNER_ONLY", "1") == "1"


# VPN/relay block for the editor. Off by default (owner, 2026-10-11); set
# EDITOR_BLOCK_VPN=1 to turn it back on. The trusted-network rule still applies.
EDITOR_BLOCK_VPN = os.getenv("EDITOR_BLOCK_VPN", "0") == "1"


def _from_vpn(request: Request) -> bool:
    if not EDITOR_BLOCK_VPN:
        return False
    from services.ip_bans import client_ip
    from services import geo
    return bool(geo.vpn_hint(client_ip(request) or ""))


# ---- editor tiers (owner, 2026-10-11) ---------------------------------------
# owner  = EDITOR_ADMINS accounts: everything.
# admin  = Discord role "hi lol": every tab (not the owner-only bits).
# editor = Data Manager role only: Players (view + edit everyday fields),
#          Online (view + kick one player) and Announce.
ADMIN_ROLE_NAME = os.getenv("EDITOR_ADMIN_ROLE_NAME", "hi lol")
# Discord role named "." (owner, 2026-10-11): counts as admin in the editor AND as
# sign-in access, so people with it can sign in without the Data Manager role.
EDITOR_EXTRA_ROLE = os.getenv("EDITOR_EXTRA_ROLE", ".")
# Discord IDs that get the admin tier in the editor as if they had the "hi lol" role
# (owner, 2026-10-11: staffalias). They still need the Data Manager role to sign in.
EDITOR_ADMIN_DISCORD_IDS = set(filter(None, (os.getenv("EDITOR_ADMIN_DISCORD_IDS") or
    "000000000000000000").replace(" ", "").split(",")))
_EDITOR_TIER_ALLOWED = [
    ("GET", r"/api/me"), ("GET", r"/api/players"), ("GET", r"/api/player/[^/]+"),
    ("POST", r"/api/player/[^/]+"), ("POST", r"/api/player/[^/]+/kick"),
    ("GET", r"/api/online"), ("GET", r"/api/announce"), ("POST", r"/api/announce"),
    ("DELETE", r"/api/announce"),
]
# fields a data editor may not change on a player
_EDITOR_TIER_LOCKED_FIELDS = {"discordUsername", "lastIP", "recentIPs"}   # owner 2026-10-11: data editors edit
# every other player field; the Discord username and IP fields stay owner-only


# Owner 2026-10-12: data editors use the full site again, like admins. The owner-only
# parts stay owner-only (Photon bot tab, giving/taking the blaze role, seeing player
# IPs). DATA_EDITOR_FULL=0 brings back the limited data-editor tier.
DATA_EDITOR_FULL = os.getenv("DATA_EDITOR_FULL", "1") == "1"


async def _session_tier(request: Request) -> str:
    acct = await _session_account(request)
    if acct in EDITOR_ADMINS:
        return "owner"
    tok = request.cookies.get(_COOKIE, "")
    doc = await editor_logins.find_one({"kind": "login", "sessionHash": _session_hash(tok)},
                                       {"_id": 0, "tier": 1})
    tier = (doc or {}).get("tier") or "editor"
    return "admin" if tier == "editor" and DATA_EDITOR_FULL else tier


async def _is_editor_admin(request: Request) -> bool:
    """True only for a real editor admin (EDITOR_ADMINS account). Destructive
    player actions (delete/block/ban/unban/device-reset) are gated on this so a
    data editor can't lock players out — matching the live editor (2026-10-21)."""
    acct = await _session_account(request)
    return bool(acct and acct in EDITOR_ADMINS)


async def _require_editor_admin(request: Request):
    if not await _is_editor_admin(request):
        raise HTTPException(status_code=403, detail="This action is for editor admins only.")


def _editor_tier_allows(method: str, path: str) -> bool:
    i = path.find("/api/")
    sub = path[i:] if i >= 0 else path
    return any(method == m and re.fullmatch(rx, sub) for m, rx in _EDITOR_TIER_ALLOWED)


async def _usable_session(request: Request) -> bool:
    """Signed in, and (for the owner's name) on a trusted network/device - the
    same rule _guard applies to every API call."""
    name = _authed_name(request)
    if not name or _from_vpn(request):
        return False
    acct = await _session_account(request) or await _owner_account_for(name)
    if EDITOR_OWNER_ONLY and acct not in EDITOR_ADMINS:
        return False
    return await _trusted_for(request, acct)


# Pages allowed to send editor changes (where the editor itself is served).
_EDITOR_ORIGINS = {"https://api.your-domain.example", "https://your-domain.example", "https://game.your-domain.example",
                   "http://127.0.0.1:8000", "http://localhost:8000"}


async def _guard(request: Request):
    """Protect the JSON API; the UI page + /login + /logout stay open (the UI
    page itself shows the login form when unauthenticated)."""
    if "/api/" not in request.url.path:
        yield
        return
    # Other sites under your-domain.example (friend.your-domain.example, ...) count as the SAME site to
    # browsers, so the editor cookie would ride along on requests they make: refuse any
    # change that comes from a page that is not the editor itself.
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        _origin = (request.headers.get("origin") or "").rstrip("/")
        if _origin and _origin not in _EDITOR_ORIGINS:
            print(f"[editor] refused cross-site {request.method} {request.url.path} from origin {_origin}")
            raise HTTPException(status_code=403, detail="Cross-site request refused")
    name = _authed_name(request)
    if not name:
        raise HTTPException(status_code=401, detail="unauthorized")
    if _from_vpn(request):
        raise HTTPException(status_code=401, detail="Turn off your VPN to use the editor")
    acct = await _session_account(request) or await _owner_account_for(name)
    if EDITOR_OWNER_ONLY and acct not in EDITOR_ADMINS:
        raise HTTPException(status_code=401, detail="The editor is owner-only right now")
    if not await _trusted_for(request, acct):
        raise HTTPException(status_code=401, detail="Sign in from your home network (where you play Yeeps) "
                                                    "or a device you trusted there")
    if (await _session_tier(request)) == "editor" and not _editor_tier_allows(request.method, request.url.path):
        raise HTTPException(status_code=403, detail="Admins only")
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        yield
        return
    detail = ""
    try:
        cl = int(request.headers.get("content-length") or 0)
        if 0 < cl <= 4096 and "json" in (request.headers.get("content-type") or "").lower():
            detail = _audit_summary(await request.json())
    except Exception:
        detail = ""
    mark: list = []
    _notified_cv.set(mark)
    yield
    if not mark:          # the handler didn't post its own message
        await _post_webhook(EDITOR_DISCORD_WEBHOOK, _action_line(request, name, detail))


router = APIRouter(dependencies=[Depends(_guard)])
editor_app = FastAPI(title="Yeeps Player Editor")

# ─── JSON API ────────────────────────────────────────────────────────────────

@router.get("/api/players")
async def list_players(q: str = "", limit: int = 50):
    """List/search player accounts."""
    filt = {}
    if q:
        rx = {"$regex": re.escape(q.strip().lstrip("@")), "$options": "i"}
        filt = {"$or": [
            {"accountID": rx},
            {"displayName": rx},
            {"discordUsername": rx},     # set by /link and /data
            {"discordID": rx},
            {"oculusID": rx},
        ]}
    cursor = accounts.find(filt, {"_id": 0, "accountID": 1, "displayName": 1, "currency": 1, "beans": 1,
                                  "discordUsername": 1}).limit(limit)
    docs = await cursor.to_list(length=limit)
    return JSONResponse(content=docs)


@router.get("/api/player/{account_id}")
async def get_player(account_id: str, request: Request):
    """Return full account document (player IPs hidden unless IP_VIEW_NAMES)."""
    doc = await accounts.find_one({"accountID": account_id}, {"_id": 0})
    if doc is None:
        raise HTTPException(status_code=404, detail="Account not found")
    if not _can_see_ips(request):
        doc.pop("lastIP", None)
        doc.pop("recentIPs", None)
    return JSONResponse(content=doc)


@router.post("/api/player/{account_id}")
async def update_player(account_id: str, request: Request):
    """Patch specific fields on an account."""
    try:
        patch = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Never allow overwriting the primary key
    patch.pop("accountID", None)
    patch.pop("_id", None)

    if not patch:
        raise HTTPException(status_code=400, detail="No fields to update")

    # Require a non-empty display name: don't let an edit clear it (keeps every
    # player identifiable in the logs and in-game instead of a bare account id).
    if "displayName" in patch and not str(patch.get("displayName") or "").strip():
        raise HTTPException(status_code=400, detail="Display name can't be empty")
    if "displayName" in patch:
        from services.names import is_visible_name
        if not is_visible_name(patch.get("displayName")):
            raise HTTPException(status_code=400, detail="Display name must have at least one visible character")
        from services.names import name_owner
        _other = await name_owner(patch.get("displayName"), account_id)
        if _other:
            raise HTTPException(status_code=409, detail=f"That name is already used by another account ({_other}). Names must be unique.")

    # Read the current values of exactly the fields being changed, so the audit
    # log can show the player's name and what actually changed (old → new).
    proj = {"_id": 0, "displayName": 1}
    for k in patch:
        proj[k] = 1
    before = await accounts.find_one({"accountID": account_id}, proj)
    if before is None:
        raise HTTPException(status_code=404, detail="Account not found")

    if (await _session_tier(request)) == "editor":
        # The players tab saves the WHOLE document, so a locked field only counts
        # when its value really changes; unchanged locked fields are dropped (2026-10-11:
        # every save of a linked or device-bound player was refused before this).
        _changed = []
        for _k in sorted(set(patch) & _EDITOR_TIER_LOCKED_FIELDS):
            if before.get(_k) != patch[_k]:
                _changed.append(_k)
            else:
                del patch[_k]
        if _changed:
            raise HTTPException(status_code=403, detail="Admins only: " + ", ".join(_changed))
        if not patch:
            return JSONResponse(content={"ok": True, "modified": 0})

    # Only the owner's account can give or take the blaze role (owner's rule,
    # 2026-10-11); the server's blaze list follows the owner's edits.
    _blaze_change = None
    if "roleKeys" in patch:
        from services import role_locks as _rl
        _had = _rl.BLAZE_ROLE in (before.get("roleKeys") or [])
        _has = _rl.BLAZE_ROLE in (patch.get("roleKeys") or [])
        if _had != _has:
            if (await _session_account(request)) not in EDITOR_ADMINS:
                raise HTTPException(status_code=403, detail="Only the owner can give or take the blaze role")
            _blaze_change = _has

    result = await accounts.update_one({"accountID": account_id}, {"$set": patch})
    if _blaze_change is not None:
        from services import role_locks as _rl
        await _rl.set_blaze_holder(account_id, _blaze_change)
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Account not found")

    name = _authed_name(request) or "?"
    disp = before.get("displayName") or "(no name)"
    changes = _summarize_changes(before, patch)
    await _discord_notify(f"**{name}** edited **{disp}** (`{account_id}`)\n{changes}")
    return JSONResponse(content={"ok": True, "modified": result.modified_count})


@router.delete("/api/player/{account_id}")
async def delete_player(account_id: str, request: Request):
    await _require_editor_admin(request)   # admins only (parity with live)
    """Permanently delete a player account. (The client re-creates a fresh one
    from the default template if that player logs in again.)"""
    doc = await accounts.find_one({"accountID": account_id}, {"_id": 0})
    if doc is None:
        raise HTTPException(status_code=404, detail="Account not found")
    # Safety: keep a full copy so an accidental/malicious delete is recoverable.
    try:
        from services.db import db as _db
        await _db["deleted_backups"].insert_one({
            "kind": "account", "accountID": account_id,
            "deletedBy": _authed_name(request) or "?",
            "deletedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "doc": doc})
    except Exception as e:
        print(f"[editor] delete backup failed for {account_id}: {e}")
    result = await accounts.delete_one({"accountID": account_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Account not found")
    # Also clear any player status row for them.
    try:
        from services.db import player_statuses
        await player_statuses.delete_one({"accountID": account_id})
    except Exception:
        pass
    name = _authed_name(request) or "?"
    dn = (doc or {}).get("displayName") or account_id
    await _discord_notify(f"{name} DELETED player {dn} ({account_id})")
    return JSONResponse(content={"ok": True, "deleted": result.deleted_count})


@router.post("/api/player/{account_id}/block")
async def block_player(account_id: str, request: Request):
    await _require_editor_admin(request)   # admins only (parity with live)
    """Block / unblock a player from connecting. Blocked players are served as
    permabanned+isolated at login, so the client refuses to join any room."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    blocked = 1 if body.get("blocked", True) else 0
    reason = str(body.get("reason") or "")
    patch = {"adminBlocked": blocked}
    if reason:
        patch["blockReason"] = reason
    result = await accounts.update_one({"accountID": account_id}, {"$set": patch})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Account not found")
    name = _authed_name(request) or "?"
    await _discord_notify(
        f"{name} {'BLOCKED' if blocked else 'unblocked'} player {account_id}"
        + (f" (reason: {reason})" if reason and blocked else ""))
    return JSONResponse(content={"ok": True, "adminBlocked": blocked})


# Which game version this deployment serves + edits. Each version runs as its
# own deployment with its own DB_NAME, so 1.19.0 and 2.4.0 data (and editors)
# stay fully separate. Set GAME_VERSION on the 2.4.0 deployment to "2.4.0".
from config import REQUIRED_CLIENT_VERSION as _REQ_VER
GAME_VERSION = os.getenv("GAME_VERSION", _REQ_VER)


@router.get("/api/editor_code")
async def editor_code(request: Request):
    """The current rotating editor code, for the owner to hand out (it isn't
    posted anywhere any more)."""
    if (await _session_account(request)) not in EDITOR_ADMINS:
        raise HTTPException(status_code=404, detail="not found")
    left = EDITOR_PW_ROTATE_SECONDS - (int(time.time()) % EDITOR_PW_ROTATE_SECONDS)
    return JSONResponse(content={"code": _editor_password(), "minutesLeft": left // 60})


# ---- data checker ("verify" password) --------------------------------------
# A separate, limited page: put in a player's Meta username + mobile code and it
# shows their real Yeeps account info (first login etc.) via the relay's /data
# (public fields only). Each check is a real Yeeps login attempt and Yeeps locks
# accounts after a few wrong codes, so checks are rate-limited per IP and per
# username, and every check goes to the admin webhook (never the code itself).
_VERIFY_COOKIE = "yeeps_verify"
VERIFY_SUPPORT_ROLE = os.getenv("VERIFY_SUPPORT_ROLE", "Verify Support")
_VERIFY_TTL = 6 * 3600
_verify_hits: dict = {}


def _verify_token() -> str:
    exp = str(int(time.time()) + _VERIFY_TTL)
    mac = hmac.new(EDITOR_SECRET.encode(), f"verify|{exp}".encode(), hashlib.sha256).hexdigest()
    return f"{exp}|{mac}"


def _verify_ok(request: Request) -> bool:
    try:
        exp, mac = request.cookies.get(_VERIFY_COOKIE, "").split("|")
        good = hmac.new(EDITOR_SECRET.encode(), f"verify|{exp}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(good, mac) and int(exp) > time.time() and not _from_vpn(request)
    except Exception:
        return False


def _verify_rate(key: str, limit: int, window: int) -> bool:
    now = time.time()
    q = [t for t in _verify_hits.get(key, []) if t > now - window]
    if len(q) >= limit:
        _verify_hits[key] = q
        return False
    q.append(now)
    _verify_hits[key] = q
    return True


@router.post("/verify/check")
async def verify_check(request: Request):
    if not _verify_ok(request):
        raise HTTPException(status_code=401, detail="Sign in with the checker password first")
    from services.ip_bans import client_ip
    ip = client_ip(request) or "?"
    try:
        body = await request.json()
    except Exception:
        body = {}
    user = str(body.get("username") or "").strip()[:40]
    code = str(body.get("code") or "").strip()
    if not user or not re.fullmatch(r"\d{4}", code):
        return JSONResponse({"ok": False, "error": "Enter the Meta username and the 4-digit mobile code."}, status_code=400)
    if not _verify_rate(f"ip:{ip}", 6, 600) or not _verify_rate(f"user:{user.lower()}", 3, 3600):
        return JSONResponse({"ok": False, "error": "Too many checks - wait a bit (each check uses one of that "
                                                   "player's Yeeps code attempts)."}, status_code=429)
    import httpx
    try:
        async with httpx.AsyncClient(timeout=40) as client:
            r = await client.post(f"{YEEPS_PROXY_URL}/data", json={"oculusID": user, "mobileCode": code})
        data = r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"The Yeeps login service didn't answer ({type(e).__name__})."},
                            status_code=502)
    ok = r.status_code == 200 and data.get("ok")
    await _discord_notify(f"Data checker looked up **{user}**: " + ("found" if ok else "failed") + f" [{ip}]")
    if not ok:
        return JSONResponse({"ok": False, "error": str(data.get("error") or "Wrong username or code.")}, status_code=400)
    return JSONResponse({"ok": True, "accountID": data.get("accountID"), "oculusID": data.get("oculusID"),
                         "stats": data.get("stats") or {}})


@router.get("/verify/logout")
async def verify_logout():
    resp = RedirectResponse("../login", status_code=302)
    resp.delete_cookie(_VERIFY_COOKIE)
    return resp


@router.get("/api/me")
async def whoami(request: Request):
    """Return the logged-in Meta name + which game version/DB this editor edits."""
    name = _authed_name(request)
    if not name:
        raise HTTPException(status_code=401, detail="unauthorized")
    from config import DB_NAME
    return JSONResponse(content={"name": name, "version": GAME_VERSION, "db": DB_NAME,
                                 "photon": await _photon_ok(request), "tier": await _session_tier(request),
                                 "roleAdmin": await _is_editor_admin(request)})


# ─── Photon bot tab (owner only) ──────────────────────────────────────────────
# Hook the local Photon bot (yeeps-bot) onto an online player and type its
# commands (koth, rain, tetris, boss, ...) from the editor. Only sessions that
# signed in as one of PHOTON_ADMINS (OwnerName's accounts) see or use it;
# everyone else gets 404s.
PHOTON_ADMINS = set(filter(None, (os.getenv("PHOTON_ADMINS") or
    "o_OWNER0000000001").replace(" ", "").split(",")))
player_statuses_col = _db["player_statuses"]


async def _photon_ok(request: Request) -> bool:
    # The editor name + rotating code is all a sign-in needs, so other people
    # sign in as the owner too (Oct 10: one kept pressing Stop on the bot from a
    # VPN). The bot is only useful while the owner is in game, so the tab also
    # has to be used from a network the owner's headset plays from.
    acct = await _session_account(request)
    if acct not in PHOTON_ADMINS:
        return False
    try:
        return await _owner_trusted(request, acct)
    except Exception:
        return False


async def _photon_guard(request: Request) -> None:
    if not await _photon_ok(request):
        raise HTTPException(status_code=404, detail="Not Found")


async def _online_players() -> list:
    now = datetime.now(timezone.utc).strftime("%Y %m/%d %H:%M:%S")
    sts = [s async for s in player_statuses_col.find(
        {"expireTimestamp": {"$gte": now}},
        {"_id": 0, "accountID": 1, "roomKey": 1, "photonRoomName": 1})]
    ids = [s["accountID"] for s in sts if s.get("accountID")]
    names = {a["accountID"]: a.get("displayName") or "" async for a in accounts.find(
        {"accountID": {"$in": ids}}, {"_id": 0, "accountID": 1, "displayName": 1})}
    rks = list({s.get("roomKey") for s in sts if s.get("roomKey")})
    rnames = {r["roomKey"]: r.get("roomName") or "" async for r in rooms.find(
        {"roomKey": {"$in": rks}}, {"_id": 0, "roomKey": 1, "roomName": 1})}
    out = []
    for s in sts:
        aid = s.get("accountID")
        if not aid or aid.startswith("o_99000000000000"):        # bots
            continue
        rk = s.get("roomKey") or ""
        out.append({"accountID": aid, "name": re.sub(r"<[^>]*>", "", names.get(aid, "")).strip() or aid,
                    "roomKey": rk, "roomName": rnames.get(rk, ""), "photonRoom": s.get("photonRoomName") or ""})
    out.sort(key=lambda p: (p["roomKey"], p["name"].lower()))
    return out


@router.get("/api/photon/status", dependencies=[Depends(_photon_guard)])
async def photon_status():
    from services import photon_bot
    st = await photon_bot.status()
    st["log"] = photon_bot.log_tail()
    return JSONResponse(content=st)


@router.get("/api/photon/players", dependencies=[Depends(_photon_guard)])
async def photon_players():
    return JSONResponse(content={"players": await _online_players()})


@router.post("/api/photon/start", dependencies=[Depends(_photon_guard)])
async def photon_start(request: Request):
    from services import photon_bot
    body = await request.json()
    target = str(body.get("target") or "").strip()
    if not re.fullmatch(r"o_\d{6,24}", target):
        raise HTTPException(status_code=400, detail="pick a player")
    online = {p["accountID"]: p for p in await _online_players()}
    if target not in online:
        raise HTTPException(status_code=400, detail="that player isn't online right now")
    who = _authed_name(request) or "?"
    res = await photon_bot.start(target, online[target]["name"], bool(body.get("stay")), who)
    if not res.get("ok"):
        raise HTTPException(status_code=500, detail=res.get("error") or "couldn't start the bot")
    p = online[target]
    await _discord_notify(f"**{who}** hooked the Photon bot onto **{p['name']}** in "
                          f"{p['roomName'] or p['roomKey']}{' (staying in that room)' if body.get('stay') else ''}")
    return JSONResponse(content={"ok": True})


_photon_tasks: set = set()


@router.post("/api/photon/mimic", dependencies=[Depends(_photon_guard)])
async def photon_mimic(request: Request):
    """Mimic the picked player: if the bot is already in their room just switch
    to them, otherwise send the bot to their room (staying there, not following
    them) and mimic once it's in."""
    from services import photon_bot
    body = await request.json()
    target = str(body.get("target") or "").strip()
    online = {p["accountID"]: p for p in await _online_players()}
    if target not in online:
        raise HTTPException(status_code=400, detail="that player isn't online right now")
    p = online[target]
    cmd = f"mimic {target}"                       # the bot matches account IDs exactly
    if await photon_bot.running() and photon_bot.ready() and p["photonRoom"] \
            and photon_bot.current_room() == p["photonRoom"] and not photon_bot.code_newer():
        if not photon_bot.send(cmd):
            raise HTTPException(status_code=409, detail="the bot isn't listening")
        return JSONResponse(content={"ok": True, "moved": False, "name": p["name"]})
    who = _authed_name(request) or "?"
    # stay=True: the bot goes to their room once and stays there as its own
    # player (it isn't hooked to them / doesn't follow them to other rooms)
    res = await photon_bot.start(target, p["name"], True, who)
    if not res.get("ok"):
        raise HTTPException(status_code=500, detail=res.get("error") or "couldn't start the bot")
    import asyncio
    t = asyncio.get_running_loop().create_task(photon_bot.send_when_ready(cmd))
    _photon_tasks.add(t)
    t.add_done_callback(_photon_tasks.discard)
    await _discord_notify(f"**{who}** sent the Photon bot to mimic **{p['name']}** in "
                          f"{p['roomName'] or p['roomKey']}")
    return JSONResponse(content={"ok": True, "moved": True, "name": p["name"]})


@router.post("/api/photon/cmd", dependencies=[Depends(_photon_guard)])
async def photon_cmd(request: Request):
    from services import photon_bot
    body = await request.json()
    cmd = " ".join(str(body.get("cmd") or "").split())[:200]
    if not cmd:
        raise HTTPException(status_code=400, detail="empty command")
    if cmd.lower() in ("quit", "exit", "q"):
        raise HTTPException(status_code=400, detail="use Stop bot")
    if await photon_bot.running() and photon_bot.code_newer():
        # the bot was updated since it started: reload it (same player, same
        # room setting), then run the command once it's back in the room
        m = photon_bot._meta()
        res = await photon_bot.start(m.get("target") or "", m.get("targetName") or "",
                                     bool(m.get("stay")), _authed_name(request) or "?")
        if res.get("ok"):
            import asyncio
            t = asyncio.get_running_loop().create_task(photon_bot.send_when_ready(cmd))
            _photon_tasks.add(t)
            t.add_done_callback(_photon_tasks.discard)
            print(f"[photon] {_authed_name(request)}: {cmd} (after reloading the updated bot)")
            return JSONResponse(content={"ok": True, "reloading": True})
    if not photon_bot.send(cmd):
        raise HTTPException(status_code=409, detail="the bot isn't running")
    print(f"[photon] {_authed_name(request)}: {cmd}")
    return JSONResponse(content={"ok": True})


# Live talk (owner 2026-10-13: "talk to the players live"): the owner's mic and the
# players' voices, relayed between the editor page and the Photon bot's talk socket
# (yeeps-bot "talk" command, 127.0.0.1:TALK_PORT). A WebSocket can't go through the
# main router's Request guard, so it has its own router and runs the same checks here.
ws_router = APIRouter()
TALK_PORT = int(os.getenv("YEEPS_TALK_PORT", "8791"))


@ws_router.websocket("/api/photon/talk")
async def photon_talk(ws: WebSocket):
    import asyncio
    import websockets
    origin = (ws.headers.get("origin") or "").rstrip("/")
    if origin not in _EDITOR_ORIGINS or _from_vpn(ws) or not await _photon_ok(ws):
        await ws.close(code=4403)
        return
    await ws.accept()
    bot_ws = None
    for i in range(80):                       # the bot may still be starting or following into a room
        try:
            bot_ws = await websockets.connect(f"ws://127.0.0.1:{TALK_PORT}", max_size=1 << 20)
            break
        except Exception:
            if i % 10 == 0:
                await ws.send_text(json.dumps({"type": "status", "msg": "waiting for the bot..."}))
            await asyncio.sleep(0.5)
    if bot_ws is None:
        from services import photon_bot
        tail = photon_bot.log_tail(4000, 20)
        why = ("The bot couldn't get into that room (the player probably just left it). "
               "Refresh players and press Start talking again." if "does not exist" in tail or "failed" in tail
               else "The bot's talk didn't start. Press Start talking again.")
        try:
            await ws.send_text(json.dumps({"type": "status", "msg": why}))
            await ws.close()
        except Exception:
            pass                               # the page already gave up
        return
    print(f"[photon] {_authed_name(ws)}: talk connected")

    async def up():
        while True:
            m = await ws.receive()
            if m["type"] == "websocket.disconnect":
                return
            if m.get("bytes"):
                await bot_ws.send(m["bytes"])

    async def down():
        async for m in bot_ws:
            if isinstance(m, (bytes, bytearray)):
                await ws.send_bytes(bytes(m))
            else:
                await ws.send_text(m)

    tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        try:
            await bot_ws.close()
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass
        print(f"[photon] {_authed_name(ws)}: talk disconnected")


@router.post("/api/photon/stop", dependencies=[Depends(_photon_guard)])
async def photon_stop(request: Request):
    from services import photon_bot
    res = await photon_bot.stop()
    if res.get("was"):
        await _discord_notify(f"**{_authed_name(request) or '?'}** stopped the Photon bot")
    return JSONResponse(content=res)


# ─── Default account editor ───────────────────────────────────────────────────
# The server's baseline for every NEW player is handlers.login.DEFAULT_ACCOUNT.
# To let it be edited from the web without a redeploy, we store an OVERRIDE in
# the globals collection (key "default_account_override") and login.py merges it
# over DEFAULT_ACCOUNT at account-creation / backfill time.
_DEFAULT_OVERRIDE_KEY = "default_account_override"


@router.get("/api/default")
async def get_default(request: Request):
    """Return the effective default account: DEFAULT_ACCOUNT merged with the
    stored override, so the editor shows the complete picture."""
    from handlers.login import DEFAULT_ACCOUNT
    from services.db import globals_col
    merged = dict(DEFAULT_ACCOUNT)
    ov = await globals_col.find_one({"key": _DEFAULT_OVERRIDE_KEY}, {"_id": 0, "value": 1})
    if ov and isinstance(ov.get("value"), dict):
        merged.update(ov["value"])
    return JSONResponse(content=merged)


@router.post("/api/default")
async def set_default(request: Request):
    """Patch the default-account override. Only the fields sent are overridden;
    everything else keeps falling back to DEFAULT_ACCOUNT."""
    from services.db import globals_col
    try:
        patch = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(patch, dict) or not patch:
        raise HTTPException(status_code=400, detail="No fields to update")
    patch.pop("_id", None)
    # blaze is never part of the default loadout (it's given one account at a time).
    if isinstance(patch.get("roleKeys"), list):
        patch["roleKeys"] = [r for r in patch["roleKeys"] if r != "blaze"]
    existing = await globals_col.find_one({"key": _DEFAULT_OVERRIDE_KEY}, {"_id": 0, "value": 1})
    value = dict((existing or {}).get("value") or {})
    value.update(patch)
    await globals_col.update_one({"key": _DEFAULT_OVERRIDE_KEY},
                                 {"$set": {"value": value}}, upsert=True)
    name = _authed_name(request) or "?"
    await _discord_notify(f"{name} edited default player data")
    return JSONResponse(content={"ok": True, "fields": list(patch.keys())})


# ─── News / in-game announcement (globals key "announcementData") ─────────────
@router.get("/api/news")
async def get_news():
    """Current in-game news text (the message the game shows from G2_Globals
    announcementData)."""
    from services.db import globals_col
    d = await globals_col.find_one({"key": "announcementData"}, {"_id": 0, "data": 1})
    msg = ""
    data = (d or {}).get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        msg = data[0].get("message", "") or ""
    return JSONResponse(content={"message": msg})


@router.post("/api/news")
async def set_news(request: Request):
    """Update the in-game news. Stored as announcementData.data = [{message,
    activeTime}] — the exact shape the client reads via G2_Globals GetItem."""
    from services.db import globals_col
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    message = str(body.get("message", ""))
    data = [{"message": message, "activeTime": "2026 01/01 00:00:00"}]
    await globals_col.update_one({"key": "announcementData"},
                                 {"$set": {"key": "announcementData", "data": data}}, upsert=True)
    name = _authed_name(request) or "?"
    await _discord_notify(f"**{name} updated the in-game news:**\n>>> {message or '(cleared)'}")
    return JSONResponse(content={"ok": True})


# ─── Map editor (import real maps in the /map API format) ─────────────────────
@router.get("/api/maps")
async def list_maps():
    """List every room that has map data — both editor-imported maps
    (imported_maps) and real in-game saves (maps collection, roomKey$current)."""
    from services.db import imported_maps, maps as maps_col
    out = {}
    async for m in imported_maps.find({}, {"_id": 0, "roomKey": 1, "blockCount": 1,
                                           "dimensions": 1, "importedAt": 1}):
        out[m["roomKey"]] = {**m, "source": "imported"}
    # The size is computed inside Mongo: pulling every saveData (~25 MB for 250
    # maps) to the self-hosted PC just for len() made this take 11 s.
    pipeline = [
        {"$match": {"mapKey": {"$regex": r"\$current$"}}},
        {"$project": {"_id": 0, "mapKey": 1, "savedBy": 1, "savedAt": 1,
                      "bytes": {"$cond": [{"$eq": [{"$type": "$saveData"}, "string"]},
                                          {"$strLenBytes": "$saveData"}, 0]}}},
    ]
    async for m in maps_col.aggregate(pipeline):
        rk = str(m.get("mapKey", "")).rsplit("$", 1)[0]
        if not rk or rk in out:
            continue
        out[rk] = {"roomKey": rk, "blockCount": None, "source": "saved",
                   "savedBy": m.get("savedBy"), "savedAt": m.get("savedAt"),
                   "bytes": m.get("bytes") or 0}
    return JSONResponse(content=sorted(out.values(), key=lambda r: r["roomKey"]))


def _fmt_of(sd: str) -> str:
    if sd.startswith("AAEAAAD"):
        return "1.19 BinaryFormatter"
    if sd.startswith("U1BBTQ"):
        return "2.x SPAM"
    return "empty" if not sd else "other"


@router.get("/api/map/{room_key}")
async def get_map(room_key: str):
    """Show a room's stored map. Prefers an editor-imported map (has parsed
    blocks); otherwise returns the real in-game save (roomKey$current) with the
    raw saveData blob summarized (it's base64 BinaryFormatter, not block JSON)."""
    from services.db import imported_maps, maps as maps_col
    doc = await imported_maps.find_one({"roomKey": room_key}, {"_id": 0, "saveData": 0})
    if doc:
        blocks = doc.get("blocks") or []
        # The full list: the page saves back what it was given, so a cut list
        # deleted every block past the cut (2026-10-12: BAM 5 Winner 5342 -> 2000).
        doc["blocks"] = blocks
        doc["blocksTruncated"] = False
        doc["source"] = "imported"
        return JSONResponse(content=doc)
    m = await maps_col.find_one({"mapKey": f"{room_key}$current"}, {"_id": 0})
    if not m:
        raise HTTPException(status_code=404, detail="No map stored for that room")
    sd = m.get("saveData") or ""
    isd = m.get("saveInternalStates") or ""
    # Prefer an exact editor-saved block list; otherwise decode the saved blob.
    if isinstance(m.get("editorBlocks"), list):
        blocks = m["editorBlocks"]
    else:
        from services.mapsync_decoder import decode_map
        try:
            blocks = decode_map(sd)
        except Exception:
            blocks = []
    m.pop("saveData", None)
    m.pop("saveInternalStates", None)
    m.pop("editorBlocks", None)
    m["source"] = "saved"
    m["saveDataFormat"] = _fmt_of(sd)
    m["saveDataBytes"] = len(sd)
    m["internalStatesBytes"] = len(isd)
    m["blockCount"] = len(blocks)
    # The full list: the page saves back what it was given, so a cut list
    # deleted every block past the cut (2026-10-12: BAM 5 Winner 5342 -> 2000).
    m["blocks"] = blocks
    m["blocksTruncated"] = False
    return JSONResponse(content=m)


@router.post("/api/map/{room_key}/blocks")
async def save_map_blocks(room_key: str, request: Request):
    """Save an edited block list back to a room: re-encode to the 1.19 map
    format, write it to the room's $current save, keep the exact blocks for
    re-editing, and bump the room's edit session so clients reload."""
    from services.db import maps as maps_col, rooms as rooms_col
    from services.mapsync_serializer import serialize_blocks_spam
    from services.role_locks import DISABLED_PATTERNS
    import time as _time
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    blocks = body.get("blocks")
    if not isinstance(blocks, list):
        raise HTTPException(status_code=400, detail="'blocks' must be a list")
    # normalize each block; drop DISABLED_PATTERNS (3x3x1_blazeScreen crashes
    # clients in big rooms; the desert decorations have no mesh in 1.31).
    norm = []
    skipped = 0
    for b in blocks:
        if not isinstance(b, dict):
            continue
        nm = str(b.get("name", "") or "")
        if nm in DISABLED_PATTERNS:
            skipped += 1
            continue
        norm.append({
            "name": nm,
            "x": int(b.get("x", 0) or 0), "y": int(b.get("y", 0) or 0), "z": int(b.get("z", 0) or 0),
            "fwd": int(b.get("fwd", 0) or 0), "up": int(b.get("up", 0) or 0),
            "color": str(b.get("color", "") or ""), "owner": str(b.get("owner", "") or ""),
        })
        if b.get("state"):
            norm[-1]["state"] = str(b["state"])[:2000]   # sign text etc. (Map Maker)
    # Blocks whose owner account doesn't exist go to the server owner.
    try:
        from services.map_owners import default_block_owners
        await default_block_owners(norm)
    except Exception as e:
        print(f"[editor] owner defaulting failed for {room_key}: {e}")
    try:
        # 1.31 reads/saves the flat SPAM format. We keep the existing
        # saveInternalStates (signs/dispensers/teleporters) byte-for-byte; those
        # index into object order, so in-place edits (move/recolor) stay valid —
        # adding/removing/reordering blocks can still misalign them.
        save_data = serialize_blocks_spam(norm)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not encode map: {e}")
    # Must be an int: lastEditedSessionID is a NUMBER field on G2_Rooms; storing
    # it as a string serializes as DynamoDB type S and the client reads null for
    # it -> Int32.Parse(null) -> fatal "Failed to load game".
    session_id = int(_time.time())
    # Snapshot the current map first so the editor's Revert can undo this save.
    try:
        cur = await maps_col.find_one({"mapKey": f"{room_key}$current"}, {"_id": 0})
        if cur and cur.get("saveData"):
            await _db["map_history"].insert_one(
                {"roomKey": room_key, "ts": session_id, "savedBy": cur.get("savedBy"),
                 "savedAt": cur.get("savedAt"), "bytes": len(cur.get("saveData") or ""),
                 "doc": cur, "preBlockSave": True})
    except Exception as e:
        print(f"[editor] pre-save snapshot failed for {room_key}: {e}")
    # Blocks that carry a "state" (sign text, variable values, ...) rewrite the
    # room's internal states. Those are keyed by block position, so old states
    # are kept only where a block still sits and brings no state of its own.
    states_set = {}
    if any("state" in b for b in norm):
        from services.dynamo_ops import _parse_spam_states, _build_spam_states
        import base64 as _b64
        cur_doc = await maps_col.find_one({"mapKey": f"{room_key}$current"}, {"saveInternalStates": 1}) or {}
        old = {}
        try:
            old = {tuple(pos): txt for pos, txt in _parse_spam_states(_b64.b64decode(cur_doc.get("saveInternalStates") or ""))}
        except Exception:
            old = {}
        entries = {}
        for b in norm:
            pos = (b["x"], b["y"], b["z"])
            if b.get("state"):
                entries[pos] = b["state"]
            elif pos in old:
                entries[pos] = old[pos]
        states_set = {"saveInternalStates": _build_spam_states(list(entries.items()))}
    await maps_col.update_one(
        {"mapKey": f"{room_key}$current"},
        {"$set": {**states_set, "mapKey": f"{room_key}$current", "roomKey": room_key,
                  "saveData": save_data, "editorBlocks": norm,
                  "mapDataVersion": 3, "savedBy": "editor",
                  "applicationVersion": "1.31.0",
                  "savedAt": datetime.now(timezone.utc).strftime("%Y %m/%d %H:%M:%S")}},
        upsert=True,
    )
    if states_set:   # the Full copy would win over the states we just wrote
        await maps_col.update_one({"mapKey": f"{room_key}$current"}, {"$unset": {"saveInternalStatesFull": ""}})
    await rooms_col.update_one({"roomKey": room_key},
                              {"$set": {"lastEditedSessionID": session_id, "roomDataVersion": 3}})
    name = _authed_name(request) or "?"
    _extra = f", skipped {skipped} disabled" if skipped else ""
    await _discord_notify(f"{name} edited the map for {room_key} ({len(norm)} blocks{_extra}) in the editor")
    return JSONResponse(content={"ok": True, "roomKey": room_key, "blockCount": len(norm), "skipped": skipped})


@router.post("/api/map/import")
async def import_map(request: Request):
    """Accept the output of the /map API (the full {ok,roomKey,blocks:[...]}
    envelope, a bare blocks array, or a JSON string of either), convert it to
    the MapSyncData format the game loads, and store it so that room serves the
    real blocks instead of an empty map."""
    from services.db import imported_maps
    from services.mapsync_serializer import serialize_blocks_spam, extract_blocks
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    room_key = (body.get("roomKey") or "").strip()
    raw = body.get("map")
    if not room_key:
        # allow the pasted envelope itself to carry the roomKey
        if isinstance(raw, dict) and raw.get("roomKey"):
            room_key = str(raw["roomKey"]).strip()
    if not room_key:
        raise HTTPException(status_code=400, detail="roomKey is required")
    if raw is None:
        raise HTTPException(status_code=400, detail="map data is required")
    try:
        blocks = extract_blocks(raw)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not parse map: {e}")
    dims = raw.get("dimensions") if isinstance(raw, dict) else None

    # Drop blocks whose item key the 1.19 client can't render. Real Yeeps /map
    # exports are 2.x and carry item keys that don't exist in 1.19; loading them
    # makes the client spawn broken objects that NRE every frame and crash the
    # room ("bricks" the pw). Keep only known-good 1.19 map-object keys.
    total = len(blocks)
    dropped_keys = []
    from services.db import globals_col
    ov = await globals_col.find_one({"key": "valid_mapobject_keys"}, {"_id": 0, "value": 1})
    allow = set(ov["value"]) if ov and isinstance(ov.get("value"), list) else None
    if allow:
        kept = []
        seen_drop = set()
        for b in blocks:
            k = (b.get("name") or b.get("key") or "") if isinstance(b, dict) else ""
            if k in allow:
                kept.append(b)
            elif k and k not in seen_drop:
                seen_drop.add(k); dropped_keys.append(k)
        blocks = kept

    # Drop blocks with out-of-range coordinates. A corrupt coord (e.g. y=14456)
    # is outside any room's grid and makes the client throw IndexOutOfRange ->
    # the whole room bricks. No Yeeps room exceeds ~256 on any axis.
    oob = 0
    in_bounds = []
    for b in blocks:
        try:
            if all(0 <= int(b.get(ax, 0)) < 256 for ax in ("x", "y", "z")):
                in_bounds.append(b)
            else:
                oob += 1
        except (TypeError, ValueError):
            oob += 1
    blocks = in_bounds
    # Blocks whose owner account doesn't exist go to the server owner.
    try:
        from services.map_owners import default_block_owners
        await default_block_owners(blocks)
    except Exception as e:
        print(f"[editor] owner defaulting failed for {room_key}: {e}")

    try:
        save_data = serialize_blocks_spam(blocks)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not serialize map: {e}")
    doc = {
        "roomKey": room_key,
        "blocks": blocks,
        "blockCount": len(blocks),
        "dimensions": dims,
        "saveData": save_data,
        "importedAt": datetime.now(timezone.utc).strftime("%Y %m/%d %H:%M:%S"),
    }
    if room_key.startswith("c_"):
        raise HTTPException(status_code=400, detail="Community worlds are read live from Yeeps and are not saved here.")
    await imported_maps.update_one({"roomKey": room_key}, {"$set": doc}, upsert=True)
    name = _authed_name(request) or "?"
    await _discord_notify(f"{name} imported a map for {room_key} ({len(blocks)} blocks, {total - len(blocks)} dropped)")
    return JSONResponse(content={"ok": True, "roomKey": room_key, "blockCount": len(blocks),
                                 "inputBlocks": total, "dropped": total - len(blocks),
                                 "droppedBadKeys": len(dropped_keys), "droppedOutOfBounds": oob,
                                 "droppedKeysSample": sorted(dropped_keys)[:30]})


@router.delete("/api/map/{room_key}")
async def delete_map(room_key: str, request: Request):
    """Fully reset a room's map to empty: remove the editor-imported map AND the
    in-game save (every $current / $map_* chunk), and bump the room's edit
    session so clients reload the now-empty room. Use this to recover a bricked
    room."""
    from services.db import imported_maps, maps as maps_col, rooms as rooms_col, db as _db
    import re as _re, time as _time
    esc = _re.escape(room_key)
    # Safety: back up every map doc before deleting so a reset is recoverable.
    try:
        victims = []
        async for d in maps_col.find({"mapKey": {"$regex": f"^{esc}\\$"}}):
            d.pop("_id", None); victims.append(d)
        async for d in imported_maps.find({"roomKey": room_key}):
            d.pop("_id", None); victims.append({"_imported": True, **d})
        if victims:
            await _db["map_backups"].insert_one({
                "roomKey": room_key, "resetBy": _authed_name(request) or "?",
                "resetAt": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "docs": victims})
    except Exception as e:
        print(f"[editor] map reset backup failed for {room_key}: {e}")
    r_imp = await imported_maps.delete_many({"roomKey": room_key})
    r_map = await maps_col.delete_many({"mapKey": {"$regex": f"^{esc}\\$"}})
    await rooms_col.update_one({"roomKey": room_key},
                               {"$set": {"lastEditedSessionID": int(_time.time())}})
    name = _authed_name(request) or "?"
    total = r_imp.deleted_count + r_map.deleted_count
    if total:
        await _discord_notify(f"{name} reset the map for {room_key} to empty "
                              f"({r_imp.deleted_count} imported + {r_map.deleted_count} saved removed)")
    return JSONResponse(content={"ok": True, "deleted": total,
                                 "importedRemoved": r_imp.deleted_count,
                                 "savedRemoved": r_map.deleted_count})


@router.post("/api/maps/default-owners")
async def maps_default_owners(request: Request):
    """Give every block whose owner account isn't in the database to the server
    owner (DEFAULT_MAP_OWNER), across all stored maps. Body {"apply": false}
    (default) only reports; {"apply": true} writes, snapshotting each changed
    map to its revert history first. Only the owner IDs change."""
    from services.map_owners import fix_all_maps
    try:
        body = await request.json()
    except Exception:
        body = {}
    res = await fix_all_maps(apply=bool(body.get("apply")))
    if res["applied"] and res["maps"]:
        who = _authed_name(request) or "?"
        await _discord_notify(f"{who} gave {res['blocks']} blocks owned by unknown accounts in "
                              f"{res['maps']} maps to {res['defaultOwner']}")
    return JSONResponse(content=res)


@router.get("/api/map/{room_key}/history")
async def map_history(room_key: str):
    """List saved map snapshots (newest first) available to revert to, plus
    whether an imported official copy exists."""
    from services.db import db as _db, imported_maps
    cur = _db["map_history"].find({"roomKey": room_key}).sort("ts", -1).limit(30)
    items = []
    async for d in cur:
        items.append({"id": str(d.get("_id")), "ts": d.get("ts"),
                      "savedAt": d.get("savedAt"), "savedBy": d.get("savedBy"),
                      "bytes": d.get("bytes") or len((d.get("doc") or {}).get("saveData") or "")})
    has_imported = await imported_maps.find_one({"roomKey": room_key}, {"_id": 1}) is not None
    return JSONResponse(content={"roomKey": room_key, "history": items,
                                 "hasImported": has_imported})


@router.post("/api/map/{room_key}/revert")
async def map_revert(room_key: str, request: Request):
    """Revert a room's map. Body: {"historyId": "..."} restores that snapshot, or
    {"toImported": true} drops the in-game saves so the room falls back to its
    imported official map. The current (griefed) state is snapshotted first so
    the revert itself is undoable."""
    from services.db import db as _db, maps as maps_col, rooms as rooms_col
    from bson import ObjectId
    import time as _t
    try:
        body = await request.json()
    except Exception:
        body = {}
    who = _authed_name(request) or "?"
    map_key = f"{room_key}$current"
    now = int(_t.time())

    # Snapshot the current state before we change it (so you can undo the revert).
    try:
        cur = await maps_col.find_one({"mapKey": map_key}, {"_id": 0})
        if cur and cur.get("saveData"):
            await _db["map_history"].insert_one(
                {"roomKey": room_key, "ts": now, "savedBy": cur.get("savedBy"),
                 "savedAt": cur.get("savedAt"), "bytes": len(cur.get("saveData") or ""),
                 "doc": cur, "preRevert": True})
    except Exception as e:
        print(f"[editor] pre-revert snapshot failed for {room_key}: {e}")

    if body.get("toImported"):
        esc = re.escape(room_key)
        r = await maps_col.delete_many({"mapKey": {"$regex": f"^{esc}\\$"}})
        await rooms_col.update_one({"roomKey": room_key}, {"$set": {"lastEditedSessionID": now}})
        await _discord_notify(f"♻️ **{who}** reverted **{room_key}** to its imported official map "
                              f"(dropped {r.deleted_count} in-game save chunk(s))")
        return JSONResponse(content={"ok": True, "revertedTo": "imported",
                                     "savedRemoved": r.deleted_count})

    hid = str(body.get("historyId") or "")
    if not hid:
        raise HTTPException(status_code=400, detail="Give a historyId or toImported:true")
    try:
        h = await _db["map_history"].find_one({"_id": ObjectId(hid), "roomKey": room_key})
    except Exception:
        h = None
    if not h or not h.get("doc"):
        raise HTTPException(status_code=404, detail="Snapshot not found")
    doc = dict(h["doc"])
    doc.pop("_id", None)
    doc["mapKey"] = map_key
    doc["roomKey"] = room_key
    await maps_col.update_one({"mapKey": map_key}, {"$set": doc}, upsert=True)
    await rooms_col.update_one({"roomKey": room_key}, {"$set": {"lastEditedSessionID": now}})
    await _discord_notify(f"♻️ **{who}** reverted **{room_key}** to the snapshot from "
                          f"{h.get('savedAt') or h.get('ts')} ({doc.get('savedBy') or '?'}'s save)")
    return JSONResponse(content={"ok": True, "revertedTo": h.get("savedAt") or h.get("ts"),
                                 "bytes": len(doc.get("saveData") or "")})


@router.get("/api/banned")
async def banned_players(request: Request):
    """List accounts that are currently banned (admin-block / permaban / shadowban
    / active timed ban). No IPs — this tab is about players, not addresses."""
    import re as _re
    q = {"$or": [{"adminBlocked": 1}, {"isPermabanned": 1}, {"shadowBanned": 1}]}
    proj = {"_id": 0, "accountID": 1, "displayName": 1, "oculusID": 1,
            "banReason": 1, "blockReason": 1, "banExpireTimestamp": 1,
            "adminBlocked": 1, "isPermabanned": 1, "shadowBanned": 1}
    out = []
    async for d in accounts.find(q, proj).limit(1000):
        raw = d.get("displayName") or d.get("oculusID") or d.get("accountID") or ""
        name = _re.sub(r"<[^>]+>", "", str(raw)).strip() or str(d.get("accountID") or "?")
        if d.get("shadowBanned"):
            kind = "shadowban"
        elif d.get("banExpireTimestamp"):
            kind = "timed"
        else:
            kind = "permanent"
        out.append({
            "accountID": d.get("accountID"),
            "name": name,
            "reason": (d.get("banReason") or d.get("blockReason") or ""),
            "kind": kind,
            "expires": d.get("banExpireTimestamp") or "",
        })
    out.sort(key=lambda x: (x["kind"], x["name"].lower()))
    return JSONResponse(content={"banned": out, "count": len(out)})


@router.get("/api/ipbans/all")
async def ip_bans_all(request: Request):
    """Owner tool: every IP ban on both servers with the names typed from it."""
    if not _can_see_ips(request):
        raise HTTPException(status_code=404, detail="not found")
    from services.ip_bans import list_bans_everywhere
    return JSONResponse(content={"bans": await list_bans_everywhere()})


@router.post("/api/ipbans/unban")
async def ip_bans_unban(request: Request):
    """Owner tool: lift an IP ban on BOTH servers (live + 1.31)."""
    if not _can_see_ips(request):
        raise HTTPException(status_code=404, detail="not found")
    try:
        body = await request.json()
    except Exception:
        body = {}
    ip = str(body.get("ip", "")).strip()
    if not ip:
        raise HTTPException(status_code=400, detail="Give an ip to unban")
    from services.ip_bans import unban_everywhere
    done = await unban_everywhere(ip)
    await _discord_notify(f"**{_authed_name(request) or '?'}** IP-unbanned `{ip}` ({', '.join(done) or 'was not banned'})")
    return JSONResponse(content={"ok": True, "unbannedOn": done})


@router.get("/api/ipbans")
async def ip_bans_list(request: Request):
    if not _can_see_ips(request):
        return JSONResponse(content={"restricted": True, "ips": []})
    from services.ip_bans import list_bans
    return JSONResponse(content=await list_bans())


@router.post("/api/ipban")
async def ip_ban_add(request: Request):
    from services.ip_bans import ban_ip
    try:
        body = await request.json()
    except Exception:
        body = {}
    ip = str(body.get("ip", "")).strip()
    if not ip:
        raise HTTPException(status_code=400, detail="Give an ip to ban")
    who = _authed_name(request) or "?"
    await ban_ip(ip, body.get("reason") or f"editor ban by {who}")
    await _discord_notify(f"⛔ **{who}** IP-banned `{ip}`")
    return JSONResponse(content={"ok": True, "banned": ip})


@router.post("/api/ipunban")
async def ip_ban_remove(request: Request):
    from services.ip_bans import unban_ip
    try:
        body = await request.json()
    except Exception:
        body = {}
    ip = str(body.get("ip", "")).strip()
    had = await unban_ip(ip)
    await _discord_notify(f"✅ **{_authed_name(request) or '?'}** IP-unbanned `{ip}`")
    return JSONResponse(content={"ok": True, "wasBanned": had})


def _fmt_hours(h: float) -> str:
    """Short human label for a ban duration in hours."""
    if h < 1:
        return f"{round(h * 60)} min"
    if h >= 168 and h % 168 == 0:
        return f"{int(h // 168)}w"
    if h >= 24 and h % 24 == 0:
        return f"{int(h // 24)}d"
    if h == int(h):
        return f"{int(h)}h"
    return f"{round(h, 2)}h"


@router.post("/api/player/{account_id}/ban")
async def ban_player(account_id: str, request: Request):
    await _require_editor_admin(request)   # admins only (parity with live)
    """Ban a player. Body `durationHours` > 0 makes it a TIMED ban that auto-lifts
    when it expires (account block only — no permanent IP ban for a temporary
    ban); omitted / 0 = PERMANENT (account block + IP ban)."""
    from services.ip_bans import ban_ip
    try:
        body = await request.json()
    except Exception:
        body = {}
    doc = await accounts.find_one({"accountID": account_id},
                                  {"_id": 0, "displayName": 1, "lastIP": 1, "recentIPs": 1, "deviceKeys": 1})
    if not doc:
        raise HTTPException(status_code=404, detail="Account not found")
    who = _authed_name(request) or "?"
    try:
        dur_h = float(body.get("durationHours") or 0)
    except Exception:
        dur_h = 0.0

    if dur_h > 0:
        # TIMED ban: real expiry so login serves "suspended until X" and
        # auto-clears it when it passes. isPermabanned stays 0 so the stored
        # state reads as a timed ban (login re-derives what the client sees).
        expire = (datetime.now(timezone.utc) + timedelta(hours=dur_h)).strftime("%Y %m/%d %H:%M:%S")
        reason = f"Banned by {who} via editor ({_fmt_hours(dur_h)})"
        await accounts.update_one({"accountID": account_id},
                                  {"$set": {"adminBlocked": 1, "isPermabanned": 0,
                                            "isMutebanned": 0, "isIsolated": 0,
                                            "blockReason": reason, "banReason": reason,
                                            "banCategory": "blocked",
                                            "banExpireTimestamp": expire,
                                            "shouldForceRefresh": 1}})
        # A temporary ban must NOT leave a permanent IP ban behind (IP bans don't
        # expire and hit everyone sharing that IP), so the account block is enough.
        ips = []
    else:
        # PERMANENT ban: block the account AND every IP it has logged in from.
        expire = ""
        reason = f"Banned by {who} via editor (permanent)"
        await accounts.update_one({"accountID": account_id},
                                  {"$set": {"adminBlocked": 1, "isPermabanned": 1,
                                            "blockReason": reason, "banReason": reason,
                                            "banCategory": "blocked",
                                            "banExpireTimestamp": "",
                                            "shouldForceRefresh": 1}})
        ips = sorted(set(filter(None, [doc.get("lastIP")] + list(doc.get("recentIPs") or []))))
        for ip in ips:
            await ban_ip(ip, f"account {account_id} ({doc.get('displayName')}) banned by {who}")
        # ...and its headset(s), so a new account on the same Quest is banned too.
        from services.device_bans import ban_devices
        await ban_devices(doc.get("deviceKeys") or [], f"account {account_id} banned by {who}")

    span = f"until {expire}" if expire else "permanent"
    # Every account linked to the same Discord is banned too, and (permanent ban)
    # that Discord can't /link an alt (owner 2026-11-02).
    try:
        from services.discord_bans import ban_linked
        _dids, _others = await ban_linked(account_id, reason, expire)
        if _dids or _others:
            await _discord_notify(f"Discord ban for `{account_id}`: Discord(s) {', '.join(sorted(_dids)) or 'none'}"
                                  + (" blocked from /link" if not expire else "")
                                  + (f"; also banned linked account(s) {', '.join(sorted(_others))}" if _others else ""))
    except Exception as _e:
        print(f"[editor] discord ban failed for {account_id}: {_e}")
    await _discord_notify(f"**{who}** banned **{doc.get('displayName') or account_id}** "
                          f"(`{account_id}`) [{span}]"
                          + (f" + {len(ips)} IP(s): {', '.join(ips)}" if ips else ""))
    # Hide the actual IPs from editor users who aren't allowed to see them.
    shown = ips if _can_see_ips(request) else ["(hidden)"] * len(ips)
    return JSONResponse(content={"ok": True, "bannedIPs": shown,
                                 "until": expire, "permanent": not expire})


@router.post("/api/player/{account_id}/unban")
async def unban_player(account_id: str, request: Request):
    await _require_editor_admin(request)   # admins only (parity with live)
    from services.ip_bans import unban_ip
    try:
        body = await request.json()
    except Exception:
        body = {}
    doc = await accounts.find_one({"accountID": account_id},
                                  {"_id": 0, "displayName": 1, "lastIP": 1, "recentIPs": 1})
    if not doc:
        raise HTTPException(status_code=404, detail="Account not found")
    who = _authed_name(request) or "?"
    await accounts.update_one({"accountID": account_id},
                              {"$set": {"adminBlocked": 0, "isPermabanned": 0,
                                        "isMutebanned": 0, "isIsolated": 0,
                                        "blockReason": "", "banReason": "",
                                        "banExpireTimestamp": "", "banCategory": "",
                                        "shadowBanned": 0, "shouldForceRefresh": 1}})
    from services.device_bans import unban_devices
    _ud = await accounts.find_one({"accountID": account_id}, {"_id": 0, "deviceKeys": 1})
    await unban_devices((_ud or {}).get("deviceKeys") or [])
    try:
        from services.discord_bans import unban_linked
        _dids, _freed = await unban_linked(account_id)
        if _freed:
            await _discord_notify(f"Also unbanned account(s) banned through `{account_id}`'s Discord: "
                                  f"{', '.join(sorted(_freed))}")
    except Exception as _e:
        print(f"[editor] discord unban failed for {account_id}: {_e}")
    unbanned = []
    if body.get("alsoIP"):
        for ip in set(filter(None, [doc.get("lastIP")] + list(doc.get("recentIPs") or []))):
            if await unban_ip(ip):
                unbanned.append(ip)
    await _discord_notify(f"✅ **{who}** unbanned **{doc.get('displayName') or account_id}** (`{account_id}`)"
                          + (f" + IPs {', '.join(unbanned)}" if unbanned else ""))
    shown = unbanned if _can_see_ips(request) else ["(hidden)"] * len(unbanned)
    return JSONResponse(content={"ok": True, "unbannedIPs": shown})


# ── Kick (boot) players ─────────────────────────────────────────────
# One-shot boots, not bans: see services/kick.py. Build UI on these.

@router.get("/api/online")
async def online_players():
    """Who is online right now: [{accountID, displayName, roomKey}]."""
    from services.kick import online_account_ids
    from services.db import player_statuses
    from services.build_gate import current_status
    ids = await online_account_ids()
    acc = {a["accountID"]: a async for a in
           accounts.find({"accountID": {"$in": ids}},
                         {"_id": 0, "accountID": 1, "displayName": 1,
                          "clientBuildTag": 1, "clientBuildStatus": 1})}
    rooms_by = {p["accountID"]: p.get("roomKey") async for p in
                player_statuses.find({"accountID": {"$in": ids}}, {"_id": 0, "accountID": 1, "roomKey": 1})}
    return JSONResponse(content={"ok": True, "count": len(ids), "players": [
        {"accountID": a, "displayName": (acc.get(a) or {}).get("displayName") or a,
         "roomKey": rooms_by.get(a) or "",
         # APK build the player is on (services/build_gate.py): "ok",
         # "outdated" (old/unofficial APK) or "modified" (patched native libs).
         "build": (acc.get(a) or {}).get("clientBuildTag") or "",
         "buildStatus": current_status(acc.get(a) or {})}
        for a in ids]})


@router.post("/api/player/{account_id}/kick")
async def kick_player(account_id: str, request: Request):
    """Boot one player once. Body (optional): {"reason": "..."}."""
    from services.kick import kick_account
    try:
        body = await request.json()
    except Exception:
        body = {}
    doc = await accounts.find_one({"accountID": account_id}, {"_id": 0, "displayName": 1})
    if not doc or not await kick_account(account_id, body.get("reason", "")):
        raise HTTPException(status_code=404, detail="Account not found")
    who = _authed_name(request) or "?"
    await _discord_notify(f"**{who}** kicked **{doc.get('displayName') or account_id}** (`{account_id}`)"
                          + (f": {body['reason']}" if body.get("reason") else ""))
    return JSONResponse(content={"ok": True})


@router.post("/api/player/{account_id}/device-reset")
async def reset_device_lock(account_id: str, request: Request):
    await _require_editor_admin(request)   # admins only (parity with live)
    """Unbind every headset from this account (services/device_auth.py). The
    player's next login - from any IP - binds their (new) headset."""
    from services import device_auth
    doc = await accounts.find_one({"accountID": account_id}, {"_id": 0, "displayName": 1})
    if not doc:
        raise HTTPException(status_code=404, detail="Account not found")
    await device_auth.reset(account_id)
    who = _authed_name(request) or "?"
    await _discord_notify(f"**{who}** reset the headset lock for **{doc.get('displayName') or account_id}** (`{account_id}`)")
    return JSONResponse(content={"ok": True})


# ── Announcements + remote disk audio (services/remote.py) ──────────
# The 1.31 APK (131b2+) shows announcements as floating text and swaps disk
# audio from these. Build UI on these endpoints.

@router.get("/api/announce")
async def list_announcements():
    from services.remote import active_announcements
    return JSONResponse(content={"ok": True, "announcements": await active_announcements()})


@router.post("/api/announce")
async def post_announcement(request: Request):
    """Body: {"text": "...", "seconds": 10 (on screen), "activeMinutes": 10
    (players who launch within this window still get it)}."""
    from services.remote import add_announcement
    try:
        body = await request.json()
    except Exception:
        body = {}
    text = str(body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    who = _authed_name(request) or "?"
    a = await add_announcement(text, body.get("seconds", 10), body.get("activeMinutes", 10), who)
    await _discord_notify(f"**{who}** sent an announcement: {a['text']}")
    return JSONResponse(content={"ok": True, "announcement": a})


@router.delete("/api/announce")
async def delete_announcements(request: Request):
    from services.remote import clear_announcements
    await clear_announcements()
    return JSONResponse(content={"ok": True})


@router.get("/api/errors")
async def list_errors(window: str = "24h", muted: int = 0):
    from services.client_errors import summary
    return JSONResponse(content={"ok": True, "errors": await summary(window, bool(muted))})


@router.get("/api/errors/{sid}")
async def error_detail(sid: str):
    from services.client_errors import detail
    d = await detail(sid)
    if not d:
        raise HTTPException(status_code=404, detail="unknown error")
    return JSONResponse(content={"ok": True, "error": d})


@router.post("/api/errors/{sid}/mute")
async def mute_error(sid: str, request: Request):
    from services.client_errors import set_muted
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not await set_muted(sid, bool(body.get("muted", True))):
        raise HTTPException(status_code=404, detail="unknown error")
    return JSONResponse(content={"ok": True})


@router.get("/api/screens")
async def list_screens():
    from services.remote import SCREEN_SLOTS, screen_images
    have = {d["slot"]: d async for d in screen_images.find({}, {"_id": 0, "data": 0, "preview": 0})}
    out = [{"slot": k, "label": v[0], "w": v[1], "h": v[2], "image": have.get(k)}
           for k, v in SCREEN_SLOTS.items()]
    return JSONResponse(content={"ok": True, "screens": out})


@router.get("/api/screens/{slot}/preview.png")
async def screen_preview(slot: str):
    from fastapi.responses import Response as _R
    from services.remote import get_screen
    d = await get_screen(slot, preview=True)
    if not d or not d.get("preview"):
        raise HTTPException(status_code=404, detail="no picture")
    return _R(content=bytes(d["preview"]), media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/api/screens/{slot}")
async def upload_screen(slot: str, request: Request):
    """Raw image file (PNG/JPG/GIF/WebP) as the request body."""
    from services.remote import SCREEN_SLOTS, set_screen
    if slot not in SCREEN_SLOTS:
        raise HTTPException(status_code=404, detail="unknown screen")
    data = await request.body()
    if len(data) < 32:
        raise HTTPException(status_code=400, detail="empty file")
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="file too big (max 25 MB)")
    try:
        info = await set_screen(slot, data, _authed_name(request) or "?")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"could not read image: {e}")
    who = _authed_name(request) or "?"
    await _discord_notify(f"**{who}** set the {SCREEN_SLOTS[slot][0]} picture "
                          f"({info['frames']} frame(s), {info['size']//1024} KB)")
    return JSONResponse(content={"ok": True, "screen": info})


@router.delete("/api/screens/{slot}")
async def remove_screen(slot: str, request: Request):
    from services.remote import delete_screen
    ok = await delete_screen(slot)
    if ok:
        await _discord_notify(f"**{_authed_name(request) or '?'}** removed the {slot} screen picture")
    return JSONResponse(content={"ok": ok})


# ── Floating video (Announce tab): load a video to play for everyone, or stop it.
@router.get("/api/video")
async def video_status():
    from services import video
    m = await video.active_video()
    return JSONResponse(content={"ok": True, "playing": bool(m), "video": m,
                                 "gen": await video.video_gen()})


@router.post("/api/video_packed")
async def video_load_packed(request: Request):
    """Video already converted IN THE BROWSER (editor Announce tab): frames as
    BSCR segments + WAV audio, so the upload is small and the server does no
    CPU work. Rejected (409) if Stop/another Load happened since it started."""
    import mmap, tempfile
    from services import video
    who = _authed_name(request) or "?"
    # A 2-minute video is ~70 MB: stream it to an unnamed temp file and parse it
    # through mmap, so it never sits in this process's RAM (self-hosted, low RAM).
    os.makedirs(video.VIDEO_DIR, exist_ok=True)
    tf = tempfile.TemporaryFile(dir=video.VIDEO_DIR)
    mm = conv = None
    try:
        n = 0
        async for chunk in request.stream():
            n += len(chunk)
            if n > 100 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="video too big (max 100 MB)")
            tf.write(chunk)
        if n < 16:
            raise HTTPException(status_code=400, detail="empty upload")
        tf.flush()
        mm = mmap.mmap(tf.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            conv = video.parse_packed(mm)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"bad packed video: {e}")
        conv["meta"]["by"] = who
        try:
            meta = await video.store_and_play(conv, conv["loop"], conv["gen"])
        except video.StaleVideo:
            raise HTTPException(status_code=409, detail="stopped while converting - not played")
    finally:
        conv = None                     # drop the memoryview slices before unmapping
        if mm is not None:
            try:
                mm.close()
            except BufferError:         # a traceback still holds a slice; GC unmaps it
                pass
        tf.close()
    await _discord_notify(
        f"**{who}** started a floating video '{meta.get('name')}' "
        f"({meta['frames']} frames, {meta.get('durationMs', 0)//1000}s, {meta['size']//1024} KB, "
        f"{'with sound' if meta.get('audioFmt') else 'no sound'})")
    return JSONResponse(content={"ok": True, "video": meta})


@router.post("/api/video")
async def video_load(request: Request):
    """Raw video file (mp4/webm/mov/gif) as the body. Query: ?loop=1&name=...
    Converts to looping floating-video frames + audio and plays it for everyone."""
    import asyncio
    from services import video
    data = await request.body()
    if len(data) < 64:
        raise HTTPException(status_code=400, detail="empty file")
    if len(data) > video.MAX_UPLOAD:
        raise HTTPException(status_code=400,
                            detail=f"file too big (max {video.MAX_UPLOAD // (1024*1024)} MB)")
    who = _authed_name(request) or "?"
    name = (request.query_params.get("name") or "video")[:80]
    loop = request.query_params.get("loop", "1") != "0"
    gen = request.query_params.get("gen")
    try:
        conv = await asyncio.to_thread(video.convert_video, data, name, who)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"could not convert video: {e}")
    if not conv["meta"].get("frames"):
        raise HTTPException(status_code=400, detail="no video frames decoded")
    try:
        meta = await video.store_and_play(conv, loop, gen)
    except video.StaleVideo:
        raise HTTPException(status_code=409, detail="stopped while converting - not played")
    dur = meta.get("durationMs", 0) // 1000
    await _discord_notify(
        f"**{who}** started a floating video '{meta.get('name')}' "
        f"({meta['frames']} frames @ {meta['fps']}fps, {dur}s, {meta['size']//1024} KB, "
        f"{'looping' if loop else 'once'})")
    return JSONResponse(content={"ok": True, "video": meta})


@router.delete("/api/video")
async def video_stop(request: Request):
    from services import video
    was = await video.stop_video()
    if was:
        await _discord_notify(f"**{_authed_name(request) or '?'}** stopped the floating video")
    return JSONResponse(content={"ok": True, "wasPlaying": was})


@router.get("/api/settings")
async def get_settings():
    from services.db import globals_col
    fe = await globals_col.find_one({"key": "friends_everyone"}, {"_id": 0, "value": 1})
    fatal = await globals_col.find_one({"key": "fatalError"}, {"_id": 0, "data": 1})
    return JSONResponse(content={"ok": True,
                                 "friends_everyone": bool((fe or {}).get("value", False)),
                                 "maintenance": (fatal or {}).get("data") or ""})


@router.post("/api/settings")
async def set_settings(request: Request):
    """Body: {"friends_everyone": bool} and/or {"maintenance": "message" | ""}.
    A maintenance message is served as the game's global fatal error, so every
    client shows it instead of playing; "" turns it off."""
    from services.db import globals_col
    try:
        body = await request.json()
    except Exception:
        body = {}
    who = _authed_name(request) or "?"
    if "friends_everyone" in body:
        on = bool(body["friends_everyone"])
        await globals_col.update_one({"key": "friends_everyone"},
                                     {"$set": {"key": "friends_everyone", "value": on}}, upsert=True)
        await _discord_notify(f"**{who}** turned everyone-is-a-friend {'ON' if on else 'OFF'}")
    if "maintenance" in body:
        msg = str(body["maintenance"] or "").strip()[:300]
        await globals_col.update_one({"key": "fatalError"},
                                     {"$set": {"key": "fatalError", "data": msg}}, upsert=True)
        await _discord_notify(f"**{who}** " + (f"turned ON maintenance: {msg}" if msg else "turned OFF maintenance"))
    return JSONResponse(content={"ok": True})


@router.get("/api/disks")
async def list_disks():
    from services.remote import disk_audio
    out = [d async for d in disk_audio.find({}, {"_id": 0, "data": 0})]
    return JSONResponse(content={"ok": True, "disks": out})


@router.post("/api/disks/{record}")
async def upload_disk(record: str, request: Request, ext: str = "ogg"):
    """Raw audio file as the request body; ?ext=ogg|wav (16-bit). `record` is the disk
    key (record_theFinalHider) or its track name (TheFinalHider)."""
    from services.remote import AUDIO_TYPES, set_disk
    ext = (ext or "ogg").lower().lstrip(".")
    if ext not in AUDIO_TYPES:
        raise HTTPException(status_code=400, detail="ext must be ogg or wav (16-bit PCM)")
    data = await request.body()
    if len(data) < 1024:
        raise HTTPException(status_code=400, detail="empty or too-small audio file")
    who = _authed_name(request) or "?"
    clip = await set_disk(record, ext, data, who)
    await _discord_notify(f"**{who}** set disk **{clip}** to a new {ext} ({len(data)//1024} KB)")
    return JSONResponse(content={"ok": True, "clip": clip, "bytes": len(data)})


@router.delete("/api/disks/{record}")
async def remove_disk(record: str, request: Request):
    from services.remote import delete_disk
    return JSONResponse(content={"ok": await delete_disk(record)})


@router.post("/api/kick_wrong_build")
async def kick_wrong_build_players(request: Request):
    """Boot everyone online who isn't on an allowed APK build. Body (optional): {"reason": "..."}."""
    from services.kick import kick_wrong_build
    try:
        body = await request.json()
    except Exception:
        body = {}
    kicked = await kick_wrong_build(body.get("reason", ""))
    who = _authed_name(request) or "?"
    await _discord_notify(f"**{who}** kicked {len(kicked)} player(s) on the wrong APK build")
    return JSONResponse(content={"ok": True, "kicked": len(kicked), "accountIDs": kicked})


@router.get("/api/require_build")
async def require_build_status():
    from services.db import globals_col
    from services.build_gate import newest_build
    d = await globals_col.find_one({"key": "requireBuild"}, {"_id": 0, "value": 1})
    return JSONResponse(content={"ok": True, "requireBuild": str((d or {}).get("value") or ""),
                                 "newest": newest_build()})


@router.post("/api/require_build")
async def set_require_build(request: Request):
    """Testing toggle. {"on": true} requires the newest build: every other client
    is served "Outdated version" and kicked (frees CCU, keeps them out while on).
    {"on": false} allows all allowlisted builds again."""
    from services.db import globals_col
    from services.build_gate import newest_build
    from services.kick import online_account_ids
    try:
        body = await request.json()
    except Exception:
        body = {}
    on = bool(body.get("on"))
    who = _authed_name(request) or "?"
    target = newest_build() if on else "*"   # "*" = allow all builds (testing)
    await globals_col.update_one({"key": "requireBuild"},
                                 {"$set": {"key": "requireBuild", "value": target}}, upsert=True)
    kicked = 0
    if on and target:
        # Force online players not on the newest build to re-pull now, so they
        # get the outdated block and leave immediately instead of after ~45 s.
        ids = await online_account_ids()
        if ids:
            wrong = [a["accountID"] async for a in accounts.find(
                {"accountID": {"$in": ids}}, {"_id": 0, "accountID": 1, "clientBuildTag": 1})
                if (a.get("clientBuildTag") or "").split(".", 1)[0] != target]
            if wrong:
                await accounts.update_many({"accountID": {"$in": wrong}},
                                           {"$set": {"shouldForceRefresh": 1}})
                kicked = len(wrong)
    await _discord_notify(
        f"**{who}** enabled require-newest-build ({target}) — kicked {kicked} old-build player(s)"
        if on else f"**{who}** disabled require-newest-build (all builds allowed again)")
    return JSONResponse(content={"ok": True, "requireBuild": target, "kicked": kicked})


@router.post("/api/kick_all")
async def kick_all_players(request: Request):
    """Boot everyone online. Body (optional): {"reason": "...", "exclude": [accountIDs]}."""
    from services.kick import kick_all
    try:
        body = await request.json()
    except Exception:
        body = {}
    kicked = await kick_all(body.get("reason", ""), body.get("exclude") or [])
    who = _authed_name(request) or "?"
    await _discord_notify(f"**{who}** kicked ALL online players ({len(kicked)})"
                          + (f": {body['reason']}" if body.get("reason") else ""))
    return JSONResponse(content={"ok": True, "kicked": len(kicked), "accountIDs": kicked})


_MEM_BASE = {"snap": None}


@router.get("/api/debug/mem")
async def debug_mem(reset: int = 0, top: int = 25, collect: int = 0, trim: int = 0):
    """Where is the game server's memory going? Object counts by type, and (when
    started with TRACEMALLOC=1) the code lines that allocated the most since the
    last ?reset=1."""
    import gc, collections, tracemalloc
    out = {}
    if collect:
        before = len(gc.get_objects())
        freed = gc.collect()
        out["gc_collect"] = {"objects_before": before, "unreachable_freed": freed,
                             "objects_after": len(gc.get_objects()), "garbage": len(gc.garbage)}
    if trim:
        # give freed-but-held C heap memory back to the OS (glibc)
        import ctypes
        def rss():
            for line in open("/proc/self/status"):
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
        before = rss()
        ctypes.CDLL("libc.so.6").malloc_trim(0)
        out["malloc_trim"] = {"rss_before_mb": before, "rss_after_mb": rss()}
    counts = collections.Counter(type(o).__name__ for o in gc.get_objects())
    out["objects"] = counts.most_common(top)
    if tracemalloc.is_tracing():
        snap = tracemalloc.take_snapshot().filter_traces(
            [tracemalloc.Filter(False, tracemalloc.__file__)])
        if reset or _MEM_BASE["snap"] is None:
            _MEM_BASE["snap"] = snap
            out["tracemalloc"] = "baseline taken"
        else:
            diff = snap.compare_to(_MEM_BASE["snap"], "traceback")
            out["growth"] = [{"kb": round(d.size_diff / 1024), "count": d.count_diff,
                              "where": [f"{f.filename.split('/')[-1]}:{f.lineno}" for f in d.traceback[-6:]]}
                             for d in diff[:top]]
        cur, peak = tracemalloc.get_traced_memory()
        out["traced_mb"] = round(cur / 1048576, 1)
    # Pending asyncio tasks grouped by where they are waiting (innermost frames)
    import asyncio
    waits = collections.Counter()
    tasks = asyncio.all_tasks()
    for t in tasks:
        st = t.get_stack(limit=40)
        mine = [f for f in st if "/vr-server/" in f.f_code.co_filename]
        where = mine[-1] if mine else (st[-1] if st else None)
        inner = st[-1] if st else None
        key = (f"{where.f_code.co_filename.split('/')[-1]}:{where.f_lineno} {where.f_code.co_name}" if where else "?") + \
              (f"  <waiting in> {inner.f_code.co_filename.split('/')[-1]}:{inner.f_lineno} {inner.f_code.co_name}" if inner else "")
        waits[key] += 1
    out["tasks"] = len(tasks)
    out["task_waits"] = waits.most_common(top)
    return JSONResponse(content=out)


@router.get("/api/debug")
async def list_debug(limit: int = 50):
    """Most recent g2_logDebug payloads the client sent, newest first."""
    cursor = debug_logs.find({}, {"_id": 0}).sort("_id", -1).limit(limit)
    return JSONResponse(content=await cursor.to_list(length=limit))


@router.delete("/api/debug")
async def clear_debug():
    result = await debug_logs.delete_many({})
    return JSONResponse(content={"ok": True, "deleted": result.deleted_count})


@router.get("/api/rooms")
async def list_rooms(q: str = "", limit: int = 200):
    """List/search rooms."""
    filt = {}
    if q:
        filt = {"$or": [
            {"roomKey": {"$regex": q, "$options": "i"}},
            {"roomName": {"$regex": q, "$options": "i"}},
        ]}
    cursor = rooms.find(
        filt,
        {"_id": 0, "roomKey": 1, "roomName": 1, "isCreative": 1,
         "isModeLocked": 1, "blockMatchmakingSegmentation": 1, "creativeSessionID": 1},
    ).limit(limit)
    docs = await cursor.to_list(length=limit)
    return JSONResponse(content=docs)


@router.get("/api/room/{room_key}")
async def get_room(room_key: str):
    """Return the full room document. For rooms not stored in the collection
    (e.g. players' private worlds, served on-the-fly) return the generated
    room data so it can still be viewed/edited; saving persists it."""
    doc = await rooms.find_one({"roomKey": room_key}, {"_id": 0})
    if doc is None:
        try:
            from services.dynamo_ops import _get_fallback_doc
            doc = await _get_fallback_doc("G2_Rooms", {"roomKey": room_key})
        except Exception:
            doc = None
        if doc is None:
            raise HTTPException(status_code=404, detail="Room not found")
        doc = {**doc, "_generated": True}
    return JSONResponse(content=doc)


@router.post("/api/room/{room_key}")
async def update_room(room_key: str, request: Request):
    """Patch fields on a room."""
    try:
        patch = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    patch.pop("roomKey", None)
    patch.pop("_id", None)
    patch.pop("_generated", None)   # UI-only marker, never store it
    if not patch:
        raise HTTPException(status_code=400, detail="No fields to update")

    if room_key.startswith("c_"):
        raise HTTPException(status_code=400, detail="Community worlds are read live from Yeeps and are not saved here.")
    # upsert so saving a room that was served on-the-fly (players' private
    # worlds have no stored doc) creates it instead of 404-ing.
    result = await rooms.update_one(
        {"roomKey": room_key}, {"$set": {**patch, "roomKey": room_key}}, upsert=True)

    # A world-map room's biome also lives on its room-map node (the client reads
    # that for the scene). Keep both in sync so changing themeKey actually works.
    if "themeKey" in patch:
        from services.db import room_maps
        async for rm in room_maps.find({}):
            data = rm.get("data")
            if not isinstance(data, list):
                continue
            changed = False
            for node in data:
                if isinstance(node, dict) and node.get("roomKey") == room_key \
                        and node.get("themeKey") != patch["themeKey"]:
                    node["themeKey"] = patch["themeKey"]; changed = True
            if changed:
                await room_maps.update_one({"_id": rm["_id"]}, {"$set": {"data": data}})

    return JSONResponse(content={"ok": True, "modified": result.modified_count,
                                 "created": bool(result.upserted_id)})


@router.post("/api/import")
async def import_accounts(request: Request):
    """
    Import one or more full account documents (create or overwrite).
    Body can be a single object or a JSON array of objects; each must
    include an accountID field, which is used as the upsert key.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    items = payload if isinstance(payload, list) else [payload]
    if not items:
        raise HTTPException(status_code=400, detail="No accounts to import")

    imported, errors = [], []
    for i, doc in enumerate(items):
        if not isinstance(doc, dict):
            errors.append(f"item {i}: not an object")
            continue
        account_id = doc.get("accountID")
        if not account_id:
            errors.append(f"item {i}: missing accountID")
            continue
        doc = {k: v for k, v in doc.items() if k != "_id"}
        await accounts.update_one({"accountID": account_id}, {"$set": doc}, upsert=True)
        imported.append(account_id)

    return JSONResponse(content={"ok": True, "imported": imported, "errors": errors})


@router.post("/api/import_login")
async def import_by_login(request: Request):
    """
    Import an account by logging in through yeeps-proxy's own /data
    endpoint (the audited relay to TrassGames' backend) instead of
    reimplementing that login flow here.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    oculus_id = (body.get("oculusID") or "").strip()
    mobile_code = (body.get("mobileCode") or "").strip()
    if not oculus_id:
        raise HTTPException(status_code=400, detail="oculusID is required")
    if not mobile_code:
        raise HTTPException(status_code=400, detail="mobileCode is required")

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{YEEPS_PROXY_URL}/data",
                json={"oculusID": oculus_id, "mobileCode": mobile_code},
            )
        data = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"yeeps-proxy request failed: {e}")

    if resp.status_code != 200 or not data.get("ok"):
        raise HTTPException(status_code=resp.status_code or 502, detail=data.get("error", "Login failed"))

    account_id = data.get("accountID")
    if not account_id:
        raise HTTPException(status_code=502, detail="yeeps-proxy response missing accountID")

    doc = {"accountID": account_id, "oculusID": data.get("oculusID", oculus_id), **data.get("stats", {})}
    await accounts.update_one({"accountID": account_id}, {"$set": doc}, upsert=True)

    return JSONResponse(content={"ok": True, "imported": [account_id]})


# ─── HTML UI ─────────────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Yeeps Player Editor</title>
<style>
  :root {
    --bg: #0e0e12;
    --surface: #18181f;
    --border: #2a2a35;
    --accent: #7c6af7;
    --accent2: #4fd1c5;
    --text: #e8e8f0;
    --muted: #888899;
    --danger: #f56565;
    --success: #48bb78;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font: 14px/1.5 'Segoe UI', system-ui, sans-serif; min-height: 100vh; }

  header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 14px 24px; display: flex; align-items: center; gap: 16px; }
  header h1 { font-size: 18px; font-weight: 700; color: var(--accent); letter-spacing: .5px; }
  header span { color: var(--muted); font-size: 12px; }

  .layout { display: flex; height: calc(100vh - 57px); }

  /* sidebar */
  .sidebar { width: 300px; min-width: 220px; border-right: 1px solid var(--border); display: flex; flex-direction: column; background: var(--surface); }
  .search-wrap { padding: 12px; border-bottom: 1px solid var(--border); }
  .search-wrap input { width: 100%; background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 8px 12px; color: var(--text); font-size: 13px; outline: none; }
  .search-wrap input:focus { border-color: var(--accent); }
  .player-list { overflow-y: auto; flex: 1; }
  .player-item { padding: 10px 16px; cursor: pointer; border-bottom: 1px solid var(--border); transition: background .1s; }
  .player-item:hover { background: rgba(124,106,247,.08); }
  .player-item.active { background: rgba(124,106,247,.18); border-left: 3px solid var(--accent); }
  .player-item .name { font-weight: 600; font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .player-item .id { color: var(--muted); font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .player-item .coins { color: var(--accent2); font-size: 11px; }

  /* main */
  .main { flex: 1; overflow-y: auto; padding: 24px; }
  .empty { color: var(--muted); text-align: center; margin-top: 80px; font-size: 15px; }

  .player-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 20px; flex-wrap: wrap; gap: 8px; }
  .player-header h2 { font-size: 20px; font-weight: 700; }
  .player-header .acct-id { color: var(--muted); font-size: 12px; margin-top: 2px; }

  .btn { padding: 8px 16px; border-radius: 6px; border: none; cursor: pointer; font-size: 13px; font-weight: 600; transition: opacity .15s; }
  .btn:hover { opacity: .85; }
  .btn-primary { background: var(--accent); color: #fff; }
  .btn-danger { background: var(--danger); color: #fff; }
  .btn-sm { padding: 5px 10px; font-size: 12px; }

  .section { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; margin-bottom: 20px; overflow: hidden; }
  .section-title { padding: 12px 16px; background: rgba(255,255,255,.03); font-weight: 700; font-size: 13px; letter-spacing: .4px; color: var(--accent2); border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; }
  .field-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 12px; padding: 16px; }

  .field { display: flex; flex-direction: column; gap: 4px; }
  .field label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; }
  .field input, .field textarea, .field select {
    background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
    padding: 7px 10px; color: var(--text); font-size: 13px; outline: none;
    font-family: inherit;
  }
  .field input:focus, .field textarea:focus, .field select:focus { border-color: var(--accent); }
  .field textarea { min-height: 80px; resize: vertical; font-family: monospace; font-size: 12px; }
  .field.wide { grid-column: 1 / -1; }

  .toast { position: fixed; bottom: 24px; right: 24px; padding: 12px 20px; border-radius: 8px; font-size: 13px; font-weight: 600; opacity: 0; transition: opacity .3s; pointer-events: none; z-index: 999; }
  .toast.show { opacity: 1; }
  .toast.ok { background: var(--success); color: #fff; }
  .toast.err { background: var(--danger); color: #fff; }

  .raw-toggle { font-size: 11px; color: var(--accent); cursor: pointer; }
  .raw-wrap { padding: 16px; }
  .raw-wrap textarea { width: 100%; height: calc(100vh - 260px); min-height: 400px; box-sizing: border-box; background: var(--bg); border: 1px solid var(--border); border-radius: 6px; color: var(--text); font-family: monospace; font-size: 12px; padding: 12px; outline: none; resize: vertical; }
  .raw-wrap textarea:focus { border-color: var(--accent); }

  @media (max-width: 600px) {
    .layout { flex-direction: column; }
    .sidebar { width: 100%; height: 200px; border-right: none; border-bottom: 1px solid var(--border); }
    .field-grid { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<header>
  <h1>&#9762; Yeeps Player Editor</h1>
  <span id="status">Loading…</span>
  <button class="btn btn-primary btn-sm" id="import-open-btn" style="margin-left:auto">Import</button>
</header>
<div class="layout">
  <div class="sidebar">
    <div style="display:flex;gap:4px;padding:8px 12px 0">
      <button class="btn" id="mode-players" style="flex:1;background:var(--accent);color:#fff">Players</button>
      <button class="btn" id="mode-rooms" style="flex:1;background:var(--border);color:var(--text)">Rooms</button>
    </div>
    <div class="search-wrap">
      <input id="search" type="text" placeholder="Search name or ID…" autocomplete="off">
    </div>
    <div class="player-list" id="player-list"></div>
  </div>
  <div class="main" id="main">
    <div class="empty">Select a player to edit</div>
  </div>
</div>
<div class="toast" id="toast"></div>

<div id="import-overlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:998;align-items:center;justify-content:center">
  <div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;width:min(700px,90vw);max-height:85vh;display:flex;flex-direction:column;overflow:hidden">
    <div class="section-title">
      Import Account Data
      <span class="raw-toggle" id="import-close-btn">Close</span>
    </div>
    <div style="padding:0 16px;display:flex;gap:4px;border-bottom:1px solid var(--border)">
      <button class="btn import-tab-btn active" id="import-tab-paste" style="background:none;border:none;border-bottom:2px solid var(--accent, #6a8cff);border-radius:0;padding:10px 12px">Paste JSON</button>
      <button class="btn import-tab-btn" id="import-tab-login" style="background:none;border:none;border-bottom:2px solid transparent;border-radius:0;padding:10px 12px;color:var(--muted)">Log in</button>
    </div>
    <div style="padding:16px;overflow-y:auto">
      <div id="import-pane-paste">
        <p style="color:var(--muted);font-size:12px;margin-bottom:10px">
          Paste a single account JSON object, or a JSON array of objects. Each must include
          <code>accountID</code> — matching accounts are overwritten (merged field-by-field), new ones are created.
        </p>
        <textarea id="import-ta" placeholder='{"accountID": "o_123", "displayName": "Example", ...}' style="width:100%;min-height:320px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);font-family:monospace;font-size:12px;padding:12px;outline:none;resize:vertical"></textarea>
      </div>
      <div id="import-pane-login" style="display:none">
        <p style="color:var(--muted);font-size:12px;margin-bottom:10px">
          Logs in through yeeps-proxy's <code>/data</code> endpoint and imports the returned account.
        </p>
        <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:4px">Oculus ID</label>
        <input id="import-login-oculus" type="text" style="width:100%;margin-bottom:10px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px;padding:8px 10px;outline:none">
        <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:4px">Mobile Code</label>
        <input id="import-login-code" type="text" maxlength="4" style="width:100%;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px;padding:8px 10px;outline:none">
      </div>
    </div>
    <div style="padding:12px 16px;border-top:1px solid var(--border);display:flex;justify-content:flex-end;gap:8px">
      <button class="btn" id="import-cancel-btn" style="background:var(--border);color:var(--text)">Cancel</button>
      <button class="btn btn-primary" id="import-submit-btn">Import</button>
    </div>
  </div>
</div>
<script>
let currentId = null;
let searchTimer = null;
const $ = id => document.getElementById(id);

// ── Toast ──────────────────────────────────────────────────────────────
function toast(msg, ok = true) {
  const el = $('toast');
  el.textContent = msg;
  el.className = 'toast show ' + (ok ? 'ok' : 'err');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('show'), 2500);
}

// ── Mode switch (players / rooms) ──────────────────────────────────────
let listMode = 'players';
function setListMode(mode) {
  listMode = mode;
  const on = 'var(--accent)', off = 'var(--border)';
  $('mode-players').style.background = mode === 'players' ? on : off;
  $('mode-players').style.color = mode === 'players' ? '#fff' : 'var(--text)';
  $('mode-rooms').style.background = mode === 'rooms' ? on : off;
  $('mode-rooms').style.color = mode === 'rooms' ? '#fff' : 'var(--text)';
  $('search').placeholder = mode === 'rooms' ? 'Search room key or name…' : 'Search name or ID…';
  $('search').value = '';
  $('main').innerHTML = '<div class="empty">Select a ' + (mode === 'rooms' ? 'room' : 'player') + ' to edit</div>';
  currentId = null;
  if (mode === 'rooms') loadRoomList(); else loadList();
}

// ── Room list ──────────────────────────────────────────────────────────
async function loadRoomList(q = '') {
  const res = await fetch('/editor/api/rooms?q=' + encodeURIComponent(q) + '&limit=200');
  const roomDocs = await res.json();
  const list = $('player-list');
  list.innerHTML = '';
  if (!roomDocs.length) {
    list.innerHTML = '<div style="padding:16px;color:var(--muted);font-size:13px">No rooms found</div>';
    $('status').textContent = '0 rooms';
    return;
  }
  roomDocs.forEach(r => {
    const div = document.createElement('div');
    div.className = 'player-item' + (r.roomKey === currentId ? ' active' : '');
    div.dataset.id = r.roomKey;
    const creative = Number(r.isCreative) ? 'Creative' : 'Not creative';
    const seg = Number(r.blockMatchmakingSegmentation) ? 'seg blocked' : 'seg on';
    div.innerHTML = `
      <div class="name">${esc(r.roomName || r.roomKey)}</div>
      <div class="id">${esc(r.roomKey)}</div>
      <div class="coins">${creative} &middot; ${seg} &middot; creativeSessionID ${esc(String(r.creativeSessionID))}</div>
    `;
    div.addEventListener('click', () => selectRoom(r.roomKey));
    list.appendChild(div);
  });
  $('status').textContent = roomDocs.length + ' room' + (roomDocs.length !== 1 ? 's' : '');
}

async function selectRoom(key) {
  currentId = key;
  document.querySelectorAll('.player-item').forEach(el => {
    el.classList.toggle('active', el.dataset.id === key);
  });
  const res = await fetch('/editor/api/room/' + encodeURIComponent(key));
  if (!res.ok) { toast('Failed to load room', false); return; }
  renderRoom(await res.json());
}

function renderRoom(doc) {
  const main = $('main');
  main.innerHTML = `
    <div class="player-header">
      <div>
        <h2>${esc(doc.roomName || doc.roomKey)}</h2>
        <div class="acct-id">${esc(doc.roomKey)}</div>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <button class="btn btn-primary" id="save-room-btn">Save Changes</button>
      </div>
    </div>
  `;
  const section = document.createElement('div');
  section.className = 'section';
  section.innerHTML = '<div class="section-title">Raw JSON <span style="color:var(--muted);font-weight:400;font-size:11px">(edit &amp; save overwrites all fields)</span></div>';
  const wrap = document.createElement('div');
  wrap.className = 'raw-wrap';
  const ta = document.createElement('textarea');
  ta.id = 'room-raw-ta';
  ta.value = JSON.stringify(doc, null, 2);
  wrap.appendChild(ta);
  section.appendChild(wrap);
  main.appendChild(section);

  $('save-room-btn').addEventListener('click', () => saveRoom(doc.roomKey));
}

async function saveRoom(key) {
  let patch;
  try {
    patch = JSON.parse($('room-raw-ta').value);
  } catch (e) {
    toast('Invalid JSON', false);
    return;
  }
  delete patch.roomKey;
  const res = await fetch('/editor/api/room/' + encodeURIComponent(key), {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(patch),
  });
  if (res.ok) {
    toast('Saved!');
    loadRoomList($('search').value);
  } else {
    const err = await res.json().catch(() => ({}));
    toast(err.detail || 'Save failed', false);
  }
}

// ── Player list ────────────────────────────────────────────────────────
async function loadList(q = '') {
  const res = await fetch('/editor/api/players?q=' + encodeURIComponent(q) + '&limit=100');
  const players = await res.json();
  const list = $('player-list');
  list.innerHTML = '';
  if (!players.length) {
    list.innerHTML = '<div style="padding:16px;color:var(--muted);font-size:13px">No players found</div>';
    return;
  }
  players.forEach(p => {
    const div = document.createElement('div');
    div.className = 'player-item' + (p.accountID === currentId ? ' active' : '');
    div.dataset.id = p.accountID;
    div.innerHTML = `
      <div class="name">${esc(p.displayName || p.accountID)}</div>
      <div class="id">${esc(p.accountID)}</div>
      <div class="coins">&#9733; ${fmt(p.currency)}  &#8857; ${fmt(p.beans)}</div>
    `;
    div.addEventListener('click', () => selectPlayer(p.accountID));
    list.appendChild(div);
  });
  $('status').textContent = players.length + ' player' + (players.length !== 1 ? 's' : '');
}

// ── Select / load player ───────────────────────────────────────────────
async function selectPlayer(id) {
  currentId = id;
  document.querySelectorAll('.player-item').forEach(el => {
    el.classList.toggle('active', el.dataset.id === id);
  });
  const res = await fetch('/editor/api/player/' + encodeURIComponent(id));
  if (!res.ok) { toast('Failed to load player', false); return; }
  const doc = await res.json();
  renderPlayer(doc);
}

// ── Render editor ──────────────────────────────────────────────────────
let rawMode = true;
function renderPlayer(doc) {
  const main = $('main');

  // Simple scalar fields shown individually
  const SCALAR = ['displayName','currency','beans','eyeColor','skinColor','mobileCode',
    'hasCreatorPack','hasUnlockedPrivateRooms','isIsolated','hasPendingMobileLogin',
    'hasPendingWarning','shouldForceRefresh','isPermabanned','isMutebanned',
    'remainingBanHours','banReason','skipAttestation','firstLogin','matchmakingSegment',
    'cw_nextCanUseFreeFuelTimestamp','loginStreakFreezeTimestamp',
    'lastChallengeRedeemedTime_login','lastChallengeRedeemedTime_easy','lastChallengeRedeemedTime_hard'];

  // Array / object fields shown as JSON textarea
  const JSON_FIELDS = ['ownedPatterns','ownedBundles','activeCosmetics','wishlist','roleKeys',
    'redeemedStashIDs','cw_admin','cw_staff','cw_favorites','analyticEventKeys',
    'loginStreakFreezeHistory','pendingMessages','loginStreakData'];

  function fieldEl(key, val) {
    const wrap = document.createElement('div');
    wrap.className = 'field' + (JSON_FIELDS.includes(key) ? ' wide' : '');
    const lbl = document.createElement('label');
    lbl.textContent = key;
    wrap.appendChild(lbl);

    if (JSON_FIELDS.includes(key)) {
      const ta = document.createElement('textarea');
      ta.dataset.key = key;
      ta.value = JSON.stringify(val, null, 2);
      wrap.appendChild(ta);
    } else {
      const inp = document.createElement('input');
      inp.dataset.key = key;
      inp.type = 'text';
      inp.value = val == null ? '' : String(val);
      wrap.appendChild(inp);
    }
    return wrap;
  }

  main.innerHTML = `
    <div class="player-header">
      <div>
        <h2 id="ph-name">${esc(doc.displayName || doc.accountID)}</h2>
        <div class="acct-id">${esc(doc.accountID)}</div>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <span class="raw-toggle" id="raw-toggle">Raw JSON</span>
        <button class="btn btn-primary" id="save-btn">Save Changes</button>
      </div>
    </div>
  `;

  // Scalar section
  const scalarSection = document.createElement('div');
  scalarSection.className = 'section';
  scalarSection.style.display = rawMode ? 'none' : '';
  scalarSection.innerHTML = '<div class="section-title">Player Fields</div>';
  const grid = document.createElement('div');
  grid.className = 'field-grid';
  SCALAR.forEach(k => {
    if (k in doc) grid.appendChild(fieldEl(k, doc[k]));
  });
  scalarSection.appendChild(grid);
  main.appendChild(scalarSection);

  // JSON fields section
  const jsonSection = document.createElement('div');
  jsonSection.className = 'section';
  jsonSection.style.display = rawMode ? 'none' : '';
  jsonSection.innerHTML = '<div class="section-title">Arrays & Objects</div>';
  const jgrid = document.createElement('div');
  jgrid.className = 'field-grid';
  JSON_FIELDS.forEach(k => {
    if (k in doc) jgrid.appendChild(fieldEl(k, doc[k]));
  });
  jsonSection.appendChild(jgrid);
  main.appendChild(jsonSection);

  // Raw JSON section (shown by default)
  const rawSection = document.createElement('div');
  rawSection.className = 'section';
  rawSection.id = 'raw-section';
  rawSection.style.display = rawMode ? '' : 'none';
  rawSection.innerHTML = '<div class="section-title">Raw JSON <span style="color:var(--muted);font-weight:400;font-size:11px">(edit & save overwrites all fields)</span></div>';
  const rawWrap = document.createElement('div');
  rawWrap.className = 'raw-wrap';
  const rawTa = document.createElement('textarea');
  rawTa.id = 'raw-ta';
  rawTa.value = JSON.stringify(doc, null, 2);
  rawWrap.appendChild(rawTa);
  rawSection.appendChild(rawWrap);
  main.appendChild(rawSection);

  $('raw-toggle').textContent = rawMode ? 'Structured' : 'Raw JSON';
  $('raw-toggle').addEventListener('click', () => {
    rawMode = !rawMode;
    rawSection.style.display = rawMode ? '' : 'none';
    scalarSection.style.display = rawMode ? 'none' : '';
    jsonSection.style.display = rawMode ? 'none' : '';
    $('raw-toggle').textContent = rawMode ? 'Structured' : 'Raw JSON';
  });

  $('save-btn').addEventListener('click', () => savePlayer(doc.accountID));
}

// ── Save ───────────────────────────────────────────────────────────────
async function savePlayer(id) {
  let patch = {};

  if (rawMode) {
    // Raw mode: send entire document except accountID
    try {
      const raw = JSON.parse($('raw-ta').value);
      raw.accountID = id; // ensure key preserved
      // Send full overwrite as patch
      Object.assign(patch, raw);
      delete patch.accountID;
    } catch(e) {
      toast('Invalid JSON in raw editor', false);
      return;
    }
  } else {
    // Structured mode: collect all inputs/textareas
    document.querySelectorAll('[data-key]').forEach(el => {
      const key = el.dataset.key;
      let val = el.value.trim();
      if (el.tagName === 'TEXTAREA') {
        try { val = JSON.parse(val); } catch(e) {
          toast('Invalid JSON in field: ' + key, false);
          throw e;
        }
      } else {
        // Try to coerce numbers
        if (val !== '' && !isNaN(val) && val !== '') val = Number(val);
      }
      patch[key] = val;
    });
  }

  const res = await fetch('/editor/api/player/' + encodeURIComponent(id), {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(patch),
  });
  if (res.ok) {
    toast('Saved!');
    // Update name in header if changed
    if (patch.displayName) {
      const h2 = document.getElementById('ph-name');
      if (h2) h2.textContent = patch.displayName;
    }
    // Refresh list entry
    loadList($('search').value);
  } else {
    const err = await res.json().catch(() => ({}));
    toast((err.detail || 'Save failed'), false);
  }
}

// ── Utils ──────────────────────────────────────────────────────────────
function esc(s) { return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function fmt(n) { return n == null ? '?' : Number(n).toLocaleString(); }

// ── Import ─────────────────────────────────────────────────────────────
let importMode = 'paste';
function openImport() {
  $('import-overlay').style.display = 'flex';
  $('import-ta').value = '';
  $('import-login-oculus').value = '';
  $('import-login-code').value = '';
  setImportMode('paste');
  $('import-ta').focus();
}
function closeImport() {
  $('import-overlay').style.display = 'none';
}
function setImportMode(mode) {
  importMode = mode;
  $('import-pane-paste').style.display = mode === 'paste' ? '' : 'none';
  $('import-pane-login').style.display = mode === 'login' ? '' : 'none';
  $('import-tab-paste').style.borderBottomColor = mode === 'paste' ? 'var(--accent, #6a8cff)' : 'transparent';
  $('import-tab-paste').style.color = mode === 'paste' ? '' : 'var(--muted)';
  $('import-tab-login').style.borderBottomColor = mode === 'login' ? 'var(--accent, #6a8cff)' : 'transparent';
  $('import-tab-login').style.color = mode === 'login' ? '' : 'var(--muted)';
}
async function submitImport() {
  if (importMode === 'login') {
    const oculusID = $('import-login-oculus').value.trim();
    const mobileCode = $('import-login-code').value.trim();
    if (!oculusID || !mobileCode) {
      toast('Oculus ID and mobile code are required', false);
      return;
    }
    const res = await fetch('/editor/api/import_login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({oculusID, mobileCode}),
    });
    const result = await res.json().catch(() => ({}));
    if (res.ok) {
      toast(`Imported ${result.imported[0]}`);
      closeImport();
      loadList($('search').value);
    } else {
      toast(result.detail || 'Import failed', false);
    }
    return;
  }

  let payload;
  try {
    payload = JSON.parse($('import-ta').value);
  } catch (e) {
    toast('Invalid JSON', false);
    return;
  }
  const res = await fetch('/editor/api/import', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  const result = await res.json().catch(() => ({}));
  if (res.ok) {
    const n = (result.imported || []).length;
    const errN = (result.errors || []).length;
    toast(`Imported ${n} account${n !== 1 ? 's' : ''}` + (errN ? ` (${errN} error${errN !== 1 ? 's' : ''})` : ''), errN === 0);
    if (errN) console.warn('Import errors:', result.errors);
    closeImport();
    loadList($('search').value);
  } else {
    toast(result.detail || 'Import failed', false);
  }
}
$('import-open-btn').addEventListener('click', openImport);
$('import-close-btn').addEventListener('click', closeImport);
$('import-cancel-btn').addEventListener('click', closeImport);
$('import-submit-btn').addEventListener('click', submitImport);
$('import-tab-paste').addEventListener('click', () => setImportMode('paste'));
$('import-tab-login').addEventListener('click', () => setImportMode('login'));
$('import-overlay').addEventListener('click', (e) => { if (e.target.id === 'import-overlay') closeImport(); });

// ── Init ───────────────────────────────────────────────────────────────
$('mode-players').addEventListener('click', () => setListMode('players'));
$('mode-rooms').addEventListener('click', () => setListMode('rooms'));
$('search').addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    if (listMode === 'rooms') loadRoomList($('search').value);
    else loadList($('search').value);
  }, 250);
});

loadList();
</script>
</body>
</html>"""


# Both "" and "/" so /editor works without the trailing slash -- main.py's
# catch-all would otherwise swallow the un-slashed form before Starlette
# could redirect it.
_LOGIN_HTML = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Yeeps Editor — Sign in</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Lilita+One&family=Quicksand:wght@500;600;700&display=swap" rel="stylesheet">
<style>
:root{color-scheme:dark;
  --bg:#1d1915;--bg2:#262019;--card:#2d2720;--ink:#f1e8d9;--muted:#a8997f;
  --line:#453c31;--red:#e26a6a;--purple:#8c7bc9}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;
  font-family:'Quicksand',system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
.card{width:min(94vw,400px);background:var(--card);border:1px solid var(--line);border-radius:20px;
  padding:30px 26px;box-shadow:0 2px 0 rgba(74,63,53,.05),0 14px 40px rgba(74,63,53,.12)}
.logo{width:46px;height:46px;border-radius:13px;background:var(--purple);display:grid;place-items:center;
  color:#fff;font-family:'Lilita One';font-size:27px;margin-bottom:14px;box-shadow:0 2px 0 rgba(74,63,53,.08)}
h1{margin:0 0 3px;font-size:22px;font-family:'Lilita One';font-weight:400;color:var(--ink)}
p.sub{margin:0 0 20px;color:var(--muted);font-size:13px;line-height:1.5}
label{display:block;font-size:12px;font-weight:700;color:var(--muted);letter-spacing:.3px;margin:14px 0 6px 2px}
input{width:100%;padding:12px 13px;border-radius:12px;border:1px solid var(--line);background:var(--card);
  color:var(--ink);font-size:15px;font-family:'Quicksand';font-weight:600}
input:focus{outline:none;border-color:var(--purple);box-shadow:0 0 0 3px rgba(115,95,176,.14)}
button{width:100%;margin-top:20px;padding:13px;border:0;border-radius:13px;background:var(--red);color:#fff;
  font-size:15px;font-weight:700;font-family:'Quicksand';cursor:pointer;transition:.14s}
button:hover{filter:brightness(1.04)}
button:active{transform:scale(.985)}
button:disabled{opacity:.6;cursor:default}
.err{margin-top:14px;color:#c0392b;font-size:13px;font-weight:600;min-height:18px}
</style></head><body>
<form class="card" id="f">
  <div class="logo">Y</div>
  <h1>Yeeps Data Editor</h1>
  <p class="sub">Sign in to edit account &amp; room data.</p>
  <label>Yeeps or Discord name</label>
  <input id="name" autocomplete="username" placeholder="in-game name or Discord username" required>
  <label>Password</label>
  <input id="pw" type="password" autocomplete="current-password" placeholder="password" required>
  <button id="go" type="submit">Sign in</button>
  <div class="err" id="err"></div>
  <p class="sub" style="margin:16px 0 0">On iPhone? Turn off <b>iCloud Private Relay</b> first
  (Settings, your name, iCloud, Private Relay), and any VPN or 1.1.1.1 / WARP app. The editor doesn't work through them.</p>
</form>
<script>
const f=document.getElementById('f'),err=document.getElementById('err'),go=document.getElementById('go');
f.addEventListener('submit',async e=>{
  e.preventDefault();err.textContent='';go.disabled=true;go.textContent='Checking…';
  try{
    const r=await fetch('/editor/login',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:document.getElementById('name').value,password:document.getElementById('pw').value})});
    const j=await r.json();
    if(r.ok&&j.ok){location.href='/editor/';return;}
    err.textContent=j.error||'Sign in failed.';
  }catch(_){err.textContent='Network error.';}
  go.disabled=false;go.textContent='Sign in';
});
</script></body></html>"""


_VERIFY_HTML = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Yeeps Data Checker</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;justify-content:center;padding:32px 16px;
  font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0d1117;color:#e6edf3}
.wrap{width:min(100%,520px)}
.card{background:#161b22;border:1px solid #30363d;border-radius:14px;padding:24px;margin-bottom:16px}
h1{margin:0 0 4px;font-size:20px}
p.sub{margin:0 0 16px;color:#8b949e;font-size:13px;line-height:1.45}
label{display:block;font-size:12px;color:#8b949e;margin:12px 0 6px}
input{width:100%;padding:11px 12px;border-radius:9px;border:1px solid #30363d;background:#0d1117;color:#e6edf3;font-size:15px}
input:focus{outline:none;border-color:#2f81f7}
button{width:100%;margin-top:18px;padding:12px;border:0;border-radius:9px;background:#238636;color:#fff;font-size:15px;font-weight:600;cursor:pointer}
button:disabled{opacity:.6;cursor:default}
.err{margin-top:12px;color:#f85149;font-size:13px;min-height:18px}
table{width:100%;border-collapse:collapse;font-size:14px}
td{padding:7px 4px;border-bottom:1px solid #21262d;vertical-align:top;word-break:break-word}
td:first-child{color:#8b949e;width:42%}
details{margin-top:14px}summary{cursor:pointer;color:#8b949e;font-size:13px}
pre{white-space:pre-wrap;word-break:break-word;font-size:12px;background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:10px;max-height:420px;overflow:auto}
.top{display:flex;justify-content:space-between;align-items:baseline}
a{color:#8b949e;font-size:13px}
</style></head><body><div class="wrap">
<form class="card" id="f">
  <div class="top"><h1>Data Checker</h1><a href="/editor/verify/logout">Sign out</a></div>
  <p class="sub">Shows a player's real Yeeps account info. Needs their Meta username and their 4-digit
  mobile code (in game: Settings, then Mobile). Each check uses one of their code attempts, so only check
  with the player's OK.</p>
  <label>Meta username</label><input id="u" autocomplete="off" required>
  <label>Mobile code</label><input id="c" inputmode="numeric" maxlength="4" autocomplete="off" required>
  <button id="go" type="submit">Check</button>
  <div class="err" id="err"></div>
</form>
<div class="card" id="out" hidden>
  <h1 id="oname"></h1><p class="sub" id="oid"></p>
  <table id="t"></table>
  <details><summary>All account data</summary><pre id="raw"></pre></details>
</div></div>
<script>
const $=id=>document.getElementById(id);
const SHOW=[["firstLogin","First login"],["lastLogin","Last login"],["sessionCount","Sessions"],
  ["hoursSpent","Hours played"],["displayName","Display name"],["currency","Coins"],["beans","Beans"],
  ["roleKeys","Roles"],["banCount","Times banned"],["isPermabanned","Permabanned"],["banReason","Ban reason"],
  ["warnCount","Warnings"],["hasCreatorPack","Creator pack"]];
const fmt=v=>Array.isArray(v)?(v.length?v.join(", "):"(none)"):(v===null||v===undefined||v===""?"-":String(v));
$("f").addEventListener("submit",async e=>{
  e.preventDefault();$("err").textContent="";$("go").disabled=true;$("go").textContent="Checking...";
  try{
    const r=await fetch("/editor/verify/check",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({username:$("u").value,code:$("c").value})});
    const j=await r.json();
    if(!r.ok||!j.ok){$("err").textContent=j.error||j.detail||"Check failed.";}
    else{
      const st=j.stats||{};$("oname").textContent=j.oculusID||"";$("oid").textContent="Account "+(j.accountID||"?");
      const t=$("t");t.textContent="";
      for(const [k,lab] of SHOW){ if(!(k in st)) continue;
        const tr=document.createElement("tr"),a=document.createElement("td"),b=document.createElement("td");
        a.textContent=lab;b.textContent=fmt(st[k]);tr.append(a,b);t.append(tr);}
      $("raw").textContent=JSON.stringify(st,null,2);$("out").hidden=false;$("c").value="";}
  }catch(_){$("err").textContent="Network error.";}
  $("go").disabled=false;$("go").textContent="Check";
});
</script></body></html>"""


@router.get("/login", response_class=HTMLResponse)
async def editor_login_page(request: Request):
    # Loading the login page posts the current password if it just rotated, so an
    # admin coming to sign in always has the latest code waiting in the webhook.
    await _maybe_notify_new_password()
    if await _usable_session(request):
        return RedirectResponse("./", status_code=302)
    resp = HTMLResponse(content=_LOGIN_HTML)
    if _authed_name(request):
        # An owner session from an untrusted place: drop it, or the page and the
        # login form would keep sending the browser back and forth.
        resp.delete_cookie(_COOKIE)
    return resp


@router.post("/login")
async def editor_login(request: Request):
    if EDITOR_LOCKED:
        return JSONResponse({"ok": False, "error": "The editor is temporarily locked."}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = str(body.get("name", "")).strip()
    pw = str(body.get("password", ""))
    from services.ip_bans import client_ip, record_failed_login, clear_failures
    ip = client_ip(request) or "?"
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    info = _ip_info(ip)

    # No VPNs / relays / hosting in the editor (owner's rule, 2026-10-10: the
    # raiders only ever came in through VPNs).
    if not info.get("vpn"):
        from services import geo as _geo
        info["vpn"] = await _geo.vpn_check(ip)
    if EDITOR_BLOCK_VPN and info.get("vpn"):
        await _log_editor_ip("vpn-refused", name, None, info)
        await _discord_notify(f"BLOCKED editor login as **{name or 'someone'}** from a VPN "
                              f"({info['vpn']}) [{ip}] ({_where(info)})")
        return JSONResponse({"ok": False, "error": "Turn off your VPN to use the editor. On iPhone that includes "
                                                   "iCloud Private Relay: Settings > your name > iCloud > Private "
                                                   "Relay > off (or Turn Off Until Tomorrow). Also turn off any "
                                                   "1.1.1.1 / WARP app."}, status_code=403)

    # Password "verify" opens only the data checker, and only for someone with the
    # "Verify Support" Discord role (owner, 2026-10-11).
    if pw.strip().lower() == "verify":
        from services.discord_roles import has_role as _has_role
        _vcands = await _name_matches(name)
        _allowed = False
        for _c in _vcands:
            if _c.get("discordID") and await _has_role(_c["discordID"], VERIFY_SUPPORT_ROLE):
                _allowed = True
                break
        if not _allowed:
            await _log_editor_ip("verify-refused", name, None, info)
            return JSONResponse({"ok": False, "error": f"The data checker is for the '{VERIFY_SUPPORT_ROLE}' "
                                                       "role only."}, status_code=403)
        resp = JSONResponse({"ok": True})
        resp.set_cookie(_VERIFY_COOKIE, _verify_token(), max_age=_VERIFY_TTL, httponly=True,
                        samesite="lax", secure=True)
        await _log_editor_ip("verify-login", name, None, info)
        await _discord_notify(f"Data checker opened ({name or 'no name'}) [{ip}] ({_where(info)})")
        return resp

    # The owner's name: own password + owner's network or trusted device only.
    owner_acct = await _owner_account_for(name)
    if owner_acct:
        on_net = await _on_owner_network(request, owner_acct)
        if EDITOR_OWNER_DEVICE_RULE and not (on_net or _owner_device_ok(request)):
            await _log_editor_ip("owner-untrusted", name, owner_acct, info)
            await _discord_notify(
                f"BLOCKED editor login as the owner (**{name}**) from a network/device that isn't "
                f"the owner's [{ip}] ({_where(info)})")
            return JSONResponse({"ok": False, "error": "This name can only sign in from its owner's own devices."},
                                status_code=403)
        if not _owner_pw_ok(pw):
            banned_now = await record_failed_login(ip)
            await _log_editor_ip("wrong-code", name, owner_acct, info, autoBanned=bool(banned_now))
            await _discord_notify(f"Owner editor login as **{name}** failed (wrong password) [{ip}] ({_where(info)})")
            return JSONResponse({"ok": False, "error": "Wrong password."}, status_code=401)
        clear_failures(ip)
        resp = JSONResponse({"ok": True})
        token = _sign(name, ip)
        resp.set_cookie(_COOKIE, token, max_age=_TTL, httponly=True, samesite="lax", secure=True)
        if on_net:   # remember this browser so it also works away from home
            resp.set_cookie(_OWNER_DEVICE_COOKIE, _owner_device_token(owner_acct), max_age=_OWNER_DEVICE_TTL,
                            httponly=True, samesite="lax", secure=True)
        await _log_editor_ip("login", name, owner_acct, info, via="owner", playsHere=on_net,
                             sessionHash=_session_hash(token), tier="owner")
        await _discord_notify(f"**{name}** (owner, `{owner_acct}`) logged into the editor [{ip}] ({_where(info)})"
                              + ("" if on_net else " from a trusted device"))
        return resp

    pw_ok = hmac.compare_digest(pw, _editor_password()) or (
        bool(_MASTER_PW) and hmac.compare_digest(pw, _MASTER_PW))
    if not pw_ok:
        # Auto-ban an IP that spams wrong passwords (brute-force / raider).
        banned_now = await record_failed_login(ip)
        await _log_editor_ip("wrong-code", name, None, info, autoBanned=bool(banned_now), typed=_typed_view(pw))
        if banned_now:
            await _discord_notify(
                f"Auto-banned IP {ip} ({_where(info)}) - too many wrong editor passwords "
                f"(last tried as '{name or 'someone'}')"
            )
            return JSONResponse(
                {"ok": False, "error": "Too many failed attempts. Your IP has been banned."},
                status_code=403,
            )
        await _discord_notify(f"{name or 'someone'} failed to log into the editor (wrong password) [{ip}] ({_where(info)})")
        return JSONResponse({"ok": False, "error": "Wrong password."}, status_code=401)

    # Username gate: the password alone is NOT enough. The typed name must map to
    # a real account, and (when EDITOR_ADMINS is set) that account must be on the
    # admin allowlist. This blocks "password only" logins where the name is blank
    # or unknown — the password + IP auto-ban is no longer the sole protection.
    if not name:
        await _log_editor_ip("no-name", "", None, info)
        await _discord_notify(f"BLOCKED editor login: no username given [{ip}] ({_where(info)})")
        return JSONResponse({"ok": False, "error": "Enter your in-game username."}, status_code=401)
    # The typed name can be an in-game name, Meta name, Discord username or
    # account id (2026-10-11). Several accounts can match (names aren't unique,
    # one Discord can link several accounts): use one whose Discord has the
    # Data Manager role, preferring the one this device/network belongs to.
    cands = await _name_matches(name)
    doc = cands[0] if cands else None
    acct_id = doc.get("accountID") if doc else None
    disc_id = doc.get("discordID") if doc else None

    # Authorised to use the editor if EITHER:
    #  (a) the account is a hardcoded super-admin (EDITOR_ADMINS), or
    #  (b) its linked Discord account has the "data manager" role.
    allowed = bool(acct_id and acct_id in EDITOR_ADMINS)
    how = "admin"
    if not allowed and cands:
        from services.discord_roles import has_editor_role
        ok_roles, authed = {}, []
        for c in cands:
            did = c.get("discordID")
            if did and did not in ok_roles:
                ok_roles[did] = await has_editor_role(did) or await has_role(did, EDITOR_EXTRA_ROLE)
            if did and ok_roles.get(did):
                authed.append(c)
        if authed:
            pick = None
            for c in authed:
                if _device_ok_for(request, c["accountID"]) or await _on_owner_network(request, c["accountID"]):
                    pick = c
                    break
            pick = pick or next((c for c in authed if c.get("deviceKeys")), authed[0])
            doc, acct_id, disc_id = pick, pick.get("accountID"), pick.get("discordID")
            allowed, how = True, "data-manager role"
    if not allowed and EDITOR_ADMINS:
        # Duplicate displayName: check for a super-admin account matching the name.
        esc = re.escape(name)
        admin_doc = await accounts.find_one(
            {"accountID": {"$in": list(EDITOR_ADMINS)},
             "$or": [{"displayName": {"$regex": f"^{esc}$", "$options": "i"}},
                     {"oculusID": {"$regex": f"^{esc}$", "$options": "i"}},
                     {"accountID": name}]},
            {"_id": 0, "accountID": 1, "displayName": 1})
        if admin_doc:
            doc, acct_id, allowed = admin_doc, admin_doc.get("accountID"), True

    if allowed and EDITOR_OWNER_ONLY:
        await _log_editor_ip("owner-only", name, acct_id, info)
        await _discord_notify(f"BLOCKED editor login as **{name}**: the editor is owner-only right now "
                              f"[{ip}] ({_where(info)})")
        return JSONResponse({"ok": False, "error": "The 1.31 editor is owner-only right now. Data managers: pick the Live tab above, or go to https://your-domain.example/editor/live"}, status_code=403)
    if not allowed:
        await _log_editor_ip("not-authorised", name, acct_id, info)
        await _discord_notify(
            f"BLOCKED editor login: **{name}** isn't an admin or a data manager "
            f"(linked Discord: `{disc_id or 'none'}`) [{ip}] ({_where(info)})")
        return JSONResponse(
            {"ok": False,
             "error": "You're not authorised. You need the Data Manager role and to have "
                      "linked your account with /link first."},
            status_code=403)

    # Same rule as the owner (2026-10-11): the account must have its own headset
    # bound, and sign in from the network that headset plays from (or a device
    # that did before), so a leaked code alone gets nobody in.
    _full = await accounts.find_one({"accountID": acct_id}, {"_id": 0, "deviceKeys": 1}) if acct_id else None
    on_net = bool(acct_id) and await _on_owner_network(request, acct_id)
    # Owner 2026-10-18: no home Wi-Fi rule any more (Data Managers couldn't sign in
    # away from home); a bound headset is still required, VPNs still refused.
    if not (_full or {}).get("deviceKeys"):
        await _log_editor_ip("untrusted-place", name, acct_id, info)
        await _discord_notify(f"BLOCKED editor login as **{name}**: no headset bound to that account "
                              f"[{ip}] ({_where(info)})")
        return JSONResponse({"ok": False, "error": "Your account needs its own headset: play Yeeps once on it, "
                                                   "then sign in here."},
                            status_code=403)

    label = (doc.get("displayName") if doc else None) or name
    clear_failures(ip)
    resp = JSONResponse({"ok": True})
    token = _sign(name or label, ip)
    resp.set_cookie(_COOKIE, token, max_age=_TTL, httponly=True, samesite="lax")
    if on_net:
        resp.set_cookie(_OWNER_DEVICE_COOKIE, _owner_device_token(acct_id), max_age=_OWNER_DEVICE_TTL,
                        httponly=True, samesite="lax", secure=True)
    # Any network is allowed, but say so loudly when it isn't one this account
    # plays Yeeps from (the leaked-code intrusion on Oct 9 came from elsewhere).
    known = await _play_ips(acct_id)
    plays_here = ip in known or info["net"] in {_ip_net(x) for x in known}
    tier = "editor"
    if disc_id:
        from services.discord_roles import has_role
        if (disc_id in EDITOR_ADMIN_DISCORD_IDS or await has_role(disc_id, ADMIN_ROLE_NAME)
                or await has_role(disc_id, EDITOR_EXTRA_ROLE)):
            tier = "admin"
    await _log_editor_ip("login", label, acct_id, info, via=how, playsHere=plays_here,
                         sessionHash=_session_hash(token), tier=tier)
    await _discord_notify(f"**{label}** (accountID: `{acct_id}`, via {how}, "
                          f"{'admin' if tier == 'admin' else 'data editor'}) logged into the editor "
                          f"[{ip}] ({_where(info)})")
    if not plays_here:
        km = _km_from_play(ip, known)
        far = f", ~{km} km from where this account plays" if km is not None else ""
        await _discord_notify(
            f"EDITOR ALERT: **{label}** signed into the editor from a different Wi-Fi/IP "
            f"[{ip}] ({_where(info)}{far}). That account doesn't play Yeeps from this network. "
            f"If this wasn't them, lock the editor or rotate the code.")
    return resp


@router.get("/logout")
async def editor_logout():
    resp = RedirectResponse("login", status_code=302)
    resp.delete_cookie(_COOKIE)
    return resp


_UI_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "editor_ui.html")


_NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
             "Pragma": "no-cache", "Expires": "0"}


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def editor_ui(request: Request):
    if not await _usable_session(request) and _verify_ok(request):
        return HTMLResponse(content=_VERIFY_HTML, headers=_NO_CACHE)
    if not await _usable_session(request):
        resp = HTMLResponse(content=_LOGIN_HTML, headers=_NO_CACHE)
        if _authed_name(request):
            resp.delete_cookie(_COOKIE)
        return resp
    try:
        with open(_UI_PATH, "r", encoding="utf-8") as f:
            html = f.read()
        # Restricted tabs (owner 2026-10-30: inspect element could unhide Photon): cut out of the
        # page for anyone not allowed, not only hidden. Their APIs check the same rights anyway.
        if not await _photon_ok(request):
            for k in ("photon", "talk"):
                html = re.sub(rf"<!--@cut {k}-->.*?<!--@end {k}-->", "", html, flags=re.S)
                html = re.sub(rf"/\*@cut {k}\*/.*?/\*@end {k}\*/", "", html, flags=re.S)
        return HTMLResponse(content=html, headers=_NO_CACHE)
    except FileNotFoundError:
        # Fall back to the legacy inline UI if the new static file is missing.
        return HTMLResponse(content=_HTML, headers=_NO_CACHE)


if __name__ == "__main__":
    editor_app.include_router(router)
    uvicorn.run("routers.editor_router:editor_app", host="0.0.0.0", port=8001, reload=True)
