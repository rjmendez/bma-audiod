#!/usr/bin/env python3
"""Mutation check for static/app.js: each mutation edits one behaviour in a COPY of ui/ and runs the suite that is meant to catch
it (node test_app_js.js for the pure helpers, test_dom_edge.py - real Edge, real server - for the DOM rules); the suite must exit
non-zero. The unmutated copy must pass both first.

    python mutation_js.py [name-substring]        (needs node; the DOM suites need Edge; env MUT_KIND=node|dom|domg|domv and MUT_PART=i/n select a subset)

Exit 0 only if the baselines are green and every mutation was caught."""

import os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "static", "app.js")

N, D, G, V = "node", "dom", "domg", "domv"
SN, ST, SD = (
    "spnode",
    "tips",
    "domspec",
)  # spectrum.js pure core (node); tooltip behaviour in Edge (test_dom_tips.py); spectrum panel in Edge (test_dom_spectrum.py)                  # node pure helpers; the long DOM suite (test_dom_edge.py); the group/pattern/steerable DOM suite (test_dom_groups.py); the direction strip / event detail / multiples / health DOM suite (test_dom_viz.py)
M = [
    (N, "hour of day by UTC hour", "out[new Date(h.hour_s * 1000).getHours()] += h.events", "out[new Date(h.hour_s * 1000).getUTCHours()] += h.events"),
    (N, "heatBucket 36-48 h tier 600 -> 300", "sec <= 3600 * 48 ? 600 : 3600", "sec <= 3600 * 48 ? 300 : 3600"),
    (N, "heatBucket 6 h edge", "sec <= 3600 * 6 ? 60", "sec < 3600 * 6 ? 60"),
    (N, "fmtAge seconds limit 90 -> 60", "if (s < 90) return", "if (s < 60) return"),
    (N, "fmtAge minutes limit 5400 -> 3600", "if (s < 5400) return", "if (s < 3600) return"),
    (N, "fmtAge hours limit 172800 -> 86400", "if (s < 172800) return", "if (s < 86400) return"),
    (N, "polar y sign flipped", "cy - r * Math.cos(t)", "cy + r * Math.cos(t)"),
    (N, "wedge arc sweep flag", '" 0 0 1 " + b[0]', '" 0 0 0 " + b[0]'),
    (N, "niceRange top rounds down", "hi = Math.ceil(max / step - 1e-9) * step", "hi = Math.floor(max / step - 1e-9) * step"),
    (N, "niceRange bottom rounds up", "lo = Math.floor(min / step + 1e-9) * step", "lo = Math.ceil(min / step + 1e-9) * step"),
    (N, "time ticks twice as dense", "span / x <= maxTicks)", "span / x <= maxTicks * 2)"),
    (
        N,
        "multi-day ticks every day",
        "const days = Math.max(1, Math.ceil(span / 86400 / maxTicks));",
        "const days = Math.max(1, Math.floor(span / 86400 / maxTicks));",
    ),
    (N, "time ticks keep ticks before the range", "if (t >= t0) out.push(t);\n    }\n    return out;", "out.push(t);\n    }\n    return out;"),
    (N, "stableScale tolerance edge", "Math.abs(prev.lo - lo) <= tol", "Math.abs(prev.lo - lo) < tol"),
    (N, "stableScale ignores hi", "Math.abs(prev.hi - hi) <= tol", "true"),
    (N, "board disabled text", 'return "Board polling is disabled."', 'return "The board did not answer."'),
    (
        N,
        "http error code echoed",
        'return code ? "The board answered with an error." : "The board is not available.";',
        'return code ? code : "The board is not available.";',
    ),
    (
        N,
        "percentile index",
        "sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))]",
        "sorted[Math.min(sorted.length - 1, Math.ceil(q * sorted.length))]",
    ),
    (N, "negative zero shown", "/^-0(\\.0+)?$/.test(t) ? t.slice(1) : t", "t"),
    (D, "out-of-region rows not greyed", '(x.in_region === false ? " out" : "") + ((x.scientific', '"" + ((x.scientific'),
    (D, "uncalibrated note dropped", '(calibrated ? "" : " Uncalibrated: 0\\u00b0 is the array\'s own axis, not north.")', '""'),
    (D, "Beams/Overview band starts at 2-4 kHz", "band: 6, range", "band: 5, range"),
    (
        D,
        "per-beam bias note dropped",
        " A persistent offset that stays with one beam number when the pattern is rotated is a channel gain difference, not a source.",
        "",
    ),
    (D, "disabled board shown as unreachable", '!bd.enabled ? "disabled" : bd.reachable ?', "bd.reachable ?"),
    (D, "bar value labels dropped", "if (bw >= 24) svg.append(", "if (false) svg.append("),
    (D, "default view includes out-of-region", 'in_region: d.showOut ? "any" : "true"', 'in_region: "any"'),
    (G, "default view includes non-species", 'non_species: d.showNon || d.group === "mechanical" ? "any" : "false"', 'non_species: "any"'),
    (D, "effective hour window not shown", 'setChild(BM.window, tnote("Whole hours: "', 'setChild(BM.window, tnote("Hours: "'),
    (D, "filter bar rebuilt under a focused input", "(sig !== DT.barSig && !DT.bar.contains(document.activeElement))", "(sig !== DT.barSig)"),
    (D, "event paging not anchored", "d.offset > 0 && d.anchor ? d.anchor : Math.ceil(until)", "Math.ceil(until)"),
    (D, "anchor not released on page 1", "if (!d.offset) d.anchor = null; refreshDetections();", "refreshDetections();"),
    (D, "click does not scroll to the timeline", 'DT.tlPanel.scrollIntoView({ block: "nearest", behavior: "smooth" })', "void 0"),
    (D, "selected row not marked", '"aria-selected": String((x.scientific || x.species) === S.selectedSpecies)', '"aria-selected": "false"'),
    (D, "clicked row not marked until the next refresh", 'tr.setAttribute("aria-selected", String(on));', 'tr.setAttribute("aria-selected", "false");'),
    (
        D,
        "system tab prints the raw board code",
        'bmsg = boardErrText(off ? "board_disabled" : Object.values(b.errors)[0])',
        'bmsg = off ? "board_disabled" : Object.values(b.errors)[0]',
    ),
    # ---- beam-slot mapping flag, physical directions, detections scatter
    (N, "physicalDirs RIGHT at 270", '{ label: "RIGHT", az: 90 }', '{ label: "RIGHT", az: 270 }'),
    (N, "physicalDirs returned for every frame", 'return frameKind === "panel" ? [{ label: "UP"', 'return true ? [{ label: "UP"'),
    (N, "dirNear edge 22.5 excluded", "if (diff <= 22.5) return d.label;", "if (diff < 22.5) return d.label;"),
    (N, "dotRadius slope 6 -> 5", "3 + 6 * Math.min(1, Math.max(0, Number(conf) || 0))", "3 + 5 * Math.min(1, Math.max(0, Number(conf) || 0))"),
    (N, "shadeBefore not clamped to 1", "return Math.min(1, Math.max(0, (fixAt - t0) / (t1 - t0)));", "return Math.max(0, (fixAt - t0) / (t1 - t0));"),
    (N, "shadeBars counts the hour starting at the fix", "hourStarts[k] < fixAt", "hourStarts[k] <= fixAt"),
    (N, "shadeCols floor instead of ceil", "Math.ceil((fixAt - t0) / bucketS)", "Math.floor((fixAt - t0) / bucketS)"),
    (N, "scatter lanes in descending azimuth", "x[0] - y[0] || x[1] - y[1]", "y[0] - x[0] || x[1] - y[1]"),
    (N, "species ties follow event order", " || (a < b ? -1 : a > b ? 1 : 0))", " || 0)"),
    (
        N,
        "triangle marker points down",
        'if (shape === "triangle") return ["polygon", { points: f(cx) + "," + f(cy - r)',
        'if (shape === "triangle") return ["polygon", { points: f(cx) + "," + f(cy + r)',
    ),
    (D, "compass marks directions when uncalibrated", "const dirs = physicalDirs(kind), S = ", 'const dirs = physicalDirs("panel"), S = '),
    (D, "unreliable-region note dropped", "function warnNote(text, prefix) { return text ? el(", "function warnNote(text, prefix) { return false ? el("),
    (
        D,
        "hourly chart hatch not drawn",
        'if (o.shadeBars > 0) svg.append(sv("rect", { class: "hatch-region", x: m.l, y: m.t, width: Math.min(n, o.shadeBars) * bw, height: ph, fill: hatchDefs(svg) }, sv("title", null, o.shadeNote || "unreliable")));\n  svg.append(sv("line", { class: "axis", x1: m.l, x2: m.l + pw, y1: zero,',
        'if (false) svg.append(sv("rect", { class: "hatch-region", x: m.l, y: m.t, width: Math.min(n, o.shadeBars) * bw, height: ph, fill: hatchDefs(svg) }, sv("title", null, o.shadeNote || "unreliable")));\n  svg.append(sv("line", { class: "axis", x1: m.l, x2: m.l + pw, y1: zero,',
    ),
    (D, "scatter hatch not drawn", 'if (frac > 0) svg.append(sv("rect"', 'if (false) svg.append(sv("rect"'),
    (D, "heatmap note not in the aria-label", '(o.shadeCols > 0 ? ". " + o.shadeNote : "")', '""'),
    (D, "heatmap note below the chart dropped", "setChild(host, cv, o.shadeCols > 0 ? warnNote(o.shadeNote) : null);", "setChild(host, cv);"),
    (
        D,
        "scatter marker shape dropped",
        "shapeAttrs(sty.shape, cx, cy, dotRadius(e.max_confidence))",
        'shapeAttrs("circle", cx, cy, dotRadius(e.max_confidence))',
    ),
    (D, "scatter out-of-region not faded", '"fill-opacity": out ? 0.35 : 0.85', '"fill-opacity": 0.85'),
    (D, "scatter unreliable tooltip dropped", '(e.slot_mapping_unreliable ? " (slot mapping unreliable)" : "")', '""'),
    (D, "scatter lane direction word dropped", '(d ? " (" + d.toLowerCase() + ")" : "")', '""'),
    (N, "speciesStyles counts in a plain object", "const cnt = Object.create(null);", "const cnt = {};"),
    (N, "speciesStyles styles in a plain object", "by = Object.create(null), legend = [];\n  names.forEach", "by = {}, legend = [];\n  names.forEach"),
    (
        N,
        "slotNote drops the UTC instant",
        '" local (" + p(u.getUTCHours()) + ":" + p(u.getUTCMinutes()) + " UTC on " + (u.getUTCMonth() + 1) + "/" + p(u.getUTCDate()) + ") the beam slots',
        '" local the beam slots',
    ),
    (N, "frameShort compass named as the fallback", 'kind === "compass" ? "compass bearings" : ', ""),
    (
        N,
        "physicalDirs drawn for compass frames",
        'return frameKind === "panel" ? [{ label: "UP"',
        'return frameKind === "panel" || frameKind === "compass" ? [{ label: "UP"',
    ),
    # ---- taxonomic groups, density, zoom, pattern card, steerable bands (ui-http 1.2.0)
    (N, "collapseRuns gap < instead of <=", "r.t_first - e.t_end_s <= gapS", "r.t_first - e.t_end_s < gapS"),
    (N, "collapseRuns never open-ended", "r.open_ended = !!truncated && r.t_first - oldest <= gapS", "r.open_ended = false"),
    (N, "collapseRuns open-end boundary < instead of <=", "r.t_first - oldest <= gapS", "r.t_first - oldest < gapS"),
    (N, "collapseRuns merges by common name only", "const key = e.scientific || e.species,", "const key = e.species,"),
    (
        N,
        "collapseRuns keeps the first beam not the best",
        "r.max_confidence = e.max_confidence; r.best_beam = e.best_beam;",
        "r.max_confidence = e.max_confidence;",
    ),
    (N, "topPerGroup keeps k-1", "if (seen[g] <= k) out.push(s);", "if (seen[g] < k) out.push(s);"),
    (N, "topPerGroup drops unknown groups", 'return i < 0 ? GROUP_KEYS.indexOf("other-bio") : i;', "return i;"),
    (
        N,
        "topPerGroup groups in input order",
        "for (const g of GROUP_KEYS) for (const s of list)",
        "for (const g of GROUP_KEYS.slice().reverse()) for (const s of list)",
    ),
    (N, "zoomRange ignores the full toggle", 'if (full || typeof first !== "number"', 'if (typeof first !== "number"'),
    (N, "zoomRange margin 5 % -> 10 %", "Math.max(60, span * 0.05)", "Math.max(60, span * 0.1)"),
    (N, "zoomRange minimum margin 60 -> 30", "Math.max(60, span * 0.05)", "Math.max(30, span * 0.05)"),
    (
        N,
        "zoomRange not clamped to the range",
        "const a = Math.max(since, first - pad), b = Math.min(until, last + pad);",
        "const a = first - pad, b = last + pad;",
    ),
    (N, "zoomRange single instant margin 900 -> 450", "span === 0 ? 900 :", "span === 0 ? 450 :"),
    (N, "densityOpacity floor 0.2 -> 0.1", "(0.2 + 0.8 * Math.log1p(count)", "(0.1 + 0.8 * Math.log1p(count)"),
    (N, "densityOpacity linear instead of log", "0.8 * Math.log1p(count) / Math.log1p(max)", "0.8 * count / max"),
    (N, "densityCells keeps the last row count only", "c.events += r.events; c.species.set", "c.events = r.events; c.species.set"),
    (N, "densityCells merges groups", 'const k = r.t_s + "|" + r.best_beam + "|" + r.group,', 'const k = r.t_s + "|" + r.best_beam,'),
    (N, "groupHourly counts hours outside the window", "if (idx.has(h.hour_s)) { row.values", "if (true) { row.values"),
    (N, "groupHourly keeps empty groups", "return GROUP_KEYS.filter(g => acc[g] && acc[g].total > 0)", "return GROUP_KEYS.filter(g => acc[g])"),
    (N, "speciesStyles uses group mode for a single group", "if (gkeys.length > 1) {", "if (gkeys.length > 0) {"),
    (N, "group shapes: amphibian shares the bird circle", 'amphibian: "triangle"', 'amphibian: "circle"'),
    (N, "cross marker is a plain square", 'if (shape === "cross") {', 'if (shape === "square") {'),
    (N, "azName tie goes to the diagonal", "(lo % 2 === 0 ? lo : lo + 1)", "lo + 1"),
    (
        N,
        "azName ties round half up (22.5 becomes up-right)",
        "f < 0.5 - 1e-9 ? lo : f > 0.5 + 1e-9 ? lo + 1 : (lo % 2 === 0 ? lo : lo + 1)",
        "f < 0.5 ? lo : lo + 1",
    ),
    (N, "azName names any frame, not only the panel frame", 'if (frameKind !== "panel" || typeof az', "if (typeof az"),
    (N, "azName wraps at 360 without normalising", "(((az % 360) + 360) % 360) / 45", "(az % 360) / 45"),
    (N, "not steerable bands only 50-125 Hz", "const NOT_STEERABLE_BANDS = [0, 1];", "const NOT_STEERABLE_BANDS = [0];"),
    (N, "not steerable bands include 250-500 Hz", "const NOT_STEERABLE_BANDS = [0, 1];", "const NOT_STEERABLE_BANDS = [0, 1, 2];"),
    (
        N,
        "patternInfo: applied=true and false swapped",
        "else if (bd && bd.beams_applied_by_dspd === true) {",
        "else if (bd && bd.beams_applied_by_dspd === false) {",
    ),
    (
        N,
        "patternInfo: unreadable state reported as not applied",
        'else { value = "not readable"; sub = "the board state could not be read, so the applied pattern is unknown"; }',
        'else { value = "not applied"; sub = "dspd reports no applied pattern"; }',
    ),
    (N, "patternInfo: fallback azimuths presented as stored", "if (cal && cal.calibrated && Array.isArray", "if (cal && Array.isArray"),
    (N, "patternInfo: provenance of the directions dropped", "Beam directions from the stored calibration profile (", "Beam directions ("),
    (G, "pattern card reads the live payload (no measured_at / provisional)", "pi = patternInfo(bd, S.cal || S.live);", "pi = patternInfo(bd, S.live);"),
    (G, "group chip aria-pressed not set", '"aria-pressed": String(d.group === k), "data-group": k', '"aria-pressed": "false", "data-group": k'),
    (G, "group filter not sent to the API", 'group: d.showNon && d.group === "bio" ? "any" : d.group }, extra || {});', 'group: "any" }, extra || {});'),
    (G, "non-biological chip does not widen the class filter", 'd.showNon || d.group === "mechanical" ? "any" : "false"', 'd.showNon ? "any" : "false"'),
    (
        G,
        "chip click keeps the old species selection",
        'd.group = k; d.offset = 0; d.species = ""; renderDetBar();',
        "d.group = k; d.offset = 0; renderDetBar();",
    ),
    (G, "scatter truncates to the newest 500 instead of aggregating", "if (e.total <= SCATTER_CAP) {", "if (true) {"),
    (G, "density counts not shown in the cell title", 'c.events + " event(s) ("', '"(" + c.events + " event(s) ("'),
    (G, "zoom ignored (always the full range)", "zoomRange(first, last, e.since_s, e.until_s, d.full);", "zoomRange(first, last, e.since_s, e.until_s, true);"),
    (G, "full range toggle does not flip", "d.full = !d.full; refreshScatter();", "d.full = d.full; refreshScatter();"),
    (
        G,
        "full range toggle has no aria-pressed",
        'el("button", { "aria-pressed": String(d.full), onclick: () => { d.full = !d.full;',
        'el("button", { onclick: () => { d.full = !d.full;',
    ),
    (
        G,
        "events table Group column dropped",
        'el("td", null, groupName(x.group)),\n        el("td", null, "B" + x.best_beam',
        'el("td", null, "B" + x.best_beam',
    ),
    (G, "continuous caller badge dropped", 's.chorus ? el("span", { class: "badge"', 'false ? el("span", { class: "badge"'),
    (G, "overview top species back to the first 8", "const top = topPerGroup(sp.data.species, 3);", "const top = sp.data.species.slice(0, 8);"),
    (G, "overview recent not collapsed", "collapseRuns(ev.data.events, 120, ev.data.total", "collapseRuns(ev.data.events, -1, ev.data.total"),
    (
        G,
        "overview recent hides that only 500 events were fetched",
        '(ev.data.total > ev.data.events.length ? "; only the newest "',
        '(false ? "; only the newest "',
    ),
    (G, "activity by group: own scale uses one shared maximum", "(0.15 + 0.85 * v / mx).toFixed(3)", "(0.15 + 0.85 * v / 1000).toFixed(3)"),
    (
        G,
        "activity by group: stacked chart has no hatch",
        'if (o.shadeBars > 0) svg.append(sv("rect", { class: "hatch-region", x: m.l, y: m.t, width: Math.min(n, o.shadeBars) * bw, height: ph, fill: hatchDefs(svg) }, sv("title", null, o.shadeNote || "unreliable")));\n  svg.append(sv("line", { class: "axis", x1: m.l, x2: m.l + pw, y1: m.t + ph, y2: m.t + ph })',
        'if (false) svg.append(sv("rect", { class: "hatch-region", x: m.l, y: m.t, width: Math.min(n, o.shadeBars) * bw, height: ph, fill: hatchDefs(svg) }, sv("title", null, o.shadeNote || "unreliable")));\n  svg.append(sv("line", { class: "axis", x1: m.l, x2: m.l + pw, y1: m.t + ph, y2: m.t + ph })',
    ),
    (G, "unclassified labels line dropped", "setChild(DT.unc, sp.data.unclassified_total > 0 ?", "setChild(DT.unc, false ?"),
    (G, "not steerable note dropped", "setChild(BM.steer, isSteerable(S.band) ? null :", "setChild(BM.steer, true ? null :"),
    (G, "not steerable bias bars not hatched", "shadeBars: s.steerable === false ? 8 : 0", "shadeBars: 0"),
    (G, "not steerable contrast rows not marked", '(s.steerable === false ? " (not steerable)" : "")', '""'),
    (
        G,
        "activity by group: own-scale strips have no hatch",
        'if (o.shadeBars > 0) svg.append(sv("rect", { class: "hatch-region", x: m.l, y: m.t, width: Math.min(n, o.shadeBars) * bw, height: rows.length * rh, fill: hatchDefs(svg) }',
        'if (false) svg.append(sv("rect", { class: "hatch-region", x: m.l, y: m.t, width: Math.min(n, o.shadeBars) * bw, height: rows.length * rh, fill: hatchDefs(svg) }',
    ),
    (G, "not steerable note missing under the heatmap", "if (o.note) host.append(", "if (false) host.append("),
    (G, "not steerable note missing in the heatmap aria-label", '(o.note ? ". " + o.note : "")', '""'),
    (G, "not steerable note missing on the compass", "if (!isSteerable(S.band)) host.append(", "if (false) host.append("),
    # ---- direction strip, event detail, hour x direction multiples, health sparklines (ui-http 1.3.0, Stage B)
    (
        N,
        "ringRelative uses the arithmetic mean of dB",
        "const ring = 10 * Math.log10(vs.reduce((a, v) => a + Math.pow(10, v / 10), 0) / vs.length);",
        "const ring = vs.reduce((a, v) => a + v, 0) / vs.length;",
    ),
    (N, "ringRelative sign flipped", "Math.round((r[c] - ring) * 100) / 100", "Math.round((ring - r[c]) * 100) / 100"),
    (N, "ringRelative fills an empty column with 0", "if (!vs.length) continue;", "if (!vs.length) { out.forEach(r => { r[c] = 0; }); continue; }"),
    (N, "ringRelative does not round to 0.01 dB", "Math.round((r[c] - ring) * 100) / 100", "(r[c] - ring)"),
    (N, "beamDirLabel prints null azimuths", 'if (az === null || az === undefined) return "B" + beam;', 'if (false) return "B" + beam;'),
    (N, "beamDirLabel names every frame", 'const nm = azName(az, kind);\n  return "B" + beam', 'const nm = azName(az, "panel");\n  return "B" + beam'),
    (N, "beamDirLabel drops the degree sign", 'return "B" + beam + " " + az + "°" + (nm', 'return "B" + beam + " " + az + (nm'),
    (N, "traceByBeam points not time ordered", "b.points.sort((x, y) => x.t - y.t);", "b.points.sort((x, y) => 0);"),
    (N, "traceByBeam beams not in beam order", ".sort((x, y) => x.beam - y.beam);\n}", ".sort((x, y) => 0);\n}"),
    (N, "traceByBeam max confidence is the last report", "b.max_conf = Math.max(b.max_conf, r.confidence);", "b.max_conf = r.confidence;"),
    (N, "traceByBeam max level is the last level", "(b.max_level === null || r.level_dbfs > b.max_level)", "(true)"),
    (N, "neighbourSummary keeps the event itself", "if (e.id === self.id) continue;", "if (false) continue;"),
    (N, "neighbourSummary ties not broken by event count", "b.events - a.events || (a.species", "0 || (a.species"),
    (
        N,
        "neighbourSummary nearest is the smallest signed dt",
        "if (Math.abs(dt) < Math.abs(r.nearest_dt)) r.nearest_dt = dt;",
        "if (dt < r.nearest_dt) r.nearest_dt = dt;",
    ),
    (
        N,
        "neighbourSummary dt from the event start only",
        "e.t_start_s > self.t_end_s ? e.t_start_s - self.t_end_s : 0",
        "e.t_start_s > self.t_end_s ? e.t_start_s - self.t_start_s : 0",
    ),
    (
        N,
        "neighbourSummary merges species by common name only",
        "key = e.scientific || e.species;\n    const r = by.get(key)",
        "key = e.species;\n    const r = by.get(key)",
    ),
    (N, "neighbourSummary window 60 -> 30 s", "NEIGHBOUR_S = 60,", "NEIGHBOUR_S = 30,"),
    (N, "hourDirMatrix by UTC hour", "m[new Date(r.t_s * 1000).getHours()][r.best_beam - 1]", "m[new Date(r.t_s * 1000).getUTCHours()][r.best_beam - 1]"),
    (N, "hourDirMatrix keyed by species name only", "(r.scientific || r.species) === key) m[", "r.species === key) m["),
    (N, "hourDirMatrix overwrites instead of adding", "[r.best_beam - 1] += r.events;", "[r.best_beam - 1] = r.events;"),
    (N, "dirHourModel readable needs more than the minimum", "readable: total >= minN", "readable: total > minN"),
    (N, "dirHourModel shares not of the hour's own total", "shares: row.map(v => total ? v / total : 0)", "shares: row.map(v => v / 100)"),
    (N, "dirHourModel peak is the last busiest hour", "x.total > hours[bi].total ? i : bi", "x.total >= hours[bi].total ? i : bi"),
    (N, "dirHourModel minimum 5 -> 4", "MIN_DIR_N = 5,", "MIN_DIR_N = 4,"),
    (N, "sparkGeom y not inverted", "(1 - (p[1] - lo) / (hi - lo))", "((p[1] - lo) / (hi - lo))"),
    (
        N,
        "sparkGeom draws a line across a gap",
        "if (p[1] === null || p[1] === undefined) { pen = false; continue; }",
        "if (p[1] === null || p[1] === undefined) { continue; }",
    ),
    (N, "sparkGeom flat series on the bottom edge", "if (o.lo === undefined) lo -= 1; }", "}"),
    (N, "sparkGeom ignores the fixed lower limit", "let lo = o.lo !== undefined ? o.lo : Math.min(...vs)", "let lo = Math.min(...vs)"),
    (N, "stateSummary sample held until the next one however long", "Math.min(s[i + 1].t_s, x.t_s + SPARK_GAP_S)", "s[i + 1].t_s"),
    (N, "stateSummary hold 90 -> 91 s", "SPARK_GAP_S = 90,", "SPARK_GAP_S = 91,"),
    (
        N,
        "stateSummary runs not merged",
        "if (last && last.state === state && last.t1 === a) last.t1 = b; else runs.push",
        "if (false) last.t1 = b; else runs.push",
    ),
    (N, "stateSummary no tail of no-data", 'add("no data", cur, t1);\n  const total', "const total"),
    (N, "stateSummary does not clip to the start", "a = Math.max(x.t_s, t0);", "a = x.t_s;"),
    (
        V,
        "direction strip rows in beam order",
        "const order = scatterLanes(az), rel = ringRelative(rows);",
        "const order = [1, 2, 3, 4, 5, 6, 7, 8], rel = ringRelative(rows);",
    ),
    (
        V,
        "direction strip shows raw levels, not relative to the ring",
        "heatmap(BM.dir, order.map(b => rel[b - 1]),",
        "heatmap(BM.dir, order.map(b => rows[b - 1]),",
    ),
    (
        V,
        "direction strip has no not-steerable note",
        'note: nst ? STEER_NOTE : null, stamp: nst ? NOT_STEERABLE_STAMP : null, empty: "No beam windows in this range: the beam-band log has no data for it." });\n    }',
        'note: null, stamp: null, empty: "No beam windows in this range: the beam-band log has no data for it." });\n    }',
    ),
    (
        V,
        "low band buttons carry no not-steerable title",
        "title: isSteerable(i) ? null : STEER_NOTE, onclick: () => { S.band = i; onPick(); }",
        "onclick: () => { S.band = i; onPick(); }",
    ),
    (
        V,
        "direction names invented in the DSP frame",
        'const nm = azName(az, kind);\n  return "B" + beam',
        'const nm = azName(az, "panel");\n  return "B" + beam',
    ),
    (
        V,
        "event row click does not open the detail",
        "pickSpecies(x.scientific || x.species, true); openEvent(x.id); }, onkeydown",
        "pickSpecies(x.scientific || x.species, true); }, onkeydown",
    ),
    (V, "event row not operable by keyboard", 'if (ev2.key === "Enter" || ev2.key === " ") {', "if (false) {"),
    (V, "event row not focusable", 'tabindex: "0", title: "Show the event detail"', 'title: "Show the event detail"'),
    (
        V,
        "open event not marked current",
        'if (tr.getAttribute("data-eid") === id) tr.setAttribute("aria-current", "true"); else',
        'if (false) tr.setAttribute("aria-current", "true"); else',
    ),
    (V, "neighbours window starts 5 s before the event", "since: (ev.t_start_s - NEIGHBOUR_S).toFixed(3)", "since: (ev.t_start_s - 5).toFixed(3)"),
    (V, "neighbours window reaches 600 s after the event", "until: (ev.t_end_s + NEIGHBOUR_S + 0.001).toFixed(3)", "until: (ev.t_end_s + 600).toFixed(3)"),
    (
        V,
        "neighbour species written as HTML",
        'el("tr", null, el("td", null, r.species), el("td", null, groupName(r.group)), el("td", { class: "num" }, r.events),',
        'el("tr", null, (() => { const t = el("td"); t.innerHTML = r.species; return t; })(), el("td", null, groupName(r.group)), el("td", { class: "num" }, r.events),',
    ),
    (V, "trace markers all circles", "shapeAttrs(BEAM_SHAPES[b.beam - 1], p[0], p[1], 4.5)", 'shapeAttrs("circle", p[0], p[1], 4.5)'),
    (V, "trace x position in absolute seconds", "fmtNum(p[2].t - t0, 2)", "fmtNum(p[2].t, 2)"),
    (V, "trace has no connecting lines", 'if (pts.length > 1) svg.append(sv("path"', 'if (false) svg.append(sv("path"'),
    (V, "level chart drawn without any level", "if (hasLevel) traceChart(lvlHost", "if (true) traceChart(lvlHost"),
    (V, "close does not hide the panel", "S.detailId = null; DT.detail.hidden = true; for", "S.detailId = null; for"),
    (V, "any error reads as event gone", "/not.found/i.test(r.error) ?", "true ?"),
    (V, "multiples: top 6 species overall", "top = topPerGroup(list, 2), order = scatterLanes(az);", "top = list.slice(0, 6), order = scatterLanes(az);"),
    (
        V,
        "multiples: rows in beam order",
        "top = topPerGroup(list, 2), order = scatterLanes(az);",
        "top = topPerGroup(list, 2), order = [1, 2, 3, 4, 5, 6, 7, 8];",
    ),
    (V, "multiples: cell shade is the count, not the share", "(0.06 + 0.94 * h.shares[b - 1]).toFixed(3)", "(0.06 + 0.94 * n).toFixed(3)"),
    (V, "multiples: hours with too few events drawn as readable", "if (h.readable) svg.append", "if (true) svg.append"),
    (V, "multiples: hour totals on a shared scale", "h.total / model.max_total * bh", "h.total / 100 * bh"),
    (V, "health refresh not throttled", "const needHealth = nowS() - S.healthAt > HEALTH_REFRESH_S;", "const needHealth = true;"),
    (V, "health state strip: stall drawn as thin as ok", 'ok ? { y: h - 4, height: 4, fill: "var(--good)" }', 'ok ? { y: 0, height: h, fill: "var(--good)" }'),
    (
        V,
        "health loss card shows the maximum as the mean",
        'card("Loss, 24 h", fmtNum(s.loss_pct_mean, 2) + " % mean"',
        'card("Loss, 24 h", fmtNum(lg.max, 2) + " % mean"',
    ),
    (V, "health loss line scaled from its own minimum", "{ lo: 0, t0: t0, t1: t1 });\n  out.push(lg.n", "{ t0: t0, t1: t1 });\n  out.push(lg.n"),
    (V, "health state card never warns after a stall", '"bad" : sum.other_s > 0 ? "warn" : "")', '"bad" : "")'),
    (
        V,
        "health temperature card shows the remote sensor",
        'card("Board temp, 24 h", fmtNum(tl.last, 1) + " °C local"',
        'card("Board temp, 24 h", fmtNum(rl, 1) + " °C local"',
    ),
    # ---- tooltips: prose behind info buttons (test_dom_edge.py counts visible prose on every tab), badges, and the Edge/CDP behaviour suite
    (
        D,
        "dynamic notes shown as visible prose again",
        'function tnote(text, label) { return el("div", { class: "tipnote" }, Tip.tip(text, { label: label || "More information" })); }',
        'function tnote(text, label) { return el("div", { class: "muted" }, text); }',
    ),
    (
        D,
        "a static panel explanation shown as visible prose again",
        'ptitle("Last 6 h: top species and where from", "',
        'el("h2", null, "Last 6 h: top species and where from"), el("div", { class: "muted" }, "',
    ),
    (D, "unreliable-region badge text changed", '"Unreliable region" : prefix.replace', '"Warning" : prefix.replace'),
    (ST, "Escape does not close the tooltip", 'if (e.key === "Escape" && T.openBtn)', 'if (e.key === "Escapex" && T.openBtn)', "tip.js"),
    (ST, "keyboard focus does not open the tooltip", 'if (b && b.matches(":focus-visible")) T.open(b, false);', "if (false) T.open(b, false);", "tip.js"),
    (ST, "hover opens without a delay", "DELAY: 300", "DELAY: 0", "tip.js"),
    (ST, "a tap elsewhere does not close", "if (T.openBtn && !bubOf(e.target)) T.close();", "if (false) T.close();", "tip.js"),
    (ST, "several tooltips can be open at once", "if (T.openBtn && T.openBtn !== btn) T.close();", "if (false) T.close();", "tip.js"),
    (ST, "aria-describedby not set", 'btn.setAttribute("aria-describedby", id); ', "", "tip.js"),
    (
        ST,
        "tip text parsed as HTML",
        'const bub = mk("span", "tip-bubble", String(text));',
        'const bub = mk("span", "tip-bubble"); bub.innerHTML = String(text);',
        "tip.js",
    ),
    (
        ST,
        "bubble not clamped to the viewport",
        "let x = Math.min(Math.max(m, r.left + r.width / 2 - w / 2), Math.max(m, vw - w - m));",
        "let x = r.left + r.width / 2 - w / 2;",
        "tip.js",
    ),
    (
        ST,
        "a second tap does not close",
        "if (T.openBtn === b) T.close(); else { T._hover = false; T.open(b, true); }",
        "T._hover = false; T.open(b, true);",
        "tip.js",
    ),
    (ST, "leaving the button does not close a hover tooltip", "T._closeTimer = setTimeout(T.close, T.CLOSE_DELAY);", ";", "tip.js"),
    (ST, "aria-expanded never set true", 'btn.setAttribute("aria-expanded", "true"); T.openBtn = btn;', "T.openBtn = btn;", "tip.js"),
    # ---- spectrum.js: pure core (node)
    (SN, "persistence decay too slow (tau doubled)", "Math.exp(-dt / this.tau)", "Math.exp(-dt / (2 * this.tau))", "spectrum.js"),
    (SN, "persistence smear 0.25 -> 0.5", "const GAIN = 8, SMEAR = 0.25;", "const GAIN = 8, SMEAR = 0.5;", "spectrum.js"),
    (SN, "viridis first entry changed", 'viridis: "440154', 'viridis: "440155', "spectrum.js"),
    (SN, "log axis uses linear frequencies", "fmin * Math.pow(fmax / fmin, t)", "fmin + (fmax - fmin) * t", "spectrum.js"),
    (SN, "band edge upper bound inclusive", "f >= edges[n]) return -1;", "f > edges[n]) return -1;", "spectrum.js"),
    (SN, "colour index not clamped", "Math.round(Math.min(1, Math.max(0, f)) * 255)", "Math.round(f * 255)", "spectrum.js"),
    (
        SN,
        "loudest beam by mean dB, not power",
        "acc[b] += Math.pow(10, (r.levels[b] * step + floor) / 10);",
        "acc[b] += (r.levels[b] * step + floor);",
        "spectrum.js",
    ),
    (SN, "loudest beam ignores the time window", "if (!r.levels || r.t < nowS - windowS) continue;", "if (!r.levels) continue;", "spectrum.js"),
    (SN, "auto range top percentile 99 -> 90", "pct(0.99)", "pct(0.9)", "spectrum.js"),
    (SN, "intensity gain 8 -> 4", "const GAIN = 8, SMEAR", "const GAIN = 4, SMEAR", "spectrum.js"),
    (SN, "fmtNum strips trailing zeros of integers", 't.indexOf(".") < 0 ? t : t.replace(/\\.?0+$/, "")', 't.replace(/\\.?0+$/, "")', "spectrum.js"),
    (
        SN,
        "row revision appended instead of replacing",
        "if (i > 0 && a[i - 1].idx === row.idx) a[i - 1] = row; else a.splice(i, 0, row);",
        "a.splice(i, 0, row);",
        "spectrum.js",
    ),
    (SN, "weighted hit ignores the weight", "H[k + v] += w;", "H[k + v] += 1;", "spectrum.js"),
    (SN, "summary median is the quietest band", "sorted[sorted.length >> 1]", "sorted[0]", "spectrum.js"),
    (SN, "summary: auth_required text dropped", 'if (o.state === "auth_required") return "Sign in to see the live spectrum.";', "", "spectrum.js"),
    # ---- spectrum panel in Edge
    (SD, "auth_required: pill text 'sign in required' lost", 'auth_required: "sign in required", unavailable', "unavailable", "spectrum.js"),
    (SD, "auth_required: tooltip button never shown", 'authTip.hidden = S.state !== "auth_required";', "authTip.hidden = true;", "spectrum.js"),
    (SD, "auth_required: client keeps polling at 500 ms", 'if (b.state === "auth_required") S.pollMs = AUTH_REQUIRED_POLL_MS;', "", "spectrum.js"),
    (SD, "auth_required: tooltip no longer says who may see it", "is shown only to a signed-in user, from any computer.", "is shown.", "spectrum.js"),
    (
        SD,
        "gated rows have no hatch",
        "if (row.gated) { for (let x = (8 - (y % 8)) % 8; x < pw; x += 8) px[o + x] = G.fg32; gatedY.push(y); }",
        "if (row.gated) { gatedY.push(y); }",
        "spectrum.js",
    ),
    (
        SD,
        "gated rows have no marker strip",
        "g.fillStyle = G.bad; for (const y of gatedY) g.fillRect(G.x0 - 7, G.py0 + y, 5, 1);",
        "for (const y of gatedY) { }",
        "spectrum.js",
    ),
    (SD, "speech-suppressed pill never shown", "gate.hidden = !g;", "gate.hidden = true;", "spectrum.js"),
    (SD, "server state ignored (always live)", "S.state = b.state; S.gatedNow", 'S.state = "live"; S.gatedNow', "spectrum.js"),
    (SD, "freeze does not stop polling", "if (isActive() && !S.frozen) { poll().then(sched); }", "if (isActive()) { poll().then(sched); }", "spectrum.js"),
    (
        SD,
        "resume does not drop the frozen rows",
        "if (!v) { S.since = 0; S.limit = 1; queue.length = 0; ensureFrame(); }",
        "if (!v) { ensureFrame(); }",
        "spectrum.js",
    ),
    (SD, "429 does not back off", 'S.pollMs = Math.max(1000, 1000 * (+(r.headers && r.headers.get("Retry-After")) || 1));', "S.pollMs = 500;", "spectrum.js"),
    (
        SD,
        "polls while the tab is hidden",
        "return host.isConnected && !doc.hidden && !(tab && tab.hidden);",
        "return host.isConnected && !doc.hidden;",
        "spectrum.js",
    ),
    (
        SD,
        "reduced motion ignored",
        'reduced: !!(win.matchMedia && win.matchMedia("(prefers-reduced-motion: reduce)").matches)',
        "reduced: false",
        "spectrum.js",
    ),
    (
        SD,
        "axis select ignored",
        "if (S.edges) G.colBand = Core.colBands(S.edges, G.pw, S.axis);",
        'if (S.edges) G.colBand = Core.colBands(S.edges, G.pw, "log");',
        "spectrum.js",
    ),
    (SD, "Loudest mode never switches beam", "if (best && best !== S.beam) {", "if (false) {", "spectrum.js"),
    (SD, "colour map select ignored", "G.lut = Core.cmapLut(S.cmap);", 'G.lut = Core.cmapLut("viridis");', "spectrum.js"),
    (SD, "detail option never offered", "if (r.ok) { S.detailOk = true; tierField.hidden = false; }", "if (r.ok) { S.detailOk = true; }", "spectrum.js"),
    (
        SD,
        "waterfall newest row at the bottom",
        "store.nearest(tTop - (y + 0.5) * S.span / wh, maxDt)",
        "store.nearest(tTop - (wh - y - 0.5) * S.span / wh, maxDt)",
        "spectrum.js",
    ),
    (SD, "peak hold line not drawn", "if (S.hold && pers.hasPeak) for", "if (false) for", "spectrum.js"),
    (SD, "compare strip beams reversed", "const c = lut[vm[row.levels[b]]], o0", "const c = lut[vm[row.levels[7 - b]]], o0", "spectrum.js"),
    (SD, "custom dB range ignored", "if (hi - lo >= 5) { S.lo = lo; S.hi = hi; }", "if (hi - lo >= 5) { }", "spectrum.js"),
    (SD, "frame rate capped at 5 fps", "S.fps = ms > 25 ? 15 : 30;", "S.fps = 5;", "spectrum.js"),
    (
        SD,
        "live pill dot does not pulse",
        '.sp-pill[data-state="live"]::before { animation: sp-pulse 1.6s ease-in-out infinite; }',
        '.sp-pill[data-state="live"]::before { }',
        "app.css",
    ),
]


