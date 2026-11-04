"""
Lambda invocation router.
Handles POST /2015-03-31/functions/{fn}/invocations
Dispatches to the appropriate handler based on function name.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import Response

from handlers.login import handle_login
from handlers.moderation import handle_moderator_ban_account, handle_moderator_warn_account
from handlers.rooms import handle_room_data, handle_room_map_data, handle_fetch_room_map_extra_properties
from handlers.community import (
    handle_cw_get_can_enter,
    handle_cw_track_visit,
    handle_cw_try_purchase_fuel,
    handle_cw_edit_permissions,
)
from handlers.cosmetics import handle_put_active_cosmetics
from handlers.maps import handle_save_room_map, handle_fetch_map_data, handle_save_room_map_properties
from handlers.currency import handle_dev_reset_currency
from handlers import cards as _cards
from handlers.challenges import (
    handle_redeem_challenge_reward,
    handle_fetch_global_rotation,
)
from handlers.misc import (
    handle_fetch_account_datas,
    handle_fetch_featured_world_list,
    handle_fetch_popular_world_list,
    handle_fetch_random_world_list,
    handle_log_debug,
    handle_fetch_global_fatal_error,
    handle_refresh_account_online,
    handle_try_redeem_currency_stash,
    handle_put_player_status,
    handle_fetch_player_statuses,
    handle_put_toymaker_request,
    handle_put_favorite_patterns,
    handle_try_purchase_customized_item,
    handle_fetch_customized_items)
from handlers.purchases import handle_purchase
from services.property_defaults import default_for_property

router = APIRouter()
LAMBDA_LOG_FILE = Path("logs/lambda_traffic.log")


def _append_lambda_log(function_name: str, request_body: dict, response_body_text: str) -> None:
    """Append Lambda request/response payloads (no headers) to a file."""
    LAMBDA_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    entry = (
        f"[{timestamp}]\n"
        f"Function: {function_name}\n"
        f"RequestBody: {json.dumps(request_body, ensure_ascii=False)}\n"
        f"ResponseBody: {response_body_text}\n"
        f"{'=' * 80}\n"
    )
    with LAMBDA_LOG_FILE.open("a", encoding="utf-8") as log_file:
        log_file.write(entry)


def _lambda_envelope_payload(data: dict) -> dict:
    """Build the Lambda envelope payload object. A handler signals an error
    with {"_lambda_error": "ERR_TYPE"} (anywhere at the top or under "data"):
    the client's LambdaResponse treats a non-200 statusCode as a failure and
    reads body.err_type."""
    err = data.get("_lambda_error") or (data.get("data") if isinstance(data.get("data"), dict) else {}).get("_lambda_error") \
        if isinstance(data, dict) else None
    if err:
        return {"statusCode": 400, "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"err_type": err})}
    body_inner = json.dumps(data)
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": body_inner,
    }


def _lambda_envelope(data: dict) -> Response:
    """
    Wrap a response in the double-JSON Lambda envelope the client expects.
    Outer: {"statusCode": 200, "headers": {...}, "body": "<json-string>"}
    """
    envelope = _lambda_envelope_payload(data)
    headers = {
        "Content-Type": "application/json",
        "x-amzn-RequestId": uuid.uuid4().hex,
        "x-amzn-Remapped-Content-Length": "0",
        "X-Amz-Executed-Version": "$LATEST",
    }
    return Response(
        content=json.dumps(envelope),
        media_type="application/json",
        headers=headers,
    )


# ─── Handler dispatch table ────────────────────────────────────────
# Each entry: (substring to match in lowercase, handler coroutine)
from handlers.v2_extras import (
    handle_save_room_passageway_links, handle_put_saved_outfits, handle_remove_saved_outfit,
    handle_put_pet_name, handle_put_pet_location, handle_put_anonymous_mode)

import handlers.purchases as _purchases_mode

HANDLER_TABLE = [
    # Claims (handlers/cards.py, 2026-10-17): MUST precede the "trypurchase" catch-all.
    # pw play/creative mode persistence (handlers/purchases.py, 2026-10-18); before any
    # "creative" catch-all
    ("entercreativemode",       _purchases_mode.handle_enter_creative_mode),
    ("enterplaymode",           _purchases_mode.handle_enter_play_mode),
    ("trypurchasetradingcardpack", _cards.handle_try_purchase_trading_card_pack),
    ("redeemtradingcardreward", _cards.handle_redeem_trading_card_reward),
    ("redeemchallengereward",   _cards.handle_redeem_challenge_reward),
    ("tryredeemcurrencystash",  _cards.handle_try_redeem_currency_stash),
    ("logintoaccount",          handle_login),
    # 2.4.0 (handlers/v2_extras.py)
    ("saveroompassagewaylinks", handle_save_room_passageway_links),
    ("putsavedoutfits",         handle_put_saved_outfits),
    ("removesavedoutfit",       handle_remove_saved_outfit),
    ("putpetname",              handle_put_pet_name),
    ("putpetlocation",          handle_put_pet_location),
    ("putanonymousmode",        handle_put_anonymous_mode),
    ("fetchglobalrotation",     handle_fetch_global_rotation),
    ("fetchfeaturedworldlist",   handle_fetch_featured_world_list),
    ("fetchpopularworldlist",   handle_fetch_popular_world_list),
    ("fetchrandomworldlist",    handle_fetch_random_world_list),
    ("putplayerstatus",         handle_put_player_status),
    ("fetchplayerstatuses",     handle_fetch_player_statuses),
    ("puttoymakerrequest",      handle_put_toymaker_request),
    ("putfavoritepatterns",     handle_put_favorite_patterns),
    ("cwgetcanenter",           handle_cw_get_can_enter),
    ("cwtrackvisit",            handle_cw_track_visit),
    ("cwtrypurchasefuel",       handle_cw_try_purchase_fuel),
    ("cweditpermissions",       handle_cw_edit_permissions),
    ("putactivecosmetics",      handle_put_active_cosmetics),
    ("saveroomterrain",         handle_save_room_map),   # 2.x terrain saves (stored separately)
    ("saveroommapcompressed",   handle_save_room_map),
    ("saveroommapproperties",   handle_save_room_map_properties),
    ("fetchroommapdata",        handle_room_map_data),
    ("fetchmapdata",            handle_fetch_map_data),
    ("devresetcurrency",        handle_dev_reset_currency),
    ("getroomdata",             handle_room_data),
    ("fetchroommapextraproperties", handle_fetch_room_map_extra_properties),
    ("fetchroomdata",           handle_room_data),
    ("logdebug",                handle_log_debug),
    ("fetchglobalfatalerror",   handle_fetch_global_fatal_error),
    ("fetchaccountdatas",       handle_fetch_account_datas),
    ("refreshaccountonline",    handle_refresh_account_online),
    ("moderatorbanaccount",     handle_moderator_ban_account),
    ("moderatorwarnaccount",    handle_moderator_warn_account),
    # Reskins (customized items): MUST be before "trypurchase" (substring match)
    # or tryPurchaseCustomizedItem routes to the generic purchase handler and the
    # reskin is never stored.
    ("trypurchasecustomizeditem", handle_try_purchase_customized_item),
    ("fetchcustomizeditems",      handle_fetch_customized_items),
    # 1.31 purchases (after cwtrypurchasefuel, which keeps its own handler).
    ("trypurchase",             handle_purchase),
    ("tryexpandprivateworld",   handle_purchase),
    ("trydeleteprivateroom",    handle_purchase),
    ("trydeletecommunityworldroom", handle_purchase),
]


async def _generic_fallback(body: dict) -> dict:
    """
    Generic fallback for unknown Lambda functions.
    If body has propertiesToGet → return defaults for each property.
    Otherwise → return success.
    """
    # Capture unhandled trading-card traffic so the card system can be built from
    # the client's real payloads (no 2.31.4 dump, and real Yeeps won't expose
    # card fields): every card/shard/pack/challenge call that lands here is
    # stored (capped) in Mongo `card_capture`. 2026-10-17.
    try:
        _fn = str(body.get("_fn") or body.get("functionName") or "")
        if any(w in _fn.lower() for w in ("card", "shard", "pack", "challenge", "binder")):
            from services.db import db as _cdb
            from datetime import datetime as _dt, timezone as _tz
            _clean = {k: v for k, v in body.items() if not str(k).startswith("_") or k in ("_fn", "_clientBuildTag")}
            await _cdb["card_capture"].insert_one({"at": _dt.now(_tz.utc).strftime("%Y-%m-%d %H:%M:%S"),
                                                   "kind": "lambda-fallback", "fn": _fn, "body": _clean})
            if await _cdb["card_capture"].estimated_document_count() > 300:
                _old = [d["_id"] async for d in _cdb["card_capture"].find({}, {"_id": 1}).sort("_id", 1).limit(100)]
                await _cdb["card_capture"].delete_many({"_id": {"$in": _old}})
            print(f"[cardcap] captured {_fn}")
    except Exception as _ce:
        print(f"[cardcap] capture failed: {_ce}")
    props = body.get("propertiesToGet", [])
    if props:
        data = {p: default_for_property(p) for p in props}
        return {"data": data}
    return {"data": {"success": 1}}


@router.post("/2015-03-31/functions/{function_name}/invocations")
async def lambda_invoke(function_name: str, request: Request):
    """Handle Lambda-style function invocations."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    fn_lower = function_name.lower()
    # The client reaches most backend calls through the DISPATCHER Lambda
    # `g2_mobileInvokeFunction`, which carries the REAL function name in the
    # body as `functionName` (e.g. {"functionName":"getRoomData","roomKey":...}).
    # The URL name is then just the dispatcher, which matches none of the
    # HANDLER_TABLE keys, so those calls fell through to the generic fallback
    # and returned wrong/placeholder data -> the client's AWS SDK rejected the
    # response, retried on a disposed HttpRequestMessage -> ObjectDisposedException
    # loop -> RoomDatabase.GetRoomData never completed -> room never loaded.
    # Fix: when the URL is the dispatcher (or matches nothing), also route on the
    # body's inner functionName.
    inner_fn = ""
    try:
        inner_fn = str(body.get("functionName", "") or "").lower()
    except Exception:
        inner_fn = ""
    # Record the caller's IP in the body so handle_login can stamp it on the
    # account (lastIP/recentIPs) — this is what makes IP-banning by username easy.
    try:
        from services.ip_bans import client_ip as _cip
        if isinstance(body, dict):
            body["_clientIP"] = _cip(request)
            body["_deviceKey"] = getattr(request.state, "device_key", "")
            from services import big_patterns as _bigp
            body["_clientBuildTag"] = await _bigp.tag_for(getattr(request.state, "client_build_tag", ""),
                                                          body["_deviceKey"])
    except Exception:
        pass
    print(f"[lambda] Invoking: {function_name}  inner={inner_fn or '-'}")
    if isinstance(body, dict):
        body["_fn"] = str(body.get("functionName") or function_name)

    # REAL-YEEPS passthrough (playyeeps line): forward world-read calls to real
    # Yeeps so the live event rotation / world lists / map geometry are real.
    # Real Yeeps' Lambda payload IS already the {statusCode,body} envelope the
    # client expects, so return it verbatim. Account/write/server calls stay
    # local. On any failure we fall through to the local handler.
    try:
        from services import big_patterns as _bpl, real_yeeps as _ry
        if _bpl.is_play(body.get("_clientBuildTag")) and _ry.should_forward_lambda(inner_fn or fn_lower):
            _resp = await _ry.forward_ok(function_name, body)
            if _resp:
                print(f"[lambda] -> real yeeps ({inner_fn or fn_lower}) {len(_resp)}b")
                return Response(content=_resp, media_type="application/json",
                                headers={"x-amzn-RequestId": uuid.uuid4().hex,
                                         "X-Amz-Executed-Version": "$LATEST"})
    except Exception as _e:
        print(f"[lambda] real-yeeps forward failed ({inner_fn or fn_lower}): {_e}")

    # COMMUNITY-WORLD read passthrough: for a c_ room READ, forward the client's
    # exact call to real Yeeps (service-account session) and return real Yeeps'
    # response VERBATIM -- real sid, saveData + saveInternalStates, spawn, cw_
    # attrs, terrain -- nothing reconstructed/dropped (Blaze: "keep cw calling the
    # real yeeps apis, dont touch that info"). Fixes wrong-spawn / blank-signs /
    # empty-blocks-crash. Writes are never in _CW_READ_FUNCS. On total
    # service-account failure we fall through to the local handler.
    try:
        import os as _os_lr
        from services import dynamo_ops as _cwd
        from services import big_patterns as _bp_live
        # Resolve the read function from the dispatcher body OR the URL. The live
        # 2.31.4 client calls g2_fetchMapData / g2_fetchRoomData DIRECTLY (no
        # body.functionName), so a body-only check never fired -> the live
        # passthrough was dead for it and CW fell back to the stale DB cache.
        _innerfn = str(body.get("functionName") or "") if isinstance(body, dict) else ""
        _urlfn = function_name[3:] if function_name.lower().startswith("g2_") else function_name
        _read_fn = ""
        if _innerfn.lower() in _cwd._CW_READ_FUNCS:
            _read_fn = _innerfn
        elif _urlfn.lower() in _cwd._CW_READ_FUNCS:
            _read_fn = _urlfn
        if isinstance(body, dict) and _read_fn:
            _is_cw = _cwd._cw_body_has_c(body)
            # LIVEBASE (biglive) also serves OFFICIAL/public rooms live from real
            # Yeeps. Our DB mirror was incomplete (minigame_*, itemCustomization
            # Studio missing; full nexus/wild_/freebuild_ empty) -> those rooms
            # "failed to load after 5 attempts". Real Yeeps has them; fetch live
            # like 1.31 (in-mem cache, nothing saved to the DB). Private worlds
            # (p_*/o_*) 403 on a service account, so they are NOT forwarded (kept
            # on our DB, where the player saved them); community worlds (c_) keep
            # their existing path.
            # OFFICIAL rooms are served from the local DB mirror, NOT live. Routing
            # nexus (37k loads) + every official room live across the 16 service
            # accounts looked like scraping and got 14 of them BANNED by real
            # Yeeps. Official rooms rarely change, so they belong in the DB mirror
            # (missing ones are mirrored in once, out of band). Only community
            # worlds (c_) fetch live here. Flip LIVE_OFFICIAL_ROOMS=1 to re-enable.
            _is_live_official = (
                _os_lr.getenv("LIVE_OFFICIAL_ROOMS", "") == "1"
                and not _is_cw
                and _bp_live.is_livebase(body.get("_clientBuildTag"))
                and _cwd._cw_body_is_live_official(body))
            if _is_cw or _is_live_official:
                # Map fetches resolve the REAL session id server-side (the client's
                # targetSessionID comes from our DB room map and 404s on real
                # Yeeps); room-data fetches forward verbatim.
                if _read_fn.lower() == "fetchmapdata":
                    _real = await _cwd._cw_passthrough_map(body)
                else:
                    _real = await _cwd._cw_passthrough(_read_fn, body)
                if (_real is not None and isinstance(_real.get("data"), dict)
                        and _real.get("data")):
                    envelope_text = json.dumps(_lambda_envelope_payload(_real), ensure_ascii=False)
                    _append_lambda_log(function_name, body, envelope_text)
                    return _lambda_envelope(_real)
                if _is_cw:
                    # Community worlds: a transient live miss returns a clean empty
                    # and lets the CLIENT retry -- do NOT fall through to the local
                    # DB/relay handler, which loops 16 accounts on the relay and
                    # creates a storm. The in-mem cache fills on the first success.
                    print(f"[lambda] cw passthrough {_read_fn}: live fetch failed -> empty (client retries)")
                    # Return a VALID empty-map shape, not a bare {} -- the 2.31.4
                    # client CRASHES on a fetchMapData response with no saveData
                    # key (1.31 tolerated it). For a map read, hand back an empty
                    # saveData so the CW loads blank instead of crashing the game;
                    # other CW reads still get {} (they tolerate it).
                    if _read_fn.lower() == "fetchmapdata":
                        return _lambda_envelope({"data": {"mapDataVersion": 3, "saveData": "", "saveInternalStates": ""}})
                    return _lambda_envelope({"data": {}})
                # Official live miss (real Yeeps lacks it / 403 / empty): FALL
                # THROUGH to the local handler so our own/custom rooms still serve
                # from the DB.
                print(f"[lambda] live-official {_read_fn} "
                      f"{body.get('roomKey') or body.get('roomMapKey') or '-'}: "
                      f"no live data -> local handler")
    except Exception as _e:
        print(f"[lambda] cw passthrough error: {_e}")

    # Find matching handler — try the URL name first, then the dispatcher's
    # inner functionName.
    for match_str, handler in HANDLER_TABLE:
        if match_str in fn_lower or (inner_fn and match_str in inner_fn):
            # The game logs in through /questLogIntoAccount, never through a
            # lambda invoke. Since Oct 8 only raiders' scripts used this route
            # (to pose as owner accounts), so refuse it and IP-ban the caller.
            if handler.__name__ == "handle_login":
                _ip = (body.get("_clientIP", "") if isinstance(body, dict) else "") or ""
                print(f"[guard] refused lambda-route login as "
                      f"{(body.get('accountID') if isinstance(body, dict) else '?')} from {_ip or '?'}")
                if _ip and not _ip.startswith(("127.", "::1", "10.", "192.168.")):
                    try:
                        from services.ip_bans import ban_ip
                        await ban_ip(_ip, "login through the lambda route (raider script)")
                    except Exception as _e:
                        print(f"[guard] auto IP-ban failed for {_ip}: {_e}")
                return _lambda_envelope({"data": {"success": 0, "error": "NOT_AUTHORIZED"}})
            # The caller must be the account it claims to be (headset key).
            if handler.__name__ != "handle_login" and isinstance(body, dict):
                from services import device_auth
                _ok, _ = await device_auth.check_request(body.get("accountID"), body.get("_deviceKey", ""),
                                                         function_name, body.get("_clientIP", ""))
                if not _ok:
                    return _lambda_envelope({"data": {"success": 0, "error": "NOT_AUTHORIZED"}})
            result = await handler(body)
            # If handler returned {data: ...}, use as-is; otherwise wrap
            if "data" not in result:
                result = {"data": result}
            # clients get themes they actually have (stock: all big-only keys;
            # live: themes newer than their build, e.g. 'classic' below biglive7)
            from services import big_patterns as _bigp
            _dtag = body.get("_clientBuildTag") if isinstance(body, dict) else ""
            _txt = json.dumps(result)
            _low = _bigp.downgrade_for_tag(_txt, _dtag)
            if _low != _txt:
                result = json.loads(_low)
            envelope_text = json.dumps(_lambda_envelope_payload(result), ensure_ascii=False)
            _append_lambda_log(function_name, body, envelope_text)
            return _lambda_envelope(result)

    # No local handler. For play clients, forward the long tail of data calls
    # to real Yeeps (unless it's an account/write/server call) so "mostly all"
    # data is real; otherwise use the generic placeholder fallback.
    try:
        from services import big_patterns as _bpf, real_yeeps as _ryf
        if _bpf.is_play(body.get("_clientBuildTag")) and _ryf.fallback_forward_ok(inner_fn or fn_lower):
            _resp = await _ryf.forward_ok(function_name, body)
            if _resp:
                print(f"[lambda] fallback -> real yeeps ({inner_fn or fn_lower}) {len(_resp)}b")
                return Response(content=_resp, media_type="application/json",
                                headers={"x-amzn-RequestId": uuid.uuid4().hex,
                                         "X-Amz-Executed-Version": "$LATEST"})
    except Exception as _e:
        print(f"[lambda] fallback real-yeeps forward failed: {_e}")

    # No match — use generic fallback
    print(f"[lambda] No handler for '{function_name}', using fallback")
    result = await _generic_fallback(body)
    envelope_text = json.dumps(_lambda_envelope_payload(result), ensure_ascii=False)
    _append_lambda_log(function_name, body, envelope_text)
    return _lambda_envelope(result)
