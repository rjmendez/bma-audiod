#!/usr/bin/env python3
"""Regenerate contracts/fixtures/verdict_labels.jsonl and review_queue.json by running the REAL tools/bma_labels.py (a subprocess:
`queue`, then three `add` calls) over a small SYNTHETIC data dir (standard library only):

    python -I make_label_fixtures.py [out_dir]      (default: ../fixtures)

The synthetic birds.jsonl / tags.jsonl have 16 events of 8 species at fixed times (ids start 1792940400125); the clock is fixed
(--now), the seed is 0, so the output is byte for byte reproducible.  Nothing here is real: no coordinates, no real timestamps of
the owner's data, no audio.  The events cover every stratum and every exclusion reason (see EVENTS)."""

import json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.abspath(os.path.join(HERE, "..", "..", "tools", "bma_labels.py"))
TAXA = os.path.abspath(os.path.join(HERE, "..", "..", "ui", "taxa.json"))
T0 = 1792940400.125
NOW = 1792945000.0
SPEC = {
    "rbw": ("Red-bellied Woodpecker", "Melanerpes carolinus"),
    "wbn": ("White-breasted Nuthatch", "Sitta carolinensis"),
    "eso": ("Eastern Screech-Owl", "Megascops asio"),
    "hw": ("Hairy Woodpecker", "Dryobates villosus"),
    "nc": ("Northern Cardinal", "Cardinalis cardinalis"),
    "gfw": ("Golden-fronted Woodpecker", "Melanerpes aurifrons"),
    "st": ("Song Thrush", "Turdus philomelos"),
    "kd": ("Common Virtuoso Katydid", "Pterophylla camellifolia"),
}
# (species, [(dt_s, beam, confidence)], in_region, kind, speech)   in_region: True / None (pre-filter) / False (bird_filtered rows)
# speech: None = a quiet tags row at the event, "active" = a speech_active tags row 4 s later, "none" = no tags row near, "label" = Human label
EVENTS = [
    ("rbw", [(0, 1, 0.30)], True, "bird", None),  # low
    ("rbw", [(0, 2, 0.55)], True, "bird", None),  # near
    ("rbw", [(0, 3, 0.84), (1, 4, 0.80)], True, "bird", None),  # near, 2 beams
    ("rbw", [(0, 5, 0.90), (1, 6, 0.88)], True, "bird", None),  # hi
    ("wbn", [(0, 1, 0.85)], True, "bird", None),  # single-beam high: disagree
    ("wbn", [(0, 2, 0.50)], True, "bird", None),  # near, exactly near_lo
    ("eso", [(0, 3, 0.40)], True, "bird", None),  # low
    ("hw", [(0, 4, 0.70)], True, "bird", None),  # near
    ("nc", [(0, 5, 0.95), (1, 6, 0.92)], True, "bird", None),  # hi
    ("rbw", [(0, 7, 0.60)], True, "bird", None),  # disagree: a filtered Golden-fronted report 2 s later
    ("gfw", [(-98, 7, 0.45)], False, "bird_filtered", None),  # that filtered report, 2 s after the previous event (own event: in_region False)
    ("human", [(0, 8, 0.80)], True, "bird", "label"),  # speech_label
    ("wbn", [(0, 1, 0.60)], True, "bird", "active"),  # speech_tag
    ("nc", [(0, 2, 0.60)], True, "bird", "none"),  # speech_unknown
    ("st", [(0, 3, 0.70)], None, "bird", None),  # region (pre-filter row)
    ("kd", [(0, 4, 0.90)], True, "bird", None),  # insect_excluded
    ("hw", [(0, 6, 0.55)], True, "bird", None),  # near
]
GAP_S = 100.0
SHA = "0123456789abcdef" * 4  # the model digest (analysis-records 1.3.0 `model.sha256`) on the rows of WITH_DIGEST species
WITH_DIGEST = ("rbw", "nc")  # the other species' rows have no `model`: their verdicts carry no `model_sha256`


def rows(extra_human=("Human vocal", "Human vocal")):
    birds, tags = [], []
    for i, (sp, reps, region, kind, speech) in enumerate(EVENTS):
        base = T0 + GAP_S * i
        common, sci = extra_human if sp == "human" else SPEC[sp]
        for dt, beam, conf in reps:
            r = {
                "k": kind,
                "t": base + dt,
                "beam": beam,
                "level_dbfs": -60.0,
                "common": common,
                "scientific": sci,
                "confidence": conf,
                "provenance": "model_opinion",
            }
            if region is not None:
                r["in_region"] = region
            if sp in WITH_DIGEST:
                r["model"] = {"name": "BirdNET", "version": "0.8.1", "variant": "geo_filter", "sha256": SHA}
            birds.append(r)
        if speech != "none":
            tags.append({"k": "tags", "t": base + 0.5, "beam": 1, "speech_p": 0.02, "speech_active": False, "top": [["Bird", 0.5]], "emb_stored": True})
        if speech == "active":
            tags.append({"k": "tags", "t": base + 4.0, "beam": 2, "speech_p": 0.60, "speech_active": True, "top": [["Speech", 0.6]], "emb_stored": False})
    return birds, tags


def jl(path, recs):
    with open(path, "wb") as f:
        for r in recs:
            f.write((json.dumps(r, separators=(",", ":")) + "\n").encode("ascii"))


def run(args):
    p = subprocess.run([sys.executable, "-I", TOOL] + args, capture_output=True)
    if p.returncode != 0:
        raise SystemExit("bma_labels failed: %s %s" % (args[:4], p.stderr.decode()))
    return p.stdout


def build(out_dir):
    work = tempfile.mkdtemp(prefix="labfix_")
    try:
        data = os.path.join(work, "data")
        os.makedirs(os.path.join(data, "analysis"))
        birds, tags = rows()
        jl(os.path.join(data, "analysis", "birds.jsonl"), birds)
        jl(os.path.join(data, "analysis", "tags.jsonl"), tags)
        common = ["--data-dir", data, "--taxa", TAXA]
        qp = os.path.join(work, "review_queue.json")
        run(common + ["--now", str(NOW), "queue", "--limit", "8", "--audit-frac", "0.25", "--audit-per-species-cap", "2", "--out", qp])
        q = json.load(open(qp, encoding="utf-8"))
        unc = next(i for i in q["items"] if i["selection"] == "uncertainty")
        aud = next(i for i in q["items"] if i["selection"] == "audit")
        run(common + ["--now", "1792945100.5", "add", "--event-id", unc["event_id"], "--verdict", "correct", "--basis", "field", "--queue", qp])
        run(
            common
            + [
                "--now",
                "1792945200.5",
                "add",
                "--event-id",
                aud["event_id"],
                "--verdict",
                "wrong",
                "--wrong-reason",
                "other_species",
                "--corrected-key",
                "melanerpes aurifrons",
                "--basis",
                "metadata_only",
                "--queue",
                qp,
            ]
        )
        run(common + ["--now", "1792945300.5", "add", "--event-id", unc["event_id"], "--verdict", "unsure", "--basis", "field"])  # a retraction
        os.makedirs(out_dir, exist_ok=True)
        shutil.copyfile(qp, os.path.join(out_dir, "review_queue.json"))
        shutil.copyfile(os.path.join(data, "labels", "labels.jsonl"), os.path.join(out_dir, "verdict_labels.jsonl"))
        return out_dir
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    print("wrote", build(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "fixtures")))
