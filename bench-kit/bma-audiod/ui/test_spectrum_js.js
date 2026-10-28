// Pure-core tests for static/spectrum.js (run: node test_spectrum_js.js): colour maps, the persistence histogram and its decay, the frequency axis
// mapping, loudest-beam pick, auto dB range, the row store and the formatting helpers. Exact values only; the real module, nothing stubbed.
// The fixture band edges are the contract's (contracts/fixtures/ui_spectrum.json), read from disk.
const fs = require("fs"), path = require("path");
const Sp = require("./static/spectrum.js"), C = Sp.core;
const EDGES = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "contracts", "fixtures", "ui_spectrum.json"), "utf8")).edges_hz;
let n = 0, fails = 0;
function eq(name, got, want) {
  n++;
  const ok = JSON.stringify(got) === JSON.stringify(want);
  console.log((ok ? "PASS " : "FAIL ") + name + (ok ? "" : "  [got " + JSON.stringify(got) + ", want " + JSON.stringify(want) + "]"));
  if (!ok) fails++;
}
const near = (a, b, e) => Math.abs(a - b) <= (e || 1e-6);

// ---- colour maps
eq("viridis endpoints are the matplotlib ones", [C.cmapRGB("viridis", 0), C.cmapRGB("viridis", 255)], [[68, 1, 84], [253, 231, 37]]);
eq("magma endpoints", [C.cmapRGB("magma", 0), C.cmapRGB("magma", 255)], [[0, 0, 4], [252, 253, 191]]);
eq("turbo endpoints", [C.cmapRGB("turbo", 0), C.cmapRGB("turbo", 255)], [[48, 18, 59], [122, 4, 3]]);
eq("grey is the identity ramp", [C.cmapRGB("grey", 0), C.cmapRGB("grey", 7), C.cmapRGB("grey", 255)], [[0, 0, 0], [7, 7, 7], [255, 255, 255]]);
eq("every map has 256 entries of 3 bytes", ["viridis", "magma", "turbo", "grey"].map(k => C.cmapBytes(k).length), [768, 768, 768, 768]);
eq("the packed lookup table round-trips the viridis endpoints (alpha 255)", [C.unpackRGB(C.cmapLut("viridis")[0]), C.unpackRGB(C.cmapLut("viridis")[255]), C.cmapLut("viridis")[0] >>> 24], [[68, 1, 84], [253, 231, 37], 255]);
eq("cmapIndex: the range ends and the middle (128 = round(127.5))", [C.cmapIndex(-100, -100, -20), C.cmapIndex(-20, -100, -20), C.cmapIndex(-60, -100, -20)], [0, 255, 128]);
eq("cmapIndex clamps below and above the range (positive twin: inside values are not clamped)", [C.cmapIndex(-130, -100, -20), C.cmapIndex(0, -100, -20), C.cmapIndex(-90, -100, -20)], [0, 255, 32]);
eq("levelDb: v * step + floor", [C.levelDb(0, -100, 0.5), C.levelDb(200, -100, 0.5), C.levelDb(255, -100, 0.5)], [-100, 0, 27.5]);

// ---- persistence histogram and decay
const P = new C.Persistence(2, 2, 10);
P.hit(1, 100);
eq("a hit adds 1 to its cell and 0.25 to each neighbour, only in its own band", [P.H[256 + 100], P.H[256 + 99], P.H[256 + 101], P.H[100], P.H[256 + 98]], [1, 0.25, 0.25, 0, 0]);
P.decay(2 * Math.LN2);
eq("decay by tau * ln 2 halves every cell (tau 2 s)", [P.H[256 + 100], P.H[256 + 99]], [0.5, 0.125]);
P.decay(0); P.decay(-1);
eq("a zero or negative time step changes nothing (positive twin of the halving above)", [P.H[256 + 100], P.H[256 + 99]], [0.5, 0.125]);
const Q = new C.Persistence(1, 4, 10); Q.hit(0, 50); const Q2 = new C.Persistence(1, 4, 10); Q2.hit(0, 50);
Q.decay(1.0); Q.decay(2.5); Q2.decay(3.5);
eq("decay composes: 1 s then 2.5 s equals 3.5 s (exp(-3.5/4) = " + Math.exp(-3.5 / 4).toFixed(6) + ")", [near(Q.H[50], Q2.H[50]), near(Q.H[50], Math.exp(-3.5 / 4))], [true, true]);
const W = new C.Persistence(1, 2, 10); W.hit(0, 10, 0.5);
eq("a weighted hit scales the cell and its smear", [W.H[10], W.H[9], W.H[11]], [0.5, 0.125, 0.125]);
const E0 = new C.Persistence(1, 2, 10); E0.hit(0, 0); E0.hit(0, 255);
eq("hits at the first and last cell do not write outside the band (no neighbour below 0 or above 255)", [E0.H[0], E0.H[1], E0.H[254], E0.H[255], E0.H.length], [1, 0.25, 0.25, 1, 256]);
const R = new C.Persistence(3, 2, 10); R.addRow(Uint8Array.from([10, 200, 30])); R.addRow(Uint8Array.from([20, 100, 30]));
eq("addRow hits every band and keeps the per-band peak (peak = max value per band)", [R.H[10], R.H[256 + 200], R.H[512 + 30], Array.from(R.P), R.hasPeak], [1, 1, 2, [20, 200, 30], true]);
R.resetPeak();
eq("resetPeak clears the peaks", [Array.from(R.P), R.hasPeak], [[0, 0, 0], false]);
eq("peak decay lowers a held peak by dB/s * dt / step (3 dB/s for 2 s at 0.5 dB = 12 cells)", (() => { const p = new C.Persistence(1, 2, 10); p.addRow(Uint8Array.from([100])); p.decayPeak(2, 3, 0.5); return p.P[0]; })(), 88);
eq("intensity: Hnorm (tau * rate = 20) is full intensity, 0 is none, more is capped at 1", [P.intensity(0), new C.Persistence(1, 2, 10).intensity(20), new C.Persistence(1, 2, 10).intensity(40)], [0, 1, 1]);
eq("intensity of one count at Hnorm 20 is log1p(8) / log1p(160) = 0.4324", Number(new C.Persistence(1, 2, 10).intensity(1).toFixed(4)), 0.4324);
const IT = C.intensityTable(20);
eq("intensity table: nothing at 0, colour index 255 at 1.3 Hnorm, monotone, never 0 above the first entry", [IT[0], IT[1023], IT.length, IT.every((v, i) => i === 0 || (v >= IT[i - 1] && v >= 1))], [0, 255, 1024, true]);

