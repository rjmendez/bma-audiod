#!/usr/bin/env python3
"""Tests for clips.py (wildlife clip capture, certainty > 0.85, 24 h audio retention) and its sidecars. The REAL ClipService, ClipStore,
host.Ring, writer and sweeper run on real files in a temp dir; only the models (BirdNET/PANNs rows) and the clock are simulated
(clips_sim.py), and the OS boundary (os.replace / os.remove / the FLAC encoder) is stubbed with RECORDING stubs where a failure
has to be injected. Exact values; every negative case has a positive twin. Run:  python -I test_clips.py  (audioeval venv or any
python with numpy, scipy, soundfile, PIL; jsonschema is optional: without it the schema checks print SKIP).
The last section runs the real host.py over UDP (about 50 s)."""

import errno, json, os, shutil, socket, struct, subprocess, sys, tempfile, threading, time
import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import clips as CL  # noqa: E402
import clip_features as CF  # noqa: E402
import clips_sim as S  # noqa: E402

FS = 48000
fails, n_skip = [], 0
TMP = tempfile.mkdtemp(prefix="clips_test_")
_k = [0]


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def skip(name, why):
    global n_skip
    n_skip += 1
    print(f"SKIP {name}  [{why}]")


def sim(**kw):
    kw.setdefault("audio", True)  # sections 1-11 test the AUDIO path (--clip-audio); section 13 tests the default, visual-only
    kw.setdefault("min_beams", 1)  # the older sections test one rule at a time with single-beam events; section 12 tests --clip-min-beams itself
    _k[0] += 1
    return S.Sim(os.path.join(TMP, f"s{_k[0]}", "clips"), **kw)


def cnt(s, k):
    return s.svc.counters()[k]


def n_stored(s):
    return len(s.clip_ids())


def one(conf, sci="Poecile carolinensis", **kw):
    """Run a single-window event (window k=5, [15, 18) s) and return the finished sim."""
    s = sim(**{k: v for k, v in kw.items() if k in ("tagger", "loud", "delay", "min_conf", "min_beams")})
    s.play({5: [S.row(sci, conf, **{k: v for k, v in kw.items() if k in ("beam", "group", "non_species", "in_region", "kind")})]}, tags=kw.get("tags"))
    return s


def base_tags(*extra, skip=()):
    """Explicit tags windows covering [4, 24) exactly (plus a later window, 25 s, so that no tags window can still start inside G), plus extra
    (start, p[, active]) windows; `skip` removes base starts."""
    return [(s, 0.0) for s in (4, 9, 14, 19, 25) if s not in skip] + list(extra)


# 0. the owner rule first: audio is OFF unless --clip-audio ("I cannot keep voice recordings")
check("audio is off by default (ClipConfig(), and on with audio=True as the twin)", CL.ClipConfig().audio is False and CL.ClipConfig(audio=True).audio is True)

# ====================================================================================================================
# 1. certainty: strictly above 0.85, the MAX of the event, never an average
s = one(0.85)
check(
    "conf 0.85 exactly: nothing stored, the event counted as low confidence",
    n_stored(s) == 0 and cnt(s, "clips_skipped_low_conf") == 1 and s.audio_files() == [],
    str((n_stored(s), s.svc.counters())),
)
s = one(0.8501)
check("conf 0.8501: stored (positive twin of 0.85)", n_stored(s) == 1 and cnt(s, "clips_stored") == 1)
s = one(0.9)
check("conf 0.9: stored", n_stored(s) == 1)
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.86)], 6: [S.row("Poecile carolinensis", 0.60)], 7: [S.row("Poecile carolinensis", 0.60)]})
ids = s.clip_ids()
check(
    "event [0.86, 0.60, 0.60] (mean 0.687): stored on its MAX, confidence_max exactly 0.86",
    len(ids) == 1 and s.sidecar(*ids[0])["confidence_max"] == 0.86,
    str(ids),
)
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.84)], 6: [S.row("Poecile carolinensis", 0.84)], 7: [S.row("Poecile carolinensis", 0.84)]})
check("event [0.84, 0.84, 0.84]: nothing stored (one event, counted once)", n_stored(s) == 0 and cnt(s, "clips_skipped_low_conf") == 1, str(s.svc.counters()))
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.9)], 6: [], 7: [S.row("Poecile carolinensis", 0.9)]})
check("two same-species reports 6 s apart with a silent window between are ONE event (one clip)", n_stored(s) == 1)
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.9)], 9: [S.row("Poecile carolinensis", 0.9)]}, k_end=14)
check("same-species reports 12 s apart are TWO events (two clips)", n_stored(s) == 2, str(s.clip_ids()))

# 2. wildlife only
for g, sci in (
    ("bird", "Poecile carolinensis"),
    ("insect", "Neoconocephalus triops"),
    ("amphibian", "Pseudacris crucifer"),
    ("mammal", "Canis latrans"),
    ("other-bio", "Xus unknownus"),
):
    s = one(0.91, sci=sci, group=g)
    check(f"group {g}: stored", n_stored(s) == 1, str(s.svc.counters()))
s = one(0.91, group=None)
check(
    "group null: NOT stored and counted (twin: group bird above is stored)",
    n_stored(s) == 0 and cnt(s, "clips_skipped_no_group") == 1 and s.audio_files() == [],
)
s = one(0.99, sci="Engine", group="mechanical", non_species=True)
check("mechanical class (Engine, non_species true): not stored", n_stored(s) == 0 and cnt(s, "clips_skipped_not_wildlife") == 1)
s = one(0.99, sci="Dog", group="mammal", non_species=True)
check("Dog (taxa says mammal but non_species true): not stored", n_stored(s) == 0 and cnt(s, "clips_skipped_not_wildlife") == 1)
s = one(0.99, sci="Human vocal", group="mechanical", non_species=True)
check("Human vocal: not stored", n_stored(s) == 0)
s = sim()
ex = (("Canis familiaris", "mammal"), ("Gallus gallus", "bird"), ("Felis catus", "mammal"), ("Bos taurus", "mammal"), ("Human vocal", "bird"))
s.play({5: [S.row(n, 0.95, group=g) for n, g in ex]})
check(
    "excluded names (dog, chicken, cat, cattle, human) with a wildlife group and non_species false: none stored, 5 counted",
    n_stored(s) == 0 and cnt(s, "clips_skipped_excluded") == 5 and s.audio_files() == [],
    str(s.svc.counters()),
)
s = sim()
tw = (("Canis latrans", "mammal"), ("Gallus varius", "bird"), ("Felis silvestris", "mammal"), ("Bos javanicus", "mammal"))
s.play({5: [S.row(n, 0.95, group=g) for n, g in tw]})
check(
    "positive twin: coyote, green junglefowl, wildcat, banteng are stored (the list matches whole names, not prefixes of them)",
    n_stored(s) == 4 and cnt(s, "clips_skipped_excluded") == 0,
    str(s.svc.counters()),
)
import taxa as T  # noqa: E402

bad = T.Taxa(os.path.join(TMP, "nope.json"))
rows = []
for sci in ("Poecile carolinensis", "Neoconocephalus triops", "Canis latrans"):
    grp, ns = bad.lookup(sci, sci)
    r = S.row(sci, 0.95, group=grp)
    r["non_species"] = False if ns is None else bool(ns)
    rows.append(r)
s = sim()
s.play({5: rows})
check(
    "taxa.json missing (the real Taxa class): group null on every row, nothing stored, the counter equals the 3 events",
    n_stored(s) == 0 and cnt(s, "clips_skipped_no_group") == 3 and bad.missing == 3,
    str((s.svc.counters(), bad.missing)),
)
tj = os.path.join(TMP, "taxa.json")
with open(tj, "w") as f:
    json.dump({"version": 1, "explicit_labels": {}, "genera": {"bird": ["poecile"], "insect": ["neoconocephalus"], "mammal": ["canis"]}}, f)
good = T.Taxa(tj)
rows = []
for sci in ("Poecile carolinensis", "Neoconocephalus triops", "Canis latrans"):
    grp, ns = good.lookup(sci, sci)
    r = S.row(sci, 0.95, group=grp)
    r["non_species"] = bool(ns)
    rows.append(r)
s = sim()
s.play({5: rows})
check("positive twin: with a real taxa.json the same three species are stored", n_stored(s) == 3 and good.missing == 0, str(s.svc.counters()))

# out of region (decision: same rule, flagged; --clip-skip-out-of-region switches it off)
s = sim()
s.play({5: [S.row("Strix varia", 0.9, kind="bird_filtered")]})
ids = s.clip_ids()
check(
    "out-of-region bird_filtered at 0.9: stored, flagged in_region false, counted",
    len(ids) == 1 and s.sidecar(*ids[0])["in_region"] is False and cnt(s, "clips_out_of_region_stored") == 1,
    str(s.svc.counters()),
)
s = sim()
s.play({5: [S.row("Strix varia", 0.85, kind="bird_filtered")]})
check("out-of-region at exactly 0.85: not stored (same threshold)", n_stored(s) == 0)
s = sim(skip_out_of_region=True)
s.play({5: [S.row("Strix varia", 0.99, kind="bird_filtered")]})
check("--clip-skip-out-of-region: an out-of-region 0.99 is not stored, counted", n_stored(s) == 0 and cnt(s, "clips_skipped_out_of_region") == 1)

# 2b. the configured --clip-min-conf (owner 2026-10-27: 0.3 live): staging follows it, the rule stays strictly above
s = one(0.31, min_conf=0.3)
check(
    "min_conf 0.3: a 0.31 event is stored, confidence_max exactly 0.31",
    n_stored(s) == 1 and s.sidecar(*s.clip_ids()[0])["confidence_max"] == 0.31 and cnt(s, "clips_stored") == 1,
    str(s.svc.counters()),
)
s = one(0.30, min_conf=0.3)
check(
    "min_conf 0.3: exactly 0.30 is not stored, counted as low confidence (twin: 0.31 above)",
    n_stored(s) == 0 and cnt(s, "clips_skipped_low_conf") == 1 and s.audio_files() == [],
    str(s.svc.counters()),
)
s = one(0.29, min_conf=0.3)
check(
    "min_conf 0.3: 0.29 is below the staging floor, never tracked (no low-conf count), nothing stored",
    n_stored(s) == 0 and cnt(s, "clips_skipped_low_conf") == 0,
    str(s.svc.counters()),
)
s = one(0.6)
check(
    "default min_conf: 0.6 not stored, counted as low confidence (old staging floor 0.5 unchanged)",
    n_stored(s) == 0 and cnt(s, "clips_skipped_low_conf") == 1 and s.cfg.stage_floor == 0.5,
    str(s.svc.counters()),
)
s = one(0.49)
check(
    "default min_conf: 0.49 is not even tracked (characterization of the old floor: no low-conf count)",
    n_stored(s) == 0 and cnt(s, "clips_skipped_low_conf") == 0,
    str(s.svc.counters()),
)
s = one(0.4, min_conf=0.35)
check(
    "min_conf 0.35: 0.4 stored (staging floor min(0.5, 0.35) = 0.35 is below the old 0.5)",
    n_stored(s) == 1 and s.cfg.stage_floor == 0.35,
    str(s.svc.counters()),
)
s = one(0.6, min_conf=0.95)
check(
    "min_conf 0.95: 0.6 not stored; staging floor stays 0.5 (twin of the 0.35 case)",
    n_stored(s) == 0 and cnt(s, "clips_skipped_low_conf") == 1 and s.cfg.stage_floor == 0.5,
    str(s.svc.counters()),
)
s = sim(min_conf=0.3)
s.play({5: [S.row("Strix varia", 0.9, kind="bird_filtered")]})
check(
    "min_conf 0.3 without the skip flag: an out-of-region 0.9 is stored, flagged in_region false",
    n_stored(s) == 1 and s.sidecar(*s.clip_ids()[0])["in_region"] is False,
    str(s.svc.counters()),
)
s = sim(min_conf=0.3, skip_out_of_region=True)
s.play({5: [S.row("Strix varia", 0.9, kind="bird_filtered")]})
check(
    "min_conf 0.3 with --clip-skip-out-of-region: the same out-of-region 0.9 is not stored, counted",
    n_stored(s) == 0 and cnt(s, "clips_skipped_out_of_region") == 1,
    str(s.svc.counters()),
)
s = sim(min_conf=0.3, skip_out_of_region=True)
s.play({5: [S.row("Strix varia", 0.31)]})
check("min_conf 0.3 with the skip flag: an in-region 0.31 is still stored (twin)", n_stored(s) == 1, str(s.svc.counters()))
for bad in (0.049, 1.01, 0.0, -1.0, float("nan")):
    try:
        CL.ClipConfig(min_conf=bad)
        check(f"ClipConfig min_conf {bad} is refused", False, "no error")
    except ValueError as e:
        check(f"ClipConfig min_conf {bad} is refused with a clear error", str(e) == f"--clip-min-conf must be between 0.05 and 1.0, got {float(bad)}", str(e))
check(
    "ClipConfig accepts the range ends 0.05 and 1.0 (twin of the refusals)",
    (CL.ClipConfig(min_conf=0.05).min_conf, CL.ClipConfig(min_conf=1.0).min_conf) == (0.05, 1.0),
)
import argparse  # noqa: E402

ap_ = argparse.ArgumentParser(prog="x")
CL.add_args(ap_)
check(
    "CLI: --clip-min-conf 0.3 parses to exactly 0.3, the default is 0.85",
    (ap_.parse_args(["--clip-min-conf", "0.3"]).clip_min_conf, ap_.parse_args([]).clip_min_conf) == (0.3, 0.85),
)
try:
    with open(os.devnull, "w") as dn, __import__("contextlib").redirect_stderr(dn):
        ap_.parse_args(["--clip-min-conf", "0.01"])
    check("CLI: --clip-min-conf 0.01 is refused", False, "no error")
except SystemExit as e:
    check("CLI: --clip-min-conf 0.01 exits with argparse status 2", e.code == 2, str(e.code))

# 3. privacy guard (clip = [14, 19), guard window G = [9, 24))
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.91)]}, tags=base_tags())
check("guard baseline: tags cover G exactly with no speech: stored", n_stored(s) == 1, str(s.svc.counters()))
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.91)]}, tags=base_tags((23.9, 0.5)))
check(
    "speech window starting 4.9 s after the clip end (overlaps G by 0.1 s): not stored",
    n_stored(s) == 0 and cnt(s, "clips_skipped_speech") == 1 and s.audio_files() == [],
    str(s.svc.counters()),
)
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.91)]}, tags=base_tags((24.1, 0.5)))
check("positive twin: the same speech window starting 5.1 s after the clip end (outside G): stored", n_stored(s) == 1, str(s.svc.counters()))
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.91)]}, tags=base_tags((4.1, 0.5)))
check("speech window ending 4.9 s before the clip start (overlaps G by 0.1 s): not stored", n_stored(s) == 0 and cnt(s, "clips_skipped_speech") == 1)
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.91)]}, tags=base_tags((3.9, 0.5)))
check("positive twin: speech window ending 5.1 s before the clip start: stored", n_stored(s) == 1)
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.91)]}, tags=base_tags((14, 0.10), skip=(14,)))
check(
    "speech_p 0.10 inside the span: not stored (threshold is >= 0.10, stricter than the tagger's 0.30)",
    n_stored(s) == 0 and cnt(s, "clips_skipped_speech") == 1,
)
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.91)]}, tags=base_tags((14, 0.099), skip=(14,)))
check(
    "positive twin: speech_p 0.099: stored, speech_guard.max_speech_p 0.099",
    n_stored(s) == 1 and s.sidecar(*s.clip_ids()[0])["speech_guard"]["max_speech_p"] == 0.099,
)
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.91)]}, tags=base_tags((14, 0.05, True), skip=(14,)))
check("speech_active true with speech_p 0.05: not stored", n_stored(s) == 0 and cnt(s, "clips_skipped_speech") == 1)
s = sim()
s.play(
    {
        5: [S.row("Poecile carolinensis", 0.91, beam=1), S.row("Human vocal", 0.30, beam=2, group="mechanical", non_species=True)],
        6: [S.row("Human vocal", 0.30, beam=2, group="mechanical", non_species=True)],
    }
)
check(
    "BirdNET 'Human vocal' on the OTHER beam inside G (low confidence, same and next window): not stored",
    n_stored(s) == 0 and cnt(s, "clips_skipped_human_label") == 1,
    str(s.svc.counters()),
)
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.91)], 4: [S.row("Human non-vocal", 0.3, beam=2, kind="bird_filtered", group="mechanical", non_species=True)]})
check("a Human non-vocal bird_filtered row 3 s before the clip: not stored", n_stored(s) == 0 and cnt(s, "clips_skipped_human_label") == 1)
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.91)], 8: [S.row("Human vocal", 0.3, beam=2, group="mechanical", non_species=True)]})
check("positive twin: the Human vocal row in the window that starts exactly at the end of G (24 s): stored", n_stored(s) == 1, str(s.svc.counters()))
s = sim(tagger=False)
s.play({5: [S.row("Poecile carolinensis", 0.91), S.row("Strix varia", 0.95)]})
check(
    "tagger unavailable (fail closed): nothing stored, tagger_unavailable equals the 2 events, no audio on disk",
    n_stored(s) == 0 and cnt(s, "clips_skipped_tagger_unavailable") == 2 and s.audio_files() == [] and s.files() == [],
    str((s.svc.counters(), s.files())),
)
s = sim(tagger=True)
s.play({5: [S.row("Poecile carolinensis", 0.91), S.row("Strix varia", 0.95)]})
check("positive twin: tagger available, the same two events are stored", n_stored(s) == 2)
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.91)]}, tags=base_tags((19.5, 0.0), skip=(19,)))
check(
    "a 0.5 s hole in the tags timeline inside G: not stored (guard_hole)",
    n_stored(s) == 0 and cnt(s, "clips_skipped_guard_hole") == 1 and s.audio_files() == [],
    str(s.svc.counters()),
)
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.91)]}, tags=base_tags((19.5, 0.0)))
check("positive twin: the same timeline with the hole filled: stored", n_stored(s) == 1)
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.91)]}, tags=[(4, 0.0), (9, 0.0), (14, 0.0), (18, 0.0)])
check(
    "verdict not complete (tags cover to 23, G ends at 24): nothing on disk, still pending",
    n_stored(s) == 0 and s.files() == [] and len(s.svc.pending) == 1 and cnt(s, "clips_skipped_no_verdict") == 0,
    str((s.files(), len(s.svc.pending))),
)
s.tags(19.0)
s.svc.drain()
check(
    "tags now cover G but no window has started at or after its end yet (one could still overlap it): still held, nothing on disk",
    n_stored(s) == 0 and len(s.svc.pending) == 1 and s.files() == [],
)
s.tags(25.0)
s.svc.drain()
check("positive twin: the next tags window arrives (starts after G), the held clip is stored", n_stored(s) == 1 and len(s.svc.pending) == 0)
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.91)]}, tags=[(4, 0.0), (9, 0.0), (14, 0.0), (18, 0.0)])
s.now = s.svc.pending[0]["deadline"] + 1
s.svc.tick(s.now)
check(
    "no verdict by the hold deadline: dropped and counted, nothing on disk",
    n_stored(s) == 0 and cnt(s, "clips_skipped_no_verdict") == 1 and len(s.svc.pending) == 0 and s.files() == [],
)
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.91)]}, tags=[])
s.now = s.svc.pending[0]["deadline"] + 1
s.svc.tick(s.now)
check("tagger enabled but it never produced a window: nothing stored (no verdict)", n_stored(s) == 0 and cnt(s, "clips_skipped_no_verdict") == 1)
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.91)]})
sp = sim()
sp.play({5: [S.row("Poecile carolinensis", 0.91)]}, tags=[(s_, 0.5) for s_ in range(-10, 46, 2)])
check(
    "speech everywhere: nothing stored and no audio file at all (twin: the default run stored one)",
    n_stored(sp) == 0 and sp.audio_files() == [] and sp.files() == [] and n_stored(s) == 1,
)

