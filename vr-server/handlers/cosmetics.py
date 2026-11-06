"""
Cosmetics handler: g2_putActiveCosmetics
"""

from services.db import accounts
from services.hair_colors import clamp_active_cosmetics


async def handle_put_active_cosmetics(body: dict) -> dict:
    account_id = body.get("accountID", "")
    active_cosmetics = body.get("activeCosmetics", [])

    # Limit hair colors to the allowed set (brown/black) on save.
    active_cosmetics = clamp_active_cosmetics(active_cosmetics)

    if account_id:
        await accounts.update_one(
            {"accountID": account_id},
            {"$set": {"activeCosmetics": active_cosmetics}},
        )

    return {"success": 1}
