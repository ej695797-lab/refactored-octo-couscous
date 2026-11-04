// Headless Chrome (SwiftShader) screenshots of the View page against view_mock.py.
// usage: node view_shots.mjs OUTDIR [query]   -> overview.png, face.png, side.png, top.png
import { spawn } from "node:child_process";
import { writeFileSync, mkdirSync } from "node:fs";

const OUT = process.argv[2], QUERY = process.argv[3] || "";
const URL_ = "http://127.0.0.1:8799/editor/view?debug=1" + (QUERY ? "&" + QUERY : "");
mkdirSync(OUT + "/prof", { recursive: true });
const chrome = spawn("/usr/bin/google-chrome", [
  "--headless=new", "--remote-debugging-port=9334", `--user-data-dir=${OUT}/prof`,
  "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist",
  "--window-size=1280,800", "--no-first-run", "--no-default-browser-check", "about:blank"], { stdio: "ignore" });
const sleep = ms => new Promise(r => setTimeout(r, ms));
let targets = [];
for (let i = 0; i < 40 && !targets.length; i++) {
  await sleep(250);
  try { targets = (await (await fetch("http://127.0.0.1:9334/json/list")).json()).filter(t => t.type === "page"); } catch {}
}
const ws = new WebSocket(targets[0].webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener("open", r));
let id = 0;
const pending = new Map(), logs = [];
ws.addEventListener("message", ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  if (m.method === "Runtime.consoleAPICalled") logs.push(m.params.type + ": " + m.params.args.map(a => a.value ?? a.description).join(" "));
  if (m.method === "Runtime.exceptionThrown") logs.push("EXCEPTION: " + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text));
});
const send = (method, params = {}) => new Promise(r => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
const shot = async name => { const r = await send("Page.captureScreenshot", { format: "png" }); writeFileSync(`${OUT}/${name}.png`, Buffer.from(r.result.data, "base64")); };
const ev = async expr => (await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true })).result.result?.value;

await send("Page.enable"); await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 800, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url: URL_ });
await sleep(20000);                                  // 5.9k blocks: let the models stream in
console.log("blocks drawn:", await ev("window.__view && window.__view.blocks.size"),
            "| players:", await ev("window.__view && window.__view.players.size"));
await shot("overview");
// close-ups of the first player (real captured pose): front, side, top
const aim = (dx, dy, dz, dist) => ev(`(() => { const V = window.__view; const s = [...V.players.values()].find(p => p.t) || [...V.players.values()][0];
  if (!s) return "no player"; const p = s.outer.getWorldPosition(new V.THREE.Vector3()); p.y += 0.6;
  const f = new V.THREE.Vector3(0, 0, 1).applyQuaternion(s.outer.getWorldQuaternion(new V.THREE.Quaternion()));
  const right = new V.THREE.Vector3().crossVectors(new V.THREE.Vector3(0, 1, 0), f).normalize();
  const off = f.clone().multiplyScalar(${dz}).add(right.multiplyScalar(${dx})).add(new V.THREE.Vector3(0, ${dy}, 0)).setLength(${dist});
  V.controls.target.copy(p); V.camera.position.copy(p).add(off); V.controls.update(); return "ok"; })()`);
console.log("aim face:", await aim(0, 0.2, 1, 5)); await sleep(800); await shot("face");
await aim(1, 0.15, 0, 5); await sleep(800); await shot("side");
await aim(0.4, 1, 0.4, 6); await sleep(800); await shot("above");
console.log("page console:", logs.length ? "\n  " + logs.slice(0, 12).join("\n  ") : "(clean)");
ws.close(); chrome.kill(); process.exit(0);