# 4. audio content
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.91)]})
day, cid = s.clip_ids()[0]
j = s.sidecar(day, cid)
a, rate = sf.read(os.path.join(s.root, day, cid + ".flac"), dtype="int16")
info = sf.info(os.path.join(s.root, day, cid + ".flac"))
check(
    "audio: every sample equals the beam-1 ring content of [14 s, 19 s) (pre-roll 1 s, window 3 s, post-roll 1 s)",
    rate == FS and a.shape == (5 * FS,) and bool((a == S.sig(0, S.BASE + 14 * FS, 5 * FS, (0, 1))).all()),
    str((rate, a.shape)),
)
check("audio: mono FLAC PCM_16 at 48000", (info.format, info.subtype, info.channels, info.samplerate) == ("FLAC", "PCM_16", 1, 48000), str(info))
check(
    "sidecar: start_sample, samples, duration and roll values exact",
    (j["start_sample"], j["samples"], j["duration_s"], j["pre_roll_s"], j["post_roll_s"], j["ring_truncated"], j["capped"])
    == (S.BASE + 14 * FS, 5 * FS, 5.0, 1.0, 1.0, False, False),
    str(j),
)
check(
    "only the three clip files and the index exist (no other audio)",
    s.files() == sorted([f"{day}/{cid}.flac", f"{day}/{cid}.json", f"{day}/{cid}.png", "index.jsonl"]),
    str(s.files()),
)
s = sim(delay=9.0)
s.play({5: [S.row("Poecile carolinensis", 0.91)]})
j = s.sidecar(*s.clip_ids()[0])
check(
    "ring too shallow for the pre-roll (processing delay 9 s): clip starts at 15 s, pre_roll_s 0.0, ring_truncated true (twin: deep ring above is 1.0 / false)",
    (j["pre_roll_s"], j["post_roll_s"], j["ring_truncated"], j["start_sample"]) == (0.0, 1.0, True, S.BASE + 15 * FS),
    str(j),
)
s = sim(loud=(2, 5))
s.play({5: [S.row("Poecile carolinensis", 0.88, beam=6), S.row("Poecile carolinensis", 0.95, beam=3)]})
day, cid = s.clip_ids()[0]
j = s.sidecar(day, cid)
a, _ = sf.read(os.path.join(s.root, day, cid + ".flac"), dtype="int16")
check(
    "beam choice: the best-confidence report (beam 3, 0.95) is stored, not the other beam; both beams are listed; audio is beam 3's",
    j["beam"] == 3 and j["beams_reported"] == [3, 6] and bool((a == S.sig(2, S.BASE + 14 * FS, 5 * FS, (2, 5))).all()) and cid.endswith("-b3"),
    str((j["beam"], j["beams_reported"], cid)),
)
s = sim(loud=(2, 5))
s.play({5: [S.row("Poecile carolinensis", 0.96, beam=6), S.row("Poecile carolinensis", 0.95, beam=3)]})
j = s.sidecar(*s.clip_ids()[0])
check("positive twin: with the confidences swapped beam 6 is stored", j["beam"] == 6)
s = sim(loud=(2, 5))
s.play({5: [S.row("Poecile carolinensis", 0.80, beam=6), S.row("Poecile carolinensis", 0.80, beam=3)], 6: [S.row("Poecile carolinensis", 0.95, beam=6)]})
day, cid = s.clip_ids()[0]
a, _ = sf.read(os.path.join(s.root, day, cid + ".flac"), dtype="int16")
check(
    "a 2-window event stores the peak window's beam across BOTH windows: 8 s, sample exact",
    bool((a == S.sig(5, S.BASE + 14 * FS, 8 * FS, (2, 5))).all()) and len(a) == 8 * FS,
    str(len(a)),
)
s = sim()
s.play({4: [S.row("Poecile carolinensis", 0.90)], 6: [S.row("Poecile carolinensis", 0.95)]})
day, cid = s.clip_ids()[0]
j = s.sidecar(day, cid)
check(
    "skipped window inside the event (audio not contiguous): clip is the contiguous run holding the peak window, [18 s, 22 s), no pre-roll",
    (j["start_sample"], j["samples"], j["pre_roll_s"], j["post_roll_s"]) == (S.BASE + 18 * FS, 4 * FS, 0.0, 1.0),
    str((j["start_sample"], j["samples"])),
)
s = sim()
birds = {k: [S.row("Neoconocephalus triops", 0.95 if k == 16 else 0.87, group="insect")] for k in range(2, 32)}
s.play(birds, k_end=40, tags_cover=(-10, 130))
ids = s.clip_ids()
j = s.sidecar(*ids[0])
day, cid = ids[0]
a, _ = sf.read(os.path.join(s.root, day, cid + ".flac"), dtype="int16")
check(
    "90 s event: ONE clip of exactly 12 s centred on the peak window, sample exact, capped true",
    len(ids) == 1
    and len(a) == 12 * FS
    and j["start_sample"] == S.BASE + int(43.5 * FS)
    and j["capped"] is True
    and bool((a == S.sig(0, S.BASE + int(43.5 * FS), 12 * FS, (0, 1))).all()),
    str((len(ids), len(a), j["start_sample"])),
)
check(
    "90 s event: the pending event never held more than the 12 s neighbourhood of the peak (no unbounded growth)",
    len(s.svc.events) == 0 and len(s.svc.pending) == 0,
)

# 5. per species per hour cap keeps the highest confidence
rng = np.random.default_rng(7)
confs = [round(0.86 + 0.0071 * i, 4) for i in range(20)]
order = list(rng.permutation(20))
s = sim()
s.play({3 * i: [S.row("Neoconocephalus triops", confs[o], group="insect")] for i, o in enumerate(order)}, k_end=64, tags_cover=(-10, 200))
kept = sorted(s.sidecar(*c)["confidence_max"] for c in s.clip_ids())
top6 = sorted(confs)[-6:]
check("20 same-species events in one UTC hour (random order): exactly the 6 highest-confidence clips remain", kept == top6, f"{kept} vs {top6}")
check(
    "cap: the 14 losers have no files and a clip_evicted tombstone each (arrival order is random, so the cap works on 'highest', not first or last)",
    sum(1 for r in s.index() if r["k"] == "clip_evicted") == cnt(s, "clips_evicted_cap") + 0
    and cnt(s, "clips_stored") - cnt(s, "clips_evicted_cap") == 6
    and len([f for f in s.files() if f.endswith(".flac")]) == 6,
    str((s.svc.counters()["clips_evicted_cap"], s.svc.counters()["clips_dropped_cap"], len([f for f in s.files() if f.endswith(".flac")]))),
)
check("cap: stored + dropped by the cap + evicted-after-store account for all 20 events", cnt(s, "clips_stored") + cnt(s, "clips_dropped_cap") == 20)
s = sim(cap_per_species_hour=2)
s.play({0: [S.row("Strix varia", 0.90)], 4: [S.row("Strix varia", 0.95)], 8: [S.row("Strix varia", 0.88)]}, k_end=14)
check(
    "cap 2: a third event with LOWER confidence than both is dropped (counted), the two stored stay",
    sorted(s.sidecar(*c)["confidence_max"] for c in s.clip_ids()) == [0.9, 0.95] and cnt(s, "clips_dropped_cap") == 1,
)
s = sim(cap_per_species_hour=2)
s.play({0: [S.row("Strix varia", 0.90)], 4: [S.row("Strix varia", 0.95)], 8: [S.row("Strix varia", 0.97)]}, k_end=14)
check(
    "positive twin: a third event with HIGHER confidence replaces the lowest (0.90): files gone, tombstone written",
    sorted(s.sidecar(*c)["confidence_max"] for c in s.clip_ids()) == [0.95, 0.97]
    and sum(1 for r in s.index() if r["k"] == "clip_evicted" and r["reason"] == "species_hour_cap") == 1
    and cnt(s, "clips_evicted_cap") == 1,
)
s = sim(cap_per_species_hour=1)
s.play({0: [S.row("Strix varia", 0.90)], 4: [S.row("Bubo virginianus", 0.90)]}, k_end=10)
check("cap 1: a different species in the same hour has its own cap", n_stored(s) == 2)
s = sim(cap_per_species_hour=1, t0=float(S.H0 + 3600 - 20))
s.play({1: [S.row("Strix varia", 0.90)], 9: [S.row("Strix varia", 0.90)]}, k_end=14)
check("cap 1: the same species in the NEXT UTC hour has its own cap (events at second 2 and 26 around the hour boundary)", n_stored(s) == 2, str(s.clip_ids()))

# 6. retention with a controllable clock
DAYS = 86400.0


def fresh(name_kw=None, **kw):
    s = sim(**kw)
    s.play({5: [S.row("Poecile carolinensis", 0.91)]})
    day, cid = s.clip_ids()[0]
    return s, day, cid, CL.id_time(cid)


def apath(s, day, cid):
    return os.path.join(s.root, day, cid + ".flac")


s, day, cid, tc = fresh()
s.svc.store.sweep(tc + 24 * 3600 - 60)
check("retention: audio at 23h59m00s is kept", os.path.exists(apath(s, day, cid)) and cnt(s, "audio_expired") == 0)
s.svc.store.sweep(tc + 24 * 3600 - 60)
check("retention: a second sweep at the same age keeps it (twin)", os.path.exists(apath(s, day, cid)))
s.svc.store.sweep(tc + 24 * 3600)
check("retention: audio at exactly 24h00m00s is deleted", not os.path.exists(apath(s, day, cid)) and cnt(s, "audio_expired") == 1)
s, day, cid, tc = fresh()
s.svc.store.sweep(tc + 24 * 3600 + 1)
check("retention: audio at 24h00m01s is deleted", not os.path.exists(apath(s, day, cid)) and cnt(s, "audio_expired") == 1)
s, day, cid, tc = fresh(audio_retention_h=2)
s.svc.store.sweep(tc + 2 * 3600 - 60)
kept = os.path.exists(apath(s, day, cid))
s.svc.store.sweep(tc + 2 * 3600 + 1)
check("retention --audio-retention-h 2: kept at 1h59m, deleted at 2h00m01s", kept and not os.path.exists(apath(s, day, cid)))
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.91)], 25: [S.row("Strix varia", 0.91)]}, k_end=32)
(d1, c1), (d2, c2) = sorted(s.clip_ids(), key=lambda x: x[1])
t1, t2 = CL.id_time(c1), CL.id_time(c2)
now = t1 + 24 * 3600 + 10
os.utime(apath(s, d1, c1), (now, now))  # fresh mtime, 24 h old capture
os.utime(apath(s, d2, c2), (1.0, 1.0))  # ancient mtime, 23 h 59 m old capture
s.svc.store.sweep(now)
check(
    "retention: age comes from the capture time in the id, not mtime (fresh-mtime old clip deleted, ancient-mtime young clip kept)",
    not os.path.exists(apath(s, d1, c1)) and os.path.exists(apath(s, d2, c2)) and t2 - t1 == 60.0,
    str((t2 - t1)),
)
s, day, cid, tc = fresh()
s.svc.store.sweep(tc - 3600)
check(
    "retention: a clock that jumped BACKWARD (negative age) keeps the audio and raises nothing",
    os.path.exists(apath(s, day, cid)) and cnt(s, "sweep_errors") == 0,
)
# sweep at start
s, day, cid, tc = fresh()
s2 = sim()
shutil.rmtree(s2.root, ignore_errors=True)
shutil.copytree(s.root, s2.root)
s2.now = tc + 30 * 3600
before = os.path.exists(apath(s2, day, cid))
s2.svc.start()
check(
    "retention: the sweep at start deletes 30 h old audio before any window is processed (twin: it existed before start)",
    before and not os.path.exists(apath(s2, day, cid)) and cnt(s2, "sweep_runs") == 1,
)
waits, gate = [], threading.Semaphore(0)


def fake_wait(sec):
    waits.append(sec)
    gate.acquire(timeout=5)


s3, day, cid, tc = fresh(start_threads=True, wait=fake_wait)
s3.now = tc + 24 * 3600 - 10
s3.svc.start()
time.sleep(0.3)
c0 = cnt(s3, "sweep_runs")
alive = os.path.exists(apath(s3, day, cid))
s3.now = tc + 24 * 3600 + 10
gate.release()
for _ in range(50):
    if cnt(s3, "sweep_runs") > c0:
        break
    time.sleep(0.1)
check(
    "retention: the periodic sweeper thread runs again when its wait returns (interval 300 s) and then deletes the expired audio",
    c0 == 1 and alive and waits[:1] == [300.0] and cnt(s3, "sweep_runs") == 2 and not os.path.exists(apath(s3, day, cid)),
    str((c0, waits, cnt(s3, "sweep_runs"))),
)
gate.release()
s3.svc.stop()
check("--clip-sweep-s is capped at 600 s (twin: 120 stays 120)", CL.ClipConfig(sweep_s=9999).sweep_s == 600.0 and CL.ClipConfig(sweep_s=120).sweep_s == 120.0)
# sidecars outlive the audio, the index only grows
s, day, cid, tc = fresh()
idx_path = os.path.join(s.root, "index.jsonl")
before_bytes = open(idx_path, "rb").read()
s.svc.store.sweep(tc + 25 * 3600)
after_bytes = open(idx_path, "rb").read()
j = s.sidecar(day, cid)
new = [json.loads(l) for l in after_bytes[len(before_bytes) :].split(b"\n") if l]
check(
    "after audio expiry: png and json remain, json says audio_expired true and audio_file null",
    os.path.exists(os.path.join(s.root, day, cid + ".png")) and j["audio_expired"] is True and j["audio_file"] is None,
    str(j.get("audio_file")),
)
check(
    "after audio expiry: index.jsonl only grew (earlier bytes identical) by exactly one clip_audio_expired tombstone for the id",
    after_bytes.startswith(before_bytes) and [(r["k"], r["id"]) for r in new] == [("clip_audio_expired", cid)],
    str(new),
)
s.svc.store.sweep(tc + 14 * DAYS - 1)
check("sidecars at 13d23h59m59s are still there (twin)", os.path.exists(os.path.join(s.root, day, cid + ".json")))
s.svc.store.sweep(tc + 14 * DAYS + 1)
tail = [json.loads(l) for l in open(idx_path, "rb").read()[len(after_bytes) :].split(b"\n") if l]
check(
    "sidecars at 14 d + 1 s: png and json deleted, clip_sidecar_expired appended, the empty day directory removed",
    not os.path.exists(os.path.join(s.root, day)) and [(r["k"], r["id"]) for r in tail] == [("clip_sidecar_expired", cid)],
    str((s.files(), tail)),
)
# orphans and temp files
s, day, cid, tc = fresh()
now = tc + 3600
dd = os.path.join(s.root, day)


def mk(name, age):
    p = os.path.join(dd, name)
    open(p, "wb").write(b"x" * 10)
    os.utime(p, (now - age, now - age))
    return p


old_tmp = mk(f"{int((now - 900) * 1000):013d}-x-b1.flac.tmp", 900)
new_tmp = mk(f"{int((now - 60) * 1000):013d}-y-b1.flac.tmp", 60)
old_orph = mk(f"{int((now - 900) * 1000):013d}-z-b1.flac", 900)
new_orph = mk(f"{int((now - 60) * 1000):013d}-w-b1.flac", 60)
s.svc.store.sweep(now)
check(
    "orphans: a .flac.tmp 15 min old and a .flac with no sidecar 15 min old are deleted (counted 2)",
    not os.path.exists(old_tmp) and not os.path.exists(old_orph) and cnt(s, "orphans_deleted") == 2,
    str(s.svc.counters()["orphans_deleted"]),
)
check(
    "orphans: the same files 1 minute old (a write may be in progress) are kept (twin), and the real clip is untouched",
    os.path.exists(new_tmp) and os.path.exists(new_orph) and os.path.exists(apath(s, day, cid)),
)
s.svc.store.sweep(now + 24 * 3600)
check("orphans: at retention age the leftover audio is deleted too", not os.path.exists(new_tmp) and not os.path.exists(new_orph))
# file in use
s, day, cid, tc = fresh()
calls = []


