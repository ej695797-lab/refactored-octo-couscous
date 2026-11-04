"""
DynamoDB operation handlers.
Translates DynamoDB API calls into MongoDB operations.
"""

import base64
import os as _os
import json
import time
import zlib
import uuid
from pathlib import Path

import httpx

# The client deserializes saveData with .NET's BinaryFormatter directly into
# a "MapSyncData" object with one field, "MapObjectSyncData[] mapObjects".
# This is a real BinaryFormatter stream for a MapSyncData with an empty
# mapObjects array, representing "0 blocks".
EMPTY_MAP_SAVE_DATA = (
    "AAEAAAD/////AQAAAAAAAAAMAgAAAEZBc3NlbWJseS1DU2hhcnAsIFZlcnNpb249MC4wLjAuMCwgQ3VsdHVyZT1uZXV0"
    "cmFsLCBQdWJsaWNLZXlUb2tlbj1udWxsBQEAAAALTWFwU3luY0RhdGEBAAAACm1hcE9iamVjdHMEE01hcE9iamVjdFN5"
    "bmNEYXRhW10CAAAAAgAAAAkDAAAABwMAAAAAAQAAAAAAAAAEEU1hcE9iamVjdFN5bmNEYXRhAgAAAAs="
)

from services.db import get_collection, get_key_field
from services.dynamo_types import unwrap_key, unwrap_attribute_updates, wrap_item, from_dynamo, to_dynamo
from services.property_defaults import default_for_property
from services.mapsync_serializer import serialize_blocks
from config import YEEPS_PROXY_URL, REQUIRED_CLIENT_VERSION, IS_V2


# ── Live community-world mirroring ──────────────────────────────────────────
# Community worlds (c_<name>) are pulled LIVE from real Yeeps on entry: the
# server logs in with a configured service account (globals "cw_fetch_account")
# and calls the yeeps-proxy relay's /rooms (grid) and /map (blocks) endpoints,
# converts the 2.x SPAM blocks to the 1.19 BinaryFormatter format, and serves
# them read-only. A short TTL cache only dedups the burst of identical GetItem
# calls the client fires during a single room load (so one entry = one real
# fetch, not dozens) -- it is not a persistent cache.
_CW_TTL = 30
_cw_cache: dict = {}


def _cw_cache_get(k):
    v = _cw_cache.get(k)
    if v and v[0] > time.time():
        return v[1]
    return None


def _cw_cache_put(k, val):
    _cw_cache[k] = (time.time() + _CW_TTL, val)


async def _cw_account():
    """(oculusID, mobileCode) of the service account used to fetch real cw data,
    read from globals 'cw_fetch_account' so it never lives in the repo."""
    try:
        from services.db import globals_col
        acc = await globals_col.find_one({"key": "cw_fetch_account"}, {"_id": 0, "value": 1})
        v = (acc or {}).get("value") or {}
        if v.get("oculusID") and v.get("mobileCode"):
            return v["oculusID"], str(v["mobileCode"])
    except Exception as e:
        print(f"[cw] account load failed: {e}")
    return None, None


async def _cw_grid_account():
    """(oculusID, mobileCode) of the SEPARATE service account used for cw room
    grids/maps (globals 'cw_grid_account'), so entering worlds doesn't share the
    rate-limit budget of the world-list account (cw_fetch_account). Falls back to
    the fetch account if no grid account is configured."""
    try:
        from services.db import globals_col
        acc = await globals_col.find_one({"key": "cw_grid_account"}, {"_id": 0, "value": 1})
        v = (acc or {}).get("value") or {}
        if v.get("oculusID") and v.get("mobileCode"):
            return v["oculusID"], str(v["mobileCode"])
    except Exception as e:
        print(f"[cw] grid account load failed: {e}")
    return await _cw_account()


async def _cw_service_accounts():
    """De-duplicated, SHUFFLED list of (oculusID, mobileCode, tag) service
    accounts for cw content/grid fetches. Pulls EVERY globals key matching
    cw_fetch_account* / cw_grid_account* (the base two plus any numbered extras
    cw_fetch_account2..N), so dropping a {oculusID, mobileCode} into a new
    cw_fetch_accountN global widens the budget with no code change (creds stay out
    of the repo, and _is_private_global keeps them unservable). Shuffled per call
    so load spreads randomly across all accounts instead of always hammering the
    first ones -> real Yeeps throttles far less."""
    import random as _r
    out, seen, pairs = [], set(), []
    try:
        from services.db import globals_col
        cursor = globals_col.find({"key": {"$regex": "^cw_(fetch|grid)_account"}},
                                  {"_id": 0, "key": 1, "value": 1})
        async for row in cursor:
            v = (row or {}).get("value") or {}
            if v.get("oculusID") and v.get("mobileCode"):
                pairs.append(((v["oculusID"], str(v["mobileCode"])), row.get("key", "cw")))
    except Exception as e:
        print(f"[cw] account list load failed: {e}")
    if not pairs:   # fallback to the base accounts if the query found nothing
        pairs = [(await _cw_account(), "fetch"), (await _cw_grid_account(), "grid")]
    for (oc, code), tag in pairs:
        if oc and (oc, code) not in seen:
            seen.add((oc, code))
            out.append((oc, code, tag))
    _r.shuffle(out)
    return out


async def _cw_post(path: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(f"{YEEPS_PROXY_URL}{path}", json=payload)
    return r.json()


# --- CW terrain (lambda path) -------------------------------------------------
# The relay /map returns BLOCKS only; a CW room's terrain layer (isTerrain=1)
# must be fetched through the g2_mobileInvokeFunction lambda with a real service
# session (same mechanism tools/mirror_live_world uses for the live world).
_cw_sess_cache: dict = {}   # oculusID -> (expires_ts, session dict)


_cw_aws_cd = {"until": 0.0, "fails": 0}   # global /AWS cooldown (storm breaker)


async def _cw_session(oc: str, code: str):
    """Relay /AWS login for a service account -> session dict, cached ~5 min.

    GLOBAL COOLDOWN: /AWS is the single choke point for every CW service call.
    When it starts failing (relay overwhelmed), retrying harder only deepens the
    storm (the client's retry-on-empty + many accounts = 60+/sec, which keeps the
    relay down, which keeps /AWS failing). So after a few consecutive failures we
    STOP calling /AWS for 20s, letting the relay recover; then one login succeeds
    and the per-account cache (5 min) keeps steady-state /AWS near zero."""
    import time as _t
    now = _t.time()
    hit = _cw_sess_cache.get(oc)
    if hit and hit[0] > now:
        return hit[1]
    if now < _cw_aws_cd["until"]:
        return None                       # in cooldown -> no /AWS at all
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{YEEPS_PROXY_URL}/AWS",
                             json={"secret": os.getenv("RELAY_SECRET_AWS", ""), "oculusID": oc, "mobileCode": str(code)})
        d = r.json()
    except Exception as e:
        d = None
        print(f"[cw] session /AWS failed for {oc}: {type(e).__name__}")
    if not isinstance(d, dict) or not d.get("ok") or not d.get("accessKeyId"):
        _cw_aws_cd["fails"] += 1
        if _cw_aws_cd["fails"] >= 5:
            _cw_aws_cd["until"] = now + 20
            _cw_aws_cd["fails"] = 0
            print("[cw] /AWS cooldown 20s (relay login failing -- letting it recover)")
        return None
    _cw_aws_cd["fails"] = 0
    _cw_aws_cd["until"] = 0
    _cw_sess_cache[oc] = (now + 300, d)
    return d


# DEDICATED bounded pool for CW boto3 calls. These MUST NOT run on asyncio's
# default ThreadPoolExecutor: boto3 lambda.invoke blocks a whole thread, and when
# real Yeeps throttles (+ the prewarm fires many at once) the default pool (~32,
# also used by Starlette for all sync work) fills with blocked threads and the
# ENTIRE server stops answering. A small dedicated pool caps CW concurrency so it
# can never starve the rest of the server.
from concurrent.futures import ThreadPoolExecutor as _TPE
_CW_EXECUTOR = _TPE(max_workers=4, thread_name_prefix="cw")
# Fail fast on a throttled/slow real-Yeeps call instead of pinning a thread ~60s.
try:
    from botocore.config import Config as _BotoConfig
    _CW_BOTO_CFG = _BotoConfig(connect_timeout=5, read_timeout=8,
                               retries={"max_attempts": 1})
except Exception:
    _CW_BOTO_CFG = None


def _cw_lambda_sync(sess: dict, fn: str, extra: dict):
    import boto3, json as _j
    lam = boto3.client("lambda", region_name="us-west-1",
                       aws_access_key_id=sess["accessKeyId"],
                       aws_secret_access_key=sess["secretAccessKey"],
                       aws_session_token=sess["sessionToken"],
                       config=_CW_BOTO_CFG)
    payload = {"accountID": sess["accountID"], "gameSessionID": sess["gameSessionID"],
               "mobilePassword": sess["mobilePassword"], "dataVersion": 2,
               "functionName": fn, "applicationVersion": "2.31.4"}
    payload.update(extra)
    r = lam.invoke(FunctionName="g2_mobileInvokeFunction", Payload=_j.dumps(payload).encode())
    outer = _j.loads(r["Payload"].read())
    body = outer.get("body")
    try:
        body = _j.loads(body)
    except Exception:
        pass
    return outer.get("statusCode"), body


async def _cw_lambda(sess: dict, fn: str, **extra):
    import asyncio as _aio
    loop = _aio.get_running_loop()
    # Dedicated pool + a hard wall-clock cap so a stuck call can never hang the
    # caller (room-load) or hold an executor slot indefinitely.
    try:
        return await _aio.wait_for(
            loop.run_in_executor(_CW_EXECUTOR, lambda: _cw_lambda_sync(sess, fn, extra)),
            timeout=12)
    except _aio.TimeoutError:
        print(f"[cw] lambda {fn} timed out (>12s) -- real Yeeps slow/throttled")
        return None, None


# ONE shared service session, reused for ALL CW calls. Each CW read used to loop
# accounts and /AWS-login per account -> under concurrent client retries that
# stormed the relay's /AWS endpoint (16+/sec) so logins failed, nothing cached,
# and it deadlocked. One service account can read ANY CW room, so we keep a
# single cached session (lock-guarded: only ONE /AWS at a time) and only re-login
# (rotating to the next account) when a call actually fails.
import asyncio as _aio_mod
_cw_shared = {"sess": None, "exp": 0.0, "idx": 0}
_cw_sess_lock = _aio_mod.Lock()


async def _cw_get_session(force: bool = False):
    import time as _t
    now = _t.time()
    if not force and _cw_shared["sess"] and _cw_shared["exp"] > now:
        return _cw_shared["sess"]
    async with _cw_sess_lock:
        now = _t.time()
        # Re-check inside the lock: a concurrent caller may have just refreshed,
        # so we reuse its session instead of making ANOTHER /AWS (this is what
        # serializes the burst into a single login the relay accepts).
        if not force and _cw_shared["sess"] and _cw_shared["exp"] > now:
            return _cw_shared["sess"]
        accts = await _cw_service_accounts()
        if not accts:
            return None
        # ONE account per call (no inner loop): a burst of /AWS trips the relay's
        # rate limit -> not-ok. On a forced refresh advance to the next account so
        # repeated failures rotate through them one login at a time.
        if force:
            _cw_shared["idx"] = (_cw_shared["idx"] + 1) % len(accts)
        oc, code, tag = accts[_cw_shared["idx"] % len(accts)]
        s = await _cw_session(oc, code)
        if s:
            _cw_shared.update(sess=s, exp=now + 240)
            return s
        return None


