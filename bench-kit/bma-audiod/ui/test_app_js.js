// Pure-function tests for static/app.js (run: node test_app_js.js). The DOM-free helpers are exported by app.js itself.
// The local time zone is pinned so the clock-boundary and hour-of-day tests have exact answers.
process.env.TZ = "America/New_York";
const A = require("./static/app.js");
let n = 0, fails = 0;
function eq(name, got, want) {
  n++;
  const ok = JSON.stringify(got) === JSON.stringify(want);
  console.log((ok ? "PASS " : "FAIL ") + name + (ok ? "" : "  [got " + JSON.stringify(got) + ", want " + JSON.stringify(want) + "]"));
  if (!ok) fails++;
}
const lt = (y, mo, d, h, mi) => new Date(y, mo - 1, d, h, mi || 0).getTime() / 1000;   // local wall clock -> epoch seconds

eq("time zone pin works (2026-10-25 12:00 local is 16:00Z, EDT)", lt(2026, 10, 25, 12, 0), Date.UTC(2026, 9, 25, 16, 0) / 1000);
eq("niceTicks 0..10 in ~5 steps", A.niceTicks(0, 10, 5), [0, 2, 4, 6, 8, 10]);
eq("niceTicks -2..0 in 4", A.niceTicks(-2, 0, 4), [-2, -1.5, -1, -0.5, 0]);
eq("niceTicks of an empty span still gives ticks", A.niceTicks(5, 5, 4).length > 0, true);
eq("niceTicks non-finite input gives none", A.niceTicks(NaN, 1, 4), []);
// the bias chart bug: bars above the highest tick
eq("niceRange -2.1..0.6 in 4 covers both ends: -3..1 step 1, the positive bars get a tick", A.niceRange(-2.1, 0.6, 4), { lo: -3, hi: 1, ticks: [-3, -2, -1, 0, 1] });
eq("niceRange of data wholly inside one step still reaches both bounds: 0.2..0.3 in 4", (() => { const r = A.niceRange(0.2, 0.3, 4); return [r.lo <= 0.2, r.hi >= 0.3]; })(), [true, true]);
eq("niceRange 0..10 in 5 matches niceTicks", A.niceRange(0, 10, 5), { lo: 0, hi: 10, ticks: [0, 2, 4, 6, 8, 10] });
eq("niceRange has no float noise (0.1 steps)", A.niceRange(0, 0.3, 3).ticks, [0, 0.1, 0.2, 0.3]);
eq("niceRange non-finite input gives null", A.niceRange(NaN, 1, 4), null);
eq("seqColor 0 is the first viridis stop", A.seqColor(0), "rgb(68,1,84)");
eq("seqColor 1 is the last viridis stop", A.seqColor(1), "rgb(253,231,37)");
eq("seqColor clamps below 0", A.seqColor(-5), "rgb(68,1,84)");
eq("seqColor 0.5 is the middle stop", A.seqColor(0.5), "rgb(33,144,141)");
eq("polar: azimuth 0 points up, 90 right (array frame, clockwise)", [A.polar(100, 100, 50, 0).map(Math.round), A.polar(100, 100, 50, 90).map(Math.round)], [[100, 50], [150, 100]]);
eq("polar: azimuth 180 points down", A.polar(100, 100, 50, 180).map(Math.round), [100, 150]);
eq("wedgePath is exactly: centre, edge at az-half, arc to az+half, close (azimuth 0, +-22.5, r 5)", A.wedgePath(10, 10, 5, 0, 22.5), "M10 10L8.1 5.4A5.0 5.0 0 0 1 11.9 5.4Z");
eq("fmtAge seconds / minutes / hours / n/a", [A.fmtAge(12), A.fmtAge(600), A.fmtAge(7200), A.fmtAge(null)], ["12 s", "10 min", "2.0 h", "n/a"]);
eq("fmtAge boundary 89 s -> seconds, 90 s -> minutes", [A.fmtAge(89), A.fmtAge(90)], ["89 s", "2 min"]);
eq("fmtAge boundary 5399 s -> minutes, 5400 s -> hours", [A.fmtAge(5399), A.fmtAge(5400)], ["90 min", "1.5 h"]);
eq("fmtAge boundary 172799 s -> hours, 172800 s -> days", [A.fmtAge(172799), A.fmtAge(172800)], ["48.0 h", "2.0 d"]);
eq("fmtNum handles null and keeps no negative zero", [A.fmtNum(null), A.fmtNum(-0.04, 1), A.fmtNum(-1.26, 1), A.fmtNum(3, 2)], ["n/a", "0.0", "-1.3", "3.00"]);
eq("percentile picks the right element", [A.percentile([1, 2, 3, 4, 5], 0.0), A.percentile([1, 2, 3, 4, 5], 0.5), A.percentile([], 0.5)], [1, 3, null]);
eq("heatBucket by range: 1 h -> 60 s, 24 h -> 300 s, 7 d -> 3600 s", [A.heatBucket(3600), A.heatBucket(86400), A.heatBucket(604800)], [60, 300, 3600]);
eq("heatBucket tiers: 6 h edge 60, just over 6 h 300, 24 h edge 300, 36 h 600, 48 h edge 600, just over 48 h 3600",
  [A.heatBucket(21600), A.heatBucket(21601), A.heatBucket(86400), A.heatBucket(36 * 3600), A.heatBucket(48 * 3600), A.heatBucket(48 * 3600 + 1)], [60, 300, 300, 600, 600, 3600]);
eq("okabe-ito palette has 8 distinct colours", new Set(A.BEAM_COLORS).size, 8);

// hour-of-day counts are by LOCAL hour (EDT = UTC-4 on 2026-10-25)
eq("hourOfDayCounts: 16:00Z is local hour 12; 04:00Z next day is local hour 0; counts add", (() => { const c = A.hourOfDayCounts([{ hour_s: Date.UTC(2026, 9, 25, 16) / 1000, events: 3 }, { hour_s: Date.UTC(2026, 9, 26, 4) / 1000, events: 2 }, { hour_s: Date.UTC(2026, 9, 25, 16, 59) / 1000, events: 1 }]); return [c.length, c[12], c[0], c.reduce((a, b) => a + b, 0)]; })(), [24, 4, 2, 6]);
eq("hourOfDayCounts positive twin: local hour 16 (= 20:00Z) is empty for the 16:00Z input", A.hourOfDayCounts([{ hour_s: Date.UTC(2026, 9, 25, 16) / 1000, events: 3 }])[16], 0);