def stub_remove(p):
    calls.append(p)
    if len(calls) == 1:
        raise PermissionError(13, "in use", p)
    os.remove(p)


s.svc.store.remove = stub_remove
s.svc.store.sweep(tc + 25 * 3600)
check(
    "file in use (OS refuses the delete once): audio_expire_retry 1, file still present, not counted as expired",
    os.path.exists(apath(s, day, cid)) and cnt(s, "audio_expire_retry") == 1 and cnt(s, "audio_expired") == 0 and cnt(s, "sweep_errors") == 0,
    str(s.svc.counters()),
)
s.svc.store.sweep(tc + 25 * 3600 + 300)
check(
    "file in use: the next sweep deletes it (audio_expired 1, retry stays 1)",
    not os.path.exists(apath(s, day, cid)) and cnt(s, "audio_expired") == 1 and cnt(s, "audio_expire_retry") == 1,
)
if os.name == "nt":
    s, day, cid, tc = fresh()
    h = open(apath(s, day, cid), "rb")
    s.svc.store.sweep(tc + 25 * 3600)
    held = os.path.exists(apath(s, day, cid)) and cnt(s, "audio_expire_retry") == 1
    h.close()
    s.svc.store.sweep(tc + 25 * 3600 + 300)
    check(
        "Windows: a real open handle blocks the delete (retry 1, file present); after close the next sweep deletes it",
        held and not os.path.exists(apath(s, day, cid)) and cnt(s, "audio_expired") == 1,
    )
else:
    skip("Windows real open-handle delete", "not Windows (run this file with Windows python too)")
# a crash between the delete and the tombstone is reconciled
s, day, cid, tc = fresh()
os.remove(apath(s, day, cid))
s.svc.store.sweep(tc + 3600)
j = s.sidecar(day, cid)
check(
    "crash between delete and tombstone: the next sweep marks the sidecar audio_expired and appends the tombstone",
    j["audio_expired"] is True
    and j["audio_file"] is None
    and any(r["k"] == "clip_audio_expired" and r["id"] == cid and r["reason"] == "reconcile" for r in s.index()),
)

# 7. atomic writes, errors, bounded queue
calls = []


def replace_once(a, b):
    calls.append((a, b))
    if len(calls) == 1:
        raise OSError(errno.EIO, "boom")
    os.replace(a, b)


s = sim(replace=replace_once)
s.play({5: [S.row("Poecile carolinensis", 0.91)], 9: [S.row("Strix varia", 0.91)]}, k_end=14)
check(
    "os.replace fails once: no final audio, no .tmp left for that clip, write_errors exactly 1; the next clip stores normally",
    cnt(s, "clip_write_errors") == 1
    and n_stored(s) == 1
    and len([f for f in s.files() if f.endswith(".tmp")]) == 0
    and "-strixvaria-" in [f for f in s.files() if f.endswith(".flac")][0]
    and cnt(s, "clips_stored") == 1,
    str((s.files(), s.svc.counters()["clip_write_errors"])),
)
check(
    "the first replace call was the audio file (a recording stub saw tmp -> final)",
    calls[0][0].endswith(".flac.tmp") and calls[0][1].endswith(".flac"),
    str(calls[:1]),
)


class FullDisk:
    def __init__(self, real):
        self.real, self.n = real, 0

    def available_formats(self):
        return self.real.available_formats()

    def write(self, path, *a, **k):
        self.n += 1
        if self.n == 1:
            open(path, "wb").write(b"partial")
            raise OSError(errno.ENOSPC, "No space left on device")
        return self.real.write(path, *a, **k)


s = sim()
s.svc.store.sf = FullDisk(s.svc.store.sf)
s.play({5: [S.row("Poecile carolinensis", 0.91)], 9: [S.row("Strix varia", 0.91)]}, k_end=14)
check(
    "disk full on the first audio write (partial tmp left by the encoder): counted once, the partial file removed, nothing raised, next clip stored",
    cnt(s, "clip_write_errors") == 1 and n_stored(s) == 1 and not [f for f in s.files() if f.endswith(".tmp")] and cnt(s, "clip_service_errors") == 0,
    str(s.files()),
)
s = sim()
s.svc.on_plugin("birds", None, None, 123.0)
s.svc.note_window("birds", 1.0, S.BASE, 144000)
s.svc.on_plugin("birds", None, np.zeros((144000, 8), np.float32), 1.0)
check(
    "an exception inside the policy code is counted (clip_service_errors 1) and does not propagate (a window with no note is ignored silently)",
    cnt(s, "clip_service_errors") == 1,
    str(cnt(s, "clip_service_errors")),
)
s.play({5: [S.row("Poecile carolinensis", 0.91)]})
check("positive twin: after the failure the service still stores a clip", n_stored(s) == 1)
# bounded queue, writer blocked
started, release = threading.Event(), threading.Event()
s = sim(queue_max=3, start_threads=True)
real_write = s.svc.store.write


def blocked_write(item):
    started.set()
    release.wait(10)
    return real_write(item)


s.svc.store.write = blocked_write
s.svc.start()
sp = [f"Genus{c} species" for c in "abcdefghij"]
first = {5: [S.row(sp[0], 0.95)]}
s.play(first, drain=False, k_end=12)
started.wait(5)
for i in range(1, 10):  # nine further events, each fully dispatched (windows 3 s apart + silence)
    base_k = 13 + 4 * i
    s.birds(3.0 * base_k, [S.row(sp[i], 0.95)])
    for k in range(base_k + 1, base_k + 4):
        s.birds(3.0 * k, [])
    for st in range(int(3.0 * base_k) - 8, int(3.0 * base_k) + 20, 2):
        s.tags(float(st))
s.svc.tick(s.now)
check(
    "bounded queue, writer blocked on the first clip: 9 clips offered, queue holds 3, the other 6 are dropped and counted exactly",
    cnt(s, "clips_dropped_queue") == 6 and s.svc.q.qsize() == 3,
    str((s.svc.counters()["clips_dropped_queue"], s.svc.q.qsize())),
)
check("bounded queue: every analyzer call returned fast while the writer was blocked (slowest on_plugin under 0.5 s)", s.max_call_s < 0.5, str(s.max_call_s))
release.set()
for _ in range(100):
    if cnt(s, "clips_stored") == 4:
        break
    time.sleep(0.1)
check(
    "bounded queue: once the writer is free exactly the in-flight clip plus the 3 queued ones are stored (4)",
    cnt(s, "clips_stored") == 4 and n_stored(s) == 4,
    str(cnt(s, "clips_stored")),
)
s.svc.stop()
s = sim(queue_max=3)
s.play({5: [S.row(sp[0], 0.95)]}, drain=False)
for i in range(1, 10):
    base_k = 13 + 4 * i
    s.birds(3.0 * base_k, [S.row(sp[i], 0.95)])
    for k in range(base_k + 1, base_k + 4):
        s.birds(3.0 * k, [])
    for st in range(int(3.0 * base_k) - 8, int(3.0 * base_k) + 20, 2):
        s.tags(float(st))
    s.svc.drain()
s.svc.tick(s.now)
s.svc.drain()
check(
    "positive twin: with the writer keeping up (queue 3) none of the 10 clips is dropped",
    cnt(s, "clips_dropped_queue") == 0 and n_stored(s) == 10,
    str((cnt(s, "clips_dropped_queue"), n_stored(s))),
)

# 8. safety size cap: lowest confidence first, then oldest
s = sim()
s.play(
    {
        5: [S.row("Poecile carolinensis", 0.90)],
        9: [S.row("Strix varia", 0.95)],
        13: [S.row("Bubo virginianus", 0.88)],
        17: [S.row("Cyanocitta cristata", 0.99)],
    },
    k_end=24,
)
ents = {c: dict(e) for c, e in s.svc.store.table.items()}
tot = sum(e["audio_bytes"] for e in ents.values())
conf_of = {c: e["conf"] for c, e in ents.items()}
s.svc.store.sweep(s.now)
check(
    "size cap not exceeded (default 512 MB): nothing evicted (twin)",
    cnt(s, "clips_evicted_size") == 0 and len([f for f in s.files() if f.endswith(".flac")]) == 4,
)
s.cfg.audio_cap = tot - 1
s.svc.store.sweep(s.now)
gone = [c for c in ents if not os.path.exists(os.path.join(s.root, ents[c]["day"], c + ".flac"))]
check(
    "size cap exceeded by 1 byte: exactly the LOWEST-confidence clip (0.88) loses its audio, counted, tombstone with reason size_cap, sidecars stay",
    len(gone) == 1
    and conf_of[gone[0]] == 0.88
    and cnt(s, "clips_evicted_size") == 1
    and os.path.exists(os.path.join(s.root, ents[gone[0]]["day"], gone[0] + ".json"))
    and any(r["k"] == "clip_audio_expired" and r["reason"] == "size_cap" and r["id"] == gone[0] for r in s.index()),
    str((gone, conf_of)),
)
check("size cap: total audio is now at or under the cap", s.svc.store.audio_stats()[1] <= tot - 1, str(s.svc.store.audio_stats()))
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.90)], 9: [S.row("Strix varia", 0.90)], 13: [S.row("Bubo virginianus", 0.90)]}, k_end=20)
ents = sorted(s.svc.store.table.items(), key=lambda kv: kv[1]["t"])
s.cfg.audio_cap = sum(e["audio_bytes"] for _, e in ents) - 1
s.svc.store.sweep(s.now)
gone = [c for c, e in ents if not os.path.exists(os.path.join(s.root, e["day"], c + ".flac"))]
check("size cap with equal confidence: the OLDEST goes first", gone == [ents[0][0]], str(gone))
m = sim()
m.play({5: [S.row("Poecile carolinensis", 0.90)], 25: [S.row("Strix varia", 0.95)]}, k_end=32, tags_cover=(-10, 100))
c1 = max(e["audio_bytes"] for e in m.svc.store.table.values())
s = sim(audio_cap_mb=(c1 + 10) / (1 << 20))
s.play({5: [S.row("Poecile carolinensis", 0.90)], 25: [S.row("Strix varia", 0.95)]}, k_end=32, tags_cover=(-10, 100))
check(
    "size cap enforced on the write path too: the second (higher confidence) clip is stored and the 0.90 one is evicted to stay under the cap",
    cnt(s, "clips_stored") == 2 and cnt(s, "clips_evicted_size") == 1 and len([f for f in s.files() if f.endswith(".flac")]) == 1,
    str((s.svc.counters()["clips_stored"], s.svc.counters()["clips_evicted_size"], s.files())),
)

# 9. sidecars: signatures with exact expected numbers
t = np.arange(2 * FS) / FS
tone = np.round(0.1 * np.sin(2 * np.pi * 4000.0 * t) * 32768).astype(np.int16)
sg = CF.signatures(tone)
check(
    "signature: pure 4 kHz tone at 0.1 amplitude: peak -20.0 dBFS, rms -23.01 dBFS, duration 2.0",
    (sg["peak_dbfs"], sg["duration_s"]) == (-20.0, 2.0) and abs(sg["rms_dbfs"] + 23.01) < 0.01,
    str((sg["peak_dbfs"], sg["rms_dbfs"])),
)
check(
    "signature: dominant frequency track of the 4 kHz tone within 1 Hz everywhere, centroid within 5 Hz, bandwidth under 100 Hz",
    all(abs(v - 4000.0) < 1.0 for v in sg["dominant_hz"]) and abs(sg["centroid_hz"]["mean"] - 4000.0) < 5.0 and sg["bandwidth_hz"]["mean"] < 100.0,
    str((sg["dominant_hz"][:3], sg["centroid_hz"], sg["bandwidth_hz"])),
)
check(
    "signature: a steady tone has no onsets, SNR about 0 dB (only the 20 ms edge fades are quieter)",
    sg["onsets_s"] == [] and abs(sg["snr_db"]) < 1.5 and sg["ioi"]["n"] == 0 and sg["ioi"]["mean_s"] is None,
    str((sg["onsets_s"], sg["snr_db"])),
)
tone2 = np.round(0.1 * np.sin(2 * np.pi * 4500.0 * t) * 32768).astype(np.int16)
bd = CF.signatures(tone2)["band_db"]
check(
    "signature: band fingerprint of a 4.5 kHz tone: band 4-8 kHz reads -23.0 dB (+-0.3), the other two bands under -60 dB, 3 bands above the floor",
    len(bd) == 3 and abs(bd[0] + 23.01) < 0.3 and all(v < -60 for v in bd[1:]),
    str(bd),
)
clk = np.zeros(int(2.4 * FS))
for k in range(20):
    a0 = int((0.15 + 0.1 * k) * FS)
    clk[a0 : a0 + 144] = 0.5 * np.sin(2 * np.pi * 6000.0 * np.arange(144) / FS)
sc = CF.signatures(np.round(clk * 32768).astype(np.int16))
check(
    "signature: a 10 Hz click train gives 20 onsets with inter-onset interval mean 0.1 s (+-0.005), rate 10 Hz (+-0.5), cv under 0.05",
    sc["ioi"]["n"] == 20 and abs(sc["ioi"]["mean_s"] - 0.1) < 0.005 and abs(sc["ioi"]["rate_hz"] - 10.0) < 0.5 and sc["ioi"]["cv"] < 0.05,
    str(sc["ioi"]),
)
bur = np.zeros(int(4.3 * FS))
for k in range(16):
    a0 = int((0.2 + 0.25 * k) * FS)
    bur[a0 : a0 + int(0.1 * FS)] = 0.2 * np.sin(2 * np.pi * 6000.0 * np.arange(int(0.1 * FS)) / FS)
sb = CF.signatures(np.round(bur * 32768).astype(np.int16))
check(
    "signature: 6 kHz bursts at 4 Hz: 16 onsets, IOI mean 0.25 s (+-0.005), SNR over 40 dB (silent gaps)",
    sb["ioi"]["n"] == 16 and abs(sb["ioi"]["mean_s"] - 0.25) < 0.005 and sb["snr_db"] > 40,
    str((sb["ioi"], sb["snr_db"])),
)
wk = rng.standard_normal(4 * FS) * 0.005
for k in range(8):  # 8 tone bursts only 12 dB above the noise bed (a weak onset: flux about 12 dB)
    a0 = int((0.3 + 0.45 * k) * FS)
    wk[a0 : a0 + int(0.12 * FS)] += 0.0224 * np.sin(2 * np.pi * 6500.0 * np.arange(int(0.12 * FS)) / FS)
sw = CF.signatures(np.round(wk * 32768).astype(np.int16))
sn = CF.signatures(np.round(rng.standard_normal(4 * FS) * 0.005 * 32768).astype(np.int16))
check(
    "signature: 8 bursts 12 dB over a noise bed are found (8 onsets, IOI 0.45 s +-0.01); steady noise alone has 0 onsets (negative twin)",
    sw["ioi"]["n"] == 8 and abs(sw["ioi"]["mean_s"] - 0.45) < 0.01 and sn["onsets_s"] == [],
    str((sw["ioi"], sn["onsets_s"])),
)
check("signature: negative twin of the onset test - a 10 Hz train does NOT read 4 Hz", abs(sc["ioi"]["mean_s"] - 0.25) > 0.1)
env = CF.envelope(tone)
check(
    "envelope: 38 values for 2 s ((96000 - 4096) // 2400: under 20 per second), step 0.05 s, no min/max pairs, every value -23.0 dB (+-0.1), keys exactly n / step_s / rms_db",
    set(env) == {"n", "step_s", "rms_db"}
    and env["n"] == len(env["rms_db"]) == 38
    and env["step_s"] == 0.05
    and all(abs(v + 23.0) < 0.1 for v in env["rms_db"]),
    str(env),
)
import clip_render as CR  # noqa: E402

silence = CR.spectrogram_index(np.zeros(FS, np.int16))
tn = np.round(0.0316228 * np.sin(2 * np.pi * 6000.0 * np.arange(2 * FS) / FS) * 32768).astype(
    np.int16
)  # -30 dBFS on an FFT bin centre (6000 / 20 Hz = bin 300)
im = CR.spectrogram_index(tn)
row_t = 95 - int(np.floor(np.log(6000.0 / 3500.0) / np.log(24000.0 / 3500.0) * 96))  # image row 0 = the highest frequency
col = im[:, 5:30].mean(axis=1)
check(
    "spectrogram: 96 x 38 for 2 s (one column per 50 ms of the settled part: (96000 - 4096) // 2400), 1 s of silence is 96 x 18 all 0, a -30 dBFS tone at 6 kHz maps to the top of the range (255) on its row and under 40 elsewhere (int16 rounding harmonics of the tone, about -100 dB; fixed dB range)",
    im.shape == (96, 38)
    and silence.shape == (96, 18)
    and silence.max() == 0
    and col[row_t - 1 : row_t + 2].max() >= 254
    and np.delete(col, range(row_t - 3, row_t + 4)).max() < 40,
    str((im.shape, silence.shape, silence.max(), col[row_t - 2 : row_t + 3])),
)
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.91)]})
day, cid = s.clip_ids()[0]
from PIL import Image  # noqa: E402

