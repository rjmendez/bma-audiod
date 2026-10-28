#!/usr/bin/env python3
"""Read-only burst-source analysis: which hypothesis explains the 8-16 kHz bursting (insect, pulser, housing thermal, unit electronics)?

    python -I burst_source_report.py --data-dir DIR --out DIR [--beam-bands FILE] [--since ISO] [--until ISO]
                                     [--thresholds FILE] [--jitter-surrogates 200] [--seed 20261026] [--max-surrogate-pairs N]
                                     [--no-write-digest] [--self-check]

Reads the metadata logs the pipeline already writes (dsp.jsonl(.1), spatial.jsonl, coherence.jsonl, ambient_temp.jsonl(.1), board_history.jsonl,
optional beam_bands.jsonl and bursts.jsonl) and writes three files under --out (burst_scorecard.json, burst_hours.jsonl, burst_scorecard.md).
No audio is read or written, no network call is made, nothing outside --out is touched. The rules are frozen in burst_source_thresholds.json;
the design and the detector parameters are in docs/design/burst-source-analysis.md, the output contract in contracts/specs/burst-scorecard.md.
Exit codes: 0 report written (even when every verdict is not-measurable), 2 usage (also an --out inside an input directory), 3 no dsp input or no numpy."""

import argparse, calendar, hashlib, json, math, os, sys, tempfile, time

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

TOOL_VERSION = "1.0.0"
PINNED_THRESHOLDS_SHA256 = "546c4bf4b5e4b388fe2a0f8b1db38ae06d9e3e3094703ec7d5948d33feb33413"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_THRESHOLDS = os.path.join(HERE, "burst_source_thresholds.json")
MAX_LINE = 1 << 20
T_MIN, T_MAX = 1577836800.0, 4102444800.0  # 2020-01-01 .. 2100-01-01
HYPOTHESES = ("insect", "pulser", "thermal", "electronics")
RAW_KEYS = ("ioi", "acorr", "level_db", "excess_db_median", "beam", "spatial", "band_8_16k", "bursts", "board", "coherence")
KINDS_DSP = (b'{"k":"transient"', b'{"k":"dsp"')


# ------------------------------------------------------------------ reading
class Skips:
    n = 0


def read_lines(path, skips):
    """Yield the lines of a file that may still be appended to. A line over MAX_LINE bytes is skipped and counted, never held in memory."""
    with open(path, "rb") as f:
        while True:
            line = f.readline(MAX_LINE + 1)
            if not line:
                return
            if len(line) > MAX_LINE and not line.endswith(b"\n"):
                skips.n += 1
                while True:
                    rest = f.readline(MAX_LINE)
                    if not rest or rest.endswith(b"\n"):
                        break
                continue
            yield line


