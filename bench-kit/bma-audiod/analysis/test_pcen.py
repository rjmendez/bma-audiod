#!/usr/bin/env python3
"""Tests for analysis/pcen.py. The REAL module runs on synthetic fixed-seed inputs; every expected value is derived in the comments
(closed forms, not a second copy of the code under test). Script style (check(name, ok, detail), exit 1 on a failure) like
test_analysis.py.  Run:  python -I test_pcen.py [--module-dir DIR] [--cpu-only]
  --module-dir DIR   import pcen.py from DIR instead of next to this file (used by mutation_pcen.py to run mutants)
Exit codes: 0 all passed; 1 a check failed; 77 all executed checks passed but the librosa agreement test was SKIPPED (librosa absent):
77 is "skipped", NOT a pass; run it in the analyzer's audioeval venv (WSL) for the full result.

Derivations
  * constant input e: M(t) -> e, so out -> (e/(eps+e)^alpha + delta)^r - delta^r  (pcen.fixed_point, re-derived below inline).
  * init="first" (M(-1) = E(0)) and constant e: M stays e at every frame (up to 1 ulp), so every frame equals the fixed point.
  * init="librosa" (M(-1) = 1) and constant e: M(n) = e + (1 - e) * (1 - s)^(n+1) after n+1 frames.
  * step e0 -> e1 at frame k (init="first"): M(t) = e1 + (e0 - e1) * (1 - s)^(t - k + 1) for t >= k.
  * time constant: T = tc * rate; s = (sqrt(1 + 4T^2) - 1) / (2 T^2) is the positive root of T^2 s^2 + s - 1 = 0 (librosa's relation)."""
import math, os, sys, time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
_md = HERE
if "--module-dir" in sys.argv:
    _md = sys.argv[sys.argv.index("--module-dir") + 1]
sys.path.insert(0, _md)
import pcen as P  # noqa: E402

assert os.path.dirname(os.path.abspath(P.__file__)) == os.path.abspath(_md), P.__file__

fails, n_checks = [], 0
SKIPPED = []


