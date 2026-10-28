#!/usr/bin/env python3
"""Tests for analysis/artifact_gate.py. Script style (check(name, ok, detail), exit 1 on a failure) like test_coherence.py; run:
    python -I test_artifact_gate.py
Exact values only; the unit under test is never stubbed; every negative case has a positive twin. Synthetic data only."""
import copy, json, math, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import artifact_gate as G  # noqa: E402

n_checks, fails = 0, []


def check(name, cond, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  [" + str(detail) + "]"))
    if not cond:
        fails.append(name)


def raises(exc, fn, match):
    """True only when fn raises exactly this exception type AND its message matches the regex."""
    import re
    try:
        fn()
    except exc as e:
        return bool(re.search(match, str(e))) and type(e) is exc
    except Exception:                                                  # noqa: BLE001
        return False
    return False


H = 1_792_972_800                                                      # an hour boundary (multiple of 3600)
assert H % 3600 == 0
T0 = H + 1800.0


def amb(*pairs):
    return G.AmbientIndex([{"k": "ambient_temp", "v": 1, "t_hour_utc": h, "temp_c": c, "source": "open-meteo", "model_grid_km": None} for h, c in pairs])


def trans(t, n, spread=1.0, dt=0.1):
    """n transients with the given spread, the first at t - dt * (n - 1) / 2 ... all inside +-30 s of t."""
    return G.TransientIndex([(t - dt * (n - 1) / 2 + dt * i, spread) for i in range(n)])


def ev(group="insect", label="Common Virtuoso Katydid", sci="Neoconocephalus triops", t=T0, ambient=None, transients=None, cfg=G.DEFAULT):
    return G.evaluate(group=group, label=label, scientific=sci, t=t, ambient=ambient, transients=transients, cfg=cfg)


# ------------------------------------------------------------------ 1 AmbientIndex vs specs/ambient-temp.md
a = amb((H, 3.4), (H + 3600, 2.9))
check("temp_at: the event's own hour record is used, age 0", a.temp_at(H + 3600 + 5) == (2.9, 0), a.temp_at(H + 3600 + 5))
a3 = amb((H, 5.0))
check("temp_at: hour H record at event hour H + 3 h (age exactly 10800 s) is usable", a3.temp_at(H + 10800 + 1) == (5.0, 10800), a3.temp_at(H + 10800 + 1))
check("temp_at: the same record one hour later (age 14400 s) is unknown (positive twin above)", a3.temp_at(H + 14400 + 1) == (None, None), a3.temp_at(H + 14400 + 1))
check("temp_at: a later hour is never used (event in H, only H+3600 exists)", amb((H + 3600, 1.0)).temp_at(H + 10) == (None, None))
check("temp_at: twin, the same record is used from its own hour on", amb((H + 3600, 1.0)).temp_at(H + 3600 + 10) == (1.0, 0))
check("temp_at: duplicate hour, the last record wins", amb((H, 1.0), (H, 2.0)).temp_at(H + 5) == (2.0, 0))
garbage = [None, 5, "x", [], {"k": "ambient_temp", "v": 2, "t_hour_utc": H, "temp_c": 9.0}, {"k": "ambient_temp", "v": 1, "t_hour_utc": H + 1, "temp_c": 9.0},
           {"k": "ambient_temp", "v": 1, "t_hour_utc": H, "temp_c": float("nan")}, {"k": "ambient_temp", "v": 1, "t_hour_utc": H, "temp_c": 61},
           {"k": "ambient_temp", "v": 1, "t_hour_utc": H, "temp_c": -91}, {"k": "ambient_temp", "v": 1, "t_hour_utc": True, "temp_c": 3},
           {"k": "ambient_temp", "v": 1, "t_hour_utc": H, "temp_c": "3"}, {"k": "other", "v": 1, "t_hour_utc": H, "temp_c": 3},
           {"k": "ambient_temp", "v": 1, "t_hour_utc": H, "temp_c": 7.5}]
ag = G.AmbientIndex(garbage)
check("AmbientIndex: 12 garbage records skipped and counted, the one good record (7.5 C) is the only one kept", ag.skipped == 12 and len(ag) == 1 and ag.temp_at(H + 1) == (7.5, 0), (ag.skipped, len(ag)))
check("AmbientIndex: empty gives (None, None) and span (None, None)", G.AmbientIndex([]).temp_at(H) == (None, None) and G.AmbientIndex([]).span() == (None, None))
check("AmbientIndex: span is (first, last) hour", amb((H + 7200, 1.0), (H, 2.0)).span() == (H, H + 7200))
check("temp_at: a non-finite t raises ValueError matching 't must be finite'", raises(ValueError, lambda: a.temp_at(float("nan")), "t must be finite")
      and raises(ValueError, lambda: a.temp_at(float("inf")), "t must be finite"))
check("temp_at: twin, a finite t does not raise", a.temp_at(H + 1) == (3.4, 0))
check("temp_at: a negative-hour event before every record is unknown, not an IndexError", a.temp_at(-5.0) == (None, None))

# ------------------------------------------------------------------ 2 floor boundary twins
def flags_at(temp, group="insect", label="Common Virtuoso Katydid"):
    r = ev(group=group, label=label, ambient=amb((H, temp)))
    return None if r is None else r["flags"]


check("floor: 4.9 C insect is cold_insect", flags_at(4.9) == ["cold_insect"], flags_at(4.9))
check("floor: exactly 5.0 C is NOT flagged (strict <)", flags_at(5.0) == [], flags_at(5.0))
check("floor: 5.1 C is not flagged", flags_at(5.1) == [], flags_at(5.1))
check("floor: the same 4.9 C with a bird label gives None (twin: the insect above is flagged)", flags_at(4.9, group="bird", label="Northern Cardinal") is None)

# ------------------------------------------------------------------ 3 cold_insect twin
cold = ev(ambient=amb((H, 2.0)))
warm = ev(ambient=amb((H, 16.0)))
check("cold_insect: insect at 2.0 C is likely with exact evidence", cold == {"v": 1, "flags": ["cold_insect"], "level": "likely", "evidence": {
    "temp_c": 2.0, "temp_age_s": 0, "floor_c": 5.0, "burst_n": None, "burst_spread_db": None, "window_s": None}}, cold)
check("cold_insect: the same insect at 16.0 C has no flags and level none", warm["flags"] == [] and warm["level"] == "none" and warm["evidence"]["temp_c"] == 16.0, warm)

# ------------------------------------------------------------------ 4 burst boundaries
def burst(trs, ambient=None):
    r = ev(ambient=ambient if ambient is not None else amb((H, 16.0)), transients=trs)
    return r["flags"], r["evidence"]["burst_n"], r["evidence"]["burst_spread_db"]


check("burst: 19 transients inside the window do not flag", burst(trans(T0, 19))[0] == [] and burst(trans(T0, 19))[1] == 19)
check("burst: 20 transients flag burst_coincident (twin of the 19 case)", burst(trans(T0, 20))[0] == ["burst_coincident"] and burst(trans(T0, 20))[1] == 20)
check("burst: median spread exactly 3.0 flags", burst(trans(T0, 20, 3.0))[0] == ["burst_coincident"])
check("burst: median spread 3.1 does not flag (twin of 3.0)", burst(trans(T0, 20, 3.1))[0] == [] and burst(trans(T0, 20, 3.1))[2] == 3.1)
edge_in = G.TransientIndex([(T0 + 30.0, 1.0)] * 20)
edge_out = G.TransientIndex([(T0 + 30.001, 1.0)] * 20)
check("burst: transients at exactly t + 30 are inside (20 flag)", burst(edge_in)[0] == ["burst_coincident"] and burst(edge_in)[1] == 20)
check("burst: transients at t + 30.001 are outside (n 0, no flag)", burst(edge_out) == ([], 0, None), burst(edge_out))
check("burst: transients at exactly t - 30 are inside, at t - 30.001 outside", burst(G.TransientIndex([(T0 - 30.0, 1.0)] * 20))[1] == 20 and burst(G.TransientIndex([(T0 - 30.001, 1.0)] * 20))[1] == 0)
check("burst: a 45 s offset is outside the pinned 30 s window", burst(G.TransientIndex([(T0 + 45.0, 1.0)] * 40))[1] == 0)
even = G.TransientIndex([(T0 + i, s) for i, s in enumerate([1.0, 2.0, 3.0, 9.0] * 5)])          # 20 values: 10 at <=... sorted-by-spread middle pair 2.0 / 3.0
check("burst: the median over an even count is the exact mean of the two middle values (2.5)", even.window(T0, 30) == (20, 2.5), even.window(T0, 30))
check("burst: that median 2.5 is the evidence value", burst(even)[2] == 2.5 and burst(even)[0] == ["burst_coincident"])
check("TransientIndex.window: empty window gives (0, None)", G.TransientIndex([]).window(T0, 30) == (0, None))
tx = G.TransientIndex([(1.0, 1.0), (float("nan"), 1.0), (2.0, float("inf")), (3.0, -1.0), "x", (4.0, 2.0), None])
check("TransientIndex: 5 hostile pairs skipped and counted, the 2 good kept", tx.skipped == 5 and len(tx) == 2, (tx.skipped, len(tx)))

# ------------------------------------------------------------------ 5 level matrix
b20, b0 = trans(T0, 25, 1.0), trans(T0, 3, 1.0)
cases = [("cold,burst", amb((H, 2.0)), b20, ["burst_coincident", "cold_insect"], "likely"),
         ("cold,noburst", amb((H, 2.0)), b0, ["cold_insect"], "likely"),
         ("warm,burst", amb((H, 16.0)), b20, ["burst_coincident"], "suspect"),
         ("warm,noburst", amb((H, 16.0)), b0, [], "none"),
         ("unknown,burst", G.AmbientIndex([]), b20, ["burst_coincident", "temp_unknown"], "suspect"),
         ("unknown,noburst", G.AmbientIndex([]), b0, ["temp_unknown"], "none")]
for name, am, tr, want_flags, want_level in cases:
    r = ev(ambient=am, transients=tr)
    check("level matrix %s -> %s %s" % (name, want_level, want_flags), r["flags"] == want_flags and r["level"] == want_level, r)
check("ambient=None behaves as temp_unknown and keeps burst evidence", ev(ambient=None, transients=b20)["flags"] == ["burst_coincident", "temp_unknown"])
check("transients=None leaves burst fields null and adds no burst flag", ev(ambient=amb((H, 16.0)), transients=None)["evidence"]["burst_n"] is None
      and ev(ambient=amb((H, 16.0)), transients=None)["flags"] == [])
check("a record 4 h old is temp_unknown (the 3 h age limit applies to the gate)", ev(ambient=amb((H, 2.0)), t=H + 4 * 3600 + 5.0)["flags"] == ["temp_unknown"])
check("twin: a record 3 h old is used (cold_insect)", ev(ambient=amb((H, 2.0)), t=H + 3 * 3600 + 5.0)["flags"] == ["cold_insect"])

# ------------------------------------------------------------------ 6 scope invariant over every label of ui/taxa.json
with open(os.path.join(HERE, "..", "ui", "taxa.json"), encoding="utf-8") as f:
    TAXA = json.load(f)
labels = []                                                         # (label, scientific, group)
for lab, v in TAXA["explicit_labels"].items():
    labels.append((lab, lab, v["group"]))
for grp, gs in TAXA["genera"].items():
    for g in gs:
        labels.append((g.capitalize() + " sample", g.capitalize() + " sample", grp))
by_group = {}
for lab, sci, grp in labels:
    by_group[grp] = by_group.get(grp, 0) + 1
check("taxa table read: 11 explicit labels + 1834 genera (bird 1798, insect 19, amphibian 11, mammal 6) = 1845 labels",
      len(labels) == 1845 and by_group == {"mechanical": 10, "mammal": 7, "bird": 1798, "amphibian": 11, "insect": 19}, by_group)
violations, got = [], {"none": 0, "evaluated": 0, "mechanical_label": 0}
hostile = amb((H, -10.0))
for lab, sci, grp in labels:
    r = ev(group=grp, label=lab, sci=sci, ambient=hostile, transients=trans(T0, 500, 0.1))
    hidden = G.is_hidden_label(lab, sci)
    if grp in ("bird", "mammal", "amphibian") and r is not None:
        violations.append(lab)
    if r is None:
        got["none"] += 1
    elif "mechanical_label" in r["flags"]:
        got["mechanical_label"] += 1
        if r["level"] != "none" or r["flags"] != ["mechanical_label"]:
            violations.append(lab)
    else:
        got["evaluated"] += 1
        if grp != "insect":
            violations.append(lab)
check("scope: out-of-scope groups (1798 bird + 7 mammal + 11 amphibian) and 3 hidden labels give None at -10 C with 500 transients; 7 non-hidden mechanical give "
      "mechanical_label only; 19 insect genera are evaluated; no violations", got == {"none": 1798 + 7 + 11 + 3, "mechanical_label": 7, "evaluated": 19} and violations == [], (got, violations[:5]))
for lab, sci in (("Red-bellied Woodpecker", "Melanerpes carolinus"), ("White-breasted Nuthatch", "Sitta carolinensis"), ("Great Horned Owl", "Bubo virginianus")):
    check("scope: %s at -10 C with 500 transients gives None" % lab, ev(group="bird", label=lab, sci=sci, ambient=hostile, transients=trans(T0, 500, 0.1)) is None)
check("scope twin: an insect label at the same inputs is likely", ev(ambient=hostile, transients=trans(T0, 500, 0.1))["level"] == "likely")
check("scope: group None and group other-bio give None", ev(group=None, ambient=hostile) is None and ev(group="other-bio", ambient=hostile) is None)

# ------------------------------------------------------------------ 7 hidden labels
for lab in ("Human vocal", "Human non-vocal", "Human whistle", "Speech", "  speech ", "HUMAN VOCAL"):
    check("hidden: %r gives None for the insect group at -10 C with a burst" % lab, ev(group="insect", label=lab, sci=lab, ambient=hostile, transients=trans(T0, 50)) is None)
    check("hidden: %r gives None for the mechanical group" % lab, ev(group="mechanical", label=lab, sci=lab, ambient=hostile) is None)
check("hidden: a hidden scientific name hides the row even when the label looks harmless", ev(group="mechanical", label="Noise", sci="Human vocal", ambient=hostile) is None)
check("hidden twin: Engine (mechanical) gives mechanical_label, level none",
      ev(group="mechanical", label="Engine", sci="Engine", ambient=hostile) == {"v": 1, "flags": ["mechanical_label"], "level": "none", "evidence": {
          "temp_c": None, "temp_age_s": None, "floor_c": None, "burst_n": None, "burst_spread_db": None, "window_s": None}})
check("is_hidden_label: Humming-style and ordinary names are not hidden, non-strings ignored",
      [G.is_hidden_label(x) for x in ("Hummingbird", "Engine", "Speechless Warbler", None, 5)] == [False, False, False, False, False])
check("is_hidden_label: any of several names hides", G.is_hidden_label("Engine", "Speech") is True and G.is_hidden_label("Engine", "Noise") is False)

# ------------------------------------------------------------------ 8 determinism, no mutation
am8, tr8 = amb((H, 2.04), (H + 3600, 2.06)), trans(T0, 30, 1.26)
before = (copy.deepcopy(am8.__dict__), copy.deepcopy(tr8.__dict__), tuple(getattr(G.DEFAULT, k) for k in G.GateConfig.__slots__))
r1, r2 = ev(ambient=am8, transients=tr8), ev(ambient=am8, transients=tr8)
check("determinism: two evaluations serialise byte-identically", json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True))
check("determinism: arguments and config unchanged afterwards", before == (am8.__dict__, tr8.__dict__, tuple(getattr(G.DEFAULT, k) for k in G.GateConfig.__slots__)))
check("rounding: temperature and spread are rounded to 1 decimal in the evidence", r1["evidence"]["temp_c"] == 2.0 and r1["evidence"]["burst_spread_db"] == 1.3, r1["evidence"])
check("flags are sorted, counts are ints", r1["flags"] == ["burst_coincident", "cold_insect"] and isinstance(r1["evidence"]["burst_n"], int) and r1["evidence"]["burst_n"] == 30)

