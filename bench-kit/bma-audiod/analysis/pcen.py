"""Per-channel energy normalisation (PCEN), numpy only. Imported by clip_render.py (pinned in pc/install_task.ps1, docker/analyzer.Dockerfile).

    PCEN(t,f) = (E / (eps + M)**alpha + delta)**r - delta**r          M(t) = (1 - s) * M(t-1) + s * E(t)

Wang, Getreuer, Hughes, Lyon, Saurous, "Trainable frontend for robust and far-field keyword spotting", ICASSP 2017
(arXiv:1607.05666, Eq. 1-2; paper values s=0.025, alpha=0.98, delta=2, r=0.5, eps=1e-6). Matches librosa.pcen (ISC licence) for
a given smoothing coefficient b == s; verified numerically in test_pcen.py.

Layout: TIME IS AXIS 0, every other axis is an independent channel (frames x bins, like clip_render's (k, bins)); librosa uses
axis=-1 for time, so pass S.T there. E is LINEAR POWER (or magnitude, per the caller's convention) and must be >= 0: eps=1e-6 is
absolute, so the input scale matters (librosa's docs: scale int-range audio up). For the analyzer's full-scale-1.0 floats use
`scale=INT16_POWER` (= 32768**2) so quiet bins are not swamped by eps.

Initial state (the one place conventions differ): librosa 1.0.0 starts the smoother from scipy.signal.lfilter_zi([b],[1,b-1]),
which equals M(-1) = 1 for every channel, independent of the data. That is init="librosa" (the default here, to agree with the
reference). init="first" starts M(-1) = E(0) (no start-up transient, better for a short clip); init=<array> is explicit.

float64 inside and out (an exact, reproducible recursion); NaN/inf/negative input raises ValueError rather than being passed on.
"""
import math

import numpy as np

DEFAULTS = dict(s=0.025, alpha=0.98, delta=2.0, r=0.5, eps=1e-6)
INT16_POWER = 32768.0 ** 2          # power of a full-scale-1.0 float signal expressed in int16 counts squared
FLOOR_HZ = 3500.0                   # owner's visual-only floor (clip_features.FLOOR_HZ)
MIN_COL_S = 0.050                   # owner's visual-only minimum column duration
MAX_ROWS = 96


def smoothing_from_time_constant(time_constant_s, frame_rate_hz):
    """s for a smoother with the given time constant in seconds at `frame_rate_hz` frames per second.
    librosa.pcen's relation: T = time_constant * frame_rate; s = (sqrt(1 + 4 T^2) - 1) / (2 T^2), the positive root of T^2 s^2 + s - 1 = 0
    (T=40 frames gives 0.0247; the paper's 0.025 is ~39.4 frames). Always in (0, 1] for T > 0 (T -> 0 gives 1: no smoothing)."""
    if not (time_constant_s > 0 and frame_rate_hz > 0):
        raise ValueError("time_constant_s and frame_rate_hz must be > 0")
    T = float(time_constant_s) * float(frame_rate_hz)
    return (math.sqrt(1.0 + 4.0 * T * T) - 1.0) / (2.0 * T * T)


def _check_params(s, alpha, delta, r, eps):
    if not (0.0 < s <= 1.0):
        raise ValueError(f"s must be in (0, 1], got {s}")
    if alpha < 0 or delta < 0 or r < 0:
        raise ValueError("alpha, delta and r must be >= 0")
    if not eps > 0:
        raise ValueError(f"eps must be > 0, got {eps}")
    if r == 0 or delta == 0:
        raise ValueError("r == 0 and delta == 0 are not supported (the log / zero-bias limits); use r > 0 and delta > 0")


def _clean(x):
    a = np.asarray(x)
    if a.dtype.kind not in "fiu":
        raise ValueError(f"input must be real numeric, got dtype {a.dtype}")
    a = a.astype(np.float64)
    if not np.all(np.isfinite(a)):
        raise ValueError("input contains NaN or inf")
    if (a < 0).any():
        raise ValueError("input must be >= 0 (linear power / magnitude)")
    return a


