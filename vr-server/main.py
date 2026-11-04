"""
Yeeps v1 Private Server API
FastAPI + MongoDB replacement for the original AWS Lambda + DynamoDB backend.

The game client sends requests to three "services" all on the same host.
We detect which service by inspecting the path, headers, and host.
"""

import json
import logging
import re
import os
from datetime import datetime, timezone
from pathlib import Path

from starlette.concurrency import iterate_in_threadpool
from fastapi import FastAPI, Request
from fastapi.responses import Response, JSONResponse

from routers.lambda_router import router as lambda_router
from routers.dynamo_router import router as dynamo_router
from routers.editor_router import router as editor_router
from seed.seed_db import seed_database
from services.replay_log import lookup_replay
from services.db import accounts, globals_col

app = FastAPI(title="Yeeps v1 Private Server")

# ── Abuse protection ─────────────────────────────────────────────────────────
# Stop the MongoDB from being flooded by a public client spamming logins/saves:
# a per-IP request rate limit plus a request-body size cap. In-memory state
# (fine for a single Render instance); generous enough for normal play and the
# burst of calls at room-load. The editor + Discord endpoints are exempt.
import time as _time
from services.build_gate import outdated_message
_RL: dict = {}                          # ip -> [window_start, count]
_RL_MAX = 600                           # max requests per window per IP
_RL_WINDOW = 60                         # window seconds
_MAX_BODY = 12 * 1024 * 1024            # 12 MB max request body
_MAX_VIDEO_BODY = 100 * 1024 * 1024     # editor video uploads (Cloudflare's own cap is 100 MB)

# Hardcoded IP bans (bans made from the editor live in services/ip_bans.py).
# 203.0.113.1 ("banned_player") was lifted on 2026-10-10 at the owner's request.
BLOCKED_IPS: set = set()


@app.middleware("http")
async def abuse_guard(request: Request, call_next):
    # 0) hard IP ban (hardcoded set + persistent auto-bans from ip_bans)
    try:
        from services.ip_bans import client_ip, is_banned
        ip0 = client_ip(request)
        if ip0 in BLOCKED_IPS or await is_banned(ip0):
            # A player banned mid-session stays in their Photon room unless their
            # game hears about the ban, and it only does through its account poll
            # (DynamoDB GetItem -> shouldForceRefresh) and refreshAccountOnline
            # (-> isPermabanned). Let just those through so the ban lands and the
            # game leaves; everything else from a banned IP stays 403.
            # The header alone isn't enough: a raider's script sent X-Amz-Target
            # GetItem on its lambda calls too (Oct 10), so the GetItem pass is
            # only for the DynamoDB endpoint itself.
            tgt = request.headers.get("x-amz-target", "")
            _p = request.url.path
            _dynamo_root = _p == "/" or (_p.startswith(("/k/", "/c/")) and _p.rstrip("/").count("/") <= 4
                                         and "/functions/" not in _p and "/2015-03-31/" not in _p)
            if not ((tgt.endswith(".GetItem") and _dynamo_root) or "g2_refreshAccountOnline" in _p
                    or "g2_fetchAccountDatas" in _p     # 2.4.0's override check
                    or "g2_loginToAccount" in _p):      # login answers with a ban screen (handlers/login.py)
                return JSONResponse({"message": "Forbidden"}, status_code=403)
    except Exception:
        pass
    # 1) reject oversized bodies outright
    try:
        cl = request.headers.get("content-length")
        lim = _MAX_BODY
        if request.method == "POST" and (request.scope.get("path") or "").startswith("/editor/api/video"):
            lim = _MAX_VIDEO_BODY       # editor-only (login checked before the body is read)
        if cl and int(cl) > lim:
            return JSONResponse({"message": "Payload too large"}, status_code=413)
    except Exception:
        pass
    # 2) per-IP rate limit on game traffic (skip admin editor + discord bot)
    try:
        path = request.scope.get("path", "") or ""
        if "/editor" not in path and "/discord" not in path:
            ip = request.client.host if request.client else "?"
            now = _time.time()
            w = _RL.get(ip)
            if w is None or now - w[0] >= _RL_WINDOW:
                _RL[ip] = [now, 1]
            else:
                w[1] += 1
                if w[1] > _RL_MAX:
                    return JSONResponse({"message": "Too Many Requests"}, status_code=429)
            if len(_RL) > 5000:          # bound memory: drop idle IPs
                cutoff = now - _RL_WINDOW
                for k in [k for k, v in list(_RL.items()) if v[0] < cutoff]:
                    _RL.pop(k, None)
    except Exception:
        pass
    return await call_next(request)


# Strip the APK patch prefix so routes still match
_PATCH_PREFIX = "/xxxxxxxxxxxxxxxxxx"

# CORS for the live editor's /editor/login ONLY, so the website (your-domain.example)
# can sign in inline with a credentialed fetch. Origin-locked to your-domain.example
# (echoed, never "*"); allows credentials. This is CORS on the login endpoint only
# — NOT a reverse proxy, so none of the cookie-jar/Host issues from the 2026-10-19
# incident. Login still requires the password + an admin account, and /editor/api/*
# stays host-guarded. (owner-approved 2026-10-19)
_EDITOR_LOGIN_ORIGIN = os.getenv("EDITOR_LOGIN_ORIGIN", "https://your-domain.example")


