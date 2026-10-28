#!/usr/bin/env python3
"""eval_models: owner_verdict labels vs each model variant's output for the same events (contract eval-report 1.0.0).

    eval_models.py --data DIR [--labels-dir DIR] [--out FILE] [--variant NAME=REL[:level[:score]]]... [--map FILE] [--taxa FILE]
                   [--confusions FILE] [--min-n N] [--min-species-n N] [--min-bin-n N] [--top N] [--generated-s S]

Read only. Metadata only: no audio, no embeddings, no coordinates, no path. stdout = terse table; --out = JSON report (one line).
--out inside --data or --labels-dir is refused (exit 2). Exit 0 ok, 2 refused or unreadable input."""

import argparse, bisect, json, math, os, re, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
BMA = os.path.dirname(HERE)
for _d in (HERE, os.path.join(BMA, "analysis")):
    sys.path.insert(0, _d)
import labels as L  # noqa: E402
import perch_report as PR  # noqa: E402

RECORD_VERSION = 1
MIN_N, MIN_SPECIES_N, MIN_BIN_N = 20, 10, 10
LEAD_S, TRAIL_S = 1.0, 3.0  # a window row (end time t) belongs to an event when t_start - 1 <= t <= t_end + 3 (= perch_report's overlap rule)
N_BINS = 10
DEFAULT_MAP = os.path.join(HERE, "eval_neighbourhood.json")
DEFAULT_TAXA = os.path.join(BMA, "ui", "taxa.json")
DEFAULT_CONFUSIONS = os.path.join(BMA, "analysis", "confusions.json")
BINOMIAL = re.compile(r"^[A-Z][a-z]+ [a-z]+$")
# name -> (file under <data>/analysis, row filter, level, score kind); `birdnet` reads birds.jsonl rows itself
DEFAULT_VARIANTS = (("perch", "perch.jsonl", "species", "logit"), ("tags", "tags.jsonl", "class", "prob"), ("clap", "clap.jsonl", "class", "prob"))
LEVELS, SCORES = ("species", "class"), ("prob", "logit")
BIN_KEYS = tuple("%.1f-%.1f" % (i / N_BINS, (i + 1) / N_BINS) for i in range(N_BINS))


def rate(a, n):
    return round(a / n, 4) if n else None


def key_of(label):
    return (label or "").strip().lower()


