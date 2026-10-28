#!/usr/bin/env python3
"""Hermetic tests for analysis/beam_agreement.py: synthetic rows only, exact values, no live data, stdlib only.
    python -I test_beam_agreement.py
Style (as analysis/test_analysis.py): exact values, a positive twin for every negative case, mutations are run by the
builder against copies of the module (see the PR notes), a FAIL line names the test that went red."""

import json, math, os, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "ui")))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "contracts", "tools")))
import beam_agreement as BA  # noqa: E402
import bma_ui as UI  # noqa: E402
import make_beam_agreement_fixtures as MK  # noqa: E402

fails = []
TAXA_PATH = os.path.join(HERE, "..", "ui", "taxa.json")
TAXA = BA.Taxa(TAXA_PATH)
T0 = 1792972800.0  # an exactly representable base; +3.0 steps stay exact


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def row(t, beam, conf, sci="Testus avis", common="Test Bird", level=-60.0, region=True, k="bird", **kw):
    r = {"k": k, "t": t, "beam": beam, "level_dbfs": level, "common": common, "scientific": sci, "confidence": conf, "provenance": "model_opinion"}
    if region is not None:
        r["in_region"] = region
    r.update(kw)
    return r


def one(rows, gap=5.0):
    evs = BA.cluster(rows, gap, TAXA)
    return evs


# ================================================================================================== 1. concurrent counting
e = one([row(100.0, 2, 0.7), row(100.0, 5, 0.7), row(103.0, 2, 0.7)])
check(
    "concurrent: beams {2,5} at 100.0 then {2} at 103.0 -> ONE event, concurrent_windows 1, beam_count 2, tier probable",
    len(e) == 1 and e[0].concurrent_windows == 1 and len(e[0].beams) == 2 and e[0].tier == "probable" and e[0].n_windows == 2,
    str([(x.concurrent_windows, x.tier) for x in e]),
)
e = one([row(100.0, 2, 0.7), row(100.0, 5, 0.7), row(103.0, 2, 0.7), row(103.0, 5, 0.7)])
check("concurrent twin: both windows with beams {2,5} -> concurrent_windows 2, confirmed", e[0].concurrent_windows == 2 and e[0].tier == "confirmed")
e = one([row(100.0, 2, 0.7), row(103.0, 5, 0.7)])
check(
    "concurrent twin: beam 2 at 100.0 and beam 5 at 103.0 (sequential only) -> concurrent_windows 0, beam_count 2, single_beam",
    e[0].concurrent_windows == 0 and len(e[0].beams) == 2 and e[0].tier == "single_beam",
    str((e[0].concurrent_windows, e[0].tier)),
)
check(
    "tier_of: 0 -> single_beam, 1 -> probable, 2 -> confirmed, 3 -> confirmed (the >= 2 edge)",
    [BA.tier_of(i) for i in (0, 1, 2, 3)] == ["single_beam", "probable", "confirmed", "confirmed"],
)

# ================================================================================================== 2. gap rule
two = [row(100.0, 2, 0.7), row(105.0, 2, 0.7)]
check("gap: reports 5.0 s apart merge at gap 5 (one event of 2 windows)", [x.n_windows for x in one(two, 5.0)] == [2])
check("gap twin: the same reports split at gap 4.9 (two events)", [x.n_windows for x in one(two, 4.9)] == [1, 1])
check(
    "gap: different species keys never merge even at the same t",
    len(one([row(100.0, 2, 0.7, sci="Alpha beta", common="A"), row(100.0, 2, 0.7, sci="Gamma delta", common="G")])) == 2,
)
check("gap twin: the same species key at the same t is ONE event", len(one([row(100.0, 2, 0.7), row(100.0, 3, 0.7)])) == 1)
check(
    "key: scientific name wins and is case/space-insensitive; without it the common name is the key",
    len(one([row(100.0, 2, 0.7, sci=" Alpha Beta ", common="x"), row(101.0, 2, 0.7, sci="alpha beta", common="y")])) == 1
    and len(one([row(100.0, 2, 0.7, sci="", common="Dog"), row(101.0, 2, 0.7, sci="", common="dog")])) == 1,
)

# ================================================================================================== 3. exact-t windows
e = one([row(100.0, 2, 0.7), row(100.0, 5, 0.7)])
check("exact t: identical t forms one concurrent window, no unequal-t pair", e[0].concurrent_windows == 1 and BA.unequal_t_pairs(e) == 0)
e = one([row(100.0, 2, 0.7), row(100.000001, 5, 0.7)])
check(
    "exact t twin: t differing by 1e-6 are TWO windows, concurrent 0 (documents the shared-t_wall dependence)",
    e[0].concurrent_windows == 0 and e[0].n_windows == 2,
)
check("exact t: that broken pair is detected and counted in beam_pairs_with_unequal_t (1)", BA.unequal_t_pairs(e) == 1, str(BA.unequal_t_pairs(e)))
rep = BA.build_report([row(100.0, 2, 0.7), row(100.000001, 5, 0.7)], 0, 5.0, TAXA, [], None, 1.0)
check("exact t: the report carries beam_pairs_with_unequal_t == 1 (the owner sees the tiers are lower bounds)", rep["input"]["beam_pairs_with_unequal_t"] == 1)
rounded = [dict(r, t=round(r["t"], 3)) for r in [row(T0, 2, 0.7), row(T0, 5, 0.7), row(T0 + 3.0, 2, 0.7), row(T0 + 3.0, 5, 0.7)]]
e = one(rounded)
check(
    "rounding t to 3 decimals does NOT split windows (both beams round identically): concurrent_windows stays 2, no unequal-t pair (refutes the 'every pair becomes single' claim for plain rounding)",
    e[0].concurrent_windows == 2 and BA.unequal_t_pairs(e) == 0,
)
check(
    "t jitter that differs per beam (one beam +4e-4) IS detected as an unequal-t pair", BA.unequal_t_pairs(one([row(T0, 2, 0.7), row(T0 + 4e-4, 5, 0.7)])) == 1
)

# ================================================================================================== 4. bin edges
check("bins: 0.5 -> bin 2 (0.5-0.65) and its neighbour 0.4999 -> bin 1", (BA.bin_index(0.5), BA.bin_index(0.4999)) == (2, 1))
check("bins: 0.65 -> bin 3 (0.65-0.85) and 0.6499 -> bin 2", (BA.bin_index(0.65), BA.bin_index(0.6499)) == (3, 2))
check(
    "bins: 0.85 -> bin 4 and 0.8499 -> bin 3; 1.0 stays in the last bin; 0.35 -> bin 1 and 0.3499 -> bin 0",
    (BA.bin_index(0.85), BA.bin_index(0.8499), BA.bin_index(1.0), BA.bin_index(0.35), BA.bin_index(0.3499)) == (4, 3, 4, 1, 0),
)
check("duration classes: 1 -> '1', 2 and 3 -> '2-3', 4 -> '>=4'", [BA.dur_class(i) for i in (1, 2, 3, 4, 9)] == ["1", "2-3", "2-3", ">=4", ">=4"])

# ================================================================================================== 5. statistics
for x, df, p in ((3.841458820694124, 1, 0.05), (5.991464547107979, 2, 0.05), (7.814727903251179, 3, 0.05), (6.634896601021213, 1, 0.01)):
    check("chi2_sf(%.6f, df=%d) == %s to 1e-9 (published critical value)" % (x, df, p), abs(BA.chi2_sf(x, df) - p) < 1e-9, str(BA.chi2_sf(x, df)))