def num(x):
    """float(x) for a finite JSON number (not bool), else None."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    try:
        v = float(x)
    except OverflowError:
        return None
    return v if math.isfinite(v) else None


def tnum(x):
    v = num(x)
    return v if v is not None and T_MIN <= v <= T_MAX else None


def iter_json(path, prefixes, skips):
    """Parsed objects of the lines that start with one of `prefixes` (bytes); an unparseable such line is counted in skips."""
    for line in read_lines(path, skips):
        if not line.startswith(prefixes):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            skips.n += 1
            continue
        if not isinstance(rec, dict):
            skips.n += 1
            continue
        yield rec


def load_dsp(paths, skips):
    """-> (transient dict of arrays sorted by t, array of distinct covered 1 s slots). `paths` that do not exist are ignored."""
    T, L, E, B, S, slots = [], [], [], [], [], set()
    for p in paths:
        if not os.path.exists(p):
            continue
        for r in iter_json(p, KINDS_DSP, skips):
            t = tnum(r.get("t"))
            if t is None:
                skips.n += 1
                continue
            if r.get("k") == "dsp":
                slots.add(int(t // 1))
                continue
            lv, ex, sp, bm = num(r.get("level_db")), num(r.get("excess_db")), num(r.get("beam_spread_db")), r.get("loudest_beam")
            if lv is None or ex is None or sp is None or isinstance(bm, bool) or not isinstance(bm, int) or not 1 <= bm <= 8:
                skips.n += 1
                continue
            T.append(t), L.append(lv), E.append(ex), B.append(bm), S.append(sp)
    o = np.argsort(np.asarray(T, np.float64), kind="stable")
    tr = {
        "t": np.asarray(T, np.float64)[o],
        "level": np.asarray(L, np.float64)[o],
        "excess": np.asarray(E, np.float64)[o],
        "beam": np.asarray(B, np.int64)[o],
        "spread": np.asarray(S, np.float64)[o],
    }
    return tr, np.asarray(sorted(slots), np.int64)


def load_spatial(path, skips):
    if not os.path.exists(path):
        return None
    t, s, d = [], [], []
    for r in iter_json(path, (b'{"k":"spatial"',), skips):
        tt, sp, di = tnum(r.get("t")), num(r.get("spread_db")), r.get("directional")
        if tt is None or sp is None or not isinstance(di, bool):
            skips.n += 1
            continue
        t.append(tt), s.append(sp), d.append(di)
    o = np.argsort(np.asarray(t, np.float64), kind="stable")
    return {"t": np.asarray(t, np.float64)[o], "spread": np.asarray(s, np.float64)[o], "directional": np.asarray(d, bool)[o]}


def load_ambient(paths, skips):
    """-> {hour_utc: temp_c} from ambient_temp.jsonl(.1), or None when neither file exists (the ambient-temp spec: t_hour_utc multiple of 3600, -90..60)."""
    if not any(os.path.exists(p) for p in paths):
        return None
    out = {}
    for p in paths:
        if not os.path.exists(p):
            continue
        for r in iter_json(p, (b'{"k":"ambient_temp"',), skips):
            h, tc = tnum(r.get("t_hour_utc")), num(r.get("temp_c"))
            if h is None or tc is None or h % 3600 != 0 or not -90.0 <= tc <= 60.0:
                skips.n += 1
                continue
            out[int(h)] = tc
    return out


def load_board(path, skips):
    """-> {hour_utc: {slots: {minute slot: temp}}} of board_sample records with a temperature, or None when the file is absent."""
    if not os.path.exists(path):
        return None
    out = {}
    for r in iter_json(path, (b'{"k":"board_sample"',), skips):
        t = tnum(r.get("t"))
        tc = r.get("temp_local_c")
        if t is None:
            skips.n += 1
            continue
        tc = num(tc)
        if tc is None:
            continue
        out.setdefault(int(t // 3600) * 3600, {})[int(t // 60)] = (t, tc)
    return out


def load_band816(path, skips):
    """-> (slot array, value array): per 1 s beam_bands record the mean over the 8 beams of the 8-16 kHz level (dB), or None when the file is absent."""
    if path is None or not os.path.exists(path):
        return None
    slot, val = [], []
    for r in iter_json(path, (b'{"k":"beam_bands"',), skips):
        t, zf, db = num(r.get("t_us")), num(r.get("zero_frac")), r.get("db")
        try:
            v = [num(row[7]) for row in db]
            ok = t is not None and zf is not None and zf < 1.0 and len(v) == 8 and None not in v
        except (TypeError, IndexError, KeyError):
            ok = False
        if not ok:
            skips.n += 1
            continue
        t = t / 1e6
        if not T_MIN <= t <= T_MAX:
            skips.n += 1
            continue
        slot.append(int(t // 1)), val.append(sum(v) / 8.0)
    o = np.argsort(np.asarray(slot, np.int64), kind="stable")
    return np.asarray(slot, np.int64)[o], np.asarray(val, np.float64)[o]


def load_coherence(path, skips):
    """-> list of (t0, t) of beam_coherence records, or None when the file is absent."""
    if not os.path.exists(path):
        return None
    out = []
    for r in iter_json(path, (b'{"k":"beam_coherence"',), skips):
        t0, t = tnum(r.get("t0")), tnum(r.get("t"))
        if t0 is None or t is None:
            skips.n += 1
            continue
        out.append((t0, t))
    return out


def load_bursts(path, skips):
    """-> {hour_utc: aggregate} of the optional w2-bursts `burst_stats` records (analysis/bursts.py), or None when the file is absent."""
    if path is None or not os.path.exists(path):
        return None
    acc = {}

    def pick(d, key):
        return num(d.get(key)) if isinstance(d, dict) else None

    for r in iter_json(path, (b'{"k":"burst_stats"',), skips):
        t0, an = tnum(r.get("t0")), num(r.get("analysed_s"))
        imp, gaps = r.get("impulses"), r.get("gaps")
        if t0 is None or an is None or not isinstance(imp, dict) or not isinstance(gaps, dict):
            skips.n += 1
            continue
        a = acc.setdefault(int(t0 // 3600) * 3600, {"an": 0.0, "n": 0.0, "coin": 0.0, "reg": [], "cv": [], "mode": [], "spr": [], "band": []})
        n = num(imp.get("n")) or 0.0
        a["an"] += an
        a["n"] += n
        a["coin"] += num(imp.get("coincident_all_n")) or 0.0
        gw = num(gaps.get("n")) or 0.0
        if num(gaps.get("regularity")) is not None and gw > 0:
            a["reg"].append((gw, num(gaps.get("regularity"))))
        for k, key in (("cv", "jitter_cv"), ("mode", "mode_s")):
            if num(gaps.get(key)) is not None:
                a[k].append(num(gaps.get(key)))
        sp = pick(imp.get("spread_db"), "p50")
        if sp is not None:
            a["spr"].append(sp)
        b = r.get("bands_db")
        try:
            p50 = [num(x) for x in b["p50"][2:10]]
            if len(p50) == 8 and None not in p50:
                a["band"].append(sum(p50) / 8.0)
        except (TypeError, KeyError):
            pass
    out = {}
    for h, a in acc.items():
        gw = sum(w for w, _ in a["reg"])
        med = lambda v: round(float(np.median(v)), 6) if v else None  # noqa: E731
        out[h] = {
            "analysed_s": round(a["an"], 1),
            "n_impulses": int(a["n"]),
            "regularity": round(sum(w * v for w, v in a["reg"]) / gw, 6) if gw > 0 else None,
            "jitter_cv": med(a["cv"]),
            "mode_s": med(a["mode"]),
            "spread_db_p50": med(a["spr"]),
            "coincident_share": round(a["coin"] / a["n"], 6) if a["n"] > 0 else None,
            "band_8_16k_db_p50": med(a["band"]),
        }
    return out


# ------------------------------------------------------------------ statistics
def rank_avg(x):
    x = np.asarray(x, np.float64)
    _, inv, cnt = np.unique(x, return_inverse=True, return_counts=True)
    cum = np.cumsum(cnt)
    return ((cum - cnt) + (cnt + 1) / 2.0)[inv.reshape(-1)]


def pearson(a, b):
    am, bm = a - a.mean(), b - b.mean()
    den = math.sqrt(float((am * am).sum()) * float((bm * bm).sum()))
    return float((am * bm).sum()) / den if den > 0 else float("nan")


def spearman(x, y, z=None):
    """Spearman rho; with covariates z (n x k, an intercept is added) the rank-based PARTIAL correlation: Pearson of the residuals of the ranks."""
    rx, ry = rank_avg(x), rank_avg(y)
    if z is not None:
        zz = np.column_stack([np.ones(len(rx)), z])
        rx = rx - zz @ np.linalg.lstsq(zz, rx, rcond=None)[0]
        ry = ry - zz @ np.linalg.lstsq(zz, ry, rcond=None)[0]
        if float(np.abs(rx).max()) < 1e-9 or float(np.abs(ry).max()) < 1e-9:
            return float("nan")
    return round(pearson(rx, ry), 10)


def spearman_block_boot(x, y, z=None, block=6, resamples=2000, seed=1, ci=0.95):
    """Spearman (partial when z is given) with a circular block bootstrap (hours are autocorrelated). -> dict rho, ci [lo, hi], p, n, resamples.
    p = two-sided sign p of the bootstrap distribution, 2 * (min(#<=0, #>=0) + 1) / (m + 1), capped at 1. rho None when the input is constant."""
    x, y = np.asarray(x, np.float64), np.asarray(y, np.float64)
    z = None if z is None else np.asarray(z, np.float64)
    n = len(x)
    rho = spearman(x, y, z)
    if not math.isfinite(rho):
        return {"rho": None, "ci": None, "p": None, "n": n, "resamples": 0}
    blk = max(1, min(int(block), n))
    nb = -(-n // blk)
    rng = np.random.default_rng(seed)
    off = np.arange(blk)
    vals = []
    for _ in range(int(resamples)):
        idx = ((rng.integers(0, n, nb)[:, None] + off) % n).ravel()[:n]
        v = spearman(x[idx], y[idx], None if z is None else z[idx])
        if math.isfinite(v):
            vals.append(v)
    if not vals:
        return {"rho": rho, "ci": None, "p": None, "n": n, "resamples": 0}
    v = np.asarray(vals)
    lo, hi = np.percentile(v, [50 * (1 - ci), 100 - 50 * (1 - ci)])
    p = min(1.0, 2.0 * (min(int((v <= 0).sum()), int((v >= 0).sum())) + 1) / (len(v) + 1))
    return {"rho": rho, "ci": [round(float(lo), 6), round(float(hi), 6)], "p": round(p, 6), "n": n, "resamples": len(v)}


def fisher_upper(a, b, c, d):
    """One-sided Fisher exact p, P(X >= a): a, b = burst hours cold / not cold; c, d = other hours cold / not cold (hypergeometric tail)."""
    N, K, n = a + b + c + d, a + c, a + b
    top = sum(math.comb(K, k) * math.comb(N - K, n - k) for k in range(a, min(n, K) + 1))
    return top / math.comb(N, n)


def holm(pvals, m):
    """Holm step-down adjusted p-values for the given p (dict name -> p) in a family of m pre-registered tests (the untested ones count as p = 1)."""
    order = sorted(pvals, key=lambda k: pvals[k])
    adj, run = {}, 0.0
    for i, k in enumerate(order):
        run = max(run, (m - i) * pvals[k])
        adj[k] = min(1.0, run)
    return adj


# ------------------------------------------------------------------ periodicity
def pair_hist(t, bin_s, hi_bin, max_lag):
    """Histogram of the lags (bins of bin_s, bins 0..hi_bin-1) of all event pairs i < j with 0 < t_j - t_i < max_lag, and the pair count."""
    h = np.zeros(hi_bin, np.int64)
    pairs, d, n = 0, 1, len(t)
    while d < n:
        diff = t[d:] - t[:-d]
        if float(diff.min()) >= max_lag:
            break
        m = diff < max_lag
        b = (diff[m] / bin_s + 1e-9).astype(np.int64)
        h += np.bincount(b, minlength=hi_bin)[:hi_bin]
        pairs += int(m.sum())
        d += 1
    return h, pairs


def count_pairs(t, max_lag):
    """Number of event pairs i < j with t_j - t_i < max_lag (t sorted) -- what pair_hist counts, without building a histogram."""
    t = np.asarray(t, np.float64)
    return int((np.searchsorted(t, t + max_lag, side="left") - np.arange(len(t)) - 1).sum())


def jitter_surrogates(t_rel, window, rng):
    """Interval jitter (Amarasingham et al.): every event moves uniformly inside its own `window` second interval; per-interval counts are preserved."""
    k = np.floor(t_rel / window)
    return np.sort((k + rng.random(len(t_rel))) * window)


def autocorrelogram(t, hour_utc, th, seed, n_surr, max_pairs):
    """Event-time autocorrelogram (lags min_lag..max_lag, bins of bin_s) against interval-jitter surrogates -> acorr dict.
    peak_z = z at the smallest-lag bin whose z is >= fundamental_fraction of the maximum (so the fundamental, not a harmonic), z = (C - mean_B) / max(sd_B, sd_floor)."""
    a = th["acorr"]
    base = {
        "bin_s": a["bin_s"],
        "max_lag_s": a["max_lag_s"],
        "jitter_window_s": a["jitter_window_s"],
        "n_surrogates": 0,
        "peak_lag_s": None,
        "peak_z": None,
        "peak_ratio": None,
    }
    if len(t) < a["min_events"]:
        return base
    rel = np.asarray(t, np.float64) - hour_utc
    bs = a["bin_s"]
    hi, lo = int(round(a["max_lag_s"] / bs)), int(round(a["min_lag_s"] / bs))
    pairs = count_pairs(rel, a["max_lag_s"])  # cheap, before the real histogram is built
    b = min(int(n_surr), int(max_pairs // max(pairs, 1)))
    if b < a["min_surrogates"]:
        return base
    real = pair_hist(rel, bs, hi, a["max_lag_s"])[0]
    rng = np.random.default_rng([int(seed), int(hour_utc)])
    sur = np.empty((b, hi - lo), np.float64)
    for i in range(b):
        sur[i] = pair_hist(jitter_surrogates(rel, a["jitter_window_s"], rng), bs, hi, a["max_lag_s"])[0][lo:]
    mean, sd = sur.mean(axis=0), sur.std(axis=0, ddof=1)
    z = (real[lo:] - mean) / np.maximum(sd, a["sd_floor"])
    zmax = float(z.max())
    cand = np.nonzero(z >= th["periodic"]["fundamental_fraction"] * zmax)[0] if zmax > 0 else np.asarray([int(np.argmax(z))])
    j = int(cand[0])
    base.update(
        {
            "n_surrogates": b,
            "peak_lag_s": round((lo + j + 0.5) * bs, 6),
            "peak_z": round(float(z[j]), 4),
            "peak_ratio": round(float(real[lo + j] / mean[j]), 4) if mean[j] > 0 else None,
        }
    )
    return base


# ------------------------------------------------------------------ per-hour features (raw, threshold independent)
def _med(v):
    return round(float(np.median(v)), 6) if len(v) else None


def ioi_features(t, th, covered_mask, h0):
    d = np.diff(t)
    tm = th["ioi"]["trim_max_gap_s"]
    dt = d[d <= tm]
    out = {
        "n": int(len(dt)),
        "median_s": None,
        "p10_s": None,
        "p90_s": None,
        "cv_trim": None,
        "serial_corr_lag1": None,
        "fano_1s": None,
        "fano_10s": None,
        "trim_max_gap_s": tm,
        "min_observable_s": th["ioi"]["min_observable_s"],
    }
    if len(dt):
        out["median_s"], out["p10_s"], out["p90_s"] = _med(dt), round(float(np.percentile(dt, 10)), 6), round(float(np.percentile(dt, 90)), 6)
        m = float(dt.mean())
        out["cv_trim"] = round(float(dt.std()) / m, 6) if m > 0 else None
    if len(dt) >= 3:
        c = pearson(dt[:-1], dt[1:])
        out["serial_corr_lag1"] = round(c, 6) if math.isfinite(c) else None
    cnt = np.bincount(np.floor(t - h0).astype(np.int64).clip(0, 3599), minlength=3600)
    for nsec, key in ((1, "fano_1s"), (10, "fano_10s")):
        k = 3600 // nsec
        c = cnt[: k * nsec].reshape(k, nsec).sum(axis=1)
        ok = covered_mask[: k * nsec].reshape(k, nsec).all(axis=1)
        c = c[ok]
        if len(c) >= 2 and c.mean() > 0:
            out[key] = round(float(c.var() / c.mean()), 6)
    return out


def hour_features(h0, tr, sl, spatial, band, board, coh, bursts, th, seed, n_surr, max_pairs):
    """The raw feature record of one UTC hour (everything that does not depend on the pre-registered selection thresholds)."""
    t = tr["t"][sl]
    slots = tr["_slots"].get(h0, np.zeros(0, np.int64))
    mask = np.zeros(3600, bool)
    mask[(slots - h0).clip(0, 3599)] = True
    n = int(len(t))
    cnt = np.bincount(tr["beam"][sl] - 1, minlength=8) if n else np.zeros(8, np.int64)
    p = cnt / cnt.sum() if n else None
    ent = round(float(-(p[p > 0] * np.log(p[p > 0])).sum() / math.log(8)), 6) if n else None
    rec = {
        "k": "burst_hour",
        "v": 1,
        "hour_utc": int(h0),
        "covered_s": int(mask.sum()),
        "n_transients": n,
        "ioi": ioi_features(t, th, mask, h0) if n else ioi_features(np.zeros(0), th, mask, h0),
        "acorr": autocorrelogram(t, h0, th, seed, n_surr, max_pairs),
        "level_db": {
            "median": _med(tr["level"][sl]),
            "iqr": round(float(np.percentile(tr["level"][sl], 75) - np.percentile(tr["level"][sl], 25)), 6) if n else None,
        },
        "excess_db_median": _med(tr["excess"][sl]),
        "beam": {
            "counts": [int(c) for c in cnt],
            "top_share": round(float(cnt.max() / cnt.sum()), 6) if n else None,
            "entropy_norm": ent,
            "spread_db_median": _med(tr["spread"][sl]),
        },
    }
    ts = {int(x) for x in np.floor(t)}
    rec["spatial"] = None
    if spatial is not None:
        i0, i1 = np.searchsorted(spatial["t"], [h0, h0 + 3600])
        st, sp, di = spatial["t"][i0:i1], spatial["spread"][i0:i1], spatial["directional"][i0:i1]
        inb = np.fromiter((int(x) in ts for x in np.floor(st)), bool, len(st))
        rec["spatial"] = {
            "n_windows": int(len(st)),
            "spread_burst_db": _med(sp[inb]),
            "spread_quiet_db": _med(sp[~inb]),
            "directional_frac": round(float(di.mean()), 6) if len(st) else None,
        }
    rec["band_8_16k"] = None
    if band is not None:
        i0, i1 = np.searchsorted(band[0], [h0, h0 + 3600])
        bs_, bv = band[0][i0:i1], band[1][i0:i1]
        if len(bs_):
            inb = np.fromiter((int(x) in ts for x in bs_), bool, len(bs_))
            rec["band_8_16k"] = {"burst_db": _med(bv[inb]), "quiet_db": _med(bv[~inb]), "n_slots": int(len(bs_))}
    rec["bursts"] = None if bursts is None or h0 not in bursts else bursts[h0]
    rec["board"] = None
    if board is not None and h0 in board:
        pts = sorted(board[h0].values())
        tt, tc = np.asarray([x[0] for x in pts]), np.asarray([x[1] for x in pts])
        cov = len(pts) / 60.0
        slope = None
        if cov >= th["t8"]["min_coverage_frac"] and len(pts) >= 2 and float(tt.std()) > 0:
            slope = round(float(np.polyfit(tt - tt.mean(), tc, 1)[0]) * 3600.0, 6)
        rec["board"] = {"temp_local_c_median": _med(tc), "dtdt_c_per_h": slope, "coverage_frac": round(min(cov, 1.0), 6)}
    rec["coherence"] = None
    if coh is not None:
        rec["coherence"] = {"n_records": sum(1 for a, b in coh if a < h0 + 3600 and b > h0)}
    return rec


def night_id(hour, th):
    return int((hour - th["night"]["boundary_utc_hour"] * 3600) // 86400)


def evidence_class(hour_start_utc, th):
    return "confirmatory" if hour_start_utc >= th["frozen_utc"] else "exploratory"


def ambient_for(h, amb, th):
    """The reader rule of specs/ambient-temp.md plus dT/dt from the hour records either side. -> ambient dict."""
    out = {"temp_c": None, "age_h": None, "dtdt_c_per_h": None}
    if amb is None:
        return out
    for k in range(th["ambient"]["max_age_h"] + 1):
        if h - 3600 * k in amb:
            out["temp_c"], out["age_h"] = amb[h - 3600 * k], k
            break
    if h + 3600 in amb and h - 3600 in amb:
        out["dtdt_c_per_h"] = round((amb[h + 3600] - amb[h - 3600]) / 2.0, 6)
    return out


def derive(rec, th, amb, thresholds_version, source):
    """Everything that depends on the thresholds, recomputed on every load (a digest row never carries a stale verdict input)."""
    cov, n = rec["covered_s"], rec["n_transients"]
    rate = n / cov if cov > 0 else None
    low = cov < th["burst"]["min_covered_s"]
    rec["rate_per_covered_s"] = None if rate is None else round(rate, 6)
    rec["excluded_reason"] = "low_coverage" if low else None
    rec["is_burst"] = bool((not low) and rate is not None and rate >= th["burst"]["min_rate_per_covered_s"])
    rec["detector_saturation_flag"] = bool(rate is not None and rate >= th["burst"]["saturation_rate_per_covered_s"])
    rec["source"] = source
    b = rec.get("bursts")
    rec["ioi_source"] = "bursts" if b and b.get("analysed_s", 0) >= th["bursts_input"]["min_analysed_s"] else "transient"
    rec["evidence_class"] = evidence_class(rec["hour_utc"], th)
    rec["thresholds_version"] = thresholds_version
    rec["features_version"] = th["features_version"]
    rec["ambient"] = ambient_for(rec["hour_utc"], amb, th)
    return rec


def order_hour(rec):
    keys = [
        "k",
        "v",
        "hour_utc",
        "covered_s",
        "n_transients",
        "rate_per_covered_s",
        "is_burst",
        "excluded_reason",
        "detector_saturation_flag",
        "source",
        "ioi_source",
        "evidence_class",
        "thresholds_version",
        "features_version",
        "ioi",
        "acorr",
        "level_db",
        "excess_db_median",
        "beam",
        "spatial",
        "band_8_16k",
        "bursts",
        "ambient",
        "board",
        "coherence",
    ]
    return {k: rec[k] for k in keys}


# ------------------------------------------------------------------ tests T1..T9
def _t(status_ok, value, n, reason=None, **numbers):
    return {"value": value, "n": n, "status": "ok" if status_ok else "not_measurable", "reason": reason, "numbers": numbers}


def _nm(n, reason):
    return _t(False, None, n, reason)


def regime(temp, th):
    if temp is None:
        return None
    return "cold" if temp < th["ambient"]["cold_c"] else ("warm" if temp >= th["ambient"]["warm_c"] else "mid")


def evaluate_tests(hours, amb, th):
    """The pre-registered tests T1..T9 over a list of finalized hour records. `amb` is {hour: temp} (exact records, for dT/dt) or None."""
    cov = [h for h in hours if h["excluded_reason"] is None]
    burst = [h for h in cov if h["is_burst"]]
    with_amb = [h for h in cov if h["ambient"]["temp_c"] is not None]
    tests = {}
    # T1 cold share of the burst hours against the base rate of all covered hours with an ambient value
    bamb = [h for h in burst if h["ambient"]["temp_c"] is not None]
    t1 = th["t1"]
    if len(bamb) < t1["min_burst_hours_with_ambient"]:
        tests["T1"] = _nm(len(bamb), f"{len(bamb)} burst hours with ambient < {t1['min_burst_hours_with_ambient']}")
    else:
        cold = lambda hs, c: sum(1 for h in hs if h["ambient"]["temp_c"] < c)  # noqa: E731
        nb, ca = len(bamb), cold(bamb, th["ambient"]["cold_c"])
        other = [h for h in with_amb if not h["is_burst"]]
        co = cold(other, th["ambient"]["cold_c"])
        base = cold(with_amb, th["ambient"]["cold_c"]) / len(with_amb)
        lift = (ca / nb) / base if base > 0 else None
        sens = {f"cold_share_{c:g}c": round(cold(bamb, c) / nb, 6) for c in th["ambient"]["sensitivity_c"]}
        warm = [h["hour_utc"] for h in bamb if regime(h["ambient"]["temp_c"], th) == "warm"]
        tests["T1"] = _t(
            True,
            round(ca / nb, 6),
            nb,
            None,
            base_cold_share=round(base, 6),
            lift=None if lift is None else round(lift, 6),
            fisher_p=round(fisher_upper(ca, nb - ca, co, len(other) - co), 10),
            n_covered_with_ambient=len(with_amb),
            n_cold_burst=ca,
            warm_burst_hours=warm,
            **sens,
        )
    # T2 rate versus temperature (raw and partial on time of day)
    t2 = th["t2"]
    if len(with_amb) < t2["min_hours"]:
        tests["T2"] = _nm(len(with_amb), f"{len(with_amb)} covered hours with ambient < {t2['min_hours']}")
    else:
        o = sorted(with_amb, key=lambda h: h["hour_utc"])
        x = np.asarray([h["rate_per_covered_s"] for h in o])
        y = np.asarray([h["ambient"]["temp_c"] for h in o])
        tod = np.asarray([(h["hour_utc"] % 86400) / 3600.0 for h in o])
        z = np.column_stack([np.sin(2 * np.pi * tod / 24), np.cos(2 * np.pi * tod / 24)])
        kw = dict(block=t2["block_h"], resamples=t2["resamples"], seed=t2["seed"], ci=t2["ci"])
        raw, par = spearman_block_boot(x, y, None, **kw), spearman_block_boot(x, y, z, **kw)
        nights = len({night_id(h["hour_utc"], th) for h in o})
        if par["rho"] is None or par["ci"] is None:
            tests["T2"] = _nm(len(o), "constant rate or temperature, rho undefined")
        else:
            tests["T2"] = _t(
                True,
                par["rho"],
                len(o),
                None,
                rho_raw=raw["rho"],
                ci_raw=raw["ci"],
                ci=par["ci"],
                p=par["p"],
                effective_nights=nights,
                block_h=t2["block_h"],
                resamples=par["resamples"],
                adjusted_for="hour_of_day",
            )
    # T3 rate versus temperature change (6 tests, Holm)
    t3 = th["t3"]
    res, ns = {}, {}
    for sign in ("cooling", "warming", "abs"):
        for lag in (0, 1):
            rows = []
            for h in sorted(cov, key=lambda r: r["hour_utc"]):
                hh = h["hour_utc"] - 3600 * lag
                if amb is not None and hh in amb and hh + 3600 in amb and hh - 3600 in amb:
                    d = (amb[hh + 3600] - amb[hh - 3600]) / 2.0
                    rows.append((h["rate_per_covered_s"], {"cooling": -d, "warming": d, "abs": abs(d)}[sign]))
            name = f"{sign}_lag{lag}"
            ns[name] = len(rows)
            if len(rows) >= t3["min_hours"]:
                r = spearman_block_boot(
                    [a for a, _ in rows], [b for _, b in rows], None, block=t2["block_h"], resamples=t2["resamples"], seed=t2["seed"] + 1, ci=t2["ci"]
                )
                if r["rho"] is not None and r["ci"] is not None:
                    res[name] = r
    if not res:
        tests["T3"] = _nm(max(ns.values()) if ns else 0, f"largest dT/dt sample {max(ns.values()) if ns else 0} hours < {t3['min_hours']}")
    else:
        adj = holm({k: v["p"] for k, v in res.items()}, t3["n_tests_family"])
        best = min(res, key=lambda k: (adj[k], -abs(res[k]["rho"])))
        tests["T3"] = _t(
            True,
            res[best]["rho"],
            res[best]["n"],
            None,
            best=best,
            ci=res[best]["ci"],
            holm_p=round(adj[best], 6),
            n_run=len(res),
            per_test={k: {"rho": v["rho"], "ci": v["ci"], "holm_p": round(adj[k], 6)} for k, v in res.items()},
        )
    # T4 periodicity
    p = th["periodic"]
    elig = [h for h in burst if h["acorr"]["peak_z"] is not None]
    if not elig:
        tests["T4"] = _nm(0, "no burst hour with a periodicity test (fewer than %d events or too many pairs)" % th["acorr"]["min_events"])
    else:
        per = [h for h in elig if h["acorr"]["peak_z"] >= p["peak_z"] and (h["acorr"]["peak_ratio"] or 0) >= p["peak_ratio"]]
        lags = [h["acorr"]["peak_lag_s"] for h in per]
        cons = None
        if len(per) >= p["min_periodic_hours"]:
            med = float(np.median(lags))
            cons = round(sum(1 for l in lags if abs(l - med) <= p["lag_tolerance"] * med) / len(per), 6)
        tests["T4"] = _t(
            True,
            round(len(per) / len(elig), 6),
            len(elig),
            None,
            n_periodic=len(per),
            consistent_lag=cons,
            median_peak_lag_s=round(float(np.median(lags)), 6) if lags else None,
            n_nonperiodic=sum(1 for h in elig if h["acorr"]["peak_z"] < p["peak_z"]),
        )
    # T5 regularity
    rg = th["regularity"]
    reg = clu = nm = 0
    for h in burst:
        if h["ioi_source"] == "bursts":
            r = h["bursts"]["regularity"]
            if r is None:
                continue
            nm += 1
            reg += r >= rg["regular_burst_regularity"]
            clu += r <= rg["clustered_burst_regularity"]
        elif h["ioi"]["cv_trim"] is not None:
            nm += 1
            reg += h["ioi"]["cv_trim"] <= rg["regular_cv"]
            clu += h["ioi"]["cv_trim"] >= rg["clustered_cv"]
    tests["T5"] = (
        _nm(0, "no burst hour with an interval statistic")
        if nm == 0
        else _t(True, round(reg / nm, 6), nm, None, regular_n=int(reg), clustered_n=int(clu), clustered_share=round(clu / nm, 6))
    )

    # T6 stationarity, T7 diffuseness (descriptive)
    def spread(h):
        return h["bursts"]["spread_db_p50"] if h["ioi_source"] == "bursts" and h["bursts"]["spread_db_p50"] is not None else h["beam"]["spread_db_median"]

    st = th["stationarity"]
    if len(burst) < st["min_burst_hours"]:
        tests["T6"] = _nm(len(burst), f"{len(burst)} burst hours < {st['min_burst_hours']}")
    else:
        lv = [h["level_db"]["median"] for h in burst]
        sp = [spread(h) for h in burst]
        iqr = float(np.percentile(lv, 75) - np.percentile(lv, 25))
        rng_ = max(sp) - min(sp)
        ok = iqr <= st["max_level_iqr_db"] and rng_ <= st["max_spread_range_db"]
        tests["T6"] = _t(True, bool(ok), len(burst), None, level_iqr_db=round(iqr, 6), spread_range_db=round(rng_, 6))
    df = th["diffuse"]
    if not burst:
        tests["T7"] = _nm(0, "no burst hours")
    else:
        dif = sum(
            1
            for h in burst
            if spread(h) is not None
            and h["beam"]["entropy_norm"] is not None
            and spread(h) <= df["max_spread_db"]
            and h["beam"]["entropy_norm"] >= df["min_entropy_norm"]
        )
        tests["T7"] = _t(True, round(dif / len(burst), 6), len(burst), None, n_diffuse=dif)
    # T8 board thermal
    t8 = th["t8"]
    bb = [h for h in burst if h["board"] and h["board"]["coverage_frac"] >= t8["min_coverage_frac"] and h["board"]["dtdt_c_per_h"] is not None]
    if len(bb) < t8["min_burst_hours"]:
        tests["T8"] = _nm(len(bb), f"{len(bb)} burst hours with board temperature coverage >= {t8['min_coverage_frac']} < {t8['min_burst_hours']}")
    else:
        r = spearman_block_boot(
            [h["rate_per_covered_s"] for h in bb],
            [-h["board"]["dtdt_c_per_h"] for h in bb],
            None,
            block=1,
            resamples=t2["resamples"],
            seed=t2["seed"] + 2,
            ci=t2["ci"],
        )
        tests["T8"] = _nm(len(bb), "constant input") if r["rho"] is None or r["ci"] is None else _t(True, r["rho"], r["n"], None, ci=r["ci"], p=r["p"])
    # T9 coherence (no decision rule)
    t9 = th["t9"]
    cc = [h for h in burst if h["coherence"] and h["coherence"]["n_records"] >= t9["min_records"]]
    tests["T9"] = (
        _nm(len(cc), f"{len(cc)} burst hours with >= {t9['min_records']} coherence records < {t9['min_burst_hours']}")
        if len(cc) < t9["min_burst_hours"]
        else _t(True, len(cc), len(cc), None)
    )
    return tests


# ------------------------------------------------------------------ verdicts
def _v(verdict, basis, rule, nb, evid, numbers=None, residual=False):
    return {"verdict": verdict, "basis": basis, "rule": rule, "evidence_class": evid, "n_burst_hours": nb, "numbers": numbers or {}, "residual": residual}


def verdicts(tests, hours, th, pinned, n_conf_burst):
    """One verdict per hypothesis, first matching rule wins; a rule whose test cannot run gives not-measurable, never a pass."""
    burst = [h for h in hours if h["excluded_reason"] is None and h["is_burst"]]
    nb = len(burst)
    nights = len({night_id(h["hour_utc"], th) for h in burst})
    evid = "unpinned" if not pinned else ("confirmatory" if n_conf_burst >= th["min_burst_hours"] else "exploratory")
    eff = th["effect_size"]
    T1, T2, T3, T4, T5, T6, T8 = (tests[k] for k in ("T1", "T2", "T3", "T4", "T5", "T6", "T8"))
    ok = lambda t: t["status"] == "ok"  # noqa: E731
    few = f"{nb} burst hours (< {th['min_burst_hours']})" if nb < th["min_burst_hours"] else None
    nig = f"burst hours span {nights} distinct nights (< {th['night']['min_nights']})" if nights < th["night"]["min_nights"] else None
    out = {}
    # insect
    r = "supported: T2 partial CI lower > 0.30 AND T1 share <= 0.50; refuted: N_b>=5 AND T1 share >= 0.80 AND lift >= 1.25 AND Fisher p < 0.05 AND T2 partial CI upper < 0.30; needs >= 3 nights"
    nums = {"T1": T1["value"], "T2_rho_partial": T2["value"], "nights": nights}
    if not (ok(T1) and ok(T2)):
        out["insect"] = _v("not-measurable", "T1 or T2 cannot run: " + "; ".join(t["reason"] for t in (T1, T2) if not ok(t)), r, nb, evid, nums)
    elif few or nig:
        out["insect"] = _v("inconclusive", "decisive verdicts refused: " + (few or nig), r, nb, evid, nums)
    else:
        n1, n2 = T1["numbers"], T2["numbers"]
        nums.update({"base_cold_share": n1["base_cold_share"], "lift": n1["lift"], "fisher_p": n1["fisher_p"], "ci": n2["ci"]})
        if n2["ci"][0] > eff and T1["value"] <= th["insect"]["supported_max_cold_share"]:
            out["insect"] = _v("supported", "rate rises with temperature (time-of-day adjusted) and bursts are not cold-concentrated", r, nb, evid, nums)
        elif (
            T1["value"] >= th["insect"]["refuted_min_cold_share"]
            and n1["lift"] is not None
            and n1["lift"] >= th["t1"]["lift_min"]
            and n1["fisher_p"] < th["t1"]["fisher_alpha"]
            and n2["ci"][1] < eff
        ):
            out["insect"] = _v("refuted", "bursts concentrate in the cold beyond the base rate and rate does not rise with temperature", r, nb, evid, nums)
        else:
            out["insect"] = _v("inconclusive", "neither rule fired", r, nb, evid, nums)
    # pulser
    pl = th["pulser"]
    r = (
        "supported (clocked periodic source, internal OR external; cannot exclude unit electronics): eligible N>=5 AND >=80% periodic AND consistent lag >= 0.80 AND regular share >= 0.5; "
        "refuted: eligible N>=5 AND >=80% non-periodic AND clustered share >= 0.5; needs >= 3 nights"
    )
    if not ok(T4):
        out["pulser"] = _v("not-measurable", "T4 cannot run: " + T4["reason"], r, nb, evid, {})
    else:
        n4 = T4["numbers"]
        nums = {
            "n_eligible": T4["n"],
            "periodic_share": T4["value"],
            "consistent_lag": n4["consistent_lag"],
            "nights": nights,
            "regular_share": T5["value"],
            "clustered_share": T5["numbers"].get("clustered_share") if ok(T5) else None,
        }
        if T4["n"] < pl["min_eligible_hours"]:
            out["pulser"] = _v("inconclusive", f"{T4['n']} burst hours with a periodicity test (< {pl['min_eligible_hours']})", r, nb, evid, nums)
        elif nig:
            out["pulser"] = _v("inconclusive", "decisive verdicts refused: " + nig, r, nb, evid, nums)
        elif (
            T4["value"] >= pl["periodic_share"]
            and n4["consistent_lag"] is not None
            and n4["consistent_lag"] >= pl["consistent_lag"]
            and ok(T5)
            and T5["value"] >= pl["regular_share"]
        ):
            out["pulser"] = _v("supported", "periodic at a consistent lag and regular; electronics with a clock would look the same", r, nb, evid, nums)
        elif ok(T5) and n4["n_nonperiodic"] / T4["n"] >= pl["nonperiodic_share"] and nums["clustered_share"] >= pl["clustered_share"]:
            out["pulser"] = _v("refuted", "no fixed-lag periodicity and clustered intervals", r, nb, evid, nums)
        else:
            out["pulser"] = _v("inconclusive", "neither rule fired", r, nb, evid, nums)
    # thermal
    r = "supported: T8 runs AND rho CI lower > 0.30 AND p < 0.05; refuted: T8 CI upper < 0.30, or (T8 cannot run) basis ambient_proxy_only when T3 best CI upper < 0.30 AND >= 60% of burst hours |dT/dt| < 0.5; ambient alone never supports; needs >= 3 nights"
    if not ok(T8) and not ok(T3):
        out["thermal"] = _v("not-measurable", "T3 and T8 cannot run: " + T3["reason"] + "; " + T8["reason"], r, nb, evid, {})
    elif few or nig:
        out["thermal"] = _v("inconclusive", "decisive verdicts refused: " + (few or nig), r, nb, evid, {"nights": nights})
    elif ok(T8):
        n8 = T8["numbers"]
        nums = {"rho": T8["value"], "ci": n8["ci"], "p": n8["p"], "nights": nights}
        if n8["ci"][0] > eff and n8["p"] < th["t3"]["alpha"]:
            out["thermal"] = _v("supported", "burst rate follows the board cooling rate", r, nb, evid, nums)
        elif n8["ci"][1] < eff:
            out["thermal"] = _v("refuted", "burst rate does not follow the board cooling rate", r, nb, evid, nums)
        else:
            out["thermal"] = _v("inconclusive", "neither rule fired", r, nb, evid, nums)
    else:
        ci = T3["numbers"]["ci"]
        calm = sum(1 for h in burst if h["ambient"]["dtdt_c_per_h"] is not None and abs(h["ambient"]["dtdt_c_per_h"]) < th["thermal"]["proxy_abs_dtdt"])
        share = calm / nb if nb else 0.0
        nums = {"T3_best_ci": ci, "calm_share": round(share, 6), "nights": nights}
        if ci[1] < eff and share >= th["thermal"]["proxy_share"]:
            out["thermal"] = _v("refuted", "ambient_proxy_only: the ambient temperature change does not explain the rate", r, nb, evid, nums)
        else:
            out["thermal"] = _v("inconclusive", "ambient proxy cannot decide; supported needs board temperature", r, nb, evid, nums)
    # electronics
    r = "never supported from these logs; refuted: T2 partial CI lower > 0.30 OR T3 best CI lower > 0.30 (it predicts independence from outdoor conditions); residual when not refuted and T6 passes; needs >= 3 nights"
    if not ok(T2) and not ok(T3):
        out["electronics"] = _v("not-measurable", "T2 and T3 cannot run, its refutation test is unavailable", r, nb, evid, {})
    elif few or nig:
        out["electronics"] = _v("inconclusive", "decisive verdicts refused: " + (few or nig), r, nb, evid, {"nights": nights})
    else:
        c2 = T2["numbers"]["ci"] if ok(T2) else None
        c3 = T3["numbers"]["ci"] if ok(T3) else None
        nums = {"T2_ci": c2, "T3_best_ci": c3, "nights": nights}
        if (c2 and c2[0] > eff) or (c3 and c3[0] > eff):
            out["electronics"] = _v("refuted", "burst rate depends on outdoor temperature or its change", r, nb, evid, nums)
        else:
            res = bool(ok(T6) and T6["value"])
            out["electronics"] = _v("inconclusive", "positive discriminators (board load, regulator state, bench probe) are absent", r, nb, evid, nums, res)
    return out


# ------------------------------------------------------------------ assembly
def parse_iso(s):
    s = s.strip().rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H", "%Y-%m-%d"):
        try:
            return calendar.timegm(time.strptime(s, fmt))
        except ValueError:
            pass
    raise ValueError("bad ISO time")


def load_digest(path, th, skips):
    out = {}
    if path is None or not os.path.exists(path):
        return out
    for r in iter_json(path, (b'{"k":"burst_hour"',), skips):
        h, cov = r.get("hour_utc"), r.get("covered_s")
        if (
            r.get("v") != 1
            or r.get("features_version") != th["features_version"]
            or isinstance(h, bool)
            or not isinstance(h, int)
            or h % 3600
            or isinstance(cov, bool)
            or not isinstance(cov, int)
            or not isinstance(r.get("n_transients"), int)
            or not all(k in r for k in RAW_KEYS)
            or not all(isinstance(r[k], dict) for k in ("ioi", "acorr", "level_db", "beam"))
        ):
            skips.n += 1
            continue
        out[h] = r
    return out


def build_hours(raw, digest, amb, th, thresholds_version):
    """Merge raw hours with the digest (a digest hour wins only when its covered_s is LARGER: the raw lines have partly rotated out) and derive."""
    hours = {}
    for h, r in raw.items():
        src = "raw"
        if h in digest and digest[h]["covered_s"] > r["covered_s"]:
            r, src = digest[h], "digest"
        hours[h] = derive(r, th, amb, thresholds_version, src)
    for h, r in digest.items():
        if h not in hours:
            hours[h] = derive(r, th, amb, thresholds_version, "digest")
    return [order_hour(hours[h]) for h in sorted(hours)]


def make_scorecard(hours, amb, th, meta):
    """-> scorecard dict from the finalized hour records (all hours, already filtered by --since/--until)."""
    pinned = meta["thresholds_pinned"]
    cov = [h for h in hours if h["excluded_reason"] is None]
    burst = [h for h in cov if h["is_burst"]]
    conf = [h for h in hours if h["evidence_class"] == "confirmatory"]
    n_conf_burst = sum(1 for h in conf if h["excluded_reason"] is None and h["is_burst"])
    tests = evaluate_tests(hours, amb, th)
    ver = verdicts(tests, hours, th, pinned, n_conf_burst)
    ctests = evaluate_tests(conf, amb, th)
    cver = verdicts(ctests, conf, th, pinned, n_conf_burst)
    decisive = sum(1 for k in HYPOTHESES if ver[k]["verdict"] in ("supported", "refuted") and ver[k]["n_burst_hours"] >= th["min_burst_hours"])
    caveats = [
        "The transient records are a censored, detector-dependent proxy for the bursts (20 ms frames, total energy, gaps under 0.04 s unobservable), not the bursts themselves.",
        "A supported verdict concerns the logged proxy only; it does not identify the source.",
        "pulser supported means a clocked periodic source, internal or external; unit electronics with a clock are not excluded by it.",
        "Ambient temperature is a model value for a cell of about 11 km, not a measurement at the unit.",
    ]
    if any(h["detector_saturation_flag"] for h in burst):
        caveats.append(
            "At least one burst hour has a rate of 5 per second or more: the detector self-suppresses there and rate and interval statistics are biased."
        )
    if not meta["beam_bands_present"]:
        caveats.append("8-16 kHz attribution not confirmed in this run (no beam_bands input).")
    elif any(h["band_8_16k"] is None for h in burst):
        caveats.append("8-16 kHz attribution not confirmed for every burst hour (beam_bands starts later than some of them).")
    if not pinned:
        caveats.append("The thresholds file differs from the pre-registered one: every verdict is unpinned.")
    nights = len({night_id(h["hour_utc"], th) for h in burst})
    if nights < th["night"]["min_nights"]:
        caveats.append(f"The burst hours span {nights} distinct nights: supported and refuted verdicts are refused below {th['night']['min_nights']}.")
    if any(h["source"] == "digest" for h in hours):
        caveats.append("Some hours come from the digest (their raw lines have rotated out).")
    if any(h["evidence_class"] == "exploratory" for h in burst):
        caveats.append("Hours before frozen_utc are exploratory: the rules were set after the author had seen them (a post-hoc fit).")
    return {
        "k": "burst_scorecard",
        "v": 1,
        "generated_utc": meta["now"],
        "tool_version": TOOL_VERSION,
        "thresholds_version": th["thresholds_version"],
        "thresholds_sha256": meta["thresholds_sha256"],
        "thresholds_pinned": pinned,
        "frozen_utc": th["frozen_utc"],
        "data_span_utc": [hours[0]["hour_utc"], hours[-1]["hour_utc"] + 3600] if hours else [0, 0],
        "inputs": meta["inputs"],
        "n_hours_covered": len(cov),
        "n_burst_hours": len(burst),
        "burst_hours_utc": [h["hour_utc"] for h in burst],
        "n_nights_burst": nights,
        "tests": tests,
        "verdicts": ver,
        "confirmatory": {"n_hours_covered": sum(1 for h in conf if h["excluded_reason"] is None), "n_burst_hours": n_conf_burst, "verdicts": cver},
        "decisive_count": decisive,
        "surviving": [k for k in HYPOTHESES if ver[k]["verdict"] != "refuted"],
        "caveats": caveats,
    }


def _fmt(v, nd=3):
    return "-" if v is None else (f"{v:.{nd}f}" if isinstance(v, float) else str(v))


def render_md(sc, hours, th):
    L = [
        "# Burst-source scorecard",
        "",
        f"tool {sc['tool_version']}, thresholds v{sc['thresholds_version']} (pinned: {str(sc['thresholds_pinned']).lower()}), hours covered {sc['n_hours_covered']}, "
        f"burst hours {sc['n_burst_hours']} over {sc['n_nights_burst']} nights, decisive verdicts {sc['decisive_count']}.",
        "",
        "## Verdicts",
        "",
        "| hypothesis | verdict | evidence | basis |",
        "| --- | --- | --- | --- |",
    ]
    for k in HYPOTHESES:
        v = sc["verdicts"][k]
        L.append(f"| {k} | {v['verdict']}{' (residual)' if v['residual'] else ''} | {v['evidence_class']} | {v['basis']} |")
    L += [
        "",
        "surviving (not refuted): " + ", ".join(sc["surviving"]) if sc["surviving"] else "surviving (not refuted): none",
        "",
        "## Tests",
        "",
        "| test | status | value | n | reason |",
        "| --- | --- | --- | --- | --- |",
    ]
    for k, t in sc["tests"].items():
        L.append(f"| {k} | {t['status']} | {_fmt(t['value'])} | {t['n']} | {t['reason'] or ''} |")
    L += [
        "",
        "## Burst hours",
        "",
        "| hour (UTC) | regime | rate/s | cv | peak z | peak lag s | temp C | dT/dt C/h | spread dB | source | evidence |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for h in hours:
        if h["excluded_reason"] is None and h["is_burst"]:
            a = h["acorr"]
            L.append(
                f"| {time.strftime('%Y-%m-%dT%H', time.gmtime(h['hour_utc']))} | {regime(h['ambient']['temp_c'], th) or '-'} | {_fmt(h['rate_per_covered_s'])} | "
                f"{_fmt(h['ioi']['cv_trim'])} | {_fmt(a['peak_z'], 1)} | {_fmt(a['peak_lag_s'])} | {_fmt(h['ambient']['temp_c'], 1)} | "
                f"{_fmt(h['ambient']['dtdt_c_per_h'], 2)} | {_fmt(h['beam']['spread_db_median'], 1)} | {h['source']}/{h['ioi_source']} | {h['evidence_class']} |"
            )
    L += ["", "## Caveats", ""] + ["- " + c for c in sc["caveats"]]
    return "\n".join(L) + "\n"


def write_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def jdump(o):
    return json.dumps(o, separators=(",", ":"), allow_nan=False)


def write_report(out, sc, hours, th, write_digest, digest_hours):
    os.makedirs(out, exist_ok=True)
    write_atomic(os.path.join(out, "burst_scorecard.json"), (jdump(sc) + "\n").encode("utf-8"))
    write_atomic(os.path.join(out, "burst_scorecard.md"), render_md(sc, hours, th).encode("utf-8"))
    if write_digest:
        write_atomic(os.path.join(out, "burst_hours.jsonl"), "".join(jdump(h) + "\n" for h in digest_hours).encode("utf-8"))


def missing_keys(ref, got, prefix=""):
    """Dotted paths of the keys present in the reference thresholds structure but absent from got (recursing into objects)."""
    if not isinstance(got, dict):
        return [prefix.rstrip(".") or "<root>"]
    out = []
    for k, v in ref.items():
        if k not in got:
            out.append(prefix + k)
        elif isinstance(v, dict):
            out += missing_keys(v, got[k], prefix + k + ".")
    return out


def inside(path, parent):
    p, q = os.path.realpath(path), os.path.realpath(parent)
    return p == q or p.startswith(q + os.sep)


def analyze(a):
    """Run the whole analysis for parsed arguments -> (exit code, message, scorecard or None)."""
    if np is None:
        return 3, "burst_source_report: numpy is required", None
    d = a.data_dir
    adir, hdir = os.path.join(d, "analysis"), os.path.join(d, "history")
    bands = a.beam_bands if a.beam_bands else os.path.join(d, "reports", "beam_bands.jsonl")
    ins = {
        "dsp1": os.path.join(adir, "dsp.jsonl.1"),
        "dsp": os.path.join(adir, "dsp.jsonl"),
        "spatial": os.path.join(adir, "spatial.jsonl"),
        "coh": os.path.join(adir, "coherence.jsonl"),
        "bursts": os.path.join(adir, "bursts.jsonl"),
        "amb1": os.path.join(hdir, "ambient_temp.jsonl.1"),
        "amb": os.path.join(hdir, "ambient_temp.jsonl"),
        "board": os.path.join(hdir, "board_history.jsonl"),
        "bands": bands,
    }
    out = a.out
    if inside(out, adir) or inside(out, hdir) or any(inside(p, out) for p in ins.values()):
        return (
            2,
            "burst_source_report: --out must not equal or lie inside an input directory, nor contain an input file (the tool writes only to a new report directory)",
            None,
        )
    if not (os.path.exists(ins["dsp"]) or os.path.exists(ins["dsp1"])):
        return 3, "burst_source_report: no dsp.jsonl input", None
    try:
        raw_th = open(a.thresholds, "rb").read()
        th = json.loads(raw_th)
        with open(DEFAULT_THRESHOLDS, "rb") as f:
            gaps = missing_keys(json.loads(f.read()), th)
    except (OSError, ValueError, TypeError):
        return 2, "burst_source_report: the thresholds file is unreadable or incomplete", None
    if gaps:
        return 2, "burst_source_report: the thresholds file is unreadable or incomplete (missing: " + ", ".join(gaps[:5]) + ")", None
    sha = hashlib.sha256(raw_th).hexdigest()
    try:
        since = parse_iso(a.since) if a.since else None
        until = parse_iso(a.until) if a.until else None
    except ValueError:
        return 2, "burst_source_report: --since/--until need an ISO UTC time", None
    sk = Skips()
    tr, slots = load_dsp([ins["dsp1"], ins["dsp"]], sk)
    spatial, amb = load_spatial(ins["spatial"], sk), load_ambient([ins["amb1"], ins["amb"]], sk)
    board, band, coh, bursts = load_board(ins["board"], sk), load_band816(bands, sk), load_coherence(ins["coh"], sk), load_bursts(ins["bursts"], sk)
    sh = (slots // 3600) * 3600
    tr["_slots"] = {int(h): slots[sh == h] for h in np.unique(sh)}
    th_hour = (np.floor(tr["t"] / 3600) * 3600).astype(np.int64)
    raw = {}
    for h in sorted({int(x) for x in np.unique(th_hour)} | set(tr["_slots"])):
        if (since is not None and h < since) or (until is not None and h >= until):
            continue
        i0, i1 = np.searchsorted(th_hour, [h, h + 1])
        raw[h] = hour_features(h, tr, slice(int(i0), int(i1)), spatial, band, board, coh, bursts, th, a.seed, a.jitter_surrogates, a.max_surrogate_pairs)
    digest_path = os.path.join(out, "burst_hours.jsonl")
    digest = load_digest(digest_path, th, sk) if not a.no_write_digest else {}
    hours = build_hours(raw, digest, amb, th, th["thresholds_version"])
    view = [h for h in hours if (since is None or h["hour_utc"] >= since) and (until is None or h["hour_utc"] < until)]
    if not view:
        return 3, "burst_source_report: no hour in range", None
    n_tr = int(len(tr["t"]))
    meta = {
        "now": int(a.now) if a.now is not None else int(time.time()),
        "thresholds_sha256": sha,
        "thresholds_pinned": sha == PINNED_THRESHOLDS_SHA256,
        "beam_bands_present": band is not None,
        "inputs": {
            "dsp_records": int(len(slots)),
            "transients": n_tr,
            "spatial_records": None if spatial is None else int(len(spatial["t"])),
            "ambient_records": 0 if amb is None else len(amb),
            "coherence_records": 0 if coh is None else len(coh),
            "board_records": None if board is None else sum(len(v) for v in board.values()),
            "beam_bands_records": None if band is None else int(len(band[0])),
            "skipped_lines": sk.n,
        },
    }
    sc = make_scorecard(view, amb, th, meta)
    # the digest keeps every hour ever seen (also those outside --since/--until)
    write_report(out, sc, view, th, not a.no_write_digest, hours)
    return (
        0,
        (
            f"burst_source_report: hours={sc['n_hours_covered']} burst_hours={sc['n_burst_hours']} decisive={sc['decisive_count']} "
            + " ".join(f"{k}={sc['verdicts'][k]['verdict']}" for k in HYPOTHESES)
        ),
        sc,
    )


def self_check():
    sys.path.insert(0, HERE)
    import make_burst_source_fixtures as M

    bad = 0
    for name, builder, expect in M.scenarios():
        with tempfile.TemporaryDirectory(prefix="bsr_selfcheck_") as d:
            builder(d)
            a = argparse.Namespace(
                data_dir=d,
                out=os.path.join(d, "report"),
                beam_bands=None,
                since=None,
                until=None,
                thresholds=DEFAULT_THRESHOLDS,
                jitter_surrogates=200,
                seed=20261026,
                max_surrogate_pairs=600_000_000,
                no_write_digest=False,
                now=None,
            )
            code, msg, sc = analyze(a)
            got = {k: sc["verdicts"][k]["verdict"] for k in HYPOTHESES} if sc else None
            ok = code == 0 and all(got[k] == v for k, v in expect.items())
            bad += not ok
            print(("ok   " if ok else "FAIL ") + name + " " + json.dumps(got))
    return 1 if bad else 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="burst_source_report.py", description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", default=os.environ.get("BMA_DATA_DIR") or "data")
    ap.add_argument("--out", default=None, help="report directory (default <data-dir>/reports/burst_source)")
    ap.add_argument("--beam-bands", default=None, help="default <data-dir>/reports/beam_bands.jsonl; the 8-16 kHz columns are null when it is absent")
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    ap.add_argument("--thresholds", default=DEFAULT_THRESHOLDS)
    ap.add_argument("--jitter-surrogates", type=int, default=200)
    ap.add_argument("--seed", type=int, default=20261026)
    ap.add_argument("--max-surrogate-pairs", type=int, default=600_000_000, help="cap on pairs x surrogates per hour; fewer surrogates are used above it")
    ap.add_argument("--no-write-digest", action="store_true", help="neither read nor write burst_hours.jsonl in --out")
    ap.add_argument("--self-check", action="store_true", help="run the synthetic scenarios through the same code")
    ap.add_argument("--now", type=int, default=None, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.self_check:
        return self_check() if np is not None else 3
    if a.out is None:
        a.out = os.path.join(a.data_dir, "reports", "burst_source")
    if a.jitter_surrogates < 1 or a.max_surrogate_pairs < 1:
        print("burst_source_report: --jitter-surrogates and --max-surrogate-pairs must be positive")
        return 2
    code, msg, _ = analyze(a)
    print(msg)
    return code


if __name__ == "__main__":
    sys.exit(main())