FILES = {"app.js", "spectrum.js", "tip.js", "app.css"}


def run(workdir, kind):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    ui = os.path.join(workdir, "ui")
    if kind == N:
        p = subprocess.run(["node", os.path.join(ui, "test_app_js.js")], capture_output=True, text=True, env=env, timeout=120)
    elif kind == SN:
        p = subprocess.run(["node", os.path.join(ui, "test_spectrum_js.js")], capture_output=True, text=True, env=env, timeout=120)
    elif kind == ST:
        p = subprocess.run([sys.executable, os.path.join(ui, "test_dom_tips.py")], capture_output=True, text=True, env=env, timeout=1800)
    elif kind == SD:
        p = subprocess.run([sys.executable, os.path.join(ui, "test_dom_spectrum.py")], capture_output=True, text=True, env=env, timeout=1800)
    elif kind == V:
        p = subprocess.run([sys.executable, os.path.join(ui, "test_dom_viz.py")], capture_output=True, text=True, env=env, timeout=1800)
    elif kind == G:
        p = subprocess.run([sys.executable, os.path.join(ui, "test_dom_groups.py")], capture_output=True, text=True, env=env, timeout=1800)
    else:
        p = subprocess.run([sys.executable, os.path.join(ui, "test_dom_edge.py")], capture_output=True, text=True, env=env, timeout=1800)
    lines = p.stdout.splitlines()
    return p.returncode, [ln for ln in lines if ln.startswith("FAIL")], (lines[-1] if lines else "(no output)")


