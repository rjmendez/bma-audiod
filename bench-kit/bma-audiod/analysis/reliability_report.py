#!/usr/bin/env python3
"""Label-free reliability report for the BirdNET stream (read-only; stdlib only).

    python -I reliability_report.py --data-dir DIR [--since ISO] [--json] [--write-confusions PATH] [--taxa FILE] [--top N]

Reads <data-dir>/analysis/birds.jsonl (rows k = bird / bird_filtered: t, beam, confidence, common, scientific, optional in_region,
group), <data-dir>/history/ambient_temp.jsonl (+ .1; reader rule of contracts/specs/ambient-temp.md) and the taxonomy table
(taxa.py). A Perch agreement file (analysis/perch_agreement.jsonl or perch.jsonl) is only noted when present; this tool does not
need it. It writes nothing except the file named by --write-confusions (refused when it names an input file or its directory
does not exist). A missing or unreadable taxonomy table is an error (exit 1, nothing printed or written): without it non-species
labels are not excluded and no confusion can be found. No audio is read: the input is metadata only.

Why label-free: the owner has no per-clip labels yet. BirdNET confidence is not a probability (research report 2026-10-26,
section 2 item 1), so the tool looks for internal evidence of (un)reliability in data we already have:
  * multi-beam agreement: the 8 beams look in different directions, a real nearby source tends to be heard by more than one,
    an electronic artifact or a single-beam fluke less so (observed on the live tree 2026-10-26, not checked by tests: 92.5 % of the
             1,680 events at >= 0.85 are multi-beam);
  * temperature plausibility for groups whose calling depends on temperature (insects, frogs);
  * confusion: an out-of-region label reported with high confidence next to an in-region species of the same group is probably
    that species (Golden-fronted -> Red-bellied Woodpecker).

Definitions
  event      rows of the SAME species (scientific name) whose consecutive timestamps are at most EVENT_GAP_S (3 s) apart; its
             confidence is the max over its rows, its beams the distinct beam numbers of its rows, its time the first row's.
  hours      distinct UTC clock hours of the day (0..23) with at least one event.
  ambient    nearest PAST hourly record, at most AMBIENT_MAX_AGE_S (3 h) older than the event's hour, else unknown (never
             a later hour, never interpolated); unknown events are left out of the temperature shares.
  excluded   labels the taxonomy marks non_species (engine, dog, noise ...) and any label with "human" in it are not species
             and are not reported anywhere (human-voice labels are never reported).

Constants (every number is a judgement call, written here with its reason; this is advice, nothing applies it)
  EVENT_GAP_S = 3.0            birds plugin windows are 3 s: rows of one call spread over neighbouring windows.
  HIGH_CONF = 0.85             the pipeline's existing gate; the report measures how much it deserves.
  MIN_EVENTS = 10              below this a share is noise (one event moves it 10 points): verdict 'too few events'.
  MIN_HIGH_EVENTS = 5          'trustworthy at 0.85' needs at least this many events at >= 0.85.
  TRUSTED_MULTIBEAM_SHARE = 0.8  and at least this share of those events heard on >= 2 beams (point estimate) ...
  TRUSTED_MULTIBEAM_LOWER = 0.6  ... whose Wilson lower bound (WILSON_Z = 1.645, one-sided 95 %) is at least this: a handful of
                               events cannot clear 0.8 by luck (10 events with 8 multi-beam: lower bound 0.54, not trusted) ...
  MIN_SPECIES_MULTIBEAM_SHARE = 0.5  ... and at least this share of ALL the species' events (not only the >= 0.85 ones) are
                               multi-beam: a species that is mostly single-beam is not trusted on its few loud events.
  TEMP_MIN_KNOWN = 10          temperature verdicts need this many events with a known ambient temperature.
  INSECT_COLD_C = 10.0         katydids and crickets are essentially silent below about 10 C (specs/ambient-temp.md).
  INSECT_SUSPECT_SHARE = 0.5   half or more of an insect species' events below 10 C -> 'suspect: temperature implausible'.
  AMPHIBIAN_COLD_C = 5.0       frogs vary by species (some call near freezing), so the flag is gentler: colder limit ...
  AMPHIBIAN_SUSPECT_SHARE = 0.8  ... and a larger share. The 10 C share is still reported for both groups.
  CONFUSION_MIN_CONF = 0.7     only filtered reports at >= 0.7 count as 'high confidence' (shipped confusions.json: Golden-fronted,
                               21 reports at this level, 18 with a Red-bellied partner within 5 s; pinned by the tests).
  CONFUSION_WINDOW_S = 5.0     a partner is an in-region row of the same group AND the same genus (first word of the scientific
                               name) within +-5 s of the filtered report. Genus, because a group is only bird / insect / amphibian
                               and any in-region bird near a filtered one is not a plausible identity.
  CONFUSION_NULL_SHIFT_S = 600.0  base-rate control: the same partner is looked for around each report's time shifted by +-600 s;
  CONFUSION_MAX_NULL_RATIO = 0.5  a pair qualifies only when that shifted share is at most half of the real share (a species heard
                               all the time would otherwise reach 60 % by chance).
  CONFUSION_MIN_REPORTS = 10   and CONFUSION_MIN_SHARE = 0.6: evidence qualifies for confusions.json and for the verdict
                               'confusable with X' only with at least 10 reports and 60 % of them sharing the same partner.
  MIN_CONF_FLOOR = 0.5         a suggested minimum confidence is never below this.
Partners must be of the same group as the out-of-region label (and not non_species) so that a dog bark or an engine next to a
filtered owl is not named as what the owl 'really' is.

Verdict, first match wins (table with the numbers: analysis/README.md)
  1 confusable with X          qualified confusion evidence for this species
  2 too few events             events < MIN_EVENTS
  3 out of region              every report of the species was rejected by the geofilter (bird_filtered / in_region false)
  4 suspect: temperature implausible   insect / amphibian rule above
  5 no high-confidence events  no event at >= 0.85 (a second beam is irrelevant then)
  6 needs second beam          fewer than MIN_HIGH_EVENTS events at >= 0.85, or multi-beam share of them < TRUSTED_MULTIBEAM_SHARE
                               or its lower bound < TRUSTED_MULTIBEAM_LOWER, or multi-beam share of all events < MIN_SPECIES_MULTIBEAM_SHARE
  7 trustworthy at 0.85        otherwise
Suggested rules (--json): minimum confidence and minimum beams per species, never below MIN_CONF_FLOOR, never a certainty claim
(see README for the key list)."""
import argparse
import bisect
import collections
import datetime
import json
import math
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import taxa as taxa_mod  # noqa: E402

