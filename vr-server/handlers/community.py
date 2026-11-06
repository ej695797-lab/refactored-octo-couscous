"""
Community world handlers:
  - g2_cwGetCanEnter
  - g2_cwTrackVisit
  - g2_cwTryPurchaseFuel
  - g2_cwEditPermissions
"""

from services.db import room_maps


async def handle_cw_get_can_enter(body: dict) -> dict:
    out = {"canEnter": 1, "reason": ""}
    world_name = body.get("worldName", "")
    if world_name:
        from services.dynamo_ops import _cw_fetch_grid, _CW_WORLD
        base = f"c_{world_name}"
        await _cw_fetch_grid(base)              # fills the world info (owner, description)
        world = _CW_WORLD.get(base) or {}
        if world.get("ownerName"):
            out["ownerName"] = world["ownerName"]
        if world.get("description"):
            out["description"] = world["description"]
    return out


async def handle_cw_track_visit(body: dict) -> dict:
    # Visits are not stored: community worlds are read live from Yeeps (owner 2026-10-11).
    return {"success": 1}


async def handle_cw_try_purchase_fuel(body: dict) -> dict:
    return {
        "success": 1,
        "newCurrency": 0,
        "fuelExpireTimestamp": "2099 01/01 00:00:00",
        "cost": 0,
    }


async def handle_cw_edit_permissions(body: dict) -> dict:
    # Permissions are not stored: community worlds are read live from Yeeps (owner 2026-10-11).
    return {"success": 1}
