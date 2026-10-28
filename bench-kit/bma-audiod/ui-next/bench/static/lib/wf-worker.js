// Waterfall in a worker: the page transfers an OffscreenCanvas once, then posts column batches (transferred buffers). The worker owns the draw loop.
// Messages in: {t:"init", canvas, kind, opts} | {t:"push", tile, buf} | {t:"range", lo, hi, cmap} | {t:"stop"}. Out: {t:"ready"} | {t:"stats", busyMs, frames} | {t:"error", msg}.
let r = null, busy = 0, frames = 0, timer = 0, dirty = false, running = true;
const KINDS = { canvas2d: "./wf-canvas2d.js", webgl: "./wf-webgl.js", webgpu: "./wf-webgpu.js" };

function loop() {
  if (!running) return;
  if (dirty) { const t0 = performance.now(); r.draw(); busy += performance.now() - t0; frames++; dirty = false; }
  if (self.requestAnimationFrame) self.requestAnimationFrame(loop); else timer = setTimeout(loop, 16);
}

self.onmessage = async e => {
  const m = e.data;
  try {
    if (m.t === "init") {
      const mod = await import(KINDS[m.kind]);
      r = await mod.create(m.canvas, m.opts);
      self.postMessage({ t: "ready", name: r.name });
      loop();
    } else if (m.t === "push" && r) { const t0 = performance.now(); r.push(m.tile, new Uint8Array(m.buf)); busy += performance.now() - t0; dirty = true; }
    else if (m.t === "range" && r) { r.setRange(m.lo, m.hi, m.cmap); dirty = true; }
    else if (m.t === "stats") { self.postMessage({ t: "stats", busyMs: busy, frames }); busy = 0; frames = 0; }
    else if (m.t === "stop") { running = false; clearTimeout(timer); if (r) r.destroy(); }
  } catch (err) { self.postMessage({ t: "error", msg: String(err && err.message || err) }); }
};
