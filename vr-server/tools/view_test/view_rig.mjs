// Close-ups of one rigged Yeep with the map hidden: front, back, right side, top.
// usage: node view_rig.mjs OUTDIR [actorIndex] [query]
import { spawn } from "node:child_process";
import { writeFileSync, mkdirSync } from "node:fs";

const OUT = process.argv[2], WHO = +(process.argv[3] || 0), QUERY = process.argv[4] || "";
mkdirSync(OUT + "/prof", { recursive: true });
const chrome = spawn("/usr/bin/google-chrome", [
  "--headless=new", "--remote-debugging-port=9335", `--user-data-dir=${OUT}/prof`,
  "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist",
  "--window-size=900,700", "--no-first-run", "--no-default-browser-check", "about:blank"], { stdio: "ignore" });
const sleep = ms => new Promise(r => setTimeout(r, ms));
let t = [];
for (let i = 0; i < 40 && !t.length; i++) { await sleep(250); try { t = (await (await fetch("http://127.0.0.1:9335/json/list")).json()).filter(x => x.type === "page"); } catch {} }
const ws = new WebSocket(t[0].webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener("open", r));
let id = 0; const pend = new Map(), logs = [];
ws.addEventListener("message", e => { const m = JSON.parse(e.data); if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); }
  if (m.method === "Runtime.exceptionThrown") logs.push("EXC " + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text)); });
const send = (method, params = {}) => new Promise(r => { const i = ++id; pend.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
const ev = async x => (await send("Runtime.evaluate", { expression: x, returnByValue: true, awaitPromise: true })).result.result?.value;
const shot = async n => { const r = await send("Page.captureScreenshot", { format: "png" }); writeFileSync(`${OUT}/${n}.png`, Buffer.from(r.result.data, "base64")); };
await send("Page.enable"); await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 900, height: 700, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url: "http://127.0.0.1:8799/editor/view?debug=1" + (QUERY ? "&" + QUERY : "") });
await sleep(9000);
console.log(await ev(`(() => { const V = window.__view; V.blockGroup.visible = false; for (const s of V.players.values()) s.tag.visible = false;
  return "players " + V.players.size; })()`));
const aim = (dx, dy, dz) => ev(`(() => { const V = window.__view; const s = [...V.players.values()][${WHO}]; if (!s) return "none";
  const p = s.outer.localToWorld(new V.THREE.Vector3(0, 0.2, 0));
  const f = s.outer.localToWorld(new V.THREE.Vector3(0, 0.2, 1)).sub(p).normalize();   // where the Yeep faces
  const r = s.outer.localToWorld(new V.THREE.Vector3(1, 0.2, 0)).sub(p).normalize();   // its right
  const off = f.multiplyScalar(${dz}).add(r.multiplyScalar(${dx})).add(new V.THREE.Vector3(0, ${dy}, 0)).setLength(3.2);
  V.controls.target.copy(p); V.camera.position.copy(p).add(off); V.controls.update(); return s.name; })()`);
for (const [n, d] of [["front", [0, .25, 1]], ["back", [0, .25, -1]], ["right", [1, .2, 0]], ["top", [0.05, 1, 0.05]]]) {
  console.log(n, await aim(...d)); await sleep(500); await shot("rig_" + n);
}
console.log(logs.length ? logs.join("\n") : "(no exceptions)");
ws.close(); chrome.kill(); process.exit(0);
