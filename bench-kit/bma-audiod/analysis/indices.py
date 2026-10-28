"""Acoustic indices plugin ('indices'): a slow, descriptive soundscape-state track from the audio the analyzer already holds in RAM.

METADATA ONLY (see the privacy header of host.py): per interval one `acoustic_indices` record of scalar statistics. No audio, no
spectrogram and no waveform is stored; 5 s of audio exists in RAM during one process() call. Design note: docs/design/w2-indices.md,
contract: contracts/specs/acoustic-indices.md.

Signal and spectrogram: the plain MEAN of the 8 beams (float64), one WIN_S = 5 s window every HOP_S = 30 s (CPU budget: the plugin costs about 31 ms per window on Windows = about 1 ms per second of audio). Hann (periodic) frames of
NFFT = 480 samples (10 ms), NO overlap, so the bin width is exactly DF = 100 Hz and every band edge below is a bin edge; a band
[lo, hi) Hz is the bins lo/100 ... hi/100 - 1.

Indices of one window (formulas and conventions in the spec; verified against scikit-maad 1.5.2 by analysis/compare_maad.py):
  aci   Acoustic Complexity Index (Pieretti et al. 2011): per frequency bin sum_t |I(t+1) - I(t)| / sum_t I(t) on the amplitude
        spectrogram I, summed over ACI_BAND_HZ. Bins whose mean amplitude is below ACI_BIN_FLOOR (silent bins) contribute 0.
  hf    spectral entropy (Sueur et al. 2008): Shannon entropy of the time-mean AMPLITUDE spectrum over ENT_BAND_HZ divided by ln N.
  ht    temporal entropy (Sueur et al. 2008): Shannon entropy of the Hilbert amplitude envelope of the signal limited to ENT_BAND_HZ,
        divided by ln n.  h = ht * hf.
  bi    Bioacoustic Index (Boelman et al. 2007): area under the mean-spectrum dB curve above its minimum in BI_BAND_HZ, dB * kHz.
  ndsi  Normalised Difference Soundscape Index (Kasten et al. 2012): (B - A) / (B + A) from the Welch power spectrum, A = NDSI_ANTHRO_HZ,
        B = NDSI_BIO_HZ.
  level_dbfs  RMS of the mean signal in dBFS;  chorus_dbfs  mean-square power in CHORUS_BAND_HZ in dBFS (a full-scale sine reads -3.01).
A window below DEAD_DBFS (lost / zero-filled audio) or with a non-finite sample is counted and excluded. A window that overlaps a
tagger window flagged as speech is excluded and counted (`win_speech`); its numbers are never stored. The tagger answers from the
pool, possibly after this plugin's window, so an interval is closed GRACE_S after its end (or by flush() at shutdown). An interval
without a single used window writes NO record. FAIL CLOSED: a window the tagger windows do not cover END TO END (tagger down, dropped
or skipped the window, answered later than GRACE_S, or flush() at shutdown before the answer came) is unknown, not "not speech": it is
excluded and counted as `win_untagged`, its numbers are never stored. A tagger that stops answering therefore yields no records and a
rising `untagged` count (host.py adds it to meta.indices_errors).

Fail-safe: process(), note_tag() and flush() never raise; failures are counted in stats() (host.py exports them as meta.indices_errors)."""

import math, threading
import numpy as np
from scipy import fft as sfft

FS = 48000
NFFT = 480
DF = FS / NFFT  # 100 Hz
WIN_S, HOP_S, INTERVAL_S, GRACE_S = 5.0, 30.0, 300.0, 30.0
ACI_BAND_HZ = (1000, 11000)
ENT_BAND_HZ = (1000, 11000)
BI_BAND_HZ = (2000, 8000)
NDSI_ANTHRO_HZ = (1000, 2000)
NDSI_BIO_HZ = (2000, 8000)
CHORUS_BAND_HZ = (4000, 12000)
DEAD_DBFS = -100.0
ACI_BIN_FLOOR = 1e-9  # mean |X| per bin (FFT units, a full-scale sine bin is 120) under which a bin counts as silent
BI_AMP_FLOOR = 1e-9  # amplitude floor (-180 dBFS) of the BI dB curve
POWER_FLOOR = 1e-20  # -200 dBFS
NAMES = ("aci", "hf", "ht", "h", "bi", "ndsi", "level_dbfs", "chorus_dbfs")
DECIMALS = 4
_HANN = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(NFFT) / NFFT)
_SUMW, _SUMW2 = float(_HANN.sum()), float((_HANN**2).sum())


