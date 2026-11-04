"""Remove BLAZE-only blocks (BLAZE Pillow, BLAZE Screen) placed by accounts without the
"blaze" role (owner 2026-10-17; editor button "Clear BLAZE blocks not placed by blaze role").

Lossless: the SPAM block stream is a list of fixed 12-byte records after the key/colour/
owner tables, so offending records are cut out and every other byte (tables, other
blocks, trailing data) is kept. Saved sign/teleporter data (saveInternalStates) refers
to blocks by list position, so a map whose internal states are not empty is SKIPPED and
reported instead of risking misaligned signs. Every changed map is snapshotted to
map_history first (editor Revert). Covers this server's maps and the shared private
worlds in the 1.31 DB (CARRYOVER_DB).
"""
import base64
import os
import struct
import time

BLAZE_KEYS = {"1x5x2_stuffedTextBlaze", "3x2x1_blazeScreenRole"}


def _read_7bit(b, i):
    n = shift = 0
    while True:
        c = b[i]; i += 1
        n |= (c & 0x7F) << shift
        if not c & 0x80:
            return n, i
        shift += 7


def _tables(raw):
    """(keys, colors, owners, stream_len_offset) of a SPAM block blob."""
    pos = 5
    out = []
    for _ in range(3):
        cnt = struct.unpack_from("<i", raw, pos)[0]; pos += 4
        t = []
        for _ in range(cnt):
            n, pos = _read_7bit(raw, pos)
            t.append(raw[pos:pos + n].decode("utf-8", "replace")); pos += n
        out.append(t)
    return out[0], out[1], out[2], pos


def strip_blocks(raw: bytes, allowed_owners: set):
    """(new_raw, removed) with BLAZE records owned by non-holders cut out."""
    keys, _colors, owners, lpos = _tables(raw)
    blen = struct.unpack_from("<i", raw, lpos)[0]
    start = lpos + 4
    stream = raw[start:start + blen]
    keep = bytearray(); removed = 0
    for i in range(0, (len(stream) // 12) * 12, 12):
        ti = struct.unpack_from("<H", stream, i)[0]
        oi = struct.unpack_from("<H", stream, i + 10)[0]
        name = keys[ti] if ti < len(keys) else ""
        owner = owners[oi] if oi < len(owners) else ""
        if name in BLAZE_KEYS and owner not in allowed_owners:
            removed += 1
            continue
        keep += stream[i:i + 12]
    keep += stream[(len(stream) // 12) * 12:]
    if not removed:
        return raw, 0
    new = raw[:lpos] + struct.pack("<i", len(keep)) + bytes(keep) + raw[start + blen:]
    return new, removed


def _states_empty(isd: str) -> bool:
    if not isd:
        return True
    try:
        raw = base64.b64decode(isd)
        return raw[:4] == b"SPAM" and struct.unpack_from("<i", raw, 5)[0] == 0
    except Exception:
        return False


async def clear_blaze_blocks(apply: bool = False) -> dict:
    from services.db import client, db as _db
    dbs = [_db]
    if os.getenv("CARRYOVER_DB"):
        dbs.append(client[os.getenv("CARRYOVER_DB")])
    holders = set()
    for d in dbs:
        async for a in d["accounts"].find({"roleKeys": "blaze"}, {"_id": 0, "accountID": 1}):
            holders.add(a["accountID"])
    changed, skipped, total = [], [], 0
    for d in dbs:
        async for doc in d["maps"].find({"mapKey": {"$regex": r"\$current$"}}):
            sd = doc.get("saveData") or ""
            if not sd.startswith("U1BBTQ"):
                continue
            raw = base64.b64decode(sd)
            if b"stuffedTextBlaze" not in raw and b"blazeScreenRole" not in raw:
                continue
            try:
                new, n = strip_blocks(raw, holders)
            except Exception as e:
                skipped.append({"mapKey": doc["mapKey"], "why": f"could not read map: {e}"[:120]})
                continue
            if not n:
                continue
            rk = doc["mapKey"].rsplit("$", 1)[0]
            if not _states_empty(doc.get("saveInternalStates") or ""):
                skipped.append({"roomKey": rk, "blocks": n, "why": "has signs/teleporters (remove by hand)"})
                continue
            total += n
            changed.append({"roomKey": rk, "blocks": n})
            if not apply:
                continue
            snap = {k: v for k, v in doc.items() if k != "_id"}
            await d["map_history"].insert_one({"roomKey": rk, "ts": int(time.time()), "savedBy": snap.get("savedBy"),
                                               "savedAt": snap.get("savedAt"), "bytes": len(sd), "doc": snap,
                                               "preBlazeCleanup": True})
            upd = {"saveData": base64.b64encode(new).decode()}
            if isinstance(doc.get("editorBlocks"), list):
                upd["editorBlocks"] = [b for b in doc["editorBlocks"] if not (
                    isinstance(b, dict) and b.get("name") in BLAZE_KEYS and b.get("owner") not in holders)]
            await d["maps"].update_one({"_id": doc["_id"]}, {"$set": upd, "$unset": {"saveChunks": ""}})
            await d["rooms"].update_one({"roomKey": rk}, {"$set": {"lastEditedSessionID": int(time.time())}})
    return {"applied": apply, "blazeHolders": len(holders), "maps": len(changed), "blocks": total,
            "changed": changed, "skipped": skipped}
