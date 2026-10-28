#!/usr/bin/env python3
"""Uncertainty-sampled review queue (contract verdict-labels 1.0.0, `review_queue`).  Pure and deterministic: the output is a function
of the events, the tags, the folded labels, the (optional) ambient temperatures and the params; nothing is read from or written to disk
here.  stdlib only.  The CLI is tools/bma_labels.py `queue`.

Selection (per run, `limit` slots, `audit_frac`):
  1. UNCERTAINTY part, round(limit * (1 - audit_frac)) slots.  Candidates: strata disagree, near, low.  Rank: `disagree` first, then the
     distance of conf_max to the nearest of {near_lo, near_hi}.  Round-robin across species, rarest first, at most `per_species_cap` per
     species.  `p_include` is null: an uncertainty pick has no known inclusion probability.
  2. AUDIT part, the remaining slots.  STRATIFIED BY SPECIES: the frame is every eligible event NOT picked in step 1 (all strata, `hi`
     included).  Species s with R_s remaining events gets the quota q_s (a deterministic round-robin allocation, rarest species first,
     q_s <= min(R_s, audit_per_species_cap)); q_s events are drawn uniformly without replacement from the R_s.  The inclusion probability
     of every event of s is exactly q_s / R_s, recorded as `p_include`.  The frame sizes are reported (`audit.frame_n`, `audit.species`),
     so an estimator can use inverse-propensity weights 1 / p_include (Horvitz-Thompson) over the audit items only.
  `artifact_probe` mode (insect events only, `insect_mode == "only"`): there is no confidence ordering; the picks are spread round-robin
  over (UTC hour of day, ambient temperature class) buckets so the owner sees insect-group reports at warm and at cold hours alike."""
import bisect, hashlib, json, random

from labels import label_for, speech_reason

NEAR_LO, NEAR_HI = 0.5, 0.85            # ARBITRARY defaults (nobody has measured a gate); the output reports per-species quantiles
DEPTH_MIN = 20
FILTERED_OVERLAP_S = 5.0                # a geo-filtered report of another species within this of the event (Golden-fronted vs Red-bellied)
TWO_SPECIES_S = 3.0
COLD_C = 5.0                            # brief finding 4: 92% of insect-group reports were below 5 C
EXCLUDED_REASONS = ("speech_label", "speech_tag", "speech_unknown", "non_species", "region", "already_labelled", "insect_excluded", "not_insect")
INSECT_MODES = ("exclude", "only")

DEFAULTS = {"days": 14.0, "seed": 0, "per_species_cap": 25, "limit": 200, "near_lo": NEAR_LO, "near_hi": NEAR_HI, "audit_frac": 0.2,
            "audit_per_species_cap": 5, "speech_margin_s": 5.0, "speech_p_min": 0.15, "include_untagged": False,
            "include_unjudged_region": False, "insect_mode": "exclude", "relabel": False}


class QueueError(ValueError):
    pass


def check_params(p):
    if p["insect_mode"] not in INSECT_MODES:
        raise QueueError("insect_mode must be one of %s" % ", ".join(INSECT_MODES))
    if not 0 <= p["near_lo"] < p["near_hi"] <= 1:
        raise QueueError("need 0 <= near_lo < near_hi <= 1")
    if not 0 <= p["audit_frac"] <= 1:
        raise QueueError("audit_frac must be in [0, 1]")
    if p["limit"] < 0 or p["per_species_cap"] < 0 or p["audit_per_species_cap"] < 0 or p["days"] <= 0:
        raise QueueError("limit, caps and days must not be negative (days > 0)")


def stratum_of(conf, n_beams, flags, near_lo, near_hi):
    if flags:
        return "disagree"
    if conf < near_lo:
        return "low"
    if conf < near_hi:
        return "near"
    return "hi"


def overlap_flags(events, near_hi):
    """-> {id(event): [flags]} for every event: (a) filtered_overlap, (b) two_species_beams, (c) single_beam_high."""
    filt = sorted((e for e in events if not e.has_true and e.has_false), key=lambda e: e.t_start)
    f_starts = [e.t_start for e in filt]
    f_dur = max((e.t_end - e.t_start for e in filt), default=0.0)
    inr = sorted((e for e in events if e.has_true), key=lambda e: e.t_start)
    i_starts = [e.t_start for e in inr]
    i_dur = max((e.t_end - e.t_start for e in inr), default=0.0)
    out = {}
    for e in events:
        fl = []
        i = bisect.bisect_left(f_starts, e.t_start - FILTERED_OVERLAP_S - f_dur)
        while i < len(filt) and f_starts[i] <= e.t_end + FILTERED_OVERLAP_S:
            o = filt[i]
            if o.key != e.key and o.t_end >= e.t_start - FILTERED_OVERLAP_S:
                fl.append("filtered_overlap")
                break
            i += 1
        i = bisect.bisect_left(i_starts, e.t_start - TWO_SPECIES_S - i_dur)
        while i < len(inr) and i_starts[i] <= e.t_end + TWO_SPECIES_S:
            o = inr[i]
            if o.key != e.key and o.t_end >= e.t_start - TWO_SPECIES_S and not (o.beams & e.beams):
                fl.append("two_species_beams")
                break
            i += 1
        if e.max_conf >= near_hi and len(e.beams) == 1:
            fl.append("single_beam_high")
        out[id(e)] = fl
    return out


