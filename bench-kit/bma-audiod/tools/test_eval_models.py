#!/usr/bin/env python3
"""Tests for tools/eval_models.py (eval-report 1.0.0). Synthetic data only (contracts/tools/make_eval_report_fixtures.py), temp directories, exact values written
by hand, a positive twin for every negative case, no stubs. McNemar is checked against an independent enumeration, ECE by hand. Run:  python -I test_eval_models.py"""

import copy, io, itertools, json, math, os, shutil, sys, tempfile
from fractions import Fraction

HERE = os.path.dirname(os.path.abspath(__file__))
BMA = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(BMA, "contracts", "tools"))
sys.path.insert(0, os.path.join(BMA, "analysis"))
import eval_models as T  # noqa: E402
import make_eval_report_fixtures as M  # noqa: E402
import labels as L  # noqa: E402

fails, n_checks = [], 0


def check(name, cond, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  [" + str(detail) + "]"))
    if not cond:
        fails.append(name)


tmp = tempfile.mkdtemp(prefix="evtest_")
CONF = os.path.join(tmp, "confusions.json")
with open(CONF, "w", encoding="utf-8", newline="\n") as f:
    json.dump(M.CONFUSIONS, f)
CMAP = T.class_map(T.DEFAULT_MAP)
PAIRS = T.lookalikes(CONF)


def run_cli(argv):
    so, se = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        rc = T.main(argv)
        return rc, sys.stdout.getvalue(), sys.stderr.getvalue()
    finally:
        sys.stdout, sys.stderr = so, se


def report(scen=None, extra=None, **kw):
    """Build the report in memory from a scenario, like main() does."""
    birds, perch, tags, clap, recs = scen or M.scenario()
    variants = T.birdnet_variant(birds)
    for name, rows, level, score in (("perch", perch, "species", "logit"), ("tags", tags, "class", "prob"), ("clap", clap, "class", "prob")):
        only = T.loudest if name == "perch" else None
        rws, bn = T.rows_from_records(rows, only)
        variants += T.make_variants(name, level, score, rws, bn)
    variants += extra or []
    kw.setdefault("min_n", 12)
    kw.setdefault("min_species_n", 2)
    kw.setdefault("min_bin_n", 2)
    return T.build_report(recs, birds, variants, CMAP, M.GENERATED_S, L.Taxa(T.DEFAULT_TAXA), PAIRS, **kw)


def var(rep, name):
    return next(v for v in rep["variants"] if v["name"] == name)


def pair(rep, a, b):
    return next(p for p in rep["pairs"] if (p["a"], p["b"]) == (a, b))


# ---------------------------------------------------------------- McNemar against an independent enumeration
def mcnemar_reference(b, c):
    """Exact two-sided p by enumerating every outcome of Bin(n, 1/2): the sum of P(x) over all x with P(x) <= P(observed). Fractions, no tail doubling."""
    n = b + c
    if n == 0:
        return Fraction(1)
    prob = [Fraction(math.comb(n, x), 2**n) for x in range(n + 1)]
    obs = prob[min(b, c)]
    return min(Fraction(1), sum(p for p in prob if p <= obs))


bad = [(b, c) for b in range(0, 16) for c in range(0, 16) if Fraction(T.mcnemar_exact(b, c)) != mcnemar_reference(b, c)]
check("mcnemar_exact equals the independent enumeration for every (b, c) in 0..15 x 0..15", bad == [], bad[:3])
check("mcnemar_exact(10, 2) = 158/4096 = 0.03857421875 (hand: 2 * (1 + 12 + 66) / 2^12)", T.mcnemar_exact(10, 2) == 0.03857421875, T.mcnemar_exact(10, 2))
check("mcnemar_exact(2, 10) is the same (symmetric)", T.mcnemar_exact(2, 10) == 0.03857421875)
check("mcnemar_exact(0, 10) = 2 / 1024 = 0.001953125", T.mcnemar_exact(0, 10) == 0.001953125)
check("mcnemar_exact(5, 5) = 1.0 (capped) and mcnemar_exact(0, 0) = 1.0", T.mcnemar_exact(5, 5) == 1.0 and T.mcnemar_exact(0, 0) == 1.0)
check("mcnemar_exact(1, 2) = 1.0: 2 * (1 + 3) / 8 = 1", T.mcnemar_exact(1, 2) == 1.0)
try:
    from scipy.stats import binomtest

    sp_bad = [(b, c) for b in range(0, 21) for c in range(0, 21) if b + c and abs(T.mcnemar_exact(b, c) - binomtest(min(b, c), b + c, 0.5).pvalue) > 1e-12]
    check("mcnemar_exact equals scipy.stats.binomtest(min, n, 0.5) for every (b, c) in 0..20 x 0..20 (scipy present)", sp_bad == [], sp_bad[:3])
except ImportError:
    print("NOTE scipy not installed: the scipy cross-check was not run")


# ---------------------------------------------------------------- the McNemar numbers inside a report (a, b = 10, 2 on 30 paired events)
def synth(n_events, outcomes):
    """n_events chickadee events (all verdict correct); outcomes[name] = list of bool (top-1 right) per event. -> (birds, recs, variants)."""
    birds = [M.bird_row(M.t_of(i), M.CACH, 0.9) for i in range(n_events)]
    events = L.iter_events(birds)
    recs = [L.make_record(e, "correct", "metadata_only", t_label=M.BASE_T + 90000.0 + i, label_id="%016x" % (i + 1)) for i, e in enumerate(events)]
    variants = []
    for name, oc in outcomes.items():
        rows = [(M.t_of(i) + 0.5, None, [((M.CACH[0] if ok else M.BLJA[0]).lower(), 0.8)], False) for i, ok in enumerate(oc)]
        variants += T.make_variants(name, "species", "prob", rows)
    return birds, recs, variants


A = [True] * 20 + [False] * 2 + [False] * 8
B = [True] * 10 + [False] * 10 + [True] * 2 + [False] * 8
birds, recs, variants = synth(30, {"va": A, "vb": B})
R = T.build_report(recs, birds, variants, {}, 1.0, None, frozenset(), 20, 10, 10)
p = pair(R, "va", "vb")
check(
    "30 paired events, A right on 0-19, B right on 0-9 and 20-21: a_only 10, b_only 2, p = 158/4096",
    p["n_paired"] == 30 and p["status"] == "ok" and p["top1"] == {"a_only": 10, "b_only": 2, "p": 0.03857421875},
    p,
)
check("the same pair: top-5 equals top-1 here (one candidate per event): same counts and p", p["top5"] == p["top1"], p["top5"])
check(
    "va top-1 20/30, vb 12/30 (exact_rate 0.6667 and 0.4)",
    var(R, "va")["top1"]["exact"] == 20
    and var(R, "va")["top1"]["exact_rate"] == 0.6667
    and var(R, "vb")["top1"]["exact"] == 12
    and var(R, "vb")["top1"]["exact_rate"] == 0.4,
)
R2 = T.build_report(recs, birds, variants, {}, 1.0, None, frozenset(), 31, 10, 10)
p2 = pair(R2, "va", "vb")
check(
    "min_n 31 > 30 paired: the pair is not_measurable, no p, counts given (n_paired 30, min_n 31)",
    p2 == {"a": "va", "b": "vb", "n_paired": 30, "min_n": 31, "status": "not_measurable", "reason": "n_below_min", "top1": None, "top5": None},
    p2,
)
check(
    "min_n 30 = 30 paired: measurable (twin of the boundary)",
    pair(T.build_report(recs, birds, variants, {}, 1.0, None, frozenset(), 30, 10, 10), "va", "vb")["status"] == "ok",
)
v2 = var(R2, "va")
check(
    "variant n 30 < min_n 31: not_measurable, no rates, no hit counts, n and min_n given",
    v2["status"] == "not_measurable"
    and v2["top1"] is None
    and v2["top5"] is None
    and v2["n"] == 30
    and v2["min_n"] == 31
    and all(r["top1_exact"] is None for r in v2["per_truth"]),
    v2,
)
# a variant that has no output for some events is paired only on the events both scored
birds3, recs3, vs3 = synth(30, {"va": A, "vb": B[:25] + [False] * 5})
vs3[1] = T.make_variants("vb", "species", "prob", [(M.t_of(i) + 0.5, None, [((M.CACH[0] if B[i] else M.BLJA[0]).lower(), 0.8)], False) for i in range(25)])[0]
R3 = T.build_report(recs3, birds3, vs3, {}, 1.0, None, frozenset(), 20, 10, 10)
check(
    "vb has output for events 0-24 only: n 25, no_output 5, paired 25; A right 0-19, B right 0-9 and 20-21: a_only 10, b_only 2 (events 25-29 are not paired)",
    var(R3, "vb")["n"] == 25
    and var(R3, "vb")["no_output"] == 5
    and pair(R3, "va", "vb")["n_paired"] == 25
    and pair(R3, "va", "vb")["top1"]["a_only"] == 10
    and pair(R3, "va", "vb")["top1"]["b_only"] == 2,
    pair(R3, "va", "vb"),
)

# ---------------------------------------------------------------- calibration and ECE by hand
evs = [{"conf": 0.6, "exact1": True}] * 8 + [{"conf": 0.3, "exact1": False}] * 2 + [{"conf": 0.5, "exact1": False}] * 6 + [{"conf": 0.9, "exact1": True}] * 2
V = T.Variant("x", "species", "prob", [], [])
c = T.calibration(V, evs, 12, 2)
check("ECE by hand: (8 * 0.4 + 2 * 0.3 + 6 * 0.5 + 2 * 0.1) / 18 = 7.0 / 18 = 0.3889", c["status"] == "ok" and c["ece"] == 0.3889 and c["ece_n"] == 18, c)
check(
    "bins: 0.3-0.4 n 2 rate 0.0, 0.5-0.6 n 6 rate 0.0, 0.6-0.7 n 8 rate 1.0, 0.9-1.0 n 2 rate 1.0",
    [(b["bin"], b["n"], b["mean_conf"], b["owner_correct_rate"]) for b in c["bins"]]
    == [("0.3-0.4", 2, 0.3, 0.0), ("0.5-0.6", 6, 0.5, 0.0), ("0.6-0.7", 8, 0.6, 1.0), ("0.9-1.0", 2, 0.9, 1.0)],
    c["bins"],
)
c2 = T.calibration(V, [{"conf": 0.9, "exact1": True}] * 10, 10, 10)
check("ten hits at confidence 0.9: ECE = |1 - 0.9| = 0.1", c2["ece"] == 0.1 and c2["bins"][0]["bin"] == "0.9-1.0", c2)
c3 = T.calibration(V, [{"conf": 1.0, "exact1": True}, {"conf": 0.0, "exact1": False}], 1, 1)
check(
    "confidence 1.0 falls in the last bin and 0.0 in the first; ECE 0 (perfect)",
    [b["bin"] for b in c3["bins"]] == ["0.0-0.1", "0.9-1.0"] and c3["ece"] == 0.0,
    c3,
)
c4 = T.calibration(V, evs, 19, 2)
check(
    "n 18 < min_n 19: ECE not_measurable (null), the bins are still listed with their n",
    c4["status"] == "not_measurable" and c4["ece"] is None and len(c4["bins"]) == 4 and c4["n"] == 18,
    c4,
)
c5 = T.calibration(V, evs, 12, 7)
check(
    "min_bin_n 7: only the n 8 bin is measurable: ece_n 8 < min_n 12 -> not_measurable; bins of n 2 and 6 have null rates",
    c5["status"] == "not_measurable" and c5["ece_n"] == 8 and [b["owner_correct_rate"] for b in c5["bins"]] == [None, None, 1.0, None],
    c5,
)
c6 = T.calibration(V, evs, 8, 7)
check(
    "min_n 8 with the same bins: ECE over the one measurable bin only = |1 - 0.6| = 0.4 (twin)",
    c6["status"] == "ok" and c6["ece"] == 0.4 and c6["ece_n"] == 8,
    c6,
)
check("a logit variant is not_applicable (no ECE), n given", T.calibration(T.Variant("p", "species", "logit", [], []), evs, 1, 1)["status"] == "not_applicable")
check(
    "a prob variant with a score of 1.5 is not_applicable; the same variant with 0.95 is ok",
    T.calibration(V, [{"conf": 1.5, "exact1": True}], 1, 1)["status"] == "not_applicable"
    and T.calibration(V, [{"conf": 0.95, "exact1": True}], 1, 1)["status"] == "ok",
)
check("bin_index: 0.7 - 0.4 = 0.29999999999999993 still lands in bin 3 (0.3-0.4), not bin 2", T.bin_index(0.7 - 0.4) == 3 and (0.7 - 0.4) < 0.3)
check("bin_index: 0.3 -> 3, 0.7 -> 7, 0.99 -> 9, 1.0 -> 9, 0.0 -> 0", [T.bin_index(x) for x in (0.3, 0.7, 0.99, 1.0, 0.0)] == [3, 7, 9, 9, 0])

check(
    "variant n 18 with min_n 18 is measurable (boundary), with min_n 19 it is not",
    var(report(min_n=18), "birdnet")["status"] == "ok" and var(report(min_n=19), "birdnet")["status"] == "not_measurable",
)
# window bounds: a row of end time t belongs to the event at t_start = T0 when T0 - 1 <= t <= T0 + 3 (t_end = T0)
bw, rw, vw = synth(1, {})
T0 = M.t_of(0)
for off, inside in ((-1.0, True), (3.0, True), (-1.01, False), (3.01, False)):
    vv = T.make_variants("w", "species", "prob", [(T0 + off, None, [(M.CACH[0].lower(), 0.8)], False)])
    Rw = T.build_report(rw, bw, vv, {}, 1.0, None, frozenset(), 1, 1, 1)
    check(
        "a window row at t_start%+.2f s %s the event (n %d, no_output %d)" % (off, "is in" if inside else "is outside", 1 if inside else 0, 0 if inside else 1),
        var(Rw, "w")["n"] == (1 if inside else 0) and var(Rw, "w")["no_output"] == (0 if inside else 1),
        var(Rw, "w")["n"],
    )

# ---------------------------------------------------------------- the fixture scenario, every number by hand (see make_eval_report_fixtures.py)
R = report()
i_ = R["input"]
check(
    "input: 28 verdict records, 26 folded events, 18 species truths, 3 rejections, 1 unmatched, 2 human voice, 1 unsure, 1 artifact",
    i_
    == {
        "label_records": 28,
        "label_events": 26,
        "unmatched_labels": 1,
        "excluded_human_voice": 2,
        "excluded_unsure": 1,
        "excluded_artifact": 1,
        "species_truth": 18,
        "rejections": 3,
    },
    i_,
)
bn, pe, tg, cl = (var(R, n) for n in ("birdnet", "perch", "tags", "clap"))
check(
    "birdnet: n 18, top-1 12 exact + 4 near (0.6667 / 0.8889), top-5 14 exact + 4 near (0.7778 / 1.0)",
    bn["n"] == 18
    and bn["top1"] == {"exact": 12, "near": 4, "exact_rate": 0.6667, "with_near_rate": 0.8889}
    and bn["top5"] == {"exact": 14, "near": 4, "exact_rate": 0.7778, "with_near_rate": 1.0},
    (bn["top1"], bn["top5"]),
)
check(
    "perch: n 14, top-1 11 exact + 2 near (0.7857 / 0.9286), top-5 13 exact + 0 near",
    pe["n"] == 14
    and pe["top1"] == {"exact": 11, "near": 2, "exact_rate": 0.7857, "with_near_rate": 0.9286}
    and pe["top5"] == {"exact": 13, "near": 0, "exact_rate": 0.9286, "with_near_rate": 0.9286},
    (pe["top1"], pe["top5"]),
)
check(
    "tags (class level): n 18, top-1 10 group hits, top-5 18, near always 0",
    tg["n"] == 18
    and tg["top1"] == {"exact": 10, "near": 0, "exact_rate": 0.5556, "with_near_rate": 0.5556}
    and tg["top5"]["exact"] == 18
    and tg["level"] == "class",
    tg["top1"],
)
check(
    "clap: n 5 < min_n 12: not_measurable, no rates",
    cl["status"] == "not_measurable" and cl["n"] == 5 and cl["top1"] is None and cl["reason"] == "n_below_min",
    cl,
)
check(
    "birdnet per species: chickadee n 14 (10 top-1, 12 top-5), melanerpes aurifrons n 2 (0, 0), tree cricket n 2 (2, 2)",
    [(r["key"], r["n"], r["top1_exact"], r["top5_exact"]) for r in bn["per_truth"]]
    == [("poecile carolinensis", 14, 10, 12), ("melanerpes aurifrons", 2, 0, 0), ("oecanthus dulcisonans", 2, 2, 2)],
    bn["per_truth"],
)
check(
    "tags per group: bird n 16 (8 top-1, 16 top-5), insect n 2 (2, 2)",
    [(r["key"], r["n"], r["top1_exact"], r["top5_exact"]) for r in tg["per_truth"]] == [("bird", 16, 8, 16), ("insect", 2, 2, 2)],
    tg["per_truth"],
)
check(
    "rejections: birdnet repeats its own claim 3 of 3 (mechanical 2/2, other_species 1/1); perch n 2 repeats 1 (event 17: blue jay again)",
    bn["rejections"]
    == {
        "n": 3,
        "repeats_claim": 3,
        "by_reason": [{"reason": "mechanical", "n": 2, "repeats_claim": 2}, {"reason": "other_species", "n": 1, "repeats_claim": 1}],
    }
    and pe["rejections"] == {"n": 2, "repeats_claim": 1, "by_reason": [{"reason": "mechanical", "n": 2, "repeats_claim": 1}]}
    and tg["rejections"] is None,
    (bn["rejections"], pe["rejections"]),
)
check(
    "calibration: birdnet ECE by hand (8 * 0.1 + 2 * 0.7 + 2 * 0.8 + 2 * 0.6 + 2 * 0.5 + 2 * 0.3) / 18 = 6.6 / 18 = 0.3667; tags 0.3889; perch not_applicable",
    bn["calibration"]["ece"] == 0.3667
    and tg["calibration"]["ece"] == 0.3889
    and pe["calibration"]["status"] == "not_applicable"
    and pe["calibration"]["ece"] is None,
    (bn["calibration"]["ece"], tg["calibration"]["ece"]),
)
pbp = pair(R, "birdnet", "perch")
check(
    "birdnet vs perch on 14 paired events: top-1 a_only 1 (event 8), b_only 2 (events 10, 12), p = 1.0; top-5 a_only 1, b_only 2",
    pbp["n_paired"] == 14 and pbp["top1"] == {"a_only": 1, "b_only": 2, "p": 1.0} and pbp["top5"] == {"a_only": 1, "b_only": 2, "p": 1.0},
    pbp,
)
check(
    "pairs across levels are not_measurable (level_differs), tags vs clap n_paired 5 < 12",
    pair(R, "birdnet", "tags")["reason"] == "level_differs" and pair(R, "tags", "clap")["reason"] == "n_below_min" and pair(R, "tags", "clap")["n_paired"] == 5,
)
check(
    "provenance: perch mv = [the recorded model version], birdnet / tags mv = []",
    pe["mv"] == [M.MV] and bn["mv"] == [] and tg["mv"] == [],
    (pe["mv"], bn["mv"]),
)
txt = json.dumps(R)
check("no human-voice label string appears anywhere in the report", "Human" not in txt and "Speech" not in txt and "human vocal" not in txt)

# ---------------------------------------------------------------- default thresholds: 18 < 20 -> not_measurable (twin: --min-n 12 above)
Rd = report(min_n=20, min_species_n=10, min_bin_n=10)
check(
    "default min_n 20: birdnet n 18 is not_measurable with its count; top-1 null (no noisy number)",
    var(Rd, "birdnet")["status"] == "not_measurable"
    and var(Rd, "birdnet")["n"] == 18
    and var(Rd, "birdnet")["top1"] is None
    and var(Rd, "birdnet")["reason"] == "n_below_min",
    var(Rd, "birdnet")["status"],
)
check(
    "default min_species_n 10 with min_n 12: the chickadee row (n 14) keeps its counts, the n 2 rows are not_measurable with null counts",
    [(r["key"], r["status"], r["top1_exact"]) for r in var(report(min_species_n=10), "birdnet")["per_truth"]]
    == [("poecile carolinensis", "ok", 10), ("melanerpes aurifrons", "not_measurable", None), ("oecanthus dulcisonans", "not_measurable", None)],
)

# ---------------------------------------------------------------- label semantics
birds_, perch_, tags_, clap_, recs_ = M.scenario()
only_last = [r for r in recs_ if r["event_id"] != "1700000000000-no-such-event"]
first0 = next(r for r in recs_ if r["verdict"] == "wrong" and r["wrong_reason"] == "mechanical" and r["event_id"].startswith(str(int(M.t_of(0) * 1000))))
check(
    "event 0 carries a superseded wrong label earlier than its correct one: the later verdict wins (18 species truths, rejections 3)",
    R["input"]["species_truth"] == 18 and first0 is not None,
)
recs_swapped = [dict(r, t_label=r["t_label"] + (1000.0 if r is first0 else 0)) for r in recs_]
Rs = report((birds_, perch_, tags_, clap_, recs_swapped))
check(
    "twin: when that wrong label is the latest, event 0 becomes a rejection (species_truth 17, rejections 4)",
    Rs["input"]["species_truth"] == 17 and Rs["input"]["rejections"] == 4,
    Rs["input"],
)
# human voice: the Speech tag of event 23 excludes it; without it the event is scored
tags_ok = copy.deepcopy(tags_)
for r in tags_ok:
    if r["top"][0][0] == "Speech":
        r["top"] = [["Bird", 0.4]]
Rh = report((birds_, perch_, tags_ok, clap_, recs_))
check(
    "twin: event 23 with a bird tag instead of Speech is scored (species_truth 19, excluded_human_voice 1)",
    Rh["input"]["species_truth"] == 19 and Rh["input"]["excluded_human_voice"] == 1,
    Rh["input"],
)
# unsure and artifact never count; a wrong without corrected_key never gives a species truth
check(
    "unsure 1 and artifact 1 are excluded and counted; neither is in species_truth or rejections",
    R["input"]["excluded_unsure"] == 1 and R["input"]["excluded_artifact"] == 1,
)
# near semantics
sci = {"poecile carolinensis", "poecile atricapillus", "melanerpes carolinus", "melanerpes aurifrons", "cyanocitta cristata"}
nr = T.Near(sci, frozenset({frozenset(("cyanocitta cristata", "poecile atricapillus"))}))
check(
    "near: same genus (both scientific) is near, symmetric",
    nr("poecile carolinensis", "poecile atricapillus") and nr("poecile atricapillus", "poecile carolinensis"),
)
check("near: an equal key is exact, not near", not nr("poecile carolinensis", "poecile carolinensis"))
check("near: different genera without a listed pair are not near", not nr("poecile carolinensis", "cyanocitta cristata"))
check(
    "near: a listed lookalike pair of different genera is near, in both directions",
    nr("cyanocitta cristata", "poecile atricapillus") and nr("poecile atricapillus", "cyanocitta cristata"),
)
check("near: two common names outside the scientific set and the genus table are not near (blue jay vs blue grosbeak)", not nr("blue jay", "blue grosbeak"))
nr2 = T.Near(set(), frozenset())
check(
    "near: a first word that is a genus of the taxa table makes unseen keys scientific (poecile pair near with genera {poecile}, not without)",
    T.Near(set(), frozenset(), frozenset({"poecile"}))("poecile carolinensis", "poecile atricapillus")
    and not T.Near(set(), frozenset(), frozenset({"cyanocitta"}))("poecile carolinensis", "poecile atricapillus"),
)
check(
    "near: with no scientific keys known, even 'poecile carolinensis' vs 'poecile atricapillus' is not near (no genus guess from the first word)",
    not nr2("poecile carolinensis", "poecile atricapillus"),
)
check("lookalikes() reads confusions.json pairs: {aurifrons, carolinus}", PAIRS == {frozenset(("melanerpes aurifrons", "melanerpes carolinus"))}, PAIRS)
check("lookalikes() of an unreadable file is empty", T.lookalikes(os.path.join(tmp, "missing.json")) == set())
check(
    "birdnet near-misses: 2 RBWO vs GFWO (genus + lookalike) and 2 BCCH vs CACH (genus) = 4; blue jay vs chickadee (events 10-11) earns none",
    bn["top1"]["near"] == 4 and bn["top1"]["exact"] == 12,
)
Rnp_pairs = T.build_report(recs_, birds_, T.birdnet_variant(birds_), {}, 1.0, L.Taxa(T.DEFAULT_TAXA), frozenset(), 12, 2, 2)
check("the genus rule alone gives the same 4 (the lookalike list only adds pairs of different genera)", var(Rnp_pairs, "birdnet")["top1"]["near"] == 4)
# class level needs a map
birds_n, perch_n, tags_n, clap_n, recs_n = M.scenario()
vn = T.birdnet_variant(birds_n) + T.make_variants("tags", "class", "prob", *T.rows_from_records(tags_n))
Rm = T.build_report(recs_n, birds_n, vn, {}, 1.0, L.Taxa(T.DEFAULT_TAXA), frozenset(), 12, 2, 2)
check(
    "a class variant without a map entry is not_measurable (no_neighbourhood_map) even at n 18",
    var(Rm, "tags")["status"] == "not_measurable" and var(Rm, "tags")["reason"] == "no_neighbourhood_map" and var(Rm, "tags")["n"] == 18,
)
Rm2 = T.build_report(recs_n, birds_n, vn, CMAP, 1.0, L.Taxa(T.DEFAULT_TAXA), frozenset(), 12, 2, 2)
check("twin: with the map the same variant is ok with 10 top-1 group hits", var(Rm2, "tags")["status"] == "ok" and var(Rm2, "tags")["top1"]["exact"] == 10)


# ---------------------------------------------------------------- human-voice guard on the event / claim / truth keys (variant rows carry no voice label here)
def voice_case(mutate, species=M.CACH, voice_event=None):
    """One chickadee event with a correct verdict; mutate(rec) edits the label; voice_event = (scientific, common) replaces the event's species. -> input counts."""
    sp = voice_event or species
    bs = [M.bird_row(M.t_of(0), sp, 0.9)]
    rc = [L.make_record(e, "correct", "metadata_only", t_label=M.BASE_T + 90000.0, label_id="%016x" % 1) for e in L.iter_events(bs)]
    rc = [mutate(dict(r)) for r in rc]
    vv = T.make_variants("w", "species", "prob", [(M.t_of(0) + 0.5, None, [(M.CACH[0].lower(), 0.8)], False)])
    return T.build_report(rc, bs, vv, {}, 1.0, None, frozenset(), 1, 1, 1)


def corrected(key):
    return lambda r: dict(r, verdict="wrong", wrong_reason="other_species", corrected_key=key)


same = lambda r: r
rv = voice_case(same)
check(
    "twin: a chickadee event with a correct verdict is scored (species_truth 1, excluded_human_voice 0, n 1)",
    rv["input"]["species_truth"] == 1 and rv["input"]["excluded_human_voice"] == 0 and var(rv, "w")["n"] == 1,
    rv["input"],
)
rv = voice_case(same, voice_event=(M.CACH[0], "Human vocal"))
check(
    "event common name Human vocal (scientific name a bird): excluded, not scored, counted",
    rv["input"]["excluded_human_voice"] == 1 and rv["input"]["species_truth"] == 0 and var(rv, "w")["n"] == 0,
    rv["input"],
)
rv = voice_case(same, voice_event=("Speech", M.CACH[1]))
check(
    "event scientific name Speech (common name a bird; also the claim key): excluded, not scored, counted",
    rv["input"]["excluded_human_voice"] == 1 and rv["input"]["species_truth"] == 0 and var(rv, "w")["n"] == 0,
    rv["input"],
)
rv = voice_case(lambda r: dict(r, verdict="wrong", wrong_reason="mechanical", species_key="speech"))
check(
    "claim (species_key) Speech on a chickadee event, mechanical rejection (no truth): excluded, not scored, counted",
    rv["input"]["excluded_human_voice"] == 1 and rv["input"]["species_truth"] == 0 and var(rv, "w")["n"] == 0,
    rv["input"],
)
rv = voice_case(lambda r: dict(r, verdict="wrong", wrong_reason="mechanical"))
check(
    "twin: a mechanical rejection with a bird claim is counted (rejections 1, excluded_human_voice 0)",
    rv["input"]["rejections"] == 1 and rv["input"]["excluded_human_voice"] == 0,
    rv["input"],
)
rv = voice_case(corrected("Speech"))
check(
    "corrected_key Speech (truth hidden): excluded, not scored, counted",
    rv["input"]["excluded_human_voice"] == 1 and rv["input"]["species_truth"] == 0 and var(rv, "w")["n"] == 0,
    rv["input"],
)
rv = voice_case(corrected("poecile atricapillus"))
check(
    "twin: corrected_key a bird species is scored as that truth (species_truth 1, per_truth poecile atricapillus n 1)",
    rv["input"]["excluded_human_voice"] == 0
    and rv["input"]["species_truth"] == 1
    and [r["key"] for r in var(rv, "w")["per_truth"]] == ["poecile atricapillus"],
    rv["input"],
)


# ---------------------------------------------------------------- rejection repeats_claim is top-1 only
def reject_case(top):
    bs = [M.bird_row(M.t_of(0), M.BLJA, 0.9)]
    rc = [L.make_record(e, "wrong", "metadata_only", wrong_reason="mechanical", t_label=M.BASE_T + 90000.0, label_id="%016x" % 1) for e in L.iter_events(bs)]
    vv = T.make_variants("w", "species", "prob", [(M.t_of(0) + 0.5, None, top, False)])
    return var(T.build_report(rc, bs, vv, {}, 1.0, None, frozenset(), 1, 1, 1), "w")["rejections"]


check(
    "rejection, claim blue jay is the top-1 candidate: repeats_claim 1 of 1",
    reject_case([("cyanocitta cristata", 0.9), ("poecile carolinensis", 0.5)])
    == {"n": 1, "repeats_claim": 1, "by_reason": [{"reason": "mechanical", "n": 1, "repeats_claim": 1}]},
)
check(
    "twin: the claim is only the second candidate: repeats_claim 0 of 1",
    reject_case([("poecile carolinensis", 0.9), ("cyanocitta cristata", 0.5)])
    == {"n": 1, "repeats_claim": 0, "by_reason": [{"reason": "mechanical", "n": 1, "repeats_claim": 0}]},
)

# ---------------------------------------------------------------- provenance split by mv
rows_mv = [(1.0, "m1", [("a", 0.5)], False), (2.0, "m2", [("a", 0.5)], False), (3.0, "m1", [("a", 0.5)], False)]
vs = T.make_variants("perch", "species", "logit", rows_mv)
check(
    "rows with two mv values give two variants perch@m1 (2 rows) and perch@m2 (1 row)",
    [(v.name, v.mvs, len(v.rows)) for v in vs] == [("perch@m1", ["m1"], 2), ("perch@m2", ["m2"], 1)],
    [(v.name) for v in vs],
)
vs1 = T.make_variants("perch", "species", "logit", [(1.0, "m1", [("a", 0.5)], False), (2.0, "m1", [("a", 0.5)], False)])
check("twin: one mv value keeps the plain name", [(v.name, v.mvs) for v in vs1] == [("perch", ["m1"])])
vsn = T.make_variants("x", "species", "prob", [(1.0, None, [("a", 0.5)], False), (2.0, "m1", [("a", 0.5)], False)])
check("rows without mv next to rows with one: x@m1 and x@unrecorded", [v.name for v in vsn] == ["x@m1", "x@unrecorded"], [v.name for v in vsn])
vs0 = T.make_variants("x", "species", "prob", [(1.0, None, [("a", 0.5)], False)])
check("rows without any mv: plain name, mv []", [(v.name, v.mvs) for v in vs0] == [("x", [])])

# ---------------------------------------------------------------- CLI
d = M.write_data(os.path.join(tmp, "data"))
out = os.path.join(tmp, "out.json")
rc, so, se = run_cli(["--data", d, "--confusions", CONF, "--out", out] + M.ARGS)
fix = os.path.join(BMA, "contracts", "fixtures", "eval_report.json")
check(
    "CLI run writes the fixture byte for byte and prints the table",
    rc == 0 and open(out, "rb").read() == open(fix, "rb").read() and so == T.table(json.loads(open(out, encoding="utf-8").read())),
    (rc, se),
)
lines = so.split("\n")
check(
    "table head: version line, counts line, birdnet row",
    lines[0] == "eval_report v1 min_n=12 min_species_n=2 min_bin_n=2"
    and lines[1] == "labels 28 events 26 species_truth 18 rejections 3 unmatched 1 excluded: human 2 unsure 1 artifact 1"
    and lines[3] == "birdnet                  species n=18 66.7% 88.9% 77.8% 100.0% ece=0.3667",
    lines[:4],
)
check(
    "table: clap says not_measurable with the counts, perch ece says not_applicable",
    "clap                     class   n=5 not_measurable (n_below_min, min_n=12)" in lines
    and "perch                    species n=14 78.6% 92.9% 92.9% 92.9% ece=not_applicable" in lines,
)
# the perch filter is the CLI's: non-loudest perch rows (a loud wrong label and a voice label) change nothing in the report
d3 = M.write_data(os.path.join(tmp, "data3"))
with open(os.path.join(d3, "analysis", "perch.jsonl"), "a", encoding="utf-8", newline="\n") as f:
    for i in range(0, 26):
        f.write(
            json.dumps(
                {"k": "perch", "src": "beam", "t": M.t_of(i) + 0.5, "top": [["zzz loud wrong", 9.0], ["Speech", 8.0]], "mv": M.MV}, separators=(",", ":")
            )
            + "\n"
        )
rc, so3, se = run_cli(["--data", d3, "--confusions", CONF, "--out", os.path.join(tmp, "o5.json")] + M.ARGS)
check(
    "CLI: 26 extra non-loudest perch rows (src beam, loud wrong label, Speech) leave the report byte for byte the fixture",
    rc == 0 and open(os.path.join(tmp, "o5.json"), "rb").read() == open(fix, "rb").read(),
    (rc, se),
)
d4 = M.write_data(os.path.join(tmp, "data4"))
with open(os.path.join(d4, "analysis", "perch.jsonl"), "a", encoding="utf-8", newline="\n") as f:
    for i in range(0, 26):
        f.write(json.dumps({"k": "perch", "src": "loudest", "t": M.t_of(i) + 0.5, "top": [["zzz loud wrong", 9.0]], "mv": M.MV}, separators=(",", ":")) + "\n")
rc, so4, se = run_cli(["--data", d4, "--confusions", CONF, "--out", os.path.join(tmp, "o6.json")] + M.ARGS)
check(
    "twin: the same rows with src loudest do change the report (perch top-1 exact falls)",
    rc == 0 and open(os.path.join(tmp, "o6.json"), "rb").read() != open(fix, "rb").read(),
    (rc, se),
)
check(
    "table ends with the selection-bias caveat line",
    so3.split("\n")[-2].startswith("-- caveat: events are selected from birdnet output") and "inflated" in so3.split("\n")[-2],
)
rc, so, se = run_cli(["--data", d, "--confusions", CONF, "--out", os.path.join(d, "r.json")])
check(
    "--out inside --data is refused (exit 2) and nothing is written", rc == 2 and not os.path.exists(os.path.join(d, "r.json")) and "refusing" in se, (rc, se)
)
rc, so, se = run_cli(["--data", d, "--labels-dir", os.path.join(tmp, "elsewhere"), "--confusions", CONF, "--out", os.path.join(tmp, "o2.json")])
check(
    "twin: an --out outside both directories works; a missing labels dir is an empty label set (0 records, exit 0)",
    rc == 0 and json.loads(open(os.path.join(tmp, "o2.json")).read())["input"]["label_records"] == 0,
    (rc, se),
)
rc, so, se = run_cli(["--data", d, "--variant", "oops"])
check("a malformed --variant is refused (exit 2) with the format in the message", rc == 2 and "NAME=REL" in se, se)
# an extra variant file, with and without provenance
ex = [{"k": "newmodel", "t": M.t_of(i) + 0.5, "top": [["poecile carolinensis", 0.8]], "mv": "newmodel@1"} for i in range(0, 8)]
M.write_jsonl(os.path.join(d, "analysis", "newmodel.jsonl"), ex)
rc, so, se = run_cli(
    [
        "--data",
        d,
        "--confusions",
        CONF,
        "--min-n",
        "5",
        "--min-species-n",
        "2",
        "--min-bin-n",
        "2",
        "--variant",
        "newmodel=newmodel.jsonl:species:prob",
        "--out",
        os.path.join(tmp, "o3.json"),
    ]
)
r3 = json.loads(open(os.path.join(tmp, "o3.json")).read())
nm = next(v for v in r3["variants"] if v["name"] == "newmodel")
check(
    "--variant newmodel=newmodel.jsonl: 8 events scored, 8 top-1 exact, mv [newmodel@1], prob calibration ok n 8",
    rc == 0 and nm["n"] == 8 and nm["top1"]["exact"] == 8 and nm["mv"] == ["newmodel@1"] and nm["calibration"]["status"] == "ok",
    nm,
)
# missing optional inputs
d2 = os.path.join(tmp, "data2")
os.makedirs(os.path.join(d2, "analysis"))
shutil.copy(os.path.join(d, "analysis", "birds.jsonl"), os.path.join(d2, "analysis", "birds.jsonl"))
rc, so, se = run_cli(["--data", d2, "--out", os.path.join(tmp, "o4.json"), "--generated-s", "1.0"])
r4 = json.loads(open(os.path.join(tmp, "o4.json")).read())
check(
    "no labels, no perch / tags / clap files: exit 0, only the birdnet variant, n 0 not_measurable, no pairs",
    rc == 0
    and [v["name"] for v in r4["variants"]] == ["birdnet"]
    and r4["variants"][0]["n"] == 0
    and r4["variants"][0]["status"] == "not_measurable"
    and r4["pairs"] == []
    and r4["input"]["label_records"] == 0,
    r4["variants"],
)
shutil.rmtree(tmp, ignore_errors=True)
print("\n%d checks, %d failed" % (n_checks, len(fails)))
sys.exit(1 if fails else 0)
