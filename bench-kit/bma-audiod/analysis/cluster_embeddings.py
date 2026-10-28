#!/usr/bin/env python3
"""Offline clustering of the stored PANNs CNN14 embeddings (2048-d, float16, non-speech windows only).
READ-ONLY on <data-dir>/analysis (<data-dir> = $BMA_DATA_DIR, default ./data); embeddings and metadata only, no audio is read or written.

    python cluster_embeddings.py [--dir <data-dir>/analysis] [--out <data-dir>/reports] [--seed 0]

Pipeline: load emb_YYYYMMDD.f16 (+ .jsonl index; row count verified) -> L2 normalise -> PCA(50) -> UMAP(10) ->
HDBSCAN. Joins tags / CLAP / DSP metadata by timestamp. Stability: a second run on a random 80 % subsample with a
different seed, compared with the adjusted Rand index on the shared windows. Writes embedding_clusters.md and
embedding_clusters.npz (labels + row metadata) into --out.

File names are UTC days; the index `i` counts across days (host start), the row order in a file is the index order.
Local time for hour-of-day is a fixed UTC offset (EDT, -4, valid for the whole 2026-10-11..25 data)."""
import argparse, collections, datetime, glob, json, os, sys, time

import numpy as np

DIM = 2048
BAND_NAMES = ["0-100", "100-1k", "1-4k", "4-12k", "12-24k"]   # Hz; = plugins.BANDS (dsp band_pct order)
INDOOR_DAYS = {"20261011", "20261012", "20261013", "20261014"}
OUTDOOR_DAYS = {"20261024", "20261025"}                      # UTC file names covering the 2026-10-24 evening outdoors
EDT = -4.0


# ------------------------------------------------------------------ pure functions (tested)
def l2_normalize(x):
    """Rows divided by their L2 norm; a zero row stays zero (never NaN)."""
    x = np.asarray(x, dtype=np.float32)
    n = np.linalg.norm(x, axis=1, keepdims=True)
    return np.divide(x, n, out=np.zeros_like(x), where=n > 0)


def join_nearest(t_ref, t_q, tol):
    """For each query time the index of the nearest reference time within `tol` seconds, else -1."""
    t_ref = np.asarray(t_ref, dtype=float)
    t_q = np.asarray(t_q, dtype=float)
    if len(t_ref) == 0:
        return np.full(len(t_q), -1, dtype=np.int64)
    order = np.argsort(t_ref, kind="stable")
    ts = t_ref[order]
    pos = np.searchsorted(ts, t_q)
    lo = np.clip(pos - 1, 0, len(ts) - 1)
    hi = np.clip(pos, 0, len(ts) - 1)
    dlo = np.abs(ts[lo] - t_q)
    dhi = np.abs(ts[hi] - t_q)
    best = np.where(dlo <= dhi, lo, hi)
    d = np.minimum(dlo, dhi)
    return np.where(d <= tol, order[best], -1)


def window_mean(t_ref, values, t_q, width):
    """Mean of `values` rows whose time lies in (t_q - width, t_q]; NaN row where the window is empty."""
    t_ref = np.asarray(t_ref, dtype=float)
    order = np.argsort(t_ref, kind="stable")
    ts = t_ref[order]
    v = np.asarray(values, dtype=float)[order]
    if v.ndim == 1:
        v = v[:, None]
    c = np.vstack([np.zeros((1, v.shape[1])), np.cumsum(v, axis=0)])
    t_q = np.asarray(t_q, dtype=float)
    hi = np.searchsorted(ts, t_q, side="right")
    lo = np.searchsorted(ts, t_q - width, side="right")
    n = hi - lo
    out = np.full((len(t_q), v.shape[1]), np.nan)
    ok = n > 0
    out[ok] = (c[hi[ok]] - c[lo[ok]]) / n[ok, None]
    return out


def local_hour(t, utc_offset_h=EDT):
    """Integer local hour 0-23 for unix times."""
    return (np.floor(np.asarray(t, dtype=float) / 3600.0 + utc_offset_h) % 24).astype(int)


def local_date(t, utc_offset_h=EDT):
    d = datetime.datetime.fromtimestamp(float(t), tz=datetime.timezone(datetime.timedelta(hours=utc_offset_h)))
    return d.strftime("%m-%d")


def cluster_counts(labels):
    """-> (sizes {cluster: n} for cluster >= 0, noise count)."""
    labels = np.asarray(labels)
    sizes = collections.Counter(int(v) for v in labels if v >= 0)
    return dict(sizes), int((labels < 0).sum())