async def _cw_call(fn: str, **args):
    """One CW lambda call on the shared session, with a single re-login+retry on
    failure. Returns (statusCode, body). Replaces per-call account loops so we
    make ~one /AWS per 4 min instead of a storm."""
    sess = await _cw_get_session()
    if sess:
        st, b = await _cw_lambda(sess, fn, **args)
        if st == 200 and isinstance(b, dict):
            return st, b
    # failed -> force a fresh session (next account) and retry once
    sess = await _cw_get_session(force=True)
    if sess:
        return await _cw_lambda(sess, fn, **args)
    return None, None


# --- PURE transparent CW read passthrough -------------------------------------
# Blaze: "keep all the cw stuff that it normally calls just to call the real yeeps
# apis and dont touch that info." For a community-world READ, forward the client's
# exact function + args to real Yeeps (via a service-account session) and return
# real Yeeps' response VERBATIM -- real sid, saveData, saveInternalStates
# (signs/screens/teleporters), overrideSpawnPosition, cw_ attrs, terrain -- nothing
# reconstructed, dropped, or defaulted. This fixes wrong-spawn / blank-signs /
# empty-blocks-crash at once. READ-ONLY: writes are never in this set.
# Only these two are intercepted at the router for guest-direct/relay handling.
# fetchRoomMapData (grid) + extraProperties + cwGetCanEnter already serve from
# guest-direct real Yeeps via their own handlers (_cw_real_room_map etc.).
_CW_READ_FUNCS = {"fetchroomdata", "fetchmapdata", "fetchroommapdata"}
_cw_pt_cache: dict = {}   # request-signature -> (expires_ts, verbatim body)


def _cw_body_has_c(body: dict) -> bool:
    """True only when the request's SUBJECT is a community world (so we never
    forward a mixed/official request to real Yeeps and get wrong data back)."""
    for k in ("roomKey", "roomMapKey", "versionKey"):
        v = body.get(k)
        if isinstance(v, str):
            return v.startswith("c_")
    rks = body.get("roomKeys")
    if isinstance(rks, list) and rks:
        return all(isinstance(x, str) and x.startswith("c_") for x in rks)
    return False


def _cw_body_is_live_official(body: dict) -> bool:
    """True when the request SUBJECT is a PUBLIC/OFFICIAL room we can read live
    from real Yeeps with a service account -- anything that is NOT a private world
    (p_*/o_*) and NOT a community world (c_*). Livebase (biglive) clients serve
    these live so the incomplete local DB mirror no longer bricks rooms real Yeeps
    actually has (nexus full map, minigame_*, itemCustomizationStudio, ch2_*, ...
    -> "failed to load after 5 attempts"). Private worlds 403 on a service
    account, so they are kept on our DB where the player saved them; community
    worlds go through _cw_body_has_c. On a live miss the caller falls through to
    the local handler, so our own/custom rooms (real Yeeps lacks them) still
    serve from the DB."""
    def _ok(v):
        return isinstance(v, str) and bool(v) and not v.startswith(("p_", "o_", "c_"))
    for k in ("roomKey", "roomMapKey", "versionKey"):
        if k in body:
            return _ok(body.get(k))
    rks = body.get("roomKeys")
    if isinstance(rks, list) and rks:
        return all(_ok(x) for x in rks)
    return False


_CW_PT_TTL = 600            # in-MEMORY verbatim cache, 10 min (1.31 model)
_CW_PT_MAX_ACCOUNTS = 4     # per request, try at most N accounts then give up fast


import random as _cw_rng
# All 16 CW fetch service accounts (globals cw_fetch_account + cw_fetch_account2..16).
# cw_grid_account is excluded (it 403s on cross-account reads).
_CW_DIRECT_POOL = ["cw_fetch_account"] + [f"cw_fetch_account{i}" for i in range(2, 8)]   # 7 accounts (owner 2026-10-31, rotate these)


def _cw_live_pool() -> list:
    """The usable service accounts: the pool minus any real Yeeps has BANNED. We
    NEVER pick a banned account (a banned login wastes a call and worsens our
    standing). Falls back to the full pool only if somehow all are flagged."""
    try:
        from services.yeeps_direct import _BANNED
    except Exception:
        _BANNED = set()
    live = [a for a in _CW_DIRECT_POOL if a not in _BANNED]
    return live or list(_CW_DIRECT_POOL)


def _cw_direct_account(room_key: str) -> str:
    """Pick a RANDOM non-banned service account per CW read (Blaze: "make the cw
    shuffle between the accounts"). yeeps_direct keeps a ~1h session per account,
    so after warmup the live accounts are logged in and this adds no extra logins.
    CW_DIRECT_ACCOUNTS=1 pins to one account (debug)."""
    import os as _os
    if (_os.getenv("CW_DIRECT_ACCOUNTS", "") or "").strip() == "1":
        return "cw_fetch_account"
    return _cw_rng.choice(_cw_live_pool())


async def _cw_passthrough(function_name: str, body: dict):
    """Serve a CW read from REAL YEEPS DIRECT (services/yeeps_direct), VERBATIM.

    Blaze: "get the maps live from yeeps, nothing saved to my database" + "stop
    using my api" (the relay). yeeps_direct logs the service account into the real
    Yeeps mobile API (eb1v6k1091.execute-api... DIRECT, NOT the relay; the mobile
    login's nonce is random -- only the VR/Oculus login checks the Meta nonce) and
    signs the lambda with guest Cognito creds, reading BLOCKS (G2_Maps, which guest
    creds alone can't) and room data verbatim -- exactly the 1.31 model. IN-MEMORY
    cache only (~10 min). Read-only (yeeps_direct refuses non-fetch* calls)."""
    import hashlib as _h, json as _j, time as _t
    from services import yeeps_direct
    fn = function_name.lower()
    skip = {"accountID", "gameSessionID", "mobilePassword", "dataVersion",
            "functionName", "applicationVersion", "secret"}
    args = {k: v for k, v in body.items()
            if not str(k).startswith("_") and k not in skip}
    sig = _j.dumps([fn, args], sort_keys=True, default=str)
    ck = "cwpt:" + _h.sha1(sig.encode()).hexdigest()
    now = _t.time()
    hit = _cw_pt_cache.get(ck)
    if hit and hit[0] > now:
        return hit[1]
    rk = body.get("roomKey") or body.get("roomMapKey") or ""
    # Try a few DIFFERENT random accounts: one account's rate-limit/empty must not
    # hard-fail the read. Shuffle (load spread) + retry (resilience). yeeps_direct
    # raises on a throttled/failed call.
    pool = _cw_live_pool()
    _cw_rng.shuffle(pool)
    for acct in pool[:3]:
        try:
            data = await yeeps_direct.invoke(function_name, args, account=acct)
        except Exception as e:
            print(f"[cw] direct {function_name} via {acct} failed for {rk}: {type(e).__name__}: {str(e)[:60]}")
            continue
        if isinstance(data, dict) and data:
            out = {"data": data}
            _cw_pt_cache[ck] = (now + _CW_PT_TTL, out)
            return out
    # SERVE-LAST-GOOD on a throttled/failed live fetch: the 2.31.4 client HARD-
    # CRASHES on a null map (null -> VoiceChatManager.OnJoinedRoom NRE). 1.31 holds
    # the last live copy ~10 min and rides failures; match that -- return the last
    # cached {"data":...} (even past its TTL) instead of None so the client never
    # gets a null map. Still live every time it CAN fetch; in-mem only, never DB.
    if hit:
        print(f"[cw] {function_name} {rk}: live fetch failed -> serving last-good (no null map)")
        return hit[1]
    return None


async def _cw_passthrough_map(body: dict):
    """Live map fetch that resolves the REAL real-Yeeps session id server-side.

    The client's targetSessionID comes from OUR DB room map and does NOT match
    real Yeeps' current session (e.g. nexus client sid 1173 vs real 173 -> 404),
    so forwarding it verbatim misses. Resolve the real lastEditedSessionID
    (blocks) / lastEditedTerrainSessionID (terrain) from a cached fetchRoomData,
    then fetch the map with it -- exactly how _cw_fetch_room / the prewarm do it.
    Returns {"data": {...}} verbatim from real Yeeps, or None on a miss. In-mem
    cache only (via _cw_passthrough), nothing saved to the DB."""
    room_key = body.get("roomKey") or body.get("roomMapKey") or ""
    if not room_key:
        return None
    is_terrain = 1 if body.get("isTerrain") else 0
    rd = await _cw_passthrough("fetchRoomData", {
        "roomKey": room_key,
        "propertiesToGet": ["lastEditedSessionID", "lastEditedTerrainSessionID",
                            "lastValidSaveOffset"]})
    rdd = (rd or {}).get("data") if isinstance(rd, dict) else None
    rdd = rdd if isinstance(rdd, dict) else {}
    sid = rdd.get("lastEditedTerrainSessionID") if is_terrain else rdd.get("lastEditedSessionID")
    if sid is None:
        return None
    off = body.get("saveOffset") or 0
    return await _cw_passthrough("fetchMapData", {
        "roomKey": room_key, "targetSessionID": sid, "saveOffset": off,
        "isTerrain": is_terrain})


async def _cw_prewarm_passthrough(room_key: str) -> bool:
    """Warm the in-mem passthrough cache for a CW room's BLOCKS so the FIRST player
    entering never cold-misses into a null map (2.31.4 crashes on null). Fetches
    the real sid then warms fetchMapData(sid, isTerrain=0) under the exact cache
    key the client uses ({roomKey,targetSessionID,saveOffset,isTerrain}). In-mem
    only, never DB. Skips if already freshly cached."""
    import hashlib as _h, json as _j, time as _t
    try:
        rd = await _cw_passthrough("fetchRoomData",
                                   {"roomKey": room_key, "propertiesToGet": ["lastEditedSessionID"]})
        sid = ((rd or {}).get("data") or {}).get("lastEditedSessionID")
        if not sid:
            return False
        mapargs = {"roomKey": room_key, "targetSessionID": sid, "saveOffset": 0, "isTerrain": 0}
        ck = "cwpt:" + _h.sha1(_j.dumps(["fetchmapdata", mapargs], sort_keys=True, default=str).encode()).hexdigest()
        hit = _cw_pt_cache.get(ck)
        if hit and hit[0] > _t.time():
            return True   # already fresh
        out = await _cw_passthrough("fetchMapData", {"functionName": "fetchMapData", **mapargs})
        return bool(out and (out.get("data") or {}).get("saveData"))
    except Exception as e:
        print(f"[cw] prewarm {room_key} failed: {type(e).__name__}")
        return False


async def _cw_fetch_terrain(room_key: str):
    """Real CW room terrain blob -> {saveData, version} or None. Tries service
    accounts in rotation. Returns {"saveData":"", "version":1} when the room has
    no terrain layer (so the caller caches 'no terrain' rather than refetching)."""
    try:
        st, b = await _cw_call("fetchRoomData", roomKey=room_key,
                               propertiesToGet=["lastEditedTerrainSessionID"])
        tsid = ((b or {}).get("data") or {}).get("lastEditedTerrainSessionID")
        if not tsid:
            return {"saveData": "", "version": 1}
        st2, b2 = await _cw_call("fetchMapData", roomKey=room_key,
                                 targetSessionID=tsid, saveOffset=0, isTerrain=1)
        td = (b2 or {}).get("data") or {}
        sd = td.get("saveData") or ""
        if sd:
            return {"saveData": sd, "version": td.get("mapDataVersion") or 1}
    except Exception as e:
        print(f"[cw] terrain fetch failed for {room_key}: {type(e).__name__}")
    return None


