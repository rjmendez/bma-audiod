#!/usr/bin/env python3
"""Regenerate contracts/fixtures/loss_log.jsonl by running the REAL tools/loss_log.py loop (and so the real shared writer)
against scratch status files, with a fixed clock and no sleeping (standard library only):

    python -I make_loss_fixtures.py [out_dir]      (default: ../fixtures)

Nine 30 s ticks from 2026-10-26T12:00:00Z: first reading; exact deltas; a loss burst; sink stale and analyzer missing; bridge
counters backwards (restart); analyzer garbage JSON; recovery (deltas span the gap); sink with rx_errors; steady."""

import json, os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "tools")))
import loss_log as L  # noqa: E402

T0 = 1793016000.0  # 2026-10-26T12:00:00Z
EVERY = 30.0


class Done(Exception):
    pass


def build(out_dir):
    work = tempfile.mkdtemp(prefix="lossfix_")
    try:
        os.makedirs(os.path.join(work, "analysis"))
        paths = {
            "bridge": os.path.join(work, "bridge_status.json"),
            "sink": os.path.join(work, "sink_status.json"),
            "analyzer": os.path.join(work, "analysis", "status.json"),
        }

        def put(name, doc, t):
            with open(paths[name], "w", encoding="utf-8", newline="\n") as f:
                f.write(doc if isinstance(doc, str) else json.dumps(doc))
            os.utime(paths[name], (t, t))

        def bridge(t, pkts, gaps, loss, up):
            put("bridge", {"t": t, "pkts": pkts, "gaps": gaps, "loss_pct": loss, "uptime_s": up, "state": "ok"}, t)

        def sink(t, dg, rxe):
            put("sink", {"t": t, "datagrams": dg, "bad": 0, "rx_errors": rxe}, t)

        def analyzer(t, dg, gaps, up):
            put("analyzer", {"t": t, "rx": {"datagrams": dg, "bad": 0, "gaps": gaps}, "uptime_s": up}, t)

        def tick(i):
            t = T0 + EVERY * i
            if i == 0:
                bridge(t, 90000, 10, 0.0, 600.0)
                sink(t, 45000, 0)
                analyzer(t, 44000, 8, 590.0)
            elif i == 1:
                bridge(t, 99000, 13, 0.02, 630.0)
                sink(t, 49500, 0)
                analyzer(t, 48500, 10, 620.0)
            elif i == 2:
                bridge(t, 107800, 133, 1.35, 660.0)
                sink(t, 53900, 0)
                analyzer(t, 52900, 120, 650.0)
            elif i == 3:
                bridge(t, 116800, 134, 0.0, 690.0)  # sink file not rewritten (stale), analyzer file removed
                os.remove(paths["analyzer"])
            elif i == 4:
                bridge(t, 500, 0, 0.0, 15.0)
                sink(t, 63000, 0)  # bridge restarted
                analyzer(t, 62000, 121, 710.0)
            elif i == 5:
                bridge(t, 9500, 2, 0.1, 45.0)
                sink(t, 67500, 0)
                put("analyzer", '{"t": 1, "rx": {"datagrams": ', t)
            elif i == 6:
                bridge(t, 18500, 2, 0.0, 75.0)
                sink(t, 72000, 4)
                analyzer(t, 71000, 130, 770.0)
            elif i == 7:
                bridge(t, 27500, 2, 0.0, 105.0)
                sink(t, 76500, 4)
                analyzer(t, 75500, 130, 800.0)
            else:
                bridge(t, 36500, 2, 0.0, 135.0)
                sink(t, 81000, 4)
                analyzer(t, 80000, 130, 830.0)

        state = {"i": 0}
        tick(0)

        def clock():
            return T0 + EVERY * state["i"] + 0.5

        def sleep(_s):
            state["i"] += 1
            if state["i"] >= 9:
                raise Done()
            tick(state["i"])

        class A:
            root, out, every_s, stale_s, max_file_bytes, once = work, os.path.join(work, "out"), EVERY, 20.0, L.CAP_BYTES, False

        try:
            L.run(A, clock=clock, sleep=sleep, mono=lambda: 0.0)
        except Done:
            pass
        os.makedirs(out_dir, exist_ok=True)
        dst = os.path.join(out_dir, "loss_log.jsonl")
        with open(os.path.join(A.out, L.FILE_NAME), "rb") as f:
            data = f.read()
        with open(dst, "wb") as f:
            f.write(data)
        return dst
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    print("wrote", build(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "fixtures")))