def top_label_share(labels, cluster, names, k=3):
    """The k most common names inside a cluster as [(name, share of the cluster)]."""
    labels = np.asarray(labels)
    sel = [names[i] for i in np.nonzero(labels == cluster)[0] if names[i]]
    n = int((labels == cluster).sum())
    return [(nm, c / n) for nm, c in collections.Counter(sel).most_common(k)]


def mean_topk_prob(labels, cluster, topk_lists, k=3):
    """Mean probability of each label over the cluster's rows (a row contributes 0 where the label is not in its
    top list); topk_lists[i] is [(name, p), ...] or None. -> the k largest [(name, mean p)]."""
    idx = np.nonzero(np.asarray(labels) == cluster)[0]
    acc = collections.defaultdict(float)
    for i in idx:
        for nm, p in (topk_lists[i] or []):
            acc[nm] += p
    n = max(1, len(idx))
    return sorted(((nm, v / n) for nm, v in acc.items()), key=lambda x: -x[1])[:k]


def adjusted_rand(a, b, drop_noise=False):
    """Adjusted Rand index between two labelings of the same rows; noise (-1) is one extra group unless dropped."""
    from sklearn.metrics import adjusted_rand_score
    a = np.asarray(a)
    b = np.asarray(b)
    if drop_noise:
        keep = (a >= 0) & (b >= 0)
        a, b = a[keep], b[keep]
    return float(adjusted_rand_score(a, b)) if len(a) else float("nan")


def best_jaccard(a, b):
    """Per-cluster stability: for every cluster id >= 0 in `a`, the best-matching cluster id >= 0 in `b` and their
    Jaccard overlap (rows in both / rows in either), over the same rows. -> {cluster: (match or None, jaccard)}."""
    a = np.asarray(a)
    b = np.asarray(b)
    out = {}
    for c in sorted(set(a[a >= 0].tolist())):
        ma = a == c
        best, bj = None, 0.0
        for d in sorted(set(b[b >= 0].tolist())):
            mb = b == d
            j = float((ma & mb).sum()) / float((ma | mb).sum())
            if j > bj:
                best, bj = d, j
        out[c] = (best, bj)
    return out


# ------------------------------------------------------------------ data loading
def read_jsonl(path, needle=None):
    out = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if needle and needle not in line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def load_embeddings(d):
    """-> X float32 (n, 2048), t, top1, day (UTC file name), file order. Row counts are verified against the index."""
    xs, ts, top1, days = [], [], [], []
    for p in sorted(glob.glob(os.path.join(d, "emb_*.f16"))):
        day = os.path.basename(p)[4:12]
        raw = np.fromfile(p, dtype=np.float16)
        if raw.size % DIM:
            raise SystemExit(f"{p}: {raw.size} halfs is not a multiple of {DIM}")
        idx = read_jsonl(p[:-4] + ".jsonl")
        if raw.size // DIM != len(idx):
            raise SystemExit(f"{p}: {raw.size // DIM} vectors but {len(idx)} index lines")
        xs.append(raw.reshape(-1, DIM).astype(np.float32))
        ts += [r["t"] for r in idx]
        top1 += [r.get("top1") for r in idx]
        days += [day] * len(idx)
    return np.vstack(xs), np.array(ts), top1, np.array(days)


def reduce_and_cluster(xn, seed, pca_dim, umap_dim, min_cluster_size, min_samples):
    from sklearn.cluster import HDBSCAN
    from sklearn.decomposition import PCA
    import umap
    p = PCA(n_components=pca_dim, svd_solver="randomized", random_state=seed).fit_transform(xn)
    u = umap.UMAP(n_components=umap_dim, n_neighbors=30, min_dist=0.0, metric="euclidean", random_state=seed).fit_transform(p)
    lab = HDBSCAN(min_cluster_size=min_cluster_size, min_samples=min_samples).fit_predict(u)
    return lab, u


def _battery_worker(job):
    d, excl, seed, pca_dim, umap_dim, mcs, ms = job
    X, t, top1, day = load_embeddings(d)
    if excl:
        keep = np.array([lbl not in excl for lbl in top1])
        X = X[keep]
    xn = l2_normalize(X)
    lab, _ = reduce_and_cluster(xn, seed, pca_dim, umap_dim, mcs, ms)
    return seed, lab


