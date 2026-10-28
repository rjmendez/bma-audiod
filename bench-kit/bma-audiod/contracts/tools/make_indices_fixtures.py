#!/usr/bin/env python3
"""Regenerate contracts/fixtures/acoustic_indices.jsonl: runs the REAL analysis/indices.py Indices plugin on fixed, seeded
synthetic windows and writes its records through the production writer (hist.RotatingJsonl); needs numpy and scipy:
    python -I make_indices_fixtures.py [out_dir]      (default: ../fixtures)
Timeline (interval 300 s, window 5 s every 15 s, speech gate on, a tagger window every 2 s):
  interval 0  20 quiet windows (-60 dBFS noise + a 1.5 kHz tone), every window tagged, none speech: a plain record;
  interval 1  louder (-50 dBFS) windows: 2 loud (-20 dBFS) windows overlap tagger windows flagged as speech (excluded, counted, their values
              absent), 1 silent window and 1 window with a NaN sample (both counted), 4 windows no tagger window covers (win_untagged 4: excluded, fail closed);
  interval 2  only 3 windows (late in the interval), closed normally 30 s after its end by the first window of interval 3;
  interval 3  one window, closed by flush() at shutdown: a partial record.
analysis/test_indices.py regenerates it in a temp dir and requires the same keys, integers and nulls and floats within 0.01."""

import os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "analysis")))
import numpy as np  # noqa: E402
import hist  # noqa: E402
import indices as I  # noqa: E402

BASE_T = 1792900200.0  # a multiple of 300 s
FS, N = 48000, 240000
NAME = "acoustic_indices.jsonl"


def win(rng, level_db, tone=True):
    x = rng.standard_normal((N, 1)) * 0.9 + rng.standard_normal((N, 8)) * 0.1
    if tone:
        x = x + 0.5 * np.sin(2 * np.pi * 1500.0 * np.arange(N) / FS)[:, None]
    x = x / np.sqrt((x**2).mean()) * 10 ** (level_db / 20.0)
    return x.astype(np.float32)


def build(out_dir):
    rng = np.random.default_rng(2027)
    p = I.Indices(300.0, 15.0, 5.0, speech_gate=True, grace_s=30.0)
    recs = []

    def tag(t_end, speech=False):
        p.note_tag(t_end, speech, 5.0)

    def feed(x, t):
        r, _ = p.process(x, t)
        recs.extend(r)

    for k in range(20):  # interval 0
        t = BASE_T + 5.0 + 15.0 * k
        tag(t)
        feed(win(rng, -60.0), t)
    for k in range(20):  # interval 1
        t = BASE_T + 300.0 + 5.0 + 15.0 * k
        x = win(rng, -50.0)
        if k in (3, 11):
            x = win(rng, -20.0)
            tag(t - 1.0, speech=True)  # overlaps this window only
        elif k == 5:
            x = np.zeros((N, 8), np.float32)
            tag(t)
        elif k == 7:
            x[100, 2] = np.nan
            tag(t)
        elif k in (13, 14, 15, 16):
            pass  # no tagger window near: untagged, excluded
        else:
            tag(t)
        feed(x, t)
    for k in range(3):  # interval 2, short
        t = BASE_T + 600.0 + 250.0 + 15.0 * k
        tag(t)
        feed(win(rng, -55.0), t)
    t = BASE_T + 900.0 + 40.0  # interval 3 (t >= end of interval 2 + 30 s: closes interval 2)
    tag(t)
    feed(win(rng, -58.0), t)
    recs.extend(p.flush())
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, NAME)
    if os.path.exists(path):
        os.remove(path)
    w = hist.RotatingJsonl(path, 64 << 20)
    for r in recs:
        w.write(r)
    w.close()
    return out_dir


if __name__ == "__main__":
    out = build(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "fixtures"))
    print("wrote", os.path.join(out, NAME))