EVENT_GAP_S = 3.0
HIGH_CONF = 0.85
MIN_EVENTS = 10
MIN_HIGH_EVENTS = 5
TRUSTED_MULTIBEAM_SHARE = 0.8
TRUSTED_MULTIBEAM_LOWER = 0.6
WILSON_Z = 1.645
MIN_SPECIES_MULTIBEAM_SHARE = 0.5
TEMP_MIN_KNOWN = 10
INSECT_COLD_C = 10.0
INSECT_SUSPECT_SHARE = 0.5
AMPHIBIAN_COLD_C = 5.0
AMPHIBIAN_SUSPECT_SHARE = 0.8
CONFUSION_MIN_CONF = 0.7
CONFUSION_WINDOW_S = 5.0
CONFUSION_MIN_REPORTS = 10
CONFUSION_MIN_SHARE = 0.6
CONFUSION_NULL_SHIFT_S = 600.0
CONFUSION_MAX_NULL_RATIO = 0.5
MIN_CONF_FLOOR = 0.5
AMBIENT_MAX_AGE_S = 3 * 3600
CONFUSION_SOURCE = "co-occurrence within 5 s"

V_TRUST = "trustworthy at 0.85"
V_BEAM = "needs second beam"
V_TEMP = "suspect: temperature implausible"
V_FEW = "too few events"
V_OOR = "out of region"
V_NOHIGH = "no high-confidence events"
V_CONF = "confusable with "


