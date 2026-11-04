"""
Misc Lambda handlers:
  - g2_fetchFeaturedWorldList
  - g2_fetchPopularWorldList
  - g2_logDebug
  - g2_refreshAccountOnline
  - g2_tryRedeemCurrencyStash
"""

import json
import os
from datetime import datetime, timezone, timedelta

from services.db import accounts, debug_logs, globals_col, player_statuses
from services.property_defaults import default_for_property
from services.build_gate import current_status, outdated_message
from services.kick import DEFAULT_KICK_REASON


async def handle_fetch_account_datas(body: dict) -> dict:
    """
    g2_fetchAccountDatas: fetch properties for a LIST of target accounts. The
    client sends {targetAccountIDs:[...], propertiesToGet:[...]} and reads the
    result keyed by accountID (result[accountID][prop]). Returning it flat made
    AccountOnlineManager.DoRefresh log "Failed to check for override refresh:
    NO_DATA_FOUND" (it couldn't find result[accountID]) and the account never
    went online -> the natural Photon join never fired. Return it keyed per
    account, from Mongo where available, defaults otherwise.
    """
    from config import IS_V2 as _V2
    if _V2 and os.getenv("DIAG_DYNAMO") == "1":
        print(f"[DIAG] fetchAccountDatas body: {json.dumps(body)[:600]}")
    target_ids = body.get("targetAccountIDs") or (
        [body["accountID"]] if body.get("accountID") else []
    )
    props = body.get("propertiesToGet", [])
    out = {}
    flat = {}
    for tid in target_ids:
        from services.account_privacy import public_view
        doc = public_view(await accounts.find_one({"accountID": tid}, {"_id": 0}) or {})
        if not doc and _V2:
            # Unknown account (e.g. a community-world builder): resolve the real
            # name straight from Yeeps (fetchAccountDatas, same as 1.31's
            # _live_account), not a placeholder. Fall back to a neutral
            # placeholder ONLY if the live lookup fails, so a genuinely
            # unresolvable id never regresses to NO_DATA_FOUND.
            from services.account_resolve import live_account
            live = await live_account(tid)
            if live:
                doc = {"accountID": tid, "displayName": live.get("displayName") or "",
                       "activeCosmetics": live.get("activeCosmetics") or [],
                       "skinColor": live.get("skinColor", -3489025),
                       "eyeColor": live.get("eyeColor", -2039846657)}
            else:
                doc = {"accountID": tid, "displayName": "Yeeps", "activeCosmetics": [],
                       "skinColor": -3489025, "eyeColor": -2039846657}
        if "ownedPatterns" in doc or "roleKeys" in doc:
            from services.dynamo_ops import _role_locked_view
            doc = _role_locked_view(doc)
        entry = {p: (doc[p] if p in doc else default_for_property(p)) for p in props}
        out[tid] = entry
        if not flat:  # first target's props also exposed flat (single-account callers)
            flat = dict(entry)
    # Return BOTH shapes: keyed-by-accountID (out) AND the props flat at data
    # level, so however the client reads it (result[accountID][p] OR result[p])
    # it finds the value and the override-refresh check doesn't hit NO_DATA_FOUND.
    from config import IS_V2
    if IS_V2:
        # 2.x batch reads take a LIST of objects carrying their own key (same
        # as g2_fetchRoomMapExtraProperties); the keyed dict gives
        # "AccountOnlineManager: Failed to check for override refresh: NO_DATA_FOUND".
        # Values are read as DynamoDB attributes (like the room-map batch).
        from services.dynamo_ops import to_dynamo
        # Reply shape is switchable live (logs/acctfmt.txt) for experiments.
        # Default R1 = {"data": {"responses": {accountID: {prop: plain}}}}:
        # found 2026-10-09 via the 240b20 [jsonmiss] hook (client read
        # data["responses"]); override-refresh errors went 6/6 -> 0. Every
        # other shape (A..Q, R2 list) gave NO_DATA_FOUND.
        try:
            fmt = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs", "acctfmt.txt")).read().strip()
        except OSError:
            fmt = "R1"
        typed = {tid: {k: to_dynamo(v) for k, v in out[tid].items() if v not in ("", None)} for tid in out}
        plain = {tid: {k: v for k, v in out[tid].items() if v is not None} for tid in out}
        if fmt == "B":   # keyed, typed
            return {"data": typed}
        if fmt == "C":   # keyed, plain
            return {"data": plain}
        if fmt in ("L", "M", "Q"):   # typed, lists as string sets, flags as BOOL
            def tv(k, v):
                if isinstance(v, list) and all(isinstance(x, str) for x in v):
                    return {"SS": v} if v else {"L": []}
                if isinstance(v, (bool, int)) and k.startswith(("should", "is", "has")):
                    return {"BOOL": bool(v)}
                return to_dynamo(v)
            t2 = {t: {k: tv(k, v) for k, v in d.items() if v not in ("", None)} for t, d in plain.items()}
            if fmt == "M":
                return {"data": t2}
            lst = [{"accountID": {"S": t}, **t2[t]} for t in t2]
            return {"data": json.dumps(lst)} if fmt == "Q" else {"data": lst}
        if fmt == "P":   # keyed plain, but data as a JSON string
            return {"data": json.dumps(plain)}
        if fmt == "R1":  # "responses": keyed by accountID, plain values
            return {"data": {"responses": plain}}
        if fmt == "R2":  # "responses": list of plain objects carrying accountID
            return {"data": {"responses": [{"accountID": t, **plain[t]} for t in plain]}}
        if fmt == "R3":  # "responses": list of DynamoDB-typed items
            return {"data": {"responses": [{"accountID": {"S": t}, **typed[t]} for t in typed]}}
        if fmt == "J":   # keyed plain, booleans as JSON true/false
            return {"data": {t: {k: (bool(v) if k.startswith(("should", "is", "has")) and isinstance(v, int) else v)
                                 for k, v in d.items()} for t, d in plain.items()}}
        if fmt == "K":   # keyed plain, every value as a string
            return {"data": {t: {k: (v if isinstance(v, str) else json.dumps(v)) for k, v in d.items()}
                             for t, d in plain.items()}}
        if fmt == "H":   # keyed, plain + first target's props flat (1.31 shape)
            first = next(iter(plain.values()), {})
            return {"data": {**plain, **first}}
        if fmt == "I":   # keyed plain for batches, flat plain for single self-reads
            if len(plain) == 1 and body.get("accountID") in plain:
                return {"data": dict(plain[body["accountID"]])}
            return {"data": plain}
        if fmt == "D":   # list, plain
            return {"data": [{"accountID": t, **plain[t]} for t in plain]}
        if fmt == "E":   # BatchGetItem-style
            return {"data": {"Responses": {"G2_Accounts": [{"accountID": {"S": t}, **typed[t]} for t in typed]}}}
        if fmt == "F":   # list under "accounts", plain
            return {"data": {"accounts": [{"accountID": t, **plain[t]} for t in plain]}}
        if fmt == "G":   # list, plain, values as JSON strings
            return {"data": [{"accountID": t, **{k: (v if isinstance(v, str) else json.dumps(v)) for k, v in plain[t].items()}} for t in plain]}
        return {"data": [{"accountID": {"S": t}, **typed[t]} for t in typed]}
    return {"data": {**out, **flat}}


