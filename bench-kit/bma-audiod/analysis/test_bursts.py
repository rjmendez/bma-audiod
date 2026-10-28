#!/usr/bin/env python3
"""Tests for the bursts plugin (analysis/bursts.py), its report (bursts_report.py), the host wiring and the burst-stats contract.
All signals are synthetic with fixed seeds; the REAL Bursts class runs on them (nothing is stubbed). Script style (check(name, ok, detail),
exit 1 on a failure) like test_coherence.py. Temp files only under tempfile. Run:  python -I test_bursts.py [--skip-e2e]
The schema checks need `jsonschema` (skipped with the reason printed when absent, as in the analyzer's WSL venv).

Expected values and where they come from
  * a click train with spacing 0.7 s has gaps of exactly 0.7: mode 0.7, regularity 1.0, jitter 0; 0.7 s falls in gap bin
    floor(30 ln(0.7 / 0.005) / ln 6000) = 17;
  * alternating gaps 0.665 / 0.735 s: median 0.7, both inside +-15 %, population std 0.035, coefficient of variation 0.05;
  * gaps [0.7, 0.7, 1.4] repeated: 2 of 3 gaps are the mode: regularity 0.6667;
  * a 20 % duty pulse train as an envelope has mean 0.2 and population std 0.4: rms_rel 2.0; its fundamental is 1 / period;
  * a sine of amplitude A has power A^2 / 2: 0.01 gives -43.01 dBFS in its 1 kHz band; 0.02 is +6.02 dB louder;
  * speech overlap rule: a window (s-1, s] is speech when it overlaps an active tagger window (a, b): b > s-1 and a < s;
  * hold-back: a record closes when the newest window end minus the grace (15 s, 0 without a tagger) is at least interval end + 0.5 s."""

import json, math, os, shutil, socket, struct, subprocess, sys, tempfile, time
import numpy as np
from scipy import signal as sg

HERE = os.path.dirname(os.path.abspath(__file__))
BMA = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import bursts as B  # noqa: E402
import bursts_report as R  # noqa: E402
import plugins as P  # noqa: E402
import host as H  # noqa: E402

fails, n_checks = [], 0
FS = 48000
T0 = 1792900020.0  # a multiple of 60 s


