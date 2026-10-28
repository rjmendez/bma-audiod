#!/usr/bin/env python3
"""Read-only report on the 'bursts' plugin output (contracts/specs/burst-stats.md): the hourly table and the hours that look
like the cold-hour artifact, next to the outdoor temperature. Never writes anything.

    python -I bursts_report.py <data-dir>/analysis/bursts.jsonl [--ambient <data-dir>/history/ambient_temp.jsonl]
                               [--min-rate 0.3] [--min-band-db -70] [--min-contrast-db 10] [--max-spread-db 4] [--utc]
    python -I bursts_report.py --replay-dsp <data-dir>/analysis/dsp.jsonl --from "2026-10-26 03:00" --to "2026-10-26 06:00" [--utc]

Hour table (one row per UTC hour, shown in local time unless --utc): impulses, rate per second, the mean 8-16 kHz sub-band level
(dB, mean over the 1 kHz bands 8-9 ... 15-16 of the per-interval p50) and its contrast over the 2-6 kHz reference, the impulse
beam spread (p50, weighted by impulses), the share of impulses that fired on all 8 beams within +-1 ms, the within-3-dB beam count,
the modal gap and the regularity (weighted by gaps), the envelope modulation fundamental, and the air temperature.
ARTIFACT-LIKE = rate >= --min-rate AND 8-16 kHz level >= --min-band-db AND contrast >= --min-contrast-db AND spread <= --max-spread-db
(defaults from the 2026-10-26 03-06 h burst: about 1 impulse/s, 8-16 kHz at -42..-50 dB while 2-8 kHz stayed at -95, spread 1.8 dB).
It says the hour looks like that burst, NOT what the source is (see the spec: how to read the fields).
Temperature: the reader rule of contracts/specs/ambient-temp.md (nearest PAST hourly record, at most 3 h old, else unknown).
--replay-dsp feeds the 'transient' records of dsp.jsonl in a time range through the plugin's pure statistics (bursts.event_stats), so
the interval statistics can be checked on recorded events (20 ms resolution, no per-beam data: those fields are null)."""
import argparse, calendar, json, math, os, sys, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bursts as B  # noqa: E402

BAND_IDX = slice(2, 10)                       # sub-bands 8-9 ... 15-16 kHz of the ten 6..16 kHz bands


