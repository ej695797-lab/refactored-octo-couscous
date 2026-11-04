# View tab test kit (no live players, no bot)

The editor's View tab (static/editor_view.html) checked against a mock of its endpoints that
serves a REAL map and players driven by a REAL captured VR pose. The in-app browser pane has
no WebGL, so these drive headless Chrome with a software renderer over the DevTools protocol
(own temp profile, never the owner's Chrome).

1. Make the map data (decodes live maps from Mongo; rows [name,x,y,z,fwd,up,color]):

       cd yeeps-proxy-240/vr-server && python3 - <<'PY'
       import sys, json, base64; sys.path.insert(0, '.')
       import config, pymongo, brotli
       from services.mapsync_decoder import _decode_spam
       db = pymongo.MongoClient(config.MONGO_URI)["Yeeps240"]; out = {}
       for rk in ("nexus",):
           doc = db.maps.find_one({"mapKey": f"{rk}$current"}, {"saveData": 1, "saveChunks": 1}) or {}
           rows = []
           for c in doc.get("saveChunks") or [doc["saveData"]]:
               raw = base64.b64decode(c); raw = raw if raw[:4] == b"SPAM" else brotli.decompress(raw)
               rows += [[b["name"], b["x"], b["y"], b["z"], b["fwd"], b["up"], b["color"]] for b in _decode_spam(raw)]
           d = (db.rooms.find_one({"roomKey": rk}) or {}).get("dimensions")
           out[rk] = {"dims": json.loads(d) if isinstance(d, str) else d, "blocks": rows}
       json.dump(out, open("/tmp/real_maps.json", "w"))
       PY

2. Run the mock (port 8799):

       python3 tools/view_test/view_mock.py static/editor_view.html /tmp/real_maps.json \
         ../../yeeps-proxy/vr-server/static/mapmaker-assets static/view-yeep.json 8799

3. Drive it (each writes PNGs into OUTDIR):
   - `node view_shots.mjs OUTDIR`  overview + close-ups
   - `node view_rig.mjs OUTDIR 0`  one Yeep from front/back/right/top, map hidden
   - `node view_edit.mjs OUTDIR`   build, right-click erase, Refresh map (prints block/bake counts)
   - `node view_perf.mjs OUTDIR`   draw calls before/after baking
   - `node view_ghost.mjs OUTDIR`  the see-through placement preview

Stop the mock with `pkill -f "[v]iew_mock.py"` (the bracket stops pkill matching your own shell).
The page's `?debug` query exposes `window.__view` (camera, controls, players, blocks, baked, renderer).
