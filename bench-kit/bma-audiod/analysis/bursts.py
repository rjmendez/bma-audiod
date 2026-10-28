"""Burst / artifact identification plugin ('bursts'): what are the cold-hour impulsive 8-16 kHz bursts BirdNET calls katydids?

Question it answers (metadata only: NO audio is ever stored, see the privacy header of host.py): is a train of short impulses in
the 6-16 kHz region the unit's own electronics or housing (very regular, identical on all beams, low beam spread), an insect
(a trill: regular but at a modulation rate that follows temperature, level varies by beam), or a nearby periodic device (regular,
some beams louder)? Per epoch-aligned interval (--bursts-interval-s, default 60 s) one `burst_stats` v1 record goes to
bursts.jsonl (contracts/specs/burst-stats.md).

Per 1 s window (array (n, 8), ring-aligned, speech is never looked at):
  * band-pass 6-16 kHz (elliptic, order 4 = 8 poles, 0.5 dB passband ripple, 60 dB stopband; second-order sections in float32,
    state carried across windows; measured response: 0 dB at 6-16 kHz, -15 dB at 5 kHz, -31 dB at 4 kHz, -55 dB at 3 kHz, -12 dB
    at 17 kHz; a Butterworth of the same cost passes 4 kHz at only -16 dB, so a bird at 4 kHz would trigger the detector) on every
    beam; per-beam mean-square in 1 ms frames (48 samples); the total is the mean over the 8 beams of the per-beam frame energy.
  * IMPULSE DETECTOR on the total, in dB: a frame is an impulse when it is a local maximum, at least MIN_GAP_MS (5 ms) from the
    previous impulse (scipy find_peaks distance), standing at least PROM_DB (6 dB) above the lowest valley between it and a
    higher peak (one peak per click or pulse: the ripple of a plateau or the ringing of a click is not a second impulse), at least THRESH_DB (10 dB) above the running MEDIAN of the last MEDIAN_S (3 s)
    of 1 ms frames, and above ABS_FLOOR_DB (-80 dBFS mean square: below that is electronics noise, not an event).
    Why: the median ignores impulses while they occupy less than half of the frames (10 per second at 1 ms is 1 %), so it tracks
    the background even in a burst hour; a 1 ms frame of Gaussian noise in this band has 36 degrees of freedom, so its level
    scatters by about 1 dB (4.34 * sqrt(2 / 36)) and +10 dB is about 10 sigma: stationary noise or a steady tone gives none (a
    false alarm needs a change of the background, not a fluctuation). The 5 ms minimum spacing is also the lower end of the gap
    histogram. The first and the last frame of a window cannot be a peak (an impulse exactly there is missed, 2 of 1000 frames);
    after a start or a gap in the windows the filter state is reset and the first RESET_MASK_FRAMES frames are ignored (the
    filter's start-up transient would otherwise look like an impulse).
  * per impulse: level (total, dB), excess over the median, the per-beam level (max of the beam's frames over the peak frame +-1 ms),
    spread (loudest minus quietest beam, dB), how many beams are within 3 dB of the loudest (1..8), the loudest beam, and how many of
    the 8 beams are themselves an impulse within +-1 ms of the peak ("hit": the beam's level in [peak - 1 ms, peak + 1 ms] is at
    least THRESH_DB over that beam's own median). `coincident_all` = all 8 beams hit.
  * 1 kHz sub-band levels 6-7 ... 15-16 kHz of the beam MEAN (11 non-overlapping Hann segments of 4096 points, power in dBFS) and the 2-6 kHz band as
    a quiet reference; per interval the p10 / p50 / p90 over the windows.
  * envelope modulation: the total frame energy (linear) averaged to 200 Hz; Welch spectrum (Hann, 4000 points = 20 s, df 0.05 Hz)
    per run of contiguous windows (runs shorter than MOD_MIN_RUN_S are not used); the peak between 0.3 and 30 Hz, its prominence
    over the median of that range (dB) and the relative RMS of the envelope.
  * inter-impulse gaps (pure function gap_stats; taken per run of contiguous used windows, never across an excluded hole): 30 log-spaced bins 5 ms..30 s, the modal gap, the regularity index (share of
    gaps within +-15 % of the mode) and the jitter (coefficient of variation of the gaps inside that window).

SPEECH: windows overlapping a tagger window that was flagged speech are excluded (counted, win_speech). The tagger runs seconds
later than this plugin, so records are held back SPEECH_GRACE_S (15 s) and the host calls note_speech() for every tagger verdict.
With the tagger on, a window that the verdicts do not cover in full is excluded too (win_unvetted): it could be speech. At shutdown, windows younger
than the grace are dropped (never written unvetted). With the tagger off (tagger=False) nothing is held back and `tagger` is false.

Fail-safe: process() never raises. A window with the wrong shape or non-finite samples counts in stats()['errors'] /
['nonfinite']; an exception inside counts in errors with last_error."""

