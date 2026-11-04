"""
Photon Auth endpoint.
Handles GET/POST / requests from the game client's Photon networking layer.
"""

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from services.db import accounts

router = APIRouter()


@router.api_route("/", methods=["GET", "POST"])
async def photon_auth(request: Request):
    """
    Photon auth requests can arrive as either:
    - GET /?UserId=...
    - POST / with optional JSON body containing UserId
    Return ResultCode 1 with a usable UserId in all cases.
    """
    query_params = request.query_params
    user_id = query_params.get("UserId") or query_params.get("userId") or query_params.get("accountID")
    username = query_params.get("UserName") or query_params.get("userName") or query_params.get("username")
    platform = "VR"

    if request.method == "POST":
        raw = await request.body()
        if raw:
            try:
                body = json.loads(raw)
                if isinstance(body, dict):
                    user_id = user_id or body.get("UserId") or body.get("userId") or body.get("accountID")
                    username = (
                        username
                        or body.get("UserName")
                        or body.get("userName")
                        or body.get("username")
                        or body.get("oculusID")
                        or body.get("displayName")
                    )
            except Exception:
                pass

    if not user_id:
        user_id = "o_0"
    if not username:
        account_doc = await accounts.find_one({"accountID": user_id}, {"_id": 0, "displayName": 1})
        if account_doc and isinstance(account_doc.get("displayName"), str) and account_doc["displayName"].strip():
            username = account_doc["displayName"].strip()
    if not username:
        username = user_id

    user_id = f"{user_id}${platform}${username}"

    print(f"[photon] Auth check ({request.method}) for UserId={user_id}")
    return JSONResponse(content={"ResultCode": 1, "UserId": user_id})