@app.middleware("http")
async def editor_login_cors(request: Request, call_next):
    if (request.scope.get("path") or "") == "/editor/login":
        origin = request.headers.get("origin", "")
        allow = origin == _EDITOR_LOGIN_ORIGIN
        if request.method == "OPTIONS":
            from starlette.responses import Response as _Resp
            resp = _Resp(status_code=204)
        else:
            resp = await call_next(request)
        if allow:
            resp.headers["Access-Control-Allow-Origin"] = _EDITOR_LOGIN_ORIGIN
            resp.headers["Access-Control-Allow-Credentials"] = "true"
            resp.headers["Access-Control-Allow-Headers"] = "content-type"
            resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
            resp.headers["Vary"] = "Origin"
        return resp
    return await call_next(request)


@app.middleware("http")
async def strip_apk_prefix(request: Request, call_next):
    if request.scope["path"].startswith(_PATCH_PREFIX):
        request.scope["path"] = request.scope["path"][len(_PATCH_PREFIX):]
        request.scope["raw_path"] = request.scope["path"].encode()
    # 131b14+ APKs add the headset's device key: /k/<64 hex>/... (device_auth.py)
    if request.scope["path"].startswith("/k/"):
        rest = request.scope["path"][3:]
        key, _, tail = rest.partition("/")
        request.scope.setdefault("state", {})["device_key"] = key[:64]
        request.scope["path"] = "/" + tail
        request.scope["raw_path"] = request.scope["path"].encode()
    # 1.31+ APKs add a build/integrity tag segment: /c/<tag>/... (build_gate.py)
    if request.scope["path"].startswith("/c/"):
        rest = request.scope["path"][3:]
        tag, _, tail = rest.partition("/")
        request.scope.setdefault("state", {})["client_build_tag"] = tag[:120]
        request.scope["path"] = "/" + tail
        request.scope["raw_path"] = request.scope["path"].encode()
    return await call_next(request)


from config import IS_V2 as _IS_V2
if _IS_V2:
    @app.middleware("http")
    async def force_connection_close(request: Request, call_next):
        """2.x only: the game's AWS SDK reuses a pooled HTTPS connection whose TLS
        stream was already disposed ("Cannot access a disposed object:
        MobileAuthenticatedStream") under the room-load burst; closing the
        connection after each response forces a fresh one."""
        response = await call_next(request)
        try:
            response.headers["Connection"] = "close"
            response.headers["Keep-Alive"] = "timeout=0, max=1"
        except Exception:
            pass
        return response


HTTP_LOG_FILE = Path("logs/http_traffic.log")


def _safe_decode(payload: bytes) -> str:
    """Decode bytes for logging without throwing on binary data."""
    if not payload:
        return ""
    return payload.decode("utf-8", errors="replace")


def _append_http_log(entry: str) -> None:
    HTTP_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with HTTP_LOG_FILE.open("a", encoding="utf-8") as log_file:
        log_file.write(entry)


# Full request+response body logging is OFF unless HTTP_TRAFFIC_LOG=1. Left on, it
# buffered every body in RAM (a 71 MB video upload -> ~800 MB, the APK download ->
# OOM) and wrote 19 GB to logs/http_traffic.log in ~1.5 days of self-hosting.
_TRAFFIC_LOG = os.getenv("HTTP_TRAFFIC_LOG", "") == "1"
_DIAG_DYNAMO = os.getenv("DIAG_DYNAMO", "") == "1"


class _QuietPolls(logging.Filter):
    """Drop uvicorn access lines for successful polls every player makes every
    few seconds (/client/video.txt alone was ~14k lines/hour)."""
    _PATHS = ("/client/video.txt", "/client/feed.txt", "/client/video/")

    def filter(self, record):
        a = record.args
        try:
            return not (isinstance(a, tuple) and len(a) >= 5 and int(a[4]) < 400
                        and str(a[2]).startswith(self._PATHS))
        except Exception:
            return True


logging.getLogger("uvicorn.access").addFilter(_QuietPolls())
_TRAFFIC_SKIP = ("/editor", "/client/", "/download", "/discord")


async def log_http_traffic(request: Request, call_next):
    if not _TRAFFIC_LOG or request.scope.get("path", "").startswith(_TRAFFIC_SKIP):
        return await call_next(request)
    try:
        if int(request.headers.get("content-length") or 0) > 256 * 1024:
            return await call_next(request)
    except ValueError:
        pass
    request_body = await request.body()
    request_body_text = _safe_decode(request_body)

    response = await call_next(request)

    response_chunks = [chunk async for chunk in response.body_iterator]
    response_body = b"".join(response_chunks)
    response_body_text = _safe_decode(response_body)

    # Rebuild response because we consumed body_iterator while logging.
    response.body_iterator = iterate_in_threadpool(iter([response_body]))

    now = datetime.now(timezone.utc).isoformat()
    full_path = str(request.url.path)
    if request.url.query:
        full_path = f"{full_path}?{request.url.query}"

    log_entry = (
        f"[{now}]\n"
        f"Client: {request.client.host if request.client else 'unknown'}\n"
        f"Method: {request.method}\n"
        f"Path: {full_path}\n"
        f"RequestBody: {request_body_text}\n"
        f"StatusCode: {response.status_code}\n"
        f"ResponseBody: {response_body_text}\n"
        f"{'=' * 80}\n"
    )
    _append_http_log(log_entry)

    return response


