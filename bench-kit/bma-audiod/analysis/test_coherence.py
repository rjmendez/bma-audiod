#!/usr/bin/env python3
"""Tests for the coherence plugin (analysis/coherence.py), the report (coherence_report.py) and the beam-coherence contract.
All signals are synthetic with fixed seeds; the REAL Coherence class runs on them (nothing is stubbed). Expected values are
derived in the comments. Script style (check(name, ok, detail), exit 1 on a failure) like test_analysis.py. Temp files only
under tempfile (set TMPDIR / TEMP to redirect). Run:  python -I test_coherence.py
The schema checks need `jsonschema` (skipped with the reason printed when it is absent, as in the analyzer's WSL venv).

Derivations used below
  * K = (48000 - 4096) // 2048 + 1 = 22 Hann segments at 50 % overlap; overlap correlation of Hann segments rho = 1/6, so
    K_eff = K / (1 + 2 rho^2) = 20.84 and independent signals have mean raw MSC 1/K_eff = 0.048 (measured in test 2).
  * mixture: common source s (power Ps) plus independent noise n_i (power Pn) in both channels, r = Ps / Pn:
    Sxy = Ps, Sxx = Syy = Ps + Pn  ->  MSC = Ps^2 / (Ps + Pn)^2 = r^2 / (1 + r)^2   (r = 1: 0.25, r = 3: 0.5625, r = 0.3: 0.0533).
    With K_eff segments the raw estimate is  MSC + (1 - MSC)^2 / K_eff  (small-bias expansion).
  * pure delay d of beam j relative to beam i: cross-phase = 360 * f * d / 48000 degrees (positive: j lags i), lag = +d samples."""
import json, math, os, re, shutil, socket, struct, subprocess, sys, tempfile, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
BMA = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(BMA, "contracts", "tools"))
import coherence as C  # noqa: E402
import coherence_report as R  # noqa: E402

fails, n_checks = [], 0
FS, N = 48000, 48000
HOP = C.Coherence.hop_s