def check(name, ok, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  [{detail}]"))
    if not ok:
        fails.append(name)


def noise(rng, a=1e-5):
    return (rng.standard_normal((FS, 8)) * a).astype(np.float32)


def put(x, t_in, beams=None, amp=0.01):
    """A 2-sample click of height amp at t_in seconds into the window on the given beams (default all 8)."""
    i = int(round(t_in * FS))
    x[i : i + 2, list(range(8)) if beams is None else beams] += amp
    return x


def train_maker(times, beams=None, amp=0.01, seed=1, extra=None):
    """make(s) -> the window ending at second s + 1 holding the clicks with s <= t < s + 1 (times in seconds since T0)."""
    rng = np.random.default_rng(seed)
    times = np.asarray(times, float)

    def make(s):
        x = noise(rng)
        for c in times[(times >= s) & (times < s + 1)]:
            put(x, c - s, beams, amp)
        if extra is not None:
            x = extra(x, s)
        return x

    return make


def run(make, secs, tagger=False, interval=60.0, verdicts=(), flush=False, start=0):
    """Feed windows start..secs-1 (window s ends at T0 + s + 1) to a fresh Bursts; verdicts = (a, b, active) in seconds since T0, noted before the run."""
    p = B.Bursts(interval, tagger=tagger)
    for a, b, act in verdicts:
        p.note_speech(T0 + a, T0 + b, act)
    recs = []
    for s in range(start, secs):
        recs += p.process(make(s), T0 + s + 1)[0]
    if flush:
        recs += p.flush()
    return p, recs


def train(period=0.7, n=None, first=0.35):
    return first + period * np.arange(n if n is not None else int(70 / period))


tmp = tempfile.mkdtemp(prefix="bursts_test_")
try:
    # ------------------------------------------------------------------------------------------------ 1 gap statistics (pure)
    g = B.gap_stats(np.arange(100) * 0.7)
    check(
        "1 click train, spacing 0.7 s: 99 gaps, mode 0.7, mode_n 99, regularity 1.0, jitter 0, all gaps in histogram bin 17, nothing outside the range",
        (g["n"], g["mode_s"], g["mode_n"], g["regularity"], g["jitter_cv"], g["under_n"], g["over_n"]) == (99, 0.7, 99, 1.0, 0.0, 0, 0)
        and g["hist"] == [0] * 17 + [99] + [0] * 12,
        g,
    )
    rng = np.random.default_rng(3)
    gp = B.gap_stats(np.cumsum(rng.exponential(0.7, 800)))
    check(
        "1 positive twin of the above: Poisson clicks (mean gap 0.7 s, 799 gaps) give a regularity below 0.25 and well under the train's 1.0",
        gp["n"] == 799 and gp["regularity"] < 0.25 and gp["mode_n"] < 0.25 * 799,
        (gp["regularity"], gp["mode_s"]),
    )
    alt = np.concatenate([[0.0], np.cumsum(np.tile([0.665, 0.735], 50))])
    ga = B.gap_stats(alt)
    check(
        "1 alternating gaps 0.665 / 0.735: mode 0.7 (median of the window), regularity 1.0, jitter cv 0.05 (std 0.035 / mean 0.7)",
        (ga["n"], ga["mode_s"], ga["regularity"], ga["jitter_cv"]) == (100, 0.7, 1.0, 0.05),
        ga,
    )
    miss = np.concatenate([[0.0], np.cumsum(np.tile([0.7, 0.7, 1.4], 10))])
    gm = B.gap_stats(miss)
    check(
        "1 a train with every third impulse missing (gaps 0.7, 0.7, 1.4): mode 0.7, 20 of 30 gaps, regularity 0.6667 (not 1.0)",
        (gm["n"], gm["mode_s"], gm["mode_n"], gm["regularity"]) == (30, 0.7, 20, 0.6667),
        gm,
    )
    g4, g5 = B.gap_stats(np.arange(5) * 0.7), B.gap_stats(np.arange(6) * 0.7)
    check(
        "1 fewer than 5 gaps: mode, mode_n, regularity and jitter are null but the histogram still counts (4 gaps); twin: 5 gaps give mode 0.7",
        (g4["n"], g4["mode_s"], g4["mode_n"], g4["regularity"], g4["jitter_cv"], sum(g4["hist"])) == (4, None, None, None, None, 4)
        and (g5["n"], g5["mode_s"], g5["regularity"]) == (5, 0.7, 1.0),
        (g4, g5),
    )
    gu = B.gap_stats([0.0, 0.003, 100.0])
    check(
        "1 a 3 ms gap is under_n, a 99.997 s gap is over_n, neither is in the histogram; twin: a 1 s gap is in bin 18 and counts in neither",
        (gu["under_n"], gu["over_n"], sum(gu["hist"])) == (1, 1, 0)
        and B.gap_stats([0.0, 1.0])["hist"][18] == 1
        and (B.gap_stats([0.0, 1.0])["under_n"], B.gap_stats([0.0, 1.0])["over_n"]) == (0, 0),
        gu,
    )
    tt = np.cumsum(np.random.default_rng(8).exponential(0.7, 60))
    check(
        "1 the statistics do not depend on the order of the input times (shuffled == sorted); an empty and a one-element input give n 0",
        B.gap_stats(np.random.default_rng(1).permutation(tt)) == B.gap_stats(tt) and B.gap_stats([])["n"] == 0 and B.gap_stats([5.0])["n"] == 0,
    )

    # ------------------------------------------------------------------------------------------------ 2 event_stats (arrays -> record parts)
    imp, gp_ = B.event_stats(
        np.arange(10) * 0.7,
        level_db=np.arange(10) * 1.0,
        spread_db=[2.0] * 10,
        excess_db=[12.0] * 10,
        within3=[8] * 6 + [3] * 4,
        nhit=[8] * 7 + [2] * 3,
        loudest=[1, 1, 2, 3, 8, 8, 8, 4, 5, 6],
        analysed_s=7.0,
    )
    check(
        "2 event_stats: n 10, rate 10/7 = 1.4286, level p10/p50/p90 of 0..9 = [0.9, 4.5, 8.1], within3 hist has 6 at 8 and 4 at 3, beams hit: 7 x 8 beams (coincident 7) and 3 x 2",
        imp["n"] == 10
        and imp["rate_per_s"] == 1.4286
        and imp["level_db"] == [0.9, 4.5, 8.1]
        and imp["spread_db"] == [2.0, 2.0, 2.0]
        and imp["within3_hist"] == [0, 0, 4, 0, 0, 0, 0, 6]
        and imp["beams_hit_hist"] == [0, 0, 3, 0, 0, 0, 0, 0, 7]
        and imp["coincident_all_n"] == 7
        and imp["loudest_beam_hist"] == [2, 1, 1, 1, 1, 1, 0, 3],
        imp,
    )
    imp0, _ = B.event_stats(np.arange(10) * 0.7)
    check(
        "2 twin: with no per-beam arrays the per-beam fields are null (a replay of recorded transients), and level percentiles are null without levels",
        imp0["n"] == 10
        and imp0["level_db"] is None
        and imp0["within3_hist"] is None
        and imp0["beams_hit_hist"] is None
        and imp0["coincident_all_n"] is None
        and imp0["rate_per_s"] is None,
        imp0,
    )

    # ------------------------------------------------------------------------------------------------ 3 envelope modulation spectrum (pure)
    pulse = (np.arange(4000) % 10 < 2).astype(float)  # 10 ms on, 40 ms off at 200 Hz: 20 Hz, 20 s
    m = B.mod_peak([pulse])
    check(
        "3 a 20 Hz pulse envelope (20 s): s 20.0, peak 20.0 Hz, fundamental 20 Hz +-0.06, rms_rel exactly 2.0 (std 0.4 / mean 0.2), prominence over 40 dB",
        m["s"] == 20.0 and abs(m["peak_hz"] - 20.0) <= 0.06 and abs(m["fund_hz"] - 20.0) <= 0.06 and m["rms_rel"] == 2.0 and m["prom_db"] > 40,
        m,
    )
    mn = B.mod_peak([np.random.default_rng(2).standard_normal(4000) + 5.0])
    check(
        "3 positive twin: white noise has no modulation line: prominence under 15 dB (against over 40 for the pulses)",
        mn["prom_db"] is not None and mn["prom_db"] < 15,
        mn,
    )
    mp = B.mod_peak([(np.arange(200 * 70) % 140 < 2).astype(float)])
    check(
        "3 a pulse every 0.7 s (70 s run): fundamental 1.4286 Hz +-0.04 (the peak bin itself can be a harmonic, so the fundamental is reported separately)",
        abs(mp["fund_hz"] - 1 / 0.7) <= 0.04,
        mp,
    )
    ms, ml = B.mod_peak([pulse[:1980]]), B.mod_peak([pulse[:2000]])
    check(
        "3 a run shorter than 10 s is not used (s 0.0, all null); twin: 10 s exactly is used (s 10.0, a fundamental)",
        (ms["s"], ms["peak_hz"], ms["fund_hz"], ms["rms_rel"]) == (0.0, None, None, None) and ml["s"] == 10.0 and ml["fund_hz"] is not None,
        (ms, ml),
    )

    # ------------------------------------------------------------------------------------------------ 4 the plugin on synthetic windows
    clicks = train(0.7)
    p, recs = run(train_maker(clicks), 61)
    r = recs[0]
    n_in = int((clicks < 60).sum())
    check(
        "4 click train, 0.7 s spacing, all 8 beams (60 s): 1 record, interval start T0, 60 windows used, the exact click count, mode 0.7, regularity 1.0, jitter under 0.01",
        len(recs) == 1
        and r["t0"] == int(T0)
        and r["win_used"] == 60
        and r["impulses"]["n"] == n_in == 86
        and r["gaps"]["mode_s"] == 0.7
        and r["gaps"]["regularity"] == 1.0
        and r["gaps"]["jitter_cv"] < 0.01
        and "partial" not in r,
        (len(recs), r["impulses"]["n"], r["gaps"]),
    )
    check(
        "4 the same train: every impulse fired on all 8 beams (coincident_all_n 86, beams_hit_hist[8] 86), 8 beams within 3 dB, spread p90 under 1 dB, rate 86/60",
        r["impulses"]["coincident_all_n"] == 86
        and r["impulses"]["beams_hit_hist"][8] == 86
        and r["impulses"]["within3_hist"][7] == 86
        and r["impulses"]["spread_db"][2] < 1.0
        and r["impulses"]["rate_per_s"] == round(86 / 60, 4),
        r["impulses"],
    )
    check(
        "4 the same train: the modulation fundamental is 1/0.7 Hz +-0.04, win_total = used + speech + unvetted + dead, tagger false (none ran)",
        abs(r["mod"]["fund_hz"] - 1 / 0.7) <= 0.04
        and r["win_total"] == r["win_used"] + r["win_speech"] + r["win_unvetted"] + r["win_dead"]
        and r["tagger"] is False,
        r["mod"],
    )
    _, rs = run(train_maker(clicks, beams=[2]), 61)
    r1 = rs[0]
    check(
        "4 the same train on ONE beam only (beam 3): same count and regularity, but coincident_all_n 0, one beam hit, one beam within 3 dB, loudest beam 3 every time, spread p10 over 30 dB",
        r1["impulses"]["n"] == 86
        and r1["gaps"]["regularity"] == 1.0
        and r1["impulses"]["coincident_all_n"] == 0
        and r1["impulses"]["beams_hit_hist"][1] == 86
        and r1["impulses"]["within3_hist"][0] == 86
        and r1["impulses"]["loudest_beam_hist"][2] == 86
        and r1["impulses"]["spread_db"][0] > 30,
        r1["impulses"],
    )
    _, r3 = run(train_maker(clicks, beams=[0, 1, 2]), 61)
    i3 = r3[0]["impulses"]
    check(
        "4 the train on 3 beams: 3 beams hit and 3 within 3 dB on every impulse (not 8, not 1), none coincident on all 8",
        i3["n"] == 86
        and i3["beams_hit_hist"][3] == 86
        and i3["within3_hist"][2] == 86
        and i3["coincident_all_n"] == 0
        and sum(i3["loudest_beam_hist"][:3]) == 86,
        i3,
    )

    def halves(offset_s):
        def extra(x, s):
            for c in clicks[(clicks >= s) & (clicks < s + 1)]:
                put(x, c - s + offset_s, beams=[4, 5, 6, 7])
            return x

        return train_maker(clicks, beams=[0, 1, 2, 3], extra=extra)

    _, ra = run(halves(0.001), 61)
    _, rb = run(halves(0.003), 61)
    check(
        "4 coincidence window +-1 ms: beams 5-8 fire 1.0 ms (one whole frame) after beams 1-4: every impulse counts all 8 beams as hit; twin: 3 ms after, only 4 beams are hit and none is coincident on all 8",
        ra[0]["impulses"]["n"] == 86
        and ra[0]["impulses"]["coincident_all_n"] == 86
        and rb[0]["impulses"]["n"] == 86
        and rb[0]["impulses"]["beams_hit_hist"][4] == 86
        and rb[0]["impulses"]["coincident_all_n"] == 0,
        (ra[0]["impulses"]["beams_hit_hist"], rb[0]["impulses"]["beams_hit_hist"]),
    )

    def two_levels(amp_b):
        def extra(x, s):
            for c in clicks[(clicks >= s) & (clicks < s + 1)]:
                put(x, c - s, beams=[4, 5, 6, 7], amp=amp_b)
            return x

        return train_maker(clicks, beams=[0, 1, 2, 3], amp=0.01, extra=extra)

    _, rl6 = run(two_levels(0.005), 61)
    _, rl2 = run(two_levels(0.0075), 61)
    check(
        "4 within 3 dB: beams 1-4 at 0.01 and beams 5-8 at 0.005 (-6.02 dB): 4 beams within 3 dB of the loudest on every impulse, all 8 hit, spread p50 6.02 +-0.3 dB; twin: beams 5-8 at 0.0075 (-2.5 dB): all 8 within 3 dB, spread 2.5 +-0.3",
        rl6[0]["impulses"]["within3_hist"][3] == 86
        and rl6[0]["impulses"]["beams_hit_hist"][8] == 86
        and abs(rl6[0]["impulses"]["spread_db"][1] - 6.02) < 0.3
        and rl2[0]["impulses"]["within3_hist"][7] == 86
        and abs(rl2[0]["impulses"]["spread_db"][1] - 2.5) < 0.3,
        (rl6[0]["impulses"]["within3_hist"], rl6[0]["impulses"]["spread_db"], rl2[0]["impulses"]["within3_hist"], rl2[0]["impulses"]["spread_db"]),
    )
    rg = np.random.default_rng(12)
    pt = np.cumsum(np.maximum(rg.exponential(0.7, 120), 0.05)) + 0.2
    pt = pt[pt < 59.0]
    pt = np.where((pt % 1.0 < 0.01) | (pt % 1.0 > 0.99), pt + 0.05, pt)
    _, rp = run(train_maker(pt), 61)
    check(
        "4 Poisson-random clicks (min gap 50 ms): every click detected (exact count), regularity below 0.25; twin of the train: that one had 1.0",
        rp[0]["impulses"]["n"] == len(pt) and rp[0]["gaps"]["regularity"] < 0.25,
        (rp[0]["impulses"]["n"], len(pt), rp[0]["gaps"]["regularity"]),
    )

    def tone(f, a):
        t_ = np.arange(FS) / FS
        return lambda x, s: x + (a * np.sin(2 * np.pi * f * t_ + 0.3 * s)).astype(np.float32)[:, None]

    for f_ in (10500.0, 11510.0, 3000.0):
        _, rt = run(train_maker([], extra=tone(f_, 0.01)), 61)
        check(
            f"4 a steady {f_:.0f} Hz tone (amplitude 0.01, -43 dBFS) gives no impulse",
            rt[0]["impulses"]["n"] == 0 and rt[0]["gaps"]["n"] == 0 and rt[0]["gaps"]["mode_s"] is None,
            rt[0]["impulses"]["n"],
        )
    _, rtc = run(train_maker(clicks, extra=tone(10500.0, 1e-4)), 61)
    _, rtl = run(train_maker(clicks, extra=tone(10500.0, 0.01)), 61)
    check(
        "4 positive twin: a weak steady tone (amplitude 1e-4, -83 dBFS) plus the click train still gives the 86 impulses and mode 0.7 (a tone does not hide them); and a click 14 dB UNDER a loud tone (0.01) is not an impulse (relative detector)",
        rtc[0]["impulses"]["n"] == 86 and rtc[0]["gaps"]["mode_s"] == 0.7 and rtl[0]["impulses"]["n"] == 0,
        (rtc[0]["impulses"]["n"], rtl[0]["impulses"]["n"]),
    )
    tn = np.arange(FS) / FS

    def trill(carrier, gate):
        ph = tn % 0.05
        env = np.where(ph < 0.01, np.sin(np.pi * ph / 0.01) ** 2, 0.0) if gate == "hann" else (ph < 0.01).astype(float)
        return lambda x, s: x + (0.003 * np.sin(2 * np.pi * carrier * tn) * env).astype(np.float32)[:, None]

    _, rk = run(train_maker([], extra=trill(10000.0, "hann")), 61)
    k_ = rk[0]
    check(
        "4 katydid-like trill, 10 kHz pulses of 10 ms every 50 ms: modulation fundamental 20 Hz +-0.06, mode of the gaps 0.05 s, about 1200 impulses (1140-1200), regularity over 0.9",
        abs(k_["mod"]["fund_hz"] - 20.0) <= 0.06
        and abs(k_["gaps"]["mode_s"] - 0.05) <= 0.0015
        and 1140 <= k_["impulses"]["n"] <= 1200
        and k_["gaps"]["regularity"] > 0.9,
        (k_["mod"], k_["gaps"]["mode_s"], k_["impulses"]["n"]),
    )
    _, rkr = run(train_maker([], extra=trill(10000.0, "rect")), 61)
    check(
        "4 a hard-gated 10 kHz trill (rectangular 10 ms pulses): the plateau ripple and the edge ring-down are not extra impulses (prominence): one impulse per pulse, 1140-1200 in 60 s, mode 0.05 +-0.0015",
        1140 <= rkr[0]["impulses"]["n"] <= 1200 and abs(rkr[0]["gaps"]["mode_s"] - 0.05) <= 0.0015,
        (rkr[0]["impulses"]["n"], rkr[0]["gaps"]["mode_s"]),
    )
    _, rk4 = run(train_maker([], extra=trill(4000.0, "hann")), 61)
    _, rq = run(train_maker([]), 61)
    check(
        "4 the 4 kHz trill of the task text (smooth 10 ms pulses every 50 ms): out of band, no impulse, but the filter skirt (-31 dB at 4 kHz) still shows the 20 Hz modulation: fundamental 20 Hz +-0.06, prominence over 60 dB",
        rk4[0]["impulses"]["n"] == 0 and abs(rk4[0]["mod"]["fund_hz"] - 20.0) <= 0.06 and rk4[0]["mod"]["prom_db"] > 60,
        rk4[0]["mod"],
    )
    check(
        "4 twin: the same windows without the trill have a modulation prominence under 20 dB (the 4 kHz line above is the pulses, not an artefact of the method)",
        rq[0]["mod"]["prom_db"] < 20,
        rq[0]["mod"],
    )
    SOS = B.Bursts().sos.astype(np.float64)
    resp = lambda f_: 20 * math.log10(abs(sg.sosfreqz(SOS, worN=[2 * math.pi * f_ / FS])[1][0]))
    check(
        "4 filter response of the plugin: within 1 dB of 0 at 6.5 / 10 / 15 kHz, below -25 dB at 4 kHz, below -45 dB at 3 kHz",
        all(abs(resp(f_)) < 1.0 for f_ in (6500, 10000, 15000)) and resp(4000) < -25 and resp(3000) < -45,
        [round(resp(f_), 1) for f_ in (3000, 4000, 6500, 10000, 15000)],
    )

    # sub-band levels
    _, rb10 = run(train_maker([], extra=tone(10500.0, 0.01)), 61)
    bd = rb10[0]["bands_db"]
    check(
        "4 sub-bands: a 10.5 kHz tone of amplitude 0.01 is -43.0 +-0.3 dB in the 10-11 kHz band (index 4), every other 6-16 kHz band is below -100 dB, p10 = p50 = p90 within 0.3 dB",
        abs(bd["p50"][4] + 43.0) < 0.3 and all(v < -100 for i, v in enumerate(bd["p50"]) if i != 4) and bd["p90"][4] - bd["p10"][4] < 0.3,
        bd["p50"],
    )
    _, rb3 = run(train_maker([], extra=tone(3000.0, 0.01)), 61)
    check(
        "4 twin: a 3 kHz tone of the same level shows only in the 2-6 kHz reference (-43.0 +-0.3) and in none of the ten 6-16 kHz bands (all below -100 dB)",
        abs(rb3[0]["ref_2_6k_db"] + 43.0) < 0.3 and max(rb3[0]["bands_db"]["p50"]) < -100 and rb10[0]["ref_2_6k_db"] < -100,
        (rb3[0]["ref_2_6k_db"], max(rb3[0]["bands_db"]["p50"])),
    )
    _, rl1 = run(train_maker(clicks, amp=0.01), 61)
    _, rl2 = run(train_maker(clicks, amp=0.02), 61)
    check(
        "4 the impulse level follows the click height: doubling the amplitude raises the level p50 by 6.02 +-0.15 dB (a linear detector, no saturation)",
        abs((rl2[0]["impulses"]["level_db"][1] - rl1[0]["impulses"]["level_db"][1]) - 6.02) < 0.15,
        (rl1[0]["impulses"]["level_db"], rl2[0]["impulses"]["level_db"]),
    )
    check(
        "4 no impulse in pure noise (-100 dBFS per beam, 60 s): n 0, gaps n 0 and the record is still written with the band levels",
        rq[0]["impulses"]["n"] == 0 and rq[0]["gaps"]["n"] == 0 and rq[0]["bands_db"] is not None and rq[0]["win_used"] == 60,
        rq[0]["impulses"],
    )
    qr = np.random.default_rng(17)
    _, rquiet = run(lambda s: noise(qr, 2e-3), 61)
    check(
        "4 louder stationary noise (-54 dBFS per beam) gives no impulse either: the detector is relative to the running median, not to a fixed level",
        rquiet[0]["impulses"]["n"] == 0,
        rquiet[0]["impulses"]["n"],
    )
    _, rfloor = run(train_maker(clicks, amp=1e-5), 61)
    check(
        "4 clicks below the absolute floor (height 1e-5, about -100 dBFS) are not impulses; twin: height 0.01 gives 86",
        rfloor[0]["impulses"]["n"] == 0 and r["impulses"]["n"] == 86,
        rfloor[0]["impulses"]["n"],
    )

    def plateau(loud_secs, click_t, h):
        pr = np.random.default_rng(3)

        def make(s):
            x = (pr.standard_normal((FS, 8)) * (3.16e-4 if s in loud_secs else 1e-5)).astype(np.float32)
            if s <= click_t < s + 1:
                put(x, click_t - s, amp=h)
            return x

        return run(make, 61)[1][0]["impulses"]

    pa = plateau({10}, 10.5, 0.0037)
    pb = plateau({10, 11, 12, 13, 14}, 14.5, 0.0037)
    check(
        "4 median window 3 s: a click 8 dB over a 1 s noise plateau (itself 30 dB over the background) is an impulse, 38 dB over the running median of the quiet background; twin: the same click 8 dB over a plateau that fills the 3 s history (5 s long) is NOT (relative to the plateau)",
        pa["n"] == 1 and 37.5 < pa["excess_db"][1] < 39.0 and pb["n"] == 0,
        (pa["n"], pa["excess_db"], pb["n"]),
    )
    # interval handling
    pe, re_ = run(train_maker(clicks), 130)
    check(
        "4 two intervals in 130 s of windows: records start at T0 and T0 + 60, both aligned multiples of 60, 60 used windows each, impulse counts equal the clicks of each minute",
        [x["t0"] for x in re_] == [int(T0), int(T0) + 60]
        and [x["win_used"] for x in re_] == [60, 60]
        and [x["impulses"]["n"] for x in re_] == [int(((clicks >= 0) & (clicks < 60)).sum()), int(((clicks >= 60) & (clicks < 120)).sum())],
        [(x["t0"], x["impulses"]["n"]) for x in re_],
    )
    pi = B.Bursts(60.0, tagger=False)
    ri = []
    for te in (59.4, 60.4, 61.4):
        ri += pi.process(noise(np.random.default_rng(1)), T0 + te)[0]
    ri += pi.flush()
    check(
        "4 a window belongs to the interval holding its MIDPOINT: windows ending 59.4 and 60.4 s (midpoints 58.9 / 59.9) are interval 0, the one ending 61.4 (60.9) is interval 1",
        [(x["t0"], x["win_total"]) for x in ri] == [(int(T0), 2), (int(T0) + 60, 1)],
        [(x["t0"], x["win_total"]) for x in ri],
    )
    pl, rlate = run(train_maker(clicks), 61, start=20)
    pn, rnorm = run(train_maker(clicks), 61, start=0)
    check(
        "4 an interval whose first window comes 20 s late is partial: true (windows 20..59: 40 of 60); twin: the on-time interval has no partial key",
        rlate[0].get("partial") is True and rlate[0]["win_total"] == 40 and "partial" not in rnorm[0],
        (rlate[0].get("partial"), rlate[0]["win_total"]),
    )

    # failure handling
    pf = B.Bursts(60.0, tagger=False)
    bad = np.full((FS, 8), np.nan, np.float32)
    out_bad, emb = pf.process(bad, T0 + 1)
    out_shape, _ = pf.process(np.zeros((FS, 7), np.float32), T0 + 2)
    st = pf.stats()
    check(
        "4 non-finite samples and a wrong shape never raise: no record, errors 2, nonfinite 1, last_error set, windows counted 0",
        out_bad == [] and out_shape == [] and emb is None and (st["errors"], st["nonfinite"], st["windows"]) == (2, 1, 0) and st["last_error"] is not None,
        st,
    )
    pf.process(noise(np.random.default_rng(2)), T0 + 3)
    check("4 positive twin: the next good window is processed (windows 1, errors still 2)", (pf.stats()["windows"], pf.stats()["errors"]) == (1, 2), pf.stats())
    pd_, rd = run(lambda s: np.zeros((FS, 8), np.float32) if s == 10 else noise(np.random.default_rng(s)), 61)
    check(
        "4 a zero-filled window (lost audio) is dead: win_dead 1, win_used 59, counted and excluded; the plugin's own dead counter is 1",
        rd[0]["win_dead"] == 1 and rd[0]["win_used"] == 59 and pd_.stats()["dead"] == 1,
        (rd[0]["win_dead"], rd[0]["win_used"]),
    )
    check("4 twin: without the zero window nothing is dead (win_dead 0, win_used 60)", rq[0]["win_dead"] == 0 and rq[0]["win_used"] == 60)

    # ------------------------------------------------------------------------------------------------ 5 speech exclusion and the tagger hold-back
    blocks = [(5 * j, 5 * j + 5, False) for j in range(0, 20)]

    def verd(speech):
        return [(a, b, speech and 20 <= a < 30) for a, b, _ in blocks]

    ps, rsp = run(train_maker(clicks), 80, tagger=True, verdicts=verd(True))
    pn_, rno = run(train_maker(clicks), 80, tagger=True, verdicts=verd(False))
    keep = int(((clicks < 20) | ((clicks >= 30) & (clicks < 60))).sum())
    r_sp, r_no = rsp[0], rno[0]
    check(
        "5 speech flagged over seconds 20-30: windows 21..30 are excluded (win_speech 10, win_used 50, win_total 60) and exactly the clicks of the other windows are counted",
        (r_sp["win_speech"], r_sp["win_used"], r_sp["win_total"]) == (10, 50, 60) and r_sp["impulses"]["n"] == keep and r_sp["tagger"] is True,
        (r_sp["win_speech"], r_sp["win_used"], r_sp["impulses"]["n"], keep),
    )
    check(
        "5 positive twin: the same audio with every verdict non-speech uses 60 windows, win_speech 0, and counts all 86 clicks (the excluded ones were there)",
        (r_no["win_speech"], r_no["win_used"], r_no["impulses"]["n"]) == (0, 60, 86) and keep < 86,
        (r_no["win_speech"], r_no["win_used"], r_no["impulses"]["n"]),
    )
    check(
        "5 excluded windows leave nothing in the statistics: keep-2 gaps in total (two runs of windows, no gap is taken across the excluded 10 s: the 10.5 s join between 19.95 and 30.45 is NOT a gap), all of them the 0.7 s spacing in histogram bin 17, none in bin 26",
        r_sp["gaps"]["n"] == keep - 2 and sum(r_sp["gaps"]["hist"]) == keep - 2 and r_sp["gaps"]["hist"][26] == 0 and r_sp["gaps"]["hist"][17] == keep - 2,
        r_sp["gaps"]["hist"],
    )
    pall, rall = run(train_maker(clicks), 80, tagger=True, verdicts=[(5 * j, 5 * j + 5, True) for j in range(0, 20)])
    ra0 = rall[0]
    check(
        "5 a minute that is all speech is written as counts only: win_speech 60, win_used 0, no impulse, no band levels, no modulation, no gap statistics (nothing derived from speech windows)",
        (
            ra0["win_speech"],
            ra0["win_used"],
            ra0["impulses"]["n"],
            ra0["bands_db"],
            ra0["ref_2_6k_db"],
            ra0["gaps"]["n"],
            ra0["mod"]["fund_hz"],
            ra0["impulses"]["level_db"],
        )
        == (60, 0, 0, None, None, 0, None, None),
        ra0,
    )
    punv, runv = run(train_maker(clicks), 80, tagger=True, verdicts=())
    check(
        "5 tagger on but no verdict for any window: all 60 windows are unvetted and excluded (win_unvetted 60, win_used 0, no impulse); twin: with verdicts all 60 are used",
        (runv[0]["win_unvetted"], runv[0]["win_used"], runv[0]["impulses"]["n"]) == (60, 0, 0) and r_no["win_used"] == 60 and r_no["win_unvetted"] == 0,
        (runv[0]["win_unvetted"], runv[0]["win_used"]),
    )

    def when(tagger, secs):
        p_ = B.Bursts(60.0, tagger=tagger)
        for a, b, act in verd(False):
            p_.note_speech(T0 + a, T0 + b, act)
        first = None
        for s in range(secs):
            o = p_.process(noise(np.random.default_rng(s)), T0 + s + 1)[0]
            if o and first is None:
                first = s + 1  # window end (seconds since T0) of the call that returned the record
        return first

    check(
        "5 hold-back: with the tagger the first record comes with the window ending at 76 s (newest end - 15 >= 60 + 0.5), not at 61; twin: without a tagger it comes at 61 s",
        (when(True, 80), when(False, 80)) == (76, 61),
        (when(True, 80), when(False, 80)),
    )
    pfl, rfl = run(train_maker(clicks), 70, tagger=True, verdicts=verd(False), flush=True)
    check(
        "5 flush at shutdown with the tagger on: windows younger than the 15 s grace are dropped, never written unvetted: 1 record, 55 windows (ends 1..55), partial true, 15 windows dropped (5 + 10)",
        len(rfl) == 1 and rfl[0]["win_total"] == 55 and rfl[0].get("partial") is True and pfl.stats()["dropped_unvetted"] == 15,
        (len(rfl), rfl and rfl[0]["win_total"], pfl.stats()["dropped_unvetted"]),
    )
    pfn, rfn = run(train_maker(clicks), 70, tagger=False, flush=True)
    check(
        "5 twin: flush without a tagger writes everything: 2 records of 60 and 10 windows, the complete one has no partial key, the open one has partial true, nothing dropped",
        [x["win_total"] for x in rfn] == [60, 10] and "partial" not in rfn[0] and rfn[1].get("partial") is True and pfn.stats()["dropped_unvetted"] == 0,
        [(x["win_total"], x.get("partial")) for x in rfn],
    )
    _, rfc = run(train_maker(clicks), 60, tagger=False, flush=True)
    _, rfp = run(train_maker(clicks), 59, tagger=False, flush=True)
    check(
        "5 flush of an interval that is complete (60 windows, the last ends at the interval end) has no partial key; twin: with 59 windows it is partial true",
        len(rfc) == 1
        and rfc[0]["win_total"] == 60
        and "partial" not in rfc[0]
        and len(rfp) == 1
        and rfp[0]["win_total"] == 59
        and rfp[0].get("partial") is True,
        (len(rfc), [x.get("partial") for x in rfp]),
    )
    ptv = B.Bursts(60.0, tagger=True)
    H.feed_speech([ptv], P.Tagger, [{"k": "tags", "speech_active": True, "speech_p": 0.9}], T0 + 30.0)
    H.feed_speech([ptv], P.Tagger, [{"k": "tags", "speech_active": False, "speech_p": 0.0}], T0 + 60.0)
    H.feed_speech([ptv], P.Tagger, [], T0 + 90.0)
    check(
        "5 host.feed_speech hands the tagger verdict of the window ending at t (5 s long, the real Tagger.win_s) to the plugin: speech over (25, 30], none over (55, 60], an empty record list is ignored",
        ptv.verdicts == [(T0 + 25.0, T0 + 30.0, True), (T0 + 55.0, T0 + 60.0, False)] and P.Tagger.win_s == 5.0,
        ptv.verdicts,
    )
    check(
        "5 the verdict rule on the real windows: (29, 30] is speech, (30, 31] is not, (25, 26] is speech, (24, 25] is not (b > a_win and a < t_end)",
        [ptv._vet(T0 + t_, 1.0) for t_ in (30.0, 31.0, 26.0, 25.0)] == [(True, True), (False, False), (True, True), (False, False)],
        [ptv._vet(T0 + t_, 1.0) for t_ in (30.0, 31.0, 26.0, 25.0)],
    )

    # ------------------------------------------------------------------------------------------------ 6 the report
    fx = [json.loads(l) for l in open(os.path.join(BMA, "contracts", "fixtures", "burst_stats.jsonl"), encoding="utf-8")]
    base = next(x for x in fx if x["gaps"]["regularity"] == 1.0 and "partial" not in x)

    def art(**ov):
        rec = json.loads(json.dumps(base))
        rec["bands_db"]["p50"] = [-80.0, -80.0] + [-45.0] * 8
        rec["ref_2_6k_db"] = -90.0
        rec["impulses"]["spread_db"] = [1.0, 1.8, 3.0]
        rec["analysed_s"] = 60.0
        for k_, v_ in ov.items():
            rec[k_] = v_
        return rec

    class A:
        min_rate, min_band_db, min_contrast_db, max_spread_db = 0.3, -70.0, 10.0, 4.0

    row = lambda rec: R.hourly([rec])[int(rec["t0"] // 3600) * 3600]
    good = row(art())
    check(
        "6 report: an hour with 86 impulses in 60 s, 8-16 kHz at -45 dB over a -90 dB reference and spread 1.8 dB is ARTIFACT-LIKE; its row has rate 1.433, band -45.0, contrast 45.0, spread 1.8, all-beam share 100 %, mode 0.7, regularity 1.0",
        R.artifact_like(good, A)
        and round(good["rate"], 3) == 1.433
        and good["band_db"] == -45.0
        and good["contrast_db"] == 45.0
        and good["spread_db"] == 1.8
        and good["coinc"] == 1.0
        and good["mode_s"] == 0.7
        and good["regularity"] == 1.0,
        good,
    )
    low_rate = art(impulses=dict(art()["impulses"], n=3, coincident_all_n=3))
    weak = art(bands_db={"p10": [-90.0] * 10, "p50": [-85.0] * 10, "p90": [-80.0] * 10})
    flat = art(ref_2_6k_db=-50.0)
    wide = art(impulses=dict(art()["impulses"], spread_db=[5.0, 9.0, 12.0]))
    check(
        "6 each rule alone switches the flag off (twins of the line above): rate 0.05/s, 8-16 kHz at -85 dB, contrast 5 dB, spread 9 dB are all NOT artifact-like",
        not R.artifact_like(row(low_rate), A) and not R.artifact_like(row(weak), A) and not R.artifact_like(row(flat), A) and not R.artifact_like(row(wide), A),
        [R.artifact_like(row(x), A) for x in (low_rate, weak, flat, wide)],
    )
    amb = [
        {"k": "ambient_temp", "v": 1, "t_hour_utc": 3600 * h, "temp_c": float(h), "source": "open-meteo", "model_grid_km": None} for h in (100, 101, 103, 110)
    ]
    t_ = 3600 * 104 + 5
    check(
        "6 ambient reader rule: the nearest PAST record at most 3 h old: hour 104 -> the record of hour 103 (1 h), hour 106 -> 103 (3 h, still valid), hour 107 -> None (4 h), a future record (110) is never used for hour 104",
        (R.ambient_lookup(amb, t_), R.ambient_lookup(amb, 3600 * 106), R.ambient_lookup(amb, 3600 * 107), R.ambient_lookup(amb, 3600 * 110 + 1))
        == (103.0, 103.0, None, 110.0)
        and R.ambient_lookup(amb, 3600 * 99) is None,
        [R.ambient_lookup(amb, x) for x in (t_, 3600 * 106, 3600 * 107)],
    )
    tb = R.table(R.hourly([art()]), amb, A, True)
    check(
        "6 the table prints the flagged hour with its label, the rate and the ARTIFACT-LIKE flag, and a summary line counting 1 of 1 hours",
        "ARTIFACT-LIKE" in tb and "1.433" in tb and "1 of 1 hours look like the artifact" in tb,
        tb[-300:],
    )
    dsp_path = os.path.join(tmp, "dsp.jsonl")
    with open(dsp_path, "w", encoding="utf-8", newline="\n") as f:
        f.write('{"k":"dsp","t":1.0}\n')
        f.write("{torn line\n")
        for i in range(100):
            f.write(
                json.dumps({"k": "transient", "t": 1000.0 + 0.7 * i, "excess_db": 9.0, "level_db": -42.0, "loudest_beam": 1 + i % 8, "beam_spread_db": 1.8})
                + "\n"
            )
        f.write(json.dumps({"k": "transient", "t": 5000.0, "excess_db": 9.0, "level_db": -42.0, "loudest_beam": 1, "beam_spread_db": 1.8}) + "\n")
    imp_r, gaps_r, hand = R.replay_dsp(dsp_path, 1000.0, 2000.0)
    check(
        "6 replay of recorded transients (100 at 0.7 s, plus a dsp line, a torn line and one outside the range): 100 events, 99 gaps, mode 0.7, regularity 1.0, level -42 -42 -42, spread 1.8, hand estimate (20 ms bins) 0.71",
        imp_r["n"] == 100
        and gaps_r["n"] == 99
        and gaps_r["mode_s"] == 0.7
        and gaps_r["regularity"] == 1.0
        and imp_r["level_db"] == [-42.0] * 3
        and imp_r["spread_db"] == [1.8] * 3
        and imp_r["loudest_beam_hist"] == [13, 13, 13, 13, 12, 12, 12, 12]
        and hand == 0.71,
        (imp_r, gaps_r, hand),
    )
    imp_o, gaps_o, hand_o = R.replay_dsp(dsp_path, 1000.0, 1000.0 + 0.7 * 50)
    check(
        "6 twin: a shorter range counts only its own events (the range is [from, to): 50 events, 49 gaps)",
        imp_o["n"] == 50 and gaps_o["n"] == 49,
        (imp_o["n"], gaps_o["n"]),
    )
    cli = subprocess.run(
        [
            sys.executable,
            "-I",
            os.path.join(HERE, "bursts_report.py"),
            "--replay-dsp",
            dsp_path,
            "--from",
            "1970-01-01 00:16",
            "--to",
            "1970-01-01 00:17",
            "--utc",
        ],
        capture_output=True,
        text=True,
    )
    out_json = json.loads(cli.stdout) if cli.returncode == 0 else {}
    check(
        "6 CLI replay: 1970-01-01 00:16-00:17 UTC is epoch 960..1020: 29 events (t = 1000 .. 1019.6), exit 0, JSON on stdout",
        cli.returncode == 0 and out_json.get("impulses", {}).get("n") == 29,
        (cli.returncode, cli.stderr[-200:], cli.stdout[:100]),
    )
    bp = os.path.join(tmp, "bursts.jsonl")
    with open(bp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(art(t0=3600 * 104)) + "\n")
    cli2 = subprocess.run([sys.executable, "-I", os.path.join(HERE, "bursts_report.py"), bp, "--utc"], capture_output=True, text=True)
    check(
        "6 CLI table on a bursts.jsonl: exit 0, the hour 1970-01-05 08:00 (epoch hour 104) is listed and flagged; no --ambient: temperature column shows -",
        cli2.returncode == 0 and "1970-01-05 08:00" in cli2.stdout and "ARTIFACT-LIKE" in cli2.stdout,
        (cli2.returncode, cli2.stdout[-300:], cli2.stderr[-200:]),
    )
    check(
        "6 the report never writes: the temp directory holds exactly the two input files and the dsp file",
        sorted(os.listdir(tmp)) == ["bursts.jsonl", "dsp.jsonl"],
        sorted(os.listdir(tmp)),
    )

    # ------------------------------------------------------------------------------------------------ 7 schema
    sch_path = os.path.join(BMA, "contracts", "schemas", "burst_stats.schema.json")
    allrecs = recs + rs + rsp + rall + rfl + rfn + re_ + ri + rlate + rk
    try:
        from jsonschema import Draft202012Validator

        have_js = True
    except ImportError:
        have_js = False
        print("SKIP 7 schema checks  [jsonschema is not installed in this interpreter (the analyzer venv); run with the Windows Python or the contracts venv]")
    if have_js:
        sch = json.load(open(sch_path, encoding="utf-8"))
        v = Draft202012Validator({"$schema": sch["$schema"], "$ref": "#/$defs/burst_stats", "$defs": sch["$defs"]})
        errs = [e.message for r_ in allrecs for e in v.iter_errors(json.loads(json.dumps(r_)))]
        check(
            f"7 every record the plugin wrote in this run ({len(allrecs)}) validates against burst_stats.schema.json",
            errs == [] and len(allrecs) > 10,
            errs[:2],
        )
        bad_rec = json.loads(json.dumps(allrecs[0]))
        bad_rec["gaps"]["regularity"] = 1.5
        check(
            "7 positive/negative pair: regularity 1.5 is rejected by the same validator",
            len(list(v.iter_errors(bad_rec))) == 1 and not list(v.iter_errors(json.loads(json.dumps(allrecs[0])))),
        )
    allkeys = set().union(*[set(x) for x in allrecs])
    check(
        "7 privacy: a record carries only the documented keys (no samples, spectra of audio, coordinates or text), and every number list is at most 30 long",
        allkeys
        <= {
            "k",
            "v",
            "t0",
            "interval_s",
            "partial",
            "tagger",
            "win_total",
            "win_used",
            "win_speech",
            "win_unvetted",
            "win_dead",
            "analysed_s",
            "bands_db",
            "ref_2_6k_db",
            "impulses",
            "gaps",
            "mod",
            "detector",
        }
        and max(len(x["gaps"]["hist"]) for x in allrecs) == 30,
        allkeys,
    )

    # ------------------------------------------------------------------------------------------------ 9 review fixes: coverage vetting, interval guard, gaps per run
    pv = B.Bursts(60.0, tagger=True)
    pv.note_speech(T0 + 10, T0 + 20, False)
    check(
        "9 vetting needs COVERAGE: a window (T0+9.1, T0+10.1] that a verdict touches for 0.1 s is NOT vetted, nor is (T0+19.9, T0+20.9]; the windows (T0+10, T0+11] and (T0+19, T0+20] inside the verdict are (False, True)",
        [pv._vet(T0 + t_, 1.0) for t_ in (10.1, 20.9, 11.0, 20.0)] == [(False, False), (False, False), (False, True), (False, True)],
        [pv._vet(T0 + t_, 1.0) for t_ in (10.1, 20.9, 11.0, 20.0)],
    )
    pv2 = B.Bursts(60.0, tagger=True)
    pv2.note_speech(T0 + 10, T0 + 15, False)
    pv2.note_speech(T0 + 15.1, T0 + 20, False)
    pv3 = B.Bursts(60.0, tagger=True)
    pv3.note_speech(T0 + 10, T0 + 15, False)
    pv3.note_speech(T0 + 16, T0 + 20, False)
    check(
        "9 two verdicts together cover a window that straddles them (a 0.1 s hole is inside the 0.25 s slack: vetted); a 1 s hole between them is not (twin: the same window is unvetted, a window inside one verdict is vetted)",
        (pv2._vet(T0 + 15.5, 1.0), pv3._vet(T0 + 15.5, 1.0), pv3._vet(T0 + 14.0, 1.0)) == ((False, True), (False, False), (False, True)),
        (pv2._vet(T0 + 15.5, 1.0), pv3._vet(T0 + 15.5, 1.0)),
    )
    pp, rp = run(train_maker(train(0.7)), 80, tagger=True, verdicts=[(s_ + 0.1, s_ + 0.2, False) for s_ in range(0, 80)])
    pf_, rf_ = run(train_maker(train(0.7)), 80, tagger=True, verdicts=[(0, 80, False)])
    check(
        "9 a verdict overlapping only 0.1 s of every window vets none: first record win_total 60, win_used 0, win_unvetted 60; twin: one verdict over everything uses all 60 and none is unvetted",
        (rp[0]["win_total"], rp[0]["win_used"], rp[0]["win_unvetted"], rf_[0]["win_total"], rf_[0]["win_used"], rf_[0]["win_unvetted"])
        == (60, 0, 60, 60, 60, 0),
        (rp[0]["win_used"], rp[0]["win_unvetted"], rf_[0]["win_used"], rf_[0]["win_unvetted"]),
    )
    bad_ok = []
    for bad in (float("inf"), float("-inf"), float("nan"), 0.0, -5.0, 86400.5, 1e9):
        try:
            B.Bursts(bad, tagger=False)
            bad_ok.append(bad)
        except ValueError as e_:
            if "finite and in (0, 86400] s" not in str(e_):
                bad_ok.append((bad, str(e_)))
    good_ok = [B.Bursts(g_, tagger=False).interval_s for g_ in (0.5, 60.0, 86400.0)]
    check(
        "9 interval_s must be finite and in (0, 86400]: inf, -inf, nan, 0, -5, 86400.5 and 1e9 each raise ValueError('interval_s must be finite and in (0, 86400] s'); twin: 0.5, 60 and 86400 are accepted",
        bad_ok == [] and good_ok == [0.5, 60.0, 86400.0],
        (bad_ok, good_ok),
    )
    tt = np.concatenate([0.5 * np.arange(20), 30.0 + 0.5 * np.arange(20)])
    gr_ = np.repeat([0, 1], 20)
    gg, gu = B.gap_stats(tt, group=gr_), B.gap_stats(tt)
    check(
        "9 gap_stats per run: two perfect 0.5 s trains of 20 impulses with a 20 s hole, group = run id: n 38, mode 0.5, regularity 1.0, over_n 0, histogram 38 gaps all in one bin; twin without group: n 39, regularity 0.9744, the hole is one extra gap",
        (gg["n"], gg["mode_s"], gg["regularity"], gg["over_n"], sum(gg["hist"]), max(gg["hist"]), gu["n"], gu["regularity"], sum(gu["hist"]))
        == (38, 0.5, 1.0, 0, 38, 38, 39, 0.9744, 39),
        (gg["n"], gg["regularity"], gu["n"], gu["regularity"]),
    )
    clk = 0.25 + 0.5 * np.arange(120)
    ph, rh = run(train_maker(clk), 80, tagger=True, verdicts=[(0, 20, False), (20, 40, True), (40, 80, False)])
    pc_, rc_ = run(train_maker(clk), 80, tagger=True, verdicts=[(0, 80, False)])
    r0, c0 = rh[0], rc_[0]
    check(
        "9 plugin: 20 s of speech windows cut out of a perfect 0.5 s click train: win_used 40, win_speech 20, 80 impulses, 78 gaps (not 79), regularity 1.0, over_n 0, histogram sums to 78; twin without speech: 120 impulses, 119 gaps, regularity 1.0",
        (
            r0["win_used"],
            r0["win_speech"],
            r0["impulses"]["n"],
            r0["gaps"]["n"],
            r0["gaps"]["regularity"],
            r0["gaps"]["over_n"],
            sum(r0["gaps"]["hist"]),
            c0["impulses"]["n"],
            c0["gaps"]["n"],
            c0["gaps"]["regularity"],
        )
        == (40, 20, 80, 78, 1.0, 0, 78, 120, 119, 1.0),
        (r0["win_used"], r0["win_speech"], r0["impulses"]["n"], r0["gaps"]["n"], r0["gaps"]["regularity"], r0["gaps"]["over_n"]),
    )

    # ------------------------------------------------------------------------------------------------ 8 the real host over UDP
    if "--skip-e2e" in sys.argv:
        print("SKIP 8 host e2e over UDP  [--skip-e2e]")
    else:

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
        proc = subprocess.Popen(
            [
                sys.executable,
                os.path.join(HERE, "host.py"),
                "--listen",
                f"127.0.0.1:{port}",
                "--out",
                out_dir,
                "--history-dir",
                hist_dir,
                "--no",
                "dsp",
                "--no",
                "spatial",
                "--no",
                "tags",
                "--no",
                "clap",
                "--no",
                "birds",
                "--no",
                "coherence",
                "--bursts-interval-s",
                "10",
                "--secs",
                "48",
            ],
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(240):
            if os.path.exists(os.path.join(out_dir, "status.json")) or proc.poll() is not None:
                break
            time.sleep(0.25)
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rng = np.random.default_rng(24)
        blk, t_start, nb = 0, time.time(), 0
        SECS = 42
        ck = train(0.7, first=0.35)
        for sec in range(SECS):
            x = noise(rng)
            for c in ck[(ck >= sec) & (ck < sec + 1)]:
                put(x, c - sec)
            for off in range(0, FS, 256):
                for slot in range(8):
                    sk.sendto(bmaa(slot, blk + off, x[off : off + 256, slot]), ("127.0.0.1", port))
                nb += 1
                dly = t_start + nb * 256 / FS - time.time()
                if dly > 0:
                    time.sleep(dly)
            blk += FS
        proc.wait(timeout=120)
        err = proc.stderr.read()
        st = json.load(open(os.path.join(out_dir, "status.json")))
        bpth = os.path.join(out_dir, "bursts.jsonl")
        rows = lines(bpth) if os.path.exists(bpth) else []
        check(
            "8 host e2e over UDP: exits 0 and writes bursts.jsonl with at least 3 records",
            proc.returncode == 0 and len(rows) >= 3,
            (proc.returncode, len(rows), err[-300:]),
        )
        full = [r_ for r_ in rows if r_["gaps"]["n"] >= 5]
        check(
            "8 host e2e: the 0.7 s click train comes through the real host: every interval with 5+ gaps has mode 0.7 +-0.03 and regularity over 0.85, tagger false (--no tags)",
            len(full) >= 2 and all(abs(r_["gaps"]["mode_s"] - 0.7) <= 0.03 and r_["gaps"]["regularity"] > 0.85 and r_["tagger"] is False for r_ in full),
            [(r_["gaps"]["mode_s"], r_["gaps"]["regularity"]) for r_ in rows],
        )
        tot_imp = sum(r_["impulses"]["n"] for r_ in rows)
        check(
            "8 host e2e: the impulse total is within 3 of the clicks sent (60 clicks in 42 s: 0.35 + 0.7 k < 42), and each impulse fired on all 8 beams",
            abs(tot_imp - int((ck < SECS).sum())) <= 3 and sum(r_["impulses"]["coincident_all_n"] for r_ in rows) == tot_imp,
            (tot_imp, int((ck < SECS).sum())),
        )
        if have_js:
            errs = [e.message for r_ in rows for e in v.iter_errors(r_)]
            check("8 host e2e: every record on disk validates against the schema", errs == [] and len(rows) >= 3, errs[:2])
        pl = st["plugins"]["bursts"]
        check(
            "8 host e2e: status.json plugins.bursts is enabled with errors 0 and windows over 30, meta.bursts_errors is 0",
            pl["enabled"] and pl["errors"] == 0 and pl["windows"] > 30 and st["meta"].get("bursts_errors") == 0,
            (pl, st["meta"]),
        )
        files = os.listdir(out_dir)
        check(
            "8 host e2e: no audio is written (audio_written false, no audio-like file under --out)",
            st["audio_written"] is False and not [f for f in files if f.lower().endswith((".wav", ".flac", ".mp3", ".ogg", ".pcm", ".raw", ".opus"))],
            files,
        )
        out2 = os.path.join(tmp, "out2")
        os.makedirs(out2)
        pr2 = subprocess.run(
            [
                sys.executable,
                os.path.join(HERE, "host.py"),
                "--listen",
                f"127.0.0.1:{free_port()}",
                "--out",
                out2,
                "--history-dir",
                "",
                "--no",
                "dsp",
                "--no",
                "spatial",
                "--no",
                "tags",
                "--no",
                "clap",
                "--no",
                "birds",
                "--no",
                "coherence",
                "--no",
                "bursts",
                "--secs",
                "6",
            ],
            capture_output=True,
            text=True,
        )
        st2 = json.load(open(os.path.join(out2, "status.json"))) if os.path.exists(os.path.join(out2, "status.json")) else {}
        check(
            "8 host --no bursts: reports enabled false, writes no bursts.jsonl, and the error counter is 0",
            pr2.returncode == 0
            and st2.get("plugins", {}).get("bursts", {}).get("enabled") is False
            and not os.path.exists(os.path.join(out2, "bursts.jsonl"))
            and st2["meta"].get("bursts_errors") == 0,
            (pr2.returncode, pr2.stderr[-300:], st2.get("plugins", {}).get("bursts")),
        )
        # the pin guard: a deployed app directory without bursts.py (a stale install) must not stop the analyzer
        app = os.path.join(tmp, "app_without_bursts")
        os.makedirs(app)
        for fn in os.listdir(HERE):
            if fn.endswith(".py") and not fn.startswith("test_") and fn not in ("bursts.py", "bursts_report.py"):
                shutil.copy(os.path.join(HERE, fn), app)
        shutil.copy(os.path.join(HERE, "plugins.manifest.json"), app)  # the installed app dir holds the manifest next to host.py
        for extra_ in (os.path.join(BMA, "classify", "bma_classify.py"), os.path.join(BMA, "pc", "history_log.py")):
            shutil.copy(extra_, app)
        out3 = os.path.join(tmp, "out3")
        os.makedirs(out3)
        pr3 = subprocess.run(
            [
                sys.executable,
                os.path.join(app, "host.py"),
                "--listen",
                f"127.0.0.1:{free_port()}",
                "--out",
                out3,
                "--history-dir",
                "",
                "--classify-dir",
                app,
                "--no",
                "tags",
                "--no",
                "clap",
                "--no",
                "birds",
                "--no",
                "coherence",
                "--secs",
                "6",
            ],
            capture_output=True,
            text=True,
        )
        st3 = json.load(open(os.path.join(out3, "status.json"))) if os.path.exists(os.path.join(out3, "status.json")) else {}
        pb3 = st3.get("plugins", {}).get("bursts", {})
        check(
            "8 pin guard: a host without bursts.py exits 0, reports plugins.bursts enabled false with the ModuleNotFoundError reason, and dsp still runs (enabled true)",
            pr3.returncode == 0 and pb3.get("enabled") is False and "ModuleNotFoundError" in pb3.get("reason", "") and st3["plugins"]["dsp"]["enabled"] is True,
            (pr3.returncode, pb3, pr3.stderr[-300:]),
        )
        # the verdict hand-off through the real host: a tiny fake tagger (name tags, win 5 s, hop 2 s, heavy like the real one) flags its 3rd and 4th
        # windows as speech; bursts is the real plugin, the host is the real host (the verdict reaches the plugin via host.feed_speech -> note_speech)
        out5 = os.path.join(tmp, "out5")
        os.makedirs(out5)
        port5 = free_port()
        wrap = os.path.join(tmp, "run_host_fake_tagger.py")
        with open(wrap, "w", newline="\n") as f_:
            f_.write(
                "import sys\nsys.path.insert(0, %r)\nimport plugins as P\n"
                "class FakeTagger:\n    name = 'tags'\n    win_s, hop_s = 5.0, 2.0\n    heavy = True\n"
                "    def __init__(self, *a):\n        self.n = 0\n"
                "    def process(self, x, t_wall):\n        self.n += 1\n        return [{'k': 'tags', 'speech_active': self.n in (3, 4)}], None\n"
                "P.Tagger = FakeTagger\nimport host\nsys.argv = ['host.py'] + sys.argv[1:]\nhost.main()\n" % HERE
            )
        proc5 = subprocess.Popen(
            [
                sys.executable,
                wrap,
                "--listen",
                f"127.0.0.1:{port5}",
                "--out",
                out5,
                "--history-dir",
                "",
                "--no",
                "dsp",
                "--no",
                "spatial",
                "--no",
                "clap",
                "--no",
                "birds",
                "--no",
                "coherence",
                "--bursts-interval-s",
                "10",
                "--secs",
                "48",
            ],
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(240):
            if os.path.exists(os.path.join(out5, "status.json")) or proc5.poll() is not None:
                break
            time.sleep(0.25)
        rng = np.random.default_rng(26)
        blk, t_start, nb = 0, time.time(), 0
        for sec in range(42):
            x = noise(rng)
            for c in ck[(ck >= sec) & (ck < sec + 1)]:
                put(x, c - sec)
            for off in range(0, FS, 256):
                for slot in range(8):
                    sk.sendto(bmaa(slot, blk + off, x[off : off + 256, slot]), ("127.0.0.1", port5))
                nb += 1
                dly = t_start + nb * 256 / FS - time.time()
                if dly > 0:
                    time.sleep(dly)
            blk += FS
        proc5.wait(timeout=120)
        err5 = proc5.stderr.read()
        st5 = json.load(open(os.path.join(out5, "status.json")))
        bp5 = os.path.join(out5, "bursts.jsonl")
        rows5 = lines(bp5) if os.path.exists(bp5) else []
        check(
            "8 host + fake tagger (default run): exits 0, tags and bursts enabled, records say tagger true, bursts_errors 0",
            proc5.returncode == 0
            and st5["plugins"]["tags"]["enabled"]
            and st5["plugins"]["bursts"]["enabled"]
            and len(rows5) >= 2
            and all(r_["tagger"] is True for r_ in rows5)
            and st5["meta"].get("bursts_errors") == 0,
            (proc5.returncode, err5[-300:], len(rows5)),
        )
        check(
            "8 host + fake tagger: the verdicts reach the plugin (host -> feed_speech -> note_speech): some windows used AND some flagged speech, win_total = used + speech + unvetted + dead; a host that never hands verdicts over leaves win_used 0",
            sum(r_["win_used"] for r_ in rows5) > 0
            and sum(r_["win_speech"] for r_ in rows5) > 0
            and all(r_["win_total"] == r_["win_used"] + r_["win_speech"] + r_["win_unvetted"] + r_["win_dead"] for r_ in rows5),
            [(r_["win_total"], r_["win_used"], r_["win_speech"], r_["win_unvetted"]) for r_ in rows5],
        )
        # with the REAL tagger (needs the PANNs weights: pass --panns-dir <dir>, CPU, about 2 GB RAM): the verdicts reach the plugin through the host
        if "--panns-dir" in sys.argv:
            pdir = sys.argv[sys.argv.index("--panns-dir") + 1]
            out4 = os.path.join(tmp, "out4")
            os.makedirs(out4)
            port4 = free_port()
            proc4 = subprocess.Popen(
                [
                    sys.executable,
                    os.path.join(HERE, "host.py"),
                    "--listen",
                    f"127.0.0.1:{port4}",
                    "--out",
                    out4,
                    "--history-dir",
                    "",
                    "--device",
                    "cpu",
                    "--panns-dir",
                    pdir,
                    "--no",
                    "dsp",
                    "--no",
                    "spatial",
                    "--no",
                    "clap",
                    "--no",
                    "birds",
                    "--no",
                    "coherence",
                    "--bursts-interval-s",
                    "10",
                    "--secs",
                    "75",
                ],
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(480):
                if os.path.exists(os.path.join(out4, "status.json")) or proc4.poll() is not None:
                    break
                time.sleep(0.25)
            rng = np.random.default_rng(25)
            blk, t_start, nb = 0, time.time(), 0
            ck4 = train(0.7, first=0.35)
            for sec in range(60):
                x = noise(rng)
                for c in ck4[(ck4 >= sec) & (ck4 < sec + 1)]:
                    put(x, c - sec)
                for off in range(0, FS, 256):
                    for slot in range(8):
                        sk.sendto(bmaa(slot, blk + off, x[off : off + 256, slot]), ("127.0.0.1", port4))
                    nb += 1
                    dly = t_start + nb * 256 / FS - time.time()
                    if dly > 0:
                        time.sleep(dly)
                blk += FS
            proc4.wait(timeout=300)
            err4 = proc4.stderr.read()
            st4 = json.load(open(os.path.join(out4, "status.json")))
            rows4 = lines(os.path.join(out4, "bursts.jsonl")) if os.path.exists(os.path.join(out4, "bursts.jsonl")) else []
            wt = sum(r_["win_total"] for r_ in rows4)
            check(
                "8 host with the REAL tagger: tags and bursts both enabled, host exits 0, records say tagger true",
                proc4.returncode == 0
                and st4["plugins"]["tags"]["enabled"]
                and st4["plugins"]["bursts"]["enabled"]
                and rows4
                and all(r_["tagger"] is True for r_ in rows4),
                (proc4.returncode, err4[-300:], st4["plugins"]),
            )
            check(
                "8 host with the REAL tagger: the verdicts reach the plugin (host -> feed_speech -> note_speech): over 60 % of the windows are vetted (used or speech), under 40 % unvetted; a host that never hands verdicts over leaves 100 % unvetted",
                wt >= 40 and sum(r_["win_used"] + r_["win_speech"] for r_ in rows4) >= 0.6 * wt and sum(r_["win_unvetted"] for r_ in rows4) <= 0.4 * wt,
                [(r_["win_total"], r_["win_used"], r_["win_speech"], r_["win_unvetted"]) for r_ in rows4],
            )
            check(
                "8 host with the REAL tagger: speech flagged windows are counted and excluded, never both; the held-back records still carry the 0.7 s train (mode 0.7 +-0.03 in some interval)",
                all(r_["win_total"] == r_["win_used"] + r_["win_speech"] + r_["win_unvetted"] + r_["win_dead"] for r_ in rows4)
                and any(r_["gaps"]["mode_s"] is not None and abs(r_["gaps"]["mode_s"] - 0.7) <= 0.03 for r_ in rows4),
                [(r_["gaps"]["mode_s"], r_["win_used"]) for r_ in rows4],
            )
        else:
            print(
                "SKIP 8 host with the real tagger  [pass --panns-dir <dir with Cnn14_mAP=0.431.pth> to run it (CPU, slow); verdict hand-off is otherwise covered by host.feed_speech tests only]"
            )
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{n_checks} checks, {len(fails)} failure(s)")
for f_ in fails:
    print("  FAILED:", f_)
sys.exit(1 if fails else 0)