if _TRAFFIC_LOG:
    app.middleware("http")(log_http_traffic)


# ─── Startup ─────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup_event():
    """Optionally seed the database on startup."""
    import config as _cfg
    _seed = os.getenv("SEED_DB", "").strip().lower() in {"1", "true", "yes", "y", "on"} or bool(getattr(_cfg, "SEED_DB", 0))
    if _seed:
        await seed_database()
    else:
        print("[seed] Skipped (set SEED_DB=1 to enable)")
    print("=" * 60)
    print("  Yeeps v1 Private Server is READY")
    print("=" * 60)

    # Warm the community-world lists once in the background so the CW browser is
    # populated from cache and clients never trigger a live (rate-limited) fetch.
    async def _warm_world_lists():
        try:
            import asyncio as _a
            from handlers.misc import _world_list
            names = []
            for kind, cat in (("popular", ""), ("featured", "parkour"), ("random", "")):
                try:
                    out = await _world_list(kind, cat)
                    for w in (out or {}).get("worlds") or []:
                        if isinstance(w, str):
                            names.append(w)
                except Exception as e:
                    print(f"[worlds] warm {kind} failed: {e}")
                await _a.sleep(3)   # space out so real Yeeps doesn't rate-limit
            # NOTE: the old cw room-map/content PREWARM is intentionally DISABLED.
            # CW is now a pure on-demand passthrough (routers/lambda_router.py ->
            # dynamo_ops._cw_passthrough) with a verbatim DB cache (cw_passthrough).
            # The prewarm populated the OLD stripped caches (cwMirror/cw_content)
            # which (a) are no longer the serving path and (b) competed with the
            # passthrough for the bounded CW executor + re-threw the throttle. We
            # only warm the world LISTS above (needed for the CW browser).
        except Exception as e:
            print(f"[worlds] warm task failed: {e}")
    import asyncio as _asyncio
    _asyncio.create_task(_warm_world_lists())

    # Periodic world-LIST refresh (keeps the CW browser populated) + a gentle
    # PREWARM of the popular worlds' sandbox BLOCKS into the in-mem passthrough
    # cache, so the FIRST player entering a popular world doesn't cold-miss into a
    # null map (the 2.31.4 client crashes on null). In-mem only (never DB), spaced
    # out (one world / few sec) so the prewarm itself doesn't add throttle; skips
    # worlds already freshly cached.
    async def _rewarm_world_lists_loop():
        import asyncio as _a2
        first = True
        while True:
            await _asyncio.sleep(90 if first else 600)
            first = False
            names = []
            try:
                from handlers.misc import _world_list
                for kind, cat in (("popular", ""), ("featured", "parkour"), ("random", "")):
                    out = await _world_list(kind, cat)
                    names += [w for w in (out or {}).get("worlds") or [] if isinstance(w, str)]
            except Exception as e:
                print(f"[worlds] list-refresh loop error: {e}")
            try:
                from services.dynamo_ops import _cw_prewarm_passthrough
                warmed = 0
                for n in list(dict.fromkeys(names))[:20]:
                    if warmed >= 12:
                        break
                    ok = await _cw_prewarm_passthrough(f"c_{n}_sandbox")
                    if ok:
                        warmed += 1
                    await _a2.sleep(4)   # space out -> no self-throttle
                if warmed:
                    print(f"[cw] prewarmed {warmed} popular sandbox worlds into mem cache")
            except Exception as e:
                print(f"[worlds] cw prewarm loop error: {e}")
    _asyncio.create_task(_rewarm_world_lists_loop())

    # One-time startup probe: log into each CW service account once so banned
    # ones (real Yeeps has banned 14/16 at one point) are known UP FRONT and
    # every CW read uses only live accounts -- instead of randomly hitting dead
    # ones for a minute after each restart. Also pre-warms the live accounts'
    # ~1h sessions. Runs before the prewarm loop's first pass (90s).
    async def _cw_ban_probe():
        try:
            from services import yeeps_direct as _yd
            from services.dynamo_ops import _CW_DIRECT_POOL
            await _yd.probe_bans(list(_CW_DIRECT_POOL))
        except Exception as e:
            print(f"[yeeps] ban probe error: {e}")
    _asyncio.create_task(_cw_ban_probe())

    # Background loop: rotate the editor password (announce the new code to its
    # webhook) when its time window rolls over. Runs while the service is up;
    # a lazy check on the editor login page covers wake-from-idle.
    import asyncio

    async def _editor_pw_rotation_loop():
        from routers.editor_router import _maybe_notify_new_password
        while True:
            try:
                await _maybe_notify_new_password()
            except Exception as e:
                print(f"[editor] pw rotation loop error: {e}")
            await asyncio.sleep(120)

    asyncio.create_task(_editor_pw_rotation_loop())

    # Background loop: trim unbounded log collections so they never fill the
    # MongoDB storage quota and block ALL writes (devlogs grew to 421MB/86k docs
    # over 4 days and hit the 512MB cap -> writes blocked -> cw_content couldn't
    # persist, logins/stats failing). Keep only recent diagnostic logs.
    async def _log_trim_loop():
        from bson import ObjectId
        from datetime import datetime, timezone, timedelta
        from services.db import db as _db
        # collection -> hours of history to keep
        _keep = {"devlogs": 6, "debug_logs": 12, "client_error_events": 48}
        while True:
            try:
                for coll, hours in _keep.items():
                    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
                    r = await _db[coll].delete_many({"_id": {"$lt": ObjectId.from_datetime(cutoff)}})
                    if r.deleted_count:
                        print(f"[logtrim] {coll}: deleted {r.deleted_count} older than {hours}h")
            except Exception as e:
                print(f"[logtrim] loop error: {e}")
            await asyncio.sleep(1800)   # every 30 min
    asyncio.create_task(_log_trim_loop())
    from services.tv import tv_watch_loop
    asyncio.create_task(tv_watch_loop())


