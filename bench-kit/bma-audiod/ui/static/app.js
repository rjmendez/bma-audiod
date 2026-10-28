"use strict";
/* BMA dashboard. Vanilla JS, no external fetches. All data reaches the DOM through textContent / setAttribute only. */

// ------------------------------------------------------------------------------------------------ pure helpers
const BEAM_COLORS = ["#E69F00", "#56B4E9", "#009E73", "#F0E442", "#0072B2", "#D55E00", "#CC79A7", "#9AA7B4"]; // Okabe-Ito
const BAND_NAMES = ["50-125 Hz", "125-250 Hz", "250-500 Hz", "500 Hz-1 kHz", "1-2 kHz", "2-4 kHz", "4-8 kHz", "8-16 kHz"];
const SEQ = [[68, 1, 84], [59, 82, 139], [33, 144, 141], [93, 201, 99], [253, 231, 37]]; // viridis stops (colour-blind safe)

function niceTicks(min, max, n) {
  if (!isFinite(min) || !isFinite(max)) return [];
  if (max === min) { max = min + 1; }
  const span = max - min, raw = span / Math.max(1, n), mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw) || 10 * mag;
  const out = [];
  for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-9; v += step) out.push(Math.round(v / step) * step);
  return out;
}
function seqColor(f) {
  f = Math.min(1, Math.max(0, f));
  const x = f * (SEQ.length - 1), i = Math.min(SEQ.length - 2, Math.floor(x)), u = x - i;
  return "rgb(" + [0, 1, 2].map(c => Math.round(SEQ[i][c] + (SEQ[i + 1][c] - SEQ[i][c]) * u)).join(",") + ")";
}
function polar(cx, cy, r, azDeg) {
  const t = azDeg * Math.PI / 180;
  return [cx + r * Math.sin(t), cy - r * Math.cos(t)];
}
function wedgePath(cx, cy, r, azDeg, halfDeg) {
  const a = polar(cx, cy, r, azDeg - halfDeg), b = polar(cx, cy, r, azDeg + halfDeg);
  return "M" + cx + " " + cy + "L" + a[0].toFixed(1) + " " + a[1].toFixed(1) + "A" + r.toFixed(1) + " " + r.toFixed(1) +
    " 0 0 1 " + b[0].toFixed(1) + " " + b[1].toFixed(1) + "Z";
}
function fmtAge(s) {
  if (s === null || s === undefined) return "n/a";
  if (s < 90) return Math.round(s) + " s";
  if (s < 5400) return Math.round(s / 60) + " min";
  if (s < 172800) return (s / 3600).toFixed(1) + " h";
  return (s / 86400).toFixed(1) + " d";
}
function fmtNum(v, d) { if (v === null || v === undefined) return "n/a"; const t = Number(v).toFixed(d === undefined ? 1 : d); return /^-0(\.0+)?$/.test(t) ? t.slice(1) : t; }
function fmtTime(t, withDate) {
  const d = new Date(t * 1000), p = n => String(n).padStart(2, "0");
  const hm = p(d.getHours()) + ":" + p(d.getMinutes());
  return withDate ? (d.getMonth() + 1) + "/" + p(d.getDate()) + " " + hm : hm;
}
function percentile(sorted, q) { return sorted.length ? sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))] : null; }
function hourOfDayCounts(hourly) { // hourly: [{hour_s, events}] -> 24 counts by LOCAL hour
  const out = new Array(24).fill(0);
  for (const h of hourly) out[new Date(h.hour_s * 1000).getHours()] += h.events;
  return out;
}
/* nice axis range that COVERS [min,max] on both sides: ticks at multiples of one step, lo/hi are ticks. */
function niceRange(min, max, n) {
  if (!isFinite(min) || !isFinite(max)) return null;
  if (max === min) max = min + 1;
  const t = niceTicks(min, max, n), step = t.length > 1 ? t[1] - t[0] : (max - min);
  const lo = Math.floor(min / step + 1e-9) * step, hi = Math.ceil(max / step - 1e-9) * step, ticks = [];
  for (let v = lo; v <= hi + step * 1e-9; v += step) ticks.push(parseFloat((Math.round(v / step) * step).toPrecision(12)));
  return { lo: parseFloat(lo.toPrecision(12)), hi: parseFloat(hi.toPrecision(12)), ticks: ticks };
}
/* tick positions (epoch s) on LOCAL clock boundaries: 1/5/10/15/30 min, 1/2/3/6/12 h, or whole days. */
const TICK_STEPS = [60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200];
function timeTickValues(t0, t1, maxTicks) {
  if (!isFinite(t0) || !isFinite(t1) || t1 <= t0) return [];
  const span = t1 - t0, step = TICK_STEPS.find(x => span / x <= maxTicks), d0 = new Date(t0 * 1000), out = [];
  if (step) {
    for (let day = 0; day < 400; day++) for (let k = 0; k < 86400 / step; k++) {
      const t = new Date(d0.getFullYear(), d0.getMonth(), d0.getDate() + day, 0, k * step / 60).getTime() / 1000;
      if (t > t1) return out;
      if (t >= t0) out.push(t);
    }
    return out;
  }
  const days = Math.max(1, Math.ceil(span / 86400 / maxTicks));
  for (let day = 0; day < 4000; day += days) {
    const t = new Date(d0.getFullYear(), d0.getMonth(), d0.getDate() + day).getTime() / 1000;
    if (t > t1) break;
    if (t >= t0) out.push(t);
  }
  return out;
}
/* colour-scale limits that do not jitter between refreshes: keep prev unless the new limits moved by more than tol. */
function stableScale(prev, lo, hi, tol) {
  if (prev && Math.abs(prev.lo - lo) <= tol && Math.abs(prev.hi - hi) <= tol) return prev;
  return { lo: lo, hi: hi };
}
/* beam-slot mapping flag: data before fixAt (epoch s, from the server) used the wrong slot mapping (angles off by 45 deg). */
function shadeBefore(t0, t1, fixAt) {                                     // fraction (0..1) of [t0,t1] that lies before fixAt
  if (typeof fixAt !== "number" || !isFinite(fixAt) || !(t1 > t0)) return 0;
  return Math.min(1, Math.max(0, (fixAt - t0) / (t1 - t0)));
}
function shadeBars(hourStarts, fixAt) {                                   // leading hourly bars that start before fixAt (the hour holding fixAt is partly unreliable and counts)
  if (typeof fixAt !== "number" || !isFinite(fixAt)) return 0;
  let k = 0; while (k < hourStarts.length && hourStarts[k] < fixAt) k++;
  return k;
}
function shadeCols(t0, bucketS, n, fixAt) {                               // leading heatmap columns that start before fixAt
  if (typeof fixAt !== "number" || !isFinite(fixAt)) return 0;
  return Math.min(n, Math.max(0, Math.ceil((fixAt - t0) / bucketS)));
}
function slotNote(fixAt) {
  const u = new Date(fixAt * 1000), p = n => String(n).padStart(2, "0");
  return "Before " + fmtTime(fixAt, true) + " local (" + p(u.getUTCHours()) + ":" + p(u.getUTCMinutes()) + " UTC on " + (u.getUTCMonth() + 1) + "/" + p(u.getUTCDate()) + ") the beam slots were mapped 45\u00b0 off: beam numbers and azimuths in the hatched region are unreliable.";
}
/* physical directions on the upright panel, seen from the front (calibrated panel frame: 0 = up, clockwise). null when the frame is not the panel frame. */
function physicalDirs(frameKind) {
  return frameKind === "panel" ? [{ label: "UP", az: 0 }, { label: "RIGHT", az: 90 }, { label: "DOWN", az: 180 }, { label: "LEFT", az: 270 }] : null;
}
function dirNear(az, frameKind) {                                         // the direction label within 22.5 deg of az, else null
  const ds = physicalDirs(frameKind); if (!ds) return null;
  for (const d of ds) { const diff = Math.abs(((az - d.az + 540) % 360) - 180); if (diff <= 22.5) return d.label; }
  return null;
}
const SPECIES_COLORS = ["#E69F00", "#56B4E9", "#009E73", "#F0E442", "#0072B2", "#D55E00", "#CC79A7"], SPECIES_OTHER = "#9AA7B4", SHAPES = ["circle", "square", "triangle", "diamond"];
/* taxonomic groups (the server derives `group` from the label, see ui-http "Taxonomic groups"): colour AND marker shape AND a name, never colour only */
const GROUP_KEYS = ["bird", "insect", "amphibian", "mammal", "other-bio", "mechanical"], GROUPS_BIO = ["bird", "insect", "amphibian", "mammal", "other-bio"];
const GROUP_NAMES = { bird: "Bird", insect: "Insect", amphibian: "Amphibian", mammal: "Mammal", "other-bio": "Other biological", mechanical: "Non-biological", bio: "All biological", any: "Everything" };
const GROUP_COLORS = { bird: "#0072B2", insect: "#E69F00", amphibian: "#009E73", mammal: "#CC79A7", "other-bio": "#56B4E9", mechanical: "#9AA7B4" };
const GROUP_SHAPES = { bird: "circle", insect: "diamond", amphibian: "triangle", mammal: "square", "other-bio": "cross", mechanical: "bar" };
/* advisory artifact flags (contract artifact-gate, ui-http 1.11.0): a verdict is shown, never acted on; the event stays listed and counted */
const ARTIFACT_LEVELS = ["likely", "suspect"];
function artifactFlagged(af) { return !!af && typeof af === "object" && ARTIFACT_LEVELS.includes(af.level); }
function artifactText(af) {
  if (!af || typeof af !== "object" || !Array.isArray(af.flags)) return "";
  const ev = af.evidence || {}, out = [];
  if (af.flags.includes("cold_insect")) out.push("Cold: the ambient temperature was " + fmtNum(ev.temp_c, 1) + " °C (" + (ev.temp_age_s ? "from " + fmtAge(ev.temp_age_s) + " earlier" : "this hour") + "), below the " + fmtNum(ev.floor_c, 0) + " °C floor. Insects are not expected to call that cold, so this label may be a noise the model matched to its nearest label.");
  if (af.flags.includes("burst_coincident")) out.push("Burst: " + ev.burst_n + " short broadband transients within " + fmtNum(ev.window_s, 0) + " s, median beam spread " + fmtNum(ev.burst_spread_db, 1) + " dB: the pattern of a diffuse noise burst, not of a single source.");
  if (af.flags.includes("temp_unknown")) out.push("No usable temperature (no ambient record within 3 hours), so the cold check could not be applied.");
  if (af.flags.includes("mechanical_label")) out.push("A mechanical class (engine, tool, siren ...), not a species.");
  return out.join(" ") + (out.length ? " " : "") + "Advisory only: the detection is kept and counted.";
}
let ARTIFACT_FLOOR_C = 5;                                                   // the shown floor in the tips; replaced by status.artifact_gate.floor_c whenever the server reports one
function setArtifactFloor(ag) { if (ag && typeof ag.floor_c === "number" && isFinite(ag.floor_c)) ARTIFACT_FLOOR_C = ag.floor_c; return ARTIFACT_FLOOR_C; }
function artifactCountText(sp, hidden) {                                    // the line next to the species-list toggle: the flagged events are always counted, hidden or not
  if (!sp || typeof sp.artifact_events_total !== "number") return "";
  const n = sp.artifact_events_total, un = sp.artifact_unscored_events || 0;
  return (hidden ? n + " flagged insect events hidden" : n + " insect events flagged as possible artifacts (still counted)") + (un ? " · " + un + " older insect events were not scored" : "");
}
function artifactGateNote(ag) {                                            // the honest System-panel note from status.artifact_gate
  if (!ag || typeof ag !== "object") return "";
  if (!ag.enabled) return "Artifact flags are off (" + (ag.available ? "the server runs with --no-artifact-gate" : "artifact_gate.py is not installed next to the dashboard") + "): no detection is flagged, which says nothing about whether any is an artifact.";
  const am = ag.ambient || {}, tr = ag.transients || {};
  const temp = am.records > 0 ? am.records + " hourly temperature records, the newest " + (am.newest_age_s === null || am.newest_age_s === undefined ? "n/a" : fmtAge(am.newest_age_s)) + " old (a record older than " + fmtAge(ag.temp_max_age_s) + " is not used)"
    : "no temperature records: every insect reads \"temperature unknown\" and the cold check is not applied";
  const burst = tr.in_memory > 0 ? tr.in_memory + " noise transients read" : "no noise transients read, so there is no burst evidence";
  return "Artifact flags (advisory): an insect detection is flagged as a possible artifact when the ambient air temperature was below " + fmtNum(ag.floor_c, 0) + " °C, or when at least " + ag.burst_min_transients + " short broadband transients fell within ±" + fmtNum(ag.burst_window_s, 0) + " s. Temperature: " + temp + ". Bursts: " + burst + ". Last 24 h: "
    + ag.events_flagged_24h + " of " + ag.events_scored_24h + " insect and mechanical events flagged. This is a heuristic, not ground truth: a flagged event is kept, counted and shown (the species-list toggle is off by default), and an unflagged one can still be an artifact.";
}
function groupName(g) { return GROUP_NAMES[g] || "Other biological"; }
function groupRank(g) { const i = GROUP_KEYS.indexOf(g); return i < 0 ? GROUP_KEYS.indexOf("other-bio") : i; }
/* the top k species of EVERY group (list is in the server's order: events descending), groups in GROUP_KEYS order: a continuous caller cannot crowd out the rest */
function topPerGroup(list, k) {
  const out = [], seen = Object.create(null);
  for (const g of GROUP_KEYS) for (const s of list) if (groupRank(s.group) === GROUP_KEYS.indexOf(g)) { seen[g] = (seen[g] || 0) + 1; if (seen[g] <= k) out.push(s); }
  return out;
}
/* newest-first events -> runs: events of the SAME species whose gap (older event end to the run's earliest start) is at most gapS seconds are one run */
function collapseRuns(events, gapS, truncated) {
  const runs = [], open = Object.create(null);
  for (const e of events) {
    const key = e.scientific || e.species, r = open[key];
    if (r && r.t_first - e.t_end_s <= gapS) {
      r.n++; r.t_first = Math.min(r.t_first, e.t_start_s); r.reports += e.reports;
      for (const b of e.beams) if (!r.beams.includes(b)) r.beams.push(b);
      if (e.max_confidence > r.max_confidence) { r.max_confidence = e.max_confidence; r.best_beam = e.best_beam; r.best_beam_azimuth_deg = e.best_beam_azimuth_deg; }
    } else {
      const n = { species: e.species, scientific: e.scientific, group: e.group, n: 1, t_first: e.t_start_s, t_last: e.t_start_s, beams: e.beams.slice(), reports: e.reports, max_confidence: e.max_confidence,
        best_beam: e.best_beam, best_beam_azimuth_deg: e.best_beam_azimuth_deg, in_region: e.in_region, non_species: e.non_species };
      runs.push(n); open[key] = n;
    }
  }
  const oldest = events.length ? Math.min(...events.map(e => e.t_start_s)) : 0;
  for (const r of runs) { r.beams.sort((x, y) => x - y); r.open_ended = !!truncated && r.t_first - oldest <= gapS; }   // truncated list: a run that reaches its oldest event may continue in the events not fetched
  return runs;
}
function runText(r) { return r.species + (r.n > 1 || r.open_ended ? " \u00d7" + r.n : "") + (r.open_ended ? "+" : ""); }
/* x-axis range: the extent of the data with a margin (5 % of the span, at least 60 s; a single instant gets +-15 min), clamped to the requested range; full = the requested range */
function zoomRange(first, last, since, until, full) {
  if (full || typeof first !== "number" || typeof last !== "number") return [since, until];
  const span = last - first, pad = span === 0 ? 900 : Math.max(60, span * 0.05);
  const a = Math.max(since, first - pad), b = Math.min(until, last + pad);
  return b > a ? [a, b] : [since, until];
}
function densityOpacity(count, max) { return max > 0 ? Math.round((0.2 + 0.8 * Math.log1p(count) / Math.log1p(max)) * 1000) / 1000 : 0; }
/* species list (with .group and .hourly_counts) -> [{group, values[per hour start], total}] for the groups that have events */
function groupHourly(species, hourStarts) {
  const idx = new Map(hourStarts.map((h, i) => [h, i])), acc = Object.create(null);
  for (const s of species) {
    const g = GROUP_KEYS.includes(s.group) ? s.group : "other-bio", row = acc[g] || (acc[g] = { group: g, values: new Array(hourStarts.length).fill(0), total: 0 });
    for (const h of s.hourly_counts) if (idx.has(h.hour_s)) { row.values[idx.get(h.hour_s)] += h.events; row.total += h.events; }
  }
  return GROUP_KEYS.filter(g => acc[g] && acc[g].total > 0).map(g => acc[g]);
}
/* several groups: colour + shape by GROUP (legend = groups); one group (a group chip selected): colour + shape by species, the top 7 by count, the rest shared */
function speciesStyles(evs) {
  const grp = Object.create(null); for (const e of evs) grp[e.group || ""] = (grp[e.group || ""] || 0) + 1;
  const gkeys = Object.keys(grp);
  if (gkeys.length > 1) {
    const by = Object.create(null), legend = [];
    for (const e of evs) { const g = e.group || "other-bio"; by[e.species] = { color: GROUP_COLORS[g] || SPECIES_OTHER, shape: GROUP_SHAPES[g] || "circle", group: g }; }
    for (const g of GROUP_KEYS.filter(k => gkeys.includes(k)).concat(gkeys.filter(k => !GROUP_KEYS.includes(k)))) legend.push({ label: groupName(g), count: grp[g], color: GROUP_COLORS[g] || SPECIES_OTHER, shape: GROUP_SHAPES[g] || "circle" });
    return { by: by, legend: legend, mode: "group" };
  }
  const cnt = Object.create(null); for (const e of evs) cnt[e.species] = (cnt[e.species] || 0) + 1;
  const names = Object.keys(cnt).sort((a, b) => cnt[b] - cnt[a] || (a < b ? -1 : a > b ? 1 : 0)), by = Object.create(null), legend = [];
  names.forEach((nm, i) => {
    if (i < SPECIES_COLORS.length) { by[nm] = { color: SPECIES_COLORS[i], shape: SHAPES[i % SHAPES.length] }; legend.push({ label: nm, count: cnt[nm], color: SPECIES_COLORS[i], shape: SHAPES[i % SHAPES.length] }); }
    else by[nm] = { color: SPECIES_OTHER, shape: "circle" };
  });
  const rest = names.slice(SPECIES_COLORS.length);
  if (rest.length) legend.push({ label: "other species (" + rest.length + ")", count: rest.reduce((a, nm) => a + cnt[nm], 0), color: SPECIES_OTHER, shape: "circle" });
  return { by: by, legend: legend, mode: "species" };
}
/* sector names of an azimuth in the PANEL frame (0 = up, clockwise); a tie between a cardinal and a diagonal goes to the cardinal (the same rule as dirNear); null in any other frame */
const AZ_NAMES = ["up", "up-right", "right", "down-right", "down", "down-left", "left", "up-left"];
function azName(az, frameKind) {
  if (frameKind !== "panel" || typeof az !== "number" || !isFinite(az)) return null;
  const r = (((az % 360) + 360) % 360) / 45, lo = Math.floor(r), f = r - lo;
  const i = f < 0.5 - 1e-9 ? lo : f > 0.5 + 1e-9 ? lo + 1 : (lo % 2 === 0 ? lo : lo + 1);
  return AZ_NAMES[i % 8];
}
/* the array cannot steer below 250 Hz: bands 0 and 1 are identical on every beam */
const NOT_STEERABLE_BANDS = [0, 1];
const STEER_NOTE = "Not steerable: 50-250 Hz is identical on all beams (the array cannot steer below 250 Hz), so differences between beams in this band are channel gain or noise, not direction.";
function isSteerable(band) { return !NOT_STEERABLE_BANDS.includes(band); }
/* what the UI can honestly say about the applied beam pattern. board = status.board, cal = /api/calibration or the live payload. Sources are named; nothing is inferred. */
function patternInfo(bd, cal) {
  let value, sub;
  if (bd && bd.pattern_ref) { value = String(bd.pattern_ref); sub = "reported by dspd (board state)"; }
  else if (bd && bd.beams_applied_by_dspd === true) { value = "applied by dspd"; sub = "reported by dspd, which gives no pattern name"; }
  else if (bd && bd.beams_applied_by_dspd === false) { value = "not applied"; sub = "dspd reports no applied pattern"; }
  else { value = "not readable"; sub = "the board state could not be read, so the applied pattern is unknown"; }
  let dirs;
  if (cal && cal.calibrated && Array.isArray(cal.beam_azimuth_deg)) {
    const kind = cal.frame_kind || "panel", names = cal.beam_azimuth_deg.map((a, i) => "B" + (i + 1) + " " + a + "\u00b0" + (azName(a, kind) ? " " + azName(a, kind) : ""));
    dirs = "Beam directions from the stored calibration profile (" + frameShort(kind) + (kind === "panel" ? ", upright array" : "") + (cal.provisional ? ", provisional" : "") + (cal.measured_at ? ", measured " + cal.measured_at : "") + "), metadata only, not read back from the DSPs: " + names.join(", ");
  } else dirs = "Beam directions: no calibration profile stored, so no physical direction is known (the DSP-frame fallback angles are not a pattern).";
  return { value: value, sub: sub, dirs: dirs };
}
/* ---- direction strip, event detail, hour x direction multiples, health sparklines (pure helpers, node-tested) */
const DIR_SPAN_DB = 6, MIN_DIR_N = 5, NEIGHBOUR_S = 60, SPARK_GAP_S = 90, NOT_STEERABLE_STAMP = "not steerable: 50-250 Hz";
/* matrix[beam][col] (dB or null) -> each cell minus the ring mean of its column (power mean of the beams that have data); null stays null, an empty column is null throughout */
function ringRelative(matrix) {
  const cols = matrix.length ? matrix[0].length : 0, out = matrix.map(() => new Array(cols).fill(null));
  for (let c = 0; c < cols; c++) {
    const vs = matrix.map(r => r[c]).filter(v => v !== null && v !== undefined);
    if (!vs.length) continue;
    const ring = 10 * Math.log10(vs.reduce((a, v) => a + Math.pow(10, v / 10), 0) / vs.length);
    matrix.forEach((r, b) => { if (r[c] !== null && r[c] !== undefined) out[b][c] = Math.round((r[c] - ring) * 100) / 100; });
  }
  return out;
}
function beamDirLabel(beam, az, kind) {                                   // "B3 345° up"; the name only in the panel frame
  if (az === null || az === undefined) return "B" + beam;
  const nm = azName(az, kind);
  return "B" + beam + " " + az + "°" + (nm ? " " + nm : "");
}
/* reports [{t_s, beam, beam_azimuth_deg, confidence, level_dbfs}] -> one entry per beam (beam order): its points in time order and its maxima */
function traceByBeam(reports) {
  const by = new Map();
  for (const r of reports) {
    const b = by.get(r.beam) || { beam: r.beam, azimuth: r.beam_azimuth_deg, n: 0, max_conf: 0, max_level: null, points: [] };
    b.n++; b.max_conf = Math.max(b.max_conf, r.confidence);
    if (r.level_dbfs !== null && r.level_dbfs !== undefined && (b.max_level === null || r.level_dbfs > b.max_level)) b.max_level = r.level_dbfs;
    b.points.push({ t: r.t_s, conf: r.confidence, level: r.level_dbfs }); by.set(r.beam, b);
  }
  for (const b of by.values()) b.points.sort((x, y) => x.t - y.t);
  return Array.from(by.values()).sort((x, y) => x.beam - y.beam);
}
/* events within +-NEIGHBOUR_S of an event, one row per species (a continuous caller is one row with a count, not a wall of rows); the event itself is left out.
   dt = seconds from the event's span (negative = before it, 0 = overlapping it); rows ordered by the nearest |dt|, then more events, then name. */
