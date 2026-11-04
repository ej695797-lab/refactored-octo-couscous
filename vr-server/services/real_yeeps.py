"""Forward world-data reads to the REAL Yeeps AWS backend.

The playyeeps line's goal is to play the REAL live Yeeps world (real maps,
rooms, seasonal content) while our server only REPLACES the player-identity
auth (login/account), which is gated by a Meta user-proof nonce a re-signed
APK can't satisfy.

The game's WORLD data is NOT gated by that nonce. It lives in Yeeps' own AWS
(DynamoDB tables G2_RoomMap / G2_Rooms / G2_Maps / G2_Globals, ...) and is
read with GUEST Cognito credentials (services/cognito_creds.py) that anyone
can mint -- the same unauthenticated path the real mobile client uses. So for
a play/livebase client we forward world-table reads straight to real Yeeps
with guest creds and hand back the real item, and keep only the account tables
(G2_Accounts / G2_AccountStats) local (served as the player's identity, which
is what we replace).

boto3 is synchronous; every call runs in the default thread-pool executor so
the FastAPI event loop is never blocked.
"""
import asyncio
import os

import boto3
from botocore.config import Config as _BotoConfig

from services.cognito_creds import get_guest_credentials

REGION = os.getenv("REAL_YEEPS_REGION", "us-west-1")

# We forward MOSTLY EVERYTHING to real Yeeps (real world + real game data). Only
# these tables stay local -- they ARE the player identity we replace (our
# account, served without the Meta nonce). Everything else (G2_RoomMap, G2_Rooms,
# G2_Maps, G2_Globals, G2_Bundles, G2_AccountStats of others, leaderboards, ...)
# is read from real Yeeps with guest creds.
LOCAL_ONLY_TABLES = {"G2_Accounts"}

# g2_* Lambda functions that are account-identity / write paths: keep local
# (served by our handlers). Everything else is attempted against real Yeeps'
# Lambda; AccessDenied/errors fall back to local automatically.
LOCAL_ONLY_LAMBDAS = {
    "questLogIntoAccount", "g2_logIntoAccount", "g2_fetchAccountData",
    "g2_fetchAccountDatas", "g2_refreshAccountOnline", "g2_setAccountData",
    "g2_createAccount", "g2_fetchGameSession", "g2_fetchCredentials",
}

_boto = {"key": None, "ddb": None, "lam": None}


def _is_personal_key(query: dict) -> bool:
    """p_/o_/c_ keys are personal/community worlds."""
    for v in (query or {}).values():
        if isinstance(v, str) and v.startswith(("p_", "o_", "c_")):
            return True
    return False


def is_world_table(table_name: str) -> bool:
    """True = forward this table's reads to real Yeeps (everything but account)."""
    return bool(table_name) and table_name not in LOCAL_ONLY_TABLES


_CFG = _BotoConfig(retries={"max_attempts": 2, "mode": "standard"},
                   connect_timeout=6, read_timeout=12)


async def _clients():
    """boto3 DynamoDB + Lambda clients bound to current guest creds."""
    creds = await get_guest_credentials()
    if not creds:
        return None, None
    if _boto["key"] != creds["accessKey"] or _boto["ddb"] is None:
        kw = dict(region_name=REGION, aws_access_key_id=creds["accessKey"],
                  aws_secret_access_key=creds["secretKey"],
                  aws_session_token=creds["sessionToken"], config=_CFG)
        _boto["ddb"] = boto3.client("dynamodb", **kw)
        _boto["lam"] = boto3.client("lambda", **kw)
        _boto["key"] = creds["accessKey"]
    return _boto["ddb"], _boto["lam"]


async def _client():
    ddb, _ = await _clients()
    return ddb


def is_local_lambda(fn: str) -> bool:
    return (fn or "") in LOCAL_ONLY_LAMBDAS


# Lambda functions whose LOCAL handler serves non-real data (global rotation,
# community-world lists, map blocks): forward these to real Yeeps for play
# clients so the live event/world lists/geometry are real. Room map/room data
# already forward through handlers/rooms.py, so they're not listed here.
FORWARD_LAMBDAS = {
    "fetchglobalrotation", "fetchfeaturedworldlist",
    "fetchpopularworldlist", "fetchmapdata",
}