def check(name, ok, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def raises(fn, exc, pat):
    try:
        fn()
    except exc as e:
        return pat in str(e), str(e)
    except Exception as e:  # wrong type
        return False, f"{type(e).__name__}: {e}"
    return False, "no exception"


def fp(e, s=0.025, a=0.98, d=2.0, r=0.5, eps=1e-6):
    return (e / (eps + e) ** a + d) ** r - d ** r


# ---- 1 time constant helper
s8 = P.smoothing_from_time_constant(0.4, 20.0)               # T = 8 frames
want = (math.sqrt(257.0) - 1.0) / 128.0
check("1a time constant 0.4 s at 20 frames/s -> s = (sqrt(257)-1)/128 = 0.117431", abs(s8 - want) < 1e-15 and abs(s8 - 0.117431) < 5e-7, str(s8))
for T in (0.5, 8.0, 40.0, 1000.0):
    sT = P.smoothing_from_time_constant(T, 1.0)
    check(f"1b T={T:g}: s solves T^2 s^2 + s - 1 = 0 (residual < 1e-12) and 0 < s <= 1", abs(T * T * sT * sT + sT - 1) < 1e-12 and 0 < sT <= 1, str(sT))
check("1c T=40 frames gives 0.02469 (the paper's 0.025 is ~39.4 frames)", abs(P.smoothing_from_time_constant(2.0, 20.0) - 0.0246895) < 1e-7)
ok, d = raises(lambda: P.smoothing_from_time_constant(0.0, 20.0), ValueError, "must be > 0")
check("1d time constant 0 raises ValueError 'must be > 0'", ok, d)
ok, d = raises(lambda: P.smoothing_from_time_constant(0.4, -1.0), ValueError, "must be > 0")
check("1e negative frame rate raises ValueError", ok, d)

# ---- 2 constant input -> analytic fixed point
for e in (1e-3, 1.0, 250.0, 3e6):
    out = P.pcen(np.full((300, 3), e), init="first")
    check(f"2a constant {e:g}, init=first: EVERY frame equals the fixed point (E/(eps+E)^a + 2)^.5 - 2^.5 within 1e-12",
          np.abs(out - fp(e)).max() < 1e-12 and abs(P.fixed_point(e) - fp(e)) < 1e-15, f"{np.abs(out - fp(e)).max():.3e}")
e = 250.0
out = P.pcen(np.full((2000, 2), e), init="librosa")
n = np.arange(2000)
M = e + (1.0 - e) * (1 - 0.025) ** (n + 1)
want = (e / (1e-6 + M) ** 0.98 + 2.0) ** 0.5 - 2.0 ** 0.5
check("2b init=librosa constant 250: frame n equals the closed form with M(n) = e + (1-e)(1-s)^(n+1) within 1e-12", np.abs(out[:, 0] - want).max() < 1e-12, f"{np.abs(out[:, 0] - want).max():.3e}")
check("2c ... and converged to the fixed point by frame 2000 within 1e-9", np.abs(out[-1] - fp(e)).max() < 1e-9, str(out[-1]))
check("2d start-up transient is real: init=librosa frame 0 differs from init=first frame 0 by > 0.1 (positive twin of 2b/2a)",
      abs(out[0, 0] - fp(e)) > 0.1, f"{out[0, 0]} vs {fp(e)}")
check("2e frame 0 of init=librosa is 4.7500 (M0 = 0.975*1 + 0.025*250 = 7.225; (250/7.225^0.98 + 2)^.5 - 2^.5), hand-computed constant",
      abs(out[0, 0] - 4.75004) < 1e-5, str(out[0, 0]))

# ---- 3 step response
e0, e1, k, s = 4.0, 400.0, 30, 0.1
E = np.full((80, 1), e0)
E[k:] = e1
out = P.pcen(E, s=s, init="first")[:, 0]
t = np.arange(k, 80)
M = e1 + (e0 - e1) * (1 - s) ** (t - k + 1)
want = (e1 / (1e-6 + M) ** 0.98 + 2.0) ** 0.5 - 2.0 ** 0.5
check("3a step 4 -> 400 at frame 30 (s=0.1): frames >= 30 equal the closed form within 1e-12", np.abs(out[k:] - want).max() < 1e-12, f"{np.abs(out[k:] - want).max():.3e}")
check("3b frames before the step sit on the fixed point of 4", np.abs(out[:k] - fp(4.0, s=s)).max() < 1e-12)
check("3c the step overshoots its new fixed point (AGC lag): peak at the step frame > fixed point of 400 + 1.0, then decays monotonically",
      out[k] > fp(400.0) + 1.0 and np.all(np.diff(out[k:]) < 0), f"{out[k]:.4f} vs {fp(400.0):.4f}")

# ---- 4 streaming == batch, any chunk split, bit-identical
rng = np.random.default_rng(7)
X = rng.gamma(0.5, 300.0, size=(257, 6))
for init in ("librosa", "first"):
    ref = P.pcen(X, init=init)
    ok_all = True
    for trial in range(25):
        cuts = np.unique(np.concatenate([[0, len(X)], rng.integers(0, len(X) + 1, size=rng.integers(0, 12))]))
        st = P.PCENStream(6, init=init)
        got = np.concatenate([st.feed(X[a:b]) for a, b in zip(cuts[:-1], cuts[1:])])
        ok_all &= np.array_equal(got, ref)
    check(f"4a init={init}: 25 random chunk splits (incl. empty chunks) give bit-identical output to the batch", ok_all)
    st = P.PCENStream(6, init=init)
    got = np.stack([st.feed_one(x) for x in X])
    check(f"4b init={init}: one frame at a time is bit-identical to the batch", np.array_equal(got, ref))
    out_a, state = P.pcen(X[:100], init=init, return_state=True)
    st = P.PCENStream(6, init=state)
    check(f"4c init={init}: batch state handed to a new stream (init=array) continues bit-identically",
          np.array_equal(np.concatenate([out_a, st.feed(X[100:])]), ref))
check("4d the splits really are different from a wrong stream: a fresh stream per chunk does NOT equal the batch (positive twin)",
      not np.array_equal(np.concatenate([P.pcen(X[:100]), P.pcen(X[100:])]), P.pcen(X)))
st = P.PCENStream(init="librosa")                       # channel count unknown until the first frame
got = np.concatenate([st.feed(X[:7]), st.feed(X[7:])])
check("4f a stream built WITHOUT n_channels (init=librosa) still starts from M(-1)=1: bit-identical to the batch", np.array_equal(got, P.pcen(X)))
st = P.PCENStream(2, init="first")
check("4e state is None before the first frame under init=first, M after", st.state is None and st.feed_one([1.0, 2.0]) is not None and np.allclose(st.state, [1.0, 2.0]))

# ---- 5 dtype / NaN handling
base = rng.uniform(0, 100, size=(20, 4))
ref64 = P.pcen(base)
for dt in (np.float32, np.float64, np.int32, np.uint8, np.int64):
    x = base.astype(dt) if dt in (np.float32, np.float64) else np.round(base).astype(dt)
    out = P.pcen(x)
    check(f"5a input dtype {np.dtype(dt).name} accepted; output float64 equal to pcen of the same values cast to float64",
          out.dtype == np.float64 and np.array_equal(out, P.pcen(x.astype(np.float64))))
for name, bad, pat in (("NaN", np.where(np.arange(20)[:, None] == 5, np.nan, base), "NaN or inf"),
                       ("inf", np.where(np.arange(20)[:, None] == 5, np.inf, base), "NaN or inf"),
                       ("negative", np.where(np.arange(20)[:, None] == 5, -1.0, base), ">= 0"),
                       ("complex", base.astype(complex), "real numeric"),
                       ("string", np.array([["a"]]), "real numeric")):
    ok, d = raises(lambda b=bad: P.pcen(b), ValueError, pat)
    check(f"5b {name} input raises ValueError matching {pat!r}", ok, d)
ok, d = raises(lambda: P.PCENStream(4).feed(np.where(np.arange(20)[:, None] == 5, np.nan, base)), ValueError, "NaN or inf")
check("5c a NaN chunk raises in the stream too",
      ok, d)
st = P.PCENStream(4)
try:
    st.feed(np.where(np.arange(20)[:, None] == 5, np.nan, base))
except ValueError:
    pass
check("5d after the rejected chunk the stream state is finite (nothing partially applied)", np.all(np.isfinite(st.state)))
check("5e zeros are fine: pcen(0) = (0 + 2)^.5 - 2^.5 = 0 exactly, no NaN", np.array_equal(P.pcen(np.zeros((5, 2))), np.zeros((5, 2))))
check("5f empty time axis returns an empty array of the right shape", P.pcen(np.zeros((0, 3))).shape == (0, 3))
for kw, pat in ((dict(s=0.0), "s must be"), (dict(s=1.5), "s must be"), (dict(eps=0.0), "eps must be"), (dict(alpha=-1.0), ">= 0"), (dict(delta=0.0), "not supported")):
    ok, d = raises(lambda k=kw: P.pcen(base, **k), ValueError, pat)
    check(f"5g invalid parameter {kw} raises ValueError matching {pat!r}", ok, d)
check("5h boundary parameters s=1.0 and s=0.001 are accepted (positive twin of 5g)", P.pcen(base, s=1.0).shape == base.shape and P.pcen(base, s=0.001).shape == base.shape)
ok, d = raises(lambda: P.PCENStream(3).feed_one([1.0, 2.0]), ValueError, "frame shape")
check("5i a frame of the wrong width raises ValueError", ok, d)
check("5j scale multiplies the input: pcen(E, scale=4) == pcen(4E) bit-identically", np.array_equal(P.pcen(base, scale=4.0), P.pcen(base * 4.0)))

# ---- 6 librosa agreement
try:
    import librosa
    import scipy  # noqa: F401
    have_librosa = True
except Exception as ex:                                     # ImportError or a broken install
    have_librosa = False
    SKIPPED.append("librosa agreement (6): librosa not importable here (" + type(ex).__name__ + "); run in the audioeval venv")
    print("SKIP 6 librosa agreement: " + SKIPPED[-1] + "  [exit code 77 = skipped, not a pass]")
if have_librosa:
    rng = np.random.default_rng(11)
    cases = {
        "random gamma": rng.gamma(0.5, 300.0, size=(300, 40)),
        "random uniform small": rng.uniform(0, 1e-3, size=(120, 17)),
        "step": np.where(np.arange(150)[:, None] < 60, 5.0, 5000.0) * np.ones((150, 8)),
        "sparse spikes": np.where(rng.random((200, 12)) < 0.05, 1e4, 0.5),
        "all zero then tone": np.concatenate([np.zeros((50, 5)), np.full((50, 5), 7.0)]),
        "huge dynamic range": 10.0 ** rng.uniform(-8, 9, size=(100, 9)),
        "single frame": rng.uniform(0, 10, size=(1, 6)),
        "1-D (one channel)": rng.gamma(1.0, 50.0, size=90),
    }
    for name, X in cases.items():
        for kw in (dict(), dict(s=0.1176, alpha=0.8, delta=1.0, r=0.3, eps=1e-3), dict(s=0.5, alpha=0.0, delta=2.0, r=0.5, eps=1e-6)):
            kw2 = dict(P.DEFAULTS, **kw)
            mine = P.pcen(X, init="librosa", **kw2)
            Xl = X.T if X.ndim == 2 else X
            theirs = librosa.pcen(Xl, sr=22050, hop_length=512, gain=kw2["alpha"], bias=kw2["delta"], power=kw2["r"], eps=kw2["eps"], b=kw2["s"], axis=-1)
            theirs = theirs.T if X.ndim == 2 else theirs
            err = float(np.abs(mine - theirs).max())
            check(f"6a librosa {librosa.__version__} agreement [{name}, {kw or 'paper defaults'}]: max |diff| < 1e-6 (init=librosa)", err < 1e-6, f"{err:.3e}")
    X = cases["random gamma"]
    # the time-constant route: librosa derives b from time_constant, sr, hop; our helper must give the same b
    for tc, sr, hop in ((0.4, 22050, 512), (0.1, 48000, 2400), (2.0, 20, 1)):
        b = P.smoothing_from_time_constant(tc, sr / hop)
        mine = P.pcen(X, s=b, init="librosa")
        theirs = librosa.pcen(X.T, sr=sr, hop_length=hop, time_constant=tc, gain=0.98, bias=2, power=0.5, eps=1e-6, axis=-1).T
        err = float(np.abs(mine - theirs).max())
        check(f"6b time-constant route tc={tc} sr={sr} hop={hop}: helper s reproduces librosa.pcen(time_constant=...) within 1e-6", err < 1e-6, f"{err:.3e}")
    # the initial-state convention is what is being matched: librosa's output is NOT the init=first output
    mine_first = P.pcen(X, init="first")
    theirs = librosa.pcen(X.T, sr=22050, hop_length=512, b=0.025, axis=-1).T
    check("6c positive twin: init=first differs from librosa by > 1e-3 at frame 0 (so 6a really tests the initial-state convention)",
          float(np.abs(mine_first[0] - theirs[0]).max()) > 1e-3, str(float(np.abs(mine_first[0] - theirs[0]).max())))
    # librosa with an explicit zi = first frame (its own way to start from the data) equals init=first
    theirs_f, _ = librosa.pcen(X.T, sr=22050, hop_length=512, b=0.025, axis=-1, zi=(1 - 0.025) * X[0:1].T, return_zf=True)
    err = float(np.abs(theirs_f.T - mine_first).max())
    check("6d librosa started from M(-1)=E(0) (zi given) equals init=first within 1e-6", err < 1e-6, f"{err:.3e}")

# ---- 7 synthetic masking demonstration (numbers reported)
RATE = 20.0                                                # 50 ms columns
rng = np.random.default_rng(3)
n_fr = 200
bg = rng.gamma(50.0, 100.0 / 50.0, size=n_fr)                  # 4 kHz channel: noise, mean power 100
call_fr = slice(100, 104)                                    # a 200 ms call, +20 dB over the noise mean
bg_call = bg.copy()
bg_call[call_fr] += 10000.0
tone = np.full(n_fr, 1.0e6)                                   # steady 8 kHz tone, +40 dB over the noise
Ein = np.stack([bg_call, tone], axis=1)                       # channels: [4 kHz, 8 kHz]
s_ = P.smoothing_from_time_constant(0.4, RATE)
out = P.pcen(Ein, s=s_, init="first")
call_peak, tone_out = float(out[call_fr, 0].max()), float(out[150:, 1].mean())
raw_ratio = float(Ein[call_fr, 0].max() / Ein[150:, 1].mean())
pc_ratio = call_peak / tone_out
quiet = float(np.median(out[20:90, 0]))
print(f"  numbers: raw call/tone power {raw_ratio:.4f} ({10 * math.log10(raw_ratio):.1f} dB); PCEN call peak {call_peak:.3f}, tone {tone_out:.3f} "
      f"(fixed point {fp(1e6, s=s_):.3f}), call/tone {pc_ratio:.2f}x; PCEN 4 kHz noise median {quiet:.3f}")
check("7a steady 8 kHz tone is suppressed to its fixed point (E/(eps+E)^a+2)^.5-2^.5 = 0.407 (+-0.001), whatever its raw level",
      abs(tone_out - fp(1e6)) < 1e-9 and abs(tone_out - 0.407) < 1e-3, str(tone_out))
check("7b raw: the call is 20 dB BELOW the tone (call/tone power ~0.01)", raw_ratio < 0.0102 and raw_ratio > 0.0100, str(raw_ratio))
check("7c PCEN: the call peak exceeds the suppressed tone by > 3x (contrast rose from 0.01x to this)", pc_ratio > 3.0, str(pc_ratio))
tone_hi = np.stack([bg_call, np.full(n_fr, 1.0e8)], axis=1)
out_hi = P.pcen(tone_hi, s=s_, init="first")
check("7d the tone's PCEN level is nearly level-independent: +20 dB louder tone moves its output by < 0.1 (positive twin: the call, which is NOT steady, is unchanged bit-for-bit)",
      abs(float(out_hi[150:, 1].mean()) - tone_out) < 0.1 and np.array_equal(out_hi[:, 0], out[:, 0]),
      f"{float(out_hi[150:, 1].mean()):.3f}")
check("7e the call is visible against its own channel's noise: PCEN call peak > 5x the 4 kHz noise median (not erased by the AGC)", call_peak > 5 * quiet, f"{call_peak:.3f} vs {quiet:.3f}")
# the same call but a steady tone IN the call channel: the tone is flattened, the call stays
img = P.render_u8(np.maximum(Ein / P.INT16_POWER, 0.0), 0.05)
check("7f render_u8 of the same input: the call's pixel (row of the 4 kHz channel) is brighter than the 8 kHz tone's pixel (positive: the raw dB image has the tone 20 dB above)",
      int(img[1, 100:104].max()) > int(img[0, 150:].max()) + 30 and img.shape == (2, n_fr), f"{img[1, 100:104].max()} vs {img[0, 150:].max()}")

# ---- 8 render_u8 contract
fr = np.full((40, 5), 100.0 / P.INT16_POWER)
img = P.render_u8(fr, 0.05, vmax=4.0, time_constant_s=0.4)      # default init (first): no start-up flash
want_px = int(round(max(0.0, min(255.0, P.fixed_point(100.0) / 4.0 * 255.0))))
check("8a constant input renders to the fixed-point pixel everywhere (exact value from fixed_point), dtype uint8, shape (rows, frames)",
      img.dtype == np.uint8 and img.shape == (5, 40) and np.all(img == want_px) and want_px > 0, f"{np.unique(img)} vs {want_px}")
ramp = np.zeros((40, 3))
ramp[30:, 2] = 1e-3
img = P.render_u8(ramp, 0.05)
check("8b row 0 is the HIGHEST frequency: the input's last column (highest) lights up row 0 only, rows 1-2 stay 0",
      img[0, 30:].max() > 100 and img[1:, :].max() == 0, f"{img[:, 30:].max(axis=1)}")
ok, d = raises(lambda: P.render_u8(np.ones((10, 5)), 0.049), ValueError, "columns must be >= 0.05")
check("8c a 49 ms column is refused (visual-only rule)", ok, d)
check("8d positive twin: exactly 50 ms columns are accepted", P.render_u8(np.ones((10, 5)), 0.05).shape == (5, 10))
ok, d = raises(lambda: P.render_u8(np.ones((10, 97)), 0.05), ValueError, "at most 96 rows")
check("8e 97 rows are refused (visual-only rule)", ok, d)
check("8f positive twin: exactly 96 rows are accepted", P.render_u8(np.ones((10, 96)), 0.05).shape == (96, 10))
ok, d = raises(lambda: P.render_u8(np.full((10, 5), np.nan), 0.05), ValueError, "NaN or inf")
check("8g NaN power refused", ok, d)
sat = P.render_u8(np.full((30, 2), 1.0), 0.05, init="librosa", vmax=0.5)
nosat = P.render_u8(np.full((30, 2), 1.0), 0.05, init="librosa", vmax=4.0)
check("8h values above vmax saturate to 255 and never wrap (vmax=0.5), while vmax=4 on the same input gives 151 (< 255: positive twin)",
      int(sat[0, 0]) == 255 and int(nosat[0, 0]) == 151, f"{sat[0, 0]} {nosat[0, 0]}")
check("8i render has no information below its inputs' rows: output rows == input rows (no extra frequency detail invented)", P.render_u8(np.ones((7, 11)), 0.05).shape[0] == 11)

# ---- 10 overflow / NaN guards (reviewer findings): finite input that becomes non-finite is refused, never passed on
ok, d = raises(lambda: P.pcen(np.array([[1e300, 1.0]]), scale=P.INT16_POWER), ValueError, "input times scale overflows")
check("10a finite 1e300 times scale 2**30 overflows: ValueError 'input times scale overflows' (not a NaN output)", ok, d)
ok, d = raises(lambda: P.PCENStream(1, scale=1e9).feed_one(np.array([1e300])), ValueError, "input times scale overflows")
check("10b the same through PCENStream.feed_one with scale=1e9", ok, d)
kw = dict(init="first", r=300.0, delta=1.0)               # a frame of 1e200 overflows the output; a frame of 1.0 does not
st = P.PCENStream(1, **kw)
ok, d = raises(lambda: st.feed_one(np.array([1e200])), ValueError, "parameters overflow the output")
check("10c0 the first frame overflows the output and is refused", ok, d)
good, fresh = st.feed_one(np.array([1.0])), P.PCENStream(1, **kw).feed_one(np.array([1.0]))
check("10c a refused frame leaves the stream state untouched: the next good frame equals a fresh stream's output (exact) and is finite",
      np.array_equal(good, fresh) and bool(np.all(np.isfinite(good))) and float(good[0]) > 1e90, str(good))
big = P.pcen(np.full((50, 1), 1e300), init="first")
check("10d positive twin: 1e300 with scale 1 is finite and every frame equals the fixed point (rel 1e-12)",
      bool(np.all(np.abs(big / P.fixed_point(1e300) - 1.0) < 1e-12)), str(big[:2]))
ok, d = raises(lambda: P.pcen(np.ones((3, 2)), r=1e6), ValueError, "delta**r overflows")
check("10e r=1e6 raises ValueError 'delta**r overflows' (not OverflowError)", ok, d)
ok, d = raises(lambda: P.pcen(np.array([[1e200, 1.0]]), r=300.0, delta=1.0), ValueError, "parameters overflow the output")
check("10f r=300 on 1e200 overflows the output: ValueError 'parameters overflow the output'", ok, d)
check("10g positive twin: r=0.5 on the same input is finite", bool(np.all(np.isfinite(P.pcen(np.array([[1e200, 1.0]]), r=0.5, delta=1.0)))))
ok, d = raises(lambda: P.render_u8(np.ones((10, 4)), float("nan"), s=0.1), ValueError, "columns must be >= 0.05")
check("10h NaN frame_s with s given explicitly is refused by the 50 ms rule", ok, d)
check("10i positive twin: 50 ms with s given explicitly is accepted, shape (4, 10)", P.render_u8(np.ones((10, 4)), 0.05, s=0.1).shape == (4, 10))

# ---- 9 CPU cost
if True:
    rng = np.random.default_rng(5)
    frames = rng.gamma(1.0, 1e-8, size=(240, 96))
    best = 1e9
    for _ in range(5):
        t0 = time.perf_counter()
        P.pcen(frames, scale=P.INT16_POWER)
        best = min(best, time.perf_counter() - t0)
    per_frame_us = best / 240 * 1e6
    print(f"  cpu: {per_frame_us:.1f} us per 96-channel frame; a 12 s clip (240 columns) takes {best * 1000:.2f} ms")
    check("9 CPU: a 96-row frame costs < 200 us (a whole 12 s clip < 50 ms), far below any analyzer budget", per_frame_us < 200.0, f"{per_frame_us:.1f} us")

print(f"\n{n_checks - len(fails)}/{n_checks} checks passed" + (f"; SKIPPED: {SKIPPED}" if SKIPPED else ""))
if fails:
    print("FAILED: " + "; ".join(fails))
    sys.exit(1)
if SKIPPED:
    print("EXIT 77: skipped checks above (librosa absent); not a full pass")
    sys.exit(77)
