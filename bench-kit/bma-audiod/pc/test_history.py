#!/usr/bin/env python3
"""Hermetic tests for history_log.py (RotatingJsonl, load_tail) and the cable sink's history
sample (bma_cable_sink.build_sink_sample + the history file written through the real RotatingJsonl). Temp directories
only, nothing under the pipeline data dir. Run:  python -I test_history.py   (exit 0 = all pass)"""
import json, os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import history_log as H  # noqa: E402

n_checks, fails = 0, []


def check(name, cond, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  [" + str(detail) + "]"))
    if not cond:
        fails.append(name)


def raises(exc, fn, match):
    try:
        fn()
    except exc as e:
        return match in str(e)
    except Exception:           # noqa: BLE001
        return False
    return False


def lines(path):
    with open(path, "rb") as f:
        return [json.loads(x) for x in f.read().splitlines() if x]


class FailingReplace:
    """Recording stand-in for os.replace that fails the way Windows does while a reader holds the file open."""
    def __init__(self):
        self.calls = []

    def __call__(self, src, dst):
        self.calls.append((src, dst))
        raise PermissionError(13, "being used by another process", src)


class RecordingReplace:
    def __init__(self):
        self.calls = []

    def __call__(self, src, dst):
        self.calls.append((src, dst))
        os.replace(src, dst)


def rec(i):
    return {"k": "t", "i": i, "pad": "x" * (30 - len(str(i)))}      # every line has the same length


L = len(H.dumps(rec(0))) + 1
CAP = 5 * L - 1                                                    # the 5th write passes the cap

tmp = tempfile.mkdtemp(prefix="hist_test_")
try:
    # ---- RotatingJsonl: plain append, exact bytes
    p = os.path.join(tmp, "a", "x.jsonl")                          # the directory does not exist yet
    w = H.RotatingJsonl(p, 10 ** 6)
    check("write returns True and creates the directory", w.write({"k": "a", "n": 1}) is True and os.path.isdir(os.path.dirname(p)))
    w.write({"k": "a", "n": 2.5, "s": None})
    with open(p, "rb") as f:
        raw = f.read()
    check("compact JSON, one LF-terminated line per record, exact bytes", raw == b'{"k":"a","n":1}\n{"k":"a","n":2.5,"s":null}\n', raw)
    check("write_line appends an already serialised line", w.write_line(b'{"k":"b"}') is True and open(p, "rb").read().endswith(b'{"k":"b"}\n'))
    check("counters after 3 writes", w.counters() == {"writes": 3, "write_errors": 0, "rotations": 0, "rotate_fallbacks": 0}, w.counters())

    # ---- non-serialisable and NaN records are counted, never raised, and nothing is written
    before = os.path.getsize(p)
    check("an unserialisable record returns False", w.write({"k": "a", "o": object()}) is False)
    check("NaN is refused (strict JSON) and counted", w.write({"k": "a", "x": float("nan")}) is False and w.write_errors == 2 and os.path.getsize(p) == before, w.counters())

    # ---- rotation by rename (positive twin of the fallback test)
    p2 = os.path.join(tmp, "rot.jsonl")
    rr = RecordingReplace()
    w2 = H.RotatingJsonl(p2, CAP, replace=rr)
    for i in range(7):
        w2.write(rec(i))
    check("rotation by rename: one os.replace call from FILE to FILE.1", rr.calls == [(p2, p2 + ".1")], rr.calls)
    check("rotation by rename: FILE.1 holds records 0-4 and FILE records 5-6 exactly",
          [r["i"] for r in lines(p2 + ".1")] == [0, 1, 2, 3, 4] and [r["i"] for r in lines(p2)] == [5, 6])
    check("rotation by rename: counters", (w2.rotations, w2.rotate_fallbacks, w2.write_errors) == (1, 0, 0), w2.counters())

    # ---- rotation when the rename is refused: copy + truncate, nothing lost, collection continues
    p3 = os.path.join(tmp, "fb.jsonl")
    fr = FailingReplace()
    w3 = H.RotatingJsonl(p3, CAP, replace=fr)
    ok = [w3.write(rec(i)) for i in range(7)]
    check("fallback: every write reported success", all(ok), ok)
    check("fallback: the refused rename was attempted exactly once", fr.calls == [(p3, p3 + ".1")], fr.calls)
    check("fallback: FILE.1 holds records 0-4, the truncated FILE records 5-6", [r["i"] for r in lines(p3 + ".1")] == [0, 1, 2, 3, 4] and [r["i"] for r in lines(p3)] == [5, 6])
    check("fallback: counters", (w3.rotations, w3.rotate_fallbacks, w3.write_errors) == (0, 1, 0), w3.counters())
    check("fallback: the file is bounded (second rotation also falls back, one backup only)",
          [w3.write(rec(i)) for i in range(7, 14)] and sorted(os.listdir(tmp)).count("fb.jsonl.1") == 1 and w3.rotate_fallbacks == 2 and
          [r["i"] for r in lines(p3 + ".1")] == [5, 6, 7, 8, 9] and [r["i"] for r in lines(p3)] == [10, 11, 12, 13], (w3.counters(), [r["i"] for r in lines(p3)]))

    # ---- both rename and copy fail: keep appending, retry only after another cap, never raise
    p4 = os.path.join(tmp, "stuck.jsonl")
    w4 = H.RotatingJsonl(p4, CAP, replace=FailingReplace())
    os.makedirs(p4 + ".1")                                         # FILE.1 is a directory, so the copy fails too
    res = [w4.write(rec(i)) for i in range(12)]
    check("stuck rotation: all 12 writes succeed and stay in FILE (no data loss)", all(res) and [r["i"] for r in lines(p4)] == list(range(12)), res)
    check("stuck rotation: retried after another cap, not on every write (2 attempts in 12 writes)", w4.rotate_fallbacks == 2, w4.counters())

    # ---- an unwritable path is counted, not raised
    blocker = os.path.join(tmp, "blocker")
    open(blocker, "w").close()
    w5 = H.RotatingJsonl(os.path.join(blocker, "sub", "x.jsonl"), 100)
    check("unwritable path: write returns False and counts one error", w5.write({"k": "a"}) is False and w5.write_errors == 1, w5.counters())

    # ---- load_tail
    p6 = os.path.join(tmp, "tail.jsonl")
    with open(p6, "wb") as f:
        f.write(b"".join(H.dumps(rec(i)).encode() + b"\n" for i in range(10)) + b'{"k":"t","i":99')     # last line mid-write
    check("load_tail: the last 3 complete records, the unterminated one dropped", [r["i"] for r in H.load_tail(p6, 3)] == [7, 8, 9])
    check("load_tail: more than the file holds returns all 10", [r["i"] for r in H.load_tail(p6, 50)] == list(range(10)))
    check("load_tail: an absent file gives []", H.load_tail(os.path.join(tmp, "nope.jsonl"), 3) == [])
    big = os.path.join(tmp, "big.jsonl")
    with open(big, "wb") as f:
        for i in range(3000):
            f.write(H.dumps(rec(i)).encode() + b"\n")
    check("load_tail with a 64 KiB block boundary inside the file: last 4 exact", [r["i"] for r in H.load_tail(big, 4)] == [2996, 2997, 2998, 2999])
    mid = os.path.join(tmp, "mid.jsonl")
    with open(mid, "wb") as f:
        f.write(b'{"k":"t","i":1}\nnot json\n{"k":"t","i":3}\n')
    check("load_tail skips a corrupt line instead of failing", [r["i"] for r in H.load_tail(mid, 5)] == [1, 3])

    # ---- load_tail partial lines that PARSE as valid JSON (a cut-off line is usually invalid JSON and skipped anyway)
    pt1 = os.path.join(tmp, "nonl.jsonl")
    with open(pt1, "wb") as f:
        f.write(b'{"i":1}\n{"i":2}')                                   # the last record is complete JSON but has no newline yet
    check("load_tail: a complete-JSON last line without newline is treated as mid-write and dropped", [r["i"] for r in H.load_tail(pt1, 5)] == [1], H.load_tail(pt1, 5))
    with open(pt1, "wb") as f:
        f.write(b'{"i":1}\n{"i":2}\n')
    check("positive twin: the same record with its newline is returned", [r["i"] for r in H.load_tail(pt1, 5)] == [1, 2], H.load_tail(pt1, 5))
    pt2 = os.path.join(tmp, "frag.jsonl")
    with open(pt2, "wb") as f:
        f.write(b"5000\nbad\nbad\nbad\n")                              # a 7-byte block cuts '5000' into the fragment '0', valid JSON
    check("load_tail: a block boundary inside a line leaves a fragment that parses ('0'); it must be dropped", H.load_tail(pt2, 1, block=7) == [], H.load_tail(pt2, 1, block=7))
    with open(pt2, "wb") as f:
        f.write(b"5000\nbad\nbad\n7\n")
    check("positive twin: with a real last record the same block size returns it exactly", H.load_tail(pt2, 1, block=7) == [7], H.load_tail(pt2, 1, block=7))

    # ---- a torn tail (writer died mid-line) is isolated: the next record is not glued onto the fragment
    pt3 = os.path.join(tmp, "torn.jsonl")
    with open(pt3, "wb") as f:
        f.write(b'{"k":"a","n":1}\n{"k":"a","n":2,"par')
    w6 = H.RotatingJsonl(pt3, 10 ** 6)
    w6.write({"k": "board_boot", "n": 3})
    w6.write({"k": "a", "n": 4})
    parsed = []
    for l in [x for x in open(pt3, "rb").read().split(b"\n") if x]:
        try:
            parsed.append(json.loads(l)["n"])
        except ValueError:
            parsed.append("TORN")
    check("torn tail: records 1, fragment, 3, 4 each on their own line; the first record after the restart survives", parsed == [1, "TORN", 3, 4] and w6.write_errors == 0 and w6.torn_repaired == 1, (parsed, w6.counters()))
    pt4 = os.path.join(tmp, "okend.jsonl")
    with open(pt4, "wb") as f:
        f.write(b'{"k":"a","n":1}\n')
    w7 = H.RotatingJsonl(pt4, 10 ** 6)
    w7.write({"k": "a", "n": 2})
    check("positive twin: a file that ends in a newline gets no extra blank line and no repair", open(pt4, "rb").read() == b'{"k":"a","n":1}\n{"k":"a","n":2}\n' and w7.torn_repaired == 0, open(pt4, "rb").read())

    # ---- sink sample
    import bma_cable_sink as S
    st = {"t": 1792900030.5, "device": None, "audio_output": False, "device_error": "no CABLE In 16 device", "datagrams": 144000, "bad": 1,
          "buffered_ms": 0, "underruns": 5, "slips": 2, "slipped_frames": 14400, "missing_slot_blocks": 7, "holes": 41, "playing": False,
          "data_frames": 141, "data_dropped": 3}
    ss = S.build_sink_sample(st, 1792899000.25)
    check("sink sample: exact record, incl. playing false and the device error", ss == {
        "k": "sink_sample", "v": 1, "t": 1792900030.5, "started_t": 1792899000.25, "holes": 41, "slips": 2, "slipped_frames": 14400,
        "underruns": 5, "missing_slot_blocks": 7, "data_dropped": 3, "data_frames": 141, "playing": False, "audio_output": False,
        "device_error": "no CABLE In 16 device", "buffered_ms": 0, "datagrams": 144000, "bad": 1}, ss)
    sp = os.path.join(tmp, "history", "sink_history.jsonl")
    hw = H.RotatingJsonl(sp, S.SINK_HISTORY_CAP)
    for i in range(3):
        hw.write(S.build_sink_sample(dict(st, t=1792900030.5 + 30 * i, holes=41 + i, playing=bool(i % 2)), 1792899000.25))
    got = lines(sp)
    check("sink history file: 3 lines with the exact holes/playing series", [(r["t"], r["holes"], r["playing"]) for r in got] ==
          [(1792900030.5, 41, False), (1792900060.5, 42, True), (1792900090.5, 43, False)], got)
    check("sink history: 3 lines are about 1 KB, 2880 a day stay far below the 32 MB cap", len(open(sp, "rb").read()) * 960 < S.SINK_HISTORY_CAP)
    # the existing bands log keeps its bytes-line interface on the shared writer and now survives a refused rename
    bp = os.path.join(tmp, "bands.jsonl")
    bl = S.JsonlLog(bp, cap_bytes=60)
    bl.replace = FailingReplace()
    for i in range(6):
        bl.write(b'{"k":"beam_bands","i":%d}' % i)
    check("JsonlLog: still rotates (copy + truncate) after a refused rename and keeps writing: 25-byte lines, cap 60 -> FILE.1 = [3,4,5], FILE empty, 2 fallbacks",
          (bl.rotate_fallbacks, bl.write_errors, [r["i"] for r in lines(bp + ".1")], lines(bp)) == (2, 0, [3, 4, 5], []), (bl.counters(), lines(bp + ".1"), lines(bp)))
    # ---- the real sink process: main() writes the history file on every stats interval (no audio device needed: metering only)
    import socket, subprocess, time
    import numpy as np
    from dante_bridge import bmaa
    hd = os.path.join(tmp, "sinkhist")
    port = 48431
    t_before = time.time()
    proc = subprocess.Popen([sys.executable, "-I", os.path.join(HERE, "bma_cable_sink.py"), "--listen", f"127.0.0.1:{port}", "--stats-s", "1",
                             "--history-dir", hd, "--bands-log", "", "--board-debug", "", "--bridge-status", os.path.join(tmp, "none.json"),
                             "--device", "no such device xyz"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(2.5)
    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for blk in (0, 256):
        for slot in range(8):
            sk.sendto(bmaa(slot, 0, 0, blk, ((np.arange(256) * 8 + slot) & 0x7FFFFF).astype(np.int32)), ("127.0.0.1", port))
    time.sleep(3.0)
    proc.terminate()
    out_txt = proc.communicate(timeout=20)[0]
    hp = os.path.join(hd, "sink_history.jsonl")
    got = lines(hp) if os.path.exists(hp) else []
    check("sink process: history file has at least 3 sink_sample lines at the 1 s interval", len(got) >= 3 and all(r["k"] == "sink_sample" for r in got), (len(got), out_txt[-300:]))
    check("sink process: the first sample precedes the traffic (0 datagrams), the last has seen exactly the 16 datagrams sent",
          got[0]["datagrams"] == 0 and got[-1]["datagrams"] == 16 and got[-1]["bad"] == 0, [r["datagrams"] for r in got])
    check("sink process: no audio device -> audio_output false, playing false, started_t within the run, t increasing",
          all(r["audio_output"] is False and r["playing"] is False for r in got) and t_before - 1 <= got[0]["started_t"] <= got[0]["t"] and
          all(a_["t"] < b_["t"] for a_, b_ in zip(got, got[1:])) and len({r["started_t"] for r in got}) == 1, got[:2])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{n_checks} checks, {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
