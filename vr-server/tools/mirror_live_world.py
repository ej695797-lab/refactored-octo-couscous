import os
"""Read-only mirror of rooms from the REAL live Yeeps world into a local JSON file.

Usage:
    python3 tools/mirror_live_world.py <roomMapKey> <keep.json> <out.json>

keep.json is the map-designer export ({"rooms": [{"key": ...}, ...]}). For the
room map and every kept room this reads, from real Yeeps:
  - fetchRoomMapData  -> the full node list (real passagewayLinks etc.)
  - fetchRoomData     -> each room's full settings
  - fetchMapData      -> every block chunk and terrain chunk (raw base64 blobs)
Nothing is written to real Yeeps; nothing is written to our DB here either
(tools/import_live_world.py does that from the JSON).

Credentials come from the yeeps-proxy relay's /AWS endpoint using the service
account in globals "cw_fetch_account" (the same one community-world mirroring uses).
"""
import asyncio
import json
import sys
import time

import boto3
import httpx

sys.path.insert(0, ".")
from config import YEEPS_PROXY_URL  # noqa: E402
from services.dynamo_ops import _cw_account  # noqa: E402

LAMBDA = "g2_mobileInvokeFunction"
REGION = "us-west-1"
APP_VERSIONS = ["2.31.4", "2.28.1"]

ROOM_PROPS = [
    "roomKey", "roomName", "themeKey", "dimensions", "lobbyDirection", "door_up", "door_right",
    "door_down", "door_left", "passagewayLinks_creative", "isCreative", "isModeLocked",
    "isAutoCycling", "autoCycleTimestamp", "noSave", "noCopy", "noPaste", "isFreebuild",
    "isDevLocked", "blockMatchmakingSegmentation", "isBeginner", "isSolo", "goingAwayTimestamp",
    "emptyMessage", "isNoTerrain", "overrideExpireDurationMinutes", "overrideSpawnPosition",
    "mapDescription", "randomization_type", "randomization_mapCount", "lastEditedSessionID",
    "lastEditedTerrainSessionID", "roomDataVersion", "floorDepth", "liquidType", "liquidColor",
    "liquidLevel", "itemRestrictionsIsWhiteList", "itemRestrictionsGroupKeys",
    "teamItemRestrictions", "customLightingKey", "isOfficial", "creativeSessionID",
    "overrideMaxPlayers", "gravityType", "minVersion", "maxVersion", "isVisible",
]


class Live:
    def __init__(self, sess):
        self.s = sess
        self.lam = boto3.client(
            "lambda", region_name=REGION,
            aws_access_key_id=sess["accessKeyId"], aws_secret_access_key=sess["secretAccessKey"],
            aws_session_token=sess["sessionToken"])
        self.app_version = APP_VERSIONS[0]
        self.calls = 0

    def call(self, fn, **extra):
        payload = {"accountID": self.s["accountID"], "gameSessionID": self.s["gameSessionID"],
                   "mobilePassword": self.s["mobilePassword"], "dataVersion": 2,
                   "functionName": fn, "applicationVersion": self.app_version}
        payload.update(extra)
        for attempt in range(4):
            try:
                r = self.lam.invoke(FunctionName=LAMBDA, InvocationType="RequestResponse",
                                    Payload=json.dumps(payload).encode())
                self.calls += 1
                outer = json.loads(r["Payload"].read())
                body = outer.get("body")
                try:
                    body = json.loads(body)
                except Exception:
                    pass
                return outer.get("statusCode"), body
            except Exception as e:
                print(f"   retry {fn} ({e})")
                time.sleep(0.6 * (attempt + 1))
        return None, None


def fetch_chunks(live, room_key, session_id, terrain):
    chunks, ver, internal = [], None, ""
    for offset in range(60):
        st, body = live.call("fetchMapData", roomKey=room_key, targetSessionID=session_id,
                             saveOffset=offset, isTerrain=1 if terrain else 0)
        data = (body or {}).get("data") or {}
        sd = data.get("saveData")
        if not sd:
            break
        if ver is None:
            ver = data.get("mapDataVersion")
        if offset == 0:
            internal = data.get("saveInternalStates") or ""
        if chunks and sd == chunks[-1]:
            break
        chunks.append(sd)
    return chunks, ver, internal


async def get_session():
    oc, code = await _cw_account()
    if not oc:
        raise SystemExit("no cw_fetch_account service account in globals")
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{YEEPS_PROXY_URL}/AWS",
                         json={"secret": os.getenv("RELAY_SECRET_AWS", ""), "oculusID": oc, "mobileCode": code})
    d = r.json()
    if not d.get("ok"):
        raise SystemExit(f"/AWS failed: {d.get('error')}")
    return d


def main():
    map_key, keep_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    keep = [r["key"] for r in json.load(open(keep_path))["rooms"]]
    sess = asyncio.run(get_session())
    live = Live(sess)

    # room map (try versions until one answers)
    nodes, rm_version = None, None
    for v in APP_VERSIONS:
        live.app_version = v
        st, body = live.call("fetchRoomMapData", roomMapKey=map_key,
                             propertiesToGet=["uniqueID", "data", "version"])
        d = (body or {}).get("data") or {}
        if d.get("data"):
            nodes, rm_version = d["data"], d.get("version")
            break
        print(f"room map with appVersion {v}: status {st}")
    if not nodes:
        raise SystemExit("could not read the room map")
    print(f"room map {map_key}: {len(nodes)} nodes, version {rm_version}, appVersion {live.app_version}")

    rooms, maps = {}, {}
    for i, rk in enumerate(keep, 1):
        st, body = live.call("fetchRoomData", roomKey=rk, propertiesToGet=ROOM_PROPS)
        rd = (body or {}).get("data") or {}
        rooms[rk] = rd
        blocks, bver, internal = fetch_chunks(live, rk, rd.get("lastEditedSessionID") or 0, False)
        terr, tver, _ = fetch_chunks(live, rk, rd.get("lastEditedTerrainSessionID") or 0, True)
        maps[rk] = {"blocks": blocks, "blocksVersion": bver, "internalStates": internal,
                    "terrain": terr, "terrainVersion": tver}
        size = sum(len(c) for c in blocks)
        print(f"[{i}/{len(keep)}] {rk:28} {rd.get('roomName')!s:26} blocks {len(blocks)} chunk(s) {size}b "
              f"v{bver} | terrain {len(terr)} chunk(s) v{tver}")

    out = {"roomMapKey": map_key, "roomMapVersion": rm_version, "appVersion": live.app_version,
           "fetchedAt": int(time.time()), "nodes": nodes, "rooms": rooms, "maps": maps}
    json.dump(out, open(out_path, "w"))
    print(f"done: {len(rooms)} rooms, {live.calls} calls -> {out_path}")


if __name__ == "__main__":
    main()