check("chi2_sf(0) == 1 and chi2_sf(60, 1) < 1e-13 (both tails)", BA.chi2_sf(0, 1) == 1.0 and BA.chi2_sf(60, 1) < 1e-13)
# hand-checked two-stratum table: [a,b,c,d] = [10,5,4,11] and [8,7,3,12]
# stratum 1: n 30, r1 15 r2 15 c1 14 c2 16: E 7, V 15*15*14*16/(900*29); stratum 2: c1 11 c2 19: E 5.5, V 15*15*11*19/(900*29)
v_hand = (15 * 15 * 14 * 16 + 15 * 15 * 11 * 19) / (900 * 29)
chi_hand = (abs(18 - (7 + 5.5)) - 0.5) ** 2 / v_hand
r_hand = (10 * 11 / 30 + 8 * 12 / 30) / (5 * 4 / 30 + 7 * 3 / 30)
res = BA.cmh([[10, 5, 4, 11], [8, 7, 3, 12]])
check(
    "cmh hand-checked: chi2 == 652500/97425 (6.6974595...) to 1e-9, p == erfc(sqrt(chi2/2)) to 1e-9, MH OR == 206/41 to 1e-9",
    abs(res["chi2"] - 652500 / 97425) < 1e-9
    and abs(chi_hand - 652500 / 97425) < 1e-9
    and abs(res["p"] - math.erfc(math.sqrt(chi_hand / 2))) < 1e-9
    and abs(res["or"] - 206 / 41) < 1e-9
    and res["status"] == "ok"
    and res["strata_used"] == 2,
    str(res),
)
res_nc = BA.cmh([[10, 5, 4, 11], [8, 7, 3, 12]])
check(
    "cmh hand-checked: Breslow-Day p of two strata with ORs 5.5/2.67 is a probability in (0, 1] and the CI brackets the OR",
    0 < res_nc["breslow_day_p"] <= 1 and res_nc["or_ci95"][0] < res_nc["or"] < res_nc["or_ci95"][1],
)
same = BA.cmh([[20, 5, 20, 5], [8, 7, 8, 7]])
check(
    "cmh: identical odds ratios in both strata (OR 1): Breslow-Day statistic 0 -> p == 1.0 to 1e-9 and MH OR == 1.0",
    abs(same["breslow_day_p"] - 1.0) < 1e-9 and abs(same["or"] - 1.0) < 1e-12,
    str(same),
)
het = BA.cmh([[40, 10, 10, 40], [10, 40, 40, 10]])
check(
    "cmh: opposite effects in two strata (OR 16 and 1/16): Breslow-Day p < 1e-6 (heterogeneity found); twin: homogeneous strata above are not flagged",
    het["breslow_day_p"] < 1e-6 and same["breslow_day_p"] > 0.05,
    str(het),
)
sp = BA.cmh([[1, 4, 2, 3]])
check(
    "cmh: a stratum with a smallest expected cell count under 5 is stratum_too_sparse, no p; twin: the same shape x10 is ok",
    sp["status"] == "stratum_too_sparse" and sp["p"] is None and BA.cmh([[10, 40, 20, 30]])["status"] == "ok",
    str(sp),
)
check("cmh: a stratum with an empty margin is dropped (strata_used 1), not an error", BA.cmh([[20, 5, 20, 5], [10, 10, 0, 0]])["strata_used"] == 1)
check("cmh: no usable stratum -> no_variation", BA.cmh([[0, 0, 5, 5]])["status"] == "no_variation")
# the Simpson case: tier looks strongly associated pooled, OR exactly 1 within each stratum
simp = [[360, 40, 90, 10], [20, 80, 80, 320]]
res = BA.cmh(simp)
check(
    "Simpson: tier independent of the outcome within strata but confounded by the stratum -> MH OR == 1.0 and p == 1.0 (> 0.05)",
    res["status"] == "ok" and abs(res["or"] - 1.0) < 1e-12 and res["p"] == 1.0,
    str(res),
)
pooled = [sum(t[i] for t in simp) for i in range(4)]
res_p = BA.cmh([pooled])
check(
    "Simpson twin: pooling the same data into one stratum WRONGLY gives a significant effect (p < 1e-20, OR > 5)",
    res_p["p"] < 1e-20 and res_p["or"] > 5,
    str(res_p),
)
eff = BA.cmh([[360, 40, 50, 50], [20, 80, 80, 320]])
check(
    "positive twin: a real within-stratum effect in one stratum -> p < 0.05 and OR == (36+12.8)/(4+12.8) == 2.9047619... to 1e-9",
    eff["p"] < 0.05 and abs(eff["or"] - 48.8 / 16.8) < 1e-9,
    str(eff),
)
check(
    "woolf: a table with an empty cell gets the Haldane 0.5 correction (finite OR), exact for [10,0,5,5] -> 10.5*5.5/(0.5*5.5)... == 21.0",
    abs(BA.woolf(10, 0, 5, 5)[0] - 21.0) < 1e-9,
    str(BA.woolf(10, 0, 5, 5)),
)


# ================================================================================================== owner labels through the real join (events + labels)
def labelled_cells(cells):
    """cells: [(conf, n_windows, concurrent bool, n_correct, n_wrong)] -> (rows, labels) of distinct-species-free events of ONE species
    key spaced 1000 s apart (never chained)."""
    rows, labels, t = [], [], T0 + 1000.0
    for conf, nw, conc, nc, nwr in cells:
        for k in range(nc + nwr):
            for j in range(nw):
                rows.append(row(t + 3.0 * j, 2, conf))
                if conc and j < (1 if nw == 1 else 2):
                    rows.append(row(t + 3.0 * j, 3, conf))
            labels.append(((BA.key_of(rows[-1]), t), "correct" if k < nc else "wrong"))
            t += 1000.0
    return rows, dict(labels)


simpson_cells = [(0.6, 1, True, 360, 40), (0.6, 1, False, 90, 10), (0.75, 1, True, 20, 80), (0.75, 1, False, 80, 320)]
rows_s, lab_s = labelled_cells(simpson_cells)
o1 = BA.o1_labels(BA.cluster(rows_s, 3.0, TAXA), lab_s)
check(
    "O1 Simpson end to end: stratified by conf bin x duration the labelled events give OR == 1.0, p == 1.0, n 1000 (500 concurrent / 500 single), strata 2",
    (o1["n"], o1["n_concurrent"], o1["n_single"], o1["strata_used"], o1["or"], o1["cmh_p"], o1["status"]) == (1000, 500, 500, 2, 1.0, 1.0, "ok"),
    str(o1),
)
rows_e, lab_e = labelled_cells([(0.6, 1, True, 360, 40), (0.6, 1, False, 50, 50), (0.75, 1, True, 20, 80), (0.75, 1, False, 80, 320)])
o1e = BA.o1_labels(BA.cluster(rows_e, 3.0, TAXA), lab_e)
check(
    "O1 positive twin: a real effect in the 0.6 stratum -> OR 2.904762 and p < 0.05 through the same path",
    o1e["or"] == 2.904762 and o1e["cmh_p"] < 0.05,
    str(o1e),
)
# label join uses the DASHBOARD's gap 3.0, not --gap-s 5
jr = [row(100.0, 2, 0.6), row(100.0, 3, 0.6), row(104.0, 2, 0.6)]
key = BA.key_of(jr[0])
ev3, ev5 = BA.cluster(jr, 3.0, TAXA), BA.cluster(jr, 5.0, TAXA)
check(
    "label join: at gap 3 the reports are two events (t_start 100.0 and 104.0); at gap 5 one",
    [x.t_start for x in ev3] == [100.0, 104.0] and [x.t_start for x in ev5] == [100.0],
)
o_3 = BA.o1_labels(ev3, {(key, 104.0): "correct"})
check(
    "label join: a verdict keyed (species, 104.0) joins the gap-3 event (n 1, single arm) and would join nothing at gap 5 (n 0)",
    (o_3["n"], o_3["n_single"]) == (1, 1) and BA.o1_labels(ev5, {(key, 104.0): "correct"})["status"] == "no_labelled_events",
)
rep = BA.build_report(jr, 0, 5.0, TAXA, [], {(key, 104.0): "correct"}, 1.0)
check(
    "label join: build_report at --gap-s 5 still joins on gap-3 events (o1.n == 1)", rep["outcomes"]["o1"]["n"] == 1 and rep["definition"]["label_gap_s"] == 3.0
)
with tempfile.TemporaryDirectory() as d:
    lp = os.path.join(d, "labels.jsonl")
    with open(lp, "w", newline="") as f:
        f.write(
            '{"k":"beam_label","v":1,"species_key":"Alpha Beta","t_start":100.0,"verdict":"correct"}\n'
            '{"k":"beam_label","v":1,"species_key":"alpha beta","t_start":100.0,"verdict":"wrong"}\n'
            '{"k":"beam_label","v":1,"species_key":"gamma","t_start":5.0,"verdict":"unsure"}\n'
            '{"k":"beam_label","v":1,"species_key":"delta","t_start":7.0,"verdict":"maybe"}\n'
            "not json\n"
            '{"k":"beam_label","v":1,"species_key":"eps","t_start":9.0,"verdict":"correct"}\n'
        )
    lab, st = BA.read_labels(lp)
    check(
        "labels: 'unsure' dropped (1), a duplicate keeps the LAST verdict (1 duplicate, 'wrong'), bad verdict and non-JSON are garbage (2), 2 usable",
        lab == {("alpha beta", 100.0): "wrong", ("eps", 9.0): "correct"} and st == {"n_lines": 6, "garbage_lines": 2, "unsure_dropped": 1, "duplicates": 1},
        str((lab, st)),
    )