pim = Image.open(os.path.join(s.root, day, cid + ".png"))
psz = os.path.getsize(os.path.join(s.root, day, cid + ".png"))
j = s.sidecar(day, cid)
check(
    "stored PNG: exactly 98 x 96 for the 5 s clip (columns = (samples - 4096) // 2400, rows 96), palette mode P, under 20 KB, and the sidecar describes it (columns 98, col_s 0.05, floor 3500 Hz)",
    (pim.size, pim.mode) == ((98, 96), "P")
    and psz < 20000
    and (j["samples"] - 4096) // 2400 == 98
    and j["spectrogram"] == {"rows": 96, "col_s": 0.05, "columns": 98, "f_lo_hz": 3500.0, "f_hi_hz": 24000.0, "scale": "log", "db_lo": -110.0, "db_hi": -30.0},
    str((pim.size, pim.mode, psz, j["spectrogram"])),
)
check(
    "sidecar: the 8 per-beam energies are those of the peak window (loud beams 0 and 1 are the two largest), azimuth null without a calibration file",
    len(j["signatures"]["beam_energies_db"]) == 8
    and sorted(range(8), key=lambda b: -j["signatures"]["beam_energies_db"][b])[:2] in ([0, 1], [1, 0])
    and j["signatures"]["beam_azimuth_deg"] is None,
    str(j["signatures"]["beam_energies_db"]),
)
cal = os.path.join(TMP, "cal.json")
az = [12.5, 57.5, 102.5, 147.5, 192.5, 237.5, 282.5, 327.5]
json.dump({"profile": {"beam_azimuth_deg": az}}, open(cal, "w"))
s = sim(calibration=cal)
s.play({5: [S.row("Poecile carolinensis", 0.91)]})
check(
    "sidecar: with a calibration file the beam azimuths equal the file's list (twin of the null above)",
    s.sidecar(*s.clip_ids()[0])["signatures"]["beam_azimuth_deg"] == az,
)
j = s.sidecar(*s.clip_ids()[0])
check(
    "sidecar: UTC timestamps and metadata exact (t_start_utc, model version, provenance, species, group, sample rate)",
    (j["t_start_utc"], j["t_end_utc"], j["model_version"], j["provenance"], j["species"]["scientific"], j["group"], j["sample_rate"], j["codec"])
    == (
        time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(S.T0 + 14)),
        time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(S.T0 + 19)),
        "birdnetlib-test",
        "model_opinion",
        "Poecile carolinensis",
        "bird",
        48000,
        "flac",
    ),
    str(j),
)
if hasattr(time, "tzset"):
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "EST5EDT"
    time.tzset()
    s = sim()
    s.play({5: [S.row("Poecile carolinensis", 0.91)]})
    j_ny = s.sidecar(*s.clip_ids()[0])
    os.environ["TZ"] = "UTC0"
    time.tzset()
    s = sim()
    s.play({5: [S.row("Poecile carolinensis", 0.91)]})
    j_utc = s.sidecar(*s.clip_ids()[0])
    if old_tz is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old_tz
    time.tzset()
    check(
        "sidecar: no local-time or timezone field; the sidecar written under TZ=EST5EDT equals the one under UTC (apart from audio-encoder dependent values), UTC stamps present",
        "local_utc_offset_s" not in j_ny
        and "local_utc_offset_s" not in j_utc
        and {k: v for k, v in j_ny.items() if k not in ("audio_bytes", "signatures", "envelope")}
        == {k: v for k, v in j_utc.items() if k not in ("audio_bytes", "signatures", "envelope")}
        and j_ny["t_start_utc"].endswith("Z"),
        str(sorted(j_ny)),
    )
else:
    skip("sidecar timezone independence", "no time.tzset on this platform")

# ====================================================================================================================
# 10b. --clip-pcen: PCEN-scale spectrogram (clips 1.2.0)
import argparse  # noqa: E402
import pcen as PCN  # noqa: E402


def _tone(db, f=6000.0, n=2 * FS, step=None):
    t = np.arange(n)
    amp = 10 ** (db / 20) * np.ones(n)
    if step:
        amp = np.where((t >= step[1]) & (t < step[2]), 10 ** (step[0] / 20), amp)
    return np.round(amp * np.sin(2 * np.pi * f * t / FS) * 32768).astype(np.int16)


_row6k = 95 - int(np.floor(np.log(6000.0 / 3500.0) / np.log(24000.0 / 3500.0) * 96))  # image row of the 6 kHz band (0 = highest frequency)
_lo, _hi = CR.row_bins()
_x = CF.restrict(_tone(-30.0))
_w = np.hanning(CR.NFFT)
_amp = 2.0 * np.abs(np.fft.rfft(_x[: (len(_x) // CR.NFFT) * CR.NFFT].reshape(-1, CR.NFFT) * _w, axis=1)) / np.sum(_w)
_pw = np.stack([(_amp[:, a:b] ** 2).max(axis=1) for a, b in zip(_lo, _hi)], axis=1)  # (38, 96) row-maximum linear power, column 0 = lowest frequency
_ref = PCN.render_u8(_pw, 0.05, vmax=4.0, scale=PCN.INT16_POWER, s=PCN.smoothing_from_time_constant(0.4, 20.0), init="first")
im_p, im_l = CR.spectrogram_index(_tone(-30.0), pcen=True), CR.spectrogram_index(_tone(-30.0))
check(
    "pcen render: byte-exact equal to analysis/pcen.py render_u8 on the row-maximum power (time constant 0.4 s, vmax 4, scale 32768**2, init first), shape (96, 38), uint8",
    im_p.shape == (96, 38) and im_p.dtype == np.uint8 and np.array_equal(im_p, _ref),
    str((im_p.shape, im_p.dtype, int((im_p != _ref).sum()))),
)
check(
    "pcen render: the log render of the same clip is a function of the same row maxima (same shape and dtype, 8 bit): the pcen image carries the same 96 x 38 x 8 bit grid",
    im_l.shape == im_p.shape
    and im_l.dtype == im_p.dtype == np.uint8
    and np.array_equal(im_l, np.clip((10 * np.log10(np.maximum(_pw, 1e-18)) - (-110.0)) / 80.0 * 255.0, 0, 255).round().astype(np.uint8).T[::-1]),
    "",
)
check(
    "pcen render: a steady -30 dBFS 6 kHz tone is 26 on its row in every column (the PCEN fixed point 0.408 / 4 * 255); log render of the same clip: 255",
    set(im_p[_row6k, :].tolist()) == {26} and set(im_l[_row6k, :].tolist()) == {255},
    str((im_p[_row6k, :6], im_l[_row6k, :6])),
)
check(
    "pcen render: a steady -70 dBFS tone is 22 (pcen) where the log render says 126: a 40 dB level difference is 4 pixel levels in the steady state",
    set(CR.spectrogram_index(_tone(-70.0), pcen=True)[_row6k, :].tolist()) == {22} and set(CR.spectrogram_index(_tone(-70.0))[_row6k, :].tolist()) == {126},
)
_st = _tone(-50.0, step=(-38.0, FS, FS + 24000))
_sp, _sl = CR.spectrogram_index(_st, pcen=True)[_row6k, :], CR.spectrogram_index(_st)[_row6k, :]
check(
    "quiet event over a steady chorus: +12 dB for 0.5 s on a -50 dBFS tone: pcen row is 24 before, 101 at the onset then 74 60 51 46 41 39 36 34 33 and 3 after it; the log row is 191 and 229..230 (a step of 39 vs 77)",
    _sp.tolist() == [24] * 19 + [101, 74, 60, 51, 46, 41, 39, 36, 34, 33] + [3, 3, 3, 4, 4, 5, 5, 6, 6]
    and _sl.tolist() == [191] * 19 + [229] + [230] * 9 + [193] + [191] * 8
    and int(_sp.max()) - int(_sp[0]) == 77,
    str((_sp.tolist(), _sl.tolist())),
)
_lo_only = np.round(0.5 * (np.sin(2 * np.pi * 1000.0 * np.arange(2 * FS) / FS) + np.sin(2 * np.pi * 3000.0 * np.arange(2 * FS) / FS)) * 32768).astype(np.int16)
check(
    "pcen render: a full-scale 1 kHz + 3 kHz clip (everything below the 3.5 kHz floor) reaches at most 19 (high-pass leakage, log render 17); the 6 kHz tone twin reaches 26",
    int(CR.spectrogram_index(_lo_only, pcen=True).max()) == 19 and int(CR.spectrogram_index(_lo_only).max()) == 17 and int(im_p.max()) == 26,
)
check(
    "pcen render: a silent clip is all 0 in both scales; a clip shorter than one frame is 96 x 1 zeros",
    not CR.spectrogram_index(np.zeros(FS, np.int16), pcen=True).any() and CR.spectrogram_index(np.zeros(3000, np.int16), pcen=True).shape == (96, 1),
)
_pp = CR.pcen_params()
check(
    "pcen parameters exact: time constant 0.4 s -> s 0.11743140267094843 at 20 columns/s, alpha 0.98, delta 2, r 0.5, eps 1e-6, power scale 32768**2, init first, vmax 4",
    _pp
    == {
        "time_constant_s": 0.4,
        "s": 0.11743140267094843,
        "alpha": 0.98,
        "delta": 2.0,
        "r": 0.5,
        "eps": 1e-06,
        "power_scale": 1073741824.0,
        "init": "first",
        "vmax": 4.0,
    },
    str(_pp),
)
_ap = argparse.ArgumentParser()
CL.add_args(_ap)
check(
    "--clip-pcen: default False, the flag sets it, ClipConfig carries it (default False)",
    _ap.parse_args([]).clip_pcen is False
    and _ap.parse_args(["--clip-pcen"]).clip_pcen is True
    and CL.ClipConfig().pcen is False
    and CL.ClipConfig(pcen=True).pcen is True
    and CL.config_from_args(_ap.parse_args(["--clip-pcen"]), None).pcen is True
    and CL.config_from_args(_ap.parse_args([]), None).pcen is False,
)
s = sim(pcen=True)
s.play({5: [S.row("Poecile carolinensis", 0.91)]})
day, cid = s.clip_ids()[0]
j = s.sidecar(day, cid)
_pcm, _ = sf.read(os.path.join(s.root, day, cid + ".flac"), dtype="int16")
_png = Image.open(os.path.join(s.root, day, cid + ".png"))
check(
    "--clip-pcen stored clip: sidecar spectrogram is exactly {rows 96, col_s 0.05, columns 98, floor 3500, top 24000, scale pcen, pcen parameters} with no db_lo/db_hi; the PNG is 98 x 96 mode P and its pixels equal the pcen render of the stored audio",
    j["spectrogram"] == {"rows": 96, "col_s": 0.05, "columns": 98, "f_lo_hz": 3500.0, "f_hi_hz": 24000.0, "scale": "pcen", "pcen": _pp}
    and (_png.size, _png.mode) == ((98, 96), "P")
    and np.array_equal(np.array(_png), CR.spectrogram_index(_pcm, pcen=True)),
    str(j["spectrogram"]),
)
check(
    "--clip-pcen: the PNG differs from the log render of the same audio (the flag changes the pixels) and has no more pixels than it (98 x 96)",
    not np.array_equal(np.array(_png), CR.spectrogram_index(_pcm)) and np.array(_png).shape == CR.spectrogram_index(_pcm).shape == (96, 98),
)
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.91)]})
day, cid = s.clip_ids()[0]
_pcm, _ = sf.read(os.path.join(s.root, day, cid + ".flac"), dtype="int16")
check(
    "default (no --clip-pcen): scale log with db_lo -110 / db_hi -30 and no pcen key, PNG equals the log render (existing clips unchanged)",
    s.sidecar(day, cid)["spectrogram"]["scale"] == "log"
    and "pcen" not in s.sidecar(day, cid)["spectrogram"]
    and s.sidecar(day, cid)["spectrogram"]["db_lo"] == -110.0
    and np.array_equal(np.array(Image.open(os.path.join(s.root, day, cid + ".png"))), CR.spectrogram_index(_pcm)),
)

# ====================================================================================================================
# 11. review round: human-sound tagger labels, fail-closed speech_p, names, caps, sweeps
HUMAN_ROWS = (
    ("Laughter", 0.95),
    ("Screaming", 0.40),
    ("Crying, sobbing", 0.30),
    ("Humming", 0.20),
    ("Whistling", 0.50),
    ("Crowd", 0.30),
    ("Cheering", 0.25),
    ("Cough", 0.35),
    ("Sneeze", 0.731),
    ("Throat clearing", 0.20),
    ("Sigh", 0.10),
    ("Clapping", 0.30),
    ("Children shouting", 0.30),
)
for lab, pr in HUMAN_ROWS:
    s = sim()
    s.play({5: [S.row("Poecile carolinensis", 0.91)]}, tags=base_tags((14, 0.0, False, [[lab, pr]]), skip=(14,)))
    check(
        f"tags `top` label {lab!r} at {pr} with speech_p 0.0 and speech_active false inside the guard span: nothing stored, counted as speech, no file at all",
        n_stored(s) == 0 and cnt(s, "clips_skipped_speech") == 1 and s.files() == [],
        str((s.svc.counters(), s.files())),
    )
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.91)]}, tags=base_tags((14, 0.0, False, [["Bird", 0.9], ["Insect", 0.3], ["Laughter", 0.04]]), skip=(14,)))
check("positive twin: the same window with only wildlife labels and Laughter BELOW 0.05 (0.04): stored", n_stored(s) == 1, str(s.svc.counters()))
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.91)]}, tags=base_tags((14, 0.0, False, [["laughter", 0.05]]), skip=(14,)))
check("a human label at exactly the 0.05 threshold (lower-case spelling) blocks the clip", n_stored(s) == 0 and cnt(s, "clips_skipped_speech") == 1)
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.91)]}, tags=base_tags((24.1, 0.0, False, [["Laughter", 0.9]])))
check("positive twin: Laughter in a tags window that starts 5.1 s after the clip end (outside the guard span): stored", n_stored(s) == 1, str(s.svc.counters()))
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.91)]}, tags=base_tags((14, 0.0, False, "Laughter 0.9"), skip=(14,)))
check(
    "a malformed `top` (a string instead of label/probability pairs) fails closed: nothing stored",
    n_stored(s) == 0 and cnt(s, "clips_skipped_speech") == 1 and cnt(s, "clip_service_errors") == 0,
    str(s.svc.counters()),
)
# speech_p missing or not a number counts as speech (fail closed)
for what, pv in (("speech_p None", None), ("speech_p missing", "missing")):
    s = sim()
    s.play({5: [S.row("Poecile carolinensis", 0.91)]}, tags=base_tags((14, pv, False), skip=(14,)))
    check(
        f"tags row with {what} and speech_active false inside the span: nothing stored, counted as speech",
        n_stored(s) == 0 and cnt(s, "clips_skipped_speech") == 1,
        str(s.svc.counters()),
    )
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.91)]}, tags=base_tags((14, 0.0, False), skip=(14,)))
check("positive twin: the same row with speech_p 0.0 is stored", n_stored(s) == 1)

# exclusion by COMMON name, human check by SCIENTIFIC name, new farmyard / pet names
s = sim()
s.play(
    {
        5: [
            S.row("Zzz aaa", 0.95, common="Rooster"),
            S.row("Zzz bbb", 0.95, common="Domestic Cat"),
            S.row("Zzz ccc", 0.95, common="Budgerigar"),
            S.row("Psittacula krameri", 0.95, common="Rose-ringed X"),
            S.row("Numida meleagris", 0.95, common="Y"),
            S.row("Cairina moschata", 0.95, common="Z"),
        ]
    }
)
check(
    "exclusion by common name (whole name 'Rooster', prefix 'Domestic Cat', whole name 'Budgerigar') and by scientific name (parrot genus prefix, guineafowl, Muscovy duck): none stored, 6 counted",
    n_stored(s) == 0 and cnt(s, "clips_skipped_excluded") == 6,
    str(s.svc.counters()),
)
s = sim()
s.play(
    {
        5: [
            S.row("Zzz aaa", 0.95, common="Eurasian Wren"),
            S.row("Columba palumbus", 0.95, common="Common Wood-Pigeon"),
            S.row("Zzz ccc", 0.95, common="Rooster Cogburn's Wren"),
        ]
    }
)
check(
    "positive twin: unrelated common names, a pigeon that is not Columba livia, and a name merely CONTAINING Rooster (not a prefix) are stored (3)",
    n_stored(s) == 3 and cnt(s, "clips_skipped_excluded") == 0,
    str(s.svc.counters()),
)
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.91, beam=1), S.row("Zzz vocalis", 0.30, beam=2, common="Human vocal", group="mechanical", non_species=True)]})
check(
    "human BirdNET label recognised from the COMMON name alone (scientific name differs): not stored",
    n_stored(s) == 0 and cnt(s, "clips_skipped_human_label") == 1,
    str(s.svc.counters()),
)
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.91, beam=1), S.row("Human vocal", 0.30, beam=2, common="Zzz vocalis", group="mechanical", non_species=True)]})
check(
    "human BirdNET label recognised from the SCIENTIFIC name alone (common name differs): not stored",
    n_stored(s) == 0 and cnt(s, "clips_skipped_human_label") == 1,
    str(s.svc.counters()),
)
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.91, beam=1), S.row("Calypte anna", 0.30, beam=2, common="Anna's Hummingbird", group="bird")]})
check(
    "positive twin: a low-confidence wildlife row on the other beam (neither name starts with Human): the clip is stored",
    n_stored(s) == 1,
    str(s.svc.counters()),
)

# --clip-cap-per-species-hour 0, --clip-max-s, --clip-sweep-s
s = sim(cap_per_species_hour=0)
s.play({5: [S.row("Poecile carolinensis", 0.95)]})
check(
    "--clip-cap-per-species-hour 0: nothing stored, counted as clips_dropped_cap 1, no service error, no audio file on disk",
    n_stored(s) == 0 and cnt(s, "clips_dropped_cap") == 1 and cnt(s, "clip_service_errors") == 0 and cnt(s, "clips_stored") == 0 and s.audio_files() == [],
    str(s.svc.counters()),
)
s = sim(cap_per_species_hour=1)
s.play({5: [S.row("Poecile carolinensis", 0.95)]})
check("positive twin: cap 1 stores the same clip", n_stored(s) == 1 and cnt(s, "clips_dropped_cap") == 0)
check(
    "--clip-max-s is clamped to the 12 s ring: 30 -> 12.0 (twin: 8 stays 8.0, 12 stays 12.0)",
    (CL.ClipConfig(max_s=30).max_s, CL.ClipConfig(max_s=8).max_s, CL.ClipConfig(max_s=12).max_s) == (12.0, 8.0, 12.0),
)
s = sim(max_s=30)
birds = {k: [S.row("Neoconocephalus triops", 0.95 if k == 16 else 0.87, group="insect")] for k in range(2, 32)}
s.play(birds, k_end=40, tags_cover=(-10, 130))
day, cid = s.clip_ids()[0]
a, _ = sf.read(os.path.join(s.root, day, cid + ".flac"), dtype="int16")
check(
    "--clip-max-s 30 on a 90 s event still yields ONE clip of exactly 12 s (twin of the 12 s check above, through the real clamp)",
    len(s.clip_ids()) == 1 and len(a) == 12 * FS,
    str(len(a)),
)
import argparse  # noqa: E402

