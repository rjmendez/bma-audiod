#!/usr/bin/env python3
"""Regenerate contracts/fixtures/event_model_report.json by running the REAL tools/bma_labels.py events on a constructed scenario
(CONSTRUCTED data, not a capture):

    python -I make_event_fixtures.py [out_dir]      (default: ../fixtures)"""

import json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BMA = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(BMA, "analysis"))
import labels as L  # noqa: E402

T0 = 1792940400.0
RBW = ("Red-bellied Woodpecker", "Melanerpes carolinus")
WREN = ("Wren", "Troglodytes aedon")
TAXA = os.path.join(BMA, "ui", "taxa.json")


def row(t, beam, sp=RBW, k="bird", conf=0.7):
    return {"k": k, "t": t, "beam": beam, "level_dbfs": -60.0, "common": sp[0], "scientific": sp[1], "confidence": conf, "provenance": "model_opinion"}


def scenario():
    """-> (birds rows now, verdict records).  Verdicts: one on an unchanged event, one on an event that grew (late earlier report), one on an event that is gone."""
    old = [row(T0 + 10, 1), row(T0 + 12, 2), row(T0 + 100, 1, WREN), row(T0 + 300, 1, ("Katydid", "Katydid"))]
    now = [
        row(T0 + 8, 3),
        row(T0 + 10, 1),
        row(T0 + 12, 2),
        row(T0 + 100, 1, WREN),
        row(T0 + 100, 1, WREN),
        row(T0 + 50, 1, k="bird_filtered"),
        row(T0 + 60, 1, ("Human vocal", "Human vocal")),
        row(T0 + 70, 9),
        {"k": "dsp", "t": T0 + 80},
    ]
    evs = L.iter_events(old, L.Taxa(TAXA))
    recs = [L.make_record(e, "correct", "field", t_label=T0 + 1000 + i, label_id="%016x" % (i + 1)) for i, e in enumerate(evs)]
    return now, recs


def write_jsonl(path, rows):
    with open(path, "wb") as f:
        f.write(b"".join((json.dumps(r, separators=(",", ":")) + "\n").encode("utf-8") for r in rows))


def build(out_dir):
    work = tempfile.mkdtemp(prefix="evfix_")
    try:
        now, recs = scenario()
        os.makedirs(os.path.join(work, "analysis"))
        os.makedirs(os.path.join(work, "labels"))
        write_jsonl(os.path.join(work, "analysis", "birds.jsonl"), now)
        write_jsonl(os.path.join(work, "labels", "labels.jsonl"), recs)
        p = subprocess.run(
            [sys.executable, "-I", os.path.join(BMA, "tools", "bma_labels.py"), "--data-dir", work, "--taxa", TAXA, "events"], capture_output=True
        )
        if p.returncode != 0:
            raise SystemExit("bma_labels.py events failed: %r" % p.stderr)
        os.makedirs(out_dir, exist_ok=True)
        dst = os.path.join(out_dir, "event_model_report.json")
        with open(dst, "wb") as f:
            f.write(p.stdout)
        return dst
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    print(build(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "fixtures")))