def main():
    only = sys.argv[1].split("|") if len(sys.argv) > 1 else None  # name substrings separated by |
    kind_only = os.environ.get("MUT_KIND")  # "node" or "dom": run only that kind (the DOM suite takes minutes per mutant)
    part = os.environ.get("MUT_PART")  # "i/n": run every n-th mutant starting at i, to split the DOM mutants over parallel processes
    root = tempfile.mkdtemp(prefix="bma_ui_jsmut_")
    try:
        work = os.path.join(root, "w")
        os.makedirs(work)
        shutil.copytree(HERE, os.path.join(work, "ui"), ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(os.path.join(HERE, "..", "contracts"), os.path.join(work, "contracts"), ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(os.path.join(HERE, "..", "classify"), os.path.join(work, "classify"), ignore=shutil.ignore_patterns("__pycache__"))
        kinds = {m[0] for m in M if (not only or any(o in m[1] for o in only)) and (not kind_only or m[0] == kind_only)}
        for k in sorted(kinds):
            code, fails, last = run(work, k)
            print(f"baseline {k}: exit {code}, {last}")
            if code != 0:
                print("baseline is not green; fix that first")
                return 2
        origs = {f: open(os.path.join(HERE, "static", f), encoding="utf-8", newline="").read() for f in FILES}
        missed, ok = [], 0
        for idx, mut in enumerate(M):
            kind, name, old, new = mut[:4]
            fname = mut[4] if len(mut) > 4 else "app.js"
            orig = origs[fname]
            if only and not any(o in name for o in only):
                continue
            if kind_only and kind != kind_only:
                continue
            if part and idx % int(part.split("/")[1]) != int(part.split("/")[0]):
                continue
            if orig.count(old) != 1:
                print(f"BROKEN MUTATION (pattern occurs {orig.count(old)}x): {name}")
                missed.append(name)
                continue
            with open(os.path.join(work, "ui", "static", fname), "w", encoding="utf-8", newline="\n") as f:
                f.write(orig.replace(old, new))
            code, fails, last = run(work, kind)
            with open(os.path.join(work, "ui", "static", fname), "w", encoding="utf-8", newline="\n") as f:
                f.write(orig)
            if code != 0:
                ok += 1
                print(f"RED   [{kind}] {name}: exit {code}, {last}; first failing check: {(fails[0] if fails else last)[:100]}")
            else:
                print(f"GREEN [{kind}] {name}: exit {code}, {last}  <-- NOT CAUGHT")
                missed.append(name)
        print(f"\n{ok} mutation(s) caught, {len(missed)} not caught")
        return 1 if missed else 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