// time ticks sit on local clock boundaries
const hm = ts => ts.map(t => new Date(t * 1000).getHours() + ":" + new Date(t * 1000).getMinutes());
eq("timeTickValues: 10:53..16:07 local in <=7 ticks -> hourly ticks 11:00..16:00", hm(A.timeTickValues(lt(2026, 10, 25, 10, 53), lt(2026, 10, 25, 16, 7), 7)), ["11:0", "12:0", "13:0", "14:0", "15:0", "16:0"]);
eq("timeTickValues: 23:46 to 05:20 next day in <=7 ticks -> hourly, 00:00..05:00 across midnight (not 23:46, 05:20)", hm(A.timeTickValues(lt(2026, 10, 25, 23, 46), lt(2026, 10, 26, 5, 20), 7)), ["0:0", "1:0", "2:0", "3:0", "4:0", "5:0"]);
eq("timeTickValues: 24 h in <=9 ticks -> every 3 h, local midnight aligned", hm(A.timeTickValues(lt(2026, 10, 25, 1, 0), lt(2026, 10, 26, 1, 0), 9)), ["3:0", "6:0", "9:0", "12:0", "15:0", "18:0", "21:0", "0:0"]);
eq("timeTickValues: 25 min in <=6 ticks -> 5-minute ticks on :05 :10 ...", hm(A.timeTickValues(lt(2026, 10, 25, 12, 2), lt(2026, 10, 25, 12, 27), 6)), ["12:5", "12:10", "12:15", "12:20", "12:25"]);
eq("timeTickValues: the DST end day keeps LOCAL hour labels (2026-11-01 00:00..06:00 local, 1 h ticks)", hm(A.timeTickValues(lt(2026, 11, 1, 0, 0), lt(2026, 11, 1, 6, 0), 8)), ["0:0", "1:0", "2:0", "3:0", "4:0", "5:0", "6:0"]);
eq("timeTickValues: 10 days in <=6 ticks -> every 2nd local midnight", A.timeTickValues(lt(2026, 10, 20, 12, 0), lt(2026, 10, 30, 12, 0), 6).map(t => { const d = new Date(t * 1000); return [d.getDate(), d.getHours()]; }), [[22, 0], [24, 0], [26, 0], [28, 0], [30, 0]]);
eq("timeTickValues: empty or inverted range gives none", [A.timeTickValues(100, 100, 5), A.timeTickValues(200, 100, 5), A.timeTickValues(NaN, 100, 5)], [[], [], []]);

// heatmap colour scale is stable under small data jitter
eq("stableScale: no previous scale -> take the new one", A.stableScale(null, -98.5, -80.1, 1.5), { lo: -98.5, hi: -80.1 });
eq("stableScale: movement within the tolerance keeps the previous limits (no flicker)", A.stableScale({ lo: -98.5, hi: -80.1 }, -98.0, -80.9, 1.5), { lo: -98.5, hi: -80.1 });
eq("stableScale: tolerance edge 1.5 is kept, 1.6 moves", [A.stableScale({ lo: 0, hi: 10 }, 1.5, 10, 1.5), A.stableScale({ lo: 0, hi: 10 }, 1.6, 10, 1.5)], [{ lo: 0, hi: 10 }, { lo: 1.6, hi: 10 }]);
eq("stableScale: a real change in either limit moves the scale", [A.stableScale({ lo: 0, hi: 10 }, 0, 14, 1.5), A.stableScale({ lo: 0, hi: 10 }, -3, 10, 1.5)], [{ lo: 0, hi: 14 }, { lo: -3, hi: 10 }]);

// board messages never show a raw error code
eq("boardErrText: disabled is not an outage", A.boardErrText("board_disabled"), "Board polling is disabled.");
eq("boardErrText: unreachable and token problems are told apart", [A.boardErrText("unreachable"), A.boardErrText("token_unavailable")], ["The board did not answer.", "The board token file could not be read."]);
eq("boardErrText: an HTTP error code is not echoed", A.boardErrText("http_500_boom_"), "The board answered with an error.");
eq("boardErrText: nothing known", A.boardErrText(undefined), "The board is not available.");
// beam-slot mapping flag (shaded region before the server-provided fix time)
eq("shadeBefore: fix at the middle of the range shades exactly half", A.shadeBefore(100, 200, 150), 0.5);
eq("shadeBefore: fix before the range shades nothing, after the range shades all", [A.shadeBefore(100, 200, 50), A.shadeBefore(100, 200, 300)], [0, 1]);
eq("shadeBefore: an empty range or a missing fix shades nothing", [A.shadeBefore(100, 100, 150), A.shadeBefore(100, 200, undefined), A.shadeBefore(100, 200, null)], [0, 0, 0]);
eq("shadeBars: the hour holding the fix is counted (partly unreliable), the hour starting exactly at it is not", [A.shadeBars([0, 3600, 7200], 3601), A.shadeBars([0, 3600, 7200], 3600)], [2, 1]);
eq("shadeBars: fix before all hours -> 0, after all -> 3, missing -> 0", [A.shadeBars([0, 3600, 7200], 0), A.shadeBars([0, 3600, 7200], 99999), A.shadeBars([0, 3600, 7200], null)], [0, 3, 0]);
eq("shadeCols: 60 s buckets, fix at 125 s -> 3 leading columns, fix exactly on a bucket edge (120) -> 2", [A.shadeCols(0, 60, 10, 125), A.shadeCols(0, 60, 10, 120)], [3, 2]);
eq("shadeCols: fix before the range -> 0, after -> all columns", [A.shadeCols(0, 60, 10, -5), A.shadeCols(0, 60, 10, 99999)], [0, 10]);

// physical directions of the upright panel (calibrated panel frame, clockwise seen from the front, 0 = up)
eq("physicalDirs: panel frame -> UP 0, RIGHT 90, DOWN 180, LEFT 270", A.physicalDirs("panel"), [{ label: "UP", az: 0 }, { label: "RIGHT", az: 90 }, { label: "DOWN", az: 180 }, { label: "LEFT", az: 270 }]);
eq("physicalDirs: array (uncalibrated) frame and unknown -> null", [A.physicalDirs("array"), A.physicalDirs(undefined)], [null, null]);
eq("dirNear: the stored azimuths 345, 75, 165, 255 are within 22.5 degrees of UP, RIGHT, DOWN, LEFT", [345, 75, 165, 255].map(a => A.dirNear(a, "panel")), ["UP", "RIGHT", "DOWN", "LEFT"]);
eq("dirNear: 30 and 120 are between two directions -> none", [A.dirNear(30, "panel"), A.dirNear(120, "panel")], [null, null]);
eq("dirNear: exactly 22.5 away counts, 22.6 does not", [A.dirNear(67.5, "panel"), A.dirNear(67.4, "panel")], ["RIGHT", null]);
eq("dirNear: uncalibrated frame never names a direction", A.dirNear(345, "array"), null);

