import { spawn } from "node:child_process"; import { writeFileSync, mkdirSync } from "node:fs";
const OUT = process.argv[2]; mkdirSync(OUT + "/prof", { recursive: true });
const ch = spawn("/usr/bin/google-chrome", ["--headless=new","--remote-debugging-port=9337",`--user-data-dir=${OUT}/prof`,"--use-angle=swiftshader","--enable-unsafe-swiftshader","--window-size=1280,800","--no-first-run","about:blank"], { stdio: "ignore" });
const sleep = ms => new Promise(r => setTimeout(r, ms)); let t = [];
for (let i = 0; i < 40 && !t.length; i++) { await sleep(250); try { t = (await (await fetch("http://127.0.0.1:9337/json/list")).json()).filter(x => x.type === "page"); } catch {} }
const ws = new WebSocket(t[0].webSocketDebuggerUrl); await new Promise(r => ws.addEventListener("open", r));
let id = 0; const pend = new Map(), logs = []; ws.addEventListener("message", e => { const m = JSON.parse(e.data); if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); } if (m.method === "Runtime.exceptionThrown") logs.push(m.params.exceptionDetails.exception?.description); });
const send = (method, params = {}) => new Promise(r => { const i = ++id; pend.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
const ev = async x => (await send("Runtime.evaluate", { expression: x, returnByValue: true })).result.result?.value;
await send("Page.enable"); await send("Runtime.enable"); await send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 800, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url: "http://127.0.0.1:8799/editor/view?debug=1" });
for (const s of [4, 10, 20]) {
  await sleep(s === 4 ? 4000 : 6000);
  console.log(`t~${s}s:`, await ev(`(() => { const V = window.__view; const i = V.renderer.info.render;
    let unbaked = 0; for (const b of V.blocks.values()) if (b.model && b.model.visible) unbaked++;
    return "blocks " + V.blocks.size + " | baked meshes " + V.baked.children.length + " | still separate " + unbaked + " | draw calls " + i.calls + " | triangles " + i.triangles; })()`));
}
const r = await send("Page.captureScreenshot", { format: "png" }); writeFileSync(`${OUT}/baked.png`, Buffer.from(r.result.data, "base64"));
console.log(logs.length ? "EXC " + logs.join("\n") : "(no exceptions)");
ws.close(); ch.kill(); process.exit(0);
