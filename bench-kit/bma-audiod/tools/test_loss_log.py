#!/usr/bin/env python3
"""Tests for tools/loss_log.py and tools/loss_report.py. File reads go through a recording stub (the Sampler's `read`); the real
files are only used by the subprocess runs, in temp directories. Run:  python -I test_loss_log.py"""

import io, json, os, shutil, subprocess, sys, tempfile, time
from contextlib import redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import loss_log as L  # noqa: E402
import loss_report as R  # noqa: E402

n_checks, fails = 0, []


def check(name, cond, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  [" + str(detail) + "]"))
    if not cond:
        fails.append(name)


ROOT = os.path.join("scratch", "root")
PB, PS, PA = (os.path.join(ROOT, p) for p in ("bridge_status.json", "sink_status.json", os.path.join("analysis", "status.json")))


class Files:
    """The read stub: path -> (bytes, mtime) or an exception instance; records every path asked for."""

    def __init__(self):
        self.d, self.asked = {}, []

    def __call__(self, path):
        self.asked.append(path)
        v = self.d[path]
        if isinstance(v, Exception):
            raise v
        return v

    def put(self, path, doc, mtime):
        self.d[path] = ((doc if isinstance(doc, bytes) else json.dumps(doc).encode()), mtime)


def bridge(pkts, gaps, loss=0.0, up=100.0):
    return {
        "t": 1.0,
        "group": "239.255.1.1:4321",
        "pkts": pkts,
        "pps": 3000.0,
        "loss_pct": loss,
        "gaps": gaps,
        "backwards": 0,
        "bad": 0,
        "uptime_s": up,
        "targets": ["peer.example.invalid:48200"],
    }


def sink(dg, rxe):
    return {"t": 1.0, "device": "CABLE In 16ch (VB-Audio Virtual Cable)", "datagrams": dg, "bad": 0, "rx_errors": rxe, "holes": 5}


def analyzer(dg, bad, gaps, up=50.0):
    return {"started": 1.0, "device": "cuda:1", "rx": {"datagrams": dg, "bad": bad, "gaps": gaps}, "uptime_s": up}


def fresh(f, now, b, s, a):
    f.put(PB, b, now - 1.0)
    f.put(PS, s, now - 2.0)
    f.put(PA, a, now - 0.5)


F = Files()
S = L.Sampler(ROOT, stale_s=20.0, read=F)
fresh(F, 1000.0, bridge(1000, 10, 0.5, 100.0), sink(500, 1), analyzer(480, 2, 8, 50.0))
r1 = S.sample(1000.0)
check("the sampler reads exactly the three status files, in order bridge, sink, analyzer", F.asked == [PB, PS, PA], F.asked)
check(
    "first tick: reason first, every counter and span null, gauges as read, ages exact",
    r1["bridge"] == {"age_s": 1.0, "reason": "first", "span_s": None, "restart": False, "datagrams": None, "gaps": None, "loss_pct": 0.5, "uptime_s": 100.0}
    and r1["sink"] == {"age_s": 2.0, "reason": "first", "span_s": None, "restart": False, "datagrams": None, "rx_errors": None}
    and r1["analyzer"] == {"age_s": 0.5, "reason": "first", "span_s": None, "restart": False, "datagrams": None, "bad": None, "gaps": None, "uptime_s": 50.0}
    and r1["restart"] is False
    and r1["t"] == 1000.0
    and r1["k"] == "loss"
    and r1["v"] == 1,
    r1,
)
fresh(F, 1030.0, bridge(10000, 13, 0.02, 130.0), sink(5000, 1), analyzer(4980, 2, 10, 80.0))
r2 = S.sample(1030.0)
check(
    "second tick: exact deltas, not cumulative values; span_s 30.0; gauges as read",
    r2["bridge"] == {"age_s": 1.0, "reason": None, "span_s": 30.0, "restart": False, "datagrams": 9000, "gaps": 3, "loss_pct": 0.02, "uptime_s": 130.0}
    and r2["sink"] == {"age_s": 2.0, "reason": None, "span_s": 30.0, "restart": False, "datagrams": 4500, "rx_errors": 0}
    and r2["analyzer"] == {"age_s": 0.5, "reason": None, "span_s": 30.0, "restart": False, "datagrams": 4500, "bad": 0, "gaps": 2, "uptime_s": 80.0}
    and r2["restart"] is False,
    r2,
)
text = json.dumps(r1) + json.dumps(r2)
check(
    "no leak: no path, device, group, address or host name in a record",
    not any(x in text for x in ("scratch", "CABLE", "239.255", "peer.example.invalid", "cuda", "targets", "device", "group")),
    text[:200],
)

# counters backwards -> restart
fresh(F, 1060.0, bridge(500, 0, 0.0, 15.0), sink(9500, 1), analyzer(9480, 2, 12, 110.0))
r3 = S.sample(1060.0)
check(
    "bridge counters went backwards: bridge restart true with null counters and span, top-level restart true, other sources keep exact deltas",
    r3["bridge"]["reason"] == "restart"
    and r3["bridge"]["restart"] is True
    and r3["bridge"]["datagrams"] is None
    and r3["bridge"]["gaps"] is None
    and r3["bridge"]["span_s"] is None
    and r3["restart"] is True
    and r3["sink"]["datagrams"] == 4500
    and r3["analyzer"]["gaps"] == 2
    and r3["sink"]["restart"] is False
    and r3["analyzer"]["restart"] is False,
    r3,
)
fresh(F, 1090.0, bridge(3500, 1, 0.0, 45.0), sink(14000, 1), analyzer(13980, 2, 12, 140.0))
r4 = S.sample(1090.0)
check(
    "positive twin: the tick after a restart is an exact delta from the new baseline (3500 - 500), restart false",
    r4["bridge"]["datagrams"] == 3000
    and r4["bridge"]["gaps"] == 1
    and r4["bridge"]["restart"] is False
    and r4["bridge"]["reason"] is None
    and r4["restart"] is False,
    r4,
)
fresh(F, 1120.0, bridge(6500, 1, 0.0, 10.0), sink(18500, 1), analyzer(18480, 2, 12, 170.0))
r5 = S.sample(1120.0)
check(
    "uptime_s going backwards with rising counters is a restart too (counters not guessed)",
    r5["bridge"]["reason"] == "restart" and r5["bridge"]["datagrams"] is None and r5["restart"] is True,
    r5,
)
fresh(F, 1150.0, bridge(9500, 1, 0.0, 40.0), sink(5, 0), analyzer(18490, 2, 12, 200.0))
r6 = S.sample(1150.0)
check(
    "sink counter backwards (no uptime on the sink) is a restart of the sink only",
    r6["sink"]["reason"] == "restart" and r6["sink"]["restart"] is True and r6["bridge"]["restart"] is False and r6["analyzer"]["datagrams"] == 10,
    r6,
)

# freshness
# the writers (sink, bridge) refresh their status every 30 s: with the default threshold a file written 30 s ago is fresh
F30 = Files()
S30 = L.Sampler(ROOT, read=F30)
fresh(F30, 1000.0, bridge(1000, 10), sink(500, 1), analyzer(480, 2, 8))
S30.sample(1000.0)
F30.put(PS, sink(900, 1), 1000.0)
F30.put(PB, bridge(2000, 12), 1000.0)
r30 = S30.sample(1030.0)
check(
    "default --stale-s (60): sink and bridge status files written 30 s ago (the writer cadence) are fresh with exact deltas (sink 400, bridge 1000)",
    L.STALE_S == 60.0
    and r30["sink"]["reason"] is None
    and r30["sink"]["datagrams"] == 400
    and r30["bridge"]["reason"] is None
    and r30["bridge"]["datagrams"] == 1000
    and r30["sink"]["age_s"] == 30.0,
    r30,
)
F30b = Files()
S30b = L.Sampler(ROOT, stale_s=20.0, read=F30b)
fresh(F30b, 1000.0, bridge(1000, 10), sink(500, 1), analyzer(480, 2, 8))
S30b.sample(1000.0)
F30b.put(PS, sink(900, 1), 1000.0)
F30b.put(PB, bridge(2000, 12), 1000.0)
r30b = S30b.sample(1030.0)
check(
    "positive twin: the old 20 s threshold marks the same 30 s old files stale with null deltas",
    r30b["sink"]["reason"] == "stale" and r30b["sink"]["datagrams"] is None and r30b["bridge"]["reason"] == "stale",
    r30b,
)
F60 = Files()
S60 = L.Sampler(ROOT, read=F60)
fresh(F60, 1000.0, bridge(1, 0), sink(1, 0), analyzer(1, 0, 0))
S60.sample(1000.0)
F60.put(PS, sink(2, 0), 1000.0)
F60.put(PB, bridge(2, 0), 999.999)
r60 = S60.sample(1060.0)
check(
    "default threshold edge: age exactly 60.0 is fresh (sink), 60.001 is stale (bridge)",
    r60["sink"]["reason"] is None and r60["bridge"]["reason"] == "stale",
    r60,
)

F2 = Files()
S2 = L.Sampler(ROOT, stale_s=20.0, read=F2)
fresh(F2, 2000.0, bridge(100, 0), sink(100, 0), analyzer(100, 0, 0))
S2.sample(2000.0)
F2.put(PB, bridge(400, 4), 2030.0 - 20.0)
F2.put(PS, sink(400, 0), 2030.0 - 20.001)
F2.put(PA, analyzer(400, 0, 0), 2030.0)
r = S2.sample(2030.0)
check(
    "file age exactly --stale-s (20.0) is fresh; 20.001 is stale (positive twin pair)",
    r["bridge"]["reason"] is None
    and r["bridge"]["age_s"] == 20.0
    and r["bridge"]["datagrams"] == 300
    and r["sink"]["reason"] == "stale"
    and r["sink"]["age_s"] == 20.001
    and r["sink"]["datagrams"] is None
    and r["sink"]["rx_errors"] is None
    and r["sink"]["span_s"] is None
    and r["analyzer"]["reason"] is None,
    r,
)
F2.put(PS, sink(700, 2), 2060.0)
r = S2.sample(2060.0)
check(
    "a stale tick does not move the baseline: the next good reading spans the gap (delta 700 - 100 over span 60.0)",
    r["sink"]["reason"] is None and r["sink"]["datagrams"] == 600 and r["sink"]["rx_errors"] == 2 and r["sink"]["span_s"] == 60.0,
    r["sink"],
)
F2.d[PA] = FileNotFoundError()
F2.put(PB, bridge(700, 4), 2090.0)
F2.put(PS, sink(800, 2), 2090.0)
r = S2.sample(2090.0)
check(
    "a missing file: reason missing, age null, no exception, the others unaffected",
    r["analyzer"] == {"age_s": None, "reason": "missing", "span_s": None, "restart": False, "datagrams": None, "bad": None, "gaps": None, "uptime_s": None}
    and r["bridge"]["datagrams"] == 300
    and r["sink"]["datagrams"] == 100,
    r,
)
F2.d[PA] = PermissionError("denied")
r = S2.sample(2090.0)
check("another OSError on read: reason unreadable, age null", r["analyzer"]["reason"] == "unreadable" and r["analyzer"]["age_s"] is None, r["analyzer"])
F5 = Files()
F5.put(PS, sink(1, 0), 500.0)
check("a file dated in the future has age 0.0, not negative", L.Sampler(ROOT, read=F5)._source("sink", 100.0)["age_s"] == 0.0)

BAD = [
    ("garbage JSON", b'{"rx": {"datagrams": '),
    ("JSON array", b"[1, 2]"),
    ("JSON null", b"null"),
    ("invalid UTF-8", b"\xff\xfe{}"),
    ("empty file", b""),
    ("deep nesting", b"[" * 100000 + b"]" * 100000),
    ("over 1 MiB", json.dumps(bridge(5, 1)).encode() + b" " * (1 << 20)),
    ("counter missing", json.dumps({"pkts": 5}).encode()),
    ("counter float", json.dumps(dict(bridge(5, 1), pkts=5.0)).encode()),
    ("counter negative", json.dumps(dict(bridge(5, 1), gaps=-1)).encode()),
    ("counter bool", json.dumps(dict(bridge(5, 1), gaps=True)).encode()),
    ("counter string", json.dumps(dict(bridge(5, 1), gaps="1")).encode()),
]
for label, raw in BAD:
    F3 = Files()
    S3 = L.Sampler(ROOT, read=F3)
    F3.put(PB, bridge(10, 1), 100.0)
    F3.put(PS, sink(1, 0), 100.0)
    F3.put(PA, analyzer(1, 0, 0), 100.0)
    S3.sample(100.0)
    F3.put(PB, raw, 130.0)
    F3.put(PS, sink(11, 0), 130.0)
    F3.put(PA, analyzer(11, 0, 0), 130.0)
    r = S3.sample(130.0)
    check(
        "bridge status " + label + ": unreadable with null counters and gauges, no exception; sink delta still exact (10)",
        r["bridge"]["reason"] == "unreadable"
        and r["bridge"]["datagrams"] is None
        and r["bridge"]["gaps"] is None
        and r["bridge"]["loss_pct"] is None
        and r["sink"]["datagrams"] == 10,
        r["bridge"],
    )
    F3.put(PB, bridge(40, 2), 160.0)
    r = S3.sample(160.0)
    check(
        "  positive twin after " + label + ": the next valid file is an exact delta against the last good reading (40 - 10, span 60.0)",
        r["bridge"]["reason"] is None and r["bridge"]["datagrams"] == 30 and r["bridge"]["gaps"] == 1 and r["bridge"]["span_s"] == 60.0,
        r["bridge"],
    )
F4 = Files()
S4 = L.Sampler(ROOT, read=F4)
F4.put(PB, dict(bridge(10, 1), loss_pct="x", uptime_s=float("nan")), 100.0)
F4.put(PS, sink(1, 0), 100.0)
F4.put(PA, analyzer(1, 0, 0), 100.0)
r = S4.sample(100.0)
check(
    "a non-numeric loss_pct and a NaN uptime_s are null gauges, the source stays readable (reason first)",
    r["bridge"]["loss_pct"] is None and r["bridge"]["uptime_s"] is None and r["bridge"]["reason"] == "first",
    r["bridge"],
)

tmp = tempfile.mkdtemp(prefix="losslog_")
try:

    class Args:
        root, out, every_s, stale_s, max_file_bytes, once = os.path.join(tmp, "r"), os.path.join(tmp, "o"), 30.0, 20.0, L.CAP_BYTES, False

    class Stop(Exception):
        pass

    class FakeSampler:
        made, calls = [], []

        def __init__(self, root, stale_s):
            FakeSampler.made.append((root, stale_s))

        def sample(self, now):
            FakeSampler.calls.append(now)
            if len(FakeSampler.calls) == 1:
                raise ValueError("boom /secret/path")
            return {"k": "loss", "v": 1, "t": now}

    real = L.Sampler
    L.Sampler = FakeSampler
    sleeps, clocks = [], iter([10.0, 40.0, 70.0, 100.0])
    monos = iter([100.0, 105.0, 105.0, 140.0, 140.0, 400.0, 400.0, 400.0, 410.0])

    def sleep(s):
        sleeps.append(s)
        if len(sleeps) == 4:
            raise Stop()

    err = io.StringIO()
    try:
        with redirect_stderr(err):
            L.run(Args, clock=lambda: next(clocks), sleep=sleep, mono=lambda: next(monos))
    except Stop:
        pass
    finally:
        L.Sampler = real
    written = [json.loads(l) for l in open(os.path.join(Args.out, "loss.jsonl"), encoding="utf-8")]
    check(
        "loop: a failing tick prints one stderr line with only the exception type (no message, no path) and the loop goes on",
        err.getvalue() == "loss_log: tick error: ValueError\n" and FakeSampler.calls == [10.0, 40.0, 70.0, 100.0],
        (err.getvalue(), FakeSampler.calls),
    )
    check(
        "loop: positive twin, the ticks after the failure are written (t 40, 70, 100), the failed one is not",
        [w["t"] for w in written] == [40.0, 70.0, 100.0],
        written,
    )
    check(
        "loop: sleeps are the time left to the monotonic schedule (25, 20), 0 when behind, and 20 again after a stall resets the schedule (no catch-up burst)",
        sleeps == [25.0, 20.0, 0.0, 20.0],
        sleeps,
    )
    check("loop: the sampler is built from the root and --stale-s", FakeSampler.made == [(Args.root, 20.0)], FakeSampler.made)

    root = os.path.join(tmp, "live")
    os.makedirs(os.path.join(root, "analysis"))
    json.dump(bridge(100, 1), open(os.path.join(root, "bridge_status.json"), "w"))
    json.dump(sink(10, 0), open(os.path.join(root, "sink_status.json"), "w"))
    json.dump(analyzer(10, 0, 0), open(os.path.join(root, "analysis", "status.json"), "w"))
    tool = os.path.join(HERE, "loss_log.py")
    p = subprocess.run([sys.executable, "-I", tool, "--root", root, "--once"], capture_output=True, text=True, timeout=60)
    lines = open(os.path.join(root, "history", "loss.jsonl"), "rb").read().split(b"\n")
    rec0 = json.loads(lines[0])
    check(
        "subprocess --once: exit 0, nothing on stderr, ONE line in <root>/history/loss.jsonl, kind loss, reason first for all three sources",
        p.returncode == 0
        and p.stderr == ""
        and len(lines) == 2
        and lines[1] == b""
        and rec0["k"] == "loss"
        and [rec0[s]["reason"] for s in ("bridge", "sink", "analyzer")] == ["first"] * 3,
        (p.returncode, p.stderr, lines),
    )
    check("subprocess: the record holds neither the root path nor the device name", root.encode() not in lines[0] and b"CABLE" not in lines[0], lines[0][:120])
    p2 = subprocess.run(
        [sys.executable, "-I", tool, "--root", root, "--once", "--out", os.path.join(tmp, "elsewhere")], capture_output=True, text=True, timeout=60
    )
    check("subprocess --out: the file goes to that directory", p2.returncode == 0 and os.path.exists(os.path.join(tmp, "elsewhere", "loss.jsonl")), p2.stderr)
    for f_ in ("bridge_status.json", "sink_status.json"):
        os.utime(os.path.join(root, f_), (time.time() - 30, time.time() - 30))
    p30 = subprocess.run([sys.executable, "-I", tool, "--root", root, "--once", "--out", os.path.join(tmp, "o30")], capture_output=True, text=True, timeout=60)
    rec30 = json.loads(open(os.path.join(tmp, "o30", "loss.jsonl")).readline())
    check(
        "subprocess, no --stale-s: status files 30 s old are read (reason first), not stale",
        p30.returncode == 0 and rec30["bridge"]["reason"] == "first" and rec30["sink"]["reason"] == "first",
        (p30.stderr, rec30),
    )
    p31 = subprocess.run(
        [sys.executable, "-I", tool, "--root", root, "--once", "--stale-s", "20", "--out", os.path.join(tmp, "o31")], capture_output=True, text=True, timeout=60
    )
    rec31 = json.loads(open(os.path.join(tmp, "o31", "loss.jsonl")).readline())
    check("positive twin: --stale-s 20 makes the same files stale", rec31["bridge"]["reason"] == "stale" and rec31["sink"]["reason"] == "stale", rec31)
    p3 = subprocess.run([sys.executable, "-I", tool, "--root", os.path.join(tmp, "nothing"), "--once"], capture_output=True, text=True, timeout=60)
    rec3 = json.loads(open(os.path.join(tmp, "nothing", "history", "loss.jsonl")).readline())
    check(
        "no status files at all: exit 0 and a record with reason missing for all three (never an error)",
        p3.returncode == 0 and [rec3[s]["reason"] for s in ("bridge", "sink", "analyzer")] == ["missing"] * 3 and rec3["restart"] is False,
        (p3.returncode, p3.stderr, rec3),
    )
    for bad in ("0", "3601", "-5"):
        pu = subprocess.run([sys.executable, "-I", tool, "--root", root, "--every-s", bad, "--once"], capture_output=True, text=True, timeout=60)
        check("usage error --every-s " + bad + ": exit 2 naming the flag", pu.returncode == 2 and "--every-s" in pu.stderr, (pu.returncode, pu.stderr[-120:]))
    pu = subprocess.run([sys.executable, "-I", tool, "--root", root, "--every-s", "1", "--once"], capture_output=True, text=True, timeout=60)
    check("positive twin: --every-s 1 is accepted", pu.returncode == 0, pu.stderr)

    rot = os.path.join(tmp, "rot")
    for _ in range(2):
        pr = subprocess.run(
            [sys.executable, "-I", tool, "--root", root, "--out", rot, "--once", "--max-file-bytes", "10"], capture_output=True, text=True, timeout=60
        )
    check(
        "--max-file-bytes: a file over the cap rotates to loss.jsonl.1 through the shared writer",
        pr.returncode == 0 and sorted(os.listdir(rot)) == ["loss.jsonl.1"],
        os.listdir(rot),
    )

    # ---- report
    def rec(t, b=None, k="loss", v=1, loss=None):
        s = {"datagrams": None, "gaps": None, "span_s": None}
        if b is not None:
            s = {"datagrams": b[0], "gaps": b[1], "span_s": 30.0, "loss_pct": loss}
        return {"k": k, "v": v, "t": t, "bridge": s}

    H0 = 1793016000  # 2026-10-26T12:00:00Z
    rows = R.hours([rec(H0 + 10, (9000, 3), loss=0.5), rec(H0 + 40, (9000, 120), loss=2.5), rec(H0 + 70, None), rec(H0 + 3605, (6000, 0), loss=0.0)], "bridge")
    check(
        "report: per-hour records, nulls, datagrams, gaps exact",
        [(e["records"], e["nulls"], e["datagrams"], e["gaps"]) for e in rows] == [(3, 1, 18000, 123), (1, 0, 6000, 0)],
        rows,
    )
    check(
        "report: gap events % = 100 x gaps / datagrams (123 / 18000 = 0.68333), 0.0 for a hour with datagrams and no gaps",
        abs(rows[0]["gap_pct"] - 0.6833333333) < 1e-9 and rows[1]["gap_pct"] == 0.0,
        rows,
    )
    check(
        "report: loss % is the loss_pct gauge weighted by datagrams ((0.5 x 9000 + 2.5 x 9000) / 18000 = 1.5; 0.0 when every gauge is 0.0), not the gap events %",
        rows[0]["loss_pct"] == 1.5 and rows[1]["loss_pct"] == 0.0 and rows[0]["loss_pct"] != rows[0]["gap_pct"],
        rows,
    )
    wt = R.hours([rec(H0, (1000, 0), loss=10.0), rec(H0 + 30, (3000, 0), loss=2.0), rec(H0 + 60, (500, 0), loss=None)], "bridge")
    check(
        "report: weighting is by datagrams ((10 x 1000 + 2 x 3000) / 4000 = 4.0), and a record with a null gauge adds no weight", wt[0]["loss_pct"] == 4.0, wt
    )
    check(
        "report: the worst window is the record with the most gaps (120 at +40 s), span 30.0",
        rows[0]["worst"] == {"t": H0 + 40, "gaps": 120, "span_s": 30.0} and rows[1]["worst"] == {"t": H0 + 3605, "gaps": 0, "span_s": 30.0},
        rows,
    )
    tbl = R.table(rows)
    check(
        "report: table text is exact",
        tbl == "hour (UTC)       records nulls   datagrams      gaps gap events %   loss %  worst window\n"
        "2026-10-26 12:00       3     1       18000       123       0.6833   1.5000  12:00:40  120 gaps in 30.0 s\n"
        "2026-10-26 13:00       1     0        6000         0       0.0000   0.0000  13:00:05  0 gaps in 30.0 s",
        tbl,
    )
    only_null = R.hours([rec(H0, None)], "bridge")
    check(
        "report: an hour whose records are all null has zero datagrams, no gap % and no worst window, printed as dashes",
        only_null[0]["gap_pct"] is None
        and only_null[0]["worst"] is None
        and R.table(only_null).splitlines()[1].endswith("0         0            -        -  -"),
        R.table(only_null),
    )
    lp = os.path.join(tmp, "rep", "loss.jsonl")
    os.makedirs(os.path.dirname(lp))
    open(lp + ".1", "wb").write((json.dumps(rec(H0 + 1, (10, 1))) + "\n").encode())
    open(lp, "wb").write(
        b"not json\n"
        + (json.dumps(rec(H0 + 31, (20, 2))) + "\n").encode()
        + b"[1]\n"
        + (json.dumps(rec(H0 + 41, (1, 1), k="other")) + "\n").encode()
        + (json.dumps(rec(H0 + 42, (1, 1), v=2)) + "\n").encode()
        + b'{"k":"loss","v":1,"t":"x"}\n'
        + json.dumps(rec(H0 + 61, (5, 0))).encode()[:20]
    )
    got = R.load(lp)
    check(
        "report: loads .1 then the file in order; skips garbage, non-objects, other kinds, other versions, a bad t and a torn last line",
        [g["t"] for g in got] == [H0 + 1, H0 + 31],
        [g["t"] for g in got],
    )
    d = os.path.dirname(lp)
    snap = lambda: {f: (os.path.getsize(os.path.join(d, f)), os.stat(os.path.join(d, f)).st_mtime_ns) for f in os.listdir(d)}
    before = snap()
    pr = subprocess.run([sys.executable, "-I", os.path.join(HERE, "loss_report.py"), "--file", lp], capture_output=True, text=True, timeout=60)
    check(
        "report CLI: exit 0, prints the hour (2 records, 30 datagrams, 3 gaps, 10.0000 %) and changes no file",
        pr.returncode == 0 and "2026-10-26 12:00       2     0          30         3      10.0000        -" in pr.stdout and before == snap(),
        (pr.returncode, pr.stdout, pr.stderr),
    )
    pe = subprocess.run(
        [sys.executable, "-I", os.path.join(HERE, "loss_report.py"), "--file", os.path.join(tmp, "none.jsonl")], capture_output=True, text=True, timeout=60
    )
    check("report CLI: no file prints `no loss records`, exit 0", pe.returncode == 0 and pe.stdout == "no loss records\n", pe.stdout)
    rows2 = R.hours([rec(H0, (1, 0)), rec(H0 + 3600, (2, 0)), rec(H0 + 7200, (3, 0))], "bridge")
    open(lp, "wb").write(b"".join((json.dumps(rec(H0 + 3600 * i, (1, 0))) + "\n").encode() for i in range(3)))
    pl = subprocess.run([sys.executable, "-I", os.path.join(HERE, "loss_report.py"), "--file", lp, "--hours", "2"], capture_output=True, text=True, timeout=60)
    check(
        "report CLI: --hours 2 keeps the newest two hours (13:00, 14:00); positive twin: without it three hours",
        [l[:16] for l in pl.stdout.splitlines()[1:]] == ["2026-10-26 13:00", "2026-10-26 14:00"] and len(rows2) == 3,
        pl.stdout,
    )
    rs = R.hours([dict(rec(H0, (9, 9)), sink={"datagrams": 7, "gaps": None, "span_s": 30.0})], "sink")
    check(
        "report: --source picks that source (sink: 7 datagrams, no gaps field -> 0 gaps, no worst window)",
        rs[0]["datagrams"] == 7 and rs[0]["gaps"] == 0 and rs[0]["worst"] is None,
        rs,
    )
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{n_checks} checks, {len(fails)} failure(s)")
for f in fails:
    print("  FAILED:", f)
sys.exit(1 if fails else 0)
