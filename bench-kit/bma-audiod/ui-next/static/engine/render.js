/* Waterfall renderer: one interface, two back ends, any number of tiles on one canvas.
     'webgl'  : WebGL2, one R32F circular texture per ring (frequency x time), one column uploaded per push; dB range, colour map, frequency scale and
                time zoom are applied in the fragment shader, so a redraw costs no per-pixel CPU work.
     'canvas' : Canvas2D, a coloured ring canvas per layer, drawImage with wrap-around.
   A ring is one stream of columns (a beam); a layer is a rectangle of the canvas that shows one ring (a large tile for the selected beam next to
   small tiles for every beam). Several layers can show the same ring. Layers share one view (range, colour map, scale, time window).
   A CPU copy of every ring is kept, so a lost WebGL context is restored without the caller replaying anything, and replayInto() refills a
   renderer of the other kind (Canvas2D after a context that does not come back).
   Original code; the circular-texture and column-canvas techniques are common to open SDR front ends. */
(function (root) {
  'use strict';
  const D = root.LabDSP, CM = root.LabColormaps, EMPTY_DB = -200;

  function cmapBytes(name) {
    const hx = CM[name] || CM.viridis, b = new Uint8Array(256 * 4);
    for (let i = 0; i < 256; i++) { b[i * 4] = parseInt(hx.substr(i * 6, 2), 16); b[i * 4 + 1] = parseInt(hx.substr(i * 6 + 2, 2), 16); b[i * 4 + 2] = parseInt(hx.substr(i * 6 + 4, 2), 16); b[i * 4 + 3] = 255; }
    return b;
  }
  /* spectrum rows carry uint8 levels in 0.5 dB steps from -100 dB (spectrum-packet): dB = floor + step * level */
  function u8ToDb(u8, out, floor, step) { const f = floor == null ? -100 : floor, s = step == null ? 0.5 : step; for (let i = 0; i < u8.length; i++) out[i] = f + s * u8[i]; return out; }

  class Waterfall {
    /* o: {mode, nBins, nCols, fs, binFreqs, fmin, fmax, yScale, cmap, lo, hi, t0, t1, rings, layers}
       t0, t1: visible fraction of the history, 0..1, oldest..newest. layers: [{ring, x, y, w, h}] in canvas pixels, y from the top; default one layer
       over the whole canvas showing ring 0. binFreqs: centre frequencies (Hz) when the bins are not linear. */
    constructor(canvas, o) {
      this.canvas = canvas; this.nBins = o.nBins; this.nCols = o.nCols; this.fs = o.fs; this.binFreqs = o.binFreqs || null;
      this.view = { fmin: o.fmin, fmax: o.fmax, yScale: o.yScale || 'lin', cmap: o.cmap || 'viridis', lo: o.lo == null ? -100 : o.lo, hi: o.hi == null ? -20 : o.hi, t0: o.t0 || 0, t1: o.t1 == null ? 1 : o.t1 };
      this.rings = [];
      for (let i = 0; i < (o.rings || 1); i++) this.rings.push({ head: -1, filled: 0, raw: new Float32Array(this.nCols * this.nBins).fill(EMPTY_DB) });
      this.autoLayer = !o.layers;
      this.layers = (o.layers || [{ ring: 0, x: 0, y: 0, w: canvas.width, h: canvas.height }]).map((l) => Object.assign({}, l));
      this.dirty = true; this.columnsPushed = 0; this.lost = false; this.onlost = null; this.kind = 'canvas';
      if (o.mode !== 'canvas') { try { this._initGL(); this.kind = 'webgl'; } catch (e) { this.glError = String((e && e.message) || e); this.gl = null; } }
      if (this.kind === 'canvas') this._initCanvas();
    }
    get head() { return this.rings[0].head; }
    get filled() { return this.rings[0].filled; }

    // ---------------------------------------------------------------- shared
    _rowMap(h) {
      const H = Math.max(2, h), v = this.view;
      return this.binFreqs ? D.makeRowMapFreqs(v.yScale, H, this.binFreqs, v.fmin, v.fmax) : D.makeRowMap(v.yScale, H, this.nBins, this.fs, v.fmin, v.fmax);
    }
    setView(v) {
      const prev = this.view; this.view = Object.assign({}, prev, v);
      if (this.kind === 'webgl') { if (v.cmap && v.cmap !== prev.cmap) this._uploadCmap(); if (v.yScale || v.fmin != null || v.fmax != null) this._uploadRows(); }
      else if (v.cmap || v.lo != null || v.hi != null || v.yScale || v.fmin != null || v.fmax != null) this._recolourAll();
      this.dirty = true;
    }
    /* new canvas size; a default (single) layer follows it, explicit layers are placed by the caller with setLayers() */
    resize(w, h) {
      this.canvas.width = w; this.canvas.height = h;
      if (this.autoLayer) Object.assign(this.layers[0], { x: 0, y: 0, w, h });
      this._relayout(); this.dirty = true;
    }
    setLayers(layers) { this.autoLayer = false; this.layers = layers.map((l) => Object.assign({}, l)); this._relayout(); this.dirty = true; }
    setLayerRing(i, ring) { this.layers[i].ring = ring; if (this.kind === 'canvas') this._recolourLayer(this.layers[i]); this.dirty = true; }
    _relayout() { if (this.kind === 'webgl') { if (!this.lost) { this.gl.viewport(0, 0, this.canvas.width, this.canvas.height); this._uploadRows(); } } else this._resizeCanvasBufs(); }
    clear() {
      for (const r of this.rings) { r.head = -1; r.filled = 0; r.raw.fill(EMPTY_DB); }
      this.columnsPushed = 0; if (this.kind === 'webgl') this._clearGL(); else this._clearCanvas(); this.dirty = true;
    }
    /* pushColumn(db) feeds ring 0, pushColumn(ring, db) feeds the given ring; db = nBins dB values, lowest frequency first */
    pushColumn(a, b) {
      const ri = typeof a === 'number' ? a : 0, db = typeof a === 'number' ? b : a, r = this.rings[ri];
      r.head = (r.head + 1) % this.nCols; r.filled = Math.min(this.nCols, r.filled + 1); this.columnsPushed++;
      r.raw.set(db, r.head * this.nBins);
      if (this.kind === 'webgl') this._pushGL(ri, r.head); else this._pushCanvas(ri, r.head);
      this.dirty = true;
    }
    /* one draw plus gl.finish(): the first use of WebGL in a fresh browser spends up to 2 s initialising the GPU process; pay it here, before a stream starts */
    warm() { if (this.kind !== 'webgl' || this.lost) return 0; const t = performance.now(); this._drawGL(); this.gl.finish(); this.dirty = true; return performance.now() - t; }
    render() { if (!this.dirty) return false; this.dirty = false; if (this.kind === 'webgl') { if (!this.lost) this._drawGL(); } else this._drawCanvas(); return true; }
    visibleColumns() { const v = this.view; return { c0: v.t0 * this.nCols, c1: v.t1 * this.nCols }; }
    /* the columns of ring i, oldest first, as one Float32Array view per column */
    columns(i) { const r = this.rings[i], n = this.nCols, out = []; for (let c = 0; c < r.filled; c++) { const p = ((r.head - (r.filled - 1 - c)) % n + n) % n; out.push(r.raw.subarray(p * this.nBins, (p + 1) * this.nBins)); } return out; }
    /* refill another waterfall (same nBins, nCols and ring count) from this one's CPU copy: used when a lost context is replaced by a Canvas2D renderer */
    replayInto(other) { for (let i = 0; i < this.rings.length; i++) for (const col of this.columns(i)) other.pushColumn(i, col); }
    destroy() {
      if (this.kind === 'webgl' && this.gl) { const c = this.canvas; c.removeEventListener('webglcontextlost', this._lostFn); c.removeEventListener('webglcontextrestored', this._restoredFn); const e = this.gl.getExtension('WEBGL_lose_context'); if (e) e.loseContext(); }
    }

    // ---------------------------------------------------------------- WebGL2
    _initGL() {
      const gl = this.gl = this.canvas.getContext('webgl2', { antialias: false, alpha: false, preserveDrawingBuffer: false, powerPreference: 'low-power' });
      if (!gl) throw new Error('webgl2 unavailable');
      this._glSetup();
      this._lostFn = (e) => { e.preventDefault(); this.lost = true; if (this.onlost) this.onlost(this); };
      this._restoredFn = () => { this.lost = false; this._glSetup(); this.dirty = true; };
      this.canvas.addEventListener('webglcontextlost', this._lostFn); this.canvas.addEventListener('webglcontextrestored', this._restoredFn);
    }
    /* program, buffer, uniforms and textures: created at start and again after a restored context (every WebGL object dies with a lost one) */
    _glSetup() {
      const gl = this.gl;
      const vs = '#version 300 es\nin vec2 p; out vec2 uv; void main(){ uv = p * 0.5 + 0.5; gl_Position = vec4(p, 0.0, 1.0); }';
      const fs = '#version 300 es\nprecision highp float; precision highp int;\n' +
        'uniform highp sampler2D uData; uniform highp sampler2D uRows; uniform sampler2D uCmap;\n' +
        'uniform int uCols, uBins, uHead, uFilled; uniform float uLo, uHi, uT0, uT1; in vec2 uv; out vec4 o;\n' +
        'void main(){\n' +
        '  int H = textureSize(uRows, 0).x; int r = clamp(int(floor((1.0 - uv.y) * float(H))), 0, H - 1);\n' +
        '  float p = texelFetch(uRows, ivec2(r, 0), 0).r; int i0 = clamp(int(floor(p)), 0, uBins - 1), i1 = min(i0 + 1, uBins - 1); float t = clamp(p - float(i0), 0.0, 1.0);\n' +
        '  float c = (uT0 + (uT1 - uT0) * uv.x) * float(uCols); int ci = clamp(int(floor(c)), 0, uCols - 1);\n' +
        '  int age = ci - (uCols - uFilled);\n' +                                  // columns older than the history read as empty
        '  if (age < 0) { o = vec4(0.0314, 0.0392, 0.0471, 1.0); return; }\n' +
        '  int col = (uHead - (uCols - 1 - ci) + uCols * 2) % uCols;\n' +          // ci = uCols-1 is the newest column (at uHead)
        '  float v = mix(texelFetch(uData, ivec2(col, i0), 0).r, texelFetch(uData, ivec2(col, i1), 0).r, t);\n' +
        '  float u = clamp((v - uLo) / (uHi - uLo), 0.0, 1.0); o = vec4(texture(uCmap, vec2(u, 0.5)).rgb, 1.0);\n}';
      const sh = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s)); return s; };
      const pr = this.prog = gl.createProgram(); gl.attachShader(pr, sh(gl.VERTEX_SHADER, vs)); gl.attachShader(pr, sh(gl.FRAGMENT_SHADER, fs)); gl.linkProgram(pr);
      if (!gl.getProgramParameter(pr, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(pr));
      gl.useProgram(pr);
      const buf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buf); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
      const loc = gl.getAttribLocation(pr, 'p'); gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
      this.U = {}; for (const n of ['uData', 'uRows', 'uCmap', 'uCols', 'uBins', 'uHead', 'uFilled', 'uLo', 'uHi', 'uT0', 'uT1']) this.U[n] = gl.getUniformLocation(pr, n);
      gl.uniform1i(this.U.uData, 0); gl.uniform1i(this.U.uRows, 1); gl.uniform1i(this.U.uCmap, 2); gl.uniform1i(this.U.uCols, this.nCols); gl.uniform1i(this.U.uBins, this.nBins);
      gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
      this._glTextures();
    }
    _tex(unit, filter) {
      const gl = this.gl, t = gl.createTexture(); gl.activeTexture(gl.TEXTURE0 + unit); gl.bindTexture(gl.TEXTURE_2D, t);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, filter); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, filter);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE); return t;
    }
    /* the CPU copy is column after column (the order of a push); a whole-texture upload wants rows of nCols values per bin */
    _texels(r) { const n = this.nCols, b = this.nBins, t = new Float32Array(n * b); for (let c = 0; c < n; c++) for (let i = 0; i < b; i++) t[i * n + c] = r.raw[c * b + i]; return t; }
    /* all textures are created here, after the context exists or was restored; the CPU copies refill the data textures */
    _glTextures() {
      const gl = this.gl;
      this.texData = this.rings.map((r) => { const t = this._tex(0, gl.NEAREST); gl.texImage2D(gl.TEXTURE_2D, 0, gl.R32F, this.nCols, this.nBins, 0, gl.RED, gl.FLOAT, this._texels(r)); return t; });
      this.texCmap = this._tex(2, gl.LINEAR); this._uploadCmap();
      for (const l of this.layers) l.texRows = null;
      this._uploadRows();
    }
    _uploadCmap() { const gl = this.gl; gl.activeTexture(gl.TEXTURE2); gl.bindTexture(gl.TEXTURE_2D, this.texCmap); gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, 256, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, cmapBytes(this.view.cmap)); }
    _uploadRows() {
      const gl = this.gl;
      for (const l of this.layers) {
        if (!l.texRows) l.texRows = this._tex(1, gl.NEAREST);
        const m = this._rowMap(l.h); gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, l.texRows); gl.texImage2D(gl.TEXTURE_2D, 0, gl.R32F, m.length, 1, 0, gl.RED, gl.FLOAT, Float32Array.from(m));
      }
    }
    _clearGL() { const gl = this.gl; if (this.lost) return; this.rings.forEach((r, i) => { gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, this.texData[i]); gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, this.nCols, this.nBins, gl.RED, gl.FLOAT, this._texels(r)); }); }
    _pushGL(ri, head) { if (this.lost) return; const gl = this.gl; gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, this.texData[ri]); gl.texSubImage2D(gl.TEXTURE_2D, 0, head, 0, 1, this.nBins, gl.RED, gl.FLOAT, this.rings[ri].raw.subarray(head * this.nBins, (head + 1) * this.nBins)); }
    _drawGL() {
      const gl = this.gl, v = this.view, H = this.canvas.height;
      gl.viewport(0, 0, this.canvas.width, H); gl.clearColor(0.0314, 0.0392, 0.0471, 1); gl.clear(gl.COLOR_BUFFER_BIT);
      gl.uniform1f(this.U.uLo, v.lo); gl.uniform1f(this.U.uHi, v.hi); gl.uniform1f(this.U.uT0, v.t0); gl.uniform1f(this.U.uT1, v.t1);
      for (const l of this.layers) {
        if (!(l.w > 0 && l.h > 0)) continue;
        const r = this.rings[l.ring];
        gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, this.texData[l.ring]); gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, l.texRows);
        gl.uniform1i(this.U.uHead, Math.max(0, r.head)); gl.uniform1i(this.U.uFilled, r.filled);
        gl.viewport(l.x, H - (l.y + l.h), l.w, l.h); gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      }
    }

    // ---------------------------------------------------------------- Canvas2D
    _initCanvas() { this.ctx = this.canvas.getContext('2d', { alpha: false }); this._resizeCanvasBufs(true); }
    _resizeCanvasBufs(first) {
      this.lut = cmapBytes(this.view.cmap);
      for (const l of this.layers) {
        const H = Math.max(2, l.h);
        l.buf = document.createElement('canvas'); l.buf.width = this.nCols; l.buf.height = H; l.bctx = l.buf.getContext('2d', { alpha: false });
        l.colImg = l.bctx.createImageData(1, H); l.rowMap = this._rowMap(H); l.tmp = new Float64Array(H);
        if (!first) this._recolourLayer(l); else { l.bctx.fillStyle = '#000'; l.bctx.fillRect(0, 0, this.nCols, H); }
      }
    }
    _clearCanvas() { for (const l of this.layers) { l.bctx.fillStyle = '#000'; l.bctx.fillRect(0, 0, this.nCols, l.buf.height); } }
    _paintColumn(l, x, db) {
      const H = l.buf.height, v = this.view, rows = D.resampleColumn(db, l.rowMap, l.tmp), d = l.colImg.data, span = v.hi - v.lo, lut = this.lut;
      for (let r = 0; r < H; r++) { const u = Math.min(255, Math.max(0, Math.round((rows[r] - v.lo) / span * 255))) * 4; d[r * 4] = lut[u]; d[r * 4 + 1] = lut[u + 1]; d[r * 4 + 2] = lut[u + 2]; d[r * 4 + 3] = 255; }
      l.bctx.putImageData(l.colImg, x, 0);
    }
    _pushCanvas(ri, head) { const r = this.rings[ri], col = r.raw.subarray(head * this.nBins, (head + 1) * this.nBins); for (const l of this.layers) if (l.ring === ri) this._paintColumn(l, head, col); }
    _recolourLayer(l) {
      if (!l.buf) return; l.rowMap = this._rowMap(l.buf.height); this.lut = cmapBytes(this.view.cmap);
      const r = this.rings[l.ring]; for (let x = 0; x < this.nCols; x++) this._paintColumn(l, x, r.raw.subarray(x * this.nBins, (x + 1) * this.nBins));
    }
    _recolourAll() { for (const l of this.layers) this._recolourLayer(l); }
    _drawCanvas() {
      const ctx = this.ctx, v = this.view, n = this.nCols; ctx.fillStyle = '#080a0c'; ctx.fillRect(0, 0, this.canvas.width, this.canvas.height); ctx.imageSmoothingEnabled = false;
      const a0 = v.t0 * n, a1 = v.t1 * n;
      for (const l of this.layers) {
        if (!(l.w > 0 && l.h > 0) || !l.buf) continue;
        const r = this.rings[l.ring], newest = r.head;                     // logical column ci (n-1 = newest) -> physical (head - (n-1-ci)) mod n
        const seg = (c0, c1) => {                                           // draw logical columns [c0, c1) as one or two physical pieces
          const ph0 = ((newest - (n - 1 - c0)) % n + n) % n, len = c1 - c0, first = Math.min(len, n - ph0), sx = (cc) => l.x + (cc - a0) / (a1 - a0) * l.w;
          ctx.drawImage(l.buf, ph0, 0, first, l.buf.height, sx(c0), l.y, first / (a1 - a0) * l.w, l.h);
          if (len > first) ctx.drawImage(l.buf, 0, 0, len - first, l.buf.height, sx(c0 + first), l.y, (len - first) / (a1 - a0) * l.w, l.h);
        };
        const lo = Math.max(Math.floor(a0), n - r.filled), hi = Math.min(Math.ceil(a1), n);
        if (hi > lo) seg(lo, hi);
      }
    }
  }

  // ---------------------------------------------------------------- axis helpers shared by the app
  function fmtHz(f) { return f >= 1000 ? (f / 1000).toFixed(f % 1000 === 0 ? 0 : 1) + 'k' : String(Math.round(f)); }
  function freqTicks(scale, fmin, fmax) {
    const cand = [50, 100, 200, 300, 500, 1000, 2000, 3000, 4000, 5000, 6000, 8000, 10000, 12000, 14000, 16000, 20000];
    if (scale === 'lin') { const step = fmax - fmin > 8000 ? 2000 : fmax - fmin > 3000 ? 1000 : 500, out = []; for (let f = Math.ceil(fmin / step) * step; f <= fmax; f += step) out.push(f); return out; }
    return cand.filter((f) => f >= fmin && f <= fmax);
  }
  function freqToY(scale, f, fmin, fmax, H) { const S = D.SCALES[scale], lo = S.fwd(fmin), hi = S.fwd(fmax); return (1 - (S.fwd(f) - lo) / (hi - lo)) * (H - 1); }
  function yToFreq(scale, y, fmin, fmax, H) { const S = D.SCALES[scale], lo = S.fwd(fmin), hi = S.fwd(fmax); return S.inv(lo + (hi - lo) * (1 - y / (H - 1))); }

  root.LabRender = { Waterfall, fmtHz, freqTicks, freqToY, yToFreq, cmapBytes, u8ToDb };
})(typeof self !== 'undefined' ? self : this);
