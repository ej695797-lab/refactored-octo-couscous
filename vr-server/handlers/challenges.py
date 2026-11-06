"""
Challenge & store rotation handlers:
  - g2_redeemChallengeReward
  - g2_fetchGlobalRotation
"""

from handlers.login import ALL_TECHWEB_ITEMS


async def handle_redeem_challenge_reward(body: dict) -> dict:
    return {
        "success": 1,
        "currencyAmount": 0,
        "newCurrency": 0,
        "items": [],
        "newLastChallengeRedeemedTime": "2099 01/01 00:00:00",
    }


async def techweb_items() -> list:
    """The techweb (build menu) item list. Remotely editable: globals key
    "techweb_items" (a list of pattern keys) overrides the built-in 1.19-era
    ALL_TECHWEB_ITEMS, so blocks can be added/removed without a redeploy."""
    try:
        from services.db import globals_col
        doc = await globals_col.find_one({"key": "techweb_items"}, {"_id": 0, "value": 1})
        if doc and isinstance(doc.get("value"), list) and doc["value"]:
            return doc["value"]
    except Exception as e:
        print(f"[techweb] override load failed: {e}")
    return ALL_TECHWEB_ITEMS


async def handle_fetch_global_rotation(body: dict) -> dict:
    rotation_key = body.get("rotationKey", "")
    # itemKeys maps a subcategory to its items:
    #   {<subcategory>: [{"value": int, "key": str, "maxCount": int?}]}
    # Returning {} leaves every item uncategorised, so nothing shows up in
    # the build menu even though the account owns it. Everything goes under
    # the base techWeb category for now; the real per-item subcategory
    # mapping (techWeb_beach, techWeb_paint, ...) isn't in the captured data.
    item_keys = {}
    if rotation_key == "techWeb":
        item_keys = {"techWeb": [{"value": 0, "key": k} for k in await techweb_items()]}

    result = {
        "rotationKey": rotation_key,
        "startTime": "1970 01/01 00:00:00",
        "expireTime": "",
        "itemKeys": item_keys,
    }

    # rotationKey="challenges" responses carry an extra streakUpdate object
    # with login-streak tier data -- missing it entirely makes the client
    # fail to parse the response and crash on load.
    if rotation_key == "challenges":
        result["streakUpdate"] = {
            "loginStreakData": {
                "length": 0,
                "currentTier": "bronze",
                "nextTierThreshold": 1,
                "nextTier": "bronze",
            },
            "itemKeysToRemove": [],
            "itemKeysToAdd": [],
        }

    return result
