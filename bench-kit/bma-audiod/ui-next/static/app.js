// Next dashboard prototype: four views (Live, Review, Detections, System) over one WebSocket of binary spectrum frames.
// Zero build step, no framework; text is set with textContent only. Engine (DSP, renderer, audition) is loaded by index.html as classic scripts.
// Data: every read and write goes through adapter.js (the mock server's v2 surface, or the existing bma_ui endpoints under /next/).
import { mountViewer } from "./review.js";

const LR = self.LabRender, LA = self.LabAudition, AD = self.LabAdapter;
const BINS = 46, BEAMS = 9, COLS = 600, BIG_H = 300, STRIP_H = 96, FMIN = 50, FMAX = 16000, PCM_RATE = 48000, FREEZE_S = 6;
// band edges (Hz) of the 46 standard-tier bands (spectrum-packet 1.0.0); band i spans EDGES_HZ[i]..EDGES_HZ[i + 1]
const EDGES_HZ = [50.0, 250.0, 315.0, 396.9, 500.0, 630.0, 793.7, 1000.0, 1259.9, 1587.4, 2000.0, 2118.9, 2244.9, 2378.4, 2519.8, 2669.7, 2828.4, 2996.6, 3174.8, 3363.6, 3563.6,
  3775.5, 4000.0, 4237.9, 4489.8, 4756.8, 5039.7, 5339.4, 5656.9, 5993.2, 6349.6, 6727.2, 7127.2, 7551.0, 8000.0, 8475.7, 8979.7, 9513.7, 10079.4, 10678.7, 11313.7, 11986.5,
  12699.2, 13454.3, 14254.4, 15102.0, 16000.0];
const CENTRES = Array.from({ length: BINS }, (_, i) => Math.sqrt(EDGES_HZ[i] * EDGES_HZ[i + 1]));

const params = new URLSearchParams(location.search);
const $ = (s, r = document) => r.querySelector(s);
const el = (tag, props = {}, ...kids) => { const e = Object.assign(document.createElement(tag), props); for (const k of kids) e.append(k); return e; };
// api() and post() serve live listening only (v2 surface); the adapter owns every other request
const api = async (path, opt) => { const r = await fetch(path, { cache: "no-store", ...opt }); if (!r.ok) throw new Error(path + " " + r.status); return r.json(); };
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const db = (v) => -100 + 0.5 * v;
const fmtHz = (f) => (f >= 1000 ? (f / 1000).toFixed(f >= 10000 ? 0 : 1) + " k" : Math.round(f) + " ");
const store = { get(k, d) { try { return localStorage.getItem(k) ?? d; } catch (e) { return d; } }, set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } } };
const wsUrl = (path) => (location.protocol === "https:" ? "wss://" : "ws://") + location.host + path;
const src = AD.create({ fetch: (...a) => fetch(...a), base: "", WebSocket, wsUrl, pathname: location.pathname, search: location.search, noSocket: params.get("ws") === "0" });
const nz = (v, f) => (typeof v === "number" ? f(v) : "n/a");                           // a figure the source does not have is shown as n/a, never as 0

const S = { view: "live", liveState: "", liveMode: "", gated: false, beam: 0, cmap: store.get("cmap", "viridis"), range: store.get("range", "-90,-30"), lastFrame: 0, clips: [], listening: null, mobile: matchMedia("(max-width: 719px)").matches };

/* ------------------------------------------------------------ live waterfall: 9 rings (beam 0 = mean) on one canvas, a big layer for the chosen beam */
let cv = $("#wf"), rend = null;
const times = new Float64Array(COLS), tmp = new Float32Array(BINS), peak = new Float32Array(BEAMS), lvl = new Float32Array(BEAMS);
let head = -1;
const wfw = () => (S.mobile ? 600 : 1200);                                        // canvas pixels across: a phone gets a 2:1 waterfall, not a 4:1 sliver
const wfh = () => (S.mobile || !src.caps.tiles ? BIG_H : BIG_H + 8 + STRIP_H);

