#!/usr/bin/env python3
"""Regenerate contracts/fixtures/perch.jsonl by running the REAL analysis/perch.py Perch plugin (and the real hist.RotatingJsonl writer
the host uses) on a REAL tiny deterministic ONNX model (analysis/perch_tiny_model.py) through onnxruntime. Needs numpy, scipy, onnx, onnxruntime:
    python -I make_perch_fixtures.py [out_dir]      (default: ../fixtures)
Timeline (all times in the minute starting at BASE_T = 1792900020, a multiple of 60 s; the tagger covers every window):
  +5 s   1 kHz tone -> Poecile carolinensis;       BirdNET names Poecile carolinensis            -> both_same_species
  +10 s  2 kHz tone -> Poecile atricapillus;       BirdNET names Poecile carolinensis            -> both_same_genus
  +15 s  5 kHz tone -> FSD class 'Insect';         BirdNET names Gryllus pennsylvanicus (insect) -> birds_only, insect perch 1 / birds 1 / both 1
  +20 s  3 kHz tone, tagger flags speech           -> no record
  +25 s  6 kHz tone -> label 'Speech' on top       -> no record (human-voice label)
  +30 s  8 kHz tone -> FSD class 'Cricket';        BirdNET silent                                -> both_silent
  +35 s  3 kHz tone -> Cyanocitta cristata;        no BirdNET window                             -> birds_missing
  +65 s  1 kHz tone in the NEXT minute, flushed at shutdown                                      -> a `partial` agreement record
analysis/test_perch.py regenerates it in a temp dir and requires the same keys, integers, strings (except `mv`: the model file's hash
depends on the onnx version) and nulls and floats within 0.01."""

import json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "analysis")))
import numpy as np  # noqa: E402
import hist  # noqa: E402
import perch as PC  # noqa: E402
import perch_tiny_model as TM  # noqa: E402
import taxa as T  # noqa: E402

BASE_T = 1792900020.0
N, FS = 240000, 48000
RUN = 1792899000


def window(hz, amp=0.3, beam=3, seed=0):
    rng = np.random.default_rng(seed)
    x = (rng.standard_normal((N, 8)) * 1e-4).astype(np.float32)  # -80 dBFS noise on every beam
    if hz:
        x[:, beam] += (amp * np.sin(2 * np.pi * hz * np.arange(N) / FS)).astype(np.float32)
    return x


def bird(sci, conf, grp):
    return {"k": "bird", "scientific": sci, "confidence": conf, "group": grp, "non_species": False}


def build(out_dir):
    work = tempfile.mkdtemp(prefix="pfix_")
    mp, lp = TM.build_dir(os.path.join(work, "model"))
    g = PC.SpeechGuard()
    p = PC.Perch(mp, lp, g, taxa=T.Taxa(os.path.join(HERE, "..", "..", "ui", "taxa.json")), min_logit=1.0, run=RUN)
    plan = [
        (5, 1000, False, bird("Poecile carolinensis", 0.91, "bird")),
        (10, 2000, False, bird("Poecile carolinensis", 0.88, "bird")),
        (15, 5000, False, bird("Gryllus pennsylvanicus", 0.71, "insect")),
        (20, 3000, True, None),
        (25, 6000, False, None),
        (30, 8000, False, "silent"),
        (35, 3000, False, "absent"),
    ]
    recs = []
    for off, hz, speech, b in plan:
        t1 = BASE_T + off
        g.note(t1 - 5.0, t1, speech)
        recs += p.process(window(hz, seed=off), t1)[0]
        if b != "absent":
            p.feed_birds(t1, [] if b in (None, "silent") else [b], {"beams_classified": 0 if b is None else 2})
    recs += p.process(window(None, amp=0.0, seed=1) * 1e-3, BASE_T + 80.0)[0]  # gated: only closes the minute
    g.note(BASE_T + 60.0, BASE_T + 65.0, False)
    recs += p.process(window(1000, seed=65), BASE_T + 65.0)[0]
    recs += p.flush()
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "perch.jsonl")
    if os.path.exists(path):
        os.remove(path)
    w = hist.RotatingJsonl(path, 64 << 20)
    for r in recs:
        assert w.write(r)
    w._close()
    return out_dir


if __name__ == "__main__":
    out = build(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "fixtures"))
    print("wrote", os.path.join(out, "perch.jsonl"))