// detections scatter helpers
eq("dotRadius: 3 px at confidence 0, 6 at 0.5, 9 at 1", [A.dotRadius(0), A.dotRadius(0.5), A.dotRadius(1)], [3, 6, 9]);
eq("dotRadius: clamped to 3..9 and tolerant of junk", [A.dotRadius(2), A.dotRadius(-1), A.dotRadius("x")], [9, 3, 3]);
eq("scatterLanes: the stored panel azimuths order the lanes 30,75,120,165,210,255,300,345 = beams 2,1,8,7,6,5,4,3", A.scatterLanes([75, 30, 345, 300, 255, 210, 165, 120]), [2, 1, 8, 7, 6, 5, 4, 3]);
eq("scatterLanes: equal azimuths keep beam order", A.scatterLanes([10, 10, 5, 10, 10, 10, 10, 10]), [3, 1, 2, 4, 5, 6, 7, 8]);
eq("shapeAttrs: circle", A.shapeAttrs("circle", 10, 20, 5), ["circle", { cx: "10.0", cy: "20.0", r: "5.0" }]);
eq("shapeAttrs: square", A.shapeAttrs("square", 10, 20, 5), ["rect", { x: "5.0", y: "15.0", width: "10.0", height: "10.0" }]);
eq("shapeAttrs: triangle points up", A.shapeAttrs("triangle", 10, 20, 5), ["polygon", { points: "10.0,15.0 15.0,25.0 5.0,25.0" }]);
eq("shapeAttrs: diamond", A.shapeAttrs("diamond", 10, 20, 5), ["polygon", { points: "10.0,15.0 15.0,20.0 10.0,25.0 5.0,20.0" }]);
eq("shapeAttrs: an unknown shape falls back to the circle", A.shapeAttrs("hexagon", 10, 20, 5)[0], "circle");
const many = []; for (let i = 1; i <= 9; i++) for (let k = 0; k < 10 - i; k++) many.push({ species: "s" + i });
const sty = A.speciesStyles(many);
eq("speciesStyles: the 7 commonest species get a colour and a shape in rank order", [1, 2, 3, 4, 5, 6, 7].map(i => [sty.by["s" + i].color, sty.by["s" + i].shape]),
  [["#E69F00", "circle"], ["#56B4E9", "square"], ["#009E73", "triangle"], ["#F0E442", "diamond"], ["#0072B2", "circle"], ["#D55E00", "square"], ["#CC79A7", "triangle"]]);
eq("speciesStyles: the 8th and 9th share the grey 'other' bucket", [sty.by.s8, sty.by.s9], [{ color: "#9AA7B4", shape: "circle" }, { color: "#9AA7B4", shape: "circle" }]);
eq("speciesStyles: legend has 7 species with counts plus one 'other' row counting 2 + 1", [sty.legend.length, sty.legend[0], sty.legend[7]],
  [8, { label: "s1", count: 9, color: "#E69F00", shape: "circle" }, { label: "other species (2)", count: 3, color: "#9AA7B4", shape: "circle" }]);
eq("speciesStyles: equal counts are ordered by name, so colours do not depend on event order", [A.speciesStyles([{ species: "b" }, { species: "a" }]).legend.map(l => l.label), A.speciesStyles([{ species: "a" }, { species: "b" }]).legend.map(l => l.label)], [["a", "b"], ["a", "b"]]);

const hostile = A.speciesStyles([{ species: "constructor" }, { species: "A" }, { species: "A" }, { species: "__proto__" }, { species: "toString" }]);
eq("speciesStyles: species named like Object.prototype members get their own exact count and style", hostile.legend,
  [{ label: "A", count: 2, color: "#E69F00", shape: "circle" }, { label: "__proto__", count: 1, color: "#56B4E9", shape: "square" }, { label: "constructor", count: 1, color: "#009E73", shape: "triangle" }, { label: "toString", count: 1, color: "#F0E442", shape: "diamond" }]);
eq("speciesStyles: every one of those names is an own key of the style map", Object.keys(hostile.by).sort(), ["A", "__proto__", "constructor", "toString"]);
eq("speciesStyles: the style lookup of those names is an own entry", [hostile.by["constructor"], hostile.by["__proto__"]], [{ color: "#009E73", shape: "triangle" }, { color: "#56B4E9", shape: "square" }]);
eq("slotNote: shows the instant in local time and in UTC (2026-10-25T21:47:00Z = 17:47 EDT)", A.slotNote(1792964820), "Before 10/25 17:47 local (21:47 UTC on 10/25) the beam slots were mapped 45° off: beam numbers and azimuths in the hatched region are unreliable.");
eq("frameShort: compass profile is named compass bearings, panel and array unchanged", ["compass", "panel", "array"].map(A.frameShort), ["compass bearings", "panel frame", "DSP-frame fallback"]);
eq("physicalDirs: a compass frame has no UP/DOWN marks (positive twin: panel has four)", [A.physicalDirs("compass"), A.physicalDirs("panel").length], [null, 4]);