function layers() {
  const out = [], W = wfw(), w = Math.floor(W / BEAMS) - 4;
  if (!S.mobile && src.caps.tiles) for (let b = 0; b < BEAMS; b++) out.push({ ring: b, x: b * Math.floor(W / BEAMS), y: BIG_H + 8, w, h: STRIP_H });
  out.push({ ring: S.beam, x: 0, y: 0, w: W, h: BIG_H });
  return out;
}
const bigLayer = () => rend.layers.length - 1;
function makeRenderer(mode) {
  const n = cv.cloneNode(false); cv.replaceWith(n); cv = n;                        // a canvas keeps its first context type: every attempt gets a fresh one
  cv.width = wfw(); cv.height = wfh();
  const [lo, hi] = S.range.split(",").map(Number);
  const r = new LR.Waterfall(cv, { mode, nBins: BINS, nCols: COLS, fs: 32000, binFreqs: CENTRES, fmin: FMIN, fmax: FMAX, yScale: "log", cmap: S.cmap, lo, hi, rings: BEAMS, layers: layers() });
  r.onlost = () => { const next = makeRenderer("canvas"); r.replayInto(next); rend = next; document.documentElement.dataset.renderer = next.kind; wireWaterfall(); drawAxes(); };   // WebKit may recycle contexts
  return r;
}
function startRenderer() {
  const want = params.get("r") || "webgl";
  rend = makeRenderer(want === "canvas2d" ? "canvas" : "webgl");
  document.documentElement.dataset.renderer = rend.kind;
  wireWaterfall(); drawAxes(); rend.warm();
}
function relayout() {                                                              // breakpoint change: same renderer, new tiles
  rend.resize(wfw(), wfh()); rend.setLayers(layers()); drawAxes(); rend.dirty = true;
}
function drawAxes() {
  const ay = $("#wf-axis-y"); ay.replaceChildren();
  for (const f of [100, 500, 1000, 2000, 4000, 8000, 16000]) {
    const s = el("span", { textContent: fmtHz(f) }); s.style.top = (100 * LR.freqToY("log", f, FMIN, FMAX, BIG_H) / cv.height) + "%"; ay.append(s);
  }
  const ax = $("#wf-axis-x"); ax.style.top = (BIG_H / cv.height * 100) + "%"; ax.style.transform = "translateY(-100%)";
}
const setLvl = (b, v) => { lvl[b] = v; peak[b] = Math.max(v, peak[b] - 0.25); };
/* one spectrum row: cols[b] = the 46 levels of beam b (beam 0 = mean) or null when the source did not send that beam; levels = the 8 beam levels in dB, or null */
function onRow(t, cols, levels) {
  head = (head + 1) % COLS; times[head] = t;
  for (let b = 0; b < BEAMS; b++) {
    if (!cols[b]) continue;
    LR.u8ToDb(cols[b], tmp); if (rend) rend.pushColumn(b, tmp);
    let p = 0; for (let i = 0; i < BINS; i++) p += Math.pow(10, tmp[i] / 10);
    setLvl(b, 10 * Math.log10(p / BINS));
  }
  if (levels) {
    for (let b = 1; b <= 8; b++) setLvl(b, levels[b - 1]);
    if (!cols[0]) setLvl(0, 10 * Math.log10(levels.reduce((a, v) => a + Math.pow(10, v / 10), 0) / 8));
  }
  S.lastFrame = performance.now();
  if (!frameQueued) { frameQueued = true; requestAnimationFrame(paint); }
}
let frameQueued = false;
function paint() {
  frameQueued = false;
  if (rend && S.view === "live") rend.render();
  if (S.view === "live") for (let b = 0; b < BEAMS; b++) {
    const c = chips[b];
    c.bar.style.transform = "scaleX(" + Math.max(0, Math.min(1, (lvl[b] + 90) / 60)).toFixed(3) + ")";
    c.peak.style.left = Math.max(0, Math.min(98, (peak[b] + 90) / 60 * 100)) + "%";
  }
}

