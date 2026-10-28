#!/usr/bin/env python3
"""Tests for analysis/beam_response_map.py. Hermetic: synthetic JSONL in a temp dir, fixed seeds, no network, no live data.
    python -I test_beam_response_map.py                 # run the tests (stdlib only; also run from the contracts venv or WSL)
    python -I test_beam_response_map.py --mutations     # also run every mutation of the tool and require its named test to go red
    python -I test_beam_response_map.py --module-dir D  # test the copy of the tool in D (used by --mutations)

Style as analysis/test_analysis.py: check(name, ok, detail), exact values, a positive twin for every negative case, recording wrappers
(never a stub of the unit under test). The honesty rules of CLAUDE.md are applied by hand (scripts/check_test_honesty.py is not in this repo)."""

import builtins, contextlib, io, json, math, os, random, shutil, socket, statistics, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MODDIR = HERE
if "--module-dir" in sys.argv:
    MODDIR = os.path.abspath(sys.argv[sys.argv.index("--module-dir") + 1])
sys.path.insert(0, MODDIR)
import beam_response_map as B  # noqa: E402

TAXA = os.path.join(HERE, "..", "ui", "taxa.json")  # the real table the dashboard ships
fails, n_pass = [], 0
TMP = tempfile.mkdtemp(prefix="brm_test_")
T0 = 86400 * 19675  # a UTC midnight
B0 = [0.0, -0.5, -1.0, -1.5, -2.0, -2.5, -3.0, -3.5]  # static per-beam level offsets of the synthetic array


def check(name, ok, detail=""):
    global n_pass
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if ok:
        n_pass += 1
    else:
        fails.append(name)


def close(a, b, tol=1e-9):
    return a is not None and abs(a - b) <= tol


def vclose(a, b, tol=1e-9):
    return len(a) == len(b) and all(close(x, y, tol) for x, y in zip(a, b))


def P(**kw):
    p = B.default_params()
    p.update(controls=200, boot=200, perms=200, bout_gap_s=10.0)  # tests space events 20-100 s apart: each its own bout unless a test sets bout_gap_s
    p.update(kw)
    return p


def mkdir(name):
    d = os.path.join(TMP, name)
    os.makedirs(d, exist_ok=True)
    return d


def wlines(path, recs):
    with open(path, "w", encoding="utf-8", newline="") as f:
        for r in recs:
            f.write((r if isinstance(r, str) else json.dumps(r, separators=(",", ":"))) + "\n")


def e_rel(e_db):
    m = max(e_db)
    return [round(v - m, 6) for v in e_db]


def cosv(amp, phi):
    return [amp * math.cos(2 * math.pi * (k - phi) / 8) for k in range(8)]


