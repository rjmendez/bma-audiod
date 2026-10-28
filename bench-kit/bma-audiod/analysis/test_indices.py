#!/usr/bin/env python3
"""Tests for the indices plugin (analysis/indices.py), its report (indices_report.py), the scikit-maad cross-check (compare_maad.py) and the
host wiring. Every signal is synthetic with a fixed seed; the REAL Indices class and the REAL host run on them (only the PANNs tagger is replaced,
in the host test, by a small stand-in that flags speech by clock time: the model is not available to Windows python). Script style
(check(name, ok, detail), exit 1 on a failure) like test_coherence.py. Temp files only under tempfile. Run:  python -I test_indices.py [--skip-e2e]
The maad check needs scikit-maad on PYTHONPATH (a scratch overlay: pip install --target <dir> --no-deps scikit-maad plus its pure dependencies) and is
skipped with the reason printed when it is absent; the schema checks need `jsonschema` (skipped likewise).

Derivations used below (bins are exactly 100 Hz wide, frames 480 samples, a 5 s window = 500 frames)
  * white noise: amplitudes are Rayleigh, E|X1 - X2| / E X = 2 - sqrt 2, so ACI per bin = (2 - sqrt 2) (n - 1) / n and over 100 bins 58.46 (n = 500);
    1 - Hf = (4/pi - 1) / (2 K ln N) = 5.9e-5; for a Rayleigh envelope Ht = 1 - 0.139028 / ln M with M = 50000 baseband samples (0.987148),
    0.139028 = 0.5 ln(pi/2) - 0.5 (psi(1.5) + ln 2).
  * a sine of amplitude A on a bin centre fills the bins k-1, k, k+1 in the ratio 1/4 : 1/2 : 1/4 of amplitude: Hf = H(.25, .5, .25) / ln 100 = 0.22577;
    its mean square is A^2 / 2 (level 20 log10(A / sqrt 2)); 5/6 of its power lies at or above its own bin edge (bin k-1 is outside a band starting at k):
    a 4 kHz tone reads 10 log10(5/6) = -0.792 dB lower in the 4-12 kHz band.
  * a 4 kHz tone gated in whole 10-frame blocks (10 on, 10 off, 25 periods): the three tone bins each have sum|dI| / sum I = 49 / 250, so ACI = 0.588 exactly;
    the envelope is zero half of the time, so Ht = ln(M/2) / ln M = 1 - ln 2 / ln M.
  * NDSI of a tone: -1 at 1.5 kHz, +1 at 5 kHz, 0.1 at 1.5 kHz plus 0.2 at 5 kHz: power ratio 4, (4 - 1) / (4 + 1) = 0.6."""

import json, math, os, shutil, socket, struct, subprocess, sys, tempfile, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
BMA = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(BMA, "contracts", "tools"))
import indices as I  # noqa: E402
import indices_report as R  # noqa: E402

fails, n_checks = [], 0
FS, N = 48000, 240000
LN2 = math.log(2.0)