ap = argparse.ArgumentParser()
CL.add_args(ap)
cfg = CL.config_from_args(ap.parse_args(["--clip-max-s", "30", "--clip-sweep-s", "0"]), None)
cfg2 = CL.config_from_args(ap.parse_args(["--clip-max-s", "9", "--clip-sweep-s", "-5"]), None)
check(
    "CLI: --clip-max-s 30 -> 12.0 and --clip-sweep-s 0 -> 1.0; --clip-max-s 9 -> 9.0 and --clip-sweep-s -5 -> 1.0",
    (cfg.max_s, cfg.sweep_s, cfg2.max_s, cfg2.sweep_s) == (12.0, 1.0, 9.0, 1.0),
    str((cfg.max_s, cfg.sweep_s)),
)
check(
    "--clip-sweep-s 1 stays 1.0 and 600 stays 600.0 (twin of the clamp)", (CL.ClipConfig(sweep_s=1).sweep_s, CL.ClipConfig(sweep_s=600).sweep_s) == (1.0, 600.0)
)
waits0, gate0 = [], threading.Semaphore(0)


def wait0(sec):
    waits0.append(sec)
    gate0.acquire(timeout=5)


s = sim(start_threads=True, wait=wait0, sweep_s=0)
s.svc.start()
time.sleep(0.3)
check(
    "the sweeper thread of a --clip-sweep-s 0 service waits 1.0 s between sweeps, not 0 (it does not spin): one sweep at start, then one wait",
    waits0[:1] == [1.0] and cnt(s, "sweep_runs") == 1,
    str((waits0[:3], cnt(s, "sweep_runs"))),
)
gate0.release()
s.svc.stop()

# sidecar retention boundary (exact), sidecar size cap
s, day, cid, tc = fresh()
tstart = s.sidecar(day, cid)["t_start"]
s.svc.store.sweep(tstart + 14 * DAYS - 0.001)
kept = os.path.exists(os.path.join(s.root, day, cid + ".json"))
s.svc.store.sweep(tstart + 14 * DAYS)
check(
    "sidecar retention boundary: kept 1 ms before 14 d, deleted at exactly 14 d 00 h 00 m 00 s (>=)",
    kept and not os.path.exists(os.path.join(s.root, day, cid + ".json")) and not os.path.exists(os.path.join(s.root, day, cid + ".png")),
)
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.90)], 9: [S.row("Strix varia", 0.95)], 13: [S.row("Bubo virginianus", 0.88)]}, k_end=20)
ents = sorted(((c, dict(e)) for c, e in s.svc.store.table.items()), key=lambda kv: kv[1]["t"])
tot_side = sum(e["side_bytes"] for _, e in ents)
s.svc.store.sweep(s.now)
check(
    "sidecar size cap not exceeded (default 256 MB): nothing evicted, three clips with audio (twin)",
    cnt(s, "clips_evicted_sidecar_size") == 0 and len(s.clip_ids()) == 3 and len([f for f in s.files() if f.endswith(".flac")]) == 3,
)
s.cfg.sidecar_cap = tot_side - 1
n0 = len(s.index())
s.svc.store.sweep(s.now)
oldest = ents[0][0]
idx_new = s.index()[n0:]
check(
    "sidecar size cap exceeded by 1 byte: exactly the OLDEST clip loses png, json and audio, counted once, clip_sidecar_expired(size_cap) appended; the other two stay",
    cnt(s, "clips_evicted_sidecar_size") == 1
    and [c for _, c in s.clip_ids()] == sorted(c for c, _ in ents[1:])
    and not [f for f in s.files() if oldest in f]
    and [(r["k"], r["id"], r["reason"]) for r in idx_new] == [("clip_sidecar_expired", oldest, "size_cap")]
    and len([f for f in s.files() if f.endswith(".flac")]) == 2,
    str((s.svc.counters()["clips_evicted_sidecar_size"], s.clip_ids(), idx_new)),
)
check("sidecar size cap: the remaining sidecar bytes are at or under the cap", sum(e["side_bytes"] for e in s.svc.store.table.values()) <= tot_side - 1)

# .wav fallback codec: the sweep and the expiry treat .wav exactly like .flac
s = sim()
s.svc.store.codec = "wav"
s.play({5: [S.row("Poecile carolinensis", 0.91)]})
day, cid = s.clip_ids()[0]
tc = CL.id_time(cid)
wp = os.path.join(s.root, day, cid + ".wav")
a, rate = sf.read(wp, dtype="int16")
wrote = os.path.exists(wp) and rate == FS and bool((a == S.sig(0, S.BASE + 14 * FS, 5 * FS, (0, 1))).all())
s.svc.store.sweep(tc + 24 * 3600 - 60)
kept = os.path.exists(wp)
s.svc.store.sweep(tc + 24 * 3600)
check(
    ".wav codec (no FLAC encoder): the clip is a sample-exact WAV, kept at 23h59m, deleted at 24h00m00s by the sweep, audio_expired 1, tombstone written",
    wrote
    and kept
    and not os.path.exists(wp)
    and cnt(s, "audio_expired") == 1
    and s.sidecar(day, cid)["audio_expired"] is True
    and s.sidecar(day, cid)["codec"] == "wav",
    str(s.files()),
)
s = sim()
s.play({5: [S.row("Poecile carolinensis", 0.91)]})
day, cid = s.clip_ids()[0]
now = CL.id_time(cid) + 3600
orphan_wav = os.path.join(s.root, day, f"{int((now - 900) * 1000):013d}-q-b1.wav")
young_wav = os.path.join(s.root, day, f"{int((now - 60) * 1000):013d}-r-b1.wav")
for pth, age in ((orphan_wav, 900), (young_wav, 60)):
    open(pth, "wb").write(b"x" * 10)
    os.utime(pth, (now - age, now - age))
s.svc.store.sweep(now)
check(
    "a .wav with no sidecar: deleted at 15 min (orphans_deleted 1), the 1 minute old one is kept (twin)",
    not os.path.exists(orphan_wav) and os.path.exists(young_wav) and cnt(s, "orphans_deleted") == 1,
)

# the OS says the file is already gone: that is success, not a retry
s, day, cid, tc = fresh()
calls = []


def vanish_remove(p):
    calls.append(p)
    os.remove(p)
    raise FileNotFoundError(2, "already gone", p)


s.svc.store.remove = vanish_remove
s.svc.store.sweep(tc + 25 * 3600)
check(
    "remove() reports FileNotFoundError (somebody else deleted it first): treated as deleted, audio_expired 1, audio_expire_retry 0, sidecar marked expired",
    cnt(s, "audio_expired") == 1
    and cnt(s, "audio_expire_retry") == 0
    and s.sidecar(day, cid)["audio_expired"] is True
    and not os.path.exists(apath(s, day, cid))
    and calls == [apath(s, day, cid)],
    str((s.svc.counters(), calls)),
)

# orphan PNG / JSON (crash between the PNG replace and the JSON write)
s, day, cid, tc = fresh()
now = tc + 40 * DAYS
dd = os.path.join(s.root, day)
orph_png = os.path.join(dd, "1767225600000-x-b1.png")
orph_json = os.path.join(dd, "1767225600000-x-b1.json")
young_png = os.path.join(dd, f"{int((now - 60) * 1000):013d}-y-b1.png")
for pth, age in ((orph_png, 1000), (orph_json, 1000), (young_png, 60)):
    open(pth, "wb").write(b"x" * 10 if pth.endswith(".png") else b"not json")
    os.utime(pth, (now - age, now - age))
s.svc.store.sweep(now)
check(
    "orphan PNG and unreadable JSON with no table entry, 1000 s old: both deleted (orphans_deleted 2); the real clip's sidecars were expired by age separately",
    not os.path.exists(orph_png) and not os.path.exists(orph_json) and cnt(s, "orphans_deleted") == 2,
    str((s.files(), s.svc.counters()["orphans_deleted"])),
)
check("orphan PNG 60 s old (a write may be in progress) is kept (twin)", os.path.exists(young_png))
s, day, cid, tc = fresh()
s.svc.store.sweep(tc + 3600)
check(
    "a stored clip's own png and json are not orphans (twin): still there after a sweep one hour later, orphans_deleted 0",
    os.path.exists(os.path.join(s.root, day, cid + ".png")) and os.path.exists(os.path.join(s.root, day, cid + ".json")) and cnt(s, "orphans_deleted") == 0,
)

# the analyzer thread never waits for the store lock (status block) and a sweep does not re-read every sidecar
s, day, cid, tc = fresh()
held, free = threading.Event(), threading.Event()


def hold_lock():
    with s.svc.store.lock:
        held.set()
        free.wait(10)


th = threading.Thread(target=hold_lock, daemon=True)
th.start()
held.wait(5)
got = []
t_status = threading.Thread(target=lambda: got.append(s.svc.status_block()), daemon=True)
t_status.start()
t_status.join(2.0)
returned = not t_status.is_alive()
free.set()
th.join(5)
t_status.join(5)
check(
    "status_block() (called by the analyzer's main loop) returns while another thread holds the store lock, with audio_files 1 and the real audio byte count",
    returned and got and got[0]["audio_files"] == 1 and got[0]["audio_bytes"] == os.path.getsize(apath(s, day, cid)),
    str((returned, got)),
)
open(os.path.join(s.root, day, cid + ".json"), "w").write("not json")
s.svc.store.sweep(tc + 3600)
check(
    "a sweep keeps its table incrementally: it does not re-read sidecars (a clobbered JSON does not drop the clip from the audio stats, no sweep error)",
    s.svc.store.audio_stats()[0] == 1 and os.path.exists(apath(s, day, cid)) and cnt(s, "sweep_errors") == 0,
)
p2 = os.path.join(TMP, "copy_reread", "clips")
shutil.copytree(s.root, p2)
s2 = S.Sim(p2)
check(
    "twin: a NEW store on the same directory re-reads the sidecars once at construction (the clobbered one is not in the table)", len(s2.svc.store.table) == 0
)

try:
    import jsonschema

    schema = json.load(open(os.path.join(HERE, "..", "contracts", "schemas", "clips.schema.json"), encoding="utf-8"))
    val = jsonschema.Draft202012Validator(schema)
    s = sim()
    s.play({5: [S.row("Poecile carolinensis", 0.91)]})
    day, cid = s.clip_ids()[0]
    errs = list(val.iter_errors(s.sidecar(day, cid))) + [e for r in s.index() for e in val.iter_errors(r)]
    check(
        "the stored sidecar JSON and the index record validate against contracts/schemas/clips.schema.json",
        errs == [] and len(s.index()) == 1,
        str([e.message for e in errs][:2]),
    )
    bad = dict(s.sidecar(day, cid), provenance="measured")
    vclip = jsonschema.Draft202012Validator({"$ref": "#/$defs/clip", "$defs": schema["$defs"]})
    check(
        "negative twin: a sidecar with provenance 'measured' is rejected by the schema at $.provenance (the stored one is accepted by the same validator)",
        any(list(e.path) == ["provenance"] for e in vclip.iter_errors(bad)) and list(vclip.iter_errors(s.sidecar(day, cid))) == [],
    )
except ImportError:
    skip("schema validation of sidecar and index", "jsonschema not installed in this venv (the Windows contracts venv runs it)")

# fixtures are what the real writer produces
sys.path.insert(0, os.path.join(HERE, "..", "contracts", "tools"))
import make_clips_fixtures as MCF  # noqa: E402

regen = MCF.build(os.path.join(TMP, "regen"))
FIX = os.path.join(HERE, "..", "contracts", "fixtures")


def jl(p):
    return [json.loads(l) for l in open(p, "rb").read().split(b"\n") if l]


def strip(r):
    return {k: v for k, v in r.items() if k not in ("audio_bytes", "signatures", "envelope")}


a_, b_ = jl(os.path.join(regen, "clips_index.jsonl")), jl(os.path.join(FIX, "clips_index.jsonl"))
check(
    "fixtures: clips_index.jsonl equals what the real writer produces now (same records, audio_bytes aside) and covers all four record kinds",
    [strip(r) for r in a_] == [strip(r) for r in b_] and {r["k"] for r in b_} == {"clip_stored", "clip_audio_expired", "clip_evicted", "clip_sidecar_expired"},
    str((len(a_), len(b_))),
)
a_v, b_v = jl(os.path.join(regen, "clips_index_visual.jsonl")), jl(os.path.join(FIX, "clips_index_visual.jsonl"))
check(
    "fixtures: clips_index_visual.jsonl (the default, visual-only mode) equals what the real writer produces now and its one record says audio_stored false, audio_file null, codec null, audio_bytes 0",
    [strip(r) for r in a_v] == [strip(r) for r in b_v]
    and len(b_v) == 1
    and (b_v[0]["audio_stored"], b_v[0]["audio_file"], b_v[0]["codec"], b_v[0]["audio_bytes"]) == (False, None, None, 0),
    str(b_v),
)
a_l, b_l = jl(os.path.join(regen, "clips_index_lowconf.jsonl")), jl(os.path.join(FIX, "clips_index_lowconf.jsonl"))
check(
    "fixtures: clips_index_lowconf.jsonl (--clip-min-conf 0.3) equals what the real writer produces now and holds one clip_stored at exactly 0.31",
    [strip(r) for r in a_l] == [strip(r) for r in b_l] and len(b_l) == 1 and b_l[0]["confidence_max"] == 0.31,
    str(b_l),
)
for fn in ("clip_sidecar.json", "clip_sidecar_audio_expired.json", "clip_sidecar_visual.json", "analysis_status_clips_visual.json"):
    ra, rb = json.load(open(os.path.join(regen, fn))), json.load(open(os.path.join(FIX, fn)))
    check(
        f"fixtures: {fn} equals what the real writer produces now (signatures and envelope aside)",
        strip(ra) == strip(rb),
        str([k for k in ra if strip(ra).get(k) != strip(rb).get(k)]),
    )

# ====================================================================================================================
# 12. beam agreement (--clip-min-beams, owner decision): at least N DISTINCT beams must report the species within the event
P = "Poecile carolinensis"
_ap = __import__("argparse").ArgumentParser()
CL.add_args(_ap)
check(
    "--clip-min-beams defaults to 2 in ClipConfig, in the argparse flag, and in a Sim built without the option",
    CL.ClipConfig().min_beams == 2
    and _ap.parse_args([]).clip_min_beams == 2
    and CL.config_from_args(_ap.parse_args([]), None).min_beams == 2
    and S.Sim(os.path.join(TMP, "dflt", "clips")).cfg.min_beams == 2,
)
check(
    "--clip-min-beams 1 is parsed to 1 and reaches the config (twin of the default)",
    CL.config_from_args(_ap.parse_args(["--clip-min-beams", "1"]), None).min_beams == 1,
)
dflt = lambda: S.Sim(os.path.join(TMP, f"d{_k.__setitem__(0, _k[0] + 1) or _k[0]}", "clips"))  # the REAL default config
s = dflt()
s.play({5: [S.row(P, 0.90, beam=1)]})
check(
    "default: a 1-beam 0.90 event is NOT stored (counted clips_skipped_single_beam 1, nothing else counted, no audio file)",
    n_stored(s) == 0 and cnt(s, "clips_skipped_single_beam") == 1 and cnt(s, "clips_skipped_low_conf") == 0 and s.audio_files() == [] and s.index() == [],
    str(s.svc.counters()),
)
s = sim(min_beams=1)
s.play({5: [S.row(P, 0.90, beam=1)]})
j = s.sidecar(*s.clip_ids()[0])
check(
    "--clip-min-beams 1: the same 1-beam 0.90 event IS stored; beams_agreeing exactly 1 in the sidecar and in the index record, beams_reported [1]",
    n_stored(s) == 1
    and cnt(s, "clips_skipped_single_beam") == 0
    and j["beams_agreeing"] == 1
    and j["beams_reported"] == [1]
    and [r["beams_agreeing"] for r in s.index()] == [1],
    str((j.get("beams_agreeing"), s.index())),
)
s = dflt()
s.play({5: [S.row(P, 0.90, beam=1), S.row(P, 0.88, beam=2)]})
j = s.sidecar(*s.clip_ids()[0])
check(
    "default: a 2-beam 0.90 event IS stored; beams_agreeing exactly 2 (sidecar and index), beams_reported [1, 2], beam 1 (the best report) is the stored beam",
    n_stored(s) == 1
    and j["beams_agreeing"] == 2
    and j["beams_reported"] == [1, 2]
    and j["beam"] == 1
    and j["confidence_max"] == 0.9
    and [r["beams_agreeing"] for r in s.index()] == [2]
    and cnt(s, "clips_skipped_single_beam") == 0,
    str((j.get("beams_agreeing"), j.get("beams_reported"), s.index())),
)
s = dflt()
s.play({5: [S.row(P, 0.80, beam=1)]})
check(
    "default: a 1-beam 0.80 event is counted as low confidence only (the certainty rule is judged first), not as single beam",
    n_stored(s) == 0 and cnt(s, "clips_skipped_low_conf") == 1 and cnt(s, "clips_skipped_single_beam") == 0,
    str(s.svc.counters()),
)
s = dflt()
s.play({5: [S.row(P, 0.85, beam=1), S.row(P, 0.80, beam=2)]})
check(
    "default: a 2-beam event whose max is exactly 0.85 is NOT stored (not strictly above); it is counted as low confidence, not as single beam",
    n_stored(s) == 0 and cnt(s, "clips_skipped_low_conf") == 1 and cnt(s, "clips_skipped_single_beam") == 0,
    str(s.svc.counters()),
)
s = dflt()
s.play({5: [S.row(P, 0.8501, beam=1), S.row(P, 0.80, beam=2)]})
check("twin: the same 2-beam event at 0.8501 IS stored", n_stored(s) == 1 and s.sidecar(*s.clip_ids()[0])["beams_agreeing"] == 2)
s = dflt()
s.play({5: [S.row(P, 0.90, beam=1)], 6: [S.row(P, 0.70, beam=2)]})
j = s.sidecar(*s.clip_ids()[0]) if n_stored(s) else {}
check(
    "the two beams may report in DIFFERENT windows of one event (beam 1 at 15 s, beam 2 at 18 s): stored with beams_agreeing 2",
    n_stored(s) == 1 and j.get("beams_agreeing") == 2 and j.get("beams_reported") == [1, 2],
    str(j.get("beams_reported")),
)
s = dflt()
s.play({5: [S.row(P, 0.90, beam=1)], 6: [S.row(P, 0.70, beam=1)], 7: [S.row(P, 0.70, beam=1)]})
check(
    "three reports of the SAME beam are one distinct beam: not stored (the rule counts beams, not reports)",
    n_stored(s) == 0 and cnt(s, "clips_skipped_single_beam") == 1,
    str(s.svc.counters()),
)
s = dflt()
s.play({5: [S.row(P, 0.90, beam=1)], 10: [S.row(P, 0.90, beam=2)]}, k_end=16)
check(
    "a second beam that reports in a LATER event (12 s gap) does not rescue the first: two events, each with one beam, nothing stored, counted twice",
    n_stored(s) == 0 and cnt(s, "clips_skipped_single_beam") == 2,
    str(s.svc.counters()),
)
s = sim(min_beams=3)
s.play({5: [S.row(P, 0.90, beam=1), S.row(P, 0.88, beam=2)]})
check(
    "--clip-min-beams 3: a 2-beam event is NOT stored (off-by-one guard)", n_stored(s) == 0 and cnt(s, "clips_skipped_single_beam") == 1, str(s.svc.counters())
)
s = sim(min_beams=3)
s.play({5: [S.row(P, 0.90, beam=1), S.row(P, 0.88, beam=2), S.row(P, 0.87, beam=7)]})
j = s.sidecar(*s.clip_ids()[0]) if n_stored(s) else {}
check(
    "twin: --clip-min-beams 3 with three beams IS stored (beams_agreeing 3, beams_reported [1, 2, 7])",
    n_stored(s) == 1 and j.get("beams_agreeing") == 3 and j.get("beams_reported") == [1, 2, 7],
    str(j.get("beams_reported")),
)
s = dflt()
s.play({5: [S.row(P, 0.90, beam=1), S.row("Cyanocitta cristata", 0.88, beam=2)]})
check(
    "two beams each reporting a DIFFERENT species do not agree: two events with one beam each, nothing stored, clips_skipped_single_beam exactly 2",
    n_stored(s) == 0 and cnt(s, "clips_skipped_single_beam") == 2,
    str(s.svc.counters()),
)
s = dflt()
s.play({5: [S.row(P, 0.90, beam=1), S.row(P, 0.88, beam=2)]}, tags=base_tags((23.9, 0.5)))
check(
    "the beam rule does not bypass the speech guard: a 2-beam 0.90 event with speech 4.9 s after its end is not stored (counted skipped_speech)",
    n_stored(s) == 0 and cnt(s, "clips_skipped_speech") == 1 and cnt(s, "clips_skipped_single_beam") == 0,
    str(s.svc.counters()),
)
s = dflt()
s.play({5: [S.row(P, 0.90, beam=1), S.row(P, 0.88, beam=2)]}, tags=base_tags((24.1, 0.5)))
check("twin: the same 2-beam event with the speech window outside the guard span is stored", n_stored(s) == 1, str(s.svc.counters()))


