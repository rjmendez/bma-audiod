#!/usr/bin/env python3
"""Empirical beam response map from natural sources (offline, read-only, stdlib only, metadata only).

Question: does the level ratio between neighbouring DSP beams give a repeatable bearing on real sources? For every
source (a BirdNET common name at high confidence) the tool joins the source's events to the 1 Hz `spatial.jsonl`
level vectors of ALL 8 beams, subtracts each beam's own running median (the quiet baseline), and asks whether the
8-beam excess has a first-harmonic (cosine over the ring) amplitude A1 and a phase that is repeatable, against a
matched null drawn from event-free windows of the same UTC hours. It never estimates a bearing.

    python -I beam_response_map.py [--analysis-dir DIR] [--bands-log FILE|''] [--taxa FILE] [--source NAME ...]
        [--min-conf 0.85] [--min-events 30] [--out FILE] [--json] [--epoch ISO=LABEL ...] ...

Time: the `t` of a spatial or birds record is the wall time of the END of that record's window (host.py: t_wall = now -
(hi - (nxt + n)) / FS, the same for every plugin; spatial window 1 s, birds window 3 s). A birds event at t therefore
covers (t - 3, t] and the spatial rows whose own 1 s window lies inside it have t in [t - 2, t]: the default join.
Level vector: `e_rel_db` is e_db - max(e_db) per row. Subtracting a row constant is undone exactly by centring the row on
its beam mean, so the tool centres first (centred = e_db - mean(e_db)); the baseline median and the excess are then taken
on the centred values, which makes them independent of the common level and of which beam happens to win.
Exit codes: 0 any verdict, 2 usage / unreadable input / missing taxa table, 3 zero usable spatial rows."""

import argparse, bisect, collections, datetime, json, math, os, random, sys, tempfile, time
from array import array

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:  # python -I does not put the script directory on sys.path
    sys.path.insert(0, HERE)
import taxa as TX  # noqa: E402

VERSION = 1
NB = 8
BAND_NAMES = ["b_50_125", "b_125_250", "b_250_500", "b_500_1000", "b_1000_2000", "b_2000_4000", "b_4000_8000", "b_8000_16000"]
IDENTICAL_BANDS = 3  # bands below 500 Hz: MSC 0.98-1.00, the beams are identical there (spec beam-coherence)
BROADBAND = "spatial_200_4000"
EVENT_WINDOW_S = 3.0  # the birds plugin window
LAG_SHIFTS = range(-10, 11)
CONFOUND = (
    "The sources are not fixed-position sources: a low R can mean omni beams or a moving source. A no_go reads "
    "not supported by natural sources; only a controlled source can prove the negative."
)
RANK = {"go": 5, "weak": 4, "inconsistent": 3, "no_go": 2, "insufficient": 1}
C8 = [math.cos(2 * math.pi * k / NB) for k in range(NB)]
S8 = [math.sin(2 * math.pi * k / NB) for k in range(NB)]


