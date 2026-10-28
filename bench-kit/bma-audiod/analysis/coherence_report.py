#!/usr/bin/env python3
"""Read-only verdict table from coherence.jsonl ('beam_coherence' records of the analyzer's coherence plugin).

    python -I coherence_report.py <path/to/coherence.jsonl> [--since ISO] [--until ISO] [--min-windows 10] [--json]

Per octave band and condition (quiet / active) it pools the per-minute medians over the 8 ring-adjacent beam pairs (median
weighted by the number of windows of each minute), corrects the raw MSC for the estimator's upward bias with the record's own
msc_floor (K_eff = 1 / msc_floor; inverts E[msc] = c + (1 - c)^2 / K_eff, see correct()) and prints a verdict:

  coherent                    corrected MSC >= COHERENT_MSC for ALL 8 pairs and median phase spread <= COHERENT_PHASE_SPREAD_DEG
  partially coherent          in between
  incoherent                  median corrected MSC over the pairs < INCOHERENT_MSC
  insufficient data           fewer than --min-windows windows in the cell
  dominated by gated windows  more than GATED_DOMINATES of ALL windows were gated (DSP gating signature); every row says so

Gated and dead windows are excluded from the statistics by the producer; this script only reports how many there were.
Reads one file, writes nothing. Thresholds are the constants below and are printed in the header."""

import argparse, datetime, json, math, statistics, sys

COHERENT_MSC = 0.8
INCOHERENT_MSC = 0.3
COHERENT_PHASE_SPREAD_DEG = 30.0
GATED_DOMINATES = 0.5
MIN_WINDOWS = 10
CONDS = ("quiet", "active")
V_COH, V_PART, V_INCOH, V_INSUF, V_GATED = "coherent", "partially coherent", "incoherent", "insufficient data", "dominated by gated windows"


def load(path, since=None, until=None):
    """-> [record]: the beam_coherence v1 records of the file (other lines are skipped), optionally within [since, until] (Unix s)."""
    out = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if not isinstance(r, dict) or r.get("k") != "beam_coherence" or r.get("v") != 1:
                continue
            if (since is not None and r["t"] < since) or (until is not None and r["t"] > until):
                continue
            out.append(r)
    return out


def wmedian(vals, weights):
    """Weighted median (lower median at an exact half)."""
    pairs = sorted(zip(vals, weights))
    half = sum(w for _, w in pairs) / 2.0
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= half:
            return v
    return pairs[-1][0]


def correct(msc, floor):
    """Bias-corrected MSC from the raw value and the record's msc_floor = 1 / K_eff (clipped to [0, 1]). The estimator's mean is
    msc_raw = c + (1 - c)^2 / K_eff for a true MSC c (Monte-Carlo checked in test_coherence.py); this inverts it exactly, so a source
    whose true MSC is 0.8 reads 0.8, not 0.79 as the linear (K * msc - 1) / (K - 1) form gives."""
    k = 1.0 / floor
    disc = max(0.0, 1.0 - 4.0 * (1.0 - msc) / k)  # the smaller root of u^2 / k - u + (1 - msc) = 0, u = 1 - c
    return min(1.0, max(0.0, 1.0 - 0.5 * k * (1.0 - math.sqrt(disc))))


def _pool(recs, b, cond, key):
    """Per pair: weighted median over the minutes' cells of `key`; -> (list of 8 or None, windows)."""
    cells = [(r["bands"][b][cond], r) for r in recs if r["bands"][b][cond] is not None]
    n = sum(c["n"] for c, _ in cells)
    if not cells:
        return None, 0
    return [wmedian([c[key][p] for c, _ in cells], [c["n"] for c, _ in cells]) for p in range(8)], n


