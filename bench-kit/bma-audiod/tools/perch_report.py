#!/usr/bin/env python3
"""perch_report: Perch vs BirdNET top-1 agreement over analysis/perch.jsonl and analysis/birds.jsonl (contract perch-report 1.0.0).

    perch_report.py --data DIR [--out FILE] [--min-logit X] [--top N] [--max-disagreements N] [--generated-s S]

Read-only. Metadata only: no audio, no embeddings, no coordinates. stdout = terse table; --out = JSON report (one line).
--out inside DIR is refused (exit 2). Exit 0 ok, 2 refused or unreadable input."""
import argparse, bisect, json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
for _d in (HERE, os.path.join(HERE, "..", "analysis")):
    sys.path.insert(0, _d)
import labels as L  # noqa: E402

RECORD_VERSION = 1
WIN_S, BIRDS_WIN_S, MIN_OVERLAP_S = 5.0, 3.0, 2.0        # = perch.py WIN_S, BIRDS_WIN_S, MIN_OVERLAP_S
CONF_BINS = ((0.0, 0.5), (0.5, 0.7), (0.7, 0.85), (0.85, 1.0))
# = perch.py HUMAN_VOICE_LABELS (pinned by test_perch_report.py); plus labels.is_speech_label and its "Human ..." prefix rule
PERCH_VOICE_LABELS = frozenset(s.lower() for s in (
    "Speech", "Male_speech_and_man_speaking", "Female_speech_and_woman_speaking", "Child_speech_and_kid_speaking",
    "Conversation", "Chatter", "Speech_synthesizer", "Human_voice", "Whispering", "Shout", "Yell", "Screaming",
    "Singing", "Male_singing", "Female_singing", "Laughter", "Chuckle_and_chortle", "Giggle", "Crying_and_sobbing",
    "Cough", "Sneeze", "Breathing", "Gasp", "Sigh", "Burping_and_eructation", "Respiratory_sounds", "Crowd", "Cheering",
    "Human_group_actions", "Homo sapiens"))
CATS = ("both_same_species", "both_same_genus", "both_differ", "perch_only", "birds_only", "both_silent")


def hidden(label):
    """The hidden-label rule: labels.is_speech_label (Human prefix, SPEECH_LABELS) plus Perch's FSD50k human-voice classes."""
    n = (label or "").strip()
    return L.is_speech_label(n) or L.is_speech_label(n.replace("_", " ")) or n.lower() in PERCH_VOICE_LABELS


def genus(sci):
    return (sci or "").strip().lower().split(" ")[0]


def conf_bin(c):
    for i, (lo, hi) in enumerate(CONF_BINS):
        if c < hi or i == len(CONF_BINS) - 1:
            return "%.2f-%.2f" % CONF_BINS[i]


def rate(agree, n):
    return round(agree / n, 4) if n else None


def valid_perch(o):
    return (o.get("k") == "perch" and L.is_num(o.get("t")) and o["t"] > 0 and o.get("src") in ("mixed", "loudest")
            and isinstance(o.get("top"), list) and all(isinstance(x, list) and len(x) == 2 and isinstance(x[0], str) for x in o["top"])
            and (o.get("best_species") is None or (isinstance(o["best_species"], list) and len(o["best_species"]) == 2
                                                   and isinstance(o["best_species"][0], str) and L.is_num(o["best_species"][1]))))


def tally(rows):
    """rows: [(key, agreed)] -> sorted [{key, compared, agree, rate}]."""
    acc = {}
    for k, ok in rows:
        a = acc.setdefault(k, [0, 0])
        a[0] += 1
        a[1] += 1 if ok else 0
    return [{"key": k, "compared": a[0], "agree": a[1], "rate": rate(a[1], a[0])} for k, a in sorted(acc.items(), key=lambda kv: kv[0])]