def read_jsonl(path):
    """Parsed dict lines of a JSONL file; unreadable file -> []; bad / non-dict lines skipped."""
    out = []
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return out
    for raw in data.split(b"\n"):
        try:
            r = json.loads(raw.decode("utf-8"))
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def load_birds(path, since=None):
    """bird / bird_filtered rows with numeric t, beam and confidence (t >= since); the rest is skipped."""
    rows = []
    for r in read_jsonl(path):
        if r.get("k") not in ("bird", "bird_filtered"):
            continue
        if not (_num(r.get("t")) and _num(r.get("confidence")) and _num(r.get("beam"))):
            continue
        if not (r.get("scientific") or r.get("common")):
            continue
        if since is not None and r["t"] < since:
            continue
        rows.append(r)
    return rows


class Ambient:
    """Reader rule of contracts/specs/ambient-temp.md: nearest past hour record at most 3 h older than the event's hour."""

    def __init__(self, recs):
        by = {}
        for r in recs:
            h, t = r.get("t_hour_utc"), r.get("temp_c")
            if r.get("k") != "ambient_temp" or isinstance(h, bool) or not isinstance(h, int) or not _num(t) or not -90 <= t <= 60:
                continue
            by[h] = float(t)          # the last line wins
        self.hours = sorted(by)
        self.temps = [by[h] for h in self.hours]

    @classmethod
    def load(cls, history_dir):
        recs = []
        for name in ("ambient_temp.jsonl.1", "ambient_temp.jsonl"):
            recs += read_jsonl(os.path.join(history_dir, name))
        return cls(recs)

    def at(self, t):
        h = int(math.floor(t / 3600.0)) * 3600
        i = bisect.bisect_right(self.hours, h) - 1
        if i < 0 or h - self.hours[i] > AMBIENT_MAX_AGE_S:
            return None
        return self.temps[i]


def species_key(r):
    return (r.get("scientific") or r.get("common") or "").strip()


def group_of(r, taxa):
    """(group or None, non_species): the row's own group when it has one, else the taxonomy table's."""
    g, ns = taxa.lookup(r.get("scientific"), r.get("common"))
    return (r.get("group") or g), bool(ns or r.get("non_species"))


def is_excluded(r, taxa):
    label = ((r.get("common") or "") + " " + (r.get("scientific") or "")).lower()
    return "human" in label or group_of(r, taxa)[1]


def build_events(rows):
    """rows of one species -> events sorted by time: {t, conf, beams (sorted list), n_rows}."""
    ev = []
    cur = None
    last_t = None
    for r in sorted(rows, key=lambda x: x["t"]):
        if cur is not None and r["t"] - last_t <= EVENT_GAP_S:
            cur["conf"] = max(cur["conf"], r["confidence"])
            cur["beams"].add(int(r["beam"]))
            cur["n_rows"] += 1
        else:
            cur = {"t": r["t"], "conf": r["confidence"], "beams": {int(r["beam"])}, "n_rows": 1}
            ev.append(cur)
        last_t = r["t"]
    for e in ev:
        e["beams"] = sorted(e["beams"])
    return ev


def _share(n, d):
    return round(n / d, 4) if d else None


def wilson_lower(k, n, z=None):
    """Lower end of the Wilson score interval for k successes of n (None for n = 0)."""
    if not n:
        return None
    z = WILSON_Z if z is None else z
    p = k / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / (1 + z * z / n)


def is_out_of_region(r):
    return r.get("in_region") is False or r["k"] == "bird_filtered"


def genus_of(r):
    sci = (r.get("scientific") or "").strip().lower()
    return sci.split()[0] if sci else None


