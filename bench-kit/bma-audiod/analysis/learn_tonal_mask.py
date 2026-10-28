#!/usr/bin/env python3
"""Learn which tonal lines are the unit's own electronics (clock spurs) from the stored tonal_state history, and
write a mask for DSPMonitor (host.py --tonal-mask).

    python learn_tonal_mask.py [dsp.jsonl] [--out <data-dir>/reports/tonal_mask.json]   (<data-dir> = $BMA_DATA_DIR, default ./data)

A line is masked when it is a PERSISTENT line, judged per frequency bin (the tracker's grid, FS/16384 = 2.93 Hz,
neighbours +-1 bin merged because the peak jitters by a bin):
  core    present in >= CORE_SHARE of the tonal states on >= MIN_DAYS different days (days with >= MIN_STATES states)
          AND still present at that share on the MOST RECENT usable day (the unit's current state; a line that is gone now is the room)
  clock   present in >= CLOCK_SHARE of the states on >= MIN_DAYS days (latest day included, as for core) AND sitting on the sample clock: within one
          bin of FS * p / q (q <= 16) or on the FS/864 comb (the ~55.6 Hz comb seen at 15 kHz)
Lines that are only persistent on ONE day (e.g. the 16-19 kHz cluster of the indoor days) are not learned: they
may be the room, not the unit. Stdlib only; no audio is read."""

import argparse, collections, datetime, json, os, sys

FS = 48000
NFFT = 16384
DF = FS / NFFT
CORE_SHARE, CLOCK_SHARE = 0.90, 0.50
MIN_DAYS, MIN_STATES = 2, 200
MERGE_BINS = 1
COMB_HZ = FS / 864.0


def clock_ratio(hz, tol):
    """(p, q) if hz is within tol of FS * p / q for some q <= 16 (lowest terms), else None."""
    for q in range(1, 17):
        p = round(hz * q / FS)
        if p >= 1 and abs(hz - FS * p / q) <= tol:
            from math import gcd

            g = gcd(p, q)
            return p // g, q // g
    return None


def on_comb(hz, tol):
    m = round(hz / COMB_HZ)
    return m >= 1 and abs(hz - m * COMB_HZ) <= tol


def load_states(path):
    """-> {day: [set(bin, ...), ...]} one set of bins per tonal_state record, plus the mean frequency per bin."""
    days = collections.defaultdict(list)
    freq_sum = collections.defaultdict(float)
    freq_n = collections.Counter()
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"tonal_state"' not in line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            day = datetime.datetime.fromtimestamp(r["t"]).strftime("%Y-%m-%d")
            bins = set()
            for ln in r["lines"]:
                b = round(ln["hz"] / DF)
                bins.add(b)
                freq_sum[b] += ln["hz"]
                freq_n[b] += 1
            days[day].append(bins)
    return days, {b: freq_sum[b] / freq_n[b] for b in freq_n}


def learn(days, bin_hz):
    """-> (mask entries, per-day state counts). Each entry: dict(hz, cls, days_over, shares)."""
    usable = {d: s for d, s in days.items() if len(s) >= MIN_STATES}
    share = collections.defaultdict(dict)  # bin -> {day: share of states with a line within +-MERGE_BINS}
    all_bins = {b for s in usable.values() for st in s for b in st}
    for d, states in usable.items():
        cnt = collections.Counter()
        for st in states:
            near = set()
            for b in st:
                for k in range(-MERGE_BINS, MERGE_BINS + 1):
                    near.add(b + k)
            cnt.update(near)
        for b in all_bins:
            if cnt[b]:
                share[b][d] = cnt[b] / len(states)
    latest = max(usable) if usable else None
    picked = []
    for b in sorted(all_bins):
        hz = bin_hz.get(b, b * DF)
        shares = share[b]
        core_days = [d for d, v in shares.items() if v >= CORE_SHARE]
        clock_days = [d for d, v in shares.items() if v >= CLOCK_SHARE]
        cr = clock_ratio(hz, DF)
        comb = on_comb(hz, DF)
        if len(core_days) >= MIN_DAYS and shares.get(latest, 0) >= CORE_SHARE:
            picked.append((b, hz, "core", len(core_days), shares))
        elif len(clock_days) >= MIN_DAYS and shares.get(latest, 0) >= CLOCK_SHARE and (cr or comb):
            picked.append((b, hz, "clock", len(clock_days), shares))
    # merge neighbouring picked bins into one entry centred on the strongest bin
    out, cur = [], []
    for item in picked:
        if cur and item[0] - cur[-1][0] > MERGE_BINS + 1:
            out.append(cur)
            cur = []
        cur.append(item)
    if cur:
        out.append(cur)
    entries = []
    for grp in out:
        best = max(grp, key=lambda it: sum(it[4].values()))
        classes = {it[2] for it in grp}
        entries.append(
            {
                "hz": round(best[1], 1),
                "cls": "core" if "core" in classes else "clock",
                "days_over": best[3],
                "share_by_day": {d: round(v, 2) for d, v in sorted(best[4].items())},
            }
        )
    return entries, {d: len(s) for d, s in sorted(days.items())}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("dsp", nargs="?", default=os.path.join(os.environ.get("BMA_DATA_DIR") or "data", "analysis", "dsp.jsonl"))
    ap.add_argument("--out", default=os.path.join(os.environ.get("BMA_DATA_DIR") or "data", "reports", "tonal_mask.json"))
    a = ap.parse_args()
    days, bin_hz = load_states(a.dsp)
    entries, counts = learn(days, bin_hz)
    print("tonal_state records per day:", counts)
    print(
        f"criteria: core >= {CORE_SHARE:.0%} of states on >= {MIN_DAYS} days; clock >= {CLOCK_SHARE:.0%} on >= {MIN_DAYS} days "
        f"and on FS*p/q (q<=16) or the FS/864 comb; days need >= {MIN_STATES} states; +-{MERGE_BINS} bin merge"
    )
    print(f"learned {len(entries)} mask line(s):")
    for e in entries:
        r = clock_ratio(e["hz"], DF)
        print(
            f"  {e['hz']:9.1f} Hz  {e['cls']:5s} days_over={e['days_over']}  shares={e['share_by_day']}"
            + (f"  = FS*{r[0]}/{r[1]}" if r else "")
            + ("  [on FS/864 comb]" if on_comb(e["hz"], DF) else "")
        )
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    mask = {
        "version": 1,
        "fs": FS,
        "tolerance_hz": 4.0,
        "lines_hz": [e["hz"] for e in entries],
        "learned_from": os.path.abspath(a.dsp),
        "criteria": {"core_share": CORE_SHARE, "clock_share": CLOCK_SHARE, "min_days": MIN_DAYS, "min_states": MIN_STATES},
        "entries": entries,
    }
    with open(a.out, "w") as f:
        json.dump(mask, f, indent=1)
    print("wrote", a.out)


if __name__ == "__main__":
    main()