def read_jsonl(path, kind):
    """Records of the given k from a JSONL file (a torn / non-JSON line is skipped)."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("k") == kind and r.get("v", 1) == 1:
                out.append(r)
    return out


def ambient_lookup(ambient, t):
    """Reader rule of specs/ambient-temp.md: nearest PAST record with t_hour_utc <= floor(t / 3600) * 3600, at most 3 h older, else None."""
    h = int(t // 3600) * 3600
    best = None
    for r in ambient:
        th = r["t_hour_utc"]
        if th <= h and (best is None or th > best["t_hour_utc"]):
            best = r
    return best["temp_c"] if best is not None and h - best["t_hour_utc"] <= 3 * 3600 else None


def _wavg(pairs):
    """Weighted mean of (value, weight) pairs, ignoring null values; None when no weight is left."""
    pairs = [(v, w) for v, w in pairs if v is not None and w > 0]
    tw = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / tw if tw else None


def hourly(recs):
    """burst_stats records -> {hour_start_utc: row dict} (merges the intervals of each UTC hour by their t0)."""
    rows = {}
    for r in recs:
        rows.setdefault(int(r["t0"] // 3600) * 3600, []).append(r)
    out = {}
    for h, rs in sorted(rows.items()):
        sec = sum(r["analysed_s"] for r in rs)
        n = sum(r["impulses"]["n"] for r in rs)
        b = [(float(np.mean(r["bands_db"]["p50"][BAND_IDX])), r["analysed_s"]) for r in rs if r["bands_db"]]
        ref = [(r["ref_2_6k_db"], r["analysed_s"]) for r in rs]
        band, refv = _wavg(b), _wavg(ref)
        coin = sum(r["impulses"]["coincident_all_n"] or 0 for r in rs)
        w3 = [sum((i + 1) * c for i, c in enumerate(r["impulses"]["within3_hist"])) for r in rs if r["impulses"]["within3_hist"]]
        gn = sum(r["gaps"]["n"] for r in rs)
        modes = [(r["gaps"]["mode_s"], r["gaps"]["n"]) for r in rs if r["gaps"]["mode_s"] is not None]
        mod = [(r["mod"]["fund_hz"], r["impulses"]["n"]) for r in rs if r["mod"]["fund_hz"] is not None]
        reg = [(r["gaps"]["regularity"], r["gaps"]["n"]) for r in rs if r["gaps"]["regularity"] is not None]
        out[h] = {"windows": sum(r["win_used"] for r in rs), "excluded": sum(r["win_speech"] + r["win_unvetted"] + r["win_dead"] for r in rs),
                  "analysed_s": sec, "n": n, "rate": n / sec if sec else None, "band_db": band, "ref_db": refv,
                  "contrast_db": None if band is None or refv is None else band - refv,
                  "spread_db": _wavg([(r["impulses"]["spread_db"][1], r["impulses"]["n"]) for r in rs if r["impulses"]["spread_db"]]),
                  "coinc": coin / n if n else None, "within3": sum(w3) / n if n and w3 else None,
                  "mode_s": float(np.median(np.repeat([m for m, _ in modes], [max(1, w) for _, w in modes]))) if modes else None,
                  "regularity": _wavg(reg), "fund_hz": _wavg(mod), "gaps": gn,
                  "intervals": len(rs)}
    return out


def artifact_like(row, a):
    """True when the hour matches the burst signature (all four rules; any missing number means no)."""
    if None in (row["rate"], row["band_db"], row["contrast_db"], row["spread_db"]):
        return False
    return (row["rate"] >= a.min_rate and row["band_db"] >= a.min_band_db and row["contrast_db"] >= a.min_contrast_db
            and row["spread_db"] <= a.max_spread_db)


def _f(v, fmt):
    return "-" if v is None else format(v, fmt)


def table(rows, ambient, a, tz_utc):
    hdr = (f"{'hour':16s} {'win':>4s} {'impulses':>8s} {'rate/s':>7s} {'8-16k dB':>8s} {'contrast':>8s} {'spread':>6s} {'all8%':>6s} "
           f"{'in3dB':>5s} {'mode s':>7s} {'regul':>6s} {'fund Hz':>7s} {'temp C':>6s}  flag")
    lines = [hdr]
    flagged = []
    for h, r in rows.items():
        t = (time.gmtime if tz_utc else time.localtime)(h)
        lab = time.strftime("%Y-%m-%d %H:00", t)
        temp = ambient_lookup(ambient, h + 1800) if ambient is not None else None
        fl = artifact_like(r, a)
        if fl:
            flagged.append((h, temp))
        lines.append(f"{lab:16s} {r['windows']:4d} {r['n']:8d} {_f(r['rate'], '.3f'):>7s} {_f(r['band_db'], '.1f'):>8s} {_f(r['contrast_db'], '.1f'):>8s} "
                     f"{_f(r['spread_db'], '.1f'):>6s} {_f(None if r['coinc'] is None else 100 * r['coinc'], '.0f'):>6s} {_f(r['within3'], '.1f'):>5s} "
                     f"{_f(r['mode_s'], '.3f'):>7s} {_f(r['regularity'], '.2f'):>6s} {_f(r['fund_hz'], '.2f'):>7s} {_f(temp, '.1f'):>6s}  {'ARTIFACT-LIKE' if fl else ''}")
    known = [t for _, t in flagged if t is not None]
    lines.append(f"{len(flagged)} of {len(rows)} hours look like the artifact; temperature known for {len(known)} of them"
                 + (f" (range {min(known):.1f}..{max(known):.1f} C, {sum(1 for t in known if t < 10)} below 10 C)" if known else ""))
    lines.append("times are " + ("UTC" if tz_utc else "local") + "; temperature = nearest past hourly record at most 3 h old (specs/ambient-temp.md)")
    return "\n".join(lines)


def replay_dsp(path, t0, t1):
    """Transient records of dsp.jsonl with t0 <= t < t1 -> (impulses, gaps) of bursts.event_stats plus an independent hand estimate of
    the modal gap (largest 20 ms bin of the gap histogram, bin centre)."""
    t, lv, sp, ld = [], [], [], []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if '"transient"' not in line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("k") == "transient" and t0 <= r["t"] < t1:
                t.append(r["t"]); lv.append(r["level_db"]); sp.append(r["beam_spread_db"]); ld.append(r["loudest_beam"])
    t = np.array(t)
    imp, gaps = B.event_stats(t, lv, sp, loudest=ld, analysed_s=t1 - t0)
    hand = None
    if t.size > 5:
        g = np.diff(np.sort(t))
        h, e = np.histogram(g[g < 5.0], bins=np.arange(0.0, 5.0 + 0.02, 0.02))
        hand = round(float((e[int(np.argmax(h))] + e[int(np.argmax(h)) + 1]) / 2), 3)
    return imp, gaps, hand


def parse_time(s, utc):
    st = time.strptime(s, "%Y-%m-%d %H:%M")
    return float(calendar.timegm(st)) if utc else time.mktime(st)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bursts", nargs="?", help="bursts.jsonl")
    ap.add_argument("--ambient", help="history/ambient_temp.jsonl (optional)")
    ap.add_argument("--min-rate", type=float, default=0.3, help="impulses per second (default 0.3)")
    ap.add_argument("--min-band-db", type=float, default=-70.0, help="mean 8-16 kHz sub-band level, dB (default -70)")
    ap.add_argument("--min-contrast-db", type=float, default=10.0, help="8-16 kHz over the 2-6 kHz reference, dB (default 10)")
    ap.add_argument("--max-spread-db", type=float, default=4.0, help="median beam spread of the impulses, dB (default 4)")
    ap.add_argument("--utc", action="store_true", help="show and parse times in UTC (default local)")
    ap.add_argument("--replay-dsp", help="dsp.jsonl: replay its transient records through the statistics")
    ap.add_argument("--from", dest="t_from")
    ap.add_argument("--to", dest="t_to")
    a = ap.parse_args(argv)
    if a.replay_dsp:
        if not (a.t_from and a.t_to):
            ap.error("--replay-dsp needs --from and --to")
        t0, t1 = parse_time(a.t_from, a.utc), parse_time(a.t_to, a.utc)
        imp, gaps, hand = replay_dsp(a.replay_dsp, t0, t1)
        print(json.dumps({"from": a.t_from, "to": a.t_to, "impulses": imp, "gaps": gaps, "hand_mode_s_20ms_bins": hand}, indent=1))
        return 0
    if not a.bursts:
        ap.error("bursts.jsonl is required")
    recs = read_jsonl(a.bursts, "burst_stats")
    amb = read_jsonl(a.ambient, "ambient_temp") if a.ambient and os.path.exists(a.ambient) else None
    print(table(hourly(recs), amb, a, a.utc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