def mcnemar_exact(b, c):
    """Two-sided exact McNemar p over the discordant pairs (b = A right B wrong, c = A wrong B right): 2 * P(X <= min(b, c)), X ~ Bin(b + c, 1/2), capped at 1."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1))
    return min(1.0, 2 * tail / 2**n)


def bin_index(conf):
    return min(int(round(conf * N_BINS, 9)), N_BINS - 1)


# ------------------------------------------------------------------------------------------------ variants
def valid_row(o):
    return (
        isinstance(o, dict)
        and L.is_num(o.get("t"))
        and o["t"] > 0
        and isinstance(o.get("top"), list)
        and o["top"]
        and all(isinstance(x, (list, tuple)) and len(x) == 2 and isinstance(x[0], str) and L.is_num(x[1]) for x in o["top"])
    )


def mv_of(o):
    mv = o.get("mv", o.get("model_version"))
    return mv if isinstance(mv, str) and mv else None


class Variant:
    """One model variant: time-sorted rows, each (t, [(label key, score)], hidden). Rows of one file with several `mv` values are separate variants."""

    def __init__(self, name, level, score, mvs, rows, binomials=frozenset()):
        self.name, self.level, self.score, self.mvs, self.binomials = name, level, score, mvs, binomials
        rows.sort(key=lambda r: r[0])
        self.t = [r[0] for r in rows]
        self.rows = rows

    def window(self, t_lo, t_hi):
        return self.rows[bisect.bisect_left(self.t, t_lo) : bisect.bisect_right(self.t, t_hi)]


def make_variants(base, level, score, rows, binomials=frozenset()):
    """rows: [(t, mv, [(label, score)], hidden)] -> [Variant]; the name is `base` for one mv group, `base@mv` per group otherwise."""
    groups = {}
    for t, mv, top, hid in rows:
        groups.setdefault(mv, []).append((t, top, hid))
    out = []
    for mv in sorted(groups, key=lambda m: (m is None, m or "")):
        name = base if len(groups) == 1 else "%s@%s" % (base, mv if mv is not None else "unrecorded")
        out.append(Variant(name, level, score, [mv] if mv is not None else [], groups[mv], binomials))
    return out


def rows_from_records(recs, only=None):
    """-> (rows, binomials): rows [(t, mv, [(label key, score)], hidden)]; binomials = label keys written as `Genus species` (a scientific name)."""
    out, binoms = [], set()
    for o in recs:
        if valid_row(o) and (only is None or only(o)):
            top = [(key_of(x[0]), float(x[1])) for x in o["top"]]
            hid = any(PR.hidden(x[0]) for x in o["top"])
            out.append((float(o["t"]), mv_of(o), top, hid))
            binoms |= {key_of(x[0]) for x in o["top"] if BINOMIAL.match(x[0])}
    return out, frozenset(binoms)


def loudest(o):
    """The perch rows that count: the loudest-beam rows (`src` = loudest)."""
    return o.get("src") == "loudest"


def birdnet_variant(bird_rows):
    """BirdNET: every valid bird / bird_filtered row is one candidate at its confidence (the events' own rows)."""
    rows = []
    for o in bird_rows:
        if L.valid_bird(o):
            rows.append((float(o["t"]), mv_of(o), [(L.species_key(o), float(o["confidence"]))], PR.hidden(o["common"]) or PR.hidden(o.get("scientific") or "")))
    return make_variants("birdnet", "species", "prob", rows)


# ------------------------------------------------------------------------------------------------ neighbourhood
def load_json(path):
    try:
        with open(path, "rb") as f:
            return json.loads(f.read())
    except (OSError, ValueError):
        return None


def class_map(path):
    """-> {variant base name: {label key: group}}; unreadable file or wrong structure -> {}."""
    d = load_json(path)
    if not isinstance(d, dict) or d.get("version") != 1 or not isinstance(d.get("groups"), dict):
        return {}
    out = {}
    for base, groups in d["groups"].items():
        if isinstance(groups, dict):
            out[base] = {key_of(lab): g for g, labs in groups.items() if isinstance(labs, list) for lab in labs if isinstance(lab, str)}
    return out


def lookalikes(path):
    """-> {frozenset({sci a, sci b})} from analysis/confusions.json; unreadable -> empty."""
    d = load_json(path)
    out = set()
    if isinstance(d, dict):
        for name, e in d.items():
            if isinstance(e, dict) and isinstance(e.get("scientific"), str) and isinstance(e.get("likely_as_scientific"), str):
                out.add(frozenset((key_of(e["scientific"]), key_of(e["likely_as_scientific"]))))
    return out


class Near:
    """Near-miss: same genus (both keys scientific: seen as a scientific name, or first word a genus of the taxa table) or a listed lookalike pair.
    Symmetric; equal keys are exact, not near."""

    def __init__(self, sci_keys, pairs, genera=frozenset()):
        self.sci, self.pairs, self.genera = sci_keys, pairs, genera

    def genus(self, k):
        g = k.split(" ")[0]
        return g if " " in k and (k in self.sci or g in self.genera) else None

    def __call__(self, a, b):
        if a == b:
            return False
        ga, gb = self.genus(a), self.genus(b)
        return (ga is not None and ga == gb) or frozenset((a, b)) in self.pairs


# ------------------------------------------------------------------------------------------------ truth
def truth_of(rec, ev):
    """-> (kind, truth key, claim key).  kind: species (correct, or wrong + corrected_key), reject (wrong without a species named), or None (unsure / artifact)."""
    claim = rec.get("species_key") or ev.key
    v = rec.get("verdict")
    if v == "correct":
        return "species", claim, claim
    if v == "wrong":
        ck = rec.get("corrected_key")
        if rec.get("wrong_reason") == "other_species" and isinstance(ck, str) and ck:
            return "species", key_of(ck), claim
        return "reject", None, claim
    return None, None, claim


def rank(rows):
    """Window rows -> [(label key, max score)] best first (score desc, key asc); None when no row."""
    if not rows:
        return None
    best = {}
    for _t, top, _h in rows:
        for lab, s in top:
            if s > best.get(lab, -math.inf):
                best[lab] = s
    return sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))