// ---------------------------------------------------------------- taxonomic groups
eq("GROUP_KEYS: exactly the six server groups in display order", A.GROUP_KEYS, ["bird", "insect", "amphibian", "mammal", "other-bio", "mechanical"]);
eq("groupName: every group has a text name; an unknown group is read as Other biological", [...A.GROUP_KEYS, "mystery"].map(A.groupName), ["Bird", "Insect", "Amphibian", "Mammal", "Other biological", "Non-biological", "Other biological"]);
eq("every group has its own marker shape (never colour only) and its own colour", [new Set(A.GROUP_KEYS.map(g => A.GROUP_SHAPES[g])).size, new Set(A.GROUP_KEYS.map(g => A.GROUP_COLORS[g])).size], [6, 6]);
eq("shapeAttrs: cross is a 12-point plus", A.shapeAttrs("cross", 10, 20, 5)[1].points.split(" ").length, 12);
eq("shapeAttrs: cross exact points (arm half-width 0.35 r)", A.shapeAttrs("cross", 10, 20, 5), ["polygon", { points: "8.3,15.0 11.8,15.0 11.8,18.3 15.0,18.3 15.0,21.8 11.8,21.8 11.8,25.0 8.3,25.0 8.3,21.8 5.0,21.8 5.0,18.3 8.3,18.3" }]);
eq("shapeAttrs: bar is a flat rectangle", A.shapeAttrs("bar", 10, 20, 5), ["rect", { x: "5.0", y: "18.0", width: "10.0", height: "4.0" }]);
const sp = (name, group, events) => ({ species: name, group: group, events: events });
const kat100 = [sp("katydid", "insect", 100)];
const frogs = [sp("Toad A", "amphibian", 3), sp("Toad B", "amphibian", 2), sp("Frog C", "amphibian", 1)];
eq("topPerGroup: a katydid with 100 events cannot crowd out the 3 frogs (all 3 kept, katydid kept)", A.topPerGroup(kat100.concat(frogs), 3).map(s => s.species), ["katydid", "Toad A", "Toad B", "Frog C"]);
eq("topPerGroup: positive twin - k=2 keeps the 2 busiest frogs, drops the third", A.topPerGroup(kat100.concat(frogs), 2).map(s => s.species), ["katydid", "Toad A", "Toad B"]);
eq("topPerGroup: groups come out in display order whatever the input order, 3 per group", A.topPerGroup([sp("i1", "insect", 9), sp("b1", "bird", 5), sp("b2", "bird", 4), sp("b3", "bird", 3), sp("b4", "bird", 2), sp("m1", "mammal", 1)], 3).map(s => s.species), ["b1", "b2", "b3", "i1", "m1"]);
eq("topPerGroup: a species with no group counts as other-bio (not dropped)", A.topPerGroup([sp("x", undefined, 1), sp("y", "bogus", 1)], 3).map(s => s.species), ["x", "y"]);
eq("topPerGroup: empty list", A.topPerGroup([], 3), []);
const ev = (species, t0, t1, beam, conf, extra) => Object.assign({ species: species, scientific: species + " sp", group: "insect", t_start_s: t0, t_end_s: t1, best_beam: beam, best_beam_azimuth_deg: beam * 10, beams: [beam], reports: 1, max_confidence: conf, in_region: true, non_species: false }, extra || {});
eq("collapseRuns: the boundary - a gap of exactly 120 s (older end 880 to newer start 1000) merges into one run of 2", A.collapseRuns([ev("k", 1000, 1000, 3, 0.5), ev("k", 800, 880, 3, 0.6)], 120).map(r => [r.n, r.t_first, r.t_last]), [[2, 800, 1000]]);
eq("collapseRuns: positive twin - 121 s apart is two runs", A.collapseRuns([ev("k", 1000, 1000, 3, 0.5), ev("k", 800, 879, 3, 0.6)], 120).map(r => [r.n, r.t_first, r.t_last]), [[1, 1000, 1000], [1, 800, 800]]);
const mix = A.collapseRuns([ev("k", 1000, 1000, 3, 0.5), ev("o", 990, 990, 1, 0.4, { group: "bird" }), ev("k", 950, 950, 4, 0.9), ev("k", 900, 900, 3, 0.3)], 120);
eq("collapseRuns: another species in between does not break a run; rows ordered by newest event", mix.map(r => [r.species, r.n, r.beams, r.best_beam, r.max_confidence]), [["k", 3, [3, 4], 4, 0.9], ["o", 1, [1], 1, 0.4]]);
eq("collapseRuns: the run keeps the best-confidence beam's azimuth and sums the reports", [mix[0].best_beam_azimuth_deg, mix[0].reports], [40, 3]);
eq("collapseRuns: two species that share a common name but differ in scientific name stay separate", A.collapseRuns([ev("same", 100, 100, 1, 0.5, { scientific: "A a" }), ev("same", 99, 99, 1, 0.5, { scientific: "B b" })], 120).length, 2);
const trunc = A.collapseRuns([ev("o", 1190, 1190, 1, 0.4, { group: "bird" }), ev("k", 1000, 1000, 3, 0.5), ev("k", 900, 900, 3, 0.3)], 120, true);
eq("collapseRuns: on a truncated list the run that reaches the oldest fetched event is open-ended (may continue unseen), the other is not", trunc.map(r => [r.species, r.n, r.open_ended]), [["o", 1, false], ["k", 2, true]]);
eq("collapseRuns: positive twin - the same list not truncated has no open-ended run", A.collapseRuns([ev("k", 1000, 1000, 3, 0.5), ev("k", 900, 900, 3, 0.3)], 120, false).map(r => r.open_ended), [false]);
eq("collapseRuns: a run more than 120 s newer than the oldest fetched event is not open-ended even when the list is truncated", A.collapseRuns([ev("k", 1000, 1000, 3, 0.5), ev("o", 700, 700, 1, 0.4)], 120, true).map(r => [r.species, r.open_ended]), [["k", false], ["o", true]]);
eq("runText: open-ended runs end with + (a single open-ended event shows its count too)", [A.runText({ species: "K", n: 459, open_ended: true }), A.runText({ species: "K", n: 1, open_ended: true })], ["K \u00d7459+", "K \u00d71+"]);
eq("collapseRuns: open-end boundary - a run exactly 120 s newer than the oldest fetched event is open-ended, 121 s is not", [A.collapseRuns([ev("o", 1120, 1120, 1, 0.4), ev("k", 1000, 1000, 3, 0.5)], 120, true).map(r => r.open_ended), A.collapseRuns([ev("o", 1121, 1121, 1, 0.4), ev("k", 1000, 1000, 3, 0.5)], 120, true).map(r => r.open_ended)], [[true, true], [false, true]]);
eq("runText: a run of 37 shows the count, a single event does not", [A.runText({ species: "Katydid", n: 37 }), A.runText({ species: "Owl", n: 1 })], ["Katydid ×37", "Owl"]);
eq("zoomRange: data 1000..2000 in range 0..86400 gets 5 % margins (50 s -> the 60 s minimum)", A.zoomRange(1000, 2000, 0, 86400, false), [940, 2060]);
eq("zoomRange: 5 % of a long span: 10000..20000 -> margin 500", A.zoomRange(10000, 20000, 0, 86400, false), [9500, 20500]);
eq("zoomRange: a single instant gets +-15 min", A.zoomRange(5000, 5000, 0, 86400, false), [4100, 5900]);
eq("zoomRange: clamped to the requested range", A.zoomRange(10, 100, 0, 150, false), [0, 150]);
eq("zoomRange: the explicit full-range toggle returns the requested range", A.zoomRange(1000, 2000, 0, 86400, true), [0, 86400]);
eq("zoomRange: no data falls back to the requested range", A.zoomRange(null, null, 0, 86400, false), [0, 86400]);
eq("densityOpacity: one event in a cell where the busiest has one -> 1; log scale in between", [A.densityOpacity(1, 1), A.densityOpacity(1, 1000), A.densityOpacity(1000, 1000), A.densityOpacity(10, 100)], [1, 0.28, 1, 0.616]);
eq("densityOpacity: nothing to scale against is 0", A.densityOpacity(0, 0), 0);
const rows = [{ t_s: 100, species: "a", group: "insect", best_beam: 3, events: 2 }, { t_s: 100, species: "b", group: "insect", best_beam: 3, events: 5 }, { t_s: 100, species: "c", group: "bird", best_beam: 3, events: 1 },
  { t_s: 100, species: "a", group: "insect", best_beam: 4, events: 1 }, { t_s: 40, species: "a", group: "insect", best_beam: 3, events: 1 }];
