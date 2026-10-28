#!/usr/bin/env python3
"""Tests for tools/perch_report.py. Synthetic data only (contracts/tools/make_perch_report_fixtures.py), temp directories, exact values, a positive twin
for every negative case, no stubs. Run:  python -I test_perch_report.py"""

import contextlib, copy, hashlib, io, json, os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "contracts", "tools"))
sys.path.insert(0, os.path.join(HERE, "..", "analysis"))
import perch_report as T  # noqa: E402
import make_perch_report_fixtures as M  # noqa: E402
import labels as L  # noqa: E402

n_checks, fails = 0, []
TMP = tempfile.mkdtemp(prefix="prt_test_")


def check(name, cond, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  [" + str(detail)[:300] + "]"))
    if not cond:
        fails.append(name)


def write_data(name, perch, birds):
    d = os.path.join(TMP, name)
    os.makedirs(os.path.join(d, "analysis"))
    M.write_jsonl(os.path.join(d, "analysis", "perch.jsonl"), perch)
    M.write_jsonl(os.path.join(d, "analysis", "birds.jsonl"), birds)
    return d


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = T.main(argv)
    return rc, out.getvalue(), err.getvalue()


def rep_of(perch, birds, **kw):
    return T.build_report(perch, birds, 1.0, **kw)


P0, B0 = M.scenario()
R = rep_of(P0, B0)

# ---- the scenario, exact values
check(
    "windows: 9 counted (11 minus 2 human-voice), categories 3/1/2/1/1/1",
    R["windows"] == {"total": 9, "both_same_species": 3, "both_same_genus": 1, "both_differ": 2, "perch_only": 1, "birds_only": 1, "both_silent": 1},
    R["windows"],
)
check(
    "input counts: 22 perch records, 11 windows, 8 birds rows (the Human vocal row dropped), 2 excluded",
    R["input"] == {"perch_records": 22, "perch_windows": 11, "birds_rows": 8, "excluded_human_voice": 2},
    R["input"],
)
check(
    "agreement 3 of 6 = 0.5; min_logit 1.0 from the perch_agreement record",
    R["agreement"] == {"compared": 6, "agree": 3, "rate": 0.5} and R["min_logit"] == 1.0,
)
check(
    "per species: CACH 3 compared 2 agree 0.6667; BCCH 1/1; BLJA 1/0; RBWO 1/0",
    [(r["key"], r["compared"], r["agree"], r["rate"]) for r in R["per_species"]]
    == [("cyanocitta cristata", 1, 0, 0.0), ("melanerpes carolinus", 1, 0, 0.0), ("poecile atricapillus", 1, 1, 1.0), ("poecile carolinensis", 3, 2, 0.6667)],
    R["per_species"],
)
check("by beam: 2 -> 0/2, 4 -> 2/2, 5 -> 1/2", [(r["key"], r["compared"], r["agree"]) for r in R["by_beam"]] == [(2, 2, 0), (4, 2, 2), (5, 2, 1)], R["by_beam"])
check(
    "by confidence bin: 0.50-0.70 0/2, 0.70-0.85 0/1, 0.85-1.00 3/3 (the 0.30 window is birds_only, not compared)",
    [(r["key"], r["compared"], r["agree"]) for r in R["by_confidence_bin"]] == [("0.50-0.70", 2, 0), ("0.70-0.85", 1, 0), ("0.85-1.00", 3, 3)],
    R["by_confidence_bin"],
)
check("by hour UTC: 0 -> 2/4, 1 -> 1/2", [(r["key"], r["compared"], r["agree"]) for r in R["by_hour_utc"]] == [(0, 4, 2), (1, 2, 1)], R["by_hour_utc"])
check(
    "disagreements: 3 in time order, genus flag on the Poecile pair only, scores and logits exact",
    [
        (i["t"] - M.BASE_T, i["beam"], i["birdnet"]["species"], i["birdnet"]["score"], i["perch"]["species"], i["perch"]["logit"], i["same_genus"])
        for i in R["disagreements"]["items"]
    ]
    == [
        (30.0, 2, "Poecile carolinensis", 0.6, "Poecile atricapillus", 6.1, True),
        (40.0, 2, "Melanerpes carolinus", 0.55, "Cyanocitta cristata", 5.0, False),
        (3620.0, 5, "Cyanocitta cristata", 0.72, "Poecile atricapillus", 6.0, False),
    ],
    R["disagreements"]["items"],
)
check(
    "coverage: 4 BirdNET species, Perch names 3, never named = Melanerpes carolinus (1 report)",
    R["coverage"]
    == {"birdnet_species": 4, "named_by_perch": 3, "never_named": [{"key": "melanerpes carolinus", "common": "Red-bellied Woodpecker", "reports": 1}]},
    R["coverage"],
)
check("top-1 group totals: bird 9", R["perch_top1_groups"] == {"bird": 9})

# ---- event ids are the label store's
evs = {e.event_id for e in L.iter_events(B0)}
check(
    "disagreement event ids are labels.py event ids of the BirdNET rows",
    all(i["event_id"] in evs for i in R["disagreements"]["items"]) and len(R["disagreements"]["items"]) == 3,
)

# ---- hidden labels
p2 = copy.deepcopy(P0)
for r in p2:
    if r.get("k") == "perch" and r["t"] == M.BASE_T + 80:
        r["top"] = M.top("Poecile carolinensis", 7.0)
R2 = rep_of(p2, B0)
check(
    "positive twin: the same window without the Speech label is counted (total 10, compared 7, same_species 4)",
    R2["windows"]["total"] == 10 and R2["agreement"]["compared"] == 7 and R2["windows"]["both_same_species"] == 4 and R2["input"]["excluded_human_voice"] == 1,
    R2["windows"],
)
b3 = copy.deepcopy(B0)
for r in b3:
    if r["common"] == "Human vocal":
        r.update(common="Carolina Chickadee", scientific="Poecile carolinensis")
R3 = rep_of(P0, b3)
check(
    "positive twin: the same BirdNET row relabelled to a bird is counted (total 10, same_species 4)",
    R3["windows"]["total"] == 10 and R3["windows"]["both_same_species"] == 4 and R3["input"]["excluded_human_voice"] == 1,
    R3["windows"],
)
for lab in ("Human vocal", "Human non-vocal", "Speech", "Singing", "Male_speech_and_man_speaking", "Homo sapiens"):
    check("hidden(%r) is True" % lab, T.hidden(lab))
for lab in ("Poecile carolinensis", "Insect", "Humming bird", "Cricket"):
    check("hidden(%r) is False (twin)" % lab, not T.hidden(lab), lab)
txt = json.dumps(R)
check("no human-voice label string appears anywhere in the report", "Human vocal" not in txt and "Speech" not in txt)
import perch as PERCH  # noqa: E402

check("PERCH_VOICE_LABELS equals perch.py HUMAN_VOICE_LABELS (lower case)", T.PERCH_VOICE_LABELS == frozenset(s.lower() for s in PERCH.HUMAN_VOICE_LABELS))

# ---- min_logit
R4 = rep_of(P0, B0, min_logit=5.5)
check(
    "--min-logit 5.5: the logit 5.0 window goes silent (birds_only 2); the logit 5.5 window stays named (same_species 3)",
    R4["windows"]["birds_only"] == 2 and R4["windows"]["both_same_species"] == 3 and R4["min_logit"] == 5.5,
    R4["windows"],
)
R5 = rep_of([r for r in P0 if r["k"] == "perch"], B0)
check(
    "no perch_agreement record and no --min-logit: min_logit null, Perch names nothing (compared 0, rate null)",
    R5["min_logit"] is None and R5["agreement"] == {"compared": 0, "agree": 0, "rate": None},
    R5["agreement"],
)

# ---- coverage uses mixed rows and min_logit
p6 = copy.deepcopy(P0)
p6.append(M.perch_row(M.BASE_T + 2000, "mixed", None, "Melanerpes carolinus", 3.0))
R6 = rep_of(p6, B0)
check(
    "a mixed record naming Melanerpes carolinus at logit 3.0 puts it in coverage (never_named empty, named 4)",
    R6["coverage"]["never_named"] == [] and R6["coverage"]["named_by_perch"] == 4,
    R6["coverage"],
)
p7 = copy.deepcopy(P0)
p7.append(M.perch_row(M.BASE_T + 2000, "mixed", None, "Melanerpes carolinus", 0.9))
check("twin: the same record below min_logit does not", [r["key"] for r in rep_of(p7, B0)["coverage"]["never_named"]] == ["melanerpes carolinus"])
p8 = copy.deepcopy(P0)
p8.append(M.perch_row(M.BASE_T + 2000, "mixed", None, "Melanerpes carolinus", 3.0, [["Speech", 3.0]] + M.top("x", 0)[:4]))
check("a mixed record with Speech in its top 5 names nothing", [r["key"] for r in rep_of(p8, B0)["coverage"]["never_named"]] == ["melanerpes carolinus"])

# ---- BirdNET opinion: highest confidence, and the overlap bounds [t1 - 3, t1 + 1]
T1 = M.BASE_T + 60  # the perch_only window (no BirdNET row of its own)


def window_rep(extra):
    return rep_of(P0, B0 + extra)


hi = window_rep([M.bird_row(T1 - 1.0, M.CACH, 0.4, 3), M.bird_row(T1 - 0.5, M.BCCH, 0.8, 3)])
check(
    "opinion is the highest-confidence row: Perch BLJA vs BirdNET BCCH 0.8 (not CACH 0.4) -> both_differ, birdnet score 0.8",
    hi["windows"]["perch_only"] == 0
    and hi["windows"]["both_differ"] == 3
    and hi["disagreements"]["items"][2]["birdnet"] == {"species": "Poecile atricapillus", "common": "Black-capped Chickadee", "score": 0.8},
    hi["disagreements"]["items"],
)
for off, inside in ((-3.0, True), (1.0, True), (-3.01, False), (1.01, False)):
    r = window_rep([M.bird_row(T1 + off, M.CACH, 0.9, 3)])
    check(
        "a BirdNET row at t1%+.2f %s the window (perch_only %d)" % (off, "is in" if inside else "is outside", 0 if inside else 1),
        r["windows"]["perch_only"] == (0 if inside else 1) and r["windows"]["both_differ"] == (3 if inside else 2),
        r["windows"],
    )

# ---- cap, empty, rotation
R9 = rep_of(P0, B0, max_disagreements=2)
check(
    "--max-disagreements 2: total 3, listed 2, items 2",
    (R9["disagreements"]["total"], R9["disagreements"]["listed"], len(R9["disagreements"]["items"])) == (3, 2, 2),
)
R10 = rep_of([], [])
check(
    "empty input: zero windows, null rate, empty tables",
    R10["windows"]["total"] == 0 and R10["agreement"]["rate"] is None and R10["per_species"] == [] and R10["coverage"]["never_named"] == [],
)
d = write_data("rot", P0[:12], B0)
os.replace(os.path.join(d, "analysis", "perch.jsonl"), os.path.join(d, "analysis", "perch.jsonl.1"))
M.write_jsonl(os.path.join(d, "analysis", "perch.jsonl"), P0[12:])
rc, out, err = run(["--data", d, "--generated-s", "1.0"])
check("a rotated generation (perch.jsonl.1) is read: same table as the unrotated run", rc == 0 and out == T.table(R), out[:200])

# ---- CLI
d = write_data("cli", P0, B0)


def digest(d):
    return {f: hashlib.sha256(open(os.path.join(d, "analysis", f), "rb").read()).hexdigest() for f in os.listdir(os.path.join(d, "analysis"))}


before = digest(d)
o = os.path.join(TMP, "out.json")
rc, out, err = run(["--data", d, "--out", o, "--generated-s", "1792972800"])
check(
    "CLI: exit 0, the --out file is the fixture bytes, stdout is the table",
    rc == 0 and open(o, "rb").read() == open(os.path.join(HERE, "..", "contracts", "fixtures", "perch_report.json"), "rb").read() and out == T.table(R),
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
check("CLI: a missing data directory is an empty report, exit 0", rc == 0 and "windows 0:" in out, out[:100])
lines = T.table(R, 2).split("\n")
check(
    "table: header, window line and agreement line exact",
    lines[:3]
    == [
        "perch_report v1 min_logit=1.0",
        "windows 9: same_species 3 same_genus 1 differ 2 perch_only 1 birds_only 1 silent 1; excluded_human 2",
        "top-1 agreement 50.0% (3/6)",
    ],
    lines[:3],
)

shutil.rmtree(TMP, ignore_errors=True)
print("%d checks, %d failed" % (n_checks, len(fails)))
sys.exit(1 if fails else 0)
