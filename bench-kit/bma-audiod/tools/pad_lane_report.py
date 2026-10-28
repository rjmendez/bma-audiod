#!/usr/bin/env python3
"""pad_lane_report: top-1 agreement and label flips between a production stream and its pad10 stream. Read only; stdout only.

    pad_lane_report.py --data DIR [--kind tags|clap] [--tol-s S] [--top N]

Reads <DIR>/analysis/tags.jsonl + tags_pad.jsonl (or clap.jsonl + clap_pad.jsonl). A pad record pairs with the nearest unused
production record within --tol-s (default 0.5). Production records flagged speech_active are not paired. Output: one JSON line
{pairs, agree, agree_rate, flips[[prod_top1, pad_top1, n]], prod_only, pad_only, speech_leak, mean_ms_written, variants, pad_s_values, warnings}.
speech_leak = pad records within --tol-s of a speech_active production record (must be 0). mean_ms_written = mean ms over the pad records written
(status.json ms_per_window averages all windows). warnings: mixed variant / pad_s values; unpaired share > UNPAIRED_WARN (grid drift). Exit 0 ok, 2 unreadable input.
Owner_verdict comparison (McNemar): tools/eval_models.py --variant tags_pad=tags_pad.jsonl:class:prob (branch evaluator)."""

import argparse, bisect, json, os, sys

NONE = "(none)"
UNPAIRED_WARN = 0.2


def read(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue  # torn line
            if isinstance(r, dict) and isinstance(r.get("t"), (int, float)) and isinstance(r.get("top"), list):
                rows.append(r)
    rows.sort(key=lambda r: r["t"])
    return rows


def top1(r):
    return r["top"][0][0] if r["top"] else NONE


def compare(prod, pad, tol_s=0.5, top=10):
    sp_ts = [r["t"] for r in prod if r.get("speech_active")]
    leak = sum(1 for r in pad if any(abs(t - r["t"]) <= tol_s for t in sp_ts))
    prod = [r for r in prod if not r.get("speech_active")]
    ts, used, pairs, agree, flips, ms = [r["t"] for r in prod], set(), 0, 0, {}, []
    for r in pad:
        if isinstance(r.get("ms"), (int, float)):
            ms.append(r["ms"])
        i = bisect.bisect_left(ts, r["t"])
        cand = [j for j in (i - 1, i) if 0 <= j < len(ts) and j not in used and abs(ts[j] - r["t"]) <= tol_s]
        if not cand:
            continue
        j = min(cand, key=lambda j: abs(ts[j] - r["t"]))
        used.add(j)
        pairs += 1
        a, b = top1(prod[j]), top1(r)
        if a == b:
            agree += 1
        else:
            flips[(a, b)] = flips.get((a, b), 0) + 1
    fl = sorted(([a, b, n] for (a, b), n in flips.items()), key=lambda x: (-x[2], x[0], x[1]))[:top]
    variants = sorted({r["variant"] for r in pad if r.get("variant") is not None})
    pad_s = sorted({r["pad_s"] for r in pad if r.get("pad_s") is not None})
    total = len(prod) + len(pad) - pairs
    warns = []
    if len(variants) > 1:
        warns.append("mixed variant: " + ",".join(map(str, variants)))
    if len(pad_s) > 1:
        warns.append("mixed pad_s: " + ",".join(map(str, pad_s)))
    if total and (total - pairs) / total > UNPAIRED_WARN:
        warns.append("unpaired %d of %d windows (grid drift or dropped windows)" % (total - pairs, total))
    return {
        "pairs": pairs,
        "agree": agree,
        "agree_rate": round(agree / pairs, 4) if pairs else None,
        "flips": fl,
        "prod_only": len(prod) - pairs,
        "pad_only": len(pad) - pairs,
        "speech_leak": leak,
        "mean_ms_written": round(sum(ms) / len(ms), 1) if ms else None,
        "variants": variants,
        "pad_s_values": pad_s,
        "warnings": warns,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True)
    ap.add_argument("--kind", choices=("tags", "clap"), default="tags")
    ap.add_argument("--tol-s", type=float, default=0.5)
    ap.add_argument("--top", type=int, default=10)
    a = ap.parse_args(argv)
    d = os.path.join(a.data, "analysis")
    try:
        prod, pad = read(os.path.join(d, a.kind + ".jsonl")), read(os.path.join(d, a.kind + "_pad.jsonl"))
    except OSError as e:
        print("pad_lane_report: " + str(e), file=sys.stderr)
        return 2
    rep = compare(prod, pad, a.tol_s, a.top)
    rep["kind"] = a.kind
    print(json.dumps(rep, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
