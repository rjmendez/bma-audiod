#!/usr/bin/env python3
"""active_coherence: event-keyed active beam coherence (contract active-coherence-report 1.0.0).

    active_coherence.py --data DIR [--out FILE] [--min-events N] [--generated-s S]

Read-only. Metadata only: no audio, no coordinates, no path. stdout = terse table; --out = JSON report (one line).
--out inside DIR is refused (exit 2). Exit 0 ok, 2 refused or unreadable input."""

import argparse, bisect, json, math, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
for _d in (HERE, os.path.join(HERE, "..", "analysis")):
    sys.path.insert(0, _d)
import labels as L  # noqa: E402
import coherence_report as CR  # noqa: E402

RECORD_VERSION = 1
BIRDS_WIN_S = 3.0  # = birds plugin window
HOP_S = 2.0  # = coherence plugin hop_s
MIN_CELL_WINDOWS = 5  # = coherence plugin: a cell with fewer windows is null
ISOLATION_MIN = 0.5  # event windows / active-cell windows
MIN_EVENTS = 3
MIN_NO_GO_SPECIES = 2  # a method is no_go only when this many species are measured and fail it
IDENTICAL_BANDS = 3  # bands below 500 Hz: beams identical (spec beam-coherence), never judged
SEPARATE_MSC = 0.5
COHERENT_MSC = CR.COHERENT_MSC
COHERENT_PHASE_SPREAD_DEG = CR.COHERENT_PHASE_SPREAD_DEG
BEARING_DL_DB = 3.0
DUET_LAG_SAMPLES = 0.5
VERDICTS = ("go", "no_go", "insufficient")
METHODS = ("beam_gated_classification", "level_ratio_bearing", "duet")


def rnd(x, nd=3):
    return None if x is None else round(float(x), nd)


def median(vals):
    s = sorted(vals)
    if not s:
        return None
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def circ_mean_deg(vals, weights):
    s = sum(w * math.sin(math.radians(v)) for v, w in zip(vals, weights))
    c = sum(w * math.cos(math.radians(v)) for v, w in zip(vals, weights))
    return math.degrees(math.atan2(s, c))


def hidden(o):
    return L.is_speech_label(o.get("common") or "") or L.is_speech_label(o.get("scientific") or "")


def valid_coh(r):
    return (
        isinstance(r, dict)
        and r.get("k") == "beam_coherence"
        and r.get("v") == 1
        and L.is_num(r.get("t0"))
        and L.is_num(r.get("span_s"))
        and r["span_s"] > 0
        and L.is_num(r.get("msc_floor"))
        and r["msc_floor"] > 0
        and isinstance(r.get("bands"), list)
        and len(r["bands"]) == 8
        and isinstance(r.get("edges_hz"), list)
        and len(r["edges_hz"]) == 8
    )


def quiet_reference(recs):
    """[band][pair] -> corrected quiet MSC pooled over all intervals (median weighted by the cell's windows), None without a quiet cell."""
    ref = []
    for b in range(8):
        cells = [(r["bands"][b]["quiet"], r["msc_floor"]) for r in recs if r["bands"][b].get("quiet")]
        if not cells:
            ref.append([None] * 8)
            continue
        ref.append([CR.wmedian([CR.correct(c["msc_p50"][p], fl) for c, fl in cells], [c["n"] for c, _ in cells]) for p in range(8)])
    return ref


def event_pairs(beams):
    """Ring pairs p = (beam p, beam p+1) with at least one member among the event's beams (1-based in birds rows)."""
    b0 = {b - 1 for b in beams}
    return [p for p in range(8) if p in b0 or (p + 1) % 8 in b0]