# Real-Yeeps community-world lists, mirrored read-only through the relay's
# /worlds endpoint (same service account as community-world mirroring). Cached
# a few minutes so the CW browser is populated and worlds are enterable (entry
# itself is mirrored live by services/dynamo_ops.py's cw handling). Empty list
# on any failure so the browser degrades gracefully.
_WORLD_LIST_TTL = 600       # serve a good list this long without re-fetching
_WORLD_LIST_RETRY = 45      # after an empty/failed fetch, wait this long before retrying
_world_list_cache: dict = {}    # ck -> (fresh_until, out)
_world_list_last_good: dict = {}  # ck -> out (most recent non-empty, kept indefinitely)
_world_list_next_try: dict = {}   # ck -> earliest time to retry after a failure


async def _world_list(list_kind: str, category: str = "") -> dict:
    import time as _t
    ck = f"{list_kind}:{category}"
    now = _t.time()
    hit = _world_list_cache.get(ck)
    if hit and hit[0] > now:
        return hit[1]
    # Real Yeeps rate-limits frequent callers, so don't hammer it: if we just
    # failed, keep serving the last good list until the retry window passes.
    if _world_list_next_try.get(ck, 0) > now and ck in _world_list_last_good:
        return _world_list_last_good[ck]
    out = None
    try:
        from services.dynamo_ops import _cw_post, _cw_account
        oc, code = await _cw_account()
        if oc:
            payload = {"oculusID": oc, "mobileCode": code, "list": list_kind}
            if category:
                payload["category"] = category
            data = await _cw_post("/worlds", payload)
            if data.get("ok") and (data.get("worlds") or []):
                out = {"worlds": data["worlds"], "expireTime": data.get("expireTime")}
    except Exception as e:
        print(f"[worlds] {list_kind} fetch failed: {e}")
    if out:
        _world_list_cache[ck] = (now + _WORLD_LIST_TTL, out)
        _world_list_last_good[ck] = out
        _world_list_next_try.pop(ck, None)
        try:   # persist so it survives restarts + real-Yeeps lockouts
            await globals_col.update_one({"key": f"cw_world_list_{ck}"},
                                         {"$set": {"key": f"cw_world_list_{ck}", "value": out}}, upsert=True)
        except Exception:
            pass
        print(f"[worlds] {list_kind}{'/' + category if category else ''}: {len(out['worlds'])} worlds")
        return out
    # Fetch came back empty/failed -> serve the last good list (in-memory, else
    # the persisted one from the DB), and back off before trying Yeeps again.
    _world_list_next_try[ck] = now + _WORLD_LIST_RETRY
    prev = _world_list_last_good.get(ck)
    if not prev:
        try:
            row = await globals_col.find_one({"key": f"cw_world_list_{ck}"}, {"_id": 0, "value": 1})
            prev = (row or {}).get("value")
            if prev:
                _world_list_last_good[ck] = prev
        except Exception:
            prev = None
    print(f"[worlds] {list_kind}: empty/failed -> serving {len(prev['worlds']) if prev else 0} cached")
    return prev or {"worlds": [], "expireTime": None}