def build_report(perch_rows, bird_rows, generated_s, min_logit=None, max_disagreements=1000):
    rows = [o for o in perch_rows if valid_perch(o)]
    if min_logit is None:
        ag = [o for o in perch_rows if o.get("k") == "perch_agreement" and L.is_num(o.get("min_logit"))]
        min_logit = float(max(ag, key=lambda o: o.get("t", 0))["min_logit"]) if ag else None
    named_all, loud = set(), {}
    groups = {}
    for o in rows:
        bs = o.get("best_species")
        if bs and min_logit is not None and bs[1] >= min_logit and not any(hidden(x[0]) for x in o["top"]):
            named_all.add(bs[0].strip().lower())
        if o["src"] == "loudest":
            loud[float(o["t"])] = o
    t_lo = min((o["t"] for o in rows), default=None)
    t_hi = max((o["t"] for o in rows), default=None)
    span = [o for o in bird_rows if L.valid_bird(o) and o["k"] == "bird" and t_lo is not None and t_lo - WIN_S <= o["t"] <= t_hi + BIRDS_WIN_S]
    hid_t = sorted(o["t"] for o in span if hidden(o["common"]) or hidden(o.get("scientific") or ""))
    birds = sorted((o for o in span if not o.get("non_species") and not hidden(o["common"]) and not hidden(o.get("scientific") or "")),
                   key=lambda o: o["t"])
    excluded = 0
    wins = []
    for t in sorted(loud):
        o = loud[t]
        if (any(hidden(x[0]) for x in o["top"]) or (o.get("best_species") and hidden(o["best_species"][0]))
                or bisect.bisect_right(hid_t, t + 1.0) > bisect.bisect_left(hid_t, t - 3.0)):
            excluded += 1
            continue
        wins.append(o)
        groups[o.get("top1_group")] = groups.get(o.get("top1_group"), 0) + 1
    b_t = [o["t"] for o in birds]
    events = {}
    for e in L.iter_events(birds):
        events.setdefault(e.key, []).append(e)
    cat_n = {k: 0 for k in CATS}
    by_sp, by_beam, by_bin, by_hour, dis = [], [], [], [], []
    for o in wins:
        t1 = float(o["t"])
        op = None
        # birds rows b with min(t1, b) - max(t1 - 5, b - 3) >= 2 s are exactly b in [t1 - 3, t1 + 1]
        for i in range(bisect.bisect_left(b_t, t1 - (WIN_S - MIN_OVERLAP_S)), bisect.bisect_right(b_t, t1 + (BIRDS_WIN_S - MIN_OVERLAP_S))):
            b = birds[i]
            if op is None or b["confidence"] > op["confidence"]:
                op = b
        bs = o.get("best_species")
        named = bs[0] if (bs and min_logit is not None and bs[1] >= min_logit) else None
        if named is None:
            cat_n["both_silent" if op is None else "birds_only"] += 1
            continue
        if op is None:
            cat_n["perch_only"] += 1
            continue
        key = L.species_key(op)
        same = named.strip().lower() == (op.get("scientific") or "").strip().lower()
        cat = "both_same_species" if same else ("both_same_genus" if genus(named) == genus(op.get("scientific")) else "both_differ")
        cat_n[cat] += 1
        by_sp.append((key, same))
        by_beam.append((o["beam"] if isinstance(o.get("beam"), int) else 0, same))
        by_bin.append((conf_bin(op["confidence"]), same))
        by_hour.append((int(t1 // 3600 % 24), same))
        if not same:
            ev = next((e for e in events.get(key, []) if e.t_start - L.EVENT_GAP_S <= op["t"] <= e.t_end + L.EVENT_GAP_S), None)
            dis.append({"event_id": ev.event_id if ev else None, "t": t1, "beam": o.get("beam"),
                        "birdnet": {"species": op.get("scientific") or "", "common": op["common"], "score": round(op["confidence"], 4)},
                        "perch": {"species": named, "logit": round(float(bs[1]), 3)}, "same_genus": cat == "both_same_genus"})
    n_cmp = len(by_sp)
    n_same = cat_n["both_same_species"]
    seen = {}
    for b in birds:
        k = L.species_key(b)
        c = seen.setdefault(k, [b["common"], 0])
        c[1] += 1
    never = sorted(({"key": k, "common": v[0], "reports": v[1]} for k, v in seen.items() if k not in named_all),
                   key=lambda d: (-d["reports"], d["key"]))
    sp = tally(by_sp)
    return {"k": "perch_report", "v": RECORD_VERSION, "generated_s": generated_s,
            "input": {"perch_records": len(rows), "perch_windows": len(loud), "birds_rows": len(birds), "excluded_human_voice": excluded},
            "min_logit": min_logit,
            "windows": dict(total=len(wins), **cat_n),
            "agreement": {"compared": n_cmp, "agree": n_same, "rate": rate(n_same, n_cmp)},
            "per_species": sp,
            "by_beam": tally(by_beam), "by_confidence_bin": tally(by_bin), "by_hour_utc": tally(by_hour),
            "disagreements": {"total": len(dis), "listed": min(len(dis), max_disagreements), "items": dis[:max_disagreements]},
            "coverage": {"birdnet_species": len(seen), "named_by_perch": len(seen) - len(never), "never_named": never},
            "perch_top1_groups": dict(sorted((str(k), v) for k, v in groups.items()))}


def fmt_rate(r):
    return "  n/a" if r is None else "%5.1f" % (100 * r)


def table(rep, top=20):
    a, w = rep["agreement"], rep["windows"]
    out = ["perch_report v%d min_logit=%s" % (rep["v"], rep["min_logit"]),
           "windows %d: same_species %d same_genus %d differ %d perch_only %d birds_only %d silent %d; excluded_human %d" % (
               w["total"], w["both_same_species"], w["both_same_genus"], w["both_differ"], w["perch_only"], w["birds_only"], w["both_silent"],
               rep["input"]["excluded_human_voice"]),
           "top-1 agreement %s%% (%d/%d)" % (fmt_rate(a["rate"]).strip(), a["agree"], a["compared"])]
    for title, k in (("species", "per_species"), ("beam", "by_beam"), ("confidence", "by_confidence_bin"), ("hour_utc", "by_hour_utc")):
        rows = rep[k]
        if k == "per_species":
            rows = sorted(rows, key=lambda r: (-r["compared"], r["key"]))[:top]
        out.append("-- by %s" % title)
        out += ["%-32s %5d %5d %s%%" % (r["key"], r["compared"], r["agree"], fmt_rate(r["rate"])) for r in rows]
    d = rep["disagreements"]
    out.append("-- disagreements %d (listed %d)" % (d["total"], d["listed"]))
    out += ["%s beam=%s birdnet=%s %.4f perch=%s %.3f%s" % (i["event_id"], i["beam"], i["birdnet"]["species"], i["birdnet"]["score"],
                                                        i["perch"]["species"], i["perch"]["logit"], " genus" if i["same_genus"] else "")
            for i in d["items"][:top]]
    c = rep["coverage"]
    out.append("-- coverage: perch names %d of %d BirdNET species; never named:" % (c["named_by_perch"], c["birdnet_species"]))
    out += ["%-32s %5d" % (r["key"], r["reports"]) for r in c["never_named"][:top]]
    return "\n".join(out) + "\n"


def _inside(path, directory):
    p, d = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(directory))
    return p == d or p.startswith(d + os.sep)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", required=True, help="data directory holding analysis/perch.jsonl and analysis/birds.jsonl (read only)")
    ap.add_argument("--out", help="JSON report file; refused inside --data")
    ap.add_argument("--min-logit", type=float, default=None, help="Perch names a species at logit >= this (default: the latest perch_agreement record's min_logit)")
    ap.add_argument("--top", type=int, default=20, help="rows per table section")
    ap.add_argument("--max-disagreements", type=int, default=1000)
    ap.add_argument("--generated-s", type=float, default=None)
    a = ap.parse_args(argv)
    if a.out and _inside(a.out, a.data):
        sys.stderr.write("refusing to write --out inside --data\n")
        return 2
    try:
        perch = L.read_rotated(os.path.join(a.data, "analysis", "perch.jsonl"))
        birds = L.read_rotated(os.path.join(a.data, "analysis", "birds.jsonl"))
    except OSError as ex:
        sys.stderr.write("cannot read input: %s\n" % type(ex).__name__)
        return 2
    rep = build_report(perch, birds, time.time() if a.generated_s is None else a.generated_s, a.min_logit, a.max_disagreements)
    sys.stdout.write(table(rep, a.top))
    if a.out:
        with open(a.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rep, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
