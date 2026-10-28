#!/usr/bin/env python3
"""daily_digest: terse plain-ASCII text digest of one local day from the existing logs.

    daily_digest.py --root DIR [--day YYYYMMDD] [--out DIR] [--top 5]

Reads (never writes) <root>/analysis/birds.jsonl, perch.jsonl, status.json, clips/index.jsonl, history/loss.jsonl, bridge_status.json
(each with its .1 generation). Writes <out>/<day>.txt, default <root>/reports/digest. --day defaults to yesterday (local).
Lines: detections by group (human labels excluded), top species (artifact share only where artifact_flags exist), loss, clips, perch,
`WARNING ON but Approved=...` for features that are ON while the registry says pending or N (feature_state.py), health.
No coordinates, paths or personal names. Exit 0; 2 usage error."""
import argparse, datetime, json, os, shutil, sys, time, unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
for _d in (HERE, os.path.join(HERE, "..", "analysis")):
    sys.path.insert(0, _d)
import labels as L  # noqa: E402
import perch_report as PR  # noqa: E402
try:
    import feature_state as FSTATE  # noqa: E402
except ImportError:                          # not pinned next to the digest: no feature line
    FSTATE = None

ARTIFACT_LEVELS = ("likely", "suspect")
MARGIN_S = 10.0          # birds rows kept around the day for the perch comparison window


def day_bounds(day):
    """'YYYYMMDD' -> (start, end) epoch seconds of that LOCAL day (end exclusive; a DST day is 23 or 25 h)."""
    d = datetime.datetime.strptime(day, "%Y%m%d")
    return time.mktime(d.timetuple()), time.mktime((d + datetime.timedelta(days=1)).timetuple())


