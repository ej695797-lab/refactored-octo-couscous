"""Direct calls to the real Yeeps API, without the relay hop.

Each service account (a globals key such as "cw_fetch_account") keeps ONE login,
reused for about 20 minutes and never refreshed by two callers at once: Yeeps
cancels an account's older session when it logs in again, so a burst of logins
made every call fail (INSUFFICIENT_PERMISSIONS). The Yeeps password comes from
the YEEPS_API_PASSWORD environment setting, never from the repo.
"""
import asyncio
import json
import os
import random
import time
import uuid

import boto3
import httpx

from services.cognito_creds import get_guest_credentials

BASE = "https://eb1v6k1091.execute-api.us-west-1.amazonaws.com"
TRUE_VERSION = "2.28.1"
DISPATCHER = "g2_mobileInvokeFunction"
HEADERS = {
    "Accept": "*/*", "Content-Type": "application/json", "Accept-Encoding": "gzip, deflate, br",
    "X-Unity-Version": "6000.1.17f1", "User-Agent": "Yeeps/0 CFNetwork/3860.400.51 Darwin/25.3.0",
    "Accept-Language": "en-US,en;q=0.9",
}
SESSION_TTL = 55 * 60     # real Yeeps mobile sessions last ~1h: reuse the login
                          # that long so we re-auth ~3x less (fewer login calls =
                          # far smaller abuse signal; the ban comes from login).
_sessions: dict = {}      # account key -> session
_locks: dict = {}
# Service accounts real Yeeps has BANNED (mobileLinkAccount -> err_type BANNED).
# Populated at login; callers skip these so we never hammer a dead account (which
# wastes a call and worsens our standing). In-memory -> clears on restart, so a
# lifted ban is retried after a restart.
_BANNED: set = set()


def _lock_for(account: str) -> asyncio.Lock:
    if account not in _locks:
        _locks[account] = asyncio.Lock()
    return _locks[account]


async def _service_account(account: str):
    from services.db import globals_col
    doc = await globals_col.find_one({"key": account}, {"_id": 0, "value": 1})
    v = (doc or {}).get("value") or {}
    return v.get("oculusID"), str(v.get("mobileCode") or "")


async def _login(account: str = "cw_fetch_account", stale_session=None) -> dict:
    """The account's current session; logs in only when there is none, it is old, or
    the session a caller just saw fail is still the current one."""
    async with _lock_for(account):
        if account in _BANNED:
            raise RuntimeError(f"Yeeps account {account} is banned (skipped)")
        s = _sessions.get(account)
        if s and time.time() - s["ts"] < SESSION_TTL and s["gameSessionID"] != stale_session:
            return s
        password = os.environ.get("YEEPS_API_PASSWORD", "")
        oculus_id, code = await _service_account(account)
        if not (oculus_id and code and password):
            raise RuntimeError(f"Yeeps API login for {account} is not configured")
        async with httpx.AsyncClient(timeout=30, headers=HEADERS) as client:
            link = await client.post(BASE + "/mobileLinkAccount", json={
                "oculusID": oculus_id, "mobileCode": code, "linkVersion": 0,
                "mobilePassword": "", "password": password})
            if link.status_code != 200:
                _et = ""
                try:
                    _et = (link.json() or {}).get("err_type", "")
                except Exception:
                    pass
                if _et == "BANNED":
                    _BANNED.add(account)   # stop hammering a dead account
                    print(f"[yeeps] {account} is BANNED by real Yeeps -> removed from pool")
                raise RuntimeError(f"Yeeps link answered {link.status_code} {_et}")
            ld = (link.json() or {}).get("data") or {}
            mobile_password, account_id = ld.get("mobilePassword"), ld.get("accountID")
            login = await client.post(BASE + "/mobileLogIntoAccount", json={
                "accountID": account_id, "mobilePassword": mobile_password, "mobileID": uuid.uuid4().hex,
                "dataVersion": 2, "trueVersion": TRUE_VERSION, "propertiesToGet": ["displayName"],
                "nonce": str(random.randint(100000, 999999)), "hashBase": str(random.randint(1000, 9999)),
                "password": password})
            if login.status_code != 200:
                raise RuntimeError(f"Yeeps login answered {login.status_code}")
            game_session = ((login.json() or {}).get("data") or {}).get("gameSessionID")
        if not game_session:
            raise RuntimeError("Yeeps login gave no game session")
        s = {"ts": time.time(), "accountID": account_id, "mobilePassword": mobile_password,
             "gameSessionID": game_session}
        _sessions[account] = s
        print(f"[yeeps] logged in {account}")
        return s