def check(name, ok, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  [{detail}]"))
    if not ok:
        fails.append(name)


def db(a):
    return 10 ** (a / 20.0)


T = np.arange(N) / FS


def white(rng, level_db=-60.0):
    return rng.standard_normal(N) * db(level_db)


def sine(f, a):
    return a * np.sin(2 * np.pi * f * T)


def beams(mono, spread=None):
    """float32 (N, 8) window whose beam mean is `mono` (the same signal on every beam)."""
    return np.repeat(np.asarray(mono, np.float32)[:, None], 8, axis=1)


def feed(p, x, t):
    return p.process(x, t)[0]


def plugin(**kw):
    kw.setdefault("interval_s", 60.0)
    kw.setdefault("hop_s", 15.0)
    kw.setdefault("grace_s", 30.0)
    return I.Indices(**kw)


tmp = tempfile.mkdtemp(prefix="ind_")
try:
    rng = np.random.default_rng(11)
    # ------------------------------------------------------------------------------------------------ 1 pure helpers, exact
    check(
        "1 entropy_norm: a flat vector is 1, one line is 0, two equal of four is ln2/ln4 = 0.5",
        abs(I.entropy_norm([1] * 8) - 1.0) < 1e-12 and I.entropy_norm([5, 0, 0, 0]) == 0.0 and abs(I.entropy_norm([1, 1, 0, 0]) - 0.5) < 1e-12,
        (I.entropy_norm([1] * 8), I.entropy_norm([5, 0, 0, 0]), I.entropy_norm([1, 1, 0, 0])),
    )
    check(
        "1 entropy_norm: all zeros and a single entry are nan (undefined), never a number",
        math.isnan(I.entropy_norm([0, 0, 0])) and math.isnan(I.entropy_norm([3])),
    )
    A2 = np.array([[1, 1], [1, 3], [1, 1], [1, 3]], float)
    check(
        "1 aci_from_amplitude: constant bin 0 adds 0, bin [1,3,1,3] adds 6/8 = 0.75 -> 0.75 exactly",
        abs(I.aci_from_amplitude(A2) - 0.75) < 1e-12,
        I.aci_from_amplitude(A2),
    )
    A3 = np.hstack([A2, np.array([[0, 1e-3, 0, 1e-3]], float).T])
    A4 = np.hstack([A2, np.array([[0, 1e-12, 0, 1e-12]], float).T])
    check(
        "1 aci_from_amplitude: a quiet bin above the silence floor ([0,1e-3,0,1e-3]: 3e-3/2e-3 = 1.5) counts, a silent one (1e-12) adds 0 (positive twin)",
        abs(I.aci_from_amplitude(A3) - 2.25) < 1e-9 and abs(I.aci_from_amplitude(A4) - 0.75) < 1e-12,
        (I.aci_from_amplitude(A3), I.aci_from_amplitude(A4)),
    )

    # ------------------------------------------------------------------------------------------------ 2 white noise: closed forms
    w = I.analyze(white(rng))
    aci_exp = (2 - math.sqrt(2)) * 499 / 500 * 100
    hf_exp = 1 - (4 / math.pi - 1) / (2 * 500 * math.log(100))
    c_ht = 0.5 * math.log(math.pi / 2) - 0.5 * (0.03648997397857652 + LN2)
    ht_exp = 1 + c_ht / math.log(50000)
    check(f"2 white noise: ACI {w['aci']:.2f} = (2 - sqrt 2)(n-1)/n * 100 = {aci_exp:.2f} within 0.6", abs(w["aci"] - aci_exp) < 0.6, (w["aci"], aci_exp))
    check(
        f"2 white noise: Hf {w['hf']:.6f} = {hf_exp:.6f} within 3e-5 and Ht {w['ht']:.5f} = {ht_exp:.5f} within 5e-4",
        abs(w["hf"] - hf_exp) < 3e-5 and abs(w["ht"] - ht_exp) < 5e-4,
        (w["hf"], hf_exp, w["ht"], ht_exp),
    )
    check(
        "2 white noise: level -60 dBFS within 0.05, chorus band 8/24 of the power (-64.77 dBFS) within 0.1, NDSI = (6-1)/(6+1) = 0.714 within 0.01 (bins 60:10)",
        abs(w["level_dbfs"] + 60.0) < 0.05 and abs(w["chorus_dbfs"] - (-60.0 + 10 * math.log10(8 / 24))) < 0.1 and abs(w["ndsi"] - 5 / 7) < 0.01,
        w,
    )

    # ------------------------------------------------------------------------------------------------ 3 tones
    tone = I.analyze(sine(4000, 0.1) + rng.standard_normal(N) * 1e-6)
    hf_tone = -(2 * 0.25 * math.log(0.25) + 0.5 * math.log(0.5)) / math.log(100)
    check(
        f"3 pure 4 kHz tone: level {tone['level_dbfs']:.4f} = 20 log10(0.1 / sqrt 2) = -23.0103, chorus band {tone['chorus_dbfs']:.3f} = level + 10 log10(5/6)",
        abs(tone["level_dbfs"] + 23.0103) < 0.001 and abs(tone["chorus_dbfs"] - (-23.0103 + 10 * math.log10(5 / 6))) < 0.01,
        tone,
    )
    check(
        f"3 pure tone: Hf {tone['hf']:.5f} = H(.25,.5,.25)/ln100 = {hf_tone:.5f} within 5e-4, Ht > 0.9999 (constant envelope), NDSI > 0.99999",
        abs(tone["hf"] - hf_tone) < 5e-4 and tone["ht"] > 0.9999 and tone["ndsi"] > 0.99999,
        tone,
    )
    check(
        "3 ordering: tone Hf is far below white-noise Hf and tone Ht is above white-noise Ht (positive twin of the limits in 2)",
        tone["hf"] < 0.3 < w["hf"] and tone["ht"] > w["ht"],
        (tone["hf"], w["hf"], tone["ht"], w["ht"]),
    )
    steady = I.analyze(sine(4000, 0.1))
    gate = (np.floor(np.arange(N) / (10 * I.NFFT)) % 2 == 0).astype(float)
    gated = I.analyze(sine(4000, 0.1) * gate)
    check(
        "3 steady noiseless tone: ACI is 0 (identical frames, silent bins add 0) and Ht is 1",
        abs(steady["aci"]) < 1e-6 and abs(steady["ht"] - 1.0) < 1e-9,
        steady,
    )
    check(
        f"3 4 kHz tone gated in 10-frame blocks: ACI = 3 * 49 / 250 = 0.588 within 1e-6 and Ht = 1 - ln2/ln50000 = {1 - LN2 / math.log(50000):.4f} within 0.01",
        abs(gated["aci"] - 0.588) < 1e-6 and abs(gated["ht"] - (1 - LN2 / math.log(50000))) < 0.01,
        gated,
    )
    check("3 ordering: gated ACI above steady ACI, gated Ht below steady Ht", gated["aci"] > steady["aci"] + 0.5 and gated["ht"] < steady["ht"] - 0.05)
    low, high = I.analyze(sine(1500, 0.1)), I.analyze(sine(5000, 0.1))
    mix = I.analyze(sine(1500, 0.1) + sine(5000, 0.2))
    eq = I.analyze(sine(1500, 0.1) + sine(5000, 0.1))
    check(
        "4 NDSI sign: 1.5 kHz tone -1 and 5 kHz tone +1 (exactly, to 1e-9), equal-amplitude mix 0, 0.1 + 0.2 amplitudes 0.6",
        abs(low["ndsi"] + 1) < 1e-9 and abs(high["ndsi"] - 1) < 1e-9 and abs(eq["ndsi"]) < 1e-9 and abs(mix["ndsi"] - 0.6) < 1e-9,
        (low["ndsi"], high["ndsi"], eq["ndsi"], mix["ndsi"]),
    )
    bi_t4 = I.analyze(sine(4000, 0.1) + white(rng, -80))["bi"]
    bi_t15 = I.analyze(sine(1500, 0.1) + white(rng, -80))["bi"]
    check(
        f"4 BI: a 4 kHz tone 60 dB above its noise floor reads {bi_t4:.1f} dB*kHz (> 20), the same tone at 1.5 kHz (outside 2-8 kHz) reads {bi_t15:.1f} (< 5) and white noise {w['bi']:.1f} (between 2 and 4)",
        bi_t4 > 20.0 and bi_t15 < 5.0 and 2.0 < w["bi"] < 4.0,
        (bi_t4, bi_t15, w["bi"]),
    )
    check(
        "4 silence: analyze returns None (dead window), and a -90 dBFS noise is NOT dead (positive twin)",
        I.analyze(np.zeros(N)) is None and I.analyze(white(rng, -90.0)) is not None and I.analyze(white(rng, -110.0)) is None,
    )
    try:
        I.analyze(np.zeros(100))
        check("4 a window shorter than 10 frames is refused", False, "no exception")
    except ValueError as e:
        check("4 a window shorter than 10 frames is refused (ValueError mentioning the length)", "shorter than 10 frames" in str(e), str(e))

    # ------------------------------------------------------------------------------------------------ 5 plugin = analyze, aggregation, boundaries
    p = plugin(speech_gate=False, grace_s=0.0)
    x1 = beams(white(rng, -55.0))
    r = feed(p, x1, 125.0) + feed(p, x1, 185.0)  # interval 60 s: bucket 2 = [120, 180), the second window closes it (grace 0)
    ref = I.analyze(x1.mean(axis=1, dtype=np.float64))
    rec = r[0] if r else {}
    check(
        "5 plugin: the record of one window holds exactly the analyze() values (to the 4 decimals written), through the real process()",
        bool(r) and all(abs(rec["indices"][k]["p50"] - ref[k]) < 6e-5 and rec["indices"][k]["p10"] == rec["indices"][k]["p90"] for k in I.NAMES),
        (rec.get("indices"), ref),
    )
    p = plugin(speech_gate=False, grace_s=0.0)
    lv = [-60.0, -50.0, -40.0]
    out = []
    for i, l in enumerate(lv):
        out += feed(p, beams(white(rng, l)), 125.0 + 15 * i)
    out += feed(p, beams(white(rng, -50)), 185.0)
    li = out[0]["indices"]["level_dbfs"] if out else {}
    check(
        "5 aggregation: three windows at -60, -50, -40 dBFS give p10 -58, p50 -50, p90 -42 (linear interpolation) within 0.05",
        bool(out) and out[0]["win_used"] == 3 and abs(li["p10"] + 58) < 0.05 and abs(li["p50"] + 50) < 0.05 and abs(li["p90"] + 42) < 0.05,
        li,
    )

    p = plugin(speech_gate=False)  # interval 60 s, grace 30 s (boundary tests are about time, not the gate)
    recs = []
    for t in (65.0, 80.0, 95.0, 110.0):
        recs += feed(p, beams(white(rng)), t)
    recs += feed(p, beams(white(rng)), 125.0)  # next bucket
    recs += feed(p, beams(white(rng)), 149.9)
    check("5 boundary: nothing is emitted before the interval's end + grace (t = 149.9 < 120 + 30)", recs == [], len(recs))
    recs += feed(p, beams(white(rng)), 150.0)
    check(
        "5 boundary: at t = 150 the interval [60, 120) closes with exactly its 4 windows, t0 60 and t 120 (positive twin of the 149.9 check)",
        len(recs) == 1 and (recs[0]["t0"], recs[0]["t"], recs[0]["win_total"], recs[0]["win_used"]) == (60.0, 120.0, 4, 4),
        [(r_["t0"], r_["win_total"]) for r_ in recs],
    )
    fl = p.flush()
    check(
        "5 flush: the open interval [120,180) with 3 windows is emitted partial at shutdown, and a second flush returns nothing",
        len(fl) == 1 and fl[0]["t0"] == 120.0 and fl[0]["win_total"] == 3 and fl[0].get("partial") is True and p.flush() == [],
        [(f_["t0"], f_["win_total"]) for f_ in fl],
    )
    late = feed(p, beams(white(rng)), 100.0)
    check("5 late window: one for an interval already closed is counted (stats.late) and written nowhere", late == [] and p.stats()["late"] == 1, p.stats())
    pl2, pe2 = plugin(grace_s=0.0, speech_gate=False), plugin(grace_s=0.0, speech_gate=False)
    a_ = feed(pl2, beams(white(rng)), 100.0) + feed(pl2, beams(white(rng)), 125.0)
    b_ = feed(pe2, beams(white(rng)), 65.0) + feed(pe2, beams(white(rng)), 125.0)
    check(
        "5 partial on a late start (close path): first interval entered at 40 s is partial true, entered at 5 s has no partial key",
        len(a_) == 1 and a_[0].get("partial") is True and len(b_) == 1 and "partial" not in b_[0],
        (a_, b_),
    )

    # ------------------------------------------------------------------------------------------------ 6 speech exclusion
    def run_speech(tags, levels, gate=True):
        """windows at t = 65, 80, 95, 110 (interval [60, 120)), the given levels; tags = [(t_end, speech)] of 5 s tagger windows; -> (records, plugin)"""
        pp = plugin(speech_gate=gate)
        for t_end, sp in tags:
            pp.note_tag(t_end, sp, 5.0)
        rr = []
        for t, l in zip((65.0, 80.0, 95.0, 110.0), levels):
            rr += feed(pp, beams(white(rng, l)), t)
        rr += feed(pp, beams(white(rng, -60)), 160.0)  # closes the interval (>= 120 + 30)
        return rr, pp

    quiet_loud = [-60.0, -20.0, -60.0, -60.0]
    all_clear = [(65.0, False), (80.0, False), (95.0, False), (110.0, False)]
    rr, pp = run_speech([(65.0, False), (80.0, True), (95.0, False), (110.0, False)], quiet_loud)
    rec = rr[0] if rr else {}
    check(
        "6 speech: the window overlapping a speech-flagged tagger window is excluded (win_speech 1, win_used 3) and its -20 dBFS level appears nowhere (p90 level < -59)",
        bool(rr) and (rec["win_total"], rec["win_used"], rec["win_speech"]) == (4, 3, 1) and rec["indices"]["level_dbfs"]["p90"] < -59.0,
        rec,
    )
    rr2, _ = run_speech(all_clear, quiet_loud)
    check(
        "6 speech twin: the same windows with the tagger saying 'not speech' keep all 4, and the loud window shows (p90 level above -45)",
        bool(rr2) and (rr2[0]["win_used"], rr2[0]["win_speech"], rr2[0]["win_untagged"]) == (4, 0, 0) and rr2[0]["indices"]["level_dbfs"]["p90"] > -45.0,
        rr2[:1],
    )
    rr3, p3 = run_speech([(65.0, True), (80.0, True), (95.0, True), (110.0, True)], quiet_loud)
    check(
        "6 speech: an interval whose every window is speech writes NO record (counted in stats.suppressed), the final window opens the next one",
        [r_ for r_ in rr3 if r_["t0"] == 60.0] == [] and p3.stats()["suppressed"] == 1 and p3.stats()["speech"] == 4,
        (p3.stats(), len(rr3)),
    )
    rr4, _ = run_speech([(65.0, False), (80.0, True), (95.0, False), (110.0, False)], quiet_loud, gate=False)
    check(
        "6 speech gate off: speech flags are ignored, all 4 windows are used and the record says speech_gate false (the host only allows this with its explicit flag)",
        bool(rr4)
        and rr4[0]["win_used"] == 4
        and rr4[0]["win_speech"] == 0
        and rr4[0]["speech_gate"] is False
        and rr4[0]["indices"]["level_dbfs"]["p90"] > -45.0,
        rr4[:1],
    )
    rr5, _ = run_speech([(65.0 - 5.0, True), (65.0, False), (80.0, False), (95.0, False), (110.0, False)], [-60.0] * 4)
    check(
        "6 overlap is strict: a speech tagger window ending exactly where the window starts ([55, 60] vs [60, 65]) excludes nothing (win_speech 0, all 4 used)",
        bool(rr5) and (rr5[0]["win_speech"], rr5[0]["win_used"], rr5[0]["win_untagged"]) == (0, 4, 0),
        rr5[:1],
    )
    rr6, _ = run_speech([(66.0, True), (80.0, False), (95.0, False), (110.0, False)], [-60.0] * 4)
    check(
        "6 overlap twin: a speech tagger window ending at 66 overlaps [60, 65] -> that window is excluded (win_speech 1, win_used 3)",
        bool(rr6) and (rr6[0]["win_speech"], rr6[0]["win_used"]) == (1, 3),
        rr6[:1],
    )
    rr7, p7 = run_speech([(80.0, False)], [-20.0, -60.0, -20.0, -60.0])
    check(
        "6 fail closed: windows no tagger window covers are EXCLUDED and counted (win_untagged 3), only the covered window is used, and the two loud untagged windows leave no trace (p90 level < -59)",
        bool(rr7)
        and (rr7[0]["win_total"], rr7[0]["win_used"], rr7[0]["win_untagged"], rr7[0]["win_speech"]) == (4, 1, 3, 0)
        and rr7[0]["indices"]["level_dbfs"]["p90"] < -59.0
        and p7.stats()["untagged"] == 3,
        (rr7[:1], p7.stats()),
    )
    rr8, p8 = run_speech([], [-60.0] * 4)
    check(
        "6 fail closed: a tagger that never answers (no tag at all) writes NO record, counts 4 untagged windows and one suppressed interval, so the dead tagger is visible in stats",
        [r_ for r_ in rr8 if r_["t0"] == 60.0] == [] and p8.stats()["untagged"] == 4 and p8.stats()["suppressed"] == 1 and p8.stats()["errors"] == 0,
        p8.stats(),
    )
    rr9, p9 = run_speech([(65.0, False), (80.0, False), (95.0, False), (110.0, False)], [-60.0] * 4)
    check(
        "6 fail closed twin: the same windows with their (non-speech) tags present are all used, 0 untagged, a record is written",
        bool(rr9) and (rr9[0]["win_used"], rr9[0]["win_untagged"]) == (4, 0) and p9.stats()["untagged"] == 0,
        rr9[:1],
    )
    # a tag with a gap in the middle of the window: [60, 62] + [63, 65] leaves (62, 63) unknown -> excluded; the gap-free twin is used
    pg = plugin()
    pg.note_tag(62.0, False, 2.0)
    pg.note_tag(65.0, False, 2.0)
    rg = feed(pg, beams(white(rng, -60)), 65.0) + feed(pg, beams(white(rng, -60)), 160.0)
    check(
        "6 coverage needs the union of tagger windows to span the whole window: [60,62] + [63,65] has a gap (62,63) so the window is untagged (no record, untagged 1)",
        [r_ for r_ in rg if r_["t0"] == 60.0] == [] and pg.stats()["untagged"] == 1,
        (pg.stats(), rg[:1]),
    )
    pg2 = plugin()
    for te, w in ((62.0, 2.0), (65.0, 3.0)):
        pg2.note_tag(te, False, w)
    rg2 = feed(pg2, beams(white(rng, -60)), 65.0) + feed(pg2, beams(white(rng, -60)), 160.0)
    check(
        "6 coverage twin: [60,62] + [62,65] touch, covering [60,65]: the window is used (win_used 1, untagged 0)",
        bool(rg2) and (rg2[0]["win_used"], rg2[0]["win_untagged"]) == (1, 0),
        (pg2.stats(), rg2[:1]),
    )
    # flush at shutdown before the tag arrived: the loud window is excluded, not stored; after the tag arrived it is a normal used window
    pfl = plugin()
    feed(pfl, beams(white(rng, -20)), 65.0)
    fl1 = pfl.flush()
    check(
        "6 flush before the tag answered: the loud window is excluded (no record, untagged 1) - the shutdown path cannot leak speech",
        fl1 == [] and pfl.stats()["untagged"] == 1 and pfl.stats()["suppressed"] == 1,
        (fl1, pfl.stats()),
    )
    pfl2 = plugin()
    feed(pfl2, beams(white(rng, -20)), 65.0)
    pfl2.note_tag(65.0, True, 5.0)
    fl2 = pfl2.flush()
    pfl3 = plugin()
    feed(pfl3, beams(white(rng, -20)), 65.0)
    pfl3.note_tag(65.0, False, 5.0)
    fl3 = pfl3.flush()
    check(
        "6 flush twin: the tag arrived before the flush -> speech tag: excluded and counted (no record, speech 1); non-speech tag: the window is used and the -20 dBFS level shows",
        fl2 == [] and pfl2.stats()["speech"] == 1 and len(fl3) == 1 and fl3[0]["win_used"] == 1 and fl3[0]["indices"]["level_dbfs"]["p50"] > -25.0,
        (pfl2.stats(), fl3[:1]),
    )
    prl = plugin()
    feed(prl, beams(white(rng, -20)), 65.0)
    r_late = feed(prl, beams(white(rng, -60)), 160.0)  # the interval closes at 150 without any tag
    prl.note_tag(65.0, True, 5.0)  # the speech tag arrives after the grace
    check(
        "6 late speech tag (after the grace): the window was already excluded as untagged at close, nothing stored",
        r_late == [] and prl.stats()["untagged"] == 1 and prl.stats()["intervals"] == 0,
        prl.stats(),
    )
    pn = plugin()
    for bad in (("x", True, 5.0), (float("nan"), True, 5.0), (100.0, True, 0.0), (100.0, True, float("inf"))):
        pn.note_tag(*bad)
    pn.note_tag(100.0, True, 5.0)
    check(
        "6 note_tag never raises: 4 bad calls are counted as errors, the good call is kept",
        pn.stats()["errors"] == 4 and len(pn.tags) == 1,
        (pn.stats(), pn.tags),
    )

    # ------------------------------------------------------------------------------------------------ 7 NaN, silence, bad input
    pf = plugin(grace_s=0.0, speech_gate=False)
    xs = np.zeros((N, 8), np.float32)
    xn = beams(white(rng))
    xn[500, 3] = np.nan
    ok_ = feed(pf, xs, 65.0) + feed(pf, xn, 80.0) + feed(pf, np.zeros((100, 8), np.float32), 90.0) + feed(pf, beams(white(rng)), 95.0)
    ok_ += feed(pf, beams(white(rng)), 125.0)
    s7 = pf.stats()
    check(
        "7 silent window, NaN window and wrong-shape window: counted (silent 1, nonfinite 1, errors 1), nothing raised, and the interval's record holds only the 1 good window",
        (s7["silent"], s7["nonfinite"], s7["errors"]) == (1, 1, 1)
        and len(ok_) == 1
        and (ok_[0]["win_total"], ok_[0]["win_used"], ok_[0]["win_silent"], ok_[0]["win_nonfinite"]) == (3, 1, 1, 1),
        (s7, ok_),
    )
    check(
        "7 the last_error text names the wrong shape (positive twin: the next good window was processed)",
        "window shape" in str(s7["last_error"]) and s7["windows"] == 4,
        s7,
    )
    pz = plugin(grace_s=0.0, speech_gate=False)
    z = feed(pz, xs, 65.0) + feed(pz, xs, 125.0)
    check("7 an interval with only silent windows writes no record (stats.suppressed 1)", z == [] and pz.stats()["suppressed"] == 1, (z, pz.stats()))
    pt = plugin()
    check(
        "7 process() with t_wall NaN and with a list instead of an array never raises (errors 2)",
        pt.process(beams(white(rng)), float("nan")) == ([], None) and pt.process([1, 2, 3], 5.0) == ([], None) and pt.stats()["errors"] == 2,
        pt.stats(),
    )
    for kw, msg in (({"interval_s": 0}, "interval_s and hop_s"), ({"hop_s": -1}, "interval_s and hop_s"), ({"win_s": 0.01}, "win_s >= 0.1")):
        try:
            plugin(**kw)
            check(f"7 constructor refuses {kw}", False, "no exception")
        except ValueError as e:
            check(f"7 constructor refuses {kw}: ValueError matching {msg!r}", msg in str(e), str(e))
    check("7 positive twin: the default constructor works", plugin().stats()["windows"] == 0)

    # ------------------------------------------------------------------------------------------------ 8 record content
    check(
        "8 record: parameters are written (bands, frame, df) so a reader never guesses, counts add up, no key looks like a coordinate",
        bool(rr2)
        and rr2[0]["params"]["aci_band_hz"] == [1000, 11000]
        and rr2[0]["params"]["ndsi_bio_hz"] == [2000, 8000]
        and rr2[0]["params"]["bi_band_hz"] == [2000, 8000]
        and rr2[0]["params"]["ndsi_anthro_hz"] == [1000, 2000]
        and rr2[0]["params"]["entropy_band_hz"] == [1000, 11000]
        and rr2[0]["params"]["chorus_band_hz"] == [4000, 12000]
        and rr2[0]["params"]["frame"] == "hann 480 no overlap"
        and rr2[0]["win_total"] == rr2[0]["win_used"] + rr2[0]["win_speech"] + rr2[0]["win_silent"] + rr2[0]["win_nonfinite"] + rr2[0]["win_untagged"]
        and not any(k in json.dumps(rr2[0]) for k in ('"lat', '"lon')),
        rr2[:1],
    )
    try:
        import jsonschema

        sch = json.load(open(os.path.join(BMA, "contracts", "schemas", "acoustic_indices.schema.json"), encoding="utf-8"))
        vv = jsonschema.Draft202012Validator(sch)
        errs = [e.message for r_ in (rr + rr2 + rr4 + out) for e in vv.iter_errors(r_)]
        check("8 records of the real plugin validate against the schema", errs == [] and len(rr + rr2 + rr4 + out) >= 4, errs[:2])
        bad = dict(rr2[0], win_used=0)
        check(
            "8 a record with win_used 0 does not validate (the schema's minimum 1), and the same record with its real win_used does (positive twin)",
            not vv.is_valid(bad) and vv.is_valid(rr2[0]),
        )
    except ImportError as ex:
        print(f"SKIP 8 schema validation  [{ex}]")

    # ------------------------------------------------------------------------------------------------ 9 report CLI
    def mkrec(t0, **ind):
        r_ = json.loads(json.dumps(rr2[0]))
        r_["t0"], r_["t"] = float(t0), float(t0) + 300.0
        for k, v in ind.items():
            r_["indices"][k] = {"p10": v, "p50": v, "p90": v}
        return r_

    base0 = 20751 * 86400.0  # 00:00 UTC of a day
    chor = [-80.0 + 2 * i for i in range(12)]  # 12 intervals of 300 s = UTC hour 0
    recs9 = [
        mkrec(base0 + 300 * i, chorus_dbfs=c, aci=2.0 * c + 200.0, hf=0.5 + (0.01 if i % 2 else -0.01), ht=-0.003 * c, ndsi=0.4) for i, c in enumerate(chor)
    ]
    jp = os.path.join(tmp, "idx.jsonl")
    with open(jp, "wb") as f:
        for r_ in recs9:
            f.write((json.dumps(r_) + "\n").encode())
        f.write(b"{not json\n")
    rep = R.build(R.read([jp]))
    cor = rep["correlation_with_chorus_dbfs"]
    check(
        "9 report: ACI = 2 * chorus + 200 correlates r = 1.0 and is labelled CHORUS METER; Ht = -0.003 * chorus gives r = -1.0 (also a meter)",
        cor["aci"] == {"r": 1.0, "chorus_meter": True} and cor["ht"] == {"r": -1.0, "chorus_meter": True},
        cor,
    )
    check(
        "9 report: a constant index (NDSI 0.4) is n/a and an alternating index (Hf +-0.01) is NOT a chorus meter (|r| < 0.8) - the positive twin of the two meters",
        cor["ndsi"] == {"r": None, "chorus_meter": False} and cor["hf"]["r"] is not None and abs(cor["hf"]["r"]) < 0.8 and cor["hf"]["chorus_meter"] is False,
        cor,
    )
    h0 = rep["hours"]["aci"][0]
    check(
        "9 report: hour 0 holds the 12 intervals: n 12, ACI min 40.0 / median 62.0 / max 84.0 exactly (2 * chorus + 200 with chorus -80 ... -58, median of the middle pair -70 and -68)",
        (h0["n"], h0["min"], h0["median"], h0["max"]) == (12, 40.0, 62.0, 84.0) and set(rep["hours"]["aci"]) == {0},
        (h0, rep["hours"]["aci"]),
    )
    short = R.build(R.read([jp])[:5])
    check(
        "9 report: fewer than 8 intervals gives n/a for every correlation (positive twin: 12 intervals above gave numbers)",
        all(c["r"] is None for c in short["correlation_with_chorus_dbfs"].values()),
        short["correlation_with_chorus_dbfs"],
    )
    cli = [sys.executable, "-I", os.path.join(HERE, "indices_report.py")]
    o1 = subprocess.run(cli + [jp], capture_output=True, text=True)
    o2 = subprocess.run(cli + [jp, "--json"], capture_output=True, text=True)
    o3 = subprocess.run(cli + [os.path.join(tmp, "nope.jsonl")], capture_output=True, text=True)
    o4 = subprocess.run(cli + [jp, "--day", "1999-01-01"], capture_output=True, text=True)
    o5 = subprocess.run(cli + [jp, "--bogus"], capture_output=True, text=True)
    check(
        "9 report CLI: exit 0 with the table (header, an ACI row and CHORUS METER), --json parses and equals build(), a missing file / an empty day / a bad option exit 2",
        o1.returncode == 0
        and f"UTC day {time.strftime('%Y-%m-%d', time.gmtime(base0))}" in o1.stdout
        and "CHORUS METER" in o1.stdout
        and o2.returncode == 0
        and json.loads(o2.stdout) == json.loads(json.dumps(rep))
        and o3.returncode == 2
        and o4.returncode == 2
        and o5.returncode == 2,
        (o1.returncode, o1.stdout[:200], o3.returncode, o4.returncode, o5.returncode, o1.stderr[:200]),
    )

    # robustness: truncated / hand-edited lines are skipped, ungated records are left out of the day's table and correlations
    bad_missing_index = json.loads(json.dumps(recs9[0]))
    bad_missing_index["indices"].pop("bi")
    bad_missing_index["t0"] = base0 + 300 * 20
    bad_missing_p50 = json.loads(json.dumps(recs9[0]))
    bad_missing_p50["indices"]["aci"].pop("p50")
    bad_missing_p50["t0"] = base0 + 300 * 21
    bad_no_indices = {k_: v_ for k_, v_ in recs9[0].items() if k_ != "indices"}
    bad_no_indices["t0"] = base0 + 300 * 22
    bad_text = json.loads(json.dumps(recs9[0]))
    bad_text["indices"]["hf"]["p90"] = "x"
    bad_text["t0"] = base0 + 300 * 23
    ungated = mkrec(base0 + 300 * 24, chorus_dbfs=-10.0, aci=999.0)
    ungated["speech_gate"] = False
    jp2 = os.path.join(tmp, "idx2.jsonl")
    with open(jp2, "wb") as f:
        for r_ in recs9 + [bad_missing_index, bad_missing_p50, bad_no_indices, bad_text, ungated]:
            f.write((json.dumps(r_) + "\n").encode())
    rd2 = R.read([jp2])
    rep2 = R.build(rd2)
    o6 = subprocess.run(cli + [jp2, "--json"], capture_output=True, text=True)
    check(
        "9 report robustness: 4 malformed v1 lines (missing index, missing p50, no indices, text value) are skipped without a KeyError (read keeps 13 of 17), CLI exits 0",
        len(rd2) == 13 and o6.returncode == 0 and o6.stderr == "",
        (len(rd2), o6.returncode, o6.stderr[-300:]),
    )
    check(
        "9 report robustness: the speech_gate false record (ACI 999) is counted (ungated_records_excluded 1) but not mixed in: table and correlations equal the 12-record report, aci max stays 84.0",
        rep2["ungated_records_excluded"] == 1
        and rep2["hours"] == rep["hours"]
        and rep2["correlation_with_chorus_dbfs"] == rep["correlation_with_chorus_dbfs"]
        and rep2["totals"] == rep["totals"]
        and rep2["hours"]["aci"][0]["max"] == 84.0
        and rep["ungated_records_excluded"] == 0,
        (rep2["ungated_records_excluded"], rep2["hours"]["aci"]),
    )

    # ------------------------------------------------------------------------------------------------ 10 fixture regenerates from the real plugin
    import make_indices_fixtures as MIND

    regen = [json.loads(l) for l in open(os.path.join(MIND.build(os.path.join(tmp, "regen")), MIND.NAME), encoding="utf-8")]
    fx = [json.loads(l) for l in open(os.path.join(BMA, "contracts", "fixtures", "acoustic_indices.jsonl"), encoding="utf-8")]

    def same(x, y):
        if isinstance(x, float) or isinstance(y, float):
            return isinstance(x, (int, float)) and isinstance(y, (int, float)) and abs(x - y) <= 0.01 + 1e-9
        if isinstance(x, dict):
            return isinstance(y, dict) and x.keys() == y.keys() and all(same(x[k], y[k]) for k in x)
        if isinstance(x, list):
            return isinstance(y, list) and len(x) == len(y) and all(same(p_, q_) for p_, q_ in zip(x, y))
        return x == y

    check(
        "10 the committed fixture equals the output of the real plugin and writer (floats within 0.01, everything else exact)",
        len(regen) == len(fx) == 4 and all(same(a_, b_) for a_, b_ in zip(regen, fx)),
    )

    # ------------------------------------------------------------------------------------------------ 11 scikit-maad cross-check
    try:
        import maad, maad.features, maad.sound  # noqa: F401
        import compare_maad as CM
    except Exception as ex:
        print(f"SKIP 11 scikit-maad cross-check  [{type(ex).__name__}: {ex}]")
    else:
        rows = {nm: CM.compare(x, maad) for nm, x in CM.signals().items()}
        rel = lambda d: abs(d["ours"] / d["maad"] - 1.0)  # noqa: E731
        check(
            "11 maad: ACI, BI (x df^2/1000) and NDSI equal scikit-maad's to 1e-4 relative on all 4 signals (same spectrogram, same bins)",
            all(rel(rows[nm][k]) < 1e-4 for nm in rows for k in ("aci", "bi", "ndsi")),
            {nm: {k: rel(rows[nm][k]) for k in ("aci", "bi", "ndsi")} for nm in rows},
        )
        check(
            "11 maad: ACI on maad's own spectrogram differs by < 0.1 % (it drops the last frame and the Nyquist bin)",
            all(rel(rows[nm]["aci_maad_spectrogram"]) < 1e-3 for nm in rows),
            {nm: rel(rows[nm]["aci_maad_spectrogram"]) for nm in rows},
        )
        check(
            "11 maad: Hf matches maad 'seewave' (sqrt of mean power) within 1.5 %, and Ht matches maad's Hilbert/seewave within 0.01 (sub-sampled baseband envelope)",
            all(rel(rows[nm]["hf_seewave"]) < 0.015 and abs(rows[nm]["ht_hilbert"]["ours"] - rows[nm]["ht_hilbert"]["maad"]) < 0.01 for nm in rows),
            {nm: (rel(rows[nm]["hf_seewave"]), rows[nm]["ht_hilbert"]) for nm in rows},
        )
        check(
            "11 maad twin: the definitions that DO differ are visibly different (maad 'QUT' Hf on the real-shaped signal is more than 20 % below ours; maad default Ht differs by > 0.002)",
            rel(rows["real-shaped"]["hf_QUT"]) > 0.2 and abs(rows["real-shaped"]["ht_default"]["ours"] - rows["real-shaped"]["ht_default"]["maad"]) > 0.002,
            rows["real-shaped"]["hf_QUT"],
        )

    # ------------------------------------------------------------------------------------------------ 12 CPU
    pc = plugin(speech_gate=False, grace_s=0.0)
    xb = beams(white(rng))
    xb[:, 3] += sine(978.0, 10 ** (-45 / 20.0)).astype(np.float32)
    ts = []
    for i in range(12):
        t0 = time.perf_counter()
        pc.process(xb, 5.0 + 15 * i)
        ts.append((time.perf_counter() - t0) * 1000)
    med = float(np.median(ts[2:]))
    print(f"  CPU: median {med:.1f} ms per 5 s window = {med / I.HOP_S:.2f} ms per second of audio at hop {I.HOP_S:g} s")
    check(
        "12 CPU: median per window under 60 ms, i.e. under 4.0 ms per second of audio (measured 22-31 ms, 1.5-2.1 ms/s; about 2x the measurement so a 3x regression goes red)",
        med < 60.0,
        med,
    )

    dflt = I.Indices(speech_gate=False, grace_s=0.0)
    d_recs = []
    for i in range(12):
        d_recs += dflt.process(beams(white(rng, -60.0)), 5.0 + dflt.hop_s * i)[0]
    d_recs += dflt.flush()
    hh = subprocess.run([sys.executable, "-I", os.path.join(HERE, "host.py"), "--help"], capture_output=True, text=True)
    check(
        "12 CPU budget: the default hop is 30 s (about 1 ms per second of audio, half of the 15 s hop): defaults give hop_s 30 and 10 windows in a 300 s interval, and host.py --help says default 30",
        (dflt.hop_s, I.HOP_S, I.INTERVAL_S) == (30.0, 30.0, 300.0)
        and [r_["win_total"] for r_ in d_recs] == [10, 2]
        and "(default 30)" in hh.stdout.replace("\n", " ").replace("  ", " "),
        ([r_["win_total"] for r_ in d_recs], hh.stdout[-300:], hh.stderr[-200:]),
    )

    # ------------------------------------------------------------------------------------------------ 12b deploy pins
    mf_ = json.load(open(os.path.join(HERE, "plugins.manifest.json"), encoding="utf-8"))
    deployed_ = {t_: {f_ for e_ in mf_["plugins"] if t_ in e_["deploy"] for f_ in e_["files"]} for t_ in ("pc", "image")}
    mods = ["coherence", "indices"]  # the plugin modules host.py loads by name: each must be installed into the live app dir and the image
    check(
        "12b pins: plugins.manifest.json, the one list pc/install_task.ps1 and docker/analyzer.Dockerfile install from, deploys every plugin module host.py loads (coherence, indices) to the PC and to the image (docker/test_install_lists.py checks the installers against it)",
        all(f"{m}.py" in deployed_["pc"] and f"{m}.py" in deployed_["image"] for m in mods),
        [(m, f"{m}.py" in deployed_["pc"], f"{m}.py" in deployed_["image"]) for m in mods],
    )
    import manifest_harness as _G  # its readers return the (source, destination) pairs the installer and the Dockerfile really produce

    try:
        _pc_dst, _img_dst = {d_ for _s, d_ in _G.ps1_pairs()}, {d_.rsplit("/", 1)[-1] for _s, d_ in _G.docker_pairs()}
    except _G.NoPowerShell as e_:  # WSL / Linux: the same effective pairs are asserted by docker/test_install_lists.py under Windows PowerShell
        print(f"SKIP 12b pins (effective)  [{e_}; docker/test_install_lists.py runs the same pairs where PowerShell exists]")
    else:
        check(
            "12b pins (effective): the pairs pc/install_task.ps1 really copies and analyzer.Dockerfile really installs include coherence.py and indices.py (and not the made-up nosuchplugin.py)",
            all(f"{m}.py" in _pc_dst and f"{m}.py" in _img_dst for m in mods) and "nosuchplugin.py" not in _pc_dst | _img_dst,
            [(m, f"{m}.py" in _pc_dst, f"{m}.py" in _img_dst) for m in mods],
        )
    check(
        "12b pins twin: a module that is no plugin of the manifest is not claimed (positive control for the check above: a made-up module is not deployed)",
        "nosuchplugin.py" not in deployed_["pc"] | deployed_["image"],
    )

    # ------------------------------------------------------------------------------------------------ 13 the real host over UDP
    if "--skip-e2e" in sys.argv:
        print("SKIP 13 host e2e over UDP  [--skip-e2e]")
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

        def stream(port, secs, level_fn, seed):
            """real-time BMAA feed: the same white noise on all 8 slots, level (dBFS) = level_fn(wall time of the second)"""
            sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            g = np.random.default_rng(seed)
            blk, t_start, nb = 0, time.time(), 0
            for sec in range(secs):
                x = (g.standard_normal(FS) * db(level_fn(t_start + sec + 0.5))).astype(np.float32)
                for off in range(0, FS, 256):
                    for slot in range(8):
                        sk.sendto(bmaa(slot, blk + off, x[off : off + 256]), ("127.0.0.1", port))
                    nb += 1
                    dly = t_start + nb * 256 / FS - time.time()
                    if dly > 0:
                        time.sleep(dly)
                blk += FS

        def wait_listening(out_dir, proc):
            for _ in range(240):
                if os.path.exists(os.path.join(out_dir, "status.json")) or proc.poll() is not None:
                    break
                time.sleep(0.25)

        NO = ["--no", "dsp", "--no", "spatial", "--no", "clap", "--no", "birds", "--no", "coherence"]
        # ---- (a) the real host with a tagger stand-in: speech = the window's own peak exceeds 0.05; the audio is loud (-30 dBFS) in the first 10 s of every 40 s of wall-clock time, quiet (-60 dBFS) otherwise
        app = os.path.join(tmp, "app")
        os.makedirs(app)
        for fn in os.listdir(HERE):
            if fn.endswith((".py", ".json")):
                shutil.copy(os.path.join(HERE, fn), app)
        with open(os.path.join(app, "plugins.py"), "a", encoding="utf-8", newline="\n") as f:
            f.write("""

class Tagger:                                                    # test stand-in for the PANNs tagger (the model is not available here)
    name = "tags"
    win_s, hop_s = 5.0, 2.0
    heavy = True

    def __init__(self, data_dir, device):
        pass

    def process(self, x, t_wall):
        flag = float(np.abs(x).max()) > 0.05              # decided from the audio of the window asked about (quiet noise peaks ~0.005, the loud noise ~0.14): independent of timing and load
        return [{"k": "tags", "t": t_wall, "beam": 1, "speech_p": 0.9 if flag else 0.0, "speech_active": flag, "top": [], "emb_stored": False}], None
""")
        out_a, hist_a = os.path.join(tmp, "out_a"), os.path.join(tmp, "hist_a")
        os.makedirs(out_a)
        port = free_port()
        proc = subprocess.Popen(
            [
                sys.executable,
                os.path.join(app, "host.py"),
                "--listen",
                f"127.0.0.1:{port}",
                "--out",
                out_a,
                "--history-dir",
                hist_a,
                "--classify-dir",
                os.path.join(BMA, "classify"),
                "--panns-dir",
                tmp,
                *NO,
                "--indices-interval-s",
                "10",
                "--indices-hop-s",
                "2",
                "--secs",
                "106",
            ],
            stderr=subprocess.PIPE,
            text=True,
        )
        wait_listening(out_a, proc)
        stream(port, 100, lambda t: -30.0 if t % 40.0 < 10.0 else -60.0, 5)
        proc.wait(timeout=120)
        err = proc.stderr.read()
        st = json.load(open(os.path.join(out_a, "status.json")))
        ip = os.path.join(out_a, "indices.jsonl")
        rows = lines(ip) if os.path.exists(ip) else []
        # The stand-in flags speech from the audio of the window it is asked about, so a starved machine delays or drops tagger answers but never moves a flag
        # off the audio. That can only cost windows (the gate fails closed: win_untagged), never leak one; the loud-window and counting checks below hold under any load, the
        # throughput checks are skipped (with the reason) when the host demonstrably fell behind.
        # Windows a starved host never evaluates are counted nowhere, so the shortfall is measured against the audio sent: 100 s at a 2 s hop is about 47 windows with a 5 s
        # window; under 80 % of that (a single-CPU host evaluates about 60 %) is a host that fell behind, not a gate that failed.
        expected_windows = (100 - 5) // 2
        starved = (
            any(st["plugins"].get(nm_, {}).get("dropped", 0) > 0 for nm_ in ("tags", "indices"))
            or sum(r["win_untagged"] for r in rows) > 0.25 * max(1, sum(r["win_total"] for r in rows))
            or sum(r["win_total"] for r in rows) < 0.8 * expected_windows
        )
        check("13a host with the tagger stand-in: exits 0", proc.returncode == 0, (proc.returncode, err[-300:]))
        if starved:
            print(
                f"SKIP 13a throughput checks (records >= 5, >= 6 speech and >= 6 used windows): the host fell behind (dropped {[st['plugins'][nm_]['dropped'] for nm_ in ('tags', 'indices')]}, untagged "
                f"{sum(r['win_untagged'] for r in rows)} of {sum(r['win_total'] for r in rows)} windows evaluated (expected about {expected_windows}), records {len(rows)})"
            )
        else:
            check("13a writes indices.jsonl with at least 5 records (10 s intervals over 100 s)", len(rows) >= 5, (len(rows), err[-300:]))
        check(
            "13a the stand-in tagger's speech flags reach the plugin through the host: at least 6 windows were excluded as speech and at least 6 used, speech_gate true everywhere",
            starved or (sum(r["win_speech"] for r in rows) >= 6 and sum(r["win_used"] for r in rows) >= 6 and all(r["speech_gate"] is True for r in rows)),
            ([(r["t0"] % 40, r["win_used"], r["win_speech"]) for r in rows], st.get("rx")),
        )
        check(
            "13a no loud window leaked: every record's level reads the quiet -60 dBFS (p50 within 0.5 dB, p90 below -59), none near the -30 dBFS speech-time level",
            bool(rows) and all(abs(r["indices"]["level_dbfs"]["p50"] + 60.0) < 0.5 and r["indices"]["level_dbfs"]["p90"] < -59.0 for r in rows),
            [r["indices"]["level_dbfs"] for r in rows][:3],
        )
        check(
            "13a counts add up in every record (win_total is the sum of used, speech, silent, nonfinite and untagged)",
            bool(rows) and all(r["win_total"] == r["win_used"] + r["win_speech"] + r["win_silent"] + r["win_nonfinite"] + r["win_untagged"] for r in rows),
            [(r["t0"], r["win_total"]) for r in rows],
        )
        # throughput: a starved host leaves whole intervals without a record, so fewer close normally; the arithmetic above is unconditional
        check(
            "13a at least 3 records closed normally (30 s grace) and the intervals still open at shutdown were flushed partial",
            starved or (sum(1 for r in rows if not r.get("partial")) >= 3 and any(r.get("partial") for r in rows)),
            [(r["t0"], r.get("partial")) for r in rows],
        )
        pl = st["plugins"]["indices"]
        check(
            "13a status.json: plugins.indices enabled, errors 0, windows > 20, meta.indices_errors covers every untagged window written, audio_written false, no audio-like file under --out",
            pl["enabled"]
            and pl["errors"] == 0
            and pl["windows"] > 20
            and st["meta"]["indices_errors"] >= sum(r["win_untagged"] for r in rows)
            and (starved or st["meta"]["indices_errors"] == 0)
            and st["audio_written"] is False
            and not [f for f in os.listdir(out_a) if f.lower().endswith((".wav", ".flac", ".mp3", ".ogg", ".pcm", ".raw", ".opus"))],
            (pl, st["meta"], os.listdir(out_a)),
        )
        # ---- (b) without the tagger: refused by default, started with the explicit flag, records say speech_gate false and the loud audio is NOT excluded
        out_b = os.path.join(tmp, "out_b")
        os.makedirs(out_b)
        pr_b = subprocess.run(
            [
                sys.executable,
                os.path.join(HERE, "host.py"),
                "--listen",
                f"127.0.0.1:{free_port()}",
                "--out",
                out_b,
                "--history-dir",
                "",
                "--no",
                "tags",
                *NO,
                "--secs",
                "6",
            ],
            capture_output=True,
            text=True,
        )
        st_b = json.load(open(os.path.join(out_b, "status.json"))) if os.path.exists(os.path.join(out_b, "status.json")) else {}
        check(
            "13b without the tags plugin the host refuses to start indices (enabled false, reason names the tags plugin, no file) and still exits 0",
            pr_b.returncode == 0
            and st_b.get("plugins", {}).get("indices", {}).get("enabled") is False
            and "tags plugin" in st_b["plugins"]["indices"].get("reason", "")
            and not os.path.exists(os.path.join(out_b, "indices.jsonl")),
            (pr_b.returncode, st_b.get("plugins", {}).get("indices"), pr_b.stderr[-200:]),
        )
        out_c, hist_c = os.path.join(tmp, "out_c"), os.path.join(tmp, "hist_c")
        os.makedirs(out_c)
        port = free_port()
        proc = subprocess.Popen(
            [
                sys.executable,
                os.path.join(HERE, "host.py"),
                "--listen",
                f"127.0.0.1:{port}",
                "--out",
                out_c,
                "--history-dir",
                hist_c,
                "--no",
                "tags",
                *NO,
                "--indices-without-speech-gate",
                "--indices-interval-s",
                "10",
                "--indices-hop-s",
                "2",
                "--secs",
                "26",
            ],
            stderr=subprocess.PIPE,
            text=True,
        )
        wait_listening(out_c, proc)
        stream(port, 20, lambda t: -30.0, 6)
        proc.wait(timeout=120)
        err = proc.stderr.read()
        cp = os.path.join(out_c, "indices.jsonl")
        rows_c = lines(cp) if os.path.exists(cp) else []
        check(
            "13b with --indices-without-speech-gate: records exist, speech_gate false, win_speech 0, and the loud -30 dBFS audio shows (p50 within 0.5 dB of -30) - the twin of 13a's exclusion",
            proc.returncode == 0
            and len(rows_c) >= 1
            and all(r["speech_gate"] is False and r["win_speech"] == 0 and abs(r["indices"]["level_dbfs"]["p50"] + 30.0) < 0.5 for r in rows_c),
            (proc.returncode, [(r["speech_gate"], r["win_speech"], r["indices"]["level_dbfs"]["p50"]) for r in rows_c], err[-300:]),
        )
        # ---- (c) --no indices
        out_d = os.path.join(tmp, "out_d")
        os.makedirs(out_d)
        pr_d = subprocess.run(
            [
                sys.executable,
                os.path.join(HERE, "host.py"),
                "--listen",
                f"127.0.0.1:{free_port()}",
                "--out",
                out_d,
                "--history-dir",
                "",
                "--no",
                "tags",
                *NO,
                "--no",
                "indices",
                "--secs",
                "6",
            ],
            capture_output=True,
            text=True,
        )
        st_d = json.load(open(os.path.join(out_d, "status.json"))) if os.path.exists(os.path.join(out_d, "status.json")) else {}
        check(
            "13c --no indices: reports enabled false (disabled by --no), writes no indices.jsonl and meta.indices_errors is 0",
            pr_d.returncode == 0
            and st_d.get("plugins", {}).get("indices", {}).get("enabled") is False
            and "--no" in st_d["plugins"]["indices"]["reason"]
            and not os.path.exists(os.path.join(out_d, "indices.jsonl"))
            and st_d["meta"]["indices_errors"] == 0,
            (pr_d.returncode, st_d.get("plugins", {}).get("indices")),
        )
        # ---- (d) a broken indices module must not stop the analyzer
        app2 = os.path.join(tmp, "app2")
        shutil.copytree(app, app2)
        with open(os.path.join(app2, "indices.py"), "w", encoding="utf-8", newline="\n") as f:
            f.write("raise RuntimeError('simulated broken module')\n")
        out_e = os.path.join(tmp, "out_e")
        os.makedirs(out_e)
        pr_e = subprocess.run(
            [
                sys.executable,
                os.path.join(app2, "host.py"),
                "--listen",
                f"127.0.0.1:{free_port()}",
                "--out",
                out_e,
                "--history-dir",
                "",
                "--classify-dir",
                os.path.join(BMA, "classify"),
                "--panns-dir",
                tmp,
                *NO,
                "--secs",
                "6",
            ],
            capture_output=True,
            text=True,
        )
        st_e = json.load(open(os.path.join(out_e, "status.json"))) if os.path.exists(os.path.join(out_e, "status.json")) else {}
        check(
            "13d an indices module that fails to import: the host still runs (exit 0), reports indices disabled with the import error, and the other plugin (tags) is enabled",
            pr_e.returncode == 0
            and st_e.get("plugins", {}).get("indices", {}).get("enabled") is False
            and "indices.py not importable" in st_e["plugins"]["indices"]["reason"]
            and st_e["plugins"]["tags"]["enabled"] is True
            and "simulated broken module" in pr_e.stderr,
            (pr_e.returncode, st_e.get("plugins", {}).get("indices"), pr_e.stderr[-300:]),
        )
        # ---- (e) a tagger whose process() always raises: no note_tag ever reaches the plugin; the gate must stay closed and the dead tagger must be visible
        app3 = os.path.join(tmp, "app3")
        shutil.copytree(app, app3)
        with open(os.path.join(app3, "plugins.py"), "a", encoding="utf-8", newline="\n") as f:
            f.write("""

class Tagger:                                                    # a tagger that is down: every window raises
    name = "tags"
    win_s, hop_s = 5.0, 2.0
    heavy = True

    def __init__(self, data_dir, device):
        pass

    def process(self, x, t_wall):
        raise RuntimeError("simulated tagger failure")
""")
        out_f = os.path.join(tmp, "out_f")
        os.makedirs(out_f)
        port = free_port()
        proc = subprocess.Popen(
            [
                sys.executable,
                os.path.join(app3, "host.py"),
                "--listen",
                f"127.0.0.1:{port}",
                "--out",
                out_f,
                "--history-dir",
                "",
                "--classify-dir",
                os.path.join(BMA, "classify"),
                "--panns-dir",
                tmp,
                *NO,
                "--indices-interval-s",
                "10",
                "--indices-hop-s",
                "2",
                "--secs",
                "40",
            ],
            stderr=subprocess.PIPE,
            text=True,
        )
        wait_listening(out_f, proc)
        stream(port, 30, lambda t: -30.0, 7)
        proc.wait(timeout=120)
        err = proc.stderr.read()
        st_f = json.load(open(os.path.join(out_f, "status.json")))
        fp = os.path.join(out_f, "indices.jsonl")
        rows_f = lines(fp) if os.path.exists(fp) else []
        check(
            "13e dead tagger: the host exits 0, the tagger's errors are counted, indices stores NO record (every -30 dBFS window is unknown, so excluded) and meta.indices_errors shows the dead gate (> 5)",
            proc.returncode == 0
            and st_f["plugins"]["tags"]["errors"] > 5
            and rows_f == []
            and st_f["plugins"]["indices"]["windows"] > 10
            and st_f["meta"]["indices_errors"] > 5
            and st_f["plugins"]["indices"]["errors"] == 0,
            (proc.returncode, st_f["plugins"]["tags"], st_f["plugins"]["indices"], st_f["meta"], len(rows_f), err[-300:]),
        )
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{n_checks} checks, {len(fails)} failure(s)")
for f_ in fails:
    print("  FAILED:", f_)
sys.exit(1 if fails else 0)