def read_jsonl(path, lo, hi):
    """Rows (dicts) whose `t` or `t_start` lies in [lo, hi); the .1 generation first. Bad lines and absent files are skipped."""
    out = []
    for p in (path + ".1", path):
        try:
            f = open(p, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with f:
            for ln in f:
                try:
                    o = json.loads(ln)
                except ValueError:
                    continue
                if not isinstance(o, dict):
                    continue
                t = o.get("t", o.get("t_start"))
                if L.is_num(t) and lo <= t < hi:
                    out.append(o)
    return out


def ascii_text(s):
    return unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode("ascii").replace("\n", " ").strip()


def hidden(o):
    return PR.hidden(o.get("common")) or PR.hidden(o.get("scientific"))


def detections(rows, top):
    groups, species, any_flags = {}, {}, False
    for o in rows:
        if o.get("k") != "bird" or hidden(o):
            continue
        g = ascii_text(o.get("group") or "unknown") or "unknown"
        groups[g] = groups.get(g, 0) + 1
        key = ascii_text(o.get("scientific") or o.get("common") or "unknown")
        s = species.setdefault(key, {"n": 0, "common": ascii_text(o.get("common") or key), "flagged": 0, "with": 0})
        s["n"] += 1
        af = o.get("artifact_flags")
        if isinstance(af, dict):
            any_flags = True
            s["with"] += 1
            s["flagged"] += 1 if af.get("level") in ARTIFACT_LEVELS else 0
    lines = ["detections %d (%s)" % (sum(groups.values()), ", ".join("%s %d" % kv for kv in sorted(groups.items(), key=lambda kv: (-kv[1], kv[0])))
                                     if groups else "none")]
    for key, s in sorted(species.items(), key=lambda kv: (-kv[1]["n"], kv[0]))[:top]:
        extra = ""
        if any_flags and s["with"]:
            extra = " artifact %d%%" % round(100.0 * s["flagged"] / s["with"])
        lines.append("species %s (%s) n=%d%s" % (s["common"], key, s["n"], extra))
    return lines


def loss_line(root, lo, hi):
    hours = {}
    for o in read_jsonl(os.path.join(root, "history", "loss.jsonl"), lo, hi):
        b = o.get("bridge")
        v = b.get("loss_pct") if isinstance(b, dict) else None
        if o.get("k") == "loss" and L.is_num(v) and not isinstance(v, bool):
            hours.setdefault(time.localtime(o["t"]).tm_hour, []).append(float(v))
    if hours:
        allv = [v for vs in hours.values() for v in vs]
        worst = max(sorted(hours), key=lambda h: sum(hours[h]) / len(hours[h]))
        return "loss mean %.2f pct, worst hour %02d %.2f pct, samples %d" % (sum(allv) / len(allv), worst, sum(hours[worst]) / len(hours[worst]), len(allv))
    try:
        with open(os.path.join(root, "bridge_status.json"), "r", encoding="utf-8") as f:
            v = json.load(f).get("loss_pct")
        if L.is_num(v) and not isinstance(v, bool):
            return "loss now %.2f pct (bridge_status, last interval)" % v
    except (OSError, ValueError, AttributeError):
        pass
    return "loss unknown"


def clips_line(root, lo, hi):
    stored = audio = purged = evicted = 0
    for o in read_jsonl(os.path.join(root, "analysis", "clips", "index.jsonl"), lo, hi):
        k = o.get("k")
        if k == "clip_stored":
            stored += 1
            audio += 1 if o.get("audio_stored") is True else 0
        elif k == "clip_evicted":
            if o.get("reason") == "human_voice":
                purged += 1
            else:
                evicted += 1
    return "clips stored %d (audio %d), purged human voice %d, evicted other %d" % (stored, audio, purged, evicted)


def perch_line(root, lo, hi, birds_wide):
    p = os.path.join(root, "analysis", "perch.jsonl")
    if not (os.path.exists(p) or os.path.exists(p + ".1")):
        return None
    rep = PR.build_report(read_jsonl(p, lo, hi), birds_wide, 0.0)
    a = rep["agreement"]
    if not a["compared"]:
        return "perch windows %d, compared 0" % rep["windows"]["total"]
    return "perch windows %d, compared %d, same species %d, agreement %.1f pct" % (rep["windows"]["total"], a["compared"], a["agree"], 100.0 * a["agree"] / a["compared"])


def feature_lines(root, registry=None):
    """the feature_state warnings (a feature ON while its registry row is pending or N), [] when there are none or the tool is not pinned"""
    if FSTATE is None:
        return []
    try:
        rows = FSTATE.collect(root)[1]
        return [ascii_text(x) for x in FSTATE.approval_warnings(rows, FSTATE.load_approved(registry))]
    except Exception:                        # noqa: BLE001  the digest never fails on a feature detector
        return []


def health_line(root, now):
    up, errs = None, None
    try:
        with open(os.path.join(root, "analysis", "status.json"), "r", encoding="utf-8") as f:
            st = json.load(f)
        if L.is_num(st.get("uptime_s")):
            up = st["uptime_s"]
        elif L.is_num(st.get("started")):
            up = max(0.0, now - st["started"])
        pl = st.get("plugins")
        if isinstance(pl, dict):
            errs = sum(p["errors"] for p in pl.values() if isinstance(p, dict) and isinstance(p.get("errors"), int))
    except (OSError, ValueError, AttributeError):
        pass
    try:
        disk = "%.1f" % (shutil.disk_usage(root).free / 1e9)
    except OSError:
        disk = "unknown"
    return "health analyzer_uptime_h %s, plugin_errors %s, disk_free_gb %s" % (
        "unknown" if up is None else "%.1f" % (up / 3600.0), "unknown" if errs is None else errs, disk)


def build(root, day, top=5, now=None, registry=None):
    lo, hi = day_bounds(day)
    now = time.time() if now is None else now
    birds_path = os.path.join(root, "analysis", "birds.jsonl")
    wide = read_jsonl(birds_path, lo - MARGIN_S, hi + MARGIN_S)
    day_rows = [o for o in wide if lo <= o["t"] < hi]
    lines = ["bma digest %s" % day] + detections(day_rows, top) + [loss_line(root, lo, hi), clips_line(root, lo, hi)]
    pl = perch_line(root, lo, hi, wide)
    if pl:
        lines.append(pl)
    lines += feature_lines(root, registry)
    lines.append(health_line(root, now))
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", required=True, help="pipeline data directory (read only apart from --out)")
    ap.add_argument("--day", help="local day YYYYMMDD (default: yesterday)")
    ap.add_argument("--out", help="output directory (default <root>/reports/digest)")
    ap.add_argument("--top", type=int, default=5)
    a = ap.parse_args(argv)
    day = a.day or (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y%m%d")
    try:
        if len(day) != 8:
            raise ValueError
        day_bounds(day)
    except ValueError:
        sys.stderr.write("--day must be YYYYMMDD\n")
        return 2
    if not os.path.isdir(a.root):
        sys.stderr.write("--root is not a directory\n")
        return 2
    text = build(a.root, day, a.top)
    out = a.out or os.path.join(a.root, "reports", "digest")
    os.makedirs(out, exist_ok=True)
    dest = os.path.join(out, day + ".txt")
    with open(dest + ".tmp", "w", encoding="ascii", newline="\n") as f:
        f.write(text)
    os.replace(dest + ".tmp", dest)
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