function neighbourSummary(events, self) {
  const by = new Map();
  for (const e of events) {
    if (e.id === self.id) continue;
    const dt = e.t_start_s < self.t_start_s ? e.t_start_s - self.t_start_s : e.t_start_s > self.t_end_s ? e.t_start_s - self.t_end_s : 0, key = e.scientific || e.species;
    const r = by.get(key) || { species: e.species, scientific: e.scientific, group: e.group, events: 0, nearest_dt: dt, max_confidence: 0, beams: [] };
    r.events++; r.max_confidence = Math.max(r.max_confidence, e.max_confidence);
    if (Math.abs(dt) < Math.abs(r.nearest_dt)) r.nearest_dt = dt;
    for (const b of e.beams) if (!r.beams.includes(b)) r.beams.push(b);
    by.set(key, r);
  }
  const rows = Array.from(by.values()); for (const r of rows) r.beams.sort((x, y) => x - y);
  rows.sort((a, b) => Math.abs(a.nearest_dt) - Math.abs(b.nearest_dt) || b.events - a.events || (a.species < b.species ? -1 : a.species > b.species ? 1 : 0));
  return { rows: rows, events: rows.reduce((a, r) => a + r.events, 0) };
}
/* density rows -> counts[24 local hours][8 beams] of one species (best beam), the hours of several days adding up */
function hourDirMatrix(rows, key) {
  const m = Array.from({ length: 24 }, () => new Array(8).fill(0));
  for (const r of rows) if ((r.scientific || r.species) === key) m[new Date(r.t_s * 1000).getHours()][r.best_beam - 1] += r.events;
  return m;
}
/* per hour: total, the direction SHARE of that hour (so a continuous caller's loud hours and a rare caller's quiet hours read on the same 0..1 scale) and whether the hour has enough events to read a direction from */
function dirHourModel(mat, minN) {
  const hours = mat.map((row, h) => { const total = row.reduce((a, v) => a + v, 0); return { hour: h, total: total, readable: total >= minN, shares: row.map(v => total ? v / total : 0), counts: row }; });
  return { hours: hours, max_total: Math.max(0, ...hours.map(x => x.total)), peak_hour: hours.reduce((bi, x, i) => x.total > hours[bi].total ? i : bi, 0) };
}
/* points [[t, v|null]] -> svg path of a w x h sparkline (null breaks the line); y spans lo..hi (default the data range, a flat series is centred) */
function sparkGeom(points, w, h, o) {
  o = o || {};
  const vs = points.map(p => p[1]).filter(v => v !== null && v !== undefined), ts = points.map(p => p[0]);
  if (!vs.length) return { d: "", min: null, max: null, last: null, n: 0 };
  let lo = o.lo !== undefined ? o.lo : Math.min(...vs), hi = o.hi !== undefined ? o.hi : Math.max(...vs);
  if (hi === lo) { hi = lo + 1; if (o.lo === undefined) lo -= 1; }
  const t0 = o.t0 !== undefined ? o.t0 : Math.min(...ts), t1 = o.t1 !== undefined ? o.t1 : Math.max(...ts), pad = 2;
  let d = "", pen = false;
  for (const p of points) {
    if (p[1] === null || p[1] === undefined) { pen = false; continue; }
    const x = t1 > t0 ? (p[0] - t0) / (t1 - t0) * w : w / 2, y = pad + (h - 2 * pad) * (1 - (p[1] - lo) / (hi - lo));
    d += (pen ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1); pen = true;
  }
  return { d: d, min: Math.min(...vs), max: Math.max(...vs), last: vs[vs.length - 1], n: vs.length };
}
/* stream samples [{t_s, state}] -> runs over [t0, t1]: a sample holds until the next one (at most SPARK_GAP_S), anything longer is "no data"; seconds per state and the totals */
function stateSummary(samples, t0, t1) {
  const runs = [], by = {}, add = (state, a, b) => {
    if (b <= a) return;
    const last = runs[runs.length - 1];
    if (last && last.state === state && last.t1 === a) last.t1 = b; else runs.push({ state: state, t0: a, t1: b });
    by[state] = (by[state] || 0) + (b - a);
  };
  const s = samples.filter(x => x.t_s < t1).slice().sort((a, b) => a.t_s - b.t_s);
  let cur = t0;
  s.forEach((x, i) => {
    const end = Math.min(t1, i + 1 < s.length ? Math.min(s[i + 1].t_s, x.t_s + SPARK_GAP_S) : x.t_s + SPARK_GAP_S), a = Math.max(x.t_s, t0);
    if (end <= t0) return;
    add("no data", cur, a); add(String(x.state), a, end); cur = Math.max(cur, end);
  });
  add("no data", cur, t1);
  const total = Math.max(0, t1 - t0), ok = by.ok || 0, nodata = by["no data"] || 0;
  return { runs: runs, seconds: by, total_s: total, ok_s: ok, no_data_s: nodata, other_s: total - ok - nodata };
}
function dotRadius(conf) { return 3 + 6 * Math.min(1, Math.max(0, Number(conf) || 0)); }
function scatterLanes(az) {                                               // beam numbers (1-based) ordered by azimuth, smallest first
  return az.map((a, i) => [a, i + 1]).sort((x, y) => x[0] - y[0] || x[1] - y[1]).map(x => x[1]);
}
function shapeAttrs(shape, cx, cy, r) {                                   // [svg tag, attributes] of one marker
  const f = v => v.toFixed(1);
  if (shape === "square") return ["rect", { x: f(cx - r), y: f(cy - r), width: f(2 * r), height: f(2 * r) }];
  if (shape === "triangle") return ["polygon", { points: f(cx) + "," + f(cy - r) + " " + f(cx + r) + "," + f(cy + r) + " " + f(cx - r) + "," + f(cy + r) }];
  if (shape === "cross") { const t = r * 0.35; return ["polygon", { points: [[-t, -r], [t, -r], [t, -t], [r, -t], [r, t], [t, t], [t, r], [-t, r], [-t, t], [-r, t], [-r, -t], [-t, -t]].map(q => f(cx + q[0]) + "," + f(cy + q[1])).join(" ") }]; }
  if (shape === "bar") return ["rect", { x: f(cx - r), y: f(cy - r * 0.4), width: f(2 * r), height: f(0.8 * r) }];
  if (shape === "diamond") return ["polygon", { points: f(cx) + "," + f(cy - r) + " " + f(cx + r) + "," + f(cy) + " " + f(cx) + "," + f(cy + r) + " " + f(cx - r) + "," + f(cy) }];
  return ["circle", { cx: f(cx), cy: f(cy), r: f(r) }];
}
function boardErrText(code) {
  if (code === "board_disabled") return "Board polling is disabled.";
  if (code === "token_unavailable") return "The board token file could not be read.";
  if (code === "unreachable") return "The board did not answer.";
  if (code === "not_polled") return "The board has not been polled yet.";
  return code ? "The board answered with an error." : "The board is not available.";
}

// ------------------------------------------------------------------------------------------------ DOM helpers
function el(tag, props, ...kids) {
  const e = document.createElement(tag);
  if (props) for (const k of Object.keys(props)) {
    if (k === "class") e.className = props[k];
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), props[k]);
    else if (props[k] !== null && props[k] !== undefined) e.setAttribute(k, props[k]);
  }
  for (const c of kids.flat(Infinity)) if (c !== null && c !== undefined && c !== false) e.append(c.nodeType ? c : document.createTextNode(String(c)));
  return e;
}
const SVGNS = "http://www.w3.org/2000/svg";
function sv(tag, props, ...kids) {
  const e = document.createElementNS(SVGNS, tag);
  if (props) for (const k of Object.keys(props)) if (props[k] !== null && props[k] !== undefined) e.setAttribute(k, props[k]);
  for (const c of kids.flat(Infinity)) if (c !== null && c !== undefined) e.append(c.nodeType ? c : document.createTextNode(String(c)));
  return e;
}
function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
function setChild(node, ...kids) { node.replaceChildren(...kids.flat(Infinity).filter(k => k)); }   // one swap: no collapsed intermediate state
function emptyNote(text, bad) { return el("div", { class: "empty" + (bad ? " err" : "") }, text); }
/* explanatory prose lives behind an info button (tip.js), never on the page: tnote = a small button row, ptitle = a panel title with its button */
function tnote(text, label) { return el("div", { class: "tipnote" }, Tip.tip(text, { label: label || "More information" })); }
function ptitle(title, text) { return el("h2", null, title, text ? Tip.tip(text, { label: "About: " + title }) : null); }
const HELP_TEXT = "Read-only. Detections are model opinions, not confirmed identifications. Angles are in the calibrated panel frame (0 = panel up, clockwise seen from the front, provisional) when a calibration is stored, otherwise a DSP-frame fallback with no known physical direction. Times are shown in your local time zone; hourly rollups are in UTC hours.";
function width(node) { return Math.max(260, Math.floor(node.clientWidth || node.parentNode.clientWidth || 600)); }

async function api(path) {
  try {
    const r = await fetch(path, { headers: { Accept: "application/json" }, cache: "no-store", credentials: "same-origin" });
    if (r.status === 401) return { ok: false, error: "Signed out: reopen the link with ?token=..." };
    const body = await r.json();
    if (!r.ok) return { ok: false, error: (body.title || "error") + ": " + (body.detail || r.status) };
    return { ok: true, data: body };
  } catch (e) { return { ok: false, error: "request failed (" + (e && e.name || "error") + ")" }; }
}

// ------------------------------------------------------------------------------------------------ charts
function axes(svg, W, H, m, xt, yt, xlabel, ylabel, xfmt, yfmt) {
  const pw = W - m.l - m.r, ph = H - m.t - m.b;
  for (const t of yt.ticks) {
    const y = m.t + ph - (t - yt.min) / (yt.max - yt.min) * ph;
    svg.append(sv("line", { class: "grid", x1: m.l, x2: m.l + pw, y1: y, y2: y }));
    svg.append(sv("text", { x: m.l - 5, y: y + 4, "text-anchor": "end" }, yfmt(t)));
  }
  for (const t of xt.ticks) {
    const x = m.l + (t - xt.min) / (xt.max - xt.min) * pw;
    svg.append(sv("line", { class: "grid", x1: x, x2: x, y1: m.t, y2: m.t + ph }));
    svg.append(sv("text", { x: x, y: m.t + ph + 14, "text-anchor": "middle" }, xfmt(t)));
  }
  svg.append(sv("line", { class: "axis", x1: m.l, x2: m.l + pw, y1: m.t + ph, y2: m.t + ph }));
  svg.append(sv("line", { class: "axis", x1: m.l, x2: m.l, y1: m.t, y2: m.t + ph }));
  svg.append(sv("text", { x: m.l + pw / 2, y: H - 4, "text-anchor": "middle" }, xlabel));
  svg.append(sv("text", { x: 12, y: m.t + ph / 2, "text-anchor": "middle", transform: "rotate(-90 12 " + (m.t + ph / 2) + ")" }, ylabel));
}
function timeTicks(t0, t1, n) { return timeTickValues(t0, t1, n); }
function timeFmt(t0, t1) { const long = (t1 - t0) > 36 * 3600; return t => fmtTime(t, long); }