# ================================================================================================== 6. negative control (matched)
def bird_ev(sci, t, conf, nw, cw, level=-60.0, region=True):
    out = []
    for j in range(nw):
        bs = [2, 3] if j < cw else [2]
        out += [row(t + 3.0 * j, b, conf, sci=sci, common=sci, level=level, region=region) for b in bs]
    return out


def control_rows(bird_cells, insect_cells):
    """cells: [(conf, n_windows, concurrent windows, count)] for birds (in region) and cold insects (in_region absent); one ambient 2 C feed."""
    rows, t = [], T0 + 1000.0
    for sci, cells, region in (("Melanerpes carolinus", bird_cells, True), ("Amblycorypha oblongifolia", insect_cells, None)):
        for conf, nw, cw, cnt in cells:
            for _ in range(cnt):
                rows += bird_ev(sci, t, conf, nw, cw, region=region)
                t += 1000.0
    amb = [(int(T0) + 3600 * h, 2.0) for h in range(int((t - T0) // 3600) + 2)]
    return rows, amb


def both(conf, nw, cw, cnt, which):
    return [(conf, nw, cw, cnt)]


# confounded: insects are mostly LONG events (always concurrent by chaining), birds are half 1-window singles
bc = [(c, 1, 0, 30) for c in (0.6, 0.75, 0.9)] + [(c, 4, 2, 30) for c in (0.6, 0.75, 0.9)]
ic = [(c, 1, 0, 10) for c in (0.6, 0.75, 0.9)] + [(c, 4, 2, 50) for c in (0.6, 0.75, 0.9)]
rows_c, amb_c = control_rows(bc, ic)
nc = BA.negative_control(BA.cluster(rows_c, 5.0, TAXA), amb_c)
raw_lower = all(r["cold_insect_single_frac"] < r["bird_single_frac"] for r in nc["bins"][2:])
check(
    "control: RAW single-beam fraction of cold insects (10/60 = 0.166667) is below birds' (30/60 = 0.5) in all 3 bins >= 0.5 (the confounded view)",
    raw_lower and nc["bins"][3]["cold_insect_single_frac"] == 0.166667 and nc["bins"][3]["bird_single_frac"] == 0.5,
    str(nc["bins"][3]),
)
check(
    "control: MATCHED on duration x level the two arms are equal (0.333333 each) -> artifact_scores_higher False (duration explains the raw gap)",
    nc["bins"][3]["matched_cold_insect_single_frac"] == 0.333333
    and nc["bins"][3]["matched_bird_single_frac"] == 0.333333
    and nc["artifact_scores_higher"] is False,
    str(nc["bins"][3]),
)
# positive twin: insects concurrent at EVERY duration, birds half single at every duration
bc2 = [(c, nw, cw, 15) for c in (0.6, 0.75, 0.9) for nw, cw in ((1, 0), (1, 1), (4, 0), (4, 2))]
ic2 = [(c, nw, (1 if nw == 1 else 2), 30) for c in (0.6, 0.75, 0.9) for nw in (1, 4)]
rows_c2, amb_c2 = control_rows(bc2, ic2)
nc2 = BA.negative_control(BA.cluster(rows_c2, 5.0, TAXA), amb_c2)
check(
    "control twin: insects always concurrent (single 0.0), birds half single within each duration -> matched 0.0 vs 0.5, artifact_scores_higher True",
    nc2["bins"][3]["matched_cold_insect_single_frac"] == 0.0 and nc2["bins"][3]["matched_bird_single_frac"] == 0.5 and nc2["artifact_scores_higher"] is True,
    str(nc2["bins"][3]),
)
check(
    "control: bins below 0.5 never vote (eligible False) and a bin needs >= 30 events in BOTH arms (eligible True here with 60/60)",
    [r["eligible"] for r in nc2["bins"]] == [False, False, True, True, True],
    str([r["eligible"] for r in nc2["bins"]]),
)
few_rows, few_amb = control_rows([(0.6, 1, 0, 29)], [(0.6, 1, 1, 29)])
check(
    "control: 29 events per arm is below the 30 minimum -> no voting bin, artifact_scores_higher None (not a False pass)",
    BA.negative_control(BA.cluster(few_rows, 5.0, TAXA), few_amb)["artifact_scores_higher"] is None,
)
check(
    "control: warm nights (ambient 20 C) leave no cold insect event -> n_cold_insect 0, artifact None",
    (lambda r: (r["n_cold_insect"], r["artifact_scores_higher"]))(BA.negative_control(BA.cluster(rows_c2, 5.0, TAXA), [(h, 20.0) for h, _ in amb_c2]))
    == (0, None),
)
check(
    "control: an ambient record older than 3 h does not count (reader rule): feed ending 4 h before the events -> no cold insect",
    BA.negative_control(BA.cluster(rows_c2, 5.0, TAXA), [(int(T0) - 4 * 3600, 2.0)])["n_cold_insect"] == 0
    and BA.temp_at([(0, 2.0)], 10800.0) == 2.0
    and BA.temp_at([(0, 2.0)], 10800.5) is None,
)
adj = BA.negative_control(BA.cluster(rows_c2, 5.0, TAXA), amb_c2)["bins"][3]["cold_insect_adjacent_share"]
check("control: concurrent insect windows were on beams {2,3}: adjacent share 1.0", adj == 1.0)


# ================================================================================================== 7. decisions (each rule either side of its threshold)
def o1d(**kw):
    d = {
        "n": 300,
        "n_concurrent": 150,
        "n_single": 150,
        "status": "ok",
        "strata_used": 6,
        "cmh_chi2": 10.0,
        "cmh_p": 0.001,
        "or": 3.0,
        "or_ci95": [2.0, 4.0],
        "breslow_day_p": 0.5,
        "level_sensitivity": None,
    }
    d.update(kw)
    return d


D = BA.decide
check(
    "decide: ship_tier when p 0.001, OR 3.0, BD 0.5, control False",
    D(True, 168, False, o1d()) == ("ship_tier", ["cmh_significant", "or_at_least_2", "homogeneous_or", "artifact_does_not_score_higher"]),
)
check(
    "decide: p 0.0499 ships, p 0.05 drops (threshold)",
    D(True, 1, False, o1d(cmh_p=0.0499))[0] == "ship_tier" and D(True, 1, False, o1d(cmh_p=0.05)) == ("drop", ["cmh_p_not_below_0.05"]),
)
check(
    "decide: OR 2.0 ships, OR 1.99 is inconclusive (or_below_2), OR 1.5 is inconclusive, OR 1.49 drops",
    D(True, 1, False, o1d(**{"or": 2.0}))[0] == "ship_tier"
    and D(True, 1, False, o1d(**{"or": 1.99})) == ("inconclusive", ["or_below_2"])
    and D(True, 1, False, o1d(**{"or": 1.5}))[0] == "inconclusive"
    and D(True, 1, False, o1d(**{"or": 1.49})) == ("drop", ["or_below_1.5"]),
)
check(
    "decide: Breslow-Day p 0.05 ships, 0.0499 is inconclusive (breslow_day_significant), None is inconclusive (not computable)",
    D(True, 1, False, o1d(breslow_day_p=0.05))[0] == "ship_tier"
    and D(True, 1, False, o1d(breslow_day_p=0.0499)) == ("inconclusive", ["breslow_day_significant"])
    and D(True, 1, False, o1d(breslow_day_p=None)) == ("inconclusive", ["breslow_day_not_computable"]),
)
check(
    "decide: 90 labelled per arm is enough, 89 in either arm is insufficient_labels",
    D(True, 1, False, o1d(n_concurrent=90, n_single=90))[0] == "ship_tier"
    and D(True, 1, False, o1d(n_concurrent=89)) == ("insufficient_labels", ["fewer_than_90_labels_per_arm"])
    and D(True, 1, False, o1d(n_single=89)) == ("insufficient_labels", ["fewer_than_90_labels_per_arm"]),
)
check(
    "decide: a sparse stratum is insufficient_labels EVEN when p >= 0.05 (first match beats drop); same numbers with status ok drop",
    D(True, 1, False, o1d(status="stratum_too_sparse", cmh_p=None, **{"or": None})) == ("insufficient_labels", ["stratum_too_sparse"])
    and D(True, 1, False, o1d(cmh_p=0.5))[0] == "drop",
)
check(
    "decide: significant labels but the control out-scores birds -> reframe_diffuse, not ship (rule 5 before 6); twin: control False ships",
    D(True, 1, True, o1d()) == ("reframe_diffuse", ["artifact_scores_higher"]) and D(True, 1, False, o1d())[0] == "ship_tier",
)
check("decide: drop beats reframe_diffuse (p 0.5 with the control True is a drop)", D(True, 1, True, o1d(cmh_p=0.5)) == ("drop", ["cmh_p_not_below_0.05"]))
check(
    "decide: label-free, control True -> reframe_diffuse (label_free_o3_only); control False -> insufficient_labels (never ship without labels)",
    D(True, 1, True, "unavailable") == ("reframe_diffuse", ["label_free_o3_only", "artifact_scores_higher"])
    and D(True, 1, False, "unavailable") == ("insufficient_labels", ["no_labels"]),
)
check(
    "decide: no ambient -> insufficient_evidence no_ambient; taxa unusable -> taxa_unusable; control not computable -> negative_control_not_computable; positive twin with all present is not insufficient_evidence",
    D(True, 0, True, o1d()) == ("insufficient_evidence", ["no_ambient"])
    and D(False, 5, True, o1d()) == ("insufficient_evidence", ["taxa_unusable"])
    and D(True, 5, None, o1d()) == ("insufficient_evidence", ["negative_control_not_computable"])
    and D(True, 5, True, o1d())[0] != "insufficient_evidence",
)


# end to end on the synthetic scenario (the same one that makes the contract fixture)
def decision_of(**kw):
    b, a, lb = MK.scenario(**{k: v for k, v in kw.items() if k != "nolabels"})
    labels = None if kw.get("nolabels") else {(BA.key_of(row(0, 1, 0.5, sci=BIRD_SCI)), x["t_start"]): x["verdict"] for x in lb}
    r = BA.build_report(b, 0, 5.0, TAXA, [(x["t_hour_utc"], x["temp_c"]) for x in a], labels, 1.0)
    return r["decision"], r["reasons"], r


BIRD_SCI = MK.BIRD[0]
d_ship = decision_of(insect_conc=0.3)
d_refr = decision_of()
d_drop = decision_of(label_effect=False, insect_conc=0.3)
d_free = decision_of(nolabels=True)
check(
    "scenario end to end: effect + insects LESS concurrent than birds -> ship_tier (OR 4.0, 240 labelled over 6 strata)",
    d_ship[0] == "ship_tier"
    and d_ship[2]["outcomes"]["o1"]["or"] == 4.0
    and d_ship[2]["outcomes"]["o1"]["strata_used"] == 6
    and d_ship[2]["negative_control"]["artifact_scores_higher"] is False,
    str(d_ship[:2]),
)
check(
    "scenario end to end: same labels but insects MORE concurrent (97%) -> reframe_diffuse (swapped control, positive twin of the ship case)",
    d_refr[0] == "reframe_diffuse" and d_refr[2]["negative_control"]["artifact_scores_higher"] is True and d_refr[2]["outcomes"]["o1"]["or"] == 4.0,
    str(d_refr[:2]),
)
check(
    "scenario end to end: no label effect (50% vs 50% correct) -> drop with OR 1.0",
    d_drop[0] == "drop" and d_drop[2]["outcomes"]["o1"]["or"] == 1.0,
    str(d_drop[:2]),
)
check(
    "scenario end to end: no labels file, insects concurrent -> reframe_diffuse via O3 alone; o1 'unavailable'",
    d_free[0] == "reframe_diffuse" and d_free[1][0] == "label_free_o3_only" and d_free[2]["outcomes"]["o1"] == "unavailable",
)
b_, a_, l_ = MK.scenario(per_cell=10)
lab_small = {(BA.key_of(row(0, 1, 0.5, sci=BIRD_SCI)), x["t_start"]): x["verdict"] for x in l_}
r_small = BA.build_report(b_, 0, 5.0, TAXA, [(x["t_hour_utc"], x["temp_c"]) for x in a_], lab_small, 1.0)
check(
    "scenario end to end: 10 per cell (120 labelled, 60 per arm) -> insufficient_labels with the sparse-stratum reason (not a p-value verdict)",
    r_small["decision"] == "insufficient_labels"
    and r_small["outcomes"]["o1"]["status"] == "stratum_too_sparse"
    and r_small["reasons"] == ["stratum_too_sparse"],
    str(r_small["reasons"]),
)
check(
    "scenario: the 18-stratum level-tercile sensitivity is reported but too sparse here and never changes the decision",
    d_ship[2]["outcomes"]["o1"]["level_sensitivity"]["status"] == "stratum_too_sparse" and d_ship[0] == "ship_tier",
)


# ================================================================================================== 8. inputs and the CLI
def write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


with tempfile.TemporaryDirectory() as d:
    inn, outd = os.path.join(d, "in"), os.path.join(d, "out")
    os.makedirs(inn)
    os.makedirs(outd)
    bp = os.path.join(inn, "birds.jsonl")
    lines = [
        json.dumps(row(100.0, 2, 0.7)),
        "{torn",
        "[1,2]",
        json.dumps(row(100.0, 3, 0.7)),
        json.dumps(row(0.0, 3, 0.7)),
        json.dumps(row(105.0, 2, 0.7, k="bird_filtered")),
    ]
    write(bp, "\n".join(lines) + "\n")
    rows, garbage = BA.read_jsonl(bp)
    rep = BA.build_report(rows, garbage, 5.0, TAXA, [], None, 7.0)
    check(
        "input: a torn line and a non-object line are counted (garbage_lines 2), valid reports 2 (t=0 row invalid, bird_filtered skipped and counted 1), nothing raised",
        rep["input"]["garbage_lines"] == 2 and rep["input"]["reports"] == 2 and rep["input"]["filtered_skipped"] == 1 and rep["input"]["events"] == 1,
        str(rep["input"]),
    )
    inside = os.path.join(inn, "report.json")
    rc = BA.main(["--birds", bp, "--out", inside])
    check("CLI: --out inside the directory of --birds exits 2 and writes nothing", rc == 2 and not os.path.exists(inside))
    sub = os.path.join(inn, "sub", "r.json")
    check("CLI: --out in a subdirectory of the input directory is refused too (exit 2)", BA.main(["--birds", bp, "--out", sub]) == 2)
    ap = os.path.join(inn, "ambient_temp.jsonl")
    write(ap, "")
    check(
        "CLI: --out inside the directory of --ambient is refused (exit 2)", BA.main(["--birds", bp, "--ambient", ap, "--out", os.path.join(inn, "x.json")]) == 2
    )
    good = os.path.join(outd, "report.json")
    rc = BA.main(["--birds", bp, "--taxa", TAXA_PATH, "--generated-s", "7", "--out", good])
    data = open(good, "rb").read()
    check(
        "CLI twin: --out elsewhere exits 0 and writes one LF-terminated compact JSON line whose decision is insufficient_evidence (no ambient: not a pass)",
        rc == 0
        and data.endswith(b"\n")
        and data.count(b"\n") == 1
        and b"\r" not in data
        and json.loads(data)["decision"] == "insufficient_evidence"
        and json.loads(data)["reasons"] == ["no_ambient"],
        data[:120],
    )
    check(
        "CLI: a missing --ambient file is the same no_ambient verdict, an empty one too; a missing --birds is exit 2",
        BA.main(["--birds", bp, "--ambient", os.path.join(inn, "nope.jsonl"), "--generated-s", "7", "--out", os.path.join(outd, "a.json")]) == 0
        and json.load(open(os.path.join(outd, "a.json")))["reasons"] == ["no_ambient"]
        and BA.main(["--birds", os.path.join(inn, "missing.jsonl")]) == 2,
    )
    check(
        "CLI: a --labels path that does not exist is a usage error (exit 2), not a silent label-free run",
        BA.main(["--birds", bp, "--labels", os.path.join(inn, "no.jsonl")]) == 2,
    )
    check("CLI: --gap-s 0 is exit 2", BA.main(["--birds", bp, "--gap-s", "0"]) == 2)
    a1 = os.path.join(outd, "r1.json")
    a2 = os.path.join(outd, "r2.json")
    BA.main(["--birds", bp, "--taxa", TAXA_PATH, "--generated-s", "7", "--out", a1])
    BA.main(["--birds", bp, "--taxa", TAXA_PATH, "--generated-s", "7", "--out", a2])
    check("CLI: with --generated-s the whole report is byte-identical across runs (deterministic golden)", open(a1, "rb").read() == open(a2, "rb").read())
    BA.main(["--birds", bp, "--taxa", os.path.join(inn, "no_taxa.json"), "--ambient", ap, "--generated-s", "7", "--out", os.path.join(outd, "t.json")])
    rt = json.load(open(os.path.join(outd, "t.json")))
    check(
        "taxa: an unusable --taxa file -> taxa_missing == events (1) and decision insufficient_evidence taxa_unusable",
        rt["input"]["taxa_missing"] == 1 and rt["reasons"] == ["taxa_unusable"],
        str(rt["reasons"]),
    )
    ambp = os.path.join(inn, "amb2.jsonl")
    write(
        ambp,
        json.dumps({"k": "ambient_temp", "v": 1, "t_hour_utc": 0, "temp_c": 3.0, "source": "x", "model_grid_km": None})
        + "\nxx\n"
        + json.dumps({"k": "ambient_temp", "v": 1, "t_hour_utc": "a", "temp_c": 3.0})
        + "\n",
    )
    ambient, ag = BA.load_ambient(ambp)
    check("ambient: 1 valid record, 2 garbage (torn line, non-integer hour)", ambient == [(0, 3.0)] and ag == 2, str((ambient, ag)))
    check(
        "private detail: absent by default, top-10 species names with --private-detail",
        "private_detail" not in rep
        and BA.build_report(rows, 0, 5.0, TAXA, [], None, 1.0, True)["private_detail"] == {"top_species": [{"common": "Test Bird", "events": 1}]},
    )

# ================================================================================================== 8b. hostile values: bad lines are garbage, never a crash
HUGE = 10**400  # a JSON integer no float can hold: math.isfinite raises OverflowError on it
check(
    "is_num: an int too large for a float is False (no OverflowError); twin: 10**300 (still a float) and 7 are True, bool and inf are False",
    BA.is_num(HUGE) is False
    and BA.is_num(-HUGE) is False
    and BA.is_num(10**300) is True
    and BA.is_num(7) is True
    and BA.is_num(True) is False
    and BA.is_num(float("inf")) is False,
    "",
)
check(
    "valid_bird: t = 10**400 is invalid; twin: the same row with t = 100.0 is valid",
    BA.valid_bird(row(HUGE, 2, 0.7)) is False and BA.valid_bird(row(100.0, 2, 0.7)) is True,
)
check(
    "valid_bird: scientific 5, [] and {} are invalid; twin: None, a string and an absent key are valid",
    [BA.valid_bird(row(100.0, 2, 0.7, sci=s)) for s in (5, ["a"], {"a": 1})] == [False, False, False]
    and [BA.valid_bird(row(100.0, 2, 0.7, sci=s)) for s in (None, "Testus avis")] == [True, True]
    and BA.valid_bird({k: v for k, v in row(100.0, 2, 0.7).items() if k != "scientific"}) is True,
)
check(
    "cluster: a row with a non-string scientific is skipped without raising and the good row still makes ONE event keyed by its scientific name; twin: scientific None falls back to the common name",
    [x.key for x in one([row(100.0, 2, 0.7, sci=5), row(100.0, 3, 0.7)])] == ["testus avis"]
    and [x.key for x in one([row(100.0, 2, 0.7, sci=None, common="Plain Name")])] == ["plain name"],
)

with tempfile.TemporaryDirectory() as d:
    inn, outd = os.path.join(d, "in"), os.path.join(d, "out")
    os.makedirs(inn)
    os.makedirs(outd)
    bp = os.path.join(inn, "birds.jsonl")
    ok2 = [row(100.0, 2, 0.7), row(100.0, 3, 0.7), row(103.0, 2, 0.7, level=HUGE)]  # a huge level only loses the level, the row is valid
    bad = [row(HUGE, 2, 0.7), row(100.0, 2, 0.7, sci=5), row(100.0, 2, 0.7, sci=["x"])]
    write(bp, "\n".join(json.dumps(r) for r in bad[:1] + ok2 + bad[1:]) + "\n")
    out1 = os.path.join(outd, "h.json")
    rc = BA.main(["--birds", bp, "--taxa", TAXA_PATH, "--generated-s", "1", "--out", out1])
    hr = json.loads(open(out1, "rb").read())
    check(
        "CLI hostile: 3 bad rows (t 10**400, scientific 5, scientific list) + 3 good -> exit 0, reports 3, events 1, concurrent beams {2,3} at t=100 and a huge level_dbfs kept as no level",
        rc == 0 and hr["input"]["reports"] == 3 and hr["input"]["events"] == 1 and hr["input"]["garbage_lines"] == 0,
        str(hr["input"]),
    )
    lp = os.path.join(inn, "labels.jsonl")
    good_label = {"k": "beam_label", "v": 1, "species_key": "testus avis", "t_start": 100.0, "verdict": "correct"}
    write(lp, json.dumps(good_label) + "\n" + json.dumps(dict(good_label, t_start=HUGE)) + "\n")
    lab, st = BA.read_labels(lp)
    check(
        "labels: t_start = 10**400 is a garbage line (counted, no OverflowError); twin: the finite label is kept",
        lab == {("testus avis", 100.0): "correct"} and st == {"n_lines": 2, "garbage_lines": 1, "unsure_dropped": 0, "duplicates": 0},
        str((lab, st)),
    )
    out2 = os.path.join(outd, "h2.json")
    rc2 = BA.main(["--birds", bp, "--taxa", TAXA_PATH, "--labels", lp, "--generated-s", "1", "--out", out2])
    check("CLI hostile labels: a huge t_start label line does not abort the run (exit 0, report written)", rc2 == 0 and os.path.exists(out2))


# O2 recurrence boundary: the NEXT event of the species must start within RECUR_S (120 s) after this one ends
def o2_cells(second_dt):
    rs = [
        row(T0, 2, 0.6, sci="Alpha beta", common="A"),
        row(T0, 3, 0.6, sci="Alpha beta", common="A"),
        row(T0 + second_dt, 2, 0.6, sci="Alpha beta", common="A"),
    ]
    return BA.o2_recurrence(BA.cluster(rs, 5.0, TAXA), T0 + 2000.0)


check(
    "O2 boundary: next start exactly 120.0 s after the end recurs (concurrent recurs, later single does not: cells 1,0,0,1 -> Haldane OR 9.0, n 2)",
    o2_cells(120.0)["n"] == 2 and o2_cells(120.0)["or"] == 9.0,
    str(o2_cells(120.0)),
)
check("O2 boundary twin: next start 120.5 s after the end does not recur (cells 0,1,0,1 -> OR 1.0)", o2_cells(120.5)["or"] == 1.0, str(o2_cells(120.5)))
check("O2 boundary twin: a later start 5.5 s after the end (inside the window) recurs -> OR 9.0", o2_cells(0.5 + 5.0)["or"] == 9.0, str(o2_cells(5.5)))
big = [row(T0 + 20.0 * i, 2, 0.6, sci="Alpha beta", common="A") for i in range(30000)]
t_wall = time.time()
o2_big = BA.o2_recurrence(BA.cluster(big, 5.0, TAXA), T0 + 20.0 * 30000 + 1000.0)
dt_big = time.time() - t_wall
check(
    "O2 scale: 30000 events of one species finish in under 5 s (sorted + bisect, not a pairwise scan: the pairwise scan needs ~20 s) n 30000 and one arm empty -> or None",
    dt_big < 5.0 and o2_big == {"n": 30000, "or": None, "or_ci95": None},
    "%.1f s %s" % (dt_big, o2_big),
)


# ================================================================================================== 9. parity with the dashboard's DetStore
def parity_rows():
    rs = []
    rs += [
        row(1000.0, 2, 0.9, sci="Alpha one", common="A"),
        row(1000.0, 3, 0.8, sci="Alpha one", common="A"),
        row(1003.0, 2, 0.7, sci="Alpha one", common="A"),
    ]  # gap exactly 3.0: merges
    rs += [row(1006.0 + 1e-9, 2, 0.7, sci="Alpha one", common="A")]  # 3.0 + 1e-9 after the last: new event
    rs += [
        row(1000.0, 5, 0.5, sci="Beta two", common="B"),
        row(1003.0, 5, 0.5, sci="Beta two", common="B"),
        row(1003.0, 6, 0.4, sci="Beta two", common="B"),
        row(1010.0, 5, 0.5, sci="Beta two", common="B"),
    ]
    rs += [row(990.0, 1, 0.6, sci="Beta two", common="B")]  # out of order, 10 s before: its own event
    rs += [row(2000.0, 4, 0.95, sci="Human vocal", common="Human vocal")]  # explicit non-species label
    rs += [row(2000.0, 4, 0.6, sci="", common="Dog", region=None)]  # no scientific name: key is the common name
    rs += [row(3000.0 + 3.0 * i, 1 + (i % 2) * 3, 0.55, sci="Gamma three", common="G", level=-50.0 - i) for i in range(8)]
    rs += [row(3000.0 + 3.0 * i, 2, 0.5, sci="Gamma three", common="G", level=-70.0) for i in range(0, 8, 2)]
    rs += [row(5000.0 + 0.5 * i, 7, 0.3, sci="Delta four", common="D", region=None) for i in range(4)]
    return rs


def ui_tier(ev):
    byt = {}
    for t, b, _c, _l in ev.trace:
        byt.setdefault(t, set()).add(b)
    return BA.tier_of(sum(1 for s in byt.values() if len(s) >= 2))


store = UI.DetStore(1e12, UI.Taxa(TAXA_PATH))
prs = parity_rows()
for r in prs:
    store.add(dict(r))
mine = BA.cluster(prs, UI.EVENT_GAP_S, TAXA)
want = [(e.key, e.t_start, e.t_end, sorted(e.beams), ui_tier(e), e.group, e.non_species, e.max_conf, e.max_level) for e in store.snapshot()]
got = [(e.key, e.t_start, e.t_end, sorted(e.beams), e.tier, e.group, e.non_species, e.max_conf, e.max_level) for e in mine]
check(
    "parity: 6 species keys, 8 events (the human-voice report is hidden by both), boundary and out-of-order rows -> the tool's events equal DetStore's one for one (key, t_start, t_end, beams, tier, group, non_species, max_conf, max_level), %d events"
    % len(want),
    got == want and len(want) == 8,
    str((len(got), len(want))),
)
check(
    "parity: the same rows chained at gap 5.0 are NOT identical to the dashboard's (the descriptive gap differs: fewer events) - the check bites",
    len(BA.cluster(prs, 5.0, TAXA)) < len(want),
    str(len(BA.cluster(prs, 5.0, TAXA))),
)
check("parity: EVENT_GAP_S is 3.0 and LABEL_GAP_S is the same constant", UI.EVENT_GAP_S == 3.0 == BA.LABEL_GAP_S)


# ================================================================================================== 10. golden: 40 synthetic rows
def golden_rows():
    rs, i = [], [0]

    def ev(conf, windows, **kw):
        sci = "Testus avis%02d" % i[0]
        t = T0 + 100.0 * i[0]
        i[0] += 1
        for j, beams in enumerate(windows):
            for b in beams:
                rs.append(row(t + 3.0 * j, b, conf, sci=sci, common=sci, **kw))
        return t

    ev(0.3, [[2]])
    ev(0.3, [[2, 3], [2]])
    ev(0.3, [[2, 3]])
    ev(0.4, [[4]])
    ev(0.4, [[4, 5]])
    ev(0.5, [[2, 3], [2, 3]])
    ev(0.5, [[2]])
    ev(0.65, [[2]])
    ev(0.8, [[6, 7], [6, 7], [6]])
    ev(0.8, [[2], [5]])
    ev(0.9, [[1, 2]] * 4)
    ev(0.95, [[1, 8], [1]])
    ev(1.0, [[3]])
    ev(0.9, [[2, 6], [2, 6]])
    rs.append(row(T0 + 5000.0, 4, 0.9, sci="Human vocal", common="Human vocal"))
    rs.append(row(T0 + 5100.0, 4, 0.9, k="bird_filtered", region=False, sci="Foreign bird", common="Foreign bird"))
    return rs


gr = golden_rows()
rep = BA.build_report(gr, 0, 5.0, TAXA, [], None, 1792972800.0)
bins = [
    (
        b["n"],
        b["tiers"]["confirmed"],
        b["tiers"]["probable"],
        b["tiers"]["single_beam"],
        b["multi_frac"],
        b["mean_windows"],
        b["concurrent_windows"],
        b["adjacent_concurrent_windows"],
        b["adjacent_share"],
        b["confirmed_nonadjacent"],
    )
    for b in rep["bins"]
]
check(
    "golden: 40 rows -> %d valid reports, 14 events (the human-voice report forms none), 1 filtered skipped; whole input block exact" % (len(gr) - 1),
    len(gr) == 40
    and rep["input"]
    == {
        "reports": 39,
        "events": 14,
        "gap_s": 5.0,
        "first_t": T0,
        "last_t": T0 + 5000.0,
        "garbage_lines": 0,
        "filtered_skipped": 1,
        "beam_pairs_with_unequal_t": 0,
        "taxa_missing": 0,
        "ambient_records": 0,
        "ambient_garbage": 0,
        "labels": None,
    },
    str(rep["input"]),
)
check(
    "golden: per bin (n, confirmed, probable, single, multi_frac, mean_windows, concurrent windows, adjacent, adjacent_share, confirmed_nonadjacent) exact",
    bins
    == [
        (3, 0, 2, 1, 0.666667, 1.333333, 2, 2, 1.0, 0),
        (2, 0, 1, 1, 0.5, 1.0, 1, 1, 1.0, 0),
        (2, 1, 0, 1, 0.5, 1.5, 2, 2, 1.0, 0),
        (3, 1, 0, 2, 0.333333, 2.0, 2, 2, 1.0, 0),
        (4, 2, 1, 1, 0.75, 2.25, 7, 5, 0.714286, 1),
    ],
    str(bins),
)
check(
    "golden: the human-voice report forms no event (14 events, all analysed) and the sequential-only event is single_beam with 2 beams",
    sum(b["n"] for b in rep["bins"]) == 14
    and [e for e in BA.cluster(gr, 5.0, TAXA) if e.key == "testus avis09"][0].tier == "single_beam"
    and len([e for e in BA.cluster(gr, 5.0, TAXA) if e.key == "testus avis09"][0].beams) == 2,
)
check(
    "golden: decision on 40 rows with no ambient is insufficient_evidence / no_ambient; o2: 5 band events, none recurs, 2 concurrent vs 3 single -> Haldane OR (0.5*3.5)/(2.5*0.5) == 1.4",
    rep["decision"] == "insufficient_evidence"
    and rep["reasons"] == ["no_ambient"]
    and rep["outcomes"]["o1"] == "unavailable"
    and rep["outcomes"]["o2_proxy"]["n"] == 5
    and rep["outcomes"]["o2_proxy"]["or"] == 1.4,
    str(rep["outcomes"]["o2_proxy"]),
)
check("golden: bins are the five fixed edges", [(b["lo"], b["hi"]) for b in rep["bins"]] == [(0.0, 0.35), (0.35, 0.5), (0.5, 0.65), (0.65, 0.85), (0.85, 1.0)])

# O2 recurrence on a hand table: 3 species-events, concurrent one recurs within 120 s, single one does not
o2rows = [
    row(T0, 2, 0.6),
    row(T0, 3, 0.6),
    row(T0 + 60.0, 2, 0.6),
    row(T0 + 3000.0, 2, 0.6, sci="Other one", common="O"),
    row(T0 + 9000.0, 2, 0.6, sci="Other two", common="P"),
    row(T0 + 9500.0, 2, 0.3, sci="Pad", common="Pad"),
]
o2 = BA.o2_recurrence(BA.cluster(o2rows, 5.0, TAXA), T0 + 9500.0)
check(
    "O2: events A (concurrent, recurs 60 s later), A' (single, no later event: the look-ahead ends before the data does), O1, O2 -> only events whose 120 s look-ahead fits in the data count (n 4)",
    o2["n"] == 4,
    str(o2),
)

check(
    "human-voice labels never form an event (twin: the same row under a species label does)",
    BA.cluster([row(2000.0, 4, 0.95, sci="Human vocal", common="Human vocal")], 5.0, TAXA) == []
    and len(BA.cluster([row(2000.0, 4, 0.95, sci="Dog", common="Dog")], 5.0, TAXA)) == 1
    and BA.HUMAN_LABELS == frozenset(("human vocal", "human non-vocal", "human whistle")),
)

# ================================================================================================== 9. shuffle null (contract 1.1.0)
T1, T2, T3, T4 = T0 + 100.0, T0 + 200.0, T0 + 300.0, T0 + 400.0
HOUR = int(T0 // 3600)


class ScriptRng:
    """Recording stub: sample(pop, k) returns the next scripted list (checked to be a k-subset of pop) and records (pop, k)."""

    def __init__(self, script):
        self.script, self.calls = list(script), []

    def sample(self, pop, k):
        pop = list(pop)
        self.calls.append((pop, k))
        out = self.script.pop(0)
        assert len(out) == k and set(out) <= set(pop), (out, pop, k)
        return out


class FirstRng:
    def __init__(self):
        self.calls = []

    def sample(self, pop, k):
        self.calls.append((list(pop), k))
        return list(pop)[:k]


nr = [row(T1, 1, 0.6), row(T2, 1, 0.6), row(T3, 2, 0.6), row(T4, 2, 0.6)]
grp = BA.shuffle_groups(
    nr + [row(T1, 3, 0.6, sci="Other one", common="O"), row(T0 + 3700.0, 1, 0.6), row(T1, 4, 0.9, sci="Human vocal", common="Human vocal"), row(T1, 9, 0.6)]
)
check(
    "shuffle groups: key (species, UTC hour) -> {beam: row indices}; a second hour and a second species are separate groups; human-voice and invalid-beam rows are in none",
    grp == {("testus avis", HOUR): {1: [0, 1], 2: [2, 3]}, ("other one", HOUR): {3: [4]}, ("testus avis", HOUR + 1): {1: [5]}},
    str(grp),
)

rng_a = ScriptRng([[T1, T2], [T1, T2]])
sh, moved, kept = BA.shuffled_rows(nr, BA.shuffle_groups(nr), rng_a)
check(
    "shuffled_rows: each beam samples its report count from the group's 4 distinct times (recording stub); picks T1,T2 for both beams -> beam and confidence kept, times [T1,T1,T2,T2], 4 moved 0 kept",
    rng_a.calls == [([T1, T2, T3, T4], 2), ([T1, T2, T3, T4], 2)]
    and [(o["t"], o["beam"], o["confidence"]) for o in sh] == [(T1, 1, 0.6), (T1, 2, 0.6), (T2, 1, 0.6), (T2, 2, 0.6)]
    and (moved, kept) == (4, 0),
    str((rng_a.calls, sh)),
)
rng_b = ScriptRng([[T1, T2], [T3, T4]])
sh_b, _, _ = BA.shuffled_rows(nr, BA.shuffle_groups(nr), rng_b)
check(
    "shuffled_rows twin: picks T1,T2 / T3,T4 give the real times back; the input rows are untouched",
    [(o["t"], o["beam"]) for o in sh_b] == [(T1, 1), (T2, 1), (T3, 2), (T4, 2)] and [o["t"] for o in nr] == [T1, T2, T3, T4],
)
two_h = nr + [row(T0 + 3700.0, 1, 0.6), row(T0 + 3800.0, 1, 0.6)]
rng_h = FirstRng()
BA.shuffled_rows(two_h, BA.shuffle_groups(two_h), rng_h)
check(
    "shuffled_rows: the pool is the group's own hour (hour 2 beam 1 samples 2 of [T0+3700, T0+3800], never hour 1 times)",
    rng_h.calls == [([T1, T2, T3, T4], 2), ([T1, T2, T3, T4], 2), ([T0 + 3700.0, T0 + 3800.0], 2)],
    str(rng_h.calls),
)
dup = [row(T1, 1, 0.6), row(T1, 1, 0.7)]
sh_d, mv_d, kp_d = BA.shuffled_rows(dup, BA.shuffle_groups(dup), FirstRng())
check(
    "shuffled_rows: a beam with more reports than the group has distinct times keeps the surplus at its real time (1 moved, 1 kept, both rows survive)",
    (mv_d, kp_d) == (1, 1) and [(o["t"], o["confidence"]) for o in sh_d] == [(T1, 0.6), (T1, 0.7)],
    str(sh_d),
)

# exact null by hand: beam 1 holds {T1,T2}, beam 2 takes each of the 6 pairs of {T1..T4}; per draw pass = |shared| / |union|
pairs = [[T1, T2], [T1, T3], [T1, T4], [T2, T3], [T2, T4], [T3, T4]]
rng_n = ScriptRng([x for p in pairs for x in ([T1, T2], p)])
ev_n = BA.cluster(nr, 5.0, TAXA)
sn = BA.shuffle_null(nr, ev_n, 5.0, TAXA, trials=6, seed=1, rng=rng_n)
b2 = sn["bins"][2]
check(
    "shuffle_null by hand: real 0 of 4 events pass; the 6 enumerated draws give rates 1, 1/3 x4, 0 -> null mean 7/18 = 0.388889, min 0, max 1, mean events 3, excess -0.388889",
    (b2["n"], b2["real_pass_rate"], b2["null_pass_rate"], b2["null_min"], b2["null_max"], b2["null_n_mean"], b2["excess"], b2["runs"])
    == (4, 0.0, 0.388889, 0.0, 1.0, 3.0, -0.388889, 6)
    and sn["all"]["null_pass_rate"] == 0.388889
    and sn["all"]["excess"] == -0.388889
    and sn["trials"] == 6
    and sn["groups"] == 1
    and (sn["reports_shuffled"], sn["reports_unshuffled"]) == (4, 0),
    str(b2),
)
check(
    "shuffle_null: an empty bin reports null rates and 0 runs (twin: bin 0.5-0.65 above is populated)",
    sn["bins"][0]
    == {
        "lo": 0.0,
        "hi": 0.35,
        "n": 0,
        "real_pass_rate": None,
        "null_pass_rate": None,
        "null_min": None,
        "null_max": None,
        "null_n_mean": 0.0,
        "excess": None,
        "runs": 0,
    },
)
rows_z = [row(T1, 1, 0.6), row(T1, 2, 0.6), row(T2, 1, 0.6), row(T2, 2, 0.6)]
sz = BA.shuffle_null(rows_z, BA.cluster(rows_z, 5.0, TAXA), 5.0, TAXA, trials=1, seed=1, rng=ScriptRng([[T1, T2], [T1, T2]]))
check(
    "shuffle_null twin: both beams really on T1,T2 -> real 1.0, null 1.0, excess 0.0 (agreement fully explained by the shared times)",
    (sz["bins"][2]["real_pass_rate"], sz["bins"][2]["null_pass_rate"], sz["bins"][2]["excess"]) == (1.0, 1.0, 0.0),
    str(sz["bins"][2]),
)

# seeded determinism and the statistic against the hand-computed expectation 7/18
s1 = BA.shuffle_null(nr, ev_n, 5.0, TAXA, trials=200, seed=7)
s1b = BA.shuffle_null(nr, ev_n, 5.0, TAXA, trials=200, seed=7)
s2 = BA.shuffle_null(nr, ev_n, 5.0, TAXA, trials=200, seed=8)
check(
    "shuffle_null: same seed -> identical block; another seed -> a different null mean (200 draws each)",
    s1 == s1b and s1["bins"][2]["null_pass_rate"] != s2["bins"][2]["null_pass_rate"] and s1["trials"] == 200 and s1["bins"][2]["runs"] == 200,
    str((s1["bins"][2], s2["bins"][2])),
)
check(
    "shuffle_null: 200 seeded draws land within 0.06 of the exact 7/18 = 0.388889",
    abs(s1["bins"][2]["null_pass_rate"] - 7 / 18) < 0.06,
    str(s1["bins"][2]["null_pass_rate"]),
)

# measured quantile bands
qv = [0.05, 0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 0.95]
check(
    "quantile_cuts: 10 values at q 0.2 0.4 0.6 0.8 -> v[2] v[4] v[6] v[8] = 0.25 0.45 0.65 0.85; no values -> None; one value -> it four times",
    BA.quantile_cuts(qv) == [0.25, 0.45, 0.65, 0.85] and BA.quantile_cuts([]) is None and BA.quantile_cuts([0.7]) == [0.7, 0.7, 0.7, 0.7],
)
q_rows = [row(T0 + 3600.0 * i + 10.0, 1, c, sci="Sp %d" % i, common="Sp %d" % i) for i, c in enumerate(qv)]
sq = BA.shuffle_null(q_rows, BA.cluster(q_rows, 5.0, TAXA), 5.0, TAXA, trials=2, seed=1, rng=FirstRng())
qb = sq["quantile_bands"]
check(
    "quantile_bands (report-only): cuts 0.25 0.45 0.65 0.85, bands [0,0.25) [0.25,0.45) [0.45,0.65) [0.65,0.85) [0.85,1] hold 2 events each, 0.5 sits above 50% of events and 0.85 above 80%, gating false",
    qb["gating"] is False
    and qb["cuts"] == [0.25, 0.45, 0.65, 0.85]
    and qb["quantiles"] == [0.2, 0.4, 0.6, 0.8]
    and [(b["lo"], b["hi"], b["n"]) for b in qb["bands"]] == [(0.0, 0.25, 2), (0.25, 0.45, 2), (0.45, 0.65, 2), (0.65, 0.85, 2), (0.85, 1.0, 2)]
    and qb["arbitrary_cuts"] == [{"cut": 0.5, "fraction_below": 0.5}, {"cut": 0.85, "fraction_below": 0.8}]
    and all(b["real_pass_rate"] == 0.0 and b["null_pass_rate"] == 0.0 and b["excess"] == 0.0 for b in qb["bands"]),
    str(qb),
)
check(
    "quantile_bands: no analysed event -> cuts null, no bands, arbitrary cuts empty",
    BA.shuffle_null([], [], 5.0, TAXA, trials=3, seed=1)["quantile_bands"]
    == {"gating": False, "quantiles": [0.2, 0.4, 0.6, 0.8], "cuts": None, "arbitrary_cuts": [], "bands": []},
)

# report-only: no tier or decision moves
rep_off = BA.build_report(gr, 0, 5.0, TAXA, [], None, 1792972800.0)
rep_on = BA.build_report(gr, 0, 5.0, TAXA, [], None, 1792972800.0, null_trials=20, null_seed=3)
check(
    "build_report: null_trials 0 has no shuffle_null key; 20 trials adds it and every other key (bins, decision, reasons, outcomes) is identical",
    "shuffle_null" not in rep_off
    and rep_on["shuffle_null"]["trials"] == 20
    and rep_on["shuffle_null"]["seed"] == 3
    and {k: v for k, v in rep_on.items() if k != "shuffle_null"} == rep_off,
    str(rep_on["shuffle_null"]["all"]),
)
with tempfile.TemporaryDirectory() as d9:
    inn9, out9 = os.path.join(d9, "in"), os.path.join(d9, "out")
    os.makedirs(inn9)
    os.makedirs(out9)
    write(os.path.join(inn9, "birds.jsonl"), "".join(json.dumps(r) + "\n" for r in nr))
    outs = {}
    for tag, extra in (("default", []), ("zero", ["--null-trials", "0"]), ("five", ["--null-trials", "5", "--null-seed", "9"])):
        rc9 = BA.main(
            ["--birds", os.path.join(inn9, "birds.jsonl"), "--taxa", TAXA_PATH, "--generated-s", "1.0", "--out", os.path.join(out9, tag + ".json")] + extra
        )
        outs[tag] = (rc9, json.load(open(os.path.join(out9, tag + ".json"))))
    check(
        "CLI: default writes shuffle_null with 200 trials and the default seed; --null-trials 0 omits it; --null-trials 5 --null-seed 9 are honoured",
        all(v[0] == 0 for v in outs.values())
        and outs["default"][1]["shuffle_null"]["trials"] == 200
        and outs["default"][1]["shuffle_null"]["seed"] == BA.NULL_SEED == 20261027
        and "shuffle_null" not in outs["zero"][1]
        and outs["five"][1]["shuffle_null"]["trials"] == 5
        and outs["five"][1]["shuffle_null"]["seed"] == 9,
    )
    rc_neg = BA.main(["--birds", os.path.join(inn9, "birds.jsonl"), "--null-trials", "-1", "--out", os.path.join(out9, "neg.json")])
    check(
        "CLI: --null-trials -1 is refused (exit 2) and writes nothing (twin: 0 above is accepted)",
        rc_neg == 2 and not os.path.exists(os.path.join(out9, "neg.json")),
    )

print(f"\n{len(fails)} failure(s)")
sys.exit(1 if fails else 0)