// ---- frequency axis
eq("the fixture has 46 bands and 47 edges starting at 50 Hz and ending at 16 kHz", [EDGES.length, EDGES[0], EDGES[46]], [47, 50, 16000]);
const cbLog = C.colBands(EDGES, 640, "log");
eq("log axis, 640 px: the column whose centre is 1001 Hz is in band 7 (1000-1260 Hz) and the column before it (997 Hz) in band 6", [cbLog[332], cbLog[331], EDGES[7], EDGES[6]], [7, 6, 1000, 793.7]);
const naive = (f) => { for (let i = 0; i < EDGES.length - 1; i++) if (f >= EDGES[i] && f < EDGES[i + 1]) return i; return -1; };
eq("log axis: every column maps to the band a naive scan finds for its centre frequency", Array.from(cbLog).every((b, x) => b === naive(50 * Math.pow(320, (x + 0.5) / 640))), true);
const cbLin = C.colBands(EDGES, 640, "lin");
eq("linear axis: every column maps to the band a naive scan finds for 50 + 15950 (x+0.5)/640", Array.from(cbLin).every((b, x) => b === naive(50 + 15950 * (x + 0.5) / 640)), true);
eq("the two axes differ: column 100 (123.7 Hz log, 2554 Hz linear) is band 0 on the log axis and band 14 (2520-2670 Hz) on the linear axis", [cbLog[100], cbLin[100]], [0, 14]);
eq("xOfFreq: 50 Hz is column 0, 16 kHz the full width, 1 kHz on a log axis is 640 ln 20 / ln 320 = 332.4", [C.xOfFreq(50, 640, "log", 50, 16000), C.xOfFreq(16000, 640, "log", 50, 16000), Number(C.xOfFreq(1000, 640, "log", 50, 16000).toFixed(1))], [0, 640, 332.4]);
eq("xOfFreq on the linear axis: 8 kHz is (8000 - 50) / 15950 of the width = 319", Number(C.xOfFreq(8000, 640, "lin", 50, 16000).toFixed(0)), 319);
eq("bandOfFreq: edges are inclusive below, exclusive above; outside the edges is -1", [C.bandOfFreq(EDGES, 49.9), C.bandOfFreq(EDGES, 50), C.bandOfFreq(EDGES, 249.99), C.bandOfFreq(EDGES, 250), C.bandOfFreq(EDGES, 15999), C.bandOfFreq(EDGES, 16000)], [-1, 0, 0, 1, 45, -1]);
eq("axisRange: a detail-tier edge list starting at 0 Hz gets a 100 Hz log minimum", [C.axisRange([0, 250, 500]), C.axisRange(EDGES)], [[100, 500], [50, 16000]]);

// ---- loudest beam, auto range
const mk = (t, levels, flags) => ({ t, levels: Uint8Array.from(levels), bins: Uint8Array.from([0]), flags: flags || 0 });
eq("pickLoudest: beam 8 has the highest levels", C.pickLoudest([mk(10, [1, 2, 3, 4, 5, 6, 7, 90]), mk(11, [1, 2, 3, 4, 5, 6, 7, 90])], 11, 5, -100, 0.5), 8);
eq("pickLoudest: a tie goes to the lowest beam", C.pickLoudest([mk(10, [50, 50, 50, 50, 50, 50, 50, 50])], 10, 5, -100, 0.5), 1);
eq("pickLoudest: rows older than the window are ignored (positive twin: the same row inside the window counts)", [C.pickLoudest([mk(1, [1, 1, 1, 1, 1, 1, 1, 200]), mk(10, [90, 1, 1, 1, 1, 1, 1, 1])], 10, 5, -100, 0.5), C.pickLoudest([mk(7, [1, 1, 1, 1, 1, 1, 1, 200]), mk(10, [90, 1, 1, 1, 1, 1, 1, 1])], 10, 5, -100, 0.5)], [1, 8]);
eq("pickLoudest averages POWER, not dB: beam 1 alternates -40/0 dB (mean power -3 dB), beam 2 is steady -5 dB, so beam 1 wins although its dB mean (-20) is lower",
  C.pickLoudest([mk(10, [120, 190, 0, 0, 0, 0, 0, 0]), mk(10.1, [200, 190, 0, 0, 0, 0, 0, 0])], 10.1, 5, -100, 0.5), 1);