/* ------------------------------------------------------------ live source (adapter: a WebSocket of frames, or /api/spectrum polled) */
let live = null;
function resetLive() { head = -1; times.fill(0); lvl.fill(-120); peak.fill(-120); S.lastFrame = 0; if (rend) rend.clear(); }
function connect() {
  live = src.live({ row: onRow, event: onEvent, state: (s) => { S.liveState = s; }, mode: (m) => { S.liveMode = m; if (rend) relayout(); }, gate: (g) => { S.gated = g; $("#gate").hidden = !g; }, reset: resetLive });
}
const ticks = [];
function onEvent(ev) {
  ticks.unshift(ev); ticks.length = Math.min(ticks.length, 3);
  $("#ticker").replaceChildren(...ticks.map((x) => el("div", {}, el("span", { className: "mono muted", textContent: new Date(x.t * 1000).toLocaleTimeString() }), el("span", { textContent: x.common }),
    el("span", { className: "muted", textContent: "beam " + x.beam + " · " + Math.round(x.confidence * 100) + "%" }))));
}
setInterval(() => {
  const age = (performance.now() - S.lastFrame) / 1000, p = $("#state");
  const k = !S.lastFrame ? "nodata" : age < 2 ? "live" : "stale";
  const note = { disabled: "not enabled", auth_required: "sign in required", unavailable: "unavailable" }[S.liveState];
  const txt = k === "live" ? "live" : k === "stale" ? "stale" : note || "no data";
  if (p.dataset.k !== k || p.textContent !== txt) { p.dataset.k = k; p.textContent = txt; }
}, 500);

