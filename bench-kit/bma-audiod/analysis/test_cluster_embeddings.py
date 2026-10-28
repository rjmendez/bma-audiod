#!/usr/bin/env python3
"""Hermetic tests for the pure functions in cluster_embeddings.py (tiny synthetic data, exact values; no data files,
no UMAP/HDBSCAN run). Needs numpy + scikit-learn:  python test_cluster_embeddings.py"""

import datetime, os, sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cluster_embeddings as C  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


# ---- l2_normalize
x = C.l2_normalize([[3.0, 4.0], [0.0, 0.0], [0.0, -2.0]])
check("l2: (3,4) -> (0.6, 0.8) exactly in float32", x[0, 0] == np.float32(0.6) and x[0, 1] == np.float32(0.8), str(x[0]))
check("l2: unit norm for a non-zero row (positive twin of the zero-row case)", abs(float(np.linalg.norm(x[2])) - 1.0) < 1e-7 and x[2, 1] == -1.0)
check("l2: a zero row stays exactly zero, never NaN", not x[1].any() and not np.isnan(x).any(), str(x[1]))

# ---- join_nearest
ref = [0.0, 10.0, 20.0]
q = [0.4, 5.1, 10.0, 14.9, 99.0]
check("join: tol 0.5 -> only the close ones match, others -1", C.join_nearest(ref, q, 0.5).tolist() == [0, -1, 1, -1, -1], str(C.join_nearest(ref, q, 0.5)))
check(
    "join: tol 5 -> nearest wins (14.9 is nearer 10 than 20), far one still -1",
    C.join_nearest(ref, q, 5.0).tolist() == [0, 1, 1, 1, -1],
    str(C.join_nearest(ref, q, 5.0)),
)
check("join: an unsorted reference returns indices into the ORIGINAL order", C.join_nearest([20.0, 0.0, 10.0], [10.0], 0.1).tolist() == [2])
check("join: empty reference -> all -1", C.join_nearest([], [1.0, 2.0], 1.0).tolist() == [-1, -1])
check("join: an exact boundary distance (== tol) matches", C.join_nearest([0.0], [2.0], 2.0).tolist() == [0])

# ---- window_mean
tr = [1.0, 2.0, 3.0, 4.0]
v = [10.0, 20.0, 30.0, 40.0]
check(
    "window: (1, 3] holds t=2,3 -> mean 25 (the row exactly at the lower edge is excluded)",
    C.window_mean(tr, v, [3.0], 2.0)[0, 0] == 25.0,
    str(C.window_mean(tr, v, [3.0], 2.0)),
)
check("window: (2, 3] holds only t=3 -> 30 (positive twin with a narrower window)", C.window_mean(tr, v, [3.0], 1.0)[0, 0] == 30.0)
check("window: an empty window is NaN", np.isnan(C.window_mean(tr, v, [0.5], 0.2)[0, 0]))
w2 = C.window_mean([1.0, 2.0], [[1.0, 10.0], [3.0, 30.0]], [2.0], 5.0)
check("window: 2-D values average per column", w2.tolist() == [[2.0, 20.0]], str(w2))
check("window: an unsorted reference gives the same answer", C.window_mean([4.0, 2.0, 3.0, 1.0], [40.0, 20.0, 30.0, 10.0], [3.0], 2.0)[0, 0] == 25.0)

# ---- local time
t_utc = datetime.datetime(2026, 10, 24, 23, 28, 0, tzinfo=datetime.timezone.utc).timestamp()
check("local_hour: 23:28 UTC is hour 19 at UTC-4", C.local_hour([t_utc]).tolist() == [19], str(C.local_hour([t_utc])))
check("local_hour: offset 0 keeps 23 (positive twin)", C.local_hour([t_utc], 0.0).tolist() == [23])
check(
    "local_hour: wraps below midnight (01:00 UTC at -4 is 21)",
    C.local_hour([datetime.datetime(2026, 10, 24, 1, 0, tzinfo=datetime.timezone.utc).timestamp()]).tolist() == [21],
)
t_next = datetime.datetime(2026, 10, 25, 0, 17, 0, tzinfo=datetime.timezone.utc).timestamp()
check("local_date: 00:17 UTC on the 25th is still 10-24 locally (UTC file day != local date)", C.local_date(t_next) == "10-24", C.local_date(t_next))
check("local_date: 23:28 UTC on the 24th is 10-24", C.local_date(t_utc) == "10-24")

