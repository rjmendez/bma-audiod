#!/usr/bin/env python3
"""Regenerate contracts/fixtures/eval_report.json by running the REAL tools/eval_models.py on synthetic labels:

    python -I make_eval_report_fixtures.py [out_dir]      (default: ../fixtures)

`scenario()` is also what tools/test_eval_models.py builds its tests from. All data is invented."""

import json, os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BMA = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(BMA, "tools"))
sys.path.insert(0, os.path.join(BMA, "analysis"))
import eval_models as EM  # noqa: E402
import labels as L  # noqa: E402

BASE_T = 1792972800.0
GENERATED_S = 1792972800.0
MV = "tiny_perch.onnx@a232216a8aa2"
CACH = ("Poecile carolinensis", "Carolina Chickadee")
BCCH = ("Poecile atricapillus", "Black-capped Chickadee")
BLJA = ("Cyanocitta cristata", "Blue Jay")
RBWO = ("Melanerpes carolinus", "Red-bellied Woodpecker")
GFWO = ("Melanerpes aurifrons", "Golden-fronted Woodpecker")
TCRI = ("Oecanthus dulcisonans", "Sweet-tailed Tree Cricket")
HUMV = ("Human vocal", "Human vocal")
FILL = [["Insect", -0.4], ["Cricket", -0.5], ["Alarm", -0.6], ["Wind_chime", -0.7]]
CONFUSIONS = {"Golden-fronted Woodpecker": {"likely_as": RBWO[1], "likely_as_scientific": RBWO[0], "scientific": GFWO[0], "source": "co-occurrence within 5 s"}}


def t_of(i):
    return BASE_T + 100.0 * i


def bird_row(t, sp, conf, beam=3):
    return {"k": "bird", "t": t, "beam": beam, "level_dbfs": -50.0, "common": sp[1], "scientific": sp[0], "confidence": conf, "provenance": "model_opinion"}


def perch_row(t, top, mv=MV, src="loudest", beam=3):
    return {
        "k": "perch",
        "v": 1,
        "t": t,
        "src": src,
        "beam": beam,
        "level_dbfs": -20.0,
        "top": top,
        "top1_group": "bird",
        "best_species": [top[0][0], top[0][1]],
        "mv": mv,
        "run": 1792972000,
        "emb_i": None,
    }


def ptop(*pairs):
    return [list(p) for p in pairs] + [list(x) for x in FILL]


# i: (claimed species, BirdNET confidence, verdict, wrong_reason, corrected species)
PLAN = {}
for i in range(0, 8):
    PLAN[i] = (CACH, 0.9, "correct", None, None)
for i in (8, 9):
    PLAN[i] = (CACH, 0.3, "correct", None, None)
for i in (10, 11):
    PLAN[i] = (BLJA, 0.8, "wrong", "other_species", CACH)
for i in (12, 13):
    PLAN[i] = (RBWO, 0.6, "wrong", "other_species", GFWO)
for i in (14, 15):
    PLAN[i] = (BCCH, 0.5, "wrong", "other_species", CACH)
for i in (16, 17):
    PLAN[i] = (BLJA, 0.4, "wrong", "mechanical", None)
PLAN[18] = (CACH, 0.4, "wrong", "other_species", None)
PLAN[19] = (CACH, 0.4, "unsure", None, None)
PLAN[20] = (CACH, 0.4, "artifact", None, None)
for i in (21, 22):
    PLAN[i] = (TCRI, 0.7, "correct", None, None)
PLAN[23] = (CACH, 0.9, "correct", None, None)  # a Speech tag within the window: excluded as human voice
PLAN[25] = (HUMV, 0.9, "correct", None, None)  # a hidden label: excluded as human voice
# Perch loudest windows (end time = event + 1 s): events 0-13 and 16, 17
PERCH = {}
for i in range(0, 8):
    PERCH[i] = ptop((CACH[0], 6.0))
PERCH[8] = ptop((BCCH[0], 3.0), (CACH[0], 2.0))  # near miss, right in the top 5
PERCH[9] = ptop((CACH[0], 5.0))
PERCH[10] = ptop((CACH[0], 5.0))
PERCH[11] = ptop((BLJA[0], 4.0))  # wrong, not near, not in the top 5
PERCH[12] = ptop((GFWO[0], 5.0))
PERCH[13] = ptop((RBWO[0], 5.0), (GFWO[0], 2.0))  # near miss, right in the top 5
PERCH[16] = ptop(("Insect", 3.0))
PERCH[17] = ptop((BLJA[0], 4.0))
# PANNs-style class rows (end time = event + 0.5 s): events 0-15, 21, 22 and the speech-tagged event 23
TAGS = {}
for i in range(0, 8):
    TAGS[i] = [["Bird", 0.6], ["Animal", 0.2]]