# ─── Include routers ─────────────────────────────────────────────────

# Lambda routes (specific path pattern)
app.include_router(lambda_router)

# Player data editor UI — must be registered before the catch-all
app.include_router(editor_router, prefix="/editor")
from routers.editor_router import ws_router as editor_ws_router
app.include_router(editor_ws_router, prefix="/editor")
from routers.editor_router import menu_router as editor_menu_router
app.include_router(editor_menu_router, prefix="/editor")    # owner hand menu -> Photon bot       # live talk WebSocket (Photon/Talk tab)

# 2.4.0 deployment: the editor is also reachable at /editor/240 (its page and
# API calls use the /editor paths, which this rewrite maps onto).
from config import IS_V2 as _EDITOR_V2
if _EDITOR_V2:
    @app.middleware("http")
    async def _editor_240_alias(request: Request, call_next):
        p = request.scope.get("path") or ""
        if p == "/editor/240" or p.startswith("/editor/240/"):
            request.scope["path"] = "/editor" + p[len("/editor/240"):]
        elif p in ("/download", "/dl"):
            # the tunnel only routes /download/... to the APK server
            from fastapi.responses import RedirectResponse
            return RedirectResponse("/download/", status_code=302,
                                    headers={"Cache-Control": "no-store"})
        return await call_next(request)


# Discord slash-command bot (/link) via HTTP interactions
from routers.discord_router import router as discord_router
app.include_router(discord_router)

# Public feed the 1.31 APK polls: announcements + remote disk audio
from routers.client_router import router as client_router
app.include_router(client_router)


# ─── Debug: dump recent redirected traffic ──────────────────────────
@app.get("/debug/traffic")
async def debug_traffic(request: Request, n: int = 60, q: str = ""):
    """Return the last N request/response blocks from http_traffic.log.
    Optional substring filter q (case-insensitive) keeps only blocks that
    mention it (e.g. q=G2_Rooms or q=Master)."""
    # Was public and read the whole (multi-GB) file: editor login required now,
    # and only the last 4 MB is read.
    from routers.editor_router import _authed_name
    if not _TRAFFIC_LOG or not _authed_name(request):
        return JSONResponse(content={"detail": "Not Found"}, status_code=404)
    try:
        with HTTP_LOG_FILE.open("rb") as f:
            f.seek(0, 2); f.seek(max(0, f.tell() - 4 * 1024 * 1024))
            text = f.read().decode("utf-8", errors="replace")
    except Exception as e:
        return JSONResponse(content={"error": str(e), "blocks": []})
    blocks = [b for b in text.split("=" * 80) if b.strip()]
    if q:
        ql = q.lower()
        blocks = [b for b in blocks if ql in b.lower()]
    return JSONResponse(content={"count": len(blocks), "blocks": blocks[-n:]})


# ─── Catch-all route ─────────────────────────────────────────────────

async def _log_photon_auth(account_id: str, allowed: bool, request, extra: dict | None = None) -> None:
    """Record every Photon custom-auth call (both the game and any other Photon
    app pointed at this server), so we can compare Photon logins with who is
    actually online. Collection photon_auths, TTL 2 days."""
    try:
        from services.db import db as _db
        from services.ip_bans import client_ip as _cip
        col = _db["photon_auths"]
        await col.create_index("at", expireAfterSeconds=2 * 86400)
        # fileHashes is ~8.5 KB and identical per APK build: keep a 16-hex fingerprint
        # (photon_auths was 118 MB of the 512 MB Atlas quota storing it in full).
        import hashlib as _hl
        q = {}
        for k, v in request.query_params.items():
            if k.lower() in ("token", "summaryhash", "botkey"):
                continue
            if k.lower() == "filehashes":
                k, v = "fileHashesFp", _hl.sha256(v.encode()).hexdigest()[:16]
            q[k] = v[:200]
        await col.insert_one({"at": datetime.now(timezone.utc), "accountID": account_id,
                              "allowed": bool(allowed), "ip": _cip(request),
                              "query": q, **(extra or {})})
    except Exception as _e:
        print(f"[photon] auth log failed: {_e}")


