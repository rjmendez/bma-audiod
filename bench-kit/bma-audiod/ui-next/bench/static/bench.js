// Render benchmark: a scrolling waterfall (1 big tile for the 8-beam mean + 8 beam strips) fed synthetic columns, in the main thread or a worker.
// Parameters in location.hash, e.g. #kind=webgl&where=worker&bins=46&rate=10&dur=12  ->  window.__bench = { ... } when finished.
import { makeGen } from "./lib/gen.js";

const P = Object.fromEntries(location.hash.slice(1).split("&").filter(Boolean).map(s => s.split("=")));
const kind = P.kind || "canvas2d", where = P.where || "main", bins = +(P.bins || 46), rate = +(P.rate || 10), dur = +(P.dur || 12), beams = 8;
const cols = +(P.cols || 600), W = +(P.w || 960), H = 420;
const cv = document.getElementById("cv");
cv.width = W; cv.height = H;
const tiles = [{ x: 0, y: 0, w: W, h: 300 }];
for (let i = 0; i < beams; i++) tiles.push({ x: Math.floor(i * W / beams), y: 312, w: Math.floor(W / beams) - 4, h: 100 });
const opts = { tiles, bins, cols, lo: -90, hi: -30, cmap: "viridis" };
const gens = Array.from({ length: beams }, (_, i) => makeGen(bins, beams, 7 + i));
const out = { kind, where, bins, rate, cols, tiles: tiles.length, dur, frames: 0, frameMs: [], pushedColumns: 0, uploadBytes: 0, workerBusyMs: 0, error: null, ready: false };
window.__bench = out;

(async () => {
  let r = null, worker = null;
  try {
    if (kind === "tiled") {
      if (where === "worker") throw new Error("tiled needs DOM canvases: no worker variant");
      const box = document.getElementById("box"); cv.hidden = true; box.hidden = false; box.style.width = W + "px"; box.style.height = H + "px";
      r = await (await import("./lib/wf-tiled.js")).create(box, opts);
    } else if (where === "worker") {
      if (!cv.transferControlToOffscreen) throw new Error("OffscreenCanvas transfer unavailable");
      const off = cv.transferControlToOffscreen();
      worker = new Worker("./lib/wf-worker.js", { type: "module" });
      await new Promise((res, rej) => { worker.onmessage = e => (e.data.t === "ready" ? res() : rej(new Error(e.data.msg || "worker"))); worker.onerror = e => rej(new Error(e.message || "worker error")); worker.postMessage({ t: "init", canvas: off, kind, opts }, [off]); });
    } else {
      const mod = await import(kind === "canvas2d" ? "./lib/wf-canvas2d.js" : kind === "webgl" ? "./lib/wf-webgl.js" : "./lib/wf-webgpu.js");
      r = await mod.create(cv, opts);
    }
  } catch (e) { out.error = String(e && e.message || e); out.done = true; return; }
  out.ready = true;
  const t0 = performance.now(); let last = t0, carry = 0;
  function frame(now) {
    const dt = (now - last) / 1000; last = now; carry += dt * rate;
    const n = Math.floor(carry); carry -= n;
    let render = 0;                                           // render cost only: column generation is not counted
    for (let k = 0; k < n; k++) {
      const mean = new Uint8Array(bins), cs = [];
      for (let i = 0; i < beams; i++) { const c = gens[i](i); cs.push(c); for (let b = 0; b < bins; b++) mean[b] += c[b] / beams; }
      const all = [mean, ...cs];
      for (let i = 0; i < all.length; i++) {
        if (worker) { const buf = all[i].buffer.slice(0); worker.postMessage({ t: "push", tile: i, buf }, [buf]); }
        else { const a = performance.now(); r.push(i, all[i]); render += performance.now() - a; }
        out.pushedColumns++; out.uploadBytes += bins;
      }
    }
    if (!worker && n > 0) { const a = performance.now(); r.draw(); render += performance.now() - a; }
    if (!worker && n > 0) out.frameMs.push(render);
    out.frames++;
    if (now - t0 < dur * 1000) requestAnimationFrame(frame);
    else finish();
  }
  async function finish() {
    if (worker) {
      const s = await new Promise(res => { worker.onmessage = e => e.data.t === "stats" && res(e.data); worker.postMessage({ t: "stats" }); });
      out.workerBusyMs = s.busyMs; out.workerFrames = s.frames; worker.postMessage({ t: "stop" });
    } else r.destroy();
    out.frameMs.sort((x, y) => x - y);
    const f = out.frameMs;
    out.mainMsPerFrame = f.length ? f.reduce((x, y) => x + y, 0) / f.length : 0;
    out.p95 = f.length ? f[Math.floor(f.length * 0.95)] : 0;
    out.max = f.length ? f[f.length - 1] : 0;
    out.fps = out.frames / dur;
    out.frameMs = [];
    out.done = true;
  }
  requestAnimationFrame(frame);
})();
