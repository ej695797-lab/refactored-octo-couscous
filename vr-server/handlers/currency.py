"""
Currency handler: g2_devResetCurrency
"""

from services.db import accounts


async def handle_dev_reset_currency(body: dict) -> dict:
    account_id = body.get("accountID", "")
    # Unlimited currency on this server: a client "reset" never lowers it.
    try:
        new_currency = max(int(body.get("newCurrency", 0) or 0), 999999999)
    except Exception:
        new_currency = 999999999

    if account_id:
        await accounts.update_one(
            {"accountID": account_id},
            {"$set": {"currency": new_currency}},
        )

    return {"success": 1, "newCurrency": new_currency}