import math, threading
import numpy as np
from scipy import signal
from scipy import fft as sfft

FS, NB = 48000, 8
SEG = 4096
FRAME = 48  # 1 ms
BAND_LO_HZ, BAND_HI_HZ = 6000.0, 16000.0
SUB_EDGES_HZ = list(range(6000, 17000, 1000))  # 11 edges -> 10 one-kHz bands
REF_BAND_HZ = (2000.0, 6000.0)
THRESH_DB = 10.0
ABS_FLOOR_DB = -80.0
PROM_DB = 6.0
MIN_GAP_MS = 5
MEDIAN_S = 3.0
HIST_FRAMES = int(MEDIAN_S * 1000)
RESET_MASK_FRAMES = 6
COINC_FRAMES = 1  # +-1 ms
GAP_MIN_S, GAP_MAX_S, GAP_BINS = 0.005, 30.0, 30
GAP_EDGES = GAP_MIN_S * (GAP_MAX_S / GAP_MIN_S) ** (np.arange(GAP_BINS + 1) / GAP_BINS)
REG_TOL = 0.15
MIN_GAPS = 5  # fewer gaps in an interval: mode / regularity / jitter are null
ENV_FS = 200
MOD_NPERSEG = 4000
MOD_BAND_HZ = (0.3, 30.0)
MOD_MIN_RUN_S = 10.0
SPEECH_GRACE_S = 15.0
VERDICT_KEEP_S = 600.0
VET_TOL_S = 0.25  # clock slack when checking that verdicts cover a window (the host loop ticks every 0.25 s)
MAX_INTERVAL_S = 86400.0
DEAD_PEAK = 1e-7  # a window whose peak is below this (-140 dBFS) is zero-filled / lost audio


def _r(v, nd=2):
    return None if v is None else round(float(v), nd)


def pct3(v, nd=2):
    """[p10, p50, p90] (linear interpolation) of a 1-D array, or None when it is empty."""
    v = np.asarray(v, np.float64)
    if v.size == 0:
        return None
    return [round(float(q), nd) for q in np.percentile(v, [10, 50, 90])]


