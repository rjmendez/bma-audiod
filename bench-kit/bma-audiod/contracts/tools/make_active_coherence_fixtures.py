#!/usr/bin/env python3
"""Regenerate contracts/fixtures/active_coherence_report.json by running the REAL tools/active_coherence.py on a synthetic scenario:

    python -I make_active_coherence_fixtures.py [out_dir]      (default: ../fixtures)

`scenario()` is also what tools/test_active_coherence.py builds its tests from. All data is invented."""

import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "tools")))
import active_coherence as AC  # noqa: E402

BASE_T = 1792972800.0
GENERATED_S = 1792972800.0
EDGES = [[50, 125], [125, 250], [250, 500], [500, 1000], [1000, 2000], [2000, 4000], [4000, 8000], [8000, 16000]]
FLOOR = 0.048


def cell(n, msc, ph_spread, lag, dl, ph=0.0):
    return {
        "n": n,
        "msc_p10": [msc] * 8,
        "msc_p50": [msc] * 8,
        "msc_p90": [msc] * 8,
        "ph_deg": [ph] * 8,
        "ph_spread_deg": [ph_spread] * 8,
        "lag_p50": [lag] * 8,
        "lag_spread": [0.1] * 8,
        "dl_db_p50": [dl] * 8,
    }


def with_pairs(c, pairs, other):
    """cell c on `pairs`, MSC `other` on the remaining pairs."""
    d = json.loads(json.dumps(c))
    for p in range(8):
        if p not in pairs:
            for k in ("msc_p10", "msc_p50", "msc_p90"):
                d[k][p] = other
    return d


def record(i, active=None):
    """60 s interval i; every band has a quiet cell (20 windows, raw MSC 0.8); `active` = {band: cell}."""
    active = active or {}
    bands = []
    for b in range(8):
        a = active.get(b)
        bands.append({"n_quiet": 20, "quiet": cell(20, 0.8, 10.0, 0.0, 0.0), "n_active": a["n"] if a else 0, "active": a, "mid": 0, "warmup": 0})
    return {
        "k": "beam_coherence",
        "v": 1,
        "t": BASE_T + 60 * (i + 1),
        "t0": BASE_T + 60 * i,
        "span_s": 60.0,
        "win_total": 30,
        "win_used": 30,
        "win_gated": 0,
        "win_dead": 0,
        "win_nonfinite": 0,
        "level_dbfs": -50.0,
        "msc_floor": FLOOR,
        "edges_hz": EDGES,
        "bands": bands,
    }


def bird(t, beam, common, sci):
    return {"k": "bird", "t": t, "beam": beam, "level_dbfs": -50.0, "common": common, "scientific": sci, "confidence": 0.9, "provenance": "model_opinion"}


def long_event(i, beams, common, sci, n=11):
    """reports every 3 s from t0+10 on, alternating over `beams`: one dashboard event of 3 * (n - 1) + 3 = 33 s."""
    t0 = BASE_T + 60 * i
    return [bird(t0 + 10 + 3 * j, beams[j % len(beams)], common, sci) for j in range(n)]


def scenario():
    """-> (coherence rows, bird rows). Intervals 0-3 Barn Owl (beams 3, 4; band 5 beams separate), 4-7 Tufted Titmouse (beams 6, 7; band 6
    coherent with a 2 sample lag), 8-10 Carolina Wren (6 s events, cannot be isolated), 11 Human vocal (excluded), 12 single beam, 13 partial, 14-15 Eastern Screech-Owl (2 events, below min_events)."""
    coh, birds = [], []
    sep = with_pairs(cell(10, 0.3, 40.0, 1.0, 4.0), {1, 2, 3}, 0.9)
    for i in range(4):
        coh.append(record(i, {5: sep}))
        birds += long_event(i, [3, 4], "Barn Owl", "Tyto alba")
    tit = cell(10, 0.95, 5.0, 2.0, 1.0)
    for i in range(4, 8):
        coh.append(record(i, {6: tit}))
        birds += long_event(i, [6, 7], "Tufted Titmouse", "Baeolophus bicolor")
    for i in range(8, 11):
        coh.append(record(i, {5: cell(10, 0.9, 5.0, 0.0, 0.0)}))
        birds += long_event(i, [5, 6], "Carolina Wren", "Thryothorus ludovicianus", n=2)
    coh.append(record(11))
    birds += long_event(11, [1, 2], "Human vocal", "Human vocal", n=3)
    coh.append(record(12))
    birds += long_event(12, [1], "Blue Jay", "Cyanocitta cristata", n=3)
    p = record(13, {5: cell(10, 0.3, 40.0, 1.0, 4.0)})
    p["partial"] = True
    coh.append(p)
    for i in (14, 15):
        coh.append(record(i, {4: sep}))
        birds += long_event(i, [3, 4], "Eastern Screech-Owl", "Megascops asio")
    return coh, birds


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


def build(out_dir):
    coh, birds = scenario()
    rep = AC.build_report(coh, birds, GENERATED_S)
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "active_coherence_report.json")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rep, separators=(",", ":")) + "\n")
    return out


if __name__ == "__main__":
    print("wrote", build(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "fixtures")))