# ------------------------------------------------------------------------------------------------ report
def per_event(variant, ranked, truth, truth_group, near, gmap):
    """-> dict(exact1, near1, exact5, near5, conf, top1) for one event; None when the variant has no output."""
    top5 = ranked[:5]
    if variant.level == "species":
        keys = [k for k, _s in top5]
        exact1 = keys[0] == truth
        exact5 = truth in keys
        near1 = (not exact1) and near(truth, keys[0])
        near5 = (not exact5) and any(near(truth, k) for k in keys)
        return {"exact1": exact1, "near1": near1, "exact5": exact5, "near5": near5, "conf": top5[0][1], "top1": keys[0]}
    groups = [gmap.get(k) for k, _s in top5]
    return {"exact1": groups[0] == truth_group, "near1": False, "exact5": truth_group in groups, "near5": False, "conf": top5[0][1], "top1": None}


def calibration(variant, evs, min_n, min_bin_n):
    if variant.score != "prob" or any(not 0 <= e["conf"] <= 1 for e in evs):
        return {"status": "not_applicable", "reason": "score_not_probability", "n": len(evs), "ece": None, "ece_n": None, "bins": []}
    acc = [[0, 0, 0.0] for _ in range(N_BINS)]
    for e in evs:
        a = acc[bin_index(e["conf"])]
        a[0] += 1
        a[1] += 1 if e["exact1"] else 0
        a[2] += e["conf"]
    bins = []
    ece_n, ece_sum = 0, 0.0
    for i, (n, ok, sc) in enumerate(acc):
        if n == 0:
            continue
        good = n >= min_bin_n
        bins.append(
            {
                "bin": BIN_KEYS[i],
                "n": n,
                "mean_conf": round(sc / n, 4) if good else None,
                "owner_correct_rate": rate(ok, n) if good else None,
                "status": "ok" if good else "not_measurable",
            }
        )
        if good:
            ece_n += n
            ece_sum += abs(ok / n - sc / n) * n
    if len(evs) < min_n or ece_n < min_n:
        return {"status": "not_measurable", "reason": "n_below_min", "n": len(evs), "ece": None, "ece_n": ece_n, "min_n": min_n, "bins": bins}
    return {"status": "ok", "reason": None, "n": len(evs), "ece": round(ece_sum / ece_n, 4), "ece_n": ece_n, "min_n": min_n, "bins": bins}


