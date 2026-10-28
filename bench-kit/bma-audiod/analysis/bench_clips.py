#!/usr/bin/env python3
"""Cost of the wildlife clip capture (SYNTHETIC noise + tone, no models, temp dir). Like bench_plugins.py:
    python -I bench_clips.py [--n 30]
Reports (1) what the analyzer's own threads pay per call: note_window, on_plugin for a birds window with no record, with one qualifying
report (audio staging) and tick; (2) the writer thread's cost PER STORED CLIP: signatures + envelope, spectrogram PNG, FLAC write, total;
for a 5 s and a 12 s clip. Budget: the window loop adds well under 1 ms per 0.25 s iteration and under 15 ms per birds window."""

import argparse, os, shutil, sys, tempfile, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import clips_sim as S  # noqa: E402

FS = 48000


def stats(v):
    v = np.array(v) * 1000.0
    return f"median {np.median(v):8.3f} ms  p95 {np.percentile(v, 95):8.3f} ms  max {v.max():8.3f} ms  n={len(v)}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30)
    a = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="bench_clips_")
    try:
        sim = S.Sim(os.path.join(tmp, "clips"))
        rng = np.random.default_rng(1)
        sim.advance(40.0)
        x = (rng.standard_normal((3 * FS, 8)) * 10 ** (-60 / 20)).astype(np.float32)
        x[:, 3] += (np.sin(2 * np.pi * 1000.0 * np.arange(3 * FS) / FS) * 10 ** (-40 / 20)).astype(np.float32)
        svc = sim.svc
        t_note, t_empty, t_cand, t_tick = [], [], [], []
        for i in range(a.n):
            tw = S.T0 + 100.0 + 3.0 * i
            t = time.perf_counter()
            svc.note_window("birds", tw, S.BASE + int(3.0 * FS * i), 3 * FS)
            t_note.append(time.perf_counter() - t)
            t = time.perf_counter()
            svc.on_plugin("birds", [], x, tw)
            t_empty.append(time.perf_counter() - t)
        for i in range(a.n):
            tw = S.T0 + 300.0 + 3.0 * i
            svc.note_window("birds", tw, S.BASE + int(3.0 * FS * (200 + i)), 3 * FS)
            t = time.perf_counter()
            svc.on_plugin("birds", [S.row("Poecile carolinensis", 0.9, beam=4, t=tw)], x, tw)
            t_cand.append(time.perf_counter() - t)
        for _ in range(a.n * 10):
            t = time.perf_counter()
            svc.tick(S.T0 + 1.0)
            t_tick.append(time.perf_counter() - t)
        print("analyzer-side (per call):")
        print("  note_window                      ", stats(t_note))
        print("  on_plugin birds, no record       ", stats(t_empty))
        print("  on_plugin birds, 1 qualifying    ", stats(t_cand))
        print("  tick (every 0.25 s loop pass)    ", stats(t_tick))
        res = {}
        for secs in (5, 12):
            n = secs * FS
            pcm = (np.sin(2 * np.pi * 3000.0 * np.arange(n) / FS) * 3000 + rng.standard_normal(n) * 30).astype(np.int16)
            tf, tp, tw_ = [], [], []
            import clip_features, clip_render
            import soundfile as sf

            for i in range(max(5, a.n // 3)):
                t = time.perf_counter()
                clip_features.signatures(pcm, FS, [0.0] * 8, None)
                clip_features.envelope(pcm, FS)
                tf.append(time.perf_counter() - t)
                t = time.perf_counter()
                clip_render.write_png(os.path.join(tmp, "b.png"), pcm)
                tp.append(time.perf_counter() - t)
                t = time.perf_counter()
                sf.write(os.path.join(tmp, "b.flac"), pcm, FS, subtype="PCM_16", format="FLAC")
                tw_.append(time.perf_counter() - t)
            res[secs] = (np.median(tf) + np.median(tp) + np.median(tw_), np.median(tf), np.median(tp), np.median(tw_))
        print("writer thread, CPU per stored clip (median):")
        for secs, (tot, f, p, w) in res.items():
            print(f"  {secs:2d} s clip: total {tot * 1000:7.1f} ms = signatures+envelope {f * 1000:6.1f} + PNG {p * 1000:6.1f} + FLAC {w * 1000:6.1f}")
        print(f"  png bytes {os.path.getsize(os.path.join(tmp, 'b.png'))}, flac bytes {os.path.getsize(os.path.join(tmp, 'b.flac'))} (12 s, tone + low noise)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
