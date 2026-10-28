#!/usr/bin/env python3
"""Tests for the mixscore plugin (analysis/mixscore.py), its report (mixscore_report.py) and its host wiring.
The REAL MixScore class runs on synthetic windows with fixed seeds; the only substitute is the documented scorer seam (a recording
scorer that answers from a table, standing in for BirdNET). The real BirdNET runs in `--real` (WSL audioeval venv, about a minute):
identical beams must score exactly as the single beam, and two different synthetic signals are scored and printed.
Script style (check(name, ok, detail), exit 1 on a failure) like test_analysis.py.  Run:  python -I test_mixscore.py [--real]
Derivations
  * identical beams: mean = beam, power ratio 1, gain 1 (0 dB), so mix == beam bit for bit.
  * 8 tones in 8 different exact FFT bins (orthogonal over the window): power(mean) = mean beam power / 8, so the gain is
    sqrt(8) = 9.0309 dB and the mix power equals the mean beam power.
  * scenario minute B (a multiple of 60): window ends B+3, B+6, B+9, B+12 (paired), B+30 (speech), B+33 (gated), B+42 (no birds report)."""

import json, math, os, shutil, socket, struct, subprocess, sys, tempfile, time, types
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
BMA = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import mixscore as M  # noqa: E402
import mixscore_report as R  # noqa: E402
import host as H  # noqa: E402
import taxa as T  # noqa: E402

FS = 48000
N = 3 * FS
fails, n_checks = [], 0
REAL = "--real" in sys.argv