def build_report(coh_rows, bird_rows, generated_s, min_events=MIN_EVENTS):
    recs = sorted((r for r in coh_rows if valid_coh(r) and not r.get("partial")), key=lambda r: r["t0"])
    partial = sum(1 for r in coh_rows if valid_coh(r) and r.get("partial"))
    birds = [o for o in bird_rows if L.valid_bird(o) and o["k"] == "bird"]
    excluded = sum(1 for o in birds if hidden(o))
    birds = sorted((o for o in birds if not hidden(o)), key=lambda o: o["t"])
    events = L.iter_events(birds)
    multi = [e for e in events if len(e.beams) >= 2]
    t0s = [r["t0"] for r in recs]
    qref = quiet_reference(recs)
    samples = {}  # (species, band) -> list of pair-level samples
    ev_seen = {}  # (species, band) -> set of event ids with a sample
    iv_seen = {}
    n_overlap_events, ev_iv, iso_iv = 0, 0, 0
    ev_len, ev_win, iv_len = [], [], []
    iso_events = set()
    for e in multi:
        s, t_e = e.t_start - BIRDS_WIN_S, e.t_end
        lo = bisect.bisect_left(t0s, s - max((r["span_s"] for r in recs), default=0.0))
        hit = False
        for r in recs[lo:]:
            if r["t0"] >= t_e:
                break
            ov = min(t_e, r["t0"] + r["span_s"]) - max(s, r["t0"])
            if ov <= 0:
                continue
            hit = True
            ev_iv += 1
            ev_len.append(t_e - s)
            ev_win.append(ov / HOP_S)
            iv_len.append(r["span_s"])
            any_iso = False
            for b in range(8):
                cell = r["bands"][b].get("active")
                if not cell or ov / HOP_S / cell["n"] < ISOLATION_MIN:
                    continue
                any_iso = True
                key = (e.common, b)
                ev_seen.setdefault(key, set()).add(e.event_id)
                iv_seen.setdefault(key, set()).add(r["t0"])
                for p in event_pairs(e.beams):
                    q = qref[b][p]
                    msc = CR.correct(cell["msc_p50"][p], r["msc_floor"])
                    samples.setdefault(key, []).append(
                        (
                            msc,
                            None if q is None else msc - q,
                            q,
                            cell["ph_deg"][p],
                            cell["ph_spread_deg"][p],
                            cell["lag_p50"][p],
                            cell["dl_db_p50"][p],
                            cell["n"],
                        )
                    )
            if any_iso:
                iso_iv += 1
                iso_events.add(e.event_id)
        n_overlap_events += 1 if hit else 0
    rows = []
    for (sp, b), ss in sorted(samples.items()):
        w = [x[7] for x in ss]
        diffs = [(x[1], x[7]) for x in ss if x[1] is not None]
        quiet = [(x[2], x[7]) for x in ss if x[2] is not None]
        events_n = len(ev_seen[(sp, b)])
        msc = CR.wmedian([x[0] for x in ss], w)
        spread = CR.wmedian([x[4] for x in ss], w)
        lag = CR.wmedian([abs(x[5]) for x in ss], w)
        dl = CR.wmedian([abs(x[6]) for x in ss], w)
        rows.append(
            {
                "species": sp,
                "band": b,
                "edges_hz": recs[0]["edges_hz"][b],
                "status": "identical_expected" if b < IDENTICAL_BANDS else ("ok" if events_n >= min_events else "insufficient"),
                "events": events_n,
                "intervals": len(iv_seen[(sp, b)]),
                "pair_samples": len(ss),
                "windows": sum(w),
                "msc_active": rnd(msc),
                "msc_quiet": rnd(CR.wmedian([d[0] for d in quiet], [d[1] for d in quiet])) if quiet else None,
                "msc_diff": rnd(CR.wmedian([d[0] for d in diffs], [d[1] for d in diffs])) if diffs else None,
                "ph_deg": rnd(circ_mean_deg([x[3] for x in ss], w), 1),
                "ph_spread_deg": rnd(spread, 1),
                "lag_abs": rnd(lag),
                "dl_abs_db": rnd(dl, 2),
                "separates": msc < SEPARATE_MSC,
                "coherent": msc >= COHERENT_MSC and spread <= COHERENT_PHASE_SPREAD_DEG,
                "bearing_dl": dl >= BEARING_DL_DB,
                "duet_lag": lag >= DUET_LAG_SAMPLES,
            }
        )
    species = []
    for sp in sorted({r["species"] for r in rows}):
        ok = [r for r in rows if r["species"] == sp and r["status"] == "ok"]

        def v(pred):
            return "insufficient" if not ok else ("go" if any(pred(r) for r in ok) else "no_go")

        species.append(
            {
                "species": sp,
                "events": max(r["events"] for r in rows if r["species"] == sp and r["band"] >= IDENTICAL_BANDS)
                if any(r["band"] >= IDENTICAL_BANDS for r in rows if r["species"] == sp)
                else 0,
                "bands_ok": len(ok),
                "beam_gated_classification": v(lambda r: r["separates"]),
                "level_ratio_bearing": v(lambda r: r["bearing_dl"]),
                "duet": v(lambda r: r["coherent"] and r["duet_lag"]),
            }
        )
    measurable = any(r["status"] == "ok" for r in rows)
    methods = {}
    for m in METHODS:
        go = [s["species"] for s in species if s[m] == "go"]
        no = [s["species"] for s in species if s[m] == "no_go"]
        methods[m] = {"verdict": "go" if go else ("no_go" if len(no) >= MIN_NO_GO_SPECIES else "not_measurable"), "go": go, "no_go": no}
    return {
        "k": "active_coherence_report",
        "v": RECORD_VERSION,
        "generated_s": generated_s,
        "params": {
            "hop_s": HOP_S,
            "min_cell_windows": MIN_CELL_WINDOWS,
            "isolation_min": ISOLATION_MIN,
            "min_events": min_events,
            "min_no_go_species": MIN_NO_GO_SPECIES,
            "separate_msc": SEPARATE_MSC,
            "coherent_msc": COHERENT_MSC,
            "coherent_phase_spread_deg": COHERENT_PHASE_SPREAD_DEG,
            "bearing_dl_db": BEARING_DL_DB,
            "duet_lag_samples": DUET_LAG_SAMPLES,
            "event_gap_s": L.EVENT_GAP_S,
        },
        "input": {
            "coherence_records": len(recs),
            "coherence_partial_skipped": partial,
            "birds_rows": len(birds),
            "excluded_human_voice": excluded,
            "events": len(events),
            "events_multi_beam": len(multi),
            "events_overlapping": n_overlap_events,
        },
        "measurability": {
            "verdict": "measurable" if measurable else "not_measurable",
            "interval_s_p50": rnd(median(iv_len), 1),
            "event_s_p50": rnd(median(ev_len), 1),
            "event_windows_p50": rnd(median(ev_win), 2),
            "event_intervals": ev_iv,
            "isolating_event_intervals": iso_iv,
            "isolating_events": len(iso_events),
            "species_ok": sum(1 for x in species if x["bands_ok"]),
            "species_insufficient": sum(1 for x in species if not x["bands_ok"]),
        },
        "rows": rows,
        "species": species,
        "methods": methods,
    }


