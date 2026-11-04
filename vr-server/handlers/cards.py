"""
Claim / reward handlers for the live 2.31.4 client (2026-10-17).

Every one of these used to answer {"success": 1} or zeros, which the client
rejected as INVALID_FORMAT. The live30 [jsonmiss] diagnostic showed the reply
fields the client actually reads:
  g2_redeemChallengeReward / g2_redeemTradingCardReward / g2_tryRedeemCurrencyStash
      -> "reward"
  g2_tryPurchaseTradingCardPack -> "newCardKeys"
Inner field names of "reward" are not known yet, so a superset of the likely
names is sent (unread keys are harmless; [jsonmiss] reports any still missing).

Currency (buttcoins) is the account field "currency"; beans = "beans". Both are
topped up at login on this server, but rewards still add to the stored balance
and return the real new balance + the real amount, so the client shows e.g.
"+170 butt coins" instead of 0.
"""
from datetime import datetime, timezone

from services.db import accounts

STASH_AMOUNT = 100          # butt coins per stash
CARD_PACK_SIZE = 100        # cards per pack (owner 2026-10-17: 100)
GOLDEN_CHANCE = 0.10        # chance each card is the golden (legendary) variant: key + "_legendary"
LEGENDARY_SUFFIX = "_legendary"   # TradingCardManager.legendaryKeySuffix


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y %m/%d %H:%M:%S")


async def _add(acct: str, field: str, amount: int) -> int:
    """Add `amount` to the account's `field` and return the new balance."""
    if not acct:
        return amount
    doc = await accounts.find_one_and_update(
        {"accountID": acct}, {"$inc": {field: int(amount)}},
        projection={"_id": 0, field: 1}, return_document=True)
    try:
        return int((doc or {}).get(field) or amount)
    except Exception:
        return amount


def _reward(kind: str, key: str, amount: int) -> dict:
    """Reward object. Superset of plausible field names (see module doc)."""
    return {"type": kind, "rewardType": kind, "key": key, "itemKey": key,
            "amount": amount, "value": amount, "count": amount, "quantity": amount,
            "currencyAmount": amount if kind == "currency" else 0}


async def _challenge_value(pool: str, item_key: str) -> int:
    """The challenge's value from today's daily rotation (login = 170 etc.)."""
    try:
        from handlers.challenges import _daily_challenges
        cur = await _daily_challenges()
        for row in (cur.get("itemKeys") or {}).get(pool) or []:
            if row.get("key") == item_key:
                return int(row.get("value") or 0)
        for rows in (cur.get("itemKeys") or {}).values():
            for row in rows or []:
                if row.get("key") == item_key:
                    return int(row.get("value") or 0)
    except Exception as e:
        print(f"[cards] challenge value lookup failed: {e}")
    return 0


async def _capture(fn: str, body: dict, reply: dict) -> None:
    try:
        from services.db import db
        clean = {k: v for k, v in body.items() if not str(k).startswith("_")}
        await db["card_capture"].insert_one({"at": _now(), "kind": "claim", "fn": fn,
                                             "body": clean, "reply": reply})
    except Exception:
        pass


async def handle_redeem_challenge_reward(body: dict) -> dict:
    """Daily tab claim (poolKey login/easy/hard, itemKey, isStreak) -> butt coins."""
    acct = str(body.get("accountID") or "")
    pool = str(body.get("poolKey") or "")
    item = str(body.get("itemKey") or "")
    amount = await _challenge_value(pool, item) or 50
    new_cur = await _add(acct, "currency", amount)
    if acct:
        await accounts.update_one({"accountID": acct},
                                  {"$set": {"lastChallengeRedeemedTime": _now()},
                                   "$addToSet": {"redeemedChallengeKeys": f"{pool}:{item}"}})
    reply = {"success": 1, "reward": _reward("currency", "currency", amount),
             "currencyAmount": amount, "newCurrency": new_cur,
             "items": [], "newLastChallengeRedeemedTime": _now()}
    await _capture("redeemChallengeReward", body, reply)
    print(f"[cards] {acct} redeemed challenge {pool}:{item} -> +{amount} coins (now {new_cur})")
    return reply


async def handle_redeem_trading_card_reward(body: dict) -> dict:
    """Card challenge board claim (itemKey = the card challenge) -> a card pack."""
    acct = str(body.get("accountID") or "")
    item = str(body.get("itemKey") or "")
    if acct:
        await accounts.update_one({"accountID": acct},
                                  {"$set": {"lastCardChallengeRedeemedTime": _now()},
                                   "$addToSet": {"redeemedCardChallengeKeys": item}})
    # "reward" as an object failed to parse with no missing keys -> the client
    # reads it as a value: the reward item key (TRADING_CARD_PACK_ITEM_KEY).
    reply = {"success": 1, "reward": "tradingCardPack",
             "newLastCardChallengeRedeemedTime": _now()}
    await _capture("redeemTradingCardReward", body, reply)
    print(f"[cards] {acct} redeemed card challenge {item} -> 1 card pack")
    return reply


