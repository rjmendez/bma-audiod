#!/usr/bin/env python3
"""Replay the stored tonal events against a mask: per day, how many tonal_appeared / tonal_gone events the mask
would suppress vs keep, and which frequencies keep producing events.

    python replay_tonal_mask.py [dsp.jsonl] [--mask <data-dir>/reports/tonal_mask.json] [--out <data-dir>/reports/tonal_mask_replay.txt]
(<data-dir> = $BMA_DATA_DIR, default ./data)

Approximation: the live monitor decides at announce time on the line's peak frequency; here the recorded event's
`hz` is used for both appeared and gone (they differ by at most the 2.9 Hz bin). No audio is read."""

import argparse, collections, datetime, json, os, sys

_D = lambda *p: os.path.join(os.environ.get("BMA_DATA_DIR") or "data", *p)
ap = argparse.ArgumentParser()
ap.add_argument("dsp", nargs="?", default=_D("analysis", "dsp.jsonl"))
ap.add_argument("--mask", default=_D("reports", "tonal_mask.json"))
ap.add_argument("--out", default=_D("reports", "tonal_mask_replay.txt"))
a = ap.parse_args()
m = json.load(open(a.mask))
lines, tol = [float(h) for h in m["lines_hz"]], float(m.get("tolerance_hz", 4.0))
masked = lambda hz: any(abs(hz - x) <= tol for x in lines)
ev = collections.defaultdict(lambda: collections.Counter())  # day -> (kind, 'suppressed'|'kept') -> n
kept_hz = collections.Counter()
kept_by_band = collections.defaultdict(collections.Counter)
band = lambda hz: "<1k" if hz < 1000 else "1-4k" if hz < 4000 else "4-12k" if hz < 12000 else "12-16k" if hz < 16000 else "16k+"
with open(a.dsp, "r", encoding="utf-8", errors="replace") as f:
    for line in f:
        if '"tonal_appeared"' not in line and '"tonal_gone"' not in line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        day = datetime.datetime.fromtimestamp(r["t"]).strftime("%Y-%m-%d")
        s = "suppressed" if masked(r["hz"]) else "kept"
        ev[day][(r["k"], s)] += 1
        if s == "kept":
            kept_hz[round(r["hz"] / 10) * 10] += 1
            kept_by_band[day][band(r["hz"])] += 1
out = [
    f"mask: {len(lines)} lines {lines} tolerance {tol} Hz ({a.mask})",
    "",
    "%-11s %9s %9s %9s %9s %10s" % ("day", "app.supp", "app.kept", "gone.supp", "gone.kept", "suppressed"),
]
tot = collections.Counter()
for d in sorted(ev):
    c = ev[d]
    n_all = sum(c.values())
    n_sup = c[("tonal_appeared", "suppressed")] + c[("tonal_gone", "suppressed")]
    out.append(
        "%-11s %9d %9d %9d %9d %9.0f%%"
        % (
            d,
            c[("tonal_appeared", "suppressed")],
            c[("tonal_appeared", "kept")],
            c[("tonal_gone", "suppressed")],
            c[("tonal_gone", "kept")],
            100.0 * n_sup / n_all,
        )
    )
    tot.update(c)
n_all = sum(tot.values())
n_sup = tot[("tonal_appeared", "suppressed")] + tot[("tonal_gone", "suppressed")]
out.append(
    "%-11s %9d %9d %9d %9d %9.0f%%"
    % (
        "ALL",
        tot[("tonal_appeared", "suppressed")],
        tot[("tonal_appeared", "kept")],
        tot[("tonal_gone", "suppressed")],
        tot[("tonal_gone", "kept")],
        100.0 * n_sup / n_all,
    )
)
out += ["", "kept events by band, per day:"]
for d in sorted(kept_by_band):
    out.append("  %s  %s" % (d, dict(kept_by_band[d].most_common())))
out += ["", "frequencies (10 Hz bins) that still produce the most events (NOT covered by the mask):"]
out.append("  " + ", ".join(f"{hz}:{n}" for hz, n in kept_hz.most_common(25)))
text = "\n".join(out)
print(text)
open(a.out, "w").write(text + "\n")
