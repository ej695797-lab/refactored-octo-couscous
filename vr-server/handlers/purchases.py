"""
1.31 purchase Lambdas (tryPurchase, tryPurchaseDirect, tryPurchasePrivateSandbox,
tryExpandPrivateWorld, tryPurchasePrivateRoomTheme/Name/LobbyDirection,
tryPurchasePet, tryPurchaseVIP, tryPurchaseBundle, tryPurchaseCreatorPack,
tryPurchaseCommunityWorld*, tryDeletePrivateRoom, ...).

None of these had a handler, so they fell to the generic fallback
({"success":1}). The client's purchase coroutines read "cost" (and
"currencyValueIndex" for item purchases) with GetInt, which throws when the key
is missing -> "Account: Failed to parse purchase response" -> every buttcoin
purchase failed. This handler answers all of them with the fields the client
reads and records ownership server-side.

The private world is free: the sandbox purchase and every private-world
expansion cost 0 and accounts default to hasUnlockedPrivateRooms=1.

Every call is logged to the "purchase_calls" collection (newest 500 kept) so the
exact request shapes can be checked from Mongo.
"""

from datetime import datetime, timezone

from services.db import accounts, db, get_collection

purchase_calls = db["purchase_calls"]

# Purchases that are always free on this server.
FREE_KINDS = ("privatesandbox", "expandprivateworld", "privateroom")


def _is_blocked(fl: str) -> bool:
    """Purchases that would add new rooms/worlds to the database are disabled
    so players can't fill the server. Renaming/retheming/deleting existing
    rooms and every other purchase still work."""
    if any(k in fl for k in ("theme", "name", "lobby", "fuel", "delete")):
        return False
    return ("trypurchaseprivateroom" in fl or "expandprivateworld" in fl
            or "trypurchasecommunityworld" in fl)


def _fn_name(body: dict) -> str:
    return str(body.get("_fn") or body.get("functionName") or "").strip()


def _int(v, default=0):
    try:
        return int(v)
    except Exception:
        return default


async def _log(fn: str, body: dict, resp: dict) -> None:
    try:
        clean = {k: v for k, v in body.items() if not str(k).startswith("_")}
        await purchase_calls.insert_one({
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "fn": fn, "body": clean, "resp": resp})
        n = await purchase_calls.estimated_document_count()
        if n > 600:
            old = purchase_calls.find({}, {"_id": 1}).sort("_id", 1).limit(n - 500)
            ids = [d["_id"] async for d in old]
            if ids:
                await purchase_calls.delete_many({"_id": {"$in": ids}})
    except Exception as e:
        print(f"[purchase] log failed: {e}")


async def handle_purchase(body: dict) -> dict:
    fn = _fn_name(body)
    fl = fn.lower()
    account_id = str(body.get("accountID") or "")
    if _is_blocked(fl):
        # No "cost" key -> the client treats the purchase as failed and
        # charges nothing.
        # The client's LambdaResponse reads statusCode + body.err_type; a body
        # without "cost" logged "Failed to parse purchase response".
        resp = {"_lambda_error": "ROOM_PURCHASES_DISABLED"}
        await _log(fn, body, resp)
        print(f"[purchase] BLOCKED {fn} {account_id}")
        return resp
    price = max(0, _int(body.get("expectedPrice", body.get("cost", 0))))
    if any(k in fl for k in FREE_KINDS):
        price = 0

    acct = None
    if account_id:
        acct = await accounts.find_one({"accountID": account_id},
                                       {"_id": 0, "currency": 1, "ownedPatterns": 1})
    currency = _int((acct or {}).get("currency"), 0)
    # The error-response format for purchases isn't known, so never refuse:
    # a short balance just bottoms out at 0 (accounts start with 999999999).

    upd = {}
    add = {}
    if acct is not None and price:
        upd["currency"] = max(0, currency - price)
    if "privatesandbox" in fl or "expandprivateworld" in fl:
        upd["hasUnlockedPrivateRooms"] = 1
    if "creatorpack" in fl:
        upd["hasCreatorPack"] = 1
    item_key = body.get("itemKey")
    if isinstance(item_key, str) and item_key and ("trypurchase" == fl or "direct" in fl
                                                  or fl.endswith("trypurchase")):
        add["ownedPatterns"] = item_key
    bundle_key = body.get("bundleKey")
    if isinstance(bundle_key, str) and bundle_key and "bundle" in fl:
        add["ownedBundles"] = bundle_key

    if acct is not None and (upd or add):
        op = {}
        if upd:
            op["$set"] = upd
        if add:
            op["$addToSet"] = add
        await accounts.update_many({"accountID": account_id}, op)

    if account_id and ("privateroom" in fl or "expandprivateworld" in fl):
        try:
            await _private_room_op(fl, account_id, body)
        except Exception as e:
            print(f"[purchase] private room op failed for {account_id}: {e}")

    resp = {
        "success": 1,
        "cost": price,
        "currencyValueIndex": 0,
        "newCurrency": max(0, currency - price) if acct is not None else 0,
    }
    # The BUNDLE purchase coroutine (AccountManager.TryPurchaseBundle) parses a
    # DIFFERENT shape than item purchases: it ContainsKey/GetInt currencyAmount +
    # centCost and GetStringList("items"); missing any one -> INVALID_FORMAT ->
    # "Account: Failed to parse purchase response" (the per-login spam). It does
    # NOT read cost/currencyValueIndex. currencyAmount stays 0 (the client may add
    # it to the local balance, and ~30 calls/login could overflow int32 since we
    # already serve 999999999). items=[] is safe — the bundle's cosmetics are
    # already granted via ownedBundles/ownedPatterns. (Field map from the best
    # session's 1.19 disassembly; the 1.31 request + error strings are identical.)
    if "bundle" in fl:
        resp["currencyAmount"] = 0
        resp["centCost"] = 0
        resp["items"] = []
    await _log(fn, body, resp)
    print(f"[purchase] {fn or '?'} {account_id} price={price} item={item_key or '-'}")
    return resp


