#!/usr/bin/env python3
"""Per-window plugin time benchmark with SYNTHETIC 8 x 48 kHz noise plus a 978 Hz tone on beam 4 (no files, no models).
Reports median / p95 / max milliseconds per 1 s window for the inline plugins (dsp, spatial) and the amortised total.
    python -I bench_plugins.py [--plugins path/to/plugins.py] [--windows 120]
--plugins lets the same script time an older plugins.py (e.g. `git show HEAD:...analysis/plugins.py > old_plugins.py`) for a
before/after comparison. dsp runs INLINE on the host's 0.25 s loop: median increase < 25 % and dsp p95 <= 60 ms is the budget."""

import argparse, importlib.util, os, sys, time
import numpy as np

FS = 48000


def load(path, name="plugins_bench"):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plugins", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins.py"))
    ap.add_argument("--windows", type=int, default=120)
    ap.add_argument(
        "--coherence",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "coherence.py"),
        help="coherence.py to time (skipped when the file is absent)",
    )
    ap.add_argument(
        "--mixscore",
        action="store_true",
        help="also time the mixscore plugin: the mean mix alone, and with the REAL BirdNET "
        "when birdnetlib is importable (second instance: CPU ms and RSS growth); one 3 s window per call, hop 3 s",
    )
    ap.add_argument(
        "--classify-dir",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "classify"),
        help="directory holding bma_classify.py (for --mixscore)",
    )
    ap.add_argument(
        "--bursts", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "bursts.py"), help="bursts.py to time (skipped when the file is absent)"
    )
    ap.add_argument(
        "--indices", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "indices.py"), help="indices.py to time (skipped when the file is absent)"
    )
    ap.add_argument("--indices-windows", type=int, default=24, help="5 s windows to time for the indices plugin (default 24)")
    a = ap.parse_args()
    P = load(a.plugins)
    rng = np.random.default_rng(1)
    tone = (np.sin(2 * np.pi * 978.0 * np.arange(FS) / FS) * 10 ** (-45 / 20)).astype(np.float32)
    wins = []
    for _ in range(a.windows):
        x = (rng.standard_normal((FS, 8)) * 10 ** (-60 / 20)).astype(np.float32)
        x[:, 3] += tone
        wins.append(x)
    res = {}
    for name, mk in (("dsp", P.DSPMonitor), ("spatial", P.Spatial)):
        p = mk()
        ts = []
        for i, w in enumerate(wins):
            t0 = time.perf_counter()
            p.process(w, float(i + 1))
            ts.append((time.perf_counter() - t0) * 1000)
        res[name] = ts
        v = np.array(ts)
        print(f"{name:8s} median {np.median(v):6.1f} ms  p95 {np.percentile(v, 95):6.1f} ms  max {v.max():6.1f} ms  n={len(v)}")
    tot = np.array(res["dsp"]) + np.array(res["spatial"])
    print(f"dsp+spatial per window: mean {tot.mean():.1f} ms  median {np.median(tot):.1f} ms  p95 {np.percentile(tot, 95):.1f} ms")
    if os.path.exists(a.coherence):  # runs every hop_s (2 s), so its cost per second of audio is the per-window cost / hop_s
        sys.path.insert(0, os.path.dirname(os.path.abspath(a.coherence)))
        Cm = load(a.coherence, "coherence_bench")
        p = Cm.Coherence()
        ts = []
        for i, w in enumerate(wins):
            t0 = time.perf_counter()
            p.process(w, i * p.hop_s)
            ts.append((time.perf_counter() - t0) * 1000)
        v = np.array(ts[5:])
        print(
            f"coherence median {np.median(v):6.1f} ms  p95 {np.percentile(v, 95):6.1f} ms  max {v.max():6.1f} ms  n={len(v)}  "
            f"(hop {p.hop_s:g} s: {v.mean() / p.hop_s:.1f} ms mean per second of audio; errors {p.stats()['errors']})"
        )
        print(f"dsp+spatial+coherence per second of audio: mean {tot.mean() + v.mean() / p.hop_s:.1f} ms  (dsp+spatial alone {tot.mean():.1f} ms)")
        coh_ms = v.mean() / p.hop_s
    else:
        coh_ms = 0.0
    if os.path.exists(a.bursts):  # 1 s windows, hop 1 s: ms per window = ms per second of audio. Windows: the same noise, with a 1.4 /s click on all beams
        sys.path.insert(0, os.path.dirname(os.path.abspath(a.bursts)))
        Bm = load(a.bursts, "bursts_bench")
        p = Bm.Bursts(60.0, tagger=False)
        ts = []
        for i, w in enumerate(wins):
            w = w.copy()
            w[int(((i * 0.7) % 1.0) * FS), :] += 0.3
            t0 = time.perf_counter()
            p.process(w, 1000.0 + i)
            ts.append((time.perf_counter() - t0) * 1000)
        v = np.array(ts[2:])
        print(
            f"bursts   median {np.median(v):6.1f} ms  p95 {np.percentile(v, 95):6.1f} ms  max {v.max():6.1f} ms  n={len(v)}  "
            f"(hop {p.hop_s:g} s: {v.mean() / p.hop_s:.1f} ms mean per second of audio; errors {p.stats()['errors']}; impulses {p.stats()['impulses']})"
        )
        print(
            f"dsp+spatial+coherence+bursts per second of audio: mean {tot.mean() + coh_ms + v.mean() / p.hop_s:.1f} ms  (budget for the whole plugin set: about 30 ms)"
        )
        base_ms = tot.mean() + v.mean() / p.hop_s
    else:
        base_ms = tot.mean()
    if os.path.exists(a.indices):  # 5 s windows every hop_s (30 s by default) in the pool: cost per second of audio = per-window cost / hop_s
        sys.path.insert(0, os.path.dirname(os.path.abspath(a.indices)))
        Im = load(a.indices, "indices_bench")
        p = Im.Indices(interval_s=300.0, speech_gate=False)
        iw = []
        for _ in range(a.indices_windows):
            x = (rng.standard_normal((5 * FS, 8)) * 10 ** (-60 / 20)).astype(np.float32)
            x[:, 3] += np.tile(tone, 5)
            iw.append(x)
        its = []
        for i, w in enumerate(iw):
            t0 = time.perf_counter()
            p.process(w, 5.0 + p.hop_s * i)
            its.append((time.perf_counter() - t0) * 1000)
        v = np.array(its[1:])
        print(
            f"indices   median {np.median(v):6.1f} ms  p95 {np.percentile(v, 95):6.1f} ms  max {v.max():6.1f} ms  n={len(v)}  "
            f"(5 s window every {p.hop_s:g} s: {v.mean() / p.hop_s:.2f} ms mean per second of audio; errors {p.stats()['errors']})"
        )
        print(
            f"dsp+spatial+coherence+indices per second of audio: mean {base_ms + v.mean() / p.hop_s:.1f} ms  (indices alone adds {v.mean() / p.hop_s:.2f} ms; the plugin-set budget is about 30 ms of CPU per second of audio)"
        )
    bench_pcen()
    if a.mixscore:
        bench_mixscore(a, rng)