async def handle_fetch_featured_world_list(body: dict) -> dict:
    return await _world_list("featured", (body.get("category") or "parkour"))


async def handle_fetch_popular_world_list(body: dict) -> dict:
    return await _world_list("popular")


async def handle_fetch_random_world_list(body: dict) -> dict:
    return await _world_list("random")


def _debug_full_log(ip: str, max_lines: int = 5000) -> list:
    """This player's recent lines from the live devlog (the APK streams every Unity log line,
    warning, error and stack trace there): the newest ~60 MB, filtered by their IP."""
    import os as _o
    path = _o.path.join(_o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))), "logs", "devlog.log")
    if not ip:
        return []
    tag = f" {ip} "
    out = []
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 60_000_000))
            f.readline()
            for raw in f:
                if tag.encode() in raw:
                    out.append(raw.decode("utf-8", "replace").rstrip("\n"))
                    if len(out) > max_lines * 2:
                        out = out[-max_lines:]
    except FileNotFoundError:
        return []
    return out[-max_lines:]


async def handle_log_debug(body: dict) -> dict:
    """Record what the client reports so it can be read back from /editor. Owner
    2026-10-18 ("make the debug button send WAY more logs"): every press also saves the
    player's full recent devlog (up to 5000 lines) as logs/debug_reports/<file>.txt."""
    entry = {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "payload": body,
    }
    try:
        import asyncio as _aio, os as _o, re as _re
        ip = str(body.get("_clientIP") or "")
        acct = str(body.get("accountID") or "")
        lines = await _aio.to_thread(_debug_full_log, ip)
        if lines:
            d = _o.path.join(_o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))), "logs", "debug_reports")
            _o.makedirs(d, exist_ok=True)
            name = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "_" + _re.sub(r"[^A-Za-z0-9_]", "", acct)[:40] + ".txt"
            build = str(body.get("_clientBuildTag") or "").split(".")[0]
            head = (f"Debug report  account {acct}  build {build}  ip {ip}  at {entry['at']} UTC\n"
                    f"=== game's own log ===\n{body.get('logs') or ''}\n\n=== full devlog ({len(lines)} lines) ===\n")
            with open(_o.path.join(d, name), "w") as fh:
                fh.write(head + "\n".join(lines) + "\n")
            entry["reportFile"] = name
            entry["reportLines"] = len(lines)
            entry["reportErrors"] = sum(1 for l in lines if "[Error]" in l or "[Exception]" in l or "Exception" in l)
            # keep the newest 300 report files
            files = sorted(f for f in _o.listdir(d) if f.endswith(".txt"))
            for f in files[:-300]:
                _o.remove(_o.path.join(d, f))
    except Exception as e:
        print(f"[debug] full log attach failed: {e}")
    print(f"[debug] {json.dumps(body)[:900]}")
    try:
        await debug_logs.insert_one(dict(entry))
        # keep only the most recent 200 entries
        count = await debug_logs.count_documents({})
        if count > 200:
            old = debug_logs.find({}, {"_id": 1}).sort("_id", 1).limit(count - 200)
            await debug_logs.delete_many({"_id": {"$in": [d["_id"] async for d in old]}})
    except Exception as e:
        print(f"[debug] could not store entry: {e}")
    try:
        from services.client_errors import ingest_background
        ingest_background(body.get("accountID", ""), body.get("logs", ""),
                          str(body.get("clientVersion") or ""))
    except Exception as e:
        print(f"[debug] error tracking failed: {e}")
    return {"success": 1}