def spatial(n, t0=T0, plants=None, noise=0.05, seed=5, start=0, step=1.0, base=B0):
    """n rows at 1 Hz: e_db = base + noise + plant; plants {row index: 8-vector}. Rows are written as the real plugin does (e_rel)."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        e = [b + rng.gauss(0, noise) for b in base]
        if plants and i in plants:
            e = [x + p for x, p in zip(e, plants[i])]
        out.append({"k": "spatial", "t": t0 + start + i * step, "e_rel_db": e_rel(e), "spread_db": 1.0})
    return out


def event_plants(times, amp, phi_of, width=3, t0=T0):
    """plants for the `width` rows ending at each event time (row index = t - t0)."""
    pl = {}
    for j, t in enumerate(times):
        v = cosv(amp, phi_of(j))
        for w in range(width):
            pl[int(t - t0) - w] = v
    return pl


def birds(times, common="Barn Owl", sci="Tyto alba", conf=0.95, extra=None):
    return [
        dict(
            {"k": "bird", "t": t, "beam": 3, "level_dbfs": -50.0, "common": common, "scientific": sci, "confidence": conf, "provenance": "model_opinion"},
            **(extra or {}),
        )
        for t in times
    ]


def ev_times(n, first=300, step=100, t0=T0):
    return [t0 + first + j * step for j in range(n)]


def setup(name, n_rows, sp_plants=None, bird_recs=(), **kw):
    d = mkdir(name)
    wlines(os.path.join(d, "spatial.jsonl"), spatial(n_rows, plants=sp_plants, **kw))
    wlines(os.path.join(d, "birds.jsonl"), list(bird_recs))
    return d


def run_an(d, params=None, sources=None, epochs=(), bands=None, now=1.0):
    tx = B.TX.Taxa(TAXA)
    sp = B.with_rotated(os.path.join(d, "spatial.jsonl"), True)
    bp = B.with_rotated(os.path.join(d, "birds.jsonl"), True)
    return B.analyze(params or P(), sp, bp, bands, tx, sources, epochs, now=now)[0]


def src(rep, name, band=B.BROADBAND):
    return next(e for e in rep["sources"] if e["name"] == name and e["band"] == band)


def cli(args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = B.main(args)
    return code, out.getvalue(), err.getvalue()


def planted_scenario(name, n_events, amp, phi_of, rows=None, spacing=100, **kw):
    times = ev_times(n_events, step=spacing)
    nrows = rows or (300 + n_events * spacing + 400)
    d = setup(name, nrows, event_plants(times, amp, phi_of), birds(times), **kw)
    return d, times


# ======================================================================================= engine level: baseline, join, centring
def engine_for(rows_recs, params=None):
    d = mkdir("eng_" + str(abs(hash(json.dumps(rows_recs[0])))))
    wlines(os.path.join(d, "spatial.jsonl"), rows_recs)
    f = {"spread_over_max": 0, "bad_vector": 0, "no_baseline": 0, "bad_json": 0}
    s = B.load_spatial([os.path.join(d, "spatial.jsonl")], 30.0, f)
    p = params or P()
    seg = B.make_segments(s, [-math.inf], ["all"])[0]
    return B.Engine(s, p), seg, s


const = [{"k": "spatial", "t": T0 + i, "e_rel_db": B0} for i in range(1000)]
eng, seg, s_ = engine_for(const)
mean0 = sum(B0) / 8
v = eng.vec(seg, T0 + 500.0, 0)
check("[T1] baseline: a constant e_rel stream has an excess of exactly 0 on every beam", isinstance(v, list) and vclose(v, [0.0] * 8), str(v))
pl = {i: [0, 0, 6.0, 0, 0, 0, 0, 0] for i in range(500, 504)}
eng, seg, s_ = engine_for(spatial(1000, plants=pl, noise=0.0))
eng.P["join_before_s"], eng.P["join_after_s"] = 0.0, 0.0
v = eng.vec(seg, T0 + 500.0, 0)
v_off = eng.vec(seg, T0 + 700.0, 0)
check(
    "[T1] baseline twin: +6 dB on beam 3 in rows 500-503 gives 5.25 on beam 3 and -0.75 elsewhere (the row is centred on its beam mean), 0 away from it",
    vclose(v, [-0.75, -0.75, 5.25, -0.75, -0.75, -0.75, -0.75, -0.75]) and vclose(v_off, [0.0] * 8),
    str((v, v_off)),
)

# linear truth: alternating winners; the excess must equal (e_db - mean) - per-beam median of the same, computed independently
PA = [0.0, 0.3, 0.6, 0.9, 1.2, 0.9, 0.6, 0.3]
PB = [1.5, 0.3, 0.6, 0.9, 0.0, 0.9, 0.6, 0.3]
rows_alt = []
for i in range(400):
    e = PA if i % 2 == 0 else PB
    c = 0.1 * (i % 7)  # a common level change the relative vector must not see
    rows_alt.append({"k": "spatial", "t": T0 + i, "e_rel_db": e_rel([x + c for x in e])})
eng, seg, s_ = engine_for(rows_alt)
eng.P["join_before_s"], eng.P["join_after_s"] = 0.0, 0.0
v = eng.vec(seg, T0 + 200.0, 0)
cen = [[x - sum(e) / 8 for x in e] for e in (PA, PB)]
base = [statistics.median([cen[i % 2][k] for i in range(400)]) for k in range(8)]
truth = [cen[0][k] - base[k] for k in range(8)]
check(
    "[T1b] centring: with winners alternating between beams 5 and 1 with different peak heights and a changing common level, the excess equals the linear value (e_db - mean) - median, 1e-9",
    isinstance(v, list) and vclose(v, truth, 1e-6),
    str((v, truth)),
)

# join window
pl = {i: [0, 0, 6.0, 0, 0, 0, 0, 0] for i in (498,)}  # one planted row at T-2 for T = row 500
eng, seg, s_ = engine_for(spatial(1000, plants=pl, noise=0.0))
vd = eng.vec(seg, T0 + 500.0, 0)
eng.P["join_before_s"] = 1.0
v1 = eng.vec(seg, T0 + 500.0, 0)
check(
    "[T2] join: the default joins rows t-2..t (the row at t-2 counts: 5.25/3 = 1.75 on beam 3); --join-before-s 1 leaves it out (0.0)",
    close(vd[2], 1.75) and close(v1[2], 0.0) and close(vd[0], -0.25),
    str((vd, v1)),
)
pl = {497: [0, 0, 6.0, 0, 0, 0, 0, 0], 501: [0, 0, 6.0, 0, 0, 0, 0, 0]}
eng, seg, s_ = engine_for(spatial(1000, plants=pl, noise=0.0))
v3 = eng.vec(seg, T0 + 500.0, 0)
check(
    "[T2] join: a row 3 s before t (its window ends before the bird window starts) and a row after t are not joined: excess 0 on every beam",
    vclose(v3, [0.0] * 8),
    str(v3),
)
gap = [r for r in spatial(1000, noise=0.0) if not 495 <= r["t"] - T0 <= 505]
eng, seg, s_ = engine_for(gap)
check(
    "[T2] join: an event in a hole of the spatial rows is 'unjoined'; positive twin: the same event time with rows present is joined",
    eng.vec(seg, T0 + 500.0, 0) == "unjoined" and isinstance(eng.vec(seg, T0 + 300.0, 0), list),
    "",
)

# baseline needs enough windows
eng, seg, s_ = engine_for(spatial(1000, noise=0.0), P(baseline_s=30.0, min_baseline_windows=70))
check(
    "[T2b] a baseline window of +-30 s holds 61 rows: < 70 gives 'no_baseline'; positive twin: a minimum of 50 gives a vector",
    eng.vec(seg, T0 + 500.0, 0) == "no_baseline",
    "",
)
eng.P["min_baseline_windows"] = 50
eng.cache.clear()
check("[T2b] baseline twin", isinstance(eng.vec(seg, T0 + 500.0, 0), list), "")

# ======================================================================================= A1, phase, Rayleigh
a1, ph = B.a1_phase(cosv(4.0, 1))
check("[T3] A1 of 4 cos(2 pi (k-1)/8) is 4.0 and the phase 45.0 degrees", close(a1, 4.0) and close(math.degrees(ph), 45.0), str((a1, ph)))
a1, ph = B.a1_phase([3.0] * 8)
check("[T3] A1 ignores a common offset: a constant vector has A1 0", close(a1, 0.0), str(a1))
check("[T15] Rayleigh, 8 equally spaced phases (R 0): p is 1.0", close(B.rayleigh_p(8, 0.0), 1.0), str(B.rayleigh_p(8, 0.0)))
check(
    "[T15] Rayleigh, n 10 R 0.5 is 0.0793557248828269 (Zar), not exp(-n R^2) = 0.0820849986238988",
    close(B.rayleigh_p(10, 0.5), 0.0793557248828269, 1e-12),
    str(B.rayleigh_p(10, 0.5)),
)
check(
    "[T15] Rayleigh, n 5 R 0.8 is 0.03174038085545273 (Zar), not exp(-n R^2) = 0.04076220397836621",
    close(B.rayleigh_p(5, 0.8), 0.03174038085545273, 1e-12),
    str(B.rayleigh_p(5, 0.8)),
)
check("[T15] Rayleigh, n 30 R 1 is exp(-50)", close(B.rayleigh_p(30, 1.0), math.exp(-50), 1e-30), str(B.rayleigh_p(30, 1.0)))
check("[T15] quantile: linear interpolation, p25 of 1,2,3,4 is 1.75", close(B.pct([1, 2, 3, 4], 0.25), 1.75), "")

# ======================================================================================= verdicts on planted scenarios
d, times = planted_scenario("v_go40", 40, 4.0, lambda j: 1)
rep = run_an(d)
e = src(rep, "Barn Owl")
blk = e["epochs"][0]
check(
    "[T3] fixed phase: 40 events, A1 median 4 dB (within 0.15), phase 45 degrees (within 2), R above 0.99, verdict go",
    abs(blk["a1_db"]["p50"] - 4.0) < 0.15
    and abs(blk["phase_deg"] - 45.0) < 2.0
    and blk["R"] > 0.99
    and e["verdict"] == "go"
    and blk["joined"] == 40
    and blk["unjoined"] == 0,
    json.dumps({k: blk.get(k) for k in ("a1_db", "phase_deg", "R", "verdict", "reason")}),
)
d, times = planted_scenario("v_uniform", 40, 4.0, lambda j: j % 8)
rep = run_an(d)
blk = src(rep, "Barn Owl")["epochs"][0]
check(
    "[T3] twin: phases uniform over the 8 beams (5 events each): R below 0.001 and no_go although A1 is 4 dB",
    blk["R"] < 0.001 and src(rep, "Barn Owl")["verdict"] == "no_go" and abs(blk["a1_db"]["p50"] - 4.0) < 0.15,
    json.dumps(blk.get("R")),
)
d, times = planted_scenario("v_small", 40, 0.3, lambda j: 1, noise=0.5)
check(
    "[T4] A1 0.3 dB at a fixed phase over 0.5 dB noise: weak (non-uniform, below the 1.0 dB floor), never go",
    src(run_an(d), "Barn Owl")["verdict"] == "weak",
    json.dumps(src(run_an(d), "Barn Owl")["epochs"][0].get("reason")),
)
d, times = planted_scenario("v_none", 40, 0.0, lambda j: 1, noise=0.5)
check(
    "[T4] twin: no planted effect over the same noise: no_go",
    src(run_an(d), "Barn Owl")["verdict"] == "no_go",
    json.dumps(src(run_an(d), "Barn Owl")["epochs"][0].get("reason")),
)
d, times = planted_scenario("v_29", 29, 4.0, lambda j: 1)
rep = run_an(d, P(min_events=30), sources=["Barn Owl"])
check("[T4] 29 joined events: insufficient", src(rep, "Barn Owl")["verdict"] == "insufficient" and src(rep, "Barn Owl")["epochs"][0]["joined"] == 29, "")
d, times = planted_scenario("v_30", 30, 4.0, lambda j: 1)
rep = run_an(d, P(min_events=30), sources=["Barn Owl"])
check("[T4] boundary twin: 30 joined events: go", src(rep, "Barn Owl")["verdict"] == "go", json.dumps(src(rep, "Barn Owl")["epochs"][0].get("reason")))
# control-matched: every non-event row carries a random-phase 4 dB pattern, so the controls have A1 about as large as the events
times = ev_times(40)
rng = random.Random(9)
pl = {i: cosv(4.0, rng.random() * 8) for i in range(4800)}
pl.update(event_plants(times, 4.0, lambda j: 1))
d = setup("v_ctl", 4800, pl, birds(times))
rep = run_an(d)
e = src(rep, "Barn Owl")
check(
    "[T4] controls as strong as the events (background 4 dB patterns everywhere): not go; control median A1 above 1.5 dB",
    e["verdict"] != "go" and e["epochs"][0]["control"]["a1_db"]["p50"] > 1.5,
    json.dumps((e["verdict"], e["epochs"][0].get("control"))),
)
d, times = planted_scenario("v_ctl_twin", 40, 4.0, lambda j: 1)
check("[T4] twin: the same events over quiet controls: go", src(run_an(d), "Barn Owl")["verdict"] == "go", "")
# weak and its edges: A1 of a single-beam spike of s dB is s/4
d, _ = planted_scenario("v_weak", 60, 0.8, lambda j: 1)
rep = run_an(d)
check(
    "[T5] A1 0.8 dB (60 events, fixed phase): weak (non-uniform, above the null, below the 1.0 dB floor)",
    src(rep, "Barn Owl")["verdict"] == "weak",
    json.dumps(src(rep, "Barn Owl")["epochs"][0].get("reason")),
)
d, _ = planted_scenario("v_edge", 60, 1.2, lambda j: 1)
check("[T5] lower-edge twin: A1 1.2 dB: go", src(run_an(d), "Barn Owl")["verdict"] == "go", "")
# the R >= 0.5 condition: 56 fixed + 64 uniform = R 0.467 -> weak; 80 fixed + 40 uniform = R 0.667 -> go
phis = lambda nfix, nuni: [1] * nfix + [k for k in range(8) for _ in range(nuni // 8)]
for nm, nf, nu, want in (("v_r47", 56, 64, "weak"), ("v_r67", 80, 40, "go")):
    pj = phis(nf, nu)
    d, _ = planted_scenario(nm, nf + nu, 4.0, lambda j, pj=pj: pj[j], spacing=40)
    rep = run_an(d)
    b_ = src(rep, "Barn Owl")["epochs"][0]
    check(
        f"[T3] R condition: {nf} fixed + {nu} uniform phases gives R {nf / (nf + nu):.3f} (within 0.03) and the verdict {want}",
        abs(b_["R"] - nf / (nf + nu)) < 0.03 and src(rep, "Barn Owl")["verdict"] == want,
        json.dumps((b_["R"], b_.get("rayleigh_p"), src(rep, "Barn Owl")["verdict"])),
    )

# Bonferroni: two evaluated sources -> alpha 0.01 / 2
t1 = ev_times(40)
t2 = ev_times(40, first=350)
d = setup(
    "v_bonf",
    4800,
    {**event_plants(t1, 4.0, lambda j: 1), **event_plants(t2, 4.0, lambda j: 3)},
    birds(t1) + birds(t2, "Hairy Woodpecker", "Dryobates villosus"),
)
rep = run_an(d)
check(
    "[T6b] Bonferroni: 2 (source, band) tests give alpha_adjusted exactly 0.005 and both planted sources go",
    rep["params"]["thresholds"]["tests"] == 2
    and rep["params"]["thresholds"]["alpha_adjusted"] == 0.005
    and src(rep, "Barn Owl")["verdict"] == "go"
    and src(rep, "Hairy Woodpecker")["verdict"] == "go",
    json.dumps(rep["params"]["thresholds"]),
)


# ======================================================================================= selection bias: the bird level is never read
class CountDict(dict):
    reads = 0

    def get(self, k, *a):
        CountDict.reads += k == "level_dbfs"
        return dict.get(self, k, *a)

    def __getitem__(self, k):
        CountDict.reads += k == "level_dbfs"
        return dict.__getitem__(self, k)

    def pop(self, k, *a):
        CountDict.reads += k == "level_dbfs"
        return dict.pop(self, k, *a)


class RecJson:  # a recording wrapper around the real json module
    def __getattr__(self, n):
        return getattr(json, n)

    def loads(self, s):
        v = json.loads(s)
        return CountDict(v) if isinstance(v, dict) else v


times = ev_times(40)
birds_lv = birds(times)
for i, r in enumerate(birds_lv):
    r["level_dbfs"] = -30.0 if i % 2 else -50.0  # 20 dB between events
d = setup("sel_a", 4800, event_plants(times, 4.0, lambda j: 1), birds_lv)
d2 = setup("sel_b", 4800, event_plants(times, 4.0, lambda j: 1), birds(times))
real_json = B.json
B.json = RecJson()
try:
    rep_a = run_an(d)
finally:
    B.json = real_json
rep_b = run_an(d2)
check(
    "[T6] selection bias: birds level_dbfs (20 dB apart between events) is read 0 times by the tool",
    CountDict.reads == 0 and B.json is real_json,
    str(CountDict.reads),
)
check(
    "[T6] twin: the recording wrapper does count a read of level_dbfs when one happens",
    (lambda c: (c.get("level_dbfs"), CountDict.reads)[1] == 1)(CountDict({"level_dbfs": 1})),
    "",
)
check("[T6] the two reports (level_dbfs differing) are identical", rep_a == rep_b, "")

# ======================================================================================= filters
rows = spatial(1000, noise=0.0)
rows[100]["e_rel_db"] = [0.0] * 7 + [-167.0]
rows[101]["e_rel_db"] = [0.0, -1, -1, -1, -1, -1, -1]
rows[102]["e_rel_db"] = [0.0, -1, -1, -1, -1, -1, -1, float("nan")]
rows[103]["e_rel_db"] = [0.0] * 7 + [-29.9]
d = mkdir("flt")
wlines(os.path.join(d, "spatial.jsonl"), [json.dumps(r) for r in rows] + ["{broken"])
wlines(os.path.join(d, "birds.jsonl"), [])
rep = run_an(d)
check(
    "[T7] filters: spread 167, a 7-element vector and a NaN, one broken line: exactly {spread_over_max 1, bad_vector 2, no_baseline 0, bad_json 1}; rows 997",
    rep["filters"] == {"spread_over_max": 1, "bad_vector": 2, "no_baseline": 0, "bad_json": 1} and rep["inputs"]["spatial_rows"] == 997,
    json.dumps((rep["filters"], rep["inputs"]["spatial_rows"])),
)
s7 = B.load_spatial([os.path.join(d, "spatial.jsonl")], 30.0, {"spread_over_max": 0, "bad_vector": 0, "no_baseline": 0, "bad_json": 0})
check(
    "[T7] twin: the row with spread 29.9 is kept (its time is among the 997 rows), the 167 dB row (row 100) is not",
    T0 + 103 in list(s7.t) and T0 + 100 not in list(s7.t),
    "",
)

# ======================================================================================= controls
times = ev_times(60, first=200, step=20)
d = setup("ctl", 2000, None, birds(times))
sp_s = B.load_spatial([os.path.join(d, "spatial.jsonl")], 30.0, {"spread_over_max": 0, "bad_vector": 0, "no_baseline": 0, "bad_json": 0})
eng = B.Engine(sp_s, P())
seg = B.make_segments(sp_s, [-math.inf], ["all"])[0]
allev = sorted(times)


def min_dist(pool, hour, n):
    return min(min(abs(sp_s.t[i] - e_) for e_ in allev) for i in pool.draw(hour, n))


pool = B.ControlPool(eng, seg, allev, 10.0, 1)
pool0 = B.ControlPool(eng, seg, allev, 0.0, 1)
check("[T8] controls stay >= 10.0 s from every event record (2000 draws)", min_dist(pool, 0, 2000) >= 10.0, str(min_dist(pool, 0, 2000)))
check(
    "[T8] twin: with the exclusion off the same draw comes within 1 s of an event (the test can fail)",
    min_dist(pool0, 0, 2000) < 1.0,
    str(min_dist(pool0, 0, 2000)),
)
check(
    "[T8] the j-th control of an hour does not depend on how many were asked first (draw 50 then 2000 == draw 2000)",
    B.ControlPool(eng, seg, allev, 10.0, 1).draw(0, 50) == pool.draw(0, 2000)[:50],
    "",
)
# stratification
times_h1 = [T0 + 3600 + 300 + 80 * j for j in range(40)]
d = setup("strat", 8000, event_plants(times_h1, 4.0, lambda j: 1, t0=T0), birds(times_h1))
sp_s = B.load_spatial([os.path.join(d, "spatial.jsonl")], 30.0, {"spread_over_max": 0, "bad_vector": 0, "no_baseline": 0, "bad_json": 0})
eng = B.Engine(sp_s, P())
seg = B.make_segments(sp_s, [-math.inf], ["all"])[0]
pool = B.ControlPool(eng, seg, times_h1, 10.0, 1)
blk, raw = B.block_stats("Barn Owl", 0, seg, times_h1, eng, pool, P())
check(
    "[T9] stratification: events all in UTC hour 1 -> controls are drawn from hour {1} only",
    set(pool.draws) == {1} and blk["control"]["n"] == 200 and blk["joined"] == 40,
    str((set(pool.draws), blk.get("control"))),
)
times_mix = [T0 + 300 + 100 * j for j in range(20)] + [T0 + 3600 + 300 + 100 * j for j in range(20)]
pool = B.ControlPool(eng, seg, times_mix, 10.0, 1)
blk, raw = B.block_stats("Barn Owl", 0, seg, times_mix, eng, pool, P())
check(
    "[T9] twin: events in hours 0 and 1 (20 each) -> controls from {0, 1}, 100 each",
    set(pool.draws) == {0, 1} and [len(pool.draws[h]) for h in (0, 1)] == [100, 100],
    str({h: len(v) for h, v in pool.draws.items()}),
)

# ======================================================================================= sources: human, engine, katydid, taxa
t100 = [T0 + 100 + 30 * j for j in range(100)]
recs = (
    birds(t100[:50], "Human vocal", "Human vocal", 0.97)
    + birds(t100[:1], "Human vocal", "Human vocal", 0.97)
    + birds(t100[:20], "human non-vocal", "Human non-vocal", 0.95)
    + birds(t100[:20], "Human whistle", "Human whistle", 0.95)
    + birds(t100, "Barn Owl", "Tyto alba", 0.95)
    + birds(t100[50:], "Engine", "Engine", 0.95)
    + birds(t100[50:], "Common Virtuoso Katydid", "Amblycorypha longinicta", 0.95)
)
d = setup("src", 3500, None, recs)
rep = run_an(d)
names = {e["name"] for e in rep["sources"]}
check(
    "[T10] human voice never appears as a source; the report carries no human-voice field or count at all",
    names == {"Barn Owl", "Common Virtuoso Katydid"} and "excluded_human" not in rep and "human" not in B.to_json(rep).lower(),
    json.dumps((sorted(names), sorted(rep))),
)
check(
    "[T10] speech windows: the 50 Barn Owl events that coincide with a Human vocal record (confidence 0.97) are dropped, the 50 others are kept",
    src(rep, "Barn Owl")["events"] == 50 and src(rep, "Barn Owl")["epochs"][0]["events"] == 50,
    str(src(rep, "Barn Owl")["events"]),
)
# a LOW-confidence human record (below min_conf) still marks a speech window; a record 6 s away (outside the 5 s guard) does not
lo = birds([T0 + 300], "Human vocal", "Human vocal", 0.30) + birds([T0 + 400 + 6], "Human vocal", "Human vocal", 0.30)
ow = ev_times(40, first=300, step=100)
dl = setup("src_lo", 4500, None, birds(ow) + lo)
rl = src(run_an(dl), "Barn Owl")["epochs"][0]
check(
    "[T10d] a human record at 0.30 confidence still drops the Barn Owl event at its time (39 events of 40); the record 6 s from the event at t0+400 leaves that event in",
    rl["events"] == 39,
    str(rl["events"]),
)
check(
    "[T10d] twin: with no human record all 40 events are kept",
    src(run_an(setup("src_lo0", 4500, None, birds(ow))), "Barn Owl")["epochs"][0]["events"] == 40,
    "",
)
# tags.jsonl speech_active rows
tg = [
    {"k": "tags", "t": T0 + 500 + 1.0, "beam": 1, "speech_p": 0.9, "speech_active": True, "top": [], "emb_stored": False},
    {"k": "tags", "t": T0 + 400 + 1.0, "beam": 1, "speech_p": 0.1, "speech_active": False, "top": [], "emb_stored": True},
]
wlines(os.path.join(dl, "tags.jsonl"), tg)
tx_ = B.TX.Taxa(TAXA)
rt = B.analyze(
    P(), [os.path.join(dl, "spatial.jsonl")], [os.path.join(dl, "birds.jsonl")], None, tx_, None, (), now=1.0, tags_paths=[os.path.join(dl, "tags.jsonl")]
)[0]
check(
    "[T10e] a tags row with speech_active true 1 s from the event at t0+500 drops it too (38 of 40: human record at t0+300 + tags row); the speech_active false row 1 s from the event at t0+400 drops nothing",
    src(rt, "Barn Owl")["epochs"][0]["events"] == 38,
    str(src(rt, "Barn Owl")["epochs"][0]["events"]),
)
check(
    "[T10b] Engine is skipped in auto mode as non_biological (listed in skipped_sources); twin: the Katydid is evaluated but flagged suspected_artifact",
    rep["skipped_sources"] == [{"name": "Engine", "reason": "non_biological"}]
    and src(rep, "Common Virtuoso Katydid")["suspected_artifact"] is True
    and src(rep, "Barn Owl")["suspected_artifact"] is False,
    json.dumps(rep["skipped_sources"]),
)
check(
    "[T10b] the Katydid stays out of `overall`: only Barn Owl is counted (sources_evaluated 1)",
    rep["overall"]["sources_evaluated"] == 1,
    json.dumps(rep["overall"]),
)
rep2 = run_an(d, sources=["engine"])
check(
    "[T10b] explicit --source engine (any case) evaluates it flagged non_biological and out of overall; no auto sources then",
    [e["name"] for e in rep2["sources"]] == ["Engine"] and rep2["sources"][0]["kind"] == "non_biological" and rep2["overall"]["sources_evaluated"] == 0,
    json.dumps(rep2["overall"]),
)
code, out, err = cli(["--analysis-dir", d, "--bands-log", "", "--taxa", TAXA, "--source", "Human vocal"])
check("[T10] --source Human vocal is refused (exit 2, message)", code == 2 and "human-voice classes can never be a source" in err, str((code, err)))
code, out, err = cli(["--analysis-dir", d, "--bands-log", "", "--taxa", os.path.join(TMP, "no_such_taxa.json")])
check(
    "[T10c] taxa table missing: fail closed, exit 2 and a message that says why",
    code == 2 and "taxa table not found" in err and "human-voice" in err,
    str((code, err)),
)
bad = os.path.join(TMP, "bad_taxa.json")
wlines(bad, ["{not json"])
code, out, err = cli(["--analysis-dir", d, "--bands-log", "", "--taxa", bad])
check("[T10c] taxa table corrupt: exit 2 'unusable'", code == 2 and "taxa table unusable" in err, str((code, err)))
code, out, err = cli(["--analysis-dir", d, "--bands-log", "", "--taxa", TAXA])
check("[T10c] twin: the real table works (exit 0, text report names Barn Owl)", code == 0 and "Barn Owl" in out and "Human" not in out, str((code, err)))

# ======================================================================================= privacy
d, times = planted_scenario("priv", 40, 4.0, lambda j: 1)
with open(os.path.join(d, "decoy.wav"), "wb") as f:
    f.write(b"RIFFxxxx")
opened, socks = [], []
real_open, real_socket = builtins.open, socket.socket


class RecSocket(real_socket):
    def __init__(self, *a, **k):
        socks.append(a)
        super().__init__(*a, **k)


def rec_open(file, *a, **k):
    opened.append(os.path.basename(str(file)))
    return real_open(file, *a, **k)


builtins.open, socket.socket = rec_open, RecSocket
try:
    out_path = os.path.join(mkdir("priv_out"), "r.json")
    code, _, _ = cli(["--analysis-dir", d, "--bands-log", "", "--taxa", TAXA, "--out", out_path, "--controls", "100", "--boot", "50", "--perms", "50"])
finally:
    builtins.open, socket.socket = real_open, real_socket
raw_out = open(out_path, "rb").read()
rep = json.loads(raw_out)
allowed = {"spatial.jsonl", "birds.jsonl", "taxa.json", os.path.basename(out_path)}
check(
    "[T11] privacy: exit 0; only spatial.jsonl, birds.jsonl and the taxa table were opened for reading, the decoy .wav never (twin: the recorder saw those files)",
    code == 0
    and {"spatial.jsonl", "birds.jsonl", "taxa.json"} <= set(opened)
    and "decoy.wav" not in opened
    and set(o for o in opened if not o.startswith(".brm_")) <= allowed,
    str(sorted(set(opened))),
)
check("[T11] no socket was created (twin: the recorder class is a real socket subclass)", socks == [] and issubclass(RecSocket, real_socket), str(socks))


def walk(o, path=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from walk(v, path + "." + k)
            yield path + "." + k, k, v
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from walk(v, path + "[]")
    else:
        yield path, None, o


keys = {k for _, k, _ in walk(rep) if k}
strings = [v for _, _, v in walk(rep) if isinstance(v, str)]
check(
    "[T11] no key named lat/lon/latitude/longitude/county/path/vec, and no string value with a path separator, a drive letter or the temp dir",
    not keys & {"lat", "lon", "latitude", "longitude", "county", "path", "vec", "vecs", "wav"}
    and not any("/" in s or "\\" in s or (len(s) > 1 and s[1] == ":" and s[0].isalpha()) or TMP in s for s in strings),
    str(strings[:3]),
)
BLOCK_KEYS = {
    "label",
    "events",
    "bouts",
    "joined",
    "unjoined",
    "events_no_baseline",
    "event_duty",
    "baseline_contaminated",
    "dominant_hist",
    "control_dominant_hist",
    "ratio_by_dominant",
    "top2_db",
    "a1_db",
    "a1_ci95",
    "phase_deg",
    "R",
    "rayleigh_p",
    "perm_p",
    "control",
    "lag_scan",
    "verdict",
    "reason",
}
bl = rep["sources"][0]["epochs"][0]
check(
    "[T11] an evaluated block holds exactly the aggregate keys (no per-event vectors), twin: the key set is the full documented one",
    set(bl) == BLOCK_KEYS,
    str(set(bl) ^ BLOCK_KEYS),
)
check("[T11] LF only in --out, one line, ends with a newline", b"\r" not in raw_out and raw_out.count(b"\n") == 1 and raw_out.endswith(b"\n"), "")

# ======================================================================================= --out safety
d, times = planted_scenario("outsafe", 30, 4.0, lambda j: 1)
fast = ["--analysis-dir", d, "--bands-log", "", "--taxa", TAXA, "--controls", "60", "--boot", "30", "--perms", "30"]
inside_path = os.path.join(d, "report.json")
code, out, err = cli(fast + ["--out", inside_path])
check(
    "[T12] --out inside the analysis directory: exit 2 'inside the analysis directory', nothing written",
    code == 2 and "inside the analysis directory" in err and not os.path.exists(inside_path),
    str((code, err)),
)
sib = os.path.join(mkdir("outsafe_sib"), "report.json")
code, out, err = cli(fast + ["--out", sib])
check("[T12] twin: a sibling directory is accepted (exit 0, file written)", code == 0 and os.path.exists(sib), str((code, err)))
dest = mkdir("outsafe_dir")
code, out, err = cli(fast + ["--out", dest])
left = [f for f in os.listdir(dest)]
check(
    "[T12] the final rename fails (the target is a directory): exit 2 'cannot write', no temp file left in the directory (twin: the sibling write above left only report.json)",
    code == 2 and "cannot write the output" in err and left == [] and os.listdir(os.path.dirname(sib)) == ["report.json"],
    str((code, err, left)),
)
code, out, err = cli(["--analysis-dir", os.path.join(TMP, "nodir"), "--taxa", TAXA])
check("[T12] a missing analysis directory exits 2 with a one-line message", code == 2 and "spatial.jsonl not found" in err, str((code, err)))
code, out, err = cli(["--bogus"])
check("[T12] a usage error exits 2", code == 2, str(code))
d0 = mkdir("empty_sp")
wlines(os.path.join(d0, "spatial.jsonl"), [])
wlines(os.path.join(d0, "birds.jsonl"), [])
code, out, err = cli(["--analysis-dir", d0, "--bands-log", "", "--taxa", TAXA])
check("[T12] zero usable spatial rows exits 3", code == 3 and "no usable spatial rows" in err, str((code, err)))

# ======================================================================================= determinism
d, times = planted_scenario("det", 40, 4.0, lambda j: 1)
ra = B.to_json(run_an(d))
rb = B.to_json(run_an(d))
rc = B.to_json(run_an(d, P(seed=2)))


def diff(a, b, path=""):
    if isinstance(a, dict) and isinstance(b, dict):
        out = set()
        for k in set(a) | set(b):
            out |= diff(a.get(k), b.get(k), path + "." + k)
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        out = set()
        for x, y in zip(a, b):
            out |= diff(x, y, path + "[]")
        return out
    return set() if a == b else {path}


check("[T13] determinism: the same seed gives byte-identical output; twin: seed 2 differs", ra == rb and ra != rc, "")
dd = diff(json.loads(ra), json.loads(rc))
allowed_diff = {
    ".params.seed",
    ".sources[].epochs[].control.n",
    ".sources[].epochs[].control.a1_db.p25",
    ".sources[].epochs[].control.a1_db.p50",
    ".sources[].epochs[].control.a1_db.p75",
    ".sources[].epochs[].control.a1_db.p95",
    ".sources[].epochs[].control.top2_db.p25",
    ".sources[].epochs[].control.top2_db.p50",
    ".sources[].epochs[].control.top2_db.p75",
    ".sources[].epochs[].perm_p",
    ".sources[].epochs[].a1_ci95",
    ".sources[].epochs[].a1_ci95[]",
    ".sources[].epochs[].control_dominant_hist[]",
    ".sources[].epochs[].control_dominant_hist",
    ".sources[].epochs[].reason",
    ".sources[].reason",
    ".overall.reason",
}
check(
    "[T13] a different seed changes only the control, permutation and bootstrap fields (a subset of the exact documented set; twin: seed is among them)",
    ".params.seed" in dd and dd <= allowed_diff and ".sources[].epochs[].a1_ci95[]" in dd,
    str(sorted(dd - allowed_diff)),
)

# ======================================================================================= lag scan
times = ev_times(40)
pl = {int(t - T0) - 2: cosv(6.0, 1) for t in times}
d = setup("lag", 4800, pl, birds(times))
rep = run_an(d, P(join_before_s=0.0, join_after_s=0.4))
lag = src(rep, "Barn Owl")["epochs"][0]["lag_scan"]
best = max(lag, key=lambda x: x["median_a1_db"])
check(
    "[T14] lag scan: an effect planted 2 s before t peaks at shift -2 s (6.0 dB, within 0.2) and is below 0.2 dB at every other shift",
    best["shift_s"] == -2 and abs(best["median_a1_db"] - 6.0) < 0.2 and all(x["median_a1_db"] < 0.2 for x in lag if x["shift_s"] != -2),
    json.dumps(lag[:12]),
)
d, _ = planted_scenario("lag_flat", 40, 0.0, lambda j: 1)
lag = src(run_an(d), "Barn Owl")["epochs"][0]["lag_scan"]
check(
    "[T14] twin: no planted effect gives a flat curve (max - min < 0.1 dB, 21 shifts)",
    len(lag) == 21 and max(x["median_a1_db"] for x in lag) - min(x["median_a1_db"] for x in lag) < 0.1,
    json.dumps(lag[:5]),
)

# ======================================================================================= epochs, clusters, rotation, baseline-dropped events
times = ev_times(40, step=100)
pl = event_plants(times, 4.0, lambda j: 1)
d = setup("epochs", 4800, pl, birds(times))
ep_both = [(T0 + 2300.0, "w55")]
rep = run_an(d, epochs=ep_both, sources=["Barn Owl"], params=P(min_events=15))
e = src(rep, "Barn Owl")
check(
    "[T16b] two epochs, both planted (events in each >= 15): both go -> the source is go; epoch labels e0 and w55",
    [b_["label"] for b_ in e["epochs"]] == ["e0", "w55"] and e["verdict"] == "go" and [b_["verdict"] for b_ in e["epochs"]] == ["go", "go"],
    json.dumps([(b_["label"], b_["verdict"], b_["joined"]) for b_ in e["epochs"]]),
)
pl2 = {i: v for i, v in pl.items() if i < 2300}  # the second epoch has no effect
d = setup("epochs2", 4800, pl2, birds(times))
rep = run_an(d, epochs=ep_both, sources=["Barn Owl"], params=P(min_events=15))
e = src(rep, "Barn Owl")
check(
    "[T16b] epochs that disagree (go in e0, no_go in w55) give `inconsistent`",
    [b_["verdict"] for b_ in e["epochs"]] == ["go", "no_go"] and e["verdict"] == "inconsistent" and rep["overall"]["verdict"] == "inconsistent",
    json.dumps([(b_["label"], b_["verdict"]) for b_ in e["epochs"]]),
)
pl3 = {i: v for i, v in pl.items() if i >= 1500}  # e0 holds 12 events without an effect, w55 28 events with it
d = setup("epochs3", 4800, pl3, birds(times))
ep3 = [(T0 + 1500.0, "w55")]
r15 = src(run_an(d, epochs=ep3, sources=["Barn Owl"], params=P(min_events=15)), "Barn Owl")
r10 = src(run_an(d, epochs=ep3, sources=["Barn Owl"], params=P(min_events=10)), "Barn Owl")
check(
    "[T16b] an epoch with fewer than --min-events does not vote: min-events 15 -> e0 (12 events) is insufficient and w55 decides go; twin: min-events 10 -> e0 votes no_go -> inconsistent",
    [b_["joined"] for b_ in r15["epochs"]] == [12, 28] and r15["verdict"] == "go" and r10["verdict"] == "inconsistent",
    json.dumps((r15["verdict"], r10["verdict"], [(b_["joined"], b_["verdict"]) for b_ in r15["epochs"]])),
)
# cluster gap: two clusters 3 days apart
c1, c2 = spatial(1500, t0=T0), spatial(1500, t0=T0 + 3 * 86400, seed=6)
ta, tb = ev_times(30, step=40), ev_times(30, step=40, t0=T0 + 3 * 86400)
d = mkdir("clusters")
wlines(os.path.join(d, "spatial.jsonl"), c1 + c2)
wlines(os.path.join(d, "birds.jsonl"), birds(ta) + birds(tb))
rep = run_an(d, sources=["Barn Owl"])
check(
    "[T16c] a 3-day gap in the spatial rows splits two segments c1, c2 (1500 rows each, 30 joined events each) and warns",
    [(s_["label"], s_["rows"]) for s_ in rep["segments"]] == [("c1", 1500), ("c2", 1500)]
    and [b_["label"] for b_ in src(rep, "Barn Owl")["epochs"]] == ["c1", "c2"]
    and any("2 clusters" in w for w in rep["warnings"]),
    json.dumps(rep["segments"]),
)
rep1 = run_an(setup("noclusters", 1500, None, birds(ta)), sources=["Barn Owl"])
check(
    "[T16c] twin: contiguous rows give one segment labelled all and no cluster warning",
    [s_["label"] for s_ in rep1["segments"]] == ["all"] and not any("cluster" in w for w in rep1["warnings"]),
    "",
)
# events whose baseline is missing are counted per source and trigger the 30% rule
d, times = planted_scenario("nobase", 40, 4.0, lambda j: 1)
rep = run_an(d, P(baseline_s=30.0, min_baseline_windows=70), sources=["Barn Owl"])
b_ = src(rep, "Barn Owl")["epochs"][0]
check(
    "[T16d] events without a baseline (61 rows < 70): events_no_baseline 40, joined 0, insufficient 'not enough joined'",
    b_["events_no_baseline"] == 40 and b_["joined"] == 0 and b_["verdict"] == "insufficient",
    json.dumps(b_),
)
rep = run_an(d, P(baseline_s=30.0, min_baseline_windows=70), sources=["Barn Owl"])
rep_b = run_an(d, P(baseline_s=30.0, min_baseline_windows=50), sources=["Barn Owl"])
check(
    "[T16d] twin: a minimum of 50 windows evaluates all 40 and goes",
    src(rep_b, "Barn Owl")["epochs"][0]["events_no_baseline"] == 0 and src(rep_b, "Barn Owl")["verdict"] == "go",
    "",
)
# > 30% of the events dropped
times = ev_times(60)
rows_d = [r for r in spatial(6500, plants=event_plants(times, 4.0, lambda j: 1)) if not any(0 <= t - r["t"] + 0 < 3 and i_ < 25 for i_, t in enumerate(times))]
d = mkdir("dropped")
wlines(os.path.join(d, "spatial.jsonl"), rows_d)
wlines(os.path.join(d, "birds.jsonl"), birds(times))
rep = run_an(d, P(min_events=30), sources=["Barn Owl"])
b_ = src(rep, "Barn Owl")["epochs"][0]
check(
    "[T16d] 25 of 60 events in holes (0.42 dropped > 0.30): insufficient with the dropped fraction in the reason although 35 events joined",
    b_["unjoined"] == 25 and b_["joined"] == 35 and b_["verdict"] == "insufficient" and "0.42" in b_["reason"],
    json.dumps(b_),
)
# rotated generation read by default
d = mkdir("rot")
wlines(os.path.join(d, "spatial.jsonl.1"), spatial(300, t0=T0 - 400))
wlines(os.path.join(d, "spatial.jsonl"), spatial(500, t0=T0))
wlines(os.path.join(d, "birds.jsonl"), [])
code, out, err = cli(["--analysis-dir", d, "--bands-log", "", "--taxa", TAXA, "--json"])
code2, out2, err2 = cli(["--analysis-dir", d, "--bands-log", "", "--taxa", TAXA, "--json", "--no-rotated"])
j1, j2 = json.loads(out), json.loads(out2)
check(
    "[T16e] the .1 rotated file is read by default (800 rows, rotated_read true); twin: --no-rotated reads 500",
    j1["inputs"]["spatial_rows"] == 800
    and j1["inputs"]["rotated_read"] is True
    and j2["inputs"]["spatial_rows"] == 500
    and j2["inputs"]["rotated_read"] is False,
    str((j1["inputs"], j2["inputs"])),
)


# ======================================================================================= band mode
def bands_file(path, n, plants, base_noise=0.05, seed=3):
    rng = random.Random(seed)
    recs = []
    for i in range(n):
        db = [[round(-70.0 - 2.0 * b + 0.3 * k + rng.gauss(0, base_noise), 3) for b in range(8)] for k in range(8)]
        if i in plants:
            for (k, b), amp in plants[i].items():
                db[k][b] += amp
        recs.append({"k": "beam_bands", "v": 1, "idx": i * 48000, "t_us": int((T0 + i) * 1e6), "n": 48000, "zero_frac": 0.0, "db": db})
    wlines(path, recs)


times = ev_times(40)
d = setup("bands", 4800, None, birds(times))
bp = os.path.join(d, "beam_bands.jsonl")
for nm, amp, want in (("go", 6.0, "go"), ("weak", 3.2, "weak"), ("none", 0.0, "no_go")):
    plants = {int(t - T0) - w: {(2, 6): amp} for t in times for w in range(3)}
    bands_file(bp, 4800, plants)
    rep = run_an(d, bands=bp, sources=["Barn Owl"])
    ban = [src(rep, "Barn Owl", n_) for n_ in B.BAND_NAMES]
    ok_ident = [e["verdict"] for e in ban[:3]] == ["identical_expected"] * 3
    others = [e["verdict"] for i_, e in enumerate(ban) if i_ not in (0, 1, 2, 6)]
    check(
        f"[T18] band mode, +{amp} dB on beam 3 in 4-8 kHz only: that band is {want}, bands 1-3 identical_expected, the other bands no_go, overall {want if want != 'weak' else 'weak'}",
        ban[6]["verdict"] == want
        and ok_ident
        and others == ["no_go"] * 4
        and rep["overall"]["verdict"] == want
        and rep["inputs"]["bands_rows"] == 4800
        and src(rep, "Barn Owl")["verdict"] == "no_go",
        json.dumps([(e["band"], e["verdict"]) for e in ban]),
    )

# ======================================================================================= scale
big = mkdir("big")
n_big = 310000
with open(os.path.join(big, "spatial.jsonl"), "w", encoding="utf-8", newline="") as f:
    rng = random.Random(1)
    for i in range(n_big):
        f.write('{"k":"spatial","t":%d,"e_rel_db":[%s]}\n' % (T0 + i, ",".join("%.1f" % (b - 0.1 * rng.random()) for b in B0)))
wlines(os.path.join(big, "birds.jsonl"), birds(ev_times(35, step=5000)))
rep, reads = B.analyze(
    P(controls=50, boot=20, perms=20), [os.path.join(big, "spatial.jsonl")], [os.path.join(big, "birds.jsonl")], None, B.TX.Taxa(TAXA), ["Barn Owl"], now=1.0
)
check(
    "[T19] scale: 310,000 rows complete; the row counter equals 310000 and the loop counter (rows joined) stays proportional to the events (< 100 per event incl. lag scan)",
    rep["inputs"]["spatial_rows"] == n_big and src(rep, "Barn Owl")["epochs"][0]["joined"] == 35 and reads < 35 * 100,
    str((rep["inputs"], reads)),
)
shutil.rmtree(big, ignore_errors=True)

# ======================================================================================= text report
code, out, err = cli(["--analysis-dir", d, "--bands-log", "", "--taxa", TAXA])
check(
    "[T20] text report: header, one row per source and band, the confound sentence",
    code == 0 and out.startswith("beam_response_map:") and "Barn Owl" in out and "controlled source" in out,
    out[:80],
)


# ======================================================================================= review fixes: speech controls, bouts, bands order and cap, flag ranges
# speech windows are not controls: rows within the guard of a speech time are never drawn (twin: the same rows are drawn without the speech list)
const_s = spatial(1200, noise=0.05)
eng_c, seg_c, s_c = engine_for(const_s, P())
sp_times = [T0 + 600.0]
pool_sp = B.ControlPool(eng_c, seg_c, [], 10.0, 1, sp_times, 5.0)
pool_no = B.ControlPool(eng_c, seg_c, [], 10.0, 1)
near_rows = [i for i in pool_no.cands.get(0, []) if abs(s_c.t[i] - sp_times[0]) <= 5.0]
check(
    "[T21] controls: rows within 5 s of a speech time are not candidates (11 rows t0+595..t0+605 excluded, none left)",
    len(near_rows) == 11
    and not [i for i in pool_sp.cands.get(0, []) if abs(s_c.t[i] - sp_times[0]) <= 5.0]
    and len(pool_no.cands[0]) - len(pool_sp.cands[0]) == 11,
    str((len(near_rows), len(pool_no.cands[0]), len(pool_sp.cands[0]))),
)
check("[T21] twin: a row 6 s from the speech time is still a candidate", any(abs(s_c.t[i] - (sp_times[0] + 6.0)) < 1e-9 for i in pool_sp.cands[0]), "")

# bouts: 60 detections 4 s apart (one bout, one stationary source) are ONE observation
DEF = B.default_params()
DEF.update(controls=200, boot=200, perms=200)
d_b, _ = planted_scenario("bout1", 60, 10.0, lambda j: 3, spacing=4)
r_b = src(run_an(d_b, DEF, ["Barn Owl"]), "Barn Owl")
blk_b = r_b["epochs"][0]
check(
    "[T22] one 4-minute bout of 60 detections 4 s apart: 60 joined events, exactly 1 bout, verdict insufficient (never go), the reason names the bouts",
    blk_b["joined"] == 60 and blk_b["bouts"] == 1 and r_b["verdict"] == "insufficient" and "1 bouts < 10" in blk_b["reason"],
    json.dumps(blk_b),
)
r_b2 = src(run_an(d_b, dict(DEF, bout_gap_s=2.0), ["Barn Owl"]), "Barn Owl")
check(
    "[T22] twin: the same data with bout_gap_s 2 (every detection its own bout) is go with 60 bouts - the bout rule, not the signal, caused the insufficient",
    r_b2["epochs"][0]["bouts"] == 60 and r_b2["verdict"] == "go",
    json.dumps(r_b2["epochs"][0]["reason"]),
)
d_s, _ = planted_scenario("bout110", 60, 10.0, lambda j: 3, spacing=110)
r_s = src(run_an(d_s, DEF, ["Barn Owl"]), "Barn Owl")
check(
    "[T22] twin: 60 detections 110 s apart (60 real bouts) with the same planted +10 dB on beam 3 is go under the default 60 s gap",
    r_s["epochs"][0]["bouts"] == 60 and r_s["verdict"] == "go",
    json.dumps(r_s["epochs"][0]["reason"]),
)
# 12 bouts x 5 detections: the Rayleigh test runs with n = 12 (not 60) on the bout mean phases
t12 = [T0 + 300 + b * 200 + j * 4 for b in range(12) for j in range(5)]
d12 = setup("bout12", 300 + 12 * 200 + 400, event_plants(t12, 10.0, lambda j: 3), birds(t12))
b12 = src(run_an(d12, dict(DEF, min_bouts=10), ["Barn Owl"]), "Barn Owl")["epochs"][0]
check(
    "[T22] 12 bouts of 5 detections: bouts 12, joined 60, rayleigh_p is Zar's p for n = 12 at the reported R (not n = 60)",
    b12["bouts"] == 12
    and b12["joined"] == 60
    and b12["rayleigh_p"] == B.rnd(B.rayleigh_p(12, b12["R"]), 12)
    and b12["rayleigh_p"] != B.rnd(B.rayleigh_p(60, b12["R"]), 12),
    json.dumps((b12["bouts"], b12["R"], b12["rayleigh_p"])),
)
r_mb = src(run_an(d12, dict(DEF, min_bouts=13), ["Barn Owl"]), "Barn Owl")
check(
    "[T22] min_bouts is a threshold with a boundary twin: 12 bouts < min_bouts 13 is insufficient, min_bouts 10 above is evaluated",
    r_mb["verdict"] == "insufficient" and "12 bouts < 13" in r_mb["epochs"][0]["reason"] and b12["verdict"] in ("go", "weak", "no_go"),
    r_mb["epochs"][0]["reason"],
)

# beam_bands.jsonl out of time order: sorted on load (twin: the ordered file)
tb = ev_times(40)
d_o = setup("bandorder", 4800, None, birds(tb))
bp_o = os.path.join(d_o, "beam_bands.jsonl")
bands_file(bp_o, 4800, {int(t - T0) - w: {(2, 6): 6.0} for t in tb for w in range(3)})
lines_o = open(bp_o, "rb").read().split(b"\n")[:-1]
bp_sw = os.path.join(d_o, "beam_bands_swapped.jsonl")
with open(bp_sw, "wb") as f_:
    f_.write(b"\n".join(lines_o[2400:] + lines_o[:2400]) + b"\n")
ser_o, ser_sw = B.load_bands(bp_o, 30.0), B.load_bands(bp_sw, 30.0)
check(
    "[T23] a bands log written second half first is sorted on load: identical time and level arrays to the ordered file, .reordered true (twin: the ordered file is not flagged)",
    ser_sw.reordered is True
    and ser_o.reordered is False
    and list(ser_sw.t) == list(ser_o.t)
    and list(ser_sw.data) == list(ser_o.data)
    and len(ser_o.t) == 4800,
    str((ser_sw.reordered, len(ser_sw.t))),
)
rep_o, rep_sw = run_an(d_o, bands=bp_o, sources=["Barn Owl"]), run_an(d_o, bands=bp_sw, sources=["Barn Owl"])
check(
    "[T23] the swapped file gives the same verdicts as the ordered one (4-8 kHz band go in both) and a warning that rows were sorted; the ordered file has no such warning",
    src(rep_sw, "Barn Owl", "b_4000_8000")["verdict"] == "go" == src(rep_o, "Barn Owl", "b_4000_8000")["verdict"]
    and [e["verdict"] for e in rep_sw["sources"]] == [e["verdict"] for e in rep_o["sources"]]
    and "beam_bands rows were not in time order: sorted by time" in rep_sw["warnings"]
    and "beam_bands rows were not in time order: sorted by time" not in rep_o["warnings"],
    str(rep_sw["warnings"]),
)

# bands cap: --bands-max-rows keeps the LAST rows, --bands-since drops the earlier ones; events outside the covered period are not counted as unjoined
t60 = [T0 + 300 + 50 * j for j in range(60)]
d_c = setup("bandcap", 4800, event_plants(t60, 4.0, lambda j: 1), birds(t60))
bp_c = os.path.join(d_c, "beam_bands.jsonl")
bands_file(bp_c, 4800, {int(t - T0) - w: {(2, 6): 6.0} for t in t60 for w in range(3)})
cap_s = B.load_bands(bp_c, 30.0, 3000)
since_s = B.load_bands(bp_c, 30.0, 0, T0 + 4000.0)
check(
    "[T24] load_bands max_rows 3000 keeps the last 3000 rows (first t = t0+1800, last t0+4799); since t0+4000 keeps 800 rows (first t0+4000); no cap keeps all 4800",
    len(cap_s.t) == 3000
    and cap_s.t[0] == T0 + 1800
    and cap_s.t[-1] == T0 + 4799
    and len(since_s.t) == 800
    and since_s.t[0] == T0 + 4000
    and len(B.load_bands(bp_c, 30.0).t) == 4800,
    str((len(cap_s.t), len(since_s.t))),
)
full = run_an(d_c, P(min_events=30), bands=bp_c, sources=["Barn Owl"])
capped = run_an(d_c, P(min_events=30, bands_max_rows=3000), bands=bp_c, sources=["Barn Owl"])
bb_f, bb_c = src(full, "Barn Owl", "b_4000_8000"), src(capped, "Barn Owl", "b_4000_8000")
check(
    "[T24] uncapped: the 4-8 kHz band sees all 60 events and is go; capped to 3000 rows: bands_rows 3000, the band block holds the 30 events inside the covered period, none unjoined, still go, plus a warning; the broadband still sees 60",
    full["inputs"]["bands_rows"] == 4800
    and bb_f["epochs"][0]["events"] == 60
    and bb_f["verdict"] == "go"
    and capped["inputs"]["bands_rows"] == 3000
    and bb_c["epochs"][0]["events"] == 30
    and bb_c["epochs"][0]["unjoined"] == 0
    and bb_c["verdict"] == "go"
    and any("only the last 3000 rows" in w for w in capped["warnings"])
    and not any("only the last" in w for w in full["warnings"])
    and src(capped, "Barn Owl")["events"] == 60,
    json.dumps((bb_c["epochs"][0], capped["warnings"])),
)
code, out, err = cli(
    [
        "--analysis-dir",
        d_c,
        "--bands-log",
        bp_c,
        "--taxa",
        TAXA,
        "--json",
        "--source",
        "Barn Owl",
        "--min-events",
        "30",
        "--controls",
        "50",
        "--boot",
        "20",
        "--perms",
        "20",
        "--bout-gap-s",
        "10",
        "--bands-max-rows",
        "3000",
    ]
)
check(
    "[T24] CLI --bands-max-rows 3000: exit 0 and inputs.bands_rows 3000", code == 0 and json.loads(out)["inputs"]["bands_rows"] == 3000, str((code, err[:200]))
)
iso = B.datetime.datetime.fromtimestamp(T0 + 4000.0, B.datetime.timezone.utc).isoformat()
code, out, err = cli(
    [
        "--analysis-dir",
        d_c,
        "--bands-log",
        bp_c,
        "--taxa",
        TAXA,
        "--json",
        "--source",
        "Barn Owl",
        "--controls",
        "50",
        "--boot",
        "20",
        "--perms",
        "20",
        "--bands-max-rows",
        "0",
        "--bands-since",
        iso,
    ]
)
check("[T24] CLI --bands-since ISO of t0+4000: exit 0, 800 band rows", code == 0 and json.loads(out)["inputs"]["bands_rows"] == 800, str((code, err[:200])))
code, out, err = cli(["--analysis-dir", d_c, "--bands-log", bp_c, "--taxa", TAXA, "--bands-since", "yesterday-ish"])
check(
    "[T24] CLI --bands-since garbage: exit 2 with a message, no traceback",
    code == 2 and "--bands-since time 'yesterday-ish' is not an ISO timestamp" in err and "Traceback" not in err,
    str((code, err)),
)

# degenerate numeric flags: exit 2 and a message, never a traceback (twin: the smallest valid value runs, exit 0)
BAD = [
    ("--boot", "0"),
    ("--perms", "0"),
    ("--controls", "0"),
    ("--min-events", "0"),
    ("--min-bouts", "0"),
    ("--min-baseline-windows", "0"),
    ("--alpha", "0"),
    ("--alpha", "nan"),
    ("--baseline-s", "-1"),
    ("--cluster-gap-s", "0"),
    ("--min-conf", "1.5"),
    ("--r-min", "2"),
    ("--bands-max-rows", "-1"),
    ("--join-before-s", "-1"),
    ("--boot", "abc"),
    ("--max-spread-db", "inf"),
]
bad_ok = True
bad_detail = []
for flag, val in BAD:
    try:
        code, out, err = cli(["--analysis-dir", d, "--bands-log", "", "--taxa", TAXA, flag, val])
    except Exception as ex_:  # a crash is a failed check, not an aborted suite
        code, out, err = -1, "", "Traceback " + repr(ex_)
    if not (code == 2 and "Traceback" not in err and flag in err and out == ""):
        bad_ok = False
        bad_detail.append((flag, val, code, err[-120:]))
check(
    f"[T25] {len(BAD)} degenerate flag values (boot 0, perms 0, alpha nan, ...) each exit 2 with the flag named on stderr, nothing on stdout",
    bad_ok,
    str(bad_detail),
)
GOOD = [
    ("--boot", "1"),
    ("--perms", "1"),
    ("--controls", "1"),
    ("--min-events", "1"),
    ("--min-bouts", "1"),
    ("--alpha", "1"),
    ("--bands-max-rows", "0"),
    ("--join-before-s", "0"),
]
good_codes = [
    (
        f,
        v,
        cli(["--analysis-dir", d_s, "--bands-log", "", "--taxa", TAXA, "--source", "Barn Owl", "--controls", "20", "--boot", "20", "--perms", "20", f, v])[0],
    )
    for f, v in GOOD
]
check(
    "[T25] twin: the smallest valid value of each flag (boot 1, perms 1, controls 1, alpha 1, ...) runs and exits 0",
    all(c == 0 for _, _, c in good_codes),
    str(good_codes),
)

# ======================================================================================= mutations
MUTATIONS = [
    ("M1 drop baseline subtraction", "return [acc[k] / n - base[k] for k in range(NB)]", "return [acc[k] / n for k in range(NB)]", "[T1] baseline"),
    (
        "M1b raw e_rel instead of centred rows",
        "rows.append((float(t), [v - m for v in e]))",
        "rows.append((float(t), [float(v) for v in e]))",
        "[T1b] centring",
    ),
    ("M3 join window 3.5 s before", '"join_before_s": 2.0', '"join_before_s": 3.5', "[T2] join"),
    ("M4 no hour stratification", "hours = [hour_of(jt[g[0]]) for g in bouts]", "hours = [0 for g in bouts]", "[T9] stratification"),
    ("M5a drop the control-p95 factor", 'P["a1_control_factor"] * raw["ctl_p95"]', '0.0 * raw["ctl_p95"]', "[T4] controls as strong"),
    ("M5b drop R >= r_min", '"R >= r_min": raw["R"] >= P["r_min"]', '"R >= r_min": True', "[T3] R condition"),
    ("M6 >= 30 becomes > 30", 'if ne < P["min_events"]:', 'if ne <= P["min_events"]:', "[T4] boundary twin"),
    (
        "M7 keep human voice as a source",
        'if isinstance(r.get("scientific"), str) else "")["human"]:',
        'if isinstance(r.get("scientific"), str) else "")["human"] and False:',
        "[T10] human voice",
    ),
    ("M19 speech-adjacent events kept", "if not near_any(speech_t, t, guard)]", "if True]", "[T10] speech windows"),
    ("M20 controls drawn from speech windows", "or near_any(speech_times, t, speech_guard_s)", "or False", "[T21] controls"),
    ("M21 tags speech_active ignored", 'r.get("speech_active") is True', 'r.get("speech_active") is None', "[T10e]"),
    ("M22 no bout clustering", 'if bouts and t - jt[bouts[-1][-1]] <= P["bout_gap_s"]:', "if False:", "[T22] one 4-minute bout"),
    ("M23 bands not sorted", "if reordered:", "if False:", "[T23]"),
    ("M24 bands cap ignored", "if max_rows and max_rows > 0:", "if False:", "[T24] load_bands max_rows"),
    ("M25 band events not restricted to the covered period", "[t for t in times if series.t[0] <= t <= series.t[-1]]", "times", "[T24] uncapped"),
    ("M26 flag ranges not enforced", "if (lo is not None and (v <= lo if lo_open else v < lo)) or (hi is not None and v > hi):", "if False:", "[T25]"),
    ("M27 bands-since ignored", "if since is not None and tu / 1e6 < since:", "if False:", "[T24] load_bands max_rows"),
    ("M28 min_bouts ignored", 'if n < P["min_bouts"]:', "if False:", "[T22] min_bouts"),
    ("M8 write per-event vectors", '"lag_scan": lag})', '"lag_scan": lag, "vecs": vecs})', "[T11] an evaluated block"),
    ("M9 dead-window threshold 30 -> 200", '"max_spread_db": 30.0', '"max_spread_db": 200.0', "[T7] filters"),
    ("M10 epochs never disagree", "if len(set(v)) > 1:", "if False:", "[T16b] epochs that disagree"),
    ("M11 no Bonferroni", 'alpha = P["alpha"] / max(1, tests)', 'alpha = P["alpha"]', "[T6b] Bonferroni"),
    (
        "M12 Rayleigh exp(-z) instead of Zar",
        "p = math.exp(math.sqrt(max(0.0, 1.0 + 4.0 * n + 4.0 * (n * n - rn * rn))) - (1.0 + 2.0 * n))",
        "p = math.exp(-rn * rn / n)",
        "[T15] Rayleigh, n 10",
    ),
    ("M13 fail open on a missing taxa table", "if not tx.ok:", "if False:", "[T10c] taxa table corrupt"),
    ("M14 no control exclusion", "if near < exclude_s or near_any(", "if False or near_any(", "[T8] controls stay"),
    (
        "M15 rotated file ignored by default",
        'sp = with_rotated(os.path.join(a.analysis_dir, "spatial.jsonl"), not a.no_rotated)',
        'sp = with_rotated(os.path.join(a.analysis_dir, "spatial.jsonl"), False)',
        "[T16e]",
    ),
    (
        "M16 read the bird level",
        'c, t, name = r.get("confidence"), r.get("t"), r.get("common")',
        'c, t, name = r.get("confidence"), r.get("t"), r.get("common"); _lv = r.get("level_dbfs")',
        "[T6] selection bias",
    ),
    ("M17 events without baseline not counted", "nob += 1", "pass", "[T16d] events without a baseline"),
    ("M18 join includes the half second after t", '"join_after_s": 0.0', '"join_after_s": 1.5', "[T2] join"),
]


def run_mutations():
    bad = []
    src_dir = HERE
    for title, old, new, test in MUTATIONS:
        md = os.path.join(TMP, "mut_" + title.split()[0])
        os.makedirs(md, exist_ok=True)
        for fn in ("taxa.py",):
            shutil.copy(os.path.join(src_dir, fn), md)
        text = open(os.path.join(src_dir, "beam_response_map.py"), encoding="utf-8", newline="").read()
        if text.count(old) != 1:
            bad.append(f"{title}: pattern found {text.count(old)} times")
            continue
        with open(os.path.join(md, "beam_response_map.py"), "w", encoding="utf-8", newline="") as f:
            f.write(text.replace(old, new))
        r = subprocess.run([sys.executable, "-I", os.path.join(HERE, "test_beam_response_map.py"), "--module-dir", md], capture_output=True, text=True)
        red = [l for l in r.stdout.splitlines() if l.startswith("FAIL ") and test in l]
        ok = r.returncode != 0 and bool(red)
        print(
            ("PASS " if ok else "FAIL ")
            + f"mutation {title}: the suite goes red at `{test}`"
            + ("" if ok else f"  [exit {r.returncode}, red: {[l[:60] for l in r.stdout.splitlines() if l.startswith('FAIL ')][:3]}]")
        )
        if not ok:
            bad.append(title)
    return bad


mut_bad = run_mutations() if "--mutations" in sys.argv else []
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{n_pass} passed, {len(fails)} failure(s)")
for f_ in fails:
    print("  FAILED:", f_)
for m in mut_bad:
    print("  MUTATION NOT CAUGHT:", m)
sys.exit(1 if fails or mut_bad else 0)