eq("densityCells: sums per (bucket, beam, group), keeps the exact counts and the species behind them, sorted by time, beam, group",
  A.densityCells(rows).map(c => [c.t_s, c.best_beam, c.group, c.events, c.species]),
  [[40, 3, "insect", 1, [["a", 1]]], [100, 3, "bird", 1, [["c", 1]]], [100, 3, "insect", 7, [["b", 5], ["a", 2]]], [100, 4, "insect", 1, [["a", 1]]]]);
eq("densityCells: the cell counts add up to the row counts (no event lost)", A.densityCells(rows).reduce((a, c) => a + c.events, 0), 10);
const hs = [3600, 7200, 10800];
const sph = [{ species: "k", group: "insect", hourly_counts: [{ hour_s: 3600, events: 400 }, { hour_s: 7200, events: 300 }] }, { species: "w", group: "bird", hourly_counts: [{ hour_s: 7200, events: 4 }, { hour_s: 99999, events: 9 }] },
  { species: "z", group: "bogus", hourly_counts: [{ hour_s: 10800, events: 2 }] }];
eq("groupHourly: per-hour counts by group in display order; hours outside the window are ignored; an unknown group is other-bio", A.groupHourly(sph, hs),
  [{ group: "bird", values: [0, 4, 0], total: 4 }, { group: "insect", values: [400, 300, 0], total: 700 }, { group: "other-bio", values: [0, 0, 2], total: 2 }]);
eq("groupHourly: a group with no events in the window is absent (positive twin: the others are there)", A.groupHourly([sph[1]], [3600]).length, 0);
const mixed = A.speciesStyles([{ species: "k", group: "insect" }, { species: "k", group: "insect" }, { species: "w", group: "bird" }, { species: "t", group: "amphibian" }]);
eq("speciesStyles with several groups: colour AND shape by group, legend lists the groups with counts in display order", [mixed.mode, mixed.by.k, mixed.by.w, mixed.legend],
  ["group", { color: "#E69F00", shape: "diamond", group: "insect" }, { color: "#0072B2", shape: "circle", group: "bird" },
    [{ label: "Bird", count: 1, color: "#0072B2", shape: "circle" }, { label: "Insect", count: 2, color: "#E69F00", shape: "diamond" }, { label: "Amphibian", count: 1, color: "#009E73", shape: "triangle" }]]);
eq("speciesStyles with ONE group (a chip is selected): colour and shape by species, as before", [A.speciesStyles([{ species: "k", group: "insect" }, { species: "j", group: "insect" }]).mode, A.speciesStyles([{ species: "k", group: "insect" }, { species: "j", group: "insect" }]).legend.map(l => l.label)], ["species", ["j", "k"]]);

// ---------------------------------------------------------------- azimuth names, steerable bands, pattern card
eq("azName: the 8 stored panel azimuths [75,30,345,300,255,210,165,120]", [75, 30, 345, 300, 255, 210, 165, 120].map(a => A.azName(a, "panel")), ["right", "up-right", "up", "up-left", "left", "down-left", "down", "down-right"]);
eq("azName: the four cardinals and four diagonals", [0, 45, 90, 135, 180, 225, 270, 315].map(a => A.azName(a, "panel")), ["up", "up-right", "right", "down-right", "down", "down-left", "left", "up-left"]);
eq("azName: boundary 22.5 is a tie that goes to the cardinal (the same as dirNear), 22.6 is the diagonal", [A.azName(22.5, "panel"), A.azName(22.6, "panel"), A.azName(22.4, "panel")], ["up", "up-right", "up"]);
eq("azName: boundary 67.5 ties to right, 67.4 is up-right; 112.5 ties to right, 112.6 is down-right", [A.azName(67.5, "panel"), A.azName(67.4, "panel"), A.azName(112.5, "panel"), A.azName(112.6, "panel")], ["right", "up-right", "right", "down-right"]);
eq("azName: wraps at 360 and handles negative angles", [A.azName(360, "panel"), A.azName(-45, "panel"), A.azName(337.6, "panel"), A.azName(337.4, "panel")], ["up", "up-left", "up", "up-left"]);
eq("azName: only the panel frame has physical names (array, compass, missing frame, non-number give null)", [A.azName(90, "array"), A.azName(90, "compass"), A.azName(90, undefined), A.azName("x", "panel"), A.azName(NaN, "panel")], [null, null, null, null, null]);
eq("dirNear agrees with azName at the cardinals (22.5 deg tie): UP both", [A.dirNear(22.5, "panel"), A.azName(22.5, "panel")], ["UP", "up"]);
eq("not steerable bands are exactly 50-125 and 125-250 Hz", [0, 1, 2, 3, 4, 5, 6, 7].map(A.isSteerable), [false, false, true, true, true, true, true, true]);
eq("NOT_STEERABLE_BANDS constant", A.NOT_STEERABLE_BANDS, [0, 1]);
eq("STEER_NOTE says what it means and why", A.STEER_NOTE, "Not steerable: 50-250 Hz is identical on all beams (the array cannot steer below 250 Hz), so differences between beams in this band are channel gain or noise, not direction.");
const calP = { calibrated: true, frame_kind: "panel", provisional: true, measured_at: "2026-10-25T22:01:46Z", beam_azimuth_deg: [75, 30, 345, 300, 255, 210, 165, 120] };
const calDirs = "Beam directions from the stored calibration profile (panel frame, upright array, provisional, measured 2026-10-25T22:01:46Z), metadata only, not read back from the DSPs: B1 75° right, B2 30° up-right, B3 345° up, B4 300° up-left, B5 255° left, B6 210° down-left, B7 165° down, B8 120° down-right";
eq("patternInfo: applied=true without a name says so and does NOT say unknown; directions are credited to the stored calibration", A.patternInfo({ pattern_ref: null, beams_applied_by_dspd: true }, calP),
  { value: "applied by dspd", sub: "reported by dspd, which gives no pattern name", dirs: calDirs });
