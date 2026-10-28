"""Cheap, well-defined signatures of ONE clip (numpy + scipy only). Called by clips.py in the writer thread, only for a clip that is
being stored; nothing here touches the analyzer's window loop.

PRIVACY (owner 2026-10-26: "I cannot keep voice recordings"): every number here is computed from `restrict(pcm)`, the clip with
EVERYTHING BELOW FLOOR_HZ (3500 Hz) REMOVED by a linear-phase FIR high-pass (4097 taps, Kaiser beta 12: at least 100 dB down below
3.5 kHz, flat above 3.6 kHz). Only the fully settled part of the filter output is used ("valid" convolution): the first and last 2048
samples (43 ms) of a clip are not analysed, so a hard clip edge cannot click through the filter; every time series starts OFFSET_S
(0.0427 s) after the clip start. Speech formants and the fundamental live below 3.4 kHz, so no stored value can carry them. What is left
is coarse on top of that: all time series run at 10-20 values per second (50 ms frames, never finer), there is no waveform and no
min/max pair at audio rate, and nothing stores phase. contracts/specs/clips.md gives the reasoning and the measured numbers.

Input is mono int16 at FS. Every number is documented in contracts/specs/clips.md.
  peak_dbfs / rms_dbfs  20*log10 of the sample peak / RMS of the high-passed signal re full scale (int16 full scale 32768), floor -200.
  snr_db                clip RMS over the RMS of the quietest 10 % of 50 ms frames (that RMS floored at -100 dBFS).
  dominant_hz           per 100 ms frame (non-overlapping) spectral peak above the floor (parabolic interpolation on log power): 10 values/s.
  centroid / bandwidth  mean and standard deviation (over those frames) of the power-weighted spectral centroid / bandwidth in Hz.
  band_db               the 3 bands 4-8, 8-16, 16-24 kHz: mean-square level in dB re full scale (a sine of amplitude A reads
                        20*log10(A/sqrt(2)), silence -200).
  onsets                clip times (s, multiples of 25 ms) of positive jumps >= 6 dB of the 50 ms RMS envelope (25 ms hop),
                        at least 100 ms apart; ioi = inter-onset interval statistics (n, mean_s, median_s, cv, rate_hz).
  envelope              RMS of the high-passed signal per 50 ms frame (20 values/s), dB re full scale, 1 decimal, floor -100.
"""
import math
import numpy as np
from scipy import signal

