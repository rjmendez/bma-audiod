#!/usr/bin/env python3
"""Offline cross-beam agreement analysis of birds.jsonl (roadmap item beam-agreement-tiers, contract beam-agreement 1.1.0).

    python beam_agreement.py --birds <birds.jsonl> [--ambient <ambient_temp.jsonl>] [--labels <labels.jsonl>] [--taxa <taxa.json>]
                             [--gap-s 5] [--null-trials 200] [--null-seed <int>] [--generated-s <unix s>] [--out <file>] [--private-detail]

Question: does "the same species was reported on two beams in the SAME window" carry information beyond BirdNET's confidence,
or is it only a diffuseness / loudness signal? The decision is computed here from rules fixed in specs/beam-agreement.md
(section Decision), so it cannot be argued after the fact. Read-only: it reads only the files it is given (no environment
defaults), refuses an --out inside the directory of any input, stores no audio and reads none (metadata JSONL only).
Stdlib only. The shape of the output is contracts/schemas/beam_agreement_report.schema.json."""

import argparse, bisect, json, math, os, random, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import events as E  # noqa: E402

BIN_EDGES = (0.0, 0.35, 0.5, 0.65, 0.85, 1.0)  # fixed in advance, never tuned; the last bin includes 1.0
PRIMARY_BINS = ((0.5, 0.65), (0.65, 0.85))  # the band the label test runs in
LABEL_GAP_S = 3.0  # the dashboard's event gap (ui/bma_ui.py EVENT_GAP_S): labels attach to those events
MIN_PER_ARM = 90
MIN_EXPECTED = 5.0  # CMH validity: smallest expected cell of every used stratum
MIN_BIN_N = 30  # negative control: events per arm in a bin for it to vote
COLD_C = 5.0
AMBIENT_MAX_AGE_S = 3 * 3600  # ambient-temp contract reader rule: nearest past hour, at most 3 h old
RECUR_S = 120.0
NULL_TRIALS = 200  # shuffle null: default repetitions
NULL_SEED = 20261027
QUANTILES = (0.2, 0.4, 0.6, 0.8)  # report-only measured confidence cut points (never gate)
ARBITRARY_CUTS = (0.5, 0.85)  # the review-queue near_lo / near_hi defaults
NEAR_T_S = 0.5  # two windows closer than this should have been one (shared t_wall)
GROUPS = ("bird", "insect", "amphibian", "mammal", "other-bio", "mechanical")
DUR_CLASSES = ("1", "2-3", ">=4")


def is_num(x):
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return False
    try:
        return math.isfinite(x)
    except OverflowError:  # an int too large for a float is garbage, never a crash
        return False


# ----------------------------------------------------------------------------------------------- taxa (same rules as ui/bma_ui.py Taxa)
class Taxa:
    def __init__(self, path=None):
        self.explicit, self.genus, self.ok = {}, {}, False
        if path is None:
            return
        try:
            with open(path, "rb") as f:
                d = json.loads(f.read())
            if not isinstance(d, dict) or d.get("version") != 1 or not isinstance(d.get("genera"), dict) or not isinstance(d.get("explicit_labels"), dict):
                raise ValueError("structure")
            genus, explicit = {}, {}
            for grp, names in d["genera"].items():
                if grp not in GROUPS or not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
                    raise ValueError("genera")
                for n in names:
                    genus[n.lower()] = grp
            for lab, e in d["explicit_labels"].items():
                if not isinstance(e, dict) or e.get("group") not in GROUPS or not isinstance(e.get("non_species"), bool):
                    raise ValueError("explicit")
                explicit[lab.lower()] = (e["group"], e["non_species"])
            self.explicit, self.genus, self.ok = explicit, genus, True
        except (OSError, ValueError, TypeError):
            self.explicit, self.genus, self.ok = {}, {}, False

    def classify(self, scientific, common, flag):
        """-> (group, how, non_species), as ui Taxa.classify: explicit label, genus table, else ('other-bio', 'unmapped', flag)."""
        for lab in (common, scientific):
            hit = self.explicit.get((lab or "").strip().lower())
            if hit:
                return hit[0], "explicit", hit[1]
        parts = (scientific or "").split()
        if len(parts) >= 2 and parts[0].lower() in self.genus:
            return self.genus[parts[0].lower()], "genus", False
        return "other-bio", "unmapped", bool(flag)


def default_taxa_path():
    p = os.path.join(HERE, "..", "ui", "taxa.json")
    return p if os.path.exists(p) else None


# ----------------------------------------------------------------------------------------------- reading
def valid_bird(o):
    """The dashboard's valid_bird restricted to `bird` rows (bird_filtered rows are out-of-region and never analysed)."""
    return (
        isinstance(o, dict)
        and o.get("k") == "bird"
        and is_num(o.get("t"))
        and o["t"] > 0
        and isinstance(o.get("beam"), int)
        and not isinstance(o.get("beam"), bool)
        and 1 <= o["beam"] <= 8
        and is_num(o.get("confidence"))
        and 0 <= o["confidence"] <= 1
        and isinstance(o.get("common"), str)
        and o["common"] != ""
        and (o.get("scientific") is None or isinstance(o["scientific"], str))
    )


