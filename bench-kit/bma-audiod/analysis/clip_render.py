"""Spectrogram PNG of one clip, VISUAL ONLY and NOT INVERTIBLE TO SPEECH (owner 2026-10-26: "I cannot keep voice recordings").
numpy + scipy + PIL only. Exact format:
  * frequency: 96 rows, log-spaced from FLOOR_HZ = 3500 Hz (bottom) to 24 kHz (top); NOTHING below 3.5 kHz is represented (speech
    formants and the fundamental live below 3.4 kHz). Each row is the MAX over the 20 Hz FFT bins of its band, so a row is one number,
    not a fine spectrum (no harmonic comb).
  * time: one column per 50 ms, frames NON-overlapping (2400 samples, Hann), so the image has (len - 4096)/2400 columns (238 for a 12 s
    clip; the first and last 43 ms are the filter run-in and are not shown); a syllable (~100-200 ms) is 2-4 columns, pitch pulses and any waveform detail are gone.
  * level: 8-bit, pixel = (level - DB_LO) / (DB_HI - DB_LO) * 255 clipped, level = dB re full scale of a sinusoid of that amplitude in
    the bin (Hann, amplitude scaling), -110 dB -> 0, -30 dB or louder -> 255. Magnitude only: no phase is stored.
  --clip-pcen: the same grid (96 rows, 50 ms columns, 8-bit, floor 3.5 kHz) with PCEN-normalised values instead of dB: the row maxima as linear power
  (amp**2 * 32768**2) go through pcen.render_u8 (PCEN_PARAMS: time constant 0.4 s, alpha .98, delta 2, r .5, eps 1e-6, init first, vmax 4).
  Same 96 x columns x 8 bits as the log render; nothing finer or lower is added.
  The input is clip_features.restrict(pcm) (zero-phase high-pass, >= 100 dB down below 3.5 kHz), so even leakage cannot carry the
  voice band into the rows. PNG palette mode P, so the image is a lookup of that one 8-bit channel."""
import numpy as np

import clip_features as CF

FS = 48000
H = 96
F_LO, F_HI = CF.FLOOR_HZ, 24000.0
DB_LO, DB_HI = -110.0, -30.0
COL_S = 0.050
NFFT = int(COL_S * FS)            # 2400 samples = 50 ms, hop = NFFT (no overlap)


PCEN_TIME_CONSTANT_S = 0.4
PCEN_VMAX = 4.0


def pcen_params():
    """The PCEN parameters of the render, as recorded in the sidecar (exact floats)."""
    import pcen
    d = pcen.DEFAULTS
    return {"time_constant_s": PCEN_TIME_CONSTANT_S, "s": pcen.smoothing_from_time_constant(PCEN_TIME_CONSTANT_S, 1.0 / COL_S), "alpha": d["alpha"],
            "delta": d["delta"], "r": d["r"], "eps": d["eps"], "power_scale": pcen.INT16_POWER, "init": "first", "vmax": PCEN_VMAX}


def palette():
    """256 x RGB: black -> blue -> magenta -> orange -> yellow (piecewise linear, fixed)."""
    stops = [(0.0, (0, 0, 0)), (0.25, (30, 20, 120)), (0.5, (170, 30, 120)), (0.75, (240, 120, 30)), (1.0, (255, 245, 120))]
    xs = np.linspace(0, 1, 256)
    out = np.zeros((256, 3), np.uint8)
    for c in range(3):
        out[:, c] = np.interp(xs, [s[0] for s in stops], [s[1][c] for s in stops]).round()
    return out


def row_bins(fs=FS):
    """(lo, hi) FFT bin index ranges of the H rows, row 0 = lowest frequency; lo[0] is the first bin at or above FLOOR_HZ."""
    f = np.fft.rfftfreq(NFFT, 1.0 / fs)
    edges = np.geomspace(F_LO, F_HI, H + 1)
    lo = np.searchsorted(f, edges[:-1], side="left")
    hi = np.maximum(np.searchsorted(f, edges[1:], side="left"), lo + 1)
    hi = np.minimum(hi, len(f))
    lo = np.minimum(lo, hi - 1)
    return lo, hi


def spectrogram_index(pcm, fs=FS, pcen=False):
    """(H, W) uint8 index image, W = (len - 4096) // 2400, row 0 = highest frequency. pcen=True: PCEN scale (see the module docstring)."""
    x = CF.restrict(pcm, fs)                 # the settled part of the high-passed clip: 4096 samples shorter
    k = len(x) // NFFT
    if k == 0:
        return np.zeros((H, 1), np.uint8)
    w = np.hanning(NFFT)
    X = np.fft.rfft(x[:k * NFFT].reshape(k, NFFT) * w, axis=1)
    amp = 2.0 * np.abs(X) / np.sum(w)                                  # (k, bins)
    if pcen:
        import pcen as PC
        lo, hi = row_bins(fs)
        power = np.stack([(amp[:, a:b] ** 2).max(axis=1) for a, b in zip(lo, hi)], axis=1)      # (k, H), column 0 = lowest frequency
        p = pcen_params()
        return PC.render_u8(power, COL_S, vmax=p["vmax"], scale=p["power_scale"], s=p["s"], alpha=p["alpha"], delta=p["delta"], r=p["r"], eps=p["eps"], init=p["init"])
    lvl = 20.0 * np.log10(np.maximum(amp, 1e-9))
    lo, hi = row_bins(fs)
    rows = np.stack([lvl[:, a:b].max(axis=1) for a, b in zip(lo, hi)])        # (H, k), row 0 = lowest frequency
    idx = np.clip((rows - DB_LO) / (DB_HI - DB_LO) * 255.0, 0, 255).round().astype(np.uint8)
    return idx[::-1]


def write_png(path, pcm, fs=FS, pcen=False):
    from PIL import Image
    idx = spectrogram_index(pcm, fs, pcen)
    im = Image.fromarray(idx, "P")
    im.putpalette(palette().flatten().tolist())
    im.save(path, format="PNG", optimize=True)