def build(recs, min_windows=MIN_WINDOWS):
    """-> the report dict (what main() prints, and what --json writes)."""
    tot = sum(r["win_total"] for r in recs)
    gated = sum(r["win_gated"] for r in recs)
    dead = sum(r["win_dead"] for r in recs)
    bad = sum(r["win_nonfinite"] for r in recs)
    used = sum(r["win_used"] for r in recs)
    gfrac = gated / tot if tot else 0.0
    dominated = tot > 0 and gfrac > GATED_DOMINATES
    rows = []
    for b in range(8):
        for cond in CONDS:
            msc, n = _pool(recs, b, cond, "msc_p50")
            row = {
                "band": b,
                "edges_hz": recs[0]["edges_hz"][b] if recs else None,
                "cond": cond,
                "windows": n,
                "msc_min": None,
                "msc_median": None,
                "phase_spread_deg": None,
                "lag_median": None,
                "unmasked_msc_median": None,
            }
            if msc is not None:
                floor = recs[-1]["msc_floor"]
                c = [correct(m, floor) for m in msc]
                spread, _ = _pool(recs, b, cond, "ph_spread_deg")
                lag, _ = _pool(recs, b, cond, "lag_p50")
                row.update(
                    msc_min=round(min(c), 3),
                    msc_median=round(statistics.median(c), 3),
                    phase_spread_deg=round(statistics.median(spread), 1),
                    lag_median=round(statistics.median(lag), 3),
                )
                um, _ = _pool(recs, b, cond, "msc_unmasked_p50") if any("msc_unmasked_p50" in (r["bands"][b][cond] or {}) for r in recs) else (None, 0)
                if um is not None:
                    row["unmasked_msc_median"] = round(statistics.median(correct(m, floor) for m in um), 3)
            if dominated:
                v = V_GATED
            elif msc is None or n < min_windows:
                v = V_INSUF
            elif row["msc_min"] >= COHERENT_MSC and row["phase_spread_deg"] <= COHERENT_PHASE_SPREAD_DEG:
                v = V_COH
            elif row["msc_median"] < INCOHERENT_MSC:
                v = V_INCOH
            else:
                v = V_PART
            row["verdict"] = v
            rows.append(row)
    coherent = [(r["band"], r["cond"]) for r in rows if r["verdict"] == V_COH]
    decided = any(r["verdict"] in (V_COH, V_PART, V_INCOH) for r in rows)
    return {
        "records": len(recs),
        "windows": {"total": tot, "used": used, "gated": gated, "dead": dead, "nonfinite": bad},
        "gated_fraction": round(gfrac, 4),
        "rows": rows,
        "thresholds": {
            "coherent_msc": COHERENT_MSC,
            "incoherent_msc": INCOHERENT_MSC,
            "coherent_phase_spread_deg": COHERENT_PHASE_SPREAD_DEG,
            "gated_dominates": GATED_DOMINATES,
            "min_windows": min_windows,
        },
        "beamspace": ("possible where coherent: " + ", ".join(f"band {b + 1} {c}" for b, c in coherent))
        if coherent
        else "not supported by this data: no band is coherent"
        if decided
        else "undetermined: no row has enough usable data (every row is gated-dominated or has insufficient data)",
    }


def render(rep):
    th, w = rep["thresholds"], rep["windows"]
    lines = [
        f"beam coherence: {rep['records']} minute record(s); windows total {w['total']}, used {w['used']}, gated {w['gated']}, dead {w['dead']}, non-finite {w['nonfinite']}",
        f"thresholds: coherent = every pair corrected MSC >= {th['coherent_msc']} and phase spread <= {th['coherent_phase_spread_deg']} deg; "
        f"incoherent = median < {th['incoherent_msc']}; insufficient < {th['min_windows']} windows; gated-dominated > {th['gated_dominates']:.0%} gated",
        "MSC is bias-corrected with the record's msc_floor; phase spread = circular std over windows; lag in samples (+ = beam p+1 lags beam p)",
        "",
        f"{'band Hz':>12} {'cond':<7} {'windows':>7} {'MSC min':>8} {'MSC med':>8} {'ph spread':>9} {'lag med':>8}  verdict",
    ]
    for r in rep["rows"]:
        if r["edges_hz"] is None:
            continue
        f = lambda v, d: "-" if v is None else f"{v:.{d}f}"
        lines.append(
            f"{r['edges_hz'][0]:>5}-{r['edges_hz'][1]:<6} {r['cond']:<7} {r['windows']:>7} {f(r['msc_min'], 3):>8} {f(r['msc_median'], 3):>8} "
            f"{f(r['phase_spread_deg'], 1):>9} {f(r['lag_median'], 3):>8}  {r['verdict']}"
        )
    lines += [
        "",
        f"beamspace processing (adjacent-beam differencing, adaptive combination): {rep['beamspace']}",
        f"gating: {rep['gated_fraction']:.1%} of windows had a beam muted > 40 dB below the loudest (the DSP gating signature); the table excludes them",
    ]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("path", help="coherence.jsonl written by the analyzer")
    ap.add_argument("--since", help="ISO time (UTC when no offset)")
    ap.add_argument("--until", help="ISO time (UTC when no offset)")
    ap.add_argument("--min-windows", type=int, default=MIN_WINDOWS)
    ap.add_argument("--json", action="store_true", help="print the table as JSON")
    a = ap.parse_args(argv)

    def ts(s):
        if not s:
            return None
        d = datetime.datetime.fromisoformat(s)
        return (d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)).timestamp()

    try:
        recs = load(a.path, ts(a.since), ts(a.until))
    except OSError as e:
        print(f"cannot read {a.path}: {e}", file=sys.stderr)
        return 2
    if not recs:
        print("no beam_coherence records in range", file=sys.stderr)
        return 1
    rep = build(recs, a.min_windows)
    print(json.dumps(rep) if a.json else render(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
