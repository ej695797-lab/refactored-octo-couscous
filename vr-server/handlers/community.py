"""
Community world handlers:
  - g2_cwGetCanEnter
  - g2_cwTrackVisit
  - g2_cwTryPurchaseFuel
  - g2_cwEditPermissions
"""

from services.db import room_maps


async def handle_cw_get_can_enter(body: dict) -> dict:
    return {"canEnter": 1, "reason": ""}


async def handle_cw_track_visit(body: dict) -> dict:
    world_name = body.get("worldName", "")
    if world_name:
        version_key = f"c_{world_name}"
        await room_maps.update_one(
            {"versionKey": version_key},
            {"$inc": {"cw_visits_total": 1}},
        )
    return {"success": 1}


async def handle_cw_try_purchase_fuel(body: dict) -> dict:
    return {
        "success": 1,
        "newCurrency": 0,
        "fuelExpireTimestamp": "2099 01/01 00:00:00",
        "cost": 0,
    }


async def handle_cw_edit_permissions(body: dict) -> dict:
    world_name = body.get("worldName", "")
    new_permissions = body.get("newPermissions", 0)
    if world_name:
        version_key = f"c_{world_name}"
        await room_maps.update_one(
            {"versionKey": version_key},
            {"$set": {"cw_permissions": new_permissions}},
        )
    return {"success": 1}
