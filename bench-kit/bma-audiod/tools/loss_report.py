#!/usr/bin/env python3
"""Read-only loss report over history/loss.jsonl (+ .1) (contract: loss-log). Standard library only.

  loss_report.py [--root DIR | --file PATH] [--source bridge|sink|analyzer] [--hours N]

Per UTC hour: records, nulls (records whose deltas for the source are null), datagrams, gaps, gap events % (100 x gaps / datagrams:
gap events per datagram, NOT packets lost), loss % (the source's loss_pct gauge, packets lost, weighted by each record's datagrams;
bridge only), worst window (the record with the most gaps: its UTC time, gaps, span_s). Writes nothing.
"""
import argparse, json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(1, os.path.join(HERE, "..", "pc"))
from history_log import DATA_DIR  # noqa: E402

SOURCES = ("bridge", "sink", "analyzer")


def load(path):
    """Records of path.1 then path; lines that do not parse or are not `loss` v1 objects are skipped."""
    out = []
    for p in (path + ".1", path):
        try:
            with open(p, "rb") as f:
                lines = f.read().split(b"\n")
        except OSError:
            continue
        for l in lines:
            try:
                r = json.loads(l.decode("utf-8"))
            except (ValueError, UnicodeDecodeError, RecursionError):
                continue
            if isinstance(r, dict) and r.get("k") == "loss" and r.get("v") == 1 and isinstance(r.get("t"), (int, float)):
                out.append(r)
    return out


def hours(recs, source):
    """-> list of dicts, one per UTC hour that has records, oldest first."""
    by = {}
    for r in recs:
        h = int(r["t"] // 3600) * 3600
        e = by.setdefault(h, {"hour": h, "records": 0, "nulls": 0, "datagrams": 0, "gaps": 0, "worst": None, "lw": 0.0, "ld": 0})
        e["records"] += 1
        s = r.get(source)
        if not isinstance(s, dict) or s.get("datagrams") is None:
            e["nulls"] += 1
            continue
        e["datagrams"] += s["datagrams"]
        lp = s.get("loss_pct")
        if isinstance(lp, (int, float)) and not isinstance(lp, bool):
            e["lw"] += lp * s["datagrams"]
            e["ld"] += s["datagrams"]
        g = s.get("gaps")
        if g is not None:
            e["gaps"] += g
            if e["worst"] is None or g > e["worst"]["gaps"]:
                e["worst"] = {"t": r["t"], "gaps": g, "span_s": s.get("span_s")}
    for e in by.values():
        e["gap_pct"] = (100.0 * e["gaps"] / e["datagrams"]) if e["datagrams"] else None
        e["loss_pct"] = (e["lw"] / e["ld"]) if e["ld"] else None
        del e["lw"], e["ld"]
    return [by[h] for h in sorted(by)]


def fmt_t(t):
    return time.strftime("%H:%M:%S", time.gmtime(t))


def table(rows):
    head = "%-16s %7s %5s %11s %9s %12s %8s  %s" % ("hour (UTC)", "records", "nulls", "datagrams", "gaps", "gap events %", "loss %", "worst window")
    lines = [head]
    for e in rows:
        w = e["worst"]
        worst = "-" if w is None else "%s  %d gaps in %s s" % (fmt_t(w["t"]), w["gaps"], "?" if w["span_s"] is None else "%.1f" % w["span_s"])
        pct = "-" if e["gap_pct"] is None else "%.4f" % e["gap_pct"]
        lpct = "-" if e["loss_pct"] is None else "%.4f" % e["loss_pct"]
        lines.append("%-16s %7d %5d %11d %9d %12s %8s  %s" % (time.strftime("%Y-%m-%d %H:00", time.gmtime(e["hour"])), e["records"], e["nulls"],
                                                        e["datagrams"], e["gaps"], pct, lpct, worst))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=DATA_DIR)
    ap.add_argument("--file", default=None, help="loss.jsonl (default <root>/history/loss.jsonl)")
    ap.add_argument("--source", choices=SOURCES, default="bridge")
    ap.add_argument("--hours", type=int, default=0, help="only the last N hours that have records (0 = all)")
    a = ap.parse_args(argv)
    path = a.file or os.path.join(a.root, "history", "loss.jsonl")
    rows = hours(load(path), a.source)
    if a.hours > 0:
        rows = rows[-a.hours:]
    if not rows:
        print("no loss records")
        return 0
    print(table(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
