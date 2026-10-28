// Engine feature probe: runs the checks the prototype depends on and POSTs {id, ua, results} to /__report (read by audit/cross_browser.py) and lists them on the page for a phone.
// Each result: {ok: true|false|null, detail}. null = informational (the prototype does not use the feature).
// Open it by hand on any device: /bench/features.html?id=phone  then GET /__reports.
const id = new URLSearchParams(location.search).get("id") || "manual";
const R = {};
const rec = (name, ok, detail = "") => { R[name] = { ok, detail: String(detail).slice(0, 160) }; };
const run = async (name, fn) => { try { const r = await fn(); if (r && typeof r === "object" && "ok" in r) rec(name, r.ok, r.detail); else rec(name, r === undefined ? true : !!r, ""); } catch (e) { rec(name, false, e && e.message || e); } };
const wait = ms => new Promise(r => setTimeout(r, ms));
const lutRgb = (level, lo = -90, hi = -30) => import("./lib/colormaps.js").then(m => { const t = m.levelTable("viridis", lo, hi); return [t[level * 4], t[level * 4 + 1], t[level * 4 + 2]]; });
const near = (a, b, tol = 3) => a.every((v, i) => Math.abs(v - b[i]) <= tol);

async function rendererPixel(kind) {                       // push 4 columns of a known level through the real renderer and read the right edge back
  const cv = document.createElement("canvas"); cv.width = 64; cv.height = 32; document.body.append(cv);
  const m = await import("./lib/wf-" + kind + ".js"), bins = 8, level = 140;
  const r = await m.create(cv, { tiles: [{ x: 0, y: 0, w: 64, h: 32 }], bins, cols: 16, lo: -90, hi: -30, cmap: "viridis" });
  const col = new Uint8Array(bins).fill(level); for (let i = 0; i < 4; i++) r.push(0, col);
  r.draw();
  let px;
  if (kind === "webgl") { const gl = cv.getContext("webgl2"); const o = new Uint8Array(4); gl.readPixels(62, 16, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, o); px = [o[0], o[1], o[2]]; }
  else { const d = cv.getContext("2d").getImageData(62, 16, 1, 1).data; px = [d[0], d[1], d[2]]; }
  const want = await lutRgb(level);
  r.destroy(); cv.remove();
  return { ok: near(px, want), detail: "read " + px + " want " + want };
}