/* ------------------------------------------------------------ beam chips, cursor, time selection */
const chips = [];
function buildChips() {
  const host = $("#beams");
  for (let b = 0; b < BEAMS; b++) {
    const bar = el("i", { className: "bar" }), pk = el("i", { className: "peak" });
    const c = el("button", { type: "button", role: "radio" }, el("span", { textContent: b === 0 ? "Mean" : "B" + b }), bar, pk);
    c.setAttribute("aria-checked", String(b === S.beam)); c.setAttribute("aria-label", b === 0 ? "Mean of 8 beams" : "Beam " + b);
    c.onclick = () => selectBeam(b);
    host.append(c); chips.push({ c, bar, peak: pk });
  }
}
function selectBeam(b) {
  S.beam = b; chips.forEach((x, i) => x.c.setAttribute("aria-checked", String(i === b)));
  if (live) live.setBeam(b);                                                         // a polled source changes what it asks for; a socket ignores it
  if (rend) { rend.setLayerRing(bigLayer(), b); rend.render(); }
  if (listen.aud) listen.aud.mixBeams(b === 0 ? [0, 1, 2, 3, 4, 5, 6, 7] : [b - 1]);
}
const cell = (e) => { const r = cv.getBoundingClientRect(), x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height * cv.height; return { x, y, col: Math.min(COLS - 1, Math.max(0, Math.floor(x * COLS))) }; };
const ring = (col) => (head + 1 + col) % COLS;                                      // display column -> ring slot (the right edge is the newest)
let sel = null;
function onMove(e) {
  const c = cell(e); if (head < 0) return;
  const cur = $("#wf-cursor"), wrap = cv.parentElement.getBoundingClientRect(), cr = cv.getBoundingClientRect();
  cur.hidden = false; cur.style.left = (cr.left - wrap.left + c.x * cr.width) + "px";
  const slot = ring(c.col), age = times[head] - times[slot];
  let parts = null;
  if (c.y < BIG_H && times[slot] > 0) {
    const f = LR.yToFreq("log", c.y, FMIN, FMAX, BIG_H);
    const bin = Math.max(0, EDGES_HZ.findIndex((x) => x > f) - 1), v = rend.rings[S.beam].raw[slot * BINS + Math.min(BINS - 1, bin)];
    parts = [["-" + age.toFixed(1) + " s", 1], [" · ", 0], [fmtHz(EDGES_HZ[bin]) + "Hz", 1], ["–" + fmtHz(EDGES_HZ[bin + 1]) + "Hz · ", 0], [v.toFixed(1) + " dB", 1], [" · " + (S.beam ? "beam " + S.beam : "mean of 8"), 0]];
  }
  if (sel) { sel.b = c.col; showSel(); parts = null; }
  if (parts) $("#inspect").replaceChildren(...parts.map(([t, bold]) => (bold ? el("b", { textContent: t }) : document.createTextNode(t))));
}
function showSel() {
  const a = Math.min(sel.a, sel.b), b = Math.max(sel.a, sel.b), box = $("#wf-sel"), cr = cv.getBoundingClientRect(), wrap = cv.parentElement.getBoundingClientRect();
  box.hidden = false; box.style.left = (cr.left - wrap.left + a / COLS * cr.width) + "px"; box.style.width = ((b - a + 1) / COLS * cr.width) + "px"; box.style.height = (BIG_H / cv.height * cr.height) + "px";
}
function clearSel() { sel = null; $("#wf-sel").hidden = true; }
function endSel() {
  if (!sel) return;
  const a = Math.min(sel.a, sel.b), b = Math.max(sel.a, sel.b), t0 = times[ring(a)], t1 = times[ring(b)], box = $("#inspect");
  let pk = -200; const raw = rend.rings[S.beam].raw; for (let c = a; c <= b; c++) for (let i = 0; i < BINS; i++) pk = Math.max(pk, raw[ring(c) * BINS + i]);
  const found = S.clips.filter((c) => !c.test && c.audio !== false && c.t >= t0 - 1 && c.t <= t1 + 1);
  box.replaceChildren(el("span", { textContent: (t1 - t0).toFixed(1) + " s selected · peak " + pk.toFixed(1) + " dB · " }));
  if (found.length) for (const c of found) box.append(el("button", { type: "button", textContent: "Open clip " + c.common, onclick: () => openClip(c.id) }));
  else box.append(el("span", { className: "muted", textContent: src.caps.listen ? "no audio is kept for this time; listen live to hear it now" : "no audio is kept for this time" }));
  sel = null;
}
function wireWaterfall() {
  cv.addEventListener("pointermove", onMove);
  cv.addEventListener("pointerleave", () => { $("#wf-cursor").hidden = true; });
  cv.addEventListener("pointerdown", (e) => { if (e.pointerType === "mouse" && e.button !== 0) return; cv.setPointerCapture(e.pointerId); sel = { a: cell(e).col, b: cell(e).col }; showSel(); });
  cv.addEventListener("pointerup", endSel);
  for (const ev of ["pointercancel", "lostpointercapture"]) cv.addEventListener(ev, clearSel);   // an interrupted gesture never leaves a stuck selection
}
addEventListener("blur", clearSel);
function markClips() {
  const m = $("#wf-marks"); m.replaceChildren(); if (head < 0) return;
  const now = times[head];
  for (const c of S.clips) {
    if (c.test || c.audio === false) continue;
    const a = now - c.t; if (a < 0 || a > 60) continue;
    const i = el("i", { title: c.common }); i.style.left = (100 - a / 60 * 100) + "%"; i.onclick = () => openClip(c.id); m.append(i);
  }
}
setInterval(markClips, 2000);