FS = 48000
FLOOR_HZ = 3500.0
BAND_EDGES_HZ = [4000, 8000, 16000, 24000]
ONSET_DB = 6.0
ONSET_MIN_GAP_S = 0.10
ENV_WIN_S, ENV_HOP_S = 0.050, 0.025
FRAME_S = 0.050                    # envelope / SNR frame (20 values per second)
DOM_FRAME_S = 0.100                # dominant-frequency frame (10 values per second)
HP_TAPS, HP_CUTOFF_HZ, HP_BETA = 4097, 3550.0, 12.0     # transition 3504..3596 Hz at fs 48 kHz
TRIM = HP_TAPS - 1                 # samples lost by the valid convolution (2048 at each end)
OFFSET_S = (TRIM // 2) / FS        # clip time of the first analysed sample
_hp = {}


def _db(v, floor=-200.0):
    return floor if v <= 0 else max(floor, 20.0 * math.log10(v))


def highpass_taps(fs=FS):
    if fs not in _hp:
        _hp[fs] = signal.firwin(HP_TAPS, HP_CUTOFF_HZ, window=("kaiser", HP_BETA), pass_zero=False, fs=fs)
    return _hp[fs]


def restrict(pcm, fs=FS):
    """float64 signal in [-1, 1] with everything below FLOOR_HZ removed (zero phase), len(pcm) - TRIM samples (the settled part only).
    THE choke point: no stored number is computed from anything else."""
    x = np.asarray(pcm, np.float64) / 32768.0
    h = highpass_taps(fs)
    if len(x) < len(h):
        return np.zeros(0)
    return signal.oaconvolve(x, h, mode="valid")


def n_frames(n_samples, frame_samples):
    """Number of whole non-overlapping frames of the analysed part of a clip of n_samples."""
    return max(0, (n_samples - TRIM) // frame_samples)


def band_db(x, fs=FS):
    """Mean-square level (dB re full scale) in each of the 3 bands above the floor, Hann window, Parseval scaled."""
    n = len(x)
    w = np.hanning(n)
    X = np.fft.rfft(x * w)
    p = 2.0 * np.abs(X) ** 2 / (n * np.sum(w * w))
    p[0] /= 2.0
    if n % 2 == 0:
        p[-1] /= 2.0
    f = np.fft.rfftfreq(n, 1.0 / fs)
    out = []
    for lo, hi in zip(BAND_EDGES_HZ[:-1], BAND_EDGES_HZ[1:]):
        s = float(p[(f >= lo) & (f < hi)].sum())
        out.append(round(10.0 * math.log10(s) if s > 1e-20 else -200.0, 1))
    return out


def envelope_rms_db(x, fs=FS):
    """RMS per non-overlapping 50 ms frame in dB re full scale (floor -100), 1 decimal: 20 values per second."""
    n = int(FRAME_S * fs)
    k = len(x) // n
    if k == 0:
        return []
    fr = x[:k * n].reshape(k, n)
    r = np.sqrt((fr * fr).mean(axis=1))
    return [round(_db(float(v), -100.0), 1) for v in r]


def _frames_rms(x, win, hop):
    if len(x) < win:
        return np.zeros(0)
    n = 1 + (len(x) - win) // hop
    c = np.concatenate([[0.0], np.cumsum(x * x)])
    idx = np.arange(n) * hop
    return np.sqrt(np.maximum(c[idx + win] - c[idx], 0.0) / win)


def onsets(x, fs=FS):
    win, hop = int(ENV_WIN_S * fs), int(ENV_HOP_S * fs)
    r = _frames_rms(x, win, hop)
    if len(r) < 3:
        return []
    env = 20.0 * np.log10(np.maximum(r, 1e-5))
    flux = np.maximum(np.diff(env), 0.0)
    pk, _ = signal.find_peaks(flux, height=ONSET_DB, distance=max(1, int(round(ONSET_MIN_GAP_S / ENV_HOP_S))))
    return [round(round(((i + 1) * hop / fs + OFFSET_S) / ENV_HOP_S) * ENV_HOP_S, 3) for i in pk]     # clip time, on the 25 ms grid


def ioi_stats(times):
    n = len(times)
    if n < 2:
        return {"n": n, "mean_s": None, "median_s": None, "cv": None, "rate_hz": None}
    d = np.diff(np.asarray(times))
    m = float(d.mean())
    return {"n": n, "mean_s": round(m, 4), "median_s": round(float(np.median(d)), 4),
            "cv": round(float(d.std() / m), 4) if m > 0 else None, "rate_hz": round(1.0 / m, 3) if m > 0 else None}


def spectral(x, fs=FS):
    """(dominant_hz track, centroid per frame, bandwidth per frame, step_s): Hann STFT of 100 ms non-overlapping frames, bins below
    the floor excluded."""
    nperseg = int(DOM_FRAME_S * fs)
    if len(x) < nperseg:
        x = np.concatenate([x, np.zeros(nperseg - len(x))])
    f, _, Z = signal.stft(x, fs, window="hann", nperseg=nperseg, noverlap=0, boundary=None, padded=False)
    keep = f >= FLOOR_HZ
    f, Z = f[keep], Z[keep]
    P = np.abs(Z) ** 2 + 1e-30
    df = f[1] - f[0]
    k = np.argmax(P, axis=0)
    dom = []
    for j, kk in enumerate(k):
        if 0 < kk < len(f) - 1:
            a, b, c = (math.log(P[kk - 1, j]), math.log(P[kk, j]), math.log(P[kk + 1, j]))
            den = a - 2 * b + c
            off = 0.5 * (a - c) / den if den != 0 else 0.0
        else:
            off = 0.0
        dom.append(float(f[kk] + off * df))
    dom = [round(v, 1) for v in dom]
    s = P.sum(axis=0)
    cen = (f[:, None] * P).sum(axis=0) / s
    bw = np.sqrt(np.maximum((((f[:, None] - cen[None, :]) ** 2) * P).sum(axis=0) / s, 0.0))
    return dom, cen, bw, DOM_FRAME_S


def signatures(pcm, fs=FS, beam_energies_db=None, beam_azimuth_deg=None):
    """The JSON `signatures` object of a clip (mono int16 array); computed from the high-passed signal only."""
    x = restrict(pcm, fs)
    rms = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
    frame = int(FRAME_S * fs)
    fr = _frames_rms(x, frame, frame)
    noise = float(np.sqrt(np.mean(np.sort(fr)[:max(1, int(math.ceil(0.1 * len(fr))))] ** 2))) if len(fr) else 0.0
    dom, cen, bw, dom_step_s = spectral(x, fs)
    ons = onsets(x, fs)
    return {
        "duration_s": round(len(pcm) / fs, 3),
        "peak_dbfs": round(_db(float(np.abs(x).max()) if len(x) else 0.0), 2),
        "rms_dbfs": round(_db(rms), 2),
        "snr_db": round(_db(rms) - max(_db(noise), -100.0), 1),
        "dominant_hz": dom, "dominant_step_s": round(dom_step_s, 4),
        "centroid_hz": {"mean": round(float(cen.mean()), 1), "std": round(float(cen.std()), 1)},
        "bandwidth_hz": {"mean": round(float(bw.mean()), 1), "std": round(float(bw.std()), 1)},
        "band_edges_hz": BAND_EDGES_HZ, "band_db": band_db(x, fs),
        "onsets_s": ons, "ioi": ioi_stats(ons),
        "beam_energies_db": beam_energies_db, "beam_azimuth_deg": beam_azimuth_deg,
    }


def envelope(pcm, fs=FS):
    """The JSON `envelope` object: coarse RMS track of the high-passed signal, 20 values per second."""
    v = envelope_rms_db(restrict(pcm, fs), fs)
    return {"n": len(v), "step_s": FRAME_S, "rms_db": v}