async def _cw_map_with_account(room_key: str, oc: str, code: str):
    """One /map fetch+serialize with a given service account -> out dict or None.
    Returns the dict even when blockCount is 0 (caller decides on fallback)."""
    try:
        # secret/isModAccess makes the relay include each block's `owner`
        # (builder's oculusID) -> carried into the SPAM ownerUserID table so the
        # client can show who built each block (1.31 parity).
        data = await _cw_post("/map", {"oculusID": oc, "mobileCode": code,
                                       "roomKey": room_key,
                                       "secret": os.getenv("RELAY_SECRET_FULL", ""), "isModAccess": True})
    except Exception as e:
        print(f"[cw] /map fetch failed for {room_key}: {e}")
        return None
    if not data.get("ok"):
        print(f"[cw] /map not ok for {room_key}: {data.get('error')}")
        return None
    blocks = data.get("blocks") or []
    try:
        save = serialize_blocks(blocks)         # 1.19 BinaryFormatter (stock clients)
    except Exception as e:
        print(f"[cw] serialize failed for {room_key}: {e}")
        return None
    # 2.x SPAM (big / live 2.31.4 clients read this; the 1.19 blob loads empty).
    save_spam = ""
    try:
        from services.mapsync_serializer import serialize_blocks_spam
        save_spam = serialize_blocks_spam(blocks)
    except Exception as e:
        print(f"[cw] spam serialize failed for {room_key}: {e}")
    return {
        "dimensions": data.get("dimensions") or [80, 80, 80],
        "floorDepth": data.get("floorDepth") if data.get("floorDepth") is not None else 0,
        "saveData": save,
        "saveDataSpam": save_spam,
        "blockCount": data.get("blockCount", len(blocks)),
    }


_CW_ROOM_PROPS = [
    "roomKey", "roomName", "themeKey", "dimensions", "lobbyDirection", "door_up", "door_right",
    "door_down", "door_left", "passagewayLinks_creative", "isCreative", "isModeLocked",
    "isAutoCycling", "autoCycleTimestamp", "noSave", "noCopy", "noPaste", "isFreebuild",
    "isDevLocked", "blockMatchmakingSegmentation", "isBeginner", "isSolo", "goingAwayTimestamp",
    "emptyMessage", "isNoTerrain", "overrideExpireDurationMinutes", "overrideSpawnPosition",
    "mapDescription", "randomization_type", "randomization_mapCount", "lastEditedSessionID",
    "lastEditedTerrainSessionID", "roomDataVersion", "floorDepth", "liquidType", "liquidColor",
    "liquidLevel", "itemRestrictionsIsWhiteList", "itemRestrictionsGroupKeys",
    "teamItemRestrictions", "customLightingKey", "isOfficial", "creativeSessionID",
    "overrideMaxPlayers", "gravityType", "minVersion", "maxVersion", "isVisible",
    "lastValidSaveOffset",
    # Fields the 2.31.4 client also requests -- real Yeeps returns them when set,
    # and the client validates the doc against real's exact shape, so we MUST ask
    # for them too (else real omits them and our relay can't include them).
    "previousMapProperties", "previousEditedSessionID", "previousEditedTerrainSessionID",
    "lastRoomDataEditorSessionID", "isNoFloor", "overrideTokenUniqueID",
    "overrideSaveLockTimestamp", "terrainDataVersion",
]


async def _cw_fetch_room_doc(room_key: str):
    """Real-Yeeps room DATA for a CW room, straight from the live lambda
    (fetchRoomData), returned AS-IS -- true read-passthrough (Blaze: "cw ... should
    call the real live yeeps apis just like 1.31"). We do NOT build a local CW doc
    or inject placeholders: our old sid=1 / roomDataVersion=1 / noSave=1 /
    isModeLocked=1 made the 2.31.4 client think there was no modern block save, so
    it never fetched the blocks -> empty world. Real values (sid=88, floorDepth=14,
    real dims/isCreative) make it fetch. Read-only; writes are never forwarded.
    Cached 5 min; None on failure so the caller can fall back."""
    ck = f"roomdoc:{room_key}"
    hit = _cw_cache_get(ck)
    if hit:
        return hit
    # DIRECT guest read of real Yeeps G2_Rooms (no relay, no login) -- room data is
    # guest-readable. VERBATIM, IN-MEMORY cache only (Blaze: nothing saved to DB).
    try:
        from services import real_yeeps
        d = await real_yeeps.get_item_plain("G2_Rooms", {"roomKey": room_key})
        if isinstance(d, dict) and d.get("lastEditedSessionID"):
            _cw_cache_put(ck, d)
            return d
    except Exception as e:
        print(f"[cw] room-doc guest read failed for {room_key}: {type(e).__name__}")
    return None


async def _cw_fetch_room_direct(room_key: str):
    """Fetch a CW room's content via the DIRECT real-Yeeps lambda (fetchRoomData
    + fetchMapData), 1.31-style, so the block saveData keeps RAW o_ accountID
    owners -> the client resolves each to a real builder name via the account API.
    (The relay /map pre-resolves owners to USERNAMES, which the real account API
    can't look up, so names come back blank.) Serves the real SPAM v3 blob as-is
    (no re-serialize). Returns {dimensions, floorDepth, saveDataSpam, blockCount,
    saveTerrainSpam, terrainVersion} or None; tries service accounts in rotation."""
    from services.mapsync_decoder import decode_map
    try:
        st, b = await _cw_call("fetchRoomData", roomKey=room_key,
            propertiesToGet=["lastEditedSessionID", "lastEditedTerrainSessionID",
                             "floorDepth", "dimensions"])
        d = (b or {}).get("data") or {}
        sid = d.get("lastEditedSessionID")
        if not sid:
            return None
        st2, b2 = await _cw_call("fetchMapData", roomKey=room_key,
                                 targetSessionID=sid, saveOffset=0, isTerrain=0)
        md = (b2 or {}).get("data") or {}
        sd = md.get("saveData") or ""
        if not sd:
            return None
        try:
            bc = len(decode_map(sd))
        except Exception:
            bc = 1
        out = {
            "dimensions": d.get("dimensions") or [80, 80, 80],
            "floorDepth": d.get("floorDepth") if d.get("floorDepth") is not None else 0,
            "saveDataSpam": sd,                       # real SPAM v3, raw o_ owners
            "blockCount": bc,
        }
        tsid = d.get("lastEditedTerrainSessionID")
        if tsid:
            st3, b3 = await _cw_call("fetchMapData", roomKey=room_key,
                                     targetSessionID=tsid, saveOffset=0, isTerrain=1)
            tdd = (b3 or {}).get("data") or {}
            out["saveTerrainSpam"] = tdd.get("saveData") or ""
            out["terrainVersion"] = tdd.get("mapDataVersion") or 1
        else:
            out["saveTerrainSpam"] = ""
            out["terrainVersion"] = 1
        print(f"[cw] direct room {room_key}: {bc} blocks (o_ owners), dims {out['dimensions']}")
        return out
    except Exception as e:
        print(f"[cw] direct fetch failed for {room_key}: {type(e).__name__}")
    return None


def _cw_relay_blocks_allowed() -> bool:
    """Whether to read CW BLOCKS through the relay (/AWS). OFF by default: blocks
    are served live by the direct passthrough (yeeps_direct / _cw_passthrough),
    not the relay. This gate being undefined was raising NameError on the legacy
    _cw_fetch_room path. CW_RELAY_BLOCKS=1 re-enables the relay block read."""
    import os as _os
    return (_os.getenv("CW_RELAY_BLOCKS", "") or "").strip() == "1"


async def _cw_fetch_room(room_key: str):
    """Live-fetch one cw room's real content -> {dimensions, floorDepth,
    saveDataSpam, blockCount, saveTerrainSpam, terrainVersion} or None.

    LIVE-ONLY + IN-MEMORY cache (Blaze: nothing saved to the DB). Direct real-
    Yeeps lambda only -- NO relay /map fallback (its 16-account loop under the
    client's retry-on-empty was storming the relay and cascading sessions to
    fail). The in-mem cache (10 min, _cw_cache) only blunts retry bursts."""
    ck = f"room:{room_key}"
    hit = _cw_cache_get(ck)
    if hit is not None and (hit or {}).get("blockCount"):
        return hit or None
    # BLOCKS need a logged-in session (relay /AWS = Blaze's api); guest creds get
    # AccessDenied on G2_Maps. Gated OFF until Blaze approves the relay for blocks.
    if not _cw_relay_blocks_allowed():
        return None
    out = await _cw_fetch_room_direct(room_key)   # direct lambda, incl terrain
    if out and out.get("blockCount"):
        _cw_cache_put(ck, out)
        return out
    return None


async def _cw_fetch_grid(base: str):
    """Live-fetch a cw's real room grid -> list of {roomKey, gridPosition,
    themeKey, roomName} or None.

    Tries the FETCH account first (the grid account is the one real Yeeps
    routinely throttles to empty) and falls back to the grid account. NEVER
    caches an empty result, so a transient throttle doesn't stick for the whole
    cache TTL -- the next request retries and can recover."""
    ck = f"grid:{base}"
    hit = _cw_cache_get(ck)
    if hit:                      # only a non-empty grid is cached
        return hit
    for oc, code, tag in await _cw_service_accounts():
        try:
            data = await _cw_post("/rooms", {"oculusID": oc, "mobileCode": code,
                                             "worldKey": base})
        except Exception as e:
            print(f"[cw] /rooms fetch failed for {base} via {tag}: {e}")
            continue
        if not data.get("ok"):
            print(f"[cw] /rooms not ok for {base} via {tag}: {data.get('error')}")
            continue
        rooms = data.get("rooms") or []
        if rooms:
            _cw_cache_put(ck, rooms)
            return rooms
        print(f"[cw] {tag} account returned 0 rooms for {base} (throttled?) — trying fallback")
    return None


def _cw_key_safe(room_key: str) -> bool:
    """True if the 1.19 client can parse this community-world room key.

    The 1.19 client derives grid coordinates by Int32.Parse-ing the part of the
    room key after the world base. Real 2.x keys encode coords in a newer format
    the old parser chokes on (Int32.Parse(null) -> crash): a '.' (c_X_room_0.1),
    an 'n' for negatives (c_X_room_n4) or a '-' (c_X_room_-1.0). Only the plain
    '<base>_sandbox' entry and '<base>_room_<digits>' are safe.
    """
    import re as _re
    if room_key.endswith("_sandbox"):
        return True
    return bool(_re.fullmatch(r"c_.+_room_\d+", room_key))


