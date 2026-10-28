// Waterfall renderer, Canvas 2D. Interface shared by all renderers:
//   create(canvas, { tiles: [{x, y, w, h, src?}], bins, cols, lo, hi, cmap }) -> { push(tile, u8Column), draw(), setRange(lo, hi, cmap), destroy(), name }
// Layout: one tile = one beam. Time runs left to right (newest at the right edge), frequency bottom to top (band 0 at the bottom).
// Each tile keeps a cols x bins RGBA bitmap in an offscreen canvas; push() writes ONE column; draw() blits it with a wrap (two source rects).
import { levelTable } from "./colormaps.js";

export function create(canvas, o) {
  const ctx = canvas.getContext("2d", { alpha: false, desynchronized: true });
  let table = levelTable(o.cmap || "viridis", o.lo ?? -90, o.hi ?? -30);
  const mk = () => (typeof OffscreenCanvas !== "undefined" ? new OffscreenCanvas(o.cols, o.bins) : Object.assign(document.createElement("canvas"), { width: o.cols, height: o.bins }));
  const T = o.tiles.map(() => { const c = mk(), x = c.getContext("2d"); return { c, x, img: x.createImageData(1, o.bins), head: 0, raw: new Uint8Array(o.cols * o.bins) }; });
  ctx.imageSmoothingEnabled = false;
  function paintColumn(t, col, u8) {
    const d = t.img.data;
    for (let b = 0; b < o.bins; b++) {                      // band 0 at the bottom
      const v = u8[b], y = o.bins - 1 - b, k = y * 4, s = v * 4;
      d[k] = table[s]; d[k + 1] = table[s + 1]; d[k + 2] = table[s + 2]; d[k + 3] = 255;
      t.raw[col * o.bins + b] = v;
    }
    t.x.putImageData(t.img, col, 0);
  }
  return {
    name: "canvas2d",
    push(i, u8) { const t = T[i]; t.head = (t.head + 1) % o.cols; paintColumn(t, t.head, u8); },
    draw() {
      for (let i = 0; i < T.length; i++) {
        const r = o.tiles[i], t = T[r.src ?? i], n1 = o.cols - (t.head + 1), w1 = r.w * n1 / o.cols;
        if (n1 > 0) ctx.drawImage(t.c, t.head + 1, 0, n1, o.bins, r.x, r.y, w1, r.h);
        ctx.drawImage(t.c, 0, 0, t.head + 1, o.bins, r.x + w1, r.y, r.w - w1, r.h);
      }
    },
    setRange(lo, hi, cmap) {                                // repaint from the raw levels: a range change costs one pass, not a data re-fetch
      table = levelTable(cmap || "viridis", lo, hi);
      const col = new Uint8Array(o.bins);
      for (const t of T) for (let c = 0; c < o.cols; c++) { for (let b = 0; b < o.bins; b++) col[b] = t.raw[c * o.bins + b]; paintColumn(t, c, col); }
    },
    setSource(i, src) { o.tiles[i].src = src; },             // a tile may show another tile's data (the large view of a selected beam)
    destroy() {},
  };
}
