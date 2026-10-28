#!/usr/bin/env python3
"""Tests for tools/burst_source_report.py. Synthetic data only (tools/make_burst_source_fixtures.py), temp directories, exact values, a positive twin for
every negative case, no stubs of the unit under test (only a RECORDING shim around open()). Run:  python -I test_burst_source_report.py"""

import builtins, contextlib, copy, hashlib, io, json, math, os, re, shutil, subprocess, sys, tempfile, time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import burst_source_report as T  # noqa: E402
import make_burst_source_fixtures as M  # noqa: E402

PINNED_SHA = "546c4bf4b5e4b388fe2a0f8b1db38ae06d9e3e3094703ec7d5948d33feb33413"  # the pre-registration (commit A)
TH_PATH = os.path.join(HERE, "burst_source_thresholds.json")
TH = json.load(open(TH_PATH))
n_checks, fails = 0, []
TMP = tempfile.mkdtemp(prefix="bsr_test_")


def check(name, cond, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  [" + str(detail)[:300] + "]"))
    if not cond:
        fails.append(name)


def D(name):
    p = os.path.join(TMP, name)
    shutil.rmtree(p, ignore_errors=True)
    os.makedirs(p)
    return p


def run(d, out=None, *extra):
    """-> (exit code, stdout text, out dir) of main() on data dir d."""
    out = out or os.path.join(d, "reports", "out")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = T.main(["--data-dir", d, "--out", out, "--now", "1793000000"] + list(extra))
    return code, buf.getvalue(), out


def sc_of(out):
    return json.load(open(os.path.join(out, "burst_scorecard.json")))


def hours_of(out):
    return [json.loads(l) for l in open(os.path.join(out, "burst_hours.jsonl"), encoding="utf-8")]


def fin(raws, amb=None, source="raw"):
    """finalize raw hour records exactly as the tool does"""
    return [T.order_hour(T.derive(copy.deepcopy(r), TH, amb, 1, source)) for r in raws]


def card(hours, amb=None, pinned=True):
    meta = {
        "now": 1,
        "thresholds_sha256": "x",
        "thresholds_pinned": pinned,
        "beam_bands_present": True,
        "inputs": {
            "dsp_records": 0,
            "transients": 0,
            "spatial_records": None,
            "ambient_records": 0,
            "coherence_records": 0,
            "board_records": None,
            "beam_bands_records": None,
            "skipped_lines": 0,
        },
    }
    return T.make_scorecard(hours, amb, TH, meta)


def plan_hours(kind, seed, nh=72, temps=None, burst_extra=None):
    rates, amb = M.scenario_plan(np.random.default_rng(seed), kind)
    if temps is not None:
        amb = {h: temps(h, t) for h, t in amb.items()}
    raws = [M.raw_hour(M.H0 + 3600 * i, 1800, int(r * 1800), cv=1.0, peak_z=None) for i, r in enumerate(rates[:nh])]
    return raws, amb


# ============================================================ 1 burst rule
def h1(n, cov):
    return fin([M.raw_hour(M.H0, cov, n)])[0]


a, b, c, d_ = h1(359, 3600), h1(360, 3600), h1(900, 1799), h1(900, 1800)
check("burst rule: n=359 of 3600 s is not a burst hour (rate 0.099722)", (a["is_burst"], a["rate_per_covered_s"]) == (False, 0.099722), a)
check("burst rule: n=360 of 3600 s is a burst hour, rate exactly 0.1 (positive twin)", (b["is_burst"], b["rate_per_covered_s"]) == (True, 0.1), b)
check("burst rule: 900 events in 1799 covered s is excluded (low_coverage), not a burst", (c["is_burst"], c["excluded_reason"]) == (False, "low_coverage"), c)
check(
    "burst rule: the same hour with 1800 covered s is a burst hour with no exclusion (twin)",
    (d_["is_burst"], d_["excluded_reason"], d_["rate_per_covered_s"]) == (True, None, 0.5),
    d_,
)

# ============================================================ 2 coverage counts distinct seconds
d = D("cov")
M.write_data_dir(d, [{"hour": M.H0, "covered_s": 2000, "dsp_records": 3600, "events": M.uniform_events(400, 2000, np.random.default_rng(1))}])
code, _, out = run(d)
h = hours_of(out)[0]
check(
    "coverage: 3600 dsp records packed in 2000 distinct seconds give covered_s 2000 (not 3600)",
    code == 0 and h["covered_s"] == 2000 and h["n_transients"] == 400,
    h["covered_s"],
)
d = D("cov2")
M.write_data_dir(d, [{"hour": M.H0, "covered_s": 3600, "events": M.uniform_events(400, 3600, np.random.default_rng(1))}])
check("coverage twin: 3600 records in 3600 distinct seconds give covered_s 3600", hours_of(run(d)[2])[0]["covered_s"] == 3600)

# ============================================================ 3 inter-event intervals
MASK = np.ones(3600, bool)
t05 = M.H0 + 1.0 + 0.5 * np.arange(100)
ioa = T.ioi_features(t05, TH, MASK, M.H0)
check("ioi: events every 0.5 s give median_s 0.5, cv_trim 0.0, 99 intervals", (ioa["median_s"], ioa["cv_trim"], ioa["n"]) == (0.5, 0.0, 99), ioa)
rng = np.random.default_rng(5)
tex = M.H0 + M.exponential(5000, 1.0, rng)
cv_ex = T.ioi_features(tex, TH, MASK, M.H0)["cv_trim"]
check("ioi: a seeded exponential train of 5000 events has cv_trim exactly 1.012713 (seed pinned)", cv_ex == 1.012713, cv_ex)
dtx = np.diff(tex)
nat = float(dtx[2499])
t_gap60 = np.concatenate([tex[:2500], tex[2500:] + (60.0 - nat)])
t_gap99 = np.concatenate([tex[:2500], tex[2500:] + (9.9 - nat)])
cvo = lambda v: round(float(np.std(v) / np.mean(v)), 6)  # noqa: E731
io60, io99 = T.ioi_features(t_gap60, TH, MASK, M.H0), T.ioi_features(t_gap99, TH, MASK, M.H0)
check(
    "ioi: an inserted 60 s gap is trimmed (10 s): cv_trim and the count equal those of the other 4998 intervals (numpy oracle)",
    (io60["cv_trim"], io60["n"]) == (cvo(np.delete(dtx, 2499)), 4998),
    io60,
)
check(
    "ioi twin: the same gap shortened to 9.9 s is included: count 4999, cv_trim equals the oracle with a 9.9 s interval and differs from the trimmed one",
    io99["n"] == 4999 and io99["cv_trim"] == cvo(np.concatenate([np.delete(dtx, 2499), [9.9]])) and io99["cv_trim"] != io60["cv_trim"],
    io99,
)


# ============================================================ 4 autocorrelogram
def acorr(ev, seed=1, n_surr=200):
    return T.autocorrelogram(M.H0 + ev, M.H0, TH, seed, n_surr, 6e8)


ap = acorr(M.periodic(2000, 1.0, 0.005, np.random.default_rng(21)))
an = acorr(M.poisson_env(2000, 1.0, np.random.default_rng(22)))
a7 = acorr(M.periodic(600, 7.0, 0.005, np.random.default_rng(23), on_s=3000, off_s=300))
check(
    "acorr: a 1.00 s +-5 ms train on a slow envelope peaks at lag 1.0 (+-0.02) with z >= 5 and ratio >= 1.5",
    abs(ap["peak_lag_s"] - 1.0) <= 0.02 and ap["peak_z"] >= 5 and ap["peak_ratio"] >= 1.5 and ap["n_surrogates"] == 200,
    ap,
)
check("acorr: the exact peak of that train (seed pinned): lag 0.99 s, z 154.0", (ap["peak_lag_s"], round(ap["peak_z"], 1)) == (0.99, 154.0), ap)
check("acorr negative twin: Poisson timing on the same envelope has z < 5 (pinned 2.5)", an["peak_z"] < 5 and round(an["peak_z"], 1) == 2.5, an)
check("acorr twin 2: a 7 s period (beyond the 5 s maximum lag) has z < 5", a7["peak_z"] < 5, a7)
check(
    "acorr: fewer than 200 events skips the test (peak_z null, no surrogates)",
    acorr(M.periodic(199, 1.0, 0.005, np.random.default_rng(24)))["peak_z"] is None
    and acorr(M.periodic(200, 1.0, 0.005, np.random.default_rng(24)))["peak_z"] is not None,
)
# calibration of z >= 5: false periodic rate on Poisson hours, hit rate on periodic hours
flag = lambda a_: a_["peak_z"] is not None and a_["peak_z"] >= 5 and a_["peak_ratio"] >= 1.5  # noqa: E731
fp = sum(1 for k in range(300) if flag(acorr(M.poisson_env(400, 1.0, np.random.default_rng(1000 + k)), seed=k)))
tp = sum(1 for k in range(50) if flag(acorr(M.periodic(400, 1.0, 0.005, np.random.default_rng(2000 + k)), seed=k)))
check(
    "calibration: 300 seeded Poisson hours (400 events on a slow envelope) give exactly 4 false periodic hours = 1.33%, within the 2% budget",
    fp == 4 and fp / 300 <= 0.02,
    fp,
)
check("calibration positive twin: 50 periodic hours (1.00 s +-5 ms) are all flagged (50/50 = 100%)", tp == 50, tp)
# surrogate cap
cap = T.autocorrelogram(M.H0 + M.periodic(2000, 1.0, 0.005, np.random.default_rng(21)), M.H0, TH, 1, 200, 10)
check(
    "surrogate cap: --max-surrogate-pairs 10 leaves peak_z null and 0 surrogates (positive twin above: 200 surrogates)",
    cap["peak_z"] is None and cap["n_surrogates"] == 0,
    cap,
)
chk = np.sort(np.random.default_rng(3).uniform(0, 100, 500))
sur = T.jitter_surrogates(chk, 10.0, np.random.default_rng(4))
check(
    "jitter surrogates keep the event count of every 10 s interval exactly",
    (np.bincount(np.floor(chk / 10).astype(int), minlength=10) == np.bincount(np.floor(sur / 10).astype(int), minlength=10)).all()
    and not np.array_equal(chk, sur),
)

# ============================================================ 5 determinism
d = D("det")
M.write_data_dir(d, [{"hour": M.H0 + 3600 * i, "covered_s": 1800, "events": M.poisson_env(400, 1.0, np.random.default_rng(30 + i))} for i in range(2)])
o1, o2, o3 = (run(d, os.path.join(TMP, "det_o%d" % i), *x)[2] for i, x in enumerate([(), (), ("--seed", "7")]))
b1, b2, b3 = (open(os.path.join(o, "burst_hours.jsonl"), "rb").read() for o in (o1, o2, o3))
h1_, h3_ = hours_of(o1), hours_of(o3)
check("determinism: the same --seed gives a byte-identical burst_hours.jsonl", b1 == b2 and len(b1) > 1000)
check(
    "determinism: another seed changes peak_z but not n_transients",
    [h["acorr"]["peak_z"] for h in h1_] != [h["acorr"]["peak_z"] for h in h3_]
    and [h["n_transients"] for h in h1_] == [h["n_transients"] for h in h3_] == [400, 400],
    (h1_[0]["acorr"], h3_[0]["acorr"]),
)

# ============================================================ 6 ambient reader
amb = {M.H0 + 3600 * k: v for k, v in zip(range(0, 3), [1.0, 3.0, 7.0])}
chk6 = lambda am, h_: T.ambient_for(h_, am, TH)  # noqa: E731
check("ambient: a record at the hour start gives its temperature, age 0", chk6({M.H0: 5.5}, M.H0) == {"temp_c": 5.5, "age_h": 0, "dtdt_c_per_h": None})
check("ambient: only h-3 h gives age 3 and is used", chk6({M.H0 - 3 * 3600: 2.5}, M.H0)["temp_c"] == 2.5 and chk6({M.H0 - 3 * 3600: 2.5}, M.H0)["age_h"] == 3)
check("ambient twin: only h-4 h is too old, temperature null", chk6({M.H0 - 4 * 3600: 2.5}, M.H0) == {"temp_c": None, "age_h": None, "dtdt_c_per_h": None})
check("ambient: dT/dt for T=[1,3,7] around the middle hour is exactly 3.0", chk6(amb, M.H0 + 3600)["dtdt_c_per_h"] == 3.0)
check("ambient twin: dT/dt is null when a neighbour hour is missing", chk6({M.H0: 1.0, M.H0 + 3600: 3.0}, M.H0 + 3600)["dtdt_c_per_h"] is None)

# ============================================================ 7 statistics
x = np.arange(1.0, 21.0)
mono = T.spearman_block_boot(x, x * 2 + 1, None, block=6, resamples=200, seed=1)
check("spearman: monotone data give rho exactly 1.0 and a CI of exactly [1, 1]", mono["rho"] == 1.0 and mono["ci"] == [1.0, 1.0], mono)
check(
    "spearman: tied ranks give the hand-computed rho 0.7631578947 (7.25 / 9.5)",
    T.spearman(np.array([1, 2, 2, 3, 4.0]), np.array([1, 3, 2, 2, 5.0])) == 0.7631578947,
)
ind = T.spearman_block_boot(np.random.default_rng(6).normal(size=60), np.random.default_rng(7).normal(size=60), None, block=6, resamples=500, seed=1)
ind2 = T.spearman_block_boot(np.random.default_rng(6).normal(size=60), np.random.default_rng(7).normal(size=60), None, block=6, resamples=500, seed=1)
check(
    "bootstrap: independent seeded data give a CI that contains 0 and identical endpoints for the same seed",
    ind["ci"][0] < 0 < ind["ci"][1] and ind == ind2,
    ind,
)
check(
    "fisher exact: [[8,2],[4,16]] is 79761 / 30045015 and [[5,0],[0,5]] is 1/252",
    T.fisher_upper(8, 2, 4, 16) == 79761 / 30045015 and T.fisher_upper(5, 0, 0, 5) == 1 / 252,
)
check(
    "holm: p .01 .02 .04 in a family of 6 give .06 .10 .16",
    {k: round(v, 10) for k, v in T.holm({"a": 0.01, "b": 0.02, "c": 0.04}, 6).items()} == {"a": 0.06, "b": 0.1, "c": 0.16},
)
zr = np.random.default_rng(9)
z1 = zr.uniform(size=200)
px, py = z1 + 0.15 * zr.normal(size=200), z1 + 0.15 * zr.normal(size=200)
check(
    "partial spearman: x and y that share only a covariate z are strongly correlated raw (> 0.7) and uncorrelated (|rho| < 0.2) once z is removed",
    T.spearman(px, py) > 0.7 and abs(T.spearman(px, py, z1.reshape(-1, 1))) < 0.2,
    (T.spearman(px, py), T.spearman(px, py, z1.reshape(-1, 1))),
)


# ============================================================ 8 sample-size gates (T2 at 48, T3 at 72) and base-rate T1
def gate_hours(n_a, ambient_hours):
    rng_ = np.random.default_rng(8)
    raws = [M.raw_hour(M.H0 + 3600 * i, 1800, int(60 + 40 * rng_.random() + 20 * np.sin(i / 3.0))) for i in range(n_a)]
    am = {M.H0 + 3600 * i: round(5.0 + 4 * math.sin(i / 4.0), 2) for i in range(-3, ambient_hours + 3)}
    return fin(raws, am), am


for n_a, t2_status in ((47, "not_measurable"), (48, "ok"), (49, "ok")):
    hs, am = gate_hours(n_a, n_a)
    # hours of the 47/48/49 sample all have ambient; trailing hours beyond n_a do not exist
    ts_ = T.evaluate_tests(hs, am, TH)
    check(
        f"gate N_a={n_a}: T2 is {t2_status}" + (", n=%d" % n_a if t2_status == "ok" else ", reason pinned"),
        ts_["T2"]["status"] == t2_status
        and ts_["T2"]["n"] == n_a
        and (ts_["T2"]["reason"] == f"{n_a} covered hours with ambient < 48" if t2_status != "ok" else ts_["T2"]["reason"] is None),
        ts_["T2"],
    )
    check(
        f"gate N_a={n_a}: T3 is not_measurable with the largest sample ({n_a} hours) and the 72 h gate in its reason",
        ts_["T3"]["status"] == "not_measurable" and ts_["T3"]["reason"] == f"largest dT/dt sample {n_a} hours < 72",
        ts_["T3"],
    )
for n_a, st in ((71, "not_measurable"), (72, "ok")):
    hs, am = gate_hours(n_a, n_a)
    t3 = T.evaluate_tests(hs, am, TH)["T3"]
    check(
        f"gate twin: T3 with {n_a} hours with dT/dt is {st}",
        t3["status"] == st and (t3["n"] == 72 if st == "ok" else t3["reason"] == "largest dT/dt sample 71 hours < 72"),
        t3,
    )

# T1 base rate: the same cold burst hours, but a colder week (every hour cold) gives lift 1.0 and no refutation
raws, am = plan_hours("cold", 11)
cold_hours = fin(raws, am)
t1c = T.evaluate_tests(cold_hours, am, TH)["T1"]
check(
    "T1: cold bursts in a mixed week: cold share 1.0, base share well below 1, lift >= 1.25, Fisher p < 0.05",
    t1c["value"] == 1.0 and t1c["numbers"]["lift"] >= 1.25 and t1c["numbers"]["fisher_p"] < 0.05 and t1c["numbers"]["base_cold_share"] < 0.8,
    t1c,
)
rawsc, amc = plan_hours("cold", 11, temps=lambda h, t: 2.0 + 0.5 * math.sin(h / 7919.0))
allcold = fin(rawsc, amc)
t1a = T.evaluate_tests(allcold, amc, TH)["T1"]
va = T.verdicts(T.evaluate_tests(allcold, amc, TH), allcold, TH, True, 0)["insect"]
vc = T.verdicts(T.evaluate_tests(cold_hours, am, TH), cold_hours, TH, True, 0)["insect"]
check(
    "T1 base rate: when every covered hour is cold the cold share is 1.0 but the lift is 1.0, so the insect verdict is not refuted (inconclusive)",
    t1a["value"] == 1.0 and t1a["numbers"]["lift"] == 1.0 and va["verdict"] == "inconclusive",
    (t1a, va),
)
check("T1 base rate twin: the mixed week (cold bursts, warm days) is refuted by the same rule", vc["verdict"] == "refuted", vc)
rawsl, aml = plan_hours("cold", 11)
aml = {
    h_: (
        8.0 + 0.1 * (((h_ - M.H0) // 3600) % 3)
        if (((h_ - M.H0) // 3600) % 5 == 0 and not 4 <= ((M.H0 // 3600 + (h_ - M.H0) // 3600) % 24) <= 10)
        else 2.0 + 0.3 * math.sin((h_ - M.H0) // 3600)
    )
    for h_ in aml
}
hsl = fin(rawsl, aml)
tsl = T.evaluate_tests(hsl, aml, TH)
vl = T.verdicts(tsl, hsl, TH, True, 0)["insect"]
check(
    "T1 lift alone blocks refutation: all 21 burst hours cold, Fisher p 0.0158 < 0.05 and T2 CI upper < 0.30, but lift 1.18 < 1.25 gives inconclusive",
    tsl["T1"]["numbers"]["lift"] == 1.180328
    and tsl["T1"]["numbers"]["fisher_p"] < 0.05
    and tsl["T2"]["numbers"]["ci"][1] < 0.30
    and vl["verdict"] == "inconclusive",
    (tsl["T1"], vl),
)
warm_burst = [h["hour_utc"] for h in cold_hours if h["is_burst"] and h["ambient"]["temp_c"] >= 10.0]
rawsw = copy.deepcopy(raws)
for i in (15, 16):
    rawsw[i]["n_transients"] = 300
amw = dict(am)
hsw = fin(rawsw, amw)
for h in hsw:
    pass
tw = T.evaluate_tests(hsw, amw, TH)["T1"]["numbers"]["warm_burst_hours"]
check("T1: the two warm burst hours (afternoon, >= 10 C) are listed separately by hour", tw == [M.H0 + 3600 * 15, M.H0 + 3600 * 16], tw)


# ============================================================ 9 verdict table
def verd(raws_, am_, n_conf=0, pinned=True):
    hs_ = fin(raws_, am_)
    ts_ = T.evaluate_tests(hs_, am_, TH)
    return T.verdicts(ts_, hs_, TH, pinned, n_conf), ts_, hs_


v_cold, t_cold, _ = verd(raws, am)
check(
    "insect: cold bursts independent of temperature give refuted on 21 burst hours over 3 nights",
    v_cold["insect"]["verdict"] == "refuted" and v_cold["insect"]["n_burst_hours"] == 21 and v_cold["insect"]["numbers"]["nights"] == 3,
    v_cold["insect"],
)
raws_w, am_w = plan_hours("warm", 12)
v_warm, t_warm, _ = verd(raws_w, am_w)
check("insect twin: bursts in the warm afternoons with rate rising with T give supported", v_warm["insect"]["verdict"] == "supported", v_warm["insect"])
check(
    "electronics: refuted when the rate rises with temperature (T2 CI lower > 0.30), twin: not refuted when independent",
    v_warm["electronics"]["verdict"] == "refuted" and v_cold["electronics"]["verdict"] == "inconclusive",
    (v_warm["electronics"], v_cold["electronics"]),
)
check(
    "electronics: independent and stationary gives inconclusive with residual true and is listed in surviving",
    v_cold["electronics"]["residual"] is True
    and "electronics" in card(fin(raws, am), am)["surviving"]
    and "electronics" not in card(fin(raws_w, am_w), am_w)["surviving"],
)
v48, _, _ = verd(raws[:48], am)
check(
    "insect: only 2 nights of bursts (48 hours) refuses the decisive verdict: inconclusive with the nights reason; twin: 72 hours decide",
    v48["insect"]["verdict"] == "inconclusive" and v48["insect"]["basis"] == "decisive verdicts refused: burst hours span 2 distinct nights (< 3)",
    v48["insect"],
)
raws_n, _ = plan_hours("cold", 11)
v_noamb = verd(raws_n, None)[0]
check(
    "no ambient: insect, thermal and electronics are not-measurable, pulser is not affected by the missing file",
    [v_noamb[k]["verdict"] for k in ("insect", "thermal", "electronics")] == ["not-measurable"] * 3 and v_noamb["pulser"]["verdict"] == "not-measurable",
    v_noamb,
)


def pulser_hours(n_elig, peak_z, cv, lag=1.2, null=0, nights=(0, 1, 2)):
    out = []
    for i in range(n_elig + null):
        day = nights[i % len(nights)]
        out.append(
            M.raw_hour(
                M.H0 + 3600 * (24 * day + 4 + i // len(nights)), 1800, 400 if i < n_elig else 190, cv=cv, peak_z=peak_z if i < n_elig else None, peak_lag=lag
            )
        )
    return out


vp = verd(pulser_hours(6, 30.0, 0.1), None)[0]
check(
    "pulser: 6 periodic regular burst hours over 3 nights give supported; electronics is NOT refuted by it (still surviving)",
    vp["pulser"]["verdict"] == "supported"
    and vp["electronics"]["verdict"] != "refuted"
    and {"pulser", "electronics"} <= set(card(fin(pulser_hours(6, 30.0, 0.1), None))["surviving"]),
    vp["pulser"],
)
check(
    "pulser: the basis states that supported means clocked periodic, internal or external",
    "electronics with a clock would look the same" in vp["pulser"]["basis"],
)
vr = verd(pulser_hours(6, 2.0, 1.5), None)[0]
check("pulser twin: 6 non-periodic clustered burst hours give refuted", vr["pulser"]["verdict"] == "refuted", vr["pulser"])
v4 = verd(pulser_hours(4, 30.0, 0.1), None)[0]
check(
    "pulser: only 4 eligible burst hours is never decisive (inconclusive, 4 < 5 in the basis), twin 5 decides",
    v4["pulser"]["verdict"] == "inconclusive"
    and v4["pulser"]["basis"] == "4 burst hours with a periodicity test (< 5)"
    and verd(pulser_hours(5, 30.0, 0.1), None)[0]["pulser"]["verdict"] == "supported",
    v4["pulser"],
)
vn = verd(pulser_hours(6, 30.0, 0.1, null=10), None)[0]
check(
    "pulser: hours whose peak_z is null (fewer than 200 events) are not counted: 6 periodic of 6 eligible gives periodic_share 1.0 and n_eligible 6",
    vn["pulser"]["numbers"]["periodic_share"] == 1.0 and vn["pulser"]["numbers"]["n_eligible"] == 6 and vn["pulser"]["n_burst_hours"] == 16,
    vn["pulser"],
)
vq = verd(pulser_hours(6, 30.0, 1.5), None)[0]
check("pulser: periodic but clustered (cv 1.5) is neither supported nor refuted: inconclusive", vq["pulser"]["verdict"] == "inconclusive", vq["pulser"])
vlag = verd(
    [M.raw_hour(M.H0 + 3600 * (24 * (i % 3) + 4 + i // 3), 1800, 400, cv=0.1, peak_z=30.0, peak_lag=[0.5, 1.2, 2.3, 3.4, 4.5, 0.9][i]) for i in range(6)], None
)[0]
check(
    "pulser: periodic hours at scattered lags (consistent_lag < 0.8) are not supported",
    vlag["pulser"]["verdict"] != "supported" and vlag["pulser"]["numbers"]["consistent_lag"] < 0.8,
    vlag["pulser"],
)


# thermal
def board_hours(rhos, nights=(0, 1, 2)):
    out = []
    for i, r in enumerate(rhos):
        out.append(
            M.raw_hour(
                M.H0 + 3600 * (24 * nights[i % 3] + 4 + i // 3),
                1800,
                int(400 + 100 * r[0]),
                cv=1.0,
                board={"temp_local_c_median": 40.0, "dtdt_c_per_h": r[1], "coverage_frac": 1.0},
            )
        )
    return out


vts = verd(board_hours([(i, -0.1 * i) for i in range(6)]), None)[0]
vtr = verd(board_hours([(i, 0.1 * i) for i in range(6)]), None)[0]
vti = verd(board_hours([(0, -0.1), (5, -0.2), (1, -0.3), (4, -0.4), (3, -0.5), (2, -0.6)]), None)[0]
check(
    "thermal: rate rising with board cooling over 6 burst hours on 3 nights gives supported (T8 rho 1.0)",
    vts["thermal"]["verdict"] == "supported" and vts["thermal"]["numbers"]["rho"] == 1.0,
    vts["thermal"],
)
check(
    "thermal twin: rate falling with board cooling gives refuted",
    vtr["thermal"]["verdict"] == "refuted" and vtr["thermal"]["numbers"]["rho"] == -1.0,
    vtr["thermal"],
)
check("thermal twin: an unordered mix gives inconclusive", vti["thermal"]["verdict"] == "inconclusive", vti["thermal"])
check(
    "thermal: without board data and with fewer than 72 h of dT/dt it is not-measurable and says why (T8 reason)",
    v_noamb["thermal"]["verdict"] == "not-measurable" and "0 burst hours with board temperature coverage >= 0.8 < 5" in v_noamb["thermal"]["basis"],
    v_noamb["thermal"],
)
check(
    "thermal: with 72 h of dT/dt and no board it is never supported by ambient alone",
    v_cold["thermal"]["verdict"] in ("inconclusive", "refuted")
    and v_cold["thermal"]["verdict"] != "supported"
    and t_cold["T8"]["status"] == "not_measurable"
    and t_cold["T3"]["status"] == "ok",
    (v_cold["thermal"], t_cold["T3"]["status"]),
)

# ============================================================ 10 not-measurable honesty (T9)
base_h = pulser_hours(6, 30.0, 0.1)
for r_ in base_h:
    r_["coherence"] = {"n_records": 3}
hc0, hc1 = fin(base_h), fin([dict(r_, coherence={"n_records": 40}) for r_ in base_h])
t9a, t9b = T.evaluate_tests(hc0, None, TH)["T9"], T.evaluate_tests(hc1, None, TH)["T9"]
va0 = T.verdicts(T.evaluate_tests(hc0, None, TH), hc0, TH, True, 0)
va1 = T.verdicts(T.evaluate_tests(hc1, None, TH), hc1, TH, True, 0)
va2 = T.verdicts(T.evaluate_tests(fin([dict(r_, coherence=None) for r_ in base_h]), None, TH), fin([dict(r_, coherence=None) for r_ in base_h]), TH, True, 0)
check(
    "T9: with 3 coherence records per burst hour it is not_measurable and says 0 hours qualify",
    t9a["status"] == "not_measurable" and t9a["n"] == 0 and "0 burst hours with >= 30" in t9a["reason"],
    t9a,
)
check(
    "T9 twin: with 40 records per burst hour it is ok (6 hours) and still changes no verdict (no rule uses it)",
    t9b["status"] == "ok" and t9b["n"] == 6 and va0 == va1 == va2,
    (t9b, va0 == va1),
)

# ============================================================ 11 pre-registration, frozen boundary
check(
    "pre-registration: the sha256 of burst_source_thresholds.json equals the constant pinned here and in the tool",
    hashlib.sha256(open(TH_PATH, "rb").read()).hexdigest() == PINNED_SHA == T.PINNED_THRESHOLDS_SHA256,
)
d = D("pre")
M.write_data_dir(d, [{"hour": M.H0 + 3600 * i, "covered_s": 1800, "events": M.poisson_env(400, 1.0, np.random.default_rng(40 + i))} for i in range(2)])
sc_ok = sc_of(run(d, os.path.join(TMP, "pre_ok"))[2])
edited = json.load(open(TH_PATH))
edited["ambient"]["cold_c"] = 6.0
ep = os.path.join(TMP, "edited_thresholds.json")
json.dump(edited, open(ep, "w"))
sc_bad = sc_of(run(d, os.path.join(TMP, "pre_bad"), "--thresholds", ep)[2])
check(
    "pre-registration: the pristine file gives thresholds_pinned true and exploratory verdicts, sha recorded",
    sc_ok["thresholds_pinned"] is True
    and sc_ok["thresholds_sha256"] == PINNED_SHA
    and {v["evidence_class"] for v in sc_ok["verdicts"].values()} == {"exploratory"},
    sc_ok["thresholds_pinned"],
)
check(
    "pre-registration: a copy with one number edited gives thresholds_pinned false and evidence_class unpinned on every verdict",
    sc_bad["thresholds_pinned"] is False
    and sc_bad["thresholds_sha256"] != PINNED_SHA
    and {v["evidence_class"] for v in sc_bad["verdicts"].values()} == {"unpinned"},
    sc_bad["thresholds_pinned"],
)
F = TH["frozen_utc"]
check(
    "frozen_utc: an hour at frozen_utc is confirmatory, one second earlier is exploratory (twins)",
    T.evidence_class(F, TH) == "confirmatory" and T.evidence_class(F - 1, TH) == "exploratory",
)
check("frozen_utc is a literal 2026-10-27T01:00:00Z, later than the last inspected data (00:30 UTC)", F == 1793062800 and F > 1793061000)
for nconf, ev in ((4, "exploratory"), (5, "confirmatory")):
    hs_ = fin([M.raw_hour(F + 3600 * i, 1800, 400, peak_z=30.0, peak_lag=1.2, cv=0.1) for i in range(nconf)])
    s_ = card(hs_)
    check(
        f"confirmatory boundary: {nconf} confirmatory burst hours give evidence_class {ev}",
        {v["evidence_class"] for v in s_["verdicts"].values()} == {ev} and s_["confirmatory"]["n_burst_hours"] == nconf,
        [v["evidence_class"] for v in s_["verdicts"].values()],
    )
check(
    "exploratory/confirmatory comes only from frozen_utc: an hour before it is exploratory in the record, one at it is confirmatory",
    [h["evidence_class"] for h in fin([M.raw_hour(F - 3600, 1800, 100), M.raw_hour(F, 1800, 100)])] == ["exploratory", "confirmatory"],
)

# ============================================================ 12 digest merge
d = D("dig")
ev = M.poisson_env(400, 1.0, np.random.default_rng(50))
M.write_data_dir(d, [{"hour": M.H0, "covered_s": 2000, "events": ev}])
oD = os.path.join(TMP, "dig_out")
shutil.rmtree(oD, ignore_errors=True)
run(d, oD)
A = hours_of(oD)[0]
check("digest: hour H raw with covered_s 2000 is written with source raw", (A["hour_utc"], A["covered_s"], A["source"]) == (M.H0, 2000, "raw"), A)
d2 = D("dig2")
M.write_data_dir(d2, [{"hour": M.H0 + 7200, "covered_s": 1900, "events": ev}])  # H rotated out, a later hour present
run(d2, oD)
hs = {h["hour_utc"]: h for h in hours_of(oD)}
check(
    "digest: with H rotated out the next run gives H as source digest and a record identical to the raw one in every other field",
    M.H0 in hs
    and hs[M.H0]["source"] == "digest"
    and {k: v for k, v in hs[M.H0].items() if k != "source"} == {k: v for k, v in A.items() if k != "source"}
    and hs[M.H0 + 7200]["source"] == "raw",
    hs.get(M.H0),
)
d3 = D("dig3")
M.write_data_dir(d3, [{"hour": M.H0, "covered_s": 3000, "events": ev}])
run(d3, oD)
hs = {h["hour_utc"]: h for h in hours_of(oD)}
check(
    "digest: a rerun whose raw hour has a LARGER covered_s (3000) replaces the digest hour (source raw)",
    (hs[M.H0]["covered_s"], hs[M.H0]["source"]) == (3000, "raw"),
    hs[M.H0],
)
d4 = D("dig4")
M.write_data_dir(d4, [{"hour": M.H0, "covered_s": 1900, "events": ev[:300]}])
run(d4, oD)
hs = {h["hour_utc"]: h for h in hours_of(oD)}
check(
    "digest: a rerun with a SMALLER covered_s (1900 < 3000) does not replace it: covered 3000, 400 events, source digest",
    (hs[M.H0]["covered_s"], hs[M.H0]["n_transients"], hs[M.H0]["source"]) == (3000, 400, "digest"),
    hs[M.H0],
)
stale = copy.deepcopy(hs[M.H0 + 7200])
stale["is_burst"], stale["rate_per_covered_s"], stale["evidence_class"], stale["covered_s"] = False, 0.0, "confirmatory", 1850
other = copy.deepcopy(hs[M.H0 + 7200])
other["hour_utc"], other["features_version"] = M.H0 + 14400, 99
os.makedirs(os.path.join(TMP, "dig_stale"), exist_ok=True)
sp_ = os.path.join(TMP, "dig_stale")
M.write_jsonl(os.path.join(sp_, "burst_hours.jsonl"), [stale, other])
dd = D("dig5")
M.write_data_dir(dd, [{"hour": M.H0 + 21600, "covered_s": 1800, "events": ev}])
run(dd, sp_)
hs5 = {h["hour_utc"]: h for h in hours_of(sp_)}
check(
    "digest: stored derived fields are recomputed on load (stale is_burst false / evidence confirmatory become true / exploratory from the raw numbers)",
    hs5[M.H0 + 7200]["is_burst"] is True
    and hs5[M.H0 + 7200]["evidence_class"] == "exploratory"
    and hs5[M.H0 + 7200]["rate_per_covered_s"] == round(400 / 1850, 6),
    hs5[M.H0 + 7200],
)
check("digest: a row from another features_version is ignored (hour absent), the raw hour is kept", M.H0 + 14400 not in hs5 and M.H0 + 21600 in hs5)
dn = D("dig6")
M.write_data_dir(dn, [{"hour": M.H0 + 21600, "covered_s": 1800, "events": ev}])
before = open(os.path.join(sp_, "burst_hours.jsonl"), "rb").read()
code6, _, o6 = run(dn, sp_, "--no-write-digest")
check(
    "digest: --no-write-digest neither writes nor reads burst_hours.jsonl (file unchanged, report written, digest hour not in the scorecard span)",
    code6 == 0 and open(os.path.join(sp_, "burst_hours.jsonl"), "rb").read() == before and sc_of(o6)["data_span_utc"] == [M.H0 + 21600, M.H0 + 25200],
    sc_of(o6)["data_span_utc"],
)
d7 = D("dig7")
M.write_data_dir(d7, [{"hour": M.H0, "covered_s": 1800, "events": ev}])
o7 = os.path.join(TMP, "dig7_out")
shutil.rmtree(o7, ignore_errors=True)
run(d7, o7, "--no-write-digest")
check(
    "digest twin: with --no-write-digest and a fresh out dir only the scorecard files are written (no burst_hours.jsonl)",
    sorted(os.listdir(o7)) == ["burst_scorecard.json", "burst_scorecard.md"],
    os.listdir(o7),
)

# ============================================================ 13 read-only guarantees
d = D("ro")
M.write_data_dir(d, [{"hour": M.H0, "covered_s": 1800, "events": ev}], ambient={M.H0: 3.0}, spatial=True, bands=True)


def snapshot(root):
    return {
        os.path.relpath(os.path.join(a_, f), root): hashlib.sha256(open(os.path.join(a_, f), "rb").read()).hexdigest()
        for a_, _, fs in os.walk(root)
        for f in fs
    }


before = snapshot(d)
r_in = run(d, os.path.join(d, "analysis", "rep"))
r_hist = run(d, os.path.join(d, "history"))
r_par = run(d, d)
r_bb = run(d, os.path.join(d, "reports"))
check(
    "read-only: --out inside analysis/ exits 2 with the rule message and writes nothing there",
    r_in[0] == 2 and "--out must not equal or lie inside an input directory" in r_in[1] and not os.path.exists(os.path.join(d, "analysis", "rep")),
    r_in[:2],
)
check(
    "read-only: --out equal to history/ exits 2; --out as the parent of the inputs (the data dir) exits 2; --out as the beam_bands directory exits 2",
    (r_hist[0], r_par[0], r_bb[0]) == (2, 2, 2),
)
r_ok = run(d, os.path.join(d, "report_sibling"))
check(
    "read-only twin: a new sibling directory exits 0, inputs are unchanged by hash, and the only new files are the three report files",
    r_ok[0] == 0
    and {k: v for k, v in snapshot(d).items() if not k.startswith("report_sibling")} == before
    and sorted(os.listdir(r_ok[2])) == ["burst_hours.jsonl", "burst_scorecard.json", "burst_scorecard.md"],
    os.listdir(r_ok[2]),
)
check(
    "read-only: a data dir with no dsp file exits 3 with one line; usage errors (bad --since) exit 2",
    run(D("empty"))[0] == 3 and run(d, os.path.join(TMP, "x_since"), "--since", "nonsense")[0] == 2,
)

# ============================================================ 14 files opened
real_open = builtins.open
opened = []


def rec_open(file, mode="r", *a_, **k_):
    if isinstance(file, (str, bytes, os.PathLike)) and "r" in mode and "+" not in mode:
        opened.append(os.path.realpath(file))
    return real_open(file, mode, *a_, **k_)


def run_recorded(d_, out_, *extra):
    del opened[:]
    builtins.open = rec_open
    try:
        code = run(d_, out_, *extra)[0]
    finally:
        builtins.open = real_open
    rel = {os.path.relpath(p, os.path.realpath(d_)).replace(os.sep, "/") if p.startswith(os.path.realpath(d_)) else os.path.basename(p) for p in opened}
    return code, rel


d = D("opn")
M.write_data_dir(
    d,
    [{"hour": M.H0, "covered_s": 1800, "events": ev}],
    ambient={M.H0: 3.0},
    board=[(M.H0 + 60 * k, 40.0) for k in range(60)],
    coherence=[(M.H0, M.H0 + 60)],
    spatial=True,
    bands=True,
)
for extra in ("tags.jsonl", "birds.jsonl", "clap.jsonl", "novelty.jsonl", "emb_20261026.jsonl"):
    M.write_jsonl(os.path.join(d, "analysis", extra), ['{"k":"tags","t":1.0}'])
M.write_jsonl(os.path.join(d, "history", "ambient_temp.jsonl.1"), [])
M.write_jsonl(os.path.join(d, "analysis", "dsp.jsonl.1"), [])
M.write_jsonl(os.path.join(d, "analysis", "bursts.jsonl"), [])
oo = os.path.join(d, "reports", "o")
shutil.rmtree(oo, ignore_errors=True)
c1, s1 = run_recorded(d, oo)
EXP = {
    "analysis/dsp.jsonl",
    "analysis/dsp.jsonl.1",
    "analysis/spatial.jsonl",
    "analysis/coherence.jsonl",
    "analysis/bursts.jsonl",
    "history/ambient_temp.jsonl",
    "history/ambient_temp.jsonl.1",
    "history/board_history.jsonl",
    "reports/beam_bands.jsonl",
    "burst_source_thresholds.json",
}
check(
    "opened files: exactly dsp(.1), spatial, coherence, bursts, ambient(.1), board_history, the default beam_bands and the thresholds; never tags/birds/clap/novelty/emb",
    c1 == 0 and s1 == EXP,
    sorted(s1 ^ EXP),
)
c2, s2 = run_recorded(d, oo)
check(
    "opened files: the second run (digest present in the out dir) additionally opens exactly burst_hours.jsonl in the out dir (here reports/o)",
    c2 == 0 and s2 == EXP | {"reports/o/burst_hours.jsonl"},
    sorted(s2 ^ EXP),
)
c3, s3 = run_recorded(d, oo, "--no-write-digest")
check("opened files twin: --no-write-digest does not open the digest again", c3 == 0 and s3 == EXP, sorted(s3 ^ EXP))
other_bb = os.path.join(TMP, "elsewhere_bands.jsonl")
shutil.copyfile(os.path.join(d, "reports", "beam_bands.jsonl"), other_bb)
os.remove(os.path.join(d, "reports", "beam_bands.jsonl"))
c4, s4 = run_recorded(d, oo, "--no-write-digest", "--beam-bands", other_bb)
check(
    "opened files: --beam-bands FILE replaces the default location (exactly that one path instead)",
    c4 == 0 and s4 == (EXP - {"reports/beam_bands.jsonl"}) | {"elsewhere_bands.jsonl"},
    sorted(s4 ^ EXP),
)

# ============================================================ 15 robustness against garbage
d = D("clean")
evs = M.poisson_env(400, 1.0, np.random.default_rng(60))
M.write_data_dir(d, [{"hour": M.H0, "covered_s": 1800, "events": evs}], ambient={M.H0: 3.0})
dg = D("garb")
shutil.copytree(d, dg, dirs_exist_ok=True)
tr = '{"k":"transient","t":%s,"excess_db":%s,"level_db":-50.0,"loudest_beam":3,"beam_spread_db":2.0}'
with open(os.path.join(dg, "analysis", "dsp.jsonl"), "ab") as f:
    f.write((tr % ("1792800100.5", "NaN")).encode() + b"\n")  # NaN
    f.write((tr % ('"1792800101"', "9.0")).encode() + b"\n")  # string timestamp
    f.write((tr % ("1" + "0" * 400, "9.0")).encode() + b"\n")  # 400-digit integer
    f.write(
        b'{"k":"transient","t":1792800102.0,"excess_db":9.0,"level_db":-50.0,"loudest_beam":3,"beam_spread_db":2.0,"pad":"' + b"a" * (1 << 21) + b'"}\n'
    )  # oversize
    f.write(b'{"k":"transient","t":17928')  # truncated last line, no newline
cg, cc_ = run(dg, os.path.join(TMP, "garb_o"))[0], run(d, os.path.join(TMP, "clean_o"))[0]
sg, sc_c = sc_of(os.path.join(TMP, "garb_o")), sc_of(os.path.join(TMP, "clean_o"))
skipped = sg["inputs"]["skipped_lines"]
sg["inputs"]["skipped_lines"] = sc_c["inputs"]["skipped_lines"] = 0
check(
    "robustness: NaN, a string timestamp, a 400-digit integer, an oversize line and a truncated last line are counted in skipped_lines (exactly 5) with exit 0",
    cg == 0 and skipped == 5,
    skipped,
)
check(
    "robustness twin: the outputs equal those of the run without the garbage (hours file byte for byte, scorecard apart from skipped_lines)",
    sg == sc_c
    and open(os.path.join(TMP, "garb_o", "burst_hours.jsonl"), "rb").read() == open(os.path.join(TMP, "clean_o", "burst_hours.jsonl"), "rb").read()
    and sc_c["inputs"]["skipped_lines"] == 0,
)

# ============================================================ 16 privacy
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import check_private_paths as P  # noqa: E402

txt = "".join(
    open(os.path.join(TMP, "garb_o", f), encoding="utf-8").read() for f in ("burst_scorecard.json", "burst_scorecard.md", "burst_hours.jsonl")
) + json.dumps(M.__dict__.get("CONTRACT_NOW"))
keys = set()


def walk(o):
    if isinstance(o, dict):
        for k, v in o.items():
            keys.add(k)
            walk(v)
    elif isinstance(o, list):
        for v in o:
            walk(v)


walk(sg)
for h in hours_of(os.path.join(TMP, "garb_o")):
    walk(h)
check(
    "privacy: no lat/lon/latitude/longitude/path key in any output", not keys & {"lat", "lon", "latitude", "longitude", "path"}, keys & {"lat", "lon", "path"}
)
hits = [cat for cat, rx in P.PATTERNS if rx.search(txt)]
canary = [cat for cat, rx in P.PATTERNS if rx.search("see D:" + "\\work\\x and /home/" + "bob/x and 192.168." + "1.5")]
check(
    "privacy: no output text matches any category of scripts/check_private_paths.py (twin: the matcher does flag a drive path, a home path and a LAN address)",
    hits == [] and {"drive-path", "home-path", "lan-ip"} <= set(canary),
    (hits, canary),
)
check("privacy: the data dir is not echoed (no temp dir name in the outputs)", os.path.basename(dg) not in txt and TMP not in txt)

# ============================================================ 17 detector limits and caveats
two = T.ioi_features(np.asarray([M.H0 + 10.0, M.H0 + 10.02]), TH, MASK, M.H0)
check(
    "detector limits: two events 20 ms apart are accepted (one interval of 0.02 s) but min_observable_s is 0.04",
    (two["n"], two["median_s"], two["min_observable_s"]) == (1, 0.02, 0.04),
    two,
)
hs60 = fin([M.raw_hour(M.H0, 3600, 21600)])
hs49 = fin([M.raw_hour(M.H0, 3600, 17640)])
check(
    "detector limits: a 6.0/s hour sets detector_saturation_flag and the caveat text is present",
    hs60[0]["detector_saturation_flag"] is True and any("self-suppresses" in c_ for c_ in card(hs60)["caveats"]),
)
check(
    "detector limits twin: a 4.9/s hour does not set the flag and has no such caveat",
    hs49[0]["detector_saturation_flag"] is False and not any("self-suppresses" in c_ for c_ in card(hs49)["caveats"]),
)

# ============================================================ 18 optional inputs: bursts.jsonl, beam_bands default, spatial, board
db_ = D("opt")
hrs = [{"hour": M.H0, "covered_s": 1800, "events": np.sort(np.random.default_rng(70).uniform(1, 1799, 450))}]
burst_recs = [M.burst_stats(M.H0 + 60 * k) for k in range(60)]
M.write_data_dir(db_, hrs, ambient={M.H0: 3.0}, spatial=True, bands=True, bursts=burst_recs)
ob = os.path.join(TMP, "opt_o")
shutil.rmtree(ob, ignore_errors=True)
run(db_, ob)
hb = hours_of(ob)[0]
check(
    "bursts.jsonl: an hour with >= 1800 s of burst_stats records is ioi_source bursts, regularity 0.9, 3600 impulses",
    hb["ioi_source"] == "bursts"
    and hb["bursts"]["regularity"] == 0.9
    and hb["bursts"]["n_impulses"] == 3600
    and hb["bursts"]["band_8_16k_db_p50"] == -45.0
    and hb["bursts"]["spread_db_p50"] == 1.5,
    hb["bursts"],
)
dno = D("opt2")
M.write_data_dir(dno, hrs, ambient={M.H0: 3.0})
ob2 = os.path.join(TMP, "opt_o2")
shutil.rmtree(ob2, ignore_errors=True)
run(dno, ob2)
hn = hours_of(ob2)[0]
check(
    "bursts.jsonl twin: without the file the same hour is ioi_source transient with bursts null, spatial null, band null",
    (hn["ioi_source"], hn["bursts"], hn["spatial"], hn["band_8_16k"]) == ("transient", None, None, None),
    hn,
)
rb = [
    M.raw_hour(
        M.H0 + 3600 * (24 * (i % 3) + 4 + i // 3),
        1800,
        400,
        cv=2.0,
        bursts={
            "analysed_s": 3600.0,
            "n_impulses": 400,
            "regularity": reg_,
            "jitter_cv": 0.03,
            "mode_s": 0.75,
            "spread_db_p50": 1.5,
            "coincident_share": 0.5,
            "band_8_16k_db_p50": -45.0,
        },
    )
    for i, reg_ in enumerate([0.9] * 3 + [0.1] * 3)
]
t5 = T.evaluate_tests(fin(rb), None, TH)["T5"]
check(
    "T5: hours with burst_stats use its gap regularity (0.9 regular x3, 0.1 clustered x3) instead of cv_trim 2.0 (which alone would be clustered x6)",
    (t5["numbers"]["regular_n"], t5["numbers"]["clustered_n"]) == (3, 3),
    t5,
)
check(
    "beam_bands default: <data-dir>/reports/beam_bands.jsonl is read without a flag: burst 8-16 kHz level -45.0 vs quiet -90.0, spatial spread 1.5 burst vs 4.0 quiet",
    hb["band_8_16k"]["burst_db"] == -45.0
    and hb["band_8_16k"]["quiet_db"] == -90.0
    and (hb["spatial"]["spread_burst_db"], hb["spatial"]["spread_quiet_db"]) == (1.5, 4.0),
    (hb["band_8_16k"], hb["spatial"]),
)
check(
    "beam_bands default twin: absent file gives band_8_16k null and the caveat that the 8-16 kHz attribution is not confirmed",
    any("8-16 kHz attribution not confirmed in this run" in c_ for c_ in sc_of(ob2)["caveats"])
    and not any("8-16 kHz attribution not confirmed" in c_ for c_ in sc_of(ob)["caveats"]),
)
# board features
dbd = D("brd")
bsamples = [(M.H0 + 60 * k, 41.0 - 0.6 * k / 60.0) for k in range(60)]
M.write_data_dir(dbd, hrs, board=bsamples)
ob3 = os.path.join(TMP, "brd_o")
shutil.rmtree(ob3, ignore_errors=True)
run(dbd, ob3)
bd = hours_of(ob3)[0]["board"]
dbd2 = D("brd2")
M.write_data_dir(dbd2, hrs, board=bsamples[:47])
ob4 = os.path.join(TMP, "brd_o2")
shutil.rmtree(ob4, ignore_errors=True)
run(dbd2, ob4)
dbd3 = D("brd3")
M.write_data_dir(dbd3, hrs, board=bsamples[:48])
ob5 = os.path.join(TMP, "brd_o3")
shutil.rmtree(ob5, ignore_errors=True)
run(dbd3, ob5)
check("board: a linear fall of 0.6 C over the hour gives dtdt exactly -0.6 and coverage 1.0", (bd["dtdt_c_per_h"], bd["coverage_frac"]) == (-0.6, 1.0), bd)
check(
    "board: 47 of 60 samples (coverage 0.783) gives dtdt null, twin 48 samples (0.8) gives a slope",
    hours_of(ob4)[0]["board"]["dtdt_c_per_h"] is None
    and hours_of(ob4)[0]["board"]["coverage_frac"] == 0.783333
    and hours_of(ob5)[0]["board"]["dtdt_c_per_h"] is not None,
    hours_of(ob4)[0]["board"],
)
# since/until
dsu = D("su")
M.write_data_dir(dsu, [{"hour": M.H0 + 3600 * i, "covered_s": 1800, "events": ev} for i in range(3)])
osu = os.path.join(TMP, "su_o")
shutil.rmtree(osu, ignore_errors=True)
iso = lambda h_: time_iso(h_)  # noqa: E731


def time_iso(h_):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(h_))


run(dsu, osu, "--since", iso(M.H0 + 3600), "--until", iso(M.H0 + 7200))
check(
    "--since/--until: only the hour in [since, until) is in the scorecard (1 hour covered)",
    sc_of(osu)["data_span_utc"] == [M.H0 + 3600, M.H0 + 7200] and sc_of(osu)["n_hours_covered"] == 1,
    sc_of(osu)["data_span_utc"],
)

# ============================================================ 19 schema-shaped outputs, LF, self-check
raw_out = b"".join(open(os.path.join(ob, f), "rb").read() for f in ("burst_scorecard.json", "burst_hours.jsonl", "burst_scorecard.md"))
check(
    "outputs: LF line ends only, no BOM, no stray .tmp file in the out dir",
    b"\r" not in raw_out and not raw_out.startswith(b"\xef\xbb\xbf") and not [f for f in os.listdir(ob) if f.endswith(".tmp")],
)
sc_ = sc_of(ob)
check(
    "scorecard: the nine tests T1..T9 and the four verdicts are present",
    list(sc_["tests"]) == ["T%d" % i for i in range(1, 10)] and list(sc_["verdicts"]) == list(T.HYPOTHESES),
    list(sc_["tests"]),
)
gitok = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, cwd=HERE) if shutil.which("git") else None
if gitok is None or gitok.returncode != 0 or not os.path.isdir(os.path.join(gitok.stdout.strip(), "bench-kit")):
    print("SKIP scripts/check_private_paths.py exits 0 on the working tree  [no git checkout here]")
else:
    pp = subprocess.run(
        [sys.executable, "-I", os.path.join(HERE, "..", "scripts", "check_private_paths.py")], capture_output=True, text=True, cwd=os.path.join(HERE, "..")
    )
    check("scripts/check_private_paths.py exits 0 on the working tree", pp.returncode == 0, pp.stdout[-300:] + pp.stderr[-300:])
buf = io.StringIO()
t0 = time.time()
with contextlib.redirect_stdout(buf):
    code_sc = T.main(["--self-check"])
out_sc = buf.getvalue()
check(
    "--self-check exits 0 and prints the three scenario verdicts",
    code_sc == 0 and out_sc.count("ok   ") == 3 and '"insect": "refuted"' in out_sc and '"insect": "supported"' in out_sc and '"pulser": "supported"' in out_sc,
    out_sc,
)
print("self-check took %.1f s" % (time.time() - t0))

# ============================================================ 20 review fixes: incomplete thresholds, self-check temp dirs, pair cap, json eol
d = D("incomplete")
M.write_data_dir(d, [{"hour": M.H0, "covered_s": 1800, "events": M.poisson_env(400, 1.0, np.random.default_rng(77))}])
for label, edit, want in (
    ("a missing top-level section", lambda t: t.pop("ioi"), "missing: ioi"),
    ("a missing nested key", lambda t: t["acorr"].pop("min_lag_s"), "missing: acorr.min_lag_s"),
    ("a section that is not an object", lambda t: t.__setitem__("acorr", 5), "missing: acorr"),
):
    th_ = json.load(open(TH_PATH))
    edit(th_)
    tp = os.path.join(TMP, "incomplete_thresholds.json")
    json.dump(th_, open(tp, "w"))
    code_i, msg_i, _ = run(d, os.path.join(TMP, "inc_out"), "--thresholds", tp)
    check(
        "thresholds with " + label + ": exit 2 and a message naming it, not a traceback",
        code_i == 2 and want in msg_i and "unreadable or incomplete" in msg_i,
        (code_i, msg_i),
    )
th_ = json.load(open(TH_PATH))
th_["extra_unused_section"] = {"x": 1}
tp = os.path.join(TMP, "extra_thresholds.json")
json.dump(th_, open(tp, "w"))
code_i, msg_i, _ = run(d, os.path.join(TMP, "inc_out2"), "--thresholds", tp)
check(
    "twin: a complete thresholds file with one extra section still runs (exit 0)",
    code_i == 0 and msg_i.startswith("burst_source_report: hours=1"),
    (code_i, msg_i),
)

tdir = D("selfcheck_tmp")
old_tmp = tempfile.tempdir
tempfile.tempdir = tdir
try:
    with contextlib.redirect_stdout(io.StringIO()):
        code_sc2 = T.main(["--self-check"])
finally:
    tempfile.tempdir = old_tmp
check("--self-check leaves no bsr_selfcheck_* directory behind (and still exits 0)", code_sc2 == 0 and os.listdir(tdir) == [], (code_sc2, os.listdir(tdir)))
leak = tempfile.mkdtemp(prefix="bsr_selfcheck_", dir=tdir)
check("twin: the leftover check sees a directory that is there", os.path.basename(leak) in os.listdir(tdir), os.listdir(tdir))

rng_ = np.random.default_rng(5)
for label, tt in (
    ("random", np.sort(rng_.random(3000) * 3600)),
    ("duplicate timestamps", np.full(500, 100.0)),
    ("mixed", np.sort(np.concatenate([np.full(50, 7.0), rng_.random(400) * 60]))),
):
    check(
        "count_pairs equals the pair count of pair_hist (" + label + ")",
        T.count_pairs(tt, 2.0) == T.pair_hist(tt, 0.02, 100, 2.0)[1],
        (T.count_pairs(tt, 2.0), T.pair_hist(tt, 0.02, 100, 2.0)[1]),
    )
calls = []
orig_ph = T.pair_hist
T.pair_hist = lambda *a_, **k_: (calls.append(1), orig_ph(*a_, **k_))[1]  # recording shim, delegates
try:
    dup = np.full(2000, 3600.0 + 10)
    ac_cap = T.autocorrelogram(dup, 3600, TH, 1, 200, 1000)
    n_cap = len(calls)
    del calls[:]
    ac_ok = T.autocorrelogram(dup, 3600, TH, 1, 200, 600_000_000)
    n_ok = len(calls)
finally:
    T.pair_hist = orig_ph
check(
    "pair cap: 2000 events at one timestamp with a tiny cap skip the test without building any histogram",
    ac_cap["n_surrogates"] == 0 and ac_cap["peak_z"] is None and n_cap == 0,
    (ac_cap, n_cap),
)
check(
    "pair cap twin: with the default cap the same events build the real histogram and every surrogate (1 + n_surrogates calls)",
    ac_ok["n_surrogates"] > 0 and n_ok == 1 + ac_ok["n_surrogates"],
    (ac_ok["n_surrogates"], n_ok),
)

ga = (
    subprocess.run(
        [
            "git",
            "check-attr",
            "eol",
            "--",
            "burst_source_thresholds.json",
            "../contracts/fixtures/ambient_temp.jsonl",
            "../contracts/fixtures/analysis_status.json",
        ],
        capture_output=True,
        text=True,
        cwd=HERE,
    )
    if shutil.which("git")
    else None
)
if ga is None or ga.returncode != 0:
    print("SKIP .gitattributes pins eol=lf for json/jsonl  [no git checkout here]")
else:
    check("gitattributes: eol is lf for the pinned thresholds file and for json and jsonl fixtures", ga.stdout.count(": eol: lf") == 3, ga.stdout)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{n_checks - len(fails)}/{n_checks} checks passed")
if fails:
    print("FAILED:", *fails, sep="\n  ")
sys.exit(1 if fails else 0)
