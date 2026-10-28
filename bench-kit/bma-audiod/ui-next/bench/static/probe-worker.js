// Classic worker used by features.js: what a worker can do in this engine.
const out = { raf: typeof self.requestAnimationFrame, offscreen: typeof OffscreenCanvas };
try { out.webgl2 = !!new OffscreenCanvas(2, 2).getContext("webgl2"); } catch (e) { out.webgl2 = "error"; }
try { out.canvas2d = !!new OffscreenCanvas(2, 2).getContext("2d"); } catch (e) { out.canvas2d = "error"; }
self.postMessage(out);
