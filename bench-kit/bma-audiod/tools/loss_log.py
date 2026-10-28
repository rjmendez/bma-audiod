#!/usr/bin/env python3
"""Persistent loss telemetry (contract: loss-log, contracts/specs/loss-log.md). Standard library only.

  loss_log.py [--root DIR] [--out DIR] [--every-s 30] [--stale-s 60] [--max-file-bytes 2097152] [--once]

Every --every-s reads <root>/bridge_status.json, <root>/sink_status.json, <root>/analysis/status.json and appends ONE `loss`
record to <out>/loss.jsonl (default <root>/history) through the shared writer (pc/history_log.py RotatingJsonl, 2 MB x 2).
Counters are written as exact deltas against the last good reading of the same source; a stale, missing, unreadable or
restarted source is null with a reason. Records carry no audio, path or address.
Exit code: 0; a usage error is 2. A failed tick is one stderr line and the loop continues.
"""
import argparse, json, math, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)                                          # -I (isolated mode) does not add the script directory
sys.path.insert(1, os.path.join(HERE, "..", "pc"))                # in the repo history_log.py lives in pc/; pinned app\ is flat
from history_log import RotatingJsonl, DATA_DIR  # noqa: E402

FILE_NAME = "loss.jsonl"
CAP_BYTES = 2 * 1024 * 1024
MAX_STATUS_BYTES = 1 << 20
STALE_S = 60.0                                                    # 2 x the slowest status writer (sink, bridge: 30 s)
# source -> (status file relative to root, counter name -> path of that counter in the status document, gauge names)
SOURCES = {
    "bridge": ("bridge_status.json", {"datagrams": ("pkts",), "gaps": ("gaps",)}, ("loss_pct", "uptime_s")),
    "sink": ("sink_status.json", {"datagrams": ("datagrams",), "rx_errors": ("rx_errors",)}, ()),
    "analyzer": (os.path.join("analysis", "status.json"),
                 {"datagrams": ("rx", "datagrams"), "bad": ("rx", "bad"), "gaps": ("rx", "gaps")}, ("uptime_s",)),
}


def read_file(path):
    """-> (bytes, mtime). Raises FileNotFoundError (absent) or another OSError (unreadable)."""
    with open(path, "rb") as f:
        raw = f.read(MAX_STATUS_BYTES + 1)
        mtime = os.fstat(f.fileno()).st_mtime
    return raw, mtime


def _pick(doc, path):
    for k in path:
        if not isinstance(doc, dict) or k not in doc:
            return None
        doc = doc[k]
    return doc


def _counter(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _gauge(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


class Sampler:
    def __init__(self, root, stale_s=STALE_S, read=read_file):
        self.root, self.stale_s, self.read = root, float(stale_s), read
        self.prev = {}                      # source -> (host time of the reading, counters dict, uptime_s or None)

    def _source(self, name, now):
        rel, counters, gauges = SOURCES[name]
        out = {"age_s": None, "reason": None, "span_s": None, "restart": False}
        for c in counters:
            out[c] = None
        for g in gauges:
            out[g] = None
        try:
            raw, mtime = self.read(os.path.join(self.root, rel))
        except FileNotFoundError:
            out["reason"] = "missing"
            return out
        except OSError:
            out["reason"] = "unreadable"
            return out
        out["age_s"] = round(max(0.0, now - mtime), 3)
        if out["age_s"] > self.stale_s:
            out["reason"] = "stale"
            return out
        try:
            if len(raw) > MAX_STATUS_BYTES:
                raise ValueError("too large")
            doc = json.loads(raw.decode("utf-8"))
            if not isinstance(doc, dict):
                raise ValueError("not an object")
        except (ValueError, UnicodeDecodeError, RecursionError):
            out["reason"] = "unreadable"
            return out
        cur = {c: _counter(_pick(doc, p)) for c, p in counters.items()}
        if any(v is None for v in cur.values()):
            out["reason"] = "unreadable"
            return out
        for g in gauges:
            out[g] = _gauge(doc.get(g))
        up = out.get("uptime_s")
        prev = self.prev.get(name)
        self.prev[name] = (now, cur, up)
        if prev is None:
            out["reason"] = "first"
            return out
        t0, p_cur, p_up = prev
        if any(cur[c] < p_cur[c] for c in cur) or (up is not None and p_up is not None and up < p_up):
            out["reason"], out["restart"] = "restart", True
            return out
        out["span_s"] = round(now - t0, 3)
        for c in cur:
            out[c] = cur[c] - p_cur[c]
        return out

    def sample(self, now):
        rec = {"k": "loss", "v": 1, "t": round(now, 3), "restart": False}
        for name in SOURCES:
            rec[name] = self._source(name, now)
        rec["restart"] = any(rec[n]["restart"] for n in SOURCES)
        return rec


def run(a, clock=time.time, sleep=time.sleep, mono=time.monotonic):
    out_dir = a.out or os.path.join(a.root, "history")
    w = RotatingJsonl(os.path.join(out_dir, FILE_NAME), a.max_file_bytes)
    s = Sampler(a.root, a.stale_s)
    nxt = mono()
    while True:
        try:
            w.write(s.sample(clock()))
        except Exception as ex:                                    # noqa: BLE001  a bad tick never stops the loop
            print("loss_log: tick error: %s" % type(ex).__name__, file=sys.stderr, flush=True)
        if a.once:
            return 0
        nxt += a.every_s
        sleep(max(0.0, nxt - mono()))
        if nxt < mono() - a.every_s:                                # the machine stalled: do not burst to catch up
            nxt = mono()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=DATA_DIR, help="data dir holding the status files (default $BMA_DATA_DIR or ./data)")
    ap.add_argument("--out", default=None, help="directory of loss.jsonl (default <root>/history)")
    ap.add_argument("--every-s", type=float, default=30.0)
    ap.add_argument("--stale-s", type=float, default=STALE_S, help="a status file older than this is stale")
    ap.add_argument("--max-file-bytes", type=int, default=CAP_BYTES)
    ap.add_argument("--once", action="store_true", help="one record, then exit")
    a = ap.parse_args(argv)
    if not 1 <= a.every_s <= 3600 or a.stale_s <= 0 or a.max_file_bytes < 1:
        ap.error("--every-s must be 1..3600; --stale-s and --max-file-bytes positive")
    try:
        return run(a)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
