"""Inter-beam phase coherence plugin ('coherence'): how phase-coherent are the 8 beam outputs with each other?

Question it answers (in memory, metadata only: NO audio is ever stored, see the privacy header of host.py): is beamspace
processing (adjacent-beam differencing, adaptive combination, direction-based separation) possible with the DSP's beam
outputs, and does the DSP's non-linear processing (noise canceller, echo canceller, NLP, gating) break inter-beam phase?

Per 1 s window (array (n, 8), slot = beam, ring-aligned) and per ring-adjacent pair p = (i, j = (i + 1) % 8):
  * Welch cross-spectra: Hann segments of NSEG = 4096 samples (df = 11.72 Hz), hop SEG_HOP = 2048 (50 % overlap): K = 22
    segments per 48000-sample window.
  * per octave band (50-125 ... 8k-16k, the beam_bands edges): magnitude-squared coherence MSC (mean of the per-bin MSC
    over the band's unmasked bins), cross-phase at the band's centre bin (degrees, the fitted delay slope removed first),
    inter-beam lag in samples (phase slope across the band), and the level difference in dB.
  * broadband cross-check: lag from the largest-MAGNITUDE cross-correlation peak of the 125-4000 Hz averaged cross-spectrum with
    parabolic sub-sample interpolation on |r| (xc_lag). The polarity of that peak is not reported: a polarity-inverted pair
    (cross-phase near 180 deg in ph_deg) still gets its true delay in xc_lag, not a noise-floor sample.
  * every FULL_EVERY-th used window the full 8 x 8 MSC (28 pairs) in three broad bands (FULL_BANDS_HZ).
Windows are classified first: dead (a 250 ms sub-block whose loudest beam is below DEAD_DBFS: zero-filled / lost audio),
gated (in any 250 ms sub-block some beam is more than GATE_DB below the loudest: the DSP gating signature). Both are
COUNTED but excluded from the coherence statistics. The others are classified per band against a running noise floor
(10th percentile of the last HIST_N levels of that band): quiet (level <= floor + QUIET_MARGIN_DB), active (>= floor +
ACTIVE_MARGIN_DB), mid (between, counted, not in the statistics), warmup (the first WARMUP_N windows).
Aggregated every --coherence-interval-s (default 60 s, epoch-aligned buckets) into one `beam_coherence` v1 record in
coherence.jsonl (contracts/specs/beam-coherence.md). The open bucket is emitted by flush() at shutdown (host.py calls it) with
"partial": true, and so is the first bucket after a start that began late in it; other records carry no `partial` key.

Conventions: pair p = (i, j = (i+1)%8). Pure delay d samples of beam j relative to beam i (x_j(n) = x_i(n - d)) gives
C = E[X_i conj(X_j)] = |X|^2 exp(+j 2 pi f d / fs): cross-phase = +360 f d / fs degrees, lag = +d (positive: j lags i).

Lag accuracy: the phase-slope lag of a pure delay d is biased low by about 2 % of d at K = 22 segments (0.2 % at K = 220;
the broadband xc_lag is exact to 1e-5 sample): measured, tolerance in test_coherence.py is 0.02 + 3 % of |lag| samples.

Bias: MSC from K segments is biased upward. Hann at 50 % overlap has K_EFF = K / (1 + 2 rho^2) with rho = 1/6, about
20.8 effective independent segments, so independent noise gives a mean MSC of about 1/K_EFF = 0.048 (not 0), and in general
E[MSC_hat] ~ MSC + (1 - MSC)^2 / K_EFF (Monte-Carlo agrees within 0.001 from MSC 0.5 to 0.95). Records carry the RAW MSC plus
msc_floor = 1/K_EFF; readers invert that quadratic: with u = 1 - msc_c, u^2 / K - u + (1 - msc) = 0, so
msc_c = 1 - (K / 2) * (1 - sqrt(1 - 4 (1 - msc) / K)) (coherence_report.correct() does).

Tonal lines (the unit's electronic lines at ~11.5 kHz, 12 kHz, 15.3-15.6 kHz, identical on all beams, so their MSC is 1 and
is not a source): bins within max(tol, MASK_GUARD_BINS * df) of a line of the --tonal-mask file get weight 0 in every band
statistic. A band that contains masked bins also reports msc_unmasked_p50 (the same median with all bins), so the effect of
the lines is visible. Without --tonal-mask nothing is masked (mask_hz: []). Leakage of a very strong tone beyond the guard
bins is not removed.

Cost: one batched float32 rfft over (8, 22, 4096) per window, no GPU, no threads, preallocated work buffers (the FFT output
of about 3 MB is allocated by the library); measured in analysis/bench_plugins.py.

Fail-safe: process() never raises. A window with non-finite samples counts in stats['nonfinite'] (and win_nonfinite of its
minute), a wrong shape / dtype / time counts in stats['errors']; state is committed only after all numbers of a window
are computed and finite."""