def table(rep):
    m, i = rep["measurability"], rep["input"]
    out = [
        "active_coherence v%d %s" % (rep["v"], m["verdict"]),
        "events %d multi_beam %d overlapping %d; excluded_human_voice %d"
        % (i["events"], i["events_multi_beam"], i["events_overlapping"], i["excluded_human_voice"]),
        "interval_s p50 %s event_s p50 %s event_windows p50 %s min_cell_windows %d"
        % (m["interval_s_p50"], m["event_s_p50"], m["event_windows_p50"], rep["params"]["min_cell_windows"]),
        "event_intervals %d isolating %d (events %d); species ok %d insufficient %d"
        % (m["event_intervals"], m["isolating_event_intervals"], m["isolating_events"], m["species_ok"], m["species_insufficient"]),
    ]
    for k in METHODS:
        out.append("%s: %s go=%s no_go=%s" % (k, rep["methods"][k]["verdict"], ",".join(rep["methods"][k]["go"]), ",".join(rep["methods"][k]["no_go"])))
    out.append("-- species/band: events active quiet diff spread lag dl status")
    for r in rep["rows"]:
        out.append(
            "%-28s b%d %3d %s %s %s %s %s %s %s"
            % (
                r["species"],
                r["band"],
                r["events"],
                r["msc_active"],
                r["msc_quiet"],
                r["msc_diff"],
                r["ph_spread_deg"],
                r["lag_abs"],
                r["dl_abs_db"],
                r["status"],
            )
        )
    return "\n".join(out) + "\n"


def _inside(path, directory):
    p, d = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(directory))
    return p == d or p.startswith(d + os.sep)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", required=True, help="data directory holding analysis/coherence.jsonl and analysis/birds.jsonl (read only)")
    ap.add_argument("--out", help="JSON report file; refused inside --data")
    ap.add_argument("--min-events", type=int, default=MIN_EVENTS)
    ap.add_argument("--generated-s", type=float, default=None)
    a = ap.parse_args(argv)
    if a.out and _inside(a.out, a.data):
        sys.stderr.write("refusing to write --out inside --data\n")
        return 2
    try:
        coh = L.read_rotated(os.path.join(a.data, "analysis", "coherence.jsonl"))
        birds = L.read_rotated(os.path.join(a.data, "analysis", "birds.jsonl"))
    except OSError as ex:
        sys.stderr.write("cannot read input: %s\n" % type(ex).__name__)
        return 2
    rep = build_report(coh, birds, time.time() if a.generated_s is None else a.generated_s, a.min_events)
    sys.stdout.write(table(rep))
    if a.out:
        with open(a.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rep, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
