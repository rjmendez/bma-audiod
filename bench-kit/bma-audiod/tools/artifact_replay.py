#!/usr/bin/env python3
"""Offline replay of the artifact gate (analysis/artifact_gate.py) over a SNAPSHOT of the pipeline's metadata files.

    python -I artifact_replay.py --data-dir <snapshot> --out <dir> [--since T] [--until T] [--floor-c 5.0] [--sweep 3,5,7,10]
                                 [--taxa PATH] [--timing] [--json]

Run it on a frozen COPY of the live files (the transient history rotates away: dsp.jsonl.1 is the only copy of the older
transients). Reads <data-dir>/analysis/birds.jsonl, analysis/dsp.jsonl(.1), history/ambient_temp.jsonl(.1) and the taxa table
(default ui/taxa.json of this repo); writes ONLY <out>/artifact_replay.json and <out>/artifact_replay.txt, and refuses an --out equal
to or inside <data-dir>/analysis or <data-dir>/history. Never audio, never network.

Exit 0 = valid run, 1 = DEGRADED run (the report is still written, experiment.valid is false: a degraded run is not a result),
2 = usage error. Time arguments: Unix seconds or an ISO date / date-time (UTC)."""
import argparse, calendar, datetime, hashlib, json, math, os, statistics, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "analysis"))
import artifact_gate as G  # noqa: E402
import taxa as T  # noqa: E402

DEFAULT_TAXA = os.path.join(HERE, "..", "ui", "taxa.json")
MAX_LINE = 1 << 20
BINS = ("lt5", "5to15", "ge15", "unknown")
TEMP_BINS = (("lt-5", None, -5.0), ("-5to0", -5.0, 0.0), ("0to5", 0.0, 5.0), ("5to10", 5.0, 10.0), ("ge10", 10.0, None))
T_MIN, T_MAX = 0.0, 4e9      # a bird report time outside this range is a hostile row: skipped and counted, never dated
COVER_TOL_S = 60.0           # a bird report may sit a few seconds past the last/first dsp record


def temp_bin(temp):
    return "unknown" if temp is None else "lt5" if temp < 5.0 else "5to15" if temp < 15.0 else "ge15"


def fine_bin(temp):
    for name, lo, hi in TEMP_BINS:
        if (lo is None or temp >= lo) and (hi is None or temp < hi):
            return name
    return "unknown"


def parse_time(s):
    try:
        v = float(s)
        if math.isfinite(v):
            return v
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return float(calendar.timegm(datetime.datetime.strptime(s, fmt).timetuple()))
        except ValueError:
            pass
    raise ValueError("time %r is neither Unix seconds nor an ISO UTC date" % s)


class Source:
    """One input file: streams decoded dict lines, counts hostile ones, hashes every byte."""
    def __init__(self, path, name):
        self.path, self.name, self.skipped, self.bytes = path, name, 0, 0
        self.h = hashlib.sha256()

    def rows(self):
        with open(self.path, "rb") as f:
            while True:
                line = f.readline(MAX_LINE + 1)
                if not line:
                    return
                self.bytes += len(line)
                self.h.update(line)
                if len(line) > MAX_LINE and not line.endswith(b"\n"):          # a hostile giant line: drop the rest of it
                    while True:
                        rest = f.readline(MAX_LINE)
                        self.bytes += len(rest)
                        self.h.update(rest)
                        if not rest or rest.endswith(b"\n"):
                            break
                    self.skipped += 1
                    continue
                if not line.strip():
                    continue
                try:
                    o = json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError, RecursionError):
                    self.skipped += 1
                    continue
                if not isinstance(o, dict):
                    self.skipped += 1
                    continue
                yield o

    def info(self):
        return {"name": self.name, "bytes": self.bytes, "sha256": self.h.hexdigest()}


def finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def new_matrix():
    return {b: {"flagged": 0, "unflagged": 0} for b in BINS}


