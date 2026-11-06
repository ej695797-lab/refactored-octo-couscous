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
from handlers.rooms import handle_room_data, handle_room_map_data
from handlers.community import (
    handle_cw_get_can_enter,
    handle_cw_track_visit,
    handle_cw_try_purchase_fuel,
    handle_cw_edit_permissions,
)
from handlers.cosmetics import handle_put_active_cosmetics
from handlers.maps import handle_save_room_map, handle_fetch_map_data, handle_save_room_map_properties
from handlers.currency import handle_dev_reset_currency
from handlers.challenges import (
    handle_redeem_challenge_reward,
    handle_fetch_global_rotation,
)
from handlers.misc import (
    handle_fetch_account_datas,
    handle_fetch_featured_world_list,
    handle_fetch_popular_world_list,
    handle_log_debug,
    handle_fetch_global_fatal_error,
    handle_refresh_account_online,
    handle_try_redeem_currency_stash,
)
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
    err = data.get("_lambda_error") or (data.get("data") or {}).get("_lambda_error") \
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
# Lambdas that change state (vs. read-only fetches): see check_write above.
WRITE_LAMBDAS = {
    "cwtrackvisit", "cwtrypurchasefuel", "cweditpermissions", "redeemchallengereward",
    "putactivecosmetics", "saveroommapcompressed", "saveroommapproperties", "devresetcurrency",
    "tryredeemcurrencystash", "moderatorbanaccount", "moderatorwarnaccount", "trypurchase",
    "tryexpandprivateworld", "trydeleteprivateroom", "trydeletecommunityworldroom",
}

HANDLER_TABLE = [
    ("logintoaccount",          handle_login),
    ("fetchglobalrotation",     handle_fetch_global_rotation),
    ("fetchfeaturedworldlist",   handle_fetch_featured_world_list),
    ("fetchpopularworldlist",   handle_fetch_popular_world_list),
    ("cwgetcanenter",           handle_cw_get_can_enter),
    ("cwtrackvisit",            handle_cw_track_visit),
    ("cwtrypurchasefuel",       handle_cw_try_purchase_fuel),
    ("cweditpermissions",       handle_cw_edit_permissions),
    ("redeemchallengereward",   handle_redeem_challenge_reward),
    ("putactivecosmetics",      handle_put_active_cosmetics),
    ("saveroommapcompressed",   handle_save_room_map),
    ("saveroommapproperties",   handle_save_room_map_properties),
    ("fetchroommapdata",        handle_room_map_data),
    ("fetchmapdata",            handle_fetch_map_data),
    ("devresetcurrency",        handle_dev_reset_currency),
    ("getroomdata",             handle_room_data),
    ("fetchroomdata",           handle_room_data),
    ("logdebug",                handle_log_debug),
    ("fetchglobalfatalerror",   handle_fetch_global_fatal_error),
    ("fetchaccountdatas",       handle_fetch_account_datas),
    ("refreshaccountonline",    handle_refresh_account_online),
    ("tryredeemcurrencystash",  handle_try_redeem_currency_stash),
    ("moderatorbanaccount",     handle_moderator_ban_account),
    ("moderatorwarnaccount",    handle_moderator_warn_account),
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
            body["_clientBuildTag"] = getattr(request.state, "client_build_tag", "")
    except Exception:
        pass
    print(f"[lambda] Invoking: {function_name}  inner={inner_fn or '-'}")
    if isinstance(body, dict):
        body["_fn"] = str(body.get("functionName") or function_name)

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
                # Anything that CHANGES something also needs the account's own bound
                # headset, even for accounts with none bound yet (scripts posing as them).
                if _ok and match_str in WRITE_LAMBDAS:
                    _ok = await device_auth.check_write(body.get("accountID"), body.get("_deviceKey", ""),
                                                        function_name, body.get("_clientIP", ""))
                if not _ok:
                    return _lambda_envelope({"data": {"success": 0, "error": "NOT_AUTHORIZED"}})
            result = await handler(body)
            # If handler returned {data: ...}, use as-is; otherwise wrap
            if "data" not in result:
                result = {"data": result}
            envelope_text = json.dumps(_lambda_envelope_payload(result), ensure_ascii=False)
            _append_lambda_log(function_name, body, envelope_text)
            return _lambda_envelope(result)

    # No match — use generic fallback
    print(f"[lambda] No handler for '{function_name}', using fallback")
    result = await _generic_fallback(body)
    envelope_text = json.dumps(_lambda_envelope_payload(result), ensure_ascii=False)
    _append_lambda_log(function_name, body, envelope_text)
    return _lambda_envelope(result)