# ------------------------------------------------------------------ 9 hostile input
for bad in (float("nan"), float("inf"), 1e400, -100.0, 61.0):
    ai = G.AmbientIndex([{"k": "ambient_temp", "v": 1, "t_hour_utc": H, "temp_c": bad}])
    r = ev(ambient=ai)
    check("hostile temp %r is unusable: temp_unknown, no cold_insect, no exception" % bad, r["flags"] == ["temp_unknown"] and r["evidence"]["temp_c"] is None, r)
check("hostile twin: -89.0 C is a usable temperature and flags cold_insect", ev(ambient=amb((H, -89.0)))["flags"] == ["cold_insect"])
for badt in (float("nan"), float("inf"), float("-inf")):
    check("evaluate: t=%r raises ValueError matching 't must be finite'" % badt, raises(ValueError, lambda: ev(t=badt, ambient=amb((H, 1.0))), "t must be finite"))
check("evaluate: a boolean t is refused as not finite-number", raises(ValueError, lambda: ev(t=True, ambient=amb((H, 1.0))), "t must be finite"))

# ------------------------------------------------------------------ 10 config
seven = amb((H, 7.0))
check("config: 7 C is not flagged at the default floor 5", ev(ambient=seven)["flags"] == [])
c10 = G.GateConfig(floors_c=(("insect", 10.0),))
check("config: floor 10 C flags the same 7 C case", ev(ambient=seven, cfg=c10)["flags"] == ["cold_insect"] and ev(ambient=seven, cfg=c10)["evidence"]["floor_c"] == 10.0)
cnf = G.GateConfig(floors_c=())
r = ev(ambient=amb((H, -20.0)), cfg=cnf)
check("config: an insect group without a floor has no temperature rule (no cold_insect, no temp_unknown, floor null)", r["flags"] == [] and r["evidence"]["floor_c"] is None and r["evidence"]["temp_c"] is None, r)
cw = G.GateConfig(burst_window_s=60.0)
check("config: a 60 s window counts a 45 s offset the pinned 30 s default does not (default 0, 60 s window 40)",
      burst(G.TransientIndex([(T0 + 45.0, 1.0)] * 40))[1] == 0 and ev(ambient=amb((H, 16.0)), transients=G.TransientIndex([(T0 + 45.0, 1.0)] * 40), cfg=cw)["evidence"]["burst_n"] == 40)
check("config: defaults are the design's (floor 5.0, age 10800, window 30, n 20, spread 3.0)",
      (G.DEFAULT.floors_c, G.DEFAULT.temp_max_age_s, G.DEFAULT.burst_window_s, G.DEFAULT.burst_min_transients, G.DEFAULT.burst_max_spread_db)
      == ((("insect", 5.0),), 10800, 30.0, 20, 3.0))
check("config: the constants REASONS and LEVELS are the closed vocabulary", G.REASONS == ("cold_insect", "burst_coincident", "temp_unknown", "mechanical_label") and G.LEVELS == ("none", "suspect", "likely"))

print("\n%d checks, %d failed" % (n_checks, len(fails)))
for f in fails:
    print("FAILED: " + f)
sys.exit(1 if fails else 0)