def run(data_dir, out_dir, since=None, until=None, floor_c=5.0, sweep=(3.0, 5.0, 7.0, 10.0), taxa_path=None, timing=False):
    """-> report dict (see contracts/specs/artifact-gate.md). Reads only; the caller writes."""
    t_wall, t_cpu = time.time(), time.process_time()
    analysis, history = os.path.join(data_dir, "analysis"), os.path.join(data_dir, "history")
    cfg = G.GateConfig(floors_c=(("insect", float(floor_c)),))
    reasons, inputs, skipped = [], [], {"birds": 0, "dsp": 0, "ambient": 0}

    # --- ambient
    amb_recs = []
    for fn in ("ambient_temp.jsonl.1", "ambient_temp.jsonl"):
        p = os.path.join(history, fn)
        if os.path.isfile(p):
            s = Source(p, "history/" + fn)
            amb_recs.extend(s.rows())
            skipped["ambient"] += s.skipped
            inputs.append(s.info())
    ambient = G.AmbientIndex(amb_recs)
    skipped["ambient"] += ambient.skipped
    a_lo, a_hi = ambient.span()
    if len(ambient) == 0:
        reasons.append("ambient_missing")

    # --- dsp: transients, plus the hours that have any record at all (the denominator of the per-hour table)
    pairs, dsp_hours, dsp_lo, dsp_hi, n_dsp_files = [], {}, None, None, 0
    for fn in ("dsp.jsonl.1", "dsp.jsonl"):
        p = os.path.join(analysis, fn)
        if os.path.isfile(p):
            n_dsp_files += 1
            s = Source(p, "analysis/" + fn)
            for o in s.rows():
                t = o.get("t")
                if not finite(t):
                    skipped["dsp"] += 1
                    continue
                dsp_lo = t if dsp_lo is None else min(dsp_lo, t)
                dsp_hi = t if dsp_hi is None else max(dsp_hi, t)
                hr = int(math.floor(t / 3600.0) * 3600)
                dsp_hours.setdefault(hr, 0)
                if o.get("k") == "transient":
                    sp = o.get("beam_spread_db")
                    if not finite(sp):
                        skipped["dsp"] += 1
                        continue
                    pairs.append((float(t), float(sp)))
                    dsp_hours[hr] += 1
            skipped["dsp"] += s.skipped
            inputs.append(s.info())
    transients = G.TransientIndex(pairs) if n_dsp_files else None
    if transients is not None:
        skipped["dsp"] += transients.skipped
    if transients is None or dsp_lo is None:
        reasons.append("dsp_missing")

    # --- taxa
    taxa = T.Taxa(taxa_path or DEFAULT_TAXA)
    if not taxa.ok:
        reasons.append("taxa_missing")

    # --- birds
    counts_group, counts_label = {}, {}
    flags_n, levels_n = {r: 0 for r in G.REASONS}, {l: 0 for l in G.LEVELS}
    flagged_by_group, oos = {}, {}
    conf = {"cold_burst": 0, "cold_noburst": 0, "warm_burst": 0, "warm_noburst": 0, "unknown_burst": 0, "unknown_noburst": 0}
    by_hour = {"flagged": [0] * 24, "unflagged": [0] * 24}
    by_date = {}
    sweep_f = [float(x) for x in sweep]
    sweep_data = {("%g" % f): {"by_bin": {b: {"n": 0, "cold": 0, "any": 0} for b in BINS}, "by_species": {}} for f in sweep_f}
    burst_by_bin = {b: {"n": 0, "burst": 0} for b in BINS}
    n_rows = n_window = hidden = 0
    b_lo = b_hi = None
    birds_path = os.path.join(analysis, "birds.jsonl")
    if os.path.isfile(birds_path):
        s = Source(birds_path, "analysis/birds.jsonl")
        for o in s.rows():
            if o.get("k") != "bird":
                continue
            common, sci = o.get("common"), o.get("scientific")
            if G.is_hidden_label(common, sci):                   # policy: dropped before any counter, timestamp or range is touched
                hidden += 1
                continue
            t = o.get("t")
            if not finite(t) or not (T_MIN <= t <= T_MAX) or not isinstance(common if isinstance(common, str) and common else sci, str):
                s.skipped += 1
                continue
            n_rows += 1
            if (since is not None and t < since) or (until is not None and t >= until):
                continue
            n_window += 1
            b_lo = t if b_lo is None else min(b_lo, t)
            b_hi = t if b_hi is None else max(b_hi, t)
            label = common if isinstance(common, str) and common else sci
            group, _ns = taxa.lookup(sci if isinstance(sci, str) else None, common if isinstance(common, str) else None)
            v = G.evaluate(group=group, label=label, scientific=sci, t=t, ambient=ambient, transients=transients, cfg=cfg)
            gkey = group if group is not None else "none"
            if v is None:
                oos[label] = oos.get(label, 0) + 1
                flagged_by_group.setdefault(gkey, 0)
                continue
            flagged = v["level"] != "none"
            temp = v["evidence"]["temp_c"]
            raw_temp = ambient.temp_at(t, cfg.temp_max_age_s)[0] if temp is not None else None   # the bin and the sweep use the raw value, like the flag
            b = temp_bin(raw_temp)
            for key, bucket in ((gkey, counts_group), (label, counts_label)):
                bucket.setdefault(key, new_matrix())[b]["flagged" if flagged else "unflagged"] += 1
            flagged_by_group[gkey] = flagged_by_group.get(gkey, 0) + (1 if flagged else 0)
            for fl in v["flags"]:
                flags_n[fl] += 1
            levels_n[v["level"]] += 1
            if group not in cfg.scope_groups:
                continue
            hr = int(t // 3600 % 24)
            by_hour["flagged" if flagged else "unflagged"][hr] += 1
            d = datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%d")
            e = by_date.setdefault(d, {"flagged": [0] * 24, "unflagged": [0] * 24})
            e["flagged" if flagged else "unflagged"][hr] += 1
            burst = "burst_coincident" in v["flags"]
            temp_class = "unknown" if temp is None else "cold" if "cold_insect" in v["flags"] else "warm"
            conf[temp_class + ("_burst" if burst else "_noburst")] += 1
            burst_by_bin[b]["n"] += 1
            burst_by_bin[b]["burst"] += 1 if burst else 0
            if temp is not None:
                for f in sweep_f:
                    cold_f = raw_temp < f
                    sd = sweep_data["%g" % f]
                    for tgt in (sd["by_bin"][b], sd["by_species"].setdefault(label, {x: {"n": 0, "cold": 0, "any": 0} for x in BINS})[b]):
                        tgt["n"] += 1
                        tgt["cold"] += 1 if cold_f else 0
                        tgt["any"] += 1 if (cold_f or burst) else 0
        skipped["birds"] += s.skipped
        inputs.append(s.info())
    else:
        reasons.append("birds_missing")
    if n_window == 0 and "birds_missing" not in reasons:
        reasons.append("no_reports_in_window")

    # --- coverage
    cover = None
    if transients is not None and dsp_lo is not None and b_lo is not None:
        cover = dsp_lo <= b_lo + COVER_TOL_S and dsp_hi >= b_hi - COVER_TOL_S
        if not cover:
            reasons.append("transients_do_not_cover_reports")
    amb_cover = None
    if len(ambient) and b_lo is not None:
        amb_cover = a_lo <= math.floor(b_lo / 3600.0) * 3600 and a_hi + 3600 + cfg.temp_max_age_s >= b_hi
    for g in list(flagged_by_group):
        if g not in cfg.scope_groups and flagged_by_group[g]:
            reasons.append("scope_violation")
    reasons = sorted(set(reasons))

    # --- transient rate by temperature (per hour seen in dsp; hours with no dsp record at all are not counted as zeros)
    hours, rate = [], {n: [] for n, _l, _h in TEMP_BINS}
    for hr in sorted(dsp_hours):
        tc = ambient.temp_at(hr, cfg.temp_max_age_s)[0] if len(ambient) else None
        hours.append({"t_hour_utc": hr, "n": dsp_hours[hr], "temp_c": G._r1(tc)})
        if tc is not None:
            rate[fine_bin(tc)].append(dsp_hours[hr])
    rate_out = {n: ({"hours": len(v), "median_per_hour": statistics.median(v), "max_per_hour": max(v)} if v else {"hours": 0, "median_per_hour": None, "max_per_hour": None})
                for n, v in rate.items()}

    def span(lo, hi):
        return {"t_min": None if lo is None else int(lo), "t_max": None if hi is None else int(hi)}

    report = {
        "k": "artifact_replay", "v": 1,
        "params": {"since": None if since is None else int(since), "until": None if until is None else int(until), "floor_c": float(floor_c),
                   "window_s": cfg.burst_window_s, "burst_min_transients": cfg.burst_min_transients,
                   "burst_max_spread_db": cfg.burst_max_spread_db, "sweep_c": sweep_f},
        "experiment": {"valid": not reasons},
        "coverage": {
            "inputs": sorted(inputs, key=lambda i: i["name"]),
            "reports": dict(span(b_lo, b_hi), n_rows=n_rows, n_in_window=n_window),
            "transients": dict(span(dsp_lo, dsp_hi), n=0 if transients is None else len(transients)),
            "ambient": dict(span(a_lo, a_hi), n=len(ambient)),
            "taxa": {"ok": taxa.ok},
            "skipped_lines": skipped,
            "transients_cover_reports": cover, "ambient_covers_reports": amb_cover,
            "degraded": bool(reasons), "degraded_reasons": reasons,
        },
        "counts": {"by_group": counts_group, "by_label": counts_label, "flags": flags_n, "levels": levels_n},
        "confusion": conf,
        "flagged_by_hour": {"utc_hour": by_hour, "by_date": by_date, "transient_rate_by_temp_bin": rate_out},
        "transient_hours": hours,
        "burst_by_temp_bin": burst_by_bin,
        "floor_sweep": sweep_data,
        "flagged_by_group": flagged_by_group,
        "out_of_scope_counts": oos,
        "excluded_hidden_reports": hidden,
    }
    if timing:
        report["timing"] = {"wall_s": round(time.time() - t_wall, 3), "cpu_s": round(time.process_time() - t_cpu, 3)}
    return report


def render_text(r):
    c = r["coverage"]
    out = ["artifact gate replay  valid=%s  degraded=%s %s" % (str(r["experiment"]["valid"]).lower(), str(c["degraded"]).lower(), ",".join(c["degraded_reasons"])),
           "floor %.1f C  window %.0f s  burst >= %d transients, spread <= %.1f dB" % (r["params"]["floor_c"], r["params"]["window_s"],
                                                                                    r["params"]["burst_min_transients"], r["params"]["burst_max_spread_db"]),
           "reports in window %d of %d rows; transients %d; ambient hours %d" % (c["reports"]["n_in_window"], c["reports"]["n_rows"], c["transients"]["n"], c["ambient"]["n"]),
           "levels: " + "  ".join("%s=%d" % (k, v) for k, v in sorted(r["counts"]["levels"].items())),
           "flags: " + "  ".join("%s=%d" % (k, v) for k, v in sorted(r["counts"]["flags"].items())),
           "confusion (insect): " + "  ".join("%s=%d" % (k, v) for k, v in sorted(r["confusion"].items())),
           "flagged by group: " + "  ".join("%s=%d" % (k, v) for k, v in sorted(r["flagged_by_group"].items())),
           "excluded_hidden_reports: %d" % r["excluded_hidden_reports"], "", "insect label x temperature (flagged/unflagged)"]
    for lab in sorted(r["counts"]["by_label"]):
        m = r["counts"]["by_label"][lab]
        out.append("  %-34s " % lab + "  ".join("%s %d/%d" % (b, m[b]["flagged"], m[b]["unflagged"]) for b in BINS))
    out += ["", "floor sweep (insect reports with a known temperature: n, flagged by the floor alone, flagged by floor or burst)"]
    for f in sorted(r["floor_sweep"], key=float):
        out.append("  floor %s C  " % f + "  ".join("%s n=%d cold=%d any=%d" % (b, v["n"], v["cold"], v["any"]) for b, v in r["floor_sweep"][f]["by_bin"].items()))
    out += ["", "out-of-scope labels (reports seen, never flagged)"]
    for lab in sorted(r["out_of_scope_counts"]):
        out.append("  %-34s %d" % (lab, r["out_of_scope_counts"][lab]))
    return "\n".join(out) + "\n"


def inside(path, parent):
    p, q = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(parent))
    return p == q or p.startswith(q.rstrip(os.sep) + os.sep)


