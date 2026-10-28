#!/usr/bin/env python3
"""Read-only daily table of the acoustic indices (indices.jsonl, `acoustic_indices` v1) and how much each index is just a chorus meter.
    python -I indices_report.py <indices.jsonl> [<more.jsonl> ...] [--day YYYY-MM-DD] [--json]
Per UTC hour of the day: min / median / max of the interval medians (p50) of every index, with the number of intervals. Then the
Pearson correlation of each index's interval median with the chorus band energy (chorus_dbfs, 4-12 kHz) over the day's intervals:
|r| >= 0.8 is labelled CHORUS METER (the index mostly tracks chorus loudness, the research report's success criterion), fewer than
MIN_N intervals or a constant series is "n/a". Default day: the UTC day of the newest record. Nothing is written; exit 0, 2 for a usage
error, a missing file or no record for the day. Partial intervals are listed in the totals and used like the others. Lines that are not complete v1 records (any index or p10/p50/p90 missing or
not a number) are skipped; records with speech_gate false (run with --indices-without-speech-gate) are left out and counted (ungated_records_excluded)."""

import json, math, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
NAMES = ("aci", "hf", "ht", "h", "bi", "ndsi", "level_dbfs", "chorus_dbfs")
MIN_N = 8
METER_R = 0.8


def _well_formed(r):
    """every index with p10/p50/p90 numbers (a truncated or hand-edited line is skipped, never a crash)"""
    ind = r.get("indices")
    if not isinstance(ind, dict):
        return False
    for nm in NAMES:
        c = ind.get(nm)
        if not isinstance(c, dict) or not all(
            isinstance(c.get(q), (int, float)) and not isinstance(c.get(q), bool) and math.isfinite(c[q]) for q in ("p10", "p50", "p90")
        ):
            return False
    return True


def read(paths):
    recs = []
    for p in paths:
        with open(p, "rb") as f:
            for ln in f.read().splitlines():
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("k") == "acoustic_indices" and r.get("v") == 1 and isinstance(r.get("t0"), (int, float)) and _well_formed(r):
                    recs.append(r)
    recs.sort(key=lambda r: r["t0"])
    return recs


def pearson(a, b):
    n = len(a)
    if n < MIN_N:
        return None
    if min(a) == max(a) or min(b) == max(b):  # exactly constant: undefined, not a rounding-noise correlation
        return None
    ma, mb = sum(a) / n, sum(b) / n
    sa = sum((x - ma) ** 2 for x in a)
    sb = sum((y - mb) ** 2 for y in b)
    if not (sa > 0 and sb > 0):
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(sa * sb)


def median(v):
    s = sorted(v)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def day_of(t):
    return time.strftime("%Y-%m-%d", time.gmtime(t))


def build(recs, day=None):
    if not recs:
        return None
    day = day or day_of(recs[-1]["t0"])
    day_recs = [r for r in recs if day_of(r["t0"]) == day]
    sel = [r for r in day_recs if r.get("speech_gate") is True]  # records of a run without the speech gate are another population: not mixed in
    if not sel:
        return None
    hours = {}
    for r in sel:
        hours.setdefault(int(time.gmtime(r["t0"]).tm_hour), []).append(r)
    table = {
        nm: {
            h: {
                "n": len(rs),
                "min": min(x["indices"][nm]["p50"] for x in rs),
                "median": median([x["indices"][nm]["p50"] for x in rs]),
                "max": max(x["indices"][nm]["p50"] for x in rs),
            }
            for h, rs in sorted(hours.items())
        }
        for nm in NAMES
    }
    chorus = [r["indices"]["chorus_dbfs"]["p50"] for r in sel]
    corr = {}
    for nm in NAMES:
        if nm == "chorus_dbfs":
            continue
        rr = pearson([r["indices"][nm]["p50"] for r in sel], chorus)
        corr[nm] = {"r": None if rr is None else round(rr, 3), "chorus_meter": rr is not None and abs(rr) >= METER_R}
    tot = {k: sum(r.get(k, 0) for r in sel) for k in ("win_total", "win_used", "win_speech", "win_silent", "win_nonfinite", "win_untagged")}
    tot["intervals"], tot["partial"] = len(sel), sum(1 for r in sel if r.get("partial"))
    return {
        "day_utc": day,
        "hours": table,
        "correlation_with_chorus_dbfs": corr,
        "totals": tot,
        "min_intervals_for_r": MIN_N,
        "ungated_records_excluded": len(day_recs) - len(sel),
    }


def render(rep):
    out = [f"acoustic indices, UTC day {rep['day_utc']}: min / median / max of the interval medians per UTC hour"]
    for nm in NAMES:
        out.append(f"\n{nm}")
        out.append("  hour  n        min     median        max")
        for h, c in rep["hours"][nm].items():
            out.append(f"  {h:02d}   {c['n']:3d} {c['min']:10.3f} {c['median']:10.3f} {c['max']:10.3f}")
    out.append(f"\ncorrelation of each index with chorus_dbfs (4-12 kHz band energy) over {rep['totals']['intervals']} intervals (needs >= {MIN_N}):")
    for nm, c in rep["correlation_with_chorus_dbfs"].items():
        out.append(
            f"  {nm:12s} " + ("n/a" if c["r"] is None else f"r = {c['r']:+.3f}") + ("   CHORUS METER (|r| >= %.1f)" % METER_R if c["chorus_meter"] else "")
        )
    t = rep["totals"]
    out.append(
        f"\nwindows: total {t['win_total']}, used {t['win_used']}, speech-excluded {t['win_speech']}, silent {t['win_silent']}, "
        f"non-finite {t['win_nonfinite']}, untagged {t['win_untagged']}; partial intervals {t['partial']}; records without the speech gate left out {rep['ungated_records_excluded']}"
    )
    return "\n".join(out)


def main(argv):
    paths, day, js = [], None, False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--json":
            js = True
        elif a == "--day" and i + 1 < len(argv):
            i += 1
            day = argv[i]
        elif a.startswith("--"):
            print(f"usage: indices_report.py <indices.jsonl> [more.jsonl] [--day YYYY-MM-DD] [--json]  (unknown option {a})", file=sys.stderr)
            return 2
        else:
            paths.append(a)
        i += 1
    if not paths:
        print("usage: indices_report.py <indices.jsonl> [more.jsonl] [--day YYYY-MM-DD] [--json]", file=sys.stderr)
        return 2
    for p in paths:
        if not os.path.isfile(p):
            print(f"no such file: {p}", file=sys.stderr)
            return 2
    rep = build(read(paths), day)
    if rep is None:
        print("no acoustic_indices record for " + (day or "any day"), file=sys.stderr)
        return 2
    print(json.dumps(rep, indent=1) if js else render(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
