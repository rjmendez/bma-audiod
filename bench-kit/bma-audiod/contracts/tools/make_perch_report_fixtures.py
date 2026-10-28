#!/usr/bin/env python3
"""Regenerate contracts/fixtures/perch_report.json by running the REAL tools/perch_report.py on a synthetic scenario:

    python -I make_perch_report_fixtures.py [out_dir]      (default: ../fixtures)

`scenario()` is also what tools/test_perch_report.py builds its tests from. All data is invented."""

import json, os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "tools")))
import perch_report as PR  # noqa: E402

BASE_T = 1792972800.0  # UTC hour boundary, 00:00 of a day
GENERATED_S = 1792972800.0
MV = "tiny_perch.onnx@a232216a8aa2"
CACH = ("Poecile carolinensis", "Carolina Chickadee")
BCCH = ("Poecile atricapillus", "Black-capped Chickadee")
BLJA = ("Cyanocitta cristata", "Blue Jay")
RBWO = ("Melanerpes carolinus", "Red-bellied Woodpecker")
FILL = [["Insect", -0.4], ["Cricket", -0.5], ["Alarm", -0.6], ["Wind_chime", -0.7]]


def top(sci, logit):
    return [[sci, logit]] + [list(x) for x in FILL]


def perch_row(t, src, beam, sci, logit, labels=None):
    return {
        "k": "perch",
        "v": 1,
        "t": t,
        "src": src,
        "beam": beam,
        "level_dbfs": -20.0,
        "top": labels or top(sci, logit),
        "top1_group": "bird",
        "best_species": [sci, logit],
        "mv": MV,
        "run": 1792972000,
        "emb_i": None,
    }


def bird_row(t, sp, conf, beam):
    return {"k": "bird", "t": t, "beam": beam, "level_dbfs": -50.0, "common": sp[1], "scientific": sp[0], "confidence": conf, "provenance": "model_opinion"}


# (offset s, perch beam, perch species, perch logit, birdnet species or None, confidence, birdnet beam, human voice in perch top)
PLAN = [
    (10, 4, CACH, 6.0, CACH, 0.91, 4, False),  # same species, conf bin 0.85-1.00, hour 0
    (20, 4, CACH, 5.5, CACH, 0.88, 4, False),  # same species
    (30, 2, BCCH, 6.1, CACH, 0.60, 4, False),  # same genus, different species
    (40, 2, BLJA, 5.0, RBWO, 0.55, 2, False),  # different genera; Perch never names RBWO
    (50, 3, CACH, 0.5, CACH, 0.30, 3, False),  # Perch below min_logit: birds_only
    (60, 3, BLJA, 4.0, None, 0.0, 0, False),  # perch_only
    (70, 3, BLJA, 0.2, None, 0.0, 0, False),  # both silent
    (80, 4, CACH, 7.0, CACH, 0.90, 4, True),  # human voice in the Perch top 5: excluded
    (90, 4, CACH, 7.0, ("Human vocal", "Human vocal"), 0.9, 4, False),  # BirdNET human label: excluded
    (3600 + 10, 5, BCCH, 6.0, BCCH, 0.95, 5, False),  # hour 1, same species
    (3600 + 20, 5, BCCH, 6.0, BLJA, 0.72, 5, False),  # hour 1, different genera, conf bin 0.70-0.85
]


def scenario():
    perch, birds = (
        [
            {
                "k": "perch_agreement",
                "v": 1,
                "t": BASE_T,
                "mv": MV,
                "min_logit": 1.0,
                "windows": 0,
                "both_same_species": 0,
                "both_same_genus": 0,
                "both_differ": 0,
                "perch_only": 0,
                "birds_only": 0,
                "both_silent": 0,
                "birds_missing": 0,
                "perch_top1_groups": {},
                "insect": {"perch": 0, "birds": 0, "both": 0},
            }
        ],
        [],
    )
    for off, beam, ps, logit, bs, conf, bbeam, voice in PLAN:
        t1 = BASE_T + off
        lab = ([["Speech", 3.0]] + top(ps[0], logit)[:4]) if voice else None
        perch.append(perch_row(t1, "mixed", None, ps[0], logit, lab))
        perch.append(perch_row(t1, "loudest", beam, ps[0], logit, lab))
        if bs is not None:
            birds.append(bird_row(t1 - 1.0, bs, conf, bbeam))
    return perch, birds


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


def build(out_dir):
    work = tempfile.mkdtemp(prefix="prfix_")
    try:
        perch, birds = scenario()
        os.makedirs(os.path.join(work, "in", "analysis"))
        write_jsonl(os.path.join(work, "in", "analysis", "perch.jsonl"), perch)
        write_jsonl(os.path.join(work, "in", "analysis", "birds.jsonl"), birds)
        os.makedirs(os.path.join(work, "out"))
        so = sys.stdout
        sys.stdout = open(os.devnull, "w")
        try:
            rc = PR.main(["--data", os.path.join(work, "in"), "--out", os.path.join(work, "out", "r.json"), "--generated-s", repr(GENERATED_S)])
        finally:
            sys.stdout.close()
            sys.stdout = so
        if rc != 0:
            raise SystemExit("perch_report.py failed")
        os.makedirs(out_dir, exist_ok=True)
        dst = os.path.join(out_dir, "perch_report.json")
        with open(os.path.join(work, "out", "r.json"), "rb") as f:
            data = f.read()
        with open(dst, "wb") as f:
            f.write(data)
        return dst
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    print(build(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "fixtures")))