def write_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="replay the artifact gate over a snapshot of the pipeline metadata")
    ap.add_argument("--data-dir", default=os.environ.get("BMA_DATA_DIR"), help="snapshot of the data dir (default BMA_DATA_DIR); never point it at live files for a result")
    ap.add_argument("--out", required=True, help="output directory (not inside <data-dir>/analysis or <data-dir>/history)")
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    ap.add_argument("--floor-c", type=float, default=5.0)
    ap.add_argument("--sweep", default="3,5,7,10", help="comma separated floors in C for the sensitivity table")
    ap.add_argument("--taxa", default=DEFAULT_TAXA, help="taxa table (default: ui/taxa.json of this repo)")
    ap.add_argument("--timing", action="store_true", help="add wall and CPU seconds to the report (not byte-reproducible)")
    ap.add_argument("--json", action="store_true", help="also print the report JSON on stdout")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    if not a.data_dir or not os.path.isdir(a.data_dir):
        print("artifact_replay: --data-dir must be an existing directory", file=sys.stderr)
        return 2
    for sub in ("analysis", "history"):
        if inside(a.out, os.path.join(a.data_dir, sub)):
            print("artifact_replay: refusing --out inside <data-dir>/%s (the replay writes only into its own output directory)" % sub, file=sys.stderr)
            return 2
    try:
        since = parse_time(a.since) if a.since is not None else None
        until = parse_time(a.until) if a.until is not None else None
        sweep = [float(x) for x in a.sweep.split(",") if x.strip()]
        if not all(math.isfinite(x) for x in sweep) or not math.isfinite(a.floor_c):
            raise ValueError("floors must be finite")
    except ValueError as e:
        print("artifact_replay: %s" % e, file=sys.stderr)
        return 2
    rep = run(a.data_dir, a.out, since, until, a.floor_c, sweep, a.taxa, a.timing)
    body = json.dumps(rep, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    os.makedirs(a.out, exist_ok=True)
    write_atomic(os.path.join(a.out, "artifact_replay.json"), body)
    write_atomic(os.path.join(a.out, "artifact_replay.txt"), render_text(rep).encode("utf-8"))
    if a.json:
        sys.stdout.write(body.decode("utf-8"))
    if rep["coverage"]["degraded"]:
        print("artifact_replay: DEGRADED run (%s): experiment.valid is false" % ",".join(rep["coverage"]["degraded_reasons"]), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