def _cw_room_map_from_grid(v_key: str, rooms: list) -> dict:
    """Build a 1.19 room-map grid from the real cw nodes, adding a 'public'
    doorway so the player can arrive/leave.

    240 server: ALL clients are 2.x (big/live 2.31.4), which parse the real
    room keys natively -- so KEEP every room (the _cw_key_safe filter was a
    1.19-only workaround; applying it here dropped all the content rooms and
    left an empty sandbox). Real themeKey is kept too (2.31 has the newer themes).
    """
    safe = [n for n in (rooms or []) if n.get("roomKey")]
    if not safe:
        return _sandbox_room_map(v_key)
    data = []
    seen_pos = set()
    for n in safe:
        gp = n.get("gridPosition") or [0, 0]
        pos = (int(gp[0]), int(gp[1]))
        if pos in seen_pos:      # client rejects duplicate grid positions
            continue
        seen_pos.add(pos)
        data.append({
            "roomKey": n["roomKey"],
            "themeKey": n.get("themeKey") or "sandbox",
            "gridPosition": [pos[0], pos[1]],
        })
    # 2.x (this server): do NOT add a transitionKey/null-roomKey doorway node.
    # The 2.31.4 client uses passagewayLinks (carried on the real sandbox node)
    # and rejects the whole room map if it sees a null-roomKey node. The legacy
    # doorway is only added for old 1.19/1.31 clients (not served here).
    if not IS_V2:
        entry = next((n for n in safe if str(n.get("roomKey", "")).endswith("_sandbox")), safe[0])
        egp = entry.get("gridPosition") or [0, 0]
        door_pos = (int(egp[0]), int(egp[1]) - 1)
        while door_pos in seen_pos:
            door_pos = (door_pos[0], door_pos[1] - 1)
        data.append({
            "themeKey": "sandbox",
            "gridPosition": [door_pos[0], door_pos[1]],
            "transitionKey": "public",
            "blockLeft": 1, "blockRight": 1,
        })
    return {"versionKey": v_key, "version": 1, "data": data}


async def _cw_real_room_map(v_key: str):
    """The REAL live-Yeeps room map for a community world, served as-is.
    The 2.31.4 client needs the real node format (passagewayLinks connecting the
    sandbox to each content room, blockAll, etc.) -- our hand-built minimal grid
    lacks the links so the client fails with "Failed to get room map node".
    Fetched via real_yeeps guest creds (not the cw service accounts) and cached."""
    ck = f"cwmap:{v_key}"
    # LIVE + IN-MEMORY only (Blaze: nothing saved to the DB). Fetched live via
    # real_yeeps guest creds each time (10-min in-mem cache to blunt retry bursts).
    hit = _cw_cache_get(ck)
    if hit is not None:
        return hit or None
    try:
        from services import real_yeeps
        from boto3.dynamodb.types import TypeDeserializer
        it = await real_yeeps.get_item("G2_RoomMap", {"versionKey": {"S": v_key}})
        if it and it.get("Item"):
            d = TypeDeserializer()
            from decimal import Decimal as _Dec
            def _plain(x):
                if isinstance(x, _Dec):
                    return int(x) if x == x.to_integral_value() else float(x)
                if isinstance(x, list):
                    return [_plain(v) for v in x]
                if isinstance(x, dict):
                    return {k: _plain(v) for k, v in x.items()}
                return x
            item = {k: _plain(d.deserialize(val)) for k, val in it["Item"].items()}
            data = item.get("data")
            # 2.31.4 uses passagewayLinks, not transitionKey. Real Yeeps' RAW
            # G2_RoomMap item includes a legacy null-roomKey {"transitionKey":
            # "public"} doorway node that the cleaned fetchRoomMapData view the
            # real client actually consumes drops. The 2.31.4 client can't parse a
            # null-roomKey node and rejects the WHOLE room map -> RoomDatabase
            # never registers the sandbox node -> "Failed to get room map node
            # ... NO_DATA_FOUND". Keep only real roomKey nodes and strip
            # transitionKey, matching the shape the real client + our working
            # halloween26_1 mirror use. (The sandbox node's passagewayLinks
            # already carry the warps, so no separate doorway node is needed.)
            if isinstance(data, list):
                data = [{k: v for k, v in n.items() if k != "transitionKey"}
                        for n in data if n.get("roomKey")]
                item["data"] = data
            if isinstance(data, list) and data:
                try:
                    ver = int(item.get("version") or 1)
                except Exception:
                    ver = 1
                # Mirror the FULL real item (all ~80 cw_* attrs), not just
                # version+data. The CW entry gate (cwGetCanEnter) does a GetItem
                # G2_RoomMap c_<NAME> requesting a set of cw_* attributes and, if
                # ANY requested attr comes back NULL, discards the whole item --
                # so the room-map nodes never register and the client throws
                # "Failed to get room map node ... NO_DATA_FOUND". Storing every
                # real attr means whatever subset the client asks for is present.
                # (Read-only mirror of the world's own public metadata; nothing is
                # written back to Yeeps.) Keep the real uniqueID (a GUID the client
                # reads) -- only override the DynamoDB key fields.
                doc = dict(item)
                doc["versionKey"] = v_key
                doc["roomMapKey"] = v_key
                doc["version"] = ver
                _cw_cache_put(ck, doc)   # in-mem only; not persisted to the DB
                return doc
    except Exception as e:
        print(f"[cw] real room map fetch failed for {v_key}: {e}")
    _cw_cache_put(ck, {})   # cache the miss briefly so we don't refetch every node request
    return None


async def _cw_prewarm_room_maps(keys, delay: float = 3.0, cap: int = 60,
                                content_cap: int = 20):
    """Pre-mirror the REAL room maps for a set of community worlds into cwMirror,
    so a world is never COLD on a player's first entry.

    A cold world served while real Yeeps is throttled falls back to the
    hand-built grid, which lacks passagewayLinks -> the 2.31.4 client caches a
    "Failed to get room map node" failure and can't enter even after we recover.
    Warming ahead of time means _cw_real_room_map's DB-first path always has the
    real map. Trickled (one every `delay`s) and skips worlds already mirrored, so
    it costs real Yeeps almost nothing after the first pass. Uses real_yeeps guest
    creds (a separate budget from the cw service accounts)."""
    import asyncio as _aio
    from services.db import room_maps as _rm, db as _dbh
    seen, done, warmed_rooms = set(), 0, 0
    for k in keys:
        if not isinstance(k, str) or not k.startswith("c_") or k in seen:
            continue
        seen.add(k)
        if done >= cap:
            break
        # --- room MAP (grid) ---
        doc = None
        try:
            doc = await _rm.find_one({"versionKey": k, "cwMirror": 1}, {"_id": 0, "data": 1})
        except Exception:
            pass
        if not doc:
            try:
                doc = await _cw_real_room_map(k)
                if doc and isinstance(doc.get("data"), list) and doc["data"]:
                    done += 1
                    print(f"[cw] prewarmed room map {k} ({len(doc['data'])} nodes)")
            except Exception as e:
                print(f"[cw] prewarm {k} failed: {e}")
            await _aio.sleep(delay)
        # --- room CONTENT (blocks) for each node, so a cold entry during a
        # throttle window still has blocks. Skip rooms already in cw_content.
        # Capped per pass (content_cap): warming every room of every world in one
        # pass bursts real Yeeps into throttling; a small cap + the 20-min loop
        # fills all worlds gradually (content persists forever, so it converges).
        room_keys = [n.get("roomKey") for n in (doc or {}).get("data") or [] if n.get("roomKey")]
        for rk in room_keys:
            if warmed_rooms >= content_cap:
                break
            try:
                if await _dbh["cw_content"].find_one({"roomKey": rk, "blockCount": {"$gt": 0}}, {"_id": 1}):
                    continue   # already cached
            except Exception:
                pass
            try:
                got = await _cw_fetch_room(rk)   # fetch-account-primary + persists
                if got and got.get("blockCount"):
                    warmed_rooms += 1
                    print(f"[cw] prewarmed content {rk} ({got['blockCount']} blocks)")
            except Exception as e:
                print(f"[cw] prewarm content {rk} failed: {e}")
            await _aio.sleep(delay)
        if warmed_rooms >= content_cap and done >= cap:
            break
    if done or warmed_rooms:
        print(f"[cw] prewarm pass done: {done} new world maps, {warmed_rooms} room contents mirrored")
    return done




def _aws_id() -> str:
    return uuid.uuid4().hex.upper()


def _make_response(payload: dict) -> tuple[str, dict]:
    """Build a DynamoDB-formatted response: body text + headers."""
    payload_text = json.dumps(payload)
    crc32 = str(zlib.crc32(payload_text.encode("utf-8")) & 0xFFFFFFFF)
    headers = {
        "Content-Type": "application/x-amz-json-1.0",
        "x-amzn-RequestId": _aws_id(),
        "x-amz-crc32": crc32,
    }
    return payload_text, headers


def _is_119_map(doc) -> bool:
    """True for a map blob the current client can read.

    1.19-era maps are .NET BinaryFormatter data (base64 "AAEAAAD..."). The
    1.31.0-blaze client SAVES in the SPAM format ("U1BBTQ...", mapDataVersion 3)
    and reads both, so serve either. (Serving only "AAEAAAD" made every 1.31
    save come back as an empty map after a relaunch.)
    """
    save = (doc or {}).get("saveData")
    return isinstance(save, str) and (save.startswith("AAEAAAD") or save.startswith("U1BBTQ"))


async def _map_doc(map_key) -> dict:
    """The G2_Maps document to serve for a map key.

    The client reads "{roomKey}$map_{lastEditedSessionID}_save{offset}", and its
    own saves are found under that exact key. For the first chunk only, an
    editor-imported map or the room's "{roomKey}$current" snapshot is used
    instead, so one blob is never re-served as save1, save2... Anything else
    gets an empty (0-block) map. Fields a stored map lacks are filled by the
    normal G2_Maps default injection, as in the original server.
    """
    map_key = map_key if isinstance(map_key, str) else ""
    room_key = map_key.split("$", 1)[0]

    # Community worlds mirror real Yeeps content live. Serve the full converted
    # map on the first chunk only (save0 / current / un-chunked); later chunks
    # return empty so one blob is never re-served as save1, save2...
    if room_key.startswith("c_"):
        first_chunk = (map_key.endswith("_save0")
                       or map_key.endswith("$current")
                       or "$map_" not in map_key)
        if first_chunk:
            live = await _cw_fetch_room(room_key)
            if live and (live.get("saveDataSpam") or live.get("saveData")):
                # Every client on THIS (240) server is 2.x, so serve the 2.x
                # SPAM blob + mapDataVersion 3 (the 1.19 blob loads empty on the
                # live 2.31.4 client).
                _spam = live.get("saveDataSpam")
                return {
                    "mapKey": map_key,
                    "roomKey": room_key,
                    "saveData": _spam if _spam else live["saveData"],
                    "saveInternalStates": EMPTY_MAP_SAVE_DATA,
                    "mapDataVersion": 3 if _spam else 1,
                }
        return {
            "mapKey": map_key,
            "roomKey": room_key,
            "saveData": EMPTY_MAP_SAVE_DATA,
            "saveInternalStates": EMPTY_MAP_SAVE_DATA,
            "mapDataVersion": 1,
        }

    collection = get_collection("G2_Maps")
    # Private worlds: ONE shared copy in the 1.31 DB (services.db.maps_for).
    from services.db import maps_for as _maps_for, is_pw_key as _is_pw
    if _is_pw(room_key):
        collection = _maps_for(room_key)
    found = None
    if collection is not None and map_key:
        found = await collection.find_one({"mapKey": map_key}, {"_id": 0})
    if not _is_119_map(found) and map_key.endswith("_save0"):
        try:
            from services.db import imported_maps
            found = await imported_maps.find_one({"roomKey": room_key}, {"_id": 0})
        except Exception as e:
            print(f"[dynamo] imported-map lookup failed: {e}")
            found = None
        if not _is_119_map(found) and collection is not None:
            found = await collection.find_one({"mapKey": f"{room_key}$current"}, {"_id": 0})
    if _is_119_map(found):
        out = {**found, "mapKey": map_key, "roomKey": room_key}
        # 1.31: a map can carry its FULL sign/dispenser internal states in
        # saveInternalStatesFull (Starter House). Those crash APKs older than
        # build_gate.GUARD_MIN_BUILD, so serve them only when every build that can
        # currently play is new enough; otherwise the safe saveInternalStates.
        full = out.pop("saveInternalStatesFull", None)
        if full:
            try:
                from services.build_gate import only_guard_builds
                if await only_guard_builds():
                    out["saveInternalStates"] = full
            except Exception as e:
                print(f"[dynamo] full-states gate failed for {map_key}: {e}")
        return out
    return {
        "mapKey": map_key,
        "roomKey": room_key,
        "saveData": EMPTY_MAP_SAVE_DATA,
        "saveInternalStates": EMPTY_MAP_SAVE_DATA,
        "mapDataVersion": 1,
    }