import json, math
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from scipy import fft as sfft

FS, NB = 48000, 8
NSEG, SEG_HOP = 4096, 2048
DF = FS / NSEG
BANDS_HZ = [(50, 125), (125, 250), (250, 500), (500, 1000), (1000, 2000), (2000, 4000), (4000, 8000), (8000, 16000)]
FULL_BANDS_HZ = [(125, 1000), (1000, 4000), (4000, 12000)]
FULL_COND_BAND = (2, 5, 6)  # octave band whose class conditions each broad band (the one holding its geometric centre)
FULL_EVERY, FULL_MIN_N = 5, 3
SUBBLOCKS = 4  # 250 ms sub-blocks of a 1 s window for the dead / gated tests
GATE_DB, DEAD_DBFS = 40.0, -100.0
QUIET_MARGIN_DB, ACTIVE_MARGIN_DB = 3.0, 6.0
WARMUP_N, HIST_N, FLOOR_EVERY = 60, 900, 10
MIN_N = 5  # a (band, condition) cell with fewer windows in a minute is null (counted in n_*)
MASK_GUARD_BINS = 2.0  # Hann main lobe is +-2 bins
MAX_LAG = 256  # xc_lag peak search range, samples
XC_BAND_HZ = (125, 4000)
HANN_RHO = 1.0 / 6.0  # overlap correlation of Hann segments at 50 % overlap
CLASSES = ("warmup", "quiet", "active", "mid")


def k_segments(n):
    return (n - NSEG) // SEG_HOP + 1


K_SEG = k_segments(FS)
K_EFF = K_SEG / (1.0 + 2.0 * HANN_RHO**2)
MSC_FLOOR = 1.0 / K_EFF


def _bin(hz):
    return int(math.ceil(hz * NSEG / FS - 1e-9))


def load_mask(path, tol_hz=None):
    """-> (sorted line list in Hz, tolerance in Hz) from the JSON learn_tonal_mask.py writes."""
    if not path:
        return [], 0.0
    with open(path, encoding="utf-8") as f:
        m = json.load(f)
    return sorted(float(h) for h in m["lines_hz"]), float(tol_hz if tol_hz is not None else m.get("tolerance_hz", 4.0))


def circ_mean_deg(deg):
    """Circular mean of angles in degrees along axis 0 -> (mean in (-180, 180], resultant length R)."""
    z = np.exp(1j * np.deg2rad(np.asarray(deg, np.float64))).mean(axis=0)
    return np.rad2deg(np.angle(z)), np.abs(z)


def circ_spread_deg(deg):
    """Circular standard deviation sqrt(-2 ln R) in degrees along axis 0."""
    _, r = circ_mean_deg(deg)
    return np.rad2deg(np.sqrt(-2.0 * np.log(np.clip(r, 1e-12, 1.0))))