# Account/write/server-specific substrings that MUST stay local even in the
# generic fallback (we can't write to real Yeeps, and identity is ours).
_LOCAL_LAMBDA_SUBSTR = (
    "login", "account", "online", "save", "put", "remove", "moderator",
    "purchase", "expand", "delete", "devreset", "redeem", "logdebug",
    "fatalerror", "anonymous", "cosmetic", "pet", "outfit", "cwtrack",
    "cwedit", "createaccount",
)


def should_forward_lambda(fn: str) -> bool:
    """Explicit world-read functions we forward even though they have a handler."""
    fn = (fn or "").lower()
    return any(k in fn for k in FORWARD_LAMBDAS)


def fallback_forward_ok(fn: str) -> bool:
    """For an UNHANDLED function: forward to real Yeeps unless it's an
    account/write/server-specific call."""
    fn = (fn or "").lower()
    if not fn:
        return False
    return not any(s in fn for s in _LOCAL_LAMBDA_SUBSTR)


async def forward_ok(function_name: str, body_dict: dict):
    """Forward a Lambda call and return the payload bytes ONLY if real Yeeps
    answered statusCode 200; otherwise None so the caller serves it locally.
    Strips our internal _fields from the body before forwarding."""
    import json as _j
    raw = _j.dumps({k: v for k, v in (body_dict or {}).items()
                    if not str(k).startswith("_")}, ensure_ascii=False).encode()
    resp = await invoke_lambda(function_name, raw)
    if not resp:
        return None
    try:
        if int(_j.loads(resp).get("statusCode", 200)) != 200:
            return None
    except Exception:
        pass
    return resp


async def invoke_lambda(fn: str, payload_bytes: bytes):
    """Forward a g2_* Lambda invoke to real Yeeps. Returns the raw response
    Payload bytes, or None on failure (so the caller serves it locally)."""
    _, lam = await _clients()
    if lam is None:
        return None
    try:
        loop = asyncio.get_running_loop()
        r = await loop.run_in_executor(
            None, lambda: lam.invoke(FunctionName=fn, Payload=payload_bytes or b"{}"))
        if r.get("FunctionError"):
            print(f"[real_yeeps] lambda {fn} FunctionError={r.get('FunctionError')}")
        return r["Payload"].read()
    except Exception as e:
        print(f"[real_yeeps] invoke {fn} failed: {type(e).__name__}: {str(e)[:120]}")
        return None


async def get_item(table_name: str, key: dict) -> dict | None:
    """Forward a GetItem to real Yeeps. `key` is the raw DynamoDB-typed Key.

    Returns the boto3 response dict ({"Item": <typed>} or {}), or None on
    failure so the caller can fall back to local handling.
    """
    ddb = await _client()
    if ddb is None:
        return None
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: ddb.get_item(TableName=table_name, Key=key))
    except Exception as e:
        print(f"[real_yeeps] get_item {table_name} failed: {type(e).__name__}: {str(e)[:120]}")
        return None


def _plain(item: dict):
    """Deserialize a DynamoDB-typed item to plain JSON (Decimal -> int/float)."""
    import decimal
    from boto3.dynamodb.types import TypeDeserializer
    de = TypeDeserializer()

    def conv(v):
        if isinstance(v, decimal.Decimal):
            return int(v) if v == v.to_integral_value() else float(v)
        if isinstance(v, (bytes, bytearray)):
            return v.decode("utf-8", "replace")
        if isinstance(v, set):
            return [conv(x) for x in v]
        if isinstance(v, list):
            return [conv(x) for x in v]
        if isinstance(v, dict):
            return {k: conv(x) for k, x in v.items()}
        return v
    return {k: conv(de.deserialize(val)) for k, val in item.items()}


async def get_item_plain(table_name: str, key_plain: dict) -> dict | None:
    """Forward a GetItem and return the item as plain JSON, or None."""
    from boto3.dynamodb.types import TypeSerializer
    ser = TypeSerializer()
    key = {k: ser.serialize(v) for k, v in key_plain.items()}
    r = await get_item(table_name, key)
    item = (r or {}).get("Item")
    return _plain(item) if item else None


async def query(table_name: str, **kwargs) -> dict | None:
    """Forward a Query to real Yeeps. kwargs are raw DynamoDB Query params."""
    ddb = await _client()
    if ddb is None:
        return None
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: ddb.query(TableName=table_name, **kwargs))
    except Exception as e:
        print(f"[real_yeeps] query {table_name} failed: {type(e).__name__}: {str(e)[:120]}")
        return None