def _sandbox_base(v_key: str) -> str:
    """Base key of a private/community world. p_o_123 / c_NAME stay as-is;
    a bare o_123 maps to its private world p_o_123."""
    if v_key.startswith("p_") or v_key.startswith("c_"):
        return v_key
    return f"p_{v_key}"


def _sandbox_room_map(v_key: str, main_theme: str = "suburb",
                      sandbox_theme: str = "sandbox") -> dict:
    """Room-map grid for a private (p_o_<acct>) or community (c_<name>) world.

    Layout is a single column:  [public doorway] -> [sandbox] -> [main].
    The "public" transition node is REQUIRED: when you travel in via the
    world map's "private"/"communityWorlds" node, the client runs
    GetTransitionNode on the destination map looking for the doorway back,
    and throws NO_TRANSITION_NODE_FOUND if it isn't there.

    The node themeKeys drive the in-game scene biome; main_theme/sandbox_theme
    are read from the stored room docs so editor biome changes take effect.
    """
    base = _sandbox_base(v_key)
    return {
        "versionKey": v_key,
        "version": 1,
        "data": [
            {"themeKey": "sandbox", "gridPosition": [0, 0], "transitionKey": "public",
             "blockLeft": 1, "blockRight": 1},
            {"roomKey": f"{base}_sandbox", "themeKey": sandbox_theme, "gridPosition": [0, 1],
             "blockLeft": 1, "blockRight": 1},
            {"roomKey": base, "themeKey": main_theme, "gridPosition": [0, 2],
             "blockLeft": 1, "blockRight": 1, "blockUp": 1},
        ],
    }


async def _pw_room_map(v_key: str) -> dict:
    """Private-world room map with node biomes taken from the stored room docs
    (falls back to suburb/sandbox when a room hasn't been themed)."""
    base = _sandbox_base(v_key)
    main_theme, sandbox_theme = "suburb", "sandbox"
    try:
        collection = get_collection("G2_Rooms")
        if collection is not None:
            md = await collection.find_one({"roomKey": base}, {"_id": 0, "themeKey": 1})
            if md and md.get("themeKey"):
                main_theme = md["themeKey"]
            sd = await collection.find_one({"roomKey": f"{base}_sandbox"}, {"_id": 0, "themeKey": 1})
            if sd and sd.get("themeKey"):
                sandbox_theme = sd["themeKey"]
    except Exception as e:
        print(f"[pw] room-map theme lookup failed: {e}")
    # Prefer the player's STORED layout (extra rooms they bought: _left/_right/
    # _room_N). Previously this always synthesized the fixed 2-room grid, so any
    # extra pw room never appeared (owner 2026-10-17: StaffAlias / Staff Member A
    # "does not see his two extra maps"). Live DB first; else migrate the 1.31
    # layout over once. Only fall back to the synthesized grid when neither exists.
    try:
        stored = await _stored_pw_layout(base)
        if stored:
            for n in stored["data"]:
                rk = n.get("roomKey")
                if rk == base:
                    n["themeKey"] = main_theme
                elif rk == f"{base}_sandbox":
                    n["themeKey"] = sandbox_theme
            return stored
    except Exception as e:
        print(f"[pw] stored layout lookup failed for {base}: {e}")
    return _sandbox_room_map(v_key, main_theme=main_theme, sandbox_theme=sandbox_theme)


async def _stored_pw_layout(base: str):
    """The stored pw room-map layout for p_o_<acct> (live DB, else the 1.31 DB,
    which is then copied to the live DB). Only returned if it actually has extra
    rooms beyond main+sandbox and contains both of those (else the synthesized
    grid is used). Version is bumped to >=2 so a client that cached the old
    2-room v1 grid re-reads it."""
    import os as _os
    from services.db import room_maps as _rm, client as _mc

    def _ok(doc):
        nodes = (doc or {}).get("data")
        if not isinstance(nodes, list):
            return False
        keys = {n.get("roomKey") for n in nodes if isinstance(n, dict)}
        if doc.get("liveMirror"):
            # copied from the player's REAL Yeeps pw (tools/copy_real_pw.py): real
            # 2.x worlds have the sandbox hub and their own rooms, no "main" room
            return f"{base}_sandbox" in keys
        return base in keys and f"{base}_sandbox" in keys and len(keys - {None}) > 2

    doc = await _rm.find_one({"versionKey": base}, {"_id": 0})
    if not _ok(doc) and _os.getenv("CARRYOVER_DB"):
        old = await _mc[_os.getenv("CARRYOVER_DB")]["room_maps"].find_one({"versionKey": base}, {"_id": 0})
        if _ok(old):
            doc = dict(old)
            doc["version"] = max(int(doc.get("version") or 1), 1) + 1
            await _rm.update_one({"versionKey": base}, {"$set": doc}, upsert=True)
            print(f"[pw] migrated {base} layout from 1.31 ({len(doc['data'])} nodes)")
    if not _ok(doc):
        return None
    doc = dict(doc)
    doc["versionKey"] = base
    doc["version"] = max(int(doc.get("version") or 1), 2)
    doc["data"] = [dict(n) for n in doc["data"]]
    return doc


# Economy / daily config the 2.31.4 client reads as G2_Globals keys. Unknown
# globals used to come back as data:0, so the *RefStartTime keys were EMPTY ->
# "TradingCardManager: trading card ref start time is empty" / "Failed to get
# trading card pack ref start time" (card packs broken, TradingCardPackItem
# exceptions) and "Tokens: Could not load daily reward refresh time".
# Owner 2026-10-17: card pack = 1 bean, buyable constantly (the client counts
# purchases per day from the ref start time, so a huge per-day cap is how
# "reset every second" is achieved). A stored G2_Globals doc with the same key
# overrides these (served before this fallback).
_CARD_ATTR_SEEN: set = set()
_ACCT_ATTR_SEEN: set = set()
_GLOBAL_KEYS_SEEN: set = set()   # G2_Globals keys clients read (logged once)   # card-capture dedupe (in-mem)

_GLOBAL_DEFAULTS = {
    "priceBeans_tradingCardPack": 1,
    "beanTradingCardPacksPerDay": 999999,
    "beanTradingCardPackRefStartTime": "2025 01/01 00:00:00",
    "worldTokenDailyRefStartTime": "2025 01/01 00:00:00",
}


# Room settings a player can change and save in their private world
# (g2_saveRoomMapProperties / pw rename/retheme). Served back over the rebuilt
# pw room doc so they persist across loads.
_PW_SAVED_ROOM_FIELDS = (
    "floorDepth", "liquidType", "liquidLevel", "liquidColor", "customLightingKey",
    "roomName", "themeKey", "lobbyDirection", "dimensions", "noPaste",
)


def _full_room(room_key: str, room_name: str, dims: list,
               read_only: bool = False, theme: str = "sandbox", lobby: int = 1) -> dict:
    """Complete room-data document. Private/community rooms need the FULL field
    set (doors, liquid, lighting, terrain/save version fields) or the client
    throws ArgumentNullException while building the RoomDatabaseEntry.

    read_only=True (community worlds): not Creative, mode-locked and save-locked
    so the mirrored real-Yeeps content can be walked but never edited or saved
    back. creativeSessionID is still non-null so the client treats it as the
    main instance (otherwise it spams "not in the main instance of this room").
    """
    door = {"block": 0, "position": [0, 0]}
    return {
        "roomKey": room_key,
        "roomName": room_name,
        "themeKey": theme,
        "dimensions": dims,
        "isOfficial": 0,
        # Creative + a non-null creativeSessionID is the "main instance" check
        # the client runs; without it building is refused as "not in the main
        # instance of this room".
        "isCreative": 0 if read_only else 1,
        "creativeSessionID": 1,
        "blockMatchmakingSegmentation": 1,
        "isModeLocked": 1 if read_only else 0,
        "isDevLocked": 0,
        "isSaveLocked": 1 if read_only else 0,
        "noSave": 1 if read_only else 0,
        "noPaste": 1 if read_only else 0,
        "floorDepth": 0,
        "lobbyDirection": lobby,
        "customLightingKey": "",
        "liquidColor": "",
        "liquidLevel": 0,
        "liquidType": 0,
        "autoCycleTimestamp": 0,
        "isAutoCycling": 0,
        "itemRestrictionsGroupKeys": [],
        "itemRestrictionsIsWhiteList": 0,
        "door_up": door, "door_down": door, "door_left": door, "door_right": door,
        "lastEditedSessionID": 1,
        "lastEditedTerrainSessionID": 1,
        "lastRoomDataEditorSessionID": 1,
        "previousEditedSessionID": 1,
        "previousEditedTerrainSessionID": 1,
        "lastValidSaveOffset": 0,
        "overrideSaveLockTimestamp": "1970 01/01 00:00:00",
        "previousMapProperties": {},
        "roomDataVersion": 1,
        "terrainDataVersion": 1,
    }