def check(name, ok, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  [{detail}]"))
    if not ok:
        fails.append(name)


def db(a):
    return 10 ** (a / 20.0)


def noise(rng, level_db=-60.0, n=N):
    """8 independent white-noise channels at level_db dBFS rms."""
    return (rng.standard_normal((n, 8)) * db(level_db)).astype(np.float32)


def common(rng, level_db=-60.0):
    """the same white noise on all 8 beams"""
    return np.repeat((rng.standard_normal((N, 1)) * db(level_db)).astype(np.float32), 8, axis=1)


def mixture(rng, r, total_db=-60.0):
    """common source + independent noise per beam; per-channel power ratio r = Ps / Pn, per-channel total power at total_db"""
    ps, pn = r / (1 + r), 1 / (1 + r)
    s = rng.standard_normal((N, 1))
    return ((np.sqrt(ps) * s + np.sqrt(pn) * rng.standard_normal((N, 8))) * db(total_db)).astype(np.float32)


def delayed_chain(rng, d, level_db=-60.0):
    """beam c = base noise delayed by c * d samples (circular FFT delay, exact for fractions) -> pair p (c, c+1) has lag +d for
    p = 0..6 and pair 7 (beam 7 -> beam 0) has lag -7 d."""
    L = N + 256
    z = rng.standard_normal(L)
    Z = np.fft.rfft(z)
    f = np.arange(len(Z)) / L
    cols = [np.fft.irfft(Z * np.exp(-2j * np.pi * f * (c * d)), n=L)[128:128 + N] for c in range(8)]
    return (np.stack(cols, axis=1) * db(level_db)).astype(np.float32)


def run(windows, interval=60.0, mask=None, tol=None, t0=0.0):
    """Feed windows (iterable of arrays) every HOP seconds to a fresh real Coherence, then one sentinel window in the next
    bucket so the last bucket closes. -> (plugin, records)."""
    p = C.Coherence(mask, tol, interval)
    recs, i = [], 0
    for i, x in enumerate(windows):
        r, e = p.process(x, t0 + i * HOP)
        assert e is None
        recs += r
    t_next = (math.floor((t0 + i * HOP) / interval) + 1) * interval
    r, _ = p.process(np.zeros((N, 8), np.float32), t_next)           # an all-zero (dead) window past the boundary
    return p, recs + r


def cells(recs, band, cond):
    return [r["bands"][band][cond] for r in recs if r["bands"][band][cond] is not None]


def pooled(recs, band, cond, key):
    """n-weighted mean over the records of a per-pair array -> (8,) ndarray; None when there is no cell"""
    cs = cells(recs, band, cond)
    if not cs:
        return None
    return sum(np.array(c[key]) * c["n"] for c in cs) / sum(c["n"] for c in cs)


def wrap(a):
    return (np.asarray(a) + 180.0) % 360.0 - 180.0


tmp = tempfile.mkdtemp(prefix="coherence_test_")
try:
    # ------------------------------------------------------------------------------------------------ constants
    check("constants: 22 segments, K_eff 20.84, floor 0.048, df 11.72 Hz", (C.K_SEG, round(C.K_EFF, 2), round(C.MSC_FLOOR, 3), C.DF) == (22, 20.84, 0.048, 11.71875),
          (C.K_SEG, C.K_EFF, C.MSC_FLOOR))
    bb = json.load(open(os.path.join(BMA, "contracts", "schemas", "beam_bands.schema.json"), encoding="utf-8"))
    check("band edges equal the beam_bands contract's edges_hz", [list(e) for e in C.BANDS_HZ] == bb["$defs"]["beam_bands"]["properties"]["edges_hz"]["const"])
    p0 = C.Coherence()
    check("band bin ranges: 50-125 Hz holds exactly 6 bins, 8-16 kHz holds 683", (p0.rng[0][1] - p0.rng[0][0], p0.rng[7][1] - p0.rng[7][0]) == (6, 683), p0.rng)

    # ------------------------------------------------------------------------------------------------ 1 identical noise
    rng = np.random.default_rng(11)
    wins = [common(rng) for _ in range(100)]
    p, recs = run(wins)
    ok = True
    for b in range(8):
        c = pooled(recs, b, "quiet", "msc_p50")
        ok &= c is not None and c.min() >= 0.999
    check("1 identical noise on 8 beams: median MSC >= 0.999 in all 8 bands and 8 pairs", ok)
    worst = lambda key: max(np.abs(np.array(c[key])).max() for b in range(8) for c in cells(recs, b, "quiet"))
    check("1 identical noise: phase within 1 deg, lag within 0.01 sample, level difference within 0.01 dB",
          worst("ph_deg") <= 1.0 and worst("lag_p50") <= 0.01 and worst("dl_db_p50") <= 0.01, (worst("ph_deg"), worst("lag_p50"), worst("dl_db_p50")))
    check("1 identical noise: phase spread under 1 deg and xc_lag 0", worst("ph_spread_deg") <= 1.0 and max(abs(v) for r in recs if r["xc_lag"] for v in r["xc_lag"]) < 0.01)

    # ------------------------------------------------------------------------------------------------ 2 independent noise = the bias floor
    rng = np.random.default_rng(12)
    wins = [noise(rng) for _ in range(100)]
    p, recs = run(wins)
    dev = []
    for b in range(8):
        dev.append(abs(float(pooled(recs, b, "quiet", "msc_p50").mean()) - C.MSC_FLOOR))
    check("2 independent noise: median MSC of every band within 0.012 of the documented floor 1/K_eff = 0.048 (positive twin: test 1 gives 1.0)",
          max(dev) < 0.012, [round(d, 4) for d in dev])
    check("2 independent noise: p90 MSC below 0.12 in every band and pair",
          all(np.array(c["msc_p90"]).max() < 0.12 for b in range(8) for c in cells(recs, b, "quiet")))
    pm = C.Coherence()
    msc_mc = []
    for _ in range(40):                                                                      # Monte-Carlo of the estimator itself
        w = pm.analyze(noise(rng))
        msc_mc.append(w.msc_all[:, 7].mean())                                                # 8 pairs x 683 bins per window
    mc = float(np.mean(msc_mc))
    check("2 Monte-Carlo of the real estimator: mean MSC of independent noise = 1/K_eff within 0.003 (so K_eff = 20.8 +- 1.4)",
          abs(mc - C.MSC_FLOOR) < 0.003 and 19.4 < 1 / mc < 22.3, (mc, 1 / mc))
    cor = lambda m: R.correct(m, C.MSC_FLOOR)
    check("2 bias correction: corrected independent-noise MSC is 0.00 +- 0.005 and corrected 1.0 stays 1.0",
          abs(cor(mc)) < 0.005 and abs(cor(1.0) - 1.0) < 1e-12, (cor(mc),))

    # ------------------------------------------------------------------------------------------------ 3 delays
    res = {}
    for d in (3.0, 2.5, -3.0, 0.0):
        rng = np.random.default_rng(13)
        p, recs = run([delayed_chain(rng, d) for _ in range(100)])
        res[d] = recs
        exp = np.array([d] * 7 + [-7 * d])
        lag_err = ph_err = 0.0
        for b in range(8):
            lag = pooled(recs, b, "quiet", "lag_p50")
            ph = pooled(recs, b, "quiet", "ph_deg")
            lag_err = max(lag_err, (np.abs(lag - exp) - 0.03 * np.abs(exp)).max())
            ph_err = max(ph_err, np.abs(wrap(ph - 360.0 * recs[2]["centre_hz"][b] * exp / FS)).max())
        check(f"3 delay d={d}: phase-slope lag equals d (and -7d on the wrap-around pair) within 0.02 sample + 3 % of the lag (finite-K bias of about 2 %)",
              lag_err <= 0.02, lag_err)
        check(f"3 delay d={d}: cross-phase equals 360*fc*lag/48000 deg at every band centre within 3 deg", ph_err <= 3.0, ph_err)
        xc = np.array([r["xc_lag"] for r in recs if r["xc_lag"]]).mean(axis=0)
        check(f"3 delay d={d}: cross-correlation peak lag agrees with the true lag within 0.05 sample", np.abs(xc - exp).max() <= 0.05, np.round(xc, 3))
    check("3 sign: d=+3 and d=-3 give opposite-sign lags, and zero delay gives zero (positive twin)",
          pooled(res[3.0], 3, "quiet", "lag_p50")[0] > 2.9 and pooled(res[-3.0], 3, "quiet", "lag_p50")[0] < -2.9 and abs(pooled(res[0.0], 3, "quiet", "lag_p50")[0]) < 0.01)

    # ------------------------------------------------------------------------------------------------ 4 known mixture
    for r in (1.0, 3.0, 0.3):
        rng = np.random.default_rng(14)
        p, recs = run([mixture(rng, r) for _ in range(100)])
        m = r * r / (1 + r) ** 2
        want = m + (1 - m) ** 2 / C.K_EFF
        got = [float(pooled(recs, b, "quiet", "msc_p50").mean()) for b in range(8)]
        check(f"4 mixture r={r}: raw MSC = MSC + (1-MSC)^2/K_eff = {want:.4f} within 0.015 in all 8 bands", max(abs(g - want) for g in got) < 0.015, [round(g, 4) for g in got])
        check(f"4 mixture r={r}: bias-corrected MSC equals the analytic r^2/(1+r)^2 = {m:.4f} within 0.008",
              max(abs(R.correct(g, C.MSC_FLOOR) - m) for g in got) < 0.008, [round(R.correct(g, C.MSC_FLOOR), 4) for g in got])

    # ------------------------------------------------------------------------------------------------ 5 gating
    def counts(p):
        s = p.stats()
        return s["gated"], s["dead"], s["used"]
    rng = np.random.default_rng(15)
    base = [mixture(rng, 1.0) for _ in range(20)]
    muted = [x.copy() for x in base]
    for x in muted:
        x[:, 5] *= 1e-5                                                                       # -100 dB
    p, recs = run(muted)
    check("5 muted beam (-100 dB): every window counted gated, none used, no cell holds a muted window",
          (counts(p)[0], counts(p)[2]) == (20, 0) and sum(r["win_gated"] for r in recs) == 20 and sum(r["win_used"] for r in recs) == 0
          and all(c is None for r in recs for bd in r["bands"] for c in (bd["quiet"], bd["active"])), (counts(p), [r["win_gated"] for r in recs]))
    p, recs = run(base)
    check("5 positive twin: the same signals unmuted are not gated and all used", counts(p) == (0, 1, 20) and sum(r["win_used"] for r in recs) == 20, counts(p))
    part = [x.copy() for x in base]
    for x in part:
        x[12000:24000, 2] = 0.0                                                               # beam 2 silent for exactly one 250 ms sub-block
    p, _ = run(part)
    check("5 partial gating: a beam muted for one 250 ms sub-block gates the window; (positive twin above: unmuted is used)", counts(p)[0] == 20, counts(p))
    short = [x.copy() for x in base]
    for x in short:
        x[12000:12100, 2] = 0.0                                                               # 100 samples (2 ms) of silence only
    p, _ = run(short)
    check("5 a 2 ms dropout inside a 250 ms block does not gate (block level drops 0.04 dB, not 40)", counts(p)[0] == 0 and counts(p)[2] == 20, counts(p))
    unit = common(np.random.default_rng(16))
    for lvl, gated in ((-39.0, False), (-41.0, True)):
        x = unit.copy()
        x[:, 4] *= db(lvl)
        p, _ = run([x])
        check(f"5 boundary: a beam exactly {-lvl:.0f} dB below the loudest is {'gated' if gated else 'used'} (GATE_DB = 40)", (p.stats()["gated"], p.stats()["used"]) == ((1, 0) if gated else (0, 1)), p.stats())
    p, recs = run([np.zeros((N, 8), np.float32)] * 3)
    check("5 all-zero windows are dead, not gated, not used", (p.stats()["dead"] - 1, p.stats()["gated"], p.stats()["used"]) == (3, 0, 0), p.stats())
    z = unit.copy()
    z[24000:36000, :] = 0.0
    p, _ = run([z])
    check("5 one all-zero 250 ms sub-block (lost audio) makes the window dead, positive twin: the unmuted window is used",
          (p.stats()["dead"] - 1, p.stats()["gated"], p.stats()["used"]) == (1, 0, 0) and run([unit])[0].stats()["used"] == 1, p.stats())

    # ------------------------------------------------------------------------------------------------ 6 quiet vs active
    rng = np.random.default_rng(17)
    wins = [mixture(rng, 1.0, -66.0) for _ in range(100)] + [mixture(rng, 1.0, -54.0) for _ in range(100)]
    p, recs = run(wins)
    tot = lambda key, b: sum(r["bands"][b][key] for r in recs)
    check("6 quiet then +12 dB: per band exactly 60 warmup, 40 quiet, 100 active windows and no mid",
          all((tot("warmup", b), tot("n_quiet", b), tot("n_active", b), tot("mid", b)) == (60, 40, 100, 0) for b in range(8)),
          [(tot("warmup", b), tot("n_quiet", b), tot("n_active", b), tot("mid", b)) for b in range(8)])
    check("6 every window lands in exactly one class: warmup + quiet + active + mid = win_used in every record and band",
          all(bd["warmup"] + bd["n_quiet"] + bd["n_active"] + bd["mid"] == r["win_used"] for r in recs for bd in r["bands"]))
    check("6 the quiet cell medians come only from quiet windows (-66 dBFS record level) and the active ones from active (-54 dBFS)",
          all(abs(r["level_dbfs"] - (-66 if r["t0"] < 200 else -54)) < 0.6 for r in recs if r["win_used"] == 30 and r["t0"] in (120.0, 240.0)), [(r["t0"], r["level_dbfs"]) for r in recs])
    fl = p.noise_floor_db()
    lv = fl + 3.0
    cl = lambda d: p.classify_levels(fl + d)
    check("6 class boundaries on the real floor: F+2.8 quiet, F+3.0 quiet, F+3.2 mid, F+5.8 mid, F+6.0 active, F+6.2 active",
          [int(cl(d)[0]) for d in (2.8, 3.0, 3.2, 5.8, 6.0, 6.2)] == [1, 1, 3, 3, 2, 2], [int(cl(d)[0]) for d in (2.8, 3.0, 3.2, 5.8, 6.0, 6.2)])
    check("6 before the warmup every class is warmup (positive twin: the warmed-up plugin above classifies)",
          list(C.Coherence().classify_levels(np.zeros(8))) == [0] * 8 and 0 not in list(cl(0.0)))
    # one band only: +12 dB of independent noise in 4-8 kHz while the rest stays put
    rng = np.random.default_rng(18)

    def boosted(rng):
        # extra independent noise confined to 4-8 kHz (1/6 of a white spectrum's power). x1 amplitude would add +3.0 dB there;
        # sqrt(15) times the filtered noise adds 15 x the white noise's own 4-8 kHz power: 16 x in total = +12.04 dB in band
        # index 6 and nothing elsewhere (brick-wall filter; Hann leakage into the neighbouring bands is under 0.1 dB)
        X = np.fft.rfft(noise(rng, -60.0), axis=0)
        f = np.fft.rfftfreq(N, 1.0 / FS)
        X[(f < 4000) | (f >= 8000)] = 0
        return (noise(rng, -60.0) + np.sqrt(15.0) * np.fft.irfft(X, n=N, axis=0)).astype(np.float32)
    wins = [noise(rng, -60.0) for _ in range(100)] + [boosted(rng) for _ in range(40)]
    p, recs = run(wins)
    tot = lambda key, b: sum(r["bands"][b][key] for r in recs)
    got = [(tot("n_quiet", b), tot("n_active", b)) for b in range(8)]
    check("6 boosting only 4-8 kHz makes only that band active: band 7 quiet 40 / active 40, every other band quiet 80 / active 0",
          got == [(80, 0)] * 6 + [(40, 40)] + [(80, 0)], got)

    # ------------------------------------------------------------------------------------------------ 7 tonal mask
    mpath = os.path.join(tmp, "mask.json")
    json.dump({"lines_hz": [12000.0], "tolerance_hz": 4.0}, open(mpath, "w"))
    rng = np.random.default_rng(19)
    t_ax = np.arange(N) / FS

    def tonal(rng):
        x = noise(rng, -60.0)
        return x + (0.002 * np.sin(2 * np.pi * 12004.7 * t_ax))[:, None].astype(np.float32)        # one tone, the same on all beams
    wins = [tonal(rng) for _ in range(100)]
    p_m, rec_m = run(wins, mask=mpath)
    p_u, rec_u = run(wins)
    seg = (0.002 * np.sin(2 * np.pi * 12004.7 * np.arange(C.NSEG) / FS)) * C.Coherence().hann
    tone_p = np.abs(np.fft.rfft(seg)) ** 2
    noise_p = db(-60.0) ** 2 * float((C.Coherence().hann.astype(np.float64) ** 2).sum())
    k_hi = [k for k in range(C.Coherence().b0 + C.Coherence().rng[7][0], C.Coherence().b1) if tone_p[k] > 9 * noise_p]
    lower = len(k_hi) * (0.81 - C.MSC_FLOOR) / 683                                              # bins with tone/noise >= 9 have MSC >= 0.81
    mm = pooled(rec_m, 7, "quiet", "msc_p50").mean()
    mu = pooled(rec_u, 7, "quiet", "msc_p50").mean()
    mun = pooled(rec_m, 7, "quiet", "msc_unmasked_p50").mean()
    check("7 mask line 12000 Hz (tone at 12004.7 Hz, 34 dB above the noise per bin at its peak): band 8-16 kHz median MSC stays at the independent-noise floor (+-0.012)", abs(mm - C.MSC_FLOOR) < 0.012, mm)
    check("7 mask: the unmasked median reported next to it is higher by at least the coherent tone bins' share", mun - mm >= lower - 0.002 and lower >= 0.004, (mun, mm, lower, len(k_hi)))
    check("7 positive twin, no --tonal-mask: the band median equals the unmasked one and carries no masked bins (mask_hz [])",
          abs(mu - mun) < 1e-9 and rec_u[2]["mask_hz"] == [] and rec_u[2]["tonal_bins"] == [0] * 8 and "msc_unmasked_p50" not in rec_u[2]["bands"][7]["quiet"], (mu, mun))
    lines = [11510.7, 12000.0, 15334.0, 15389.6, 15445.3, 15501.0, 15556.6]               # the unit's electronic lines (a copy, so the repo's mask file can evolve)
    real_mask = os.path.join(tmp, "mask7.json")
    json.dump({"version": 1, "lines_hz": lines, "tolerance_hz": 4.0}, open(real_mask, "w"))
    pr = C.Coherence(real_mask)
    guard = max(4.0, 2 * C.DF)
    want = sum(1 for k in range(pr.b0 + pr.rng[7][0], pr.b1) if any(abs(k * C.DF - ln) <= guard for ln in lines))
    check("7 the 7 electronic lines (11.5, 12, 15.33-15.56 kHz; guard 2 bins = 23.4 Hz, overlapping) mask exactly 29 of the 683 bins of 8-16 kHz and none below",
          pr.tonal_bins == [0] * 7 + [want] and want == 29, (pr.tonal_bins, want))

    # ------------------------------------------------------------------------------------------------ 8 aggregation boundaries
    z0 = np.zeros((N, 8), np.float32)

    def feed(ts, interval=60.0):
        p = C.Coherence(None, None, interval)
        out = []
        for t in ts:
            r, _ = p.process(z0, t)
            out += r
        return p, out
    p, out = feed([10, 58, 59.0, 61.0, 119.0, 121.0, 181.0, 245.0])
    check("8 buckets: windows at 59 and 61 split at 60, 119 and 121 at 120: 4 records with t0 0/60/120/180 and win_total 3/2/1/1... ",
          [(r["t0"], r["t"], r["win_total"]) for r in out] == [(0.0, 60.0, 3), (60.0, 120.0, 2), (120.0, 180.0, 1), (180.0, 240.0, 1)], [(r["t0"], r["win_total"]) for r in out])
    p, out = feed([10, 190])
    check("8 an empty minute emits nothing: windows at 10 s and 190 s give exactly one record (t0 0)", [(r["t0"], r["win_total"]) for r in out] == [(0.0, 1)], out and out[0]["t0"])
    p, out = feed([float(t) for t in range(0, 330, 2)])
    check("8 165 windows over 330 s at 60 s: 5 closed records of 30 windows each", [r["win_total"] for r in out] == [30] * 5 and p.stats()["minutes"] == 5, [r["win_total"] for r in out])
    p, out = feed([float(t) for t in range(0, 330, 2)], 30.0)
    check("8 --coherence-interval-s 30 doubles the record count (10 of 15 windows each) and span_s says 30",
          [r["win_total"] for r in out] == [15] * 10 and {r["span_s"] for r in out} == {30.0}, [r["win_total"] for r in out])
    p, out = feed([130.0, 135.0, 50.0, 140.0, 185.0])
    check("8 a late window (t=50 after 130) is counted in stats.late and folded into the open bucket, never raises; 4 windows then close at 185",
          p.stats()["late"] == 1 and [(r["t0"], r["win_total"]) for r in out] == [(120.0, 4)], (p.stats(), [(r["t0"], r["win_total"]) for r in out]))

    # ------------------------------------------------------------------------------------------------ 9 fail-safe
    rng = np.random.default_rng(20)
    good = mixture(rng, 1.0)
    p = C.Coherence()
    nanw, infw = good.copy(), good.copy()
    nanw[100, 3] = np.nan
    infw[5, 0] = np.inf
    bad_in = [nanw, infw, np.zeros((100, 8), np.float32), np.zeros((N, 7), np.float32), np.array([["a"] * 8] * 5000, dtype=object), None, "x"]
    outs = [p.process(w, 10.0) for w in bad_in]
    outs.append(p.process(good, float("nan")))
    check("9 fail-safe: NaN, Inf, wrong shapes, strings, None and a NaN time return ([], None) and never raise", all(o == ([], None) for o in outs), outs)
    check("9 counts are exact: 2 non-finite windows, 6 errors (2 shapes, strings, None, str, NaN time)", (p.stats()["nonfinite"], p.stats()["errors"]) == (2, 6), p.stats())
    r1, _ = p.process(good, 20.0)
    r2, _ = p.process(good, 65.0)
    check("9 positive twin: the next good windows are processed (2 used), and the minute record shows win_nonfinite 2 with win_total 3, win_used 1",
          p.stats()["used"] == 2 and len(r2) == 1 and (r2[0]["win_nonfinite"], r2[0]["win_total"], r2[0]["win_used"]) == (2, 3, 1), (r2 and (r2[0]["win_nonfinite"], r2[0]["win_total"])))
    check("9 the plugin survived: a window after the garbage has the exact normal answer (finite level)", r2 and math.isfinite(r2[0]["level_dbfs"]) and abs(r2[0]["level_dbfs"] + 60.0) < 0.3, r2 and r2[0]["level_dbfs"])

    # ------------------------------------------------------------------------------------------------ 10 schema
    sch_path = os.path.join(BMA, "contracts", "schemas", "beam_coherence.schema.json")
    allrecs = rec_m + rec_u + recs + res[3.0]
    try:
        from jsonschema import Draft202012Validator
        have_js = True
    except ImportError:
        have_js = False
        print("SKIP 10 schema checks  [jsonschema is not installed in this interpreter (the analyzer venv); run with the Windows Python or the contracts venv]")
    if have_js:
        sch = json.load(open(sch_path, encoding="utf-8"))
        v = Draft202012Validator({"$schema": sch["$schema"], "$ref": "#/$defs/beam_coherence", "$defs": sch["$defs"]})
        errs = [e.message for r in allrecs for e in v.iter_errors(r)]
        check(f"10 every record the plugin wrote in this run ({len(allrecs)}) validates against beam_coherence.schema.json", errs == [], errs[:2])
        bad = json.loads(json.dumps(allrecs[-1]))
        cell = next(c for b in bad["bands"] for c in (b["quiet"], b["active"]) if c)
        cell["msc_p50"][0] = 1.2
        e2 = [e.message for e in v.iter_errors(bad)]
        vc = Draft202012Validator({"$schema": sch["$schema"], "$ref": "#/$defs/cell", "$defs": sch["$defs"]})
        e3 = [e.message for e in vc.iter_errors(cell)]
        check("10 a record with msc 1.2 is rejected and its cell says 'greater than the maximum of 1' (positive twin above: the unmodified records pass)",
              len(e2) >= 1 and any("greater than the maximum of 1" in m for m in e3), (e2[:1], e3[:2]))

    # ------------------------------------------------------------------------------------------------ 11 cost
    rng = np.random.default_rng(21)
    ws = [noise(rng) for _ in range(25)]
    p = C.Coherence()
    ts = []
    for i, w in enumerate(ws):
        t0 = time.perf_counter()
        p.process(w, i * HOP)
        ts.append((time.perf_counter() - t0) * 1000)
    ts = ts[5:]
    print(f"INFO cost per 1 s window: median {np.median(ts):.1f} ms  p95 {np.percentile(ts, 95):.1f} ms  (hop {HOP:g} s: {np.median(ts) / HOP:.1f} ms per second of audio)")
    check("11 cost guard: median per-window time under 50 ms (the real budget is measured by bench_plugins.py)", np.median(ts) < 50.0, np.median(ts))

    # ------------------------------------------------------------------------------------------------ 13 report script
    def verdicts(recs_, **kw):
        rep = R.build(recs_, **kw)
        return rep, {(r["band"], r["cond"]): r["verdict"] for r in rep["rows"]}
    rng = np.random.default_rng(22)
    _, rc = run([common(rng, -60) for _ in range(100)])
    _, ri = run([noise(rng, -60) for _ in range(100)])
    _, rp = run([mixture(rng, 1.0) for _ in range(100)])
    _, rg = run([np.concatenate([mixture(rng, 1.0)[:, :5], mixture(rng, 1.0)[:, 5:] * 1e-5], axis=1) for _ in range(40)])
    _, rt = run([common(rng, -60) for _ in range(66)])
    for nm, recs_, want_v in (("identical channels", rc, R.V_COH), ("independent noise", ri, R.V_INCOH), ("SNR 1 mixture (MSC 0.25)", rp, R.V_INCOH)):
        rep, vv = verdicts(recs_)
        check(f"13 report: {nm} -> every quiet row says {want_v!r}", all(vv[(b, "quiet")] == want_v for b in range(8)), [vv[(b, "quiet")] for b in range(8)])
    rng = np.random.default_rng(23)
    _, rp3 = run([mixture(rng, 3.0) for _ in range(100)])              # corrected MSC 0.5625: between 0.3 and 0.8
    rep, vv = verdicts(rp3)
    check("13 report: SNR 3 mixture (corrected MSC 0.5625) -> partially coherent in every quiet row", all(vv[(b, "quiet")] == R.V_PART for b in range(8)), [vv[(b, "quiet")] for b in range(8)])
    rep, vv = verdicts(rg)
    check("13 report: a window set with 3 muted beams is 100 % gated -> every row dominated by gated windows, gated fraction 1.0 (twin: identical channels are not)",
          set(vv.values()) == {R.V_GATED} and rep["gated_fraction"] == 1.0 and verdicts(rc)[0]["gated_fraction"] == 0.0, (set(vv.values()), rep["gated_fraction"]))
    rep, vv = verdicts(rt)
    check("13 report: 66 windows (6 beyond the warmup) with --min-windows 10 -> insufficient data; twin: with --min-windows 5 -> coherent",
          vv[(0, "quiet")] == R.V_INSUF and verdicts(rt, min_windows=5)[1][(0, "quiet")] == R.V_COH, (vv[(0, "quiet")], verdicts(rt, min_windows=5)[1][(0, "quiet")]))
    rep, vv = verdicts(rc)
    check("13 report: no active windows -> insufficient data in the active rows; beamspace line names the coherent bands",
          all(vv[(b, "active")] == R.V_INSUF for b in range(8)) and rep["beamspace"].startswith("possible where coherent: band 1 quiet"), rep["beamspace"])
    check("13 report: independent noise -> the beamspace line says no band is coherent", verdicts(ri)[0]["beamspace"] == "not supported by this data: no band is coherent")
    jp = os.path.join(tmp, "coh.jsonl")
    with open(jp, "w") as f:
        for r in rc:
            f.write(json.dumps(r) + "\n")
        f.write("garbage\n{\"k\":\"other\"}\n")
    out_txt = subprocess.run([sys.executable, "-I", os.path.join(HERE, "coherence_report.py"), jp], capture_output=True, text=True)
    check("13 report CLI: exit 0, a table with a 'coherent' row for 50-125 and the thresholds in the header (garbage lines skipped)",
          out_txt.returncode == 0 and "thresholds:" in out_txt.stdout and re.search(r"50-125\s+quiet", out_txt.stdout) and out_txt.stdout.count("  coherent") >= 8, out_txt.stdout[:300] + out_txt.stderr[-200:])
    out_json = subprocess.run([sys.executable, "-I", os.path.join(HERE, "coherence_report.py"), jp, "--json"], capture_output=True, text=True)
    check("13 report CLI --json: parses and has 16 rows", out_json.returncode == 0 and len(json.loads(out_json.stdout)["rows"]) == 16)
    check("13 report CLI: a missing file exits 2", subprocess.run([sys.executable, "-I", os.path.join(HERE, "coherence_report.py"), os.path.join(tmp, "nope.jsonl")], capture_output=True).returncode == 2)

    # ------------------------------------------------------------------------------------------------ 14 fixtures reproducible
    import make_coherence_fixtures as MCF
    regen = MCF.build(os.path.join(tmp, "fix"))
    a = [json.loads(l) for l in open(os.path.join(regen, "beam_coherence.jsonl"), encoding="utf-8")]
    b = [json.loads(l) for l in open(os.path.join(BMA, "contracts", "fixtures", "beam_coherence.jsonl"), encoding="utf-8")]

    def same(x, y, path=""):
        if isinstance(x, float) or isinstance(y, float):
            return isinstance(x, (int, float)) and isinstance(y, (int, float)) and abs(x - y) <= 0.002 + 1e-9
        if isinstance(x, dict):
            return isinstance(y, dict) and x.keys() == y.keys() and all(same(x[k], y[k], path + "." + k) for k in x)
        if isinstance(x, list):
            return isinstance(y, list) and len(x) == len(y) and all(same(p_, q_) for p_, q_ in zip(x, y))
        return x == y
    check("14 the committed fixture equals what the real plugin writes (floats within 0.002: FFT last-bit differences between platforms), same keys, ints and nulls",
          len(a) == len(b) and all(same(x, y) for x, y in zip(a, b)), (len(a), len(b)))
    if have_js:
        errs = [e.message for r in b for e in v.iter_errors(r)]
        check("14 every committed fixture record validates and the fixture holds quiet, active, gated and thin cells",
              errs == [] and any(r["win_gated"] for r in b) and any(bd["quiet"] for r in b for bd in r["bands"]) and any(bd["active"] for r in b for bd in r["bands"])
              and any(bd["quiet"] is None and bd["n_quiet"] > 0 for r in b for bd in r["bands"]), errs[:2])
        check("14 the fixture holds cells with msc_unmasked_p50 and a non-empty mask", any("msc_unmasked_p50" in (bd["quiet"] or {}) for r in b for bd in r["bands"]) and b[2]["mask_hz"] != [])

    # ------------------------------------------------------------------------------------------------ 15 pinned values (review findings)
    # 15a bias correction inverts the documented quadratic exactly
    f48 = 0.048
    raw_of = lambda m, f=f48: m + (1 - m) ** 2 * f                   # the documented mean of the raw estimate
    errs = [abs(R.correct(raw_of(m), f48) - m) for m in (0.05, 0.25, 0.3, 0.5, 0.5625, 0.8, 0.95, 1.0)]
    check("15 correct() inverts raw = MSC + (1 - MSC)^2 * floor exactly (error < 1e-9 at 0.05..1.0); twin: a true 0.8 is NOT read as 0.79",
          max(errs) < 1e-9 and abs(R.correct(raw_of(0.8), f48) - 0.8) < 1e-9 and R.correct(0.0, f48) == 0.0 and R.correct(f48, f48) == 0.0, errs)
    # 15b Monte-Carlo through the real estimator: true MSC from a known mixture, 40 independent windows
    pm = C.Coherence()
    for r_, m_ in ((1.0, 0.25), (3.0, 0.5625)):
        rng = np.random.default_rng(31)
        mc_ = float(np.mean([pm.analyze(mixture(rng, r_)).msc_all[:, 4].mean() for _ in range(40)]))
        check(f"15 Monte-Carlo MSC {m_}: raw mean {mc_:.4f} is {raw_of(m_, 1 / C.K_EFF):.4f} +- 0.006 and the corrected value is {m_} +- 0.006",
              abs(mc_ - raw_of(m_, 1 / C.K_EFF)) < 0.006 and abs(R.correct(mc_, 1 / C.K_EFF) - m_) < 0.006, (mc_, R.correct(mc_, 1 / C.K_EFF)))

    # 15c verdict script on hand-built records (exact values; nothing of the plugin involved)
    def cell(n, msc, spread=10.0, lag=0.0, unm=None):
        c = {"n": n, "msc_p50": list(msc), "ph_spread_deg": [spread] * 8, "lag_p50": list(lag) if isinstance(lag, (list, tuple)) else [lag] * 8}
        if unm is not None:
            c["msc_unmasked_p50"] = list(unm)
        return c

    def hrec(t, quiet, total=100, gated=0, floor=f48, active=None, band=0):
        bands = [{"quiet": None, "active": None} for _ in range(8)]
        bands[band]["quiet"], bands[band]["active"] = quiet, active
        return {"k": "beam_coherence", "v": 1, "t": t, "t0": t - 60, "span_s": 60.0, "win_total": total, "win_used": total - gated, "win_gated": gated,
                "win_dead": 0, "win_nonfinite": 0, "msc_floor": floor, "edges_hz": [list(e) for e in C.BANDS_HZ], "bands": bands}
    rawv = lambda m, f=f48: [raw_of(x, f) for x in m]
    row0 = lambda recs_, **kw: R.build(recs_, **kw)["rows"][0]            # band 0, quiet

    tr = [0.95] * 7 + [0.7]
    r0 = row0([hrec(60, cell(30, rawv(tr)))])
    check("15 report: one weak pair (true MSC 0.7, others 0.95) -> msc_min 0.7, msc_median 0.95, partially coherent (the 'ALL 8 pairs' rule)",
          (r0["msc_min"], r0["msc_median"], r0["verdict"]) == (0.7, 0.95, R.V_PART), r0)
    r0 = row0([hrec(60, cell(30, rawv([0.95] * 8)))])
    check("15 report: positive twin, all 8 pairs at true 0.95 -> msc_min 0.95, coherent", (r0["msc_min"], r0["verdict"]) == (0.95, R.V_COH), r0)
    for sp, want in ((30.0, R.V_COH), (30.5, R.V_PART), (45.0, R.V_PART)):
        r0 = row0([hrec(60, cell(30, rawv([0.95] * 8), spread=sp))])
        check(f"15 report: MSC 0.95 with phase spread {sp} deg -> {want} (limit 30 deg, inclusive)", (r0["phase_spread_deg"], r0["verdict"]) == (sp, want), r0)
    for m_, want in ((0.8, R.V_COH), (0.79, R.V_PART), (0.3, R.V_PART), (0.29, R.V_INCOH)):
        r0 = row0([hrec(60, cell(30, rawv([m_] * 8)))])
        check(f"15 report: every pair at true MSC {m_} -> {want} (0.8 inclusive coherent, below 0.3 incoherent)", (r0["msc_median"], r0["verdict"]) == (m_, want), r0)
    for g, want in ((50, False), (51, True)):
        rep_ = R.build([hrec(60, cell(30, rawv([0.95] * 8)), total=100, gated=g)])
        check(f"15 report: {g} % of the windows gated -> gated fraction {g / 100}, {'dominated' if want else 'not dominated'} (> 50 % dominates)",
              (rep_["gated_fraction"], rep_["rows"][0]["verdict"] == R.V_GATED) == (g / 100, want), (rep_["gated_fraction"], rep_["rows"][0]["verdict"]))
    r0 = row0([hrec(60, cell(30, rawv([0.9] * 8))), hrec(120, cell(10, rawv([0.5] * 8)))])
    r1 = row0([hrec(60, cell(10, rawv([0.9] * 8))), hrec(120, cell(30, rawv([0.5] * 8)))])
    check("15 report: pooling is weighted by the cell's window count: 30 windows at 0.9 + 10 at 0.5 -> 0.9; swapped weights -> 0.5; windows 40 each",
          (r0["msc_median"], r1["msc_median"], r0["windows"], r1["windows"]) == (0.9, 0.5, 40, 40), (r0, r1))
    r0 = row0([hrec(60, cell(30, rawv([0.4] * 8), lag=[0, 1, 2, 3, 4, 5, 6, 7]))])
    check("15 report: lag_median is the median over the pairs of the pooled per-pair lags: lags 0..7 -> 3.5", r0["lag_median"] == 3.5, r0)
    r0 = row0([hrec(60, cell(30, rawv([0.4] * 8), lag=-2.0))])
    check("15 report: positive twin, every pair at lag -2.0 -> lag_median -2.0 (sign kept)", r0["lag_median"] == -2.0, r0)
    r0 = row0([hrec(60, cell(30, rawv([0.4] * 8), unm=rawv([0.9] * 8)))])
    r1 = row0([hrec(60, cell(30, rawv([0.4] * 8)))])
    check("15 report: unmasked_msc_median pools msc_unmasked_p50 (true 0.9 next to the masked 0.4); None when the cells carry none",
          (r0["unmasked_msc_median"], r0["msc_median"], r1["unmasked_msc_median"]) == (0.9, 0.4, None), (r0, r1))
    r0 = row0([hrec(60, cell(30, rawv([0.6] * 8, 0.1)), floor=0.1)])
    check("15 report: the record's own msc_floor is used: floor 0.1 (K_eff 10) with raw = 0.6 + 0.16 * 0.1 reads true 0.6", r0["msc_min"] == 0.6, r0)
    # beamspace: undetermined vs not supported
    und = lambda rep_: rep_["beamspace"].startswith("undetermined")
    rep_g = R.build([hrec(60, cell(30, rawv([0.95] * 8)), total=100, gated=100)])
    rep_i = R.build([hrec(60, cell(5, rawv([0.95] * 8)))])
    rep_n = R.build([hrec(60, cell(30, rawv([0.1] * 8)))])
    rep_c = R.build([hrec(60, cell(30, rawv([0.95] * 8)))])
    check("15 report: every row gated-dominated -> beamspace 'undetermined', not 'not supported'", und(rep_g) and "not supported" not in rep_g["beamspace"], rep_g["beamspace"])
    check("15 report: every row insufficient (5 windows < 10) -> beamspace 'undetermined'", und(rep_i), rep_i["beamspace"])
    check("15 report: twins: an incoherent cell -> 'not supported by this data'; a coherent one -> 'possible where coherent: band 1 quiet'",
          rep_n["beamspace"] == "not supported by this data: no band is coherent" and rep_c["beamspace"] == "possible where coherent: band 1 quiet", (rep_n["beamspace"], rep_c["beamspace"]))
    check("15 report: a build from no records says undetermined", und(R.build([])), R.build([])["beamspace"])
    # CLI --since / --until
    from datetime import datetime, timezone
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    t_base = 1792900020
    recs3 = [hrec(t_base + 60 * k, cell(10 * (k + 1), rawv([0.95] * 8)), total=10 * (k + 1)) for k in range(3)]       # windows 10, 20, 30
    jp3 = os.path.join(tmp, "coh3.jsonl")
    with open(jp3, "w") as f:
        for r_ in recs3:
            f.write(json.dumps(r_) + "\n")

    def cli(*args):
        o = subprocess.run([sys.executable, "-I", os.path.join(HERE, "coherence_report.py"), jp3, "--json", *args], capture_output=True, text=True)
        return o.returncode, (json.loads(o.stdout) if o.returncode == 0 else o.stderr)
    rc_all, j_all = cli()
    rc_s, j_s = cli("--since", iso(t_base + 60))
    rc_u, j_u = cli("--until", iso(t_base + 60))
    rc_b, j_b = cli("--since", iso(t_base + 60), "--until", iso(t_base + 60))
    rc_n, e_n = cli("--since", iso(t_base + 600))
    check("15 report CLI: no filter 3 records / 60 windows; --since t+60 keeps 2 / 50; --until t+60 keeps 2 / 30; both -> 1 / 20; a range with no record exits 1",
          (rc_all, j_all["records"], j_all["windows"]["total"], j_s["records"], j_s["windows"]["total"], j_u["records"], j_u["windows"]["total"], j_b["records"], j_b["windows"]["total"], rc_n)
          == (0, 3, 60, 2, 50, 2, 30, 1, 20, 1), (rc_all, rc_s, rc_u, rc_b, rc_n))

    # 15d xc_lag of polarity-inverted pairs (the argmax of +r picked the noise floor)
    pa = C.Coherence()
    rng = np.random.default_rng(32)
    base1 = rng.standard_normal((N, 1)).astype(np.float32) * np.float32(db(-60.0))
    alt = np.array([1, -1, 1, -1, 1, -1, 1, -1], np.float32)[None, :]
    wa = pa.analyze((base1 * alt).astype(np.float32))
    check("15 anti-phase beams (beam k = (-1)^k * s): cross-phase 180 deg and phase-slope lag 0 in every pair, and xc_lag 0 +- 0.01 (was -8.6 samples)",
          bool((np.abs(wa.ph[:, 4]) > 179.0).all()) and bool((np.abs(wa.lag[:, 4]) < 0.01).all()) and float(np.abs(wa.xc_lag).max()) < 0.01, (wa.ph[:, 4], wa.xc_lag))
    wi = pa.analyze(np.repeat(base1, 8, axis=1))
    check("15 positive twin: the in-phase beams have phase 0 and xc_lag 0", bool((np.abs(wi.ph[:, 4]) < 1.0).all()) and float(np.abs(wi.xc_lag).max()) < 0.01, (wi.ph[:, 4], wi.xc_lag))
    dch = delayed_chain(np.random.default_rng(33), 3.0)
    wd = pa.analyze((dch * alt).astype(np.float32))
    check("15 anti-phase AND delayed: xc_lag equals the true delay [3]*7 + [-21] within 0.05 sample", np.abs(wd.xc_lag - np.array([3.0] * 7 + [-21.0])).max() < 0.05, np.round(wd.xc_lag, 3))

    # 15e flush at shutdown, partial first bucket
    rng = np.random.default_rng(34)
    one = mixture(rng, 1.0)
    pf = C.Coherence(None, None, 60.0)
    got = []
    for i in range(20):
        got += pf.process(one, 2.0 * i)[0]
    fl = pf.flush()
    check("15 flush: 20 windows (40 s) of an open bucket give no record from process(), exactly one from flush(): win_total 20, win_used 20, partial true, t0 0; a second flush gives none",
          got == [] and len(fl) == 1 and (fl[0]["win_total"], fl[0]["win_used"], fl[0].get("partial"), fl[0]["t0"]) == (20, 20, True, 0.0) and pf.flush() == [] and pf.stats()["minutes"] == 1,
          (got, [(r["win_total"], r.get("partial")) for r in fl]))
    check("15 flush: a plugin that saw no window flushes nothing", C.Coherence().flush() == [])
    pg = C.Coherence(None, None, 60.0)
    rr = []
    for i in range(65):
        rr += pg.process(one, 45.0 + 2.0 * i)[0]
    rr += pg.flush()
    check("15 a start 45 s into the minute makes the first record partial (8 windows), the full minute after it carries no partial key (30 windows), the shutdown flush is partial (27)",
          [(r["t0"], r["win_total"], r.get("partial")) for r in rr] == [(0.0, 8, True), (60.0, 30, None), (120.0, 27, True)], [(r["t0"], r["win_total"], r.get("partial")) for r in rr])
    ps0 = C.Coherence(None, None, 60.0)
    rr0 = []
    for i in range(32):
        rr0 += ps0.process(one, 2.0 * i)[0]
    check("15 positive twin: the same plugin started at the interval boundary writes a first record without a partial key", len(rr0) == 1 and "partial" not in rr0[0] and rr0[0]["win_total"] == 30, [(r["win_total"], r.get("partial")) for r in rr0])
    if have_js:
        errs = [e.message for r in fl + rr for e in v.iter_errors(r)]
        check("15 partial records validate against the schema", errs == [] and len(fl + rr) == 4, errs[:2])

    # 15f plugin internals pinned: floor, band level, overflow, full-matrix conditioning, fully masked band
    rng = np.random.default_rng(35)
    gains = 10 ** (rng.uniform(-6, 6, 100) / 20.0)
    unit1 = mixture(rng, 1.0)
    pp = C.Coherence(None, None, 1000.0)
    Ls = []
    for i, g_ in enumerate(gains):
        x = (unit1 * np.float32(g_)).astype(np.float32)
        Ls.append(pp.analyze(x).L)
        pp.process(x, 2.0 * i)
    want_floor = np.percentile(np.array(Ls), 10, axis=0)
    got_floor = pp.noise_floor_db()
    check("15 noise floor = the 10th percentile of the band-level history (numpy, 100 windows with gains spread over 12 dB): equal within 1e-9 in all 8 bands; twin: the median is 1.5+ dB above it",
          np.abs(got_floor - want_floor).max() < 1e-9 and (np.median(np.array(Ls), axis=0) - got_floor).min() > 1.5, (got_floor, want_floor))
    xu = common(np.random.default_rng(36))
    L_eq = pp.analyze(xu).L
    off = np.array([0, 0, 0, 0, 0, 0, 0, -20.0])
    L_uneq = pp.analyze((xu * (10 ** (off / 20.0)).astype(np.float32)[None, :]).astype(np.float32)).L
    check("15 band level L is the MEAN over the beams in dB: one beam 20 dB down lowers L by 2.5 dB in every band (a max would give 0)",
          np.abs((L_uneq - L_eq) - off.mean()).max() < 0.01, L_uneq - L_eq)
    pov = C.Coherence(None, None, 60.0)
    fixed = mixture(np.random.default_rng(37), 1.0)
    for i in range(128):
        pov.process(fixed, 0.0)
    ov0 = pov.stats()["overflow"]
    for i in range(2):
        pov.process(fixed, 0.0)
    check("15 overflow: the per-minute store holds 68 windows per cell; 60 warmup + 68 stored fill it (overflow 0), 2 more windows drop 2 x 8 cell stores",
          (pov.agg.cap, ov0, pov.stats()["overflow"], int(pov.agg.n[0, 0])) == (68, 0, 16, 68), (pov.agg.cap, ov0, pov.stats()["overflow"], pov.agg.n[0, 0]))
    # full-matrix conditioning: FULL_COND_BAND = (2, 5, 6): only 4-8 kHz boosted -> broad band 3 (4-12 kHz) goes active, bands 1 and 2 stay quiet
    rng = np.random.default_rng(38)

    def boosted2(rng):
        X = np.fft.rfft(noise(rng, -60.0), axis=0)
        f = np.fft.rfftfreq(N, 1.0 / FS)
        X[(f < 4000) | (f >= 8000)] = 0
        return (noise(rng, -60.0) + np.sqrt(15.0) * np.fft.irfft(X, n=N, axis=0)).astype(np.float32)
    _, rf = run([noise(rng, -60.0) for _ in range(100)] + [boosted2(rng) for _ in range(40)], interval=1000.0)
    fq, fa = rf[0]["full"]["cond"]["quiet"], rf[0]["full"]["cond"]["active"]
    check("15 full-matrix conditioning by FULL_COND_BAND: every 5th used window (60..135 classified): quiet n [16, 16, 8], active n [0, 0, 8] (broad band 3 follows band 7's boost)",
          (len(rf), (fq or {}).get("n"), (fa or {}).get("n")) == (1, [16, 16, 8], [0, 0, 8]), (len(rf), fq and fq["n"], fa and fa["n"]))
    # a mask covering a whole band: the band falls back to all its bins
    mp2 = os.path.join(tmp, "maskwide.json")
    json.dump({"lines_hz": [87.5], "tolerance_hz": 100.0}, open(mp2, "w"))
    rng = np.random.default_rng(39)
    pw, rw = run([common(rng, -60.0) for _ in range(100)], mask=mp2)
    c0, c2 = pooled(rw, 0, "quiet", "msc_p50"), pooled(rw, 2, "quiet", "msc_p50")
    check("15 a mask covering the whole 50-125 Hz band: 6 of 6 bins masked, yet the band reports MSC >= 0.999 on all windows used (falls back to all bins; a zero weight sum would make every window non-finite)",
          pw.tonal_bins[0] == 6 and pw.stats()["nonfinite"] == 0 and pw.stats()["used"] == 100 and c0 is not None and c0.min() >= 0.999 and c2.min() >= 0.999,
          (pw.tonal_bins, pw.stats(), c0))
    check("15 twin: the partially masked band 125-250 Hz keeps its unmasked bins (tonal_bins below its width) and the cell carries msc_unmasked_p50",
          0 < pw.tonal_bins[1] < pw.rng[1][1] - pw.rng[1][0] and "msc_unmasked_p50" in cells(rw, 1, "quiet")[0], (pw.tonal_bins, pw.rng[1]))

    if "--skip-e2e" in sys.argv:                                          # mutation runs: everything but the 40 s real-time host test
        print("SKIP 12 host e2e over UDP  [--skip-e2e]")
    else:
        # ------------------------------------------------------------------------------------------------ 12 the real host over UDP
        def free_port():
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.1", 0))
            pt = s.getsockname()[1]
            s.close()
            return pt

        def bmaa(slot, block, x):
            s24 = np.clip((x * 8388608.0).astype(np.int64), -8388608, 8388607).astype("<i4")
            return b"BMAA" + bytes([1, 0, slot, 0]) + struct.pack("<III", 0, block & 0xFFFFFFFF, len(x)) + s24.tobytes()

        def lines(path):
            with open(path, "rb") as f:
                return [json.loads(x) for x in f.read().splitlines() if x]
        out_dir, hist_dir = os.path.join(tmp, "out"), os.path.join(tmp, "hist")
        os.makedirs(out_dir)
        port = free_port()
        proc = subprocess.Popen([sys.executable, os.path.join(HERE, "host.py"), "--listen", f"127.0.0.1:{port}", "--out", out_dir, "--history-dir", hist_dir,
                                 "--no", "dsp", "--no", "spatial", "--no", "tags", "--no", "clap", "--no", "birds", "--coherence-interval-s", "20", "--secs", "66"],
                                stderr=subprocess.PIPE, text=True)
        for _ in range(240):                                                  # the host writes status.json from its first loop pass on: it is listening
            if os.path.exists(os.path.join(out_dir, "status.json")) or proc.poll() is not None:
                break
            time.sleep(0.25)
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rng = np.random.default_rng(24)
        blk, t_start, nb = 0, time.time(), 0
        SECS, MUTE_FROM, MUTE_TO = 52, 14, 22                                 # slot 3 is silent (-160 dBFS) for 8 s in the middle
        for sec in range(SECS):
            x = mixture(rng, 3.0, -60.0)
            if MUTE_FROM <= sec < MUTE_TO:
                x[:, 3] *= 1e-5
            for off in range(0, FS, 256):
                for slot in range(8):
                    sk.sendto(bmaa(slot, blk + off, x[off:off + 256, slot]), ("127.0.0.1", port))
                nb += 1
                dly = t_start + nb * 256 / FS - time.time()
                if dly > 0:
                    time.sleep(dly)
            blk += FS
        proc.wait(timeout=120)
        err = proc.stderr.read()
        st = json.load(open(os.path.join(out_dir, "status.json")))
        cp = os.path.join(out_dir, "coherence.jsonl")
        rows = lines(cp) if os.path.exists(cp) else []
        check("12 host e2e over UDP: exits 0 and writes coherence.jsonl with at least 2 records", proc.returncode == 0 and len(rows) >= 2, (proc.returncode, len(rows), err[-300:]))
        if have_js:
            errs = [e.message for r in rows for e in v.iter_errors(r)]
            check("12 host e2e: every record on disk validates against the schema", errs == [] and len(rows) >= 2, errs[:2])
        check("12 host e2e: the open interval is flushed at shutdown: the last record on disk is partial and no record between the first and the last is",
              bool(rows) and rows[-1].get("partial") is True and not any(r.get("partial") for r in rows[1:-1]), [(r["t0"], r["win_total"], r.get("partial")) for r in rows])
        g = sum(r["win_gated"] for r in rows)
        check("12 host e2e: the 8 s mute of slot 3 gates 3 to 5 windows (hop 2 s, 1 s windows, 250 ms sub-blocks)", 3 <= g <= 5, g)
        withlag = [r for r in rows if r["xc_lag"] is not None]
        check("12 host e2e: positive twin: used windows exist, at least one record has a broadband lag (5+ used windows) and every such lag is 0 +- 0.05 sample",
              sum(r["win_used"] for r in rows) >= 8 and len(withlag) >= 1 and all(max(abs(v_) for v_ in r["xc_lag"]) <= 0.05 for r in withlag), [(r["win_gated"], r["win_used"], r["xc_lag"]) for r in rows])
        check("12 host e2e: win_total = used + gated + dead + nonfinite in every record and no window was non-finite",
              all(r["win_total"] == r["win_used"] + r["win_gated"] + r["win_dead"] + r["win_nonfinite"] and r["win_nonfinite"] == 0 for r in rows))
        pl = st["plugins"]["coherence"]
        check("12 host e2e: status.json plugins.coherence is enabled with errors 0 and meta.coherence_errors is 0",
              pl["enabled"] and pl["errors"] == 0 and pl["windows"] > 10 and st["meta"]["coherence_errors"] == 0, (pl, st["meta"]))
        files = os.listdir(out_dir)
        check("12 host e2e: no audio is written (audio_written false, no audio-like file under --out)",
              st["audio_written"] is False and not [f for f in files if f.lower().endswith((".wav", ".flac", ".mp3", ".ogg", ".pcm", ".raw", ".opus"))], files)
        out2 = os.path.join(tmp, "out2")
        os.makedirs(out2)
        pr2 = subprocess.run([sys.executable, os.path.join(HERE, "host.py"), "--listen", f"127.0.0.1:{free_port()}", "--out", out2, "--history-dir", "", "--no", "dsp", "--no", "spatial",
                              "--no", "tags", "--no", "clap", "--no", "birds", "--no", "coherence", "--secs", "6"], capture_output=True, text=True)
        st2 = json.load(open(os.path.join(out2, "status.json"))) if os.path.exists(os.path.join(out2, "status.json")) else {}
        check("12 host --no coherence: reports enabled false, writes no coherence.jsonl and exposes no coherence errors counter value",
              pr2.returncode == 0 and st2.get("plugins", {}).get("coherence", {}).get("enabled") is False and not os.path.exists(os.path.join(out2, "coherence.jsonl"))
              and st2["meta"]["coherence_errors"] == 0, (pr2.returncode, pr2.stderr[-300:], st2.get("plugins", {}).get("coherence")))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{n_checks} checks, {len(fails)} failure(s)")
for f_ in fails:
    print("  FAILED:", f_)
sys.exit(1 if fails else 0)