def classify_window(x):
    """Time-domain window class from four 250 ms sub-blocks -> (dead, gated, mean level dBFS).
    dead: some sub-block's loudest beam is below DEAD_DBFS. gated (and not dead): in some sub-block the quietest beam is more
    than GATE_DB below the loudest. x: (n, 8) float32."""
    sb = x.shape[0] // SUBBLOCKS
    v = x[: sb * SUBBLOCKS].reshape(SUBBLOCKS, sb, NB)
    ms = np.einsum("ibj,ibj->ij", v, v) / sb  # (4, 8) mean square per beam
    db = 10.0 * np.log10(ms.astype(np.float64) + 1e-30)
    dead = bool((db.max(axis=1) < DEAD_DBFS).any())
    gated = (not dead) and bool(((db.max(axis=1) - db.min(axis=1)) > GATE_DB).any())
    return dead, gated, float(10.0 * np.log10(ms.mean(dtype=np.float64) + 1e-30))


class Win:
    """Numbers of one analysed window."""

    __slots__ = ("dead", "gated", "level_dbfs", "L", "msc", "msc_all", "ph", "lag", "dl", "xc_lag", "full")

    def __init__(self, dead=False, gated=False, level_dbfs=0.0):
        self.dead, self.gated, self.level_dbfs = dead, gated, level_dbfs
        self.L = self.msc = self.msc_all = self.ph = self.lag = self.dl = self.xc_lag = self.full = None