class Fail(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


# ----------------------------------------------------------------------------------------------- statistics
def pct(s, q):
    """Linear-interpolated quantile of a SORTED list (q 0..1); None for an empty list."""
    if not s:
        return None
    p = (len(s) - 1) * q
    lo = int(math.floor(p))
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (p - lo)


def median(vals):
    return pct(sorted(vals), 0.5)


def a1_phase(x):
    """First-harmonic amplitude (dB, = peak of the best-fit cosine) and phase (rad) of 8 beam values, beam k at 45 k degrees."""
    re = sum(v * c for v, c in zip(x, C8))
    im = sum(v * s for v, s in zip(x, S8))
    return (2.0 / NB) * math.hypot(re, im), math.atan2(im, re)


def rayleigh_p(n, rbar):
    """Rayleigh test of circular uniformity, Zar's approximation (Biostatistical Analysis, p. 617, as in CircStat circ_rtest.m
    [S: one source, the code comment]): R = n * rbar, p = exp(sqrt(1 + 4n + 4 (n^2 - R^2)) - (1 + 2n))."""
    if n < 1:
        return 1.0
    rn = n * rbar
    p = math.exp(math.sqrt(max(0.0, 1.0 + 4.0 * n + 4.0 * (n * n - rn * rn))) - (1.0 + 2.0 * n))
    return min(1.0, max(0.0, p))


def quart(vals, qs=(0.25, 0.5, 0.75)):
    s = sorted(vals)
    return {f"p{int(round(q * 100))}": rnd(pct(s, q)) for q in qs}


def rnd(x, nd=6):
    return None if x is None else round(float(x), nd)


def hour_of(t):
    return int(t % 86400 // 3600)


# ----------------------------------------------------------------------------------------------- input
def read_jsonl(paths, filters):
    for p in paths:
        with open(p, "rb") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except (ValueError, RecursionError):
                    filters["bad_json"] += 1
                    continue
                if isinstance(r, dict):
                    yield r


def num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def with_rotated(path, include_rotated):
    out = []
    if include_rotated and os.path.exists(path + ".1"):
        out.append(path + ".1")
    if os.path.exists(path):
        out.append(path)
    return out


class Series:
    """Rows sorted by time; `data` is flat, row i band b beam k at i*stride + b*8 + k, already centred per row and band."""

    def __init__(self, t, data, nb):
        self.t, self.data, self.nb, self.stride = t, data, nb, NB * nb


def load_spatial(paths, max_spread, filters):
    rows = []
    for r in read_jsonl(paths, filters):
        if r.get("k") != "spatial":
            continue
        e, t = r.get("e_rel_db"), r.get("t")
        if not (num(t) and isinstance(e, list) and len(e) == NB and all(num(v) for v in e)):
            filters["bad_vector"] += 1
            continue
        if max(e) - min(e) > max_spread:
            filters["spread_over_max"] += 1
            continue
        m = sum(e) / NB
        rows.append((float(t), [v - m for v in e]))
    rows.sort(key=lambda r: r[0])
    data = array("d")
    for _, c in rows:
        data.extend(c)
    return Series(array("d", [r[0] for r in rows]), data, 1)


def tail_lines(f, max_rows, since):
    """The lines of an open binary file; with max_rows > 0 only the last max_rows non-empty lines (cheap: they are not parsed first).
    `since` (epoch seconds or None) is applied after parsing, in load_bands."""
    if max_rows and max_rows > 0:
        return collections.deque((ln for ln in f if ln.strip()), maxlen=max_rows)
    return f


def load_bands(path, max_spread, max_rows=0, since=None):
    """beam_bands.jsonl streamed row by row into a float32 array (centred per band), sorted by time like load_spatial (a clock step or
    a merge of rotated files would otherwise break the bisect joins). max_rows > 0 keeps only the last max_rows lines; `since` drops rows
    before that epoch second. Returns the Series; .dropped counts rejected rows, .reordered is True when rows had to be sorted."""
    t, data, dropped = array("d"), array("f"), 0
    with open(path, "rb") as f:
        for line in tail_lines(f, max_rows, since):
            try:
                r = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if not isinstance(r, dict) or r.get("k") != "beam_bands":
                continue
            db, tu = r.get("db"), r.get("t_us")
            try:  # NaN / Inf propagate into the sum, a non-number raises TypeError
                ok = (
                    num(tu)
                    and num(r.get("zero_frac", 0))
                    and r.get("zero_frac", 0) <= 0.2
                    and isinstance(db, list)
                    and len(db) == NB
                    and all(isinstance(b, list) and len(b) == NB for b in db)
                    and math.isfinite(sum(sum(b) for b in db))
                )
                cols = list(zip(*db)) if ok else None
            except TypeError:
                ok = False
            if not ok:
                dropped += 1
                continue
            lv = [sum(b) / NB for b in db]
            if max(lv) - min(lv) > max_spread:
                dropped += 1
                continue
            if since is not None and tu / 1e6 < since:
                continue
            t.append(tu / 1e6)
            for col in cols:
                m = sum(col) / NB
                data.extend([v - m for v in col])
    stride = NB * NB
    reordered = any(t[i] < t[i - 1] for i in range(1, len(t)))
    if reordered:
        order = sorted(range(len(t)), key=t.__getitem__)
        t2, d2 = array("d", (t[i] for i in order)), array("f")
        for i in order:
            d2.extend(data[i * stride : (i + 1) * stride])
        t, data = t2, d2
    s = Series(t, data, NB)
    s.dropped, s.reordered = dropped, reordered
    return s


# ----------------------------------------------------------------------------------------------- events
def classify(tx, common, sci):
    """-> dict(human, group, non_species) from the taxa table (explicit labels first, lower-cased; then the genus table)."""
    names = [str(x).strip().lower() for x in (common, sci) if x]
    for n in names:
        if n.startswith("human") and n in tx.explicit:
            return {"human": True, "group": tx.explicit[n][0], "non_species": True}
    for n in names:
        if n in tx.explicit:
            g, ns = tx.explicit[n]
            return {"human": False, "group": g, "non_species": ns}
    g, ns = tx.lookup(sci or "", common)
    return {"human": False, "group": g, "non_species": bool(ns)}


def load_speech_times(tags_paths, filters):
    """Times (sorted) of the tags rows with speech_active true: a speech window is never joined and never a control."""
    out = [float(r["t"]) for r in read_jsonl(tags_paths, filters) if r.get("k") == "tags" and r.get("speech_active") is True and num(r.get("t"))]
    out.sort()
    return out


def near_any(sorted_times, t, guard):
    """True when some time of the sorted list lies within `guard` seconds of t (inclusive)."""
    if not sorted_times:
        return False
    j = bisect.bisect_left(sorted_times, t)
    return any(abs(t - sorted_times[x]) <= guard for x in (j - 1, j) if 0 <= x < len(sorted_times))


def load_events(paths, min_conf, tx, filters):
    """-> (events {common: [t...]}, sci {common: scientific}, all_times sorted (every record >= min_conf), human_times sorted).
    human_times: the time of EVERY human-voice record whatever its confidence; they only mark speech, they are never counted or reported."""
    groups, sci, allt, human_t = {}, {}, [], []
    for r in read_jsonl(paths, filters):
        if r.get("k") != "bird":
            continue
        c, t, name = r.get("confidence"), r.get("t"), r.get("common")
        if num(t) and isinstance(name, str) and name and classify(tx, name, r.get("scientific") if isinstance(r.get("scientific"), str) else "")["human"]:
            human_t.append(float(t))
            continue
        if not (num(c) and num(t) and isinstance(name, str) and name) or c < min_conf or r.get("in_region") is False:
            continue
        groups.setdefault((name, int(round(t))), []).append(float(t))
        sci.setdefault(name, r.get("scientific") if isinstance(r.get("scientific"), str) else "")
        allt.append(float(t))
    events = {}
    for (name, _), ts in groups.items():
        events.setdefault(name, []).append(min(ts))
    for v in events.values():
        v.sort()
    allt.sort()
    human_t.sort()
    return events, sci, allt, human_t


# ----------------------------------------------------------------------------------------------- segments, baseline, join, controls
class Seg:
    def __init__(self, idx, label, t0, t1, lo, hi):
        self.idx, self.label, self.t0, self.t1, self.lo, self.hi = idx, label, t0, t1, lo, hi


def make_segments(series, bounds, labels):
    """Segments [bounds[i], bounds[i+1]) with the row index range of `series` in each."""
    segs = []
    for i, b in enumerate(bounds):
        t1 = bounds[i + 1] if i + 1 < len(bounds) else math.inf
        segs.append(Seg(i, labels[i], b, t1, bisect.bisect_left(series.t, b), bisect.bisect_left(series.t, t1)))
    return segs


def seg_of(segs, t):
    i = bisect.bisect_right([s.t0 for s in segs], t) - 1
    return segs[max(0, i)]


class Engine:
    def __init__(self, series, P):
        self.s, self.P, self.cache = series, P, {}
        self.reads = 0  # rows touched by joins (a loop counter for the scale test)

    def window(self, seg, T, W):
        lo = bisect.bisect_left(self.s.t, T - W, seg.lo, seg.hi)
        hi = bisect.bisect_right(self.s.t, T + W, seg.lo, seg.hi)
        return lo, hi

    def baseline(self, seg, T, b):
        """Per-beam median of the centred rows within +-baseline_s of T inside the segment, or None (< min windows)."""
        key = (seg.idx, T, b)
        if key in self.cache:
            return self.cache[key]
        lo, hi = self.window(seg, T, self.P["baseline_s"])
        out = None
        if hi - lo >= self.P["min_baseline_windows"]:
            s = self.s
            out = [median(s.data[lo * s.stride + b * NB + k : hi * s.stride : s.stride]) for k in range(NB)]
        self.cache[key] = out
        return out

    def vec(self, seg, T, b, shift=0.0):
        """-> excess vector (mean centred joined rows - baseline), or 'unjoined' / 'no_baseline'."""
        s = self.s
        lo = bisect.bisect_left(s.t, T + shift - self.P["join_before_s"], seg.lo, seg.hi)
        hi = bisect.bisect_right(s.t, T + shift + self.P["join_after_s"], seg.lo, seg.hi)
        if hi <= lo:
            return "unjoined"
        base = self.baseline(seg, T, b)
        if base is None:
            return "no_baseline"
        acc = [0.0] * NB
        for i in range(lo, hi):
            o = i * s.stride + b * NB
            for k in range(NB):
                acc[k] += s.data[o + k]
        self.reads += hi - lo
        n = hi - lo
        return [acc[k] / n - base[k] for k in range(NB)]


class ControlPool:
    """Control windows: rows of one segment that are >= exclude_s from every event record, drawn per UTC hour of day with a seed that
    depends only on (seed, segment, hour): the j-th control of an hour is the same whatever sources were asked for."""

    def __init__(self, engine, seg, excl_times, exclude_s, seed, speech_times=(), speech_guard_s=0.0):
        s = engine.s
        self.engine, self.seg, self.seed, self.draws = engine, seg, seed, {}
        self.cands = {}
        P = engine.P
        for i in range(seg.lo, seg.hi):
            t = s.t[i]
            j = bisect.bisect_left(excl_times, t)
            near = min(abs(t - excl_times[x]) for x in (j - 1, j) if 0 <= x < len(excl_times)) if excl_times else math.inf
            if near < exclude_s or near_any(speech_times, t, speech_guard_s):
                continue
            lo, hi = engine.window(seg, t, P["baseline_s"])
            if hi - lo < P["min_baseline_windows"]:
                continue
            self.cands.setdefault(hour_of(t), []).append(i)
        self.vcache = {}

    def draw(self, hour, n):
        """First n control row indices of `hour` (with replacement); [] when the hour has no candidate."""
        c = self.cands.get(hour)
        if not c:
            return []
        d = self.draws.setdefault(hour, [])
        if len(d) < n:
            rng = random.Random(f"{self.seed}:{self.seg.idx}:{hour}:{len(d)}")  # re-seeded per index so extending never reorders
            for _ in range(len(d), n):
                d.append(c[rng.randrange(len(c))])
                rng = random.Random(f"{self.seed}:{self.seg.idx}:{hour}:{len(d)}")
        return d[:n]

    def vec(self, i, b):
        key = (i, b)
        if key not in self.vcache:
            v = self.engine.vec(self.seg, self.engine.s.t[i], b)
            self.vcache[key] = v if isinstance(v, list) else None
        return self.vcache[key]


# ----------------------------------------------------------------------------------------------- one block (source, band, segment)
def hist8(vs):
    h = [0] * NB
    for v in vs:
        h[max(range(NB), key=lambda k: (v[k], -k))] += 1
    return h


def top2(v):
    s = sorted(v, reverse=True)
    return s[0] - s[1]


def block_stats(name, b, seg, times, eng, pool, P):
    """Raw statistics of one source in one segment and band (verdict decided later, once the test family size is known)."""
    ev = [t for t in times if seg.t0 <= t < seg.t1]
    evecs, jt, unj, nob = [], [], 0, 0
    for t in ev:
        v = eng.vec(seg, t, b)
        if v == "unjoined":
            unj += 1
        elif v == "no_baseline":
            nob += 1
        else:
            evecs.append(v)
            jt.append(t)
    # Detections of one bout (consecutive events no more than bout_gap_s apart) are pseudo-replicates of one observation: every
    # inferential statistic below runs on the per-bout mean vectors; `vecs` stays per event for the descriptive histograms only.
    bouts = []
    for i, t in enumerate(jt):
        if bouts and t - jt[bouts[-1][-1]] <= P["bout_gap_s"]:
            bouts[-1].append(i)
        else:
            bouts.append([i])
    vecs = evecs
    bvecs = [[sum(evecs[i][k] for i in g) / len(g) for k in range(NB)] for g in bouts]
    hours = [hour_of(jt[g[0]]) for g in bouts]
    blk = {"label": seg.label, "events": len(ev), "joined": len(evecs), "unjoined": unj, "events_no_baseline": nob, "bouts": len(bouts)}
    ne = len(evecs)
    n = len(bvecs)
    dropped = (unj + nob) / len(ev) if ev else 0.0
    if ne < P["min_events"]:
        blk["verdict"], blk["reason"] = "insufficient", f"{ne} joined events < {P['min_events']}"
        return blk, None
    if dropped > 0.30:
        blk["verdict"], blk["reason"] = "insufficient", f"{dropped:.2f} of the events dropped (unjoined or no baseline) > 0.30"
        return blk, None
    if n < P["min_bouts"]:
        blk["verdict"], blk["reason"] = "insufficient", f"{n} bouts < {P['min_bouts']} (events less than {P['bout_gap_s']:g} s apart form one bout)"
        return blk, None
    # matched controls: per-hour counts proportional to the events' hours
    per_hour = {}
    for h in hours:
        per_hour[h] = per_hour.get(h, 0) + 1
    ctl, ctl_by_hour = [], {}
    for h in sorted(per_hour):
        want = max(1, int(round(P["controls"] * per_hour[h] / n)))
        for i in pool.draw(h, want):
            v = pool.vec(i, b)
            if v is not None:
                ctl.append(v)
                ctl_by_hour.setdefault(h, []).append(v)
    if not ctl:
        blk["verdict"], blk["reason"] = "insufficient", "no control windows in the event hours"
        return blk, None
    a1 = [a1_phase(v) for v in bvecs]
    a1s = sorted(x[0] for x in a1)
    cos_m = sum(math.cos(x[1]) for x in a1) / n
    sin_m = sum(math.sin(x[1]) for x in a1) / n
    rbar = math.hypot(cos_m, sin_m)
    c_a1 = sorted(a1_phase(v)[0] for v in ctl)
    rng = random.Random(f"{P['seed']}:boot:{name}:{b}:{seg.idx}")
    meds = sorted(median([a1s[rng.randrange(n)] for _ in range(n)]) for _ in range(P["boot"]))
    obs = pct(a1s, 0.5)
    pools = {h: [a1_phase(v)[0] for v in vs] for h, vs in ctl_by_hour.items()}
    allc = [x for v in pools.values() for x in v]
    rng = random.Random(f"{P['seed']}:perm:{name}:{b}:{seg.idx}")
    ge = 0
    for _ in range(P["perms"]):
        pick = [pools[h][rng.randrange(len(pools[h]))] if h in pools else allc[rng.randrange(len(allc))] for h in hours]
        if median(pick) >= obs:
            ge += 1
    lag = []
    for s in LAG_SHIFTS:
        xs = []
        for t in ev:
            v = eng.vec(seg, t, b, float(s))
            if isinstance(v, list):
                xs.append(a1_phase(v)[0])
        lag.append({"shift_s": s, "median_a1_db": rnd(median(xs)) if xs else None})
    ratio = []
    for d in range(NB):
        sel = [v for v in vecs if max(range(NB), key=lambda k: (v[k], -k)) == d]
        if len(sel) >= 5:
            nx = sorted(v[d] - v[(d + 1) % NB] for v in sel)
            pv = sorted(v[d] - v[(d - 1) % NB] for v in sel)
            q = lambda s_: {f"p{p}": rnd(pct(s_, p / 100.0)) for p in (10, 25, 50, 75, 90)}
            ratio.append({"beam": d + 1, "n": len(sel), "to_next_db": q(nx), "to_prev_db": q(pv)})
    duty = EVENT_WINDOW_S * ne / max(1, seg.hi - seg.lo)
    blk.update(
        {
            "event_duty": rnd(duty),
            "baseline_contaminated": duty > 0.3,
            "dominant_hist": hist8(vecs),
            "control_dominant_hist": hist8(ctl),
            "ratio_by_dominant": ratio,
            "top2_db": quart([top2(v) for v in vecs]),
            "a1_db": {**quart(a1s), "p95": rnd(pct(a1s, 0.95))},
            "a1_ci95": [rnd(pct(meds, 0.025)), rnd(pct(meds, 0.975))],
            "phase_deg": rnd(math.degrees(math.atan2(sin_m, cos_m)), 3),
            "R": rnd(rbar),
            "rayleigh_p": rnd(rayleigh_p(n, rbar), 12),
            "perm_p": rnd((ge + 1) / (P["perms"] + 1)),
            "control": {"n": len(ctl), "a1_db": {**quart(c_a1), "p95": rnd(pct(c_a1, 0.95))}, "top2_db": quart([top2(v) for v in ctl])},
            "lag_scan": lag,
        }
    )
    raw = {
        "a1_med": obs,
        "ci_lo": pct(meds, 0.025),
        "ctl_p95": pct(c_a1, 0.95),
        "ctl_med": pct(c_a1, 0.5),
        "R": rbar,
        "ray": rayleigh_p(n, rbar),
        "perm": (ge + 1) / (P["perms"] + 1),
    }
    return blk, raw


def decide(blk, raw, P, alpha):
    """Verdict of an evaluated block. go: ALL of median A1 >= max(a1_min_db, factor x control p95), R >= r_min, Rayleigh p < alpha, the
    bootstrap CI of the median A1 above the control median. weak: Rayleigh p < alpha and the permutation p at its floor or < alpha."""
    thr = max(P["a1_min_db"], P["a1_control_factor"] * raw["ctl_p95"])
    c = {
        "median A1 >= threshold": raw["a1_med"] >= thr,
        "R >= r_min": raw["R"] >= P["r_min"],
        "Rayleigh p < alpha": raw["ray"] < alpha,
        "CI above control median": raw["ci_lo"] > raw["ctl_med"],
    }
    if all(c.values()):
        return "go", f"median A1 {raw['a1_med']:.2f} dB >= {thr:.2f} dB, R {raw['R']:.2f}, Rayleigh p {raw['ray']:.2g} < {alpha:.2g}"
    floor = 1.0 / (P["perms"] + 1)
    if raw["ray"] < alpha and raw["perm"] <= max(alpha, floor):
        return "weak", "non-uniform and above the permutation null, but " + ", ".join(k for k, v in c.items() if not v) + " failed"
    return (
        "no_go",
        f"median A1 {raw['a1_med']:.2f} dB vs threshold {thr:.2f} dB (control p95 {raw['ctl_p95']:.2f}), R {raw['R']:.2f}, Rayleigh p {raw['ray']:.2g}",
    )


def aggregate(blocks):
    """Source-band verdict over segments: the classes of the segments that have enough events must agree."""
    v = [b["verdict"] for b in blocks if b["verdict"] != "insufficient"]
    if not v:
        return "insufficient", "no segment has enough events"
    if len(set(v)) > 1:
        return "inconsistent", "segments disagree: " + ", ".join(f"{b['label']} {b['verdict']}" for b in blocks if b["verdict"] != "insufficient")
    return v[0], next(b["reason"] for b in blocks if b["verdict"] == v[0])


# ----------------------------------------------------------------------------------------------- the analysis
def default_params():
    return {
        "min_conf": 0.85,
        "min_events": 30,
        "max_spread_db": 30.0,
        "baseline_s": 900.0,
        "min_baseline_windows": 50,
        "join_before_s": 2.0,
        "join_after_s": 0.0,
        "controls": 2000,
        "exclude_s": 10.0,
        "seed": 1,
        "boot": 1000,
        "perms": 1000,
        "a1_min_db": 1.0,
        "a1_control_factor": 3.0,
        "r_min": 0.5,
        "alpha": 0.01,
        "cluster_gap_s": 86400.0,
        "bout_gap_s": 60.0,
        "min_bouts": 10,
        "speech_guard_s": 5.0,
        "bands_max_rows": 60000,
    }


def segments_plan(t, epochs, P):
    """Segment boundaries: gaps in the spatial rows longer than cluster_gap_s (separate recording clusters) plus the owner's --epoch
    starts. Returns (bounds, labels, n_clusters); the first segment starts at -inf."""
    cuts = [(t[i], 0, None) for i in range(1, len(t)) if t[i] - t[i - 1] > P["cluster_gap_s"]]
    cuts += [(ts, 1, label) for ts, label in epochs]
    cuts.sort(key=lambda c: (c[0], c[1]))
    nclust = 1 + sum(1 for c in cuts if c[1] == 0)
    ci, ep = 0, ("e0" if epochs else None)
    bounds, labels = [-math.inf], [lab(nclust, 0, ep)]
    for ts, kind, label in cuts:
        if kind == 0:
            ci += 1
        else:
            ep = label
        if bounds[-1] == ts:
            labels[-1] = lab(nclust, ci, ep)
        else:
            bounds.append(ts)
            labels.append(lab(nclust, ci, ep))
    return bounds, labels, nclust


def lab(nclust, ci, epoch):
    parts = ([f"c{ci + 1}"] if nclust > 1 else []) + ([epoch] if epoch else [])
    return "+".join(parts) if parts else "all"


def analyze(P, spatial_paths, birds_paths, bands_path, tx, sources_req=None, epochs=(), now=None, tags_paths=(), bands_since=None):
    filters = {"spread_over_max": 0, "bad_vector": 0, "no_baseline": 0, "bad_json": 0}
    spat = load_spatial(spatial_paths, P["max_spread_db"], filters)
    if not len(spat.t):
        raise Fail(3, "no usable spatial rows")
    events, sci, ev_times, human_t = load_events(birds_paths, P["min_conf"], tx, filters)
    speech_t = sorted(human_t + load_speech_times(list(tags_paths), filters))
    guard = P["speech_guard_s"]
    if speech_t:  # speech windows feed no record: no event and no control near one
        events = {n_: [t for t in v if not near_any(speech_t, t, guard)] for n_, v in events.items()}
        events = {n_: v for n_, v in events.items() if v}
    warnings = []
    bounds, labels, nclust = segments_plan(spat.t, list(epochs), P)
    segs_s = make_segments(spat, bounds, labels)
    eng_s = Engine(spat, P)
    for sg in segs_s:  # rows without a baseline (counted, not removed)
        for i in range(sg.lo, sg.hi):
            lo, hi = eng_s.window(sg, spat.t[i], P["baseline_s"])
            if hi - lo < P["min_baseline_windows"]:
                filters["no_baseline"] += 1
    bands, bdrop, segs_b, eng_b = None, 0, None, None
    if bands_path and os.path.exists(bands_path):
        bands = load_bands(bands_path, P["max_spread_db"], P["bands_max_rows"], bands_since)
        bdrop = bands.dropped
        if bands.reordered:
            warnings.append("beam_bands rows were not in time order: sorted by time")
        if P["bands_max_rows"] and len(bands.t) + bdrop >= P["bands_max_rows"]:
            warnings.append(f"beam_bands: only the last {P['bands_max_rows']} rows were read (--bands-max-rows), band verdicts cover that period only")
        if len(bands.t):
            segs_b = make_segments(bands, bounds, labels)
            eng_b = Engine(bands, P)
        else:
            bands = None
    if bands is None:
        warnings.append("beam_bands not read: broadband only")
    if not epochs:
        warnings.append("beam pattern epoch unknown")
    if nclust > 1:
        warnings.append(f"spatial history has {nclust} clusters separated by gaps longer than {int(P['cluster_gap_s'])} s: analysed separately")

    # sources
    auto = not sources_req
    chosen, skipped = [], []
    names = sorted(events) if auto else []
    if auto:
        for n_ in names:
            if len(events[n_]) < P["min_events"]:
                continue
            c = classify(tx, n_, sci[n_])
            if c["non_species"] or c["group"] == "mechanical":
                skipped.append({"name": n_, "reason": "non_biological"})
                continue
            chosen.append(n_)
    else:
        lower = {n_.lower(): n_ for n_ in events}
        for want in sources_req:
            n_ = lower.get(want.lower(), want)
            if classify(tx, n_, sci.get(n_, ""))["human"] or want.lower().startswith("human"):
                raise Fail(2, "human-voice classes can never be a source")
            chosen.append(n_)
    bands_in_use = [(BROADBAND, spat, segs_s, eng_s, 0)]
    if bands is not None:
        bands_in_use += [(BAND_NAMES[j], bands, segs_b, eng_b, j) for j in range(NB)]
    pools = {}

    def pool_for(series_key, seg, eng):
        k = (series_key, seg.idx)
        if k not in pools:
            pools[k] = ControlPool(eng, seg, ev_times, P["exclude_s"], P["seed"], speech_t, guard)
        return pools[k]

    entries, raws = [], []
    for name in chosen:
        c = classify(tx, name, sci.get(name, ""))
        nonbio = bool(c["non_species"] or c["group"] == "mechanical")
        times = events.get(name, [])
        for bname, series, segs, eng, bi in bands_in_use:
            ent = {
                "name": name,
                "band": bname,
                "kind": "non_biological" if nonbio else "species",
                "suspected_artifact": c["group"] == "insect",
                "events": len(times),
                "epochs": [],
            }
            entries.append(ent)
            if bname != BROADBAND and bi < IDENTICAL_BANDS:
                ent["verdict"], ent["reason"] = "identical_expected", "bands below 500 Hz: the beams are identical there (MSC 0.98-1.00)"
                continue
            stimes = times if series is spat else [t for t in times if series.t[0] <= t <= series.t[-1]]  # a capped bands log covers less
            if not stimes:
                ent["verdict"], ent["reason"] = (
                    "insufficient",
                    "no events at the confidence floor" if not times else "no events inside the period the band rows cover",
                )
                continue
            for sg in segs:
                if not any(sg.t0 <= t < sg.t1 for t in stimes):
                    continue
                blk, raw = block_stats(name, bi, sg, stimes, eng, pool_for("spatial" if series is spat else "bands", sg, eng), P)
                ent["epochs"].append(blk)
                if raw:
                    raws.append((ent, blk, raw))
    tests = len({id(r[0]) for r in raws})
    alpha = P["alpha"] / max(1, tests)
    for _, blk, raw in raws:
        blk["verdict"], blk["reason"] = decide(blk, raw, P, alpha)
    for e in entries:
        if "verdict" not in e:
            e["verdict"], e["reason"] = aggregate(e["epochs"])
            if any(b.get("baseline_contaminated") for b in e["epochs"]):
                warnings.append(f"{e['name']} {e['band']}: events fill more than 30% of the windows, the baseline is contaminated")
    cnt = {}
    for e in entries:
        if e["verdict"] != "identical_expected" and not e["suspected_artifact"] and e["kind"] == "species":
            cnt[e["verdict"]] = cnt.get(e["verdict"], 0) + 1
    best = max(cnt, key=lambda v: RANK[v]) if cnt else "insufficient"
    evaluated = len({e["name"] for e in entries if e["verdict"] not in ("identical_expected",) and not e["suspected_artifact"] and e["kind"] == "species"})
    overall = {
        "verdict": best,
        "sources_evaluated": evaluated,
        "reason": "no biological source with a verdict" if not cnt else ", ".join(f"{k} {v}" for k, v in sorted(cnt.items())),
    }
    cl = []
    for sg in segs_s:
        cl.append(
            {
                "label": sg.label,
                "t_first": rnd(spat.t[sg.lo], 1) if sg.hi > sg.lo else None,
                "t_last": rnd(spat.t[sg.hi - 1], 1) if sg.hi > sg.lo else None,
                "rows": sg.hi - sg.lo,
            }
        )
    return {
        "k": "beam_response_map",
        "v": VERSION,
        "generated_t": rnd(time.time() if now is None else now, 1),
        "inputs": {
            "spatial_rows": len(spat.t),
            "t_first": rnd(spat.t[0], 1),
            "t_last": rnd(spat.t[-1], 1),
            "bands_rows": None if bands is None else len(bands.t),
            "bands_dropped": None if bands is None else bdrop,
            "rotated_read": any(p.endswith(".1") for p in spatial_paths + birds_paths),
        },
        "params": {
            k_: P[k_]
            for k_ in (
                "min_conf",
                "min_events",
                "max_spread_db",
                "baseline_s",
                "min_baseline_windows",
                "join_before_s",
                "join_after_s",
                "controls",
                "exclude_s",
                "seed",
                "boot",
                "perms",
                "cluster_gap_s",
                "bout_gap_s",
                "min_bouts",
                "speech_guard_s",
                "bands_max_rows",
            )
        }
        | {
            "thresholds": {
                "a1_min_db": P["a1_min_db"],
                "a1_control_factor": P["a1_control_factor"],
                "r_min": P["r_min"],
                "alpha": P["alpha"],
                "tests": tests,
                "alpha_adjusted": rnd(alpha, 12),
            }
        },
        "filters": filters,
        "segments": cl,
        "epochs": [{"t_start": rnd(ts, 1), "label": lb} for ts, lb in epochs] or None,
        "skipped_sources": skipped,
        "sources": entries,
        "overall": overall,
        "confound": CONFOUND,
        "warnings": warnings,
    }, eng_s.reads


# ----------------------------------------------------------------------------------------------- CLI
def to_json(report):
    return json.dumps(report, ensure_ascii=True, separators=(",", ":")) + "\n"


def write_atomic(path, text):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".brm_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError as ex:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise Fail(2, f"cannot write the output: {type(ex).__name__}") from None


def inside(path, directory):
    p, d = os.path.realpath(path), os.path.realpath(directory)
    try:
        return os.path.commonpath([p, d]) == d
    except ValueError:
        return False


def parse_iso(ts, flag):
    try:
        d = datetime.datetime.fromisoformat(ts)
    except ValueError:
        raise Fail(2, f"{flag} time {ts!r} is not an ISO timestamp") from None
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d.timestamp()


def parse_epoch(s):
    ts, sep, label = s.partition("=")
    if not sep or not label:
        raise Fail(2, f"--epoch needs ISO=LABEL, got {s!r}")
    return parse_iso(ts, "--epoch"), label


def find_taxa(arg):
    cands = [arg] if arg else [os.path.join(HERE, "taxa.json"), os.path.join(HERE, "..", "ui", "taxa.json")]
    for c in cands:
        if os.path.exists(c):
            return c
    raise Fail(2, "taxa table not found (--taxa FILE): without it human-voice labels cannot be excluded")


def text_report(r):
    out = [
        f"beam_response_map: {r['inputs']['spatial_rows']} spatial rows, {len(r['segments'])} segment(s)",
        f"overall {r['overall']['verdict']} ({r['overall']['sources_evaluated']} sources): {r['overall']['reason']}",
    ]
    for e in r["sources"]:
        flag = " [suspected artifact]" if e["suspected_artifact"] else (" [non-biological]" if e["kind"] == "non_biological" else "")
        out.append(f"  {e['name']:<28} {e['band']:<18} events {e['events']:>5}  {e['verdict']:<18} {e['reason']}{flag}")
    out += [f"warning: {w}" for w in r["warnings"]]
    out.append(r["confound"])
    return "\n".join(out)


def _bounded(conv, lo, hi, lo_open, what):
    """argparse type: a finite number of type `conv` in the range (an open lower bound excludes lo)."""

    def f(x):
        try:
            v = conv(x)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{x!r} is not {what}") from None
        if isinstance(v, float) and not math.isfinite(v):
            raise argparse.ArgumentTypeError(f"{x!r} is not finite")
        if (lo is not None and (v <= lo if lo_open else v < lo)) or (hi is not None and v > hi):
            raise argparse.ArgumentTypeError(f"{x!r} is out of range: need {what}")
        return v

    return f


FLAG_TYPES = {  # every numeric flag: its type and range (a bad value is exit 2, never a traceback)
    "min_conf": _bounded(float, 0.0, 1.0, False, "a number in [0, 1]"),
    "min_events": _bounded(int, 1, None, False, "an integer >= 1"),
    "max_spread_db": _bounded(float, 0.0, None, True, "a number > 0"),
    "baseline_s": _bounded(float, 0.0, None, True, "a number > 0"),
    "min_baseline_windows": _bounded(int, 1, None, False, "an integer >= 1"),
    "join_before_s": _bounded(float, 0.0, None, False, "a number >= 0"),
    "join_after_s": _bounded(float, 0.0, None, False, "a number >= 0"),
    "controls": _bounded(int, 1, None, False, "an integer >= 1"),
    "exclude_s": _bounded(float, 0.0, None, False, "a number >= 0"),
    "seed": int,
    "boot": _bounded(int, 1, None, False, "an integer >= 1"),
    "perms": _bounded(int, 1, None, False, "an integer >= 1"),
    "a1_min_db": _bounded(float, 0.0, None, False, "a number >= 0"),
    "a1_control_factor": _bounded(float, 0.0, None, False, "a number >= 0"),
    "r_min": _bounded(float, 0.0, 1.0, False, "a number in [0, 1]"),
    "alpha": _bounded(float, 0.0, 1.0, True, "a number in (0, 1]"),
    "cluster_gap_s": _bounded(float, 0.0, None, True, "a number > 0"),
    "bout_gap_s": _bounded(float, 0.0, None, False, "a number >= 0"),
    "min_bouts": _bounded(int, 1, None, False, "an integer >= 1"),
    "speech_guard_s": _bounded(float, 0.0, None, False, "a number >= 0"),
    "bands_max_rows": _bounded(int, 0, None, False, "an integer >= 0 (0 = read every row)"),
}


def main(argv=None):
    d0 = os.environ.get("BMA_DATA_DIR") or "data"
    D = default_params()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--analysis-dir", default=os.path.join(d0, "analysis"), help="holds spatial.jsonl and birds.jsonl (read only)")
    ap.add_argument("--bands-log", default=os.path.join(d0, "reports", "beam_bands.jsonl"), help="beam_bands.jsonl for the per-band mode ('' = off)")
    ap.add_argument("--taxa", help="taxa.json (default: next to this file, else ../ui/taxa.json); missing = exit 2")
    ap.add_argument("--source", action="append", help="a BirdNET common name (repeatable); default: every non-mechanical source with enough events")
    ap.add_argument("--epoch", action="append", default=[], help="beam-pattern epoch start as ISO=LABEL (naive times are UTC), repeatable")
    ap.add_argument("--no-rotated", action="store_true", help="do not read the .1 rotated generation (read by default when present)")
    ap.add_argument("--out", help="write the JSON report here (atomic); refused inside --analysis-dir")
    ap.add_argument("--json", action="store_true", help="print the JSON report instead of the text table")
    ap.add_argument("--bands-since", help="ignore beam_bands rows before this ISO time (naive = UTC)")
    for k, typ in FLAG_TYPES.items():
        ap.add_argument("--" + k.replace("_", "-"), type=typ, default=D[k])
    try:
        a = ap.parse_args(argv)
    except SystemExit as ex:
        return int(ex.code or 0)
    P = {k: getattr(a, k) for k in D}
    try:
        if a.out and inside(a.out, a.analysis_dir):
            raise Fail(2, "refusing --out inside the analysis directory")
        epochs = sorted(parse_epoch(e) for e in a.epoch)
        since = parse_iso(a.bands_since, "--bands-since") if a.bands_since else None
        tx = TX.Taxa(find_taxa(a.taxa))
        if not tx.ok:
            raise Fail(2, "taxa table unusable: human-voice labels cannot be excluded")
        sp = with_rotated(os.path.join(a.analysis_dir, "spatial.jsonl"), not a.no_rotated)
        bp = with_rotated(os.path.join(a.analysis_dir, "birds.jsonl"), not a.no_rotated)
        if not os.path.exists(os.path.join(a.analysis_dir, "spatial.jsonl")):
            raise Fail(2, "spatial.jsonl not found in the analysis directory")
        if not os.path.exists(os.path.join(a.analysis_dir, "birds.jsonl")):
            raise Fail(2, "birds.jsonl not found in the analysis directory")
        tg = with_rotated(os.path.join(a.analysis_dir, "tags.jsonl"), not a.no_rotated)
        report, _ = analyze(P, sp, bp, a.bands_log or None, tx, a.source, epochs, tags_paths=tg, bands_since=since)
        text = to_json(report)
        if a.out:
            write_atomic(a.out, text)
        print(text, end="") if a.json else print(text_report(report))
        return 0
    except Fail as f:
        print(f"beam_response_map: {f.msg}", file=sys.stderr)
        return f.code
    except OSError as ex:
        print(f"beam_response_map: cannot read the input ({type(ex).__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