def check(name, ok, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  [{detail}]"))
    if not ok:
        fails.append(name)


def raises(fn, exc, match):
    try:
        fn()
    except exc as e:
        return match in str(e)
    return False


rng = np.random.default_rng(7)
TAXA = T.Taxa(os.path.join(BMA, "ui", "taxa.json"))
check("taxa table loaded (the group stamps below need it)", TAXA.ok)


def common_win(level_db=-40.0, n=N):
    """the same white noise on all 8 beams at level_db dBFS rms"""
    return np.repeat((rng.standard_normal((n, 1)) * 10 ** (level_db / 20)).astype(np.float32), 8, axis=1)


# ============================================================ mean mix: exactness
x = common_win()
mix, g = M.mean_mix(x)
check("mix of 8 identical beams is the beam bit for bit, gain 0 dB", np.array_equal(mix, x[:, 0]) and g == 0.0, str(g))
tones = np.stack([np.sin(2 * np.pi * (300 + 100 * k) * np.arange(N) / FS) for k in range(8)], axis=1).astype(np.float32) * 0.1
mix, g = M.mean_mix(tones)
pb = float((tones.astype(np.float64) ** 2).mean())
check("mix of 8 orthogonal tones: gain 20 log10 sqrt(8) = 9.0309 dB", abs(g - 9.0309) < 1e-3, str(g))
check("mix of 8 orthogonal tones has the mean beam power (relative error < 1e-5)", abs(float((mix.astype(np.float64) ** 2).mean()) - pb) / pb < 1e-5)
check("mix is float32 of length n", mix.dtype == np.float32 and mix.shape == (N,))
base1 = common_win(n=4800)[:, 0]
anti = np.stack([base1 * s for s in (1, -1, 1, -1, 1, -1, 1, -1)], axis=1)
am, ag = M.mean_mix(anti)
check("beams that cancel exactly give a zero mix and gain 0 (no division blow-up)", float(np.abs(am).max()) == 0.0 and ag == 0.0, str(ag))
near = anti.copy()
near[:, 0] *= 1.0 + 1e-3  # almost cancelling: the gain is capped, not 1/eps
nm, ng = M.mean_mix(near)
check("an almost cancelling window is amplified by at most sqrt(8) (9.0309 dB)", abs(ng - 9.0309) < 1e-3, str(ng))

# the window arrives shifted by --beam-shift: beam 1 one sample late, beam 8 one sample early
sig = (rng.standard_normal(20000) * 0.05).astype(np.float32)
rings = []
for k in range(8):
    r = H.Ring(20000 + 16)
    delay = {0: 1, 7: -1}.get(k, 0)  # beam 1 arrives 1 sample late, beam 8 one sample early
    r.push(1000, np.roll(sig, delay)[:20000] if delay >= 0 else np.concatenate([sig[1:], sig[:1]]))
    rings.append(r)
SH = [1, 0, 0, 0, 0, 0, 0, -1]
ref = np.stack([sig[100:4100]] * 8, axis=1)
w_shift = H.assemble_window(rings, 1000 + 100, 4000, SH)
w_none = H.assemble_window(rings, 1000 + 100, 4000, [0] * 8)
m_ref, _ = M.mean_mix(ref)
m_s, _ = M.mean_mix(w_shift)
m_n, _ = M.mean_mix(w_none)
check(
    "with --beam-shift the assembled window is aligned: its mix is the single source bit for bit (columns identical)",
    np.array_equal(w_shift, ref) and np.array_equal(m_s, m_ref) and np.array_equal(m_s, sig[100:4100]),
)
check(
    "twin: without the shift the beams are misaligned and the mix differs from the source", not np.array_equal(m_n, m_ref) and not np.array_equal(w_none, ref)
)

# ============================================================ the scorer receives the mix; gate; process() returns nothing itself


class Rec:
    """recording scorer: answers from `answers` by call number, keeps what it was asked"""

    def __init__(self, answers=()):
        self.answers, self.calls = list(answers), []

    def __call__(self, audio, when):
        self.calls.append((audio.copy(), when))
        return (list(self.answers[len(self.calls) - 1]) if len(self.calls) <= len(self.answers) else []), []


rs = Rec()
p = M.MixScore(taxa=TAXA, scorer=rs, gate_dbfs=-70.0)
xw = np.stack([rng.standard_normal(N) * 10 ** (-40 / 20)] * 8, axis=1).astype(np.float32)
xw[:, 3] += (rng.standard_normal(N) * 10 ** (-45 / 20)).astype(np.float32)  # beams differ: the mix is not any one beam
out_, emb_ = p.process(xw, 1800000003.0)
check(
    "process scores exactly the mean mix: one scorer call with a 144000-sample float32 array equal to mean_mix(window)",
    len(rs.calls) == 1 and rs.calls[0][0].dtype == np.float32 and len(rs.calls[0][0]) == N and np.array_equal(rs.calls[0][0], M.mean_mix(xw)[0]),
)
check("process itself returns no record and no embedding (records come from observe)", out_ == [] and emb_ is None)
quiet = (rng.standard_normal((N, 8)) * 10 ** (-90 / 20)).astype(np.float32)
p.process(quiet, 1800000006.0)
check(
    "a window whose loudest beam is below the -70 dBFS gate is not scored (call count stays 1) and counts as gated",
    len(rs.calls) == 1 and p.stats()["gated"] == 1,
    str(p.stats()),
)
p.process(xw, 1800000009.0)
check("twin: a window above the gate is scored (2 calls)", len(rs.calls) == 2 and p.stats()["scored"] == 2)
loud_one = np.zeros((N, 8), np.float32)
loud_one[:, 6] = (rng.standard_normal(N) * 10 ** (-50 / 20)).astype(np.float32)
rs2 = Rec()
p2 = M.MixScore(taxa=TAXA, scorer=rs2, gate_dbfs=-70.0)
p2.process(loud_one, 1800000003.0)
check("the gate follows the LOUDEST beam like birds (one beam at -50 dBFS, seven silent: scored)", len(rs2.calls) == 1)

# ============================================================ aggregation scenario
B = 1800000000.0


def det(common, sci, conf, **kw):
    return dict({"common": common, "scientific": sci, "confidence": conf}, **kw)


CHICK, TIT, CARD, KATY, OWL = (
    ("Carolina Chickadee", "Poecile carolinensis"),
    ("Tufted Titmouse", "Baeolophus bicolor"),
    ("Northern Cardinal", "Cardinalis cardinalis"),
    ("Common Virtuoso Katydid", "Neoconocephalus triops"),
    ("Barn Owl", "Tyto alba"),
)


def brec(sp, conf, t, beam=1):
    g, _ = TAXA.lookup(sp[1], sp[0])
    return {
        "k": "bird",
        "t": t,
        "beam": beam,
        "level_dbfs": -40.0,
        "common": sp[0],
        "scientific": sp[1],
        "confidence": conf,
        "provenance": "model_opinion",
        "in_region": True,
        "non_species": False,
        "group": g,
    }


def scenario(speech, with_tags=True):
    # per window: (end offset, mix answer, per-beam answer or None = no birds report at all, loud?)
    wins = [
        (3, [det(*CHICK, 0.70), det("Human vocal", "Human vocal", 0.90)], [brec(CHICK, 0.60, B + 3, 1), brec(CHICK, 0.40, B + 3, 2)], True),
        (6, [det(*TIT, 0.40)], [brec(TIT, 0.50, B + 6), brec(KATY, 0.90, B + 6, 2)], True),
        (9, [det(*CARD, 0.30)], [], True),
        (12, [], [brec(CHICK, 0.30, B + 12)], True),
        (30, [det(*OWL, 0.88)], [brec(OWL, 0.80, B + 30)], True),  # the speech window
        (33, None, [], False),  # gated
        (42, [det(*CHICK, 0.55)], None, True),
    ]  # birds never reports this one
    rs = Rec([w[1] for w in wins if w[1] is not None])
    pl = M.MixScore(taxa=TAXA, scorer=rs, gate_dbfs=-70.0)
    recs = []
    ev = [(B + off, 0, (mixans, beam, loud)) for off, mixans, beam, loud in wins]  # the mix window ends
    ev += [(B + off + 0.2, 1, beam) for off, mixans, beam, loud in wins if beam is not None]  # birds reports 0.2 s later
    if with_tags:
        ev += [(B + tt + 0.5, 2, speech and 28 <= tt <= 34) for tt in range(2, 121, 2)]  # tags reports 0.5 s after its window
    for t, kind, arg in sorted(ev, key=lambda e: (e[0], e[1])):  # delivered in time order, as the host would
        if kind == 0:
            pl.process(common_win(-40.0) if arg[2] else (rng.standard_normal((N, 8)) * 10 ** (-90 / 20)).astype(np.float32), round(t))
        elif kind == 1:
            recs += pl.observe("birds", arg, t)
        else:
            recs += pl.observe("tags", [{"k": "tags", "speech_active": arg}], round(t - 0.5))
    recs += pl.observe("dsp", [], B + 107.0)  # the clock passes 60 + 45 s after the minute start: the minute is emitted
    return pl, rs, recs


pl, rs, recs = scenario(True)
cmp_ = [r for r in recs if r["type"] == "mixscore_compare"]
wr = [r for r in recs if r["type"] == "mixscore"]
check(
    "scenario: exactly one compare record for the minute, at the minute start", len(cmp_) == 1 and cmp_[0]["t"] == B and cmp_[0]["interval_s"] == 60, str(cmp_)
)
c = cmp_[0]
check(
    "scenario: window counts scored 5, paired 4, unpaired 1, gated 1, dropped_speech 1, dropped_unknown 0",
    c["windows"] == {"gated": 1, "scored": 5, "paired": 4, "unpaired": 1, "dropped_speech": 1, "dropped_unknown": 0},
    str(c["windows"]),
)
check(
    "scenario: events both 2, only per-beam 2 (katydid, chickadee .30), only mix 1 (cardinal)",
    c["events"] == {"both": 2, "beam_only": 2, "mix_only": 1},
    str(c["events"]),
)
check(
    "scenario: shared-event confidences: best beam mean 0.55 (0.60, 0.50), mix mean 0.55 (0.70, 0.40), diff_mean 0.0, mix higher in 1",
    c["both_conf"] == {"n": 2, "best_beam_mean": 0.55, "mix_mean": 0.55, "diff_mean": 0.0, "mix_higher": 1},
    str(c["both_conf"]),
)
check(
    "scenario: counts by group (bird both 2 / beam_only 1 / mix_only 1; insect beam_only 1)",
    c["groups"] == {"bird": {"both": 2, "beam_only": 1, "mix_only": 1}, "insect": {"both": 0, "beam_only": 1, "mix_only": 0}},
    str(c["groups"]),
)
check(
    "scenario: mix gain mean is 0.0 (identical beams) and min_conf is recorded", c["mix_gain_db_mean"] == 0.0 and c["min_conf"] == 0.25 and "partial" not in c
)
check(
    "scenario: window records for the 4 windows with mix detections, in time order, top sorted, no human label",
    [r["t"] for r in wr] == [B + 3, B + 6, B + 9, B + 42]
    and wr[0]["top"][0]["scientific"] == "Poecile carolinensis"
    and wr[0]["top"][0]["confidence"] == 0.7
    and len(wr[0]["top"]) == 1
    and wr[0]["top"][0]["group"] == "bird"
    and wr[0]["top"][0]["in_region"] is None,
    str(wr[:1]),
)
txt = json.dumps(recs)
check(
    "speech window: the Barn Owl answer of the dropped window appears in no record (0 occurrences of 'Barn Owl' and 'Tyto')",
    "Barn Owl" not in txt and "Tyto" not in txt,
)
check(
    "human labels: 'Human vocal' appears in no record, counted as dropped (mix side 1)", "uman" not in txt and pl.stats()["human_dropped"] == 1, str(pl.stats())
)
check("scenario: no record carries audio-like or embedding keys", not any(k in r for r in recs for k in ("audio", "emb", "embedding", "samples", "lat", "lon")))
check("scenario: the scorer was called for every window above the gate (6 calls)", len(rs.calls) == 6, str(len(rs.calls)))

pl2, rs2_, recs2 = scenario(False)
c2 = next(r for r in recs2 if r["type"] == "mixscore_compare")
txt2 = json.dumps(recs2)
check(
    "twin without speech flags: the same window is kept (scored 6, paired 5, dropped_speech 0) and the owl is reported by both sides",
    c2["windows"] == {"gated": 1, "scored": 6, "paired": 5, "unpaired": 1, "dropped_speech": 0, "dropped_unknown": 0}
    and c2["events"] == {"both": 3, "beam_only": 2, "mix_only": 1}
    and "Barn Owl" in txt2,
    str((c2["windows"], c2["events"])),
)
check("twin: the Barn Owl is the third shared event (group bird both 3)", c2["groups"]["bird"]["both"] == 3 and c2["both_conf"]["n"] == 3, str(c2["groups"]))

pl3, rs3, recs3 = scenario(False, with_tags=False)
recs3 += pl3.flush()
c3 = [r for r in recs3 if r["type"] == "mixscore_compare"]
check(
    "fail closed: with no tags report at all every scored window is dropped as speech-unknown and nothing is recorded by species",
    len(c3) == 1
    and c3[0]["windows"]["dropped_unknown"] == 6
    and c3[0]["windows"]["scored"] == 0
    and c3[0]["events"] == {"both": 0, "beam_only": 0, "mix_only": 0}
    and not [r for r in recs3 if r["type"] == "mixscore"],
    str(c3),
)
check("fail closed: nothing stays pending after flush", pl3.stats()["pending"] == 0)

# a speech flag from a tags window that ends AFTER the mix window still drops it (overlap in time), a clean tags window next to it does not
plx = M.MixScore(taxa=TAXA, scorer=Rec([[det(*CHICK, 0.7)]]))
plx.process(common_win(), B + 3.0)
rx = plx.observe("birds", [brec(CHICK, 0.6, B + 3.0)], B + 3.0)
for tt in (2, 4, 6, 8):  # tags windows [-3,2] .. [3,8]; only the one ending at 4 is speech
    rx += plx.observe("tags", [{"k": "tags", "speech_active": tt == 4}], B + tt)
check(
    "one speech tags window ending 1 s after the mix window drops it at once (no record), before coverage is complete",
    rx == [] and plx.stats()["pending"] == 0,
    str(rx),
)
rx += plx.flush()
cx = [r for r in rx if r["type"] == "mixscore_compare"]
check(
    "that window is counted dropped_speech 1 and scored 0",
    len(cx) == 1 and cx[0]["windows"]["dropped_speech"] == 1 and cx[0]["windows"]["scored"] == 0,
    str(cx),
)
plc = M.MixScore(taxa=TAXA, scorer=Rec([[det(*CHICK, 0.7)]]))
plc.process(common_win(), B + 3.0)
rc = plc.observe("birds", [brec(CHICK, 0.6, B + 3.0)], B + 3.0)
for tt in (2, 4, 6, 8):
    rc += plc.observe("tags", [{"k": "tags", "speech_active": False}], B + tt)
rc += plc.flush()
cc = [r for r in rc if r["type"] == "mixscore_compare"]
check(
    "twin: the same sequence without the speech flag is kept (scored 1, both 1)",
    len(cc) == 1 and cc[0]["windows"]["scored"] == 1 and cc[0]["events"]["both"] == 1,
    str(cc),
)

# a tie (mix confidence equal to the best-beam confidence) is not "mix higher"; a mix 0.01 above is
pt = M.MixScore(taxa=TAXA, scorer=Rec([[det(*CHICK, 0.6)], [det(*TIT, 0.51)]]))
pt.process(common_win(), B + 3.0)
pt.process(common_win(), B + 6.0)
rt = pt.observe("birds", [brec(CHICK, 0.6, B + 3.0)], B + 3.0) + pt.observe("birds", [brec(TIT, 0.5, B + 6.0)], B + 6.0)
for tt in range(2, 16, 2):
    rt += pt.observe("tags", [{"k": "tags", "speech_active": False}], B + tt)
rt += pt.flush()
ct = next(r for r in rt if r["type"] == "mixscore_compare")
check(
    "a tie is not counted as mix higher (0.60 vs 0.60 and 0.51 vs 0.50: n 2, mix_higher 1, diff_mean 0.005)",
    ct["both_conf"] == {"n": 2, "best_beam_mean": 0.55, "mix_mean": 0.555, "diff_mean": 0.005, "mix_higher": 1},
    str(ct["both_conf"]),
)

# human labels on the per-beam side are dropped too
plh = M.MixScore(taxa=TAXA, scorer=Rec([[det(*CHICK, 0.7)]]))
plh.process(common_win(), B + 3.0)
rh = plh.observe(
    "birds",
    [brec(CHICK, 0.6, B + 3.0), {"k": "bird", "t": B + 3.0, "common": "Human vocal", "scientific": "Human vocal", "confidence": 0.9, "group": "mechanical"}],
    B + 3.0,
)
for tt in range(2, 12, 2):
    rh += plh.observe("tags", [{"k": "tags", "speech_active": False}], B + tt)
rh += plh.flush()
ch = next(r for r in rh if r["type"] == "mixscore_compare")
check(
    "per-beam human label dropped: events both 1, nothing else, counted",
    ch["events"] == {"both": 1, "beam_only": 0, "mix_only": 0} and "mechanical" not in json.dumps(ch) and plh.stats()["human_dropped"] == 1,
    str(ch["events"]),
)

# --mixscore-every: the cadence knob keeps the 3 s window and the grid, only the hop grows
check(
    "every=1 is the birds cadence (win 3 s, hop 3 s); every=4 keeps the 3 s window and hops 12 s",
    (M.MixScore(taxa=TAXA, scorer=Rec()).win_s, M.MixScore(taxa=TAXA, scorer=Rec()).hop_s) == (3.0, 3.0)
    and (M.MixScore(taxa=TAXA, scorer=Rec(), every=4).win_s, M.MixScore(taxa=TAXA, scorer=Rec(), every=4).hop_s) == (3.0, 12.0),
)
check(
    "every=0 and every=1.5 are refused",
    raises(lambda: M.MixScore(taxa=TAXA, scorer=Rec(), every=0), ValueError, "every must be an integer >= 1")
    and raises(lambda: M.MixScore(taxa=TAXA, scorer=Rec(), every=1.5), ValueError, "got 1.5"),
)

# dependencies
check("require(): refuses without tags (message names it)", raises(lambda: M.require({"birds", "dsp"}), RuntimeError, "tags"))
check("require(): refuses without birds (message names it)", raises(lambda: M.require({"tags"}), RuntimeError, "birds"))
check("require(): twin, with tags and birds it returns", M.require({"tags", "birds", "dsp"}) is None)

# observe never raises into the window loop
plb = M.MixScore(taxa=TAXA, scorer=Rec())
rb = plb.observe("birds", [{"k": "bird", "confidence": "not a number", "scientific": "X y", "common": "X"}], B + 3)
check(
    "observe swallows a malformed record, returns no records and counts the error",
    rb == [] and plb.stats()["errors"] == 1 and "ValueError" in plb.stats()["last_error"],
    str(plb.stats()),
)
rb2 = plb.observe("birds", [brec(CHICK, 0.6, B + 6)], B + 6)
check("twin: a well-formed record afterwards is accepted (errors stay 1)", rb2 == [] and plb.stats()["errors"] == 1 and len(plb.birds_obs) == 1)

# a failing scorer raises from process (the host counts it as the plugin's error and carries on)


def boom(a, w):
    raise RuntimeError("model blew up")


check(
    "a failing scorer raises out of process() so the host counts a plugin error",
    raises(lambda: M.MixScore(taxa=TAXA, scorer=boom).process(common_win(), B + 3), RuntimeError, "model blew up"),
)


# ============================================================ reviewer round: branches the tests above left unpinned
def one_window(answer, tags, birds=None, t=3.0, gate_dbfs=-70.0):
    """one mix window ending at B+t, scored from `answer`; tags = [(end offset, speech)] delivered in order, then flush.
    Returns (records, compare record, plugin)."""
    pl_ = M.MixScore(taxa=TAXA, scorer=Rec([answer]), gate_dbfs=gate_dbfs)
    pl_.process(common_win(), B + t)
    rr_ = pl_.observe("birds", birds if birds is not None else [], B + t)
    for tt_, sp_ in tags:
        rr_ += pl_.observe("tags", [{"k": "tags", "speech_active": sp_}], B + tt_)
    rr_ += pl_.flush()
    return rr_, next(r for r in rr_ if r["type"] == "mixscore_compare"), pl_


# speech rule, coverage: the tags windows seen must cover the whole mix window [t-3, t]; a hole (tags skipped ahead under load) drops it
HOLE_TAGS = [(95, False), (100, False), (110, False), (112, False), (114, False)]  # windows [90,95] [95,100] [105,110] ...: [100,105] missing
FULL_TAGS = [(95, False), (100, False), (105, False), (110, False), (112, False), (114, False)]
r_hole, c_hole, _ = one_window([det(*OWL, 0.9)], HOLE_TAGS, t=106.0)
check(
    "speech rule, coverage hole: tags windows ending 95,100,110,112,114 leave [100,105] open, the mix window [103,106] is dropped as dropped_unknown, no record, no species",
    c_hole["windows"] == {"gated": 0, "scored": 0, "paired": 0, "unpaired": 0, "dropped_speech": 0, "dropped_unknown": 1}
    and not [r for r in r_hole if r["type"] == "mixscore"]
    and "Barn Owl" not in json.dumps(r_hole),
    str(c_hole["windows"]),
)
r_full, c_full, _ = one_window([det(*OWL, 0.9)], FULL_TAGS, t=106.0)
check(
    "twin: tags windows ending 95,100,105,110,112,114 cover [103,106], the same window is kept (scored 1, dropped_unknown 0, one record naming the owl)",
    c_full["windows"] == {"gated": 0, "scored": 1, "paired": 1, "unpaired": 0, "dropped_speech": 0, "dropped_unknown": 0}
    and [r["top"][0]["common"] for r in r_full if r["type"] == "mixscore"] == ["Barn Owl"],
    str(c_full["windows"]),
)

# speech rule, overlap edges: a tags window only touching the mix window ([lo-5, lo] or [t, t+5]) is no overlap; one reaching 0.5 s in is
EDGE_OK = [(2, False), (4, False), (6, False), (8, False)]
_, c_lo, _ = one_window([det(*CHICK, 0.7)], [(0, True)] + EDGE_OK)
check(
    "overlap edge: a speech tags window ending exactly where the mix window [0,3] starts only touches it: kept (scored 1, dropped_speech 0)",
    c_lo["windows"]["scored"] == 1 and c_lo["windows"]["dropped_speech"] == 0,
    str(c_lo["windows"]),
)
_, c_lo2, _ = one_window([det(*CHICK, 0.7)], [(0.5, True)] + EDGE_OK)
check(
    "twin: the same speech window ending 0.5 s inside the mix window drops it (scored 0, dropped_speech 1)",
    c_lo2["windows"]["scored"] == 0 and c_lo2["windows"]["dropped_speech"] == 1,
    str(c_lo2["windows"]),
)
_, c_hi, _ = one_window([det(*CHICK, 0.7)], [(2, False), (4, False), (6, False), (8, True)])
check(
    "overlap edge: a speech tags window [3,8] starting exactly where the mix window [0,3] ends only touches it: kept (scored 1, dropped_speech 0)",
    c_hi["windows"]["scored"] == 1 and c_hi["windows"]["dropped_speech"] == 0,
    str(c_hi["windows"]),
)
_, c_hi2, _ = one_window([det(*CHICK, 0.7)], [(2, False), (4, False), (6, False), (7.5, True), (8, False)])
check(
    "twin: a speech window [2.5,7.5] starting 0.5 s inside drops it (scored 0, dropped_speech 1)",
    c_hi2["windows"]["scored"] == 0 and c_hi2["windows"]["dropped_speech"] == 1,
    str(c_hi2["windows"]),
)

# gate boundary: the loudest beam exactly at the gate is scored (only lvl < gate is gated), a hair above the gate level is gated
xg = np.full((N, 8), 0.01, np.float32)
lvl_g = 10.0 * math.log10(float((xg.astype(np.float64) ** 2).mean(axis=0).max()))
pg_eq, pg_up = M.MixScore(taxa=TAXA, scorer=Rec(), gate_dbfs=lvl_g), M.MixScore(taxa=TAXA, scorer=Rec(), gate_dbfs=lvl_g + 1e-6)
pg_eq.process(xg, B + 3.0)
pg_up.process(xg, B + 3.0)
gate_seen = (len(pg_eq.scorer.calls), pg_eq.stats()["gated"], len(pg_up.scorer.calls), pg_up.stats()["gated"])
check(
    "gate boundary: a loudest beam exactly at the gate level is scored (1 call, 0 gated); a gate 1e-6 dB above it gates the same window (0 calls, 1 gated)",
    gate_seen == (1, 0, 0, 1),
    str(gate_seen),
)

# TOP_N: the window record lists at most 5 species, strongest first
six = [det(f"Bird {i}", f"Genus sp{i}", round(0.9 - 0.1 * i, 2)) for i in range(6)]  # 0.9 .. 0.4
t_ok = [(2, False), (4, False), (6, False), (8, False)]
r6, _, _ = one_window(six, t_ok)
w6 = [r for r in r6 if r["type"] == "mixscore"]
check(
    "six species in one window: the record keeps the five strongest in order (0.9 .. 0.5), the sixth (Bird 5, 0.4) is in no record",
    len(w6) == 1 and [d["common"] for d in w6[0]["top"]] == ["Bird 0", "Bird 1", "Bird 2", "Bird 3", "Bird 4"] and "Bird 5" not in json.dumps(r6),
    str(w6),
)
r5, _, _ = one_window(six[:5], t_ok)
check(
    "twin: five species are all kept",
    [d["common"] for d in next(r for r in r5 if r["type"] == "mixscore")["top"]] == ["Bird 0", "Bird 1", "Bird 2", "Bird 3", "Bird 4"],
)

# a human label given only in the scientific field (the common name is innocuous) is dropped on both sides
rhs, _, plhs = one_window([det("Whistler", "Human whistle", 0.9), det(*CHICK, 0.7)], t_ok)
whs = next(r for r in rhs if r["type"] == "mixscore")
check(
    "mix side: a label whose scientific name is 'Human whistle' is dropped and counted although the common name is 'Whistler'",
    [d["common"] for d in whs["top"]] == ["Carolina Chickadee"]
    and "Whistler" not in json.dumps(rhs)
    and "uman" not in json.dumps(rhs)
    and plhs.stats()["human_dropped"] == 1,
    str((whs, plhs.stats())),
)
rhk, _, plhk = one_window([det("Whistling Duck", "Dendrocygna sp", 0.9), det(*CHICK, 0.7)], t_ok)
check(
    "twin: a label starting 'Whistling' (not 'Human ') is kept, nothing counted",
    [d["common"] for d in next(r for r in rhk if r["type"] == "mixscore")["top"]] == ["Whistling Duck", "Carolina Chickadee"]
    and plhk.stats()["human_dropped"] == 0,
)
plhb = M.MixScore(taxa=TAXA, scorer=Rec())
plhb.observe("birds", [{"k": "bird", "common": "Speech", "scientific": "Human vocal", "confidence": 0.9}], B + 3.0)
plhb.observe("birds", [{"k": "bird", "common": "Speech", "scientific": "Poecile carolinensis", "confidence": 0.9}], B + 6.0)
check(
    "per-beam side: scientific 'Human vocal' is dropped and counted (window 1 has no species), the same common name with a bird scientific name is kept (window 2)",
    [(t_, sorted(sp_)) for t_, sp_ in plhb.birds_obs] == [(B + 3.0, []), (B + 6.0, ["Poecile carolinensis"])] and plhb.stats()["human_dropped"] == 1,
    str((plhb.birds_obs, plhb.stats())),
)

# partial minute: a minute flushed at shutdown before its grace ran out is marked partial, one emitted after the grace is not
_, c_part, _ = one_window([det(*CHICK, 0.7)], t_ok)
check("flush at shutdown before the minute's grace: the compare record has partial true", c_part.get("partial") is True, str(c_part))
pl_full = M.MixScore(taxa=TAXA, scorer=Rec([[det(*CHICK, 0.7)]]))
pl_full.process(common_win(), B + 3.0)
rfull = pl_full.observe("birds", [], B + 3.0)
for tt in (2, 4, 6, 8):
    rfull += pl_full.observe("tags", [{"k": "tags", "speech_active": False}], B + tt)
rfull += pl_full.observe("dsp", [], B + 106.0)  # the clock passes minute end + 45 s: emitted by observe, not flush
cfull = [r for r in rfull if r["type"] == "mixscore_compare"]
check(
    "twin: the minute emitted after its grace has no partial key (and nothing is left for flush)",
    len(cfull) == 1 and "partial" not in cfull[0] and pl_full.flush() == [],
    str(cfull),
)

# an all-zero minute emits no record, a minute with one gated window does
pl_empty = M.MixScore(taxa=TAXA, scorer=Rec())
check("an all-zero minute bucket produces no compare record", pl_empty._compare(B, pl_empty._bucket(B), False) is None)
_, c_gated, _ = one_window([], [], t=3.0, gate_dbfs=-10.0)
check(
    "twin: a minute whose only content is one gated window is emitted (windows.gated 1, all else 0)",
    c_gated["windows"] == {"gated": 1, "scored": 0, "paired": 0, "unpaired": 0, "dropped_speech": 0, "dropped_unknown": 0},
    str(c_gated["windows"]),
)

# measured cost for status.json (the thread CPU clock is the only substitute: a scripted clock gives exact figures)
_clock = iter([100.0, 100.06, 200.0, 200.09])  # two process() calls: 60 ms and 90 ms of CPU
M.time = types.SimpleNamespace(thread_time=lambda: next(_clock))
try:
    pl_cost = M.MixScore(taxa=TAXA, scorer=Rec(), every=4)
    cost0 = pl_cost.cost()
    pl_cost.process(common_win(), B + 3.0)
    pl_cost.process(common_win(), B + 15.0)
    cost2 = pl_cost.cost()
finally:
    M.time = time
check(
    "cost: before any window only the cadence (every 4); after two calls of 60 and 90 ms: 75.0 ms per window, 6.25 ms per second of audio (hop 12 s)",
    cost0 == {"every": 4} and cost2 == {"every": 4, "cpu_ms_per_window": 75.0, "cpu_ms_per_audio_s": 6.25},
    str((cost0, cost2)),
)


def _boom_win(a, w):
    raise RuntimeError("model blew up")


pl_cb = M.MixScore(taxa=TAXA, scorer=_boom_win)
raised_cb = raises(lambda: pl_cb.process(common_win(), B + 3.0), RuntimeError, "model blew up")
check(
    "cost: a failing scorer call is still a measured call (it raised, cpu_ms_per_window is present, 1 error counted)",
    raised_cb and "cpu_ms_per_window" in pl_cb.cost() and pl_cb.stats()["errors"] == 1,
    str((raised_cb, pl_cb.cost(), pl_cb.stats())),
)

# ============================================================ report
tmp = tempfile.mkdtemp(prefix="mixrep_")
with open(os.path.join(tmp, "mixscore.jsonl"), "w", newline="\n") as f:
    for r in recs + recs2 + [{"type": "mixscore_compare", "v": 2, "t": B}]:
        f.write(json.dumps(r) + "\n")
    f.write("not json\n")
shifted = dict(c, t=B + 86400.0)
with open(os.path.join(tmp, "mixscore.jsonl.1"), "w", newline="\n") as f:
    f.write(json.dumps(shifted) + "\n")
rec_all, bad = R.load([tmp])
check(
    "report: loads 3 compare records, skips the v2 record and the window records, counts 1 unreadable line",
    len(rec_all) == 3 and bad == 1,
    f"{len(rec_all)} {bad}",
)
s = R.summarize(rec_all)
check(
    "report: summed events both 7, only per-beam 6, only mix 3; shared-event mean diff 0.0 weighted over 7",
    s["events"] == {"both": 7, "beam_only": 6, "mix_only": 3} and s["both_conf"]["n"] == 7 and s["minutes"] == 3,
    str(s["events"]),
)
rec_d1, _ = R.load([tmp], R.parse_day("2026-12-01"), R.parse_day("2026-12-01"))
d_first = time.strftime("%Y-%m-%d", time.gmtime(B))
r_one, _ = R.load([tmp], R.parse_day(d_first), R.parse_day(d_first))
check(
    "report: the date range keeps only that UTC day (2 records on the first day, the +1 day record excluded; twin: other day alone gives 1)",
    len(r_one) == 2 and len(R.load([tmp], R.parse_day(time.strftime("%Y-%m-%d", time.gmtime(B + 86400))), None)[0]) == 1 and rec_d1 == [],
)
before = sorted((n, os.path.getsize(os.path.join(tmp, n))) for n in os.listdir(tmp))
pr = subprocess.run([sys.executable, "-I", os.path.join(HERE, "mixscore_report.py"), tmp, "--json"], capture_output=True, text=True)
after = sorted((n, os.path.getsize(os.path.join(tmp, n))) for n in os.listdir(tmp))
js = json.loads(pr.stdout) if pr.returncode == 0 else {}
check(
    "report CLI: exit 0, JSON summary equals summarize(), read-only (directory unchanged)",
    pr.returncode == 0 and js["events"] == s["events"] and before == after,
    pr.stderr[-200:],
)
pe = subprocess.run([sys.executable, "-I", os.path.join(HERE, "mixscore_report.py"), tmp, "--from", "2030-01-01"], capture_output=True, text=True)
check(
    "report CLI: an empty range exits 1 with a message, bad date exits 2",
    pe.returncode == 1
    and "no mixscore_compare records" in pe.stderr
    and subprocess.run([sys.executable, "-I", os.path.join(HERE, "mixscore_report.py"), tmp, "--from", "2026-13-40"], capture_output=True, text=True).returncode
    == 2,
)
tx = subprocess.run([sys.executable, "-I", os.path.join(HERE, "mixscore_report.py"), tmp], capture_output=True, text=True).stdout
check(
    "report CLI text: states the event counts and per-beam share (6 of 13 per-beam events are only per-beam: 0.4615)",
    "both 7, only per-beam 6, only mix 3" in tx and "0.4615" in tx,
    tx,
)

# deploy pins: the live installer and the image must carry the module the host imports (a deploy without it must not be possible)
pin_mf = json.load(open(os.path.join(HERE, "plugins.manifest.json"), encoding="utf-8"))
pin_dep = {t_: {f_ for e_ in pin_mf["plugins"] if t_ in e_["deploy"] for f_ in e_["files"]} for t_ in ("pc", "image")}
check(
    "pins: plugins.manifest.json (the one list the installer and the Dockerfile install from; docker/test_install_lists.py checks them against it) deploys mixscore.py to the PC and the image (positive twin: a made-up module is not deployed)",
    "mixscore.py" in pin_dep["pc"] and "mixscore.py" in pin_dep["image"] and "nosuchplugin.py" not in pin_dep["pc"] | pin_dep["image"],
)

import manifest_harness as _G  # the (source, destination) pairs the installer and the Dockerfile really produce

try:
    _pc_dst, _img_dst = {d_ for _s, d_ in _G.ps1_pairs()}, {d_.rsplit("/", 1)[-1] for _s, d_ in _G.docker_pairs()}
except _G.NoPowerShell as e_:  # WSL / Linux: the same effective pairs are asserted by docker/test_install_lists.py under Windows PowerShell
    print(f"SKIP pins (effective)  [{e_}; docker/test_install_lists.py runs the same pairs where PowerShell exists]")
else:
    check(
        "pins (effective): pc/install_task.ps1 really copies mixscore.py and analyzer.Dockerfile really installs it (positive twin: nosuchplugin.py is in neither)",
        "mixscore.py" in _pc_dst and "mixscore.py" in _img_dst and "nosuchplugin.py" not in _pc_dst | _img_dst,
        ("mixscore.py" in _pc_dst, "mixscore.py" in _img_dst),
    )

pr3_dir = tempfile.mkdtemp(prefix="mixmiss_")
for f_ in os.listdir(HERE):
    if f_.endswith(".py") and f_ != "mixscore.py":
        shutil.copy(os.path.join(HERE, f_), pr3_dir)
shutil.copy(os.path.join(HERE, "plugins.manifest.json"), pr3_dir)  # the installed app dir holds the manifest next to host.py
shutil.copy(os.path.join(BMA, "ui", "taxa.json"), pr3_dir)
od3 = tempfile.mkdtemp(prefix="mixmisso_")
pr3 = subprocess.run(
    [
        sys.executable,
        "-I",
        os.path.join(pr3_dir, "host.py"),
        "--listen",
        "127.0.0.1:48394",
        "--out",
        od3,
        "--history-dir",
        "",
        "--no",
        "tags",
        "--no",
        "clap",
        "--no",
        "birds",
        "--mixscore",
        "--secs",
        "3",
        "--classify-dir",
        os.path.join(BMA, "classify"),
    ],
    capture_output=True,
    text=True,
    timeout=90,
)
_sp3 = os.path.join(od3, "status.json")
assert os.path.exists(_sp3), "host produced no status.json: " + pr3.stderr[-600:]
st3 = json.load(open(_sp3))
check(
    "a deploy that lacks mixscore.py: --mixscore is disabled with ModuleNotFoundError in status.json and the analyzer still runs (exit 0, dsp enabled)",
    pr3.returncode == 0
    and st3["plugins"]["mixscore"]["enabled"] is False
    and "ModuleNotFoundError" in st3["plugins"]["mixscore"]["reason"]
    and st3["plugins"]["dsp"]["enabled"] is True,
    str((pr3.stderr[-200:], st3["plugins"].get("mixscore"))),
)

if "--unit-only" in sys.argv:  # fast loop for mutation runs: everything above, none of the host processes below
    print(f"{n_checks} checks (unit only), {len(fails)} failure(s)")
    sys.exit(1 if fails else 0)

# ============================================================ host: default off, flags
port = 48392


def run_host(extra, secs=4):
    od, hd = tempfile.mkdtemp(prefix="mixh_"), tempfile.mkdtemp(prefix="mixhh_")
    pr_ = subprocess.run(
        [
            sys.executable,
            os.path.join(HERE, "host.py"),
            "--listen",
            f"127.0.0.1:{port}",
            "--out",
            od,
            "--history-dir",
            hd,
            "--no",
            "tags",
            "--no",
            "clap",
            "--no",
            "birds",
            "--secs",
            str(secs),
        ]
        + extra,
        capture_output=True,
        text=True,
        timeout=90,
    )
    st_ = json.load(open(os.path.join(od, "status.json")))
    return pr_, st_, sorted(os.listdir(od))


pr0, st0, files0 = run_host([])
check(
    "host default: mixscore is disabled with the reason 'off by default', no mixscore.jsonl, no meta counter",
    pr0.returncode == 0
    and st0["plugins"]["mixscore"] == {"enabled": False, "reason": "off by default (--mixscore)"}
    and "mixscore.jsonl" not in files0
    and "mixscore_errors" not in st0["meta"],
    str((pr0.stderr[-200:], st0["plugins"].get("mixscore"), files0)),
)
pr1, st1, files1 = run_host(["--mixscore"])
check(
    "host --mixscore without tags and birds: disabled with the dependency reason, the analyzer runs on, no mixscore.jsonl",
    pr1.returncode == 0
    and st1["plugins"]["mixscore"]["enabled"] is False
    and "RuntimeError: mixscore needs the running plugins: tags, birds" in st1["plugins"]["mixscore"]["reason"]
    and st1["plugins"]["dsp"]["enabled"] is True
    and "mixscore.jsonl" not in files1,
    str(st1["plugins"].get("mixscore")),
)
pr2, st2, files2 = run_host(["--mixscore", "--no", "mixscore"])
check(
    "host --mixscore --no mixscore: --no wins ('disabled by --no')",
    st2["plugins"]["mixscore"] == {"enabled": False, "reason": "disabled by --no"},
    str(st2["plugins"].get("mixscore")),
)

# ============================================================ host end to end with the plugin ON (neighbour plugins and the model are stand-ins)
# A temp copy of the analysis directory with a stand-in plugins.py (Tagger: speech when the loudest beam is above -30 dBFS; birds: one
# Carolina Chickadee 0.40 per window above the gate) and a bma_classify.py whose BirdNet is a stand-in model (`good`: Chickadee 0.60 for
# every window; `broken`: raises). The host, mixscore.py, hist.py, taxa and the UDP path are the real code.
STUB_PLUGINS = """
import math
import numpy as np
import taxa as _t
class _Inline:
    heavy = False
    win_s, hop_s = 1.0, 1.0
    def process(self, x, t): return [], None
class DSPMonitor(_Inline):
    name = "dsp"
    def __init__(self, *a): pass
class Spatial(_Inline):
    name = "spatial"
class ClapZeroShot(_Inline):
    name = "clap"
    heavy = True
    def __init__(self, *a): pass
def _lvl(x):
    return 10 * math.log10(max(float((np.asarray(x, dtype=np.float64) ** 2).mean(axis=0).max()), 1e-24))
class Tagger:
    name, win_s, hop_s, heavy = "tags", 5.0, 2.0, True
    def __init__(self, *a): pass
    def process(self, x, t):
        return [{"k": "tags", "t": t, "speech_active": _lvl(x) > -30.0, "top": []}], None
class MultiBeamBirds:
    name, win_s, hop_s, heavy = "birds", 3.0, 3.0, True
    def __init__(self, classify_dir, min_conf=0.25, gate_dbfs=-70.0, geo_config=None, taxa=None):
        self.taxa, self.gate, self.window_info = taxa or _t.Taxa(), gate_dbfs, {}
    def process(self, x, t):
        if _lvl(x) < self.gate:
            return [], None
        g, _ = self.taxa.lookup("Poecile carolinensis", "Carolina Chickadee")
        return [{"k": "bird", "t": t, "beam": 1, "common": "Carolina Chickadee", "scientific": "Poecile carolinensis", "confidence": 0.4, "group": g}], None
class EmbeddingStore:
    def __init__(self, d): self.n = 0
    def add(self, *a): self.n += 1
class Novelty:
    def __init__(self, *a, **k): self.mv = "stub"
    def add(self, e, t): return []
    def save(self): pass
"""
STUB_BIRDNET = """

class BirdNet:
    broken = %s
    def __init__(self, *a, **k):
        self.filtered = []
    def classify(self, audio, when=None):
        if self.broken:
            raise RuntimeError("stand-in model failure")
        return [{"common": "Carolina Chickadee", "scientific": "Poecile carolinensis", "confidence": 0.6, "start_s": 0.0, "end_s": 3.0}]
"""


def bmaa_pkt(slot, block, x):
    s24 = np.clip((x * 8388608.0).astype(np.int64), -8388608, 8388607).astype("<i4")
    return b"BMAA" + bytes([1, 0, slot, 0]) + struct.pack("<III", 0, block & 0xFFFFFFFF, len(x)) + s24.tobytes()


def stub_tree(model_broken):
    """temp copy of the analysis dir with the stand-in plugins.py and a stand-in BirdNet; returns (work, app dir, classify dir)"""
    work = tempfile.mkdtemp(prefix="mixe2e_")
    ana, cls = os.path.join(work, "app"), os.path.join(work, "classify")
    os.makedirs(ana)
    os.makedirs(cls)
    for f in os.listdir(HERE):
        if f.endswith(".py") and f != "plugins.py":
            shutil.copy(os.path.join(HERE, f), ana)
    shutil.copy(os.path.join(HERE, "plugins.manifest.json"), ana)  # the installed app dir holds the manifest next to host.py
    shutil.copy(os.path.join(BMA, "ui", "taxa.json"), ana)
    shutil.copy(os.path.join(HERE, "clip_exclude.json"), ana)  # host.py imports the clip capture, which reads it at start
    with open(os.path.join(ana, "plugins.py"), "w", newline="\n") as f:
        f.write(STUB_PLUGINS)
    with open(os.path.join(BMA, "classify", "bma_classify.py"), "rb") as f:
        real_src = f.read()
    with open(os.path.join(cls, "bma_classify.py"), "wb") as f:
        f.write(real_src + (STUB_BIRDNET % ("True" if model_broken else "False")).encode())
    shutil.copy(os.path.join(BMA, "classify", "birdnet_coverage.json"), cls)
    return work, ana, cls


# ============================================================ host: pool size, cadence default, cost in status, warning
def host_status(extra, secs=3):
    work, ana, cls = stub_tree(False)
    od = os.path.join(work, "out")
    pr_ = subprocess.run(
        [
            sys.executable,
            "-I",
            os.path.join(ana, "host.py"),
            "--listen",
            "127.0.0.1:48395",
            "--out",
            od,
            "--history-dir",
            "",
            "--classify-dir",
            cls,
            "--no",
            "coherence",
            "--secs",
            str(secs),
        ]
        + extra,
        capture_output=True,
        text=True,
        timeout=90,
    )
    st_ = json.load(open(os.path.join(od, "status.json")))
    shutil.rmtree(work, ignore_errors=True)
    return pr_, st_


prP0, stP0 = host_status([])
prP1, stP1 = host_status(["--mixscore"])
check(
    "pool: without mixscore the four heavy plugins (tags, clap, birds, indices) get 4 workers and mixscore is off",
    prP0.returncode == 0 and stP0["pool_workers"] == 4 and stP0["plugins"]["mixscore"]["enabled"] is False,
    str((prP0.stderr[-200:], stP0.get("pool_workers"))),
)
check(
    "pool: with mixscore the five heavy plugins (tags, clap, birds, indices, mixscore) get 5 workers, so none queues behind another",
    prP1.returncode == 0 and stP1["plugins"]["mixscore"]["enabled"] is True and stP1["pool_workers"] == 5,
    str((prP1.stderr[-200:], stP1.get("pool_workers"), stP1["plugins"].get("mixscore"))),
)
check(
    "cadence: --mixscore alone runs every 4th window (status plugins.mixscore.every 4) and prints no cost warning",
    stP1["plugins"]["mixscore"].get("every") == 4 and "WARN: --mixscore-every" not in prP1.stderr,
    str((stP1["plugins"]["mixscore"], prP1.stderr[-200:])),
)
prP2, stP2 = host_status(["--mixscore", "--mixscore-every", "1"])
check(
    "twin: --mixscore-every 1 is honoured (every 1, hop is the birds hop) and prints the CPU budget warning once",
    stP2["plugins"]["mixscore"].get("every") == 1 and prP2.stderr.count("WARN: --mixscore-every 1: the analyzer CPU budget") == 1,
    str((stP2["plugins"]["mixscore"], prP2.stderr[-300:])),
)

if "--pool-only" in sys.argv:  # fast loop for mutation runs of the host flags
    print(f"{n_checks} checks (pool only), {len(fails)} failure(s)")
    sys.exit(1 if fails else 0)


def host_with_mixscore(model_broken, secs=58, feed_s=50):
    work, ana, cls = stub_tree(model_broken)
    od, hd = os.path.join(work, "out"), os.path.join(work, "hist")
    prt = 48393
    pr_ = subprocess.Popen(
        [
            sys.executable,
            "-I",
            os.path.join(ana, "host.py"),
            "--listen",
            f"127.0.0.1:{prt}",
            "--out",
            od,
            "--history-dir",
            hd,
            "--classify-dir",
            cls,
            "--no",
            "clap",
            "--no",
            "coherence",
            "--mixscore",
            "--mixscore-every",
            "1",
            "--secs",
            str(secs),
        ],
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(2.0)
    sk_ = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    r2 = np.random.default_rng(11)
    blk_, t0, nb = 0, time.time(), 0
    for sec in range(feed_s):
        lvl = -20 if 8 <= sec < 12 else -40  # seconds 8-11 are loud: the stand-in tagger calls them speech
        xs = np.repeat((r2.standard_normal((FS, 1)) * 10 ** (lvl / 20)).astype(np.float32), 8, axis=1)
        for off in range(0, FS, 256):
            for slot in range(8):
                sk_.sendto(bmaa_pkt(slot, blk_ + off, xs[off : off + 256, slot]), ("127.0.0.1", prt))
            nb += 1
            d_ = t0 + nb * 256 / FS - time.time()
            if d_ > 0:
                time.sleep(d_)
        blk_ += FS
    pr_.wait(timeout=90)
    err_ = pr_.stderr.read()
    st_ = json.load(open(os.path.join(od, "status.json")))
    path = os.path.join(od, "mixscore.jsonl")
    rr = [json.loads(l) for l in open(path, encoding="utf-8")] if os.path.exists(path) else []
    shutil.rmtree(work, ignore_errors=True)
    return pr_.returncode, err_, st_, rr


rcA, errA, stA, rrA = host_with_mixscore(False)
cmpA = [r for r in rrA if r["type"] == "mixscore_compare"]
winA = [r for r in rrA if r["type"] == "mixscore"]
wsum = lambda k: sum(r["windows"][k] for r in cmpA)
check(
    "host e2e mixscore ON: exits 0, status says enabled with 0 errors and the meta counter present and 0",
    rcA == 0 and stA["plugins"]["mixscore"]["enabled"] is True and stA["plugins"]["mixscore"]["errors"] == 0 and stA["meta"].get("mixscore_errors") == 0,
    str((rcA, errA[-300:], stA["plugins"].get("mixscore"))),
)
check(
    "host e2e: the other plugins ran as before (tags and birds windows > 0)",
    stA["plugins"]["tags"]["windows"] >= 8 and stA["plugins"]["birds"]["windows"] >= 6,
    str({k: v.get("windows") for k, v in stA["plugins"].items()}),
)
check(
    "host e2e: mixscore windows scored, records written through the host (>= 5 window records, every one a Chickadee 0.6)",
    len(winA) >= 5 and all(r["top"][0]["confidence"] == 0.6 and r["top"][0]["scientific"] == "Poecile carolinensis" for r in winA),
    str(len(winA)),
)
check(
    "host e2e: the speech seconds were dropped, not recorded (dropped_speech >= 1; every recorded window is below -30 dBFS)",
    wsum("dropped_speech") >= 1 and all(r["level_dbfs"] < -30 for r in winA),
    str((wsum("dropped_speech"), [r["level_dbfs"] for r in winA][:6])),
)
check(
    "host e2e: pairing through the host: every paired window is an event shared by both sides (both == paired >= 4, mix 0.60 vs best beam 0.40, mix higher every time, no only-sides)",
    sum(r["events"]["both"] for r in cmpA) == wsum("paired") >= 4
    and sum(r["events"]["beam_only"] + r["events"]["mix_only"] for r in cmpA) == 0
    and all(
        r["both_conf"]["n"] == 0
        or (r["both_conf"]["mix_mean"] == 0.6 and r["both_conf"]["best_beam_mean"] == 0.4 and r["both_conf"]["mix_higher"] == r["both_conf"]["n"])
        for r in cmpA
    ),
    str([(r["windows"], r["events"]) for r in cmpA][:3]),
)
check(
    "host e2e: window accounting adds up (paired + unpaired == scored == window records, since every kept window had a detection)",
    wsum("paired") + wsum("unpaired") == wsum("scored") == len(winA),
    str((wsum("paired"), wsum("unpaired"), wsum("scored"), len(winA))),
)
rcB, errB, stB, rrB = host_with_mixscore(True, secs=34, feed_s=30)
check(
    "host e2e with a failing model: errors counted in status and meta (>= 3), no mixscore record, the rest of the analyzer unaffected (exit 0, tags and birds keep running)",
    rcB == 0
    and stB["plugins"]["mixscore"]["errors"] >= 3
    and stB["meta"]["mixscore_errors"] >= 3
    and rrB == []
    and "stand-in model failure" in stB["plugins"]["mixscore"]["last_error"]
    and stB["plugins"]["birds"]["windows"] >= 3
    and stB["plugins"]["tags"]["windows"] >= 3
    and stB["plugins"]["birds"]["errors"] == 0,
    str((rcB, stB["plugins"].get("mixscore"), stB["meta"])),
)

# ============================================================ real BirdNET (opt-in)
if REAL:
    sys.path.insert(0, os.path.join(BMA, "classify"))
    sc = M.BirdNetScorer(os.path.join(BMA, "classify"), 0.05, None)
    import datetime

    when = datetime.datetime(2026, 6, 1, 6, 0, 0)
    t_ax = np.arange(N) / FS
    chirp = (np.sin(2 * np.pi * (2500 + 1200 * np.sin(2 * np.pi * 6 * t_ax)) * t_ax) * np.where((t_ax * 4) % 1 < 0.4, 0.1, 0.0)).astype(np.float32)
    noise_ = (rng.standard_normal(N) * 0.01).astype(np.float32)
    for nm, sig1 in (("chirp-train", chirp), ("white-noise", noise_)):
        beams = np.repeat(sig1[:, None], 8, axis=1)
        mixsig, gdb = M.mean_mix(beams)
        t0 = time.perf_counter()
        a_mix = sc(mixsig, when)
        dt_mix = (time.perf_counter() - t0) * 1000
        a_one = sc(beams[:, 0].copy(), when)
        check(f"real BirdNET, {nm}: identical beams -> the mix scores exactly like the single beam", a_mix == a_one and gdb == 0.0, str((a_mix[:1], a_one[:1])))
        print(f"  real BirdNET {nm}: {dt_mix:.0f} ms for one 3 s window; detections {[(d['common'], d['confidence']) for d in a_mix[0][:3]]}")
    diff_beams = np.stack([chirp * (0.5 + 0.1 * k) + (rng.standard_normal(N) * 0.01).astype(np.float32) for k in range(8)], axis=1)
    mixsig, gdb = M.mean_mix(diff_beams)
    print(
        f"  real BirdNET, 8 different beams (chirp + independent noise): mix gain {gdb:.2f} dB, mix {[(d['common'], d['confidence']) for d in sc(mixsig, when)[0][:3]]}, beam1 {[(d['common'], d['confidence']) for d in sc(diff_beams[:, 0].copy(), when)[0][:3]]}"
    )

print(f"{n_checks} checks, {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