eq("pickLoudest: no rows with levels gives null", C.pickLoudest([{ t: 10, levels: null }], 10, 5, -100, 0.5), null);
const ramp = Uint8Array.from(Array.from({ length: 100 }, (_, i) => 20 + i));
eq("autoRange: 5th percentile value 24 (-88 dB) rounds down to -90, 99th value 118 (-41 dB) rounds up to -40", C.autoRange([{ t: 10, bins: ramp, flags: 0 }], 10, 10, -100, 0.5), [-90, -40]);
eq("autoRange: a flat signal at -50 dB is widened to at least 20 dB around it: [-60, -40]", C.autoRange([{ t: 10, bins: Uint8Array.from([100, 100, 100]), flags: 0 }], 10, 10, -100, 0.5), [-60, -40]);
eq("autoRange: gap and zero rows (flags 2 and 4) and rows older than the span are ignored, so only the 100-valued row counts", C.autoRange([{ t: 10, bins: Uint8Array.from([100]), flags: 0 }, { t: 10, bins: Uint8Array.from([255]), flags: 2 }, { t: 10, bins: Uint8Array.from([0]), flags: 4 }, { t: 0, bins: Uint8Array.from([250]), flags: 0 }], 10, 5, -100, 0.5), [-60, -40]);
eq("autoRange: no data gives the default -100..-20", C.autoRange([], 10, 10, -100, 0.5), [-100, -20]);

// ---- row store
const st = new C.Store(3);
st.put({ idx: 5, t: 1.0 }); st.put({ idx: 6, t: 1.1 }); st.put({ idx: 6, t: 1.1, rev: 2 }); st.put({ idx: 4, t: 0.9 });
eq("store: rows sorted by idx, a revised idx replaces the row (no duplicate)", st.rows.map(r => [r.idx, r.rev || 0]), [[4, 0], [5, 0], [6, 2]]);
st.put({ idx: 7, t: 1.2 });
eq("store: bounded, the oldest row leaves", st.rows.map(r => r.idx), [5, 6, 7]);
eq("store.nearest: the closest row inside maxDt, null beyond it (positive twin: just inside)", [st.nearest(1.04, 0.1).idx, st.nearest(1.04, 0.01), st.nearest(1.16, 0.1).idx, st.nearest(5, 0.1)], [5, null, 7, null]);

// ---- text
eq("fmtBand across units", [C.fmtBand(250, 315), C.fmtBand(800, 1000), C.fmtBand(4000, 4240), C.fmtBand(2000, 2118.9)], ["250-315 Hz", "800 Hz-1 kHz", "4-4.24 kHz", "2-2.12 kHz"]);
eq("fmtNum keeps integers whole (10 is not 1) and trims trailing zeros", [C.fmtNum(10, 0), C.fmtNum(100, 0), C.fmtNum(1, 2), C.fmtNum(4.2, 2), C.fmtNum(-52.5, 1)], ["10", "100", "1", "4.2", "-52.5"]);
const bins = new Uint8Array(46).fill(60); bins[30] = 200;
eq("summaryText: state, beam, the loudest band with its level and distance from the median, the speech-suppressed seconds",
  C.summaryText({ state: "live", bins, edges: EDGES, floor: -100, step: 0.5, beamLabel: "mean of 8 beams", gatedS: 0, spanS: 30 }),
  "Live, mean of 8 beams. Loudest band " + C.fmtBand(EDGES[30], EDGES[31]) + " at 0 dB, 70 dB above the median band. Speech suppressed 0 s of the last 30 s.");
eq("summaryText: disabled and no-data states", [C.summaryText({ state: "disabled" }), C.summaryText({ state: "no_data", bins: null })], ["The live spectrum is not enabled on this server.", "No spectrum data yet."]);
eq("summaryText: auth_required asks for a sign-in (positive twin: disabled keeps its own text)",
  [C.summaryText({ state: "auth_required" }), C.summaryText({ state: "disabled" })], ["Sign in to see the live spectrum.", "The live spectrum is not enabled on this server."]);
eq("summaryText: stale data is said to be stale", C.summaryText({ state: "stale", bins, edges: EDGES, floor: -100, step: 0.5, beamLabel: "beam 3", gatedS: 1.5, spanS: 30 }).slice(0, 20), "Stale, beam 3. Loude");

console.log("\n" + n + " checks, " + fails + " failure(s)");
process.exit(fails ? 1 : 0);