# ====================================================================================================================
# 13. AUDIO OFF BY DEFAULT, visual-only, not invertible to speech (owner 2026-10-26: "I cannot keep voice recordings")
def walk_files(root):
    return sorted(os.path.relpath(os.path.join(dp, f), root).replace(os.sep, "/") for dp, _, fs_ in os.walk(root) for f in fs_)


def row_freq(img_row):
    """Centre frequency (Hz) of an image row (row 0 = the highest frequency), from the documented log spacing 3500 .. 24000 Hz in 96 rows."""
    r = 95 - img_row
    return 3500.0 * (24000.0 / 3500.0) ** ((r + 0.5) / 96)


_ap2 = __import__("argparse").ArgumentParser()
CL.add_args(_ap2)
check(
    "--clip-audio: the default is OFF in ClipConfig, in the argparse flag and in config_from_args; giving the flag turns it on (twin)",
    CL.ClipConfig().audio is False
    and _ap2.parse_args([]).clip_audio is False
    and CL.config_from_args(_ap2.parse_args([]), None).audio is False
    and CL.ClipConfig(audio=True).audio is True
    and CL.config_from_args(_ap2.parse_args(["--clip-audio"]), None).audio is True,
)
vis = lambda **kw: S.Sim(os.path.join(TMP, f"v{_k.__setitem__(0, _k[0] + 1) or _k[0]}", "clips"), **kw)  # the DEFAULT config: audio off
s = vis(min_beams=1)
s.play({5: [S.row(P, 0.95, beam=1)]})
day, cid = s.clip_ids()[0]
j = s.sidecar(day, cid)
check(
    "default (audio off): a qualifying 0.95 event stores exactly <id>.png, <id>.json and index.jsonl; walking the clips dir finds NO other file (no flac, wav, pcm, tmp)",
    walk_files(s.root) == sorted([f"{day}/{cid}.json", f"{day}/{cid}.png", "index.jsonl"]) and s.audio_files() == [],
    str(walk_files(s.root)),
)
check(
    "default: the sidecar and the index record say audio_stored false, audio_file null, codec null, audio_bytes 0, audio_expired false (clean semantics: the audio never existed)",
    (j["audio_stored"], j["audio_file"], j["codec"], j["audio_bytes"], j["audio_expired"]) == (False, None, None, 0, False)
    and [(r["audio_stored"], r["audio_file"], r["codec"], r["audio_bytes"]) for r in s.index()] == [(False, None, None, 0)],
    str((j.get("audio_file"), s.index())),
)
check(
    "default: audio_written() is false, the store has no audio encoder (sf None, codec None), status block says audio false and audio_files 0 / audio_bytes 0, clips_stored 1",
    s.svc.audio_written() is False
    and s.svc.store.sf is None
    and s.svc.store.codec is None
    and s.svc.status_block()["audio"] is False
    and (s.svc.status_block()["audio_files"], s.svc.status_block()["audio_bytes"], s.svc.status_block()["stored"]) == (0, 0, 1),
    str(s.svc.status_block()),
)
sa = vis(min_beams=1, audio=True)
sa.play({5: [S.row(P, 0.95, beam=1)]})
da, ca = sa.clip_ids()[0]
check(
    "twin: with --clip-audio the same event writes exactly one audio file next to png, json and index (4 files), audio_written true, audio_stored true",
    len(sa.audio_files()) == 1
    and sa.audio_files()[0].endswith((".flac", ".wav"))
    and len(walk_files(sa.root)) == 4
    and sa.svc.audio_written() is True
    and sa.sidecar(da, ca)["audio_stored"] is True,
    str(walk_files(sa.root)),
)
# the clock far past the audio retention: nothing to expire, nothing reconciled, sidecars keep their 14 days
tc = j["t_start"]
s.now = tc + 25 * 3600
s.svc.store.sweep(s.now)
check(
    "visual-only 25 h later: the sweep expires no audio and writes no audio tombstone (audio_expired 0, no clip_audio_expired record), png and json are untouched (audio_expired still false)",
    cnt(s, "audio_expired") == 0
    and [r["k"] for r in s.index()] == ["clip_stored"]
    and os.path.exists(os.path.join(s.root, day, cid + ".png"))
    and s.sidecar(day, cid)["audio_expired"] is False,
    str((s.svc.counters()["audio_expired"], s.index())),
)
p3 = os.path.join(TMP, "copy_visual", "clips")
shutil.copytree(s.root, p3)
s3 = S.Sim(p3)
s3.svc.store.sweep(tc + 25 * 3600)
check(
    "visual-only after a restart (a new store re-reads the sidecar): the sweep still finds nothing to expire or reconcile (audio_expired 0, the index unchanged)",
    len(s3.svc.store.table) == 1 and cnt(s3, "audio_expired") == 0 and [r["k"] for r in s3.index()] == ["clip_stored"],
    str((s3.svc.counters()["audio_expired"], s3.index())),
)
s.now = tc + 14 * 86400
s.svc.store.sweep(s.now)
check(
    "visual-only at 14 d: png and json are deleted with a clip_sidecar_expired(age) tombstone, exactly as before",
    not os.path.exists(os.path.join(s.root, day, cid + ".png"))
    and [r["k"] for r in s.index()] == ["clip_stored", "clip_sidecar_expired"]
    and s.index()[1]["reason"] == "age",
    str(s.index()),
)
s = vis(min_beams=1)
s.play({5: [S.row(P, 0.95, beam=1)]}, tags=base_tags((23.9, 0.5)))
check(
    "the speech guard still gates visual-only: speech 4.9 s after the event stores NO png and NO json (nothing at all), counted skipped_speech 1",
    n_stored(s) == 0 and cnt(s, "clips_skipped_speech") == 1 and s.files() == [],
    str((s.svc.counters()["clips_skipped_speech"], s.files())),
)
s = vis(min_beams=1)
s.play({5: [S.row(P, 0.95, beam=1)]}, tags=base_tags((24.1, 0.5)))
check("twin: the same speech window outside the guard span stores the visual products", n_stored(s) == 1 and s.audio_files() == [], str(s.files()))
s = vis(min_beams=1, tagger=False)
s.play({5: [S.row(P, 0.95, beam=1)]})
check(
    "the tagger being off still stores NOTHING in visual-only mode (fail closed): no png, no json",
    n_stored(s) == 0 and cnt(s, "clips_skipped_tagger_unavailable") == 1 and s.files() == [],
)

# ---- the exact format of the visual products ------------------------------------------------------------------------
lo_b, hi_b = CR.row_bins()
fb = np.fft.rfftfreq(CR.NFFT, 1.0 / FS)
check(
    "format constants: 96 rows, 3500 Hz floor, 24 kHz top, 50 ms columns of 2400 samples (hop = 2400: non-overlapping), 8-bit; the lowest FFT bin of the lowest row is exactly 3500 Hz and no row starts below it",
    (CR.H, CR.F_LO, CR.F_HI, CR.COL_S, CR.NFFT) == (96, 3500.0, 24000.0, 0.05, 2400)
    and fb[lo_b[0]] == 3500.0
    and float(fb[lo_b].min()) >= 3500.0
    and float(fb[hi_b[-1] - 1]) <= 24000.0
    and CF.FLOOR_HZ == 3500.0
    and CF.FRAME_S == 0.05
    and CF.DOM_FRAME_S == 0.1,
    str((CR.H, CR.F_LO, fb[lo_b[0]])),
)
t5 = np.arange(5 * FS) / FS
rng2 = np.random.default_rng(7)
# a speech-like voiced signal: vibrato pitch 80-160 Hz, harmonics only up to 3.36 kHz, 4 Hz syllabic amplitude modulation
f0 = 120 + 40 * np.sin(2 * np.pi * 0.7 * t5)
phs = 2 * np.pi * np.cumsum(f0) / FS
syl = 0.5 * (1 + np.sin(2 * np.pi * 4 * t5))
voiced = sum(np.sin(k * phs) / k**0.7 for k in range(1, 22))
voiced = voiced / np.abs(voiced).max() * 0.3 * syl
pcm_v = np.round(voiced * 32768).astype(np.int16)
im_v = CR.spectrogram_index(pcm_v)
sg_v = CF.signatures(pcm_v)
env_v = CF.envelope(pcm_v)
check(
    "speech-like harmonics (80-160 Hz pitch to 3.36 kHz, 4 Hz syllables, -10 dBFS): the image is (96, 98) and EVERY pixel is 0 (the floor), the envelope is at its -100 dB floor in all 98 frames",
    im_v.shape == (96, 98) and int(im_v.max()) == 0 and env_v["rms_db"] == [-100.0] * 98,
    str((im_v.shape, int(im_v.max()), max(env_v["rms_db"]))),
)
check(
    "speech-like harmonics: the signatures hold nothing of it: peak under -85 dBFS (the int16 rounding-noise floor is -91), rms under -95, all three band levels under -100 dB (the same signal would read -10 dBFS)",
    sg_v["peak_dbfs"] < -85 and sg_v["rms_dbfs"] < -95 and all(v < -100 for v in sg_v["band_db"]) and sg_v["onsets_s"] == [],
    str((sg_v["peak_dbfs"], sg_v["rms_dbfs"], sg_v["band_db"], sg_v["onsets_s"])),
)
# positive twin: a 6 kHz chirp train (5.5 -> 7 kHz in 0.3 s, every 0.6 s, -20 dBFS) is clearly visible
chirp = np.zeros_like(t5)
for s0 in np.arange(0.2, 4.6, 0.6):
    mk = (t5 >= s0) & (t5 < s0 + 0.3)
    tt_ = t5[mk] - s0
    chirp[mk] = 0.1 * np.sin(2 * np.pi * (5500.0 * tt_ + 0.5 * (1500.0 / 0.3) * tt_**2))
pcm_c = np.round(chirp * 32768).astype(np.int16)
im_c = CR.spectrogram_index(pcm_c)
sg_c = CF.signatures(pcm_c)
lit = np.argwhere(im_c > 128)
check(
    "positive twin, 6 kHz chirp train: the image is (96, 98) with maximum 255, at least 150 lit pixels (> 128), all of them between 4.8 and 7.6 kHz (the chirp is 5.5-7.0 kHz; Hann skirts), and every pixel above 240 between 5.3 and 7.2 kHz",
    im_c.shape == (96, 98)
    and int(im_c.max()) == 255
    and len(lit) >= 150
    and all(4800.0 <= row_freq(r) <= 7600.0 for r, _ in lit)
    and all(5300.0 <= row_freq(r) <= 7200.0 for r, _ in np.argwhere(im_c > 240)),
    str((im_c.shape, int(im_c.max()), len(lit), sorted({round(row_freq(r)) for r, _ in lit})[:3])),
)
check(
    "positive twin: the chirp train's signature shows 8 onsets (0.2 + 0.6 k s, +-0.03), a peak near -20 dBFS (+-1) and its energy in the 4-8 kHz band (about -29 dB, the others under -60)",
    len(sg_c["onsets_s"]) == 8
    and all(abs(o - (0.2 + 0.6 * k)) < 0.03 for k, o in enumerate(sg_c["onsets_s"]))
    and abs(sg_c["peak_dbfs"] + 20.0) < 1.0
    and sg_c["band_db"][0] > -35
    and all(v < -60 for v in sg_c["band_db"][1:]),
    str((sg_c["onsets_s"], sg_c["peak_dbfs"], sg_c["band_db"])),
)
tone_lo = np.round(0.3 * np.sin(2 * np.pi * 3400.0 * t5) * 32768).astype(np.int16)
tone_hi = np.round(0.3 * np.sin(2 * np.pi * 3700.0 * t5) * 32768).astype(np.int16)
check(
    "the floor is sharp: a -10 dBFS tone at 3.4 kHz leaves an image at the int16 rounding-noise level only (max under 16, i.e. under -105 dB); the same tone at 3.7 kHz is lit (max 255) on the lowest rows (twin)",
    int(CR.spectrogram_index(tone_lo).max()) < 16
    and int(CR.spectrogram_index(tone_hi).max()) == 255
    and row_freq(int(np.argwhere(CR.spectrogram_index(tone_hi)[:, 50] == 255)[0][0])) < 4000.0,
    "",
)
dom_lo = CF.signatures(tone_lo)
check(
    "the floor is sharp in the signatures too: the 3.4 kHz tone reads under -90 dBFS peak (rounding noise only, 80 dB below the tone), the 3.7 kHz tone reads -10.0 dBFS (+-0.6: it is inside the filter transition) (twin)",
    dom_lo["peak_dbfs"] < -90 and abs(CF.signatures(tone_hi)["peak_dbfs"] + 10.0) < 0.6,
    str((dom_lo["peak_dbfs"], CF.signatures(tone_hi)["peak_dbfs"])),
)
# a hard clip edge must not click through the high-pass (the 20 ms fades)
edge = np.round(0.3 * np.sin(2 * np.pi * 440.0 * t5) * 32768).astype(np.int16)
check(
    "a 440 Hz tone that starts and ends abruptly (non-zero at the clip edges) leaves no edge click above the floor: peak under -85 dBFS and a blank image",
    CF.signatures(edge)["peak_dbfs"] < -85 and int(CR.spectrogram_index(edge).max()) == 0,
    str(CF.signatures(edge)["peak_dbfs"]),
)
# resolution limits of every stored time series
sgn = s_ = None
s = vis(min_beams=1)
s.play({5: [S.row(P, 0.95, beam=1)]})
j = s.sidecar(*s.clip_ids()[0])
dur = j["duration_s"]
check(
    "resolution of what is stored for the 5 s clip: envelope 98 values (under 20/s) in keys n / step_s / rms_db only; dominant_hz 49 values (under 10/s) with step 0.1 s; PNG columns 98 (50 ms each); no min/max pairs anywhere in the sidecar",
    dur == 5.0
    and j["envelope"]["n"] == len(j["envelope"]["rms_db"]) == 98
    and set(j["envelope"]) == {"n", "step_s", "rms_db"}
    and len(j["envelope"]["rms_db"]) / dur <= 20.0
    and len(j["signatures"]["dominant_hz"]) == 49
    and len(j["signatures"]["dominant_hz"]) / dur <= 10.0
    and j["signatures"]["dominant_step_s"] == 0.1
    and j["spectrogram"]["columns"] == 98
    and "minmax" not in json.dumps(j)
    and j["signatures"]["band_edges_hz"] == [4000, 8000, 16000, 24000]
    and len(j["signatures"]["band_db"]) == 3,
    str((dur, j["envelope"]["n"], len(j["signatures"]["dominant_hz"]))),
)
check(
    "the longest clip (12 s) still obeys the limits: envelope 238 values, dominant 119 values, PNG 238 columns",
    (lambda e, g, i: len(e["rms_db"]) == 238 and len(g["dominant_hz"]) == 119 and i.shape == (96, 238))(
        CF.envelope(np.zeros(12 * FS, np.int16)), CF.signatures(np.zeros(12 * FS, np.int16)), CR.spectrogram_index(np.zeros(12 * FS, np.int16))
    ),
)
check(
    "no field of the sidecar can rebuild audio: the key set of the sidecar is exactly the documented one (no waveform, no phase, no per-sample array, no per-frame spectrum)",
    sorted(j)
    == sorted(
        [
            "k",
            "v",
            "id",
            "t_start",
            "t_end",
            "t_start_utc",
            "t_end_utc",
            "species",
            "group",
            "confidence_max",
            "reports",
            "beam",
            "beams_reported",
            "beams_agreeing",
            "in_region",
            "model",
            "model_version",
            "provenance",
            "sample_rate",
            "channels",
            "subtype",
            "samples",
            "duration_s",
            "pre_roll_s",
            "post_roll_s",
            "capped",
            "ring_truncated",
            "start_sample",
            "speech_guard",
            "audio_stored",
            "audio_expired",
            "audio_file",
            "codec",
            "png",
            "json",
            "audio_bytes",
            "spectrogram",
            "signatures",
            "envelope",
        ]
    ),
    str(sorted(j)),
)
# nothing that starts the analyzer may opt in to audio (read as data files: they are the callers, not the unit under test)
BMA_ROOT = os.path.abspath(os.path.join(HERE, ".."))
starters = [
    os.path.join(BMA_ROOT, *p_.split("/"))
    for p_ in ("pc/run_pipeline.ps1", "pc/install_task.ps1", "analysis/run_analyzer.sh", "docker/entrypoint.sh", "docker/analyzer.Dockerfile")
]
present = [p_ for p_ in starters if os.path.isfile(p_)]


