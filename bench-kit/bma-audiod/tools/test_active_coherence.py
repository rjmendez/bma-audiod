#!/usr/bin/env python3
"""Tests for tools/active_coherence.py. Synthetic data only (contracts/tools/make_active_coherence_fixtures.py), temp directories, exact values,
a positive twin for every negative case, no stubs. Run:  python -I test_active_coherence.py"""

import contextlib, copy, hashlib, io, json, os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "contracts", "tools"))
sys.path.insert(0, os.path.join(HERE, "..", "analysis"))
import active_coherence as T  # noqa: E402
import make_active_coherence_fixtures as M  # noqa: E402

n_checks, fails = 0, []
TMP = tempfile.mkdtemp(prefix="act_test_")


def check(name, cond, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  [" + str(detail)[:300] + "]"))
    if not cond:
        fails.append(name)


def write_data(name, coh, birds):
    d = os.path.join(TMP, name)
    os.makedirs(os.path.join(d, "analysis"))
    M.write_jsonl(os.path.join(d, "analysis", "coherence.jsonl"), coh)
    M.write_jsonl(os.path.join(d, "analysis", "birds.jsonl"), birds)
    return d


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = T.main(argv)
    return rc, out.getvalue(), err.getvalue()


def rep_of(coh, birds, **kw):
    return T.build_report(coh, birds, 1.0, **kw)


def row(rep, sp, band):
    return next((r for r in rep["rows"] if r["species"] == sp and r["band"] == band), None)


def verdicts(rep, sp):
    s = next(x for x in rep["species"] if x["species"] == sp)
    return (s["beam_gated_classification"], s["level_ratio_bearing"], s["duet"])


def raw_of(c):
    """Independent forward model of the estimator bias: E[raw MSC] = c + (1 - c)^2 / K_eff, K_eff = 1 / 0.048."""
    return c + (1 - c) ** 2 * M.FLOOR


C0, B0 = M.scenario()
R = rep_of(C0, B0)

# ---- the scenario, exact values
check(
    "input: 15 coherence records (1 partial skipped), 119 bird rows, 3 human-voice rows excluded, 14 events, 13 with >= 2 beams, 13 overlapping",
    R["input"]
    == {
        "coherence_records": 15,
        "coherence_partial_skipped": 1,
        "birds_rows": 119,
        "excluded_human_voice": 3,
        "events": 14,
        "events_multi_beam": 13,
        "events_overlapping": 13,
    },
    R["input"],
)
check(
    "measurability: 60 s intervals, 33 s events (16.5 windows), 13 event-intervals, 10 isolating (the 3 Carolina Wren 6 s events do not), 2 species ok, 1 insufficient",
    R["measurability"]
    == {
        "verdict": "measurable",
        "interval_s_p50": 60.0,
        "event_s_p50": 33.0,
        "event_windows_p50": 16.5,
        "event_intervals": 13,
        "isolating_event_intervals": 10,
        "isolating_events": 10,
        "species_ok": 2,
        "species_insufficient": 1,
    },
    R["measurability"],
)
check(
    "rows: Barn Owl band 5, Eastern Screech-Owl band 4, Tufted Titmouse band 6, nothing else",
    [(r["species"], r["band"], r["status"], r["events"]) for r in R["rows"]]
    == [("Barn Owl", 5, "ok", 4), ("Eastern Screech-Owl", 4, "insufficient", 2), ("Tufted Titmouse", 6, "ok", 4)],
    R["rows"],
)
owl = row(R, "Barn Owl", 5)
check(
    "Barn Owl band 5: 4 intervals, 12 pair samples (pairs 1-3 x 4), 120 windows, corrected MSC 0.275 active, 0.798 quiet, diff -0.523",
    (owl["intervals"], owl["pair_samples"], owl["windows"], owl["msc_active"], owl["msc_quiet"], owl["msc_diff"]) == (4, 12, 120, 0.275, 0.798, -0.523),
    owl,
)
check(
    "Barn Owl band 5: phase 0.0, spread 40.0, |lag| 1.0, |dl| 4.0; separates, not coherent, bearing dl, lag ok",
    (owl["ph_deg"], owl["ph_spread_deg"], owl["lag_abs"], owl["dl_abs_db"], owl["separates"], owl["coherent"], owl["bearing_dl"], owl["duet_lag"])
    == (0.0, 40.0, 1.0, 4.0, True, False, True, True),
    owl,
)
tit = row(R, "Tufted Titmouse", 6)
check(
    "Tufted Titmouse band 6: corrected MSC 0.95 active, 0.798 quiet, diff 0.152, spread 5.0, |lag| 2.0, |dl| 1.0; coherent, no separation, no bearing",
    (
        tit["msc_active"],
        tit["msc_quiet"],
        tit["msc_diff"],
        tit["ph_spread_deg"],
        tit["lag_abs"],
        tit["dl_abs_db"],
        tit["separates"],
        tit["coherent"],
        tit["bearing_dl"],
        tit["duet_lag"],
    )
    == (0.95, 0.798, 0.152, 5.0, 2.0, 1.0, False, True, False, True),
    tit,
)
check(
    "the corrected values obey the forward bias model: raw(0.275) = 0.3 and raw(0.798) = 0.8 within 1e-3",
    abs(raw_of(owl["msc_active"]) - 0.3) < 1e-3 and abs(raw_of(owl["msc_quiet"]) - 0.8) < 1e-3,
    (raw_of(owl["msc_active"]), raw_of(owl["msc_quiet"])),
)
check(
    "species: Barn Owl go / go / no_go, Eastern Screech-Owl insufficient x3, Tufted Titmouse no_go / no_go / go",
    [(s["species"], s["events"], s["bands_ok"]) for s in R["species"]] == [("Barn Owl", 4, 1), ("Eastern Screech-Owl", 2, 0), ("Tufted Titmouse", 4, 1)]
    and verdicts(R, "Barn Owl") == ("go", "go", "no_go")
    and verdicts(R, "Eastern Screech-Owl") == ("insufficient",) * 3
    and verdicts(R, "Tufted Titmouse") == ("no_go", "no_go", "go"),
    R["species"],
)
check(
    "methods: gating go [Barn Owl] no_go [Tufted Titmouse]; bearing the same; DUET go [Tufted Titmouse] no_go [Barn Owl]",
    R["methods"]
    == {
        "beam_gated_classification": {"verdict": "go", "go": ["Barn Owl"], "no_go": ["Tufted Titmouse"]},
        "level_ratio_bearing": {"verdict": "go", "go": ["Barn Owl"], "no_go": ["Tufted Titmouse"]},
        "duet": {"verdict": "go", "go": ["Tufted Titmouse"], "no_go": ["Barn Owl"]},
    },
    R["methods"],
)
check(
    "params pin the constants",
    R["params"]
    == {
        "hop_s": 2.0,
        "min_cell_windows": 5,
        "isolation_min": 0.5,
        "min_events": 3,
        "min_no_go_species": 2,
        "separate_msc": 0.5,
        "coherent_msc": 0.8,
        "coherent_phase_spread_deg": 30.0,
        "bearing_dl_db": 3.0,
        "duet_lag_samples": 0.5,
        "event_gap_s": 3.0,
    },
    R["params"],
)

# ---- isolation: event windows / active-cell windows >= 0.5
wren_long = [x for x in B0 if x["common"] != "Carolina Wren"] + [
    x for i in range(8, 11) for x in M.long_event(i, [5, 6], "Carolina Wren", "Thryothorus ludovicianus")
]
w1 = row(rep_of(C0, wren_long), "Carolina Wren", 5)
check("twin: the Carolina Wren events lengthened to 33 s are used (band 5, 3 events, ok)", w1 is not None and (w1["events"], w1["status"]) == (3, "ok"), w1)
check("a 6 s event (3 windows) against a 10 window cell (ratio 0.3) is not used", row(R, "Carolina Wren", 5) is None)


def wren_cell_n(n):
    c = copy.deepcopy(C0)
    for r in c[8:11]:
        r["bands"][5]["active"]["n"] = n
    return row(rep_of(c, B0), "Carolina Wren", 5)


check(
    "boundary: a 6 window cell (3 / 6 = 0.5) is used (inclusive), 54 windows", wren_cell_n(6) is not None and wren_cell_n(6)["windows"] == 6 * 9, wren_cell_n(6)
)
check("boundary twin: a 7 window cell (3 / 7 < 0.5) is not used", wren_cell_n(7) is None)
nm = rep_of(C0[8:11], [x for x in B0 if x["common"] == "Carolina Wren"])
check(
    "only non-isolating events: not_measurable, the numbers say why (3 windows of event against 10 of cell), no rows, every method not_measurable",
    nm["measurability"]
    == {
        "verdict": "not_measurable",
        "interval_s_p50": 60.0,
        "event_s_p50": 6.0,
        "event_windows_p50": 3.0,
        "event_intervals": 3,
        "isolating_event_intervals": 0,
        "isolating_events": 0,
        "species_ok": 0,
        "species_insufficient": 0,
    }
    and nm["rows"] == []
    and all(m["verdict"] == "not_measurable" for m in nm["methods"].values()),
    nm["measurability"],
)

# ---- inputs excluded
b2 = [dict(x, common="Carolina Chickadee", scientific="Poecile carolinensis") if x["common"] == "Human vocal" else x for x in B0]
R2 = rep_of(C0, b2)
check(
    "twin: the Human vocal rows relabelled to a bird are an event (15 events, 0 excluded)",
    R2["input"]["events"] == 15 and R2["input"]["excluded_human_voice"] == 0,
    R2["input"],
)
check("Human vocal is never a row, species or string in the report", "Human" not in json.dumps(R))
c3 = [{k: v for k, v in r.items() if k != "partial"} for r in C0]
R3 = rep_of(c3, B0)
check(
    "twin: the partial record made complete is read (16 records, 0 skipped)",
    R3["input"]["coherence_records"] == 16 and R3["input"]["coherence_partial_skipped"] == 0,
    R3["input"],
)
check("the partial record is skipped (15 records, 1 skipped)", R["input"]["coherence_records"] == 15 and R["input"]["coherence_partial_skipped"] == 1)
b4 = B0 + [M.bird(M.BASE_T + 60 * 12 + 10, 1, "Blue Jay", "Cyanocitta cristata"), M.bird(M.BASE_T + 60 * 12 + 11, 2, "Blue Jay", "Cyanocitta cristata")]
check("a single-beam Blue Jay event is not multi-beam (14 events, 13 multi)", R["input"]["events"] - R["input"]["events_multi_beam"] == 1)
check(
    "twin: the same event with beams 1 and 2 is multi-beam (the 3 s gap joins it to the Blue Jay event: 14 of 14)",
    rep_of(C0, b4)["input"]["events"] == 14 and rep_of(C0, b4)["input"]["events_multi_beam"] == 14,
    rep_of(C0, b4)["input"],
)
check(
    "an event outside every interval does not overlap one (13 overlapping)",
    rep_of(C0, B0 + M.long_event(40, [3, 4], "Barn Owl", "Tyto alba"))["input"]["events_overlapping"] == 13,
)

# ---- band rules
c5 = copy.deepcopy(C0)
for r in c5[:4]:
    r["bands"][1]["active"], r["bands"][5]["active"] = r["bands"][5]["active"], None
R5 = rep_of(c5, B0)
check(
    "band 1 (125-250 Hz) is identical_expected and never judged: Barn Owl has that status and no gating verdict",
    row(R5, "Barn Owl", 1)["status"] == "identical_expected" and verdicts(R5, "Barn Owl") == ("insufficient",) * 3,
    row(R5, "Barn Owl", 1),
)
c5b = copy.deepcopy(C0)
for r in c5b[:4]:
    r["bands"][3]["active"], r["bands"][5]["active"] = r["bands"][5]["active"], None
R5b = rep_of(c5b, B0)
check(
    "twin: band 3 (500-1000 Hz) is judged: ok, gating go",
    row(R5b, "Barn Owl", 3)["status"] == "ok" and verdicts(R5b, "Barn Owl")[0] == "go",
    row(R5b, "Barn Owl", 3),
)


# ---- flags at their boundaries
def owl_with(msc=0.3, ph_spread=40.0, lag=1.0, dl=4.0, ph=0.0):
    c = copy.deepcopy(C0)
    for r in c[:4]:
        r["bands"][5]["active"] = M.cell(10, msc, ph_spread, lag, dl, ph)
    return rep_of(c, B0)


check("separates: raw 0.45 (corrected under 0.5) separates", row(owl_with(msc=0.45), "Barn Owl", 5)["separates"])
check("separates twin: raw 0.6 does not", not row(owl_with(msc=0.6), "Barn Owl", 5)["separates"])
check("bearing: |dl| 3.0 is go (inclusive)", row(owl_with(dl=3.0), "Barn Owl", 5)["bearing_dl"] and verdicts(owl_with(dl=3.0), "Barn Owl")[1] == "go")
check("bearing twin: 2.9 is not", not row(owl_with(dl=2.9), "Barn Owl", 5)["bearing_dl"] and verdicts(owl_with(dl=2.9), "Barn Owl")[1] == "no_go")
check(
    "bearing: a negative level difference counts by magnitude (-4.0 -> 4.0, go)",
    row(owl_with(dl=-4.0), "Barn Owl", 5)["dl_abs_db"] == 4.0 and row(owl_with(dl=-4.0), "Barn Owl", 5)["bearing_dl"],
)
check("lag: 0.5 samples is enough (inclusive)", row(owl_with(lag=0.5), "Barn Owl", 5)["duet_lag"])
check("lag twin: 0.4 is not", not row(owl_with(lag=0.4), "Barn Owl", 5)["duet_lag"])
check(
    "lag: a negative lag counts by magnitude (-2.0 -> 2.0)",
    row(owl_with(lag=-2.0), "Barn Owl", 5)["lag_abs"] == 2.0 and row(owl_with(lag=-2.0), "Barn Owl", 5)["duet_lag"],
)
check("DUET go: coherent with spread 30 (inclusive) and lag 2.0", verdicts(owl_with(msc=0.95, ph_spread=30.0, lag=2.0), "Barn Owl")[2] == "go")
check("DUET twin: spread 31 is not coherent -> no_go", verdicts(owl_with(msc=0.95, ph_spread=31.0, lag=2.0), "Barn Owl")[2] == "no_go")
check("DUET twin: coherent but lag 0.2 -> no_go", verdicts(owl_with(msc=0.95, ph_spread=30.0, lag=0.2), "Barn Owl")[2] == "no_go")
check("circular phase mean: +170 and -170 give 180, not 0", abs(abs(T.circ_mean_deg([170, -170], [1, 1])) - 180) < 1e-9)
check("circular phase mean twin: +10 and -10 give 0", abs(T.circ_mean_deg([10, -10], [1, 1])) < 1e-9)
check("the pooled phase is reported: 20 deg -> 20.0", row(owl_with(ph=20.0), "Barn Owl", 5)["ph_deg"] == 20.0)
r2 = rep_of(C0, B0, min_events=2)
check(
    "min_events 2 makes the Eastern Screech-Owl row ok and a gating go",
    row(r2, "Eastern Screech-Owl", 4)["status"] == "ok"
    and r2["methods"]["beam_gated_classification"]["go"] == ["Barn Owl", "Eastern Screech-Owl"]
    and r2["params"]["min_events"] == 2,
    r2["methods"],
)
c6 = copy.deepcopy(C0)
for r in c6:
    for b in r["bands"]:
        b["quiet"], b["n_quiet"] = None, 0
R6 = rep_of(c6, B0)
check(
    "without a quiet cell anywhere the quiet reference and the diff are null, the active value stays 0.275",
    (row(R6, "Barn Owl", 5)["msc_quiet"], row(R6, "Barn Owl", 5)["msc_diff"], row(R6, "Barn Owl", 5)["msc_active"]) == (None, None, 0.275),
)
check(
    "event pairs: beams 3 and 4 (1-based) touch pairs 1, 2, 3; beam 1 touches pairs 0 and 7",
    T.event_pairs({3, 4}) == [1, 2, 3] and T.event_pairs({1}) == [0, 7],
)
e = rep_of([], [])
check(
    "empty input: not_measurable, 0 events, null medians",
    (e["measurability"]["verdict"], e["input"]["events"], e["measurability"]["interval_s_p50"]) == ("not_measurable", 0, None),
)


# ---- an event spanning two intervals: overlap clipped to each interval
def span_scenario(n1):
    """One Barn Owl event of 33 s starting 37 s into interval 0: 23 s in interval 0 (11.5 windows), 10 s in interval 1 (5 windows)."""
    c = [copy.deepcopy(C0[0]), copy.deepcopy(C0[1])]
    c[0]["bands"][5]["active"] = M.cell(10, 0.3, 40.0, 1.0, 4.0)
    c[1]["bands"][5]["active"] = M.cell(n1, 0.3, 40.0, 1.0, 4.0)
    t0 = M.BASE_T + 40
    return c, [M.bird(t0 + 3 * j, [3, 4][j % 2], "Barn Owl", "Tyto alba") for j in range(11)]


sc, sb = span_scenario(11)
S1 = rep_of(sc, sb, min_events=1)
check(
    "spanning event: 1 event, 2 event-intervals, windows 11.5 and 5.0 (p50 8.25), event 33 s; cell n 11 -> 5 / 11 < 0.5, so 1 isolating interval",
    (
        S1["input"]["events_overlapping"],
        S1["measurability"]["event_intervals"],
        S1["measurability"]["event_windows_p50"],
        S1["measurability"]["event_s_p50"],
        S1["measurability"]["isolating_event_intervals"],
        S1["measurability"]["isolating_events"],
    )
    == (1, 2, 8.25, 33.0, 1, 1),
    S1["measurability"],
)
check(
    "spanning event: only interval 0 is sampled (1 interval, 3 pair samples, 30 windows)",
    (row(S1, "Barn Owl", 5)["intervals"], row(S1, "Barn Owl", 5)["pair_samples"], row(S1, "Barn Owl", 5)["windows"]) == (1, 3, 30),
    row(S1, "Barn Owl", 5),
)
sc, sb = span_scenario(10)
S2 = rep_of(sc, sb, min_events=1)
check(
    "spanning event twin: cell n 10 -> 5 / 10 = 0.5 isolates interval 1 too (2 isolating, 2 intervals, 6 pair samples, 60 windows)",
    (
        S2["measurability"]["isolating_event_intervals"],
        row(S2, "Barn Owl", 5)["intervals"],
        row(S2, "Barn Owl", 5)["pair_samples"],
        row(S2, "Barn Owl", 5)["windows"],
    )
    == (2, 2, 6, 60),
    row(S2, "Barn Owl", 5),
)

# ---- quiet reference: median weighted by the cell's windows
qc = [copy.deepcopy(r) for r in C0[:4]]
for r, (n, msc) in zip(qc, [(20, 0.8), (1, 0.2), (1, 0.2), (20, 0.8)]):
    r["bands"][5]["quiet"] = M.cell(n, msc, 10.0, 0.0, 0.0)
bo = [x for x in B0 if x["common"] == "Barn Owl"]
Q1 = row(rep_of(qc, bo), "Barn Owl", 5)
check(
    "quiet reference weighted by n: cells (20, 0.8), (1, 0.2), (1, 0.2), (20, 0.8) give corrected 0.798 (unweighted: 0.167)",
    (Q1["msc_quiet"], Q1["msc_diff"]) == (0.798, -0.523),
    Q1,
)
qc2 = [copy.deepcopy(r) for r in qc]
for r in qc2:
    r["bands"][5]["quiet"]["n"] = 20
Q2 = row(rep_of(qc2, bo), "Barn Owl", 5)
check(
    "quiet reference twin: the same MSC values with equal n give the lower median, raw 0.2 (corrected 0.167, diff 0.108)",
    (Q2["msc_quiet"], Q2["msc_diff"]) == (0.167, 0.108) and abs(raw_of(Q2["msc_quiet"]) - 0.2) < 1e-3,
    Q2,
)

# ---- method verdict: no_go needs >= 2 species measured and failing
one = rep_of(C0[:4], bo)
check(
    "one species fails DUET: the species is no_go but the method stays not_measurable (go [], no_go [Barn Owl])",
    verdicts(one, "Barn Owl")[2] == "no_go" and one["methods"]["duet"] == {"verdict": "not_measurable", "go": [], "no_go": ["Barn Owl"]},
    one["methods"],
)
two = rep_of(C0, wren_long)
check(
    "twin: two species fail DUET (Barn Owl, Carolina Wren): the method is no_go",
    two["methods"]["duet"] == {"verdict": "go", "go": ["Tufted Titmouse"], "no_go": ["Barn Owl", "Carolina Wren"]}
    and rep_of(C0[:11], [x for x in wren_long if x["common"] != "Tufted Titmouse"])["methods"]["duet"]
    == {"verdict": "no_go", "go": [], "no_go": ["Barn Owl", "Carolina Wren"]},
    two["methods"],
)

# ---- CLI
d = write_data("cli", C0, B0)


def digest(d):
    return {f: hashlib.sha256(open(os.path.join(d, "analysis", f), "rb").read()).hexdigest() for f in os.listdir(os.path.join(d, "analysis"))}


before = digest(d)
o = os.path.join(TMP, "out.json")
rc, out, err = run(["--data", d, "--out", o, "--generated-s", str(M.GENERATED_S)])
check(
    "CLI: exit 0, the --out file is the fixture bytes, stdout is the table",
    rc == 0
    and open(o, "rb").read() == open(os.path.join(HERE, "..", "contracts", "fixtures", "active_coherence_report.json"), "rb").read()
    and out == T.table(R),
    err,
)
check("CLI: the input files are unchanged and nothing else was created in --data", before == digest(d) and os.listdir(d) == ["analysis"])
rc, out, err = run(["--data", d, "--out", os.path.join(d, "analysis", "r.json")])
check(
    "CLI: --out inside --data refused, exit 2, nothing written",
    rc == 2 and "refusing" in err and not os.path.exists(os.path.join(d, "analysis", "r.json")) and out == "",
    err,
)
rc, out, err = run(["--data", os.path.join(TMP, "none")])
check("CLI: a missing data directory is an empty report, exit 0, not_measurable", rc == 0 and out.startswith("active_coherence v1 not_measurable\n"), out[:100])
d2 = write_data("rot", C0[:8], B0)
os.replace(os.path.join(d2, "analysis", "coherence.jsonl"), os.path.join(d2, "analysis", "coherence.jsonl.1"))
M.write_jsonl(os.path.join(d2, "analysis", "coherence.jsonl"), C0[8:])
rc, out, err = run(["--data", d2, "--generated-s", "1.0"])
check("a rotated generation (coherence.jsonl.1) is read: same table as the unrotated run", rc == 0 and out == T.table(R), out[:200])
lines = T.table(R).split("\n")
check(
    "table: header and counts lines exact",
    lines[:2] == ["active_coherence v1 measurable", "events 14 multi_beam 13 overlapping 13; excluded_human_voice 3"],
    lines[:2],
)

shutil.rmtree(TMP, ignore_errors=True)
print("%d checks, %d failed" % (n_checks, len(fails)))
sys.exit(1 if fails else 0)
