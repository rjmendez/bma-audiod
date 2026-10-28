#!/usr/bin/env python3
"""Tests for analysis/review_queue.py (verdict-labels 1.0.0).  Pure functions over hand-built events; exact values written by hand; a
positive twin for every negative case; the unit under test is never stubbed.   python -I test_review_queue.py   (stdlib only)"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
BMA = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import labels as L  # noqa: E402
import review_queue as Q  # noqa: E402

TAXA = L.Taxa(os.path.join(BMA, "ui", "taxa.json"))
fails, n_checks = [], 0


def check(name, cond, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  [" + str(detail) + "]"))
    if not cond:
        fails.append(name)


def raises(exc, match, fn):
    try:
        fn()
    except exc as e:
        return match in str(e) and type(e) is exc
    except Exception:           # noqa: BLE001
        return False
    return False


T0 = 1792900000.0
NOW = T0 + 10 ** 6


def sp(i):
    return ("Species %02d" % i, "Genus%02d species" % i)


class World:
    """Events spaced 20 s apart; every event gets a quiet tags row 1 s after its start (so none is speech_unknown)."""
    def __init__(self):
        self.rows, self.tags, self.n = [], [], 0

    def add(self, species, conf, beams=(1,), region=True, k="bird", t=None, tag=True, **kw):
        t = T0 + 20.0 * self.n + 0.25 if t is None else t
        self.n += 1
        for j, b in enumerate(beams):
            r = {"k": k, "t": t + 0.1 * j, "beam": b, "level_dbfs": -60.0, "common": species[0], "scientific": species[1], "confidence": conf, "provenance": "model_opinion"}
            if region is not None and k == "bird":
                r["in_region"] = region
            r.update(kw)
            self.rows.append(r)
        if tag:
            self.tags.append({"k": "tags", "t": t + 1.0, "beam": 1, "speech_p": 0.0, "speech_active": False, "top": [], "emb_stored": True})
        return t

    def build(self, taxa=None, folded=None, ambient=None, **params):
        evs = L.iter_events(self.rows, taxa or L.Taxa())
        return Q.build_queue(evs, folded or {}, L.Tags(self.tags), params, NOW, ambient=ambient, taxa_loaded=bool(taxa and taxa.loaded)), evs


def by_species(q, sel=None):
    out = {}
    for i in q["items"]:
        if sel is None or i["selection"] == sel:
            out[i["species_key"]] = out.get(i["species_key"], 0) + 1
    return out


BIG = dict(days=1e5, limit=1000, audit_frac=0.0, per_species_cap=1000)

# ---------------------------------------------------------------------------------------------- strata and thresholds
w = World()
w.add(sp(1), 0.499, (1, 2))
w.add(sp(2), 0.500, (1, 2))
w.add(sp(3), 0.849, (1, 2))
w.add(sp(4), 0.850, (1, 2))
w.add(sp(5), 0.850, (1,))
q, _ = w.build(**BIG)
check("strata: 0.499 low, 0.500 near, 0.849 near, 0.850 on 2 beams hi, 0.850 on 1 beam disagree: exact counts",
      q["eligible"]["by_stratum"] == {"disagree": 1, "near": 2, "low": 1, "hi": 1, "artifact_probe": 0}, q["eligible"]["by_stratum"])
check("strata: the uncertainty picks are exactly the 4 non-hi events (hi enters only through the audit slice), each with the right stratum",
      sorted((i["species_key"], i["stratum"]) for i in q["items"]) == [("genus01 species", "low"), ("genus02 species", "near"), ("genus03 species", "near"), ("genus05 species", "disagree")])
check("the single-beam 0.85 event carries the single_beam_high flag and no other", [i["flags"] for i in q["items"] if i["species_key"] == "genus05 species"] == [["single_beam_high"]])
q2, _ = w.build(**dict(BIG, near_hi=0.9))
check("positive twin: with near_hi 0.9 the 0.85 events are near (3 near + the single beam one is near too, not disagree)",
      q2["eligible"]["by_stratum"] == {"disagree": 0, "near": 4, "low": 1, "hi": 0, "artifact_probe": 0}, q2["eligible"]["by_stratum"])

# ---------------------------------------------------------------------------------------------- caps, round robin, ranking
w = World()
for _ in range(60):
    w.add(sp(1), 0.6, (1, 2))
for _ in range(10):
    w.add(sp(2), 0.6, (1, 2))
q, _ = w.build(days=1e5, limit=1000, audit_frac=0.0, per_species_cap=25)
check("cap: 60 eligible with cap 25 yields exactly 25; 10 eligible yields exactly 10 (not padded)", by_species(q) == {"genus01 species": 25, "genus02 species": 10}, by_species(q))
q, _ = w.build(days=1e5, limit=1000, audit_frac=0.0, per_species_cap=1000)
check("positive twin: with the cap lifted all 60 + 10 are queued", by_species(q) == {"genus01 species": 60, "genus02 species": 10})
w = World()
for s in range(1, 7):
    for _ in range(30):
        w.add(sp(s), 0.6, (1, 2))
q, _ = w.build(days=1e5, limit=12, audit_frac=0.0, per_species_cap=1000)
check("round robin: limit 12 over 6 species x 30 candidates gives exactly 2 each", by_species(q) == {"genus0%d species" % s: 2 for s in range(1, 7)}, by_species(q))
q, _ = w.build(days=1e5, limit=13, audit_frac=0.0, per_species_cap=1000)
check("round robin twin: limit 13 gives 13 items, the extra one goes to the first species by name (3 vs 2)", len(q["items"]) == 13 and by_species(q)["genus01 species"] == 3 and sum(1 for v in by_species(q).values() if v == 2) == 5, by_species(q))
w = World()
for _ in range(3):
    w.add(sp(1), 0.6, (1, 2))                    # a common species
w.add(sp(2), 0.6, (1, 2))                        # a rare one
q, _ = w.build(days=1e5, limit=2, audit_frac=0.0, per_species_cap=1000)
check("round robin: the rarest species goes first (limit 2: one rare, one common), not the alphabetical first twice", sorted(by_species(q).items()) == [("genus01 species", 1), ("genus02 species", 1)] and q["items"][0]["species_key"] == "genus02 species", [i["species_key"] for i in q["items"]])
w = World()
t_far = w.add(sp(1), 0.60, (1, 2))               # distance 0.10 to near_lo
t_mid = w.add(sp(1), 0.52, (1, 2))               # 0.02
t_near = w.add(sp(1), 0.84, (1, 2))              # 0.01 to near_hi
t_dis = w.add(sp(1), 0.95, (1,))                 # disagree (single beam high), farthest from the thresholds
q, _ = w.build(**BIG)
check("ranking within a species: disagree first, then the distance to the nearest threshold (0.84 < 0.52 < 0.60)",
      [i["conf_max"] for i in q["items"]] == [0.95, 0.84, 0.52, 0.60], [i["conf_max"] for i in q["items"]])

# ---------------------------------------------------------------------------------------------- determinism and the audit slice
w = World()
for s in range(1, 4):
    for _ in range(40):
        w.add(sp(s), 0.9, (1, 2))                # 120 hi events: audit only
    for _ in range(5):
        w.add(sp(s), 0.6, (1, 2))                # 15 near
P = dict(days=1e5, limit=30, audit_frac=0.5, per_species_cap=1000, audit_per_species_cap=3)
qa, _ = w.build(seed=0, **P)
qb, _ = w.build(seed=0, **P)
qc, _ = w.build(seed=1, **P)
check("determinism: the same params and seed give byte-identical queue JSON and the same queue_id", Q.dumps(qa) == Q.dumps(qb) and qa["queue_id"] == qb["queue_id"] and len(qa["queue_id"]) == 12)
unc = lambda q_: [i["event_id"] for i in q_["items"] if i["selection"] == "uncertainty"]   # noqa: E731
aud = lambda q_: sorted(i["event_id"] for i in q_["items"] if i["selection"] == "audit")   # noqa: E731
check("determinism twin: another seed gives the same uncertainty picks and different audit picks and a different queue_id",
      unc(qa) == unc(qc) and len(unc(qa)) == 15 and aud(qa) != aud(qc) and len(aud(qa)) == len(aud(qc)) == 9 and qa["queue_id"] != qc["queue_id"], (len(unc(qa)), len(aud(qa)), len(aud(qc))))
check("the audit picks are disjoint from the uncertainty picks and every audit item has a p_include while no uncertainty item has one",
      not set(unc(qa)) & set(aud(qa)) and all(i["p_include"] is None for i in qa["items"] if i["selection"] == "uncertainty")
      and all(i["p_include"] is not None for i in qa["items"] if i["selection"] == "audit"))
# exact p_include: two species, 16 and 3 remaining hi events
w = World()
for _ in range(16):
    w.add(sp(1), 0.9, (1, 2))
for _ in range(3):
    w.add(sp(2), 0.9, (1, 2))
qp, _ = w.build(days=1e5, limit=4, audit_frac=1.0, per_species_cap=1000, audit_per_species_cap=4, seed=0)
pi = {i["species_key"]: i["p_include"] for i in qp["items"]}
cnt = by_species(qp)
check("audit p_include is exactly quota / remaining of the species: rarest first allocation 4 slots -> 2 and 2; 2/16 = 0.125 and 2/3",
      cnt == {"genus01 species": 2, "genus02 species": 2} and pi == {"genus01 species": 0.125, "genus02 species": 2 / 3}, (cnt, pi))
check("the frame is reported: audit.frame_n 19 (= params.audit_frame_n), species remaining 16 and 3, quotas 2 and 2",
      qp["audit"]["frame_n"] == 19 == qp["params"]["audit_frame_n"] and qp["audit"]["species"] == {"genus01 species": {"remaining": 16, "quota": 2}, "genus02 species": {"remaining": 3, "quota": 2}}, qp["audit"])
check("inverse-propensity check: the sum of 1/p_include over the audit items is exactly the frame size 19 (Horvitz-Thompson)", round(sum(1.0 / i["p_include"] for i in qp["items"]), 9) == 19.0)
qp1, _ = w.build(days=1e5, limit=1, audit_frac=1.0, per_species_cap=1000, audit_per_species_cap=4, seed=0)
check("twin: limit 1 gives one audit item from the rarest species (p 1/3), frame 19", [(i["species_key"], i["p_include"]) for i in qp1["items"]] == [("genus02 species", 1 / 3)] and qp1["audit"]["frame_n"] == 19)
# the audit slice is capped per species (the dominant species cannot take it all)
w = World()
for _ in range(50):
    w.add(sp(1), 0.9, (1, 2))
for _ in range(5):
    w.add(sp(2), 0.9, (1, 2))
qc2, _ = w.build(days=1e5, limit=20, audit_frac=1.0, per_species_cap=1000, audit_per_species_cap=5, seed=0)
check("audit cap per species: limit 20 with a 50-event and a 5-event species and cap 5 gives exactly 5 and 5 (10 items, not 20)", by_species(qc2) == {"genus01 species": 5, "genus02 species": 5}, by_species(qc2))
qc3, _ = w.build(days=1e5, limit=20, audit_frac=1.0, per_species_cap=1000, audit_per_species_cap=15, seed=0)
check("audit cap twin: with cap 15 the dominant species takes the remaining slots (15 and 5)", by_species(qc3) == {"genus01 species": 15, "genus02 species": 5}, by_species(qc3))
check("limit 0 gives no items; audit_frac 0 gives no audit items (twin: audit_frac 1 gives only audit items)",
      w.build(days=1e5, limit=0)[0]["items"] == [] and not by_species(w.build(**BIG)[0], "audit") and set(i["selection"] for i in qc2["items"]) == {"audit"})

# ---------------------------------------------------------------------------------------------- eligibility counters
w = World()
w.add(sp(1), 0.6, (1, 2))                                           # eligible
w.add(sp(2), 0.6, (1, 2), region=None)                              # region (pre-filter row)
w.add(sp(3), 0.6, (1, 2), region=False, k="bird_filtered")          # region (geo filtered)
w.add(("Engine", "Engine"), 0.9, (1, 2), region=None)               # non_species (mapped mechanical), unjudged region: first failing reason is non_species
w.add(("Human vocal", ""), 0.8, (1, 2))                             # speech_label
w.add(sp(4), 0.6, (1, 2), tag=False)                                # speech_unknown
tl = w.add(sp(5), 0.6, (1, 2), tag=False)
w.tags.append({"k": "tags", "t": tl + 2.0, "beam": 1, "speech_p": 0.9, "speech_active": True, "top": [], "emb_stored": False})   # speech_tag
w.add(sp(6), 0.6, (1, 2))                                           # already labelled
w.add(("Common Virtuoso Katydid", "Pterophylla camellifolia"), 0.9, (1, 2))   # insect_excluded
ev_ids = {e.key: e.event_id for e in L.iter_events(w.rows, TAXA)}
folded = {ev_ids["genus06 species"]: {"k": "verdict"}}
q, _ = w.build(taxa=TAXA, folded=folded, **BIG)
check("eligibility counters, exact: 1 eligible; region 2; non_species 1; speech_label 1; speech_unknown 1; speech_tag 1; already_labelled 1; insect_excluded 1",
      (q["eligible"]["events"], q["excluded"]) == (1, {"speech_label": 1, "speech_tag": 1, "speech_unknown": 1, "non_species": 1, "region": 2, "already_labelled": 1, "insect_excluded": 1, "not_insect": 0}), (q["eligible"], q["excluded"]))
check("the queue carries no label of an excluded speech event anywhere", b"human" not in Q.dumps(q).encode().lower() and [i["species_key"] for i in q["items"]] == ["genus01 species"])
q, _ = w.build(taxa=TAXA, folded=folded, include_unjudged_region=True, include_untagged=True, relabel=True, **BIG)
check("positive twins: --include-unjudged-region lifts the pre-filter row (not the geo-filtered one nor Engine), --include-untagged lifts speech_unknown only, --relabel lifts already_labelled",
      q["excluded"] == {"speech_label": 1, "speech_tag": 1, "speech_unknown": 0, "non_species": 1, "region": 1, "already_labelled": 0, "insect_excluded": 1, "not_insect": 0}
      and sorted(i["species_key"] for i in q["items"]) == ["genus01 species", "genus02 species", "genus04 species", "genus06 species"], (q["excluded"], [i["species_key"] for i in q["items"]]))
w = World()
w.add(sp(1), 0.6, (1, 2), t=T0)
w.add(sp(1), 0.6, (1, 2), t=NOW - 86400 * 2)
w.add(sp(1), 0.6, (1, 2), t=NOW - 3600)
w.add(sp(1), 0.6, (1, 2), t=NOW + 5000)
q, _ = w.build(days=1.0, limit=100, audit_frac=0.0, per_species_cap=100)
check("window: only the event inside [now - days, now] counts (1 of 4 in window, 1 queued; 4 in total)", (q["eligible"]["events_in_window"], q["eligible"]["events_total"], len(q["items"])) == (1, 4, 1), q["eligible"])
q, _ = w.build(days=3.0, limit=100, audit_frac=0.0, per_species_cap=100)
check("window twin: days 3 brings the 2-days-old event in (2 in window; the 11-days-old and the future one stay out)", (q["eligible"]["events_in_window"], len(q["items"])) == (2, 2), q["eligible"])

# ---------------------------------------------------------------------------------------------- disagreement flags
def flags_of(w_, key):
    q_, _ = w_.build(**BIG)
    return [i["flags"] for i in q_["items"] if i["species_key"] == key]


w = World()
t = w.add(sp(1), 0.6, (1, 2))
w.add(sp(2), 0.5, (3,), region=False, k="bird_filtered", t=t + 4.0, tag=False)       # a filtered report of another species 4 s after the event start
check("flag filtered_overlap: a geo-filtered report of another species within 5 s", flags_of(w, "genus01 species") == [["filtered_overlap"]])
w = World()
t = w.add(sp(1), 0.6, (1, 2))
w.add(sp(2), 0.5, (3,), region=False, k="bird_filtered", t=t + 6.5, tag=False)       # event ends at t + 0.1; 6.5 s after start is 6.4 s after the end
check("flag twin: the same filtered report 6.4 s after the event end is no flag", flags_of(w, "genus01 species") == [[]])
w = World()
t = w.add(sp(1), 0.6, (1, 2))
w.add(sp(1), 0.5, (3,), region=False, k="bird_filtered", t=t + 30.0, tag=False)
check("flag twin: a filtered report of the SAME species is no flag", flags_of(w, "genus01 species") == [[]])
w = World()
t = w.add(sp(1), 0.6, (1, 2))
w.add(sp(2), 0.6, (5, 6), t=t + 3.0)
check("flag two_species_beams: another in-region species on disjoint beams within 3 s (both events flagged)", flags_of(w, "genus01 species") == [["two_species_beams"]] and flags_of(w, "genus02 species") == [["two_species_beams"]])
w = World()
t = w.add(sp(1), 0.6, (1, 2))
w.add(sp(2), 0.6, (2, 3), t=t + 3.0)
check("flag twin: the other species on an overlapping beam (2) is no flag", flags_of(w, "genus01 species") == [[]])
w = World()
t = w.add(sp(1), 0.6, (1, 2))
w.add(sp(2), 0.6, (5, 6), t=t + 3.5)
check("flag twin: disjoint beams but 3.4 s after the event end (0.1 s long) is no flag", flags_of(w, "genus01 species") == [[]])
w = World()
w.add(sp(1), 0.6, (1,))
w.add(sp(2), 0.95, (1,))
check("flag single_beam_high needs conf >= near_hi: 0.6 on one beam is no flag, 0.95 on one beam is", (flags_of(w, "genus01 species"), flags_of(w, "genus02 species")) == ([[]], [["single_beam_high"]]))

# ---------------------------------------------------------------------------------------------- insects and the artifact probe
KATY = ("Common Virtuoso Katydid", "Pterophylla camellifolia")
w = World()
w.add(sp(1), 0.6, (1, 2))
w.add(KATY, 0.9, (1, 2), region=None)
q, _ = w.build(taxa=TAXA, **BIG)
check("insect mode exclude (default): a pre-filter katydid row fails the region check first (region 1), the bird stays", (q["excluded"]["region"], q["excluded"]["insect_excluded"], q["eligible"]["events"]) == (1, 0, 1), q["excluded"])
q, _ = w.build(taxa=TAXA, insect_mode="only", include_unjudged_region=True, **BIG)
check("artifact probe: --insect-mode only with --include-unjudged-region queues the katydid (stratum artifact_probe, selection probe) and files the bird as not_insect",
      [(i["species_key"], i["stratum"], i["selection"]) for i in q["items"]] == [("pterophylla camellifolia", "artifact_probe", "probe")] and q["excluded"]["not_insect"] == 1, (q["items"], q["excluded"]))
q, _ = w.build(taxa=TAXA, insect_mode="only", **BIG)
check("artifact probe twin: --insect-mode only WITHOUT --include-unjudged-region still drops the pre-filter katydid (region 1): the two flags are separate", (q["eligible"]["events"], q["excluded"]["region"]) == (0, 1), q["excluded"])
q, _ = w.build(taxa=L.Taxa(), insect_mode="exclude", include_unjudged_region=True, **BIG)
check("without a taxa table nothing is classed insect: the katydid is queued as an ordinary event (and `taxa_loaded` says false)", q["taxa_loaded"] is False and (q["eligible"]["events"], q["excluded"]["insect_excluded"]) == (2, 0), (q["eligible"], q["excluded"]))
q, _ = w.build(taxa=TAXA, insect_mode="exclude", include_unjudged_region=True, **BIG)
check("insect mode exclude with the region lifted: the katydid is insect_excluded (1) and only the bird is queued", (q["excluded"]["insect_excluded"], [i["species_key"] for i in q["items"]]) == (1, ["genus01 species"]))
# probe buckets: 20 cold events and 1 warm one at the same UTC hour of day on different days
w = World()
am_rows = []
for d in range(21):
    t = (1792800000 // 86400 + d) * 86400 + 3 * 3600 + 600.0           # 03:10 UTC on 21 days
    w.add(KATY, 0.9, (1, 2), t=t, region=True)
    am_rows.append({"k": "ambient_temp", "t_hour_utc": int(t) // 3600 * 3600, "temp_c": 12.0 if d == 7 else 2.0})
qp, evs = None, None
w.rows.sort(key=lambda r: r["t"])
w.tags.sort(key=lambda r: r["t"])
NOW_SAVE = NOW
NOW = max(r["t"] for r in w.rows) + 100
qp, evs = w.build(taxa=TAXA, ambient=L.Ambient(am_rows), insect_mode="only", days=1e5, limit=2, audit_frac=0.0, per_species_cap=100, seed=0)
warm_id = [e.event_id for e in evs if abs(e.t_start - ((1792800000 // 86400 + 7) * 86400 + 3 * 3600 + 600.0)) < 1][0]
check("probe buckets: with ambient temperatures the 2 picks are one cold and the single warm event (the smallest bucket goes first)",
      len(qp["items"]) == 2 and warm_id in [i["event_id"] for i in qp["items"]], [i["event_id"] for i in qp["items"]])
qp2, _ = w.build(taxa=TAXA, ambient=L.Ambient(am_rows), insect_mode="only", days=1e5, limit=2, audit_frac=0.0, per_species_cap=1, seed=0)
check("probe twin: per_species_cap 1 caps the single katydid species at 1 item", len(qp2["items"]) == 1)
NOW = NOW_SAVE

# ---------------------------------------------------------------------------------------------- quantiles and depth
w = World()
for c in (0.1, 0.2, 0.3, 0.4, 0.5):
    w.add(sp(1), c, (1, 2))
q, _ = w.build(**BIG)
check("conf_quantiles: n 5, p25 0.2, p50 0.3, p75 0.4 (nearest rank)", q["conf_quantiles"] == {"genus01 species": {"n": 5, "p25": 0.2, "p50": 0.3, "p75": 0.4}}, q["conf_quantiles"])
w = World()
for _ in range(20):
    w.add(sp(1), 0.6, (1, 2))                                      # 20 near
for _ in range(12):
    w.add(sp(2), 0.6, (1, 2))
for _ in range(8):
    w.add(sp(2), 0.3, (1, 2))                                      # 12 near + 8 low
for _ in range(19):
    w.add(sp(3), 0.6, (1, 2))                                      # 19 near
q, _ = w.build(**BIG)
check("depth: exact per-species counts and the summary (union 2 species, near-only 1, >= 20 events 2)",
      q["depth"]["genus01 species"] == {"near": 20, "disagree": 0, "low": 0, "hi": 0, "artifact_probe": 0}
      and q["depth"]["genus02 species"] == {"near": 12, "disagree": 0, "low": 8, "hi": 0, "artifact_probe": 0}
      and q["depth_summary"] == {"min_events": 20, "species_union": 2, "species_near_only": 1, "species_ge_min_events": 2}, (q["depth"], q["depth_summary"]))
w2 = World()
for _ in range(19):
    w2.add(sp(1), 0.6, (1, 2))
q19, _ = w2.build(**BIG)
check("depth twin: 19 near events do not count (union 0)", q19["depth_summary"]["species_union"] == 0)

# ---------------------------------------------------------------------------------------------- parameter checks and the document
check("params: near_lo above near_hi is refused", raises(Q.QueueError, "need 0 <= near_lo < near_hi <= 1", lambda: w.build(near_lo=0.9, near_hi=0.5)))
check("params: audit_frac 1.5 is refused", raises(Q.QueueError, "audit_frac must be in [0, 1]", lambda: w.build(audit_frac=1.5)))
check("params: an unknown insect_mode is refused", raises(Q.QueueError, "insect_mode must be one of exclude, only", lambda: w.build(insect_mode="include")))
check("params: a negative limit is refused, limit 0 (twin) is accepted", raises(Q.QueueError, "must not be negative", lambda: w.build(limit=-1)) and w.build(limit=0)[0]["items"] == [])
check("the document: k, v, t_built, window, params echo the inputs exactly",
      (q["k"], q["v"], q["t_built"], q["window"], q["params"]["seed"], q["params"]["near_lo"], q["params"]["near_hi"]) == ("review_queue", 1, NOW, {"days": 1e5, "t0": NOW - 1e5 * 86400.0, "t1": NOW}, 0, 0.5, 0.85))
check("the queue JSON is LF-terminated compact ASCII and parses back to the same document", Q.dumps(q).endswith("}\n") and "\r" not in Q.dumps(q) and json.loads(Q.dumps(q)) == q)
print("\n%d checks, %d failure(s)" % (n_checks, len(fails)))
for f_ in fails:
    print("  FAILED:", f_)
sys.exit(1 if fails else 0)