eq("patternInfo: a reported pattern_ref is shown as reported by dspd", A.patternInfo({ pattern_ref: "pattern-7", beams_applied_by_dspd: true }, calP).slice === undefined && [A.patternInfo({ pattern_ref: "pattern-7", beams_applied_by_dspd: true }, calP).value, A.patternInfo({ pattern_ref: "pattern-7", beams_applied_by_dspd: true }, calP).sub], ["pattern-7", "reported by dspd (board state)"]);
eq("patternInfo: applied=false is 'not applied'", [A.patternInfo({ pattern_ref: null, beams_applied_by_dspd: false }, calP).value, A.patternInfo({ pattern_ref: null, beams_applied_by_dspd: false }, calP).sub], ["not applied", "dspd reports no applied pattern"]);
eq("patternInfo: applied=null (board not readable) is 'not readable', never 'applied' or 'not applied'", [A.patternInfo({ pattern_ref: null, beams_applied_by_dspd: null }, calP).value, A.patternInfo({ pattern_ref: null, beams_applied_by_dspd: null }, calP).sub],
  ["not readable", "the board state could not be read, so the applied pattern is unknown"]);
eq("patternInfo: no board object at all is 'not readable'", A.patternInfo(undefined, undefined).value, "not readable");
eq("patternInfo: no calibration stored -> no physical direction claimed, fallback angles are not a pattern", A.patternInfo({ beams_applied_by_dspd: false }, { calibrated: false, frame_kind: "array", beam_azimuth_deg: [90, 45, 0, 315, 270, 225, 180, 135] }).dirs,
  "Beam directions: no calibration profile stored, so no physical direction is known (the DSP-frame fallback angles are not a pattern).");
eq("patternInfo: a compass-frame profile gets no up/down names (only panel azimuths do)", A.patternInfo({ beams_applied_by_dspd: true }, { calibrated: true, frame_kind: "compass", provisional: true, measured_at: null, beam_azimuth_deg: [0, 45, 90, 135, 180, 225, 270, 315] }).dirs,
  "Beam directions from the stored calibration profile (compass bearings, provisional), metadata only, not read back from the DSPs: B1 0°, B2 45°, B3 90°, B4 135°, B5 180°, B6 225°, B7 270°, B8 315°");

// ---- Stage B: direction strip, event detail, hour x direction multiples, health sparklines
eq("ringRelative: two beams at 0 and -10 dB against the power-mean ring -2.6 dB are +2.6 and -7.4; a column with one beam is 0 for it; null stays null, an empty column is all null",
  A.ringRelative([[0, -10, null], [-10, null, null]]), [[2.6, 0, null], [-7.4, null, null]]);
eq("ringRelative: equal beams are all 0 (positive twin)", A.ringRelative([[-30, -30], [-30, -30], [-30, -30]]), [[0, 0], [0, 0], [0, 0]]);
eq("ringRelative: no beams gives no rows", A.ringRelative([]), []);
eq("DIR_SPAN_DB is the compass span (6 dB)", A.DIR_SPAN_DB, 6);
eq("beamDirLabel: panel frame names the direction (345 deg is 15 deg off up), the array frame and a missing azimuth give no name",
  [A.beamDirLabel(3, 345, "panel"), A.beamDirLabel(1, 75, "panel"), A.beamDirLabel(3, 345, "array"), A.beamDirLabel(3, null, "panel"), A.beamDirLabel(8, 120, "panel")], ["B3 345° up", "B1 75° right", "B3 345°", "B3", "B8 120° down-right"]);
const rep = (t, beam, az, conf, level) => ({ t_s: t, beam: beam, beam_azimuth_deg: az, confidence: conf, level_dbfs: level });
eq("traceByBeam: one entry per beam in beam order, points in time order, max confidence, max level ignoring a null level, report counts",
  A.traceByBeam([rep(10, 4, 315, 0.8, -45), rep(11, 3, 0, 0.6, null), rep(9, 3, 0, 0.5, -50)]),
  [{ beam: 3, azimuth: 0, n: 2, max_conf: 0.6, max_level: -50, points: [{ t: 9, conf: 0.5, level: -50 }, { t: 11, conf: 0.6, level: null }] }, { beam: 4, azimuth: 315, n: 1, max_conf: 0.8, max_level: -45, points: [{ t: 10, conf: 0.8, level: -45 }] }]);
eq("traceByBeam: max level is the highest, whichever report comes first (-50 then -70, and -70 then -50)", [A.traceByBeam([rep(1, 3, 0, 0.5, -50), rep(2, 3, 0, 0.5, -70)])[0].max_level, A.traceByBeam([rep(1, 3, 0, 0.5, -70), rep(2, 3, 0, 0.5, -50)])[0].max_level], [-50, -50]);
eq("traceByBeam: a beam whose reports all lack a level has max_level null (not 0)", A.traceByBeam([rep(1, 2, 30, 0.4, null)])[0].max_level, null);
eq("traceByBeam: no reports, no beams", A.traceByBeam([]), []);
const nev = (id, sp, sci, t0, t1, conf, beams, g) => ({ id: id, species: sp, scientific: sci, t_start_s: t0, t_end_s: t1, max_confidence: conf, beams: beams, group: g });
const selfEv = nev("s", "Self", "Selfus", 100, 102, 0.9, [1], "bird");
eq("neighbourSummary: one row per species, the event itself left out, ordered by the nearest |dt| (0 = overlapping), then more events, then name; exact dt, counts, beams and max confidence",
  A.neighbourSummary([selfEv, nev("k1", "Katydid", "Kat", 90, 90, 0.5, [3], "insect"), nev("k2", "Katydid", "Kat", 95, 95, 0.7, [4, 3], "insect"), nev("t1", "Toad", "Toadus", 102, 102, 0.6, [5], "amphibian"),
    nev("o1", "Owl", "Owlus", 150, 150, 0.9, [2], "bird"), nev("d1", "Deer", "Deerus", 103, 103, 0.4, [6], "mammal"), nev("f1", "Frog", "Frogus", 107, 107, 0.1, [7], "amphibian")], selfEv),
  { rows: [{ species: "Toad", scientific: "Toadus", group: "amphibian", events: 1, nearest_dt: 0, max_confidence: 0.6, beams: [5] }, { species: "Deer", scientific: "Deerus", group: "mammal", events: 1, nearest_dt: 1, max_confidence: 0.4, beams: [6] },
    { species: "Katydid", scientific: "Kat", group: "insect", events: 2, nearest_dt: -5, max_confidence: 0.7, beams: [3, 4] }, { species: "Frog", scientific: "Frogus", group: "amphibian", events: 1, nearest_dt: 5, max_confidence: 0.1, beams: [7] },
    { species: "Owl", scientific: "Owlus", group: "bird", events: 1, nearest_dt: 48, max_confidence: 0.9, beams: [2] }], events: 6 });
