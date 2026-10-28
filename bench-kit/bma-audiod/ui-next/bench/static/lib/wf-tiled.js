// Waterfall renderer, tiled DOM canvases (the technique OpenWebRX uses, written fresh here; the reference is AGPL and was only read).
// Each tile is a clipped box holding a strip of small fixed-width canvases. push() writes ONE column with putImageData into the newest canvas and moves the
// strip with a CSS transform, so scrolling costs no pixel copy and no draw(): the compositor scales and scrolls. Full canvases scroll out and are dropped.
// Needs DOM elements: main thread only (no OffscreenCanvas worker variant). Differs from the other renderers: create(containerElement, opts).
import { levelTable } from "./colormaps.js";

const CHUNK = 50;                                           // columns per canvas

export function create(box, o) {
  let table = levelTable(o.cmap || "viridis", o.lo ?? -90, o.hi ?? -30);
  box.style.position = "relative"; box.style.overflow = "hidden";
  const T = o.tiles.map(r => {
    const view = document.createElement("div");
    Object.assign(view.style, { position: "absolute", left: r.x + "px", top: r.y + "px", width: r.w + "px", height: r.h + "px", overflow: "hidden", background: "#000" });
    const strip = document.createElement("div");
    Object.assign(strip.style, { position: "absolute", left: "0", top: "0", height: r.h + "px", whiteSpace: "nowrap", willChange: "transform" });
    view.appendChild(strip); box.appendChild(view);
    const px = r.w / o.cols;                                // CSS pixels per column
    return { r, view, strip, px, chunks: [], total: 0, img: new ImageData(1, o.bins) };
  });
  function addChunk(t) {
    const c = document.createElement("canvas"); c.width = CHUNK; c.height = o.bins;
    Object.assign(c.style, { display: "inline-block", width: CHUNK * t.px + "px", height: t.r.h + "px", imageRendering: "pixelated", verticalAlign: "top" });
    t.strip.appendChild(c); t.chunks.push({ c, x: c.getContext("2d"), n: 0 });
    while (t.chunks.length * CHUNK > o.cols + CHUNK * 2) { const old = t.chunks.shift(); old.c.remove(); t.dropped = (t.dropped || 0) + CHUNK; }
  }
  return {
    name: "tiled",
    push(i, u8) {
      const t = T[i];
      let ch = t.chunks[t.chunks.length - 1];
      if (!ch || ch.n >= CHUNK) { addChunk(t); ch = t.chunks[t.chunks.length - 1]; }
      const d = t.img.data;
      for (let b = 0; b < o.bins; b++) { const y = o.bins - 1 - b, s = u8[b] * 4, k = y * 4; d[k] = table[s]; d[k + 1] = table[s + 1]; d[k + 2] = table[s + 2]; d[k + 3] = 255; }
      ch.x.putImageData(t.img, ch.n++, 0);
      t.total++;
      t.strip.style.transform = "translateX(" + (t.r.w - (t.total - (t.dropped || 0)) * t.px) + "px)";     // newest column at the right edge
      // the strip's left edge moves with the dropped chunks: chunk positions stay relative to the first kept chunk
    },
    draw() {},
    setRange(lo, hi, cmap) { table = levelTable(cmap || "viridis", lo, hi); },       // existing columns keep their colours (the tiled bitmaps are not re-read)
    destroy() { for (const t of T) t.view.remove(); },
  };
}