class PCENStream:
    """PCEN with explicit state. feed(frames) takes shape (n, *channels), feed_one(frame) one frame (*channels); output has the input's
    shape. `state` is M(t) of the last frame (None before the first frame under init="first"); feeding any split of a sequence gives
    bit-identical output to feeding it whole."""

    def __init__(self, n_channels=None, s=DEFAULTS["s"], alpha=DEFAULTS["alpha"], delta=DEFAULTS["delta"], r=DEFAULTS["r"],
                 eps=DEFAULTS["eps"], init="librosa", scale=1.0):
        _check_params(s, alpha, delta, r, eps)
        if not scale > 0:
            raise ValueError("scale must be > 0")
        self.s, self.alpha, self.delta, self.r, self.eps, self.scale = float(s), float(alpha), float(delta), float(r), float(eps), float(scale)
        self.init = init
        self.shape = None if n_channels is None else (n_channels,) if isinstance(n_channels, int) else tuple(n_channels)
        self.M = None
        if isinstance(init, str):
            if init not in ("librosa", "first"):
                raise ValueError(f"init must be 'librosa', 'first' or an array, got {init!r}")
        else:
            m0 = np.asarray(init, np.float64)
            if self.shape is not None:
                m0 = np.broadcast_to(m0, self.shape)
            self.M = m0.copy()
            self.shape = self.M.shape
        if isinstance(init, str) and init == "librosa" and self.shape is not None:
            self.M = np.ones(self.shape)
        try:
            self._dr = self.delta ** self.r
        except OverflowError:
            raise ValueError(f"delta**r overflows (delta={self.delta}, r={self.r})") from None

    @property
    def state(self):
        return None if self.M is None else self.M.copy()

    def feed_one(self, e):
        e = _clean(e)
        with np.errstate(over="ignore"):
            e = e * self.scale
        if not np.all(np.isfinite(e)):
            raise ValueError("input times scale overflows (not finite)")
        if self.shape is None:
            self.shape = e.shape
        if e.shape != self.shape:
            raise ValueError(f"frame shape {e.shape} != stream shape {self.shape}")
        M0 = self.M
        if M0 is None:
            M0 = np.ones(self.shape) if self.init == "librosa" else e
        M = (1.0 - self.s) * M0 + self.s * e
        with np.errstate(over="ignore", invalid="ignore"):
            smooth = np.exp(-self.alpha * (math.log(self.eps) + np.log1p(M / self.eps)))     # (eps + M)**-alpha, log-space as librosa
            out = (e * smooth + self.delta) ** self.r - self._dr
        if not np.all(np.isfinite(out)):
            raise ValueError("parameters overflow the output (r or alpha too large)")
        self.M = M                          # state advances only for a frame that produced a finite output
        return out

    def feed(self, frames):
        f = _clean(frames)
        if f.ndim == 0:
            raise ValueError("frames needs a time axis (axis 0)")
        out = np.empty_like(f)
        for i in range(f.shape[0]):
            out[i] = self.feed_one(f[i])
        return out


def pcen(E, s=DEFAULTS["s"], alpha=DEFAULTS["alpha"], delta=DEFAULTS["delta"], r=DEFAULTS["r"], eps=DEFAULTS["eps"],
         init="librosa", scale=1.0, return_state=False):
    """Batch PCEN of E (time on axis 0). Same code path as PCENStream (one feed); the equality with chunked feeding is tested."""
    E = _clean(E)
    if E.ndim == 0:
        raise ValueError("E needs a time axis (axis 0)")
    st = PCENStream(E.shape[1:], s, alpha, delta, r, eps, init, scale)
    out = st.feed(E)
    return (out, st.state) if return_state else out


def fixed_point(e, s=DEFAULTS["s"], alpha=DEFAULTS["alpha"], delta=DEFAULTS["delta"], r=DEFAULTS["r"], eps=DEFAULTS["eps"], scale=1.0):
    """The output a CONSTANT input e converges to (M -> e): (e / (eps + e)**alpha + delta)**r - delta**r, e after `scale`."""
    e = float(e) * scale
    return (e / (eps + e) ** alpha + delta) ** r - delta ** r


# ---- visual-only rendering (owner 2026-10-26: no audio, floor 3.5 kHz, columns >= 50 ms, <= 96 rows) -------------------------------

def render_u8(power_rows, frame_s, vmax=4.0, scale=INT16_POWER, s=None, time_constant_s=0.4, **pcen_kw):
    """8-bit image of PCEN. power_rows: (frames, rows) LINEAR POWER already band-reduced to <= 96 log-spaced rows from >= 3.5 kHz,
    row 0 = LOWEST frequency, one frame per column of `frame_s` seconds (>= 0.050). Returns (rows, frames) uint8 with row 0 = HIGHEST
    frequency (clip_render's orientation); pixel = clip(pcen / vmax * 255). Smoothing s defaults to the time constant
    `time_constant_s` at the frame rate; init="first" unless given (a clip is short; no start-up flash). With vmax=4 a bin 10x
    above its running mean gives ~ (10+2)**.5 - 2**.5 = 2.05 -> 131 and 31.6x gives ~3.8 -> ~242 (alpha 0.98 shifts these slightly).
    This is an image only: magnitude per >= 50 ms column, no phase, nothing invertible."""
    if not frame_s >= MIN_COL_S - 1e-12:            # written so NaN is refused too
        raise ValueError(f"columns must be >= {MIN_COL_S} s (visual-only rule), got {frame_s}")
    P = _clean(power_rows)
    if P.ndim != 2:
        raise ValueError("power_rows must be 2-D (frames, rows)")
    if P.shape[1] > MAX_ROWS:
        raise ValueError(f"at most {MAX_ROWS} rows (visual-only rule), got {P.shape[1]}")
    if not vmax > 0:
        raise ValueError("vmax must be > 0")
    if s is None:
        s = smoothing_from_time_constant(time_constant_s, 1.0 / frame_s)
    pcen_kw.setdefault("init", "first")
    out = pcen(P, s=s, scale=scale, **pcen_kw)
    return np.clip(out / vmax * 255.0, 0, 255).round().astype(np.uint8).T[::-1].copy()
