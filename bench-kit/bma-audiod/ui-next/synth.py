"""Synthetic signals for the ui-lab view/audition prototype and its tests. numpy only, deterministic (seeded). Never real audio.

All signals: float64, 48000 Hz, full scale = 1.0. `scene()` is a labelled mix; `beams()` makes the 8-beam array from one scene.
"""
import numpy as np

FS = 48000
UNIT_LINES_HZ = (11510.7, 12000.0, 15334.0, 15389.6, 15445.3, 15501.0, 15556.6)   # the unit's electronic lines (docs/FEATURES.md audit)


def t_axis(dur, fs=FS):
    return np.arange(int(round(dur * fs))) / fs


def tone(f, dur, amp=0.1, phase=0.0, fs=FS):
    return amp * np.sin(2 * np.pi * f * t_axis(dur, fs) + phase)


def chirp(f0, f1, dur, amp=0.1, fs=FS):
    """Linear chirp f0 -> f1 over dur seconds (instantaneous frequency exactly f0 + (f1-f0) t / dur)."""
    t = t_axis(dur, fs)
    return amp * np.sin(2 * np.pi * (f0 * t + 0.5 * (f1 - f0) / dur * t * t))


def harmonic_stack(f0, dur, n=8, amp=0.1, decay=0.7, fs=FS):
    t = t_axis(dur, fs)
    x = np.zeros_like(t)
    for k in range(1, n + 1):
        if k * f0 < fs / 2:
            x += (decay ** (k - 1)) * np.sin(2 * np.pi * k * f0 * t)
    return amp * x / max(1e-12, np.abs(x).max())


def am_tone(fc, fm, depth, dur, amp=0.1, fs=FS):
    t = t_axis(dur, fs)
    return amp * (1 + depth * np.sin(2 * np.pi * fm * t)) / (1 + depth) * np.sin(2 * np.pi * fc * t)


def fm_tone(fc, fm, dev, dur, amp=0.1, fs=FS):
    t = t_axis(dur, fs)
    return amp * np.sin(2 * np.pi * fc * t + (dev / fm) * np.sin(2 * np.pi * fm * t))


def clicks(period, dur, first=0.0, amp=0.5, width=24, fs=FS):
    """Short Hann-shaped clicks of `width` samples every `period` seconds, starting at `first` seconds."""
    x = np.zeros(int(round(dur * fs)))
    w = np.hanning(width) * amp
    t = first
    while int(t * fs) + width < len(x):
        i = int(round(t * fs))
        x[i:i + width] += w
        t += period
    return x


def noise(dur, level_dbfs=-60.0, seed=1, fs=FS):
    """White gaussian noise with the given RMS level in dBFS."""
    r = np.random.default_rng(seed)
    x = r.standard_normal(int(round(dur * fs)))
    return x * 10 ** (level_dbfs / 20.0) / x.std()


def unit_lines(dur, level_dbfs=-62.0, fs=FS):
    x = np.zeros(int(round(dur * fs)))
    for f in UNIT_LINES_HZ:
        x += tone(f, dur, 10 ** (level_dbfs / 20.0), fs=fs)
    return x


def burst(sig, t0, dur_total, fs=FS):
    """Place `sig` into a silent track of dur_total seconds at t0 seconds (cut at the end)."""
    out = np.zeros(int(round(dur_total * fs)))
    i = int(round(t0 * fs))
    n = max(0, min(len(sig), len(out) - i))
    out[i:i + n] = sig[:n]
    return out


def scene(dur=6.0, seed=7, fs=FS):
    """Labelled mix: noise floor -60 dBFS, unit lines, a bird-like downsweep every 1.5 s (6 kHz -> 3.5 kHz, 0.25 s), an insect
    (AM 6.5 kHz carrier, 45 Hz), a frog-like pulse train (1.6 kHz carrier gated at 35 Hz), a mechanical hum (100 Hz + harmonics).
    Returns (x, truth) with truth = list of dicts {kind, t0, t1, f0, f1} for the visual proof (no label is a model output)."""
    x = noise(dur, -60.0, seed, fs) + unit_lines(dur, -62.0, fs)
    truth = []
    t = 0.4
    while t + 0.25 < dur:
        x += burst(chirp(6000, 3500, 0.25, 0.08, fs) * np.hanning(int(0.25 * fs)), t, dur, fs)
        truth.append(dict(kind="bird", t0=t, t1=t + 0.25, f0=3500, f1=6000))
        t += 1.5
    x += burst(am_tone(6500, 45, 0.9, 1.2, 0.03, fs) * np.hanning(int(1.2 * fs)), 2.0, dur, fs)
    truth.append(dict(kind="insect", t0=2.0, t1=3.2, f0=6500, f1=6500))
    gate = (np.sin(2 * np.pi * 35 * t_axis(1.0, fs)) > 0).astype(float)
    x += burst(tone(1600, 1.0, 0.05, fs=fs) * gate, 3.6, dur, fs)
    truth.append(dict(kind="frog", t0=3.6, t1=4.6, f0=1600, f1=1600))
    x += harmonic_stack(100, dur, 10, 0.02, 0.8, fs)
    truth.append(dict(kind="mechanical", t0=0.0, t1=dur, f0=100, f1=1000))
    return np.clip(x, -1.0, 1.0), truth


def beams(dur=6.0, n_beams=8, seed=7, fs=FS):
    """(n_beams, N) array. A shared scene reaches every beam with a per-beam gain (a ring of 45 degree beams, source at beam 0 with a
    cos^2 pattern) and a per-beam integer-sample delay, plus INDEPENDENT noise per beam so inter-beam coherence is below 1 off-axis."""
    base, _ = scene(dur, seed, fs)
    base = base - noise(dur, -60.0, seed, fs)                  # the source content only
    out = []
    for b in range(n_beams):
        ang = 2 * np.pi * b / n_beams
        g = max(0.05, np.cos(ang / 2) ** 2)
        d = int(round(1.5 * np.sin(ang)))                       # |delay| <= 2 samples
        s = np.roll(base, d) * g
        out.append(s + noise(dur, -60.0, seed * 100 + b, fs))
    return np.clip(np.array(out), -1.0, 1.0)


def to_wav16(x, fs=FS):
    """x: (N,) or (C, N) float in [-1, 1] -> bytes of a PCM16 WAV (interleaved)."""
    import struct
    a = np.atleast_2d(x)
    c, n = a.shape
    pcm = np.clip(np.round(a.T * 32767.0), -32768, 32767).astype("<i2").tobytes()
    hdr = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, c, fs, fs * c * 2, c * 2, 16) + b"data" + struct.pack("<I", len(pcm))
    return hdr + pcm
