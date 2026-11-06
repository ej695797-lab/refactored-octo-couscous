"""
Give map blocks whose owner account doesn't exist in the database (official
Yeeps players from copied maps, bot accounts, "testing", ...) to the server
owner's account (DEFAULT_MAP_OWNER).

Lossless: only the owner-ID strings in the save blob's ownerUserIDs table are
rewritten. The 12-byte block records (which point into that table by index),
keys, colours, and every byte after the table stay exactly as they were, so the
room's saveInternalStates (signs, dispensers, teleporters) still line up.

Works on both stored formats:
  * 1.31 SPAM ("U1BBTQ..."): "SPAM" u8 | keys[] | colorKeys[] | ownerUserIDs[] | stream
  * 1.19 MapSyncDataCompressed (BinaryFormatter, "AAEAAAD..."): the 3rd string
    array record holds ownerUserIDs.
"""

import base64
import struct

from config import DEFAULT_MAP_OWNER


def _read_7bit(b: bytes, i: int):
    n = shift = 0
    while True:
        x = b[i]; i += 1
        n |= (x & 0x7F) << shift
        if not (x & 0x80):
            return n, i
        shift += 7


def _bw(s: str) -> bytes:
    """.NET BinaryWriter / NRBF length-prefixed UTF-8 string."""
    data = s.encode("utf-8")
    n, out = len(data), bytearray()
    while True:
        x = n & 0x7F
        n >>= 7
        if n:
            out.append(x | 0x80)
        else:
            out.append(x)
            break
    return bytes(out) + data


def _str_at(b: bytes, i: int):
    """(value, start, end) of the length-prefixed string at i."""
    n, j = _read_7bit(b, i)
    return b[j:j + n].decode("utf-8", "replace"), i, j + n


def _spam_owner_slots(raw: bytes) -> list:
    """[(value, start, end)] for each ownerUserIDs entry of a SPAM blob."""
    pos = 5

    def skip_table(p):
        n = struct.unpack_from("<i", raw, p)[0]; p += 4
        for _ in range(n):
            _, _, p = _str_at(raw, p)
        return p

    pos = skip_table(pos)          # keys
    pos = skip_table(pos)          # colorKeys
    n = struct.unpack_from("<i", raw, pos)[0]; pos += 4
    slots = []
    for _ in range(n):
        v, s, e = _str_at(raw, pos)
        slots.append((v, s, e))
        pos = e
    return slots


def _bf_owner_slots(raw: bytes) -> list:
    """[(value, start, end)] for each ownerUserIDs entry of a 1.19
    MapSyncDataCompressed blob: the strings of the 3rd string-array record.
    Entries that are references to a string defined elsewhere are left out
    (they keep their value)."""
    i = 0
    arrays = 0
    sizes = {1: 1, 2: 1, 3: 2, 4: 2, 5: 1, 6: 8, 7: 8, 8: 4, 9: 8, 10: 4, 11: 1, 13: 8, 14: 4, 15: 8, 16: 8}

    def i32():
        nonlocal i
        v = struct.unpack_from("<i", raw, i)[0]; i += 4; return v

    def u8():
        nonlocal i
        v = raw[i]; i += 1; return v

    def skip_str():
        nonlocal i
        _, _, i = _str_at(raw, i)

    def skip_prim(pt):
        nonlocal i
        if pt == 18:
            skip_str()
        else:
            i += sizes.get(pt, 0)

    while i < len(raw):
        tag = u8()
        if tag == 0:                       # header
            i += 16
        elif tag == 12:                    # BinaryLibrary
            i32(); skip_str()
        elif tag == 11:                    # MessageEnd
            break
        elif tag == 6:                     # BinaryObjectString
            i32(); skip_str()
        elif tag == 10:
            pass
        elif tag == 13:
            u8()
        elif tag == 14:
            i32()
        elif tag == 9:
            i32()
        elif tag == 15:                    # ArraySinglePrimitive
            i32(); length = i32(); pt = u8()
            if pt == 2:
                i += length
            else:
                for _ in range(length):
                    skip_prim(pt)
        elif tag == 16:                    # ArraySingleObject
            i32(); i32()
        elif tag == 17:                    # ArraySingleString
            i32(); length = i32()
            arrays += 1
            slots = []
            k = 0
            while k < length:
                t = u8()
                if t == 6:
                    i32()
                    v, s, e = _str_at(raw, i)
                    slots.append((v, s, e)); i = e; k += 1
                elif t == 13:
                    k += u8()
                elif t == 14:
                    k += i32()
                elif t == 10:
                    k += 1
                elif t == 9:
                    i32(); k += 1
                else:
                    raise ValueError(f"unexpected record {t} in string array")
            if arrays == 3:
                return slots
        elif tag == 5:                     # ClassWithMembersAndTypes
            i32(); skip_str()
            mc = i32()
            for _ in range(mc):
                skip_str()
            bts = [u8() for _ in range(mc)]
            prims = []
            for bt in bts:
                if bt == 0:
                    prims.append(u8())
                else:
                    prims.append(None)
                    if bt == 3:
                        skip_str()
                    elif bt == 4:
                        skip_str(); i32()
                    elif bt == 7:
                        u8()
            i32()                          # libraryId
            for bt, pr in zip(bts, prims):
                if bt == 0:
                    skip_prim(pr)
        else:
            raise ValueError(f"unsupported record {tag} before the owner table")
    return []        # empty map: the string members all point at one empty array


def owner_slots(save_b64: str):
    """(format, raw bytes, [(value, start, end)]) or (None, None, []) if the
    blob isn't a map format we can rewrite."""
    if not save_b64 or not isinstance(save_b64, str):
        return None, None, []
    raw = base64.b64decode(save_b64)
    if raw[:4] == b"SPAM":
        return "spam", raw, _spam_owner_slots(raw)
    if save_b64.startswith("AAEAAAD") and b"MapSyncDataCompressed" in raw[:400]:
        return "bf", raw, _bf_owner_slots(raw)
    return None, None, []