def read_jsonl(path):
    """-> (records that are JSON objects, garbage line count). Torn, non-JSON and non-object lines are counted and skipped."""
    out, garbage = [], 0
    with open(path, "rb") as f:
        for raw in f.read().split(b"\n"):
            if not raw.strip():
                continue
            try:
                o = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                garbage += 1
                continue
            if isinstance(o, dict):
                out.append(o)
            else:
                garbage += 1
    return out, garbage


# ----------------------------------------------------------------------------------------------- events
class Ev:
    __slots__ = (
        "key",
        "common",
        "group",
        "how",
        "non_species",
        "t_start",
        "t_end",
        "windows",
        "max_conf",
        "max_level",
        "has_true",
        "has_false",
        "reports",
        "event_id",
    )

    def __init__(self, key, o, taxa):
        self.key, self.common = key, o["common"]
        self.group, self.how, self.non_species = taxa.classify(o.get("scientific") or "", o["common"], o.get("non_species"))
        self.t_start = self.t_end = o["t"]
        self.windows = {o["t"]: {o["beam"]}}  # exact t -> beams that reported the species in that window
        self.max_conf = o["confidence"]
        lv = o.get("level_dbfs")
        self.max_level = lv if is_num(lv) else None
        self.has_true = o.get("in_region") is True
        self.has_false = o.get("in_region") is False
        self.reports = 1
        self.event_id = None

    def merge(self, o):
        self.t_start, self.t_end = min(self.t_start, o["t"]), max(self.t_end, o["t"])
        self.windows.setdefault(o["t"], set()).add(o["beam"])
        self.reports += 1
        self.max_conf = max(self.max_conf, o["confidence"])
        lv = o.get("level_dbfs")
        if is_num(lv) and (self.max_level is None or lv > self.max_level):
            self.max_level = lv
        self.has_true = self.has_true or o.get("in_region") is True
        self.has_false = self.has_false or o.get("in_region") is False
        if self.how == "unmapped":
            self.non_species = self.non_species or bool(o.get("non_species"))

    @property
    def beams(self):
        out = set()
        for b in self.windows.values():
            out |= b
        return out

    @property
    def concurrent_windows(self):
        return sum(1 for b in self.windows.values() if len(b) >= 2)

    @property
    def in_region(self):
        return True if self.has_true else (False if self.has_false else None)

    @property
    def tier(self):
        return tier_of(self.concurrent_windows)

    @property
    def n_windows(self):
        return len(self.windows)


def tier_of(concurrent_windows):
    """The ONE definition: confirmed >= 2 concurrent windows, probable == 1, single_beam == 0."""
    return "confirmed" if concurrent_windows >= 2 else ("probable" if concurrent_windows == 1 else "single_beam")


def key_of(o):
    return (o.get("scientific") or o["common"]).strip().lower()


# Human voices are hidden from every view (ui/bma_ui.py HUMAN_LABELS, owner decision): such reports never become events here either.
HUMAN_LABELS = frozenset(("human vocal", "human non-vocal", "human whistle"))


def is_human(o):
    return any(isinstance(v, str) and v.strip().lower() in HUMAN_LABELS for v in (o.get("common"), o.get("scientific")))


def cluster_counted(rows, gap_s, taxa):
    """Chain reports of one species key into events exactly as ui/bma_ui.py DetStore.add does (file order, last event per key,
    t_start - gap <= t <= t_end + gap), through analysis/events.py. -> (events in creation order, events.Result with the accounting)."""
    res = E.build(rows, gap_s, kinds=("bird",), drop_human=True)
    events = []
    for c in res.clusters:
        ev = Ev(c.key, c.rows[0], taxa)
        for o in c.rows[1:]:
            ev.merge(o)
        ev.event_id = c.id
        events.append(ev)
    return events, res


def cluster(rows, gap_s, taxa):
    return cluster_counted(rows, gap_s, taxa)[0]


def beams_adjacent(a, b):
    return (a - b) % 8 in (1, 7)


def window_adjacent(beams):
    bl = sorted(beams)
    return any(beams_adjacent(x, y) for i, x in enumerate(bl) for y in bl[i + 1 :])


def concurrent_stats(ev):
    """-> (concurrent windows, of which with a ring-adjacent beam pair)."""
    conc = [b for b in ev.windows.values() if len(b) >= 2]
    return len(conc), sum(1 for b in conc if window_adjacent(b))


def unequal_t_pairs(events):
    """Pairs of consecutive windows of one event whose t differ by less than NEAR_T_S but are not equal: a consumer that
    re-serialised t would have split one window in two and silently turned an agreement into 'single'."""
    n = 0
    for ev in events:
        ts = sorted(ev.windows)
        n += sum(1 for a, b in zip(ts, ts[1:]) if 0 < b - a < NEAR_T_S)
    return n