def _invoke_sync(creds: dict, payload: dict, function_name: str = DISPATCHER) -> dict:
    lam = boto3.client("lambda", region_name="us-west-1", aws_access_key_id=creds["accessKey"],
                       aws_secret_access_key=creds["secretKey"], aws_session_token=creds["sessionToken"])
    out = lam.invoke(FunctionName=function_name, InvocationType="RequestResponse",
                     Payload=json.dumps(payload))
    return json.loads(out["Payload"].read())


def _read_only(function_name: str) -> None:
    """We only ever READ from Yeeps (owner rule, 2026-10-11): any function that is not a
    fetch* call (put*, try*, save*, update*, ...) is refused before anything is sent."""
    name = function_name[3:] if function_name.startswith("g2_") else function_name
    if not name.startswith("fetch"):
        raise PermissionError(f"refused to call {function_name} on Yeeps: read-only")


async def _call(function_name: str, extra: dict, account: str, standalone: bool, with_session: bool) -> dict:
    _read_only(function_name)
    raw, stale = {}, None
    for _ in range(2):                            # a cancelled session is replaced once
        creds = await get_guest_credentials()
        if not creds:
            raise RuntimeError("no AWS credentials")
        payload = dict(extra)
        s = None
        if with_session:
            s = await _login(account, stale_session=stale)
            payload.update(accountID=s["accountID"], gameSessionID=s["gameSessionID"],
                           mobilePassword=s["mobilePassword"], dataVersion=2)
        if not standalone:
            payload.update(functionName=function_name, applicationVersion=TRUE_VERSION)
        raw = await asyncio.to_thread(_invoke_sync, creds, payload, function_name if standalone else DISPATCHER)
        if raw.get("statusCode") == 200:
            body = raw.get("body")
            body = json.loads(body) if isinstance(body, str) else (body or {})
            return body.get("data") or {}
        if raw.get("statusCode") in (400, 404) or s is None:
            break                                 # a real "not found / bad input", not a session problem
        stale = s["gameSessionID"]
    raise RuntimeError(f"{function_name} answered {raw.get('statusCode')}")


async def invoke(function_name: str, extra: dict, account: str = "cw_fetch_account") -> dict:
    """One dispatcher function on the real Yeeps API; returns the body's data dict."""
    return await _call(function_name, extra, account, standalone=False, with_session=True)


async def invoke_standalone(function_name: str, extra: dict, account: str = "cw_fetch_account",
                            with_session: bool = True) -> dict:
    """A standalone g2_ Lambda (world lists)."""
    return await _call(function_name, extra, account, standalone=True, with_session=with_session)


async def fetch_accounts(account_ids: list, props: list, account: str = "cw_fetch_account") -> dict:
    """{accountID: {prop: value}} straight from the real Yeeps account-data call (fetchAccountDatas)."""
    data = await invoke("fetchAccountDatas", {"targetAccountIDs": list(account_ids), "propertiesToGet": list(props)},
                        account=account)
    return data.get("responses") or {}


async def probe_bans(accounts: list, delay: float = 3.0, tries: int = 3) -> None:
    """Log into each account once (sequentially, well spaced) so _BANNED is
    populated UP FRONT and callers skip dead accounts instead of randomly hitting
    them after a restart. A definitive BANNED is final (no retry); a TRANSIENT
    error (real Yeeps' FAILED_TO_REACH / timeout under a login burst) is retried
    so a good account is never misclassified as unusable. A good login establishes
    its ~1h session for reuse. Called once at startup."""
    import asyncio as _a
    ok = 0
    for a in accounts:
        for attempt in range(tries):
            try:
                await _login(a)
                ok += 1
                break
            except RuntimeError as e:
                if "BANNED" in str(e) or "is banned" in str(e):
                    break                      # confirmed banned -> stop
                await _a.sleep(2)              # transient -> retry
            except Exception:
                await _a.sleep(2)
        await _a.sleep(delay)
    print(f"[yeeps] ban probe done: {ok}/{len(accounts)} service accounts usable, "
          f"{len(_BANNED)} banned ({sorted(_BANNED)})")
