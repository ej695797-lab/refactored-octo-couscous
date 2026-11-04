"""
Live account-name resolution for community-world builders (and any player we
have no local record of). Mirrors 1.31's dynamo_ops._live_account: for an
unknown o_ accountID, fetch its public profile straight from the real Yeeps API
(g2_fetchAccountDatas via the shared cw service-account session -- NO relay
/lookup hop), cached 10 min in memory.

CW blocks are served as-is with their raw o_ owner IDs (the direct-lambda fetch
keeps them unresolved). The client turns each owner o_ id into a name by reading
G2_Accounts -- GetItem / BatchGetItem / g2_fetchAccountDatas -- so those handlers
call live_account() here to return the real displayName instead of a "Yeeps"
placeholder, exactly the way 1.31 does it.
"""
import re
import time

_LIVE_ACCT: dict = {}          # accountID -> (expires_ts, doc|None)
_NO_PERM: set = set()          # service-account tags that 403 reading others
_PROPS = ["displayName", "activeCosmetics", "skinColor", "eyeColor"]
_MAX_TRIES = 8                 # bound room-load latency on a cold resolve


def _parse(body, tid):
    """Pull one account's fields out of the real fetchAccountDatas reply. The
    live shape is {"data": {"responses": {accountID: {...}}}} (same as the relay
    at server.js:830-849); accept the looser variants too."""
    if not isinstance(body, dict):
        return None
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    for src in (data.get("responses"), data, body.get("responses"), body):
        if isinstance(src, dict) and isinstance(src.get(tid), dict):
            return src[tid]
    return None


async def live_account(account_id) -> dict | None:
    """Public profile of an unknown o_ account straight from real Yeeps, cached
    10 min (a miss is cached 60s). None for anything that is not an o_ id."""
    if not isinstance(account_id, str) or not re.fullmatch(r"o_\d+", account_id):
        return None
    hit = _LIVE_ACCT.get(account_id)
    if hit and hit[0] > time.time():
        return hit[1]
    doc = None
    try:
        # RELAY-FREE: direct real-Yeeps fetchAccountDatas via yeeps_direct (the
        # 1.31 model) -- logs into Yeeps directly (mobileLogIntoAccount), no proxy
        # /AWS relay hop. yeeps_direct.invoke handles login/session/retry; its
        # default account is a fetch account (not the owner/grid one that 403s).
        from services.yeeps_direct import fetch_accounts
        # Use a LIVE (non-banned) service account -- the hardcoded default
        # (cw_fetch_account) is among the 14 real Yeeps banned, so the lookup
        # would fail and every builder show as the "Yeep" placeholder.
        try:
            from services.dynamo_ops import _cw_direct_account as _pick
            _acct = _pick("")
        except Exception:
            _acct = "cw_fetch_account"
        found = (await fetch_accounts([account_id], _PROPS, account=_acct)).get(account_id)
        if found and found.get("displayName"):
            doc = {"accountID": account_id,
                   "displayName": found.get("displayName"),
                   "activeCosmetics": found.get("activeCosmetics") or [],
                   "skinColor": found.get("skinColor"),
                   "eyeColor": found.get("eyeColor")}
            doc = {k: v for k, v in doc.items() if v is not None}
    except Exception as e:
        print(f"[live] account resolve failed for {account_id}: {type(e).__name__}: {e}")
    _LIVE_ACCT[account_id] = (time.time() + (600 if doc else 60), doc)
    return doc