def run_battery(a, excl, log):
    """K independent full-data runs (seeds 0..K-1, in parallel processes) and their pairwise ARI."""
    from concurrent.futures import ProcessPoolExecutor
    jobs = [(a.dir, excl, s, a.pca_dim, a.umap_dim, a.min_cluster_size, a.min_samples) for s in range(a.battery)]
    with ProcessPoolExecutor(max_workers=a.battery) as ex:
        res = dict(ex.map(_battery_worker, jobs))
    seeds = sorted(res)
    L = [f"# Seed battery{' (excluding ' + ','.join(excl) + ')' if excl else ''}: {len(seeds)} independent full-data runs\n",
         f"{len(res[seeds[0]])} rows, PCA{a.pca_dim} -> UMAP{a.umap_dim} -> HDBSCAN(min_cluster_size {a.min_cluster_size}, min_samples {a.min_samples}).\n",
         "| seed | clusters | noise | noise % |", "|---|---|---|---|"]
    for s in seeds:
        sz, nz = cluster_counts(res[s])
        L.append(f"| {s} | {len(sz)} | {nz} | {100.0 * nz / len(res[s]):.1f} |")
    pairs = [(i, j) for i in seeds for j in seeds if i < j]
    allv = [adjusted_rand(res[i], res[j]) for i, j in pairs]
    core = [adjusted_rand(res[i], res[j], drop_noise=True) for i, j in pairs]
    L.append("\n| pair | ARI (noise as group) | ARI (non-noise) |\n|---|---|---|")
    for (i, j), x, y in zip(pairs, allv, core):
        L.append(f"| {i}-{j} | {x:.3f} | {y:.3f} |")
    L.append(f"\nARI all: min {min(allv):.3f} median {float(np.median(allv)):.3f} max {max(allv):.3f}; "
             f"non-noise: min {min(core):.3f} median {float(np.median(core)):.3f} max {max(core):.3f}.")
    return "\n".join(L) + "\n"