async def handle_fetch_global_fatal_error(body: dict) -> dict:
    """Serve a global fatal error if one is set in the globals collection.

    Set it with a globals row {"key": "fatalError", "data": "<message>"};
    clear the row (or leave data empty) to turn it off.
    """
    from services import build_gate as _bg
    _aid = str(body.get("accountID") or "")
    _ip = str(body.get("_clientIP") or "")
    print(f"[fatal_error] asked by acct={_aid or '-'} ip={_ip or '-'} keys={sorted(k for k in body if not k.startswith('_'))}")
    if _bg.was_link_blocked(_aid, _ip):
        print(f"[fatal_error] serving /link message to {_aid or _ip}")
        msg = _bg.LINK_REQUIRED_MESSAGE
        return {"success": 1, "errorMessage": msg, "fatalError": msg, "message": msg}
    try:
        row = await globals_col.find_one({"key": "fatalError"}, {"_id": 0})
    except Exception:
        row = None
    message = (row or {}).get("data") or ""
    if message:
        print(f"[fatal_error] serving: {message}")
        return {"success": 1, "errorMessage": message, "fatalError": message}
    return {"success": 1}


async def handle_refresh_account_online(body: dict) -> dict:
    """refreshAccountOnline: the 1.31 client's periodic heartbeat, and also what
    it calls when shouldForceRefresh is set ("Doing override refresh"). It reads
    isPermabanned / banReason / remainingBanHours from this response, so this is
    how a kick or ban lands MID-SESSION (1.31 does not re-login on a refresh).

      pendingKick  -> one-shot boot with kickReason, then cleared
      adminBlocked -> ban (editor/staff), real remaining hours for timed bans
      outdated     -> trueVersion != REQUIRED_CLIENT_VERSION
    Consumes any pending shouldForceRefresh.
    """
    from config import REQUIRED_CLIENT_VERSION
    from services.build_gate import enforced as _build_enforced
    resp = {"success": 1, "isPermabanned": 0, "banReason": "", "remainingBanHours": 0}
    from config import IS_V2 as _V2R
    if _V2R:
        # 2.4 also reads these (devlog [jsonmiss] showed them missing)
        resp.update(warnReason="", isMutebanned=0, hasPendingWarning=0)
    account_id = body.get("accountID", "")
    if not account_id:
        return resp
    doc = await accounts.find_one(
        {"accountID": account_id},
        {"_id": 0, "pendingKick": 1, "kickReason": 1, "adminBlocked": 1, "banReason": 1,
         "blockReason": 1, "banExpireTimestamp": 1, "shouldForceRefresh": 1,
         "clientBuildStatus": 1, "clientBuildTag": 1}) or {}
    ver = str(body.get("trueVersion") or "").strip()
    if doc.get("pendingKick"):
        # isPermabanned=1 always reads as "permanently banned" on the client;
        # a kick is a timed boot, so send 0 + hours.
        resp.update(isPermabanned=0, remainingBanHours=1,
                    banReason=doc.get("kickReason") or DEFAULT_KICK_REASON)
        await accounts.update_one({"accountID": account_id},
                                  {"$set": {"pendingKick": 0, "kickReason": ""}})
        print(f"[kick] delivered to {account_id} via refreshAccountOnline")
    elif doc.get("adminBlocked"):
        hours = 99999
        exp_ts = doc.get("banExpireTimestamp") or ""
        if exp_ts:
            try:
                exp = datetime.strptime(exp_ts, "%Y %m/%d %H:%M:%S").replace(tzinfo=timezone.utc)
                secs = (exp - datetime.now(timezone.utc)).total_seconds()
                hours = max(1, -(-int(secs) // 3600)) if secs > 0 else 0
            except Exception:
                hours = 99999
        if hours:
            # Permanent only when there is no expiry; timed bans show hours.
            resp.update(isPermabanned=1 if hours == 99999 else 0, remainingBanHours=hours,
                        banReason=doc.get("banReason") or doc.get("blockReason") or "You are banned.")
    else:
        # Big / live-base clients run their own version track (2.4.0 / 2.31.4),
        # gated at login by big_outdated, NOT by trueVersion vs
        # REQUIRED_CLIENT_VERSION. Without this skip, a live client reporting
        # trueVersion "2.31.4" is told "Outdated version" right after it loads
        # the spawn room and the game fatals out.
        from services import big_patterns as _bp
        _tag = doc.get("clientBuildTag") or body.get("_clientBuildTag") or ""
        if not _bp.is_big(_tag) and (
                (ver and ver != REQUIRED_CLIENT_VERSION) or (
                    current_status(doc) == "outdated" and _build_enforced())):
            resp.update(isPermabanned=0, remainingBanHours=1,
                        banReason=outdated_message())
    if doc.get("shouldForceRefresh"):
        try:
            from services.dynamo_ops import _clear_force_refresh
            await _clear_force_refresh(account_id)
        except Exception as e:
            print(f"[refresh] clear force-refresh failed for {account_id}: {e}")
    return resp


async def handle_try_redeem_currency_stash(body: dict) -> dict:
    return {
        "success": 1,
        "newCurrency": 0,
        "currencyAmount": 0,
    }


async def handle_put_player_status(body: dict) -> dict:
    """g2_putPlayerStatus: record a player's live presence so the Discord
    /online list (and the server's online/kick logic) see 2.31.4 players.
    The client posts this ~every `lifetime` seconds with its current room;
    isClear=1 on leave. Row shape matches the 1.31/2.4 presence rows
    ("%Y %m/%d %H:%M:%S" UTC)."""
    acct = body.get("accountID")
    if not acct:
        return {"success": 0}
    now = datetime.now(timezone.utc)
    life = max(15, min(int(body.get("lifetime") or 45), 600)) + 30
    exp = now if body.get("isClear") else now + timedelta(seconds=life)
    doc = {k: body.get(k) for k in ("roomKey", "roomMapKey", "roomMapCode", "photonRoomName",
                                    "privateRoomPermissions", "applicationVersion", "type", "gameSessionID")}
    doc["expireTimestamp"] = exp.strftime("%Y %m/%d %H:%M:%S")
    try:
        await player_statuses.update_one({"accountID": acct},
                                         {"$set": {"accountID": acct, **doc}}, upsert=True)
    except Exception as e:
        print(f"[status] put failed for {acct}: {e}")
        return {"success": 0}
    return {"success": 1}


async def handle_try_purchase_customized_item(body: dict) -> dict:
    """SAVE a reskin (an item decorated with charms). The client sends
    customItemKey (base item) + customItemConfig ("ver|owner|name|...|charms").
    We assign the real-Yeeps-style fullKey owner$<index>, store it in the global
    customized_items collection (immutable config), add it to the owner's
    customizedItemDatas, and return the key so it can be placed/shared. Was
    mis-routed to handle_purchase (substring 'trypurchase'), which stored NOTHING
    -> reskins didn't save. Can't write to real Yeeps as the player, so new
    reskins live in our DB; existing real-Yeeps ones load via the fetch handler."""
    from services.db import db as _dbh
    acct = str(body.get("accountID") or "")
    key = str(body.get("customItemKey") or "")
    cfg = str(body.get("customItemConfig") or "")
    if not acct or not key:
        return {"success": 0}
    coll = _dbh["customized_items"]
    # Buying SOMEONE ELSE'S shared reskin: the config's owner field (2nd "|" part) is
    # its creator. Real Yeeps gives the buyer the creator's existing reskin (their
    # fullKey); minting a new key under the buyer with the creator's config made a copy
    # the game never showed in the buyer's imagination (owner 2026-10-18 test).
    parts = cfg.split("|")
    creator = parts[1] if len(parts) > 1 else acct
    if creator and creator != acct:
        orig = await coll.find_one({"owner": creator, "itemKey": key, "itemConfig": cfg}, {"_id": 0, "fullKey": 1})
        if orig and orig.get("fullKey"):
            full = orig["fullKey"]
            await accounts.update_one({"accountID": acct}, {"$addToSet": {"customizedItemDatas": full}})
            print(f"[reskin] {acct} bought shared {full} itemKey={key}")
            return {"success": 1, "cost": 0, "currencyValueIndex": 0, "newCurrency": 999999999,
                    "customItemFullKey": full, "fullKey": full, "fullKeys": [full]}
    # Next free index = highest existing suffix + 1, NOT count_documents.
    # count was unsafe: read-through-cached real-Yeeps reskins (and prior saves)
    # leave NON-CONTIGUOUS indices with gaps, so count landed on an index that
    # already existed -> the upsert OVERWROTE it, count never grew, and every new
    # reskin kept colliding on the same key and clobbering the last one. That was
    # the "make a reskin, rejoin, it's gone" bug (e.g. o_..$41 saved twice).
    mx = -1
    async for _d in coll.find({"owner": acct}, {"fullKey": 1, "_id": 0}):
        _sfx = (_d.get("fullKey") or "").split("$", 1)
        if len(_sfx) == 2 and _sfx[1].isdigit():
            mx = max(mx, int(_sfx[1]))
    n = mx + 1
    full = f"{acct}${n}"
    # belt-and-suspenders: never reuse a key that somehow still exists
    while await coll.find_one({"fullKey": full}, {"_id": 0, "fullKey": 1}):
        n += 1
        full = f"{acct}${n}"
    await coll.update_one({"fullKey": full},
                          {"$set": {"fullKey": full, "owner": acct, "itemKey": key,
                                    "itemConfig": cfg, "savedAt": datetime.now(timezone.utc).strftime("%Y %m/%d %H:%M:%S")}},
                          upsert=True)
    await accounts.update_one({"accountID": acct}, {"$addToSet": {"customizedItemDatas": full}})
    print(f"[reskin] saved {full} itemKey={key} cfglen={len(cfg)}")
    # mirror the generic purchase success shape + hand back the assigned key
    return {"success": 1, "cost": 0, "currencyValueIndex": 0, "newCurrency": 999999999,
            "customItemFullKey": full, "fullKey": full, "fullKeys": [full]}


_RESKIN_MISS: dict = {}            # fullKey -> ts of last real-Yeeps miss (don't re-hammer)


async def handle_fetch_customized_items(body: dict) -> dict:
    """LOAD reskins by fullKey (owner$index). Local store first; any still missing
    are read-through from real Yeeps (existing reskins made there) and cached
    locally so the service accounts are not hammered. Returns real Yeeps' exact
    shape: {"data": {"responses": [{fullKey, itemKey, itemConfig}, ...]}}. Was
    unhandled (generic fallback returned {} -> reskins never rendered)."""
    import time as _t
    from services.db import db as _dbh
    keys = [k for k in (body.get("customItemFullKeys") or []) if isinstance(k, str)]
    coll = _dbh["customized_items"]
    out, missing = [], []
    local = {}
    if keys:
        async for d in coll.find({"fullKey": {"$in": keys}}, {"_id": 0}):
            local[d["fullKey"]] = d
    now = _t.time()
    for k in keys:
        if k in local:
            out.append({"fullKey": k, "itemKey": local[k].get("itemKey", ""),
                        "itemConfig": local[k].get("itemConfig", "")})
        elif _RESKIN_MISS.get(k, 0) < now - 300:      # not a recently-confirmed miss
            missing.append(k)
    if missing:
        try:
            from services.dynamo_ops import _cw_direct_account
            from services import yeeps_direct
            real = await yeeps_direct.invoke("fetchCustomizedItems",
                                             {"customItemFullKeys": missing},
                                             account=_cw_direct_account(""))
            got = {it.get("fullKey"): it for it in ((real or {}).get("responses") or []) if isinstance(it, dict)}
            for k in missing:
                it = got.get(k)
                if it:
                    out.append({"fullKey": k, "itemKey": it.get("itemKey", ""),
                                "itemConfig": it.get("itemConfig", "")})
                    # read-through cache: immutable config, store so we never refetch
                    owner = k.split("$", 1)[0]
                    await coll.update_one({"fullKey": k},
                                          {"$set": {"fullKey": k, "owner": owner,
                                                    "itemKey": it.get("itemKey", ""),
                                                    "itemConfig": it.get("itemConfig", ""), "fromReal": 1}},
                                          upsert=True)
                else:
                    _RESKIN_MISS[k] = now
        except Exception as e:
            print(f"[reskin] real fetchCustomizedItems failed: {type(e).__name__}: {e}")
    return {"data": {"responses": out}}


async def handle_put_favorite_patterns(body: dict) -> dict:
    """g2_putFavoritePatterns: the client's "favorite" and "hide" item actions
    (one call carries BOTH). favoriteItemKeys is a dict {category: [keys]} (the
    default category is "_"); hiddenItemKeys is a flat list. Reskins appear as
    "<itemKey>~<owner>$<n>". Persist to the account so they load next session
    (login serves ownedPatterns_favorite / ownedPatterns_hidden back). Was
    unhandled -> generic fallback stored nothing, so favorites/hiding never
    stuck."""
    acct = str(body.get("accountID") or "")
    if not acct:
        return {"success": 0}
    fav = body.get("favoriteItemKeys")
    hid = body.get("hiddenItemKeys")
    sets = {}
    if isinstance(fav, dict):
        sets["ownedPatterns_favorite"] = fav
    if isinstance(hid, list):
        sets["ownedPatterns_hidden"] = hid
    if sets:
        await accounts.update_one({"accountID": acct}, {"$set": sets})
    nf = sum(len(v) for v in fav.values()) if isinstance(fav, dict) else 0
    print(f"[favorite] {acct} favorites={nf} hidden={len(hid) if isinstance(hid, list) else 0}")
    return {"success": 1}


async def handle_put_toymaker_request(body: dict) -> dict:
    """g2_putToymakerRequest: the RESKIN save. A reskin is an item decorated with
    charms (ItemDecorationManager / CustomItemAccepterMapObject); the client sends
    the made item here. Previously unhandled -> generic fallback returned
    success but stored NOTHING, so reskins never persisted or loaded. We store the
    request on the player's own account (our DB) and serve it back at login /
    fetchAccountDatas, so a biglive-made reskin survives. (Pre-existing reskins
    made on real Yeeps can't be recovered -- Yeeps 400s a service account reading
    another account's toymakerRequest.) The exact payload shape is logged the
    first few times so the serve-back can be matched to the real format."""
    acct = body.get("accountID")
    if not acct:
        return {"success": 0}
    req = body.get("toymakerRequest", "")
    toc = body.get("toymakerTOCAcknowledged")
    # One-time structure capture: the client sends empty strings on a no-op, so
    # only log a NON-empty request (the actual reskin data) to understand it.
    if req not in ("", None, [], {}):
        try:
            print(f"[toymaker] putToymakerRequest {acct}: {json.dumps(req)[:1500]}")
        except Exception:
            print(f"[toymaker] putToymakerRequest {acct}: <unserializable {type(req).__name__}>")
    _set = {"toymakerRequest": req}
    if toc is not None:
        _set["toymakerTOCAcknowledged"] = toc
    try:
        await accounts.update_one({"accountID": acct}, {"$set": _set})
    except Exception as e:
        print(f"[toymaker] put failed for {acct}: {e}")
        return {"success": 0}
    return {"success": 1}


async def handle_fetch_player_statuses(body: dict) -> dict:
    """g2_fetchPlayerStatuses: live presence (which room / photon room / version)
    for a LIST of accounts, so the Friends UI shows who is online and lets you
    JOIN them. Reads the rows handle_put_player_status writes to player_statuses;
    an expired or missing row = offline (empty roomKey). Returned keyed by
    accountID in the 2.x 'responses' shape (same as fetchAccountDatas R1).

    Previously unhandled -> the generic fallback returned ONE flat empty status,
    which the client rejected as "Failed to fetch initial friend statuses", so
    friends never showed online and couldn't be joined ("adding people" broken)."""
    target_ids = body.get("targetAccountIDs") or (
        [body["accountID"]] if body.get("accountID") else [])
    props = body.get("propertiesToGet") or [
        "accountID", "roomKey", "roomMapKey", "roomMapCode", "photonRoomName",
        "privateRoomPermissions", "applicationVersion", "expireTimestamp", "type"]
    now = datetime.now(timezone.utc)
    rows = {}
    if target_ids:
        try:
            async for r in player_statuses.find({"accountID": {"$in": list(target_ids)}}, {"_id": 0}):
                rows[r.get("accountID")] = r
        except Exception as e:
            print(f"[status] fetch failed: {e}")
    out = {}
    for tid in target_ids:
        r = rows.get(tid) or {}
        online = False
        try:
            exp = r.get("expireTimestamp")
            if exp:
                online = datetime.strptime(exp, "%Y %m/%d %H:%M:%S").replace(tzinfo=timezone.utc) > now
        except Exception:
            online = False
        entry = {}
        for p in props:
            if p == "accountID":
                entry[p] = tid
            elif online and r.get(p) is not None:
                entry[p] = r.get(p)     # real live presence
            elif p == "expireTimestamp":
                # offline: an already-expired date string. The generic default was int 0 ->
                # client "Player Status Manager: Failed to parse some data <id>" for EVERY
                # offline friend on every refresh (thousands of log lines per player).
                entry[p] = "2000 01/01 00:00:00"
            elif p == "applicationVersion":
                entry[p] = ""
            elif p in ("privateRoomPermissions", "type"):
                entry[p] = 0            # ints, like the online rows (the generic default was "")
            else:
                entry[p] = default_for_property(p)   # offline / unknown -> empty
        out[tid] = entry
    from config import IS_V2
    if IS_V2:
        plain = {tid: {k: v for k, v in out[tid].items() if v is not None} for tid in out}
        return {"data": {"responses": plain}}
    # 1.x: flat single (callers read result[prop]); keep the first target's row.
    first = next(iter(out.values()), {})
    return {"data": {**first, "responses": out}}
