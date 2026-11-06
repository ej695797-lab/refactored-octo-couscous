"""
Misc Lambda handlers:
  - g2_fetchFeaturedWorldList
  - g2_fetchPopularWorldList
  - g2_logDebug
  - g2_refreshAccountOnline
  - g2_tryRedeemCurrencyStash
"""

import json
from datetime import datetime, timezone

from services.db import accounts, debug_logs, globals_col
from services.property_defaults import default_for_property
from services.build_gate import current_status, outdated_message
from services.kick import DEFAULT_KICK_REASON


def _cw_owner_names() -> dict:
    from services.dynamo_ops import CW_OWNER_NAMES
    return CW_OWNER_NAMES


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
    target_ids = body.get("targetAccountIDs") or (
        [body["accountID"]] if body.get("accountID") else []
    )
    props = body.get("propertiesToGet", [])
    out = {}
    flat = {}
    for tid in target_ids:
        from services.account_privacy import public_view
        doc = public_view(await accounts.find_one({"accountID": tid}, {"_id": 0}) or {})
        if not doc and tid in _cw_owner_names():       # a community-world owner who never played here
            doc = {"accountID": tid, "displayName": _cw_owner_names()[tid]}
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
    return {"data": {**out, **flat}}


_WORLD_LIST_CACHE: dict = {}


async def _world_list(body: dict, which: str) -> dict:
    """Popular / featured community-world lists from the real service, through the relay
    with the cw service account. Cached 5 minutes; an empty list if the relay is down."""
    import time
    from services.yeeps_direct import invoke_standalone
    category = str(body.get("category") or "").strip() if which == "featured" else ""
    key = f"{which}:{category}"
    hit = _WORLD_LIST_CACHE.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    try:
        if which == "featured":
            data = await invoke_standalone("g2_fetchFeaturedWorldList", {"category": category or "parkour"},
                                           with_session=False)
        else:
            data = await invoke_standalone("g2_fetchPopularWorldList", {})
    except Exception as e:
        print(f"[worlds] {which} fetch failed: {e}")
        return {"worlds": []}
    out = {"worlds": data.get("worlds") or []}
    if data.get("expireTime"):
        out["expireTime"] = data["expireTime"]
    _WORLD_LIST_CACHE[key] = (time.time() + 300, out)
    return out


async def handle_fetch_featured_world_list(body: dict) -> dict:
    return await _world_list(body, "featured")


async def handle_fetch_popular_world_list(body: dict) -> dict:
    return await _world_list(body, "popular")


async def handle_log_debug(body: dict) -> dict:
    """Record what the client reports so it can be read back from /editor."""
    entry = {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "payload": body,
    }
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
    # The client only asks for this when it's about to show its fatal-error
    # screen, so log who hit it (build tag, account, the rest of the body).
    _extra = {k: (str(v)[:80]) for k, v in body.items()
              if k not in ("_clientIP", "_clientBuildTag", "_deviceKey", "_fn")}
    print(f"[fatal_error] client hit the error screen: ip={body.get('_clientIP', '')} "
          f"tag={body.get('_clientBuildTag') or '-'} dk={'yes' if body.get('_deviceKey') else 'no'} "
          f"body={_extra}")
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
    from services.build_gate import enforced as _build_enforced
    resp = {"success": 1, "isPermabanned": 0, "banReason": "", "remainingBanHours": 0}
    account_id = body.get("accountID", "")
    if not account_id:
        return resp
    doc = await accounts.find_one(
        {"accountID": account_id},
        {"_id": 0, "pendingKick": 1, "kickReason": 1, "adminBlocked": 1, "banReason": 1,
         "blockReason": 1, "banExpireTimestamp": 1, "shouldForceRefresh": 1,
         "clientBuildStatus": 1, "clientBuildTag": 1}) or {}
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
    elif current_status(doc) == "outdated" and _build_enforced():
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