def gap_stats(t, tol=REG_TOL, min_n=MIN_GAPS, group=None):
    """Inter-impulse statistics of impulse times t (seconds, any order). PURE: it can be fed recorded events (dsp 'transient' times).
    group (optional, same length as t): a run id per impulse; gaps are only taken between neighbouring impulses of the SAME run, so a
    hole (excluded or lost windows) never becomes one false long gap; n is then impulses minus the number of runs holding impulses.
    -> {"n": gaps, "hist": [30 counts, bins GAP_EDGES], "under_n", "over_n", "mode_s", "mode_n", "regularity", "jitter_cv"}.
    n = number of gaps (impulses - 1). hist counts the gaps in [5 ms, 30 s); under_n / over_n the rest.
    mode_s: among the gaps inside [5 ms, 30 s) the candidate whose +-tol window (|g - c| <= tol * c) holds most gaps (ties: the
    smallest c) is found; mode_s is the MEDIAN of the gaps inside that window. mode_n = gaps within +-tol of mode_s, regularity =
    mode_n / n (all gaps in the denominator), jitter_cv = std / mean of those gaps (population std; null below 3 gaps).
    mode_s / mode_n / regularity / jitter_cv are null when n < min_n."""
    t = np.asarray(t, np.float64)
    if group is None:
        g = np.diff(np.sort(t))
    else:
        gr = np.asarray(group)
        o = np.lexsort((t, gr))  # by run, then by time
        t, gr = t[o], gr[o]
        g = np.diff(t)[gr[1:] == gr[:-1]]
    n = int(g.size)
    inr = g[(g >= GAP_MIN_S) & (g < GAP_MAX_S)]
    hist = np.histogram(inr, bins=GAP_EDGES)[0] if inr.size else np.zeros(GAP_BINS, int)
    out = {
        "n": n,
        "hist": [int(h) for h in hist],
        "under_n": int((g < GAP_MIN_S).sum()),
        "over_n": int((g >= GAP_MAX_S).sum()),
        "mode_s": None,
        "mode_n": None,
        "regularity": None,
        "jitter_cv": None,
    }
    if n < min_n or inr.size == 0:
        return out
    s = np.sort(inr)
    lo = np.searchsorted(s, (1.0 - tol) * s, side="left")
    hi = np.searchsorted(s, (1.0 + tol) * s, side="right")
    best = int(np.argmax(hi - lo))  # first maximum = smallest candidate
    mode = float(np.median(s[lo[best] : hi[best]]))
    mem = g[(g >= (1.0 - tol) * mode) & (g <= (1.0 + tol) * mode)]
    out["mode_s"] = round(mode, 4)
    out["mode_n"] = int(mem.size)
    out["regularity"] = round(mem.size / n, 4)
    out["jitter_cv"] = round(float(mem.std() / mem.mean()), 4) if mem.size >= 3 else None
    return out


def event_stats(t, level_db=None, spread_db=None, excess_db=None, within3=None, nhit=None, loudest=None, analysed_s=None, group=None):
    """The interval statistics of a set of impulses as the (impulses, gaps) pair of a burst_stats record. PURE function of the arrays
    (t seconds; the others optional, same length as t; a missing one gives null in its field)."""
    t = np.asarray(t, np.float64)
    n = int(t.size)
    imp = {
        "n": n,
        "rate_per_s": _r(n / analysed_s, 4) if analysed_s else None,
        "level_db": pct3(level_db) if level_db is not None else None,
        "excess_db": pct3(excess_db) if excess_db is not None else None,
        "spread_db": pct3(spread_db) if spread_db is not None else None,
        "within3_hist": None,
        "loudest_beam_hist": None,
        "beams_hit_hist": None,
        "coincident_all_n": None,
    }
    if within3 is not None:
        imp["within3_hist"] = [int(v) for v in np.bincount(np.asarray(within3, int), minlength=NB + 1)[1 : NB + 1]]
    if loudest is not None:
        imp["loudest_beam_hist"] = [int(v) for v in np.bincount(np.asarray(loudest, int), minlength=NB + 1)[1 : NB + 1]]
    if nhit is not None:
        nh = np.asarray(nhit, int)
        imp["beams_hit_hist"] = [int(v) for v in np.bincount(nh, minlength=NB + 1)[: NB + 1]]
        imp["coincident_all_n"] = int((nh == NB).sum())
    return imp, gap_stats(t, group=group)


def _fundamental(f, p, band):
    """Fundamental of the envelope spectrum p (axis f): a pulse train has equal harmonics, so the largest bin can be the 7th. Weighted
    harmonic sum S(f0) = sum_{k=1..6, k f0 <= Nyquist} P(k f0) / k over the candidates in `band`; the LOWEST candidate whose S is at
    least 0.8 of the maximum is the fundamental (the 1/k weights keep a half-rate sub-harmonic at or below 0.5 for a pure modulation,
    about 0.4 for a pulse train). The spectrum is first max-filtered over +-2 bins so scalloping loss does not favour a line that sits on a bin."""
    grid = np.arange(MOD_BAND_HZ[0], MOD_BAND_HZ[1] + 1e-9, 0.01)  # candidates at 0.01 Hz, P read by linear interpolation
    pk = np.max([np.roll(p, d) for d in range(-2, 3)], axis=0)  # +-2 bins max: a line between two bins keeps its height
    sc = np.zeros(grid.size)
    for k in range(1, 7):
        v = np.where(k * grid <= f[-1], np.interp(k * grid, f, pk), 0.0)
        sc += v / k
    j = int(np.nonzero(sc >= 0.8 * sc.max())[0][0])
    lo, hi = max(0, j - 5), min(grid.size, j + 6)  # refine: best score within +-0.05 Hz
    return float(grid[lo + int(np.argmax(sc[lo:hi]))])


