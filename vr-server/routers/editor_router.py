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
# The rotating code is only 6 digits AND is shared with the 1.31 editor (it gets
# posted to a Discord channel), so it can be seen/leaked rather than guessed
# (2026-10-27 intrusion: someone used the visible 6-digit code, not brute force).
# When EDITOR_DISABLE_ROTATING_PW=1, ONLY the strong fixed code (EDITOR_PW) is
# accepted on this editor, closing that leak path.
_ROTATING_PW_OFF = os.getenv("EDITOR_DISABLE_ROTATING_PW", "").strip() == "1"


def _pw_window() -> int:
    return int(time.time() // EDITOR_PW_ROTATE_SECONDS)


def _editor_password(window: int = None) -> str:
    w = _pw_window() if window is None else window
    # EDITOR_PW_SECRET (2.31.0-blaze instance): derive the code from the 1.31 server's
    # secret so both editors share one password; cookies stay signed separately.
    key = os.getenv("EDITOR_PW_SECRET") or EDITOR_SECRET
    # Match the 1.31 editor exactly (same code on both): key = EDITOR_PW_SECRET
    # (set to 1.31's EDITOR_SECRET) and the salt in the message, so hmac matches.
    _salt = os.getenv("EDITOR_PW_SALT", "")
    msg = f"editorpw|{w}" + (f"|{_salt}" if _salt else "")
    mac = hmac.new(key.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{int(mac[:8], 16) % 1000000:06d}"

# Admin allowlist: ONLY these accountIDs may open the editor, even with the right
# password. Empty set = no restriction. Override via env EDITOR_ADMINS (comma-sep).
# Owner-only: when "1" (default), ONLY EDITOR_ADMINS accounts may sign in — the
# Discord "Data Manager" role bypass is disabled (owner 2026-10-19: non-owners like
# a player were getting into the LIVE editor via the role). Set EDITOR_OWNER_ONLY=0
# in the env to re-allow data-manager-role accounts.
EDITOR_OWNER_ONLY = (os.getenv("EDITOR_OWNER_ONLY", "1").strip() != "0")
EDITOR_ADMINS = set(filter(None, (os.getenv("EDITOR_ADMINS") or
    "o_OWNER0000000001").replace(" ", "").split(",")))
# Moderators: not full admins, but may ban / unban / block players (and with a
# permanent ban, IP-ban them). Owner 2026-11-02: staff_b.
EDITOR_MODERATORS = set(filter(None, (os.getenv("EDITOR_MODERATORS") or
    "o_STAFF00000000004").replace(" ", "").split(",")))
# Extra Meta-name -> accountID aliases for the editor (owner 2026-10-19: "yes
# StaffAlias is a staff member, make them full admin"). A name here resolves to the given
# account in _name_exists / roleAdmin even though that account's game displayName
# and oculusID differ, so their in-game login identity is left untouched.
# Env EDITOR_NAME_ALIASES = "AliasName=o_123,Other=o_456" (case-insensitive name).
EDITOR_NAME_ALIASES = {}
for _pair in (os.getenv("EDITOR_NAME_ALIASES") or "StaffAlias=o_STAFF00000000002").split(","):
    if "=" in _pair:
        _a, _acct = _pair.split("=", 1)
        if _a.strip() and _acct.strip():
            EDITOR_NAME_ALIASES[_a.strip().lower()] = _acct.strip()
# Only these editor usernames may SEE player IPs (lastIP/recentIPs and the IP-ban
# list). Player IPs are sensitive, so for everyone else they're stripped/masked.
# Override/extend via env IP_VIEW_NAMES (comma-separated display names).
IP_VIEW_NAMES = set(filter(None, (n.strip() for n in
    (os.getenv("IP_VIEW_NAMES") or
     "OwnerName,o_OWNER0000000001,"
     "Staff Member A,StaffAlias,o_STAFF00000000002").split(","))))
# Lowercased for case-insensitive matching (owner 2026-10-19: signed in as
# "ownername" and the Banned-tab IP-unban tool was hidden because the match
# was exact-case).
_IP_VIEW_LC = {x.lower() for x in IP_VIEW_NAMES}
EDITOR_SECRET          = os.getenv("EDITOR_SECRET", "yeeps-editor-secret-change-me")
# Discord audit webhooks. Editor login + player/data edits go to the ADMIN
# channel; in-game activity goes to the INGAME channel. Both live in config so
# they can be overridden by env and shared with the other routers.
from config import ADMIN_DISCORD_WEBHOOK, INGAME_DISCORD_WEBHOOK
# Back-compat: an explicit EDITOR_DISCORD_WEBHOOK env still wins if set.
EDITOR_DISCORD_WEBHOOK = os.getenv("EDITOR_DISCORD_WEBHOOK", ADMIN_DISCORD_WEBHOOK)
_COOKIE = "yeeps_editor"
_TTL = 12 * 3600  # 12-hour sessions
_SESSION_CUTOFF = int(os.getenv("EDITOR_SESSION_CUTOFF", "0") or 0)  # boot older sessions


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
# Audit trail of every mutating editor action (who / ip / what / when), so the
# owner can see e.g. who sent an announcement or drove the Photon bot. Capped by
# the daily cleanup (CAP_COLLS). Viewable at /actions and GET /api/actions.
editor_actions = _db["editor_actions"]
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
    # 2.31.0-blaze: the same accounts' networks on the 1.31 server count too (read-only)
    if os.getenv("CARRYOVER_DB"):
        try:
            from services.db import client as _mc
            async for a in _mc[os.getenv("CARRYOVER_DB")]["accounts"].find(
                    {"accountID": {"$in": [x for x in ids if x]}}, {"_id": 0, "lastIP": 1, "recentIPs": 1}):
                known.update(filter(None, [a.get("lastIP")] + list(a.get("recentIPs") or [])))
        except Exception as _e:
            print(f"[editor] 1.31 IP lookup failed: {_e}")
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


def _audit_summary(body) -> str:
    """One short, safe field value from a request body for the audit log."""
    if not isinstance(body, dict):
        return ""
    for k in ("text", "message", "cmd", "command", "reason", "role", "roleKey",
              "build", "requireBuild", "record", "name", "username"):
        v = body.get(k)
        if v not in (None, "", []):
            return f"{k}={str(v)[:140]}"
    # Otherwise the body's simple fields (e.g. View tab: op=put x=.. pattern=..),
    # never anything that looks like a secret.
    parts = [f"{k}={v}" for k, v in body.items()
             if isinstance(v, (str, int, float, bool)) and str(v) != ""
             and not re.search(r"pass|code|token|secret|key", str(k), re.I)][:8]
    return " ".join(parts)[:140]


# Durable on-disk copy of the audit trail. Mongo can block writes when the shared
# Atlas cluster is over quota, so every action is ALSO appended here (append-only
# JSONL, trimmed by size) and the log page falls back to it if Mongo is empty.
_ACTIONS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "editor_actions.jsonl")
_ACTIONS_FILE_MAX = 4 << 20   # 4 MB; trim to newest half when exceeded


def _append_action_file(rec: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_ACTIONS_FILE), exist_ok=True)
        with open(_ACTIONS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        if os.path.getsize(_ACTIONS_FILE) > _ACTIONS_FILE_MAX:
            with open(_ACTIONS_FILE, "rb") as f:
                f.seek(-_ACTIONS_FILE_MAX // 2, 2)
                keep = f.read()
            keep = keep[keep.find(b"\n") + 1:]
            with open(_ACTIONS_FILE, "wb") as f:
                f.write(keep)
    except Exception as e:
        print(f"[editor] action file write failed: {e}")


def _read_action_file(limit: int) -> list:
    try:
        with open(_ACTIONS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
        out = []
        for ln in reversed(lines):
            ln = ln.strip()
            if ln:
                try:
                    out.append(json.loads(ln))
                except Exception:
                    pass
        return out
    except Exception:
        return []


async def _log_action(request: Request, name: str, detail: str = "") -> None:
    from services.ip_bans import client_ip
    rec = {"at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
           "name": name or "?", "ip": client_ip(request) or "?",
           "method": request.method, "path": request.url.path, "detail": detail}
    _append_action_file(rec)     # durable, survives a Mongo quota block
    try:
        await editor_actions.insert_one(dict(rec))
    except Exception as e:
        print(f"[editor] action db log failed (kept on disk): {e}")


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


def _from_vpn(request: Request) -> str:
    """VPN / relay / datacenter label for the caller's IP ("" = none). Owner's
    rule after the Oct 10 raid: the editor is never usable through a VPN."""
    try:
        from services.ip_bans import client_ip
        from services import geo
        return geo.vpn_hint(client_ip(request) or "") or ""
    except Exception:
        return ""


def _authed_name(request: Request):
    """Return the Meta name from a valid signed cookie, else None."""
    if EDITOR_LOCKED:
        return None
    if _from_vpn(request):
        return None   # no editor use from a VPN, even with a valid session
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
            return None  # signed in before the cutoff -> must sign in again
        if int(w) != _pw_window():
            return None  # password rotated since login -> booted, must re-sign-in
        if net is not None:
            _check_session_net(request, name, mac, net)
        return name
    except Exception:
        return None


def _can_see_ips(request: Request) -> bool:
    """True only for editor users allowed to view player IPs (IP_VIEW_NAMES).
    Case-insensitive, and an aliased admin name (e.g. StaffAlias->Staff Member A) counts."""
    n = (_authed_name(request) or "").strip().lower()
    if not n:
        return False
    if n in _IP_VIEW_LC:
        return True
    alias = EDITOR_NAME_ALIASES.get(n)
    return bool(alias and alias in EDITOR_ADMINS)


async def _name_exists(name: str):
    """Look the Meta name up in the accounts collection (displayName / oculusID /
    accountID, case-insensitive). Returns the matched doc or None."""
    n = (name or "").strip()
    if not n:
        return None
    alias = EDITOR_NAME_ALIASES.get(n.lower())
    if alias:
        doc = await accounts.find_one(
            {"accountID": alias},
            {"_id": 0, "accountID": 1, "displayName": 1, "oculusID": 1, "discordID": 1})
        if doc:
            return doc
    esc = re.escape(n)
    return await accounts.find_one(
        {"$or": [
            {"displayName": {"$regex": f"^{esc}$", "$options": "i"}},
            {"oculusID": {"$regex": f"^{esc}$", "$options": "i"}},
            {"accountID": n},
        ]},
        {"_id": 0, "accountID": 1, "displayName": 1, "oculusID": 1, "discordID": 1},
    )


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
    """ADMIN channel: editor login + player/data edits (+ /link via discord_router).
    _post_webhook already prefixes INSTANCE_LABEL, so the channel shared with 1.31
    says which editor it is."""
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
    if not EDITOR_PW_WEBHOOK:
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


# Legit editor Hosts. The editor is only ever reached through Cloudflare on these
# names; a request arriving with any other Host (e.g. a reverse proxy connecting to
# 127.0.0.1:8240 with Host 127.0.0.1) is refused BEFORE auth — defence against a
# proxy/SSRF being able to reach the editor API at all (2026-10-19 incident: a proxy
# forwarded to loopback and an attacker got owner access). Override via EDITOR_HOSTS.
EDITOR_HOSTS = set(filter(None, (h.strip().lower() for h in
    (os.getenv("EDITOR_HOSTS") or "live.your-domain.example,play.your-domain.example").split(","))))


def _editor_host_ok(request) -> bool:
    host = (request.headers.get("host") or "").split(":")[0].strip().lower()
    return host in EDITOR_HOSTS


async def _guard(request: Request):
    """Protect the JSON API; the UI page + /login + /logout stay open (the UI
    page itself shows the login form when unauthenticated)."""
    if "/api/" in request.url.path:
        if not _editor_host_ok(request):
            raise HTTPException(status_code=404, detail="Not Found")
        name = _authed_name(request)
        if not name:
            raise HTTPException(status_code=401, detail="unauthorized")
        # Audit every mutating action (announcements, bot commands, edits, bans...).
        # Reading a small JSON body here caches it, so the handler still gets it.
        if request.method in ("POST", "PUT", "PATCH", "DELETE") \
                and not request.url.path.rstrip("/").endswith("/api/actions"):
            detail = ""
            try:
                cl = int(request.headers.get("content-length") or 0)
                if 0 < cl <= 4096 and "json" in (request.headers.get("content-type") or "").lower():
                    detail = _audit_summary(await request.json())
            except Exception:
                detail = ""
            await _log_action(request, name, detail)
            mark: list = []
            _notified_cv.set(mark)
            yield
            if not mark:      # the handler didn't post its own message
                await _post_webhook(EDITOR_DISCORD_WEBHOOK, _action_line(request, name, detail))
            return
    yield


async def _require_admin(request: Request):
    """Gate a route to editor admins (EDITOR_ADMINS). The lockout/destructive
    player actions (ban/unban/block/delete/unlink-discord/device-reset) are
    admin-only so a limited (master-password) data editor can't lock players out
    (owner 2026-10-21). Resolves _is_role_admin at request time."""
    if not await _is_role_admin(request):
        raise HTTPException(status_code=403, detail="Admins only.")


async def _is_moderator(request: Request) -> bool:
    """Editor admin, or an EDITOR_MODERATORS account (ban/unban/block only)."""
    doc = await _name_exists(_authed_name(request) or "")
    aid = doc and doc.get("accountID")
    return bool(aid and (aid in EDITOR_ADMINS or aid in EDITOR_MODERATORS))


async def _require_mod(request: Request):
    if not await _is_moderator(request):
        raise HTTPException(status_code=403, detail="Admins and moderators only.")
    # A moderator can't act on an admin or another moderator.
    target = request.path_params.get("account_id")
    if target in EDITOR_ADMINS or target in EDITOR_MODERATORS:
        if not await _is_role_admin(request):
            raise HTTPException(status_code=403, detail="Moderators can't ban admins or moderators.")


router = APIRouter(dependencies=[Depends(_guard)])
editor_app = FastAPI(title="Yeeps Player Editor")

# ─── JSON API ────────────────────────────────────────────────────────────────

async def _discord_lookup(account_ids: list) -> dict:
    """{accountID: {"discordID","discordUsername"}} for the given accounts, merged
    from (1) the Yeeps240 discord_links collection, then (2) the player's 1.31
    account (CARRYOVER_DB). Lets the editor show a player's linked Discord even
    when they only ever linked on 1.31 (discordID lives on the Yeeps119 account,
    not the 2.31.4 one) -- Blaze: "right now only 1.31.0 has discords"."""
    out = {}
    ids = [a for a in (account_ids or []) if a]
    if not ids:
        return out
    try:
        async for r in _db["discord_links"].find(
                {"accountID": {"$in": ids}},
                {"_id": 0, "accountID": 1, "discordID": 1, "discordUsername": 1}):
            if r.get("discordUsername") or r.get("discordID"):
                out[r["accountID"]] = {"discordID": r.get("discordID") or "",
                                       "discordUsername": r.get("discordUsername") or ""}
    except Exception as _e:
        print(f"[editor] discord_links lookup failed: {_e}")
    cdb = os.getenv("CARRYOVER_DB", "")
    missing = [a for a in ids if not (out.get(a) or {}).get("discordUsername")]
    if cdb and missing:
        try:
            from services.db import client as _mc
            async for r in _mc[cdb]["accounts"].find(
                    {"accountID": {"$in": missing}, "discordID": {"$nin": [None, ""]}},
                    {"_id": 0, "accountID": 1, "discordID": 1, "discordUsername": 1}):
                out[r["accountID"]] = {"discordID": r.get("discordID") or "",
                                       "discordUsername": r.get("discordUsername") or ""}
        except Exception as _e:
            print(f"[editor] 1.31 discord lookup failed: {_e}")
    return out


@router.get("/api/players")
async def list_players(q: str = "", limit: int = 50):
    """List/search player accounts."""
    filt = {}
    carry_ids = []
    if q:
        rx = {"$regex": re.escape(q.strip().lstrip("@")), "$options": "i"}
        filt = {"$or": [
            {"accountID": rx},
            {"displayName": rx},
            {"discordUsername": rx},     # set by /link and /data
            {"discordID": rx},
            {"oculusID": rx},
        ]}
        # Also match players whose Discord link lives on 1.31 / the 240
        # discord_links collection (not the 240 account), so a Discord search
        # finds them too.
        try:
            async for r in _db["discord_links"].find(
                    {"$or": [{"discordUsername": rx}, {"discordID": rx}]},
                    {"_id": 0, "accountID": 1}).limit(limit):
                carry_ids.append(r["accountID"])
            cdb = os.getenv("CARRYOVER_DB", "")
            if cdb:
                from services.db import client as _mc
                async for r in _mc[cdb]["accounts"].find(
                        {"$or": [{"discordUsername": rx}, {"discordID": rx}]},
                        {"_id": 0, "accountID": 1}).limit(limit):
                    carry_ids.append(r["accountID"])
        except Exception as _e:
            print(f"[editor] discord search failed: {_e}")
        if carry_ids:
            filt = {"$or": filt["$or"] + [{"accountID": {"$in": list(dict.fromkeys(carry_ids))}}]}
    cursor = accounts.find(filt, {"_id": 0, "accountID": 1, "displayName": 1, "currency": 1, "beans": 1,
                                  "discordUsername": 1, "discordID": 1}).limit(limit)
    docs = await cursor.to_list(length=limit)
    # Fill Discord for players who linked on 1.31 / via discord_links.
    need = [d["accountID"] for d in docs if not d.get("discordUsername")]
    if need:
        dl = await _discord_lookup(need)
        for d in docs:
            info = dl.get(d["accountID"])
            if info and not d.get("discordUsername"):
                d["discordUsername"] = info["discordUsername"]
                d["discordID"] = d.get("discordID") or info["discordID"]
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
    # Show the linked Discord even when the player only linked on 1.31 (discord
    # lives on the Yeeps119 account / 240 discord_links, not the 240 account).
    if not doc.get("discordUsername"):
        info = (await _discord_lookup([account_id])).get(account_id)
        if info:
            doc["discordUsername"] = info["discordUsername"]
            doc["discordID"] = doc.get("discordID") or info["discordID"]
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

    # Only editor admins may change a player's roles (owner 2026-10-19). A
    # non-admin's other edits still save; the editor sends the whole account, so
    # an unchanged roleKeys just passes through (stripped), a changed one is refused.
    if "roleKeys" in patch and not await _is_role_admin(request):
        _cur = await accounts.find_one({"accountID": account_id}, {"_id": 0, "roleKeys": 1})
        if set(patch.get("roleKeys") or []) != set((_cur or {}).get("roleKeys") or []):
            raise HTTPException(status_code=403, detail="Only admins can change a player's roles.")
        patch.pop("roleKeys", None)

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

    result = await accounts.update_one({"accountID": account_id}, {"$set": patch})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Account not found")

    name = _authed_name(request) or "?"
    disp = before.get("displayName") or "(no name)"
    changes = _summarize_changes(before, patch)
    await _discord_notify(f"**{name}** edited **{disp}** (`{account_id}`)\n{changes}")
    return JSONResponse(content={"ok": True, "modified": result.modified_count})


@router.delete("/api/player/{account_id}", dependencies=[Depends(_require_admin)])
async def delete_player(account_id: str, request: Request):
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


@router.post("/api/player/{account_id}/block", dependencies=[Depends(_require_mod)])
async def block_player(account_id: str, request: Request):
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
# own deployment with its own DB_NAME, so 1.19.0 and 2.31.0-blaze data (and editors)
# stay fully separate. GAME_VERSION on this deployment is "2.31.0-blaze".
from config import REQUIRED_CLIENT_VERSION as _REQ_VER
GAME_VERSION = os.getenv("GAME_VERSION", _REQ_VER)


@router.get("/api/me")
async def whoami(request: Request):
    """Return the logged-in Meta name + which game version/DB this editor edits."""
    name = _authed_name(request)
    if not name:
        raise HTTPException(status_code=401, detail="unauthorized")
    from config import DB_NAME
    # photon=True reveals the Photon + Talk tabs -- OWNER ONLY (PHOTON_OWNERS),
    # not every signed-in data manager.
    return JSONResponse(content={"name": name, "version": GAME_VERSION, "db": DB_NAME,
                                 "photon": await _photon_ok(request),
                                 "view": await _view_ok(request),
                                 "roleAdmin": await _is_role_admin(request),
                                 "canBan": await _is_moderator(request)})


# Special roles the owner tracks (owner 2026-10-18: "me and staff member a can see who has
# the roles like blaze frost and frostbackpack"). Read-only view, editor admins only.
SPECIAL_ROLES = ["blaze", "frost", "frostpack"]


async def _is_role_admin(request: Request) -> bool:
    """Signed-in editor user whose Meta name resolves to an EDITOR_ADMINS account."""
    doc = await _name_exists(_authed_name(request) or "")
    return bool(doc and doc.get("accountID") in EDITOR_ADMINS)


@router.get("/api/builders")
async def builders_get(request: Request):
    """Map builders (may put shared maps in creative + save them). Editor admins only."""
    if not await _is_role_admin(request):
        raise HTTPException(status_code=404, detail="not found")
    from services import creative_builders
    ids = await creative_builders.stored_ids()
    names = {a["accountID"]: re.sub(r"<[^>]*>", "", a.get("displayName") or "") async for a in accounts.find(
        {"accountID": {"$in": ids + list(EDITOR_ADMINS)}}, {"_id": 0, "accountID": 1, "displayName": 1})}
    return JSONResponse(content={
        "builders": [{"accountID": i, "name": names.get(i, "")} for i in ids],
        "always": [{"accountID": i, "name": names.get(i, "")} for i in sorted(EDITOR_ADMINS)]})


@router.post("/api/builders")
async def builders_set(request: Request):
    """Add or remove one map builder: {"accountID": "o_...", "add": true|false}."""
    if not await _is_role_admin(request):
        raise HTTPException(status_code=404, detail="not found")
    try:
        body = await request.json()
    except Exception:
        body = {}
    aid = str(body.get("accountID", "")).strip()
    if not re.fullmatch(r"o_\d{6,20}", aid):
        raise HTTPException(status_code=400, detail="Give an account ID like o_123...")
    doc = await accounts.find_one({"accountID": aid}, {"_id": 0, "displayName": 1})
    if not doc:
        raise HTTPException(status_code=404, detail="No live account with that ID")
    from services import creative_builders
    ids = set(await creative_builders.stored_ids())
    (ids.add if body.get("add", True) else ids.discard)(aid)
    out = await creative_builders.set_ids(list(ids))
    _nm = re.sub(r"<[^>]*>", "", doc.get("displayName") or "")
    await _discord_notify(f"**{_authed_name(request) or '?'}** {'added' if body.get('add', True) else 'removed'} "
                          f"map builder **{_nm}** (`{aid}`)")
    return JSONResponse(content={"ok": True, "builders": out})


@router.post("/api/roles/remove")
async def role_remove(request: Request):
    """Strip one role from one account, on live and 1.31. Editor admins only."""
    if not await _is_role_admin(request):
        raise HTTPException(status_code=404, detail="not found")
    try:
        body = await request.json()
    except Exception:
        body = {}
    aid = str(body.get("accountID", "")).strip()
    role = str(body.get("role", "")).strip()
    if not re.fullmatch(r"o_\d{6,20}", aid) or not re.fullmatch(r"[A-Za-z0-9_]{1,40}", role):
        raise HTTPException(status_code=400, detail="Need an account ID and a role name")
    from services.db import client as _mc
    dbs = [accounts]
    cdb = os.getenv("CARRYOVER_DB")
    if cdb:
        dbs.append(_mc[cdb]["accounts"])
    removed = 0
    for col in dbs:
        r = await col.update_one({"accountID": aid}, {"$pull": {"roleKeys": role}})
        removed += r.modified_count
    # ALL_NON_BLAZE_ROLES are re-granted every login; blaze/frost/frostpack stick.
    from handlers.login import ALL_NON_BLAZE_ROLES
    auto = role in set(ALL_NON_BLAZE_ROLES)
    await _discord_notify(f"**{_authed_name(request) or '?'}** removed role **{role}** from `{aid}`"
                          + (" (auto-granted role — will come back on next login)" if auto else ""))
    return JSONResponse(content={"ok": True, "removed": removed, "autoGranted": auto})


@router.get("/api/roles/holders")
async def role_holders(request: Request, roles: str = ""):
    """Who holds each special role, on live and on 1.31 (shared accounts, separate DBs)."""
    if not await _is_role_admin(request):
        raise HTTPException(status_code=404, detail="not found")
    want = [r for r in (x.strip() for x in roles.split(",")) if re.fullmatch(r"[A-Za-z0-9_]{1,40}", r)] or SPECIAL_ROLES
    from services.db import client as _mc
    dbs = [("live", accounts)]
    cdb = os.getenv("CARRYOVER_DB")
    if cdb:
        dbs.append(("1.31", _mc[cdb]["accounts"]))
    out = {}
    for role in want[:12]:
        holders = {}
        for label, col in dbs:
            async for a in col.find({"roleKeys": role},
                                    {"_id": 0, "accountID": 1, "displayName": 1, "discordUsername": 1}):
                h = holders.setdefault(a.get("accountID"), {
                    "accountID": a.get("accountID"),
                    "name": re.sub(r"<[^>]*>", "", a.get("displayName") or ""),   # drop color tags
                    "discord": a.get("discordUsername") or "", "servers": []})
                if label not in h["servers"]:
                    h["servers"].append(label)
                if not h["name"] and a.get("displayName"):
                    h["name"] = re.sub(r"<[^>]*>", "", a["displayName"])
        out[role] = sorted(holders.values(), key=lambda h: h["name"].lower())
    return JSONResponse(content={"roles": out})


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
    if "roleKeys" in patch and not await _is_role_admin(request):
        raise HTTPException(status_code=403, detail="Only admins can edit default roles.")
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


@router.get("/api/news/popup")
async def get_news_popup():
    from services.db import globals_col
    d = await globals_col.find_one({"key": "news_popup"}, {"_id": 0, "value": 1})
    return JSONResponse(content=(d or {}).get("value") or {"text": "", "enabled": False, "seconds": 15})


@router.post("/api/news/popup")
async def set_news_popup(request: Request):
    """Popup shown once per launch to every live player (feed 'A' line, client/feed.txt).
    Body {text, enabled, seconds}."""
    import time as _t
    from services.db import globals_col
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    v = {"text": str(body.get("text") or "")[:600], "enabled": bool(body.get("enabled")),
         "seconds": max(3, min(int(body.get("seconds") or 15), 60)), "ts": int(_t.time())}
    await globals_col.update_one({"key": "news_popup"}, {"$set": {"key": "news_popup", "value": v}}, upsert=True)
    name = _authed_name(request) or "?"
    await _discord_notify(f"**{name}** {'set' if v['enabled'] else 'turned off'} the news popup:\n>>> {v['text'] or '(empty)'}")
    return JSONResponse(content={"ok": True, **v})


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
    from services.db import maps_for as _maps_for
    maps_col = _maps_for(room_key)   # private worlds live in the shared 1.31 DB
    doc = await imported_maps.find_one({"roomKey": room_key}, {"_id": 0, "saveData": 0})
    if doc:
        blocks = doc.get("blocks") or []
        # Return the FULL list: the page saves back exactly what it got, so any
        # cap here silently DELETES every block past it on save (real live rooms
        # have thousands -- Home has 5989).
        doc["blocks"] = blocks
        doc["blockCount"] = len(blocks)
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
    # Return ALL blocks: the editor saves back what it received, so a cap here
    # would delete every block past it (real live rooms have thousands).
    m["blocks"] = blocks
    m["blocksTruncated"] = False
    return JSONResponse(content=m)


def _spam_states_parse(raw: bytes) -> list:
    """Internal states: "SPAM", u8 3, i32 count, then per entry i32 x, y, z + a .NET
    string (7-bit length). Same format as 1.31 (see yeeps-proxy dynamo_ops)."""
    import struct
    if raw[:4] != b"SPAM" or len(raw) < 9:
        return []
    out, p = [], 9
    try:
        for _ in range(struct.unpack_from("<i", raw, 5)[0]):
            x, y, z = struct.unpack_from("<iii", raw, p); p += 12
            n = shift = 0
            while True:
                byte = raw[p]; p += 1
                n |= (byte & 0x7F) << shift
                if not byte & 0x80:
                    break
                shift += 7
            out.append(((x, y, z), raw[p:p + n].decode("utf-8", "replace"))); p += n
    except Exception:
        pass
    return out


def _spam_states_build(entries) -> str:
    import struct, base64
    buf = bytearray(b"SPAM") + bytes([3]) + struct.pack("<i", len(entries))
    for (x, y, z), text in entries:
        buf += struct.pack("<iii", x, y, z)
        data = text.encode("utf-8")
        n = len(data)
        while True:
            seven = n & 0x7F
            n >>= 7
            buf.append(seven | (0x80 if n else 0))
            if not n:
                break
        buf += data
    return base64.b64encode(bytes(buf)).decode("ascii")


@router.post("/api/map/{room_key}/blocks")
async def save_map_blocks(room_key: str, request: Request):
    """Save an edited block list back to a room: re-encode to the 1.19 map
    format, write it to the room's $current save, keep the exact blocks for
    re-editing, and bump the room's edit session so clients reload."""
    from services.db import maps as maps_col, rooms as rooms_col
    from services.db import maps_for as _maps_for
    maps_col = _maps_for(room_key)   # private worlds live in the shared 1.31 DB
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
    cur = await maps_col.find_one({"mapKey": f"{room_key}$current"}, {"_id": 0}) or {}
    # Snapshot the current map first so the editor's Revert can undo this save.
    try:
        if cur.get("saveData"):
            await _db["map_history"].insert_one(
                {"roomKey": room_key, "ts": session_id, "savedBy": cur.get("savedBy"),
                 "savedAt": cur.get("savedAt"), "bytes": len(cur.get("saveData") or ""),
                 "doc": cur, "preBlockSave": True})
    except Exception as e:
        print(f"[editor] pre-save snapshot failed for {room_key}: {e}")
    # Blocks that carry a "state" (Map Maker: sign text, variable values, ...)
    # rewrite the room's internal states. Those are keyed by block position, so
    # old states are kept only where a block still sits and brings none of its own.
    states_set = {}
    if any("state" in b for b in norm):
        import base64 as _b64
        old = {tuple(pos): txt for pos, txt in _spam_states_parse(_b64.b64decode(cur.get("saveInternalStates") or ""))}
        entries = {}
        for b in norm:
            pos = (b["x"], b["y"], b["z"])
            if b.get("state"):
                entries[pos] = b["state"]
            elif pos in old:
                entries[pos] = old[pos]
        states_set = {"saveInternalStates": _spam_states_build(list(entries.items()))}
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
    from services.db import maps_for as _maps_for
    maps_col = _maps_for(room_key)   # private worlds live in the shared 1.31 DB
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


@router.post("/api/maps/clear-blaze")
async def maps_clear_blaze(request: Request):
    """Remove BLAZE Pillows / BLAZE Screens placed by accounts without the blaze role
    (services/blaze_cleanup.py). Body {"apply": false} = report only."""
    from services.blaze_cleanup import clear_blaze_blocks
    try:
        body = await request.json()
    except Exception:
        body = {}
    res = await clear_blaze_blocks(apply=bool(body.get("apply")))
    if res["applied"] and res["maps"]:
        who = _authed_name(request) or "?"
        await _discord_notify(f"{who} removed {res['blocks']} BLAZE blocks not placed by blaze-role accounts "
                              f"from {res['maps']} maps")
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
    from services.db import maps_for as _maps_for
    maps_col = _maps_for(room_key)   # private worlds live in the shared 1.31 DB
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
    await _discord_notify(f"**{_authed_name(request) or '?'}** IP-unbanned `{ip}`")
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


@router.post("/api/player/{account_id}/ban", dependencies=[Depends(_require_mod)])
async def ban_player(account_id: str, request: Request):
    """Ban a player. Body `durationHours` > 0 makes it a TIMED ban that auto-lifts
    when it expires (account block only — no permanent IP ban for a temporary
    ban); omitted / 0 = PERMANENT (account block + IP ban)."""
    from services.ip_bans import ban_ip
    try:
        body = await request.json()
    except Exception:
        body = {}
    doc = await accounts.find_one({"accountID": account_id},
                                  {"_id": 0, "displayName": 1, "lastIP": 1, "recentIPs": 1})
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


@router.post("/api/player/{account_id}/unban", dependencies=[Depends(_require_mod)])
async def unban_player(account_id: str, request: Request):
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
    await _discord_notify(f"**{who}** unbanned **{doc.get('displayName') or account_id}** (`{account_id}`)"
                          + (f" + IPs {', '.join(unbanned)}" if unbanned else ""))
    shown = unbanned if _can_see_ips(request) else ["(hidden)"] * len(unbanned)
    return JSONResponse(content={"ok": True, "unbannedIPs": shown})


# ── Kick (boot) players ─────────────────────────────────────────────
# One-shot boots, not bans: see services/kick.py. Build UI on these.

@router.get("/api/editor_code")
async def editor_code(request: Request):
    """The current 2.31.0-blaze editor code (posted nowhere since the Oct 10 raid), shown
    to the owner in the editor header so he can sign in again without a link."""
    name = _authed_name(request) or ""
    doc = await _name_exists(name) if name else None
    if not doc or doc.get("accountID") not in EDITOR_ADMINS:
        raise HTTPException(status_code=404, detail="not found")
    left = EDITOR_PW_ROTATE_SECONDS - (int(time.time()) % EDITOR_PW_ROTATE_SECONDS)
    return JSONResponse(content={"code": _editor_password(), "minutesLeft": left // 60})


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


@router.post("/api/player/{account_id}/unlink-discord", dependencies=[Depends(_require_admin)])
async def unlink_discord(account_id: str, request: Request):
    """Remove this account's Discord link on live + 1.31 and delete its live
    discord_links record. With no link the account can't join (login gate), so
    this also forces them to /link again before they can play."""
    from services.db import client as _mc
    dbs = [("live", accounts)]
    cdb = os.getenv("CARRYOVER_DB")
    if cdb:
        dbs.append(("1.31", _mc[cdb]["accounts"]))
    before = None
    changed = 0
    who = _authed_name(request) or "?"
    at = datetime.now(timezone.utc).strftime("%Y %m/%d %H:%M:%S")
    for _, col in dbs:
        d = await col.find_one({"accountID": account_id}, {"_id": 0, "displayName": 1, "discordID": 1, "discordUsername": 1})
        if d and before is None:
            before = d
        upd = {"$unset": {"discordID": "", "discordUsername": ""}}
        # Keep the old Discord on the account (same fields as the bot's /unlink).
        if d and d.get("discordID"):
            old_id, old_name = str(d["discordID"]), d.get("discordUsername") or ""
            upd["$set"] = {"oldDiscordID": old_id, "oldDiscordUsername": old_name}
            upd["$push"] = {"oldDiscords": {"discordID": old_id, "discordUsername": old_name,
                                            "unlinkedAt": at, "by": who}}
        r = await col.update_one({"accountID": account_id}, upd)
        changed += r.modified_count
    if before is None:
        raise HTTPException(status_code=404, detail="Account not found")
    try:
        from services.db import db as _db240
        for _ldb in [_db240] + ([_mc[cdb]] if cdb else []):
            async for l in _ldb["discord_links"].find({"accountID": account_id}, {"_id": 0}):
                await _db240["discord_unlinks"].insert_one({**l, "unlinkedAt": at, "by": who})
            await _ldb["discord_links"].delete_many({"accountID": account_id})
    except Exception as e:
        print(f"[editor] discord_links cleanup failed for {account_id}: {e}")
    had = before.get("discordUsername") or before.get("discordID") or "(none)"
    await _discord_notify(f"**{who}** unlinked Discord **{had}** from **{before.get('displayName') or account_id}** (`{account_id}`) "
                          f"— they must /link again to join.")
    return JSONResponse(content={"ok": True, "unlinked": had, "changed": changed})


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


@router.post("/api/player/{account_id}/device-reset", dependencies=[Depends(_require_admin)])
async def reset_device_lock(account_id: str, request: Request):
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


@router.get("/api/actions")
async def list_actions(request: Request, limit: int = 300):
    """Audit trail of editor actions (admins only)."""
    if not await _is_role_admin(request):
        raise HTTPException(status_code=403, detail="Admins only.")
    limit = max(1, min(int(limit or 300), 1000))
    rows, src = [], "db"
    try:
        rows = [d async for d in editor_actions.find({}, {"_id": 0}).sort([("_id", -1)]).limit(limit)]
    except Exception:
        rows = []
    if not rows:   # Mongo empty or blocked (e.g. over quota) -> durable on-disk copy
        rows, src = _read_action_file(limit), "file"
    return JSONResponse(content={"ok": True, "actions": rows, "source": src})


_ACTIONS_HTML = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Editor actions</title>
<style>
:root{color-scheme:dark;--bg:#1d1915;--card:#2d2720;--ink:#f1e8d9;--muted:#a8997f;--line:#453c31;--purple:#8c7bc9}
body{background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,sans-serif;margin:0;padding:16px}
h1{font-size:18px;margin:0 0 4px}.sub{color:var(--muted);margin:0 0 14px}
table{border-collapse:collapse;width:100%;max-width:1100px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;position:sticky;top:0;background:var(--bg)}
tr:hover td{background:var(--card)}
.who{color:var(--purple);font-weight:600;white-space:nowrap}
.at{color:var(--muted);white-space:nowrap;font-variant-numeric:tabular-nums}
.path{font-family:ui-monospace,monospace;font-size:12px}
.m{display:inline-block;min-width:46px;font-family:ui-monospace,monospace;font-size:11px;color:var(--muted)}
.bar{display:flex;gap:10px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
input{background:var(--card);border:1px solid var(--line);color:var(--ink);border-radius:6px;padding:6px 9px}
a{color:var(--purple)}
</style>
<h1>Editor actions</h1>
<p class=sub>Who did what in the editor (announcements, bot commands, edits, bans). Admins only. <a href="/editor/">back to editor</a></p>
<div class=bar><input id=f placeholder="filter by name / path / detail" oninput=render()>
<label><input type=checkbox id=auto checked onchange=tick()> auto-refresh</label>
<span id=stat class=sub></span></div>
<table><thead><tr><th>time (UTC)</th><th>who</th><th>IP</th><th></th><th>action</th><th>detail</th></tr></thead><tbody id=b></tbody></table>
<script>
let ALL=[];
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function pretty(p){return (p||'').replace(/^\\/api\\//,'')}
async function load(){
 try{const r=await fetch('/api/actions?limit=500',{credentials:'same-origin'});
  if(r.status===403){document.getElementById('stat').textContent='admins only — log in as an admin';return;}
  const j=await r.json();ALL=j.actions||[];render();
  document.getElementById('stat').textContent=ALL.length+' recent';}catch(e){}}
function render(){
 const q=document.getElementById('f').value.toLowerCase();
 const rows=ALL.filter(a=>!q||JSON.stringify(a).toLowerCase().includes(q));
 document.getElementById('b').innerHTML=rows.map(a=>`<tr>
  <td class=at>${esc(a.at)}</td><td class=who>${esc(a.name)}</td>
  <td class=at>${esc(a.ip)}</td><td><span class=m>${esc(a.method)}</span></td>
  <td class=path>${esc(pretty(a.path))}</td><td>${esc(a.detail)}</td></tr>`).join('');}
function tick(){}
load();setInterval(()=>{if(document.getElementById('auto').checked)load()},5000);
</script>"""


@router.get("/actions", response_class=HTMLResponse)
async def actions_page(request: Request):
    if not _authed_name(request):
        return RedirectResponse("/editor/login", status_code=302)
    return HTMLResponse(content=_ACTIONS_HTML)


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


# ---- Daily tab (daily challenges + card economy), 2026-10-17 -----------------
# Daily challenges: live Yeeps by default (handlers/challenges.py fetches once per
# 08:00-UTC window); the editor can set a custom set (override) or force a refresh.
# Economy: G2_Globals keys the client reads (dynamo_ops._GLOBAL_DEFAULTS); a stored
# {"key", "data"} doc overrides the default.
_ECONOMY_KEYS = {
    "priceBeans_tradingCardPack": "Card pack price (beans)",
    "beanTradingCardPacksPerDay": "Card packs per day (huge = unlimited)",
    "card_pack_size": "Cards in each pack (big packs can lag)",
}
# server-only economy keys (not G2_Globals the client reads): their built-in defaults
_ECONOMY_SERVER_DEFAULTS = {"card_pack_size": 100}


@router.get("/api/daily")
async def get_daily():
    from handlers import challenges as ch
    from services.db import globals_col
    from services.dynamo_ops import _GLOBAL_DEFAULTS
    cur = await ch._daily_challenges()
    econ = {}
    for k, label in _ECONOMY_KEYS.items():
        d = await globals_col.find_one({"key": k}, {"_id": 0, "data": 1})
        _dflt = _GLOBAL_DEFAULTS.get(k, _ECONOMY_SERVER_DEFAULTS.get(k))
        econ[k] = {"label": label, "value": (d or {}).get("data", _dflt),
                   "default": _dflt, "custom": d is not None}
    return JSONResponse(content={"ok": True, "itemKeys": cur.get("itemKeys") or {},
                                 "startTime": cur.get("startTime"), "expireTime": cur.get("expireTime"),
                                 "source": cur.get("source") or "live",
                                 "hasOverride": bool(await ch.daily_override()), "economy": econ})


@router.post("/api/daily")
async def set_daily(request: Request):
    """Body: {"mode": "live"} -> drop the custom set (back to live Yeeps), or
    {"mode": "custom", "itemKeys": {group: [{"key", "value"}]}} -> use these."""
    from handlers import challenges as ch
    from services.db import globals_col
    try:
        body = await request.json()
    except Exception:
        body = {}
    who = _authed_name(request) or "?"
    if body.get("mode") == "live":
        await globals_col.delete_one({"key": ch._DAILY_OVERRIDE_KEY})
        ch.reset_daily_cache()
        await _discord_notify(f"**{who}** set the daily challenges back to live Yeeps")
        return JSONResponse(content={"ok": True})
    ik = body.get("itemKeys")
    if not isinstance(ik, dict) or not ik:
        raise HTTPException(status_code=400, detail="itemKeys required")
    clean = {}
    for g, rows in ik.items():
        g = str(g).strip()[:40]
        if not g or not isinstance(rows, list):
            continue
        out = []
        for r in rows[:20]:
            k = str((r or {}).get("key") or "").strip()[:80]
            if not k:
                continue
            try:
                v = max(0, min(1000000, int((r or {}).get("value") or 0)))
            except Exception:
                v = 0
            out.append({"key": k, "value": v})
        if out:
            clean[g] = out
    if not clean:
        raise HTTPException(status_code=400, detail="no challenges given")
    await globals_col.update_one({"key": ch._DAILY_OVERRIDE_KEY},
                                 {"$set": {"key": ch._DAILY_OVERRIDE_KEY, "value": {"itemKeys": clean}, "by": who}},
                                 upsert=True)
    ch.reset_daily_cache()
    await _discord_notify(f"**{who}** set custom daily challenges: "
                          + ", ".join(f"{g}: {len(v)}" for g, v in clean.items()))
    return JSONResponse(content={"ok": True, "itemKeys": clean})


@router.post("/api/daily/refresh")
async def refresh_daily():
    """Re-fetch today's challenges from live Yeeps now (one read-only call)."""
    from handlers import challenges as ch
    from services.db import globals_col
    await globals_col.delete_one({"key": ch._DAILY_CACHE_KEY})
    ch.reset_daily_cache()
    cur = await ch._daily_challenges()
    return JSONResponse(content={"ok": True, "source": cur.get("source"), "itemKeys": cur.get("itemKeys")})


@router.post("/api/economy")
async def set_economy(request: Request):
    """Body: {"<key>": int | null}; null resets that key to the built-in default."""
    from services.db import globals_col
    try:
        body = await request.json()
    except Exception:
        body = {}
    who = _authed_name(request) or "?"
    done = {}
    for k, v in body.items():
        if k not in _ECONOMY_KEYS:
            continue
        if v is None or v == "":
            await globals_col.delete_one({"key": k})
            done[k] = "default"
            continue
        try:
            iv = max(0, min(999999999, int(v)))
            if k == "card_pack_size":      # an empty pack crashes the client (no max: owner 2026-10-18)
                iv = max(1, iv)
        except Exception:
            raise HTTPException(status_code=400, detail=f"{k} must be a number")
        await globals_col.update_one({"key": k}, {"$set": {"key": k, "data": iv}}, upsert=True)
        done[k] = iv
    if done:
        await _discord_notify(f"**{who}** changed the card economy: {done}")
    return JSONResponse(content={"ok": True, "set": done})


@router.get("/api/settings")
async def get_settings():
    from services.db import globals_col
    fe = await globals_col.find_one({"key": "friends_everyone"}, {"_id": 0, "value": 1})
    fatal = await globals_col.find_one({"key": "fatalError"}, {"_id": 0, "data": 1})
    return JSONResponse(content={"ok": True,
                                 "friends_everyone": bool((fe or {}).get("value", True)),
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
    target = newest_build() if on else ""
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


@router.get("/api/debug/report")
async def get_debug_report(name: str = ""):
    """A saved debug-button report (game log + the player's full devlog) as plain text."""
    import os as _os, re as _re
    from fastapi.responses import PlainTextResponse
    if not _re.fullmatch(r"[0-9]{8}-[0-9]{6}_[A-Za-z0-9_]{0,40}\.txt", name or ""):
        raise HTTPException(status_code=404)
    path = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "logs", "debug_reports", name)
    if not _os.path.isfile(path):
        raise HTTPException(status_code=404)
    return PlainTextResponse(open(path, errors="replace").read(),
                             headers={"Content-Disposition": f'inline; filename="{name}"'})


@router.get("/api/devlog")
async def get_devlog(lines: int = 500, q: str = ""):
    """Tail of the 2.31.0-blaze test-build log (routers/client_router.py /client/devlog)."""
    import os as _os
    path = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "logs", "devlog.log")
    lines = max(1, min(int(lines or 500), 5000))
    out = []
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 6_000_000))
            data = f.read().decode("utf-8", "replace").splitlines()
        if q:
            ql = q.lower()
            data = [l for l in data if ql in l.lower()]
        out = data[-lines:]
    except FileNotFoundError:
        pass
    return JSONResponse(content={"lines": out})


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
<link href="https://fonts.googleapis.com/css2?family=Lilita+One&family=Quicksand:wght@500;600;700&display=swap" rel="stylesheet">
<style>
:root{color-scheme:dark;--bg:#1d1915;--bg2:#262019;--card:#2d2720;--field:#211c17;--ink:#f1e8d9;--muted:#a8997f;--line:#453c31;--red:#e26a6a;--purple:#8c7bc9}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
  background:linear-gradient(160deg,var(--bg),var(--bg2));color:var(--ink);
  font-family:'Quicksand',system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:16px}
.card{width:min(94vw,400px);background:var(--card);border:1px solid var(--line);border-radius:20px;
  padding:30px 26px;box-shadow:0 16px 50px rgba(120,95,60,.18)}
.logo{width:46px;height:46px;border-radius:14px;background:var(--purple);color:#fff;
  font-family:'Lilita One',cursive;font-size:28px;display:flex;align-items:center;justify-content:center;
  margin:0 auto 14px;box-shadow:0 6px 16px rgba(115,95,176,.35)}
h1{margin:0 0 4px;font-family:'Lilita One',cursive;font-size:24px;text-align:center;font-weight:400}
p.sub{margin:0 0 18px;color:var(--muted);font-size:13px;text-align:center}
label{display:block;font-size:12px;color:var(--muted);margin:14px 0 6px;font-weight:600}
input{width:100%;padding:12px 13px;border-radius:12px;border:1px solid var(--line);background:var(--field);
  color:var(--ink);font-size:15px;font-family:inherit}
input:focus{outline:none;border-color:var(--purple);box-shadow:0 0 0 4px rgba(115,95,176,.14)}
button{width:100%;margin-top:22px;padding:13px;border:0;border-radius:13px;background:var(--red);color:#fff;
  font-size:15px;font-weight:700;font-family:inherit;cursor:pointer;transition:filter .15s}
button:hover{filter:brightness(1.05)}
button:disabled{opacity:.6;cursor:default}
.err{margin-top:14px;color:#c0392b;font-size:13px;min-height:18px;text-align:center}
</style></head><body>
<form class="card" id="f">
  <div class="logo">Y</div>
  <h1>Yeeps Data Editor</h1>
  <p class="sub">Sign in to edit account &amp; room data.</p>
  <label>Yeeps name</label>
  <input id="name" autocomplete="username" placeholder="your in-game name" required>
  <label>Password</label>
  <input id="pw" type="password" autocomplete="current-password" placeholder="password" required>
  <button id="go" type="submit">Sign in</button>
  <div class="err" id="err"></div>
  <p class="sub" style="margin-top:14px;font-size:12px">VPNs are blocked. On iPhone turn off iCloud Private Relay
  (Settings &gt; your name &gt; iCloud &gt; Private Relay) and any 1.1.1.1 / WARP app before signing in.</p>
</form>
<script>
const f=document.getElementById('f'),err=document.getElementById('err'),go=document.getElementById('go');
try{const _n=new URLSearchParams(location.search).get('name');if(_n){const el=document.getElementById('name');el.value=_n;document.getElementById('pw').focus();}}catch(_){}
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


@router.get("/login", response_class=HTMLResponse)
async def editor_login_page(request: Request):
    # Loading the login page posts the current password if it just rotated, so an
    # admin coming to sign in always has the latest code waiting in the webhook.
    await _maybe_notify_new_password()
    if _authed_name(request):
        return RedirectResponse("./", status_code=302)
    return HTMLResponse(content=_LOGIN_HTML)


# ── One-time login links (2.31.0-blaze) ─────────────────────────────────────
# For an admin who is away from home (the network lock refuses VPN / mobile
# addresses). A token is created on the server machine only (logs/editor_magic.json,
# written by the operator, never via the web), works once, within 30 minutes, and
# the session it opens is bound to the network it was used from.
_MAGIC_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs", "editor_magic.json")


@router.get("/magic/{token}")
async def editor_magic_login(token: str, request: Request):
    import json as _json
    from services.ip_bans import client_ip
    ip = client_ip(request) or "?"
    if _from_vpn(request):
        print(f"[editor] magic link refused from VPN {ip}")
        return HTMLResponse("<h3>Turn off your VPN (WARP / iCloud Private Relay) and open the link again.</h3>",
                            status_code=403)
    try:
        tokens = _json.load(open(_MAGIC_PATH))
    except Exception:
        tokens = {}
    t = tokens.get(token) if isinstance(tokens, dict) and len(token) >= 24 else None
    if not t or t.get("used") or time.time() > float(t.get("exp", 0)):
        print(f"[editor] magic link refused from {ip}")
        return HTMLResponse("<h3>This login link is invalid or expired.</h3>", status_code=403)
    t["used"] = time.time(); t["usedFrom"] = ip
    with open(_MAGIC_PATH, "w") as f:
        _json.dump(tokens, f)
    name = t.get("name") or "OwnerName"
    print(f"[editor] magic link login OK for {name!r} from {ip}")
    await _discord_notify(f"**{name}** signed into the 2.31.0-blaze editor with a one-time link [{ip}]")
    resp = RedirectResponse("/editor/", status_code=302)
    resp.set_cookie(_COOKIE, _sign(name, ip), max_age=_TTL, httponly=True, samesite="lax")
    return resp


@router.post("/login")
async def editor_login(request: Request):
    if EDITOR_LOCKED:
        print("[editor] login refused (503): editor locked")
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
    if not info.get("vpn"):
        from services import geo as _geo
        info["vpn"] = await _geo.vpn_check(ip)
    if info.get("vpn"):
        print(f"[editor] login refused (VPN {info.get('vpn')}) for {name!r} from {ip}")
        await _log_editor_ip("vpn", name, None, info)
        await _discord_notify(f"BLOCKED editor login as **{name or 'someone'}** from a VPN [{ip}] ({_where(info)})")
        return JSONResponse({"ok": False, "error": "Turn off your VPN to use the editor. On iPhone that includes "
                                                   "iCloud Private Relay: Settings > your name > iCloud > Private "
                                                   "Relay > off (or Turn Off Until Tomorrow). Also turn off any "
                                                   "1.1.1.1 / WARP app."}, status_code=403)

    async def _wrong_code(acct=None, note=""):
        # Auto-ban an IP that spams wrong passwords (brute-force / raider).
        banned_now = await record_failed_login(ip)
        await _log_editor_ip("wrong-code", name, acct, info, autoBanned=bool(banned_now), typed=_typed_view(pw))
        if banned_now:
            await _discord_notify(
                f"Auto-banned IP {ip} ({_where(info)}) - too many wrong editor passwords "
                f"(last tried as '{name or 'someone'}')"
            )
            return JSONResponse(
                {"ok": False, "error": "Too many failed attempts. Your IP has been banned."},
                status_code=403,
            )
        print(f"[editor] wrong password for {name!r} from {ip}{note}")
        await _discord_notify(f"{name or 'someone'} failed to log into the editor (wrong password{note}) [{ip}] ({_where(info)})")
        return JSONResponse({"ok": False, "error": "Wrong password."}, status_code=401)

    pw_rotating = (not _ROTATING_PW_OFF) and hmac.compare_digest(pw, _editor_password())
    pw_fixed = not pw_rotating and bool(_MASTER_PW) and hmac.compare_digest(pw, _MASTER_PW)
    # Personal one-time code from /editor in Discord (owner 2026-11-02): only the
    # Discord linked to the account can get it, so it proves who is signing in.
    otp = None
    if not (pw_rotating or pw_fixed) and re.fullmatch(r"\d{8}", pw):
        otp = await _db["editor_otp"].find_one(
            {"h": hashlib.sha256(pw.encode()).hexdigest(), "used": False, "exp": {"$gt": int(time.time())}})
    pw_personal = bool(otp)
    if not (pw_rotating or pw_fixed or pw_personal) and re.fullmatch(r"\d{8}", pw) and \
            await _db["editor_otp"].find_one({"h": hashlib.sha256(pw.encode()).hexdigest()}, {"_id": 1}):
        # A real /editor code that was already used or expired: not a guess, no auto-ban.
        await _log_editor_ip("otp-stale", name, None, info)
        return JSONResponse({"ok": False, "error": "That code was already used or is older than 10 minutes. "
                                                   "Run /editor in the Discord for a new one."}, status_code=401)
    if not (pw_rotating or pw_fixed or pw_personal):
        # Live is owner-only with the shared 6-digit code turned off. A data manager
        # typing that code (the 1.31 one, same secret) is not guessing: tell them to
        # use 1.31 instead of "Wrong password", and don't count it toward the IP
        # auto-ban (it banned real data managers on both servers, 2026-10-27).
        if _ROTATING_PW_OFF and any(hmac.compare_digest(pw, _editor_password(_pw_window() - k))
                                    for k in (0, 1)):
            await _log_editor_ip("live-owner-only", name, None, info)
            await _discord_notify(f"{name or 'someone'} tried the 1.31 code on the live editor "
                                  f"(live is owner-only) [{ip}] ({_where(info)})")
            return JSONResponse({"ok": False, "error": "The Live editor is owner-only right now. "
                                 "Data managers: pick the 1.31.0 tab above and sign in there."},
                                status_code=403)
        return await _wrong_code()

    # Username gate: the password alone is NOT enough. The typed name must map to
    # a real account, and (when EDITOR_ADMINS is set) that account must be on the
    # admin allowlist. This blocks "password only" logins where the name is blank
    # or unknown — the password + IP auto-ban is no longer the sole protection.
    if not name:
        print(f"[editor] login refused (401) for {name!r} from {ip}")
        await _log_editor_ip("no-name", "", None, info)
        await _discord_notify(f"BLOCKED editor login: no username given [{ip}] ({_where(info)})")
        return JSONResponse({"ok": False, "error": "Enter your in-game username."}, status_code=401)
    doc = await _name_exists(name)
    if not doc and os.getenv("CARRYOVER_DB"):
        # name as known on the 1.31 server (players' names can differ on 2.4)
        try:
            from services.db import client as _mc
            esc = re.escape(name)
            doc = await _mc[os.getenv("CARRYOVER_DB")]["accounts"].find_one(
                {"$or": [{"displayName": {"$regex": f"^{esc}$", "$options": "i"}},
                         {"oculusID": {"$regex": f"^{esc}$", "$options": "i"}},
                         {"discordUsername": {"$regex": f"^{esc}$", "$options": "i"}},   # 1.31 /link
                         {"accountID": name}]},
                {"_id": 0, "accountID": 1, "displayName": 1, "discordID": 1})
        except Exception:
            doc = None
    acct_id = doc.get("accountID") if doc else None
    disc_id = doc.get("discordID") if doc else None
    if pw_personal:
        from services.db import client as _mc
        _ids = otp.get("accountIDs") or []
        if acct_id not in _ids:
            _odoc = None
            for _col in [accounts] + ([_mc[os.getenv("CARRYOVER_DB")]["accounts"]] if os.getenv("CARRYOVER_DB") else []):
                _odoc = await _col.find_one({"accountID": {"$in": _ids}},
                                            {"_id": 0, "accountID": 1, "displayName": 1, "discordID": 1})
                if _odoc:
                    break
            doc = _odoc
            acct_id = (doc or {}).get("accountID")
        disc_id = otp.get("discordID")

    # Authorised to use the editor if EITHER:
    #  (a) the account is a hardcoded super-admin (EDITOR_ADMINS), or
    #  (b) its linked Discord account has the "data manager" role.
    allowed = bool(acct_id and acct_id in EDITOR_ADMINS)
    how = "admin"
    if not allowed and disc_id and not EDITOR_OWNER_ONLY:
        from services.discord_roles import has_editor_role
        if await has_editor_role(disc_id):
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

    # The fixed code (EDITOR_PW) is the owner's own (owner 2026-10-12: "my code"). It
    # only works for the owner's accounts, from a network they play Yeeps on; for
    # anyone else, or the owner somewhere new, it is a wrong code (checked before
    # the name gate so its answer doesn't reveal that the code was right).
    # Owner 2026-10-18: no home-network rule (the fixed code failed on the owner's
    # phone on mobile data and auto-banned it); the code still only works for the
    # owner/admin accounts (EDITOR_ADMINS).
    if pw_fixed and acct_id not in EDITOR_ADMINS:
        return await _wrong_code(acct_id, ", fixed code is for admin accounts only")

    # The shared 6-digit code is for data managers. Admin accounts sign in with the
    # admin password, so a leaked code can't be used to log in as the owner (that is
    # how the Photon bot was taken over, 2026-10-27).
    if (pw_rotating or pw_personal) and acct_id in EDITOR_ADMINS:
        await _log_editor_ip("admin-needs-pw", name, acct_id, info)
        await _discord_notify(f"BLOCKED editor login as admin **{name}** with the shared code [{ip}] ({_where(info)})")
        return JSONResponse({"ok": False, "error": "Admin accounts sign in with the admin password, "
                             "not the shared code."}, status_code=403)

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

    # The shared code + a typed name let anyone sign in as any data manager (owner
    # 2026-11-02: native did this). Data managers now need their personal code
    # from /editor in Discord. Not counted toward the IP auto-ban.
    if how == "data-manager role" and not pw_personal:
        await _log_editor_ip("dm-needs-personal-code", name, acct_id, info)
        return JSONResponse(
            {"ok": False,
             "error": "Data managers sign in with their own code now: run /editor in the Discord "
                      "and put that code in the password box."},
            status_code=403)

    label = (doc.get("displayName") if doc else None) or name
    clear_failures(ip)
    resp = JSONResponse({"ok": True})
    print(f"[editor] login OK for {name!r} from {ip}")
    if pw_personal:
        await _db["editor_otp"].update_one({"_id": otp["_id"]}, {"$set": {"used": True, "usedBy": acct_id, "usedIP": ip}})
        how = "personal code"
    resp.set_cookie(_COOKIE, _sign(label if pw_personal else (name or label), ip), max_age=_TTL, httponly=True, samesite="lax")
    # Any network is allowed, but say so loudly when it isn't one this account
    # plays Yeeps from (the leaked-code intrusion on Oct 9 came from elsewhere).
    known = await _play_ips(acct_id)
    plays_here = ip in known or info["net"] in {_ip_net(x) for x in known}
    await _log_editor_ip("login", label, acct_id, info, via=how, playsHere=plays_here)
    await _discord_notify(f"**{label}** (accountID: `{acct_id}`, via {how}) logged into the editor "
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
    if not _authed_name(request):
        return HTMLResponse(content=_LOGIN_HTML, headers=_NO_CACHE)
    try:
        with open(_UI_PATH, "r", encoding="utf-8") as f:
            html = f.read()
        # Restricted tabs (owner 2026-10-30: inspect element could unhide Photon): their button
        # and code are cut out of the page for anyone not allowed, instead of only hidden.
        # The APIs behind them check the same rights server-side anyway.
        allowed = {"photon": await _photon_ok(request), "talk": await _photon_ok(request),
                   "liveview": await _view_ok(request), "roles": await _is_role_admin(request)}
        for k, ok in allowed.items():
            if not ok:
                html = re.sub(rf"<!--@cut {k}-->.*?<!--@end {k}-->", "", html, flags=re.S)
                html = re.sub(rf"/\*@cut {k}\*/.*?/\*@end {k}\*/", "", html, flags=re.S)
        return HTMLResponse(content=html, headers=_NO_CACHE)
    except FileNotFoundError:
        # Fall back to the legacy inline UI if the new static file is missing.
        return HTMLResponse(content=_HTML, headers=_NO_CACHE)


if __name__ == "__main__":
    editor_app.include_router(router)
    uvicorn.run("routers.editor_router:editor_app", host="0.0.0.0", port=8001, reload=True)


# ─── Photon bot / Talk tab (biglive) ──────────────────────────────────────────
# Port of the 1.31 editor's Photon tab for the biglive (2.31.4) server: control
# the Photon bot (yeeps-bot, biglive env + talk port 8792 via services/photon_bot
# .py) and talk to players live from the editor's Talk panel. The whole editor
# /api/ is already behind _guard (signed-in staff only); _photon_ok repeats the
# check for the WebSocket, which has its own router (no _guard).
_player_statuses_col = _db["player_statuses"]
_EDITOR_ORIGINS = {"https://live.your-domain.example", "https://play.your-domain.example",
                   "http://127.0.0.1:8240", "http://localhost:8240"}


# ONLY these editor sign-in names get the Photon + Talk tabs (owner-only, not
# every data manager). Override via env PHOTON_OWNERS (comma-separated names).
PHOTON_OWNERS = set(filter(None, (n.strip() for n in
    (os.getenv("PHOTON_OWNERS") or "OwnerName,Staff Member A,StaffAlias").split(","))))
# Account-ID gate for the Photon tab (robust vs the name-based list: a user may
# sign in with their oculusID instead of displayName). Defaults to the editor
# admins (owner x2 + staff member A o_STAFF00000000002). Override via PHOTON_ADMINS.
PHOTON_ADMINS = set(filter(None, (a.strip() for a in
    (os.getenv("PHOTON_ADMINS")
     or "o_OWNER0000000001,o_STAFF00000000002").split(","))))


async def _photon_ok(request) -> bool:
    """Photon/Talk tab gate: a signed-in editor user whose login name is in
    PHOTON_OWNERS, or whose account resolves to a PHOTON_ADMINS id (robust to
    oculusID-vs-displayName). Works for a WebSocket too (it carries .cookies)."""
    try:
        name = _authed_name(request) or ""
        if not name:
            return False
        if name in PHOTON_OWNERS:
            return True
        doc = await _name_exists(name)
        return bool(doc and doc.get("accountID") in PHOTON_ADMINS)
    except Exception:
        return False


async def _photon_guard(request: Request) -> None:
    if not await _photon_ok(request):
        raise HTTPException(status_code=404, detail="Not Found")


async def _online_players() -> list:
    now = datetime.now(timezone.utc).strftime("%Y %m/%d %H:%M:%S")
    sts = [s async for s in _player_statuses_col.find(
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
    from services import photon_bot
    body = await request.json()
    target = str(body.get("target") or "").strip()
    online = {p["accountID"]: p for p in await _online_players()}
    if target not in online:
        raise HTTPException(status_code=400, detail="that player isn't online right now")
    p = online[target]
    cmd = f"mimic {target}"
    if await photon_bot.running() and photon_bot.ready() and p["photonRoom"] \
            and photon_bot.current_room() == p["photonRoom"] and not photon_bot.code_newer():
        if not photon_bot.send(cmd):
            raise HTTPException(status_code=409, detail="the bot isn't listening")
        return JSONResponse(content={"ok": True, "moved": False, "name": p["name"]})
    who = _authed_name(request) or "?"
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
        m = photon_bot._meta()
        res = await photon_bot.start(m.get("target") or "", m.get("targetName") or "",
                                     bool(m.get("stay")), _authed_name(request) or "?")
        if res.get("ok"):
            import asyncio
            t = asyncio.get_running_loop().create_task(photon_bot.send_when_ready(cmd))
            _photon_tasks.add(t)
            t.add_done_callback(_photon_tasks.discard)
            return JSONResponse(content={"ok": True, "reloading": True})
    if not photon_bot.send(cmd):
        raise HTTPException(status_code=409, detail="the bot isn't running")
    print(f"[photon] {_authed_name(request)}: {cmd}")
    return JSONResponse(content={"ok": True})


@router.post("/api/photon/stop", dependencies=[Depends(_photon_guard)])
async def photon_stop(request: Request):
    from services import photon_bot
    res = await photon_bot.stop()
    if res.get("was"):
        await _discord_notify(f"**{_authed_name(request) or '?'}** stopped the Photon bot")
    return JSONResponse(content=res)


# ---- View tab (owner 2026-10-27): a live 3D look at the bot's room -------------
# The bot writes view.json (players, 4x a second) and view_blocks.json (blocks,
# when the map changes) into its state dir; the page (static/editor_view.html)
# polls them and sends single-block edits back as bot commands (vput / vdel).
# Same owner-only gate as the Photon tab.
# Only the owner gets the View tab (owner 2026-10-27: "only i should have the view tab"),
# a narrower list than the Photon/Talk owners. Override via env VIEW_ADMINS.
VIEW_ADMINS = set(filter(None, (a.strip() for a in
    (os.getenv("VIEW_ADMINS") or "o_OWNER0000000001").split(","))))


VIEW_ON_SITE = os.getenv("VIEW_ON_SITE", "0") == "1"


async def _view_ok(request) -> bool:
    # Off the site (owner 2026-10-29: "i dont like it being on the editor site"): the View is
    # now a program on the owner's PC, tools/view_local.py, which calls these handlers directly.
    if not VIEW_ON_SITE:
        return False
    try:
        if not await _photon_ok(request):
            return False
        doc = await _name_exists(_authed_name(request) or "")
        return bool(doc and doc.get("accountID") in VIEW_ADMINS)
    except Exception:
        return False


async def _view_guard(request: Request) -> None:
    if not await _view_ok(request):
        raise HTTPException(status_code=404, detail="Not Found")


_VIEW_PAT = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_VIEW_COL = re.compile(r"^[a-z_]{1,24}$")
_view_said: dict = {}


@router.get("/view", response_class=HTMLResponse)
async def view_page(request: Request):
    if not _authed_name(request):
        return RedirectResponse("/editor/login", status_code=302)
    if not await _view_ok(request):
        raise HTTPException(status_code=404, detail="Not Found")
    page = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "editor_view.html")
    return HTMLResponse(content=open(page, encoding="utf-8").read(), headers={"Cache-Control": "no-store"})


# Real block models/textures for the View: the 1.31 Map Maker's assets (extracted from
# the game files), served read-only here so the page loads them same-origin.
_MAPMAKER_ASSETS = os.path.realpath(os.getenv("MAPMAKER_ASSETS") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "..", "yeeps-proxy", "vr-server",
    "static", "mapmaker-assets"))
# The real Yeep player rig (skeleton + skinned body/hands + eyes), exported from the
# Photon bot Unity project by tools/export_yeep_rig.py.
_VIEW_YEEP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "view-yeep.json")


@router.get("/view/a/{path:path}")
async def view_asset(path: str):
    from fastapi.responses import FileResponse, Response
    if path == "yeep.json":
        f = _VIEW_YEEP
    else:
        f = os.path.realpath(os.path.join(_MAPMAKER_ASSETS, path))
        if not f.startswith(_MAPMAKER_ASSETS + os.sep):
            return Response(status_code=404)
    if not os.path.isfile(f):
        return Response(status_code=404)
    mt = {".png": "image/png", ".json": "application/json"}.get(os.path.splitext(f)[1], "application/octet-stream")
    return FileResponse(f, media_type=mt, headers={"Cache-Control": "public, max-age=3600"})


_view_looks: dict = {}      # accountID -> (time, skin hex, eye hex)


def _rgba_hex(v):
    """Yeeps colour int32 (R<<24|G<<16|B<<8|A, signed) -> '#rrggbb'."""
    try:
        u = int(v) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return None
    return f"#{(u >> 24) & 255:02x}{(u >> 16) & 255:02x}{(u >> 8) & 255:02x}"


_view_dims: dict = {}       # roomKey -> (time, [x, y, z]) as the game gets it


def _dims_list(d):
    """A DynamoDB-typed or plain dimensions value -> [x, y, z] ints, or None."""
    try:
        if isinstance(d, dict):
            if "L" in d:
                d = [x.get("N") for x in d["L"]]
            elif "S" in d:
                d = json.loads(d["S"])
            elif "NS" in d:
                d = d["NS"]
        if isinstance(d, str):
            d = json.loads(d)
        if isinstance(d, (list, tuple)) and len(d) == 3:
            out = [int(float(v)) for v in d]
            return out if all(0 < v <= 1024 for v in out) else None
    except Exception:
        pass
    return None


async def _view_room_dims(rk: str):
    """The room's real size: the same G2_Rooms read the game makes, then the same
    defaults the game falls back to (pw 32x80x32 / 80x80x80, a community world's
    real size, 128x96x128 for any other room with no size saved). The bot used to
    guess 96x96x96 for every room without a saved size, so big maps were cut off
    and players drawn in the wrong place."""
    if not rk:
        return None
    hit = _view_dims.get(rk)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    dims = None
    try:
        from services.dynamo_ops import handle_get_item
        txt, _ = await handle_get_item({"TableName": "G2_Rooms", "Key": {"roomKey": {"S": rk}}})
        dims = _dims_list((json.loads(txt).get("Item") or {}).get("dimensions"))
    except Exception as e:
        print(f"[view] room size read failed for {rk}: {e!r}")
    if not dims and rk.startswith("c_"):
        try:
            from services.dynamo_ops import _cw_fetch_room
            dims = _dims_list(((await _cw_fetch_room(rk)) or {}).get("dimensions"))
        except Exception:
            dims = None
    if not dims:
        if rk.startswith(("p_", "o_", "c_")):
            dims = [32, 80, 32] if rk.endswith("_sandbox") else [80, 80, 80]
        else:
            dims = [128, 96, 128]
    _view_dims[rk] = (time.time(), dims)
    return dims


_view_dims_sent: dict = {}  # bot sid -> roomKey it was last told the size of


@router.get("/api/photon/view", dependencies=[Depends(_view_guard)])
async def photon_view():
    from services import photon_bot
    v = photon_bot.view_state()
    # Use the room's real size. Players come in as grid positions centred on the
    # bot's idea of the size, so shift them over when the real size differs, and
    # tell the bot once (its own placing / range checks use the size too).
    try:
        real = await _view_room_dims(v.get("rk") or "")
        botd = _dims_list(v.get("dims"))
        if real and botd and real != botd:
            dx, dz = (real[0] - botd[0]) / 2, (real[2] - botd[2]) / 2
            for p in v.get("players") or []:
                if isinstance(p.get("x"), (int, float)):
                    p["x"] = round(p["x"] + dx, 3)
                if isinstance(p.get("z"), (int, float)):
                    p["z"] = round(p["z"] + dz, 3)
            v["dims"] = real
            sid = v.get("sid") or ""
            if _view_dims_sent.get(sid) != v.get("rk"):
                if photon_bot.send(f"vdims {real[0]} {real[1]} {real[2]}"):
                    _view_dims_sent[sid] = v.get("rk")
        elif real:
            v["dims"] = real
    except Exception as e:
        print(f"[view] size fix failed: {e!r}")
    # each player's real skin + eye colour (cached a minute)
    now = time.time()
    ids = [p.get("id") for p in v.get("players") or [] if p.get("id")]
    need = [i for i in ids if now - _view_looks.get(i, (0,))[0] > 60]
    if need:
        async for a in accounts.find({"accountID": {"$in": need}},
                                     {"_id": 0, "accountID": 1, "skinColor": 1, "eyeColor": 1, "activeCosmetics": 1}):
            cos = [str(c) for c in (a.get("activeCosmetics") or []) if not str(c).startswith("roleBadge")][:24]
            _view_looks[a["accountID"]] = (now, _rgba_hex(a.get("skinColor")), _rgba_hex(a.get("eyeColor")), cos)
        for i in need:
            _view_looks.setdefault(i, (now, None, None, []))
    for p in v.get("players") or []:
        look = _view_looks.get(p.get("id"))
        if look:
            p["skin"], p["eye"], p["cos"] = look[1], look[2], look[3]
    return JSONResponse(content={"now": now, "view": v})


@router.get("/api/photon/view/blocks", dependencies=[Depends(_view_guard)])
async def photon_view_blocks(rev: int = -1, sid: str = ""):
    from services import photon_bot
    b = photon_bot.view_blocks()
    if b and b.get("rev") == rev and b.get("sid") == sid:
        return JSONResponse(content={"same": True})
    return JSONResponse(content=b or {"rev": -1, "sid": "", "blocks": []})


_view_catalog: dict = {}


@router.get("/api/photon/view/patterns", dependencies=[Depends(_view_guard)])
async def photon_view_patterns():
    """Every placeable block (owner 2026-10-27: "make it so i have the full list of
    blocks"): the Map Maker catalog (all of it exists on live) + the bot's known
    sizes + any other live block-shaped key, with sizes for the ones the Map Maker
    index doesn't have."""
    if _view_catalog.get("patterns"):
        return JSONResponse(content=_view_catalog)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    def _load(path, default):
        try:
            return json.load(open(path))
        except Exception:
            return default
    live = {k for k in _load(os.path.join(here, "data", "live2314_patterns.json"), []) if isinstance(k, str)}
    index = _load(os.path.join(_MAPMAKER_ASSETS, "index.json"), {})
    known = _load(os.path.join(here, "..", "..", "yeeps-bot", "media", "known_sizes.json"), {})
    pats = set(index) | (set(known) & live) | {k for k in live if re.match(r"^\d+x\d+(x\d+)?_", k)
                                                or k.startswith("decoration_")}
    sizes = {k: known[k] for k in pats if k not in index and isinstance(known.get(k), list)}
    _view_catalog.update(patterns=sorted(pats, key=str.lower), sizes=sizes)
    return JSONResponse(content=_view_catalog)


@router.post("/api/photon/view/edit", dependencies=[Depends(_view_guard)])
async def photon_view_edit(request: Request):
    from services import photon_bot
    body = await request.json()
    op = body.get("op")
    try:
        x, y, z = (int(body.get(k)) for k in ("x", "y", "z"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="bad cell")
    if not all(0 <= v < 512 for v in (x, y, z)):
        raise HTTPException(status_code=400, detail="that spot is outside the room")
    if op == "put":
        pat, col = str(body.get("pattern") or ""), str(body.get("color") or "white")
        if not _VIEW_PAT.match(pat) or not _VIEW_COL.match(col):
            raise HTTPException(status_code=400, detail="pick a block and a color")
        try:
            fwd, up = int(body.get("fwd", 4)), int(body.get("up", 2))
        except (TypeError, ValueError):
            fwd, up = 4, 2
        if not (0 <= fwd < 6 and 0 <= up < 6) or fwd // 2 == up // 2:      # facing + up must be perpendicular
            raise HTTPException(status_code=400, detail="bad facing")
        cmd = f"vput {pat} {x} {y} {z} {col} {fwd} {up}"
    elif op == "del":
        cmd = f"vdel {x} {y} {z}"
    else:
        raise HTTPException(status_code=400, detail="op must be put or del")
    if not photon_bot.send(cmd):
        raise HTTPException(status_code=409, detail="the bot isn't running - pick a player and press Watch")
    # Every click lands in the audit log; Discord gets one line per person + room
    # every 5 minutes instead of one per block.
    who = _authed_name(request) or "?"
    room = photon_bot.view_state().get("rk") or "?"
    if time.time() - _view_said.get((who, room), 0) > 300:
        _view_said[(who, room)] = time.time()
        await _discord_notify(f"**{who}** is building in **{room}** from the editor's View tab")
    else:
        mark = _notified_cv.get()
        if mark is not None:
            mark.append(1)
    return JSONResponse(content={"ok": True})


# Live talk: the owner's mic + the players' voices, relayed between the editor
# page and the Photon bot's talk socket (127.0.0.1:TALK_PORT). A WebSocket can't
# go through the main router's _guard, so it has its own router + checks here.
ws_router = APIRouter()
TALK_PORT = int(os.getenv("YEEPS_TALK_PORT", "8792"))


# ---- RPC recorder (owner 2026-10-18): record what players do in the bot's room ----
# The bot logs every RPC / spawn it receives to yeeps-bot/logs/events-*.jsonl (method
# names already decoded with the live RpcList, plus arg types). Start marks the time;
# Stop saves every rpc/instantiate/join event since then to yeeps-bot/recordings/.
_REC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "..", "yeeps-bot")
_REC_STATE = os.path.expanduser("~/.local/state/yeeps/photon-bot-240/recording.json")


@router.post("/api/photon/record/start", dependencies=[Depends(_photon_guard)])
async def photon_record_start(request: Request):
    import re as _re, time as _t
    try:
        body = await request.json()
    except Exception:
        body = {}
    label = _re.sub(r"[^A-Za-z0-9_-]+", "_", str(body.get("label") or "rec"))[:40] or "rec"
    os.makedirs(os.path.dirname(_REC_STATE), exist_ok=True)
    json.dump({"label": label, "start": _t.time()}, open(_REC_STATE, "w"))
    return JSONResponse(content={"ok": True, "label": label})


@router.post("/api/photon/record/stop", dependencies=[Depends(_photon_guard)])
async def photon_record_stop():
    import glob as _g, time as _t, collections as _c
    try:
        st = json.load(open(_REC_STATE))
    except Exception:
        raise HTTPException(status_code=400, detail="not recording")
    start, end = float(st["start"]), _t.time()
    logs = sorted(_g.glob(os.path.join(_REC_DIR, "logs", "events-*.jsonl")), key=os.path.getmtime)
    logs = [f for f in logs if os.path.getmtime(f) >= start]
    os.makedirs(os.path.join(_REC_DIR, "recordings"), exist_ok=True)
    name = f"{st['label']}-{_t.strftime('%Y%m%d-%H%M%S', _t.localtime(start))}.jsonl"
    out_path = os.path.join(_REC_DIR, "recordings", name)
    counts, n = _c.Counter(), 0
    with open(out_path, "w") as out:
        for f in logs:
            with open(f, errors="replace") as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if not (start <= float(e.get("t", 0)) <= end) or e.get("kind") not in ("rpc", "instantiate", "join", "leave", "joined", "event"):
                        continue
                    out.write(line if line.endswith("\n") else line + "\n")
                    n += 1
                    counts[(e.get("v") or {}).get("method") or e.get("kind")] += 1
    os.remove(_REC_STATE)
    return JSONResponse(content={"ok": True, "file": name, "events": n, "seconds": int(end - start),
                                 "top": counts.most_common(15)})


@router.get("/api/photon/record/live", dependencies=[Depends(_photon_guard)])
async def photon_record_live(after: float = 0, limit: int = 80):
    """Events since the recording started (or since `after`), newest last, for the
    Photon tab's live view while recording. Reads only the tail of the bot's log."""
    import glob as _g
    try:
        st = json.load(open(_REC_STATE))
    except Exception:
        return JSONResponse(content={"recording": False, "events": []})
    since = max(float(st["start"]), float(after or 0))
    logs = sorted(_g.glob(os.path.join(_REC_DIR, "logs", "events-*.jsonl")), key=os.path.getmtime)[-1:]
    out = []
    for f in logs:
        try:
            with open(f, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 400_000))
                chunk = fh.read().decode("utf-8", "replace").splitlines()[1:]
        except Exception:
            continue
        for line in chunk:
            try:
                e = json.loads(line)
            except Exception:
                continue
            t = float(e.get("t", 0))
            if t <= since or e.get("kind") not in ("rpc", "instantiate", "join", "leave"):
                continue
            v = e.get("v") or {}
            if e.get("kind") == "rpc":
                args = ", ".join(str(a)[:40] for a in (v.get("args") or [])[:20])
                txt = f"#{v.get('sender')} {v.get('method')}({args})"
            elif e.get("kind") == "instantiate":
                txt = f"spawn {(v.get('prefab') if isinstance(v, dict) else str(v))}"[:160]
            else:
                txt = f"{e.get('kind')} {str(v)[:120]}"
            out.append({"t": t, "txt": txt[:600]})
    return JSONResponse(content={"recording": True, "label": st.get("label"), "events": out[-limit:]})


@router.get("/api/photon/recordings", dependencies=[Depends(_photon_guard)])
async def photon_recordings():
    import glob as _g
    rec = None
    try:
        rec = json.load(open(_REC_STATE))
    except Exception:
        pass
    files = sorted(_g.glob(os.path.join(_REC_DIR, "recordings", "*.jsonl")), key=os.path.getmtime, reverse=True)[:20]
    return JSONResponse(content={"recording": rec, "files": [
        {"name": os.path.basename(f), "kb": os.path.getsize(f) // 1024} for f in files]})


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
    for i in range(80):                       # the bot may still be starting / following into a room
        try:
            bot_ws = await websockets.connect(f"ws://127.0.0.1:{TALK_PORT}", max_size=1 << 20)
            break
        except Exception:
            if i % 10 == 0:
                try:
                    await ws.send_text(json.dumps({"type": "status", "msg": "waiting for the bot..."}))
                except Exception:
                    return
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
            pass
        return
    print(f"[photon] {_authed_name(ws)}: talk connected")

    async def up():
        while True:
            m = await ws.receive()
            if m["type"] == "websocket.disconnect":
                return
            if m.get("bytes"):
                await bot_ws.send(m["bytes"])
            elif m.get("text"):                  # View tab camera ({"type":"cam",...})
                await bot_ws.send(m["text"][:500])

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


# ---- Owner hand menu -> Photon bot (owner 2026-11-01) ----------------------------
# Blaze's PRIVATE in-game hand menu drives the same bot as the Photon tab. No editor
# session; instead ALL of: X-Menu-Key == ~/.config/yeeps/owner-menu-key.txt, body
# "account" in OWNER_MENU_ACCOUNTS, the request's Cloudflare IP is one that account
# logged into the GAME from (lastIP/recentIPs), and a legit editor Host. So a leaked
# key alone is useless. 5 req/s per IP; 5 bad keys from an IP = locked out 1 hour.
menu_router = APIRouter()
OWNER_MENU_ACCOUNTS = set(filter(None, (os.getenv("OWNER_MENU_ACCOUNTS") or
    "o_OWNER0000000001").replace(" ", "").split(",")))
_MENU_KEY_FILE = os.path.expanduser(os.getenv("OWNER_MENU_KEY_FILE") or "~/.config/yeeps/owner-menu-key.txt")
_menu_hits: dict = {}
_menu_bad: dict = {}


def _menu_key() -> str:
    try:
        return open(_MENU_KEY_FILE).read().strip()
    except OSError:
        return ""


async def _menu_auth(request: Request) -> dict:
    import hmac
    ip = request.headers.get("cf-connecting-ip", "").strip()
    now = time.time()
    if not ip or not _editor_host_ok(request):
        raise HTTPException(status_code=404, detail="Not Found")
    bad = [t for t in _menu_bad.get(ip, []) if now - t < 3600]
    _menu_bad[ip] = bad
    if len(bad) >= 5:
        raise HTTPException(status_code=404, detail="Not Found")
    hits = [t for t in _menu_hits.get(ip, []) if now - t < 1.0]
    if len(hits) >= 5:
        raise HTTPException(status_code=429, detail="slow down")
    _menu_hits[ip] = hits + [now]
    key = _menu_key()
    given = request.headers.get("x-menu-key", "")
    if not key or not hmac.compare_digest(given.encode(), key.encode()):
        _menu_bad[ip] = bad + [now]
        await _menu_log(request, {"_ip": ip}, f"wrong key ({len(given)} chars)", name="UNKNOWN (wrong menu key)")
        await _discord_notify(f"BLOCKED hand menu call with a wrong key from `{ip}` ({_where(_ip_info(ip))})"
                              + (" - locked out for 1 hour" if len(_menu_bad[ip]) == 5 else ""))
        raise HTTPException(status_code=404, detail="Not Found")
    try:
        body = await request.json() if request.method == "POST" else {}
    except Exception:
        body = {}
    acct = str(body.get("account") or request.query_params.get("account") or "").strip()
    if acct not in OWNER_MENU_ACCOUNTS:
        await _menu_log(request, {"_ip": ip}, f"account={acct[:40]} not allowed", name="UNKNOWN (right key, wrong account)")
        await _discord_notify(f"BLOCKED hand menu call: right key but account `{acct[:40]}` from `{ip}` ({_where(_ip_info(ip))}). The menu key may have leaked.")
        raise HTTPException(status_code=403, detail="not allowed")
    doc = await accounts.find_one({"accountID": acct}, {"_id": 0, "lastIP": 1, "recentIPs": 1, "displayName": 1}) or {}
    if ip not in ({doc.get("lastIP")} | set(doc.get("recentIPs") or [])):
        await _menu_log(request, {"_ip": ip}, f"account={acct} from an IP it doesn't play from", name="UNKNOWN (right key, new IP)")
        await _discord_notify(f"BLOCKED hand menu call for `{acct}` from `{ip}` ({_where(_ip_info(ip))}): right key, "
                              f"but not an IP that account plays from. If this wasn't you, the menu key leaked.")
        raise HTTPException(status_code=403, detail="play Yeeps on this network first")
    body["_ip"] = ip
    return body


async def _menu_log(request: Request, body: dict, detail: str = "", name: str = "Blaze (hand menu)") -> None:
    rec = {"at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "name": name,
           "ip": body.get("_ip", "?"), "method": request.method, "path": request.url.path, "detail": detail}
    _append_action_file(rec)
    try:
        await editor_actions.insert_one(dict(rec))
    except Exception as e:
        print(f"[menu] action db log failed (kept on disk): {e}")


@menu_router.api_route("/api/menu/photon/status", methods=["GET", "POST"])
async def menu_photon_status(request: Request):
    from services import photon_bot
    await _menu_auth(request)
    m = photon_bot._meta() if await photon_bot.running() else {}
    return JSONResponse(content={"running": await photon_bot.running(), "ready": photon_bot.ready(),
                                 "room": photon_bot.current_room(), "targetName": m.get("targetName") or ""})


@menu_router.post("/api/menu/photon/start")
async def menu_photon_start(request: Request):
    from services import photon_bot
    body = await _menu_auth(request)
    target = str(body.get("account"))
    online = {p["accountID"]: p for p in await _online_players()}
    if target not in online:
        raise HTTPException(status_code=400, detail="you aren't online right now")
    who = "Blaze (hand menu)"
    res = await photon_bot.start(target, online[target]["name"], True, who)
    if not res.get("ok"):
        raise HTTPException(status_code=500, detail=res.get("error") or "couldn't start the bot")
    p = online[target]
    await _menu_log(request, body, f"start in {p['roomKey']}")
    await _discord_notify(f"**{who}** hooked the Photon bot onto **{p['name']}** in "
                          f"{p['roomName'] or p['roomKey']} (staying in that room)")
    return JSONResponse(content={"ok": True})


@menu_router.post("/api/menu/photon/cmd")
async def menu_photon_cmd(request: Request):
    from services import photon_bot
    body = await _menu_auth(request)
    cmd = " ".join(str(body.get("cmd") or "").split())[:200]
    if not cmd:
        raise HTTPException(status_code=400, detail="empty command")
    if cmd.lower() in ("quit", "exit", "q"):
        raise HTTPException(status_code=400, detail="use stop")
    await _menu_log(request, body, f"cmd={cmd}")
    if await photon_bot.running() and photon_bot.code_newer():
        m = photon_bot._meta()
        res = await photon_bot.start(m.get("target") or "", m.get("targetName") or "",
                                     bool(m.get("stay")), "Blaze (hand menu)")
        if res.get("ok"):
            import asyncio
            t = asyncio.get_running_loop().create_task(photon_bot.send_when_ready(cmd))
            _photon_tasks.add(t)
            t.add_done_callback(_photon_tasks.discard)
            return JSONResponse(content={"ok": True, "reloading": True})
    if not photon_bot.send(cmd):
        raise HTTPException(status_code=409, detail="the bot isn't running")
    print(f"[photon] Blaze (hand menu): {cmd}")
    await _discord_notify(f"**Blaze (hand menu)** bot command from `{body['_ip']}`: `{cmd}`")
    return JSONResponse(content={"ok": True})


@menu_router.post("/api/menu/photon/stop")
async def menu_photon_stop(request: Request):
    from services import photon_bot
    body = await _menu_auth(request)
    res = await photon_bot.stop()
    await _menu_log(request, body, "stop")
    if res.get("was"):
        await _discord_notify("**Blaze (hand menu)** stopped the Photon bot")
    return JSONResponse(content=res)