eq("neighbourSummary: a different event of the SAME species at the same time is a neighbour (only the same id is left out - positive twin)", A.neighbourSummary([selfEv, nev("s2", "Self", "Selfus", 100, 100, 0.2, [2], "bird")], selfEv).rows.map(r => [r.species, r.events, r.nearest_dt]), [["Self", 1, 0]]);
eq("neighbourSummary: two species with the same common name but different scientific names are two rows (the species key is the scientific name)", A.neighbourSummary([selfEv, nev("w1", "Wren", "Aa", 101, 101, 0.5, [1], "bird"), nev("w2", "Wren", "Bb", 101, 101, 0.5, [2], "bird")], selfEv).rows.map(r => [r.species, r.scientific, r.events]), [["Wren", "Aa", 1], ["Wren", "Bb", 1]]);
eq("neighbourSummary: nothing around gives no rows", A.neighbourSummary([selfEv], selfEv), { rows: [], events: 0 });
eq("neighbourSummary: a continuous caller (40 events) is ONE row with its count", (() => { const r = A.neighbourSummary(Array.from({ length: 40 }, (_, i) => nev("c" + i, "Katydid", "Kat", 90 + i * 0.5, 90 + i * 0.5, 0.3, [1 + i % 8], "insect")), selfEv); return [r.rows.length, r.rows[0].events, r.rows[0].beams]; })(), [1, 40, [1, 2, 3, 4, 5, 6, 7, 8]]);
eq("NEIGHBOUR_S is 60", A.NEIGHBOUR_S, 60);
const dr = (t, sp, sci, beam, n) => ({ t_s: t, species: sp, scientific: sci, best_beam: beam, events: n });
const hm12 = A.hourDirMatrix([dr(Date.UTC(2026, 9, 25, 16) / 1000, "A", "Aa", 3, 4), dr(Date.UTC(2026, 9, 25, 16, 30) / 1000, "A", "Aa", 3, 2), dr(Date.UTC(2026, 9, 25, 16) / 1000, "A", "Aa", 1, 1), dr(Date.UTC(2026, 9, 25, 16) / 1000, "B", "Bb", 3, 9),
  dr(Date.UTC(2026, 9, 26, 4) / 1000, "A", "Aa", 8, 7), dr(Date.UTC(2026, 9, 25, 16) / 1000, "NoSci", "", 2, 5)], "Aa");
eq("hourDirMatrix: 24 x 8, by LOCAL hour (16:00Z is hour 12 EDT, 04:00Z is hour 0), by best beam, rows of other species ignored, the counts add", [hm12.length, hm12[0].length, hm12[12][2], hm12[12][0], hm12[0][7], hm12.flat().reduce((a, v) => a + v, 0)], [24, 8, 6, 1, 7, 14]);
eq("hourDirMatrix: a row without a scientific name is keyed by its species name (positive twin)", A.hourDirMatrix([dr(Date.UTC(2026, 9, 25, 16) / 1000, "NoSci", "", 2, 5)], "NoSci")[12][1], 5);
const mod = A.dirHourModel(Array.from({ length: 24 }, (_, h) => h === 12 ? [1, 0, 3, 0, 0, 0, 0, 0] : h === 13 ? [2, 0, 3, 0, 0, 0, 0, 0] : new Array(8).fill(0)), 5);
eq("dirHourModel: an hour with 4 events is not readable, one with exactly 5 is (boundary); shares are of the hour's own total", [mod.hours[12].total, mod.hours[12].readable, mod.hours[13].total, mod.hours[13].readable, mod.hours[13].shares.slice(0, 3), mod.hours[14].readable, mod.hours[14].shares[0]], [4, false, 5, true, [0.4, 0, 0.6], false, 0]);
eq("dirHourModel: max_total and peak_hour (the first busiest hour); MIN_DIR_N is 5", [mod.max_total, mod.peak_hour, A.MIN_DIR_N], [5, 13, 5]);
eq("dirHourModel: two equally busy hours (6 events each) - the first one is the peak hour", A.dirHourModel(Array.from({ length: 24 }, (_, h) => h === 3 || h === 7 ? [6, 0, 0, 0, 0, 0, 0, 0] : new Array(8).fill(0)), 5).peak_hour, 3);
eq("dirHourModel: a continuous caller's 400-event hour and a rare caller's 5-event hour both have shares summing to 1 (the share scale is per hour)", (() => { const m = A.dirHourModel([[100, 100, 100, 100, 0, 0, 0, 0], [5, 0, 0, 0, 0, 0, 0, 0]], 5); return [m.hours[0].shares.reduce((a, v) => a + v, 0), m.hours[1].shares[0]]; })(), [1, 1]);
eq("sparkGeom: exact path for a series with a gap (null breaks the line) on a fixed 0..10 scale, 100 x 20, 2 px padding", A.sparkGeom([[0, 0], [10, 5], [20, null], [30, 10]], 100, 20, { lo: 0, hi: 10, t0: 0, t1: 30 }), { d: "M0.0 18.0L33.3 10.0M100.0 2.0", min: 0, max: 10, last: 10, n: 3 });
eq("sparkGeom: a flat series is centred (not drawn on an edge)", A.sparkGeom([[0, 3], [1, 3]], 100, 20).d, "M0.0 10.0L100.0 10.0");
eq("sparkGeom: no values gives an empty path and null extremes", A.sparkGeom([[0, null]], 100, 20), { d: "", min: null, max: null, last: null, n: 0 });
eq("sparkGeom: with lo = 0 a flat positive series sits at the top half, not centred on lo (loss 0.5 %, lo 0)", A.sparkGeom([[0, 0.5], [1, 0.5]], 100, 20, { lo: 0 }).d, "M0.0 2.0L100.0 2.0");
const ss = A.stateSummary([{ t_s: 0, state: "ok" }, { t_s: 30, state: "ok" }, { t_s: 60, state: "down" }, { t_s: 90, state: "ok" }], 0, 200);
eq("stateSummary: same-state neighbours merge, a sample holds until the next, the last holds 90 s, then 'no data'; exact seconds", [ss.runs, ss.seconds, ss.total_s, ss.ok_s, ss.no_data_s, ss.other_s],
  [[{ state: "ok", t0: 0, t1: 60 }, { state: "down", t0: 60, t1: 90 }, { state: "ok", t0: 90, t1: 180 }, { state: "no data", t0: 180, t1: 200 }], { ok: 150, down: 30, "no data": 20 }, 200, 150, 20, 30]);
