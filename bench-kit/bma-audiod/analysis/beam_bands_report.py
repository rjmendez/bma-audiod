#!/usr/bin/env python3
"""Per-band beam directivity report from the sink's beam_bands.jsonl (metadata only).

For every band: how far apart (max - min over the 8 beams) the beams' EXCESS over their own quiet baseline are during
loud events, versus the same spread in quiet windows. A source with a direction should open a spread in the bands where
the array can resolve it; a spread that stays at the quiet level means the beams behave as one omni channel there.

    python beam_bands_report.py [beam_bands.jsonl] [--since ISO] [--until ISO] [--event-db 6] [--split ISO]

--split gives a before/after comparison (e.g. around a beam-config change)."""

import argparse, collections, datetime, json, os, statistics, sys

# <data-dir> = $BMA_DATA_DIR (default ./data)
DEFAULT = os.path.join(os.environ.get("BMA_DATA_DIR") or "data", "reports", "beam_bands.jsonl")


def load(path, since, until):
    rows = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("k") != "beam_bands" or r.get("zero_frac", 0) > 0.2:
                continue
            t = r["t_us"] / 1e6
            if since and t < since or until and t > until:
                continue
            rows.append(r)
    return rows


def baseline(rows):
    """Per beam, per band median over the quietest half of the windows (by mean band level)."""
    nb = len(rows[0]["edges_hz"])
    base = [[0.0] * nb for _ in range(8)]
    for k in range(nb):
        lv = sorted((sum(r["db"][b][k] for b in range(8)) / 8, i) for i, r in enumerate(rows))
        quiet = [rows[i] for _, i in lv[: max(1, len(lv) // 2)]]
        for b in range(8):
            base[b][k] = statistics.median(r["db"][b][k] for r in quiet)
    return base


def analyse(rows, base, event_db):
    edges = rows[0]["edges_hz"]
    out = []
    for k, (lo, hi) in enumerate(edges):
        quiet_spread, ev_spread, leaders = [], [], collections.Counter()
        for r in rows:
            ex = [r["db"][b][k] - base[b][k] for b in range(8)]
            mean = sum(ex) / 8
            spread = max(ex) - min(ex)
            if mean >= event_db:
                ev_spread.append(spread)
                leaders[ex.index(max(ex)) + 1] += 1
            elif mean < 2.0:
                quiet_spread.append(spread)
        out.append(
            (
                lo,
                hi,
                len(quiet_spread),
                statistics.median(quiet_spread) if quiet_spread else None,
                len(ev_spread),
                statistics.median(ev_spread) if ev_spread else None,
                max(ev_spread) if ev_spread else None,
                leaders.most_common(3),
            )
        )
    return out


def show(title, rows, event_db):
    print(f"\n== {title}: {len(rows)} windows ==")
    if len(rows) < 20:
        print("  too few windows")
        return
    base = baseline(rows)
    print("band Hz        quiet n  quiet spread | event n  median spread  max spread | leading beams")
    for lo, hi, qn, qs, en, es, em, lead in analyse(rows, base, event_db):
        f = lambda v: "   -  " if v is None else f"{v:6.1f}"
        print(f"{lo:>5}-{hi:<6} {qn:8d}  {f(qs)} dB    | {en:6d}  {f(es)} dB   {f(em)} dB | {lead}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("path", nargs="?", default=DEFAULT)
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--split")
    ap.add_argument("--event-db", type=float, default=6.0, help="mean excess over baseline that makes a window an event")
    a = ap.parse_args()
    ts = lambda s: datetime.datetime.fromisoformat(s).timestamp() if s else None
    rows = load(a.path, ts(a.since), ts(a.until))
    if not rows:
        sys.exit("no beam_bands records in that range")
    t0, t1 = rows[0]["t_us"] / 1e6, rows[-1]["t_us"] / 1e6
    fmt = lambda t: datetime.datetime.fromtimestamp(t).strftime("%m-%d %H:%M:%S")
    print(f"{len(rows)} windows {fmt(t0)} .. {fmt(t1)} (local), event = mean excess >= {a.event_db} dB")
    if a.split:
        s = ts(a.split)
        show(f"before {a.split}", [r for r in rows if r["t_us"] / 1e6 < s], a.event_db)
        show(f"after {a.split}", [r for r in rows if r["t_us"] / 1e6 >= s], a.event_db)
    else:
        show("all", rows, a.event_db)


if __name__ == "__main__":
    main()
