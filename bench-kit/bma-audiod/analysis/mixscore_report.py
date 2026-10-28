#!/usr/bin/env python3
"""Read-only summary of the mixscore comparison (mixscore_compare records of mixscore.jsonl).
    python mixscore_report.py [PATH ...] [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--json]
PATH: a mixscore.jsonl file or a directory holding mixscore.jsonl (and mixscore.jsonl.1); default <BMA_DATA_DIR or ./data>/analysis.
--from / --to: inclusive UTC dates. Nothing is written. Exit 0 with a summary, 2 on a usage error, 1 when no record matched.
Definitions are in contracts/specs/mixscore.md: an event is one species in one 3 s window; both = reported by the per-beam birds
plugin and by the mean mix; the confidence comparison covers the `both` events only (mix minus the best beam)."""

import argparse, calendar, json, os, sys, time


def parse_day(s):
    try:
        return calendar.timegm(time.strptime(s, "%Y-%m-%d"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {s!r}") from None


def files_of(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            for n in ("mixscore.jsonl.1", "mixscore.jsonl"):
                f = os.path.join(p, n)
                if os.path.isfile(f):
                    out.append(f)
        elif os.path.isfile(p):
            out.append(p)
    return out


def load(paths, t_from=None, t_to=None):
    """-> (compare records within [t_from, t_to_end), bad line count). Unknown record kinds and versions are skipped."""
    recs, bad = [], 0
    for f in files_of(paths):
        with open(f, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                if not isinstance(r, dict) or r.get("type") != "mixscore_compare" or r.get("v") != 1:
                    continue
                t = r.get("t")
                if not isinstance(t, (int, float)):
                    bad += 1
                    continue
                if t_from is not None and t < t_from:
                    continue
                if t_to is not None and t >= t_to + 86400:
                    continue
                recs.append(r)
    recs.sort(key=lambda r: r["t"])
    return recs, bad


def summarize(recs):
    s = {
        "minutes": len(recs),
        "partial_minutes": sum(1 for r in recs if r.get("partial")),
        "windows": {k: 0 for k in ("gated", "scored", "paired", "unpaired", "dropped_speech", "dropped_unknown")},
        "events": {"both": 0, "beam_only": 0, "mix_only": 0},
        "groups": {},
    }
    n = hi = 0
    wb = wm = 0.0
    for r in recs:
        for k in s["windows"]:
            s["windows"][k] += int(r["windows"][k])
        for k in s["events"]:
            s["events"][k] += int(r["events"][k])
        bc = r["both_conf"]
        if bc["n"]:
            n += bc["n"]
            wb += bc["best_beam_mean"] * bc["n"]
            wm += bc["mix_mean"] * bc["n"]
            hi += bc["mix_higher"]
        for g, v in r["groups"].items():
            d = s["groups"].setdefault(g, {"both": 0, "beam_only": 0, "mix_only": 0})
            for k in d:
                d[k] += int(v[k])
    ev = s["events"]
    total_beam = ev["both"] + ev["beam_only"]
    total_mix = ev["both"] + ev["mix_only"]
    s["both_conf"] = {
        "n": n,
        "best_beam_mean": round(wb / n, 4) if n else None,
        "mix_mean": round(wm / n, 4) if n else None,
        "diff_mean": round((wm - wb) / n, 4) if n else None,
        "mix_higher_share": round(hi / n, 4) if n else None,
    }
    s["beam_events"], s["mix_events"] = total_beam, total_mix
    s["beam_only_share_of_beam_events"] = round(ev["beam_only"] / total_beam, 4) if total_beam else None
    s["mix_only_share_of_mix_events"] = round(ev["mix_only"] / total_mix, 4) if total_mix else None
    return s


def text(s, t_from, t_to):
    ev, w, bc = s["events"], s["windows"], s["both_conf"]
    f = lambda v: "n/a" if v is None else f"{v:.4f}"
    L = [
        f"mixscore comparison: {s['minutes']} minutes ({s['partial_minutes']} partial)"
        + (f", from {time.strftime('%Y-%m-%d', time.gmtime(t_from))}" if t_from is not None else "")
        + (f" to {time.strftime('%Y-%m-%d', time.gmtime(t_to))}" if t_to is not None else ""),
        f"windows: scored {w['scored']}, paired with birds {w['paired']}, unpaired {w['unpaired']}, gated {w['gated']}, "
        f"dropped for speech {w['dropped_speech']}, dropped speech-unknown {w['dropped_unknown']}",
        f"events (species x 3 s window): both {ev['both']}, only per-beam {ev['beam_only']}, only mix {ev['mix_only']}",
        f"  per-beam adds {f(s['beam_only_share_of_beam_events'])} of its events over the mix; the mix adds {f(s['mix_only_share_of_mix_events'])} of its events",
        f"confidence on the {bc['n']} shared events: best beam {f(bc['best_beam_mean'])}, mix {f(bc['mix_mean'])}, mix minus best beam {f(bc['diff_mean'])}, "
        f"mix higher in {f(bc['mix_higher_share'])} of them",
        "by group (both / only per-beam / only mix):",
    ]
    for g, v in sorted(s["groups"].items()):
        L.append(f"  {g:12s} {v['both']:6d} {v['beam_only']:6d} {v['mix_only']:6d}")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--from", dest="t_from", type=parse_day, help="first UTC day, YYYY-MM-DD")
    ap.add_argument("--to", dest="t_to", type=parse_day, help="last UTC day (inclusive), YYYY-MM-DD")
    ap.add_argument("--json", action="store_true", help="print the summary as one JSON object")
    a = ap.parse_args(argv)
    paths = a.paths or [os.path.join(os.environ.get("BMA_DATA_DIR") or "data", "analysis")]
    if a.t_from is not None and a.t_to is not None and a.t_to < a.t_from:
        ap.error("--to is before --from")
    recs, bad = load(paths, a.t_from, a.t_to)
    if not recs:
        print("no mixscore_compare records in range" + (f" ({bad} unreadable lines)" if bad else ""), file=sys.stderr)
        return 1
    s = summarize(recs)
    s["bad_lines"] = bad
    print(json.dumps(s, sort_keys=True) if a.json else text(s, a.t_from, a.t_to) + (f"\n{bad} unreadable lines skipped" if bad else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