async def _get_fallback_doc(table_name: str, query: dict) -> dict:
    """Provide default documents for sensitive tables when missing from DB."""
    # 1. G2_RoomMap fallback (World map / grid)
    if table_name == "G2_RoomMap":
        v_key = query.get("versionKey")
        if isinstance(v_key, str) and v_key.startswith("c_"):
            # Community world: serve the REAL live-Yeeps room map (real nodes +
            # passagewayLinks), read DIRECT from real Yeeps G2_RoomMap with guest
            # creds (no relay). No relay /rooms fallback (that was "my api").
            real = await _cw_real_room_map(v_key)
            if real:
                return real
            return _sandbox_room_map(v_key)
        if isinstance(v_key, str) and (v_key.startswith("o_")
                                       or v_key.startswith("p_o_")):
            return await _pw_room_map(v_key)
        # Generic fallback for room map
        collection = get_collection("G2_RoomMap")
        if collection is not None:
            return await collection.find_one({"versionKey": "beta4"}, {"_id": 0})

    # 2. G2_Rooms fallback (Individual room data)
    if table_name == "G2_Rooms":
        room_key = query.get("roomKey")
        if isinstance(room_key, str):
            is_sandbox = (room_key.startswith("o_")
                          or room_key.startswith("p_o_")
                          or room_key.startswith("c_"))
            if room_key.startswith("c_"):
                # Community world: TRUE real-Yeeps read passthrough. Serve the
                # live room doc AS-IS (real sid/version/floorDepth/dims/isCreative)
                # so the 2.31.4 client sees a real block save and fetches the
                # blocks. No local _full_room, no placeholder/mode-lock injection
                # (our old sid=1/roomDataVersion=1/noSave=1/isModeLocked=1 made the
                # client skip the block fetch -> empty world). Read-only; real
                # Yeeps' own perms stop cross-world griefing, so no mode-lock here.
                real = await _cw_fetch_room_doc(room_key)
                if real:
                    return real
                # Fallback only if the live fetch fails: minimal doc with real
                # dims from the block fetch. Use NON-GATING values (roomDataVersion
                # 3, noSave 0, no mode-lock, sid!=1) so the client still tries to
                # fetch blocks -- the gating placeholders (_full_room's sid=1/
                # roomDataVersion=1/noSave=1/isModeLocked=1) are what caused the
                # empty-room skip in the first place.
                live = await _cw_fetch_room(room_key)
                dims = ((live or {}).get("dimensions")
                        or ([32, 80, 32] if room_key.endswith("_sandbox") else [80, 80, 80]))
                r = _full_room(room_key, "Community World", dims, read_only=True)
                if live and live.get("floorDepth") is not None:
                    r["floorDepth"] = live["floorDepth"]
                r["roomDataVersion"] = 3
                r["noSave"] = 0
                r["lastEditedSessionID"] = 2
                r.pop("isModeLocked", None)
                return r
            if is_sandbox:
                # Private worlds (p_o_<acct>) stay editable/saveable.
                # _sandbox suffix = the small 32x80x32 starter room; the bare
                # base key = the big 80x80x80 main room.
                if room_key.endswith("_sandbox"):
                    r = _full_room(room_key, "Sandbox", [32, 80, 32])
                else:
                    # The big main room uses a real biome, not the flat sandbox theme.
                    # lobbyDirection 3 is the standard orientation for the pw main room.
                    r = _full_room(room_key, "Private World", [80, 80, 80], theme="suburb", lobby=3)
                # Overlay what the player SAVED for this room (floor level, liquid,
                # lighting, name, theme, ...). Without this every pw load served the
                # rebuilt defaults (floorDepth 0) and the floor level the player set
                # never stuck (owner 2026-10-17: "peoples pw dont save the floor level").
                try:
                    _stored = await get_collection("G2_Rooms").find_one({"roomKey": room_key}, {"_id": 0})
                    # shared pw settings: prefer the 1.31 copy (same doc both servers use)
                    if _os.getenv("CARRYOVER_DB"):
                        from services.db import client as _mc
                        _s131 = await _mc[_os.getenv("CARRYOVER_DB")]["rooms"].find_one({"roomKey": room_key}, {"_id": 0})
                        if _s131:
                            _stored = {**(_stored or {}), **{k: v for k, v in _s131.items() if v is not None}}
                    for _k in _PW_SAVED_ROOM_FIELDS:
                        if _stored and _stored.get(_k) is not None:
                            r[_k] = _stored[_k]
                except Exception as _e:
                    print(f"[pw] saved room props overlay failed for {room_key}: {_e}")
                # 2.x draws the ground as terrain at floorDepth; no pw has terrain
                # edits, so floorDepth 0 = no ground at all (owner 2026-10-17: "pw
                # terrain is not there ... make it the flat terrain"). A new real
                # Yeeps pw is floorDepth 1 = flat ground.
                if not r.get("floorDepth"):
                    r["floorDepth"] = 1
                # 2.x terrain needs every dimension divisible by its chunk size (16)
                # or it throws and draws no terrain; 1.31 pws can be e.g. 200x100x200.
                try:
                    _dims = r.get("dimensions")
                    if isinstance(_dims, str):
                        _dims = json.loads(_dims)
                    if isinstance(_dims, list) and len(_dims) == 3:
                        r["dimensions"] = [((int(v) + 15) // 16) * 16 for v in _dims]
                except Exception:
                    pass
                return r
            # Official / public room fallback.
            return {
                "roomKey": room_key,
                "roomName": room_key,
                "isOfficial": 1,
                "themeKey": "suburb",
                # 2.4 terrain needs every dimension divisible by its chunk size
                # (16): 100 threw "playspace dimensions must be divisible by
                # chunk size" in freebuild rooms. 1.31 keeps 100.
                "dimensions": [128, 96, 128] if IS_V2 else [100, 100, 100],
                "floorDepth": 0,
                "lobbyDirection": 1,
                "isCreative": 1,
                # Official / public rooms are mode-locked: can't be flipped to
                # creative (wilds + pw are exempt and set elsewhere).
                "isModeLocked": 1,
                "noSave": 0,
                "lastEditedSessionID": 1,
                "roomDataVersion": 1,
                "creativeSessionID": 1,
                "blockMatchmakingSegmentation": 1,
            }

    # 3. G2_Maps fallback (Saved block data)
    if table_name == "G2_Maps":
        return await _map_doc(query.get("mapKey", ""))

    # 4. G2_Globals fallback
    if table_name == "G2_Globals":
        key = query.get("key")
        if key == "activeBundleKey":
            return {"key": "activeBundleKey", "data": "bundle_commando"}
        if key in _GLOBAL_DEFAULTS:
            return {"key": key, "data": _GLOBAL_DEFAULTS[key]}
        return {"key": key, "data": 0}

    return None


# shouldForceRefresh stickiness: the client polls this single attr every ~30s
# and only reloads on a *persistent* 1 (serving 1 once then clearing let it
# re-read a 0 mid-refresh and abort). We keep serving 1 until the client
# actually re-pulls its account — consumed in login.py (questLogIntoAccount) or
# by a full-account GetItem here. Live testing proved the client NEVER acts on
# the flag while standing in a loaded room (it just keeps polling), so there is
# no refresh-loop risk; the backstop is just a long safety expiry so a flag set
# on an account that never logs back in can't persist indefinitely. The flag now
# survives long enough to be picked up on the player's next room transition /
# relog. Only warned/banned accounts ever have it set.
# Maps accountID -> monotonic time we first served a 1.
_FR_FIRST_SEEN: dict[str, float] = {}
_FR_BACKSTOP_SEC = 600.0


async def _clear_force_refresh(account_id: str) -> None:
    _FR_FIRST_SEEN.pop(account_id, None)
    if not account_id:
        return
    try:
        from services.db import accounts as _accts
        await _accts.update_one({"accountID": account_id},
                                {"$set": {"shouldForceRefresh": 0}})
    except Exception as _e:
        print(f"[dynamo] shouldForceRefresh clear failed: {_e}")


from services.account_privacy import PRIVATE_ACCOUNT_FIELDS, public_view

# Server-only G2_Globals entries (service logins, editor state) never served.
PRIVATE_GLOBAL_KEYS = {"cw_fetch_account", "editorPwWindow"}


def _is_private_global(key) -> bool:
    """A G2_Globals key that must NEVER be served to a client. Covers the fixed
    set plus ALL cw service-account creds by prefix (cw_fetch_account,
    cw_grid_account, cw_fetch_account2/3..., cw_grid_account2...): these hold
    oculusID/mobileCode logins and leaking them would expose the accounts."""
    if not isinstance(key, str):
        return False
    # Prefix covers EVERY fetch/grid service-account login, incl. named ones like
    # cw_fetch_staffalias (2026-10-17) -- not just cw_fetch_accountN. Internal
    # server caches (leading "_") are never client data either.
    return (key in PRIVATE_GLOBAL_KEYS
            or key.startswith("cw_fetch_")
            or key.startswith("cw_grid_")
            or key.startswith("_"))


def _role_locked_view(doc: dict) -> dict:
    """Account as the client may see it: role-locked patterns only for holders
    of their role; server-only roles hidden (services/role_locks.py)."""
    from services import role_locks
    if "ownedPatterns" not in doc and "roleKeys" not in doc and "ownedBundles" not in doc:
        return doc
    doc = dict(doc)
    if "ownedBundles" in doc:
        doc["ownedBundles"] = role_locks.apply_bundles(doc.get("ownedBundles"), doc.get("roleKeys"))
    if "customizedItemDatas" in doc:
        # shared reskins everyone owns (login.py SHARED_RESKINS): Kitty K TUFF banner
        _cid = list(doc.get("customizedItemDatas") or [])
        if "o_STAFF00000000002$1" not in _cid:
            _cid.append("o_STAFF00000000002$1")
        doc["customizedItemDatas"] = _cid
    owned, roles = role_locks.apply(doc.get("ownedPatterns"), doc.get("roleKeys"))
    if "ownedPatterns" in doc:
        doc["ownedPatterns"] = owned
    if "roleKeys" in doc:
        doc["roleKeys"] = roles
    return doc


async def handle_get_item(body: dict) -> tuple[str, dict]:
    """Handle DynamoDB GetItem operation."""
    table_name = body.get("TableName", "")
    key = body.get("Key", {})
    attrs_to_get = body.get("AttributesToGet", [])
    requested_attrs = list(attrs_to_get)

    collection = get_collection(table_name)
    query = unwrap_key(key)
    # Log each distinct G2_Globals key the client reads (once per process), to find
    # where config like the trading-card catalog is expected (2026-10-17).
    try:
        if table_name == "G2_Globals":
            _gk = str(query.get("key"))
            if _gk not in _GLOBAL_KEYS_SEEN and len(_GLOBAL_KEYS_SEEN) < 500:
                _GLOBAL_KEYS_SEEN.add(_gk)
                print(f"[globals] client read G2_Globals key={_gk} attrs={requested_attrs}")
    except Exception:
        pass
    # Reskin hunt (2026-10-18): every distinct attribute set the client reads from
    # G2_Accounts, once per process, to find where owned reskins are expected.
    try:
        if table_name == "G2_Accounts":
            _asig = ",".join(sorted(map(str, requested_attrs))) + "|" + str(body.get("ProjectionExpression") or "")
            if _asig not in _ACCT_ATTR_SEEN and len(_ACCT_ATTR_SEEN) < 300:
                _ACCT_ATTR_SEEN.add(_asig)
                print(f"[acctread] G2_Accounts attrs={_asig[:600]}")
    except Exception:
        pass
    # Card-system capture (2026-10-17): record which card fields the client reads
    # from its account (once per distinct attr set) -> Mongo card_capture.
    try:
        _pe = str(body.get("ProjectionExpression") or "")
        _cattrs = [a for a in requested_attrs if any(w in str(a).lower() for w in ("card", "shard"))]
        if table_name == "G2_Accounts" and (_cattrs or "card" in _pe.lower() or "shard" in _pe.lower()):
            _sig = ",".join(sorted(map(str, requested_attrs))) + "|" + _pe
            if _sig not in _CARD_ATTR_SEEN:
                _CARD_ATTR_SEEN.add(_sig)
                from services.db import db as _cdb
                from datetime import datetime as _dt, timezone as _tz
                await _cdb["card_capture"].insert_one({
                    "at": _dt.now(_tz.utc).strftime("%Y-%m-%d %H:%M:%S"), "kind": "account-getitem",
                    "attrs": requested_attrs, "projection": _pe,
                    "names": body.get("ExpressionAttributeNames")})
                print(f"[cardcap] account read wants card attrs: {requested_attrs or _pe}")
    except Exception as _ce:
        print(f"[cardcap] getitem capture failed: {_ce}")

    # 0. REAL-YEEPS passthrough (playyeeps line). Live/play clients play the
    # real Yeeps world: forward world-geometry reads (G2_RoomMap/G2_Rooms/
    # G2_Maps) straight to real Yeeps' AWS with guest Cognito creds and return
    # the real item. Only the player identity (G2_Accounts/*) is served locally.
    # Personal/community worlds (p_/o_/c_) are forwarded too (real private
    # worlds are guest-readable); if real Yeeps has no such item we fall through
    # to local handling so our own staging worlds still work.
    from services import big_patterns as _bp
    if _bp.is_play(body.get("_clientBuildTag")):
        from services import real_yeeps
        if real_yeeps.is_world_table(table_name):
            real = await real_yeeps.get_item(table_name, key)
            if real is not None:
                item = real.get("Item")
                if item:
                    return _make_response({"Item": item})
                # real Yeeps has no such key: fall through to local handling
                # (our staging/personal worlds), below.

    # 1. Database lookup
    doc = None
    _mk = query.get("mapKey", "") if table_name == "G2_Maps" else ""
    from config import IS_V2 as _V2
    if _V2 and isinstance(_mk, str) and "terrain" in _mk.lower():
        # 2.x terrain saves: serve only a real stored terrain doc. Falling back
        # to the room's block map gives "TerrainSaveData: unsupported data
        # version" / "Failed to load terrain save data: INVALID_FORMAT".
        print(f"[dynamo] terrain read {_mk}")
        from services.db import maps_for as _maps_for2, is_pw_key as _is_pw2
        _tcol = _maps_for2(_mk) if _is_pw2(_mk) else collection
        doc = await _tcol.find_one({"mapKey": _mk}, {"_id": 0}) if _tcol is not None else None
        if doc is None and _tcol is not None:
            # the room's latest terrain save (handlers/maps.py, isTerrain)
            doc = await _tcol.find_one({"mapKey": f"{_mk.split('$', 1)[0]}$terrain_current"}, {"_id": 0})
        if doc is None:
            return _make_response({})
    elif table_name == "G2_Maps":
        doc = await _map_doc(query.get("mapKey", ""))
    elif collection is not None:
        doc = await collection.find_one(query, {"_id": 0})

    # Big APK clients get the 1.31.0 world as their main room map (services/big_world.py).
    # Stock big clients use "freebuild_0"; the live 2.31.4 base (biglive*) spawns
    # into a seasonal hub map key instead (e.g. "halloween26_1"). BOTH the REST
    # fetchRoomMapData path (handlers/rooms.py) and this DynamoDB GetItem path must
    # serve the SAME map doc, or the client reads version 1311 from one and version
    # 1 (the beta4 fallback) from the other -> on the GetItem the `version` attr
    # comes back inconsistent/NULL and the client throws ArgumentNullException,
    # the room never finishes loading and the Photon join never fires. Route any
    # non-personal hub map a big client asks for to big_world (version 1311, full
    # node data) so both paths agree. Personal/community worlds (p_/o_/c_) keep
    # their own handling below.
    if table_name == "G2_RoomMap":
        _vk = query.get("versionKey")
        if isinstance(_vk, str) and not _vk.startswith(("p_", "o_", "c_")):
            from services import big_patterns, big_world
            # Stock big clients (big51) get the beta4 world. Live/play clients do
            # NOT -- they play the real Yeeps world (forwarded above); the beta4
            # override here would put them back in the 2.31.0 map on any key real
            # Yeeps happens to lack.
            if big_patterns.is_big(body.get("_clientBuildTag")) and not big_patterns.is_play(body.get("_clientBuildTag")):
                doc = await big_world.room_map_doc(_vk) or doc

    # 2. Fallback logic
    if doc is None:
        doc = await _get_fallback_doc(table_name, query)

    # Unknown account (e.g. a community-world builder we have no local record
    # of): resolve its public profile straight from real Yeeps, like 1.31's
    # _live_account, so the client shows the real builder name. CW blocks carry
    # raw o_ owner IDs; this is where the client turns one into a displayName.
    if doc is None and table_name == "G2_Accounts":
        from services.account_resolve import live_account
        doc = await live_account(query.get("accountID"))

    # 3. Build item and post-process
    if table_name == "G2_Globals" and _is_private_global(query.get("key")):
        doc = None
    if doc:
        if table_name == "G2_Accounts" and isinstance(doc, dict):
            doc = public_view(_role_locked_view(doc))
        item = wrap_item(doc, requested_attrs if requested_attrs else None)
        
        # Specialized G2_Bundles logic (MinVersion, etc)
        if table_name == "G2_Bundles" and isinstance(doc, dict):
            if doc.get("bundleKey") == "bundle_commando":
                item["endTimestamp"] = {"S": "2025 02/20 07:00:00"}
                item["minVersion"] = {"S": "1.0.0"}
            elif "minVersion" in doc:
                item["minVersion"] = to_dynamo(doc["minVersion"])
    else:
        # Not found — initialize with NULLs to be filled by defaults
        item = {a: {"NULL": True} for a in requested_attrs}

    # Community-world rooms are a VERBATIM real-Yeeps relay: do NOT inject the
    # ~21 room defaults (door_*, noSave, isModeLocked, roomDataVersion, ...) that
    # real Yeeps omits -- the client validates the doc against real Yeeps' exact
    # shape and stalls room load otherwise. Serve only the real keys (missing
    # requested attrs stay NULL and get stripped below, as real Yeeps omits them).
    _cw_room_verbatim = (table_name == "G2_Rooms"
                         and str(query.get("roomKey") or "").startswith("c_"))
    # Inject defaults for sensitive tables
    if not _cw_room_verbatim and table_name in ["G2_Rooms", "G2_Maps", "G2_Accounts", "G2_Bundles", "G2_Globals", "G2_AccountStats"] and item:
        for attr in list(item.keys()):
            # shouldForceRefresh: STICKY until the client actually consumes it.
            # Set to 1 on an in-game ban/warn so the client force-refreshes and
            # picks up adminBlocked / hasPendingWarning mid-session. See the
            # _FR_FIRST_SEEN note above for why we don't clear on the first poll.
            if table_name == "G2_Accounts" and attr == "shouldForceRefresh":
                stored = 0
                try:
                    stored = int((doc or {}).get("shouldForceRefresh", 0) or 0)
                except Exception:
                    stored = 0
                _aid = query.get("accountID") or ""
                if stored != 1:
                    item[attr] = {"N": "0"}
                    _FR_FIRST_SEEN.pop(_aid, None)
                    continue
                # A full-account read (more than just this attr) IS the refresh
                # the flag asks for -> serve 1 and consume it so it fires once.
                if len(requested_attrs) > 1:
                    item[attr] = {"N": "1"}
                    await _clear_force_refresh(_aid)
                    continue
                # Single-attr poll: keep serving 1 (sticky) until consumed, with
                # a monotonic backstop so it can never force-refresh forever.
                now = time.monotonic()
                first = _FR_FIRST_SEEN.setdefault(_aid, now)
                if now - first > _FR_BACKSTOP_SEC:
                    item[attr] = {"N": "0"}
                    await _clear_force_refresh(_aid)
                else:
                    item[attr] = {"N": "1"}
                continue
            
            # TESTING: un-mode-lock every room so modes can be switched freely.
            # Toggle off with env UNLOCK_ALL_MODES=0 after testing.
            if table_name == "G2_Rooms" and attr == "isModeLocked":
                from config import UNLOCK_ALL_MODES
                if UNLOCK_ALL_MODES:
                    item[attr] = {"N": "0"}
                    continue
                # Owner's rule (2026-10-12): shared maps can never be switched
                # to creative in-game (players flipped Starter House and wiped it).
                # Only private (p_) and community (c_) worlds keep their own lock.
                _rk = str(query.get("roomKey") or "")
                if _rk and not _rk.startswith(("p_", "c_")):
                    from services import creative_builders as _cb
                    item[attr] = {"N": "0" if await _cb.is_builder("", body.get("_deviceKey", "")) else "1"}
                    continue

            # General default injection
            if item[attr] == {"NULL": True}:
                item[attr] = to_dynamo(default_for_property(attr))

    # 2.x reads numeric room fields with Int32.Parse(attr.N); a field served as
    # {"S": ""} has N == null -> ArgumentNullException in RoomDatabase and the
    # room never loads. Real DynamoDB omits unset attributes, so do the same.
    from config import IS_V2 as _V2
    if table_name == "G2_Rooms" and item and (not requested_attrs or "isModeLocked" in requested_attrs):
        from config import UNLOCK_ALL_MODES as _UNL
        _rk = str(query.get("roomKey") or "")
        if not _UNL and _rk and not _rk.startswith(("p_", "c_")):
            from services import creative_builders as _cb2
            item["isModeLocked"] = {"N": "0" if await _cb2.is_builder("", body.get("_deviceKey", "")) else "1"}
    if _V2 and table_name == "G2_Rooms" and item:
        for attr in [a for a, v in item.items() if v in ({"S": ""}, {"NULL": True})]:
            del item[attr]

    if not item:
        return _make_response({})

    return _make_response({"Item": item})


async def handle_batch_get_item(body: dict) -> tuple[str, dict]:
    """Handle DynamoDB BatchGetItem operation."""
    request_items = body.get("RequestItems", {})
    responses = {}

    from services import big_patterns as _bp_batch
    _play = _bp_batch.is_play(body.get("_clientBuildTag"))

    for table_name, table_req in request_items.items():
        collection = get_collection(table_name)
        keys = table_req.get("Keys", [])
        attrs_to_get = table_req.get("AttributesToGet", [])

        items = []
        for dynamo_key in keys:
            query = unwrap_key(dynamo_key)
            # REAL-YEEPS passthrough: play clients get each world-table key from
            # real Yeeps (guest creds). Missing keys fall through to local.
            if _play:
                from services import real_yeeps
                if real_yeeps.is_world_table(table_name):
                    _r = await real_yeeps.get_item(table_name, dynamo_key)
                    _ri = (_r or {}).get("Item")
                    if _ri:
                        items.append(_ri if not attrs_to_get
                                     else {a: _ri[a] for a in attrs_to_get if a in _ri})
                        continue
            doc = None
            if table_name == "G2_Maps":
                doc = await _map_doc(query.get("mapKey", ""))
            elif collection is not None:
                doc = await collection.find_one(query, {"_id": 0})

            # Fallback logic
            if doc is None:
                doc = await _get_fallback_doc(table_name, query)

            # Unknown account (community-world builder): real name from Yeeps,
            # same as the single GetItem path above and 1.31's _live_account.
            if doc is None and table_name == "G2_Accounts":
                from services.account_resolve import live_account
                doc = await live_account(query.get("accountID"))

            if table_name == "G2_Globals" and _is_private_global(query.get("key")):
                doc = None
            # Build item
            if doc:
                if table_name == "G2_Accounts" and isinstance(doc, dict):
                    doc = public_view(_role_locked_view(doc))
                item = wrap_item(doc, attrs_to_get if attrs_to_get else None)
            elif table_name in ["G2_Rooms", "G2_Maps", "G2_Accounts", "G2_Bundles", "G2_Globals"]:
                # If sensitive table and no doc, provide an empty item with NULLs (to be filled below)
                item = {a: {"NULL": True} for a in attrs_to_get}
            else:
                # For other tables, skip missing items (standard DynamoDB behavior)
                continue

            # Specialized logic (MinVersion, etc)
            if table_name == "G2_Bundles" and doc and isinstance(doc, dict):
                if doc.get("bundleKey") == "bundle_commando":
                    item["endTimestamp"] = {"S": "2025 02/20 07:00:00"}
                    item["minVersion"] = {"S": "1.0.0"}
                elif "minVersion" in doc:
                    item["minVersion"] = to_dynamo(doc["minVersion"])
            
            # Inject defaults for sensitive tables
            if table_name in ["G2_Rooms", "G2_Maps", "G2_Accounts", "G2_Bundles", "G2_Globals", "G2_AccountStats"] and item:
                for attr in list(item.keys()):
                    # Special case for accounts
                    if table_name == "G2_Accounts" and attr == "shouldForceRefresh":
                        item[attr] = {"N": "0"}
                        continue
                    
                    # General default injection
                    if item[attr] == {"NULL": True}:
                        item[attr] = to_dynamo(default_for_property(attr))
                            
            items.append(item)

        responses[table_name] = items

    return _make_response({
        "Responses": responses,
        "UnprocessedKeys": {},
    })

async def _record_and_force_presence_version(table_name: str, doc: dict) -> None:
    """Learn each account's REAL build from its presence write, then force the
    shared presence version to the current build.

    The game's login call carries NO client version, so we can't gate there
    directly. But the client DOES report its version in the G2_PlayerStatuses
    presence write (and the attestation heartbeat). We persist that real value
    onto the account as `lastClientVersion`; login.py then isolates any account
    whose last-known build is outdated — which fires on the next relaunch /
    re-login. We also rewrite the shared presence version to
    REQUIRED_CLIENT_VERSION so up-to-date clients match for the in-room join
    check while older clients ("1.19.0") don't.
    """
    if "G2_PlayerStatuses" not in (table_name or "") or "applicationVersion" not in doc:
        return
    real = str(doc.get("applicationVersion") or "")
    acct = doc.get("accountID")
    if acct and real:
        try:
            from services.db import accounts
            await accounts.update_one({"accountID": acct},
                                      {"$set": {"lastClientVersion": real}})
        except Exception as e:
            print(f"[ver] lastClientVersion persist failed for {acct}: {e}")
    doc["applicationVersion"] = REQUIRED_CLIENT_VERSION


# Account fields a CLIENT may never write via the DynamoDB API. These are
# moderation/security/role fields the SERVER owns: bans are applied only through
# g2_moderatorBanAccount (gated to MOD_STAFF) and the editor, which write to
# Mongo directly (NOT through these handlers), so stripping them here blocks a
# modded client from banning anyone (or clearing its own ban, or granting itself
# roles) by writing straight to G2_Accounts — without affecting legit bans or a
# client's normal writes (currency, cosmetics, settings, ...).
PROTECTED_ACCOUNT_FIELDS = {
    "adminBlocked", "isPermabanned", "isMutebanned", "isIsolated",
    "banExpireTimestamp", "banReason", "blockReason", "banCategory",
    "banCount", "remainingBanHours", "pendingBanDuration", "longestBanDuration",
    "shadowBanned", "hasPendingWarning", "warnReason", "warnCategory",
    "warnCount", "shouldForceRefresh", "roleKeys", "attestHash",
    "pendingKick", "kickReason",
    "clientBuildTag", "clientBuildStatus", "clientBuildSeenAt",
}


def _strip_protected_account_fields(table_name: str, fields: dict, who: str = "") -> None:
    """Drop server-owned moderation/security fields from a CLIENT account write."""
    if table_name != "G2_Accounts" or not isinstance(fields, dict):
        return
    hit = [k for k in list(fields.keys()) if k in PROTECTED_ACCOUNT_FIELDS or k in PRIVATE_ACCOUNT_FIELDS]
    for k in hit:
        fields.pop(k, None)
    if hit:
        print(f"[guard] stripped protected account fields from client write "
              f"to {who or '?'}: {hit}")


def _take_owned_patterns(table_name: str, fields: dict) -> list:
    """Clients must never SHRINK a player's owned blocks (everyone owns all of
    them; a client write of its own, shorter list used to replace the stored
    one). Pull ownedPatterns out of a client account write; the caller only
    ADDS those keys ($addToSet). Role-locked keys are never added by a client."""
    if table_name != "G2_Accounts" or not isinstance(fields, dict) or "ownedPatterns" not in fields:
        return []
    from services.role_locks import ROLE_LOCKED_PATTERNS
    owned = fields.pop("ownedPatterns") or []
    return [k for k in owned if isinstance(k, str) and k not in ROLE_LOCKED_PATTERNS]


async def handle_update_item(body: dict) -> tuple[str, dict]:
    """Handle DynamoDB UpdateItem operation."""
    table_name = body.get("TableName", "")
    key = body.get("Key", {})
    updates = body.get("AttributeUpdates", {})

    collection = get_collection(table_name)
    if collection is not None:
        query = unwrap_key(key)
        set_fields = unwrap_attribute_updates(updates)
        # Block ban/role/security field writes from clients (ban-bypass guard).
        _strip_protected_account_fields(table_name, set_fields, query.get("accountID", ""))
        if table_name == "G2_Accounts" and "activeCosmetics" in set_fields:
            from services.hair_colors import clamp_active_cosmetics
            set_fields["activeCosmetics"] = clamp_active_cosmetics(set_fields["activeCosmetics"])
        add_owned = _take_owned_patterns(table_name, set_fields)
        if add_owned and query:
            await collection.update_one(query, {"$addToSet": {"ownedPatterns": {"$each": add_owned}}})
        if set_fields:
            # Merge key fields (accountID lives in the Key, not the updates) in
            # first so the version recorder can see whose presence this is.
            set_fields.update(query)
            await _record_and_force_presence_version(table_name, set_fields)
            await collection.update_one(
                query,
                {"$set": set_fields},
                upsert=True,
            )

    return _make_response({})


async def handle_put_item(body: dict) -> tuple[str, dict]:
    """Handle DynamoDB PutItem operation."""
    table_name = body.get("TableName", "")
    item = body.get("Item", {})

    collection = get_collection(table_name)
    if collection is not None and item:
        # Convert DynamoDB-typed Item to plain doc
        doc = {}
        for k, v in item.items():
            doc[k] = from_dynamo(v)

        # Block ban/role/security field writes from clients (ban-bypass guard).
        # On PutItem this also means a banned client can't overwrite its own
        # account to clear adminBlocked — the stripped field keeps its DB value.
        _strip_protected_account_fields(table_name, doc, doc.get("accountID", ""))
        if table_name == "G2_Accounts" and "activeCosmetics" in doc:
            from services.hair_colors import clamp_active_cosmetics
            doc["activeCosmetics"] = clamp_active_cosmetics(doc["activeCosmetics"])
        add_owned = _take_owned_patterns(table_name, doc)
        if add_owned and doc.get("accountID"):
            await collection.update_one({"accountID": doc["accountID"]},
                                        {"$addToSet": {"ownedPatterns": {"$each": add_owned}}})

        await _record_and_force_presence_version(table_name, doc)

        key_field = get_key_field(table_name)
        if key_field and key_field in doc:
            await collection.update_one(
                {key_field: doc[key_field]},
                {"$set": doc},
                upsert=True,
            )
        else:
            await collection.insert_one(doc)

    return _make_response({})


async def handle_delete_item(body: dict) -> tuple[str, dict]:
    """Handle DynamoDB DeleteItem operation."""
    table_name = body.get("TableName", "")
    key = body.get("Key", {})

    # A client must never delete an account via the DynamoDB API (a modded client
    # could wipe other players). The editor deletes via Mongo directly, so it is
    # unaffected.
    if table_name == "G2_Accounts":
        print(f"[guard] blocked client DeleteItem on G2_Accounts: {unwrap_key(key)}")
        return _make_response({})

    collection = get_collection(table_name)
    if collection is not None:
        query = unwrap_key(key)
        await collection.delete_one(query)

    return _make_response({})


_QUERY_PASS = ("KeyConditionExpression", "FilterExpression", "ExpressionAttributeValues",
               "ExpressionAttributeNames", "IndexName", "Limit", "ScanIndexForward",
               "ExclusiveStartKey", "ProjectionExpression", "Select", "ConsistentRead")


async def handle_query(body: dict) -> tuple[str, dict]:
    """Handle DynamoDB Query. Locally a stub; play clients get real Yeeps data."""
    from services import big_patterns as _bp
    table = body.get("TableName", "")
    if _bp.is_play(body.get("_clientBuildTag")):
        from services import real_yeeps
        if real_yeeps.is_world_table(table):
            params = {k: body[k] for k in _QUERY_PASS if k in body}
            r = await real_yeeps.query(table, **params)
            if r is not None:
                items = r.get("Items", [])
                return _make_response({"Items": items, "Count": r.get("Count", len(items)),
                                       "ScannedCount": r.get("ScannedCount", len(items))})
    return _make_response({"Items": [], "Count": 0, "ScannedCount": 0})


async def handle_scan(body: dict) -> tuple[str, dict]:
    """Handle DynamoDB Scan operation (stub — returns empty)."""
    return _make_response({"Items": [], "Count": 0, "ScannedCount": 0})


async def handle_describe_table(body: dict) -> tuple[str, dict]:
    """Handle DynamoDB DescribeTable operation."""
    return _make_response({
        "Table": {
            "TableName": body.get("TableName", "Unknown"),
            "TableStatus": "ACTIVE",
            "ItemCount": 0,
        }
    })


# Operation dispatcher
OPERATION_HANDLERS = {
    "GetItem":       handle_get_item,
    "BatchGetItem":  handle_batch_get_item,
    "UpdateItem":    handle_update_item,
    "PutItem":       handle_put_item,
    "DeleteItem":    handle_delete_item,
    "Query":         handle_query,
    "Scan":          handle_scan,
    "DescribeTable": handle_describe_table,
}


# The only DynamoDB write the real game makes (measured on a normal day, Oct 8:
# 25k UpdateItem on G2_PlayerStatuses and nothing else). Every other write or
# delete through this unauthenticated API came from raiders (Oct 9: deleted
# rooms, maps, room maps, globals, bundles). Those are acknowledged but ignored.
ALLOWED_CLIENT_WRITES = {("UpdateItem", "G2_PlayerStatuses")}
_WRITE_OPS = {"UpdateItem", "PutItem", "DeleteItem", "BatchWriteItem", "TransactWriteItems"}


def _write_tables(op: str, body: dict) -> list:
    if op in ("BatchWriteItem",):
        return list((body.get("RequestItems") or {}).keys()) or ["?"]
    if op in ("TransactWriteItems",):
        return ["?"]
    return [body.get("TableName", "") or "?"]


async def dispatch_dynamo(target: str, body: dict, client_ip: str = "", device_key: str = "") -> tuple[str, dict]:
    """
    Dispatch a DynamoDB operation by X-Amz-Target.
    Returns (body_text, headers).
    """
    op = (target or "").rsplit(".", 1)[-1]
    if op in _WRITE_OPS:
        tables = _write_tables(op, body if isinstance(body, dict) else {})
        if not all((op, t) in ALLOWED_CLIENT_WRITES for t in tables):
            try:
                key = json.dumps(body.get("Key") or {k: body.get("Item", {}).get(k) for k in list(body.get("Item") or {})[:2]})[:200]
            except Exception:
                key = "?"
            print(f"[guard] BLOCKED client {op} on {','.join(tables)} from {client_ip or '?'}: {key}")
            return _make_response({})
        # The allowed write (a player's own presence) must come from that player's headset.
        from services import device_auth
        _acct = unwrap_key(body.get("Key") or {}).get("accountID", "") if isinstance(body, dict) else ""
        _ok, _ = await device_auth.check_request(_acct, device_key, f"{op} {tables[0]}", client_ip)
        if not _ok:
            return _make_response({})
    handler = OPERATION_HANDLERS.get(op)
    if isinstance(body, dict):
        body["_deviceKey"] = device_key   # map builders are recognized by headset (creative_builders)

    if handler:
        return await handler(body)

    # Unknown operation — return empty
    return _make_response({})