def build_report(
    label_recs, bird_rows, variants, gmaps, generated_s, taxa=None, pairs=frozenset(), min_n=MIN_N, min_species_n=MIN_SPECIES_N, min_bin_n=MIN_BIN_N
):
    taxa = taxa or L.Taxa()
    folded = L.fold(label_recs)
    events = {e.event_id: e for e in L.iter_events(bird_rows, taxa)}
    sci = {key_of(e.scientific) for e in events.values() if e.scientific} | {k for p in pairs for k in p}
    for v in variants:
        if v.level == "species":
            sci |= v.binomials
    near = Near(sci, pairs, frozenset(taxa.genus))
    inp = {
        "label_records": sum(1 for r in label_recs if isinstance(r, dict) and r.get("k") == "verdict"),
        "label_events": len(folded),
        "unmatched_labels": 0,
        "excluded_human_voice": 0,
        "excluded_unsure": 0,
        "excluded_artifact": 0,
        "species_truth": 0,
        "rejections": 0,
    }
    work = []  # (event, kind, truth, claim, {variant name: ranked or None})
    for eid in sorted(folded):
        rec = folded[eid]
        ev = events.get(eid)
        if ev is None:
            inp["unmatched_labels"] += 1
            continue
        kind, truth, claim = truth_of(rec, ev)
        if PR.hidden(ev.common) or PR.hidden(ev.scientific) or PR.hidden(claim) or (truth is not None and PR.hidden(truth)):
            inp["excluded_human_voice"] += 1
            continue
        ranks, hid = {}, False
        for v in variants:
            lo, hi = (ev.t_start - L.EVENT_GAP_S, ev.t_end + L.EVENT_GAP_S) if v.name.split("@")[0] == "birdnet" else (ev.t_start - LEAD_S, ev.t_end + TRAIL_S)
            rows = v.window(lo, hi)
            hid = hid or any(h for _t, _top, h in rows)
            ranks[v.name] = rank(rows)
        if hid:
            inp["excluded_human_voice"] += 1
            continue
        if kind is None:
            inp["excluded_" + ("unsure" if rec.get("verdict") == "unsure" else "artifact")] += 1
            continue
        inp["species_truth" if kind == "species" else "rejections"] += 1
        work.append((ev, kind, truth, claim, rec, ranks))
    out_vars, outcomes = [], {}
    for v in variants:
        gmap = gmaps.get(v.name.split("@")[0], {})
        evs, per, rej = {}, {}, {"n": 0, "repeats_claim": 0, "by_reason": {}}
        no_output = 0
        for ev, kind, truth, claim, rec, ranks in work:
            ranked = ranks[v.name]
            if ranked is None:
                no_output += 1
                continue
            if kind == "species":
                tgroup = None
                if v.level == "class":
                    own = ev.taxa_group if truth == ev.key else taxa.classify(truth, truth, False)[0]
                    tgroup = own
                e = per_event(v, ranked, truth, tgroup, near, gmap)
                evs[ev.event_id] = e
                per.setdefault(tgroup if v.level == "class" else truth, []).append(e)
            else:
                r = rej["by_reason"].setdefault(rec.get("wrong_reason") or "none", {"n": 0, "repeats_claim": 0})
                rej["n"] += 1
                r["n"] += 1
                if v.level == "species" and ranked[0][0] == claim:
                    rej["repeats_claim"] += 1
                    r["repeats_claim"] += 1
        outcomes[v.name] = evs
        n = len(evs)
        row = {"name": v.name, "level": v.level, "score": v.score, "mv": v.mvs, "n": n, "no_output": no_output, "min_n": min_n}
        if v.level == "class" and not gmap:
            row.update(status="not_measurable", reason="no_neighbourhood_map")
        elif n < min_n:
            row.update(status="not_measurable", reason="n_below_min")
        else:
            row.update(status="ok", reason=None)
        ok = row["status"] == "ok"
        c1, n1, c5, n5 = (sum(e[k] for e in evs.values()) for k in ("exact1", "near1", "exact5", "near5"))
        row["top1"] = {"exact": c1, "near": n1, "exact_rate": rate(c1, n), "with_near_rate": rate(c1 + n1, n)} if ok else None
        row["top5"] = {"exact": c5, "near": n5, "exact_rate": rate(c5, n), "with_near_rate": rate(c5 + n5, n)} if ok else None
        tab = []
        for k, es in sorted(per.items(), key=lambda kv: (-len(kv[1]), str(kv[0]))):
            m = len(es)
            good = m >= min_species_n and ok
            tab.append(
                {
                    "key": k if k is not None else "unmapped",
                    "n": m,
                    "top1_exact": sum(e["exact1"] for e in es) if good else None,
                    "top1_near": sum(e["near1"] for e in es) if good else None,
                    "top5_exact": sum(e["exact5"] for e in es) if good else None,
                    "top5_near": sum(e["near5"] for e in es) if good else None,
                    "top1_exact_rate": rate(sum(e["exact1"] for e in es), m) if good else None,
                    "status": "ok" if good else "not_measurable",
                }
            )
        row["per_truth"] = tab
        row["rejections"] = (
            None
            if v.level == "class"
            else {"n": rej["n"], "repeats_claim": rej["repeats_claim"], "by_reason": [dict(reason=k, **x) for k, x in sorted(rej["by_reason"].items())]}
        )
        row["calibration"] = calibration(v, list(evs.values()), min_n, min_bin_n)
        out_vars.append(row)
    pairs_out = []
    for i, a in enumerate(variants):
        for b in variants[i + 1 :]:
            ids = sorted(set(outcomes[a.name]) & set(outcomes[b.name]))
            p = {"a": a.name, "b": b.name, "n_paired": len(ids), "min_n": min_n}
            if a.level != b.level:
                p.update(status="not_measurable", reason="level_differs", top1=None, top5=None)
            elif len(ids) < min_n:
                p.update(status="not_measurable", reason="n_below_min", top1=None, top5=None)
            else:
                p.update(status="ok", reason=None)
                for k in ("1", "5"):
                    x = sum(1 for e in ids if outcomes[a.name][e]["exact" + k] and not outcomes[b.name][e]["exact" + k])
                    y = sum(1 for e in ids if not outcomes[a.name][e]["exact" + k] and outcomes[b.name][e]["exact" + k])
                    p["top" + k] = {"a_only": x, "b_only": y, "p": mcnemar_exact(x, y)}
            pairs_out.append(p)
    return {
        "k": "eval_report",
        "v": RECORD_VERSION,
        "generated_s": generated_s,
        "thresholds": {"min_n": min_n, "min_species_n": min_species_n, "min_bin_n": min_bin_n},
        "input": inp,
        "variants": out_vars,
        "pairs": pairs_out,
    }