def pct(v, q):
    v = v[~np.isnan(v)]
    return float(np.percentile(v, q)) if len(v) else float("nan")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir", default=os.path.join(os.environ.get("BMA_DATA_DIR") or "data", "analysis"))
    ap.add_argument("--out", default=os.path.join(os.environ.get("BMA_DATA_DIR") or "data", "reports"))
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--stability-seed", type=int, default=1)
    ap.add_argument("--stability-frac", type=float, default=0.8)
    ap.add_argument("--pca-dim", type=int, default=50)
    ap.add_argument("--umap-dim", type=int, default=10)
    ap.add_argument("--min-cluster-size", type=int, default=150)
    ap.add_argument("--min-samples", type=int, default=10)
    ap.add_argument("--exclude-top1", default="", help="comma list of tagger top-1 labels to leave out (e.g. Silence)")
    ap.add_argument("--tag", default="", help="suffix for the output file names")
    ap.add_argument("--battery", type=int, default=0, help="instead of the report: K independent full-data seeds + pairwise ARI")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()
    log = lambda m: print(f"[{time.time() - t0:6.0f}s] {m}", flush=True)
    excl = [s for s in a.exclude_top1.split(",") if s]
    if a.battery:
        text = run_battery(a, excl, log)
        open(os.path.join(a.out, "embedding_stability" + a.tag + ".md"), "w", encoding="utf-8").write(text)
        log("wrote " + os.path.join(a.out, "embedding_stability" + a.tag + ".md"))
        return

    X, t, top1, day = load_embeddings(a.dir)
    if excl:
        keep = np.array([lbl not in excl for lbl in top1])
        log(f"excluding tagger top-1 {excl}: dropping {int((~keep).sum())} of {len(keep)} windows")
        X, t, day = X[keep], t[keep], day[keep]
        top1 = [lbl for lbl, k in zip(top1, keep) if k]
    n = len(X)
    log(f"loaded {n} vectors from {sorted(set(day))}")
    norms = np.linalg.norm(X, axis=1)
    xn = l2_normalize(X)
    del X
    log(f"norm min/median/max {norms.min():.3f}/{np.median(norms):.3f}/{norms.max():.3f}; zero rows {(norms == 0).sum()}")

    lab, _u = reduce_and_cluster(xn, a.seed, a.pca_dim, a.umap_dim, a.min_cluster_size, a.min_samples)
    sizes, noise = cluster_counts(lab)
    log(f"seed {a.seed}: {len(sizes)} clusters, noise {noise} ({100.0 * noise / n:.1f}%)")

    rng = np.random.default_rng(12345)
    sub = np.sort(rng.choice(n, size=int(a.stability_frac * n), replace=False))
    lab2, _ = reduce_and_cluster(xn[sub], a.stability_seed, a.pca_dim, a.umap_dim, a.min_cluster_size, a.min_samples)
    s2, noise2 = cluster_counts(lab2)
    ari_all = adjusted_rand(lab[sub], lab2)
    ari_core = adjusted_rand(lab[sub], lab2, drop_noise=True)
    log(f"stability run (seed {a.stability_seed}, {len(sub)} rows): {len(s2)} clusters, noise {noise2}; ARI all {ari_all:.3f}, ARI non-noise {ari_core:.3f}")

    # ---- metadata joins (timestamps are the window time shared by tags / clap / emb index)
    tags = read_jsonl(os.path.join(a.dir, "tags.jsonl"), '"tags"')
    clap = read_jsonl(os.path.join(a.dir, "clap.jsonl"), '"clap"')
    dsp = [r for r in read_jsonl(os.path.join(a.dir, "dsp.jsonl"), '"rms_dbfs"') if r.get("k") == "dsp"]
    birds = read_jsonl(os.path.join(a.dir, "birds.jsonl"), '"bird"')
    log(f"metadata: tags {len(tags)}, clap {len(clap)}, dsp {len(dsp)}, birds {len(birds)}")
    jt = join_nearest([r["t"] for r in tags], t, 0.002)
    jc = join_nearest([r["t"] for r in clap], t, 0.002)
    tag_top = [tags[j]["top"] if j >= 0 else None for j in jt]
    clap_top1 = [clap[j]["top"][0][0] if j >= 0 and clap[j].get("top") else None for j in jc]
    clap_top = [clap[j]["top"] if j >= 0 else None for j in jc]
    td = np.array([r["t"] for r in dsp])
    lvl = window_mean(td, [float(np.mean(r["rms_dbfs"])) for r in dsp], t, 5.0)[:, 0]
    cen = window_mean(td, [r["centroid_hz"] for r in dsp], t, 5.0)[:, 0]
    band = window_mean(td, [r["band_pct"] for r in dsp], t, 5.0)
    hour = local_hour(t)
    ldate = np.array([local_date(x) for x in t])
    outdoor = np.isin(day, list(OUTDOOR_DAYS))

    # ---- special moments
    fly0 = datetime.datetime(2026, 10, 24, 19, 28, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=EDT))).timestamp()
    fly = (t >= fly0) & (t <= fly0 + 120)
    FP = {"Alpine Swift", "Laysan Albatross", "Black-footed Albatross", "Tawny Frogmouth"}
    fp_rows = np.zeros(n, bool)
    fp_dets = [b for b in birds if b.get("common") in FP and b["t"] > 1.79e9]
    for b in fp_dets:
        fp_rows |= np.abs(t - b["t"]) <= 3.0

    # ---- report
    L = []
    L.append("# Embedding clusters (PANNs CNN14, non-speech windows)\n")
    L.append(f"{n} vectors, days (UTC files) {', '.join(sorted(set(day)))}; indoor 10-11..14 = {int((~outdoor).sum())}, outdoors 10-24 evening = {int(outdoor.sum())}.")
    L.append(f"Method: L2 -> PCA{a.pca_dim} -> UMAP{a.umap_dim} (n_neighbors 30, min_dist 0) -> HDBSCAN(min_cluster_size {a.min_cluster_size}, min_samples {a.min_samples}), seed {a.seed}.")
    L.append(f"**{len(sizes)} clusters, {noise} noise ({100.0 * noise / n:.1f}%).**")
    L.append(f"Stability: second run, seed {a.stability_seed} on a random {int(100 * a.stability_frac)}% subsample ({len(sub)} rows): {len(s2)} clusters, {noise2} noise; "
             f"ARI vs the first run on the shared rows = {ari_all:.3f} (noise as a group), {ari_core:.3f} (non-noise only).")
    bj = best_jaccard(lab[sub], lab2)
    L.append("Per-cluster stability (best Jaccard match in the second run): " + ", ".join(f"c{c}={j:.2f}" for c, (_m, j) in bj.items()) + ".")
    if excl:
        L.append(f"**Filter: tagger top-1 in {excl} excluded ({len(excl)} label(s)); this is the non-silence subset.**")
    L.append("")
    L.append("Level/centroid/bands = mean over the 5 s of dsp.jsonl ending at the window time (assumed window alignment); bands %: " + " | ".join(BAND_NAMES) + ".\n")
    order = sorted(sizes, key=lambda c: -sizes[c])
    L.append("| id | n | % | outdoor n (expected) | tagger top-1 | tagger mean p (top-5 lists) | CLAP top-1 | level dBFS p10/50/90 | centroid Hz | bands % | hours (local) |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    base_out = outdoor.mean()
    for c in order:
        m = lab == c
        k = int(m.sum())
        oc = int((m & outdoor).sum())
        tl = "; ".join(f"{nm} {100 * s:.0f}%" for nm, s in top_label_share(lab, c, top1, 3))
        tp = "; ".join(f"{nm} {p:.2f}" for nm, p in mean_topk_prob(lab, c, tag_top, 3))
        cl = "; ".join(f"{nm} {100 * s:.0f}%" for nm, s in top_label_share(lab, c, clap_top1, 2))
        lv = lvl[m]
        bd = np.nanmean(band[m], axis=0) if np.isfinite(band[m]).any() else np.full(5, np.nan)
        hh = collections.Counter(hour[m].tolist())
        hs = " ".join(f"{h:02d}:{100 * v / k:.0f}" for h, v in sorted(hh.items(), key=lambda x: -x[1])[:4])
        L.append(f"| {c} | {k} | {100 * k / n:.1f} | {oc} ({k * base_out:.1f}) | {tl} | {tp} | {cl} | {pct(lv, 10):.1f}/{pct(lv, 50):.1f}/{pct(lv, 90):.1f} | {np.nanmedian(cen[m]):.0f} | "
                 f"{' '.join(f'{v:.0f}' for v in bd)} | {hs} |")
    mnoise = lab < 0
    L.append(f"| noise | {noise} | {100 * noise / n:.1f} | {int((mnoise & outdoor).sum())} ({noise * base_out:.1f}) | "
             f"{'; '.join(f'{nm} {100 * s:.0f}%' for nm, s in top_label_share(lab, -1, top1, 3))} | | | | | | |")
    L.append("\nOutdoor windows are only %.2f%% of the data, so 'indoor-only' is weak evidence unless the expected outdoor count (in brackets) is clearly above 1." % (100 * base_out))

    L.append("\n## Per-day / per-local-date distribution (rows per cluster)\n")
    dates = sorted(set(ldate.tolist()))
    L.append("| local date | rows | " + " | ".join(f"c{c}" for c in order) + " | noise |")
    L.append("|---|---|" + "---|" * (len(order) + 1))
    for dt in dates:
        m = ldate == dt
        L.append(f"| {dt} | {int(m.sum())} | " + " | ".join(str(int((m & (lab == c)).sum())) for c in order) + f" | {int((m & mnoise).sum())} |")

    L.append("\n## 2026-10-24 19:28-19:30 local (aircraft flyover)\n")
    L.append(f"{int(fly.sum())} windows in the 120 s after 19:28:00; cluster membership: " + ", ".join(f"{('c%d' % c) if c >= 0 else 'noise'}: {v}" for c, v in sorted(collections.Counter(lab[fly].tolist()).items(), key=lambda x: -x[1])) + ".")
    if fly.any():
        L.append("Tagger top-1 in those windows: " + "; ".join(f"{k} {v}" for k, v in collections.Counter(np.array(top1, dtype=object)[fly].tolist()).most_common(5)) + ".")
        L.append(f"Mean level over those windows {np.nanmean(lvl[fly]):.1f} dBFS (outdoors-night median {np.nanmedian(lvl[outdoor]):.1f}).")
    L.append("\n## BirdNET false-positive moments (Alpine Swift, Albatross, Tawny Frogmouth; embeddings within +-3 s)\n")
    L.append(f"{len(fp_dets)} such detections after 2026-10-24 start ({collections.Counter(b['common'] for b in fp_dets)}); {int(fp_rows.sum())} embedding windows near them.")
    if fp_rows.any():
        L.append("Cluster membership: " + ", ".join(f"{('c%d' % c) if c >= 0 else 'noise'}: {v}" for c, v in sorted(collections.Counter(lab[fp_rows].tolist()).items(), key=lambda x: -x[1])) + ".")
        L.append("Tagger top-1 there: " + "; ".join(f"{k} {v}" for k, v in collections.Counter(np.array(top1, dtype=object)[fp_rows].tolist()).most_common(5)) + ".")
    base = "embedding_clusters" + a.tag
    open(os.path.join(a.out, base + ".md"), "w", encoding="utf-8").write("\n".join(L) + "\n")
    np.savez_compressed(os.path.join(a.out, base + ".npz"), labels=lab, t=t, day=day, sub=sub, labels_stability=lab2,
                        level=lvl, hour=hour)
    log(f"wrote {os.path.join(a.out, base + '.md')}")


if __name__ == "__main__":
    main()
