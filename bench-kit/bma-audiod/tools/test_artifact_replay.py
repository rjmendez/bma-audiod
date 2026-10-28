#!/usr/bin/env python3
"""Tests for tools/artifact_replay.py. The tool runs as a real subprocess on a synthetic snapshot (tools/artifact_synth.py: invented
data, temp directories only, never live data). Script style (check(name, ok, detail), exit 1 on a failure); run:
    python -I test_artifact_replay.py"""

import datetime, hashlib, json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import artifact_synth as S  # noqa: E402

TOOL = os.path.join(HERE, "artifact_replay.py")
n_checks, fails = 0, []


def check(name, cond, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  [" + str(detail)[:400] + "]"))
    if not cond:
        fails.append(name)


tmp = tempfile.mkdtemp(prefix="artreplay_")


def listing(d):
    return sorted(os.path.relpath(os.path.join(r, f), d).replace(os.sep, "/") for r, _ds, fs in os.walk(d) for f in fs)


def tree_hash(d):
    h = hashlib.sha256()
    for rel in listing(d):
        h.update(rel.encode())
        h.update(open(os.path.join(d, rel), "rb").read())
    return h.hexdigest()


def run(args):
    p = subprocess.run([sys.executable, "-I", TOOL] + args, capture_output=True, text=True, encoding="utf-8")
    return p.returncode, p.stdout, p.stderr


def load(out):
    return json.load(open(os.path.join(out, "artifact_replay.json"), encoding="utf-8"))


def fresh(name, **kw):
    d = os.path.join(tmp, name)
    first, last = S.build(d, **kw)
    return d, first, last


E = S.EXPECT
BINS_ = ("lt5", "5to15", "ge15", "unknown")
data, first, last = fresh("snap")
before = tree_hash(data)
out = os.path.join(tmp, "out1")
rc, so, se = run(["--data-dir", data, "--out", out, "--since", "0", "--sweep", "3,5,12"])
check("valid run exits 0 with an empty stderr (default --taxa works: ui/taxa.json of the repo)", rc == 0 and se == "", (rc, se))
check("the replay wrote exactly artifact_replay.json and artifact_replay.txt", listing(out) == ["artifact_replay.json", "artifact_replay.txt"], listing(out))
check("the snapshot is untouched (tree hash before == after)", tree_hash(data) == before)
r = load(out)
cov = r["coverage"]
check(
    "default taxa: taxa_missing is false, degraded is false, experiment.valid is true, no reasons",
    cov["taxa"] == {"ok": True} and cov["degraded"] is False and cov["degraded_reasons"] == [] and r["experiment"] == {"valid": True},
    cov,
)
check("confusion table: every cell exact (5, 4, 3, 10, 2, 1)", r["confusion"] == E["confusion"], r["confusion"])
check("reason counts exact", r["counts"]["flags"] == E["flags"], r["counts"]["flags"])
check("level counts exact (likely 9, suspect 5, none 13)", r["counts"]["levels"] == E["levels"], r["counts"]["levels"])
hf, hu = r["flagged_by_hour"]["utc_hour"]["flagged"], r["flagged_by_hour"]["utc_hour"]["unflagged"]
check(
    "flagged-by-hour histogram exact: 24 bins, flagged 9/2/1/2 at hours 2/8/14/20, unflagged 4/6/1 at 8/14/20",
    len(hf) == len(hu) == 24 and hf == [E["hour_flagged"].get(h, 0) for h in range(24)] and hu == [E["hour_unflagged"].get(h, 0) for h in range(24)],
    (hf, hu),
)
day = datetime.datetime.fromtimestamp(S.DAY0, datetime.timezone.utc).strftime("%Y-%m-%d")
check(
    "flagged-by-date: one date key, the same histograms",
    list(r["flagged_by_hour"]["by_date"]) == [day] and r["flagged_by_hour"]["by_date"][day] == {"flagged": hf, "unflagged": hu},
)
bg = r["counts"]["by_group"]
check(
    "counts by group and temperature bin: insect lt5 9/0, 5to15 2/4, ge15 1/6, unknown 2/1 (flagged/unflagged)",
    bg["insect"]
    == {
        "lt5": {"flagged": 9, "unflagged": 0},
        "5to15": {"flagged": 2, "unflagged": 4},
        "ge15": {"flagged": 1, "unflagged": 6},
        "unknown": {"flagged": 2, "unflagged": 1},
    },
    bg["insect"],
)
check(
    "counts by label: the Orchelimum cold report is its own row (1/0 in lt5); Engine is label-only (0/2 unknown)",
    r["counts"]["by_label"]["Slender Meadow Katydid"]["lt5"] == {"flagged": 1, "unflagged": 0}
    and r["counts"]["by_label"]["Engine"]["unknown"] == {"flagged": 0, "unflagged": 2}
    and r["counts"]["by_group"]["mechanical"]["unknown"] == {"flagged": 0, "unflagged": 2},
)
sw = r["floor_sweep"]
check(
    "floor sweep exact: floors 3, 5, 12",
    sorted(sw, key=float) == ["3", "5", "12"]
    and sw["3"]["by_bin"]
    == {
        "lt5": {"n": 9, "cold": 9, "any": 9},
        "5to15": {"n": 6, "cold": 0, "any": 2},
        "ge15": {"n": 7, "cold": 0, "any": 1},
        "unknown": {"n": 0, "cold": 0, "any": 0},
    }
    and sw["12"]["by_bin"]["5to15"] == {"n": 6, "cold": 6, "any": 6}
    and sw["12"]["by_bin"]["ge15"] == {"n": 7, "cold": 0, "any": 1}
    and sw["5"]["by_bin"]["5to15"] == {"n": 6, "cold": 0, "any": 2},
    sw["3"]["by_bin"],
)
check(
    "floor sweep per species: the Orchelimum cold report is cold at floor 3, the katydid cold ones 8",
    sw["3"]["by_species"]["Slender Meadow Katydid"]["lt5"] == {"n": 1, "cold": 1, "any": 1}
    and sw["3"]["by_species"]["Common Virtuoso Katydid"]["lt5"] == {"n": 8, "cold": 8, "any": 8},
)
check(
    "floor 12 differs from floor 5 on the 5-15 C bin (cold 6 vs 0), the lt5 bin is equal",
    sw["12"]["by_bin"]["lt5"] == sw["5"]["by_bin"]["lt5"] and sw["12"]["by_bin"]["5to15"]["cold"] == 6 and sw["5"]["by_bin"]["5to15"]["cold"] == 0,
)
check(
    "burst by temperature bin exact",
    r["burst_by_temp_bin"] == {"lt5": {"n": 9, "burst": 5}, "5to15": {"n": 6, "burst": 2}, "ge15": {"n": 7, "burst": 1}, "unknown": {"n": 3, "burst": 2}},
    r["burst_by_temp_bin"],
)
rate = r["flagged_by_hour"]["transient_rate_by_temp_bin"]
check(
    "transient rate by temperature bin: 0-5 C 1 h of 150, >=10 C 2 h median 37.5 max 50, the others empty",
    rate["0to5"] == {"hours": 1, "median_per_hour": 150, "max_per_hour": 150}
    and rate["ge10"] == {"hours": 2, "median_per_hour": 37.5, "max_per_hour": 50}
    and rate["lt-5"]["hours"] == 0
    and rate["-5to0"]["hours"] == 0
    and rate["5to10"]["hours"] == 0,
    rate,
)
th = {h["t_hour_utc"]: (h["n"], h["temp_c"]) for h in r["transient_hours"]}
check(
    "transient_hours is the durable evidence table: hour 2 150 at 2.0 C, hour 8 50 at 10.0, hour 14 25 at 20.0, hour 20 50 with no temperature",
    th == {S.DAY0 + 2 * 3600: (150, 2.0), S.DAY0 + 8 * 3600: (50, 10.0), S.DAY0 + 14 * 3600: (25, 20.0), S.DAY0 + 20 * 3600: (50, None)},
    th,
)
check(
    "out-of-scope labels: woodpecker 4, nuthatch 3; flagged by group bird 0, insect 14, mechanical 0",
    r["out_of_scope_counts"] == {"Red-bellied Woodpecker": 4, "White-breasted Nuthatch": 3}
    and r["flagged_by_group"] == {"bird": 0, "insect": 14, "mechanical": 0},
    (r["out_of_scope_counts"], r["flagged_by_group"]),
)
check(
    "coverage: rows 34, in window 34, skipped hostile bird lines 4 (not JSON, a list, NaN time, a 1.1 MB line)",
    cov["reports"]["n_rows"] == 34 and cov["reports"]["n_in_window"] == 34 and cov["skipped_lines"] == {"birds": 4, "dsp": 0, "ambient": 0},
    cov,
)
check(
    "coverage: 11 burst reports (10 insect + 1 woodpecker) x 25 transients = 275, and the exact report range",
    cov["transients"]["n"] == 275 and cov["reports"]["t_min"] == first and cov["reports"]["t_max"] == last,
    cov,
)
check(
    "coverage: ambient 3 hours, transients cover the reports, ambient does NOT (hour 20 is more than 3 h after the last record)",
    cov["ambient"]["n"] == 3 and cov["transients_cover_reports"] is True and cov["ambient_covers_reports"] is False,
)
bsha = hashlib.sha256(open(os.path.join(data, "analysis", "birds.jsonl"), "rb").read()).hexdigest()
names = sorted(i["name"] for i in cov["inputs"])
check(
    "coverage.inputs: exactly the 4 input files",
    names == ["analysis/birds.jsonl", "analysis/dsp.jsonl", "analysis/dsp.jsonl.1", "history/ambient_temp.jsonl"],
    names,
)
check(
    "coverage.inputs: birds.jsonl sha256 and byte size equal the file's",
    {i["name"]: i for i in cov["inputs"]}["analysis/birds.jsonl"]
    == {"name": "analysis/birds.jsonl", "bytes": os.path.getsize(os.path.join(data, "analysis", "birds.jsonl")), "sha256": bsha},
)
check("no timing block without --timing (reproducible)", "timing" not in r)
rc2, _o, _e = run(["--data-dir", data, "--out", os.path.join(tmp, "out2"), "--since", "0", "--sweep", "3,5,12"])
check(
    "determinism: a second run is byte-identical (json and txt)",
    rc2 == 0
    and all(
        open(os.path.join(out, f), "rb").read() == open(os.path.join(tmp, "out2", f), "rb").read() for f in ("artifact_replay.json", "artifact_replay.txt")
    ),
)
rc3, _o, _e = run(["--data-dir", data, "--out", os.path.join(tmp, "out3"), "--since", "0", "--sweep", "3,5,12", "--timing"])
check("--timing adds wall_s and cpu_s (twin of the no-timing check)", rc3 == 0 and set(load(os.path.join(tmp, "out3"))["timing"]) == {"wall_s", "cpu_s"})

# ---- hidden labels
txt = open(os.path.join(out, "artifact_replay.json"), encoding="utf-8").read() + open(os.path.join(out, "artifact_replay.txt"), encoding="utf-8").read()
check("hidden rows: excluded_hidden_reports == 5 (3 + 1 + 1)", r["excluded_hidden_reports"] == 5)
check(
    "hidden rows: the serialised json and txt contain neither 'human' nor 'speech' (case-insensitive)",
    "human" not in txt.lower() and "speech" not in txt.lower(),
    [w for w in ("human", "speech") if w in txt.lower()],
)
check("hidden rows: their timestamp (integer part) appears nowhere in the output", str(int(S.HID_T)) not in txt and str(S.HID_T) not in txt)
check("hidden twin: a normal row's timestamp (first report) and the label Engine ARE in the output", str(first) in txt and "Engine" in txt)
check(
    "hidden rows: they are in no histogram (25 insect reports counted) and their hour 22 is not in transient_hours",
    sum(hf) + sum(hu) == 25 and S.DAY0 + 22 * 3600 not in th,
)

# ---- since / until
rc, _o, _e = run(["--data-dir", data, "--out", os.path.join(tmp, "out_since"), "--since", str(S.DAY0 + 14 * 3600)])
rs = load(os.path.join(tmp, "out_since"))
rc_, _o_, _e_ = run(["--data-dir", data, "--out", os.path.join(tmp, "out_cov"), "--until", str(S.DAY0 + 15 * 3600)])
check(
    "coverage twin: restricted to reports up to hour 15 the ambient feed covers them (true)",
    rc_ == 0 and load(os.path.join(tmp, "out_cov"))["coverage"]["ambient_covers_reports"] is True,
)
check(
    "--since at hour 14: 12 rows in the window (10 insect + 2 Engine), 34 rows read; the cold hour is outside",
    rc == 0
    and rs["coverage"]["reports"]["n_in_window"] == 12
    and rs["coverage"]["reports"]["n_rows"] == 34
    and rs["confusion"]["cold_burst"] == 0
    and rs["confusion"]["warm_noburst"] == 6,
    rs["confusion"],
)
iso = datetime.datetime.fromtimestamp(S.DAY0 + 14 * 3600, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
rc, _o, _e = run(["--data-dir", data, "--out", os.path.join(tmp, "out_iso"), "--since", iso, "--until", str(S.DAY0 + 15 * 3600)])
ri = load(os.path.join(tmp, "out_iso"))
check(
    "--since ISO and --until: hour 14 only (7 insect + 2 Engine = 9 rows)",
    rc == 0 and ri["coverage"]["reports"]["n_in_window"] == 9 and ri["params"]["since"] == S.DAY0 + 14 * 3600,
    ri["coverage"]["reports"],
)
rc, _o, se = run(["--data-dir", data, "--out", os.path.join(tmp, "out_bad"), "--since", "yesterday"])
check("a bad --since exits 2 with a message naming the value", rc == 2 and "time 'yesterday' is neither Unix seconds nor an ISO UTC date" in se, (rc, se))

# ---- --out guard
for sub in ("analysis", "history"):
    for tail in ("", "sub"):
        o = os.path.join(data, sub, tail) if tail else os.path.join(data, sub)
        rc, _o, se = run(["--data-dir", data, "--out", o])
        check(
            "--out %s%s refused: exit 2, message names the directory, nothing written" % (sub, "/" + tail if tail else ""),
            rc == 2 and ("refusing --out inside <data-dir>/%s" % sub) in se and tree_hash(data) == before,
            (rc, se),
        )
rc, _o, _e = run(["--data-dir", data, "--out", os.path.join(tmp, "out_good")])
check(
    "twin: a valid --out outside analysis/ and history/ exits 0 and writes exactly two files",
    rc == 0 and listing(os.path.join(tmp, "out_good")) == ["artifact_replay.json", "artifact_replay.txt"],
)

# ---- degraded runs are never a pass
rc, _o, se = run(["--data-dir", data, "--out", os.path.join(tmp, "out_notaxa"), "--taxa", os.path.join(tmp, "no_such_taxa.json")])
rn = load(os.path.join(tmp, "out_notaxa"))
check(
    "nonexistent --taxa: exit 1, degraded true, taxa_missing, experiment.valid false, nothing flagged, stderr says DEGRADED",
    rc == 1
    and rn["coverage"]["degraded"] is True
    and rn["coverage"]["degraded_reasons"] == ["taxa_missing"]
    and rn["experiment"]["valid"] is False
    and rn["coverage"]["taxa"] == {"ok": False}
    and rn["counts"]["levels"] == {"likely": 0, "none": 0, "suspect": 0}
    and "DEGRADED" in se,
    (rc, rn["coverage"], se),
)
amb_dir, _f, _l = fresh("noamb", with_ambient=False)
rc, _o, _e = run(["--data-dir", amb_dir, "--out", os.path.join(tmp, "out_noamb")])
ra = load(os.path.join(tmp, "out_noamb"))
check(
    "missing ambient: exit 1, ambient_missing, experiment.valid false; temp_unknown 25, no cold_insect, burst flags still 10",
    rc == 1
    and ra["coverage"]["degraded_reasons"] == ["ambient_missing"]
    and ra["experiment"]["valid"] is False
    and ra["counts"]["flags"]["temp_unknown"] == 25
    and ra["counts"]["flags"]["cold_insect"] == 0
    and ra["counts"]["flags"]["burst_coincident"] == 10,
    (rc, ra["counts"]["flags"]),
)
dsp_dir, _f, _l = fresh("nodsp", with_dsp=False)
rc, _o, _e = run(["--data-dir", dsp_dir, "--out", os.path.join(tmp, "out_nodsp")])
rd = load(os.path.join(tmp, "out_nodsp"))
check(
    "missing dsp: exit 1, dsp_missing, no burst flags, cold_insect still 9 (temperature rule unaffected)",
    rc == 1
    and rd["coverage"]["degraded_reasons"] == ["dsp_missing"]
    and rd["counts"]["flags"]["burst_coincident"] == 0
    and rd["counts"]["flags"]["cold_insect"] == 9,
    (rc, rd["counts"]["flags"]),
)
short_dir, _f, _l = fresh("shortdsp", dsp_to_hour=10)
rc, _o, _e = run(["--data-dir", short_dir, "--out", os.path.join(tmp, "out_short")])
rsd = load(os.path.join(tmp, "out_short"))
check(
    "transient history that ends before the reports: exit 1, transients_do_not_cover_reports, transients_cover_reports false",
    rc == 1 and rsd["coverage"]["degraded_reasons"] == ["transients_do_not_cover_reports"] and rsd["coverage"]["transients_cover_reports"] is False,
    (rc, rsd["coverage"]["degraded_reasons"]),
)
empty = os.path.join(tmp, "empty")
os.makedirs(empty)
rc, _o, _e = run(["--data-dir", empty, "--out", os.path.join(tmp, "out_empty")])
re_ = load(os.path.join(tmp, "out_empty"))
check(
    "an empty data dir is degraded (ambient_missing, birds_missing, dsp_missing), exit 1, no exception",
    rc == 1 and re_["coverage"]["degraded_reasons"] == ["ambient_missing", "birds_missing", "dsp_missing"],
    (rc, re_["coverage"]["degraded_reasons"]),
)
rc, _o, se = run(["--data-dir", os.path.join(tmp, "nonexistent"), "--out", os.path.join(tmp, "o9")])
check("a missing --data-dir exits 2", rc == 2 and "--data-dir must be an existing directory" in se, (rc, se))
rc, so, _e = run(["--data-dir", data, "--out", os.path.join(tmp, "out_json"), "--json"])
check(
    "--json prints exactly the written report on stdout", rc == 0 and so == open(os.path.join(tmp, "out_json", "artifact_replay.json"), encoding="utf-8").read()
)


def mini(name, rows, temp):
    d = os.path.join(tmp, name)
    os.makedirs(os.path.join(d, "analysis"))
    os.makedirs(os.path.join(d, "history"))
    with open(os.path.join(d, "analysis", "birds.jsonl"), "w", encoding="utf-8", newline="\n") as f:
        f.write("".join(json.dumps(x) + "\n" for x in rows))
    with open(os.path.join(d, "history", "ambient_temp.jsonl"), "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"k": "ambient_temp", "v": 1, "t_hour_utc": 0, "temp_c": temp, "source": "open-meteo", "model_grid_km": None}) + "\n")
    return d


def katydid(t):
    return {"k": "bird", "t": t, "common": "Common Virtuoso Katydid", "scientific": "Neoconocephalus triops"}


# bin edges use the raw temperature, like the flag (4.96 rounds to 5.0 in the evidence but is below the 5.0 floor)
for temp, expect_bin, expect_flag in ((4.96, "lt5", 1), (5.0, "5to15", 0), (14.96, "5to15", 0), (15.0, "ge15", 0)):
    d = mini("edge%s" % temp, [katydid(10.0)], temp)
    outd = os.path.join(tmp, "oe%s" % temp)
    run(["--data-dir", d, "--out", outd])
    re_ = load(outd)
    got = {b: v["flagged"] + v["unflagged"] for b, v in re_["counts"]["by_group"]["insect"].items()}
    check(
        "temperature %s C: the report is in bin %s (raw value), cold_insect=%d, no other bin counted" % (temp, expect_bin, expect_flag),
        got == {b: (1 if b == expect_bin else 0) for b in BINS_} and re_["counts"]["flags"].get("cold_insect", 0) == expect_flag,
        (got, re_["counts"]["flags"]),
    )
    check(
        "temperature %s C: the sweep at floor 5 puts the report in the same bin with cold=%d" % (temp, expect_flag),
        re_["floor_sweep"]["5"]["by_bin"][expect_bin] == {"n": 1, "cold": expect_flag, "any": expect_flag},
        re_["floor_sweep"]["5"]["by_bin"],
    )

# a finite but absurd report time is a skipped hostile row: no crash, the valid row still counted
for bad in (1e20, -1e20, 5e9):
    d = mini("hostile%s" % bad, [katydid(10.0), katydid(bad)], 2.0)
    outd = os.path.join(tmp, "oh%s" % bad)
    rc, _o, se = run(["--data-dir", d, "--out", outd])
    rh = load(outd)
    check(
        "report time %g: no traceback, skipped.birds == 1, the one valid row counted (n_rows 1)" % bad,
        "Traceback" not in se and rh["coverage"]["skipped_lines"]["birds"] == 1 and rh["coverage"]["reports"]["n_rows"] == 1,
        (rc, se[-200:], rh["coverage"]["skipped_lines"]),
    )
d = mini("tedge", [katydid(0.0), katydid(4e9)], 2.0)
run(["--data-dir", d, "--out", os.path.join(tmp, "oedge")])
rt = load(os.path.join(tmp, "oedge"))
check(
    "positive twin: report times 0 and 4e9 are accepted (n_rows 2, skipped.birds 0)",
    rt["coverage"]["reports"]["n_rows"] == 2 and rt["coverage"]["skipped_lines"]["birds"] == 0,
    rt["coverage"],
)

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d checks, %d failed" % (n_checks, len(fails)))
for f in fails:
    print("FAILED: " + f)
sys.exit(1 if fails else 0)