def pct(r):
    return "n/a" if r is None else "%.1f%%" % (100 * r)


def table(rep, top=20):
    t, i = rep["thresholds"], rep["input"]
    out = [
        "eval_report v%d min_n=%d min_species_n=%d min_bin_n=%d" % (rep["v"], t["min_n"], t["min_species_n"], t["min_bin_n"]),
        "labels %d events %d species_truth %d rejections %d unmatched %d excluded: human %d unsure %d artifact %d"
        % (
            i["label_records"],
            i["label_events"],
            i["species_truth"],
            i["rejections"],
            i["unmatched_labels"],
            i["excluded_human_voice"],
            i["excluded_unsure"],
            i["excluded_artifact"],
        ),
        "-- variants: name level n top1 top1+near top5 top5+near ece",
    ]
    for v in rep["variants"]:
        if v["status"] != "ok":
            out.append("%-24s %-7s n=%d not_measurable (%s, min_n=%d)" % (v["name"], v["level"], v["n"], v["reason"], v["min_n"]))
            continue
        c = v["calibration"]
        out.append(
            "%-24s %-7s n=%d %s %s %s %s ece=%s"
            % (
                v["name"],
                v["level"],
                v["n"],
                pct(v["top1"]["exact_rate"]),
                pct(v["top1"]["with_near_rate"]),
                pct(v["top5"]["exact_rate"]),
                pct(v["top5"]["with_near_rate"]),
                c["ece"] if c["status"] == "ok" else c["status"] if c["status"] == "not_applicable" else "not_measurable",
            )
        )
    out.append("-- McNemar exact (a_only b_only p), top1 / top5")
    for p in rep["pairs"]:
        if p["status"] != "ok":
            out.append("%s vs %s n=%d not_measurable (%s, min_n=%d)" % (p["a"], p["b"], p["n_paired"], p["reason"], p["min_n"]))
        else:
            out.append(
                "%s vs %s n=%d top1 %d %d p=%.4g top5 %d %d p=%.4g"
                % (
                    p["a"],
                    p["b"],
                    p["n_paired"],
                    p["top1"]["a_only"],
                    p["top1"]["b_only"],
                    p["top1"]["p"],
                    p["top5"]["a_only"],
                    p["top5"]["b_only"],
                    p["top5"]["p"],
                )
            )
    for v in rep["variants"]:
        out.append("-- %s per %s (n top1 top5)" % (v["name"], "group" if v["level"] == "class" else "species"))
        for r in v["per_truth"][:top]:
            out.append(
                "%-32s %5d %s %s" % (r["key"], r["n"], pct(r["top1_exact_rate"]), "n/a" if r["top5_exact"] is None else "%d/%d" % (r["top5_exact"], r["n"]))
            )
        c = v["calibration"]
        if c["bins"]:
            out.append("-- %s calibration: bin n mean_conf owner_correct" % v["name"])
            out += [
                "%s %5d %s %s" % (b["bin"], b["n"], "n/a" if b["mean_conf"] is None else "%.4f" % b["mean_conf"], pct(b["owner_correct_rate"]))
                for b in c["bins"]
            ]
    out.append(
        "-- caveat: events are selected from birdnet output and a correct verdict sets truth = birdnet claim: birdnet top-1 is inflated; McNemar pairs against birdnet are on birdnet-selected events; events are treated as independent"
    )
    return "\n".join(out) + "\n"


