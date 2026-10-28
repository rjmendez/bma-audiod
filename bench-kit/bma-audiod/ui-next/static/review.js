/* Clip viewer: one view engine for a stored clip or a review item, mounted inside the dashboard's Review view.
   Linked views: the spectrogram box sets the loop and the band-pass of the audition engine; the beam chips set the signal both the picture and the
   sound are made from. Verdict records follow contract verdict-labels 1.1.0 (the evidence block is a proposal, see docs/ui-next/DATA-MODEL.md).
   Engine globals (LabDSP, LabRender, LabAudition) are loaded by index.html before this module. */
const D = self.LabDSP, R = self.LabRender, A = self.LabAudition, FS = 48000, INT16_POWER = 32768 * 32768, EPOCH_MS = 1793000000000;

/* mountViewer(root, hooks): root = the Review view element (all lookups are scoped to it); hooks.changed() runs after a verdict.
   hooks.src is the data source (adapter.js): clip audio, the review queue, verdicts. Returns {S, setClips, openBuffer, setTab, resume}. */
export function mountViewer(root, hooks) {
  hooks = hooks || {};
  const src = hooks.src, FS_BEAMS = [0, 1, 2, 3, 4, 5, 6, 7];
  const $ = (id) => root.querySelector('#' + id), q = new URLSearchParams(location.search);
  const S = {
    mode: 'select', tok: null, font: 'system-ui',
    tab: 'clips', clips: [], avail: null, depth: 0, clip: null, buf: null, beams: null, dur: 0, sel: new Set([0]), a: 0, b: 1, tf: 'stft', fr: null, wf: null, view: { t0: 0, t1: 1 },
    box: null, loopOn: false, truth: true, mode: 'select', marks: [], aud: new A.Audition(), review: [], rvi: -1, cursor: null, dirty: true,
    stats: { frames: 0, cols: 0, dropped: 0, rafs: 0, t0: performance.now(), analyseMs: 0, colMs: 0, lastRafT: 0, rafDt: [] }, errors: [],
  };
  window.addEventListener('error', (e) => { S.errors.push(String(e.message)); }); window.addEventListener('unhandledrejection', (e) => { S.errors.push(String(e.reason)); });

  // ------------------------------------------------------------------ transforms (value rows are dB unless unit says otherwise)
  const cfg = () => ({ n: +$('nfft').value, hop: +$('hop').value, pad: +$('pad').value, win: $('win').value });
  const melFB = (c, nb, scale) => D.filterbank(scale, nb, c.n * c.pad, FS, 100, 15500);
  function viaBands(x, c, scale, nb, pcen) {
    const s = D.stft(x, { n: c.n, hop: c.hop, pad: c.pad, window: c.win }), F = melFB(c, nb, scale), pw = new Float64Array(s.mag.length);
    for (let i = 0; i < pw.length; i++) pw[i] = s.mag[i] * s.mag[i];
    const E = D.applyFilterbank(pw, s.nFrames, s.nBins, F);
    let val, unit = 'dB';
    if (pcen) { const e2 = E.map(v => v * INT16_POWER); val = D.pcen(e2, s.nFrames, nb, { s: D.pcenSmoothing(0.4, FS / c.hop), init: 'first' }); unit = 'pcen'; }
    else { val = new Float64Array(E.length); for (let i = 0; i < E.length; i++) val[i] = 10 * Math.log10(Math.max(E[i], 1e-14)); }
    return { val, nF: s.nFrames, nB: nb, binF: F.centres, t0: c.n / 2 / FS, hopS: c.hop / FS, unit };
  }
  const TF = {
    stft: { name: 'STFT', run(x, c) { const s = D.stft(x, { n: c.n, hop: c.hop, pad: c.pad, window: c.win }); return { val: D.toDb(s.mag), nF: s.nFrames, nB: s.nBins, binF: null, t0: c.n / 2 / FS, hopS: c.hop / FS, unit: 'dB' }; } },
    mt: { name: 'Multitaper (K=4)', run(x, c) { const s = D.multitaper(x, { n: c.n, hop: c.hop, K: 4, pad: c.pad }); return { val: D.toDb(s.mag), nF: s.nFrames, nB: s.nBins, binF: null, t0: c.n / 2 / FS, hopS: c.hop / FS, unit: 'dB' }; } },
    reas: { name: 'Reassigned', run(x, c) { const s = D.reassigned(x, { n: c.n, hop: c.hop, pad: c.pad, floor: 1e-6 }); return { val: D.toDb(s.mag, -120), nF: s.nFrames, nB: s.nBins, binF: null, t0: c.n / 2 / FS, hopS: c.hop / FS, unit: 'dB' }; } },
    cqt: { name: 'Constant-Q (24 bins/oct)', run(x, c) { const s = D.cqt(x, { fs: FS, fmin: 200, bpo: 24, nBins: 144, hop: c.hop, window: 'hann' }); return { val: D.toDb(s.mag), nF: s.nFrames, nB: s.nBins, binF: s.freqs, t0: 0, hopS: c.hop / FS, unit: 'dB' }; } },
    morlet: { name: 'Morlet wavelet (12 bins/oct)', run(x, c) { const s = D.cqt(x, { fs: FS, fmin: 200, bpo: 12, nBins: 72, hop: c.hop, window: 'gauss', qScale: 0.5 }); return { val: D.toDb(s.mag), nF: s.nFrames, nB: s.nBins, binF: s.freqs, t0: 0, hopS: c.hop / FS, unit: 'dB' }; } },
    mel: { name: 'Mel (64 bands)', run: (x, c) => viaBands(x, c, 'mel', 64, false) },
    erb: { name: 'ERB (48 bands)', run: (x, c) => viaBands(x, c, 'erb', 48, false) },
    bark: { name: 'Bark (24 bands)', run: (x, c) => viaBands(x, c, 'bark', 24, false) },
    pcen: { name: 'PCEN (mel 64)', run: (x, c) => viaBands(x, c, 'mel', 64, true) },
    bgn: { name: 'Background-normalised', run(x, c) { const s = D.stft(x, { n: c.n, hop: c.hop, pad: c.pad, window: c.win }); return { val: D.bgNorm(D.toDb(s.mag), s.nFrames, s.nBins, { stepDb: 0.1 }), nF: s.nFrames, nB: s.nBins, binF: null, t0: c.n / 2 / FS, hopS: c.hop / FS, unit: 'dB over background', fixed: [-5, 25] }; } },
    harm: { name: 'Harmonic part (HPSS)', run(x, c) { const s = D.stft(x, { n: c.n, hop: c.hop, pad: c.pad, window: c.win }), h = D.hpss(s.mag, s.nFrames, s.nBins, { kernelTime: 17, kernelFreq: 17 }); return { val: D.toDb(h.harm), nF: s.nFrames, nB: s.nBins, binF: null, t0: c.n / 2 / FS, hopS: c.hop / FS, unit: 'dB' }; } },
    perc: { name: 'Percussive part (HPSS)', run(x, c) { const s = D.stft(x, { n: c.n, hop: c.hop, pad: c.pad, window: c.win }), h = D.hpss(s.mag, s.nFrames, s.nBins, { kernelTime: 17, kernelFreq: 17 }); return { val: D.toDb(h.perc), nF: s.nFrames, nB: s.nBins, binF: null, t0: c.n / 2 / FS, hopS: c.hop / FS, unit: 'dB' }; } },
  };
  function fillSelects() {
    const tf = $('tf'), keep = tf.value || S.tf; tf.innerHTML = '';
    for (const k of Object.keys(TF)) { const o = document.createElement('option'); o.value = k; o.textContent = TF[k].name; tf.appendChild(o); }
    tf.value = TF[keep] ? keep : 'stft'; S.tf = tf.value;
    const w = $('win'); if (!w.options.length) for (const n of D.WINDOWS) { const o = document.createElement('option'); o.textContent = n; w.appendChild(o); }
  }

  // ------------------------------------------------------------------ signal, analysis, waterfall
  function displaySignal() {
    const list = [...S.sel], n = S.beams[0].length;
    if (list.length === 1) return Float64Array.from(S.beams[list[0]]);
    const x = new Float64Array(n); for (const b of list) { const s = S.beams[b]; for (let i = 0; i < n; i++) x[i] += s[i] / list.length; } return x;
  }
  function pct(a, p) { const s = Float64Array.from(a).sort(); return s[Math.min(s.length - 1, Math.floor(p * (s.length - 1)))]; }
  function autoRange(r) {
    if (r.fixed) return r.fixed.slice();
    if (r.unit === 'pcen') return [0, Math.max(1, pct(r.val, 0.995))];
    const lo = pct(r.val, 0.02), hi = pct(r.val, 0.998) + 3; return [Math.max(lo, hi - 90), hi];
  }
  function newCanvas() { const old = $('rwf'), c = old.cloneNode(false); try { const gl = old.getContext && old.getContext('webgl2'); if (gl) { const e = gl.getExtension('WEBGL_lose_context'); if (e) e.loseContext(); } } catch (e) { /* none */ } old.replaceWith(c); return c; }
  function sizeCanvas(c) { const st = $('stage'), dpr = Math.min(2, devicePixelRatio || 1); c.width = Math.max(64, Math.round(st.clientWidth * dpr)); c.height = Math.max(64, Math.round(st.clientHeight * dpr)); const ov = $('ov'); ov.width = c.width; ov.height = c.height; }
  function viewParams() { return { fmin: 100, fmax: 15500, yScale: $('scale').value, cmap: $('cmap').value }; }
  function buildWaterfall(r, nCols) {
    const tGl = performance.now(); const c = newCanvas(); sizeCanvas(c); const v = viewParams(), rg = $('auto').checked ? autoRange(r) : [+$('lo').value, +$('hi').value];
    S.wf = new R.Waterfall(c, { mode: q.get('gl') === '0' ? 'canvas' : 'webgl', nBins: r.nB, nCols, fs: FS, binFreqs: r.binF, fmin: v.fmin, fmax: v.fmax, yScale: v.yScale, cmap: v.cmap, lo: rg[0], hi: rg[1], t0: S.view.t0, t1: S.view.t1 });
    $('lo').value = Math.round(rg[0] * 10) / 10; $('hi').value = Math.round(rg[1] * 10) / 10; $('unit').textContent = r.unit; S.dirty = true; S.stats.glInitMs = performance.now() - tGl; S.stats.glWarmMs = S.wf.warm(); S.firstDraw = true;
  }
  function analyse() {
    if (!S.beams) return; const t0 = performance.now(), x = displaySignal(), r = TF[S.tf].run(x, cfg()); S.fr = r; S.stats.analyseMs = performance.now() - t0;
    buildWaterfall(r, r.nF); for (let f = 0; f < r.nF; f++) S.wf.pushColumn(r.val.subarray(f * r.nB, (f + 1) * r.nB));
    S.level = new Float64Array(r.nF); S.flux = D.flux(r.val, r.nF, r.nB);
    const half = +$('hop').value; for (let f = 0; f < r.nF; f++) { const c = Math.round(r.t0 * FS + f * r.hopS * FS); let s = 0, n = 0; for (let i = Math.max(0, c - half); i < Math.min(x.length, c + half); i++, n++) s += x[i] * x[i]; S.level[f] = 10 * Math.log10(Math.max(s / Math.max(1, n), 1e-14)); }
    status(); S.dirty = true;
  }
  const rangeLabel = () => S.fr ? [S.fr.t0 + S.view.t0 * S.fr.nF * S.fr.hopS, S.fr.t0 + S.view.t1 * S.fr.nF * S.fr.hopS] : [0, 1];
  function timeOfX(x01) { const r = S.fr, v = S.view; return r.t0 + (v.t0 + (v.t1 - v.t0) * x01) * r.nF * r.hopS; }
  function xOfTime(t) { const r = S.fr, v = S.view; return ((t - r.t0) / (r.nF * r.hopS) - v.t0) / (v.t1 - v.t0); }

  // ------------------------------------------------------------------ overlay
  const ov = () => $('ov').getContext('2d');
  function drawAxes(g, W, H, dpr) {                         // static part: frequency and time ticks, cached until the view changes
    const v = viewParams(); const T = S.tok; g.font = (12 * dpr) + 'px ' + S.font; g.fillStyle = T.ink; g.strokeStyle = T.grid; g.lineWidth = 1;
    for (const f of R.freqTicks(v.yScale, v.fmin, v.fmax)) { const y = R.freqToY(v.yScale, f, v.fmin, v.fmax, H); g.beginPath(); g.moveTo(0, y); g.lineTo(6 * dpr, y); g.stroke(); g.fillText(R.fmtHz(f), 8 * dpr, Math.min(H - 3, Math.max(12 * dpr, y + 4 * dpr))); }
    const t0 = timeOfX(0), t1 = timeOfX(1), span = t1 - t0, step = [0.1, 0.2, 0.5, 1, 2, 5, 10].find(s => span / s < 9) || 10;
    for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) { const x = xOfTime(t) * W; g.beginPath(); g.moveTo(x, H); g.lineTo(x, H - 6 * dpr); g.stroke(); g.fillText(t.toFixed(step < 1 ? 1 : 0) + ' s', x + 3 * dpr, H - 8 * dpr); }
  }
  function drawOverlay() {
    const cv = $('ov'), g = ov(), W = cv.width, H = cv.height; g.clearRect(0, 0, W, H); if (!S.fr || !S.wf) return;
    const v = viewParams(), dpr = W / $('stage').clientWidth, key = [S.tok.ink, S.view.t0, S.view.t1, v.yScale, W, H, S.fr.nF, S.fr.hopS, S.fr.t0].join('|');
    if (S.axKey !== key) { S.axKey = key; S.axCanvas = S.axCanvas || document.createElement('canvas'); S.axCanvas.width = W; S.axCanvas.height = H; drawAxes(S.axCanvas.getContext('2d'), W, H, dpr); }
    const T = S.tok; g.drawImage(S.axCanvas, 0, 0); g.font = (12 * dpr) + 'px ' + S.font; g.fillStyle = T.ink; g.lineWidth = 1;
    const box = (b, color, dash, label) => { const x0 = xOfTime(b.t0) * W, x1 = xOfTime(b.t1) * W, y0 = R.freqToY(v.yScale, b.f1, v.fmin, v.fmax, H), y1 = R.freqToY(v.yScale, b.f0, v.fmin, v.fmax, H); g.save(); g.strokeStyle = color; g.fillStyle = color; g.lineWidth = 1.5 * dpr; g.setLineDash(dash ? [5 * dpr, 4 * dpr] : []); g.strokeRect(x0, y0, x1 - x0, y1 - y0); if (label) g.fillText(label, x0 + 3 * dpr, y0 - 3 * dpr); g.restore(); };
    const clip = S.clip; if (S.truth && clip && clip.truth) for (const t of clip.truth) box(t, T.truth, true, S.tab === 'review' ? '' : t.kind);
    if (S.tab === 'review' && S.rvi >= 0) { const it = S.review[S.rvi]; box(it, T.claim, true, S.rv && S.rv.revealed ? it.model.species : 'claimed box'); }
    for (const m of S.marks) box(m, T.mark, false, '');
    if (S.box) box(S.box, T.sel, false, '');
    if (S.cursor != null) { const x = xOfTime(S.cursor) * W; g.strokeStyle = T.cursor; g.beginPath(); g.moveTo(x, 0); g.lineTo(x, H); g.stroke(); }
    if (S.aud.playing) { const x = xOfTime(S.aud.position) * W; g.strokeStyle = T.play; g.lineWidth = 2 * dpr; g.beginPath(); g.moveTo(x, 0); g.lineTo(x, H); g.stroke(); }
    drawStrip($('sLevel'), S.level, 'level dBFS', -100, -20); drawStrip($('sFlux'), S.flux, 'flux', 0, null);
  }
  function drawStrip(cv, arr, label, lo, hi) {
    const dpr = Math.min(2, devicePixelRatio || 1), W = Math.round(cv.clientWidth * dpr), H = Math.round(56 * dpr), T = S.tok; if (cv.width !== W) cv.width = W; if (cv.height !== H) cv.height = H;
    const g = cv.getContext('2d'); g.clearRect(0, 0, W, H); g.fillStyle = T.muted; g.font = (12 * dpr) + 'px ' + S.font; g.fillText(label, 6 * dpr, 15 * dpr); if (!arr || !S.fr) return;
    const n = arr.length, c0 = S.view.t0 * n, c1 = S.view.t1 * n; let mx = hi == null ? 1e-9 : hi, mn = lo; if (hi == null) for (let i = 0; i < n; i++) mx = Math.max(mx, arr[i]);
    g.strokeStyle = T.line; g.lineWidth = 1.5 * dpr; g.beginPath(); for (let x = 0; x < W; x++) { const c = Math.min(n - 1, Math.max(0, Math.floor(c0 + (c1 - c0) * x / W))), y = H - 4 * dpr - (H - 22 * dpr) * Math.min(1, Math.max(0, (arr[c] - mn) / (mx - mn))); if (x) g.lineTo(x, y); else g.moveTo(x, y); } g.stroke();
    if (S.aud.playing) { const px = xOfTime(S.aud.position) * W; g.strokeStyle = T.play; g.beginPath(); g.moveTo(px, 0); g.lineTo(px, H); g.stroke(); }
  }
  function status() {
    const b = S.box, r = S.fr; if (!r) return; const parts = [];
    parts.push((S.wf ? S.wf.kind : '-') + ' | ' + TF[S.tf].name + ' | ' + r.nB + ' bins x ' + r.nF + ' cols'); if (b) parts.push('box ' + b.t0.toFixed(2) + '-' + b.t1.toFixed(2) + ' s, ' + R.fmtHz(b.f0) + '-' + R.fmtHz(b.f1) + ' Hz');
    root.dataset.status = parts.join('  |  ');
  }

  // ------------------------------------------------------------------ audition wiring
  function applyAudition() {
    const a = S.aud; a.mixBeams([...S.sel]); a.autoLevel = $('lvl').checked;
    a.cfg.mode = { kind: $('amode').value, target: +$('atarget').value, div: +$('adiv').value };
    a.cfg.band = S.box && $('solo').checked ? [Math.min(S.box.f0, S.box.f1), Math.max(S.box.f0, S.box.f1)] : null;
    a.loop = S.box && S.loopOn ? [S.box.t0, S.box.t1] : null; a.setRate(+$('rate').value);
    $('loop').setAttribute('aria-pressed', S.loopOn ? 'true' : 'false'); if (a.playing) a._restartIfPlaying();
  }
  async function togglePlay() {
    const a = S.aud; if (!S.buf) return;
    if (a.playing || a.media) { a.pause(); $('play').textContent = 'Play'; return; }
    applyAudition(); if ($('keep').checked && +$('rate').value !== 1) await a.playPreserved(+$('rate').value); else await a.play(S.cursor != null && !S.loopOn ? S.cursor : undefined);
    $('play').textContent = 'Pause';
  }
  // ------------------------------------------------------------------ clips
  /* mono review audio -> 8 channels with the signal in the clip's own beam: every view and the audition chain keep one shape, the other beams are switched off */
  function toEight(mono, beam) { const k = beam >= 1 && beam <= 8 ? beam - 1 : 0, out = new AudioBuffer({ length: mono.length, sampleRate: mono.sampleRate, numberOfChannels: 8 }); out.copyToChannel(mono.getChannelData(0), k); return [out, new Set([k])]; }
  async function loadClip(id, item) {
    S.aud.stop(); $('play').textContent = 'Play'; const meta = item ? { id, title: id, truth: [] } : S.clips.find(c => c.id === id); S.clip = meta; $('truth-row').hidden = !(meta && meta.truth && meta.truth.length);
    const got = await src.clipAudio(id, item), off = new OfflineAudioContext(8, 1, FS), dec = await off.decodeAudioData(got.bytes), [buf, avail] = got.beam == null ? [dec, null] : toEight(dec, got.beam);
    S.avail = avail; S.buf = buf; S.beams = []; for (let c = 0; c < 8; c++) S.beams.push(buf.getChannelData(c)); if (avail) { S.sel = new Set(avail); refreshBeams(); } S.dur = buf.duration; S.aud.load(buf); S.view = { t0: 0, t1: 1 }; S.box = null; S.cursor = null; S.marks = [];
    $('pan').value = 0; analyse(); applyAudition();
  }
  function setView(t0f, t1f) { const span = Math.min(1, Math.max(0.02, t1f - t0f)); t0f = Math.min(1 - span, Math.max(0, t0f)); S.view = { t0: t0f, t1: t0f + span }; if (S.wf) S.wf.setView({ t0: S.view.t0, t1: S.view.t1 }); $('pan').value = Math.round(1000 * t0f / Math.max(1e-9, 1 - span)); S.dirty = true; }
  function zoom(f) { const c = (S.view.t0 + S.view.t1) / 2, s = (S.view.t1 - S.view.t0) * f; setView(c - s / 2, c + s / 2); }
  function focusTime(t0, t1, pad) { const r = S.fr, tot = r.nF * r.hopS, a = (t0 - pad - r.t0) / tot, b = (t1 + pad - r.t0) / tot; setView(a, b); }

  // ------------------------------------------------------------------ pointer: select a box, pan, click to place the cursor
  function wire() {
    const cv = $('ov'); let drag = null;
    const pos = (e) => { const r = cv.getBoundingClientRect(); return { x: (e.clientX - r.left) / r.width, y: (e.clientY - r.top) / r.height }; };
    const fOfY = (y01) => R.yToFreq($('scale').value, y01 * (cv.height - 1), 100, 15500, cv.height);
    cv.addEventListener('pointerdown', (e) => { if (!S.fr) return; cv.setPointerCapture(e.pointerId); const p = pos(e); drag = { p0: p, t0: timeOfX(p.x), f0: fOfY(p.y), moved: false, v0: Object.assign({}, S.view) }; });
    cv.addEventListener('pointermove', (e) => {
      if (!drag) return; const p = pos(e); if (Math.abs(p.x - drag.p0.x) + Math.abs(p.y - drag.p0.y) > 0.01) drag.moved = true; if (!drag.moved) return;
      if (S.mode === 'pan') { const span = drag.v0.t1 - drag.v0.t0; setView(drag.v0.t0 - (p.x - drag.p0.x) * span, drag.v0.t0 - (p.x - drag.p0.x) * span + span); return; }
      const t1 = timeOfX(p.x), f1 = fOfY(p.y); S.box = { t0: Math.min(drag.t0, t1), t1: Math.max(drag.t0, t1), f0: Math.min(drag.f0, f1), f1: Math.max(drag.f0, f1) }; S.dirty = true; status();
    });
    cv.addEventListener('pointerup', () => { if (!drag) return; if (drag.moved && S.mode === 'select') applyAudition(); else if (!drag.moved) { S.cursor = Math.max(0, drag.t0); S.dirty = true; if (S.aud.playing) S.aud.seek(S.cursor); } drag = null; });
    cv.addEventListener('dblclick', () => { S.box = null; applyAudition(); S.dirty = true; status(); });
    cv.addEventListener('wheel', (e) => { if (!(e.ctrlKey || e.metaKey || e.shiftKey)) return; e.preventDefault(); const p = pos(e), a = timeOfX(p.x); zoom(e.deltaY > 0 ? 1.25 : 0.8); const r = S.fr, tot = r.nF * r.hopS, span = S.view.t1 - S.view.t0, cx = (a - r.t0) / tot; setView(cx - p.x * span, cx - p.x * span + span); }, { passive: false });
    $('zoomIn').onclick = () => zoom(0.6); $('zoomOut').onclick = () => zoom(1 / 0.6); $('pan').oninput = (e) => { const span = S.view.t1 - S.view.t0; setView((+e.target.value / 1000) * (1 - span), (+e.target.value / 1000) * (1 - span) + span); };
    $('mode').onclick = () => { S.mode = S.mode === 'select' ? 'pan' : 'select'; applyMode(); };
    const endDrag = () => { drag = null; }; cv.addEventListener('pointercancel', endDrag); cv.addEventListener('lostpointercapture', endDrag); addEventListener('blur', endDrag);
    for (const id of ['tf', 'win', 'nfft', 'hop', 'pad']) $(id).onchange = () => { S.tf = $('tf').value; analyse(); applyAudition(); };
    for (const id of ['scale', 'cmap']) $(id).onchange = () => { if (!S.wf) return; S.wf.setView(viewParams()); S.dirty = true; };
    for (const id of ['lo', 'hi']) $(id).onchange = () => { $('auto').checked = false; S.wf && S.wf.setView({ lo: +$('lo').value, hi: +$('hi').value }); S.dirty = true; };
    $('auto').onchange = () => { if ($('auto').checked && S.fr) { const rg = autoRange(S.fr); $('lo').value = rg[0].toFixed(0); $('hi').value = rg[1].toFixed(0); S.wf.setView({ lo: rg[0], hi: rg[1] }); S.dirty = true; } };
    $('truth').onchange = () => { S.truth = $('truth').checked; S.dirty = true; };
    for (const id of ['amode', 'atarget', 'adiv', 'solo', 'lvl', 'rate']) $(id).onchange = () => applyAudition();
    $('loop').onclick = () => { S.loopOn = !S.loopOn; applyAudition(); }; $('play').onclick = togglePlay;
    $('markBtn').onclick = markBox;
    for (const b of root.querySelectorAll('#rev button[data-v]')) b.onclick = () => verdict(b.dataset.v);
    for (const b of root.querySelectorAll('#tabs button')) b.onclick = () => setTab(b.dataset.tab);
    addEventListener('resize', () => { if (S.fr && !root.hidden) analyse(); });
    addEventListener('keydown', (e) => { if (!root.hidden) onKey(e); });
  }
  function buildBeams() {
    const el = $('rbeams'); el.innerHTML = '';
    for (let b = 0; b < 8; b++) { const x = document.createElement('button'); x.textContent = 'B' + (b + 1); x.dataset.b = b; x.title = 'Beam ' + (b + 1) + ' (key ' + (b + 1) + '; shift adds)'; x.onclick = (e) => pickBeam(b, e.shiftKey || e.ctrlKey || e.metaKey); el.appendChild(x); }
    const mk = (t, title, f) => { const x = document.createElement('button'); x.textContent = t; x.title = title; x.onclick = f; el.appendChild(x); return x; };
    mk('Mean', 'Mean of all 8 beams (M)', () => { S.sel = new Set(allBeams()); beamsChanged(); }); const sep = document.createElement('span'); sep.className = 'sep'; el.appendChild(sep);
    mk('A', 'Remember the current beam as A', () => { S.a = [...S.sel][0]; }); mk('B', 'Remember the current beam as B', () => { S.b = [...S.sel][0]; }); mk('A/B', 'Switch between A and B, level matched (Tab)', abToggle);
    refreshBeams();
  }
  const allBeams = () => (S.avail ? [...S.avail] : FS_BEAMS);                            // the beams that carry a signal: all 8, or the one beam of a mono review clip
  function refreshBeams() { for (const x of root.querySelectorAll('#rbeams button[data-b]')) { x.setAttribute('aria-pressed', S.sel.has(+x.dataset.b) ? 'true' : 'false'); x.disabled = !!S.avail && !S.avail.has(+x.dataset.b); } }
  let beamTimer = 0;
  function beamsChanged() { refreshBeams(); applyAudition(); clearTimeout(beamTimer); beamTimer = setTimeout(analyse, 120); }
  function pickBeam(b, add) { if (S.avail && !S.avail.has(b)) return; if (add) { if (S.sel.has(b) && S.sel.size > 1) S.sel.delete(b); else S.sel.add(b); } else S.sel = new Set([b]); beamsChanged(); }
  function abToggle() { if (S.avail) return; const cur = [...S.sel][0]; S.sel = new Set([cur === S.a ? S.b : S.a]); beamsChanged(); }

  // ------------------------------------------------------------------ marks and verdicts (records follow contract verdict-labels 1.1.0; the evidence block is a proposal)
  function markBox() { if (!S.box) return; S.marks.push(Object.assign({ transform: S.tf, beams: [...S.sel].map(b => b + 1) }, S.box)); S.dirty = true; $('rec').textContent = 'marks: ' + JSON.stringify(S.marks.map(m => ({ t: +m.t0.toFixed(2) + '-' + m.t1.toFixed(2), f: Math.round(m.f0) + '-' + Math.round(m.f1), transform: m.transform })), null, 0); }
  const hex16 = () => Array.from(crypto.getRandomValues(new Uint8Array(8)), b => b.toString(16).padStart(2, '0')).join('');
  async function verdict(v) {
    if (S.tab !== 'review' || S.rvi < 0 || S.busy) return; const it = S.review[S.rvi], wrong = v === 'wrong' ? $('wr').value : null, key = it.model.species.toLowerCase().replace(/ \(claim\)$/, '').replace(/[^a-z0-9 .-]/g, '').slice(0, 60), tms = EPOCH_MS + Math.round(it.t0 * 1000);
    const rec = { k: 'verdict', v: 1, label_id: hex16(), t_label: Date.now() / 1000, event_id: tms + '-' + key.replace(/ /g, '-'), t_start_ms: tms, species_key: key, verdict: v, wrong_reason: v === 'wrong' ? $('wr').value : null, corrected_key: null, basis: 'clip_visual', source: 'ui', stratum: null, p_include: null, queue_id: null, model: { conf_max: it.model.conf, n_beams: 1, best_beam: it.model.beam, group: null, in_region: null }, provenance: 'owner_verdict' };
    const evidence = { views: [...new Set([S.tf].concat(S.marks.map(m => m.transform)))], listened: S.aud.playing || S.aud.media != null || !!S.listenedOnce, box: S.box ? { t0_ms: Math.round(S.box.t0 * 1000), t1_ms: Math.round(S.box.t1 * 1000), f0_hz: Math.round(S.box.f0), f1_hz: Math.round(S.box.f1) } : null };
    S.rv = { revealed: true }; $('claim').textContent = 'model claimed: ' + it.model.species + ' (' + it.model.conf + ')'; $('rec').textContent = JSON.stringify({ record: rec, proposed_evidence: evidence }, null, 1);
    S.busy = true;                                                                  // one verdict at a time; marked done only once the source has taken it
    try { const next = await src.verdict(it, v, wrong, rec, evidence); it.done = v; (S.verdicts = S.verdicts || {})[it.id] = v; if (next) { S.depth = src.depth; S.busy = false; await replaceReview(next); } else renderList(); }
    catch (e) { S.errors.push('verdict post ' + e); $('claim').textContent = 'not saved: ' + e.message; } finally { S.busy = false; }
    S.dirty = true; if (hooks.changed) hooks.changed();
  }
  /* the server hands out review items itself (legacy): a new queue replaces the list, the first item opens */
  async function replaceReview(items) { S.review = items; S.rvi = -1; renderList(); if (items.length) await selectReview(0); else showEmpty(); }
  function showEmpty() { S.aud.pause(); S.buf = null; S.box = null; root.querySelector('.viewer').classList.add('png-only'); $('clip-png').hidden = true; $('rev').hidden = true; $('claim').textContent = 'Nothing left to review.'; }
  async function skip() {
    if (S.tab !== 'review' || S.rvi < 0 || S.busy) return; const it = S.review[S.rvi]; S.busy = true;
    try { const next = await src.skip(it); if (next) { S.depth = src.depth; S.busy = false; await replaceReview(next); } else { S.busy = false; await selectReview(S.rvi + 1); } }
    catch (e) { S.errors.push('skip ' + e); $('claim').textContent = 'not skipped: ' + e.message; } finally { S.busy = false; }
    if (hooks.changed) hooks.changed();
  }
  /* a verdict is refused while an item is still loading: its box and clip are not in place yet */
  async function selectReview(i) {
    if (i < 0 || i >= S.review.length) return; S.busy = true; try { await selectReviewItem(i); } finally { S.busy = false; }
  }
  async function selectReviewItem(i) {
    S.rvi = i; const it = S.review[i]; S.rv = { revealed: !$('blind').checked }; $('claim').textContent = ''; const png = $('clip-png'), vw = root.querySelector('.viewer');
    if (src.caps.clipList) { if (!S.clip || S.clip.id !== it.clip) await loadClip(it.clip); } else {
      vw.classList.toggle('png-only', !it.audio); png.hidden = !!it.audio || !it.png; if (!it.audio) { S.aud.pause(); S.buf = null; S.box = null; S.clip = { id: it.id, title: it.id, truth: [] }; if (it.png) png.src = it.png; $('rev').hidden = false; renderList(); return; }
      await loadClip(it.id, it); $('rev').hidden = false;
    }
    S.sel = new Set([it.model.beam - 1]); refreshBeams(); analyse(); S.box = { t0: it.t0, t1: it.t1, f0: it.f0 * 0.9, f1: it.f1 * 1.1 }; S.loopOn = true; focusTime(it.t0, it.t1, 0.6); applyAudition(); renderList(); S.dirty = true;
  }

  // ------------------------------------------------------------------ a buffer that is not a stored clip (the shell's Freeze: audio the user chose to listen to)
  function openBuffer(buf, id, title) {
    S.aud.stop(); S.tab = 'clips'; S.avail = null; S.clip = { id, title, truth: [] }; $('truth-row').hidden = true; S.buf = buf; S.beams = []; for (let c = 0; c < buf.numberOfChannels; c++) S.beams.push(buf.getChannelData(c));
    S.dur = buf.duration; S.aud.load(buf); S.view = { t0: 0, t1: 1 }; S.box = null; S.cursor = null; S.marks = []; syncTab(); analyse(); applyAudition();
  }

  // ------------------------------------------------------------------ tabs, lists, keys
  let queue = [];                                                                     // the review items last read from the source (pending() counts them)
  async function readQueue() { queue = await src.reviewItems(S.clips); S.depth = src.depth; return queue; }
  function renderList() {
    const ul = $('list'); ul.replaceChildren();
    const row = (title, sub, on, click) => { const li = document.createElement('li'), b = document.createElement('button'), t = document.createElement('b'), m = document.createElement('small'); t.textContent = title; m.textContent = sub; b.append(t, m); b.setAttribute('aria-current', on ? 'true' : 'false'); b.onclick = click; li.appendChild(b); ul.appendChild(li); };
    if (S.tab === 'clips') for (const c of S.clips) row(c.id, c.title, S.clip && S.clip.id === c.id, () => loadClip(c.id).then(renderList));
    else S.review.forEach((it, i) => row(it.id + (it.done ? '  [' + it.done + ']' : ''), it.clip + ' | ' + it.t0.toFixed(2) + '-' + it.t1.toFixed(2) + ' s | ' + (it.done || !$('blind').checked ? it.model.species : 'model claim hidden'), i === S.rvi, () => selectReview(i)));
  }
  function syncTab() {
    for (const b of root.querySelectorAll('#tabs button')) b.setAttribute('aria-pressed', b.dataset.tab === S.tab ? 'true' : 'false');
    $('rev').hidden = S.tab !== 'review'; fillSelects(); renderList();
  }
  async function setTab(t) {
    if (t === S.tab && S.clip) return; S.aud.pause(); $('play').textContent = 'Play'; S.tab = t; S.box = null; S.marks = []; syncTab();
    if (t === 'review') { S.review = (await readQueue()).map((it) => Object.assign(it, { done: (S.verdicts || {})[it.id] })); renderList(); if (S.review.length) await selectReview(Math.max(0, S.rvi)); else showEmpty(); }
    else if (S.clip) await loadClip(S.clip.id); else if (S.clips.length) await loadClip(S.clips[0].id);
    renderList();
  }
  /* touch behaviour follows the mode: Select needs both axes, so it owns the gesture; Pan leaves vertical drags to the page, so a finger can still scroll past the picture */
  function applyMode() { $('mode').textContent = S.mode === 'select' ? 'Select' : 'Pan'; $('mode').setAttribute('aria-pressed', S.mode === 'select' ? 'true' : 'false'); $('stage').style.touchAction = S.mode === 'pan' ? 'pan-y' : 'none'; }
  function readTokens() {
    const cs = getComputedStyle(root), g = (n) => cs.getPropertyValue(n).trim(); S.font = cs.fontFamily || 'system-ui';
    S.tok = { ink: g('--ov-ink'), grid: g('--ov-grid'), truth: g('--ov-truth'), claim: g('--ov-claim'), mark: g('--ov-mark'), sel: g('--ov-sel'), cursor: g('--ov-cursor'), play: g('--ov-play'), muted: g('--muted'), line: g('--accent') };
    S.axKey = null; S.dirty = true;
  }
  function onKey(e) {
    if (/INPUT|SELECT|TEXTAREA/.test(e.target.tagName) && e.key !== 'Escape') return; const k = e.key; let used = true;
    if (k === ' ') togglePlay(); else if (k === 'l' || k === 'L') { S.loopOn = !S.loopOn; applyAudition(); } else if (k >= '1' && k <= '8') pickBeam(+k - 1, e.shiftKey); else if (k === 'm' || k === 'M') { S.sel = new Set(allBeams()); beamsChanged(); }
    else if (k === 'Tab') abToggle(); else if (k === 'h' || k === 'H') { $('amode').value = $('amode').value === 'hetero' ? 'normal' : 'hetero'; applyAudition(); } else if (k === 'd' || k === 'D') { $('amode').value = $('amode').value === 'divide' ? 'normal' : 'divide'; applyAudition(); }
    else if (k === '[' || k === ']') { const o = $('rate'), i = o.selectedIndex + (k === '[' ? 1 : -1); if (i >= 0 && i < o.options.length) { o.selectedIndex = i; applyAudition(); } }
    else if (k === 't' || k === 'T') { const o = $('tf'); o.selectedIndex = (o.selectedIndex + (e.shiftKey ? -1 : 1) + o.options.length) % o.options.length; o.onchange(); } else if (k === 'c' || k === 'C') { const o = $('cmap'); o.selectedIndex = (o.selectedIndex + 1) % o.options.length; o.onchange(); }
    else if (k === '+' || k === '=') { $('hi').value = +$('hi').value - 5; $('hi').onchange(); } else if (k === '-') { $('hi').value = +$('hi').value + 5; $('hi').onchange(); }
    else if (k === 'z' || k === 'Z') zoom(0.6); else if (k === 'x' || k === 'X') zoom(1 / 0.6); else if (k === 'ArrowLeft' || k === 'ArrowRight') { const s = S.view.t1 - S.view.t0; setView(S.view.t0 + (k === 'ArrowLeft' ? -1 : 1) * s * 0.2, S.view.t1 + (k === 'ArrowLeft' ? -1 : 1) * s * 0.2); }
    else if (k === 'v' || k === 'V') $('mode').onclick(); else if (k === 'Enter') markBox(); else if (k === 'Escape') { S.box = null; applyAudition(); S.dirty = true; }
    else if (S.tab === 'review' && (k === 'y' || k === 'Y')) verdict('correct'); else if (S.tab === 'review' && (k === 'n' || k === 'N')) verdict('wrong'); else if (S.tab === 'review' && (k === 'u' || k === 'U')) verdict('unsure'); else if (S.tab === 'review' && (k === 'a' || k === 'A')) verdict('artifact');
    else if (S.tab === 'review' && (k === 's' || k === 'S')) skip();
    else if (S.tab === 'review' && (k === 'j' || k === 'J')) selectReview(S.rvi + 1); else if (S.tab === 'review' && (k === 'k' || k === 'K')) selectReview(S.rvi - 1); else used = false;
    if (used) e.preventDefault();
  }

  // ------------------------------------------------------------------ frame loop and stats for the measurements
  function frame(ts) {
    S.stats.rafs++; if (S.stats.lastRafT) { S.stats.rafDt.push(ts - S.stats.lastRafT); if (S.stats.rafDt.length > 600) S.stats.rafDt.shift(); } S.stats.lastRafT = ts;
    const tR = performance.now(); if (S.wf && S.wf.render()) { S.dirty = true; if (S.firstDraw) { S.firstDraw = false; S.stats.firstDrawMs = performance.now() - tR; S.stats.maxFrameMs = 0; } } if (S.dirty || S.aud.playing) { drawOverlay(); S.dirty = false; } if (root.hidden) looping = false; else requestAnimationFrame(frame);   // idle while another view is shown
  }
  let looping = false;
  function kick() { if (!looping) { looping = true; requestAnimationFrame(frame); } }
  window.__lab = { S, stats() { const d = S.stats.rafDt.slice().sort((a, b) => a - b), sec = (performance.now() - S.stats.t0) / 1000; return { backend: S.wf && S.wf.kind, fps: S.stats.rafs / sec, rafP50: d[d.length >> 1] || 0, rafP95: d[Math.floor(d.length * 0.95)] || 0, analyseMs: S.stats.analyseMs, errors: S.errors.slice(), nodes: root.getElementsByTagName('*').length, tab: S.tab, tf: S.tf, glInitMs: S.stats.glInitMs, glWarmMs: S.stats.glWarmMs, firstDrawMs: S.stats.firstDrawMs }; }, resetStats() { S.stats.rafs = 0; S.stats.t0 = performance.now(); S.stats.rafDt = []; } };

  let started = false;
  async function start() {
    if (started) return; started = true;
    S.mode = matchMedia('(pointer: coarse)').matches ? 'pan' : 'select'; applyMode();
    $('adv').open = innerWidth > 820;                          // settings open on a desktop, folded away on a phone
    readTokens(); fillSelects(); buildBeams(); wire(); syncTab(); kick();
    if (q.get('tf') && TF[q.get('tf')]) { $('tf').value = q.get('tf'); S.tf = q.get('tf'); } for (const [k, id] of [['n', 'nfft'], ['hop', 'hop'], ['pad', 'pad'], ['scale', 'scale'], ['cmap', 'cmap'], ['win', 'win']]) if (q.get(k)) $(id).value = q.get(k);
    if (q.get('beam')) S.sel = new Set(q.get('beam').split(',').map((v) => +v - 1)); refreshBeams();
    if (src.caps.clipList) await loadClip(q.get('clip') || S.clips[0].id); else { root.querySelector('#tabs button[data-tab=clips]').hidden = true; await setTab('review'); }
    renderList();
    if (q.get('box')) { const [a, b, c, d] = q.get('box').split(',').map(Number); S.box = { t0: a, t1: b, f0: c, f1: d }; applyAudition(); S.dirty = true; status(); }
    if (q.get('zoom')) { const [a, b] = q.get('zoom').split(',').map(Number); focusTime(a, b, 0); }
    if (q.get('tab') === 'review') await setTab('review');
  }
  /* the shell calls resume() whenever the Review view is shown: the first call boots the viewer, later calls redraw it (the canvas was hidden, so its size was 0) */
  async function resume() { if (src.caps.clipList && !S.clips.length) return; if (!started) await start().catch((e) => { S.errors.push('boot ' + e); }); else { kick(); readTokens(); if (S.fr && S.beams) analyse(); } }
  document.addEventListener('themechange', readTokens);
  const pending = () => (src.caps.clipList ? queue.filter((i) => !(i.id in (S.verdicts || {}))).length : S.depth);
  return { S, pending, async setClips(list) { S.clips = list; S.verdicts = S.verdicts || {}; await readQueue().catch((e) => { S.errors.push('queue ' + e); }); if (started) renderList(); }, openBuffer, setTab, resume, loadClip };
}
