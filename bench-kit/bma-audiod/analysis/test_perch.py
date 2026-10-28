#!/usr/bin/env python3
"""Tests for the Perch 2.0 shadow plugin (analysis/perch.py), tools/fetch_perch.py and the host wiring.
The REAL Perch class runs on a REAL tiny ONNX model (analysis/perch_tiny_model.py: 8 single-frequency detectors, so a known tone
gives a known label and logit) through the REAL onnxruntime. Nothing under test is stubbed. The only substitution is in the
host end-to-end test: the third-party `panns_inference` library (the tagger's neural net) is replaced by a 15-line module that
flags a window as speech when its peak exceeds 0.6; the real Tagger class, SpeechGuard, Perch and host run on top of it.
Needs numpy, scipy, onnx and onnxruntime (WSL: PYTHONPATH=<overlay with onnx + onnxruntime>); without them the file says why and
exits 2 (it does not pass). Script style (check(name, ok, detail), exit 1 on a failure) like test_coherence.py. Temp files only
under tempfile. Run:  python test_perch.py [--skip-e2e]

Derivations
  * tiny model: a tone A sin(2 pi f_j t) gives label j the logit SCALE * A^2 - 0.1 j, every other label -0.1 k; the Perch
    normalisation fixes A = 0.25, so label j reads 6.25 - 0.1 j minus a small loss: the 48k -> 32k polyphase filter and the -80 dBFS
    noise floor shave at most 0.07 (measured 0.012 at 1 kHz up to 0.062 at 8 kHz).
  * a tone of amplitude 0.3 on one beam: loudest-beam rms = 0.3 / sqrt(2) = -13.47 dBFS (reported -13.5); the mean of the 8 beams has
    amplitude 0.3 / 8 = 0.0375, rms -31.53 dBFS (reported -31.5)."""

import json, math, os, shutil, socket, struct, subprocess, sys, tempfile, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
BMA = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(BMA, "tools"))
try:
    import onnx, onnxruntime  # noqa: F401
    import scipy  # noqa: F401
except ImportError as ex:
    print(f"CANNOT RUN: {ex}. test_perch.py needs numpy, scipy, onnx and onnxruntime (WSL: PYTHONPATH=<overlay with onnx and onnxruntime>).")
    sys.exit(2)
import perch as PC  # noqa: E402
import perch_tiny_model as TM  # noqa: E402
import fetch_perch as FP  # noqa: E402
import plugins as PL  # noqa: E402

fails, n_checks = [], 0
FS, N_IN = 48000, 240000