class Coherence:
    name = "coherence"
    win_s, hop_s = 1.0, 2.0
    heavy = False

    def __init__(self, mask_path=None, mask_tol_hz=None, interval_s=60.0):
        self.interval_s = float(interval_s)
        if not self.interval_s > 0:
            raise ValueError("interval_s must be > 0")
        self.mask, self.mask_tol = load_mask(mask_path, mask_tol_hz)
        self.hann = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(NSEG) / NSEG)).astype(np.float32)
        self.nxt = (np.arange(NB) + 1) % NB
        self.b0, self.b1 = _bin(BANDS_HZ[0][0]), _bin(BANDS_HZ[-1][1])  # reduced bin axis = absolute bins [b0, b1)
        nb = self.b1 - self.b0
        absf = np.arange(self.b0, self.b1) * DF
        guard = max(self.mask_tol, MASK_GUARD_BINS * DF)
        self.unmasked = np.ones(nb, bool)
        for ln in self.mask:
            self.unmasked &= np.abs(absf - ln) > guard
        self.rng = [(_bin(lo) - self.b0, _bin(hi) - self.b0) for lo, hi in BANDS_HZ]
        self.centre_bin = [(self.b0 + s + self.b0 + e - 1) / 2.0 for s, e in self.rng]
        self.centre_hz = [round(c * DF, 1) for c in self.centre_bin]
        self.tonal_bins = [int((~self.unmasked[s:e]).sum()) for s, e in self.rng]
        self.Wm = np.zeros((nb, len(BANDS_HZ)))
        self.Wa = np.zeros((nb, len(BANDS_HZ)))
        for b, (s, e) in enumerate(self.rng):
            self.Wa[s:e, b] = 1.0
            self.Wm[s:e, b] = self.unmasked[s:e]
            if self.Wm[:, b].sum() == 0:  # a fully masked band: use all bins
                self.Wm[s:e, b] = 1.0
        self.Wn, self.Wan = self.Wm / self.Wm.sum(0), self.Wa / self.Wa.sum(0)
        self.wpair = [self.Wm[s : e - 1, b] * self.Wm[s + 1 : e, b] for b, (s, e) in enumerate(self.rng)]
        self.off = [np.arange(self.b0 + s, self.b0 + e) - self.centre_bin[b] for b, (s, e) in enumerate(self.rng)]
        self.fb0, self.fb1 = _bin(FULL_BANDS_HZ[0][0]), _bin(FULL_BANDS_HZ[-1][1])
        fedges = [_bin(FULL_BANDS_HZ[0][0])] + [_bin(hi) for _, hi in FULL_BANDS_HZ]
        nf = self.fb1 - self.fb0
        self.Wf = np.zeros((nf, len(FULL_BANDS_HZ)))
        fm = self.unmasked[self.fb0 - self.b0 : self.fb1 - self.b0]
        for q in range(len(FULL_BANDS_HZ)):
            sl = slice(fedges[q] - self.fb0, fedges[q + 1] - self.fb0)
            self.Wf[sl, q] = fm[sl]
            if self.Wf[:, q].sum() == 0:
                self.Wf[sl, q] = 1.0
        self.Wf /= self.Wf.sum(0)
        self.iu = np.triu_indices(NB, 1)
        self.xc0, self.xc1 = _bin(XC_BAND_HZ[0]), _bin(XC_BAND_HZ[1])
        self.xc_lags = np.arange(-MAX_LAG, MAX_LAG + 1)
        self.xc_idx = self.xc_lags % NSEG
        self.xc_cross = np.zeros((NB, NSEG // 2 + 1), np.complex128)
        self._k = None
        self.hist = np.zeros((len(BANDS_HZ), HIST_N))
        self.nh = 0
        self.floor = np.zeros(len(BANDS_HZ))
        self.n_used = 0
        self.agg = Aggregator(self)
        self.st = {"windows": 0, "errors": 0, "nonfinite": 0, "late": 0, "overflow": 0, "minutes": 0, "gated": 0, "dead": 0, "used": 0, "last_error": None}

    # ---------------------------------------------------------------- analysis (no state besides scratch buffers)
    def _ensure(self, n):
        k = k_segments(n)
        if self._k != (n, k):
            self._k = (n, k)
            self._xt = np.empty((NB, n), np.float32)
            self._buf = np.empty((NB, k, NSEG), np.float32)
            self._X = np.empty((NB, k, self.b1 - self.b0), np.complex64)
            self._Xc = np.empty_like(self._X)
            self._F = np.empty((NB, k, self.fb1 - self.fb0), np.complex64)

    def analyze(self, x, full=False):
        """Everything process() computes for one finite (n >= NSEG, 8) float32 window, without touching the running state."""
        dead, gated, lvl = classify_window(x)
        w = Win(dead, gated, lvl)
        if dead or gated:
            return w
        self._ensure(x.shape[0])
        np.copyto(self._xt, x.T)
        seg = sliding_window_view(self._xt, NSEG, axis=1)[:, ::SEG_HOP]
        np.multiply(seg, self.hann, out=self._buf)
        nbin = self.b1 - self.b0
        for i in range(NB):  # per beam: the FFT output stays in cache
            Fi = sfft.rfft(self._buf[i], axis=-1, workers=1)  # (K, 2049) complex64
            self._X[i] = Fi[:, self.b0 : self.b1]
            if full:
                self._F[i] = Fi[:, self.fb0 : self.fb1]
        X = self._X
        k = X.shape[1]
        np.conjugate(X, out=self._Xc)
        Sxx = (np.einsum("ikf,ikf->if", X.real, X.real).astype(np.float64) + np.einsum("ikf,ikf->if", X.imag, X.imag)) / k
        C = np.empty((NB, nbin), np.complex128)
        C[:-1] = np.einsum("ikf,ikf->if", X[:-1], self._Xc[1:])
        C[-1] = np.einsum("kf,kf->f", X[-1], self._Xc[0])
        C /= k  # E[X_i conj(X_j)]
        msc = np.clip((C.real**2 + C.imag**2) / (Sxx * Sxx[self.nxt] + 1e-60), 0.0, 1.0)
        w.msc, w.msc_all = msc @ self.Wn, msc @ self.Wan  # (8 pairs, 8 bands)
        Pb = Sxx @ self.Wm + 1e-60  # (8 beams, 8 bands)
        w.L = (10.0 * np.log10(Pb)).mean(axis=0)
        w.dl = 10.0 * np.log10(Pb / Pb[self.nxt])
        nbd = len(BANDS_HZ)
        w.ph, w.lag = np.zeros((NB, nbd)), np.zeros((NB, nbd))
        Z = C[:, :-1].conj() * C[:, 1:]
        for b, (s, e) in enumerate(self.rng):
            slope = np.angle((Z[:, s : e - 1] * self.wpair[b]).sum(axis=1))
            w.lag[:, b] = slope * NSEG / (2 * np.pi)
            rot = np.exp(-1j * slope[:, None] * self.off[b][None, :])
            w.ph[:, b] = np.rad2deg(np.angle((C[:, s:e] * rot * self.Wm[s:e, b]).sum(axis=1)))
        self.xc_cross[:, self.xc0 : self.xc1] = C[:, self.xc0 - self.b0 : self.xc1 - self.b0].conj()  # irfft of conj(C): peak at +lag
        r = np.fft.irfft(self.xc_cross, n=NSEG, axis=1)[:, self.xc_idx]  # lags -MAX_LAG..MAX_LAG
        ra = np.abs(r)  # magnitude: an anti-phase pair has its peak negative
        i = np.argmax(ra, axis=1)
        xc = self.xc_lags[i].astype(np.float64)
        for p in range(NB):
            a = i[p]
            if 0 < a < len(self.xc_lags) - 1:
                y0, y1, y2 = ra[p, a - 1], ra[p, a], ra[p, a + 1]
                den = y0 - 2 * y1 + y2
                if den < 0:
                    xc[p] += 0.5 * (y0 - y2) / den
        w.xc_lag = xc
        if full:
            A = self._F.transpose(2, 0, 1)  # (nbin, 8, K)
            Cf = np.matmul(A, A.conj().transpose(0, 2, 1)) / k  # (nbin, 8, 8)
            d = np.real(np.einsum("fii->fi", Cf))
            m = (np.abs(Cf) ** 2) / (d[:, :, None] * d[:, None, :] + 1e-60)
            w.full = np.clip(m[:, self.iu[0], self.iu[1]].T.astype(np.float64) @ self.Wf, 0.0, 1.0).T  # (3 bands, 28 pairs)
        return w

    # ---------------------------------------------------------------- conditioning
    def noise_floor_db(self):
        return self.floor.copy()

    def classify_levels(self, L):
        """Band classes (0 warmup, 1 quiet, 2 active, 3 mid) of band levels L (8,) against the running floor."""
        if self.nh < WARMUP_N:
            return np.zeros(len(BANDS_HZ), int)
        L = np.asarray(L, np.float64)
        return np.where(L <= self.floor + QUIET_MARGIN_DB, 1, np.where(L >= self.floor + ACTIVE_MARGIN_DB, 2, 3))

    def _push_levels(self, L):
        self.hist[:, self.nh % HIST_N] = L
        self.nh += 1
        if self.nh % FLOOR_EVERY == 0:
            self.floor = np.percentile(self.hist[:, : min(self.nh, HIST_N)], 10, axis=1)

    # ---------------------------------------------------------------- plugin API
    def stats(self):
        return dict(self.st)

    def flush(self):
        """Close the open bucket at shutdown -> [record] (marked partial) or []. Running state is kept; a second call returns []."""
        try:
            r = self.agg.close(partial=True)
        except Exception as e:  # fail-safe like process()
            self.st["errors"] += 1
            self.st["last_error"] = f"{type(e).__name__}: {e}"[:200]
            return []
        if r is None:
            return []
        self.st["minutes"] += 1
        self.agg.reset()
        return [r]

    def process(self, x, t_wall):
        recs = []
        try:
            try:
                xa = np.asarray(x)
                if xa.ndim != 2 or xa.shape[1] != NB or xa.shape[0] < NSEG:
                    raise ValueError(f"window shape {xa.shape}, need (n >= {NSEG}, {NB})")
                xa = np.ascontiguousarray(xa, dtype=np.float32)
                t = float(t_wall)
                if not math.isfinite(t):
                    raise ValueError("non-finite t_wall")
            except (TypeError, ValueError) as e:
                self.st["errors"] += 1
                self.st["last_error"] = f"{type(e).__name__}: {e}"[:200]
                return recs, None
            finite = bool(np.isfinite(xa.sum(dtype=np.float64)))
            w = None
            if finite:
                w = self.analyze(xa, full=(self.n_used % FULL_EVERY == 0))
                if not w.dead and not w.gated and not all(np.isfinite(a).all() for a in (w.msc, w.ph, w.lag, w.dl, w.L, w.xc_lag)):
                    finite, w = False, None
            # ---- commit: nothing above changed any running state
            self.st["windows"] += 1
            bkt = math.floor(t / self.interval_s)
            if self.agg.bucket is None:
                self.agg.open(bkt, partial=(t - bkt * self.interval_s) >= self.hop_s)  # started late in the bucket
            elif bkt > self.agg.bucket:
                r = self.agg.close()
                if r is not None:
                    recs.append(r)
                    self.st["minutes"] += 1
                self.agg.open(bkt)
            elif bkt < self.agg.bucket:
                self.st["late"] += 1
            if not finite:
                self.st["nonfinite"] += 1
                self.agg.add_nonfinite()
            elif w.dead:
                self.st["dead"] += 1
                self.agg.add_dead()
            elif w.gated:
                self.st["gated"] += 1
                self.agg.add_gated()
            else:
                cls = self.classify_levels(w.L)
                self._push_levels(w.L)
                self.n_used += 1
                self.st["used"] += 1
                self.st["overflow"] += self.agg.add_used(w, cls)
        except Exception as e:  # fail-safe: counted, never raised
            self.st["errors"] += 1
            self.st["last_error"] = f"{type(e).__name__}: {e}"[:200]
        return recs, None


class Aggregator:
    """One bucket (epoch-aligned interval) of window numbers -> one beam_coherence record at close."""

    def __init__(self, c):
        self.c = c
        self.cap = max(64, 2 * int(math.ceil(c.interval_s / c.hop_s)) + 8)
        nbd = len(BANDS_HZ)
        self.A = {k: np.zeros((2, self.cap, nbd, NB)) for k in ("msc", "ph", "lag", "dl")}
        self.unm = np.zeros((2, self.cap, nbd, NB))
        self.fa = np.zeros((2, self.cap, len(FULL_BANDS_HZ), 28))
        self.xcs = np.zeros((self.cap, NB))
        self.bucket = None
        self.partial = False
        self.reset()

    def reset(self):
        nbd = len(BANDS_HZ)
        self.n = np.zeros((2, nbd), int)
        self.nf = np.zeros((2, len(FULL_BANDS_HZ)), int)
        self.mid = np.zeros(nbd, int)
        self.warm = np.zeros(nbd, int)
        self.n_xc = 0
        self.total = self.used = self.gated = self.dead = self.nonfinite = 0
        self.lvl_sum = 0.0

    def open(self, bucket, partial=False):
        self.bucket = bucket
        self.partial = bool(partial)
        self.reset()

    def add_nonfinite(self):
        self.total += 1
        self.nonfinite += 1

    def add_dead(self):
        self.total += 1
        self.dead += 1

    def add_gated(self):
        self.total += 1
        self.gated += 1

    def add_used(self, w, cls):
        """-> number of per-cell stores dropped because a cell was full (0 normally)."""
        self.total += 1
        self.used += 1
        self.lvl_sum += w.level_dbfs
        over = 0
        for b in range(len(BANDS_HZ)):
            ci = int(cls[b])
            if ci == 0:
                self.warm[b] += 1
            elif ci == 3:
                self.mid[b] += 1
            else:
                cond = ci - 1
                j = self.n[cond, b]
                if j >= self.cap:
                    over += 1
                    continue
                self.A["msc"][cond, j, b], self.A["ph"][cond, j, b] = w.msc[:, b], w.ph[:, b]
                self.A["lag"][cond, j, b], self.A["dl"][cond, j, b] = w.lag[:, b], w.dl[:, b]
                self.unm[cond, j, b] = w.msc_all[:, b]
                self.n[cond, b] = j + 1
        if self.n_xc < self.cap:
            self.xcs[self.n_xc] = w.xc_lag
            self.n_xc += 1
        if w.full is not None:
            for q, ob in enumerate(FULL_COND_BAND):
                ci = int(cls[ob])
                if ci in (1, 2) and self.nf[ci - 1, q] < self.cap:
                    self.fa[ci - 1, self.nf[ci - 1, q], q] = w.full[q]
                    self.nf[ci - 1, q] += 1
        return over

    def close(self, partial=False):
        """-> the record of the open bucket, or None when it saw no window at all. partial: the bucket did not run to its end."""
        if self.total == 0:
            return None
        c, nbd = self.c, len(BANDS_HZ)
        r3, r1, r2 = (lambda a: np.round(a, 3).tolist()), (lambda a: np.round(a, 1).tolist()), (lambda a: np.round(a, 2).tolist())
        bands = []
        for b in range(nbd):
            ent = {}
            for cond, nm in ((0, "quiet"), (1, "active")):
                n = int(self.n[cond, b])
                ent["n_" + nm] = n
                if n < MIN_N:
                    ent[nm] = None
                    continue
                msc, lag = self.A["msc"][cond, :n, b], self.A["lag"][cond, :n, b]
                p10, p50, p90 = np.percentile(msc, [10, 50, 90], axis=0)
                l10, l50, l90 = np.percentile(lag, [10, 50, 90], axis=0)
                ph = self.A["ph"][cond, :n, b]
                cell = {
                    "n": n,
                    "msc_p10": r3(p10),
                    "msc_p50": r3(p50),
                    "msc_p90": r3(p90),
                    "ph_deg": r1(circ_mean_deg(ph)[0]),
                    "ph_spread_deg": r1(circ_spread_deg(ph)),
                    "lag_p50": r3(l50),
                    "lag_spread": r3(l90 - l10),
                    "dl_db_p50": r2(np.median(self.A["dl"][cond, :n, b], axis=0)),
                }
                if c.tonal_bins[b] > 0:
                    cell["msc_unmasked_p50"] = r3(np.median(self.unm[cond, :n, b], axis=0))
                ent[nm] = cell
            ent["mid"], ent["warmup"] = int(self.mid[b]), int(self.warm[b])
            bands.append(ent)
        cond_full = {}
        for cond, nm in ((0, "quiet"), (1, "active")):
            ns = [int(v) for v in self.nf[cond]]
            if max(ns) < FULL_MIN_N:
                cond_full[nm] = None
                continue
            med = [np.median(self.fa[cond, : ns[q], q], axis=0) if ns[q] >= FULL_MIN_N else np.full(28, np.nan) for q in range(len(ns))]
            cond_full[nm] = {"n": ns, "msc": [[None if not np.isfinite(v) else round(float(v), 3) for v in row] for row in med]}
        xc = r3(np.median(self.xcs[: self.n_xc], axis=0)) if self.n_xc >= MIN_N else None
        t0 = self.bucket * c.interval_s
        rec = {
            "k": "beam_coherence",
            "v": 1,
            "t": round(t0 + c.interval_s, 3),
            "t0": round(t0, 3),
            "span_s": c.interval_s,
            "win_total": int(self.total),
            "win_used": int(self.used),
            "win_gated": int(self.gated),
            "win_dead": int(self.dead),
            "win_nonfinite": int(self.nonfinite),
            "level_dbfs": round(self.lvl_sum / self.used, 1) if self.used else None,
            "nfft": NSEG,
            "seg_hop": SEG_HOP,
            "nseg_per_win": K_SEG,
            "msc_floor": round(MSC_FLOOR, 3),
            "gate_db": GATE_DB,
            "dead_dbfs": DEAD_DBFS,
            "quiet_margin_db": QUIET_MARGIN_DB,
            "active_margin_db": ACTIVE_MARGIN_DB,
            "edges_hz": [list(e) for e in BANDS_HZ],
            "centre_hz": c.centre_hz,
            "mask_hz": c.mask,
            "mask_tol_hz": c.mask_tol,
            "tonal_bins": c.tonal_bins,
            "bands": bands,
            "xc_lag": xc,
            "full": {"edges_hz": [list(e) for e in FULL_BANDS_HZ], "pairs": "i<j row-major", "cond": cond_full},
        }
        if partial or self.partial:
            rec["partial"] = True
        return rec


def make(ctx):
    """Plugin factory named by analysis/plugins.manifest.json (ctx = manifest.Ctx)."""
    a = ctx.args
    return Coherence(a.tonal_mask, a.tonal_mask_tol, a.coherence_interval_s)