/* ------------------------------------------------------------ live listening: explicit per-session consent, indicator, no server copy, no stored audio */
const listen = { aud: null, ws: null, tok: "", end: 0, timer: 0, hidden: 0, ring: null, pos: 0 };
const note = (t) => { $("#listen-note").textContent = t; };
function startListenFlow() { $("#consent-ok").checked = false; $("#consent-go").disabled = true; $("#consent").showModal(); }
$("#consent-ok").onchange = (e) => { $("#consent-go").disabled = !e.target.checked; };
$("#consent").addEventListener("close", async () => {
  if ($("#consent").returnValue !== "go") return;
  try { const r = await post("/api/v2/listen/consent", {}); listen.tok = r.token; listen.end = Date.now() + r.expires_s * 1000; await startListen(); } catch (e) { note("could not start: " + e.message); }
});
async function startListen() {
  stopListen(true);
  const aud = listen.aud = new LA.Audition();
  await aud.startLive();                                                            // called from the click that closed the dialog: the autoplay rule is met
  aud.mixBeams(S.beam === 0 ? [0, 1, 2, 3, 4, 5, 6, 7] : [S.beam - 1]);
  listen.ring = Array.from({ length: 8 }, () => new Float32Array(PCM_RATE * FREEZE_S)); listen.pos = 0;   // the last seconds, in this tab's memory only, for Freeze
  const sock = listen.ws = new WebSocket(wsUrl("/api/v2/listen?token=" + encodeURIComponent(listen.tok) + "&beams=0,1,2,3,4,5,6,7")); sock.binaryType = "arraybuffer";
  let header = false;
  sock.onmessage = (e) => {
    if (!header) { header = true; return; }                                         // 'UILAB1' header frame: rate, channels, bits
    const n = Math.floor(e.data.byteLength / 16), dv = new DataView(e.data), planar = new Float32Array(8 * n);
    for (let i = 0; i < n; i++) for (let b = 0; b < 8; b++) planar[b * n + i] = dv.getInt16((i * 8 + b) * 2, true) / 32768;
    for (let b = 0; b < 8; b++) for (let i = 0; i < n; i++) listen.ring[b][(listen.pos + i) % listen.ring[b].length] = planar[b * n + i];
    listen.pos = (listen.pos + n) % listen.ring[0].length;
    aud.feedLive(planar, n);
  };
  sock.onclose = () => { if (S.listening) stopListen(); };
  S.listening = { start: Date.now() };
  $("#listen").textContent = "Stop listening"; $("#listen").classList.remove("primary"); $("#freeze").hidden = false;
  clearInterval(listen.timer); listen.timer = setInterval(tickListen, 1000); tickListen();
}
function tickListen() {
  const left = Math.max(0, Math.round((listen.end - Date.now()) / 1000)), ind = $("#listen-ind");
  if (left <= 0) return stopListen();
  ind.hidden = false; ind.onclick = () => stopListen();
  ind.textContent = "● LISTENING " + (S.beam ? "beam " + S.beam : "mean") + " · " + Math.floor(left / 60) + ":" + String(left % 60).padStart(2, "0");
  document.title = "● LISTENING · BMA array"; note("Audio is not recorded by the server.");
}
function stopListen(keepFlow) {
  clearInterval(listen.timer);
  if (listen.ws) { listen.ws.onclose = null; listen.ws.close(); listen.ws = null; }
  if (listen.aud) { listen.aud.stopLive(); if (listen.aud.ctx) listen.aud.ctx.close(); listen.aud = null; }
  if (keepFlow) return;
  S.listening = null; listen.tok = ""; listen.ring = null;
  $("#listen-ind").hidden = true; document.title = "BMA array"; $("#listen").textContent = "Listen"; $("#listen").classList.add("primary"); $("#freeze").hidden = true; note("");
}
$("#listen").onclick = () => (S.listening ? stopListen() : startListenFlow());
if (!src.caps.listen) { $("#listen").hidden = true; note("Live listening is not available on this server."); }
document.addEventListener("visibilitychange", () => {
  clearTimeout(listen.hidden);
  if (document.hidden && S.listening) listen.hidden = setTimeout(() => { stopListen(); note("stopped: the tab was hidden"); }, 30000);
});
/* Freeze: the last seconds of what the user listened to become a clip in the viewer; nothing leaves the tab */
$("#freeze").onclick = () => {
  if (!listen.ring) return;
  const n = listen.ring[0].length, buf = new AudioBuffer({ length: n, sampleRate: PCM_RATE, numberOfChannels: 8 });
  for (let b = 0; b < 8; b++) { const a = new Float32Array(n); for (let i = 0; i < n; i++) a[i] = listen.ring[b][(listen.pos + i) % n]; buf.copyToChannel(a, b); }
  stopListen(); go("review");
  viewer.resume().then(() => viewer.openBuffer(buf, "live-freeze", "Frozen live window (" + FREEZE_S + " s, this tab only)"));
};

