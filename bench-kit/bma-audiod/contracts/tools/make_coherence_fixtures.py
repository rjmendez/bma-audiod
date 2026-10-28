#!/usr/bin/env python3
"""Regenerate contracts/fixtures/beam_coherence.jsonl by running the REAL analysis/coherence.py Coherence plugin on fixed,
seeded synthetic windows (needs numpy and scipy):
    python -I make_coherence_fixtures.py [out_dir]      (default: ../fixtures)
Timeline (hop 2 s, interval 60 s, 5 records): minutes 0-1 are the 60-window warmup (all cells null); minute 2 is quiet with one
window whose beam 5 is muted (gated) and a common 12004.7 Hz tone against a mask at 12000 Hz; minute 3 is 12 dB louder
(active); minute 4 has 3 quiet windows (a thin, null cell with n_quiet 3) and then active ones. analysis/test_coherence.py
regenerates it in a temp dir and requires the same keys, integers and nulls and floats within 0.002 (FFT last-bit differences)."""

import json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "analysis")))
import numpy as np  # noqa: E402
import coherence as C  # noqa: E402

BASE_T = 1792900020.0  # a multiple of 60 s
N, FS = 48000, 48000


def window(rng, total_db, r=3.0, tone=True, mute=None):
    ps, pn = r / (1 + r), 1 / (1 + r)
    x = np.sqrt(ps) * rng.standard_normal((N, 1)) + np.sqrt(pn) * rng.standard_normal((N, 8))
    if tone:
        x = x + (0.002 / 10 ** (-60 / 20.0)) * np.sin(2 * np.pi * 12004.7 * np.arange(N) / FS)[:, None]
    x = (x * 10 ** (total_db / 20.0)).astype(np.float32)
    if mute is not None:
        x[:, mute] *= 1e-5
    return x


def build(out_dir):
    work = tempfile.mkdtemp(prefix="cfix_")
    mask = os.path.join(work, "mask.json")
    with open(mask, "w") as f:
        json.dump({"version": 1, "lines_hz": [11510.7, 12000.0, 15334.0], "tolerance_hz": 4.0}, f)
    rng = np.random.default_rng(2026)
    p = C.Coherence(mask, None, 60.0)
    recs = []
    for i in range(150):
        lvl = -66.0 if (i < 90 or 120 <= i < 123) else -54.0
        x = window(rng, lvl, mute=5 if i == 70 else None)
        r, _ = p.process(x, BASE_T + 2.0 * i)
        recs += r
    r, _ = p.process(np.zeros((N, 8), np.float32), BASE_T + 300.0)
    recs += r
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "beam_coherence.jsonl"), "w", encoding="utf-8", newline="\n") as f:
        for rec in recs:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    return out_dir


if __name__ == "__main__":
    out = build(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "fixtures"))
    print("wrote", os.path.join(out, "beam_coherence.jsonl"))