const sg = A.stateSummary([{ t_s: 0, state: "ok" }, { t_s: 500, state: "ok" }], 0, 501);
eq("stateSummary: a gap longer than 90 s is 'no data' (a sample holds at most 90 s): 90 s ok, 410 s no data, then the next sample's 1 s ok", [sg.ok_s, sg.no_data_s, sg.runs.map(r => r.state)], [91, 410, ["ok", "no data", "ok"]]);
eq("stateSummary: a gap of exactly 90 s is still covered (boundary twin): samples at 0 and 90 over [0, 90] are all ok", (() => { const r = A.stateSummary([{ t_s: 0, state: "ok" }, { t_s: 90, state: "ok" }], 0, 91); return [r.ok_s, r.no_data_s]; })(), [91, 0]);
eq("stateSummary: nothing before the first sample is 'no data'; a sample before t0 is clipped to t0", [A.stateSummary([{ t_s: 50, state: "ok" }], 0, 100).seconds, A.stateSummary([{ t_s: -10, state: "ok" }], 0, 50).seconds], [{ "no data": 50, ok: 50 }, { ok: 50 }]);
eq("stateSummary: no samples at all is all 'no data'", (() => { const r = A.stateSummary([], 0, 100); return [r.runs, r.ok_s, r.no_data_s]; })(), [[{ state: "no data", t0: 0, t1: 100 }], 0, 100]);
eq("SPARK_GAP_S is 90", A.SPARK_GAP_S, 90);

// ---- advisory artifact flags (ui-http 1.11.0)
const AF_COLD = { v: 1, flags: ["cold_insect"], level: "likely", evidence: { temp_c: 2.3, temp_age_s: 0, floor_c: 5, burst_n: null, burst_spread_db: null, window_s: null } };
const AF_BURST = { v: 1, flags: ["burst_coincident"], level: "suspect", evidence: { temp_c: 12, temp_age_s: 3600, floor_c: 5, burst_n: 214, burst_spread_db: 1.6, window_s: 30 } };
const AF_UNKNOWN = { v: 1, flags: ["temp_unknown"], level: "none", evidence: { temp_c: null, temp_age_s: null, floor_c: 5, burst_n: null, burst_spread_db: null, window_s: null } };
eq("artifactFlagged: likely and suspect are flagged", [A.artifactFlagged(AF_COLD), A.artifactFlagged(AF_BURST)], [true, true]);
eq("artifactFlagged: level none (temp_unknown alone, a clean insect) is NOT flagged: no badge", [A.artifactFlagged(AF_UNKNOWN), A.artifactFlagged({ v: 1, flags: [], level: "none", evidence: {} })], [false, false]);
eq("artifactFlagged: absent, null and non-object verdicts are not flagged (a bird event has no artifact_flags)", [A.artifactFlagged(undefined), A.artifactFlagged(null), A.artifactFlagged("likely")], [false, false, false]);
eq("artifactText cold: exact wording with the temperature, the age and the floor, and the advisory sentence",
  A.artifactText(AF_COLD), "Cold: the ambient temperature was 2.3 °C (this hour), below the 5 °C floor. Insects are not expected to call that cold, so this label may be a noise the model matched to its nearest label. Advisory only: the detection is kept and counted.");
eq("artifactText burst: count, window and median spread in the text", A.artifactText(AF_BURST),
  "Burst: 214 short broadband transients within 30 s, median beam spread 1.6 dB: the pattern of a diffuse noise burst, not of a single source. Advisory only: the detection is kept and counted.");
eq("artifactText both flags: the cold sentence comes first, then the burst sentence", (() => { const t = A.artifactText({ v: 1, flags: ["burst_coincident", "cold_insect"], level: "likely", evidence: AF_BURST.evidence }); return [t.startsWith("Cold: the ambient temperature was 12.0"), t.indexOf("Burst:") > t.indexOf("Cold:")]; })(), [true, true]);
eq("artifactText temp_unknown: says the cold check could not be applied", A.artifactText(AF_UNKNOWN), "No usable temperature (no ambient record within 3 hours), so the cold check could not be applied. Advisory only: the detection is kept and counted.");
eq("artifactText: a malformed object is the empty string, never an exception", [A.artifactText(null), A.artifactText({}), A.artifactText({ flags: "x" })], ["", "", ""]);
eq("artifactCountText (toggle off): the flagged events are counted and still shown", A.artifactCountText({ artifact_events_total: 412, artifact_unscored_events: 0 }, false), "412 insect events flagged as possible artifacts (still counted)");
eq("artifactCountText (toggle on): the same count says hidden; an unscored count is added", A.artifactCountText({ artifact_events_total: 412, artifact_unscored_events: 30 }, true), "412 flagged insect events hidden · 30 older insect events were not scored");
eq("artifactCountText: a server without the field (older ui-http) gives no text", A.artifactCountText({}, true), "");
const AG_ON = { enabled: true, available: true, floor_c: 5, temp_max_age_s: 10800, burst_window_s: 30, burst_min_transients: 20, ambient: { records: 168, newest_age_s: 1800 }, transients: { in_memory: 52000 }, events_scored_24h: 6000, events_flagged_24h: 5000 };
eq("artifactGateNote: the System-panel note states the rule, both feeds and the 24 h tally, and that it is a heuristic",
  A.artifactGateNote(AG_ON), "Artifact flags (advisory): an insect detection is flagged as a possible artifact when the ambient air temperature was below 5 °C, or when at least 20 short broadband transients fell within ±30 s. Temperature: 168 hourly temperature records, the newest 30 min old (a record older than 3.0 h is not used). Bursts: 52000 noise transients read. Last 24 h: 5000 of 6000 insect and mechanical events flagged. This is a heuristic, not ground truth: a flagged event is kept, counted and shown (the species-list toggle is off by default), and an unflagged one can still be an artifact.");
eq("artifactGateNote: no temperature records is said plainly (cold check not applied)", A.artifactGateNote(Object.assign({}, AG_ON, { ambient: { records: 0, newest_age_s: null } })).includes("no temperature records: every insect reads \"temperature unknown\" and the cold check is not applied"), true);
eq("artifactGateNote: no transients is said plainly", A.artifactGateNote(Object.assign({}, AG_ON, { transients: { in_memory: 0 } })).includes("no noise transients read, so there is no burst evidence"), true);
eq("artifactGateNote: disabled by flag", A.artifactGateNote({ enabled: false, available: true }), "Artifact flags are off (the server runs with --no-artifact-gate): no detection is flagged, which says nothing about whether any is an artifact.");
eq("artifactGateNote: module missing", A.artifactGateNote({ enabled: false, available: false }).includes("artifact_gate.py is not installed"), true);
eq("artifactGateNote: an older server (no block) gives no note", A.artifactGateNote(undefined), "");

eq("the artifact toggle is OFF by default (nothing is hidden until the user asks)", A.S.det.hideArtifact, false);
eq("the tip floor defaults to 5 and follows status.artifact_gate.floor_c", [A.setArtifactFloor(undefined), A.setArtifactFloor({ floor_c: 7 }), A.setArtifactFloor({ floor_c: "x" }), A.setArtifactFloor({ floor_c: NaN })], [5, 7, 7, 7]);

console.log(`\n${n} checks, ${fails} failure(s)`);
process.exit(fails ? 1 : 0);