def _bins(band):
    lo, hi = band
    return int(round(lo / DF)), int(round(hi / DF))


def entropy_norm(v):
    """Shannon entropy of the non-negative vector v turned into a pmf, divided by its maximum ln(len(v)): 0..1.
    1 for a flat vector, 0 for a single non-zero entry, nan when everything is zero or v has fewer than 2 entries."""
    v = np.asarray(v, np.float64)
    s = v.sum()
    if v.size < 2 or not s > 0 or not np.isfinite(s):
        return float("nan")
    p = v[v > 0] / s
    return float(-(p * np.log(p)).sum() / math.log(v.size))


def aci_from_amplitude(A):
    """A: (frames, bins) amplitude spectrogram of the band -> sum over bins of sum_t |dI| / sum_t I (silent bins add 0)."""
    A = np.asarray(A, np.float64)
    tot = A.sum(axis=0)
    ok = tot / A.shape[0] > ACI_BIN_FLOOR
    d = np.abs(np.diff(A, axis=0)).sum(axis=0)
    return float((d[ok] / tot[ok]).sum())


def envelope_ht(x, band=ENT_BAND_HZ, fs=FS):
    """Normalised Shannon entropy of the Hilbert amplitude envelope of x limited to band. The analytic signal of the band is shifted to
    baseband (positive band bins doubled, then an inverse FFT of just the band's M bins): that is the exact complex baseband, so
    |z| is the envelope sampled at fs * M / len(x) = about the band width (10 kHz here), with no aliasing. It costs a fifth of the
    full-rate envelope; the entropy of the sub-sampled envelope differs from the full-rate one by about 1e-3 (measured, spec)."""
    n = len(x)
    X = sfft.rfft(np.asarray(x, np.float64) - np.mean(x))
    lo, hi = int(math.ceil(band[0] * n / fs - 1e-9)), int(math.ceil(band[1] * n / fs - 1e-9))
    return entropy_norm(np.abs(sfft.ifft(2.0 * X[lo:hi])))


def analyze(x):
    """All indices of one finite 1-D float window (len >= 10 * NFFT) -> dict(NAMES -> float), or None for a dead window (below DEAD_DBFS).
    Pure function: no state."""
    x = np.asarray(x, np.float64)
    nfr = len(x) // NFFT
    if nfr < 10:
        raise ValueError(f"window of {len(x)} samples is shorter than 10 frames of {NFFT}")
    ms = float(np.mean(x * x))
    level = 10.0 * math.log10(ms + 1e-30)
    if level < DEAD_DBFS:
        return None
    X = sfft.rfft(x[: nfr * NFFT].reshape(nfr, NFFT) * _HANN, axis=1)
    A = np.abs(X)
    a0, a1 = _bins(ACI_BAND_HZ)
    aci = aci_from_amplitude(A[:, a0:a1])
    e0, e1 = _bins(ENT_BAND_HZ)
    hf = entropy_norm(A[:, e0:e1].mean(axis=0))
    ht = envelope_ht(x[: nfr * NFFT])
    Pm = (2.0 * (A**2) / (NFFT * _SUMW2)).mean(axis=0)  # Welch power per bin, mean-square units
    n0, n1 = _bins(NDSI_ANTHRO_HZ)
    b0, b1 = _bins(NDSI_BIO_HZ)
    anth, bio = float(Pm[n0:n1].sum()), float(Pm[b0:b1].sum())
    ndsi = (bio - anth) / (bio + anth) if bio + anth > 0 else float("nan")
    i0, i1 = _bins(BI_BAND_HZ)
    L = 20.0 * np.log10(np.maximum(A[:, i0:i1].mean(axis=0) * 2.0 / _SUMW, BI_AMP_FLOOR))
    bi = float((L - L.min()).sum() * (DF / 1000.0))
    c0, c1 = _bins(CHORUS_BAND_HZ)
    chorus = 10.0 * math.log10(max(float(Pm[c0:c1].sum()), POWER_FLOOR))
    return {"aci": aci, "hf": hf, "ht": ht, "h": hf * ht, "bi": bi, "ndsi": ndsi, "level_dbfs": level, "chorus_dbfs": chorus}


