"""
Fetch real, working AWS credentials from the game's own Cognito guest
identity pool - the same unauthenticated path the audited relay uses.

The client reads plaintext accessKey/secretKey/sessionToken from the login
response and signs its direct DynamoDB calls with them. Guest Cognito creds
require no account auth, are genuinely valid for ~1h, and are what the real
game issues, so this keeps the login response using real Yeeps/AWS APIs
instead of stale hardcoded blobs.
"""

import time

import httpx

COGNITO_ENDPOINT = "https://cognito-identity.us-west-1.amazonaws.com/"
COGNITO_POOL_ID = "us-west-1:00000000-0000-0000-0000-000000000001"

_cache = {"creds": None, "expiry": 0}


async def get_guest_credentials() -> dict | None:
    """Return {accessKey, secretKey, sessionToken} or None on failure.

    Cached until ~60s before expiry so every login doesn't mint a new
    identity.
    """
    now = time.time()
    if _cache["creds"] and now < _cache["expiry"] - 60:
        return _cache["creds"]

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r1 = await client.post(
                COGNITO_ENDPOINT,
                headers={
                    "Content-Type": "application/x-amz-json-1.1",
                    "X-Amz-Target": "AWSCognitoIdentityService.GetId",
                },
                content='{"IdentityPoolId":"%s"}' % COGNITO_POOL_ID,
            )
            identity_id = r1.json().get("IdentityId")
            if not identity_id:
                print(f"[cognito] GetId failed: {r1.text[:200]}")
                return None

            r2 = await client.post(
                COGNITO_ENDPOINT,
                headers={
                    "Content-Type": "application/x-amz-json-1.1",
                    "X-Amz-Target": "AWSCognitoIdentityService.GetCredentialsForIdentity",
                },
                content='{"IdentityId":"%s"}' % identity_id,
            )
            c = r2.json().get("Credentials")
            if not c:
                print(f"[cognito] GetCredentialsForIdentity failed: {r2.text[:200]}")
                return None
    except Exception as e:
        print(f"[cognito] request error: {e}")
        return None

    creds = {
        "accessKey": c["AccessKeyId"],
        "secretKey": c["SecretKey"],
        "sessionToken": c["SessionToken"],
    }
    _cache["creds"] = creds
    _cache["expiry"] = float(c.get("Expiration") or (now + 3000))
    print(f"[cognito] refreshed guest creds (expires {_cache['expiry']})")
    return creds
