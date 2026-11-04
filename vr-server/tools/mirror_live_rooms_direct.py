"""Read-only mirror of live-Yeeps rooms via services/yeeps_direct (no /AWS relay).

Same output format as tools/mirror_live_world.py (the relay it uses is gone), so
tools/import_live_world.py --rooms-only can import it.
Usage: python3 tools/mirror_live_rooms_direct.py <roomMapKey> <keep.json> <out.json>
"""
import asyncio
import json
import sys
import time

sys.path.insert(0, ".")
from services import yeeps_direct  # noqa: E402
from tools.mirror_live_world import ROOM_PROPS  # noqa: E402

POOL = ["cw_fetch_account3", "cw_fetch_account6", "cw_fetch_account7", "cw_fetch_account8"]


async def main():
    map_key, keep_path, out_path = sys.argv[1:4]
    keep = [r["key"] for r in json.load(open(keep_path))["rooms"]]
    calls = {"n": 0}

    async def call(fn, i, **extra):
        last = None
        for k in range(len(POOL)):
            acct = POOL[(i + k) % len(POOL)]
            try:
                calls["n"] += 1
                return await yeeps_direct.invoke(fn, extra, account=acct)
            except Exception as e:
                last = e
                await asyncio.sleep(1)
        print(f"   {fn} failed: {last}")
        return {}

    async def chunks(i, rk, sid, terrain):
        out, ver, internal = [], None, ""
        for off in range(60):
            d = await call("fetchMapData", i, roomKey=rk, targetSessionID=sid, saveOffset=off,
                           isTerrain=1 if terrain else 0)
            sd = (d or {}).get("saveData")
            if not sd or (out and sd == out[-1]):
                break
            if ver is None:
                ver = d.get("mapDataVersion")
            if off == 0:
                internal = d.get("saveInternalStates") or ""
            out.append(sd)
        return out, ver, internal

    rooms, maps = {}, {}
    for i, rk in enumerate(keep, 1):
        rd = await call("fetchRoomData", i, roomKey=rk, propertiesToGet=ROOM_PROPS) or {}
        rooms[rk] = rd
        b, bv, internal = await chunks(i, rk, rd.get("lastEditedSessionID") or 0, False)
        t, tv, _ = await chunks(i, rk, rd.get("lastEditedTerrainSessionID") or 0, True)
        maps[rk] = {"blocks": b, "blocksVersion": bv, "internalStates": internal,
                    "terrain": t, "terrainVersion": tv}
        print(f"[{i}/{len(keep)}] {rk:28} {rd.get('roomName')!s:26} blocks {len(b)} chunk(s) "
              f"{sum(len(c) for c in b)}b | terrain {len(t)}", flush=True)
        await asyncio.sleep(0.3)
    json.dump({"roomMapKey": map_key, "fetchedAt": int(time.time()), "nodes": [],
               "rooms": rooms, "maps": maps}, open(out_path, "w"))
    print(f"done: {len(rooms)} rooms, {calls['n']} calls -> {out_path}")


asyncio.run(main())