def quantiles(values):
    v = sorted(values)
    n = len(v)
    return {"n": n, **{name: round(v[int(round(q * (n - 1)))], 4) for name, q in (("p25", 0.25), ("p50", 0.5), ("p75", 0.75))}}


def round_robin(lists, limit, cap):
    """lists: {species: [items in rank order]}.  Take one per species per pass, rarest species first (fewest candidates, then name),
    at most `cap` per species, until `limit` is reached.  -> [(species, item)] in pick order."""
    order = sorted(lists, key=lambda s: (len(lists[s]), s))
    pos = {s: 0 for s in order}
    out = []
    while len(out) < limit:
        progressed = False
        for s in order:
            if len(out) >= limit:
                break
            if pos[s] < len(lists[s]) and pos[s] < cap:
                out.append((s, lists[s][pos[s]]))
                pos[s] += 1
                progressed = True
        if not progressed:
            break
    return out


def probe_order(cands, ambient, seed):
    """Candidates ordered round-robin over (hour of day UTC, temperature class) buckets; deterministic."""
    buckets = {}
    for e in cands:
        temp = ambient.at(e.t_start) if ambient is not None else None
        cls = "unknown" if temp is None else ("cold" if temp < COLD_C else "warm")
        buckets.setdefault((int(e.t_start) // 3600 % 24, cls), []).append(e)
    for b, lst in buckets.items():
        lst.sort(key=lambda e: e.event_id)
        random.Random("%s:probe:%s:%s" % (seed, b[0], b[1])).shuffle(lst)
    order = sorted(buckets, key=lambda b: (len(buckets[b]), b))
    out, i = [], 0
    while any(i < len(buckets[b]) for b in order):
        out += [buckets[b][i] for b in order if i < len(buckets[b])]
        i += 1
    return out


def build_queue(events, folded, tags, params, now, ambient=None, taxa_loaded=True):
    """events: [labels.Event]; folded: labels.fold() result; tags: labels.Tags; params: dict (DEFAULTS keys); now: Unix seconds.
    -> the review_queue document (a dict)."""
    p = dict(DEFAULTS, **params)
    check_params(p)
    t0, t1 = now - p["days"] * 86400.0, now
    window = [e for e in events if e.t_end >= t0 and e.t_start <= t1]
    flags = overlap_flags(window, p["near_hi"])
    excluded = {k: 0 for k in EXCLUDED_REASONS}
    eligible = []
    for e in window:
        reason = speech_reason(e, tags, p["speech_margin_s"], p["speech_p_min"], p["include_untagged"])
        if reason is None and (e.non_species or e.taxa_group == "mechanical"):
            reason = "non_species"
        if reason is None and not (e.in_region is True or (p["include_unjudged_region"] and e.in_region is None)):
            reason = "region"
        if reason is None and not p["relabel"] and label_for(folded, e) is not None:
            reason = "already_labelled"
        if reason is None:
            insect = e.taxa_group == "insect"
            if p["insect_mode"] == "exclude" and insect:
                reason = "insect_excluded"
            elif p["insect_mode"] == "only" and not insect:
                reason = "not_insect"
        if reason:
            excluded[reason] += 1
        else:
            eligible.append(e)
    probe = p["insect_mode"] == "only"
    strat = {id(e): ("artifact_probe" if probe else stratum_of(e.max_conf, len(e.beams), flags[id(e)], p["near_lo"], p["near_hi"]))
             for e in eligible}
    by_stratum = {s: 0 for s in ("disagree", "near", "low", "hi", "artifact_probe")}
    depth, per_species = {}, {}
    for e in eligible:
        by_stratum[strat[id(e)]] += 1
        depth.setdefault(e.key, {s: 0 for s in ("near", "disagree", "low", "hi", "artifact_probe")})[strat[id(e)]] += 1
        per_species.setdefault(e.key, []).append(e)
    # ---- 1. uncertainty / probe part
    slots_u = int(round(p["limit"] * (1 - p["audit_frac"])))
    picks = []                                                       # [(event, selection)]
    if probe:
        cnt = {}
        for e in probe_order(eligible, ambient, p["seed"]):
            if len(picks) >= slots_u:
                break
            if cnt.get(e.key, 0) < p["per_species_cap"]:
                cnt[e.key] = cnt.get(e.key, 0) + 1
                picks.append((e, "probe"))
    else:
        lists = {}
        for e in eligible:
            if strat[id(e)] != "hi":
                dist = min(abs(e.max_conf - p["near_lo"]), abs(e.max_conf - p["near_hi"]))
                lists.setdefault(e.key, []).append(e)
        for s in lists:
            lists[s].sort(key=lambda e: (strat[id(e)] != "disagree", min(abs(e.max_conf - p["near_lo"]), abs(e.max_conf - p["near_hi"])), e.event_id))
        picks = [(e, "uncertainty") for _s, e in round_robin(lists, slots_u, p["per_species_cap"])]
    chosen = {id(e) for e, _ in picks}
    # ---- 2. audit part: stratified by species over every event not picked above
    slots_a = max(0, p["limit"] - slots_u)
    remaining = {}
    for e in eligible:
        if id(e) not in chosen:
            remaining.setdefault(e.key, []).append(e)
    quota = {s: 0 for s in remaining}
    order = sorted(remaining, key=lambda s: (len(remaining[s]), s))
    left = slots_a
    while left > 0:
        progressed = False
        for s in order:
            if left > 0 and quota[s] < min(len(remaining[s]), p["audit_per_species_cap"]):
                quota[s] += 1
                left -= 1
                progressed = True
        if not progressed:
            break
    audit_items = []
    audit_species = {}
    for s in sorted(remaining):
        r = len(remaining[s])
        audit_species[s] = {"remaining": r, "quota": quota[s]}
        if quota[s]:
            ids = sorted(e.event_id for e in remaining[s])
            by_id = {e.event_id: e for e in remaining[s]}
            drawn = random.Random("%s:audit:%s" % (p["seed"], s)).sample(ids, quota[s])
            audit_items += [(by_id[i], "audit", quota[s] / r) for i in sorted(drawn)]
    frame_n = sum(len(v) for v in remaining.values())
    items = []
    for e, sel in picks:
        items.append(_item(e, strat[id(e)], sel, None, flags[id(e)]))
    for e, sel, pi in sorted(audit_items, key=lambda x: x[0].event_id):
        items.append(_item(e, strat[id(e)], sel, pi, flags[id(e)]))
    out_params = {k: p[k] for k in sorted(p)}
    qid = hashlib.sha256((json.dumps(sorted(i["event_id"] for i in items)) + json.dumps(out_params, sort_keys=True)).encode()).hexdigest()[:12]
    union = {s for s, d in depth.items() if d["near"] + d["disagree"] + d["low"] >= DEPTH_MIN}
    near_only = {s for s, d in depth.items() if d["near"] >= DEPTH_MIN}
    return {"k": "review_queue", "v": 1, "queue_id": qid, "t_built": now,
            "params": dict(out_params, audit_frame_n=frame_n),
            "window": {"days": p["days"], "t0": t0, "t1": t1},
            "eligible": {"events": len(eligible), "species": len(per_species), "by_stratum": by_stratum,
                         "events_in_window": len(window), "events_total": len(events)},
            "excluded": excluded, "taxa_loaded": bool(taxa_loaded),
            "depth": {s: depth[s] for s in sorted(depth)},
            "depth_summary": {"min_events": DEPTH_MIN, "species_union": len(union), "species_near_only": len(near_only),
                              "species_ge_min_events": sum(1 for s in per_species if len(per_species[s]) >= DEPTH_MIN)},
            "conf_quantiles": {s: quantiles([e.max_conf for e in per_species[s]]) for s in sorted(per_species)},
            "audit": {"frame_n": frame_n, "slots": slots_a, "species": audit_species},
            "items": items}


def _item(e, stratum, selection, p_include, flags):
    return {"event_id": e.event_id, "species_key": e.key, "common": e.common, "t_start_ms": int(e.t_start * 1000),
            "t_end_ms": int(e.t_end * 1000), "conf_max": round(e.max_conf, 4), "n_beams": len(e.beams), "best_beam": e.best_beam,
            "stratum": stratum, "selection": selection, "p_include": p_include, "flags": list(flags)}


def dumps(q):
    """Canonical queue JSON: sorted keys at the top level are NOT used (field order is the builder's); LF; ASCII."""
    return json.dumps(q, separators=(",", ":")) + "\n"
