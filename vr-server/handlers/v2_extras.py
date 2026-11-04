"""
2.4.0 Lambdas that used to hit the generic fallback (reply "success" but
nothing was saved): passageway links, saved outfits, pet name/location,
anonymous mode. Each only writes the caller's own account (the router already
checked the headset key), except passageway links, which edit a room.
"""
from services.db import accounts, rooms

OK = {"data": {"success": 1}}
_EDIT_ROLES = {"developer", "developer_senior", "moderator", "moderator_senior", "blaze"}


async def _own(body):
    aid = str(body.get("accountID") or "")
    return aid if aid and await accounts.count_documents({"accountID": aid}, limit=1) else ""


async def handle_save_room_passageway_links(body: dict) -> dict:
    """Links between rooms (which passageway leads where), saved in creative
    mode. Stored on the room doc as passagewayLinks_creative."""
    aid = await _own(body)
    room_key = str(body.get("roomKey") or "")
    links = body.get("passagewayLinks")
    if not aid or not room_key or not isinstance(links, list) or len(links) > 200:
        return {"data": {"success": 0}}
    acct = await accounts.find_one({"accountID": aid}, {"_id": 0, "roleKeys": 1})
    owner_room = room_key == f"p_o_{aid}" or room_key.startswith(f"p_o_{aid}_")
    if not owner_room and not (_EDIT_ROLES & set((acct or {}).get("roleKeys") or [])):
        print(f"[v2] passageway links refused: {aid} on {room_key}")
        return {"data": {"success": 0}}
    clean = [l for l in links if isinstance(l, dict)]
    # The client reads these back as G2_Rooms.passagewayLinks_creative (its
    # GetItem attribute list); play mode keeps using the room-map graph links.
    r = await rooms.update_one({"roomKey": room_key}, {"$set": {"passagewayLinks_creative": clean},
                                                      "$unset": {"passagewayLinks": ""}})
    print(f"[v2] passageway links {room_key}: {len(clean)} by {aid} (matched={r.matched_count})")
    return OK


async def handle_put_saved_outfits(body: dict) -> dict:
    aid = await _own(body)
    outfits = body.get("savedOutfits")
    if aid and isinstance(outfits, list) and len(outfits) <= 50:
        outfits = [[c for c in o if isinstance(c, str)][:30] for o in outfits if isinstance(o, list)]
        await accounts.update_one({"accountID": aid}, {"$set": {"savedOutfits": outfits}})
    return OK


async def handle_remove_saved_outfit(body: dict) -> dict:
    aid = await _own(body)
    gone = body.get("outfitToRemove")
    if aid and isinstance(gone, list):
        await accounts.update_one({"accountID": aid}, {"$pull": {"savedOutfits": gone}})
    return OK


async def _pet_set(body, fields):
    aid = await _own(body)
    pet = str(body.get("petKey") or "")
    if not aid or not pet or "." in pet or "$" in pet:
        return {"data": {"success": 0}}
    await accounts.update_one({"accountID": aid, f"pets.{pet}": {"$exists": True}},
                              {"$set": {f"pets.{pet}.{k}": v for k, v in fields.items()}})
    return OK


async def handle_put_pet_name(body: dict) -> dict:
    return await _pet_set(body, {"name": str(body.get("petName") or "")[:40]})


async def handle_put_pet_location(body: dict) -> dict:
    pos = body.get("position")
    if not (isinstance(pos, list) and len(pos) == 3 and all(isinstance(x, (int, float)) for x in pos)):
        return {"data": {"success": 0}}
    return await _pet_set(body, {"location_roomKey": str(body.get("roomKey") or ""),
                                 "location_position": pos})


async def handle_put_anonymous_mode(body: dict) -> dict:
    aid = await _own(body)
    if aid:
        upd = {"anonymous": 1 if body.get("isOn") else 0,
               "anonymous_displayName": str(body.get("name") or "")[:40]}
        for k in ("skinColor", "eyeColor"):
            v = body.get(k)
            if isinstance(v, int) and -2**31 <= v < 2**31:
                upd[f"anonymous_{k}"] = v
        await accounts.update_one({"accountID": aid}, {"$set": upd})
    return OK