# ----------------------------------------------------------------------------------------------- binning helpers
def bin_index(c):
    for i in range(len(BIN_EDGES) - 1):
        if BIN_EDGES[i] <= c < BIN_EDGES[i + 1]:
            return i
    return len(BIN_EDGES) - 2 if c >= BIN_EDGES[-1] else 0


def dur_class(n_windows):
    return DUR_CLASSES[0] if n_windows <= 1 else (DUR_CLASSES[1] if n_windows <= 3 else DUR_CLASSES[2])


def tercile_cuts(values):
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    return v[len(v) // 3], v[(2 * len(v)) // 3]


def level_class(level, cuts):
    if level is None or cuts is None:
        return 3
    return 0 if level <= cuts[0] else (1 if level <= cuts[1] else 2)


def r6(x):
    return None if x is None else round(float(x), 6)


def frac(a, b):
    return None if b == 0 else a / b


def in_region_species(events):
    return [e for e in events if e.in_region is True and not e.non_species]


def bin_table(events):
    """Per confidence bin over in-region species events (see the spec)."""
    evs = in_region_species(events)
    out = []
    for i in range(len(BIN_EDGES) - 1):
        lo, hi = BIN_EDGES[i], BIN_EDGES[i + 1]
        sub = [e for e in evs if bin_index(e.max_conf) == i]
        tiers = {"confirmed": 0, "probable": 0, "single_beam": 0}
        cw = cadj = conf_nonadj = 0
        for e in sub:
            tiers[e.tier] += 1
            c, a = concurrent_stats(e)
            cw += c
            cadj += a
            if e.tier == "confirmed" and a < c:
                conf_nonadj += 1
        by_dur = {}
        for dc in DUR_CLASSES:
            d = [e for e in sub if dur_class(e.n_windows) == dc]
            by_dur[dc] = {"n": len(d), "multi_frac": r6(frac(sum(1 for e in d if e.concurrent_windows >= 1), len(d)))}
        n = len(sub)
        out.append(
            {
                "lo": lo,
                "hi": hi,
                "n": n,
                "tiers": tiers,
                "multi_frac": r6(frac(tiers["confirmed"] + tiers["probable"], n)),
                "mean_windows": r6(frac(sum(e.n_windows for e in sub), n)),
                "concurrent_windows": cw,
                "adjacent_concurrent_windows": cadj,
                "adjacent_share": r6(frac(cadj, cw)),
                "confirmed_nonadjacent": conf_nonadj,
                "by_duration": by_dur,
            }
        )
    return out


# ----------------------------------------------------------------------------------------------- ambient + negative control
def load_ambient(path):
    """-> (sorted [(t_hour_utc, temp_c)], garbage). A missing or unreadable file is an empty list, never an error."""
    if path is None:
        return [], 0
    try:
        recs, garbage = read_jsonl(path)
    except OSError:
        return [], 0
    out = []
    for r in recs:
        if r.get("k") == "ambient_temp" and isinstance(r.get("t_hour_utc"), int) and not isinstance(r.get("t_hour_utc"), bool) and is_num(r.get("temp_c")):
            out.append((r["t_hour_utc"], float(r["temp_c"])))
        else:
            garbage += 1
    return sorted(out), garbage


def temp_at(ambient, t):
    """Reader rule of specs/ambient-temp.md: the newest record whose hour started at or before t, at most 3 h old, else None."""
    hours = [h for h, _ in ambient]
    i = bisect.bisect_right(hours, t) - 1
    if i < 0 or t - hours[i] > AMBIENT_MAX_AGE_S:
        return None
    return ambient[i][1]


def _single_frac(evs):
    return frac(sum(1 for e in evs if e.concurrent_windows == 0), len(evs))


def matched_single_fracs(cold, bird):
    """Single-beam fraction of each arm after direct standardisation to the pooled duration-class x level-tercile mix, over the
    strata both arms populate. -> (cold, bird) or (None, None) when no stratum is shared."""
    cuts = tercile_cuts([e.max_level for e in cold + bird])

    def cell(e):
        return (dur_class(e.n_windows), level_class(e.max_level, cuts))

    cs, bs = {}, {}
    for e in cold:
        cs.setdefault(cell(e), []).append(e)
    for e in bird:
        bs.setdefault(cell(e), []).append(e)
    shared = [k for k in cs if k in bs]
    w = {k: len(cs[k]) + len(bs[k]) for k in shared}
    tot = sum(w.values())
    if tot == 0:
        return None, None
    return (sum(w[k] * _single_frac(cs[k]) for k in shared) / tot, sum(w[k] * _single_frac(bs[k]) for k in shared) / tot)


def negative_control(events, ambient):
    """Cold-night insect events (outdoor < COLD_C, the suspected electronic artifact) against in-region bird events, per bin.
    artifact_scores_higher is decided on the duration x level MATCHED version: True when, in the majority of bins >= 0.5 with at
    least MIN_BIN_N events in both arms, the cold-insect single-beam fraction is below the bird one (the artifact looks MORE
    agreed than birds). None when no bin votes. The unmatched fractions are reported as descriptive only."""
    cold = []
    if ambient:
        for e in events:
            if e.group == "insect" and not e.non_species and e.in_region is not False:
                t = temp_at(ambient, e.t_start)
                if t is not None and t < COLD_C:
                    cold.append(e)
    birds = [e for e in in_region_species(events) if e.group == "bird"]
    rows, votes, higher = [], 0, 0
    for i in range(len(BIN_EDGES) - 1):
        lo, hi = BIN_EDGES[i], BIN_EDGES[i + 1]
        c = [e for e in cold if bin_index(e.max_conf) == i]
        b = [e for e in birds if bin_index(e.max_conf) == i]
        eligible = lo >= 0.5 and len(c) >= MIN_BIN_N and len(b) >= MIN_BIN_N
        mc, mb = matched_single_fracs(c, b) if c and b else (None, None)
        if eligible and mc is not None:
            votes += 1
            higher += 1 if mc < mb else 0
        cc = sum(concurrent_stats(e)[0] for e in c)
        rows.append(
            {
                "lo": lo,
                "hi": hi,
                "n_cold_insect": len(c),
                "n_bird": len(b),
                "cold_insect_single_frac": r6(_single_frac(c)),
                "bird_single_frac": r6(_single_frac(b)),
                "matched_cold_insect_single_frac": r6(mc),
                "matched_bird_single_frac": r6(mb),
                "eligible": bool(eligible and mc is not None),
                "cold_insect_adjacent_share": r6(frac(sum(concurrent_stats(e)[1] for e in c), cc)),
            }
        )
    return {"n_cold_insect": len(cold), "n_bird": len(birds), "bins": rows, "artifact_scores_higher": None if votes == 0 else bool(higher * 2 > votes)}


# ----------------------------------------------------------------------------------------------- statistics
def chi2_sf(x, df):
    """P(chi-square_df >= x): the regularised upper incomplete gamma Q(df/2, x/2) (series below, continued fraction above)."""
    if x <= 0:
        return 1.0
    a, z = df / 2.0, x / 2.0
    if z < a + 1:
        term = s = 1.0 / a
        n = a
        for _ in range(1000):
            n += 1
            term *= z / n
            s += term
            if abs(term) < abs(s) * 1e-16:
                break
        return max(0.0, 1.0 - s * math.exp(-z + a * math.log(z) - math.lgamma(a)))
    tiny = 1e-300
    b = z + 1 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = tiny if abs(d) < tiny else d
        c = b + an / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-16:
            break
    return min(1.0, math.exp(-z + a * math.log(z) - math.lgamma(a)) * h)


def strata_tables(items):
    """items: [(stratum key, exposed bool, correct bool)] -> {key: [a, b, c, d]} with a exposed&correct, b exposed&wrong,
    c unexposed&correct, d unexposed&wrong."""
    t = {}
    for k, exp, ok in items:
        row = t.setdefault(k, [0, 0, 0, 0])
        row[(0 if exp else 2) + (0 if ok else 1)] += 1
    return t


def _usable(tab):
    a, b, c, d = tab
    return a + b > 0 and c + d > 0 and a + c > 0 and b + d > 0 and a + b + c + d > 1


def _min_expected(tab):
    a, b, c, d = tab
    n = a + b + c + d
    return min((a + b) * (a + c), (a + b) * (b + d), (c + d) * (a + c), (c + d) * (b + d)) / n


def cmh(tables):
    """Cochran-Mantel-Haenszel over 2x2 strata (continuity corrected, as R's mantelhaen.test), Mantel-Haenszel common OR with the
    Robins-Breslow-Greenland 95% CI, and the Breslow-Day test with Tarone's correction. Strata with an empty margin are dropped.
    -> dict: status ok|stratum_too_sparse|no_variation, strata_used, chi2, p, or, or_ci95, breslow_day_p (None with < 2 strata)."""
    used = [t for t in tables if _usable(t)]
    base = {"strata_used": len(used), "chi2": None, "p": None, "or": None, "or_ci95": None, "breslow_day_p": None}
    if not used:
        return dict(base, status="no_variation")
    if any(_min_expected(t) < MIN_EXPECTED for t in used):
        return dict(base, status="stratum_too_sparse")
    R = S = ea = va = sa = 0.0
    for a, b, c, d in used:
        n = a + b + c + d
        R += a * d / n
        S += b * c / n
        r1, r2, c1, c2 = a + b, c + d, a + c, b + d
        ea += r1 * c1 / n
        va += r1 * r2 * c1 * c2 / (n * n * (n - 1))
        sa += a
    chi2 = max(0.0, abs(sa - ea) - 0.5) ** 2 / va
    out = dict(base, status="ok", chi2=chi2, p=chi2_sf(chi2, 1))
    if R <= 0 or S <= 0:
        return dict(out, status="no_variation")
    orr = R / S
    v = 0.0
    for a, b, c, d in used:
        n = a + b + c + d
        P, Q, Ri, Si = (a + d) / n, (b + c) / n, a * d / n, b * c / n
        v += P * Ri / (2 * R * R) + (P * Si + Q * Ri) / (2 * R * S) + Q * Si / (2 * S * S)
    se = math.sqrt(v)
    out["or"], out["or_ci95"] = orr, [math.exp(math.log(orr) - 1.96 * se), math.exp(math.log(orr) + 1.96 * se)]
    if len(used) >= 2:
        out["breslow_day_p"] = breslow_day_p(used, orr)
    return out


def _expected_a(tab, psi):
    a0, b0, c0, d0 = tab
    r1, r2, c1 = a0 + b0, c0 + d0, a0 + c0
    lo, hi = max(0.0, c1 - r2), float(min(r1, c1))
    for _ in range(200):  # a (r2-c1+a) = psi (r1-a)(c1-a) is increasing in a: bisect
        mid = (lo + hi) / 2
        if mid * (r2 - c1 + mid) < psi * (r1 - mid) * (c1 - mid):
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def breslow_day_p(used, psi):
    stat = dev = vtot = 0.0
    for tab in used:
        a0, b0, c0, d0 = tab
        r1, r2, c1 = a0 + b0, c0 + d0, a0 + c0
        e = _expected_a(tab, psi)
        parts = (e, r1 - e, c1 - e, r2 - c1 + e)
        var = 1.0 / sum(1.0 / p for p in parts) if all(p > 0 for p in parts) else 0.0
        if var <= 0:
            continue
        stat += (a0 - e) ** 2 / var
        dev += a0 - e
        vtot += var
    if vtot > 0:
        stat -= dev * dev / vtot
    return chi2_sf(max(0.0, stat), len(used) - 1)


def woolf(a, b, c, d):
    """Odds ratio with a Haldane 0.5 correction when a cell is empty, and the Woolf 95% CI."""
    if min(a, b, c, d) == 0:
        a, b, c, d = a + 0.5, b + 0.5, c + 0.5, d + 0.5
    orr = a * d / (b * c)
    se = math.sqrt(1 / a + 1 / b + 1 / c + 1 / d)
    return orr, [math.exp(math.log(orr) - 1.96 * se), math.exp(math.log(orr) + 1.96 * se)]


# ----------------------------------------------------------------------------------------------- outcomes
def in_primary_band(e):
    return PRIMARY_BINS[0][0] <= e.max_conf < PRIMARY_BINS[-1][1]


def o2_recurrence(events, last_t):
    """Proxy outcome: another event of the same species starts within RECUR_S after this one ends. Events whose look-ahead
    runs past the end of the data are dropped (censored). Never a pass criterion."""
    starts = {}
    for e in events:
        starts.setdefault(e.key, []).append(e.t_start)
    for v in starts.values():
        v.sort()
    cells = [0, 0, 0, 0]
    for e in in_region_species(events):
        if not in_primary_band(e) or e.t_end + RECUR_S > last_t:
            continue
        ts = starts[e.key]
        i = bisect.bisect_right(ts, e.t_end)  # first start strictly after this event ends; the nearest decides
        rec = i < len(ts) and ts[i] - e.t_end <= RECUR_S
        cells[(0 if e.concurrent_windows >= 1 else 2) + (0 if rec else 1)] += 1
    n = sum(cells)
    if min(cells[0] + cells[1], cells[2] + cells[3]) == 0:
        return {"n": n, "or": None, "or_ci95": None}
    orr, ci = woolf(*cells)
    return {"n": n, "or": r6(orr), "or_ci95": [r6(ci[0]), r6(ci[1])]}


def read_labels(path):
    """-> ({(species key, t_start rounded to 6 dp): 'correct'|'wrong'}, stats). `unsure` verdicts are dropped (counted),
    a duplicate key keeps the last verdict (counted), a line that is not a valid beam_label is garbage (counted)."""
    recs, garbage = read_jsonl(path)
    n_lines = len(recs) + garbage
    lab, unsure, dup = {}, 0, 0
    for r in recs:
        if (
            r.get("k") != "beam_label"
            or r.get("v") != 1
            or not isinstance(r.get("species_key"), str)
            or not r["species_key"]
            or not is_num(r.get("t_start"))
            or r.get("verdict") not in ("correct", "wrong", "unsure")
        ):
            garbage += 1
            continue
        if r["verdict"] == "unsure":
            unsure += 1
            continue
        k = (r["species_key"].strip().lower(), round(float(r["t_start"]), 6))
        dup += 1 if k in lab else 0
        lab[k] = r["verdict"]
    return lab, {"n_lines": n_lines, "garbage_lines": garbage, "unsure_dropped": unsure, "duplicates": dup}


def o1_labels(events3, labels):
    """Owner-verdict outcome on gap-3.0 events: CMH of (concurrent vs single) against 'correct' within conf bin x duration class
    (6 strata); the level-tercile stratification is a reported sensitivity only."""
    items, items_lv, matched = [], [], 0
    band = [e for e in in_region_species(events3) if in_primary_band(e) and (e.key, round(e.t_start, 6)) in labels]
    cuts = tercile_cuts([e.max_level for e in band])
    for e in band:
        ok = labels[(e.key, round(e.t_start, 6))] == "correct"
        exp = e.concurrent_windows >= 1
        k = (bin_index(e.max_conf), dur_class(e.n_windows))
        items.append((k, exp, ok))
        items_lv.append((k + (level_class(e.max_level, cuts),), exp, ok))
        matched += 1
    n_exp = sum(1 for _, x, _ in items if x)
    base = {"n": len(items), "n_concurrent": n_exp, "n_single": len(items) - n_exp}
    if not items:
        return dict(
            base,
            status="no_labelled_events",
            strata_used=0,
            cmh_chi2=None,
            cmh_p=None,
            **{"or": None},
            or_ci95=None,
            breslow_day_p=None,
            level_sensitivity=None,
        )
    main = cmh(list(strata_tables(items).values()))
    sens = cmh(list(strata_tables(items_lv).values()))
    return dict(
        base,
        status=main["status"],
        strata_used=main["strata_used"],
        cmh_chi2=r6(main["chi2"]),
        cmh_p=r6(main["p"]),
        **{"or": r6(main["or"])},
        or_ci95=None if main["or_ci95"] is None else [r6(x) for x in main["or_ci95"]],
        breslow_day_p=r6(main["breslow_day_p"]),
        level_sensitivity={"status": sens["status"], "strata_used": sens["strata_used"], "cmh_p": r6(sens["p"]), "or": r6(sens["or"])},
    )


# ----------------------------------------------------------------------------------------------- decision
def decide(taxa_ok, n_ambient, artifact, o1):
    """Ordered, first match wins (specs/beam-agreement.md). -> (decision, reasons)."""
    if not taxa_ok:
        return "insufficient_evidence", ["taxa_unusable"]
    if n_ambient == 0:
        return "insufficient_evidence", ["no_ambient"]
    if artifact is None:
        return "insufficient_evidence", ["negative_control_not_computable"]
    if o1 == "unavailable":
        if artifact:
            return "reframe_diffuse", ["label_free_o3_only", "artifact_scores_higher"]
        return "insufficient_labels", ["no_labels"]
    if o1["status"] != "ok":
        return "insufficient_labels", [o1["status"]]
    if o1["n_concurrent"] < MIN_PER_ARM or o1["n_single"] < MIN_PER_ARM:
        return "insufficient_labels", ["fewer_than_%d_labels_per_arm" % MIN_PER_ARM]
    p, orr, bd = o1["cmh_p"], o1["or"], o1["breslow_day_p"]
    if p >= 0.05 or orr < 1.5:
        return "drop", ["cmh_p_not_below_0.05" if p >= 0.05 else "or_below_1.5"]
    if artifact:
        return "reframe_diffuse", ["artifact_scores_higher"]
    if orr >= 2 and bd is not None and bd >= 0.05:
        return "ship_tier", ["cmh_significant", "or_at_least_2", "homogeneous_or", "artifact_does_not_score_higher"]
    return "inconclusive", ["or_below_2" if orr < 2 else ("breslow_day_not_computable" if bd is None else "breslow_day_significant")]


# ----------------------------------------------------------------------------------------------- shuffle null
def eligible(o):
    return valid_bird(o) and not is_human(o)


def shuffle_groups(rows):
    """-> {(species key, UTC hour): {beam: [row index in file order]}} over the rows that can form an event."""
    groups = {}
    for i, o in enumerate(rows):
        if eligible(o):
            groups.setdefault((key_of(o), int(o["t"] // 3600)), {}).setdefault(o["beam"], []).append(i)
    return groups


def shuffled_rows(rows, groups, rng):
    """One null draw: inside each species/hour group every beam keeps its report count, confidences and levels and takes a random
    sample (without replacement) of the group's distinct report times. A beam with more reports than the group has distinct times
    keeps the surplus reports at their real times. -> (rows sorted by t, reports moved, reports left at their real time)."""
    out, moved, kept = [], 0, 0
    for g in sorted(groups):
        beams = groups[g]
        pool = sorted({rows[i]["t"] for b in beams.values() for i in b})
        for beam in sorted(beams):
            idx = beams[beam]
            k = min(len(idx), len(pool))
            picks = rng.sample(pool, k)
            for i, t in zip(idx[:k], picks):
                out.append((t, i, dict(rows[i], t=t)))
            for i in idx[k:]:
                out.append((rows[i]["t"], i, rows[i]))
            moved += k
            kept += len(idx) - k
    out.sort(key=lambda x: (x[0], x[1]))
    return [o for _, _, o in out], moved, kept


def band_counts(events, band_of, nbands):
    """[[events, events with a concurrent window]] per band over in-region species events."""
    c = [[0, 0] for _ in range(nbands)]
    for e in in_region_species(events):
        row = c[band_of(e.max_conf)]
        row[0] += 1
        row[1] += 1 if e.concurrent_windows >= 1 else 0
    return c


def quantile_cuts(values, quantiles=QUANTILES):
    v = sorted(values)
    if not v:
        return None
    return [v[min(len(v) - 1, int(q * len(v)))] for q in quantiles]


def _null_rows(real, runs, labels):
    """real: [[n, passes]] per band; runs: one such list per trial -> the per-band report rows."""
    out = []
    for i, lab in enumerate(labels):
        rates = [r[i][1] / r[i][0] for r in runs if r[i][0] > 0]
        ns = [r[i][0] for r in runs]
        rr = frac(real[i][1], real[i][0])
        nm = None if not rates else sum(rates) / len(rates)
        out.append(
            dict(
                lab,
                n=real[i][0],
                real_pass_rate=r6(rr),
                null_pass_rate=r6(nm),
                null_min=r6(min(rates)) if rates else None,
                null_max=r6(max(rates)) if rates else None,
                null_n_mean=r6(sum(ns) / len(ns)) if ns else None,
                excess=r6(rr - nm) if rr is not None and nm is not None else None,
                runs=len(rates),
            )
        )
    return out


def shuffle_null(rows, events, gap_s, taxa, trials=NULL_TRIALS, seed=NULL_SEED, rng=None):
    """Misassociation null for the agreement statistic (event with >= 1 window holding >= 2 beams): per-beam report times are
    shuffled within the same species and UTC hour (per-beam rates, confidences and species mix kept), the events are re-clustered
    and the pass fraction is taken per bin; the real fraction is reported beside the mean over `trials` seeded draws. The measured
    quantile bands (cut points of the real in-region species event confidences) are report-only: no tier or decision reads them."""
    rng = random.Random(seed) if rng is None else rng
    groups = shuffle_groups(rows)
    nb = len(BIN_EDGES) - 1
    cuts = quantile_cuts([e.max_conf for e in in_region_species(events)])
    qband = (lambda c: bisect.bisect_right(cuts, c)) if cuts is not None else (lambda c: 0)
    nq = len(QUANTILES) + 1
    real_f, real_q = band_counts(events, bin_index, nb), band_counts(events, qband, nq)
    runs_f, runs_q, moved, kept = [], [], 0, 0
    for _ in range(int(trials)):
        shuf, m, k = shuffled_rows(rows, groups, rng)
        moved, kept = moved + m, kept + k
        ev = cluster(shuf, gap_s, taxa)
        runs_f.append(band_counts(ev, bin_index, nb))
        runs_q.append(band_counts(ev, qband, nq))
    tot = lambda c: [[sum(r[0] for r in c), sum(r[1] for r in c)]]
    bins = _null_rows(real_f, runs_f, [{"lo": BIN_EDGES[i], "hi": BIN_EDGES[i + 1]} for i in range(nb)])
    pooled = _null_rows(tot(real_f), [tot(r) for r in runs_f], [{}])[0]
    if cuts is None:
        quant = {"gating": False, "quantiles": list(QUANTILES), "cuts": None, "arbitrary_cuts": [], "bands": []}
    else:
        edges = [0.0] + cuts + [1.0]
        evs = in_region_species(events)
        quant = {
            "gating": False,
            "quantiles": list(QUANTILES),
            "cuts": [r6(c) for c in cuts],
            "arbitrary_cuts": [{"cut": c, "fraction_below": r6(sum(1 for e in evs if e.max_conf < c) / len(evs))} for c in ARBITRARY_CUTS],
            "bands": _null_rows(real_q, runs_q, [{"lo": r6(edges[i]), "hi": r6(edges[i + 1])} for i in range(nq)]),
        }
    return {
        "statistic": "event_with_concurrent_window",
        "shuffle": "per_beam_report_times_within_species_hour",
        "trials": int(trials),
        "seed": seed,
        "groups": len(groups),
        "reports_shuffled": moved // trials if trials else 0,
        "reports_unshuffled": kept // trials if trials else 0,
        "bins": bins,
        "all": pooled,
        "quantile_bands": quant,
    }


# ----------------------------------------------------------------------------------------------- report
def build_report(
    rows, garbage, gap_s, taxa, ambient, labels, generated_s, private_detail=False, label_stats=None, ambient_garbage=0, null_trials=0, null_seed=NULL_SEED
):
    valid = [o for o in rows if valid_bird(o)]
    filtered = sum(1 for o in rows if isinstance(o, dict) and o.get("k") == "bird_filtered")
    events = cluster(rows, gap_s, taxa)
    events3 = events if gap_s == LABEL_GAP_S else cluster(rows, LABEL_GAP_S, taxa)
    neg = negative_control(events, ambient)
    o1 = "unavailable" if labels is None else o1_labels(events3, labels)
    last_t = max((o["t"] for o in valid), default=0.0)
    decision, reasons = decide(taxa.ok, len(ambient), neg["artifact_scores_higher"], o1)
    rep = {
        "k": "beam_agreement_report",
        "v": 1,
        "generated_s": generated_s,
        "input": {
            "reports": len(valid),
            "events": len(events),
            "gap_s": gap_s,
            "first_t": r6(min((o["t"] for o in valid), default=0.0)),
            "last_t": r6(last_t),
            "garbage_lines": garbage,
            "filtered_skipped": filtered,
            "beam_pairs_with_unequal_t": unequal_t_pairs(events),
            "taxa_missing": 0 if taxa.ok else len(events),
            "ambient_records": len(ambient),
            "ambient_garbage": ambient_garbage,
            "labels": None if label_stats is None else dict(label_stats, usable=len(labels)),
        },
        "definition": {"tier": "concurrent", "top2_cap": True, "window_equality": "exact_t", "label_gap_s": LABEL_GAP_S, "min_labels_per_arm": MIN_PER_ARM},
        "bins": bin_table(events),
        "sensitivity_gap_3s": {"events": len(events3), "bins": bin_table(events3)},
        "negative_control": neg,
        "outcomes": {"o1": o1, "o2_proxy": o2_recurrence(events, last_t)},
        "decision": decision,
        "reasons": reasons,
    }
    if null_trials > 0:
        rep["shuffle_null"] = shuffle_null(rows, events, gap_s, taxa, null_trials, null_seed)
    if private_detail:
        cnt = {}
        for e in in_region_species(events):
            cnt[e.common] = cnt.get(e.common, 0) + 1
        rep["private_detail"] = {"top_species": [{"common": k, "events": v} for k, v in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[:10]]}
    return rep


def _inside(path, dirpath):
    p, d = os.path.realpath(path), os.path.realpath(dirpath)
    return p == d or p.startswith(d + os.sep)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--birds", required=True, help="birds.jsonl (read only)")
    ap.add_argument("--ambient", help="history/ambient_temp.jsonl (read only); absent or empty gives decision insufficient_evidence")
    ap.add_argument("--labels", help="owner verdicts, one beam_label v1 JSON line each (specs/beam-agreement.md)")
    ap.add_argument("--taxa", help="taxa.json (default: ui/taxa.json next to this repo's analysis/ directory)")
    ap.add_argument("--gap-s", type=float, default=5.0, help="same-species event chaining gap for the descriptive tables (default 5; labels always use 3)")
    ap.add_argument("--null-trials", type=int, default=NULL_TRIALS, help="shuffle null repetitions (default 200; 0 omits the shuffle_null block)")
    ap.add_argument("--null-seed", type=int, default=NULL_SEED, help="shuffle null seed (default fixed: the report is deterministic)")
    ap.add_argument("--generated-s", type=float, default=None, help="value of generated_s (default: now); fix it for a reproducible report")
    ap.add_argument("--out", help="write the report here (default stdout); refused inside the directory of any input")
    ap.add_argument("--private-detail", action="store_true", help="add the top-10 species names (owner only; the default report holds group aggregates)")
    a = ap.parse_args(argv)
    if not (a.gap_s > 0):
        print("--gap-s must be > 0", file=sys.stderr)
        return 2
    if a.null_trials < 0:
        print("--null-trials must be >= 0", file=sys.stderr)
        return 2
    inputs = [p for p in (a.birds, a.ambient, a.labels) if p]
    if a.out and any(_inside(a.out, os.path.dirname(os.path.abspath(p))) for p in inputs):
        print("refusing to write --out inside the directory of an input file (the live data directories are read only)", file=sys.stderr)
        return 2
    try:
        rows, garbage = read_jsonl(a.birds)
        labels = lstats = None
        if a.labels:
            labels, lstats = read_labels(a.labels)
    except OSError as ex:
        print("cannot read input: %s" % type(ex).__name__, file=sys.stderr)
        return 2
    ambient, amb_garbage = load_ambient(a.ambient)
    taxa = Taxa(a.taxa or default_taxa_path())
    try:
        cons = cluster_counted(rows, a.gap_s, taxa)[1].check()
    except E.ConservationError as ex:
        print("event conservation FAILED: %s" % ex, file=sys.stderr)
        return 1
    print("event conservation ok: %s" % json.dumps(cons, sort_keys=True), file=sys.stderr)
    rep = build_report(
        rows,
        garbage,
        a.gap_s,
        taxa,
        ambient,
        labels,
        time.time() if a.generated_s is None else a.generated_s,
        a.private_detail,
        lstats,
        amb_garbage,
        a.null_trials,
        a.null_seed,
    )
    text = json.dumps(rep, separators=(",", ":")) + "\n"
    if a.out:
        with open(a.out, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
