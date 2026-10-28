#!/usr/bin/env python3
"""Tests for analysis/pad_lane.py (tags_pad / clap_pad), tools/pad_lane_report.py and the host wiring (contract pad-lane 1.0.0).
The real plugin classes run; the only substitute is the model seam: a recording stub standing in for PANNs (`at.inference`) and for
the CLAP processor / audio tower. Expected values are derived here by hand. Windows Python has no torch: the CLAP checks run only
where torch imports (WSL audioeval venv) and print SKIP otherwise.   Run:  python -I test_pad_lane.py [--unit-only]"""

import io, json, math, os, subprocess, sys, tempfile, contextlib
import numpy as np
from scipy import signal

HERE = os.path.dirname(os.path.abspath(__file__))
BMA = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(BMA, "tools"))
import plugins as P  # noqa: E402
import pad_lane as PL  # noqa: E402
import pad_lane_report as RP  # noqa: E402

fails, n_checks, n_skip = [], 0, 0


def check(name, ok, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  [{str(detail)[:300]}]"))
    if not ok:
        fails.append(name)


def raises(fn, exc, match):
    try:
        fn()
    except exc as e:
        return match in str(e)
    return False


# ---------------------------------------------------------------- pad_zeros
check(
    "pad_zeros: [1,2,3] to 6 = [1,2,3,0,0,0] float32",
    PL.pad_zeros([1, 2, 3], 6).tolist() == [1, 2, 3, 0, 0, 0] and PL.pad_zeros([1, 2, 3], 6).dtype == np.float32,
)
check(
    "pad_zeros twin: input already 3 long, n 3 -> unchanged; n 2 -> NOT trimmed",
    PL.pad_zeros([1, 2, 3], 3).tolist() == [1, 2, 3] and PL.pad_zeros([1, 2, 3], 2).tolist() == [1, 2, 3],
)
check("pad_zeros: a 2-D input is refused", raises(lambda: PL.pad_zeros(np.zeros((3, 2)), 6), ValueError, "1-D"))
check(
    "pad seconds: 10 and 5 accepted (5 = the window), 4 refused with both numbers",
    PL.check_pad_s(10, 5.0) == 10.0
    and PL.check_pad_s(5, 5.0) == 5.0
    and raises(lambda: PL.check_pad_s(4, 5.0), ValueError, "pad seconds 4 must be >= the window 5.0"),
)

# ---------------------------------------------------------------- tags stub
LABELS = ["Speech", "Insect", "Dog", "Silence", "Bird", "Wind", "Rain"]


class AtStub:
    """Recording stand-in for panns_inference.AudioTagging: remembers every input, answers `probs`."""

    def __init__(self, probs):
        self.probs, self.calls = np.array(probs, dtype=np.float64), []  # float64: 0.30 is exactly SPEECH_P, so `>` and `>=` differ

    def inference(self, a):
        self.calls.append(np.array(a))
        return self.probs[None, :], np.zeros((1, 4), dtype=np.float32)


MODEL_T = {"name": "PANNs CNN14 (stub)", "version": "9.9", "variant": "best_beam_32k", "sha256": "ef" * 32}  # the production tagger's block
MODEL_C = {"name": "laion/clap (stub)", "version": "rev9", "variant": "best_beam_48k", "sha256": "12" * 32}


class Share:
    def __init__(self, at):
        self.labels, self.speech_idx, self.at, self.device = LABELS, [0], at, "cpu"
        self.model = dict(MODEL_T)


def window(loud_beam=3, n=5 * 48000, seed=1):
    rng = np.random.default_rng(seed)
    x = (rng.standard_normal((n, 8)) * 0.01).astype(np.float32)
    x[:, loud_beam] *= 20.0
    return x


X = window()
A32 = signal.resample_poly(X[:, 3].astype(np.float32), 2, 3).astype(np.float32)
PROBS = [0.10, 0.70, 0.2999, 0.06, 0.049, 0.02, 0.01]  # speech 0.10 (< 0.30); 0.049 is under the 0.05 floor
at = AtStub(PROBS)
pt = PL.PadTagger(None, "cpu", 10, share=Share(at))
recs, emb = pt.process(X, 1800000000.0)
r = recs[0]
check(
    "tags_pad: window 160000 samples at 32 kHz -> model input 320000 (10 s), shape (1, 320000); then the same window unpadded (1, 160000) for the speech gate",
    len(at.calls) == 2
    and at.calls[0].shape == (1, 320000)
    and at.calls[1].shape == (1, 160000)
    and np.array_equal(at.calls[1][0], A32)
    and A32.shape == (160000,),
    [c.shape for c in at.calls],
)
check("tags_pad: the first 160000 samples are the 2:3 resampled loudest beam (beam 4), exactly", np.array_equal(at.calls[0][0, :160000], A32))
check("tags_pad: the remaining 160000 samples are exactly zero", not np.any(at.calls[0][0, 160000:]) and at.calls[0][0, 160000:].shape == (160000,))
check(
    "tags_pad: record fields exact (top floor 0.05, rounded 3 d.p., variant pad10, pad_s 10.0, speech false, no embedding)",
    {k: r[k] for k in ("k", "t", "beam", "speech_p", "speech_active", "top", "emb_stored", "variant", "pad_s")}
    == {
        "k": "tags",
        "t": 1800000000.0,
        "beam": 4,
        "speech_p": 0.1,
        "speech_active": False,
        "top": [["Insect", 0.7], ["Dog", 0.3], ["Speech", 0.1], ["Silence", 0.06]],
        "emb_stored": False,
        "variant": "pad10",
        "pad_s": 10.0,
    }
    and isinstance(r["ms"], float)
    and r["ms"] >= 0
    and emb is None
    and len(recs) == 1,
    r,
)
check(
    "tags_pad: the record has exactly the keys k t beam speech_p speech_active top emb_stored variant pad_s ms",
    sorted(r)
    == sorted(["k", "t", "beam", "speech_p", "speech_active", "top", "emb_stored", "variant", "pad_s", "ms", "model", "claim", "top_k", "max_unstored_score"]),
    sorted(r),
)
SH_T = Share(AtStub(PROBS))
r_t = PL.PadTagger(None, "cpu", 10, share=SH_T).process(X, 1800000000.0)[0][0]
check(
    "tags_pad provenance: model = the production tagger's block (same name, version, sha256) with variant pad10; the shared production block itself is not changed",
    r_t["model"] == {"name": "PANNs CNN14 (stub)", "version": "9.9", "variant": "pad10", "sha256": "ef" * 32}
    and SH_T.model == MODEL_T
    and r_t["model"] is not SH_T.model,
    (r_t["model"], SH_T.model),
)
check(
    "tags_pad provenance: claim is human_verified false / model_opinion, and a copy (changing it does not touch the module constant)",
    r_t["claim"] == {"human_verified": False, "source": "model_opinion"} and r_t["claim"] is not P.CLAIM,
    r_t["claim"],
)
check(
    "tags_pad provenance: top_k = the 5 best non-speech classes of the padded output, largest first, no floor (Bird 0.049 is in, Speech 0.1 is not); max_unstored_score = the 6th best, 0.01",
    r_t["top_k"] == [["Insect", 0.7], ["Dog", 0.3], ["Silence", 0.06], ["Bird", 0.049], ["Wind", 0.02]] and r_t["max_unstored_score"] == 0.01,
    (r_t["top_k"], r_t["max_unstored_score"]),
)
check(
    "tags_pad provenance twin: the production tagger's own record for the same output has the same top_k and max_unstored_score (variant best_beam_32k) - the lane differs only by variant, pad_s, ms",
    (
        lambda pr: (
            pr["top_k"] == r_t["top_k"]
            and pr["max_unstored_score"] == r_t["max_unstored_score"]
            and pr["model"]["sha256"] == r_t["model"]["sha256"]
            and pr["model"]["variant"] == "best_beam_32k"
        )
    )(
        (
            lambda p_: (
                setattr(p_, "labels", LABELS),
                setattr(p_, "speech_idx", [0]),
                setattr(p_, "at", AtStub(PROBS)),
                setattr(p_, "device", "cpu"),
                setattr(p_, "model", dict(MODEL_T)),
                p_.process(X, 1800000000.0)[0][0],
            )[-1]
        )(object.__new__(P.Tagger))
    ),
)
_orig_init = P.Tagger.__init__


def _own_init(self, data_dir, device):  # the production constructor stand-in: sets what P.Tagger.__init__ sets, own block included
    self.labels, self.speech_idx, self.at, self.device = LABELS, [0], AtStub(PROBS), device
    self.model = P.model_block("own", None, "best_beam_32k", None)


P.Tagger.__init__ = _own_init
try:
    own = PL.PadTagger(None, "cpu", 10)
finally:
    P.Tagger.__init__ = _orig_init
check(
    "tags_pad provenance: without a production plugin to share, the lane's own loaded tagger block is stamped with variant pad10 (name, version, sha256 as the constructor set them)",
    own.model == {"name": "own", "version": None, "variant": "pad10", "sha256": None} and own.process(X, 1.0)[0][0]["model"] == own.model,
    own.model,
)
check(
    "tags_pad: name is tags_pad and the window grid is the production one (5 s / 2 s), so the files line up",
    PL.PadTagger.name == "tags_pad" and (pt.win_s, pt.hop_s) == (P.Tagger.win_s, P.Tagger.hop_s) == (5.0, 2.0),
)

# production unchanged: same stub, the real Tagger.process still feeds the unpadded 160000 samples and writes its own record
at_p = AtStub(PROBS)
prod = object.__new__(P.Tagger)
prod.labels, prod.speech_idx, prod.at, prod.device = LABELS, [0], at_p, "cpu"
prod.model = P.model_block("PANNs CNN14 (stub)", "0", "best_beam_32k", None)  # master: the production tagger stamps its model on every record (provenance)
prec, pemb = prod.process(X, 1800000000.0)
check(
    "production tags: input stays 160000 samples (no padding), record has no variant/pad_s/ms",
    at_p.calls[0].shape == (1, 160000)
    and np.array_equal(at_p.calls[0][0], A32)
    and "variant" not in prec[0]
    and "pad_s" not in prec[0]
    and "ms" not in prec[0]
    and prec[0]["emb_stored"] is True
    and pemb is not None,
    prec[0],
)

# speech gate: >= 0.30 writes no record; twin just below writes one; both go through the same code path
for p_sp, n_rec, label in ((0.30, 0, "exactly 0.30 (>=)"), (0.31, 0, "0.31"), (0.299, 1, "0.299 (twin)"), (0.0, 1, "0.0 (twin)")):
    rr, ee = PL.PadTagger(None, "cpu", 10, share=Share(AtStub([p_sp] + PROBS[1:]))).process(X, 5.0)
    check("tags_pad speech gate: speech_p %s -> %d record(s), no embedding" % (label, n_rec), len(rr) == n_rec and ee is None, rr)


class AtLen(AtStub):
    """Answers a different speech probability for the padded (320000) and the unpadded (160000) input: padding dilutes the clip-level speech output."""

    def __init__(self, sp_padded, sp_plain):
        super().__init__(PROBS)
        self.sp = {320000: sp_padded, 160000: sp_plain}

    def inference(self, a):
        self.calls.append(np.array(a))
        p = self.probs.copy()
        p[0] = self.sp[a.shape[1]]
        return p[None, :], np.zeros((1, 4), dtype=np.float32)


for sp_pad, sp_plain, n_rec, n_calls, label in (
    (0.10, 0.45, 0, 2, "padded 0.10 / unpadded 0.45 (production flags speech)"),
    (0.10, 0.30, 0, 2, "padded 0.10 / unpadded exactly 0.30"),
    (0.10, 0.299, 1, 2, "padded 0.10 / unpadded 0.299 (twin)"),
    (0.45, 0.0, 0, 1, "padded 0.45 (unpadded call skipped)"),
):
    a_ = AtLen(sp_pad, sp_plain)
    rr, ee = PL.PadTagger(None, "cpu", 10, share=Share(a_)).process(X, 5.0)
    check(
        "tags_pad speech guard: %s -> %d record(s), %d model call(s)" % (label, n_rec, n_calls),
        len(rr) == n_rec and ee is None and len(a_.calls) == n_calls,
        (len(rr), len(a_.calls)),
    )
sub = PL.PadTagger(None, "cpu", 10, share=Share(AtStub([0.0, 0.9, 0.0, 0.0, 0.0, 0.0, 0.0])))
sub.speech_idx = []
check("tags_pad: no speech labels known -> speech_p 0.0 and the record is written (the production fallback)", sub.process(X, 5.0)[0][0]["speech_p"] == 0.0)

# cost
check("cost: a plugin that processed no window reports only pad_s", PL.PadTagger(None, "cpu", 10, share=Share(AtStub(PROBS))).cost() == {"pad_s": 10.0})
c = PL.Cost(2.0)
check("cost: empty -> {pad_s}", c.report(10.0) == {"pad_s": 10.0})
c.add(10.0)
c.add(30.0)
check(
    "cost: mean of 10 and 30 ms = 20.0 per window, 10.0 per audio second (hop 2 s)",
    c.report(10.0) == {"pad_s": 10.0, "ms_per_window": 20.0, "ms_per_audio_s": 10.0},
    c.report(10.0),
)
fresh = PL.PadTagger(None, "cpu", 10, share=Share(AtStub([0.9] + PROBS[1:])))
fresh.process(X, 5.0)  # a speech-suppressed window still costs time


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


clk = Clock()


class AtTimed(AtStub):
    def __init__(self):
        super().__init__(PROBS)

    def inference(self, a):
        clk.t += 0.010 if a.shape[1] == 320000 else 0.030  # padded call 10 ms, unpadded call 30 ms (fake seconds)
        return super().inference(a)


real_pc, real_bb = PL.time.perf_counter, P.best_beam
PL.time.perf_counter = clk


def slow_beam(x):
    clk.t += 0.500  # work before the model call must not be timed
    return real_bb(x)


P.best_beam = slow_beam
try:
    tp = PL.PadTagger(None, "cpu", 10, share=Share(AtTimed()))
    tr_, _ = tp.process(X, 5.0)
finally:
    PL.time.perf_counter, P.best_beam = real_pc, real_bb
check(
    "cost: record ms and status ms are the padded model call only (10.0; best_beam 500 ms excluded); ms_ref_per_window 30.0 is the unpadded call",
    tr_[0]["ms"] == 10.0 and tp.cost() == {"pad_s": 10.0, "ms_per_window": 10.0, "ms_per_audio_s": 5.0, "ms_ref_per_window": 30.0},
    (tr_[0]["ms"], tp.cost()),
)
check(
    "cost: a speech-suppressed window is counted (n 1 after one speech window)",
    fresh.cost_.n == 1 and "ms_per_window" in fresh.cost() and fresh.cost()["pad_s"] == 10.0,
)
check(
    "pad_s 4 is refused at construction (no model touched)",
    raises(lambda: PL.PadTagger(None, "cpu", 4, share=Share(AtStub(PROBS))), ValueError, "pad seconds 4"),
)
check("pad_s 7.5 -> 240000 samples at 32 kHz", PL.PadTagger(None, "cpu", 7.5, share=Share(AtStub(PROBS))).n_pad == 240000)

# ---------------------------------------------------------------- CLAP (torch only)
try:
    import torch
except Exception as e:  # noqa: BLE001
    torch = None
    n_skip += 1
    print("SKIP clap_pad checks [no torch here: %s]" % type(e).__name__)
if torch is not None:

    class ProcStub:
        def __init__(self):
            self.wavs = []

        def __call__(self, audio=None, sampling_rate=None, return_tensors=None):
            self.wavs.append((np.array(audio[0]), sampling_rate))
            return {"input_features": torch.zeros(1, 1)}

    class ModelStub:
        def get_audio_features(self, **kw):
            return torch.tensor([[1.0, 0.0]])

    class ClapShare:
        def __init__(self):
            self.torch, self.device, self.proc, self.model = torch, "cpu", ProcStub(), ModelStub()
            self.model_meta = dict(MODEL_C)
            self.prompts = ["silence", "a dog barking", "rain falling", "wind blowing"]
            t = torch.tensor([[1.0, 0.0], [0.0, 1.0], [0.6, 0.8], [-1.0, 0.0]])
            self.text, self.scale = torch.nn.functional.normalize(t, dim=-1), 1.0

    sh = ClapShare()
    cl = PL.PadClap("cpu", 10, share=sh)
    xc = window()
    rc, ec = cl.process(xc, 1800000000.0)
    wav, fs = sh.proc.wavs[0]
    check(
        "clap_pad: processor gets 480000 samples at 48000 Hz; first 240000 = loudest beam (4), rest exactly zero",
        fs == 48000 and wav.shape == (480000,) and np.array_equal(wav[:240000], xc[:, 3]) and not np.any(wav[240000:]),
        (fs, wav.shape),
    )
    ex = [math.exp(1.0), math.exp(0.0), math.exp(0.6), math.exp(-1.0)]
    pz = [v / sum(ex) for v in ex]
    check(
        "clap_pad: top 3 prompts with exact softmax values, variant pad10, pad_s 10.0, beam 4, no embedding",
        [x[0] for x in rc[0]["top"]] == ["silence", "rain falling", "a dog barking"]
        and [x[1] for x in rc[0]["top"]] == [round(pz[0], 3), round(pz[2], 3), round(pz[1], 3)]
        and rc[0]["variant"] == "pad10"
        and rc[0]["pad_s"] == 10.0
        and rc[0]["beam"] == 4
        and rc[0]["k"] == "clap"
        and ec is None
        and sorted(rc[0]) == ["beam", "claim", "k", "max_unstored_score", "model", "ms", "pad_s", "t", "top", "top_k", "variant"],
        rc[0],
    )
    check(
        "clap_pad provenance: model = the production clap's block (same name, version, sha256) with variant pad10 and the shared block untouched; claim exact; top_k = all 4 prompts by score (the test model has 4), max_unstored_score null",
        rc[0]["model"] == {"name": "laion/clap (stub)", "version": "rev9", "variant": "pad10", "sha256": "12" * 32}
        and sh.model_meta == MODEL_C
        and rc[0]["claim"] == {"human_verified": False, "source": "model_opinion"}
        and rc[0]["top_k"]
        == [["silence", round(pz[0], 3)], ["rain falling", round(pz[2], 3)], ["a dog barking", round(pz[1], 3)], ["wind blowing", round(pz[3], 3)]]
        and rc[0]["max_unstored_score"] is None,
        rc[0],
    )
    sh2 = ClapShare()
    prod_c = object.__new__(P.ClapZeroShot)
    prod_c.torch, prod_c.device, prod_c.proc, prod_c.model = torch, "cpu", sh2.proc, sh2.model
    prod_c.prompts, prod_c.text, prod_c.scale = sh2.prompts, sh2.text, sh2.scale
    prod_c.model_meta = P.model_block("laion/clap (stub)", "0", "best_beam_48k", None)
    pc_, _ = prod_c.process(xc, 5.0)
    check(
        "production clap: processor still gets the unpadded 240000 samples, record has no variant",
        sh2.proc.wavs[0][0].shape == (240000,) and "variant" not in pc_[0] and "pad_s" not in pc_[0],
    )
    check(
        "clap_pad: name clap_pad, grid 5 s / 5 s as production, pad 4 refused",
        PL.PadClap.name == "clap_pad"
        and (cl.win_s, cl.hop_s) == (5.0, 5.0)
        and raises(lambda: PL.PadClap("cpu", 4, share=ClapShare()), ValueError, "pad seconds 4"),
    )
    cl.process(xc, 6.0)
    check("clap_pad cost: two windows counted", cl.cost_.n == 2 and "ms_per_window" in cl.cost())


# ---------------------------------------------------------------- pad_lane_report
def rec(t, top1, **kw):
    return dict({"k": "tags", "t": t, "beam": 1, "top": [[top1, 0.5]] if top1 else []}, **kw)


prod_rows = [rec(100.0, "Insect"), rec(102.0, "Speech", speech_active=True), rec(104.0, "Dog"), rec(106.0, "Insect"), rec(108.0, "Wind")]
pad_rows = [rec(100.2, "Insect", ms=40.0, variant="pad10"), rec(104.1, "Insect", ms=60.0), rec(106.9, "Insect"), rec(130.0, "Dog")]
cmp_ = RP.compare(prod_rows, pad_rows, 0.5, 10)
check(
    "report: 2 pairs (100.0/100.2 agree, 104.0/104.1 flip Dog->Insect); 106.9 is 0.9 s from 106.0 (outside 0.5) and 130 has no partner",
    cmp_["pairs"] == 2 and cmp_["agree"] == 1 and cmp_["agree_rate"] == 0.5 and cmp_["flips"] == [["Dog", "Insect", 1]],
    cmp_,
)
check(
    "report: the speech-flagged production record is not paired and not counted (prod_only 2 = 106 and 108; pad_only 2); mean_ms_written 50.0; variants [pad10]; speech_leak 0",
    cmp_["prod_only"] == 2 and cmp_["pad_only"] == 2 and cmp_["mean_ms_written"] == 50.0 and cmp_["variants"] == ["pad10"] and cmp_["speech_leak"] == 0,
    cmp_,
)
leak = RP.compare(prod_rows, pad_rows + [rec(102.1, "Insect", variant="pad10")], 0.5, 10)
check(
    "report: a pad record 0.1 s from the speech_active production record at 102.0 is a speech_leak (1); the production speech record stays unpaired",
    leak["speech_leak"] == 1 and leak["pairs"] == 2 and leak["pad_only"] == 3,
    leak,
)
GRID = [rec(float(t), "Dog") for t in (0, 2, 4, 6, 8, 10)]
ok_ = RP.compare(GRID, [rec(t + 0.1, "Dog", variant="pad10", pad_s=10.0) for t in (0, 2, 4, 6, 8, 10)], 0.5, 10)
check(
    "report: aligned grids -> 6 pairs, no warnings, variants [pad10], pad_s_values [10.0]",
    ok_["pairs"] == 6 and ok_["warnings"] == [] and ok_["variants"] == ["pad10"] and ok_["pad_s_values"] == [10.0],
    ok_,
)
dr = RP.compare(GRID, [rec(t, "Dog", variant="pad10", pad_s=10.0) for t in (0.0, 2.6, 5.2, 7.8, 10.4)], 0.5, 10)
check(
    "report: pad grid drifting 0.6 s per window pairs 0.0, 7.8 (8), 10.4 (10) only: 3 pairs, prod_only 3, pad_only 2, warning 'unpaired 5 of 8 windows'",
    dr["pairs"] == 3 and dr["prod_only"] == 3 and dr["pad_only"] == 2 and dr["warnings"] == ["unpaired 5 of 8 windows (grid drift or dropped windows)"],
    dr,
)
mx = RP.compare(GRID[:2], [rec(0.0, "Dog", variant="pad10", pad_s=10.0), rec(2.0, "Dog", variant="pad7", pad_s=7.5)], 0.5, 10)
check(
    "report: mixed variant and pad_s in the pad file are warned, both listed",
    mx["warnings"] == ["mixed variant: pad10,pad7", "mixed pad_s: 7.5,10.0"] and mx["variants"] == ["pad10", "pad7"] and mx["pad_s_values"] == [7.5, 10.0],
    mx,
)
wide = RP.compare(prod_rows, pad_rows, 1.0, 10)
check("report twin: --tol-s 1.0 pairs 106.9 with 106.0 too (3 pairs, agree 2)", wide["pairs"] == 3 and wide["agree"] == 2, wide)
one = RP.compare([rec(1.0, "Dog"), rec(1.5, "Dog")], [rec(1.1, "Dog")], 0.5, 10)
check(
    "report: one pad record pairs with one production record only (nearest, 1.0), prod_only 1, pad_only 0",
    one["pairs"] == 1 and one["prod_only"] == 1 and one["pad_only"] == 0,
    one,
)
emp = RP.compare([rec(1.0, None)], [rec(1.0, "Dog")], 0.5, 10)
check("report: empty top counts as (none): flip (none)->Dog", emp["flips"] == [["(none)", "Dog", 1]] and emp["agree_rate"] == 0.0, emp)
two = RP.compare([rec(1.0, "Dog")], [rec(1.1, "Dog"), rec(1.2, "Dog")], 0.5, 10)
check(
    "report: a production record is used once (two pad records near one production record: 1 pair, pad_only 1)",
    two["pairs"] == 1 and two["pad_only"] == 1 and two["prod_only"] == 0,
    two,
)
check("report: no pairs -> agree_rate null", RP.compare([], [rec(1.0, "Dog")], 0.5, 10)["agree_rate"] is None)
tmp = tempfile.mkdtemp(prefix="padrep_")
os.makedirs(os.path.join(tmp, "analysis"))
with open(os.path.join(tmp, "analysis", "tags.jsonl"), "w") as f:
    f.write("\n".join(json.dumps(x) for x in prod_rows) + "\n{torn")
with open(os.path.join(tmp, "analysis", "tags_pad.jsonl"), "w") as f:
    f.write("\n".join(json.dumps(x) for x in pad_rows) + "\n")
before = sorted((n, os.path.getsize(os.path.join(tmp, "analysis", n))) for n in os.listdir(os.path.join(tmp, "analysis")))
out, err = io.StringIO(), io.StringIO()
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
    rc_ = RP.main(["--data", tmp])
after = sorted((n, os.path.getsize(os.path.join(tmp, "analysis", n))) for n in os.listdir(os.path.join(tmp, "analysis")))
check(
    "report CLI: exit 0, one JSON line equal to compare() (torn tail line skipped), read-only",
    rc_ == 0 and json.loads(out.getvalue()) == dict(cmp_, kind="tags") and before == after,
    out.getvalue(),
)
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
    rc2 = RP.main(["--data", tmp, "--kind", "clap"])
check("report CLI: clap files absent -> exit 2 with a message", rc2 == 2 and "clap.jsonl" in err.getvalue(), err.getvalue())

if "--unit-only" in sys.argv:
    print("%d checks (unit only), %d skipped, %d failure(s)" % (n_checks, n_skip, len(fails)))
    sys.exit(1 if fails else 0)


# ---------------------------------------------------------------- host wiring
def run_host(extra):
    od, hd = tempfile.mkdtemp(prefix="padh_"), tempfile.mkdtemp(prefix="padhh_")
    pr_ = subprocess.run(
        [
            sys.executable,
            os.path.join(HERE, "host.py"),
            "--listen",
            "127.0.0.1:48396",
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
            "--panns-dir",
            os.path.join(od, "none"),
            "--secs",
            "3",
        ]
        + extra,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return pr_, json.load(open(os.path.join(od, "status.json"))), sorted(os.listdir(od))


RUNNER = """import sys, runpy
sys.path.insert(0, %r)
import plugins as P
LOG = %r


class T(P.Tagger):
    def __init__(self, data_dir, device):
        open(LOG, "a").write("tags:" + type(self).__name__ + "\\n")
        self.labels, self.speech_idx, self.at, self.device = ["Speech"], [0], object(), device
        self.model = {"name": "stub", "version": None, "variant": "best_beam_32k", "sha256": None}


class C(P.ClapZeroShot):
    def __init__(self, device, prompts=None):
        open(LOG, "a").write("clap:" + type(self).__name__ + "\\n")
        self.torch, self.device, self.proc, self.model, self.prompts, self.text, self.scale = None, device, object(), object(), ["p"], None, 1.0
        self.model_meta = {"name": "stub", "version": None, "variant": "best_beam_48k", "sha256": None}


P.Tagger, P.ClapZeroShot = T, C
sys.argv = ["host.py"] + sys.argv[1:]
runpy.run_path(%r, run_name="__main__")
"""


def run_stubbed(extra):
    """The real host.py and the real pad_lane plugins; only the two production model classes (weights) are recording stubs that log each construction."""
    od, hd, wd = tempfile.mkdtemp(prefix="padh_"), tempfile.mkdtemp(prefix="padhh_"), tempfile.mkdtemp(prefix="padr_")
    log, runner = os.path.join(wd, "ctor.log"), os.path.join(wd, "run.py")
    open(runner, "w").write(RUNNER % (HERE, log, os.path.join(HERE, "host.py")))
    pr_ = subprocess.run(
        [
            sys.executable,
            runner,
            "--listen",
            "127.0.0.1:48397",
            "--out",
            od,
            "--history-dir",
            hd,
            "--device",
            "cpu",
            "--no",
            "birds",
            "--panns-dir",
            os.path.join(od, "none"),
            "--secs",
            "3",
            "--tags-pad-s",
            "10",
            "--clap-pad-s",
            "10",
        ]
        + extra,
        capture_output=True,
        text=True,
        timeout=120,
    )
    lines = open(log).read().split() if os.path.exists(log) else []
    return pr_, json.load(open(os.path.join(od, "status.json"))), lines


ps, ss, ls_ = run_stubbed([])
check(
    "host with production tags + clap running: pad plugins share them (each model class constructed once, as the production class), enabled, pad_s published by the cost hook",
    ps.returncode == 0
    and ls_ == ["tags:T", "clap:C"]
    and ss["plugins"]["tags_pad"]["enabled"] is True
    and ss["plugins"]["clap_pad"]["enabled"] is True
    and ss["plugins"]["tags_pad"]["pad_s"] == 10.0
    and ss["plugins"]["clap_pad"]["pad_s"] == 10.0,
    (ps.stderr[-300:], ls_, ss["plugins"]),
)
pn, sn, ln_ = run_stubbed(["--no", "tags", "--no", "clap"])
check(
    "host twin --no tags --no clap: nothing to share, each pad plugin loads its own model (constructed as PadTagger / PadClap)",
    pn.returncode == 0 and ln_ == ["tags:PadTagger", "clap:PadClap"] and sn["plugins"]["tags_pad"]["enabled"] is True,
    (pn.stderr[-300:], ln_, sn["plugins"]),
)
check("pad_lane.shared: picks the plugin by name, None when absent", PL.shared([pt], "tags_pad") is pt and PL.shared([pt], "tags") is None)

p0, s0, f0 = run_host([])
check(
    "host default: no tags_pad / clap_pad entry in status, no pad files",
    p0.returncode == 0 and "tags_pad" not in s0["plugins"] and "clap_pad" not in s0["plugins"] and not any("_pad" in n for n in f0),
    (p0.stderr[-200:], f0),
)
p1, s1, f1 = run_host(["--tags-pad-s", "4", "--clap-pad-s", "4"])
check(
    "host --tags-pad-s 4 / --clap-pad-s 4: both disabled with the pad-seconds reason, analyzer still runs (exit 0, dsp enabled)",
    p1.returncode == 0
    and s1["plugins"]["tags_pad"]["enabled"] is False
    and "pad seconds 4.0 must be >= the window 5.0" in s1["plugins"]["tags_pad"]["reason"]
    and "pad seconds 4.0 must be >= the window 5.0" in s1["plugins"]["clap_pad"]["reason"]
    and s1["plugins"]["dsp"]["enabled"] is True,
    (p1.stderr[-300:], s1["plugins"]),
)
p2, s2, f2 = run_host(["--tags-pad-s", "10"])
check(
    "host --tags-pad-s 10 (twin): the lane is attempted, the reason is the missing model, not the pad length",
    p2.returncode == 0
    and s2["plugins"]["tags_pad"]["enabled"] is False
    and "must be >=" not in s2["plugins"]["tags_pad"]["reason"]
    and s2["plugins"]["tags_pad"]["reason"]
    and "clap_pad" not in s2["plugins"],
    (p2.stderr[-300:], s2["plugins"]),
)
p3, s3, f3 = run_host(["--tags-pad-s", "10", "--no", "tags_pad"])
check(
    "host --no tags_pad wins over --tags-pad-s 10", s3["plugins"]["tags_pad"] == {"enabled": False, "reason": "disabled by --no"}, s3["plugins"].get("tags_pad")
)

print("%d checks, %d skipped, %d failure(s)" % (n_checks, n_skip, len(fails)))
for f_ in fails:
    print("  FAILED:", f_)
sys.exit(1 if fails else 0)