def params():
    return {
        "fs": FS,
        "mix": "mean of 8 beams",
        "frame": f"hann {NFFT} no overlap",
        "df_hz": DF,
        "aci_band_hz": list(ACI_BAND_HZ),
        "entropy_band_hz": list(ENT_BAND_HZ),
        "bi_band_hz": list(BI_BAND_HZ),
        "ndsi_anthro_hz": list(NDSI_ANTHRO_HZ),
        "ndsi_bio_hz": list(NDSI_BIO_HZ),
        "chorus_band_hz": list(CHORUS_BAND_HZ),
        "dead_dbfs": DEAD_DBFS,
    }


class Indices:
    name = "indices"
    heavy = True

    def __init__(self, interval_s=INTERVAL_S, hop_s=HOP_S, win_s=WIN_S, speech_gate=True, grace_s=GRACE_S):
        self.interval_s, self.hop_s, self.win_s, self.grace_s = float(interval_s), float(hop_s), float(win_s), float(grace_s)
        if not (self.interval_s > 0 and self.hop_s > 0 and self.grace_s >= 0 and self.win_s >= 0.1):
            raise ValueError("interval_s and hop_s must be > 0, grace_s >= 0, win_s >= 0.1")
        self.speech_gate = bool(speech_gate)
        self.lock = threading.Lock()
        self.pend = {}  # bucket -> {"wins": [(t_end, kind, vals)], "partial": bool}
        self.closed_upto = None  # highest bucket already emitted
        self.tags = []  # (t0, t1, speech) of every tagger window seen
        self.st = {
            "windows": 0,
            "errors": 0,
            "nonfinite": 0,
            "silent": 0,
            "speech": 0,
            "untagged": 0,
            "late": 0,
            "intervals": 0,
            "suppressed": 0,
            "last_error": None,
        }

    def stats(self):
        with self.lock:
            return dict(self.st)

    def _err(self, e):
        self.st["errors"] += 1
        self.st["last_error"] = f"{type(e).__name__}: {e}"[:200]

    # ------------------------------------------------------------------------------------------------ tagger feed
    def note_tag(self, t_end, speech_active, win_s):
        """One tagger result: its window is [t_end - win_s, t_end]. Never raises."""
        try:
            t1, w = float(t_end), float(win_s)
            if not (math.isfinite(t1) and math.isfinite(w) and w > 0):
                raise ValueError("bad tagger window")
            with self.lock:
                self.tags.append((t1 - w, t1, bool(speech_active)))
        except Exception as e:
            with self.lock:
                self._err(e)

    def _overlaps(self, a, b, speech_only):
        return any(s0 < b and a < s1 and (sp or not speech_only) for s0, s1, sp in self.tags)

    def _covered(self, a, b):
        """True when the union of the tagger windows spans [a, b] without a gap (caller holds the lock)."""
        reach = a
        for s0, s1, _sp in sorted(self.tags):
            if s0 > reach + 1e-6:
                return False
            reach = max(reach, s1)
            if reach >= b - 1e-6:
                return True
        return False

    # ------------------------------------------------------------------------------------------------ plugin API
    def process(self, x, t_wall):
        recs = []
        try:
            xa = np.asarray(x)
            if xa.ndim != 2 or xa.shape[1] != 8 or xa.shape[0] < 10 * NFFT:
                raise ValueError(f"window shape {xa.shape}, need (n >= {10 * NFFT}, 8)")
            t = float(t_wall)
            if not math.isfinite(t):
                raise ValueError("non-finite t_wall")
            mono = xa.mean(axis=1, dtype=np.float64)
            if np.isfinite(mono.sum()):
                vals = analyze(mono)
                kind = "silent" if vals is None else "used"
                if vals is not None and not all(math.isfinite(v) for v in vals.values()):
                    kind, vals = "nonfinite", None
            else:
                kind, vals = "nonfinite", None
            with self.lock:
                self.st["windows"] += 1
                b = int(math.floor(t / self.interval_s))
                if self.closed_upto is not None and b <= self.closed_upto:
                    self.st["late"] += 1
                else:
                    if b not in self.pend:
                        late_start = not self.pend and self.closed_upto is None and (t - b * self.interval_s) >= self.win_s + self.hop_s
                        self.pend[b] = {"wins": [], "partial": bool(late_start)}
                    self.pend[b]["wins"].append((t, kind, vals))
                if kind != "used":
                    self.st["silent" if kind == "silent" else "nonfinite"] += 1
                for bb in sorted(self.pend):
                    if (bb + 1) * self.interval_s + self.grace_s <= t:
                        r = self._close(bb, False)
                        if r is not None:
                            recs.append(r)
        except Exception as e:
            with self.lock:
                self._err(e)
        return recs, None

    def flush(self):
        """Close every open interval at shutdown (marked partial) -> list of records."""
        recs = []
        try:
            with self.lock:
                for bb in sorted(self.pend):
                    r = self._close(bb, True)
                    if r is not None:
                        recs.append(r)
        except Exception as e:
            with self.lock:
                self._err(e)
        return recs

    def _close(self, b, partial):
        """Caller holds the lock. Removes bucket b; -> the record or None (no used window)."""
        ent = self.pend.pop(b)
        self.closed_upto = b if self.closed_upto is None else max(self.closed_upto, b)
        total = len(ent["wins"])
        used, speech, untagged = [], 0, 0
        silent = nonfinite = 0
        for t_end, kind, vals in ent["wins"]:
            a = t_end - self.win_s
            if kind == "silent":
                silent += 1
            elif kind == "nonfinite":
                nonfinite += 1
            elif self.speech_gate and self._overlaps(a, t_end, True):
                speech += 1
            elif self.speech_gate and not self._covered(a, t_end):
                untagged += 1
            else:
                used.append(vals)
        keep_from = (min(self.pend) if self.pend else b + 1) * self.interval_s - self.win_s  # no later window starts before this
        self.tags = [s for s in self.tags if s[1] > keep_from][-5000:]
        self.st["speech"] += speech
        self.st["untagged"] += untagged
        if not used:
            self.st["suppressed"] += 1
            return None
        ind = {}
        for nm in NAMES:
            p10, p50, p90 = np.percentile([v[nm] for v in used], [10, 50, 90])
            ind[nm] = {"p10": round(float(p10), DECIMALS), "p50": round(float(p50), DECIMALS), "p90": round(float(p90), DECIMALS)}
        t0 = b * self.interval_s
        rec = {
            "k": "acoustic_indices",
            "v": 1,
            "t": round(t0 + self.interval_s, 3),
            "t0": round(t0, 3),
            "span_s": self.interval_s,
            "win_s": self.win_s,
            "hop_s": self.hop_s,
            "win_total": total,
            "win_used": len(used),
            "win_speech": speech,
            "win_silent": silent,
            "win_nonfinite": nonfinite,
            "win_untagged": untagged,
            "speech_gate": self.speech_gate,
            "params": params(),
            "indices": ind,
        }
        if partial or ent["partial"]:
            rec["partial"] = True
        self.st["intervals"] += 1
        return rec


def make(ctx):
    """Plugin factory named by analysis/plugins.manifest.json (ctx = manifest.Ctx)."""
    gate = "tags" in ctx.started
    if not gate and not ctx.args.indices_without_speech_gate:
        raise RuntimeError("needs the tags plugin as its speech gate (or --indices-without-speech-gate)")
    return Indices(ctx.args.indices_interval_s, ctx.args.indices_hop_s, speech_gate=gate)