# ─── Private world grid edits ────────────────────────────────────────
# A side is open between two grid cells only when neither node blocks it.
_SIDES = {"Left": (-1, 0, "Right"), "Right": (1, 0, "Left"),
          "Up": (0, 1, "Down"), "Down": (0, -1, "Up")}


def _gp(v):
    try:
        return int(v[0]), int(v[1])
    except Exception:
        return None


def _room_key(base: str, x: int, y: int) -> str:
    sx = f"m{-x}" if x < 0 else str(x)
    sy = f"m{-y}" if y < 0 else str(y)
    return f"{base}_r{sx}_{sy}"


async def _load_pw_map(base: str) -> dict:
    col = get_collection("G2_RoomMap")
    doc = await col.find_one({"versionKey": base}, {"_id": 0})
    if not doc:
        from services.dynamo_ops import _pw_room_map
        doc = await _pw_room_map(base)
    return doc


async def _save_pw_map(doc: dict) -> None:
    col = get_collection("G2_RoomMap")
    await col.update_one({"versionKey": doc["versionKey"]},
                         {"$set": {"data": doc["data"], "version": doc.get("version", 1)}},
                         upsert=True)


async def _private_room_op(fl: str, account_id: str, body: dict) -> None:
    base = f"p_{account_id}"
    pos = _gp(body.get("gridPosition"))
    if pos is None:
        return
    doc = await _load_pw_map(base)
    nodes = doc.get("data") or []
    by_pos = {_gp(n.get("gridPosition")): n for n in nodes}
    node = by_pos.get(pos)
    rooms = get_collection("G2_Rooms")

    if "delete" in fl:
        # Never delete the doorway, the sandbox or the main room.
        if not node or node.get("transitionKey") or node.get("roomKey") in (base, f"{base}_sandbox"):
            return
        nodes.remove(node)
        x, y = pos
        for side, (dx, dy, opp) in _SIDES.items():
            nb = by_pos.get((x + dx, y + dy))
            if nb is not None:
                nb[f"block{opp}"] = 1
        doc["data"] = nodes
        await _save_pw_map(doc)
        print(f"[pw] {account_id} deleted room at {pos}")
        return

    if any(k in fl for k in ("theme", "name", "lobby")):
        if not node or not node.get("roomKey"):
            return
        rk = node["roomKey"]
        upd = {}
        if "theme" in fl and body.get("themeKey"):
            upd["themeKey"] = node["themeKey"] = str(body["themeKey"])
            await _save_pw_map(doc)
        if "name" in fl and body.get("roomName"):
            upd["roomName"] = str(body["roomName"])[:40]
        if "lobby" in fl:
            for k in ("lobbyDirection", "direction", "newLobbyDirection"):
                if k in body:
                    try:
                        upd["lobbyDirection"] = int(body[k])
                    except Exception:
                        pass
                    break
        if upd:
            exists = await rooms.find_one({"roomKey": rk}, {"_id": 1})
            if not exists:
                from services.dynamo_ops import _get_fallback_doc
                base_doc = await _get_fallback_doc("G2_Rooms", {"roomKey": rk}) or {}
                base_doc.update(upd)
                await rooms.insert_one(base_doc)
            else:
                await rooms.update_one({"roomKey": rk}, {"$set": upd})
            print(f"[pw] {account_id} room {rk} <- {upd}")
        return

    # Purchase/expand: add a new room at pos.
    if node is not None:
        return
    theme = str(body.get("themeKey") or "sandbox")
    rk = _room_key(base, *pos)
    new = {"roomKey": rk, "themeKey": theme, "gridPosition": [pos[0], pos[1]]}
    x, y = pos
    for side, (dx, dy, opp) in _SIDES.items():
        nb = by_pos.get((x + dx, y + dy))
        if nb is None:
            new[f"block{side}"] = 1
        else:
            nb.pop(f"block{opp}", None)
    nodes.append(new)
    doc["data"] = nodes
    await _save_pw_map(doc)
    if not await rooms.find_one({"roomKey": rk}, {"_id": 1}):
        from services.dynamo_ops import _full_room
        name = str(body.get("roomName") or f"Room {len(nodes) - 1}")[:40]
        await rooms.insert_one(_full_room(rk, name, [80, 80, 80], theme=theme))
    print(f"[pw] {account_id} added {theme} room {rk} at {pos}")