async def _bot_photon_auth(q, request):
    """Photon custom auth for the owner's local bot (yeeps-bot). Wrong/disabled
    key -> the same covert deny a modified client gets."""
    import hmac as _hm
    from config import BOT_KEY, COVERT_DENY_MESSAGE
    if not BOT_KEY or not _hm.compare_digest(str(q.get("botKey")), BOT_KEY):
        print("[photon] bot auth DENIED (bad key)")
        return JSONResponse(content={"ResultCode": 2, "Message": COVERT_DENY_MESSAGE})
    try:
        n = min(max(int(q.get("botId") or 1), 1), 9)
    except ValueError:
        n = 1
    aid = f"o_99000000000000{n:02d}"
    name = re.sub(r"[^A-Za-z0-9_ .-]", "", str(q.get("botName") or "BlazeBot"))[:24] or "BlazeBot"
    if not await accounts.find_one({"accountID": aid}, {"_id": 1}):
        from handlers.login import DEFAULT_ACCOUNT, _server_time
        acc = dict(DEFAULT_ACCOUNT)
        ov = await globals_col.find_one({"key": "default_account_override"}, {"_id": 0, "value": 1})
        if ov and isinstance(ov.get("value"), dict):
            acc.update(ov["value"])
        acc.update({"accountID": aid, "displayName": name, "isBot": 1, "firstLogin": _server_time()})
        await accounts.insert_one(acc)
        print(f"[photon] created bot account {aid} ({name})")
    elif q.get("botName"):
        await accounts.update_one({"accountID": aid}, {"$set": {"displayName": name}})
    await _log_photon_auth(aid, True, request, {"via": "bot"})
    return JSONResponse(content={"ResultCode": 1, "UserId": f"{aid}$VR${name}${_photon_role_mask(aid)}"})


# Photon UserId = "accountID$VR$name$roleMask". The client strips five gated
# roles (developer, moderator, ...) from every REMOTE player unless their bit is
# set in field 3 (PlayerDataManager.ExtractCanHaveRole -> RoleManager.TryGetRoleBit,
# bits 1/2/4/8/16). Without it other players never see a developer (trass) badge
# or its name icon. Login grants every player all non-blaze roles, so all 5 bits.
PHOTON_ROLE_MASK = 31
# 2026-11-01: a remote client also trusts this mask for the game's own moderator
# panel (kick/warn/ban RPCs are obeyed when the SENDER has the bit). With 31 for
# everyone, any player could kick anyone. Only MOD_STAFF get the gated bits now.


def _photon_role_mask(account_id: str) -> int:
    from handlers.moderation import MOD_STAFF
    return PHOTON_ROLE_MASK if account_id in MOD_STAFF else 0
_SHARD_SEEN: dict = {}   # accountID -> last-written photon shard (in-memory; write-on-change)