let HATCH_N = 0;
function hatchDefs(svg) {                                                 // diagonal hatch (not colour only); returns the fill url
  const id = "hatch" + (++HATCH_N);
  svg.append(sv("defs", null, sv("pattern", { id: id, width: 7, height: 7, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" }, sv("line", { class: "hatch-line", x1: 0, y1: 0, x2: 0, y2: 7 }))));
  return "url(#" + id + ")";
}
function warnNote(text, prefix) { return text ? el("div", { class: "note-warn" }, Tip.badge(prefix === undefined ? "Unreliable region" : prefix.replace(/:\s*$/, ""), "warn", text)) : null; }

/* series: [{name,color,points:[[x_s,y|null]],dash}] */
function lineChart(host, series, o) {
  const W = width(host), H = o.height || 190, m = { l: 48, r: 10, t: 8, b: 38 };
  const xs = series.flatMap(s => s.points.map(p => p[0])), ys = series.flatMap(s => s.points.map(p => p[1]).filter(v => v !== null));
  if (!xs.length || !ys.length) { setChild(host, emptyNote(o.empty || "No data in this range.")); return; }
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = o.ymin !== undefined ? o.ymin : Math.min(...ys), y1 = o.ymax !== undefined ? o.ymax : Math.max(...ys);
  if (o.minSpan && y1 - y0 < o.minSpan) { const c = (y0 + y1) / 2; y0 = c - o.minSpan / 2; y1 = c + o.minSpan / 2; }
  if (y1 === y0) { y1 += 1; y0 -= o.ymin === undefined ? 1 : 0; }
  const nr = niceRange(y0, y1, 4), yt = nr.ticks; y0 = nr.lo; y1 = nr.hi;
  const xt = { min: x0, max: x1 === x0 ? x0 + 1 : x1 }; xt.ticks = timeTicks(xt.min, xt.max, Math.max(2, Math.floor(W / 110)));
  const svg = sv("svg", { width: W, height: H, viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": o.title || o.ylabel });
  axes(svg, W, H, m, xt, { min: y0, max: y1, ticks: yt }, o.xlabel || "time (local)", o.ylabel, timeFmt(xt.min, xt.max), o.yfmt || (v => String(v)));
  const pw = W - m.l - m.r, ph = H - m.t - m.b;
  for (const s of series) {
    let d = "", pen = false;
    for (const p of s.points) {
      if (p[1] === null) { pen = false; continue; }
      const x = m.l + (p[0] - xt.min) / (xt.max - xt.min) * pw, y = m.t + ph - (p[1] - y0) / (y1 - y0) * ph;
      d += (pen ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1); pen = true;
    }
    svg.append(sv("path", { d: d, fill: "none", stroke: s.color, "stroke-width": 1.6, "stroke-dasharray": s.dash || null }));
  }
  const leg = el("div", { class: "legend" }, series.map(s => el("span", null, el("i", { "data-c": s.color }), s.name)));
  for (const i of leg.querySelectorAll("i")) i.style.background = i.getAttribute("data-c");
  setChild(host, svg, series.length > 1 ? leg : null);
}

/* bars: labels[], values[], colors (one or array); signed allowed (zero line) */
function barChart(host, labels, values, o) {
  const W = width(host), H = o.height || 170, m = { l: 48, r: 8, t: 18, b: o.rotate ? 52 : 38 };
  const vs = values.filter(v => v !== null);
  if (!vs.length) { setChild(host, emptyNote(o.empty || "No data in this range.")); return; }
  let lo = Math.min(0, ...vs), hi = Math.max(0, ...vs); if (o.ymin !== undefined) lo = Math.min(lo, o.ymin); if (o.ymax !== undefined) hi = Math.max(hi, o.ymax);
  if (hi === lo) hi = lo + 1;
  const nr = niceRange(lo, hi, 4), yt = nr.ticks; lo = nr.lo; hi = nr.hi;
  const svg = sv("svg", { width: W, height: H, viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": o.title || o.ylabel });
  const pw = W - m.l - m.r, ph = H - m.t - m.b, n = values.length, bw = pw / n, fy = o.yfmt || String;
  for (const t of yt) {
    const y = m.t + ph - (t - lo) / (hi - lo) * ph;
    svg.append(sv("line", { class: "grid", x1: m.l, x2: m.l + pw, y1: y, y2: y }), sv("text", { x: m.l - 5, y: y + 4, "text-anchor": "end" }, (o.yfmt || String)(t)));
  }
  const zero = m.t + ph - (0 - lo) / (hi - lo) * ph;
  values.forEach((v, i) => {
    if (v === null) return;
    const y = m.t + ph - (v - lo) / (hi - lo) * ph, top = Math.min(y, zero), h = Math.max(1, Math.abs(y - zero));
    const c = Array.isArray(o.colors) ? o.colors[i] : (o.colors || "#56B4E9");
    svg.append(sv("rect", { x: m.l + i * bw + bw * 0.12, y: top, width: bw * 0.76, height: h, fill: c }, sv("title", null, labels[i] + ": " + fy(v))));
    if (bw >= 24) svg.append(sv("text", { class: "vlabel", x: m.l + i * bw + bw / 2, y: v >= 0 ? top - 3 : zero - 3, "text-anchor": "middle" }, fy(v)));   // value labels: hover does not work on touch
  });
  if (o.shadeBars > 0) svg.append(sv("rect", { class: "hatch-region", x: m.l, y: m.t, width: Math.min(n, o.shadeBars) * bw, height: ph, fill: hatchDefs(svg) }, sv("title", null, o.shadeNote || "unreliable")));
  svg.append(sv("line", { class: "axis", x1: m.l, x2: m.l + pw, y1: zero, y2: zero }), sv("line", { class: "axis", x1: m.l, x2: m.l, y1: m.t, y2: m.t + ph }));
  const every = Math.max(1, Math.ceil(n / Math.floor(pw / (o.rotate ? 16 : 34))));
  labels.forEach((l, i) => {
    if (i % every) return;
    const x = m.l + i * bw + bw / 2, y = m.t + ph + 14;
    svg.append(sv("text", o.rotate ? { x: x, y: y, "text-anchor": "end", transform: "rotate(-40 " + x + " " + y + ")" } : { x: x, y: y, "text-anchor": "middle" }, l));
  });
  svg.append(sv("text", { x: m.l + pw / 2, y: H - 4, "text-anchor": "middle" }, o.xlabel), sv("text", { x: 12, y: m.t + ph / 2, "text-anchor": "middle", transform: "rotate(-90 12 " + (m.t + ph / 2) + ")" }, o.ylabel));
  setChild(host, svg, o.shadeBars > 0 ? warnNote(o.shadeNote, o.shadePrefix) : null);
}

/* horizontal bars for long labels */
function hbarChart(host, labels, values, o) {
  if (!values.length) { setChild(host, emptyNote(o.empty || "No data in this range.")); return; }
  const W = width(host), rh = 24, m = { l: Math.min(190, Math.floor(W * 0.45)), r: 36, t: 6, b: 34 }, H = m.t + rh * values.length + m.b;
  const mx = Math.max(...values, 1), svg = sv("svg", { width: W, height: H, viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": o.title || o.xlabel });
  const pw = W - m.l - m.r, maxc = Math.floor(m.l / 6.3);
  values.forEach((v, i) => {
    const y = m.t + i * rh, w = v / mx * pw;
    svg.append(sv("text", { x: m.l - 6, y: y + rh / 2 + 4, "text-anchor": "end", class: "strong" }, labels[i].length > maxc ? labels[i].slice(0, maxc - 1) + "…" : labels[i], sv("title", null, labels[i])),
      sv("rect", { x: m.l, y: y + 3, width: Math.max(1, w), height: rh - 7, fill: o.color || "#56B4E9" }, sv("title", null, labels[i] + ": " + v)),
      sv("text", { x: m.l + w + 4, y: y + rh / 2 + 4 }, String(v)));
  });
  svg.append(sv("line", { class: "axis", x1: m.l, x2: m.l, y1: m.t, y2: m.t + rh * values.length }), sv("text", { x: m.l + pw / 2, y: H - 6, "text-anchor": "middle" }, o.xlabel));
  setChild(host, svg);
}

/* stacked share bars: cols [{label, shares[8]|null}] */
function stackedShare(host, cols, o) {
  const W = width(host), H = 200, m = { l: 48, r: 8, t: 8, b: 46 };
  if (!cols.some(c => c.shares)) { setChild(host, emptyNote(o.empty)); return; }
  const svg = sv("svg", { width: W, height: H, viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": o.title });
  const pw = W - m.l - m.r, ph = H - m.t - m.b, bw = pw / cols.length;
  for (const t of [0, 0.25, 0.5, 0.75, 1]) {
    const y = m.t + ph - t * ph;
    svg.append(sv("line", { class: "grid", x1: m.l, x2: m.l + pw, y1: y, y2: y }), sv("text", { x: m.l - 5, y: y + 4, "text-anchor": "end" }, Math.round(t * 100) + "%"));
  }
  cols.forEach((c, i) => {
    if (!c.shares) return;
    let acc = 0;
    c.shares.forEach((s, b) => {
      if (!s) return;
      const h = s * ph;
      svg.append(sv("rect", { x: m.l + i * bw + bw * 0.08, y: m.t + ph - (acc + s) * ph, width: bw * 0.84, height: Math.max(0.5, h), fill: BEAM_COLORS[b] }, sv("title", null, c.label + " beam " + (b + 1) + ": " + Math.round(s * 100) + "%")));
      acc += s;
    });
  });
  svg.append(sv("line", { class: "axis", x1: m.l, x2: m.l + pw, y1: m.t + ph, y2: m.t + ph }), sv("line", { class: "axis", x1: m.l, x2: m.l, y1: m.t, y2: m.t + ph }));
  const every = Math.max(1, Math.ceil(cols.length / Math.floor(pw / 40)));
  cols.forEach((c, i) => { if (i % every === 0) svg.append(sv("text", { x: m.l + i * bw + bw / 2, y: m.t + ph + 14, "text-anchor": "middle" }, c.label)); });
  svg.append(sv("text", { x: m.l + pw / 2, y: H - 4, "text-anchor": "middle" }, o.xlabel), sv("text", { x: 12, y: m.t + ph / 2, "text-anchor": "middle", transform: "rotate(-90 12 " + (m.t + ph / 2) + ")" }, o.ylabel));
  setChild(host, svg, beamLegend());
}
function beamLegend(az) {
  const leg = el("div", { class: "legend" }, BEAM_COLORS.map((c, i) => el("span", null, el("i", { "data-c": c }), "B" + (i + 1) + (az ? " " + az[i] + "°" : ""))));
  for (const i of leg.querySelectorAll("i")) i.style.background = i.getAttribute("data-c");
  return leg;
}

function groupLegend(groups, counts) {
  return el("div", { class: "legend" }, groups.map(g => { const sa = shapeAttrs(GROUP_SHAPES[g] || "circle", 7, 7, 5);
    return el("span", { class: "sp-legend", "data-group": g }, sv("svg", { width: 14, height: 14, viewBox: "0 0 14 14", "aria-hidden": "true" }, sv(sa[0], Object.assign({ fill: GROUP_COLORS[g] || SPECIES_OTHER, stroke: "currentColor", "stroke-width": 1 }, sa[1]))), groupName(g) + (counts ? " (" + counts[g] + ")" : "")); }));
}
/* events per hour stacked by group. rows = groupHourly(); o: shadeBars, shadeNote, title */
function stackedGroupBars(host, labs, rows, o) {
  const W = width(host), H = 190, m = { l: 48, r: 8, t: 12, b: 52 }, n = labs.length;
  if (!rows.length) { setChild(host, emptyNote(o.empty || "No events in this range.")); return; }
  const totals = labs.map((_, i) => rows.reduce((a, r) => a + r.values[i], 0)), nr = niceRange(0, Math.max(1, ...totals), 4);
  const svg = sv("svg", { width: W, height: H, viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": o.title }), pw = W - m.l - m.r, ph = H - m.t - m.b, bw = pw / n;
  for (const t of nr.ticks) { const y = m.t + ph - (t - nr.lo) / (nr.hi - nr.lo) * ph; svg.append(sv("line", { class: "grid", x1: m.l, x2: m.l + pw, y1: y, y2: y }), sv("text", { x: m.l - 5, y: y + 4, "text-anchor": "end" }, String(t))); }
  labs.forEach((lb, i) => {
    let acc = 0;
    for (const r of rows) {
      const v = r.values[i]; if (!v) continue;
      const h = v / (nr.hi - nr.lo) * ph, y = m.t + ph - (acc + v) / (nr.hi - nr.lo) * ph; acc += v;
      svg.append(sv("rect", { class: "grp-bar", "data-group": r.group, x: m.l + i * bw + bw * 0.12, y: y, width: bw * 0.76, height: Math.max(0.5, h), fill: GROUP_COLORS[r.group] || SPECIES_OTHER }, sv("title", null, lb + " " + groupName(r.group) + ": " + v)));
    }
  });
  if (o.shadeBars > 0) svg.append(sv("rect", { class: "hatch-region", x: m.l, y: m.t, width: Math.min(n, o.shadeBars) * bw, height: ph, fill: hatchDefs(svg) }, sv("title", null, o.shadeNote || "unreliable")));
  svg.append(sv("line", { class: "axis", x1: m.l, x2: m.l + pw, y1: m.t + ph, y2: m.t + ph }), sv("line", { class: "axis", x1: m.l, x2: m.l, y1: m.t, y2: m.t + ph }));
  const every = Math.max(1, Math.ceil(n / Math.floor(pw / 16)));
  labs.forEach((l, i) => { if (i % every) return; const x = m.l + i * bw + bw / 2, y = m.t + ph + 14; svg.append(sv("text", { x: x, y: y, "text-anchor": "end", transform: "rotate(-40 " + x + " " + y + ")" }, l)); });
  svg.append(sv("text", { x: m.l + pw / 2, y: H - 4, "text-anchor": "middle" }, "hour starting (local time)"), sv("text", { x: 12, y: m.t + ph / 2, "text-anchor": "middle", transform: "rotate(-90 12 " + (m.t + ph / 2) + ")" }, "events per hour"));
  const counts = {}; for (const r of rows) counts[r.group] = r.total;
  setChild(host, svg, groupLegend(rows.map(r => r.group), counts), o.shadeBars > 0 ? warnNote(o.shadeNote) : null);
}
/* one strip per group, each on its OWN scale (opacity = events / that group's busiest hour), so a quiet group stays visible next to a continuous caller */
function groupStrips(host, labs, rows, o) {
  const W = width(host), rh = 24, m = { l: 168, r: 8, t: 6, b: 46 }, n = labs.length;
  if (!rows.length) { setChild(host, emptyNote(o.empty || "No events in this range.")); return; }
  const H = m.t + rows.length * rh + m.b, pw = W - m.l - m.r, bw = pw / n, svg = sv("svg", { width: W, height: H, viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": o.title });
  rows.forEach((r, ri) => {
    const mx = Math.max(...r.values), y = m.t + ri * rh;
    svg.append(sv("text", { class: "strong", x: m.l - 6, y: y + rh / 2 + 4, "text-anchor": "end" }, groupName(r.group) + " (max " + mx + "/h)"));
    r.values.forEach((v, i) => svg.append(sv("rect", { class: "grp-cell", "data-group": r.group, "data-events": v, x: m.l + i * bw + 0.5, y: y + 2, width: Math.max(1, bw - 1), height: rh - 5, fill: GROUP_COLORS[r.group] || SPECIES_OTHER,
      "fill-opacity": v ? (0.15 + 0.85 * v / mx).toFixed(3) : 0.04, stroke: "currentColor", "stroke-opacity": 0.15 }, sv("title", null, labs[i] + " " + groupName(r.group) + ": " + v + " (group's busiest hour: " + mx + ")"))));
  });
  if (o.shadeBars > 0) svg.append(sv("rect", { class: "hatch-region", x: m.l, y: m.t, width: Math.min(n, o.shadeBars) * bw, height: rows.length * rh, fill: hatchDefs(svg) }, sv("title", null, o.shadeNote || "unreliable")));
  const every = Math.max(1, Math.ceil(n / Math.floor(pw / 34)));
  labs.forEach((l, i) => { if (i % every === 0) svg.append(sv("text", { x: m.l + i * bw + bw / 2, y: m.t + rows.length * rh + 14, "text-anchor": "middle" }, l)); });
  svg.append(sv("text", { x: m.l + pw / 2, y: H - 6, "text-anchor": "middle" }, "hour starting (local time); darker = more events relative to that group's own busiest hour"));
  setChild(host, svg, o.shadeBars > 0 ? warnNote(o.shadeNote) : null);
}

/* canvas heatmap. matrix[row][col] (null = no data). o: rowLabels, xTicks:[{col,label}], unit, xlabel, ylabel */
function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
const HEAT_SCALE = {};
function heatmap(host, matrix, o) {
  const flat = matrix.flat().filter(v => v !== null && v !== undefined);
  if (!flat.length) { setChild(host, emptyNote(o.empty || "No data in this range.")); return; }
  const sorted = flat.slice().sort((a, b) => a - b);
  let lo = o.lo !== undefined ? o.lo : percentile(sorted, 0.02), hi = o.hi !== undefined ? o.hi : percentile(sorted, 0.98);
  if (hi - lo < 0.5) { hi = lo + 0.5; }
  if (o.lo === undefined && o.key) { const sc = stableScale(HEAT_SCALE[o.key], lo, hi, 1.5); HEAT_SCALE[o.key] = sc; lo = sc.lo; hi = sc.hi; }
  const W = width(host), rows = matrix.length, cols = matrix[0].length, rh = o.rowH || 22;
  const m = { l: o.labelW || 78, r: 10, t: 6, b: 40 }, H = m.t + rows * rh + m.b + (o.lo === undefined ? 40 : 26), dpr = window.devicePixelRatio || 1;
  const cv = el("canvas", { role: "img", "aria-label": (o.title || o.ylabel) + (o.shadeCols > 0 ? ". " + o.shadeNote : "") + (o.note ? ". " + o.note : "") });
  cv.width = W * dpr; cv.height = H * dpr; cv.style.width = W + "px"; cv.style.height = H + "px";
  const g = cv.getContext("2d"); g.scale(dpr, dpr);
  const pw = W - m.l - m.r, cw = pw / cols, none = cssVar("--panel2"), txt = cssVar("--muted");
  for (let r = 0; r < rows; r++) for (let c = 0; c < cols; c++) {
    const v = matrix[r][c];
    g.fillStyle = v === null || v === undefined ? none : seqColor((v - lo) / (hi - lo));
    g.fillRect(Math.floor(m.l + c * cw), m.t + r * rh, Math.ceil(cw) + (cw < 1.5 ? 0 : 0), rh - 1);
  }
  if (o.shadeCols > 0) {                                                  // hatched overlay + outline + label: not colour only
    const x1 = m.l + o.shadeCols * cw, hh = rows * rh - 1, warn = cssVar("--warn");
    g.save(); g.beginPath(); g.rect(m.l, m.t, x1 - m.l, hh); g.clip();
    g.strokeStyle = warn; g.lineWidth = 1.5; g.globalAlpha = 0.8; g.beginPath();
    for (let x = m.l - hh; x < x1; x += 9) { g.moveTo(x, m.t + hh); g.lineTo(x + hh, m.t); }
    g.stroke(); g.restore();
    g.strokeStyle = warn; g.lineWidth = 1; g.strokeRect(m.l + 0.5, m.t + 0.5, x1 - m.l - 1, hh - 1);
    if (x1 - m.l > 90) { g.fillStyle = warn; g.font = "bold 11px system-ui, sans-serif"; g.textAlign = "left"; g.textBaseline = "top"; g.fillText("unreliable (slot mapping)", m.l + 4, m.t + 3); }
  }
  if (o.stamp) {                                                          // a text stamp in a box (not colour only): the matrix is drawn but the rows cannot be compared
    g.save(); g.font = "bold 11px system-ui, sans-serif"; const sw = g.measureText(o.stamp).width + 10, sx = m.l + 4, sy = m.t + rows * rh - 21, warn2 = cssVar("--warn");
    g.globalAlpha = 0.88; g.fillStyle = cssVar("--panel"); g.fillRect(sx, sy, sw, 17); g.globalAlpha = 1; g.strokeStyle = warn2; g.lineWidth = 1; g.strokeRect(sx + 0.5, sy + 0.5, sw - 1, 16);
    g.fillStyle = warn2; g.textAlign = "left"; g.textBaseline = "middle"; g.fillText(o.stamp, sx + 5, sy + 9); g.restore();
  }
  g.fillStyle = txt; g.font = "11px system-ui, sans-serif"; g.textBaseline = "middle"; g.textAlign = "right";
  o.rowLabels.forEach((l, r) => g.fillText(l, m.l - 6, m.t + r * rh + rh / 2));
  g.textAlign = "center"; g.textBaseline = "alphabetic";
  for (const t of o.xTicks) g.fillText(t.label, m.l + (t.col + 0.5) * cw, m.t + rows * rh + 14);
  g.fillText(o.xlabel, m.l + pw / 2, m.t + rows * rh + 30);
  g.save(); g.translate(10, m.t + rows * rh / 2); g.rotate(-Math.PI / 2); g.fillText(o.ylabel, 0, 0); g.restore();
  const ly = m.t + rows * rh + 36, lw = Math.min(180, pw);
  for (let i = 0; i < lw; i++) { g.fillStyle = seqColor(i / (lw - 1)); g.fillRect(m.l + i, ly, 1, 8); }
  g.fillStyle = txt; g.textAlign = "left"; g.fillText(fmtNum(lo, 1) + " " + o.unit, m.l, ly + 20);
  g.textAlign = "right"; g.fillText(fmtNum(hi, 1) + " " + o.unit, m.l + lw, ly + 20);
  g.textAlign = "left"; g.fillText("grey = no data", m.l + lw + 12, ly + 8);
  if (o.lo === undefined) g.fillText("colour scale auto-fitted to this view", m.l, ly + 34);
  setChild(host, cv, o.shadeCols > 0 ? warnNote(o.shadeNote) : null);
  if (o.note) host.append(el("div", { class: "note-warn" }, Tip.badge("Not steerable", "warn", o.note)));
}

/* beam compass. band = {power_mean_db[8], rel_ring_db[8], leader_beam}; az = azimuths */
function compass(host, band, az, frame, calibrated, kind) {
  if (!band || !band.rel_ring_db) { setChild(host, emptyNote("No beam windows in the last 60 s: the beam-band log is not advancing.", true)); return; }
  const dirs = physicalDirs(kind), S = Math.min(width(host), 380), cx = S / 2, cy = S / 2, R = S / 2 - (dirs ? 62 : 30), SPAN = 6;
  const svg = sv("svg", { width: S, height: S, viewBox: "0 0 " + S + " " + S, role: "img", "aria-label": "Beam compass, " + frame + (dirs ? ", panel frame, clockwise seen from the front, 0 degrees is panel up" : "") });
  for (const f of [0.25, 0.5, 0.75, 1]) svg.append(sv("circle", { class: "grid", cx: cx, cy: cy, r: R * f }));
  svg.append(sv("circle", { class: "ring", cx: cx, cy: cy, r: R * 0.5 }));
  band.rel_ring_db.forEach((v, i) => {
    const r = Math.max(6, R * Math.min(1, Math.max(0, (v + SPAN) / (2 * SPAN))));
    const w = sv("path", { d: wedgePath(cx, cy, r, az[i], 21), fill: BEAM_COLORS[i], "fill-opacity": 0.8, stroke: band.leader_beam === i + 1 ? cssVar("--fg") : "none", "stroke-width": 2 },
      sv("title", null, "Beam " + (i + 1) + " (" + az[i] + "°): " + fmtNum(band.power_mean_db[i], 1) + " dB re full scale, " + (v >= 0 ? "+" : "") + fmtNum(v, 1) + " dB vs ring"));
    svg.append(w);
    const p = polar(cx, cy, R + 14, az[i]);
    svg.append(sv("text", { class: "strong", x: p[0], y: p[1] + 4, "text-anchor": "middle" }, "B" + (i + 1)));
  });
  if (dirs) for (const d of dirs) {                                       // physical directions on the upright panel, seen from the front
    const p = polar(cx, cy, R + 24, d.az);
    svg.append(sv("text", { class: "strong dirmark", "data-dir": d.label, x: d.az === 90 ? p[0] - 2 : d.az === 270 ? p[0] + 2 : p[0], y: p[1] + 4, "text-anchor": d.az === 90 ? "start" : d.az === 270 ? "end" : "middle" }, d.label));
  }
  else for (const a of [0, 90, 180, 270]) { const p = polar(cx, cy, R + 24, a); if (!az.includes(a)) svg.append(sv("text", { x: p[0], y: p[1] + 4, "text-anchor": "middle" }, a + "°")); }
  const help = "Angles: " + frame + ". Wedge length = beam level relative to the ring mean (dashed circle = 0 dB, edge = +" + SPAN + " dB, centre = -" + SPAN + " dB). Outlined wedge = loudest beam."
    + (calibrated ? "" : " Uncalibrated: 0\u00b0 is the array's own axis, not north.")
    + (dirs ? " UP / RIGHT / DOWN / LEFT are directions on the panel seen from the front (the array is mounted upright), provisional. 0\u00b0 = panel up, angles increase clockwise. These are not compass bearings: with the panel facing north, 90\u00b0 is the west side and 270\u00b0 the east side." : "");
  setChild(host, svg, el("div", { class: "note-warn frame-badge" }, Tip.badge(calibrated ? "Calibrated " + frameShort(kind) + " (provisional)" : "Uncalibrated frame", calibrated ? "info" : "warn", help)));
}

// ------------------------------------------------------------------------------------------------ state / tabs
const S = { tab: "overview", status: null, live: null, cal: null, health: null, healthAt: 0, detailId: null, band: 6, range: "24h", det: { range: "24h", species: "", minConf: 0.3, beam: "", showOut: false, showNon: false, hideArtifact: false, group: "bio", full: false, offset: 0, anchor: null, from: "", to: "" },
  beamRange: "24h", timer: null, selectedSpecies: null, actMode: "counts" };
const RANGE_S = { "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800 };

function curCal() { return S.live || S.cal; }
function curAz() { const c = curCal(); return c ? c.beam_azimuth_deg : null; }
function curFrame() { const c = curCal(); return c ? c.frame : "frame unknown"; }
function curKind() { const c = curCal(); return c ? (c.frame_kind || (c.calibrated ? "panel" : "array")) : "array"; }
function frameShort(kind) { return kind === "panel" ? "panel frame" : kind === "compass" ? "compass bearings" : "DSP-frame fallback"; }
function slotFix() { return S.status && typeof S.status.slot_mapping_reliable_from_s === "number" ? S.status.slot_mapping_reliable_from_s : null; }
function nowS() { return Date.now() / 1000; }
function rangeOf(key, custom) {
  if (key === "custom" && custom && custom.from && custom.to) return [new Date(custom.from).getTime() / 1000, new Date(custom.to).getTime() / 1000];
  const n = nowS(); return [n - (RANGE_S[key] || 86400), n];
}
function q(o) { return Object.entries(o).filter(([, v]) => v !== "" && v !== null && v !== undefined).map(([k, v]) => encodeURIComponent(k) + "=" + encodeURIComponent(v)).join("&"); }
function presetBar(current, onPick, withCustom) {
  const keys = ["1h", "6h", "24h", "7d"].concat(withCustom ? ["custom"] : []);
  return keys.map(k => el("button", { "aria-pressed": String(k === current), onclick: () => onPick(k) }, k === "custom" ? "Custom" : k));
}
function bandBar(onPick) {
  return el("div", { class: "bar" }, el("span", { class: "muted" }, "Band:"), BAND_NAMES.map((n, i) => el("button", { "aria-pressed": String(S.band === i), title: isSteerable(i) ? null : STEER_NOTE, onclick: () => { S.band = i; onPick(); } }, n)));
}

function card(k, v, s, cls) { return el("div", { class: "card " + (cls || "") }, el("div", { class: "k" }, k), el("div", { class: "v" }, v), el("div", { class: "s" }, s || "")); }

/* 24 h health sparklines for the status strip, from /api/system (the same data as the System tab: no new source) */
const HEALTH_REFRESH_S = 55;
function sparkSvg(paths, w, h, label) {
  const svg = sv("svg", { class: "spark", width: "100%", height: h, viewBox: "0 0 " + w + " " + h, preserveAspectRatio: "none", role: "img", "aria-label": label });
  for (const p of paths) if (p.d) svg.append(sv("path", { class: "spark-line", d: p.d, fill: "none", stroke: p.color, "stroke-width": 1.4, "stroke-dasharray": p.dash || null, "vector-effect": "non-scaling-stroke" }));
  return svg;
}
function stateStrip(sum, t0, t1, w, h, label) {
  const svg = sv("svg", { class: "spark state-strip", width: "100%", height: h, viewBox: "0 0 " + w + " " + h, preserveAspectRatio: "none", role: "img", "aria-label": label }), x = t => (t - t0) / (t1 - t0) * w;
  for (const r of sum.runs) {
    const ok = r.state === "ok", nd = r.state === "no data", a = { class: "state-run", "data-state": r.state, x: x(r.t0).toFixed(2), width: Math.max(0.6, x(r.t1) - x(r.t0)).toFixed(2) };
    svg.append(sv("rect", Object.assign(a, ok ? { y: h - 4, height: 4, fill: "var(--good)" } : nd ? { y: 0, height: h, fill: "none", stroke: "var(--muted)", "stroke-dasharray": "3 3", "stroke-width": 1, "vector-effect": "non-scaling-stroke" } : { y: 0, height: h, fill: "var(--bad)" }), sv("title", null, r.state + ", " + fmtTime(r.t0, true) + " to " + fmtTime(r.t1, true))));
  }
  return svg;
}
function healthCards() {
  const h = S.health, na = (k, why) => card(k, "n/a", why);
  if (!h) return [na("Loss, 24 h", "loading"), na("Board temp, 24 h", "loading"), na("Stream state, 24 h", "loading")];
  if (!h.ok) return [na("Loss, 24 h", h.error), na("Board temp, 24 h", h.error), na("Stream state, 24 h", h.error)];
  const d = h.data, s = d.stream, b = d.board, t0 = d.since_s, t1 = d.now_s, out = [];
  const lg = sparkGeom(s.samples.map(x => [x.t_s, x.loss_pct]), 120, 28, { lo: 0, t0: t0, t1: t1 });
  out.push(lg.n ? card("Loss, 24 h", fmtNum(s.loss_pct_mean, 2) + " % mean", [sparkSvg([{ d: lg.d, color: "var(--accent)" }], 120, 28, "Packet loss over the last 24 hours: mean " + fmtNum(s.loss_pct_mean, 2) + " %, maximum " + fmtNum(lg.max, 2) + " %, latest " + fmtNum(lg.last, 2) + " %"),
    el("div", { class: "muted" }, "max " + fmtNum(lg.max, 2) + " % · latest " + fmtNum(lg.last, 2) + " % · " + s.n_samples + " samples")]) : na("Loss, 24 h", "no stream samples in the last 24 h"));
  const th = b.temperatures.history, tl = sparkGeom(th.map(x => [x.t_s, x.local_c]), 120, 28, { t0: t0, t1: t1 }), rl = th.length ? th[th.length - 1].remote_c : null;
  const off = S.status && S.status.board && S.status.board.enabled === false;
  out.push(tl.n ? card("Board temp, 24 h", fmtNum(tl.last, 1) + " °C local", [sparkSvg([{ d: tl.d, color: "var(--warn)" }], 120, 28, "Board local temperature over the last 24 hours: minimum " + fmtNum(tl.min, 1) + ", maximum " + fmtNum(tl.max, 1) + ", latest " + fmtNum(tl.last, 1) + " degrees Celsius"),
    el("div", { class: "muted" }, fmtNum(tl.min, 1) + " to " + fmtNum(tl.max, 1) + " °C local · remote now " + fmtNum(rl, 1) + " °C")]) : na("Board temp, 24 h", boardErrText(off ? "board_disabled" : Object.values(b.errors)[0])));
  const sum = stateSummary(s.samples, t0, t1), sk = S.status && S.status.sink, pct = sum.total_s ? Math.round(sum.ok_s / sum.total_s * 1000) / 10 : 0, latest = s.latest ? s.latest.state : null;
  const txt = "ok " + fmtNum(pct, 1) + " % of the last 24 h, other states " + fmtAge(sum.other_s) + ", no data " + fmtAge(sum.no_data_s);
  out.push(card("Stream state, 24 h", latest || "no data", [stateStrip(sum, t0, t1, 120, 14, "Stream state over the last 24 hours: " + txt), el("div", { class: "muted" }, txt),
    el("div", { class: "muted" }, sk ? "sink now: " + (sk.playing ? "playing" : "not playing") + ", " + sk.holes + " holes, " + sk.underruns + " underruns" : "sink status not available")], latest && latest !== "ok" ? "bad" : sum.other_s > 0 ? "warn" : ""));
  return out;
}
function renderStrip() {
  const st = S.status, host = document.getElementById("strip");
  if (!st) { setChild(host, card("Dashboard", "waiting", "no status yet")); return; }
  const b = st.bridge, bd = st.board, le = st.last_event, sp = st.latest.spatial;
  const stale = st.sources.filter(s => s.stale === true), pi = patternInfo(bd, S.cal || S.live);
  setChild(host,
    card("Stream", b ? b.state : "no status", b ? "loss " + fmtNum(b.loss_pct, 2) + " % (30 s) · " + fmtNum(b.pps, 0) + " pkt/s · " + fmtAge(b.age_s) + " ago" : "bridge_status.json missing", b && b.state !== "ok" ? "bad" : ""),
    card("Board", !bd.enabled ? "disabled" : bd.reachable ? "reachable" : "unreachable", bd.reachable ? "API " + (bd.api_version || "?") + " · " + fmtNum(bd.temp_local_c, 1) + " / " + fmtNum(bd.temp_remote_c, 1) + " °C local/remote" : boardErrText(bd.error), bd.reachable || !bd.enabled ? "" : "warn"),
    card("Last detection", le ? fmtAge(le.age_s) + " ago" : "none in window", le ? le.species + " (" + fmtNum(le.confidence, 2) + ")" + (le.in_region === false ? " out of region" : "") : "model opinions only"),
    card("Beam pattern", pi.value, [pi.sub, el("span", { class: "pattern-dirs" }, Tip.tip(pi.dirs, { label: "About: beam directions" }))]),
    card("Sources", stale.length ? stale.length + " stale" : "all fresh", stale.length ? [stale.slice(0, 3).map(s => s.name + " " + fmtAge(s.age_s)).join(", ") + (stale.length > 3 ? " and " + (stale.length - 3) + " more" : ""), Tip.tip("Stale sources: " + stale.map(s => s.name + " " + fmtAge(s.age_s)).join(", "), { label: "About: stale sources" })] : st.sources.filter(s => s.stale === false).length + " live files checked", stale.length ? "warn" : ""),
    card("Backfill", st.ingest.backfill_done ? "done" : "running", st.ingest.beam_windows_in_memory + " beam windows, " + st.ingest.events_in_memory + " events in memory", st.ingest.backfill_done ? "" : "warn"),
    healthCards());
  document.getElementById("refresh").textContent = "updated " + new Date().toLocaleTimeString() + (document.hidden ? " (paused)" : "");
}

// ---- Overview
const OV = {};
function miniBeams(counts, label) {
  const mini = sv("svg", { width: 70, height: 16, viewBox: "0 0 70 16", role: "img", "aria-label": label }), mx = Math.max(...counts, 1);
  counts.forEach((c, i) => mini.append(sv("rect", { x: i * 8.5, y: 16 - 15 * c / mx, width: 7, height: Math.max(1, 15 * c / mx), fill: BEAM_COLORS[i] }, sv("title", null, "Beam " + (i + 1) + ": " + c + " events"))));
  return mini;
}
function buildOverview() {
  const t = document.getElementById("tab-overview");
  OV.recent = el("div", { class: "tablewrap" }); OV.top = el("div", { class: "tablewrap" }); OV.hourly = el("div", { class: "chart" }); OV.act = el("div", { class: "chart" }); OV.actBar = el("div", { class: "bar" }); OV.compass = el("div", { class: "chart" }); OV.band = el("div", { class: "bar" });
  OV.tags = el("div", { class: "muted" });
  setChild(t, el("div", { class: "grid" },
    el("div", { class: "panel" }, ptitle("Last 6 h: most recent detections", "One row per call (reports on several beams within 3 s are one event). Model opinions; out-of-region species and non-species classes (Engine, Siren) are left out here and viewable on the Detections tab."), OV.recent),
    el("div", { class: "panel" }, ptitle("Last 6 h: top species and where from", "Bars = events per beam 1-8. The azimuth frame (calibrated panel frame or DSP-frame fallback) is named under the compass and in the tables."), OV.top),
    el("div", { class: "panel" }, el("h2", null, "Live beam compass (last 60 s)"), OV.band, OV.compass),
    el("div", { class: "panel" }, el("h2", null, "Events per hour, last 24 h"), OV.hourly),
    el("div", { class: "panel" }, ptitle("Activity by group, last 24 h", "Events per hour by taxonomic group. Insects (katydids, crickets) call continuously at this time of year and are about two thirds of all events, so the second view gives every group its own scale."), OV.actBar, OV.act),
    el("div", { class: "panel" }, el("h2", null, "Background sound"), OV.tags)));
}
async function refreshOverview() {
  const n = nowS(), p6 = { since: Math.floor(n - 21600), until: Math.ceil(n) };
  const [sp, ev, d24] = await Promise.all([api("/api/detections/species?" + q(p6)), api("/api/detections/events?" + q(Object.assign({ limit: 500, min_conf: 0 }, p6))),
    api("/api/detections/species?" + q({ since: Math.floor(n - 86400), until: Math.ceil(n) }))]);
  if (!ev.ok) setChild(OV.recent, emptyNote(ev.error, true));
  else if (!ev.data.events.length) setChild(OV.recent, emptyNote("No in-region detections in the last 6 h."));
  else {
    const runs = collapseRuns(ev.data.events, 120, ev.data.total > ev.data.events.length);
    setChild(OV.recent, el("table", null, el("thead", null, el("tr", null, ["Time", "Species", "Group", "Best beam", "Beams", "Conf"].map((h, i) => el("th", { class: i === 5 ? "num" : "" }, h)))),
      el("tbody", null, runs.slice(0, 10).map(x => el("tr", null, el("td", null, fmtTime(x.t_first, false) + (x.n > 1 ? "-" + fmtTime(x.t_last, false) : "")), el("td", null, runText(x)), el("td", null, groupName(x.group)), el("td", null, "B" + x.best_beam + " " + x.best_beam_azimuth_deg + "\u00b0"),
        el("td", null, x.beams.map(b => "B" + b).join(" ")), el("td", { class: "num" }, fmtNum(x.max_confidence, 2)))))),
      tnote(ev.data.total + " event(s) in the last 6 h, shown as " + Math.min(10, runs.length) + " of " + runs.length + " call run(s): calls of one species less than 120 s apart are one row (\u00d7 n = how many events, first-last time)" +
        (ev.data.total > ev.data.events.length ? "; only the newest " + ev.data.events.length + " events were fetched" : "") + "; angles: " + ev.data.frame + "."));
  }
  if (!sp.ok) setChild(OV.top, emptyNote(sp.error, true));
  else if (!sp.data.species.length) setChild(OV.top, emptyNote("No in-region species in the last 6 h (or the detection log has produced none)."));
  else {
    const top = topPerGroup(sp.data.species, 3);
    setChild(OV.top, el("table", null, el("thead", null, el("tr", null, [el("th", null, "Species"), el("th", null, "Group"), el("th", { class: "num" }, "Events"), el("th", null, "Events by beam 1-8")])),
      el("tbody", null, top.map(s => el("tr", null, el("td", null, s.species), el("td", null, groupName(s.group)), el("td", { class: "num" }, s.events), el("td", null, miniBeams(s.beam_events, "events per beam")))))),
      tnote("The 3 busiest species of each group (" + top.length + " of " + sp.data.species.length + " species): a continuous caller cannot push the others out."));
  }
  if (!d24.ok) setChild(OV.hourly, emptyNote(d24.error, true));
  else {
    const hours = {}; for (const s of d24.data.species) for (const h of s.hourly_counts) hours[h.hour_s] = (hours[h.hour_s] || 0) + h.events;
    const end = Math.floor(n / 3600) * 3600, labs = [], vals = [];
    for (let h = end - 23 * 3600; h <= end; h += 3600) { labs.push(fmtTime(h)); vals.push(hours[h] || 0); }
    const starts = []; for (let h = end - 23 * 3600; h <= end; h += 3600) starts.push(h);
    const sb = shadeBars(starts, slotFix());
    barChart(OV.hourly, labs, vals, { ylabel: "events per hour", xlabel: "hour starting (local time)", rotate: true, shadeBars: sb, shadeNote: sb ? slotNote(slotFix()) : null });
    const rows = groupHourly(d24.data.species, starts), mode = S.actMode;
    clear(OV.actBar); OV.actBar.append(el("span", { class: "muted" }, "Scale:"), el("button", { "aria-pressed": String(mode === "counts"), onclick: () => { S.actMode = "counts"; refreshOverview(); } }, "Stacked counts"),
      el("button", { "aria-pressed": String(mode === "own"), onclick: () => { S.actMode = "own"; refreshOverview(); } }, "Each group on its own scale"));
    const o = { title: "Events per hour by taxonomic group, " + (mode === "own" ? "each group on its own scale" : "stacked counts"), shadeBars: sb, shadeNote: sb ? slotNote(slotFix()) : null, empty: "No events in the last 24 h." };
    (mode === "own" ? groupStrips : stackedGroupBars)(OV.act, labs, rows, o);
  }
  clear(OV.band); OV.band.append(el("span", { class: "muted" }, "Band:"), ...BAND_NAMES.map((nm, i) => el("button", { "aria-pressed": String(S.band === i), title: isSteerable(i) ? null : STEER_NOTE, onclick: () => { S.band = i; refreshOverview(); } }, nm)));
  drawLive(OV.compass);
  const st = S.status; if (st) {
    const tg = st.latest.tags, cl = st.latest.clap;
    setChild(OV.tags, el("div", null, "PANNs tags: " + (tg ? tg.map(x => x[0] + " " + fmtNum(x[1], 2)).join(", ") : "none yet")),
      el("div", null, "CLAP zero-shot: " + (cl ? cl.map(x => x[0] + " " + fmtNum(x[1], 2)).join(", ") : "none yet")),
      el("div", null, "Latest spatial: " + (st.latest.spatial ? "dominant beam " + st.latest.spatial.dominant_beam + ", spread " + fmtNum(st.latest.spatial.spread_db, 1) + " dB, " + (st.latest.spatial.directional ? "directional" : "not directional") : "none yet")));
  }
}
function drawLive(host) {
  if (!S.live) { setChild(host, emptyNote("Waiting for the beam data.")); return; }
  compass(host, S.live.bands[S.band], S.live.beam_azimuth_deg, S.live.frame, S.live.calibrated, S.live.frame_kind);
  if (!isSteerable(S.band)) host.append(el("div", { class: "note-warn steer-note" }, Tip.badge("Not steerable", "warn", STEER_NOTE)));
}

// ---- Detections
const DT = {};
function buildDetections() {
  const t = document.getElementById("tab-detections");
  DT.bar = el("div", { class: "bar" }); DT.chips = el("div", { class: "bar chips", role: "group", "aria-label": "taxonomic group" }); DT.unc = el("div", { class: "muted" }); DT.scatterBar = el("div", { class: "bar" }); DT.events = el("div", { class: "tablewrap" }); DT.summary = el("div", { class: "tablewrap" }); DT.hod = el("div", { class: "chart" }); DT.timeline = el("div", { class: "chart" }); DT.tlTitle = el("h2", null, "Species timeline"); DT.pager = el("div", { class: "bar" }); DT.scatter = el("div", { class: "chart" });
  DT.tlPanel = el("div", { class: "panel" }, DT.tlTitle, DT.timeline);
  DT.detail = el("div", { class: "panel", id: "event-detail", hidden: "", role: "region", "aria-label": "Event detail" }); DT.sm = el("div", { class: "chart" }); DT.smNote = el("div");
  setChild(t, el("div", { class: "panel" }, DT.bar, DT.chips, DT.unc),
    el("div", { class: "panel" }, ptitle("Events", "One event = one call reported on one or more beams within 3 s. Out-of-region rows are greyed (the county filter says not expected here). A possible-artifact badge is an advisory flag (a cold-night insect label or a burst of noise transients), the event stays listed. Click (or press Enter on) a row for the event detail."), DT.events, DT.pager),
    DT.detail,
    el("div", { class: "panel" }, el("h2", null, "Detections: time \u00d7 direction"), DT.scatterBar, DT.scatter),
    el("div", { class: "grid" },
      el("div", { class: "panel" }, el("h2", null, "Species summary"), DT.summary),
      el("div", { class: "panel" }, el("h2", null, "Hour of day (local), events"), DT.hod)),
    el("div", { class: "panel", id: "hour-dir-panel" }, el("h2", null, "Species × hour of day × direction"), DT.smNote, DT.sm),
    DT.tlPanel);
}
function detParams(extra) {
  const d = S.det, [since, until] = rangeOf(d.range, d);
  const fixed = d.offset > 0 && d.anchor ? d.anchor : Math.ceil(until);                 // paging past page 1 is anchored: new events do not shift the rows
  return Object.assign({ since: Math.floor(since), until: fixed, in_region: d.showOut ? "any" : "true", non_species: d.showNon || d.group === "mechanical" ? "any" : "false", group: d.showNon && d.group === "bio" ? "any" : d.group, artifact: d.hideArtifact ? "none" : "" }, extra || {});
}
function renderDetBar() {
  const d = S.det;
  const species = el("select", { "aria-label": "species", onchange: e => { d.species = e.target.value; d.offset = 0; refreshDetections(); } }, el("option", { value: "" }, "All species"));
  for (const s of (DT.speciesList || [])) { const o = el("option", { value: s.scientific || s.species }, s.species + " (" + s.events + ")"); if ((s.scientific || s.species) === d.species) o.selected = true; species.append(o); }
  const beam = el("select", { "aria-label": "beam", onchange: e => { d.beam = e.target.value; d.offset = 0; refreshDetections(); } }, el("option", { value: "" }, "Any beam"), [1, 2, 3, 4, 5, 6, 7, 8].map(b => { const o = el("option", { value: b }, "Beam " + b); if (String(b) === String(d.beam)) o.selected = true; return o; }));
  const conf = el("input", { type: "number", min: 0, max: 1, step: 0.05, value: d.minConf, "aria-label": "minimum confidence", style: null, onchange: e => { d.minConf = Math.min(1, Math.max(0, Number(e.target.value) || 0)); d.offset = 0; refreshDetections(); } });
  conf.size = 4;
  const out = el("input", { type: "checkbox", onchange: e => { d.showOut = e.target.checked; d.offset = 0; refreshDetections(); } }); out.checked = d.showOut;
  const non = el("input", { type: "checkbox", onchange: e => { d.showNon = e.target.checked; d.offset = 0; refreshDetections(); } }); non.checked = d.showNon;
  const art = el("input", { type: "checkbox", "data-artifact-toggle": "1", onchange: e => { d.hideArtifact = e.target.checked; d.offset = 0; refreshDetections(); } }); art.checked = d.hideArtifact;
  const bar = [presetBar(d.range, k => { d.range = k; d.offset = 0; renderDetBar(); refreshDetections(); }, true), species, beam, el("label", null, "min conf", conf), el("label", null, out, "show out-of-region"), el("label", null, non, "show non-species (Engine, Siren)"),
    el("label", null, art, "hide flagged insect detections"), Tip.tip("Off by default. Insect labels reported on a cold night (below " + fmtNum(ARTIFACT_FLOOR_C, 0) + " °C) or inside a burst of noise transients are flagged as possible artifacts. Turning this on leaves the flagged events out of the lists and counts of this tab; nothing is deleted and the number left out is shown next to the toggle.", { label: "About: hide flagged insect detections" }), el("span", { class: "muted", id: "artifact-count" }, DT.artText || "")];
  if (d.range === "custom") {
    const from = el("input", { type: "datetime-local", "aria-label": "from", value: d.from, onchange: e => { d.from = e.target.value; refreshDetections(); } });
    const to = el("input", { type: "datetime-local", "aria-label": "to", value: d.to, onchange: e => { d.to = e.target.value; refreshDetections(); } });
    bar.push(el("label", null, "from", from), el("label", null, "to", to));
  }
  setChild(DT.bar, bar);
  renderChips();
}
/* group chips with exact counts (species.groups, counted before the group filter); the non-biological count comes from its own request because that class is hidden by default */
function renderChips() {
  const d = S.det, g = DT.groups, cnt = k => (g ? (g.find(x => x.group === k) || { events: 0 }).events : null);
  const bio = g ? GROUPS_BIO.reduce((a, k) => a + cnt(k), 0) : null;
  const chips = [["bio", bio]].concat(GROUPS_BIO.map(k => [k, cnt(k)]), [["mechanical", DT.mech === undefined ? null : DT.mech]]);
  setChild(DT.chips, el("span", { class: "muted" }, "Group:"), chips.map(([k, n]) => el("button", { "aria-pressed": String(d.group === k), "data-group": k, onclick: () => { d.group = k; d.offset = 0; d.species = ""; renderDetBar(); refreshDetections(); } },
    groupName(k) + " (" + (n === null ? "n/a" : n) + ")")));
}
async function refreshDetections() {
  const d = S.det;
  if (d.range === "custom" && (!d.from || !d.to)) { const n = new Date(), p = x => x.toISOString().slice(0, 16); d.to = p(new Date(n.getTime() - n.getTimezoneOffset() * 60000)); d.from = p(new Date(n.getTime() - n.getTimezoneOffset() * 60000 - 86400000)); renderDetBar(); }
  const LIMIT = 50;
  const [ev, sp, mech] = await Promise.all([api("/api/detections/events?" + q(detParams({ species: d.species, min_conf: d.minConf, beam: d.beam, limit: LIMIT, offset: d.offset }))),
    api("/api/detections/species?" + q(detParams())), api("/api/detections/species?" + q(detParams({ group: "mechanical", non_species: "any" })))]);
  DT.mech = mech.ok ? mech.data.groups.find(x => x.group === "mechanical").events : null;
  if (sp.ok) {
    DT.speciesList = sp.data.species; DT.groups = sp.data.groups;
    DT.artText = artifactCountText(sp.data, d.hideArtifact); const ac = document.getElementById("artifact-count"); if (ac) ac.textContent = DT.artText;
    const unc = sp.data.unclassified;
    setChild(DT.unc, sp.data.unclassified_total > 0 ? [el("span", null, "Unclassified labels: " + sp.data.unclassified_total + " event(s)"), Tip.tip("Unclassified labels (filed under Other biological, not dropped): " + sp.data.unclassified_total + " event(s): " + unc.map(u => u.species + " (" + u.events + ")").join(", ") + (sp.data.unclassified_total > unc.reduce((a, u) => a + u.events, 0) ? ", ..." : ""), { label: "About: unclassified labels" })] : null);
    const sig = d.range + "|" + d.showOut + "|" + d.showNon + "|" + d.group + "|" + sp.data.groups.map(g => g.events).join("/") + "|" + (DT.mech === null ? "x" : DT.mech) + "|" + sp.data.species.map(s => (s.scientific || s.species) + ":" + s.events).join(",");
    if (!DT.barBuilt || (sig !== DT.barSig && !DT.bar.contains(document.activeElement))) { renderDetBar(); DT.barBuilt = true; DT.barSig = sig; }   // never rebuild a control the user is editing
  }
  if (!ev.ok) setChild(DT.events, emptyNote(ev.error, true));
  else {
    const e = ev.data, az = e.frame;
    if (!e.events.length) setChild(DT.events, emptyNote("No events match these filters in this range" + (e.truncated_by_window ? " (part of the range is older than the in-memory window; individual events are not kept in rollups)." : ".")));
    else {
      const head = ["Time", "Species", "Group", "Best beam", "Beams", "Reports", "Max conf", "Max level (dBFS)"];
      const rows = e.events.map(x => el("tr", { class: "click" + (x.in_region === false ? " out" : "") + ((x.scientific || x.species) === S.selectedSpecies ? " sel" : ""), "data-key": x.scientific || x.species, "data-eid": x.id, tabindex: "0", title: "Show the event detail",
        onclick: () => { pickSpecies(x.scientific || x.species, true); openEvent(x.id); }, onkeydown: ev2 => { if (ev2.key === "Enter" || ev2.key === " ") { ev2.preventDefault(); pickSpecies(x.scientific || x.species, true); openEvent(x.id); } },
        "aria-selected": String((x.scientific || x.species) === S.selectedSpecies), "aria-current": x.id === S.detailId ? "true" : null },
        el("td", null, fmtTime(x.t_start_s, true) + ":" + String(new Date(x.t_start_s * 1000).getSeconds()).padStart(2, "0")), el("td", null, x.species + (x.in_region === false ? " (out of region)" : "") + (x.non_species ? " (non-species)" : ""), artifactFlagged(x.artifact_flags) ? [" ", Tip.badge("possible artifact", x.artifact_flags.level === "likely" ? "warn" : "info", artifactText(x.artifact_flags))] : null), el("td", null, groupName(x.group)),
        el("td", null, "B" + x.best_beam + " " + x.best_beam_azimuth_deg + "°"), el("td", null, x.beams.map(b => "B" + b).join(" ")), el("td", { class: "num" }, x.reports),
        el("td", { class: "num" }, fmtNum(x.max_confidence, 2)), el("td", { class: "num" }, fmtNum(x.max_level_dbfs, 1))));
      setChild(DT.events, el("table", null, el("thead", null, el("tr", null, head.map((h, i) => el("th", { class: i >= 5 ? "num" : "" }, h)))), el("tbody", null, rows)));
    }
    setChild(DT.pager, el("button", { onclick: () => { d.offset = Math.max(0, d.offset - LIMIT); if (!d.offset) d.anchor = null; refreshDetections(); }, disabled: d.offset ? null : "" }, "Newer"),
      el("span", { class: "muted" }, e.total ? (d.offset + 1) + "-" + Math.min(e.total, d.offset + LIMIT) + " of " + e.total + " events · angles: " + e.frame + (d.offset ? " · frozen at " + fmtTime(d.anchor, true) : "") : "0 events"), d.offset ? Tip.tip("Frozen while paging: go to the first page to see new events.", { label: "About paging" }) : null,
      el("button", { onclick: () => { if (!d.offset) d.anchor = Math.ceil(e.until_s); d.offset += LIMIT; refreshDetections(); }, disabled: d.offset + LIMIT < e.total ? null : "" }, "Older"));
  }
  refreshScatter();
  if (!sp.ok) { setChild(DT.summary, emptyNote(sp.error, true)); setChild(DT.hod, emptyNote(sp.error, true)); setChild(DT.sm, emptyNote(sp.error, true)); return; }
  const list = sp.data.species;
  refreshMultiples(list);
  if (!list.length) { setChild(DT.summary, emptyNote("No species in this range.")); setChild(DT.hod, emptyNote("No species in this range.")); }
  else {
    const SHOWN = 40, rows = list.slice(0, SHOWN).map(s => {
      const mini = sv("svg", { width: 70, height: 16, viewBox: "0 0 70 16", role: "img", "aria-label": "events per beam" }), mx = Math.max(...s.beam_events, 1);
      s.beam_events.forEach((c, i) => mini.append(sv("rect", { x: i * 8.5, y: 16 - 15 * c / mx, width: 7, height: Math.max(1, 15 * c / mx), fill: BEAM_COLORS[i] }, sv("title", null, "Beam " + (i + 1) + ": " + c + " events"))));
      return el("tr", { class: "click" + (s.in_region === false ? " out" : "") + ((s.scientific || s.species) === S.selectedSpecies ? " sel" : ""), "data-key": s.scientific || s.species, onclick: () => pickSpecies(s.scientific || s.species), "aria-selected": String((s.scientific || s.species) === S.selectedSpecies) },
        el("td", null, s.species + (s.in_region === false ? " (out of region)" : "") + (s.non_species ? " (non-species)" : ""), s.chorus ? el("span", { class: "badge", title: s.events_per_active_hour + " events per active hour over " + s.active_hours + " hours" }, " continuous caller") : null,
          s.artifact_events > 0 ? [" ", Tip.badge(s.artifact_events + " of " + s.events + " possible artifact", s.artifact_events * 2 > s.events ? "warn" : "info", "Advisory: " + s.artifact_events + " of the " + s.events + " events of this label were reported on a cold night (below " + fmtNum(ARTIFACT_FLOOR_C, 0) + " °C) or inside a burst of noise transients. They are kept and counted; the toggle above can leave them out.")] : null),
        el("td", null, groupName(s.group)), el("td", { class: "num" }, s.events), el("td", { class: "num" }, s.reports), el("td", { class: "num" }, fmtNum(s.max_confidence, 2)),
        el("td", null, fmtTime(s.last_seen_s, true) + (s.bounds_exact ? "" : " ≈")), el("td", null, mini));
    });
    setChild(DT.summary, el("table", null, el("thead", null, el("tr", null, ["Species", "Group", "Events", "Reports", "Max conf", "Last seen", "Events by beam 1-8"].map((h, i) => el("th", { class: i > 1 && i < 5 ? "num" : "" }, h)))), el("tbody", null, rows)),
      tnote("≈ = last seen known to the hour only (older rollup data). Click a row for its timeline." + (list.length > SHOWN ? " Showing the busiest " + SHOWN + " of " + list.length + " species; pick a group above to see the rest." : "") + " A continuous caller makes at least 60 events per active hour over 3 or more hours; it is only a label, nothing is filtered out."));
    const top = topPerGroup(list, 3).slice(0, 15), hod = top.map(s => hourOfDayCounts(s.hourly_counts));
    heatmap(DT.hod, hod, { rowLabels: top.map(s => s.species.length > 18 ? s.species.slice(0, 17) + "…" : s.species), rowH: 20, labelW: 116, lo: 0, hi: Math.max(1, ...hod.flat()), unit: "events",
      xTicks: [0, 3, 6, 9, 12, 15, 18, 21].map(h => ({ col: h, label: String(h) })), xlabel: "hour of day (local time)", ylabel: "species" });
  }
  refreshTimeline();
}
/* ---- event detail: the per-beam confidence / level trace of one event, its beams, group, species, provenance and the detections within +-60 s */
const BEAM_SHAPES = ["circle", "square", "triangle", "diamond", "cross", "bar", "circle", "square"];
async function openEvent(id) {
  S.detailId = id;
  for (const tr of document.querySelectorAll("#tab-detections tr[data-eid]")) { if (tr.getAttribute("data-eid") === id) tr.setAttribute("aria-current", "true"); else tr.removeAttribute("aria-current"); }
  DT.detail.hidden = false;
  setChild(DT.detail, el("h2", null, "Event detail"), emptyNote("Loading the event."));
  const r = await api("/api/detections/event?" + q({ id: id }));
  if (S.detailId !== id) return;                                                           // another event was picked meanwhile
  if (!r.ok) { setChild(DT.detail, closeBar(), emptyNote(/not.found/i.test(r.error) ? "This event is no longer in the server's in-memory window (events older than the window are kept only as hourly rollups)." : r.error, true)); return; }
  const ev = r.data.event, nb = await api("/api/detections/events?" + q({ since: (ev.t_start_s - NEIGHBOUR_S).toFixed(3), until: (ev.t_end_s + NEIGHBOUR_S + 0.001).toFixed(3), in_region: "any", non_species: "any", min_conf: 0, limit: 500 }));
  if (S.detailId !== id) return;
  renderEventDetail(r.data, nb);
  if (DT.detail.scrollIntoView) DT.detail.scrollIntoView({ block: "nearest", behavior: "smooth" });
}
function closeBar() { return el("div", { class: "bar" }, el("h2", null, "Event detail"), el("button", { class: "close-detail", onclick: () => { S.detailId = null; DT.detail.hidden = true; for (const tr of document.querySelectorAll("#tab-detections tr[data-eid]")) tr.removeAttribute("aria-current"); } }, "Close")); }
function renderEventDetail(det, nb) {
  const ev = det.event, kind = curKind(), tr = traceByBeam(det.reports), span = ev.t_end_s - ev.t_start_s, sec = t => fmtTime(t, true) + ":" + String(new Date(t * 1000).getSeconds()).padStart(2, "0");
  const fact = (k, v) => el("tr", null, el("th", null, k), el("td", null, v));
  const facts = el("table", { class: "wrap facts" }, el("tbody", null,
    fact("Species", ev.species + (ev.scientific && ev.scientific !== ev.species ? " (" + ev.scientific + ")" : "")), fact("Group", groupName(ev.group)),
    fact("Provenance", ev.provenance + " (a model's opinion, not a confirmed identification)"),
    fact("Time", sec(ev.t_start_s) + (span > 0 ? " to " + sec(ev.t_end_s) + " (" + fmtNum(span, 1) + " s)" : " (a single instant)") + ", local"),
    fact("Beams involved", tr.map(b => beamDirLabel(b.beam, b.azimuth, kind)).join(", ") + " (" + tr.length + " of 8)"),
    fact("Best beam", beamDirLabel(ev.best_beam, ev.best_beam_azimuth_deg, kind) + ", confidence " + fmtNum(ev.best_confidence, 2)),
    fact("Reports", String(ev.reports) + (det.trace_truncated ? " (only the first " + det.reports.length + " are listed)" : "")),
    fact("Region", ev.in_region === false ? "out of region (the county filter says not expected here)" : ev.in_region === true ? "in region" : "unknown"),
    ev.artifact_flags ? fact("Artifact check", artifactFlagged(ev.artifact_flags) ? [Tip.badge("possible artifact", ev.artifact_flags.level === "likely" ? "warn" : "info", artifactText(ev.artifact_flags)), " ", artifactText(ev.artifact_flags)] : artifactText(ev.artifact_flags)) : null,
    ev.non_species ? fact("Class", "non-species") : null, ev.slot_mapping_unreliable ? fact("Beam slots", "unreliable: detected before the slot mapping was fixed") : null));
  const perBeam = el("div", { class: "tablewrap" }, el("table", null, el("thead", null, el("tr", null, ["Beam", "Reports", "Max conf", "Max level (dBFS)"].map((h, i) => el("th", { class: i ? "num" : "" }, h)))),
    el("tbody", null, tr.map(b => el("tr", null, el("td", null, beamDirLabel(b.beam, b.azimuth, kind)), el("td", { class: "num" }, b.n), el("td", { class: "num" }, fmtNum(b.max_conf, 2)), el("td", { class: "num" }, fmtNum(b.max_level, 1)))))));
  const confHost = el("div", { class: "chart" }), lvlHost = el("div", { class: "chart" }), hasLevel = tr.some(b => b.max_level !== null);
  const ns = nb.ok ? neighbourSummary(nb.data.events, ev) : null, SHOWN = 12;
  const nbBlock = !nb.ok ? emptyNote(nb.error, true) : !ns.rows.length ? emptyNote("No other detections within " + NEIGHBOUR_S + " s of this event.") :
    el("div", { class: "tablewrap" }, el("table", null, el("thead", null, el("tr", null, ["Species", "Group", "Events", "Nearest", "Beams", "Max conf"].map((h, i) => el("th", { class: i === 2 || i === 5 ? "num" : "" }, h)))),
      el("tbody", null, ns.rows.slice(0, SHOWN).map(r => el("tr", null, el("td", null, r.species), el("td", null, groupName(r.group)), el("td", { class: "num" }, r.events),
        el("td", null, r.nearest_dt === 0 ? "overlaps" : (r.nearest_dt < 0 ? "" : "+") + fmtNum(r.nearest_dt, 1) + " s"), el("td", null, r.beams.map(b => "B" + b).join(" ")), el("td", { class: "num" }, fmtNum(r.max_confidence, 2)))))),
      tnote(ns.events + " other event(s) of " + ns.rows.length + " species within " + NEIGHBOUR_S + " s before and after (one row per species, so a continuous caller is one row)" + (ns.rows.length > SHOWN ? "; the nearest " + SHOWN + " species are shown" : "") +
        (nb.data.total > nb.data.events.length ? "; only the newest " + nb.data.events.length + " of " + nb.data.total + " events could be fetched" : "") + "."));
  setChild(DT.detail, closeBar(), el("div", { class: "grid" }, el("div", null, facts, perBeam), el("div", null, el("h3", null, "Confidence per beam"), confHost, el("h3", null, "Level per beam"), lvlHost)),
    el("h3", null, "Detections within ±" + NEIGHBOUR_S + " s"), nbBlock);
  traceChart(confHost, tr, ev.t_start_s, span, "conf", { ymin: 0, ymax: 1, ylabel: "confidence (model opinion)", yfmt: v => v.toFixed(2), title: "Confidence of each report, per beam" });
  if (hasLevel) traceChart(lvlHost, tr, ev.t_start_s, span, "level", { ylabel: "level (dBFS)", yfmt: v => String(v), title: "Level of each report, per beam" }); else setChild(lvlHost, emptyNote("No report of this event carried a level."));
}
/* one polyline + marker series per beam (colour AND marker shape, with a legend and the table beside it); x = seconds from the event start */
function traceChart(host, byBeam, t0, span, field, o) {
  const W = Math.max(260, Math.floor(host.clientWidth || (host.parentNode && host.parentNode.clientWidth) || 420)), H = 190, m = { l: 52, r: 10, t: 8, b: 38 }, pw = W - m.l - m.r, ph = H - m.t - m.b;
  const vals = byBeam.flatMap(b => b.points.map(p => p[field])).filter(v => v !== null && v !== undefined);
  if (!vals.length) { setChild(host, emptyNote("No data.")); return; }
  const pad = span > 0 ? span * 0.08 : 1, x0 = -pad, x1 = span + pad;
  let y0 = o.ymin !== undefined ? o.ymin : Math.min(...vals), y1 = o.ymax !== undefined ? o.ymax : Math.max(...vals);
  if (y1 === y0) { y1 += 1; y0 -= o.ymin === undefined ? 1 : 0; }
  const nr = niceRange(y0, y1, 4); y0 = nr.lo; y1 = nr.hi;
  const xt = { min: x0, max: x1, ticks: niceTicks(0, Math.max(span, 1), 5).filter(t => t >= x0 && t <= x1) };
  const svg = sv("svg", { width: W, height: H, viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": o.title + ": " + byBeam.map(b => "beam " + b.beam + " " + b.n + " report(s)").join(", ") });
  axes(svg, W, H, m, xt, { min: y0, max: y1, ticks: nr.ticks }, "seconds from the first report (" + fmtTime(t0, true) + ")", o.ylabel, t => (t > 0 ? "+" : "") + fmtNum(t, span < 5 ? 1 : 0), o.yfmt);
  for (const b of byBeam) {
    const pts = b.points.filter(p => p[field] !== null && p[field] !== undefined).map(p => [m.l + (p.t - t0 - x0) / (x1 - x0) * pw, m.t + ph - (p[field] - y0) / (y1 - y0) * ph, p]);
    if (pts.length > 1) svg.append(sv("path", { d: pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(""), fill: "none", stroke: BEAM_COLORS[b.beam - 1], "stroke-width": 1.4, "stroke-opacity": 0.8 }));
    for (const p of pts) {
      const sa = shapeAttrs(BEAM_SHAPES[b.beam - 1], p[0], p[1], 4.5);
      svg.append(sv(sa[0], Object.assign({ class: "trace-pt", "data-beam": b.beam, fill: BEAM_COLORS[b.beam - 1], stroke: "currentColor", "stroke-width": 1 }, sa[1]), sv("title", null, "beam " + b.beam + " at +" + fmtNum(p[2].t - t0, 2) + " s: " + (field === "conf" ? fmtNum(p[2].conf, 2) : fmtNum(p[2].level, 1) + " dBFS"))));
    }
  }
  const leg = el("div", { class: "legend" }, byBeam.map(b => {
    const sa = shapeAttrs(BEAM_SHAPES[b.beam - 1], 7, 7, 5);
    return el("span", { class: "sp-legend" }, sv("svg", { width: 14, height: 14, viewBox: "0 0 14 14", "aria-hidden": "true" }, sv(sa[0], Object.assign({ fill: BEAM_COLORS[b.beam - 1], stroke: "currentColor", "stroke-width": 1 }, sa[1]))), "B" + b.beam + " (" + b.n + ")");
  }));
  setChild(host, svg, leg);
}

/* ---- species x hour of day x direction: the top species of every group, each a 24 x 8 grid (hour by direction), the cell = the share of that hour's events whose best beam points that way */
async function refreshMultiples(list) {
  const d = S.det, [since] = rangeOf(d.range, d), s0 = Math.floor(since / 3600) * 3600, az = curAz();
  if (!list.length) { setChild(DT.smNote, null); setChild(DT.sm, emptyNote("No species in this range.")); return; }
  if (!az) { setChild(DT.sm, emptyNote("Waiting for the beam azimuths.")); return; }
  const r = await api("/api/detections/density?" + q(Object.assign(detParams({ min_conf: d.minConf }), { since: s0, bucket_s: 3600 })));
  if (!r.ok) { setChild(DT.sm, emptyNote(r.error, true)); return; }
  const kind = curKind(), top = topPerGroup(list, 2), order = scatterLanes(az);
  setChild(DT.smNote, tnote("The 2 busiest species of each group (so a continuous caller cannot crowd out the others). Rows = directions (" + frameShort(kind) + "), columns = local hour of day; a cell is the share of that hour's events whose best beam points that way (darker = larger share). Hours with fewer than " + MIN_DIR_N +
    " events are drawn dashed and empty: too few to read a direction from. The bars underneath are events per hour, each species on its own scale (its busiest hour is stated). Range, region, class, group and confidence filters above apply; the in-memory window only" + (r.data.truncated_by_window ? " (part of the range is older than it)" : "") + "."));
  setChild(DT.sm, el("div", { class: "grid multiples" }, top.map(sp => dirHourFigure(sp, hourDirMatrix(r.data.rows, sp.scientific || sp.species), az, order, kind))));
}
function dirHourFigure(sp, mat, az, order, kind) {
  const model = dirHourModel(mat, MIN_DIR_N), W = 360, ml = 92, mt = 4, rh = 11, bh = 16, cw = (W - ml - 6) / 24, H = mt + 8 * rh + 4 + bh + 18, color = GROUP_COLORS[sp.group] || SPECIES_OTHER, total = model.hours.reduce((a, h) => a + h.total, 0);
  const svg = sv("svg", { class: "dirhour", width: "100%", viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": sp.species + ", " + groupName(sp.group) + ": " + total + (total === 1 ? " event" : " events") + " by hour of day and direction, busiest hour " + model.peak_hour + ":00 with " + model.max_total });
  order.forEach((b, i) => svg.append(sv("text", { x: ml - 4, y: mt + i * rh + rh - 2, "text-anchor": "end" }, beamDirLabel(b, az[b - 1], kind))));
  for (const h of model.hours) {
    order.forEach((b, i) => {
      const n = h.counts[b - 1], base = { class: "dh-cell", "data-hour": h.hour, "data-beam": b, "data-events": n, x: (ml + h.hour * cw + 0.4).toFixed(1), y: mt + i * rh + 0.4, width: (cw - 0.8).toFixed(1), height: rh - 0.8 };
      if (h.readable) svg.append(sv("rect", Object.assign(base, { fill: color, "fill-opacity": (0.06 + 0.94 * h.shares[b - 1]).toFixed(3) }), sv("title", null, h.hour + ":00 " + beamDirLabel(b, az[b - 1], kind) + ": " + n + " of " + h.total + " events (" + Math.round(h.shares[b - 1] * 100) + " %)")));
      else svg.append(sv("rect", Object.assign(base, { fill: "none", stroke: "currentColor", "stroke-opacity": h.total ? 0.45 : 0.12, "stroke-dasharray": "1.5 1.5", "stroke-width": 0.6 }), sv("title", null, h.hour + ":00 " + beamDirLabel(b, az[b - 1], kind) + ": " + (h.total ? n + " of " + h.total + " events (too few to read a direction)" : "no events"))));
    });
    const bhh = model.max_total ? h.total / model.max_total * bh : 0;
    svg.append(sv("rect", { class: "dh-total", "data-hour": h.hour, "data-events": h.total, x: (ml + h.hour * cw + 0.4).toFixed(1), y: (mt + 8 * rh + 4 + bh - bhh).toFixed(1), width: (cw - 0.8).toFixed(1), height: Math.max(h.total ? 1 : 0, bhh).toFixed(1), fill: color }, sv("title", null, h.hour + ":00 " + h.total + " events")));
  }
  for (const hh of [0, 6, 12, 18]) svg.append(sv("text", { x: (ml + hh * cw).toFixed(1), y: H - 4 }, hh + "h"));
  svg.append(sv("text", { x: W - 4, y: H - 4, "text-anchor": "end" }, "events/hour, max " + model.max_total));
  const sa = shapeAttrs(GROUP_SHAPES[sp.group] || "circle", 7, 7, 5);
  return el("figure", { class: "dh-fig", "data-species": sp.species }, el("figcaption", null, sv("svg", { width: 14, height: 14, viewBox: "0 0 14 14", "aria-hidden": "true" }, sv(sa[0], Object.assign({ fill: color, stroke: "currentColor", "stroke-width": 1 }, sa[1]))),
    " " + sp.species + " (" + groupName(sp.group) + "), " + total + (total === 1 ? " event" : " events") + (sp.chorus ? ", continuous caller" : "") + ", busiest hour " + model.peak_hour + ":00"), svg);
}
/* shared frame of the dots and density views: axes (time, zoomed or full), one lane per beam ordered by azimuth, the pre-fix hatched region */
function scatterFrame(W, az, o, label) {
  const laneH = 24, lanes = scatterLanes(az), kind = o.kind, m = { l: 118, r: 10, t: 8, b: 38 }, H = m.t + lanes.length * laneH + m.b;
  const pw = W - m.l - m.r, ph = lanes.length * laneH, t0 = o.t0, t1 = o.t1;
  const svg = sv("svg", { width: W, height: H, viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": label });
  axes(svg, W, H, m, { min: t0, max: t1, ticks: timeTickValues(t0, t1, Math.max(2, Math.floor(W / 110))) }, { min: 0, max: 1, ticks: [] }, "time (local)", "beam, azimuth (" + frameShort(kind) + ")", timeFmt(t0, t1), String);
  lanes.forEach((b, i) => {
    const y = m.t + i * laneH + laneH / 2, d = dirNear(az[b - 1], kind);
    svg.append(sv("line", { class: "grid", x1: m.l, x2: m.l + pw, y1: y, y2: y }), sv("text", { class: "lane", x: m.l - 5, y: y + 4, "text-anchor": "end" }, "B" + b + " " + az[b - 1] + "\u00b0" + (d ? " (" + d.toLowerCase() + ")" : "")));
  });
  const frac = shadeBefore(t0, t1, o.fixAt);
  if (frac > 0) svg.append(sv("rect", { class: "hatch-region", x: m.l, y: m.t, width: frac * pw, height: ph, fill: hatchDefs(svg) }, sv("title", null, slotNote(o.fixAt))));
  return { svg: svg, lanes: lanes, laneH: laneH, m: m, pw: pw, frac: frac };
}
/* time on x, beam direction lane on y (ordered by azimuth), dot size = confidence, colour + shape = group (several groups) or species (one group). Every event is a dot. */
function detScatter(host, evs, az, o) {
  const W = width(host), t0 = o.t0, t1 = o.t1, st = speciesStyles(evs);
  const fr = scatterFrame(W, az, o, "Detections scatter: " + evs.length + " events, time by beam direction, " + st.legend.length + " species groups, dot size is confidence"), svg = fr.svg, lanes = fr.lanes, laneH = fr.laneH, m = fr.m, pw = fr.pw, frac = fr.frac;
  evs.forEach((e, i) => {
    const lane = lanes.indexOf(e.best_beam); if (lane < 0) return;
    const cx = m.l + Math.min(1, Math.max(0, (e.t_start_s - t0) / (t1 - t0))) * pw, cy = m.t + lane * laneH + laneH / 2 + ((i % 3) - 1) * 3, sty = st.by[e.species], out = e.in_region === false;
    const sa = shapeAttrs(sty.shape, cx, cy, dotRadius(e.max_confidence));
    svg.append(sv(sa[0], Object.assign({ class: "ev-dot", "data-species": e.species, "data-shape": sty.shape, "data-group": e.group || null, fill: sty.color, "fill-opacity": out ? 0.35 : 0.85, stroke: "currentColor", "stroke-width": 1, "stroke-dasharray": out ? "2 2" : null }, sa[1]),
      sv("title", null, fmtTime(e.t_start_s, true) + " " + e.species + " conf " + fmtNum(e.max_confidence, 2) + " beam B" + e.best_beam + " " + e.best_beam_azimuth_deg + "\u00b0" + (out ? " (out of region)" : "") + (e.slot_mapping_unreliable ? " (slot mapping unreliable)" : ""))));
  });
  const leg = el("div", { class: "legend" }, st.legend.map(l => { const sa = shapeAttrs(l.shape, 7, 7, 5); return el("span", { class: "sp-legend" }, sv("svg", { width: 14, height: 14, viewBox: "0 0 14 14", "aria-hidden": "true" }, sv(sa[0], Object.assign({ fill: l.color, stroke: "currentColor", "stroke-width": 1 }, sa[1]))), l.label + " (" + l.count + ")"); }));
  setChild(host, fr.svg, leg, tnote("All " + evs.length + " event(s) shown as dots" + (o.total > evs.length ? " (of " + o.total + ")" : "") + ". Dot size = confidence (radius 3 to 9 px); " + (st.mode === "group" ? "colour and shape = group (pick a group above to tell its species apart)" : "colour and shape = species") + "; faded dashed outline = out of region. Rows are beams ordered by azimuth, angles: " + o.frame + "."),
    frac > 0 ? warnNote(slotNote(o.fixAt)) : null);
}
/* density rows (per bucket, species, beam) -> cells per (bucket, beam, group) with the exact count and the species behind it; sorted, deterministic */
function densityCells(rows) {
  const by = new Map();
  for (const r of rows) {
    const k = r.t_s + "|" + r.best_beam + "|" + r.group, c = by.get(k) || { t_s: r.t_s, best_beam: r.best_beam, group: r.group, events: 0, species: new Map() };
    c.events += r.events; c.species.set(r.species, (c.species.get(r.species) || 0) + r.events); by.set(k, c);
  }
  return Array.from(by.values()).map(c => ({ t_s: c.t_s, best_beam: c.best_beam, group: c.group, events: c.events, species: Array.from(c.species.entries()).sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1)) }))
    .sort((a, b) => a.t_s - b.t_s || a.best_beam - b.best_beam || groupRank(a.group) - groupRank(b.group));
}
/* the same axes for ranges with more events than dots: one cell per (time bucket, beam lane, group); darker = more events (log scale), the exact count is in each cell's title */
function detDensity(host, rows, az, o) {
  const W = width(host), cells = densityCells(rows), groups = GROUP_KEYS.filter(g => cells.some(c => c.group === g)), t0 = o.t0, t1 = o.t1;
  const fr = scatterFrame(W, az, o, "Detections density: " + o.total + " events in " + cells.length + " cells, time by beam direction, darker is more events"), svg = fr.svg, mx = Math.max(1, ...cells.map(c => c.events));
  const sub = (fr.laneH - 2) / Math.max(1, groups.length), cw = Math.max(2, o.bucketS / (t1 - t0) * fr.pw);
  for (const c of cells) {
    const lane = fr.lanes.indexOf(c.best_beam); if (lane < 0) continue;
    const x = fr.m.l + Math.min(fr.pw - 1, Math.max(0, (c.t_s - t0) / (t1 - t0) * fr.pw)), y = fr.m.t + lane * fr.laneH + 1 + groups.indexOf(c.group) * sub;
    svg.append(sv("rect", { class: "dens-cell", "data-group": c.group, "data-events": c.events, x: x.toFixed(1), y: y.toFixed(1), width: cw.toFixed(1), height: Math.max(1, sub - 0.5).toFixed(1), fill: GROUP_COLORS[c.group] || SPECIES_OTHER, "fill-opacity": densityOpacity(c.events, mx) },
      sv("title", null, fmtTime(c.t_s, true) + " " + groupName(c.group) + ", beam B" + c.best_beam + ": " + c.events + " event(s) (" + c.species.slice(0, 4).map(x => x[0] + " \u00d7" + x[1]).join(", ") + (c.species.length > 4 ? ", ..." : "") + ")")));
  }
  const counts = {}; for (const c of cells) counts[c.group] = (counts[c.group] || 0) + c.events;
  setChild(host, svg, groupLegend(groups, counts), tnote(o.total + " events are aggregated into " + cells.length + " cells of " + o.bucketS + " s \u00d7 beam \u00d7 group because there are more than " + SCATTER_CAP + " (dots would overlap). Darker = more events (log scale, busiest cell " + mx + "); the exact count and species are in each cell's title. Within a lane each group has its own row, in the order of the legend. Angles: " + o.frame + "."),
    fr.frac > 0 ? warnNote(slotNote(o.fixAt)) : null);
}
const SCATTER_CAP = 500;
async function refreshScatter() {
  const d = S.det, base = { species: d.species, min_conf: d.minConf, beam: d.beam }, r = await api("/api/detections/events?" + q(detParams(Object.assign({ limit: SCATTER_CAP, offset: 0 }, base))));
  if (!r.ok) { setChild(DT.scatterBar); setChild(DT.scatter, emptyNote(r.error, true)); return; }
  const e = r.data, az = curAz();
  if (!e.events.length) { setChild(DT.scatterBar); setChild(DT.scatter, emptyNote("No events match these filters in this range.")); return; }
  if (!az) { setChild(DT.scatter, emptyNote("Waiting for the beam azimuths.")); return; }
  let first, last, dn = null;
  if (e.total <= SCATTER_CAP) { const ts = e.events.map(x => x.t_start_s); first = Math.min(...ts); last = Math.max(...ts); }
  else {
    const p0 = detParams(base), d0 = await api("/api/detections/density?" + q(p0));
    if (!d0.ok) { setChild(DT.scatter, emptyNote(d0.error, true)); return; }
    dn = d0.data; first = dn.data_first_s; last = dn.data_last_s;
  }
  const [t0, t1] = zoomRange(first, last, e.since_s, e.until_s, d.full);
  if (dn && !d.full && (t0 > e.since_s || t1 < e.until_s)) {                                  // re-bucket the zoomed range: finer cells, the same events
    const d1 = await api("/api/detections/density?" + q(Object.assign(detParams(base), { since: Math.floor(t0), until: Math.ceil(t1) })));
    if (d1.ok && d1.data.total === dn.total) dn = d1.data; else if (!d1.ok) { setChild(DT.scatter, emptyNote(d1.error, true)); return; }
  }
  const o = { t0: dn && !d.full ? dn.since_s : t0, t1: dn && !d.full ? dn.until_s : t1, fixAt: e.slot_mapping_reliable_from_s, kind: curKind(), total: e.total, frame: e.frame, bucketS: dn ? dn.bucket_s : 0 };
  setChild(DT.scatterBar, el("button", { "aria-pressed": String(d.full), onclick: () => { d.full = !d.full; refreshScatter(); } }, "Full range"),
    tnote("x-axis: " + (d.full ? "the full range " : "the extent of the data ") + fmtTime(o.t0, true) + " to " + fmtTime(o.t1, true) + (d.full ? "" : " (press Full range to see the whole selected range)")));
  if (dn) detDensity(DT.scatter, dn.rows, az, o); else detScatter(DT.scatter, e.events, az, o);
}
function pickSpecies(key, noScroll) {                                    // noScroll: the caller scrolls somewhere else (the event detail)
  S.selectedSpecies = key;
  for (const tr of document.querySelectorAll("#tab-detections tr.click")) {                   // acknowledge the click at once, not at the next refresh
    const on = tr.getAttribute("data-key") === key;
    tr.classList.toggle("sel", on); tr.setAttribute("aria-selected", String(on));
  }
  refreshTimeline().then(() => { if (!noScroll && DT.tlPanel && DT.tlPanel.scrollIntoView) DT.tlPanel.scrollIntoView({ block: "nearest", behavior: "smooth" }); });
}
async function refreshTimeline() {
  const sel = S.selectedSpecies; if (!sel) { setChild(DT.timeline, emptyNote("Click a species to see its timeline.")); return; }
  const r = await api("/api/detections/events?" + q(detParams({ species: sel, limit: 500, min_conf: 0 })));
  setChild(DT.tlTitle, el("span", null, "Timeline: " + sel + " "), el("button", { onclick: () => { S.selectedSpecies = null; refreshTimeline(); } }, "clear"));
  if (!r.ok) { setChild(DT.timeline, emptyNote(r.error, true)); return; }
  const evs = r.data.events;
  if (!evs.length) { setChild(DT.timeline, emptyNote("No events for this species in the range.")); return; }
  if (r.data.total > evs.length) {                                                            // more events than dots: events per time bucket, exact counts, nothing truncated
    const dn = await api("/api/detections/density?" + q(detParams({ species: sel, min_conf: 0 })));
    if (!dn.ok) { setChild(DT.timeline, emptyNote(dn.error, true)); return; }
    const by = new Map(); for (const x of dn.data.rows) by.set(x.t_s, (by.get(x.t_s) || 0) + x.events);
    const ts = Array.from(by.keys()).sort((a, b) => a - b);
    barChart(DT.timeline, ts.map(t => fmtTime(t, (dn.data.until_s - dn.data.since_s) > 36 * 3600)), ts.map(t => by.get(t)), { ylabel: "events per " + dn.data.bucket_s + " s", xlabel: "bucket start (local time)", rotate: true, title: "events over time, aggregated" });
    DT.timeline.append(el("div", { class: "note-warn" }, Tip.badge("aggregated", "info", dn.data.total + " events aggregated into " + ts.length + " buckets of " + dn.data.bucket_s + " s (more than " + SCATTER_CAP + " events: dots would overlap); empty buckets are not drawn.")));
    return;
  }
  const W = width(DT.timeline), H = 200, m = { l: 48, r: 10, t: 8, b: 38 }, t0 = r.data.since_s, t1 = r.data.until_s;
  const svg = sv("svg", { width: W, height: H, viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": "events over time" });
  const xt = { min: t0, max: t1, ticks: timeTickValues(t0, t1, Math.max(2, Math.floor(W / 110))) };
  axes(svg, W, H, m, xt, { min: 0, max: 1, ticks: [0, 0.25, 0.5, 0.75, 1] }, "time (local)", "max confidence (model opinion)", timeFmt(t0, t1), v => v.toFixed(2));
  const pw = W - m.l - m.r, ph = H - m.t - m.b;
  for (const e of evs) svg.append(sv("circle", { cx: m.l + (e.t_start_s - t0) / (t1 - t0) * pw, cy: m.t + ph - e.max_confidence * ph, r: 3.5, fill: BEAM_COLORS[e.best_beam - 1], "fill-opacity": e.in_region === false ? 0.35 : 0.9 }, sv("title", null, fmtTime(e.t_start_s, true) + " beam " + e.best_beam + " conf " + e.max_confidence)));
  setChild(DT.timeline, svg, beamLegend(curAz()), tnote(evs.length + " event(s); dot colour = best-confidence beam; faded = out of region."));
}

// ---- Beams
const BM = {};
function buildBeams() {
  const t = document.getElementById("tab-beams");
  BM.bar = el("div", { class: "bar" }); BM.compass = el("div", { class: "chart" }); BM.heat = el("div", { class: "chart" }); BM.bias = el("div", { class: "chart" }); BM.lead = el("div", { class: "chart" }); BM.contrast = el("div", { class: "tablewrap" }); BM.band = el("div"); BM.biasNote = el("div"); BM.steer = el("div"); BM.window = el("div"); BM.dir = el("div", { class: "chart" });
  setChild(t, el("div", { class: "panel" }, BM.bar, BM.band, BM.steer),
    el("div", { class: "grid" },
      el("div", { class: "panel" }, el("h2", null, "Live beams, last 60 s"), BM.compass),
      el("div", { class: "panel" }, ptitle("Bias: each beam relative to the ring mean", "Long-run power mean per beam minus the mean power of all 8 beams, in the selected band. A persistent offset that stays with one beam number when the pattern is rotated is a channel gain difference, not a source."), BM.bias,
        BM.biasNote, BM.window)),
    el("div", { class: "panel" }, ptitle("Beam × time heatmap", "Power-mean level per beam in the selected band; row labels carry each beam's azimuth in the frame named on the axis (calibrated panel frame when a profile is stored, else the DSP-frame fallback)."), BM.heat),
    el("div", { class: "panel", id: "dir-strip-panel" }, ptitle("Direction strip: time × direction", "The same beam data ordered by direction (azimuth) instead of beam number: each cell is the beam's level minus the mean of all beams at that time (±" + DIR_SPAN_DB + " dB), so a bright row is the direction the sound comes from. Beams are named by their physical direction when a panel-frame calibration is stored."), BM.dir),
    el("div", { class: "grid" },
      el("div", { class: "panel" }, ptitle("Leader share per hour (2-8 kHz)", "Share of windows with at least 3 dB contrast between loudest and quietest beam in which each beam led."), BM.lead),
      el("div", { class: "panel" }, el("h2", null, "Contrast statistics"), BM.contrast)));
}
function heatBucket(sec) { return sec <= 3600 * 6 ? 60 : sec <= 3600 * 24 ? 300 : sec <= 3600 * 48 ? 600 : 3600; }
async function refreshBeams() {
  const [since, until] = rangeOf(S.beamRange), span = until - since, bucket = heatBucket(span);
  const p = { since: Math.floor(since), until: Math.ceil(until), band: S.band };
  clear(BM.bar); BM.bar.append(el("span", { class: "muted" }, "Range:"), ...presetBar(S.beamRange, k => { S.beamRange = k; refreshBeams(); }, false));
  clear(BM.band); BM.band.append(bandBar(refreshBeams));
  setChild(BM.steer, isSteerable(S.band) ? null : el("div", { class: "note-warn steer-note" }, Tip.badge("Not steerable", "warn", STEER_NOTE)));
  drawLive(BM.compass);
  const [hm, st] = await Promise.all([api("/api/beams/heatmap?" + q(Object.assign({ bucket_s: bucket }, p))), api("/api/beams/stats?" + q(p))]);
  const az = curAz() || new Array(8).fill(null), frame = curFrame(), kind = curKind();
  if (!hm.ok) { setChild(BM.heat, emptyNote(hm.error, true)); setChild(BM.dir, emptyNote(hm.error, true)); }
  else {
    const d = hm.data, rows = d.values_power_mean_db, n = d.n_buckets, ticks = [];
    for (const t of timeTickValues(d.t0_s, d.t0_s + n * d.bucket_s, Math.max(2, Math.floor(width(BM.heat) / 90)))) ticks.push({ col: Math.min(n - 1, Math.floor((t - d.t0_s) / d.bucket_s)), label: fmtTime(t, span > 36 * 3600) });
    const sc = shadeCols(d.t0_s, d.bucket_s, n, d.slot_mapping_reliable_from_s), nst = d.steerable === false;
    heatmap(BM.heat, rows, { key: "beams|" + d.band + "|" + S.beamRange, shadeCols: sc, shadeNote: sc ? slotNote(d.slot_mapping_reliable_from_s) : null, rowLabels: az.map((a, i) => "B" + (i + 1) + (a === null ? "" : " " + a + "°")), xTicks: ticks, unit: "dB re FS", xlabel: "time (local), " + d.bucket_s + " s buckets, " + BAND_NAMES[d.band], ylabel: "beam, azimuth (" + frameShort(kind) + ")", note: nst ? STEER_NOTE : null, stamp: nst ? NOT_STEERABLE_STAMP : null, empty: "No beam windows in this range: the beam-band log has no data for it." });
    if (!curAz()) setChild(BM.dir, emptyNote("Waiting for the beam azimuths."));
    else {                                                                // the same windows ordered by direction, each beam against the ring mean at that time
      const order = scatterLanes(az), rel = ringRelative(rows);
      heatmap(BM.dir, order.map(b => rel[b - 1]), { lo: -DIR_SPAN_DB, hi: DIR_SPAN_DB, shadeCols: sc, shadeNote: sc ? slotNote(d.slot_mapping_reliable_from_s) : null, rowLabels: order.map(b => beamDirLabel(b, az[b - 1], kind)), labelW: 124, xTicks: ticks, unit: "dB vs ring mean",
        title: "Direction strip, beams ordered by azimuth, level relative to the ring mean, " + BAND_NAMES[d.band], xlabel: "time (local), " + d.bucket_s + " s buckets, " + BAND_NAMES[d.band], ylabel: "direction, beam (" + frameShort(kind) + ")",
        note: nst ? STEER_NOTE : null, stamp: nst ? NOT_STEERABLE_STAMP : null, empty: "No beam windows in this range: the beam-band log has no data for it." });
    }
  }
  if (!st.ok) { for (const h of [BM.bias, BM.lead, BM.contrast]) setChild(h, emptyNote(st.error, true)); return; }
  const s = st.data;
  clear(BM.biasNote);
  setChild(BM.window, tnote("Whole hours: " + fmtTime(s.effective_since_s, true) + " to " + fmtTime(s.effective_until_s, true) + " local (the range you picked, widened to hour boundaries).", "About the time range"));
  if (!s.windows) { for (const h of [BM.bias, BM.lead, BM.contrast]) setChild(h, emptyNote("No beam windows in this range (no live data or rollup hours).")); return; }
  barChart(BM.bias, s.beams.map(b => "B" + b.beam), s.beams.map(b => b.rel_ring_db), { colors: BEAM_COLORS, ylabel: "dB re ring mean (" + BAND_NAMES[s.band] + ")", xlabel: "beam (" + frame + ")", yfmt: v => (v > 0 ? "+" : "") + v.toFixed(1), shadeBars: s.steerable === false ? 8 : 0, shadeNote: STEER_NOTE, shadePrefix: "" });
  const cols = s.hourly.map(h => ({ label: fmtTime(h.hour_s, s.hourly.length > 30), shares: h.leader_share }));
  stackedShare(BM.lead, cols, { empty: "No windows with 3 dB contrast in this range.", title: "leader share per hour", xlabel: "hour starting (local time)", ylabel: "share of contrast windows" });
  setChild(BM.contrast, el("table", null, el("thead", null, el("tr", null, ["Beam", "Azimuth", "Mean (dB re FS)", "Max (dB re FS)", "vs ring (dB)", "Leader share", "2-8 kHz vs ring (dB)"].map((h, i) => el("th", { class: i ? "num" : "" }, h)))),
    el("tbody", null, s.beams.map(b => el("tr", null, el("td", null, "Beam " + b.beam), el("td", { class: "num" }, b.azimuth_deg + "°"), el("td", { class: "num" }, fmtNum(b.power_mean_db, 1)), el("td", { class: "num" }, fmtNum(b.max_db, 1)),
      el("td", { class: "num" }, (b.rel_ring_db > 0 ? "+" : "") + fmtNum(b.rel_ring_db, 1) + (s.steerable === false ? " (not steerable)" : "")), el("td", { class: "num" }, b.leader_share === null ? "n/a" : Math.round(b.leader_share * 100) + " %"),
      el("td", { class: "num" }, b.mean_rel_ring_db_2_8khz === null ? "n/a" : (b.mean_rel_ring_db_2_8khz > 0 ? "+" : "") + fmtNum(b.mean_rel_ring_db_2_8khz, 1)))))),
    tnote(s.windows + " one-second windows over " + s.hours + " hour(s); " + s.windows_with_contrast + " with at least " + s.contrast_min_db + " dB contrast. Angles: " + frame + "."));
}

// ---- System
const SY = {};
function buildSystem() {
  const t = document.getElementById("tab-system");
  SY.loss = el("div", { class: "chart" }); SY.hourly = el("div", { class: "chart" }); SY.temps = el("div", { class: "chart" }); SY.facts = el("div", { class: "tablewrap" }); SY.plugins = el("div", { class: "tablewrap" }); SY.mon = el("div", { class: "tablewrap" });
  SY.spectrum = el("div", { class: "panel", id: "spectrum-panel" });
  setChild(t, SY.spectrum, el("div", { class: "grid" },
    el("div", { class: "panel" }, ptitle("Stream loss, last 24 h", "30 s samples from the bridge. Loss of 1-2 % is a known steady condition: read the trend, not single points."), SY.loss),
    el("div", { class: "panel" }, el("h2", null, "Loss per hour"), SY.hourly),
    el("div", { class: "panel" }, el("h2", null, "Board temperatures"), SY.temps),
    el("div", { class: "panel" }, el("h2", null, "Board, memory and versions"), SY.facts),
    el("div", { class: "panel" }, el("h2", null, "Analysis plugins"), SY.plugins),
    el("div", { class: "panel" }, el("h2", null, "DSP monitor (raw)"), SY.mon)));
  mountSpectrum();
}
/* spectrum.js (44 KB) is fetched only when the System tab is first shown, so the first render of the other tabs stays light */
function mountSpectrum() {
  if (SY.spectrumApi || SY.spectrumLoading) return;
  const go = () => { SY.spectrumApi = Spectrum.mount(SY.spectrum); };
  if (typeof Spectrum !== "undefined") { go(); return; }
  SY.spectrumLoading = true;
  const sc = document.createElement("script"); sc.src = "/static/spectrum.js"; sc.onload = go;
  sc.onerror = () => { SY.spectrumLoading = false; setChild(SY.spectrum, emptyNote("The live spectrum module could not be loaded.", true)); };
  document.head.append(sc);
}
async function refreshSystem() {
  const r = await api("/api/system?" + q({ since: Math.floor(nowS() - 86400) }));
  if (!r.ok) { for (const h of [SY.loss, SY.hourly, SY.temps, SY.facts, SY.plugins, SY.mon]) setChild(h, emptyNote(r.error, true)); return; }
  const d = r.data, s = d.stream, b = d.board;
  lineChart(SY.loss, [{ name: "loss per 30 s window", color: "#56B4E9", points: s.samples.map(x => [x.t_s, x.loss_pct]) }], { ymin: 0, ylabel: "packet loss (%)", yfmt: v => v.toFixed(1), empty: "No stream samples in the last 24 h (bridge.log has no stats lines)." });
  if (!s.hourly.length) setChild(SY.hourly, emptyNote("No hourly stream rows yet."));
  else barChart(SY.hourly, s.hourly.map(h => fmtTime(h.hour_s)), s.hourly.map(h => h.loss_pct_mean), { ylabel: "mean loss (%)", xlabel: "hour starting (local time)", yfmt: v => v.toFixed(1), rotate: true });
  const th = b.temperatures.history;
  const off = S.status && S.status.board && S.status.board.enabled === false, bmsg = boardErrText(off ? "board_disabled" : Object.values(b.errors)[0]);
  if (!th.length) setChild(SY.temps, emptyNote(b.reachable ? "No temperature samples yet (the board is polled every 20 s)." : bmsg, !b.reachable && !off));
  else lineChart(SY.temps, [{ name: "local", color: "#E69F00", points: th.map(x => [x.t_s, x.local_c]) }, { name: "remote", color: "#56B4E9", points: th.map(x => [x.t_s, x.remote_c]), dash: "5 3" }], { ylabel: "temperature (°C)", yfmt: v => v.toFixed(1), minSpan: 4 });
  const row = (k, v) => el("tr", null, el("th", null, k), el("td", null, v));
  setChild(SY.facts, el("table", { class: "wrap" }, el("tbody", null,
    row("dspd API version", b.api_version || "n/a"), row("Board uptime (dspd)", b.uptime_s === null || b.uptime_s === undefined ? "n/a" : fmtAge(b.uptime_s)),
    row("Load average", b.load || "n/a"), row("MemFree", b.memory.memfree_kb === null || b.memory.memfree_kb === undefined ? "n/a" : (b.memory.memfree_kb / 1024).toFixed(1) + " MiB"),
    row("Log partition free", b.memory.fwlog_free_kb === null || b.memory.fwlog_free_kb === undefined ? "n/a" : (b.memory.fwlog_free_kb / 1024).toFixed(0) + " MiB"),
    b.network ? row("Board rx / tx (" + b.network.interface + ")", fmtNum(b.network.per_s && b.network.per_s.rx.bytes, 0) + " / " + fmtNum(b.network.per_s && b.network.per_s.tx.bytes, 0) + " bytes/s") : null,
    b.dsps.map(x => row("DSP " + x.dsp + " version", (x.observed_version || "not read") + " · " + x.role)))),
    tnote(b.memory.note, "About these numbers"));
  const gateNote = artifactGateNote(S.status && S.status.artifact_gate);
  const withGate = node => [node, gateNote ? el("div", { class: "tipnote", id: "artifact-gate-note" }, Tip.tip(gateNote, { label: "About artifact flags" })) : null];
  if (!d.pipeline.plugins.length) setChild(SY.plugins, withGate(emptyNote("analysis/status.json has no plugin table.")));
  else setChild(SY.plugins, withGate( el("table", null, el("thead", null, el("tr", null, ["Plugin", "State", "Records", "Errors", "Last output"].map((h, i) => el("th", { class: i > 1 && i < 4 ? "num" : "" }, h)))),
    el("tbody", null, d.pipeline.plugins.map(p => el("tr", null, el("td", null, p.name), el("td", null, el("span", { class: "pill " + (p.enabled ? "ok" : "warn") }, p.enabled ? "on" : "off")), el("td", { class: "num" }, p.records), el("td", { class: "num" }, p.errors), el("td", null, fmtAge(p.last_t_age_s) + " ago")))))));
  if (!b.monitor) setChild(SY.mon, emptyNote(b.reachable ? "No monitor samples." : bmsg, !b.reachable && !off));
  else {
    const byDsp = {}; for (const x of b.monitor.samples) (byDsp[x.dsp] = byDsp[x.dsp] || {})[x.slot] = x;
    setChild(SY.mon, el("table", null, el("thead", null, el("tr", null, el("th", null, "DSP"), [0, 1, 2, 3, 4, 5, 6].map(k => el("th", { class: "num" }, "slot " + k)))),
      el("tbody", null, Object.keys(byDsp).map(k => el("tr", null, el("td", null, k), [0, 1, 2, 3, 4, 5, 6].map(c => el("td", { class: "num" }, byDsp[k][c] ? String(byDsp[k][c].mean_square_raw) : "-")))))),
      tnote(b.monitor.note + (b.monitor.stale ? " The monitor log is STALE." : ""), "About the raw monitor"), b.monitor.stale ? el("div", { class: "note-warn" }, Tip.badge("STALE", "warn", "The monitor log is STALE.")) : null);
  }
}

// ------------------------------------------------------------------------------------------------ loop
const TABS = { overview: [buildOverview, refreshOverview], detections: [buildDetections, refreshDetections], beams: [buildBeams, refreshBeams], system: [buildSystem, refreshSystem], review: [() => Review.build(), () => Review.refresh()] };
const built = {};
function show(tab) {
  if (!TABS[tab]) tab = "overview";
  S.tab = tab;
  for (const k of Object.keys(TABS)) document.getElementById("tab-" + k).hidden = k !== tab;
  for (const b of document.querySelectorAll("#nav button")) b.setAttribute("aria-pressed", String(b.dataset.tab === tab));
  if (!built[tab]) { TABS[tab][0](); built[tab] = true; if (tab === "detections") renderDetBar(); }
  refresh();
}
async function refresh() {
  if (document.hidden) return;
  const needHealth = nowS() - S.healthAt > HEALTH_REFRESH_S;                // the 24 h series change slowly: one request a minute, not every 7 s
  const [st, lv, cal, hl] = await Promise.all([api("/api/status"), api("/api/beams/live"), api("/api/calibration"), needHealth ? api("/api/system?" + q({ since: Math.floor(nowS() - 86400) })) : null]);
  S.status = st.ok ? st.data : null; setArtifactFloor(S.status && S.status.artifact_gate); S.live = lv.ok ? lv.data : null; S.cal = cal.ok ? cal.data : null;
  if (hl) { S.health = hl; S.healthAt = nowS(); }
  if (!st.ok) document.getElementById("strip").replaceChildren(card("Dashboard", "server error", st.error, "bad")); else renderStrip();
  try { await TABS[S.tab][1](); } catch (e) { /* a chart error must not stop the refresh loop */ }
}
function applyHashParams() {                                              // #/detections?out=1&nonsp=1 : shareable initial filter state
  const m = /\?(.*)$/.exec(location.hash || ""), ps = new URLSearchParams(m ? m[1] : "");
  if (ps.get("out") === "1") S.det.showOut = true;
  if (ps.get("nonsp") === "1") S.det.showNon = true;
  if (ps.get("group") && (GROUP_KEYS.includes(ps.get("group")) || ps.get("group") === "bio")) S.det.group = ps.get("group");
}
function start() {
  applyHashParams();
  document.getElementById("help-slot").append(Tip.tip(HELP_TEXT, { label: "About this dashboard" }));
  for (const b of document.querySelectorAll("#nav button")) b.addEventListener("click", () => { location.hash = "#/" + b.dataset.tab; });
  window.addEventListener("hashchange", () => show(location.hash.replace("#/", "").split("?")[0]));
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  let rt; window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(refresh, 250); });
  setInterval(refresh, 7000);
  show((location.hash.replace("#/", "").split("?")[0]) || "overview");
}
if (typeof document !== "undefined" && document.getElementById("nav")) start();
if (typeof module !== "undefined") module.exports = { niceTicks, niceRange, timeTickValues, stableScale, boardErrText, seqColor, polar, wedgePath, fmtAge, fmtNum, percentile, hourOfDayCounts, BEAM_COLORS, heatBucket,
  shadeBefore, shadeBars, shadeCols, slotNote, frameShort, physicalDirs, dirNear, speciesStyles, dotRadius, scatterLanes, shapeAttrs, SPECIES_COLORS, SPECIES_OTHER,
  ringRelative, beamDirLabel, traceByBeam, neighbourSummary, hourDirMatrix, dirHourModel, sparkGeom, stateSummary, DIR_SPAN_DB, MIN_DIR_N, NEIGHBOUR_S, SPARK_GAP_S,
  GROUP_KEYS, GROUPS_BIO, GROUP_COLORS, GROUP_SHAPES, groupName, groupRank, topPerGroup, artifactFlagged, artifactText, artifactCountText, artifactGateNote, setArtifactFloor, S, ARTIFACT_LEVELS, collapseRuns, runText, zoomRange, densityOpacity, densityCells, groupHourly, azName, NOT_STEERABLE_BANDS, STEER_NOTE, isSteerable, patternInfo };
