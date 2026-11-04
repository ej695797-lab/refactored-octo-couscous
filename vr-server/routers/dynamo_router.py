"""
DynamoDB API router.
Handles POST requests with X-Amz-Target header.
"""

import json

from fastapi import APIRouter, Request
from fastapi.responses import Response

from services.dynamo_ops import dispatch_dynamo

router = APIRouter()


@router.post("/dynamodb")
@router.post("/")
async def dynamo_handler(request: Request):
    """
    Handle DynamoDB-style requests.
    The operation is determined by the X-Amz-Target header.
    """
    target = request.headers.get("x-amz-target", "")
    if not target:
        return Response(content="{}", media_type="application/json")

    try:
        body = await request.json()
    except Exception:
        body = {}

    op_name = target.rsplit(".", 1)[-1] if "." in target else target
    print(f"[dynamo] Operation: {op_name}")

    from services.ip_bans import client_ip
    payload_text, headers = await dispatch_dynamo(target, body, client_ip(request),
                                                  getattr(request.state, "device_key", ""))

    return Response(
        content=payload_text,
        media_type="application/x-amz-json-1.0",
        headers=headers,
    )