def check(name, ok, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  [{detail}]"))
    if not ok:
        fails.append(name)


def raises(fn, exc, text):
    try:
        fn()
    except exc as e:
        return text in str(e), str(e)
    except Exception as e:  # a different exception type is a failure with its text
        return False, f"{type(e).__name__}: {e}"
    return False, "no exception"


def window(tone_hz=None, amp=0.3, beam=3, noise_dbfs=-80.0, seed=0, n=N_IN):
    """(n, 8) float32 at 48 kHz: white noise at noise_dbfs on every beam, plus a sine of amplitude amp on `beam` (0-based)."""
    rng = np.random.default_rng(seed)
    x = (rng.standard_normal((n, 8)) * 10 ** (noise_dbfs / 20.0)).astype(np.float32)
    if tone_hz:
        x[:, beam] += (amp * np.sin(2 * np.pi * tone_hz * np.arange(n) / FS)).astype(np.float32)
    return x


def noise_window(level_dbfs, seed=1):
    """8 independent white-noise beams with EXACTLY level_dbfs rms each (each beam is rescaled to the target rms)."""
    rng = np.random.default_rng(seed)
    x = rng.standard_normal((N_IN, 8))
    x = x / np.sqrt((x**2).mean(axis=0)) * 10 ** (level_dbfs / 20.0)
    return x.astype(np.float32)


tmp = tempfile.mkdtemp(prefix="perch_t_")
try:
    MP, LP = TM.build_dir(os.path.join(tmp, "model"))
    TAXA = os.path.join(BMA, "ui", "taxa.json")
    taxa_doc = json.load(open(TAXA, encoding="utf-8"))

    def mk(guard=None, **kw):
        g = guard or PC.SpeechGuard()
        kw.setdefault("min_logit", 1.0)
        import taxa as T_

        return PC.Perch(MP, LP, g, taxa=T_.Taxa(TAXA), **kw), g

    def run1(p, g, x, t1=1000.0, speech=False):
        """one window ending at t1 with the tagger covering it; returns the records the plugin emits for it (it needs one more
        process() call to resolve the guard, which is a gated silent window 1 s later)"""
        g.note(t1 - 5.0, t1, speech)
        recs, _ = p.process(x, t1)
        more, _ = p.process(window(None, noise_dbfs=-100.0), t1 + 1.0)
        return recs + more

    # ================================================================================================ 1 labels
    lab_ok = PC.read_labels(LP)
    check("1 read_labels: the list name is line 1 and the 8 labels follow in order", lab_ok == ("tiny_test", TM.LABELS), lab_ok)
    bom = os.path.join(tmp, "bom.csv")
    open(bom, "wb").write(b"\xef\xbb\xbftiny_test\r\nA b\r\nC d\r\n")
    check(
        "1 read_labels: a BOM and CRLF line endings do not change names (positive twin of the three refusals below)",
        PC.read_labels(bom) == ("tiny_test", ["A b", "C d"]),
        PC.read_labels(bom),
    )
    for nm, body, text in (("blank", "n\nA b\n\nC d\n", "blank label"), ("dup", "n\nA b\nC d\nA b\n", "duplicate labels"), ("none", "n\n", "no labels")):
        fp = os.path.join(tmp, nm + ".csv")
        open(fp, "w").write(body)
        ok, msg = raises(lambda: PC.read_labels(fp), PC.PerchUnavailable, text)
        check(f"1 read_labels refuses a {nm} label file with 'not started' and '{text}'", ok and msg.startswith("not started"), msg)
    check(
        "1 is_species: binomials (also with quotes) are species; FSD50k classes are not",
        [PC.is_species(x) for x in ("Poecile carolinensis", "Pelophylax 'esculentus'", "Wind_chime", "Alarm", "Chirp_and_tweet")]
        == [True, True, False, False, False],
    )
    check(
        "1 the gate and the birds window length equal plugins.MultiBeamBirds' (so the shadow sees the same windows)",
        __import__("inspect").signature(PL.MultiBeamBirds.__init__).parameters["gate_dbfs"].default == PC.GATE_DBFS
        and PL.MultiBeamBirds.win_s == PC.BIRDS_WIN_S,
    )

    # ================================================================================================ 2 resampling and normalisation
    n_t = np.arange(N_IN)
    y = PC.resample_32k((0.5 * np.sin(2 * np.pi * 1000 * n_t / FS)).astype(np.float32))
    ref = 0.5 * np.sin(2 * np.pi * 1000 * np.arange(160000) / 32000)
    err = float(np.abs(y[2000:-2000] - ref[2000:-2000]).max())
    check(
        "2 resample 48k -> 32k: 240000 samples in, 160000 out, and a 1 kHz tone matches the analytic 32 kHz sine within 1e-3 (measured 4.7e-4: float32 input and filter ripple; middle of the window)",
        y.shape == (160000,) and err < 1e-3,
        (y.shape, err),
    )
    y2 = PC.resample_32k((0.5 * np.sin(2 * np.pi * 20000 * n_t / FS)).astype(np.float32))
    check(
        "2 resample: a 20 kHz tone (above the 16 kHz Nyquist of 32 kHz) is removed by the anti-alias filter: rms < 0.5 % of the input's 0.354 (positive twin: the 1 kHz tone keeps 0.354)",
        float(np.sqrt((y2[2000:-2000] ** 2).mean())) < 0.00177 and abs(float(np.sqrt((y[2000:-2000] ** 2).mean())) - 0.5 / math.sqrt(2)) < 1e-3,
        (float(np.sqrt((y2[2000:-2000] ** 2).mean())), float(np.sqrt((y[2000:-2000] ** 2).mean()))),
    )
    ok, msg = raises(lambda: PC.resample_32k(np.zeros(N_IN + 3, np.float32)), ValueError, "expected 160000")
    check("2 resample: a window of the wrong length is refused (positive twin: the right length above works)", ok, msg)
    z = PC.peak_normalize((0.9 * np.sin(2 * np.pi * 1000 * np.arange(160000) / 32000) + 0.1).astype(np.float32))
    check(
        "2 peak_normalize: removes the 0.1 offset (mean < 1e-6) and scales the peak to exactly 0.25 (|err| < 1e-6)",
        abs(float(z.mean())) < 1e-6 and abs(float(np.abs(z).max()) - 0.25) < 1e-6,
        (float(z.mean()), float(np.abs(z).max())),
    )
    check("2 peak_normalize: an all-zero window stays all zero (no NaN from 0/0)", not np.any(PC.peak_normalize(np.zeros(160000, np.float32))))

    # ================================================================================================ 3 top-k formatting
    lg = np.array([1.0, 5.0, 5.0, 0.12345, 9.0, -3.0, 5.0], np.float32)
    lb = list("abcdefg")
    check(
        "3 top_k: largest first, ties by lower index, logits rounded to 3 decimals, exactly k entries",
        PC.top_k(lg, lb, 5) == [["e", 9.0], ["b", 5.0], ["c", 5.0], ["g", 5.0], ["a", 1.0]],
        PC.top_k(lg, lb, 5),
    )
    check(
        "3 top_k_tail: the 5 best as top_k and the 6th largest logit (0.12345 -> 0.123) as max_unstored_score",
        PC.top_k_tail(lg, lb, 5) == ([["e", 9.0], ["b", 5.0], ["c", 5.0], ["g", 5.0], ["a", 1.0]], 0.123),
        PC.top_k_tail(lg, lb, 5),
    )
    check("3 top_k_tail: k >= the list length leaves nothing unstored: None", PC.top_k_tail(lg, lb, 7)[1] is None and len(PC.top_k_tail(lg, lb, 7)[0]) == 7)
    check(
        "3 top_k: k larger than the list returns all, and rounding is to 3 decimals (0.12345 -> 0.123)",
        PC.top_k(lg, lb, 99)[-2:] == [["d", 0.123], ["f", -3.0]],
        PC.top_k(lg, lb, 99)[-2:],
    )

    # ================================================================================================ 4 the plugin on known tones
    p, g = mk()
    recs = run1(p, g, window(3000, 0.3, beam=3))
    if len(recs) != 2:
        check(
            "4 a 3 kHz tone on beam 4 yields exactly two records (mixed, loudest): the remaining plugin checks cannot run", False, [r.get("src") for r in recs]
        )
        print(f"\n{n_checks} checks, {len(fails)} failure(s)")
        sys.exit(1)
    mixed, loud = recs[0], recs[1]
    exp = 6.25 - 0.1 * 2
    check(
        "4 a 3 kHz tone on beam 4: two records, mixed (beam null) then loudest (beam 4), kind perch v1, t = the window end",
        [(r["k"], r["v"], r["src"], r["beam"], r["t"]) for r in recs] == [("perch", 1, "mixed", None, 1000.0), ("perch", 1, "loudest", 4, 1000.0)],
        [(r["src"], r["beam"]) for r in recs],
    )
    check(
        "4 the label for 3 kHz is 'Cyanocitta cristata' (label 2) with logit 6.05 within 0.07 on both sources",
        mixed["top"][0][0] == loud["top"][0][0] == "Cyanocitta cristata" and abs(loud["top"][0][1] - exp) < 0.07 and abs(mixed["top"][0][1] - exp) < 0.07,
        (mixed["top"][0], loud["top"][0]),
    )
    check(
        "4 top lists have exactly 5 entries and the other four labels are the idle ranking [0, -0.1, -0.3, -0.4] (label 2 is the tone)",
        [t_[0] for t_ in loud["top"][1:]] == ["Poecile carolinensis", "Poecile atricapillus", "Oecanthus dulcisonans", "Insect"]
        and [t_[1] for t_ in loud["top"][1:]] == [0.0, -0.1, -0.3, -0.4],
        loud["top"],
    )
    check(
        "4 level_dbfs is the source's rms: mixed -31.5, loudest -13.5 (derivation in the header)",
        (mixed["level_dbfs"], loud["level_dbfs"]) == (-31.5, -13.5),
        (mixed["level_dbfs"], loud["level_dbfs"]),
    )
    check(
        "4 best_species = the tone label, top1_group 'bird' (genus Cyanocitta is in ui/taxa.json's bird list), emb_i null, run null, mv = file name @ sha256[:12]",
        loud["best_species"][0] == "Cyanocitta cristata"
        and loud["top1_group"] == "bird"
        and "cyanocitta" in taxa_doc["genera"]["bird"]
        and loud["emb_i"] is None
        and loud["run"] is None
        and loud["mv"] == p.mv
        and p.mv.startswith("tiny_perch.onnx@")
        and len(p.mv.split("@")[1]) == 12,
        (loud, p.mv),
    )
    import hashlib

    with open(MP, "rb") as fh_:
        want_sha = hashlib.sha256(fh_.read()).hexdigest()
    check(
        "4 provenance (perch 1.1.0): model {Perch, 2.0, variant = src, sha256 of the model file}, claim, max_unstored_score = the 6th logit -0.5 on both sources",
        mixed["model"] == {"name": "Perch", "version": "2.0", "variant": "mixed", "sha256": want_sha}
        and loud["model"] == {"name": "Perch", "version": "2.0", "variant": "loudest", "sha256": want_sha}
        and p.mv.endswith("@" + want_sha[:12])
        and mixed["claim"] == loud["claim"] == {"human_verified": False, "source": "model_opinion"}
        and (mixed["max_unstored_score"], loud["max_unstored_score"]) == (-0.5, -0.5),
        (mixed["model"], loud["model"], loud["max_unstored_score"]),
    )
    r4 = run1(p, g, window(4000, 0.3), t1=1010.0)[1]
    r5 = run1(p, g, window(5000, 0.3), t1=1020.0)[1]
    r7 = run1(p, g, window(7000, 0.3), t1=1030.0)[1]
    check(
        "4 groups: 4 kHz 'Oecanthus dulcisonans' -> insect (genus in the table), 5 kHz FSD class 'Insect' -> insect, 7 kHz FSD class 'Alarm' -> non_species",
        (r4["top1_group"], r5["top1_group"], r7["top1_group"]) == ("insect", "insect", "non_species") and "oecanthus" in taxa_doc["genera"]["insect"],
        (r4["top1_group"], r5["top1_group"], r7["top1_group"]),
    )
    check(
        "4 best_species skips non-species classes: the 5 kHz window's top-1 is 'Insect' but best_species is the best BINOMIAL ('Poecile carolinensis', logit 0.0); positive twin: 3 kHz best_species is the top-1",
        r5["top"][0][0] == "Insect" and r5["best_species"] == ["Poecile carolinensis", 0.0],
        (r5["top"][0], r5["best_species"]),
    )
    st = p.stats()
    check(
        "4 stats after 4 emitted windows: gated 4 (the silent resolving windows), nothing skipped or pending",
        (st["gated"], st["speech_skipped"], st["voice_skipped"], st["unknown_dropped"], st["nonfinite"], st["pending"]) == (4, 0, 0, 0, 0, 0),
        st,
    )

    # ================================================================================================ 5 skips, each with its positive twin
    p, g = mk()
    speech_recs = run1(p, g, window(3000, 0.3), speech=True)
    check(
        "5 speech: a window the tagger flagged as speech yields NO record and counts speech_skipped 1",
        speech_recs == [] and p.stats()["speech_skipped"] == 1,
        (speech_recs, p.stats()),
    )
    clear_recs = run1(p, g, window(3000, 0.3), t1=2000.0)
    check(
        "5 speech twin: the same window with a non-speech tagger flag yields 2 records and speech_skipped stays 1",
        len(clear_recs) == 2 and p.stats()["speech_skipped"] == 1,
        (len(clear_recs), p.stats()),
    )
    p, g = mk()
    only_perch = lambda rs: [r for r in rs if r["k"] == "perch"]
    r_lo, r_hi = only_perch(run1(p, g, noise_window(-71.0))), only_perch(run1(p, g, noise_window(-69.0), t1=2000.0))
    check(
        "5 gate: every beam at -71 dBFS (below the -70 dBFS gate) yields nothing and counts gated; the twin at -69 dBFS yields 2 records",
        r_lo == [] and len(r_hi) == 2 and r_hi[1]["level_dbfs"] == -69.0 and p.stats()["gated"] == 3,
        (r_lo, [r["level_dbfs"] for r in r_hi], p.stats()),
    )
    p, g = mk()
    voice = run1(p, g, window(6000, 0.3))
    check(
        "5 human voice: a 6 kHz tone makes the label 'Speech' the top-1: no record, voice_skipped 1, and the word never appears in any output",
        voice == [] and p.stats()["voice_skipped"] == 1 and "Speech" not in json.dumps(p.stats()) and p.pending == [],
        (voice, p.stats()),
    )
    ok_recs = run1(p, g, window(8000, 0.3), t1=2000.0)
    check(
        "5 human voice twin: an 8 kHz tone (label 'Cricket') is reported, and its top 5 hold no voice label",
        len(ok_recs) == 2 and not any(t_[0] in PC.HUMAN_VOICE_LABELS for r in ok_recs for t_ in r["top"]),
        ok_recs,
    )
    p, g = mk()

    def two_tones(b_):  # 3 kHz at 0.3 plus 6 kHz at b_, both on beam 4 (mixed and loudest then see the same ratio)
        x_ = window(3000, 0.3)
        x_[:, 3] += (b_ * np.sin(2 * np.pi * 6000 * np.arange(N_IN) / FS)).astype(np.float32)
        return x_

    hid = run1(p, g, two_tones(0.05))
    check(
        "5 voice check covers the whole top 5, not only the top 1: 3 kHz at 0.3 plus 6 kHz at 0.05 puts 'Speech' 5th (logit -0.372 above 'Insect' -0.4): the window is dropped",
        hid == [] and p.stats()["voice_skipped"] == 1,
        p.stats(),
    )
    hid2 = run1(p, g, two_tones(0.012), t1=2000.0)
    check(
        "5 voice twin: with the 6 kHz part at 0.012 'Speech' stays 6th (-0.49), outside the top 5: the window is reported (2 records, no voice label in them)",
        len(hid2) == 2 and not any(t_[0] in PC.HUMAN_VOICE_LABELS for r in hid2 for t_ in r["top"]),
        hid2,
    )
    p, g = mk()
    bad = window(3000, 0.3)
    bad[1000, 2] = np.nan
    short = np.zeros((N_IN - 1, 8), np.float32)
    nf = run1(p, g, bad) + run1(p, g, short, t1=2000.0)
    check(
        "5 non-finite input and a wrong-shaped window are refused and counted (nonfinite 2), no record; twin: a finite window works",
        nf == [] and p.stats()["nonfinite"] == 2 and len(run1(p, g, window(3000), t1=3000.0)) == 2,
        p.stats(),
    )

    # ---- 5b every human-voice label (the owner policy: never reported), the human species, and non-finite model outputs
    EXPECTED_VOICE = sorted(
        [
            "Speech",
            "Male_speech_and_man_speaking",
            "Female_speech_and_woman_speaking",
            "Child_speech_and_kid_speaking",
            "Conversation",
            "Chatter",
            "Speech_synthesizer",
            "Human_voice",
            "Whispering",
            "Shout",
            "Yell",
            "Screaming",
            "Singing",
            "Male_singing",
            "Female_singing",
            "Laughter",
            "Chuckle_and_chortle",
            "Giggle",
            "Crying_and_sobbing",
            "Cough",
            "Sneeze",
            "Breathing",
            "Gasp",
            "Sigh",
            "Burping_and_eructation",
            "Respiratory_sounds",
            "Crowd",
            "Cheering",
            "Human_group_actions",
            "Homo sapiens",
        ]
    )
    check(
        "5 the human-voice set is exactly the 30 pinned labels (a deleted or added member is a deliberate, reviewed change)",
        sorted(PC.HUMAN_VOICE_LABELS) == EXPECTED_VOICE,
        sorted(set(EXPECTED_VOICE) ^ set(PC.HUMAN_VOICE_LABELS)),
    )
    NEUTRAL = ["Poecile carolinensis", "Poecile atricapillus", "Cyanocitta cristata", "Oecanthus dulcisonans", "Insect", "Alarm", "Cricket"]
    vdir = os.path.join(tmp, "voice")
    os.makedirs(vdir)

    def voice_perch(labels):
        lp_ = os.path.join(vdir, f"l{len(os.listdir(vdir))}.csv")
        TM.write_labels(lp_, labels)
        return PC.Perch(MP, lp_, PC.SpeechGuard(), taxa=None, min_logit=1.0)

    leaked, kept_wrongly = [], []
    for lab_ in EXPECTED_VOICE:
        pv = voice_perch([lab_] + NEUTRAL)  # label 0 = the voice label; a 1 kHz tone makes it the top-1
        out_ = run1(pv, pv.guard, window(1000, 0.3))
        if out_ != [] or pv.stats()["voice_skipped"] != 1 or pv.pending != []:
            leaked.append((lab_, len(out_), pv.stats()["voice_skipped"]))
        pw = voice_perch([lab_] + NEUTRAL)  # tone on label 1: label 0 is idle rank 2, still inside the top 5
        if run1(pw, pw.guard, window(2000, 0.3)) != [] or pw.stats()["voice_skipped"] != 1:
            kept_wrongly.append(lab_)
    check("5 every one of the 30 human-voice labels as the top-1 drops the window: no record, voice_skipped 1, nothing pending", leaked == [], leaked)
    check(
        "5 every one of the 30 labels in the idle top 5 (tone on another label) drops the window too: the check covers the whole top 5",
        kept_wrongly == [],
        kept_wrongly,
    )
    pk = voice_perch(["Rain"] + NEUTRAL)
    rk = run1(pk, pk.guard, window(1000, 0.3))
    check(
        "5 voice twin: the identical model and tone with a non-voice label 0 ('Rain') yields 2 records, top-1 that label, voice_skipped 0",
        len(rk) == 2 and rk[1]["top"][0][0] == "Rain" and pk.stats()["voice_skipped"] == 0,
        (rk, pk.stats()),
    )
    # 'Homo sapiens' is a binomial: it must never be the reported best species even when it sits outside the top 5
    ph = voice_perch(["Insect", "Alarm", "Cricket", "Wind_chime", "Chirp_and_tweet", "Homo sapiens", "Poecile carolinensis", "Rain"])
    rh = run1(ph, ph.guard, window(8000, 0.3))
    hs_top = [t_[0] for t_ in rh[1]["top"]] if len(rh) == 2 else None
    check(
        "5 'Homo sapiens' outside the top 5 (idle rank 7, -0.5) is not the best species although it is the best binomial of the list: best_species 'Poecile carolinensis' (-0.6); top 5 = Rain, Insect, Alarm, Cricket, Wind_chime",
        len(rh) == 2
        and hs_top == ["Rain", "Insect", "Alarm", "Cricket", "Wind_chime"]
        and rh[1]["best_species"][0] == "Poecile carolinensis"
        and abs(rh[1]["best_species"][1] + 0.6) < 0.07,
        (rh and rh[1]["best_species"], hs_top),
    )
    check(
        "5 twin: the species selection keeps the ordinary binomial and drops the human one",
        [ph.labels[i] for i in ph.species_idx] == ["Poecile carolinensis"],
        [ph.labels[i] for i in ph.species_idx],
    )
    # a model that returns NaN: the window is dropped and counted, nothing is written
    nd_ = os.path.join(tmp, "nanmodels")
    os.makedirs(nd_)
    for which in ("label", "embedding"):
        nm_ = TM.build(os.path.join(nd_, which + ".onnx"), poison=which)
        ed_ = os.path.join(tmp, "nanemb_" + which)
        os.makedirs(ed_)
        pn = PC.Perch(nm_, LP, PC.SpeechGuard(), taxa=None, min_logit=1.0, embeddings_dir=ed_)
        rn = run1(pn, pn.guard, window(3000, 0.3))
        check(
            f"5 a model whose {which} output is NaN: no record, nonfinite 1, no embedding stored, nothing pending, no file written",
            rn == [] and pn.stats()["nonfinite"] == 1 and pn.stats()["embeddings_stored"] == 0 and pn.pending == [] and os.listdir(ed_) == [],
            (rn, pn.stats(), os.listdir(ed_)),
        )
    ed_ok = os.path.join(tmp, "nanemb_ok")
    os.makedirs(ed_ok)
    pn = PC.Perch(MP, LP, PC.SpeechGuard(), taxa=None, min_logit=1.0, embeddings_dir=ed_ok)
    rn = run1(pn, pn.guard, window(3000, 0.3))
    check(
        "5 NaN twin: the clean model on the same window gives 2 records with finite logits, nonfinite 0 and 2 stored embeddings",
        len(rn) == 2 and pn.stats()["nonfinite"] == 0 and pn.stats()["embeddings_stored"] == 2 and all(math.isfinite(t_[1]) for r in rn for t_ in r["top"]),
        (rn, pn.stats()),
    )

    # ================================================================================================ 6 the speech guard
    G = PC.SpeechGuard()
    check("6 guard: no tagger window at all -> None (wait)", G.verdict(100.0, 105.0) is None)
    G.note(98.0, 103.0, False)
    check("6 guard: tagger covers the start only (ends 103 < 105) -> None", G.verdict(100.0, 105.0) is None)
    G.note(100.0, 105.0, False)
    check("6 guard: windows [98,103] and [100,105] without speech cover [100,105] -> clear", G.verdict(100.0, 105.0) == "clear")
    G.note(102.0, 107.0, True)
    G.note(107.0, 112.0, False)
    check(
        "6 guard: a speech window overlapping at all ([102,107] vs [100,105]) -> speech; a window that only touches the speech window's end ([107,112]) is clear (positive twin)",
        G.verdict(100.0, 105.0) == "speech" and G.verdict(107.0, 112.0) == "clear",
    )
    G2 = PC.SpeechGuard()
    G2.note(100.0, 105.0, False)
    G2.note(110.0, 115.0, False)
    check(
        "6 guard: a gap in the tagger coverage (105..110) after the tagger passed the window's end -> unknown; positive twin: a window inside one tagger window is clear",
        G2.verdict(104.0, 112.0) == "unknown" and G2.verdict(101.0, 104.0) == "clear",
    )
    G3 = PC.SpeechGuard()
    G3.note(100.2, 105.0, False)
    check(
        "6 guard: edge jitter within 0.5 s is covered ([100.2,105] covers [100,105]); 0.6 s late is not (unknown)",
        G3.verdict(100.0, 105.0) == "clear" and (lambda g_: (g_.note(100.6, 105.0, False), g_.verdict(100.0, 105.0))[1])(PC.SpeechGuard()) == "unknown",
    )
    p, g = mk()
    recs0, _ = p.process(window(3000, 0.3), 5000.0)
    check("6 plugin: with no tagger window yet the window is held (no record, pending 1)", recs0 == [] and p.stats()["pending"] == 1, p.stats())
    g.note(4995.0, 5000.0, False)
    recs1, _ = p.process(window(None, noise_dbfs=-100.0), 5006.0)
    check(
        "6 plugin: once the tagger covers it the held window is written by the next process() call (2 records, t 5000.0, pending 0)",
        len(recs1) == 2 and recs1[0]["t"] == 5000.0 and p.stats()["pending"] == 0,
        (recs1, p.stats()),
    )
    p, g = mk()
    p.process(window(3000, 0.3), 5000.0)
    r_late, _ = p.process(window(None, noise_dbfs=-100.0), 5031.0)
    check(
        "6 plugin: a window the tagger never covered is dropped after 30 s (unknown_dropped 1, no record) - fail closed",
        r_late == [] and p.stats()["unknown_dropped"] == 1 and p.stats()["pending"] == 0,
        p.stats(),
    )
    p, g = mk()
    p.process(window(3000, 0.3), 5000.0)
    check(
        "6 plugin: shutdown flush drops the unconfirmed window (unknown_dropped 1) and writes nothing for it",
        p.flush() == [] and p.stats()["unknown_dropped"] == 1,
        p.stats(),
    )

    # ================================================================================================ 7 embeddings
    ed = os.path.join(tmp, "emb")
    os.makedirs(ed)
    p, g = mk(embeddings_dir=ed)
    run1(p, g, window(3000, 0.3), t1=1000.0, speech=True)
    check("7 embeddings: a speech-flagged window leaves no embedding file at all", not os.listdir(ed) and p.stats()["embeddings_stored"] == 0, os.listdir(ed))
    recs = run1(p, g, window(3000, 0.3), t1=2000.0)
    bin_ = os.path.join(ed, "perch_emb.f16")
    vec = (
        np.frombuffer(open(bin_, "rb").read(), np.float16).astype(np.float32).reshape(-1, 16) if os.path.exists(bin_) else np.full((2, 16), np.nan, np.float32)
    )
    idx = (
        [json.loads(x) for x in open(os.path.join(ed, "perch_emb.jsonl"), "rb").read().splitlines()]
        if os.path.exists(os.path.join(ed, "perch_emb.jsonl"))
        else []
    )
    check(
        "7 embeddings: two float16 vectors of 16 values (64 bytes), emb_i 0 and 1 in the records, index lines carry t, src, beam, run",
        os.path.exists(bin_)
        and os.path.getsize(bin_) == 64
        and [r["emb_i"] for r in recs] == [0, 1]
        and idx == [{"i": 0, "t": 2000.0, "src": "mixed", "beam": None, "run": None}, {"i": 1, "t": 2000.0, "src": "loudest", "beam": 4, "run": None}],
        (os.path.exists(bin_), idx),
    )
    check(
        "7 embeddings: vector content: the sine part of detector 3 (index 8 + 2) is 0.25 within 0.003 and the cosine part ~0 (|c| < 0.003), rest ~0",
        abs(vec[1][10] - 0.25) < 0.003 and np.abs(np.delete(vec[1], 10)).max() < 0.003,
        vec[1],
    )
    check(
        "7 embeddings: without --perch-embeddings (the default) no file is written and emb_i is null",
        not os.path.exists(os.path.join(tmp, "perch_emb.f16")) and mk()[0].store is None,
    )
    from perch import PerchEmbeddingStore as PES

    rd = os.path.join(tmp, "rot")
    os.makedirs(rd)
    st_ = PES(rd, cap=96, run=7)
    for i in range(5):
        st_.add(np.full(16, i, np.float32), 100.0 + i, "mixed", None)
    cur = [json.loads(x)["i"] for x in open(os.path.join(rd, "perch_emb.jsonl"), "rb").read().splitlines()]
    old = [json.loads(x)["i"] for x in open(os.path.join(rd, "perch_emb.jsonl.1"), "rb").read().splitlines()]
    check(
        "7 rotation: cap 96 bytes = 3 vectors: vectors 0-2 rotate to .1 when vector 3 arrives; current holds 3 and 4; disk use <= 2 x cap; 1 rotation, 0 errors",
        old == [0, 1, 2]
        and cur == [3, 4]
        and os.path.getsize(os.path.join(rd, "perch_emb.f16.1")) == 96
        and os.path.getsize(os.path.join(rd, "perch_emb.f16")) == 64
        and st_.rotations == 1
        and st_.errors == 0
        and st_.n == 5,
        (old, cur, st_.rotations, st_.errors),
    )
    nd = os.path.join(tmp, "norot")
    os.makedirs(nd)
    s2 = PES(nd, cap=160)
    for i in range(5):
        s2.add(np.zeros(16, np.float32), 1.0, "mixed", None)
    check(
        "7 rotation twin: with cap 160 (5 vectors) the same 5 vectors never rotate (0 rotations, no .1 file, 160 bytes)",
        s2.rotations == 0 and not os.path.exists(os.path.join(nd, "perch_emb.f16.1")) and os.path.getsize(os.path.join(nd, "perch_emb.f16")) == 160,
        (s2.rotations, os.listdir(nd)),
    )
    blocker = os.path.join(tmp, "blocker")
    open(blocker, "w").write("x")
    bad_store = PES(blocker, cap=96)
    check(
        "7 failure: an unwritable directory is counted (errors 1), add returns None, nothing raises; positive twin: a good directory returns the index",
        bad_store.add(np.zeros(16), 1.0, "mixed", None) is None
        and bad_store.errors == 1
        and PES(os.path.join(tmp, "rot")).add(np.zeros(16), 1.0, "mixed", None) is not None,
    )

    # 7 alignment: a failed index write leaves no orphan vector; numbering survives a restart; misaligned leftovers are not appended to
    def read_store(d):
        raw = open(os.path.join(d, "perch_emb.f16"), "rb").read() if os.path.exists(os.path.join(d, "perch_emb.f16")) else b""
        ix = (
            [json.loads(x) for x in open(os.path.join(d, "perch_emb.jsonl"), "rb").read().splitlines()]
            if os.path.exists(os.path.join(d, "perch_emb.jsonl"))
            else []
        )
        return np.frombuffer(raw, np.float16).astype(np.float32).reshape(-1, 16), ix

    fd = os.path.join(tmp, "idxfail")
    os.makedirs(fd)
    sf = PES(fd)
    sf.add(np.full(16, 1, np.float32), 1.0, "mixed", None)
    os.makedirs(os.path.join(fd, "held"))
    os.replace(os.path.join(fd, "perch_emb.jsonl"), os.path.join(fd, "held", "keep"))
    os.makedirs(os.path.join(fd, "perch_emb.jsonl"))  # the index path is a directory: the vector write works, the index write raises
    lost = sf.add(np.full(16, 2, np.float32), 2.0, "mixed", None)
    os.rmdir(os.path.join(fd, "perch_emb.jsonl"))
    os.replace(os.path.join(fd, "held", "keep"), os.path.join(fd, "perch_emb.jsonl"))
    got3 = sf.add(np.full(16, 3, np.float32), 3.0, "mixed", None)
    vecs, ix = read_store(fd)
    check(
        "7 a failed index write is undone: add returns None, errors 1, afterwards vectors == index lines (2) and index i=1 (t 3.0) points at the vector of value 3, not at the lost value 2",
        lost is None
        and sf.errors == 1
        and got3 == 1
        and vecs.shape == (2, 16)
        and [i_["i"] for i_ in ix] == [0, 1]
        and ix[1]["t"] == 3.0
        and float(vecs[1][0]) == 3.0
        and float(vecs[0][0]) == 1.0,
        (lost, sf.errors, got3, vecs[:, 0], ix),
    )
    gd = os.path.join(tmp, "okstore")
    os.makedirs(gd)
    sg = PES(gd)
    for v_ in (1, 2, 3):
        sg.add(np.full(16, v_, np.float32), float(v_), "mixed", None)
    vg, ig = read_store(gd)
    check(
        "7 twin: with no failure 3 adds give 3 vectors with values 1 2 3 and index i 0 1 2, 0 errors",
        vg.shape == (3, 16) and list(vg[:, 0]) == [1.0, 2.0, 3.0] and [i_["i"] for i_ in ig] == [0, 1, 2] and sg.errors == 0,
        (vg[:, 0], ig),
    )
    rs = os.path.join(tmp, "restart")
    os.makedirs(rs)
    for run_, vals in ((1, (10, 11)), (2, (12, 13))):
        s_ = PES(rs, run=run_)
        for v_ in vals:
            s_.add(np.full(16, v_, np.float32), float(v_), "mixed", None)
    vr, ir = read_store(rs)
    first_i = ir[0]["i"]
    check(
        "7 restart: a second store on the same directory continues the numbering: i = 0,1,2,3 (runs 1,1,2,2), and vector i sits at (i - first_i) with values 10..13",
        [i_["i"] for i_ in ir] == [0, 1, 2, 3]
        and [i_["run"] for i_ in ir] == [1, 1, 2, 2]
        and [float(vr[i_["i"] - first_i][0]) for i_ in ir] == [10.0, 11.0, 12.0, 13.0],
        (ir, vr[:, 0]),
    )
    ms = os.path.join(tmp, "misaligned")
    os.makedirs(ms)
    s_ = PES(ms)
    for v_ in (1, 2):
        s_.add(np.full(16, v_, np.float32), float(v_), "mixed", None)
    with open(os.path.join(ms, "perch_emb.f16"), "ab") as f_:
        f_.write(np.full(16, 9, np.float16).tobytes())  # an orphan vector left by a crash between the two writes
    s2_ = PES(ms)
    r_new = s2_.add(np.full(16, 5, np.float32), 5.0, "mixed", None)
    vm, im = read_store(ms)
    vold = np.frombuffer(open(os.path.join(ms, "perch_emb.f16.1"), "rb").read(), np.float16).reshape(-1, 16)
    check(
        "7 leftover files with 3 vectors but 2 index lines are not appended to: they rotate to .1 (3 vectors kept), the new generation holds 1 vector whose index line has i=2 at offset 0, 1 rotation",
        r_new == 2 and s2_.rotations == 1 and vold.shape == (3, 16) and vm.shape == (1, 16) and float(vm[0][0]) == 5.0 and [i_["i"] for i_ in im] == [2],
        (r_new, s2_.rotations, vold.shape, vm.shape, im),
    )

    # ================================================================================================ 8 agreement aggregation, exact counts
    A = PC.Agreement("mv1", 1.0)
    M = 6000.0  # minute 100 = [6000, 6060)

    def bird(sci, conf, grp="bird", **kw):
        return dict(k="bird", scientific=sci, confidence=conf, group=grp, **kw)

    plan = [  # t1, perch (best sci, logit, top1 group), birds (None = no window; "gated"; [] = silent; or top)
        (6005.0, ("Poecile carolinensis", 6.0, "bird"), ("avail", ("poecile carolinensis", "bird", 0.9))),
        (6010.0, ("Poecile atricapillus", 6.0, "bird"), ("avail", ("poecile carolinensis", "bird", 0.9))),
        (6015.0, ("Cyanocitta cristata", 6.0, "bird"), ("avail", ("poecile carolinensis", "bird", 0.9))),
        (6020.0, ("Cyanocitta cristata", 6.0, "bird"), ("avail", None)),
        (6025.0, ("Cyanocitta cristata", 0.5, "bird"), ("avail", ("poecile carolinensis", "bird", 0.9))),
        (6030.0, ("Cyanocitta cristata", 0.5, "non_species"), ("avail", None)),
        (6035.0, ("Cyanocitta cristata", 6.0, "bird"), None),
        (6040.0, ("Cyanocitta cristata", 6.0, "bird"), ("gated", None)),
        (6045.0, ("Oecanthus dulcisonans", 6.0, "insect"), ("avail", ("gryllus pennsylvanicus", "insect", 0.7))),
        (6050.0, ("Cyanocitta cristata", 6.0, "bird"), ("avail", ("gryllus pennsylvanicus", "insect", 0.6))),
    ]
    for t1, (sci, lg_, grp), b in plan:
        A.add_perch(t1, sci, lg_, grp)
        if b is not None:
            A.add_birds(t1 - 3.0, t1, b[0] == "avail", b[1])
    check("8 agreement: a minute is not closed before its end + 15 s grace (now 6074.9 -> nothing); positive twin below at 6075.0", A.close(6074.9) == [])
    rec = A.close(6075.0)
    want = {
        "k": "perch_agreement",
        "v": 1,
        "t": 6000.0,
        "mv": "mv1",
        "min_logit": 1.0,
        "windows": 10,
        "both_same_species": 1,
        "both_same_genus": 1,
        "both_differ": 3,
        "perch_only": 1,
        "birds_only": 1,
        "both_silent": 1,
        "birds_missing": 2,
        "perch_top1_groups": {"bird": 8, "insect": 1, "non_species": 1},
        "insect": {"perch": 1, "birds": 2, "both": 1},
    }
    check(
        "8 agreement: exact record for the 10-window minute (same species 1, same genus 1, differ 3 [two bird/bird, one insect/insect], perch_only 1, birds_only 1, both_silent 1, birds_missing 2 [no window, gated window], groups, insect 1/2/1)",
        rec == [want] and sum(want[c] for c in PC.Agreement.CATS) == want["windows"],
        rec,
    )
    check("8 agreement: a closed minute is not emitted twice", A.close(7000.0) == [])
    B = PC.Agreement("mv1", 1.0)
    B.add_perch(6100.0, "Poecile carolinensis", 6.0, "bird")  # P = [6095, 6100]
    B.add_birds(6094.0, 6097.0, True, ("poecile carolinensis", "bird", 0.9))  # overlap exactly 2.0 s: counts
    C = PC.Agreement("mv1", 1.0)
    C.add_perch(6100.0, "Poecile carolinensis", 6.0, "bird")
    C.add_birds(6093.9, 6096.9, True, ("poecile carolinensis", "bird", 0.9))  # overlap 1.9 s: does not count
    rb, rc = B.close(7000.0)[0], C.close(7000.0)[0]
    check(
        "8 agreement overlap rule: a birds window overlapping the Perch window by exactly 2.0 s counts (both_same_species 1); by 1.9 s it does not (birds_missing 1)",
        (rb["both_same_species"], rb["birds_missing"], rc["both_same_species"], rc["birds_missing"]) == (1, 0, 0, 1),
        (rb, rc),
    )
    D = PC.Agreement("mv1", 1.0)
    D.add_perch(6100.0, "Cyanocitta cristata", 6.0, "bird")
    D.add_birds(6095.0, 6098.0, True, ("poecile carolinensis", "bird", 0.4))
    D.add_birds(6097.0, 6100.0, True, ("cyanocitta cristata", "bird", 0.8))
    check(
        "8 agreement: with two overlapping birds windows the higher-confidence species is BirdNET's opinion (0.8 'cyanocitta cristata' beats 0.4 -> same species, case-insensitively)",
        D.close(7000.0)[0]["both_same_species"] == 1,
    )
    E = PC.Agreement("mv1", 1.0)
    E.add_perch(6100.0, "Cyanocitta cristata", 6.0, "bird")
    E.add_birds(6097.0, 6100.0, True, ("poecile carolinensis", "bird", 0.8))
    E.add_perch(6200.0, "Cyanocitta cristata", 6.0, "bird")
    check(
        "8 agreement: minutes are separate records in time order (6100 -> minute 6060, 6200 -> minute 6180); the final close marks a minute that had not reached its grace as partial",
        [(r["t"], r["windows"], r.get("partial")) for r in E.close(6250.0)] == [(6060.0, 1, None)]
        and [(r["t"], r["windows"], r.get("partial")) for r in E.close(6250.0, final=True)] == [(6180.0, 1, True)],
    )
    pf, _ = mk()
    pf.feed_birds(
        100.0,
        [
            dict(k="bird", scientific="Canis lupus familiaris", confidence=0.99, non_species=True),
            dict(k="bird_filtered", scientific="Poecile atricapillus", confidence=0.9),
        ],
        {"beams_classified": 2},
    )
    top_none = pf.agree.birds[-1][3]
    pf.feed_birds(110.0, [dict(k="bird", scientific="Poecile atricapillus", confidence=0.9, group="bird", non_species=False)], {"beams_classified": 2})
    pf.feed_birds(120.0, [], {"beams_classified": 0})
    check(
        "8 feed_birds: non_species rows and bird_filtered rows do not count as BirdNET naming a species (top None) while a real species row does; a window with beams_classified 0 is marked unavailable",
        (top_none, pf.agree.birds[-2][3], pf.agree.birds[-1][2]) == (None, ("poecile atricapillus", "bird", 0.9), False),
        pf.agree.birds,
    )

    # ================================================================================================ 9 plugin + agreement through the real model
    p, g = mk(min_logit=1.0)
    t0, allrec = 6005.0, []
    for i, (hz, bsci) in enumerate(
        [(1000, "Poecile carolinensis"), (2000, "Poecile carolinensis"), (3000, "Poecile carolinensis"), (8000, None), (1000, "Poecile carolinensis")]
    ):
        t1 = t0 + 5 * i
        g.note(t1 - 5.0, t1, i == 4)  # the 5th window is flagged speech by the tagger
        allrec += p.process(window(hz, 0.3), t1)[0]
        p.feed_birds(t1, [dict(k="bird", scientific=bsci, confidence=0.8, group="bird", non_species=False)] if bsci else [], {"beams_classified": 2})
    allrec += p.process(window(None, noise_dbfs=-100.0), 6080.0)[0]
    ag = [r for r in allrec if r["k"] == "perch_agreement"]
    pr = [r for r in allrec if r["k"] == "perch"]
    check(
        "9 through the real model: 1 kHz / 2 kHz / 3 kHz / 8 kHz (Cricket, BirdNET silent) / a speech-flagged window -> one minute record over 4 windows: same species 1 (P. carolinensis), same genus 1 (P. atricapillus vs carolinensis), differ 1 (Cyanocitta), both_silent 1 (Cricket is no species: best binomial logit 0.0 < min_logit 1.0)",
        len(ag) == 1
        and ag[0]["windows"] == 4
        and (ag[0]["both_same_species"], ag[0]["both_same_genus"], ag[0]["both_differ"], ag[0]["both_silent"]) == (1, 1, 1, 1)
        and ag[0]["perch_top1_groups"] == {"bird": 3, "insect": 1}
        and ag[0]["insect"] == {"perch": 1, "birds": 0, "both": 0}
        and "partial" not in ag[0],
        ag,
    )
    check(
        "9 the speech-flagged window (t 6025) is in no perch record and counted once (speech_skipped 1); the other four windows (6005..6020) have 2 records each",
        sorted(r["t"] for r in pr) == [t_ for t_ in (6005.0, 6010.0, 6015.0, 6020.0) for _ in (0, 1)] and p.stats()["speech_skipped"] == 1,
        (sorted(r["t"] for r in pr), p.stats()),
    )

    # ================================================================================================ 10 start-up failures: plugin not started
    g = PC.SpeechGuard()
    for nm, args, text in (
        ("missing model", (os.path.join(tmp, "nope.onnx"), LP, g), "model file not found"),
        ("missing labels", (MP, os.path.join(tmp, "nope.csv"), g), "label file not found"),
        ("no labels path", (MP, None, g), "label file not found"),
        ("no speech guard", (MP, LP, None), "no speech guard"),
    ):
        ok, msg = raises(lambda: PC.Perch(*args), PC.PerchUnavailable, text)
        check(f"10 {nm}: PerchUnavailable whose message starts with 'not started' and names the cause", ok and msg.startswith("not started"), msg)
    seven = os.path.join(tmp, "seven.csv")
    TM.write_labels(seven, TM.LABELS[:7])
    ok, msg = raises(lambda: PC.Perch(MP, seven, g), PC.PerchUnavailable, "model has 8 labels, the label file 7")
    check("10 a label file whose length differs from the model's label output is refused", ok, msg)
    # ================================================================================================ 11 device selection (GPU) and the runtime directory
    check("11 provider_list: cpu is exactly the CPU provider", PC.provider_list("cpu") == ["CPUExecutionProvider"])
    check(
        "11 provider_list: cuda:1 is CUDA device_id 1 with the CPU behind it; cuda is device 0",
        PC.provider_list("cuda:1") == [("CUDAExecutionProvider", {"device_id": 1}), "CPUExecutionProvider"]
        and PC.provider_list("cuda")[0] == ("CUDAExecutionProvider", {"device_id": 0}),
    )
    for bad in ("gpu", "cuda:x", "cuda:", "", "CUDA"):
        ok, msg = raises(lambda: PC.provider_list(bad), PC.PerchUnavailable, "unknown device")
        check(f"11 provider_list refuses {bad!r} (not started, names the device)", ok and msg.startswith("not started"), msg)
    import onnxruntime as _ort

    _real, seen = _ort.InferenceSession, []

    class _Rec:  # records the providers asked for; the session behind it is the real CPU one
        def __init__(self, path, so, providers=None):
            seen.append(providers)
            self._s = _real(path, so, providers=["CPUExecutionProvider"])

        def __getattr__(self, n):
            return getattr(self._s, n)

        def get_providers(self):
            return ["CPUExecutionProvider"]

    _ort.InferenceSession = _Rec
    try:
        ok, msg = raises(lambda: PC.Perch(MP, LP, g, device="cuda:1"), PC.PerchUnavailable, "cuda:1 requested but the active provider is CPUExecutionProvider")
        p_cpu = PC.Perch(MP, LP, g)
    finally:
        _ort.InferenceSession = _real
    check(
        "11 CUDA requested but the session came up on the CPU: refused, no silent fallback (positive twin: device cpu starts)",
        ok and p_cpu.stats()["device"] == "cpu",
        msg,
    )
    check(
        "11 the providers handed to onnxruntime: cuda:1 -> CUDA device 1 then CPU; default -> CPU only",
        seen == [[("CUDAExecutionProvider", {"device_id": 1}), "CPUExecutionProvider"], ["CPUExecutionProvider"]],
        str(seen),
    )
    rt = os.path.join(tmp, "ortdir")
    os.makedirs(rt)
    n0 = len(sys.path)
    PC.Perch(MP, LP, g, ort_dir=rt)
    PC.Perch(MP, LP, g, ort_dir=rt)
    check(
        "11 ort_dir is appended to the END of sys.path, once (nothing in front of it is shadowed)",
        sys.path[-1] == rt and sys.path.count(rt) == 1 and len(sys.path) == n0 + 1,
        str(sys.path[-2:]),
    )
    sys.path.remove(rt)
    if "CUDAExecutionProvider" in _ort.get_available_providers():
        pg = PC.Perch(MP, LP, g, device="cuda:0")
        xin = np.random.default_rng(3).standard_normal((2, PC.N_MODEL)).astype(np.float32) * 0.1
        gout = pg.sess.run(None, {pg.in_name: xin})
        cout = p_cpu.sess.run(None, {p_cpu.in_name: xin})
        check(
            "11 real CUDA session: active provider CUDA, stats device cuda:0, outputs equal the CPU session's within 1e-4",
            pg.sess.get_providers()[0] == "CUDAExecutionProvider"
            and pg.stats()["device"] == "cuda:0"
            and all(float(np.max(np.abs(a - b))) < 1e-4 for a, b in zip(gout, cout)),
        )
    else:
        print("SKIP 11 real CUDA session: CUDAExecutionProvider is not available in this onnxruntime")

    garbage = os.path.join(tmp, "garbage.onnx")
    open(garbage, "wb").write(b"not an onnx model")
    ok, msg = raises(lambda: PC.Perch(garbage, LP, g), PC.PerchUnavailable, "not started")
    check("10 a model file that is not ONNX is refused with 'not started'", ok, msg)
    check("10 positive twin: the real tiny model and labels start", PC.Perch(MP, LP, g).labels == TM.LABELS)
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, importlib.abc\n"
                "class B(importlib.abc.MetaPathFinder):\n"
                "    def find_spec(self, n, p, t=None):\n"
                "        if n.split('.')[0] == 'onnxruntime': raise ImportError('blocked for test')\n"
                "sys.meta_path.insert(0, B()); sys.path.insert(0, %r)\n"
                "import perch\n"
                "try:\n"
                "    perch.Perch(%r, %r, perch.SpeechGuard())\n"
                "except perch.PerchUnavailable as e:\n"
                "    print('UNAVAILABLE', e)\n" % (HERE, MP, LP)
            ),
        ],
        capture_output=True,
        text=True,
    )
    check(
        "10 onnxruntime not importable: PerchUnavailable 'not started, onnxruntime is not installed' (the child blocks the import of the real package)",
        child.stdout.startswith("UNAVAILABLE not started: onnxruntime is not installed") and "<model-dir>/overlay" in child.stdout,
        (child.stdout, child.stderr[-200:]),
    )

    # ================================================================================================ 11 fetch tool
    import http.server, threading

    served = os.path.join(tmp, "served")
    os.makedirs(served)
    body_a, body_b = b"model-bytes" * 1000, b"label,list\n"
    open(os.path.join(served, "a.bin"), "wb").write(body_a)
    open(os.path.join(served, "b.csv"), "wb").write(body_b)

    class Q(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=served, **k)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Q)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    import hashlib

    files = [
        {"name": "perch_v2.onnx", "size": len(body_a), "sha256": hashlib.sha256(body_a).hexdigest(), "url": base + "/a.bin"},
        {"name": "labels.csv", "size": len(body_b), "sha256": hashlib.sha256(body_b).hexdigest(), "url": base + "/b.csv"},
    ]
    d1 = os.path.join(tmp, "dl1")
    msgs = []
    rc1 = FP.fetch(d1, files, log=msgs.append)
    check(
        "11 fetch: both files downloaded, size and sha256 verified, exit 0, no .part left",
        rc1 == 0
        and open(os.path.join(d1, "perch_v2.onnx"), "rb").read() == body_a
        and open(os.path.join(d1, "labels.csv"), "rb").read() == body_b
        and sorted(os.listdir(d1)) == ["labels.csv", "perch_v2.onnx"],
        (rc1, msgs, os.listdir(d1)),
    )
    rc2 = FP.fetch(d1, files, log=msgs.append)
    check(
        "11 fetch: refuses to overwrite (exit 2) and leaves the files byte-identical",
        rc2 == 2 and open(os.path.join(d1, "perch_v2.onnx"), "rb").read() == body_a,
        (rc2, msgs[-1]),
    )
    bad = [dict(files[0], sha256="0" * 64), files[1]]
    d2 = os.path.join(tmp, "dl2")
    rc3 = FP.fetch(d2, bad, log=msgs.append)
    check(
        "11 fetch: a sha256 mismatch exits 1 and deletes the partial file (nothing left in dest); positive twin above verified the same bytes",
        rc3 == 1 and os.listdir(d2) == [],
        (rc3, os.listdir(d2)),
    )
    d3 = os.path.join(tmp, "dl3")
    rc4 = FP.fetch(d3, [dict(files[0], url=base + "/missing.bin")], log=msgs.append)
    check("11 fetch: a 404 exits 1 with no file left", rc4 == 1 and os.listdir(d3) == [], (rc4, msgs[-1]))
    d4 = os.path.join(tmp, "dl4")
    os.makedirs(d4)
    open(os.path.join(d4, "labels.csv"), "w").write("mine")
    rc5 = FP.main(["--dest", d4])  # the REAL pins: refusal happens before any network traffic
    check(
        "11 fetch CLI with the pinned list: an existing labels.csv makes it refuse (exit 2) without touching it",
        rc5 == 2 and open(os.path.join(d4, "labels.csv")).read() == "mine" and os.listdir(d4) == ["labels.csv"],
        (rc5, os.listdir(d4)),
    )
    srv.shutdown()
    check(
        "11 pins: the two pinned sha256 values are 64 hex characters and the URLs are commit-pinned (40 hex) Hugging Face resolve links",
        all(len(f["sha256"]) == 64 and int(f["sha256"], 16) >= 0 and __import__("re").search(r"/resolve/[0-9a-f]{40}/", f["url"]) for f in FP.PINNED),
    )

    # ================================================================================================ 12 the real host over UDP
    if "--skip-e2e" in sys.argv:
        print("SKIP 12 host e2e over UDP  [--skip-e2e]")
    else:
        fake = os.path.join(tmp, "fakepanns")
        os.makedirs(os.path.join(fake, "panns_inference"))
        open(os.path.join(fake, "panns_inference", "__init__.py"), "w").write(
            "import numpy as np\n"
            "class AudioTagging:\n"
            "    def __init__(self, checkpoint_path=None, device=None):\n"
            "        pass\n"
            "    def inference(self, a):                       # speech when the window peak exceeds 0.6\n"
            "        clip = np.zeros((1, 527), np.float32)\n"
            "        clip[0, 0] = 0.9 if float(np.abs(a).max()) > 0.6 else 0.01\n"
            "        return clip, np.zeros((1, 2048), np.float32)\n"
        )
        open(os.path.join(fake, "panns_inference", "config.py"), "w").write("labels = ['Speech'] + ['label%d' % i for i in range(1, 527)]\n")
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([fake] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])))

        def free_port():
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.1", 0))
            pt = s.getsockname()[1]
            s.close()
            return pt

        def bmaa(slot, block, x):
            s24 = np.clip((x * 8388608.0).astype(np.int64), -8388608, 8388607).astype("<i4")
            return b"BMAA" + bytes([1, 0, slot, 0]) + struct.pack("<III", 0, block & 0xFFFFFFFF, len(x)) + s24.tobytes()

        def jl(path):
            with open(path, "rb") as f:
                return [json.loads(x) for x in f.read().splitlines() if x]

        def host(extra, secs, out_dir, launcher=None, port=None):
            os.makedirs(out_dir, exist_ok=True)
            cmd = [sys.executable] + ([launcher] if launcher else [os.path.join(HERE, "host.py")])
            return subprocess.Popen(
                cmd
                + [
                    "--listen",
                    f"127.0.0.1:{port or free_port()}",
                    "--out",
                    out_dir,
                    "--history-dir",
                    "",
                    "--no",
                    "spatial",
                    "--no",
                    "clap",
                    "--no",
                    "birds",
                    "--no",
                    "coherence",
                    "--secs",
                    str(secs),
                ]
                + extra,
                env=env,
                stderr=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )

        def status(out_dir):
            return json.load(open(os.path.join(out_dir, "status.json")))

        # 12a no --perch-model: off with the documented reason, the analyzer continues
        oa = os.path.join(tmp, "ea")
        pa = host([], 7, oa)
        pa.wait(timeout=180)
        sa = status(oa)
        check(
            "12 host without --perch-model: plugins.perch = {enabled false, reason 'disabled, no --perch-model'}, no perch.jsonl, dsp and tags run, exit 0",
            pa.returncode == 0
            and sa["plugins"]["perch"] == {"enabled": False, "reason": "disabled, no --perch-model"}
            and sa["plugins"]["dsp"]["enabled"]
            and sa["plugins"]["tags"]["enabled"]
            and not os.path.exists(os.path.join(oa, "perch.jsonl")),
            (pa.returncode, sa["plugins"], pa.stderr.read()[-300:]),
        )
        # 12b missing model file: not started, the others carry on
        ob = os.path.join(tmp, "eb")
        pb = host(["--perch-model", os.path.join(tmp, "absent.onnx")], 7, ob)
        pb.wait(timeout=180)
        sb = status(ob)
        rb = sb["plugins"]["perch"]
        check(
            "12 host with a missing --perch-model: perch enabled false, reason contains 'not started' and 'model file not found', dsp and tags still enabled, exit 0",
            pb.returncode == 0
            and rb["enabled"] is False
            and "not started" in rb["reason"]
            and "model file not found" in rb["reason"]
            and sb["plugins"]["dsp"]["enabled"]
            and sb["plugins"]["tags"]["enabled"],
            (pb.returncode, rb),
        )
        # 12c without tags the plugin refuses
        oc = os.path.join(tmp, "ec")
        pc_ = host(["--perch-model", MP, "--perch-labels", LP, "--no", "tags"], 7, oc)
        pc_.wait(timeout=180)
        rc_ = status(oc)["plugins"]["perch"]
        check(
            "12 host with --no tags: perch refuses ('not started: needs the tags plugin'), exit 0",
            pc_.returncode == 0 and rc_["enabled"] is False and "needs the tags plugin" in rc_["reason"],
            (pc_.returncode, rc_),
        )
        # 12d onnxruntime missing: launcher blocks the import and runs the real host
        launcher = os.path.join(tmp, "launch_blocked.py")
        open(launcher, "w").write(
            "import sys, importlib.abc, runpy\n"
            "class B(importlib.abc.MetaPathFinder):\n"
            "    def find_spec(self, n, p, t=None):\n"
            "        if n.split('.')[0] == 'onnxruntime': raise ImportError('blocked for test')\n"
            "sys.meta_path.insert(0, B())\n"
            "sys.argv[0] = %r\n"
            "runpy.run_path(%r, run_name='__main__')\n" % (os.path.join(HERE, "host.py"), os.path.join(HERE, "host.py"))
        )
        od = os.path.join(tmp, "ed")
        pd = host(["--perch-model", MP, "--perch-labels", LP], 7, od, launcher=launcher)
        pd.wait(timeout=180)
        rd_ = status(od)["plugins"]
        check(
            "12 host with onnxruntime not importable: perch 'not started ... onnxruntime is not installed', dsp still enabled, exit 0",
            pd.returncode == 0 and rd_["perch"]["enabled"] is False and "onnxruntime is not installed" in rd_["perch"]["reason"] and rd_["dsp"]["enabled"],
            (pd.returncode, rd_["perch"]),
        )
        # 12d2 perch.py not deployed (an install list that forgot it): perch is "not started", the rest runs; twin: with perch.py it starts
        for tag_, with_perch in (("noperch", False), ("withperch", True)):
            dd = os.path.join(tmp, "app_" + tag_)  # a deployment tree: analysis/ (with or without perch.py) next to classify/, pc/ and ui/
            os.makedirs(os.path.join(dd, "analysis"))
            os.makedirs(os.path.join(dd, "ui"))
            for fn_ in os.listdir(HERE):
                if fn_.endswith((".py", ".json")) and not fn_.startswith("test_") and (with_perch or fn_ != "perch.py"):
                    shutil.copy(os.path.join(HERE, fn_), os.path.join(dd, "analysis"))
            for sub_ in ("classify", "pc"):
                shutil.copytree(os.path.join(BMA, sub_), os.path.join(dd, sub_), ignore=shutil.ignore_patterns("__pycache__", "test_*"))
            shutil.copy(TAXA, os.path.join(dd, "ui", "taxa.json"))
            oo = os.path.join(tmp, "eo_" + tag_)
            po = host(["--perch-model", MP, "--perch-labels", LP], 7, oo, launcher=os.path.join(dd, "analysis", "host.py"))
            po.wait(timeout=180)
            ro = status(oo)["plugins"]
            if not with_perch:
                check(
                    "12 host copied WITHOUT perch.py plus --perch-model: perch enabled false, reason names the missing module, dsp and tags run, exit 0",
                    po.returncode == 0
                    and ro["perch"]["enabled"] is False
                    and "No module named 'perch'" in ro["perch"]["reason"]
                    and ro["dsp"]["enabled"]
                    and ro["tags"]["enabled"],
                    (po.returncode, ro["perch"]),
                )
            else:
                check(
                    "12 twin: the same copy WITH perch.py starts perch (enabled true), exit 0",
                    po.returncode == 0 and ro["perch"]["enabled"] is True,
                    (po.returncode, ro["perch"], po.stderr.read()[-300:]),
                )
        # 12e the full run: tone, then a loud (speech-flagged) stretch, then tone again
        oe = os.path.join(tmp, "ee")
        port = free_port()
        pe = host(["--perch-model", MP, "--perch-labels", LP, "--perch-embeddings", "--perch-min-logit", "1.0"], 60, oe, port=port)
        for _ in range(480):
            if os.path.exists(os.path.join(oe, "status.json")) or pe.poll() is not None:
                break
            time.sleep(0.25)
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blk, t_start, nb = 0, time.time(), 0
        SECS, LOUD_FROM, LOUD_TO = 46, 20, 30
        rng = np.random.default_rng(5)
        for sec in range(SECS):
            amp = 0.9 if LOUD_FROM <= sec < LOUD_TO else 0.3
            x = (rng.standard_normal((FS, 8)) * 1e-4).astype(np.float32)
            x[:, 3] += (amp * np.sin(2 * np.pi * 3000 * np.arange(blk, blk + FS) / FS)).astype(np.float32)
            for off in range(0, FS, 256):
                for slot in range(8):
                    sk.sendto(bmaa(slot, blk + off, x[off : off + 256, slot]), ("127.0.0.1", port))
                nb += 1
                dly = t_start + nb * 256 / FS - time.time()
                if dly > 0:
                    time.sleep(dly)
            blk += FS
        pe.wait(timeout=180)
        err = pe.stderr.read()
        se = status(oe)
        rows = jl(os.path.join(oe, "perch.jsonl")) if os.path.exists(os.path.join(oe, "perch.jsonl")) else []
        recs_ = [r for r in rows if r["k"] == "perch"]
        agr = [r for r in rows if r["k"] == "perch_agreement"]
        pl = se["plugins"]["perch"]
        check(
            "12 full host run: exits 0, perch enabled with errors 0 and records > 0",
            pe.returncode == 0 and pl["enabled"] and pl["errors"] == 0 and pl["records"] > 0 and len(recs_) > 0,
            (pe.returncode, pl, err[-400:]),
        )
        check(
            "12 full host run: every record names the 3 kHz label 'Cyanocitta cristata' on both sources (the tone), pairs per window",
            all(r["top"][0][0] == "Cyanocitta cristata" for r in recs_) and sorted({r["src"] for r in recs_}) == ["loudest", "mixed"] and len(recs_) % 2 == 0,
            [(r["src"], r["top"][0]) for r in recs_][:4],
        )
        lo, hi = t_start + LOUD_FROM + 1.0, t_start + LOUD_TO - 1.0
        inside = [r for r in recs_ if r["t"] - 5.0 < hi and r["t"] > lo]
        before = {r["t"] for r in recs_ if r["t"] <= t_start + LOUD_FROM + 2.0}
        after = {r["t"] for r in recs_ if r["t"] - 5.0 >= t_start + LOUD_TO - 3.0}
        check(
            "12 full host run: NO record has a window overlapping the speech-flagged stretch (seconds 21-29), and there are records before it and after it (positive twin)",
            inside == [] and len(before) >= 1 and len(after) >= 1,
            ([(round(r["t"] - t_start, 1), r["src"]) for r in inside], sorted(round(t - t_start, 1) for t in before | after)),
        )
        check("12 full host run: the windows overlapping the loud stretch were counted as skipped (status speech_skipped >= 2)", pl["speech_skipped"] >= 2, pl)
        emb_idx = jl(os.path.join(oe, "perch_emb.jsonl")) if os.path.exists(os.path.join(oe, "perch_emb.jsonl")) else []
        emb_bytes = os.path.getsize(os.path.join(oe, "perch_emb.f16")) if os.path.exists(os.path.join(oe, "perch_emb.f16")) else -1
        print(
            f"INFO e2e: {len(recs_)} perch records ({len(recs_) // 2} windows), {len(agr)} agreement records, status counters "
            f"{ {k_: pl[k_] for k_ in ('windows', 'records', 'gated', 'speech_skipped', 'voice_skipped', 'unknown_dropped', 'pending', 'nonfinite')} }"
        )
        check(
            "12 full host run: with --perch-embeddings every record has an emb_i and the index/vector files hold exactly one entry per record (32 bytes each: the tiny model has 16 values)",
            all(r["emb_i"] is not None for r in recs_) and len(emb_idx) == len(recs_) and emb_bytes == 32 * len(recs_),
            (len(emb_idx), emb_bytes, len(recs_)),
        )
        wins = len(recs_) // 2
        check(
            "12 full host run: agreement records cover exactly the written windows (sum of windows == windows) and, with birds off, every window is birds_missing; the last record is partial (flushed at shutdown)",
            sum(r["windows"] for r in agr) == wins and all(r["birds_missing"] == r["windows"] for r in agr) and agr and agr[-1].get("partial") is True,
            (wins, agr),
        )
        files = os.listdir(oe)
        check(
            "12 full host run: no audio is written (audio_written false, no audio-like file under --out)",
            se["audio_written"] is False and not [f for f in files if f.lower().endswith((".wav", ".flac", ".mp3", ".ogg", ".pcm", ".raw", ".opus"))],
            files,
        )
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{n_checks} checks, {len(fails)} failure(s)")
for f_ in fails:
    print("  FAILED:", f_)
sys.exit(1 if fails else 0)
