#!/usr/bin/env python3
"""Regenerate contracts/fixtures/burst_stats.jsonl by running the REAL analysis/bursts.py Bursts plugin on fixed, seeded synthetic
windows (needs numpy and scipy):
    python -I make_burst_fixtures.py [out_dir]      (default: ../fixtures)
Timeline (1 s windows, interval 60 s, tagger on, 5 records; tagger verdicts of 5 s windows every 2 s are fed with note_speech):
  minute 0: noise only (-100 dBFS per beam), no impulse
  minute 1: a click on all 8 beams every 0.7 s (coincident, regular: mode 0.7, regularity 1.0)
  minute 2: Poisson clicks (mean gap 1.2 s) on ONE beam each (random beam), speech flagged for seconds 20-30 (excluded windows) and no tagger
            verdict for audio ending 44-58 s (windows left unvetted, excluded)
  minute 3: a 10 kHz katydid-like trill: 10 ms pulses every 50 ms on all beams (modulation fundamental 20 Hz)
  minute 4: starts 20 s late (a partial record: the first interval begun late) and holds clicks every 0.7 s on all beams
analysis/test_bursts.py regenerates it in a temp dir and requires the same keys, integers and nulls and floats within 0.01."""

import json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "analysis")))
import numpy as np  # noqa: E402
import bursts as B  # noqa: E402

BASE_T = 1792900020.0  # a multiple of 60 s
FS = 48000


def noise(rng):
    return (rng.standard_normal((FS, 8)) * 1e-5).astype(np.float32)


def clicks(x, times, beams=None, amp=0.01):
    for c in times:
        i = int(round(c * FS))
        if 0 <= i < FS - 2:
            x[i : i + 2, :] += amp if beams is None else 0
            if beams is not None:
                x[i : i + 2, beams] += amp
    return x


def trill(x, amp=0.003, f=10000.0):
    t = np.arange(FS) / FS
    pulse = ((np.floor(t / 0.05) * 0.05 + 0.01) > t) & (t >= np.floor(t / 0.05) * 0.05)  # 10 ms on, 40 ms off
    x += (amp * np.sin(2 * np.pi * f * t) * pulse).astype(np.float32)[:, None]
    return x


def build(out_dir):
    rng = np.random.default_rng(2026)
    p = B.Bursts(60.0, tagger=True)
    recs = []
    poisson = np.cumsum(rng.exponential(1.2, 100)) + 0.3  # click times (s) in minute 2
    poisson_beam = rng.integers(0, 8, 100)
    secs = list(range(0, 240)) + list(range(260, 300))  # minute 4 begins 20 s late
    for s in secs:
        m, sm = divmod(s, 60)
        x = noise(rng)
        if m == 1:
            x = clicks(x, [c - sm for c in np.arange(0.35, 60, 0.7) if sm <= c < sm + 1])
        elif m == 2:
            for c, b in zip(poisson, poisson_beam):
                if sm <= c < sm + 1:
                    x = clicks(x, [c - sm], beams=[int(b)])
        elif m == 3:
            x = trill(x)
        elif m == 4:
            x = clicks(x, [c - sm for c in np.arange(0.35, 60, 0.7) if sm <= c < sm + 1])
        t_end = BASE_T + s + 1.0
        if (s + 1) % 2 == 0:  # a tagger verdict for the 5 s window ending now
            te = s + 1
            a, b = te - 5, te
            active = a < 150 and b > 140  # speech at absolute seconds 140-150 (minute 2, seconds 20-30)
            if not (164 <= te <= 178):  # no verdict for audio ending 44-58 s into minute 2
                p.note_speech(BASE_T + a, BASE_T + b, bool(active))
        r, _ = p.process(x, t_end)
        recs += r
    recs += p.flush()
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "burst_stats.jsonl"), "w", encoding="utf-8", newline="\n") as f:
        for rec in recs:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    return out_dir


if __name__ == "__main__":
    out = build(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "fixtures"))
    print("wrote", os.path.join(out, "burst_stats.jsonl"))