# ---- cluster bookkeeping
sizes, noise = C.cluster_counts([0, 0, 1, -1, -1, -1])
check("counts: sizes {0: 2, 1: 1}, noise 3", sizes == {0: 2, 1: 1} and noise == 3, str((sizes, noise)))
tl = C.top_label_share([0, 0, 0, 1], 0, ["a", "a", "b", "c"], 3)
check("top labels: cluster 0 -> a 2/3, b 1/3", tl == [("a", 2 / 3), ("b", 1 / 3)], str(tl))
tl2 = C.top_label_share([0, 0, 0, 1], 0, ["a", None, "a", "z"], 3)
check("top labels: a missing name is ignored but the share stays out of the whole cluster (2/3, not 1)", tl2 == [("a", 2 / 3)], str(tl2))
mp = C.mean_topk_prob([0, 0, 1], 0, [[("x", 0.5), ("y", 0.1)], [("x", 0.3)], None], 3)
check(
    "mean top-k prob: x (0.5+0.3)/2 = 0.4, y 0.1/2 = 0.05, other cluster excluded",
    [nm for nm, _ in mp] == ["x", "y"] and abs(mp[0][1] - 0.4) < 1e-12 and abs(mp[1][1] - 0.05) < 1e-12,
    str(mp),
)

# ---- adjusted Rand
check("ARI: identical partitions with permuted ids -> 1.0", C.adjusted_rand([0, 0, 1, 1], [5, 5, 3, 3]) == 1.0)
check(
    "ARI: [0,0,1,1] vs [0,1,0,1] -> -0.5 exactly",
    abs(C.adjusted_rand([0, 0, 1, 1], [0, 1, 0, 1]) + 0.5) < 1e-12,
    str(C.adjusted_rand([0, 0, 1, 1], [0, 1, 0, 1])),
)
a = [0, 0, 1, 1, -1]
b = [0, 0, 1, 1, 0]
check("ARI: dropping noise rows ignores the disagreement on the noise point -> 1.0", C.adjusted_rand(a, b, drop_noise=True) == 1.0)
check("ARI: keeping noise as a group sees the disagreement -> below 1 (positive twin)", C.adjusted_rand(a, b) < 1.0, str(C.adjusted_rand(a, b)))

# ---- best_jaccard (per-cluster stability)
bj = C.best_jaccard([0, 0, 0, 1, 1, -1], [5, 5, 7, 7, 7, 5])
check("jaccard: cluster 0 {0,1,2} best matches 5 {0,1,5}: 2/4; cluster 1 {3,4} matches 7 {2,3,4}: 2/3", bj == {0: (5, 0.5), 1: (7, 2 / 3)}, str(bj))
check("jaccard: identical partitions with permuted ids -> 1.0 each", C.best_jaccard([0, 0, 1, 1], [3, 3, 9, 9]) == {0: (3, 1.0), 1: (9, 1.0)})
check("jaccard: a cluster whose rows are all noise in b has no match and 0.0", C.best_jaccard([0, 0, 1, 1], [-1, -1, 4, 4]) == {0: (None, 0.0), 1: (4, 1.0)})
check("jaccard: noise rows (-1) in a are not reported as a cluster", -1 not in C.best_jaccard([-1, 0, 0], [0, 0, 0]))

print(f"\n{len(fails)} failure(s)")
sys.exit(1 if fails else 0)