async def _photon_auth_denied(account_id: str, request) -> str | None:
    """Return a deny MESSAGE if this connection must be rejected at Photon
    custom-auth, else None. Rejects banned account / IP / name, AND outdated or
    modified builds (e.g. clone_maker's dev-menu clone).

    Enforcing the build check HERE matters: a patched client can ignore the
    client-side login isolation (GetHasPermissionToJoinRoom), but it cannot join
    a Photon room if Photon's own auth webhook rejects it. The client reports its
    attestation `summaryHash` in the auth query (applicationVersion is a useless
    hardcoded "1.19.0" in every build); we deny on a POSITIVE mismatch against the
    current build's hash, and never when no hash has been seen (no false
    positives for not-yet-attested clients)."""
    from config import (ALLOWED_ATTEST_HASHES, RETIRED_ATTEST_HASHES,
                        REQUIRED_CLIENT_VERSION, COVERT_DENY_MESSAGE,
                        SHADOW_BAN_IDS, SHADOW_BAN_IPS,
                        ENFORCE_CLIENT_TOKEN, valid_client_tokens)
    try:
        from services.ip_bans import is_banned, client_ip
        ip = client_ip(request)
        # Covert targets first: a generic "can't connect" message, so they can't
        # tell they were banned or how. Env lists let you covert-ban without a DB
        # trace at all.
        if (account_id and account_id in SHADOW_BAN_IDS) or (ip and ip in SHADOW_BAN_IPS):
            print(f"[photon] shadow(list) DENIED {account_id} ip={ip}")
            return COVERT_DENY_MESSAGE
        if await is_banned(ip):
            return "You are banned."
        adoc = None
        if account_id and account_id != "o_0":
            adoc = await accounts.find_one(
                {"accountID": account_id},
                {"_id": 0, "adminBlocked": 1, "isPermabanned": 1, "shadowBanned": 1,
                 "displayName": 1, "oculusID": 1, "attestHash": 1, "clientBuildStatus": 1,
                 "clientBuildTag": 1, "discordID": 1, "roleKeys": 1})
            # Hidden per-account shadowban: no "banned" screen, just a dead connection.
            if adoc and adoc.get("shadowBanned"):
                print(f"[photon] shadow(flag) DENIED {account_id}")
                return COVERT_DENY_MESSAGE
            if adoc and (adoc.get("adminBlocked") or adoc.get("isPermabanned")):
                return "You are banned."
            # Headset key: 131b15+ adds dk=<key> to its Photon auth params, so a
            # room join must come from a headset bound to this account.
            from services import device_auth as _da
            _dk = str(request.query_params.get("dk") or getattr(request.state, "device_key", "") or "")
            _dok, _ = await _da.check_request(account_id, _dk, "photon join", ip,
                                              enforce=_da.photon_enforcing())
            if not _dok:
                return ("This account is locked to another headset. "
                        "Ask an admin to reset your headset lock.")
            if _da.valid_key(_dk):
                _da._log(f"photon join {account_id} with headset key ok", every=600)
            if adoc:
                try:
                    from handlers.login import _is_banned_name
                    if _is_banned_name(adoc.get("displayName"), adoc.get("oculusID")):
                        return "You are banned."
                except Exception:
                    pass
        # Build gate: reject outdated/modified builds at the one layer a patched
        # client can't bypass. Denied COVERTLY — a modified client just sees a
        # generic connection failure and can't tell its build was detected (so it
        # can't learn to spoof the hash). Honest outdated players already get a
        # clear "update" message on the LOGIN path, so this doesn't strand them.
        # Use ONLY the live hash from THIS auth request (the GET join/heartbeat
        # auth carries it). No stored-attestHash fallback: a stale stored hash
        # from a build the account previously ran would false-positive. Deny only
        # a hash positively present and not on the allowlist.
        # APK build/integrity gate, from the tag this session's login recorded
        # (services/build_gate.py). Modified native libs -> covert; old/missing
        # build -> clear update message.
        try:
            from services.build_gate import enforced as _benforced
            from services.build_gate import current_status as _cur_status
            _bst = _cur_status(adoc or {})
            if _benforced() and _bst == "modified":
                print(f"[photon] build DENIED(covert, modified libs) {account_id}")
                return COVERT_DENY_MESSAGE
            if _benforced() and _bst == "outdated":
                return outdated_message()
            # "require newest build" testing toggle: deny Photon to any client
            # not on the required build so they can't hold a CCU slot mid-test.
            from services.build_gate import require_build as _req_build, require_blocks as _req_blocks
            _reqb = await _req_build()
            if _benforced() and _req_blocks(str((adoc or {}).get("clientBuildTag") or ""), _reqb):
                return outdated_message()
            # Live-track livelock at Photon: login (handlers/login.py) isolates an
            # old live/biglive build, but a client already in a room must ALSO be
            # refused here so it's actually kicked and can't rejoin on the old
            # APK. Track-specific (newest_livebase_build from LATEST_BIG); 1.31,
            # 2.4 and play* clients are unaffected.
            _lt = str((adoc or {}).get("clientBuildTag") or "")
            if _benforced() and _lt.startswith(("biglive", "live")):
                from services import big_patterns as _bp
                _nl = _bp.newest_livebase_build()
                if _nl and _bp._biglive_ver(_lt) < _nl:
                    print(f"[photon] livelock DENY {account_id}: {_lt.split('.', 1)[0]} < biglive{_nl}")
                    from services.build_gate import LIVE_OUTDATED_MESSAGE
                    return LIVE_OUTDATED_MESSAGE
            # Owner 2026-10-28: live accounts with no Discord link can't play at all
            # (login already fails with the /link box; this stops a patched client).
            if (_lt.startswith(("biglive", "live")) and adoc and not adoc.get("discordID")
                    and not str(account_id).startswith("o_99000000")
                    and "blaze" not in (adoc.get("roleKeys") or [])):
                from services.db import db as _db240
                _linked = bool(await _db240["discord_links"].find_one({"accountID": account_id}, {"_id": 1}))
                if not _linked and os.getenv("CARRYOVER_DB", ""):
                    from services.db import client as _mc
                    _linked = bool(await _mc[os.getenv("CARRYOVER_DB")]["accounts"].find_one(
                        {"accountID": account_id, "discordID": {"$nin": [None, ""]}}, {"_id": 1}))
                if not _linked:
                    print(f"[photon] no-link DENY {account_id}")
                    from services.build_gate import LINK_REQUIRED_MESSAGE
                    return LINK_REQUIRED_MESSAGE
        except Exception as _e:
            print(f"[photon] build gate error: {_e}")
        live = str(request.query_params.get("summaryHash") or "").strip().upper()
        from config import ENFORCE_ATTEST_HASH
        if live and live not in ALLOWED_ATTEST_HASHES and not ENFORCE_ATTEST_HASH:
            print(f"[photon] attest OBSERVE {account_id}: {live}")
        elif live and live not in ALLOWED_ATTEST_HASHES:
            if live in RETIRED_ATTEST_HASHES:
                print(f"[photon] build DENIED(retired 1.19) {account_id}")
                return outdated_message()
            print(f"[photon] build DENIED(covert) {account_id}: attest {live} not allowed")
            return COVERT_DENY_MESSAGE
        # Rotating client token: the real build sends `ct` (a 6-digit code that
        # changes hourly from a shared secret). While ENFORCE is off we only
        # OBSERVE (log) so you can confirm the token-client is live for everyone
        # before switching enforcement on; when on, a missing/stale token is a
        # covert deny. Deliberately last, so banned/outdated get their own result.
        tok = str(request.query_params.get("ct") or "").strip()
        if ENFORCE_CLIENT_TOKEN:
            if tok not in valid_client_tokens():
                print(f"[photon] token DENIED(covert) {account_id}: ct='{tok}'")
                return COVERT_DENY_MESSAGE
        elif tok:
            ok = tok in valid_client_tokens()
            print(f"[photon] token OBSERVE {account_id}: ct='{tok}' valid={ok}")
    except Exception as e:
        print(f"[photon] auth check failed for {account_id}: {e}")
    return None