def mod_peak(runs):
    """Envelope modulation spectrum of a list of 200 Hz envelope runs (1-D arrays) -> dict(s, peak_hz, fund_hz, prom_db, rms_rel).
    Welch (Hann, MOD_NPERSEG points or the run length when shorter, 50 % overlap, mean removed) per run of at least
    MOD_MIN_RUN_S, averaged weighted by run length; peak = the largest bin in 0.3..30 Hz, prom_db = peak / median of that range; fund_hz = the fundamental
    found by _fundamental (the rate to read for a pulse train, peak_hz can be a harmonic)."""
    f0 = np.arange(0.0, ENV_FS / 2.0 + 1e-9, ENV_FS / MOD_NPERSEG)  # common axis, df 0.05 Hz
    ps, ws, tot, ss, nn = [], [], 0, 0.0, 0.0
    for r in runs:
        r = np.asarray(r, np.float64)
        if r.size < MOD_MIN_RUN_S * ENV_FS:
            continue
        npg = min(MOD_NPERSEG, r.size)
        f, p = signal.welch(r, ENV_FS, nperseg=npg, noverlap=npg // 2, detrend="constant")
        ps.append(p if npg == MOD_NPERSEG else np.interp(f0, f, p))
        ws.append(r.size)
        tot += r.size
        ss += float(((r - r.mean()) ** 2).sum())
        nn += float(r.mean()) * r.size
    out = {"s": round(tot / ENV_FS, 1), "peak_hz": None, "fund_hz": None, "prom_db": None, "rms_rel": None}
    if not ps:
        return out
    p = np.average(np.stack(ps), axis=0, weights=ws)
    m = (f0 >= MOD_BAND_HZ[0]) & (f0 <= MOD_BAND_HZ[1])
    mean = nn / tot
    if mean > 0:
        out["rms_rel"] = round(math.sqrt(ss / tot) / mean, 4)
    pm = p[m]
    med = float(np.median(pm))
    if pm.max() > 0 and med > 0:
        k = int(np.argmax(pm))
        out["peak_hz"] = round(float(f0[m][k]), 3)
        out["prom_db"] = round(10.0 * math.log10(pm[k] / med), 2)
        out["fund_hz"] = round(_fundamental(f0, p, m), 3)
    return out


class _Win:
    """What one analysed window leaves for the interval it belongs to."""

    __slots__ = ("t_end", "dead", "bands", "ref", "env", "ev")

    def __init__(self, t_end, dead=False):
        self.t_end, self.dead = t_end, dead
        self.bands = self.ref = self.env = self.ev = None


class Bursts:
    name = "bursts"
    win_s, hop_s = 1.0, 1.0
    heavy = False

    def __init__(self, interval_s=60.0, tagger=True):
        self.interval_s = float(interval_s)
        if not (math.isfinite(self.interval_s) and 0 < self.interval_s <= MAX_INTERVAL_S):
            raise ValueError(f"interval_s must be finite and in (0, {MAX_INTERVAL_S:g}] s")
        self.tagger = bool(tagger)
        self.sos = np.asarray(signal.ellip(4, 0.5, 60, [BAND_LO_HZ, BAND_HI_HZ], btype="bandpass", fs=FS, output="sos"), np.float32)
        self.zi = None
        self.last_end = None
        self.fresh = True
        self.h_tot = np.zeros(0)  # dB history of the total, last HIST_FRAMES frames
        self.h_beam = np.zeros((NB, 0))
        self.f = None
        self.lock = threading.Lock()
        self.pending = []  # _Win list, oldest first
        self.verdicts = []  # (a, b, speech_active) of tagger windows
        self.latest = None
        self.st = {
            "windows": 0,
            "errors": 0,
            "nonfinite": 0,
            "dead": 0,
            "records": 0,
            "speech": 0,
            "unvetted": 0,
            "dropped_unvetted": 0,
            "impulses": 0,
            "last_error": None,
        }

    def stats(self):
        return dict(self.st)

    # ---------------------------------------------------------------- tagger verdicts (called by the host from the tags thread)
    def note_speech(self, t_start, t_end, active):
        """One tagger verdict for the audio [t_start, t_end] (wall seconds, the same clock as process()'s t_wall)."""
        with self.lock:
            self.verdicts.append((float(t_start), float(t_end), bool(active)))
            lim = max(v[1] for v in self.verdicts) - VERDICT_KEEP_S
            self.verdicts = [v for v in self.verdicts if v[1] >= lim]

    def _vet(self, t_end, win_s):
        """(speech, vetted) for the window (t_end - win_s, t_end]: overlaps an active verdict / the verdicts TOGETHER cover the whole
        window (up to VET_TOL_S of slack at the ends and between verdicts). A verdict that touches only part of the window does not
        vet it: the rest could still turn out to be speech."""
        a = t_end - win_s
        hit = sorted((v for v in self.verdicts if v[1] > a and v[0] < t_end), key=lambda v: v[0])
        reach = a  # the window is covered up to here
        for v in hit:
            if v[0] > reach + VET_TOL_S:
                break
            reach = max(reach, v[1])
        return any(v[2] for v in hit), bool(hit) and reach >= t_end - VET_TOL_S

    # ---------------------------------------------------------------- one window
    def _setup(self, n):
        nf = n // FRAME
        self.f = (nf, np.fft.rfftfreq(SEG, 1.0 / FS))
        self.hann = np.hanning(SEG + 1)[:SEG].astype(np.float32)  # periodic Hann
        self.hann_pow = float((self.hann.astype(np.float64) ** 2).sum())
        f = self.f[1]
        self.sub = [(f >= lo) & (f < hi) for lo, hi in zip(SUB_EDGES_HZ[:-1], SUB_EDGES_HZ[1:])]
        self.refm = (f >= REF_BAND_HZ[0]) & (f < REF_BAND_HZ[1])

    def analyze(self, x, t_end, reset=False):
        """Everything process() computes for one finite (n, 8) window -> _Win; carries the filter state and the dB history."""
        n = x.shape[0]
        if self.f is None or self.f[0] != n // FRAME:
            self._setup(n)
        nf = n // FRAME
        w = _Win(t_end)
        if float(np.abs(x).max()) < DEAD_PEAK:
            w.dead = True
            self.zi = None
            return w
        xt = np.ascontiguousarray(x.T, dtype=np.float32)
        if self.zi is None or reset:
            self.zi = np.zeros((self.sos.shape[0], NB, 2), np.float32)
            mask = RESET_MASK_FRAMES
        else:
            mask = 0
        y, self.zi = signal.sosfilt(self.sos, xt, axis=-1, zi=self.zi)
        yf = y[:, : nf * FRAME].reshape(NB, nf, FRAME)
        eb = np.einsum("bfs,bfs->bf", yf, yf, dtype=np.float64) / FRAME  # (8, nf) per-beam frame energy
        et = eb.mean(axis=0)
        dbt = 10.0 * np.log10(np.maximum(et, 1e-14))
        dbb = 10.0 * np.log10(np.maximum(eb, 1e-14))
        self.h_tot = np.concatenate([self.h_tot, dbt])[-HIST_FRAMES:]
        self.h_beam = np.concatenate([self.h_beam, dbb], axis=1)[:, -HIST_FRAMES:]
        med = float(np.median(self.h_tot))
        pk = signal.find_peaks(dbt, height=max(med + THRESH_DB, ABS_FLOOR_DB), distance=MIN_GAP_MS, prominence=PROM_DB)[0]
        pk = pk[pk >= mask]
        nseg = n // SEG
        sg = x[: nseg * SEG].mean(axis=1, dtype=np.float32).reshape(nseg, SEG) * self.hann
        m = (np.abs(sfft.rfft(sg, axis=1, workers=1)) ** 2).mean(axis=0) * (2.0 / (SEG * self.hann_pow))  # power per bin: Parseval, one-sided
        sub = np.array([m[s].sum() for s in self.sub])
        w.bands = 10.0 * np.log10(np.maximum(sub, 1e-20))
        w.ref = 10.0 * math.log10(max(float(m[self.refm].sum()), 1e-20))
        step = FS // ENV_FS // FRAME  # 1 ms frames per 5 ms
        ne = (nf // step) * step
        w.env = et[:ne].reshape(-1, step).mean(axis=1)
        ev = {
            "t": np.zeros(0),
            "level": np.zeros(0),
            "excess": np.zeros(0),
            "spread": np.zeros(0),
            "within3": np.zeros(0, int),
            "nhit": np.zeros(0, int),
            "loudest": np.zeros(0, int),
        }
        if pk.size:
            medb = np.median(self.h_beam, axis=1)
            idx = pk[:, None] + np.arange(-COINC_FRAMES, COINC_FRAMES + 1)[None, :]  # (E, 3)
            lv = dbb[:, idx].max(axis=2).T  # (E, 8) per-beam level at the peak +-1 ms
            top = lv.max(axis=1)
            ev = {
                "t": t_end - (n - (pk * FRAME + FRAME / 2.0)) / FS,
                "level": dbt[pk],
                "excess": dbt[pk] - med,
                "spread": top - lv.min(axis=1),
                "within3": (lv >= top[:, None] - 3.0).sum(axis=1),
                "nhit": (lv >= medb[None, :] + THRESH_DB).sum(axis=1),
                "loudest": lv.argmax(axis=1) + 1,
            }
        w.ev = ev
        return w

    # ---------------------------------------------------------------- host interface
    def process(self, x, t_wall):
        out = []
        try:
            if not (isinstance(x, np.ndarray) and x.ndim == 2 and x.shape[1] == NB and x.shape[0] >= FRAME * 10):
                raise ValueError(f"bad window shape {getattr(x, 'shape', None)}")
            if not np.isfinite(x).all():
                self.st["nonfinite"] += 1
                self.st["errors"] += 1
                self.st["last_error"] = "non-finite samples"
                self.zi = None
                return [], None
            reset = self.last_end is None or abs((t_wall - self.last_end) - x.shape[0] / FS) > 0.25
            w = self.analyze(x, t_wall, reset)
            self.last_end = t_wall
            self.st["windows"] += 1
            if w.dead:
                self.st["dead"] += 1
            else:
                self.st["impulses"] += int(w.ev["t"].size)
            with self.lock:
                self.pending.append(w)
                self.win_s_last = x.shape[0] / FS
                self.latest = t_wall
                out = self._drain(t_wall, False)
            self.st["records"] += len(out)
        except Exception as e:  # never take the window loop down
            self.st["errors"] += 1
            self.st["last_error"] = f"{type(e).__name__}: {e}"[:200]
            self.zi = None
        return out, None

    def flush(self):
        """Close the open interval(s) at shutdown (partial: true); windows younger than the speech grace are dropped unvetted."""
        try:
            with self.lock:
                return self._drain(self.latest, True)
        except Exception as e:
            self.st["errors"] += 1
            self.st["last_error"] = f"{type(e).__name__}: {e}"[:200]
            return []

    def _bucket(self, w):
        return math.floor((w.t_end - 0.5 * getattr(self, "win_s_last", 1.0)) / self.interval_s) * self.interval_s

    def _drain(self, now, final):
        recs = []
        grace = SPEECH_GRACE_S if self.tagger else 0.0
        while self.pending:
            T = self._bucket(self.pending[0])
            if not final and now - grace < T + self.interval_s + 0.5 * getattr(self, "win_s_last", 1.0):
                break
            mine = [w for w in self.pending if self._bucket(w) == T]
            self.pending = [w for w in self.pending if self._bucket(w) != T]
            partial = final and max(w.t_end for w in mine) < T + self.interval_s - 0.25 * getattr(self, "win_s_last", 1.0)
            if final and self.tagger:
                keep = [w for w in mine if w.t_end <= now - grace]
                self.st["dropped_unvetted"] += len(mine) - len(keep)
                if len(keep) < len(mine):
                    partial = True
                mine = keep
                if not mine:
                    continue
            recs.append(self._record(T, mine, partial))
        return recs

    def _record(self, T, wins, partial):
        wl = getattr(self, "win_s_last", 1.0)
        used, n_sp, n_unv, n_dead = [], 0, 0, 0
        for w in wins:
            if self.tagger:
                sp, vet = self._vet(w.t_end, wl)
                if sp:
                    n_sp += 1
                    continue
                if not vet:
                    n_unv += 1
                    continue
            if w.dead:
                n_dead += 1
                continue
            used.append(w)
        self.st["speech"] += n_sp
        self.st["unvetted"] += n_unv
        rec = {"k": "burst_stats", "v": 1, "t0": int(T), "interval_s": self.interval_s}
        if partial or (self.t_first_late(wins, T)):
            rec["partial"] = True
        rec.update(
            {
                "tagger": self.tagger,
                "win_total": len(wins),
                "win_used": len(used),
                "win_speech": n_sp,
                "win_unvetted": n_unv,
                "win_dead": n_dead,
                "analysed_s": round(len(used) * wl, 1),
            }
        )
        if used:
            bands = np.stack([w.bands for w in used])
            rec["bands_db"] = {
                "p10": [round(float(v), 2) for v in np.percentile(bands, 10, axis=0)],
                "p50": [round(float(v), 2) for v in np.percentile(bands, 50, axis=0)],
                "p90": [round(float(v), 2) for v in np.percentile(bands, 90, axis=0)],
            }
            rec["ref_2_6k_db"] = _r(np.percentile([w.ref for w in used], 50))
        else:
            rec["bands_db"], rec["ref_2_6k_db"] = None, None
        run_of, nrun, last = [], 0, None
        for w in used:  # contiguous runs of windows (a gap or an excluded window ends a run)
            if last is not None and abs(w.t_end - last - wl) > 0.25:
                nrun += 1
            run_of.append(nrun)
            last = w.t_end
        cat = (
            {k: np.concatenate([w.ev[k] for w in used]) for k in ("t", "level", "excess", "spread", "within3", "nhit", "loudest")}
            if used
            else {k: np.zeros(0) for k in ("t", "level", "excess", "spread", "within3", "nhit", "loudest")}
        )
        grp = np.concatenate([np.full(w.ev["t"].size, r_, int) for w, r_ in zip(used, run_of)]) if used else np.zeros(0, int)
        imp, gaps = event_stats(
            cat["t"],
            cat["level"],
            cat["spread"],
            cat["excess"],
            cat["within3"].astype(int),
            cat["nhit"].astype(int),
            cat["loudest"].astype(int),
            len(used) * wl,
            group=grp,
        )
        rec["impulses"], rec["gaps"] = imp, gaps
        runs = [np.concatenate([w.env for w, r_ in zip(used, run_of) if r_ == k]) for k in range(nrun + 1)] if used else []
        rec["mod"] = mod_peak(runs)
        rec["detector"] = {
            "band_hz": [int(BAND_LO_HZ), int(BAND_HI_HZ)],
            "frame_ms": 1,
            "thresh_db": THRESH_DB,
            "median_s": MEDIAN_S,
            "min_gap_ms": MIN_GAP_MS,
            "abs_floor_db": ABS_FLOOR_DB,
        }
        return rec

    def t_first_late(self, wins, T):
        """True when the first window of the bucket starts more than one window after the bucket start (a start late in the interval)."""
        wl = getattr(self, "win_s_last", 1.0)
        return bool(wins) and min(w.t_end for w in wins) - wl > T + 1.5 * wl


def make(ctx):
    """Plugin factory named by analysis/plugins.manifest.json (ctx = manifest.Ctx)."""
    p = Bursts(ctx.args.bursts_interval_s)
    p.tagger = "tags" in ctx.started  # no tagger running: no speech verdicts will ever arrive, nothing to wait for
    return p