def code_lines(p_):
    return [ln for ln in open(p_, encoding="utf-8", errors="replace") if not ln.lstrip().startswith("#")]


_ra = [os.path.normcase(os.path.join(BMA_ROOT, "analysis", "run_analyzer.sh"))]
_ra_lines = [ln.strip() for ln in code_lines(_ra[0])] if os.path.isfile(_ra[0]) else []
check(
    "opt-in only (owner 2026-10-27): the installer, supervisor script and container files never name --clip-audio on a non-comment line; the analyzer launcher names it on exactly one line, guarded by the 4th positional being 1 (twin: the flag exists in clips.py add_args)",
    len(present) >= 4
    and all("--clip-audio" not in ln for p_ in present if os.path.normcase(p_) != _ra[0] for ln in code_lines(p_))
    and [ln for ln in _ra_lines if "--clip-audio" in ln] == ['[ "$4" = 1 ] && EXTRA+=(--clip-audio)']
    and "--clip-audio" in [o for a_ in _ap2._actions for o in a_.option_strings],
    str((present, _ra_lines)),
)
# reconstruction attempt: Griffin-Lim from the stored image rows
try:
    import librosa
except ImportError:
    librosa = None
    skip("Griffin-Lim reconstruction attempt", "librosa not installed")
if librosa is not None:
    from scipy import signal as _sig

    def recon(img):
        """The best attack we can think of with the image alone: undo the pixel mapping, spread each row over its FFT bins, repeat each 50 ms column 4 times and run Griffin-Lim (60 iterations)."""
        im_ = img[::-1].astype(np.float64)
        amp = 10 ** ((CR.DB_LO + im_ / 255.0 * (CR.DB_HI - CR.DB_LO)) / 20.0)
        amp[im_ == 0] = 0.0
        Sx = np.zeros((CR.NFFT // 2 + 1, amp.shape[1]))
        for r in range(CR.H):
            Sx[lo_b[r] : hi_b[r], :] = amp[r]
        return librosa.griffinlim(np.repeat(Sx, 4, axis=1) * (CR.NFFT / 2), n_iter=60, hop_length=CR.NFFT // 4, win_length=CR.NFFT, n_fft=CR.NFFT, rng=0)

    def ncc(a, b, lag=24000):
        n = min(len(a), len(b))
        a, b = a[:n] - a[:n].mean(), b[:n] - b[:n].mean()
        if a.std() == 0 or b.std() == 0:
            return 0.0
        c = _sig.fftconvolve(a, b[::-1], mode="full") / (np.linalg.norm(a) * np.linalg.norm(b))
        return float(np.abs(c[n - 1 - lag : n - 1 + lag]).max())

    fric = _sig.lfilter(*_sig.butter(6, 4500, "high", fs=FS), rng2.standard_normal(len(t5)))
    mixed = voiced + fric / np.abs(fric).max() * 0.2 * (syl > 0.8)  # the voiced signal plus fricative bursts above 4.5 kHz at every syllable peak
    pcm_m = np.round(mixed * 32768).astype(np.int16)
    n_voiced = ncc(voiced, recon(im_v))
    n_mixed = ncc(mixed, recon(CR.spectrogram_index(pcm_m)))
    n_noise = ncc(mixed, rng2.standard_normal(len(mixed)))
    print(
        f"INFO Griffin-Lim reconstruction from the stored image, max |normalised cross-correlation| over +-0.5 s lag: voiced speech-like {n_voiced:.4f}, voiced + fricatives {n_mixed:.4f}, white-noise baseline {n_noise:.4f}"
    )
    check(
        "reconstruction attempt (Griffin-Lim from the image rows) of the speech-like signal is NOT correlated with it: max |ncc| < 0.05 for the voiced signal (its image is blank) and for voiced + fricatives (chance level of 48000 lags is about 0.01; an intelligible reconstruction would exceed 0.3)",
        n_voiced < 0.05 and n_mixed < 0.05,
        str((n_voiced, n_mixed, n_noise)),
    )
    check(
        "positive twin of the attack: the attack does recover what is stored - the level envelope of the fricative bursts (correlation of 50 ms RMS tracks > 0.8): the image is informative, just not about the voice",
        (
            lambda y: (
                float(
                    np.corrcoef(
                        np.sqrt((mixed[CF.TRIM // 2 :][: len(y) // 2400 * 2400].reshape(-1, 2400) ** 2).mean(1)),
                        np.sqrt((y[: len(y) // 2400 * 2400].reshape(-1, 2400) ** 2).mean(1)),
                    )[0, 1]
                )
                > 0.8
            )
        )(recon(CR.spectrogram_index(pcm_m))),
    )

# ====================================================================================================================
# 14. human-voice purge (owner 2026-10-27: keep the audio clips, purge CONFIRMED human voices): ClipService.purge_human re-checks stored
# clips against the persisted <out>/tags.jsonl and <out>/birds.jsonl. The clip of one(0.93) spans [T0+14, T0+19]; +-5 s guard = [T0+9, T0+24].
T0 = S.T0
HUM_TOP, HUM_P = sorted(CL.load_human_top(os.path.join(HERE, "clip_exclude.json"))[0])[0], CL.load_human_top(os.path.join(HERE, "clip_exclude.json"))[1]


def trow(t, p=0.0, active=False, top=None):
    return {"k": "tags", "t": t, "beam": 1, "speech_p": p, "speech_active": active, "top": top if top is not None else [], "emb_stored": True}


def brow(t, common="Human vocal", sci="Homo sapiens", k="bird"):
    return {"k": k, "t": t, "beam": 3, "common": common, "scientific": sci, "confidence": 0.5}


def purged(s, name, logs):
    """one clip with audio, the given logs on disk, one purge: -> (sim, n_purged)."""
    for fn, rows in logs.items():
        s.log(fn, rows)
    return s.svc.purge_human()


def evicted(s):
    return [(r["k"], r["reason"]) for r in s.index() if r["k"] == "clip_evicted"]


def intact(s):
    return sorted(os.path.splitext(f)[1] for f in s.files() if not f.endswith("index.jsonl")) == [".flac", ".json", ".png"]


s = one(0.93)
check(
    "purge setup: the clip has audio, PNG and JSON, nothing in the logs: purge deletes nothing (twin of every purge below)",
    intact(s) and s.svc.purge_human() == 0 and cnt(s, "clips_purged_human") == 0 and evicted(s) == [],
    str(s.files()),
)
s = one(0.93)
n = purged(s, "speech_active inside", {"tags.jsonl": [trow(T0 + 20.0, 0.0, True)]})
check(
    "purge: a LATER tags row with speech_active inside the span deletes the whole clip: no file left, one human_voice tombstone, counter 1, table empty",
    n == 1
    and [f for f in s.files() if not f.endswith("index.jsonl")] == []
    and evicted(s) == [("clip_evicted", "human_voice")]
    and cnt(s, "clips_purged_human") == 1
    and s.svc.store.table == {}
    and cnt(s, "clip_service_errors") == 0,
    str((n, s.files(), evicted(s))),
)
check(
    "purge: idempotent - a second run deletes nothing, counter stays 1, still one tombstone",
    s.svc.purge_human() == 0 and cnt(s, "clips_purged_human") == 1 and len(evicted(s)) == 1,
)
s = one(0.93)
n = purged(s, "speech_active just outside", {"tags.jsonl": [trow(T0 + 29.0, 0.0, True)]})
check(
    "purge twin: a speech_active tags row whose window (T0+24, T0+29] ends just past span+guard is kept: files intact, counter 0, no tombstone",
    n == 0 and intact(s) and cnt(s, "clips_purged_human") == 0 and evicted(s) == [],
    str((n, s.files())),
)
s = one(0.93)
n = purged(s, "speech_active just inside", {"tags.jsonl": [trow(T0 + 28.5, 0.0, True)]})
check(
    "purge boundary: the same row 0.5 s earlier (window (T0+23.5, T0+28.5]) overlaps the guard and is purged",
    n == 1 and cnt(s, "clips_purged_human") == 1,
    str((n, s.files())),
)
s = one(0.93)
n = purged(s, "before", {"tags.jsonl": [trow(T0 + 9.0, 0.0, True)]})
check("purge twin: a speech_active tags window (T0+4, T0+9] ending exactly at the start of the guard is kept", n == 0 and intact(s), str((n, s.files())))
s = one(0.93)
n = purged(s, "before", {"tags.jsonl": [trow(T0 + 9.5, 0.0, True)]})
check("purge boundary: the same window 0.5 s later overlaps the guard and is purged", n == 1 and cnt(s, "clips_purged_human") == 1, str((n, s.files())))

s = one(0.93)
n = purged(s, "birds human inside", {"birds.jsonl": [brow(T0 + 17.0)]})
check(
    "purge: a Human BirdNET row with t inside the span deletes the clip (tombstone human_voice, counter 1)",
    n == 1 and evicted(s) == [("clip_evicted", "human_voice")] and cnt(s, "clips_purged_human") == 1 and not [f for f in s.files() if f.endswith(".flac")],
    str((n, s.files())),
)
s = one(0.93)
n = purged(s, "birds human outside", {"birds.jsonl": [brow(T0 + 24.5)]})
check(
    "purge twin: the same Human row at T0+24.5 (after span+guard) keeps the clip",
    n == 0 and intact(s) and cnt(s, "clips_purged_human") == 0,
    str((n, s.files())),
)
s = one(0.93)
n = purged(s, "birds non-human inside", {"birds.jsonl": [brow(T0 + 17.0, "Carolina Wren", "Thryothorus ludovicianus")]})
check("purge twin: a non-human BirdNET row inside the span keeps the clip", n == 0 and intact(s), str((n, s.files())))
s = one(0.93)
n = purged(s, "birds human scientific only", {"birds.jsonl": [brow(T0 + 17.0, "Vocal", "Human non-vocal", "bird_filtered")]})
check(
    "purge: a bird_filtered row whose SCIENTIFIC name starts with Human inside the span deletes the clip",
    n == 1 and cnt(s, "clips_purged_human") == 1,
    str((n, s.files())),
)

s = one(0.93)
n = purged(s, "speech_p at threshold", {"tags.jsonl": [trow(T0 + 20.0, 0.10)]})
check(
    "purge: speech_p exactly at --clip-speech-p (0.10) with speech_active false deletes the clip",
    n == 1 and cnt(s, "clips_purged_human") == 1,
    str((n, s.files())),
)
s = one(0.93)
n = purged(s, "speech_p below", {"tags.jsonl": [trow(T0 + 20.0, 0.0999)]})
check("purge twin: speech_p 0.0999 (just below) keeps the clip", n == 0 and intact(s), str((n, s.files())))
s = one(0.93)
n = purged(s, "human_top at p", {"tags.jsonl": [trow(T0 + 20.0, 0.0, False, [[HUM_TOP, HUM_P]])]})
check(f"purge: a top label {HUM_TOP!r} at exactly human_top_p ({HUM_P}) deletes the clip", n == 1 and cnt(s, "clips_purged_human") == 1, str((n, s.files())))
s = one(0.93)
n = purged(s, "human_top below", {"tags.jsonl": [trow(T0 + 20.0, 0.0, False, [[HUM_TOP, HUM_P - 0.01]])]})
check("purge twin: the same label just below human_top_p keeps the clip", n == 0 and intact(s), str((n, s.files())))
s = one(0.93)
n = purged(s, "non-human top", {"tags.jsonl": [trow(T0 + 20.0, 0.0, False, [["Bird", 0.99]])]})
check("purge twin: a non-human top label at 0.99 keeps the clip", n == 0 and intact(s), str((n, s.files())))

s = one(0.93)
garbage = [
    b"not json at all",
    b'{"k": "tags", "t": ',
    b'{"k": "tags", "t": "soon", "speech_active": true}',
    b'\xff\xfe{"k":"tags"}',
    b'[1, 2, "tags"]',
    b'{"k": "tags", "t": NaN, "speech_active": true}',
    b'{"k": "tags", "t": 1e999, "speech_active": true}',
    b"human",
    b"",
]
s.log("tags.jsonl", [], raw=garbage)
s.log("birds.jsonl", [], raw=garbage)
n = s.svc.purge_human()
check(
    "purge: garbage lines (truncated JSON, bad bytes, non-numeric / NaN / inf t, arrays) do not raise and purge nothing: clip intact, service_errors 0",
    n == 0 and intact(s) and cnt(s, "clip_service_errors") == 0,
    str((n, cnt(s, "clip_service_errors"))),
)
s.log("tags.jsonl", [trow(T0 + 20.0, 0.0, True)], raw=garbage)
check("purge twin: the same garbage plus one valid flagged row deletes the clip", s.svc.purge_human() == 1 and cnt(s, "clip_service_errors") == 0)
s = one(0.93)
check(
    "purge: missing logs (no tags.jsonl, no birds.jsonl) do not raise and keep the clip",
    s.svc.purge_human() == 0 and intact(s) and cnt(s, "clip_service_errors") == 0,
)

# no audio clip stored => the logs are not read at all (cost bound), visual-only clips are untouched by themselves
reads = []
v = sim(audio=False)
v.play({5: [S.row("Poecile carolinensis", 0.93)]})
orig_read = v.svc._read_tail_rows
v.svc._read_tail_rows = lambda name, needle: (reads.append(name), orig_read(name, needle))[1]
v.log("tags.jsonl", [trow(T0 + 20.0, 0.0, True)])
check(
    "purge: no audio clip stored -> the logs are not read (recording stub on the reader sees 0 calls), nothing purged, the visual-only clip stays",
    v.svc.purge_human() == 0 and reads == [] and len(v.clip_ids()) == 1 and cnt(v, "clips_purged_human") == 0,
    str((reads, v.clip_ids())),
)
reads2 = []
s = one(0.93)
orig_read = s.svc._read_tail_rows
s.svc._read_tail_rows = lambda name, needle: (reads2.append(name), orig_read(name, needle))[1]
s.log("tags.jsonl", [trow(T0 + 40.0, 0.0, True)])
s.svc.purge_human()
check("purge twin: with an audio clip stored both logs ARE read, in order (tags.jsonl, birds.jsonl)", reads2 == ["tags.jsonl", "birds.jsonl"], str(reads2))

# the visual-only sidecars of a confirmed-voice span go too, as long as some audio clip is stored; other clips are untouched
s = sim(cap_per_species_hour=6)
s.play({5: [S.row("Poecile carolinensis", 0.93)], 15: [S.row("Strix varia", 0.95)]}, k_end=18)
ids = [c for _, c in s.clip_ids()]
t_first = CL.id_time(ids[0])
s.now = t_first + 24 * 3600 + 10.0
s.svc.store.sweep(s.now)
first_audio_gone = not [f for f in s.files() if ids[0] in f and f.endswith(".flac")] and [f for f in s.files() if ids[1] in f and f.endswith(".flac")] != []
s.log("tags.jsonl", [trow(T0 + 20.0, 0.0, True)])
n = s.svc.purge_human()
left = sorted(f for f in s.files() if not f.endswith("index.jsonl"))
check(
    "purge: the first clip (audio already expired, PNG and JSON left) is purged with its sidecars; the second clip, outside the span, keeps audio, PNG and JSON",
    first_audio_gone
    and n == 1
    and left == sorted(f"{CL.time.strftime('%Y%m%d', CL.time.gmtime(CL.id_time(ids[1])))}/{ids[1]}{e}" for e in (".flac", ".json", ".png"))
    and cnt(s, "clips_purged_human") == 1,
    str((first_audio_gone, n, left)),
)
check(
    "purge: the tombstone names the purged clip",
    [r["id"] for r in s.index() if r["k"] == "clip_evicted"] == [ids[0]] and evicted(s) == [("clip_evicted", "human_voice")],
)

# a restart rebuilds the table from the sidecars: t_end survives and the purge still works
s = one(0.93)
s2 = S.Sim(s.root, audio=True, min_beams=1)
s2.log("tags.jsonl", [trow(T0 + 20.0, 0.0, True)])
check(
    "purge: after a restart (table rebuilt from the sidecars) the clip is still purged",
    s2.svc.purge_human() == 1 and [f for f in s2.files() if not f.endswith("index.jsonl")] == [],
    str(s2.files()),
)

# bounded cost: only the last 8 MB of a log are read
s = one(0.93)
pad = b'{"k": "tags", "t": 1.0, "speech_active": false, "speech_p": 0.0, "top": []}' + b" " * 100
rows_old = [trow(T0 + 20.0, 0.0, True)]
s.log("tags.jsonl", rows_old, raw=[pad] * ((CL.PURGE_TAIL_BYTES // len(pad)) + 10))
check("purge: a flagged row older than the last 8 MB of the log is not read (clip kept, bounded cost)", s.svc.purge_human() == 0 and intact(s))
s.log("tags.jsonl", [], raw=[pad] * ((CL.PURGE_TAIL_BYTES // len(pad)) + 10) + [json.dumps(trow(T0 + 20.0, 0.0, True)).encode()])
check("purge twin: the same flagged row inside the last 8 MB is read and purges the clip", s.svc.purge_human() == 1)

# the sweeper loop runs the purge after the sweep, every sweep
calls = []
s = one(0.93)
s.svc.store.sweep = lambda now=None: calls.append("sweep")
s.svc.purge_human = lambda: calls.append("purge")
waits = []


def fake_wait(sec):
    waits.append(sec)
    if len(waits) == 3:
        s.svc.stop_ev.set()


s.svc.wait = fake_wait
s.svc._sweeper_loop()
check(
    "sweeper loop: each pass is wait, sweep, purge (two passes, then stop)",
    calls == ["sweep", "purge", "sweep", "purge"] and waits == [s.cfg.sweep_s] * 3,
    str((calls, waits)),
)
s = one(0.93)
s.log("tags.jsonl", [trow(T0 + 20.0, 0.0, True)])
ws = []
s.svc.wait = lambda sec: (ws.append(sec), s.svc.stop_ev.set() if len(ws) == 2 else None)
s.svc._sweeper_loop()
check(
    "sweeper loop, real functions: one pass purges the clip (counter 1, files gone, sweep_runs 1)",
    cnt(s, "clips_purged_human") == 1 and cnt(s, "sweep_runs") == 1 and [f for f in s.files() if not f.endswith("index.jsonl")] == [],
    str((s.svc.counters(), s.files())),
)

# the counter is in the status meta set and the config default stays audio OFF
check("COUNTERS has clips_purged_human exactly once", CL.COUNTERS.count("clips_purged_human") == 1 and len(CL.COUNTERS) == 30)
check(
    "the audio default is still OFF (ClipConfig and the argparse flag)",
    CL.ClipConfig().audio is False and not (lambda ap: (CL.add_args(ap), ap.parse_args([]).clip_audio)[1])(__import__("argparse").ArgumentParser()),
)

# 10. the real host over UDP
if "--no-e2e" in sys.argv:  # the mutation harness skips the 50 s host run except for the host mutations
    skip("host e2e over UDP", "--no-e2e")
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{len(fails)} failure(s), {n_skip} skipped")
    for f in fails:
        print("  FAILED:", f)
    sys.exit(1 if fails else 0)

RUNNER = """import sys, types
sys.path.insert(0, {here!r})
import plugins as P
import numpy as np
SPEECH = {speech}
MULTI = {multi}
class FakeTags:
    name, win_s, hop_s, heavy = "tags", 5.0, 2.0, True
    def __init__(self, *a): pass
    def process(self, x, t):
        a = SPEECH
        return [{{"k": "tags", "t": t, "beam": 1, "speech_p": 0.9 if a else 0.01, "speech_active": a, "top": [], "emb_stored": not a}}], None
class FakeBirds:
    name, win_s, hop_s, heavy = "birds", 3.0, 3.0, True
    def __init__(self, *a, **k):
        self.window_info = {{}}
        self.bn = types.SimpleNamespace(version="fake-1")
    def process(self, x, t):
        e = (x.astype(np.float64) ** 2).mean(axis=0)
        if e[3] > 2e-4:
            rows = [{{"k": "bird", "t": t, "beam": 4, "level_dbfs": -33.0, "common": "Test Katydid", "scientific": "Neoconocephalus testus",
                      "confidence": 0.93, "provenance": "model_opinion", "in_region": True, "non_species": False, "group": "insect"}}]
            if MULTI:                                   # the second loudest beam hears it too (--clip-min-beams 2 needs two distinct beams)
                rows.append(dict(rows[0], beam=5, confidence=0.88, level_dbfs=-45.0))
            return rows, None
        return [], None
P.Tagger, P.MultiBeamBirds = FakeTags, FakeBirds
sys.argv = ["host.py"] + sys.argv[1:]
import host
host.main()
"""


def bmaa(slot, block, x):
    s24 = np.clip((x * 8388608.0).astype(np.int64), -8388608, 8388607).astype("<i4")
    return b"BMAA" + bytes([1, 0, slot, 0]) + struct.pack("<III", 0, block & 0xFFFFFFFF, len(x)) + s24.tobytes()


def free_port():
    """A UDP port nobody holds right now: two concurrent runs of this file (the mutation harness) must not share a port."""
    k = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    k.bind(("127.0.0.1", 0))
    p = k.getsockname()[1]
    k.close()
    return p


runs = []
# "tick": an 8 s tone (28-36 s) that runs to the very end of the stream, after which NO window is ever processed again; only the host's clips.tick() can close the
# event and expire the held clip (short guard and hold so that the deadline lies well inside the run)
# "single": BirdNET reports one beam only: the default --clip-min-beams 2 stores nothing; "single1" is the same stream with --clip-min-beams 1 (stored)
for label, speech, port, extra in (
    ("clean", False, free_port(), []),
    ("speech", True, free_port(), []),
    ("tick", False, free_port(), ["--clip-guard-s", "1", "--clip-hold-s", "1"]),
    ("single", False, free_port(), []),
    ("single1", False, free_port(), ["--clip-min-beams", "1", "--clip-audio"]),
):
    out_dir = os.path.join(TMP, "e2e_" + label)
    os.makedirs(out_dir)
    runner = os.path.join(TMP, f"runner_{label}.py")
    open(runner, "w").write(RUNNER.format(here=HERE, speech=speech, multi=not label.startswith("single")))
    p = subprocess.Popen(
        [
            sys.executable,
            runner,
            "--listen",
            f"127.0.0.1:{port}",
            "--out",
            out_dir,
            "--history-dir",
            "",
            "--no",
            "clap",
            "--no",
            "spatial",
            "--panns-dir",
            TMP,
            "--device",
            "cpu",
            "--secs",
            "52",
        ]
        + extra,
        stderr=subprocess.PIPE,
        text=True,
    )
    runs.append((label, p, out_dir, port))
time.sleep(3.0)
sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
rng = np.random.default_rng(3)
blk, t_start, nb = 0, time.time(), 0
for sec in range(36):
    x = (rng.standard_normal((FS, 8)) * 10 ** (-60 / 20)).astype(np.float32)
    xt = x.copy()
    tone = (np.sin(2 * np.pi * 6000.0 * np.arange(FS) / FS) * 10 ** (-30 / 20)).astype(np.float32)
    if 14 <= sec < 20:
        x[:, 3] += tone
    if sec >= 28:
        xt[:, 3] += tone
    for off in range(0, FS, 256):
        for label, _, _, port in runs:
            xs = xt if label == "tick" else x
            for slot in range(8):
                sk.sendto(bmaa(slot, blk + off, xs[off : off + 256, slot]), ("127.0.0.1", port))
        nb += 1
        d = t_start + nb * 256 / FS - time.time()
        if d > 0:
            time.sleep(d)
    blk += FS
res = {}
for label, p, out_dir, port in runs:
    p.wait(timeout=90)
    err = p.stderr.read()
    res[label] = (p.returncode, err, json.load(open(os.path.join(out_dir, "status.json"))), out_dir)
rc, err, st, od = res["clean"]
cd = os.path.join(od, "clips")
allf = [os.path.relpath(os.path.join(dp, f), cd).replace(os.sep, "/") for dp, _, fs_ in os.walk(cd) for f in fs_]
pcm_like = [f for f in allf if not f.endswith((".png", ".json", ".jsonl"))]
check("host e2e (clean, DEFAULT flags = visual-only, fake models over real UDP rings): exited cleanly", rc == 0, err[-400:])
check(
    "host e2e (default): exactly one clip stored as png + json + index, NO audio file of any kind under the clips dir, status audio_written false, clips.audio false, audio_files 0",
    len(allf) == 3 + 0
    and not pcm_like
    and sorted(os.path.splitext(f)[1] for f in allf) == [".json", ".jsonl", ".png"]
    and st["audio_written"] is False
    and st["clips"]["audio"] is False
    and st["clips"]["stored"] == 1
    and st["clips"]["audio_files"] == 0
    and st["meta"]["clips_stored"] == 1
    and st["meta"]["clip_write_errors"] == 0,
    str((allf, st.get("clips"), st.get("meta"))),
)
_jn = [f for f in allf if f.endswith(".json")]
if _jn:
    jj = json.load(open(os.path.join(cd, _jn[0])))
    dm = float(np.median(jj["signatures"]["dominant_hz"]))
    check(
        "host e2e (default): the sidecar is the 6 kHz tone on beam 4 (median dominant 6000 Hz +-25, species, beam), audio_stored false, audio_file / codec null, audio_bytes 0, speech guard stamped (>= 3 tags windows, max speech_p 0.01)",
        abs(dm - 6000.0) < 25
        and jj["beam"] == 4
        and jj["species"]["scientific"] == "Neoconocephalus testus"
        and jj["model_version"] == "fake-1"
        and 5.0 <= jj["duration_s"] <= 12.0
        and (jj["audio_stored"], jj["audio_file"], jj["codec"], jj["audio_bytes"], jj["audio_expired"]) == (False, None, None, 0, False)
        and jj["speech_guard"]["tags_windows"] >= 3
        and jj["speech_guard"]["max_speech_p"] == 0.01,
        str((dm, jj["beam"], jj["duration_s"], jj["speech_guard"])),
    )
rc, err, st, od = res["single1"]
cd = os.path.join(od, "clips")
flacs = [os.path.join(dp, f) for dp, _, fs_ in os.walk(cd) for f in fs_ if f.endswith((".flac", ".wav"))]
check(
    "host e2e (--clip-audio): exactly one audio file, status audio_written true, clips.audio true, audio_files 1",
    rc == 0
    and len(flacs) == 1
    and st["audio_written"] is True
    and st["clips"]["audio"] is True
    and st["clips"]["audio_files"] == 1
    and st["clips"]["stored"] == 1,
    str((rc, flacs, st.get("clips"))),
)
if flacs:
    jj = json.load(open(os.path.splitext(flacs[0])[0] + ".json"))
    aa, rr = sf.read(flacs[0], dtype="int16")
    check(
        "host e2e (--clip-audio): the audio is the clip (length equals the sidecar samples, 48 kHz), audio_stored true, and the index line names the same file relative to the clips dir",
        len(aa) == jj["samples"]
        and rr == 48000
        and jj["audio_stored"] is True
        and [r["audio_file"] for r in map(json.loads, open(os.path.join(cd, "index.jsonl")))] == [os.path.relpath(flacs[0], cd).replace(os.sep, "/")],
        str((len(aa), jj["samples"])),
    )
rc, err, st, od = res["speech"]
aud = [f for dp, _, fs_ in os.walk(od) for f in fs_ if f.endswith((".flac", ".wav", ".tmp", ".part"))]
check(
    "host e2e (speech flagged by the tagger on every window): nothing stored, no audio file anywhere, audio_written false, skipped_speech counted",
    rc == 0 and aud == [] and st["audio_written"] is False and st["clips"]["stored"] == 0 and st["meta"]["clips_skipped_speech"] >= 1,
    str((rc, aud, st.get("clips"), st["meta"].get("clips_skipped_speech"), err[-300:])),
)
rc, err, st, od = res["tick"]
check(
    "host e2e (tone to the end of the stream, then silence): the host's clips.tick() closes the event and expires the held clip by its deadline: exited cleanly, clips_skipped_no_verdict 1, nothing stored or pending, no audio file",
    rc == 0
    and st["meta"]["clips_skipped_no_verdict"] == 1
    and st["clips"]["stored"] == 0
    and st["clips"]["pending"] == 0
    and st["audio_written"] is False
    and not [f for dp, _, fs_ in os.walk(od) for f in fs_ if f.endswith((".flac", ".wav", ".tmp", ".part"))],
    str((rc, st.get("clips"), st["meta"].get("clips_skipped_no_verdict"), err[-300:])),
)
rc, err, st, od = res["single"]
check(
    "host e2e (BirdNET reports ONE beam, default --clip-min-beams 2): nothing stored, no audio, clips_skipped_single_beam exactly 1, audio_written false",
    rc == 0
    and st["meta"]["clips_skipped_single_beam"] == 1
    and st["clips"]["stored"] == 0
    and st["audio_written"] is False
    and not [f for dp, _, fs_ in os.walk(od) for f in fs_ if f.endswith((".flac", ".wav", ".tmp", ".part"))],
    str((rc, st.get("clips"), st["meta"].get("clips_skipped_single_beam"), err[-300:])),
)
rc, err, st, od = res["single1"]
_j1 = [os.path.join(dp, f) for dp, _, fs_ in os.walk(os.path.join(od, "clips")) for f in fs_ if f.endswith(".json") and f != "index.jsonl"]
check(
    "host e2e (same one-beam stream, --clip-min-beams 1 through the real argparse): stored, beams_agreeing 1, clips_skipped_single_beam 0",
    rc == 0
    and st["meta"]["clips_skipped_single_beam"] == 0
    and st["clips"]["stored"] == 1
    and len(_j1) == 1
    and json.load(open(_j1[0]))["beams_agreeing"] == 1,
    str((rc, st.get("clips"), _j1, err[-300:])),
)
_jc = [os.path.join(dp, f) for dp, _, fs_ in os.walk(os.path.join(res["clean"][3], "clips")) for f in fs_ if f.endswith(".json") and f != "index.jsonl"]
check(
    "host e2e (clean, two beams reported): beams_agreeing exactly 2 and beams_reported [4, 5] in the sidecar",
    len(_jc) == 1 and json.load(open(_jc[0]))["beams_agreeing"] == 2 and json.load(open(_jc[0]))["beams_reported"] == [4, 5],
    str(_jc),
)

# ====================================================================================================================
# 14. --clip-min-conf (deploy setting, e.g. 0.5 so the Review tab has evidence for uncertain events): honoured end to end, the other rules unchanged
check(
    "--clip-min-conf: default 0.85 in ClipConfig and in the argparse flag; '--clip-min-conf 0.5' reaches the config as 0.5",
    CL.ClipConfig().min_conf == 0.85
    and _ap.parse_args([]).clip_min_conf == 0.85
    and CL.config_from_args(_ap.parse_args(["--clip-min-conf", "0.5"]), None).min_conf == 0.5,
)
mc = lambda **kw: sim(min_conf=0.5, min_beams=2, **kw)
s = mc()
s.play({5: [S.row(P, 0.55, beam=1), S.row(P, 0.52, beam=2)]})
j = s.sidecar(*s.clip_ids()[0]) if n_stored(s) else {}
check(
    "--clip-min-conf 0.5: a 2-beam event with max 0.55 IS stored (audio file, sidecar confidence_max 0.55, beams_agreeing 2, no low_conf count)",
    n_stored(s) == 1
    and len(s.audio_files()) == 1
    and j.get("confidence_max") == 0.55
    and j.get("beams_agreeing") == 2
    and cnt(s, "clips_skipped_low_conf") == 0,
    str((s.svc.counters(), j)),
)
s = dflt()
s.play({5: [S.row(P, 0.55, beam=1), S.row(P, 0.52, beam=2)]})
check(
    "twin: the same 0.55 two-beam event with the DEFAULT --clip-min-conf is not stored (low_conf 1)",
    n_stored(s) == 0 and cnt(s, "clips_skipped_low_conf") == 1,
    str(s.svc.counters()),
)
s = mc()
s.play({5: [S.row(P, 0.50, beam=1), S.row(P, 0.45, beam=2)]})
check(
    "--clip-min-conf 0.5: max exactly 0.50 is NOT stored (strictly above), low_conf 1",
    n_stored(s) == 0 and cnt(s, "clips_skipped_low_conf") == 1,
    str(s.svc.counters()),
)
s = mc()
s.play({5: [S.row(P, 0.5001, beam=1), S.row(P, 0.45, beam=2)]})
check("twin: max 0.5001 IS stored", n_stored(s) == 1, str(s.svc.counters()))
s = mc()
s.play({5: [S.row(P, 0.60, beam=1)]})
check(
    "--clip-min-conf 0.5: a single-beam 0.60 event is still refused by --clip-min-beams 2 (single_beam 1, low_conf 0)",
    n_stored(s) == 0 and cnt(s, "clips_skipped_single_beam") == 1 and cnt(s, "clips_skipped_low_conf") == 0,
    str(s.svc.counters()),
)
s = mc()
s.play({5: [S.row(P, 0.55, beam=1), S.row(P, 0.52, beam=2)]}, tags=base_tags((14, 0.5), skip=(14,)))
check(
    "--clip-min-conf 0.5: the speech guard still refuses a 0.55 two-beam event with speech in the guard window (speech 1, nothing stored, no audio)",
    n_stored(s) == 0 and cnt(s, "clips_skipped_speech") == 1 and s.audio_files() == [],
    str(s.svc.counters()),
)
s = mc()
s.play(
    {
        5: [S.row(P, 0.55, beam=1), S.row(P, 0.52, beam=3), S.row("Human vocal", 0.30, beam=2, group="mechanical", non_species=True)],
        6: [S.row("Human vocal", 0.30, beam=2, group="mechanical", non_species=True)],
    }
)
check(
    "--clip-min-conf 0.5: a Human-labelled report on another beam inside the guard window still refuses it (human_label 1, nothing stored)",
    n_stored(s) == 0 and cnt(s, "clips_skipped_human_label") == 1,
    str(s.svc.counters()),
)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{len(fails)} failure(s), {n_skip} skipped")
for f in fails:
    print("  FAILED:", f)
sys.exit(1 if fails else 0)