@app.api_route("/", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def catch_all(request: Request, path: str = ""):
    """
    Catch-all route that detects which AWS service the game client
    is trying to reach and dispatches accordingly.
    """
    host = request.headers.get("host", "")
    target = request.headers.get("x-amz-target", "")
    auth = request.headers.get("authorization", "")
    method = request.method

    print(f"\n{'='*60}")
    print(f"{method} /{path}")
    print(f"Host: {host}")

    # ── 1. Photon Auth: GET / with query string ──
    if method == "GET" and path == "" and str(request.query_params):
        q = request.query_params
        if q.get("botKey") is not None:
            return await _bot_photon_auth(q, request)
        account_id = q.get("UserId") or q.get("userId") or q.get("accountID") or "o_0"
        # The client's integrity/attestation heartbeat arrives here too, carrying
        # summaryHash — a per-BUILD signature (the current build's libBNMExplorer.so
        # differs from the old one, so the hash differs). Unlike applicationVersion
        # (hardcoded "1.19.0" in every build), this reliably distinguishes the
        # current build from old ones. Record it so /online can flag stale builds.
        _summary = q.get("summaryHash")
        if _summary and account_id and account_id != "o_0":
            try:
                await accounts.update_one({"accountID": account_id},
                                          {"$set": {"attestHash": _summary}})
            except Exception as _e:
                print(f"[attest] record failed for {account_id}: {_e}")
        # Photon shard: server2's custom-auth URL carries sv=2 (server1 has none).
        # Written only when it CHANGES (in-memory guard) so /online can split the
        # two shards without a Mongo write on every auth heartbeat.
        if account_id and account_id != "o_0":
            _shard = "2" if str(q.get("sv") or "") in ("2", "server2") else "1"
            if _SHARD_SEEN.get(account_id) != _shard:
                _SHARD_SEEN[account_id] = _shard
                try:
                    await accounts.update_one({"accountID": account_id},
                                              {"$set": {"photonShard": _shard}})
                    print(f"[shard] {account_id} -> server{_shard}")
                except Exception as _e:
                    print(f"[shard] write failed for {account_id}: {_e}")
        username = q.get("UserName") or q.get("userName") or q.get("username")
        if not username and account_id:
            account_doc = await accounts.find_one({"accountID": account_id}, {"_id": 0, "displayName": 1})
            if account_doc and isinstance(account_doc.get("displayName"), str) and account_doc["displayName"].strip():
                username = account_doc["displayName"].strip()
        if not username:
            username = account_id
        _deny = await _photon_auth_denied(account_id, request)
        await _log_photon_auth(account_id, not _deny, request, {"via": "GET"})
        if _deny:
            print(f"Service: Photon Auth DENIED ({_deny}) -> {account_id}")
            print(f"{'='*60}\n")
            return JSONResponse(content={"ResultCode": 2, "Message": _deny})
        payload = {"ResultCode": 1, "UserId": f"{account_id}$VR${username}${_photon_role_mask(account_id)}"}
        print(f"Service: Photon Auth -> {payload}")
        print(f"{'='*60}\n")
        return JSONResponse(content=payload)

    # Parse body for POST/PUT requests
    raw = await request.body()
    raw_text = _safe_decode(raw)

    # ── 1b. Photon Auth: POST / (Photon Cloud's custom-auth webhook shape) ──
    if method == "POST" and path == "" and not target:
        try:
            body_peek = json.loads(raw) if raw else {}
        except Exception:
            body_peek = {}
        if isinstance(body_peek, dict):
            account_id = (
                request.query_params.get("UserId")
                or body_peek.get("UserId")
                or body_peek.get("userId")
                or body_peek.get("accountID")
            )
            if account_id:
                username = body_peek.get("UserName") or body_peek.get("userName") or body_peek.get("username")
                if not username:
                    account_doc = await accounts.find_one({"accountID": account_id}, {"_id": 0, "displayName": 1})
                    if account_doc and isinstance(account_doc.get("displayName"), str) and account_doc["displayName"].strip():
                        username = account_doc["displayName"].strip()
                if not username:
                    username = account_id
                _deny = await _photon_auth_denied(account_id, request)
                await _log_photon_auth(account_id, not _deny, request, {"via": "POST"})
                if _deny:
                    print(f"Service: Photon Auth (POST) DENIED ({_deny}) -> {account_id}")
                    print(f"{'='*60}\n")
                    return JSONResponse(content={"ResultCode": 2, "Message": _deny})
                payload = {"ResultCode": 1, "UserId": f"{account_id}$VR${username}${_photon_role_mask(account_id)}"}
                print(f"Service: Photon Auth (POST) -> {payload}")
                print(f"{'='*60}\n")
                return JSONResponse(content=payload)

    # Replay exact responses captured in server.log when possible.
    replay = lookup_replay(method, f"/{path}" if path else "/", raw_text)
    if replay is not None:
        print("Service: Replay | source: server.log")
        print(f"-> {replay['status_code']} {replay['content_type']}")
        print(f"{'='*60}\n")
        return Response(
            content=replay["response_body"],
            status_code=replay["status_code"],
            media_type=replay["content_type"],
        )

    try:
        body = json.loads(raw) if raw else {}
    except Exception:
        body = {}

    # ── 2. Lambda: path matches invocation pattern ──
    m = re.match(r"^2015-03-31/functions/([^/]+)/invocations/?$", path)
    if m or "/lambda/" in auth or host.startswith("lambda"):
        fn = m.group(1) if m else (path or "unknown")
        print(f"Service: Lambda | function: {fn}")
        # Forward to the lambda router handler directly
        from routers.lambda_router import lambda_invoke
        return await lambda_invoke(fn, request)

    # ── 3. DynamoDB: X-Amz-Target header present ──
    if (target and target.startswith("DynamoDB_")) or "/dynamodb/" in auth or host.startswith("dynamodb"):
        op = target.rsplit(".", 1)[-1] if "." in target else "?"
        tbl = body.get("TableName", "")
        if not tbl and isinstance(body, dict):
            ri = body.get("RequestItems", {})
            if isinstance(ri, dict) and ri:
                tbl = ",".join(ri.keys())
        print(f"Service: DynamoDB | op: {op} | table: {tbl}")

        # TEMP DIAGNOSTIC: dump request/response for the tables involved in
        # the main-instance / build-block decision, so the exact fields the
        # client reads and writes are visible in the logs.
        # Opt-in (DIAG_DYNAMO=1): ~5000 heartbeat dumps/hour otherwise fill the journal.
        _diag = _DIAG_DYNAMO and ("G2_Rooms" in tbl or "G2_PlayerStatuses" in tbl or "G2_RoomMap" in tbl or "G2_Maps" in tbl)
        if _diag:
            print(f"[DIAG] REQ {op} {tbl}: {json.dumps(body)[:1200]}")

        from services.dynamo_ops import dispatch_dynamo
        from services.ip_bans import client_ip as _dyn_ip
        from services import big_patterns as _bigp
        _dyn_tag = await _bigp.tag_for(getattr(request.state, "client_build_tag", ""),
                                       getattr(request.state, "device_key", ""))
        if isinstance(body, dict):
            body["_clientBuildTag"] = _dyn_tag
        payload_text, headers = await dispatch_dynamo(target, body, _dyn_ip(request),
                                                      getattr(request.state, "device_key", ""))
        payload_text = _bigp.downgrade_for_tag(payload_text, _dyn_tag)
        if _diag:
            print(f"[DIAG] RES {op} {tbl}: {payload_text[:1200]}")
        print(f"-> 200 application/x-amz-json-1.0")
        print(f"{'='*60}\n")
        return Response(
            content=payload_text,
            media_type="application/x-amz-json-1.0",
            headers=headers,
        )

    # ── 4. Direct API: path matches known handler ──
    # Direct HTTPS routes like /questLogIntoAccount go through API Gateway's
    # Lambda proxy integration, which UNWRAPS the Lambda's
    # {"statusCode","headers","body"} envelope - the client receives only
    # the inner body ({"data": {...}}) as the HTTP response. The envelope is
    # only seen by AWS SDK Lambda.Invoke callers (step 2 above).
    from routers.lambda_router import HANDLER_TABLE
    fn_lower = path.lower()
    try:
        from services.ip_bans import client_ip as _cip
        if isinstance(body, dict):
            body["_clientIP"] = _cip(request)
            from services import big_patterns as _bigp0
            body["_clientBuildTag"] = await _bigp0.tag_for(getattr(request.state, "client_build_tag", ""),
                                                           getattr(request.state, "device_key", ""))
            body["_deviceKey"] = getattr(request.state, "device_key", "")
            body["_fn"] = path.rstrip("/").rsplit("/", 1)[-1]
    except Exception:
        pass
    for match_str, handler in HANDLER_TABLE:
        if match_str in fn_lower:
            print(f"Service: Direct API | route: {path} | handler: {handler.__name__}")
            if handler.__name__ != "handle_login" and isinstance(body, dict):
                from services import device_auth
                _ok, _ = await device_auth.check_request(body.get("accountID"), body.get("_deviceKey", ""),
                                                         path, body.get("_clientIP", ""))
                if not _ok:
                    return JSONResponse(content={"data": {"success": 0, "error": "NOT_AUTHORIZED"}})
            result = await handler(body)
            _err = (result.get("_lambda_error") or (result.get("data") or {}).get("_lambda_error")) \
                if isinstance(result, dict) else None
            if _err:
                return JSONResponse(status_code=400, content={
                    "statusCode": 400, "err_type": _err, "body": {"err_type": _err}})
            if isinstance(result, dict) and "data" not in result:
                result = {"data": result}
            from services import big_patterns as _bigp
            _dtag = body.get("_clientBuildTag") if isinstance(body, dict) else ""
            _txt = json.dumps(result)
            _low = _bigp.downgrade_for_tag(_txt, _dtag)
            if _low != _txt:
                result = json.loads(_low)
            print(f"-> 200 application/json")
            print(f"{'='*60}\n")
            return JSONResponse(content=result)

    # ── 5. Unknown — the 1.19.0 client treats an empty 200 as "nothing to do"
    # (an error status makes its SDK retry, e.g. the Talsec log posts).
    if _IS_V2:
        # 2.x: mimic real API Gateway for an unmatched route; its AWS SDK treats
        # this canonical shape as "this is really API Gateway, keep going".
        print(f"Service: (unknown) — returning API Gateway-style 403")
        print(f"{'='*60}\n")
        return JSONResponse(content={"message": "Missing Authentication Token"}, status_code=403)
    print(f"Service: (unknown) — returning empty 200")
    print(f"{'='*60}\n")
    return JSONResponse(content={})


# ─── Entry point ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    from config import SERVER_PORT
    uvicorn.run("main:app", host="0.0.0.0", port=SERVER_PORT, reload=True)