def bench_pcen(frames=240, rows=96):
    """pcen.py is a library (no plugin yet): time it on one 12 s clip worth of 50 ms columns (96 rows) and report per frame and as
    ms per second of audio (20 frames/s). Skipped when pcen.py is absent."""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pcen.py")
    if not os.path.exists(p):
        return
    PC = load(p, "pcen_bench")
    x = np.random.default_rng(5).gamma(1.0, 1e-8, size=(frames, rows))
    best = 1e9
    for _ in range(5):
        t0 = time.perf_counter()
        PC.pcen(x, scale=PC.INT16_POWER)
        best = min(best, time.perf_counter() - t0)
    print(
        f"pcen     {best / frames * 1e6:6.1f} us per {rows}-row frame; {best / frames * 20 * 1000:.3f} ms per second of audio at 20 frames/s (library, not in the window loop)"
    )


def rss_mb():
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return float("nan")


def bench_mixscore(a, rng):
    """CPU (process_time, all threads) and wall milliseconds per 3 s window of the mixscore plugin: the mean mix alone, then the whole
    process() with the real BirdNET as a second instance. Amortised = per second of audio (hop 3 s)."""
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    import mixscore as MS

    n = 3 * FS
    wins = [
        np.repeat((rng.standard_normal((n, 1)) * 10 ** (-45 / 20)).astype(np.float32), 8, axis=1)
        + (rng.standard_normal((n, 8)) * 10 ** (-60 / 20)).astype(np.float32)
        for _ in range(12)
    ]
    cpu, wall = [], []
    for w in wins:
        c0, w0 = time.process_time(), time.perf_counter()
        MS.mean_mix(w)
        cpu.append((time.process_time() - c0) * 1000)
        wall.append((time.perf_counter() - w0) * 1000)
    print(f"mixscore mean mix only: CPU {np.mean(cpu):.2f} ms, wall {np.mean(wall):.2f} ms per 3 s window -> {np.mean(cpu) / 3:.2f} ms CPU per second of audio")
    try:
        sys.path.insert(0, os.path.abspath(a.classify_dir))
        r0 = rss_mb()
        p = MS.MixScore(os.path.abspath(a.classify_dir), min_conf=0.25)
        p.process(wins[0], 1800000003.0)  # first call loads the model: excluded from the timing
        r1 = rss_mb()
    except Exception as e:
        print(f"mixscore with BirdNET: skipped ({type(e).__name__}: {e})")
        return
    cpu, wall = [], []
    for i, w in enumerate(wins[1:] * 3):
        c0, w0 = time.process_time(), time.perf_counter()
        p.process(w, 1800000006.0 + 3 * i)
        cpu.append((time.process_time() - c0) * 1000)
        wall.append((time.perf_counter() - w0) * 1000)
    print(
        f"mixscore process() with the real BirdNET (second instance): CPU median {np.median(cpu):.1f} ms mean {np.mean(cpu):.1f} ms, wall median "
        f"{np.median(wall):.1f} ms mean {np.mean(wall):.1f} ms per 3 s window (n={len(cpu)}) -> amortised CPU {np.mean(cpu) / 3:.1f} ms per second of audio; "
        f"RSS {r0:.0f} -> {r1:.0f} MB after loading the second instance"
    )


if __name__ == "__main__":
    main()