def species_stats(events, group, ambient):
    n = len(events)
    high = [e for e in events if e["conf"] >= HIGH_CONF]
    multi = [e for e in events if len(e["beams"]) >= 2]
    multi_high = [e for e in high if len(e["beams"]) >= 2]
    confs = [e["conf"] for e in events]
    st = {
        "events": n,
        "events_high": len(high),
        "share_high": _share(len(high), n),
        "median_conf": round(statistics.median(confs), 4) if confs else None,
        "max_conf": round(max(confs), 4) if confs else None,
        "mean_beams": round(sum(len(e["beams"]) for e in events) / n, 4) if n else None,
        "share_multibeam": _share(len(multi), n),
        "share_multibeam_high": _share(len(multi_high), len(high)),
        "share_multibeam_high_lower": round(wilson_lower(len(multi_high), len(high)), 4) if high else None,
        "hours_active": len({int(e["t"] // 3600) % 24 for e in events}),
        "temp_known": None, "share_below_5c": None, "share_below_10c": None,
    }
    if group in ("insect", "amphibian") and ambient is not None:
        temps = [x for x in (ambient.at(e["t"]) for e in events) if x is not None]
        st["temp_known"] = len(temps)
        if temps:
            st["share_below_5c"] = _share(sum(1 for x in temps if x < 5.0), len(temps))
            st["share_below_10c"] = _share(sum(1 for x in temps if x < 10.0), len(temps))
    return st


def _plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def verdict_of(st, group, confusion, all_out_of_region=False):
    """-> (verdict, detail). confusion = qualified confusions entry for this species or None."""
    if confusion:
        return V_CONF + confusion["likely_as"], "partner in %d of %d reports" % (confusion["evidence"]["co_occurring"], confusion["evidence"]["reports"])
    if st["events"] < MIN_EVENTS:
        return V_FEW, "%s, need %d" % (_plural(st["events"], "event"), MIN_EVENTS)
    if all_out_of_region:
        return V_OOR, "all %d events are geofilter-rejected reports" % st["events"]
    if st["temp_known"] is not None and st["temp_known"] >= TEMP_MIN_KNOWN:
        if group == "insect" and st["share_below_10c"] >= INSECT_SUSPECT_SHARE:
            return V_TEMP, "%.0f%% of events below %g C" % (100 * st["share_below_10c"], INSECT_COLD_C)
        if group == "amphibian" and st["share_below_5c"] >= AMPHIBIAN_SUSPECT_SHARE:
            return V_TEMP, "%.0f%% of events below %g C" % (100 * st["share_below_5c"], AMPHIBIAN_COLD_C)
    mbh = st["share_multibeam_high"]
    if st["events_high"] == 0:
        return V_NOHIGH, "none of %d events reaches %g (median %.2f)" % (st["events"], HIGH_CONF, st["median_conf"])
    if st["events_high"] < MIN_HIGH_EVENTS:
        return V_BEAM, "only %s at >= %g" % (_plural(st["events_high"], "event"), HIGH_CONF)
    if mbh < TRUSTED_MULTIBEAM_SHARE:
        return V_BEAM, "%.0f%% of events at >= %g are multi-beam" % (100 * mbh, HIGH_CONF)
    lo = st["share_multibeam_high_lower"]
    if lo < TRUSTED_MULTIBEAM_LOWER:
        return V_BEAM, "%.0f%% of %d events at >= %g are multi-beam, lower bound %.0f%% < %.0f%%" % (
            100 * mbh, st["events_high"], HIGH_CONF, 100 * lo, 100 * TRUSTED_MULTIBEAM_LOWER)
    if st["share_multibeam"] < MIN_SPECIES_MULTIBEAM_SHARE:
        return V_BEAM, "only %.0f%% of all %d events are multi-beam, need %.0f%%" % (
            100 * st["share_multibeam"], st["events"], 100 * MIN_SPECIES_MULTIBEAM_SHARE)
    return V_TRUST, "%d events at >= %g, %.0f%% multi-beam" % (st["events_high"], HIGH_CONF, 100 * mbh)


def suggest(verdict):
    """Advice per verdict: (min_confidence, min_beams). Never below MIN_CONF_FLOOR."""
    if verdict == V_TRUST:
        c, b = HIGH_CONF, 1
    elif verdict in (V_BEAM, V_FEW, V_NOHIGH):
        c, b = HIGH_CONF, 2
    else:                      # temperature implausible or confusable: ask for more than the default gate
        c, b = 0.95, 2
    return max(MIN_CONF_FLOOR, c), b


def find_confusions(rows, taxa):
    """Out-of-region reports at >= CONFUSION_MIN_CONF with their in-region same-group, same-genus partners within
    +-CONFUSION_WINDOW_S. -> {scientific: entry}; entry has label, partner, partner_label, reports, co_occurring, share,
    null_share (the best partner's share around the report times shifted by +-CONFUSION_NULL_SHIFT_S), qualifies, reason."""
    in_rows = []
    out_by = collections.defaultdict(list)
    for r in rows:
        if is_out_of_region(r):
            if r["confidence"] >= CONFUSION_MIN_CONF and not is_excluded(r, taxa):
                out_by[species_key(r)].append(r)
        else:
            in_rows.append(r)
    in_rows.sort(key=lambda r: r["t"])
    ts = [r["t"] for r in in_rows]
    ts_by_sp = collections.defaultdict(list)
    for r in in_rows:
        ts_by_sp[species_key(r)].append(r["t"])
    res = {}
    for sp, reps in out_by.items():
        grp = group_of(reps[0], taxa)[0]
        genus = genus_of(reps[0])
        per_partner = collections.Counter()
        label = {}
        for r in reps:
            lo = bisect.bisect_left(ts, r["t"] - CONFUSION_WINDOW_S)
            hi = bisect.bisect_right(ts, r["t"] + CONFUSION_WINDOW_S)
            seen = set()
            for p in in_rows[lo:hi]:
                pk = species_key(p)
                if pk == sp or grp is None or genus is None or genus_of(p) != genus or is_excluded(p, taxa) or group_of(p, taxa)[0] != grp:
                    continue
                seen.add(pk)
                label[pk] = p.get("common") or pk
            per_partner.update(seen)
        n = len(reps)
        entry = {"label": reps[0].get("common") or sp, "reports": n, "partner": None, "partner_label": None,
                 "co_occurring": 0, "share": 0.0, "null_share": 0.0}
        if per_partner:
            best = sorted(per_partner.items(), key=lambda kv: (-kv[1], kv[0]))[0]
            entry.update(partner=best[0], partner_label=label[best[0]], co_occurring=best[1], share=round(best[1] / n, 4))
            pts, nulls = ts_by_sp[best[0]], 0
            for r in reps:
                for shift in (CONFUSION_NULL_SHIFT_S, -CONFUSION_NULL_SHIFT_S):
                    c = r["t"] + shift
                    nulls += bisect.bisect_right(pts, c + CONFUSION_WINDOW_S) > bisect.bisect_left(pts, c - CONFUSION_WINDOW_S)
            entry["null_share"] = round(nulls / (2 * n), 4)
        if n < CONFUSION_MIN_REPORTS:
            entry["qualifies"], entry["reason"] = False, "%d reports, need %d" % (n, CONFUSION_MIN_REPORTS)
        elif entry["share"] < CONFUSION_MIN_SHARE:
            entry["qualifies"], entry["reason"] = False, "best partner in %d of %d reports (%.0f%%), need %.0f%%" % (
                entry["co_occurring"], n, 100 * entry["share"], 100 * CONFUSION_MIN_SHARE)
        elif entry["null_share"] > CONFUSION_MAX_NULL_RATIO * entry["share"]:
            entry["qualifies"], entry["reason"] = False, "partner also near %.0f%% of the reports shifted by +-%g s (base rate), real %.0f%%" % (
                100 * entry["null_share"], CONFUSION_NULL_SHIFT_S, 100 * entry["share"])
        else:
            entry["qualifies"], entry["reason"] = True, "ok"
        res[sp] = entry
    return res


def confusions_document(conf):
    """The confusions.json content: qualified entries only, keyed by the out-of-region common name."""
    doc = {}
    for sp, e in conf.items():
        if e["qualifies"]:
            doc[e["label"]] = {
                "likely_as": e["partner_label"],
                "scientific": sp,
                "likely_as_scientific": e["partner"],
                "evidence": {"reports": e["reports"], "co_occurring": e["co_occurring"], "share": e["share"],
                             "min_confidence": CONFUSION_MIN_CONF, "window_s": CONFUSION_WINDOW_S},
                "source": CONFUSION_SOURCE,
            }
    return doc


def dump_json(obj):
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def default_taxa_path():
    """analysis/taxa.json next to this file (as installed), else the repository's ui/taxa.json."""
    p = taxa_mod.default_path()
    return p if os.path.exists(p) else os.path.normpath(os.path.join(HERE, "..", "ui", "taxa.json"))


def input_files(data_dir, taxa_path):
    a_dir = os.path.join(data_dir, "analysis")
    h_dir = os.path.join(data_dir, "history")
    return [os.path.join(a_dir, "birds.jsonl"), os.path.join(h_dir, "ambient_temp.jsonl"), os.path.join(h_dir, "ambient_temp.jsonl.1"),
            os.path.join(a_dir, "perch_agreement.jsonl"), os.path.join(a_dir, "perch.jsonl"), taxa_path or default_taxa_path()]


def analyse(data_dir, since=None, taxa_path=None):
    a_dir = os.path.join(data_dir, "analysis")
    rows = load_birds(os.path.join(a_dir, "birds.jsonl"), since)
    taxa = taxa_mod.Taxa(taxa_path or default_taxa_path())
    ambient = Ambient.load(os.path.join(data_dir, "history"))
    by = collections.defaultdict(list)
    skipped = 0
    for r in rows:
        if is_excluded(r, taxa):
            skipped += 1
            continue
        by[species_key(r)].append(r)
    conf = find_confusions(rows, taxa)
    species = {}
    for sp, rs in by.items():
        grp = next((g for g in (group_of(r, taxa)[0] for r in rs) if g), None)
        st = species_stats(build_events(rs), grp, ambient)
        all_out = all(is_out_of_region(r) for r in rs)
        verdict, detail = verdict_of(st, grp, confusion_entry(sp, conf), all_out)
        min_c, min_b = suggest(verdict)
        species[sp] = {"common": next((r.get("common") for r in rs if r.get("common")), sp), "group": grp,
                       "out_of_region": any(is_out_of_region(r) for r in rs), "all_out_of_region": all_out,
                       "stats": st, "verdict": verdict, "detail": detail,
                       "min_confidence": min_c, "min_beams": min_b}
    perch = [n for n in ("perch_agreement.jsonl", "perch.jsonl") if os.path.exists(os.path.join(a_dir, n))]
    return {"version": 1, "advice_only": True, "rows": len(rows), "skipped_non_species_rows": skipped,
            "taxa_ok": taxa.ok, "ambient_records": len(ambient.hours), "perch_files": perch,
            "species": species, "confusion_candidates": conf}


def confusion_entry(sp, conf):
    e = conf.get(sp)
    if e and e["qualifies"]:
        return {"likely_as": e["partner_label"], "evidence": {"co_occurring": e["co_occurring"], "reports": e["reports"]}}
    return None


def json_view(rep):
    """The --json document: per species the stats, verdict and the suggested rule; advice only."""
    return {
        "version": rep["version"], "advice_only": True,
        "species": {sp: {"common": s["common"], "group": s["group"], "out_of_region": s["out_of_region"],
                         "all_out_of_region": s["all_out_of_region"], "verdict": s["verdict"], "detail": s["detail"],
                         "min_confidence": s["min_confidence"], "min_beams": s["min_beams"], "stats": s["stats"]}
                    for sp, s in rep["species"].items()},
    }


def fmt(v, pct=False):
    if v is None:
        return "-"
    return "%d%%" % round(100 * v) if pct else ("%.2f" % v if isinstance(v, float) else str(v))


def text_view(rep, top):
    out = []
    sp = sorted(rep["species"].items(), key=lambda kv: (-kv[1]["stats"]["events"], kv[0]))
    out.append("rows %d, species %d (non-species rows skipped %d), ambient records %d, taxa %s, perch files: %s" % (
        rep["rows"], len(sp), rep["skipped_non_species_rows"], rep["ambient_records"], "ok" if rep["taxa_ok"] else "MISSING",
        ", ".join(rep["perch_files"]) or "none"))
    out.append("")
    out.append("%-34s %-9s %-4s %6s %6s %5s %5s %5s %5s %5s %5s %4s %5s %5s  %s" % (
        "species", "group", "oor", "events", "hi", "med", "max", "bms", "multi", "m|hi", "hrs", "tkn", "<5C", "<10C", "verdict"))
    for key, s in sp[:top]:
        st = s["stats"]
        out.append("%-34s %-9s %-4s %6d %6s %5s %5s %5s %5s %5s %5d %4s %5s %5s  %s" % (
            s["common"][:34], (s["group"] or "?")[:9], "all" if s["all_out_of_region"] else "some" if s["out_of_region"] else "-", st["events"], fmt(st["share_high"], True), fmt(st["median_conf"]),
            fmt(st["max_conf"]), fmt(st["mean_beams"]), fmt(st["share_multibeam"], True), fmt(st["share_multibeam_high"], True),
            st["hours_active"], fmt(st["temp_known"]), fmt(st["share_below_5c"], True), fmt(st["share_below_10c"], True),
            s["verdict"] + (" (%s)" % s["detail"] if s["verdict"] != V_TRUST else "")))
    if len(sp) > top:
        out.append("... %d more species (use --top or --json)" % (len(sp) - top))
    out.append("")
    out.append("verdict counts: " + ", ".join("%s %d" % (k, v) for k, v in sorted(collections.Counter(
        V_CONF.strip() + " X" if v["verdict"].startswith(V_CONF) else v["verdict"] for v in rep["species"].values()).items())))
    out.append("")
    out.append("out-of-region reports at >= %g, best in-region same-genus partner within +-%g s (oor = rows rejected by the geofilter)" % (
        CONFUSION_MIN_CONF, CONFUSION_WINDOW_S))
    cs = sorted(rep["confusion_candidates"].values(), key=lambda e: (-e["reports"], e["label"]))
    for e in cs[:15]:
        out.append("  %-30s -> %-26s %3d reports, %3d with partner (%s)  %s" % (
            e["label"][:30], (e["partner_label"] or "-")[:26], e["reports"], e["co_occurring"], fmt(e["share"], True),
            "QUALIFIES" if e["qualifies"] else "no: " + e["reason"]))
    if not cs:
        out.append("  none")
    return "\n".join(out) + "\n"


def parse_since(s):
    d = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d.timestamp()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--since", help="ISO time; naive = UTC. Rows before it are ignored")
    ap.add_argument("--json", action="store_true", help="print the suggested-rules JSON (advice only)")
    ap.add_argument("--write-confusions", metavar="PATH", help="write the qualified confusion evidence (deterministic JSON)")
    ap.add_argument("--taxa", help="taxa.json (default: analysis/taxa.json next to this file, else ../ui/taxa.json); an unreadable table is an error")
    ap.add_argument("--top", type=int, default=30, help="text table rows (default 30)")
    a = ap.parse_args(argv)
    try:
        since = parse_since(a.since) if a.since else None
    except ValueError:
        ap.error("--since: not an ISO time: %r" % a.since)
    taxa_path = a.taxa or default_taxa_path()
    if a.write_confusions:
        target = os.path.realpath(a.write_confusions)
        if os.path.isdir(target):
            ap.error("--write-confusions: %s is a directory" % a.write_confusions)
        if not os.path.isdir(os.path.dirname(target)):
            ap.error("--write-confusions: directory %s does not exist" % os.path.dirname(a.write_confusions or "."))
        if any(os.path.normcase(os.path.realpath(f)) == os.path.normcase(target) for f in input_files(a.data_dir, taxa_path)):
            ap.error("--write-confusions: %s is an input file of this report, refusing to overwrite it" % a.write_confusions)
    rep = analyse(a.data_dir, since, taxa_path)
    if not rep["taxa_ok"]:
        sys.stderr.write("reliability_report: taxonomy table %s is missing or unreadable; refusing to report without it "
                         "(non-species labels would not be excluded and no confusion could be found)\n" % taxa_path)
        return 1
    if a.write_confusions:
        try:
            with open(a.write_confusions, "wb") as f:
                f.write(dump_json(confusions_document(rep["confusion_candidates"])).encode("utf-8"))
        except OSError as e:
            ap.error("--write-confusions: cannot write %s: %s" % (a.write_confusions, e.strerror or e))
    out = dump_json(json_view(rep)) if a.json else text_view(rep, a.top)
    sys.stdout.flush()
    sys.stdout.buffer.write(out.encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