/* ------------------------------------------------------------ status strip + system view */
let status = null; const lossHist = [];
async function pollStatus() {
  try {
    status = await src.status(); if (typeof status.rx_loss_pct === "number") { lossHist.push(status.rx_loss_pct); if (lossHist.length > 40) lossHist.shift(); }
    $("#strip").replaceChildren(
      el("span", {}, "loss ", el("b", { textContent: nz(status.rx_loss_pct, (v) => v.toFixed(2) + "%") })),
      el("span", {}, "board ", el("b", { textContent: nz(status.board && status.board.temp_c, (v) => v.toFixed(0) + " °C") })));
    if (S.view === "sys") renderSys();
  } catch (e) { /* the state pill already shows stale */ }
}
function renderSys() {
  const v = $("#v-sys"); if (!status) return;
  const kv = (k, val) => el("div", {}, el("span", { className: "muted", textContent: k }), el("b", { textContent: String(val) }));
  v.replaceChildren(
    el("div", { className: "panel" }, el("h2", { textContent: "Health" }), el("div", { className: "kv" }, kv("uptime", nz(status.uptime_s, (v) => Math.floor(v / 60) + " min")), kv("receive loss", nz(status.rx_loss_pct, (v) => v.toFixed(2) + " %")),
      kv("board", status.board.state + " · " + nz(status.board.temp_c, (v) => v.toFixed(0) + " °C")), kv("clips kept, 24 h", nz(status.clips.stored_24h, String)), kv("detections, 1 h", nz(status.detections_1h, String)))),
    el("div", { className: "panel" }, el("h2", { textContent: "Plugins" }), el("div", { className: "chips" }, ...Object.entries(status.plugins).map(([k, val]) => el("span", { className: "muted", textContent: k + " " + val })))),
    el("div", { className: "panel" }, el("h2", { textContent: "Receive loss, last " + lossHist.length * 5 + " s" }), sparkline(lossHist)));
}
function sparkline(vals) {
  const c = el("canvas", { width: 600, height: 64, className: "spark" }), x = c.getContext("2d"), mx = Math.max(1, ...vals);
  x.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim(); x.lineWidth = 2; x.beginPath();
  vals.forEach((v, i) => { const px = i / Math.max(1, vals.length - 1) * 598 + 1, py = 62 - v / mx * 58; i ? x.lineTo(px, py) : x.moveTo(px, py); }); x.stroke();
  return c;
}

/* ------------------------------------------------------------ clips: detections list, review viewer */
const viewer = mountViewer($("#v-review"), { src, changed: () => updateCount() });
async function loadClips() { S.clips = await src.clips(); await viewer.setClips(S.clips); updateCount(); }
function updateCount() { const n = viewer.pending(); $("#rv-count").textContent = n ? String(n) : ""; }
async function openClip(id) { go("review"); await viewer.resume(); await viewer.setTab("clips"); await viewer.loadClip(id); }
function renderDet() {
  const v = $("#v-det"), real = S.clips.filter((c) => !c.test), species = [...new Set(real.map((c) => c.common))], f = S.detFilter || (S.detFilter = { sp: null, min: 0.3 });
  const rows = real.filter((c) => (!f.sp || c.common === f.sp) && c.confidence >= f.min);
  const chipRow = el("div", { className: "chips" }, ...species.map((s) => { const b = el("button", { type: "button", className: "chip", textContent: s, onclick: () => { f.sp = f.sp === s ? null : s; renderDet(); } }); b.setAttribute("aria-pressed", String(f.sp === s)); return b; }));
  const rng = Object.assign(el("input", { type: "range", min: 0, max: 1, step: 0.05, value: f.min }), { oninput: (e) => { f.min = +e.target.value; renderDet(); } }); rng.setAttribute("aria-label", "Minimum confidence");
  const head = el("tr", {}, ...["Time", "Species", "Beam", "Conf.", ""].map((h) => el("th", { textContent: h })));
  const body = rows.length ? rows.map((c) => el("tr", {}, el("td", { className: "mono", textContent: new Date(c.t * 1000).toLocaleTimeString() }), el("td", { textContent: c.common + (c.in_region ? "" : " (out of region)") }),
    el("td", { className: "num", textContent: String(c.beam) }), el("td", { className: "num", textContent: Math.round(c.confidence * 100) + "%" }), el("td", {}, c.audio === false ? "" : el("button", { type: "button", textContent: "Open", onclick: () => openClip(c.id) }))))
    : [el("tr", {}, el("td", { colSpan: 5, className: "muted", textContent: "No detection at this confidence. Lower the minimum or clear the species filter." }))];
  v.replaceChildren(el("div", { className: "panel" }, el("h2", { textContent: "Detections" }), chipRow, el("label", { className: "row" }, "Minimum confidence ", rng, el("span", { className: "mono", textContent: Math.round(f.min * 100) + "%" }))),
    el("div", { className: "panel" }, el("table", {}, el("thead", {}, head), el("tbody", {}, ...body))));
}