(async () => {
  rec("ecmascript: BigInt DataView (frame timestamps)", typeof DataView.prototype.getBigUint64 === "function");
  rec("ecmascript: dynamic import, module scripts", true);
  await run("webgl2: context", () => { const gl = document.createElement("canvas").getContext("webgl2"); if (!gl) return { ok: false, detail: "none" }; const ext = gl.getExtension("WEBGL_debug_renderer_info"); return { ok: true, detail: (ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER)) + ", max texture " + gl.getParameter(gl.MAX_TEXTURE_SIZE) }; });
  await run("webgl2: waterfall renderer draws the right pixel (R8 texture, LUT, ring)", () => rendererPixel("webgl"));
  await run("canvas2d: waterfall renderer draws the right pixel", () => rendererPixel("canvas2d"));
  await run("webgl: float textures (informational, not used: the waterfall is R8)", () => {
    const g2 = document.createElement("canvas").getContext("webgl2"), g1 = document.createElement("canvas").getContext("webgl");
    return { ok: null, detail: "webgl2 EXT_color_buffer_float " + !!(g2 && g2.getExtension("EXT_color_buffer_float")) + ", OES_texture_float_linear " + !!(g2 && g2.getExtension("OES_texture_float_linear")) + ", webgl1 OES_texture_float " + !!(g1 && g1.getExtension("OES_texture_float")) };
  });
  await run("webgpu: adapter (optional renderer)", async () => { if (!navigator.gpu) return { ok: false, detail: "navigator.gpu missing" }; const a = await navigator.gpu.requestAdapter(); return { ok: !!a, detail: a ? "adapter" : "no adapter" }; });
  await run("webgpu: waterfall renderer creates and draws (no pixel check)", async () => { const cv = document.createElement("canvas"); cv.width = 64; cv.height = 32; const m = await import("./lib/wf-webgpu.js"); const r = await m.create(cv, { tiles: [{ x: 0, y: 0, w: 64, h: 32 }], bins: 8, cols: 16, lo: -90, hi: -30 }); r.push(0, new Uint8Array(8).fill(100)); r.draw(); r.destroy(); return true; });
  await run("offscreencanvas: constructor, 2d and webgl2 contexts on the main thread", () => { const o = new OffscreenCanvas(4, 4); return { ok: !!o.getContext("2d") || !!new OffscreenCanvas(4, 4).getContext("webgl2"), detail: "2d " + !!new OffscreenCanvas(4, 4).getContext("2d") + " webgl2 " + !!new OffscreenCanvas(4, 4).getContext("webgl2") }; });
  await run("worker: module worker + OffscreenCanvas waterfall (canvas2d)", () => workerRender("canvas2d"));
  await run("worker: module worker + OffscreenCanvas waterfall (webgl)", () => workerRender("webgl"));
  await run("worker: requestAnimationFrame and webgl2 inside a classic worker", () => new Promise((res, rej) => { const w = new Worker("probe-worker.js"); w.onmessage = e => { w.terminate(); res({ ok: true, detail: JSON.stringify(e.data) }); }; w.onerror = e => rej(new Error(e.message)); setTimeout(() => rej(new Error("timeout")), 15000); }));
  await run("audio: AudioContext at 32 kHz + AudioWorklet module + node", async () => { const c = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 32000 }); await c.audioWorklet.addModule("./listen-worklet.js"); const n = new AudioWorkletNode(c, "pcm-player", { processorOptions: { inRate: 32000 } }); const d = "state " + c.state + ", rate " + c.sampleRate; n.disconnect(); await c.close(); return { ok: true, detail: d }; });
  await run("audio: HTMLAudioElement WAV / FLAC / Ogg", () => { const a = new Audio(); return { ok: !!a.canPlayType("audio/wav"), detail: "wav '" + a.canPlayType("audio/wav") + "' flac '" + a.canPlayType("audio/flac") + "' ogg '" + a.canPlayType("audio/ogg; codecs=vorbis") + "'" }; });
  await run("image: createImageBitmap(ImageData)", async () => { const b = await createImageBitmap(new ImageData(2, 2)); return b.width === 2; });
  await run("stream: DecompressionStream('deflate') (backlog)", () => ({ ok: typeof DecompressionStream === "function" }));
  await run("net: WebSocket binary frame from the mock (438 bytes, BMW1)", () => new Promise((res, rej) => { const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/api/v2/live"); ws.binaryType = "arraybuffer"; ws.onmessage = e => { if (typeof e.data !== "string") { const u = new Uint8Array(e.data), tag = String.fromCharCode(...u.subarray(0, 4)); ws.close(); res({ ok: u.length === 438 && tag === "BMW1", detail: u.length + " bytes " + tag }); } }; ws.onerror = () => rej(new Error("websocket error")); setTimeout(() => rej(new Error("timeout")), 5000); }));
  await run("dom: dialog.showModal", () => ({ ok: typeof HTMLDialogElement !== "undefined" && typeof HTMLDialogElement.prototype.showModal === "function" }));
  await run("css: 100dvh and env(safe-area-inset-top)", () => ({ ok: CSS.supports("height", "100dvh") && CSS.supports("padding-top", "env(safe-area-inset-top)"), detail: "dvh " + CSS.supports("height", "100dvh") + " env " + CSS.supports("padding-top", "env(safe-area-inset-top)") }));
  await run("css: prefers-color-scheme, prefers-reduced-motion", () => ({ ok: matchMedia("(prefers-color-scheme: dark)").media !== "not all" && matchMedia("(prefers-reduced-motion: reduce)").media !== "not all" }));
  await run("pointer: pointer events and setPointerCapture", () => ({ ok: "PointerEvent" in window && "setPointerCapture" in Element.prototype }));
  await run("net: navigator.connection.saveData (informational)", () => ({ ok: null, detail: "connection " + ("connection" in navigator) + (navigator.connection ? ", saveData " + navigator.connection.saveData : "") }));
  await fetch("/__report", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id, ua: navigator.userAgent, results: R }) });
  const out = document.getElementById("out");
  if (out) { out.textContent = ""; for (const [k, v] of Object.entries(R)) { const d = document.createElement("div"); d.textContent = (v.ok === null ? "info  " : v.ok ? "pass  " : "FAIL  ") + k + (v.detail ? "  [" + v.detail + "]" : ""); if (v.ok === false) d.style.color = "#c0392b"; out.append(d); } }
  document.title = "done";
})();

function workerRender(kind) {
  return new Promise((res, rej) => {
    const cv = document.createElement("canvas"); cv.width = 64; cv.height = 32;
    if (!cv.transferControlToOffscreen) return res({ ok: false, detail: "transferControlToOffscreen missing" });
    const off = cv.transferControlToOffscreen(), w = new Worker("./lib/wf-worker.js", { type: "module" });
    w.onerror = e => rej(new Error(e.message || "worker error"));
    w.onmessage = async e => {
      if (e.data.t === "error") return rej(new Error(e.data.msg));
      if (e.data.t === "ready") { const buf = new Uint8Array(8).fill(120).buffer; w.postMessage({ t: "push", tile: 0, buf }, [buf]); await wait(400); w.postMessage({ t: "stats" }); }
      if (e.data.t === "stats") { w.postMessage({ t: "stop" }); res({ ok: e.data.frames > 0, detail: "frames drawn " + e.data.frames }); }
    };
    w.postMessage({ t: "init", canvas: off, kind, opts: { tiles: [{ x: 0, y: 0, w: 64, h: 32 }], bins: 8, cols: 16, lo: -90, hi: -30, cmap: "viridis" } }, [off]);
    setTimeout(() => rej(new Error("timeout")), 15000);
  });
}