def _inside(path, directory):
    p, d = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(directory))
    return p == d or p.startswith(d + os.sep)


def parse_variant(spec):
    name, _, rest = spec.partition("=")
    parts = rest.split(":")
    if not name or not parts[0] or len(parts) > 3 or (len(parts) > 1 and parts[1] not in LEVELS) or (len(parts) > 2 and parts[2] not in SCORES):
        raise ValueError("bad --variant %r: NAME=REL[:species|class[:prob|logit]]" % spec)
    return name, parts[0], parts[1] if len(parts) > 1 else "species", parts[2] if len(parts) > 2 else "prob"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", required=True, help="data directory holding analysis/*.jsonl and labels/labels.jsonl (read only)")
    ap.add_argument("--labels-dir", default=None, help="directory holding labels.jsonl (default <data>/labels)")
    ap.add_argument("--out", help="JSON report file; refused inside --data and --labels-dir")
    ap.add_argument(
        "--variant",
        action="append",
        default=[],
        help="extra variant NAME=REL[:level[:score]], REL under <data>/analysis; rows need t and top [[label, score], ...]",
    )
    ap.add_argument("--map", default=DEFAULT_MAP, help="class-level neighbourhood map (group -> labels per variant)")
    ap.add_argument("--taxa", default=DEFAULT_TAXA)
    ap.add_argument("--confusions", default=DEFAULT_CONFUSIONS)
    ap.add_argument("--min-n", type=int, default=MIN_N)
    ap.add_argument("--min-species-n", type=int, default=MIN_SPECIES_N)
    ap.add_argument("--min-bin-n", type=int, default=MIN_BIN_N)
    ap.add_argument("--top", type=int, default=20, help="rows per table section")
    ap.add_argument("--generated-s", type=float, default=None)
    a = ap.parse_args(argv)
    ldir = a.labels_dir or os.path.join(a.data, "labels")
    if a.out and (_inside(a.out, a.data) or _inside(a.out, ldir)):
        sys.stderr.write("refusing to write --out inside --data or --labels-dir\n")
        return 2
    try:
        extra = [parse_variant(s) for s in a.variant]
    except ValueError as ex:
        sys.stderr.write(str(ex) + "\n")
        return 2
    try:
        recs, _bad, _part = L.LabelStore(ldir).read()
        birds = L.read_rotated(os.path.join(a.data, "analysis", "birds.jsonl"))
        variants = birdnet_variant(birds)
        for name, rel, level, score in list(DEFAULT_VARIANTS) + extra:
            only = loudest if name == "perch" else None
            rows, binoms = rows_from_records(L.read_rotated(os.path.join(a.data, "analysis", rel)), only)
            variants += make_variants(name, level, score, rows, binoms)
    except OSError as ex:
        sys.stderr.write("cannot read input: %s\n" % type(ex).__name__)
        return 2
    rep = build_report(
        recs,
        birds,
        variants,
        class_map(a.map),
        time.time() if a.generated_s is None else a.generated_s,
        L.Taxa(a.taxa),
        lookalikes(a.confusions),
        a.min_n,
        a.min_species_n,
        a.min_bin_n,
    )
    sys.stdout.write(table(rep, a.top))
    if a.out:
        with open(a.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rep, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
