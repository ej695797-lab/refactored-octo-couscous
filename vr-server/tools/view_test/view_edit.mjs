import { spawn } from "node:child_process"; import { mkdirSync } from "node:fs";
const OUT = process.argv[2]; mkdirSync(OUT + "/prof", { recursive: true });
const ch = spawn("/usr/bin/google-chrome", ["--headless=new","--remote-debugging-port=9338",`--user-data-dir=${OUT}/prof`,"--use-angle=swiftshader","--enable-unsafe-swiftshader","--window-size=1280,800","--no-first-run","about:blank"], { stdio: "ignore" });
const sleep = ms => new Promise(r => setTimeout(r, ms)); let t = [];
for (let i = 0; i < 40 && !t.length; i++) { await sleep(250); try { t = (await (await fetch("http://127.0.0.1:9338/json/list")).json()).filter(x => x.type === "page"); } catch {} }
const ws = new WebSocket(t[0].webSocketDebuggerUrl); await new Promise(r => ws.addEventListener("open", r));
let id = 0; const pend = new Map(), logs = []; ws.addEventListener("message", e => { const m = JSON.parse(e.data); if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); } if (m.method === "Runtime.exceptionThrown") logs.push(m.params.exceptionDetails.exception?.description); });
const send = (method, params = {}) => new Promise(r => { const i = ++id; pend.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
const ev = async x => (await send("Runtime.evaluate", { expression: x, returnByValue: true })).result.result?.value;
const key = async (k, code) => { await send("Input.dispatchKeyEvent", { type: "keyDown", key: k, code, text: k.length === 1 ? k : undefined }); await send("Input.dispatchKeyEvent", { type: "keyUp", key: k, code }); };
const click = async (x, y, button = "left") => { await send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y }); for (const type of ["mousePressed", "mouseReleased"]) await send("Input.dispatchMouseEvent", { type, x, y, button, clickCount: 1 }); };
const rev = async () => (await (await fetch("http://127.0.0.1:8799/editor/api/photon/view")).json()).view.rev;
const stat = () => ev(`(() => { const V = window.__view; let sep = 0; for (const b of V.blocks.values()) if (b.model && b.model.visible) sep++; return "blocks " + V.blocks.size + ", separate " + sep + ", baked " + V.baked.children.length; })()`);
await send("Page.enable"); await send("Runtime.enable"); await send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 800, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url: "http://127.0.0.1:8799/editor/view?debug=1" }); await sleep(12000);
console.log("loaded:", await stat());
// aim straight down at open floor near the players, then build there
await ev(`(() => { const V = window.__view; const s = [...V.players.values()][0]; const p = s.outer.localToWorld(new V.THREE.Vector3(6, 0, 0));
  V.controls.target.copy(p); V.camera.position.copy(p).add(new V.THREE.Vector3(0.01, 30, 0.01)); V.controls.update(); })()`);
const r0 = await rev();
await key("1", "Digit1"); await click(795, 400);
await sleep(1500); console.log("build: rev", r0, "->", await rev(), "|", await stat(), "|", await ev(`document.getElementById("msg").textContent`));
await sleep(2000); console.log("after bake:", await stat());
// right click the same spot = erase (Map Maker style)
await click(795, 400, "right");
await sleep(1500); console.log("right-click erase: rev ->", await rev(), "|", await ev(`document.getElementById("msg").textContent`));
await sleep(1500); console.log("after re-bake:", await stat());
await ev(`document.getElementById("refresh").click()`); await sleep(6000);
console.log("after Refresh map:", await stat());
console.log(logs.length ? "EXC " + logs.join("\n") : "(no exceptions)");
ws.close(); ch.kill(); process.exit(0);