def owner_ids(save_b64: str) -> set:
    try:
        _, _, slots = owner_slots(save_b64)
    except Exception:
        return set()
    return {v for v, _, _ in slots}


def rewrite_owners(save_b64: str, mapping: dict):
    """Return (new_save_b64, entries_changed). Only the owner strings named in
    `mapping` change; every other byte is copied as-is."""
    fmt, raw, slots = owner_slots(save_b64)
    todo = [(v, s, e) for v, s, e in slots if v in mapping and mapping[v] != v]
    if not todo:
        return save_b64, 0
    out = bytearray()
    last = 0
    for v, s, e in todo:                   # slots are in byte order
        out += raw[last:s]
        out += _bw(mapping[v])
        last = e
    out += raw[last:]
    new_b64 = base64.b64encode(bytes(out)).decode("ascii")
    _verify(save_b64, new_b64, mapping)
    return new_b64, len(todo)


def _verify(old_b64: str, new_b64: str, mapping: dict):
    """Every block must decode the same except its owner, which must be the
    mapped one. Raises if anything else moved."""
    from services.mapsync_decoder import decode_map
    a, b = decode_map(old_b64), decode_map(new_b64)
    if len(a) != len(b) or not a:
        raise ValueError(f"block count changed ({len(a)} -> {len(b)})")
    for x, y in zip(a, b):
        for k in ("name", "x", "y", "z", "fwd", "up", "color"):
            if x.get(k) != y.get(k):
                raise ValueError(f"block field {k} changed")
        if y.get("owner") != mapping.get(x.get("owner"), x.get("owner")):
            raise ValueError("owner not mapped as expected")


def unknown_mapping(ids, known: set, default: str = DEFAULT_MAP_OWNER) -> dict:
    """{unknown id: default} for every non-empty id that isn't an account."""
    return {o: default for o in ids if o and o != default and o not in known}


async def known_accounts(ids) -> set:
    from services.db import accounts
    ids = [o for o in ids if o]
    if not ids:
        return set()
    return {d["accountID"] async for d in accounts.find({"accountID": {"$in": ids}}, {"_id": 0, "accountID": 1})}


async def default_unknown_owners(save_b64: str, default: str = DEFAULT_MAP_OWNER):
    """For a save coming in: give blocks of non-existent accounts to `default`.
    Returns (save_b64, entries_changed); the input is returned untouched on any
    problem."""
    ids = owner_ids(save_b64)
    if not ids:
        return save_b64, 0
    mapping = unknown_mapping(ids, await known_accounts(ids), default)
    if not mapping:
        return save_b64, 0
    return rewrite_owners(save_b64, mapping)


async def default_block_owners(blocks: list, default: str = DEFAULT_MAP_OWNER) -> int:
    """Same for an editor block list (dicts with "owner"), in place."""
    ids = {str(b.get("owner") or "") for b in blocks if isinstance(b, dict)}
    mapping = unknown_mapping(ids, await known_accounts(ids), default)
    n = 0
    for b in blocks:
        if isinstance(b, dict) and b.get("owner") in mapping:
            b["owner"] = mapping[b["owner"]]; n += 1
    return n


async def fix_all_maps(apply: bool = False, default: str = DEFAULT_MAP_OWNER) -> dict:
    """Every stored map ($current saves): give blocks of non-existent accounts
    to `default`. With apply=False nothing is written (a report only). Each
    changed map is snapshotted to map_history first, so the editor's Revert can
    undo it."""
    import time as _t
    from services.db import maps, accounts, db as _db
    from services.mapsync_decoder import decode_map
    known = {a["accountID"] async for a in accounts.find({}, {"_id": 0, "accountID": 1}) if a.get("accountID")}
    changed, failed = [], []
    total_blocks = total_entries = 0
    async for doc in maps.find({}):
        sd = doc.get("saveData") or ""
        try:
            ids = owner_ids(sd)
            mapping = unknown_mapping(ids, known, default)
            if not mapping:
                continue
            new_sd, n = rewrite_owners(sd, mapping)
            if not n:
                continue
            nblocks = sum(1 for b in decode_map(sd) if b.get("owner") in mapping)
        except Exception as e:
            failed.append({"mapKey": doc.get("mapKey"), "error": str(e)[:200]})
            continue
        total_blocks += nblocks
        total_entries += n
        changed.append({"roomKey": str(doc.get("mapKey", "")).rsplit("$", 1)[0], "blocks": nblocks,
                        "accounts": len(mapping)})
        if not apply:
            continue
        snap = {k: v for k, v in doc.items() if k != "_id"}
        await _db["map_history"].insert_one({"roomKey": snap.get("roomKey") or changed[-1]["roomKey"],
                                             "ts": int(_t.time()), "savedBy": snap.get("savedBy"),
                                             "savedAt": snap.get("savedAt"), "bytes": len(sd),
                                             "doc": snap, "preOwnerFix": True})
        upd = {"saveData": new_sd}
        if isinstance(doc.get("editorBlocks"), list):
            eb = doc["editorBlocks"]
            for b in eb:
                if isinstance(b, dict) and b.get("owner") in mapping:
                    b["owner"] = mapping[b["owner"]]
            upd["editorBlocks"] = eb
        await maps.update_one({"_id": doc["_id"]}, {"$set": upd})
    return {"applied": apply, "defaultOwner": default, "maps": len(changed), "blocks": total_blocks,
            "ownerEntries": total_entries, "changed": sorted(changed, key=lambda c: -c["blocks"]),
            "failed": failed}