/* ------------------------------------------------------------ navigation, keys, theme */
const VIEWS = ["live", "review", "det", "sys"];
function go(view, push = true) {
  S.view = view; for (const n of VIEWS) $("#v-" + n).hidden = n !== view;
  document.querySelectorAll("#nav button").forEach((b) => { if (b.dataset.view === view) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current"); });
  if (view === "review") viewer.resume(); else viewer.S.aud.pause();
  if (view === "det") renderDet();
  if (view === "sys") renderSys();
  if (view === "live" && rend) { rend.dirty = true; rend.render(); }
  if (push && location.hash !== "#" + view) history.replaceState(null, "", "#" + view);
}
document.querySelectorAll("#nav button").forEach((b) => { b.onclick = () => go(b.dataset.view); });
addEventListener("hashchange", () => { const h = location.hash.slice(1); if (VIEWS.includes(h)) go(h, false); });
addEventListener("keydown", (e) => {
  if (e.target.matches("input, select, textarea") || e.ctrlKey || e.metaKey) return;
  if (e.altKey && /^[1-4]$/.test(e.key)) { go(VIEWS[+e.key - 1]); e.preventDefault(); return; }
  if (S.view !== "live") return;
  if (/^[0-8]$/.test(e.key)) selectBeam(+e.key);
  else if ((e.key === "l" || e.key === "L") && src.caps.listen) $("#listen").click();
  else if (e.key === "c") {
    S.cmap = S.cmap === "viridis" ? "magma" : S.cmap === "magma" ? "turbo" : "viridis"; store.set("cmap", S.cmap);
    if (rend) { rend.setView({ cmap: S.cmap }); rend.render(); }
  }
});
function setTheme(t) { document.documentElement.dataset.theme = t; store.set("theme", t); document.dispatchEvent(new Event("themechange")); }
$("#theme").onclick = () => setTheme(document.documentElement.dataset.theme === "dark" || (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches) ? "light" : "dark");
{ const t = store.get("theme", ""); if (t) document.documentElement.dataset.theme = t; }
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => document.dispatchEvent(new Event("themechange")));
matchMedia("(max-width: 719px)").addEventListener("change", (e) => { S.mobile = e.matches; if (rend) relayout(); $("#queue").open = !e.matches; });

buildChips();
$("#queue").open = !S.mobile;
if (params.has("dev")) import("./dev-reload.js");
{ const h = location.hash.slice(1); go(VIEWS.includes(h) ? h : "live", false); }       // show the shell at once; data and the renderer arrive behind it
startRenderer();
loadClips().catch(() => {}).then(() => { connect(); pollStatus(); setInterval(pollStatus, 5000); if (S.view === "review") viewer.resume(); else if (S.view === "det") renderDet(); });
window.__ui = { S, src, viewer, listen, get live() { return live; }, get renderer() { return rend && rend.kind; }, get rend() { return rend; } };
