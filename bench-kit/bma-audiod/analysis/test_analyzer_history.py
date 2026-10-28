#!/usr/bin/env python3
"""Tests for analyzer_history (analyzer_sample records every 30 s): the sample builder with a fixed status dict, the cadence
and rotation of AnalyzerHistory, and the real host end to end over UDP (dsp only) with the history written to a temp
directory. No models, no network beyond loopback, nothing outside temp. Run:  python -I test_analyzer_history.py"""
import json, os, shutil, socket, struct, subprocess, sys, tempfile, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import analyzer_history as A  # noqa: E402

fails, n_checks = [], 0
FS = 48000


def check(name, ok, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  [{detail}]"))
    if not ok:
        fails.append(name)


def lines(path):
    with open(path, "rb") as f:
        return [json.loads(x) for x in f.read().splitlines() if x]


STATUS = {"started": 1792900000.4, "t": 1792900030.12345, "uptime_s": 30.0, "device": "cpu",
          "plugins": {"dsp": {"enabled": True, "windows": 30, "records": 31, "errors": 0, "dropped": 1},
                      "spatial": {"enabled": True, "windows": 29, "records": 29, "errors": 2, "dropped": 0},
                      "clap": {"enabled": False, "reason": "disabled by --no"},
                      "novelty": {"enabled": True, "note": "fed by tags embeddings (non-speech only)"}},
          "rx": {"datagrams": 12000, "bad": 3, "gaps": 40}, "contiguous_s": 29.5, "embeddings_stored": 17, "audio_written": False}

tmp = tempfile.mkdtemp(prefix="anhist_test_")
try:
    s = A.build_analyzer_sample(STATUS)
    check("builder: exact record (disabled and counter-less plugins left out; arrays [windows, records, errors, dropped])", s == {
        "k": "analyzer_sample", "v": 1, "t": 1792900030.123, "started": 1792900000.4, "uptime_s": 30.0,
        "rx": {"datagrams": 12000, "bad": 3, "gaps": 40}, "contiguous_s": 29.5,
        "plugins": {"dsp": [30, 31, 0, 1], "spatial": [29, 29, 2, 0]}, "embeddings_stored": 17, "device": "cpu"}, s)
    check("builder: a sample is under 400 bytes", len(json.dumps(s, separators=(",", ":"))) < 400)

    # cadence with a fixed clock: first tick writes, one 20 s later does not, one at +30 s does
    h = A.AnalyzerHistory(os.path.join(tmp, "h"), every_s=30.0)
    r = [h.tick(dict(STATUS, t=100.0 + dt), 100.0 + dt) for dt in (0, 10, 20, 30, 31, 60)]
    check("cadence: written at +0, +30, +60 only", r == [True, False, False, True, False, True], r)
    got = lines(os.path.join(tmp, "h", "analyzer_history.jsonl"))
    check("cadence: 3 lines, with the status t of the tick that wrote each", [x["t"] for x in got] == [100.0, 130.0, 160.0], [x["t"] for x in got])
    bad = h.tick({"plugins": {}}, 1000.0)                      # no "t" in the status dict: counted, not raised
    check("a status dict without t is counted as an error and nothing is written", bad is False and h.errors == 1 and len(lines(os.path.join(tmp, "h", "analyzer_history.jsonl"))) == 3, h.errors)
    h2 = A.AnalyzerHistory(os.path.join(tmp, "h"), every_s=30.0)
    check("positive twin of the error case: a well-formed status after a failure writes", h2.tick(STATUS, 5.0) is True)

    # rotation bounded by the cap, with a refused rename (Windows reader holding the file)
    class Refuse:
        def __call__(self, src, dst):
            raise PermissionError(13, "in use", src)
    h3 = A.AnalyzerHistory(os.path.join(tmp, "r"), cap_bytes=1000, every_s=0.0, replace=Refuse())
    for i in range(20):
        h3.tick(dict(STATUS, t=1000.0 + i), 1000.0 + i)
    main_sz = os.path.getsize(os.path.join(tmp, "r", "analyzer_history.jsonl"))
    check("rotation with a refused rename: copy + truncate, file under cap, backup present, no write errors",
          h3.out.rotate_fallbacks >= 1 and h3.out.write_errors == 0 and main_sz <= 1000 and os.path.exists(os.path.join(tmp, "r", "analyzer_history.jsonl.1")),
          (h3.out.rotate_fallbacks, h3.out.write_errors, main_sz))

    # ---- the real host over UDP (dsp only): history lands in --history-dir, rx.gaps matches the hole that was injected
    out_dir, hist_dir = os.path.join(tmp, "out"), os.path.join(tmp, "hist")
    os.makedirs(out_dir)
    port = 48392
    rng = np.random.default_rng(2)
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "host.py"), "--listen", f"127.0.0.1:{port}", "--out", out_dir,
                             "--history-dir", hist_dir, "--no", "tags", "--no", "clap", "--no", "birds", "--no", "spatial", "--secs", "40"],
                            stderr=subprocess.PIPE, text=True)
    time.sleep(2.0)
    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def bmaa(slot, block, x):
        s24 = np.clip((x * 8388608.0).astype(np.int64), -8388608, 8388607).astype("<i4")
        return b"BMAA" + bytes([1, 0, slot, 0]) + struct.pack("<III", 0, block & 0xFFFFFFFF, len(x)) + s24.tobytes()

    blk, t_start, nb = 0, time.time(), 0
    for sec in range(34):
        x = (rng.standard_normal((FS, 8)) * 10 ** (-60 / 20)).astype(np.float32)
        for off in range(0, FS, 256):
            for slot in range(8):
                if sec == 8 and slot == 3 and off < 2048:
                    continue                                    # a hole on slot 3 only
                sk.sendto(bmaa(slot, blk + off, x[off:off + 256, slot]), ("127.0.0.1", port))
            nb += 1
            d = t_start + nb * 256 / FS - time.time()
            if d > 0:
                time.sleep(d)
        blk += FS
    proc.wait(timeout=90)
    err = proc.stderr.read()
    st = json.load(open(os.path.join(out_dir, "status.json")))
    hp = os.path.join(hist_dir, "analyzer_history.jsonl")
    rows = lines(hp) if os.path.exists(hp) else []
    check("host e2e: exited cleanly", proc.returncode == 0, err[-300:])
    check("host e2e: at least two analyzer_sample lines (start and +30 s)", len(rows) >= 2 and all(r["k"] == "analyzer_sample" for r in rows), len(rows))
    last = rows[-1] if rows else {}
    check("host e2e: the +30 s sample saw the injected hole (rx.gaps >= 1) and not more than the final status.json",
          1 <= last.get("rx", {}).get("gaps", 0) <= st["rx"]["gaps"], (last.get("rx"), st["rx"]))
    check("host e2e: sample counters never exceed the final status.json and dsp windows are the dsp entry of the same plugin",
          0 < last.get("plugins", {}).get("dsp", [0])[0] <= st["plugins"]["dsp"]["windows"], (last.get("plugins"), st["plugins"]["dsp"]))
    check("host e2e: sample says started equals status.json started", last.get("started") == round(st["started"], 3), (last.get("started"), st["started"]))
    check("host e2e: the history file is metadata only and small (under 4 KB for 2-3 samples)", os.path.getsize(hp) < 4096 if rows else False)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{n_checks} checks, {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