async def handle_try_redeem_currency_stash(body: dict) -> dict:
    """Butt-coin stash in a room -> butt coins."""
    acct = str(body.get("accountID") or "")
    stash = str(body.get("stashKey") or "")
    amount = STASH_AMOUNT
    new_cur = await _add(acct, "currency", amount)
    if acct and stash:
        await accounts.update_one({"accountID": acct},
                                  {"$addToSet": {"redeemedStashIDs": f"{body.get('roomKey', '')}:{stash}"}})
    reply = {"success": 1, "reward": _reward("currency", "currency", amount),
             "currencyAmount": amount, "newCurrency": new_cur}
    await _capture("tryRedeemCurrencyStash", body, reply)
    print(f"[cards] {acct} stash {stash} -> +{amount} coins (now {new_cur})")
    return reply


async def card_catalog() -> list:
    """Card keys a pack can contain. Source still being identified (the client
    holds no card definitions in its data); editable via globals key
    "trading_card_keys" (list of keys) until the real catalog is known."""
    try:
        from services.db import globals_col
        doc = await globals_col.find_one({"key": "trading_card_keys"}, {"_id": 0, "value": 1})
        v = (doc or {}).get("value")
        if isinstance(v, list) and v:
            return [str(x) for x in v]
    except Exception:
        pass
    # The client's own card list: TradingCardManager.cardConfigs keys, read from the
    # live data (level2 object 477, script globalgamemanagers 3880) -> 402 keys.
    try:
        import json as _j, os as _o
        f = _o.path.join(_o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))), "data", "trading_card_keys.json")
        keys = _j.load(open(f))
        if keys:
            return [str(k) for k in keys]
    except Exception as e:
        print(f"[cards] card list load failed: {e}")
    return []


_bad_cache = {}


def _bad_cards():
    """(base keys, golden keys) the live clients reject as "invalid or unregistered
    trading card key" and destroy (data/bad_card_keys.json, from the client logs)."""
    import json as _j, os as _o
    if not _bad_cache:
        f = _o.path.join(_o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))), "data", "bad_card_keys.json")
        try:
            d = _j.load(open(f))
            _bad_cache["v"] = (set(d.get("base") or []), set(d.get("legendary") or []))
        except Exception:
            _bad_cache["v"] = (set(), set())
    return _bad_cache["v"]


async def handle_try_purchase_trading_card_pack(body: dict) -> dict:
    """Buy a trading card pack (isBeans=1 -> beans, else butt coins)."""
    import random
    acct = str(body.get("accountID") or "")
    is_beans = bool(int(body.get("isBeans") or 0))
    price = max(0, int(body.get("expectedPrice") or 0))
    field = "beans" if is_beans else "currency"
    new_bal = await _add(acct, field, -price)
    bad_base, bad_gold = _bad_cards()
    # dev cards (OwnerName + Staff Member A only, owner 2026-10-18) never come from packs,
    # even if someone adds them to the globals catalog
    _dev = {"staffcarda", "staffcardb", "yeepswholarpstuff", "bluemagnifyingglass"}
    cat = [c for c in await card_catalog() if c not in bad_base and c not in _dev]
    # pack size: editor Daily tab -> globals "card_pack_size" (min 1, no max), else CARD_PACK_SIZE
    size = CARD_PACK_SIZE
    try:
        from services.db import globals_col as _gc
        _ps = await _gc.find_one({"key": "card_pack_size"}, {"_id": 0, "data": 1})
        if _ps and _ps.get("data") is not None:
            size = max(1, int(_ps["data"]))
    except Exception as _e:
        print(f"[cards] pack size lookup failed: {_e}")
    cards = random.sample(cat, size) if len(cat) >= size else [random.choice(cat) for _ in range(size)] if cat else []
    # Golden (legendary) variants: the client's GetLegendaryCardKey = key + "_legendary".
    cards = [c + LEGENDARY_SUFFIX if random.random() < GOLDEN_CHANCE and c not in bad_gold else c for c in cards]
    # Binder persistence DISABLED 2026-10-17: serving accountTradingCardsData as
    # {cardKey: count} made the client NRE in TradingCardManager.ExtractTradingCardData
    # -> "Failed to load game account" for everyone. Real shape still unknown.
    if acct:
        upd = {"$inc": {"beanPurchasedCardCount": 1}} if is_beans else {}
        upd.setdefault("$set", {})["lastCardBeanPurchaseTime" if is_beans else "lastCardPurchaseTime"] = _now()
        await accounts.update_one({"accountID": acct}, upd)
    reply = {"success": 1, "cost": price, "currencyValueIndex": 1 if is_beans else 0,
             "newCurrency": new_bal, "newCardKeys": cards}
    await _capture("tryPurchaseTradingCardPack", body, reply)
    print(f"[cards] {acct} bought a card pack ({'beans' if is_beans else 'coins'} {price}) -> {len(cards)} cards")
    return reply