for i in (8, 9):
    TAGS[i] = [["Silence", 0.3], ["Bird", 0.25]]
for i in range(10, 16):
    TAGS[i] = [["Insect", 0.5], ["Bird", 0.3]]
for i in (21, 22):
    TAGS[i] = [["Insect", 0.9], ["Bird", 0.1]]
TAGS[23] = [["Speech", 0.4], ["Bird", 0.3]]
CLAP = {i: [["bird song", 0.7], ["silence", 0.1]] for i in range(0, 5)}


def scenario():
    """-> (birds rows, perch rows, tags rows, clap rows, label records)."""
    birds, perch, tags, clap = [], [], [], []
    for i, (sp, conf, *_rest) in sorted(PLAN.items()):
        birds.append(bird_row(t_of(i), sp, conf))
    birds.append(bird_row(t_of(10) + 1.5, CACH, 0.35))  # a second, lower BirdNET species inside event 10's window: BirdNET top 5 holds the truth
    birds.append(bird_row(t_of(11) + 1.5, CACH, 0.35))
    for i, top in sorted(PERCH.items()):
        perch.append(perch_row(t_of(i) + 1.0, top))
        perch.append(perch_row(t_of(i) + 1.0, top, src="mixed", beam=None))
    for i, top in sorted(TAGS.items()):
        tags.append({"k": "tags", "t": t_of(i) + 0.5, "beam": 3, "speech_p": 0.1, "speech_active": False, "top": top, "emb_stored": False})
    for i, top in sorted(CLAP.items()):
        clap.append({"k": "clap", "t": t_of(i) + 0.5, "beam": 3, "top": top})
    events = {e.event_id: e for e in L.iter_events(birds)}
    recs = []
    label_t = [BASE_T + 90000.0]

    def add(ev, verdict, reason, corrected):
        label_t[0] += 1.0
        recs.append(
            L.make_record(ev, verdict, "metadata_only", wrong_reason=reason, corrected_key=corrected, t_label=label_t[0], label_id="%016x" % (len(recs) + 1))
        )

    by_t = {round(e.t_start, 3): e for e in events.values()}
    add(by_t[round(t_of(0), 3)], "wrong", "mechanical", None)  # superseded by the later verdict below
    for i, (sp, conf, verdict, reason, corr) in sorted(PLAN.items()):
        add(by_t[round(t_of(i), 3)], verdict, reason, corr[0].lower() if corr else None)
    add(by_t[round(t_of(0), 3)], "correct", None, None)  # the effective verdict of event 0
    ghost = L.make_record(by_t[round(t_of(0), 3)], "correct", "metadata_only", t_label=label_t[0] + 1.0, label_id="%016x" % (len(recs) + 1))
    ghost.update(event_id="1700000000000-no-such-event", t_start_ms=1700000000000)
    recs.append(ghost)  # a label whose event is not in birds.jsonl
    return birds, perch, tags, clap, recs


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


def write_data(root, scen=None):
    birds, perch, tags, clap, recs = scen or scenario()
    os.makedirs(os.path.join(root, "analysis"))
    os.makedirs(os.path.join(root, "labels"))
    for name, rows in (("birds", birds), ("perch", perch), ("tags", tags), ("clap", clap)):
        write_jsonl(os.path.join(root, "analysis", name + ".jsonl"), rows)
    write_jsonl(os.path.join(root, "labels", "labels.jsonl"), recs)
    with open(os.path.join(root, "confusions.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(CONFUSIONS, f)
    return root


ARGS = ["--min-n", "12", "--min-species-n", "2", "--min-bin-n", "2", "--generated-s", repr(GENERATED_S)]


def build(out_dir):
    work = tempfile.mkdtemp(prefix="evfix_")
    try:
        write_data(os.path.join(work, "in"))
        os.makedirs(os.path.join(work, "out"))
        so = sys.stdout
        sys.stdout = open(os.devnull, "w")
        try:
            rc = EM.main(
                ["--data", os.path.join(work, "in"), "--confusions", os.path.join(work, "in", "confusions.json"), "--out", os.path.join(work, "out", "r.json")]
                + ARGS
            )
        finally:
            sys.stdout.close()
            sys.stdout = so
        if rc != 0:
            raise SystemExit("eval_models.py failed")
        os.makedirs(out_dir, exist_ok=True)
        dst = os.path.join(out_dir, "eval_report.json")
        with open(os.path.join(work, "out", "r.json"), "rb") as f:
            data = f.read()
        with open(dst, "wb") as f:
            f.write(data)
        return dst
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    print(build(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "fixtures")))
