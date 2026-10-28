#!/usr/bin/env python3
"""Tests for the stream-segmentation path of bma_classify (no BirdNET, no network model needed).

Run:  python test_segment.py
The run() tests drive the real UDP source over loopback and replace only the model with a recorder,
so they prove what reaches the model: no window may contain samples from both sides of a hole.
"""

import io, json, socket, struct, sys, threading, time, types
import contextlib
import numpy as np

import bma_classify as bc

FAILED = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (("  " + str(extra)) if extra else ""))
    if not cond:
        FAILED.append(name)


def dgram(slot, block, samples, flags=0, seq=0):
    s = np.asarray(samples, dtype="<i4")
    return b"BMAA" + bytes([1, 0, slot, flags]) + struct.pack("<III", seq, block & 0xFFFFFFFF, len(s)) + s.tobytes()


# ---- Segmenter -----------------------------------------------------------------------------
sg = bc.Segmenter()
r = [sg.feed(0, 256), sg.feed(256, 256), sg.feed(512, 256)]
check("contiguous blocks: start then cont", [k for k, _ in r] == ["start", "cont", "cont"], r)
check("no gaps, coverage 1.0", sg.gaps == 0 and sg.coverage() == 1.0)
k, lost = sg.feed(1000, 256)  # expected 768
check("gap detected with exact loss", (k, lost) == ("gap", 232), (k, lost))
check("gap counted, run reset", sg.gaps == 1 and sg.lost == 232 and sg.run == 256, (sg.gaps, sg.lost, sg.run))
check("longest run kept across the hole", sg.longest == 768, sg.longest)
k, _ = sg.feed(500, 256)  # goes backwards
check("backwards block = reorder, not a gap", k == "reorder" and sg.reordered == 1 and sg.gaps == 1, k)

sg = bc.Segmenter()
sg.feed(2**32 - 128, 256)
k, _ = sg.feed(128, 256)  # 2^32-128+256 = 128 mod 2^32: contiguous across the wrap
check("32-bit block counter wraps without a false gap", k == "cont", k)

sg = bc.Segmenter(fill_gap_samples=48)
sg.feed(0, 256)
k, lost = sg.feed(256 + 40, 256)
check("small hole inside --fill-gap is a counted fill", (k, lost) == ("fill", 40) and sg.filled == 40 and sg.gaps == 0)
k, lost = sg.feed(256 + 40 + 256 + 49, 256)
check("hole just over --fill-gap is a gap", (k, lost) == ("gap", 49), (k, lost))

# ---- datagram parsing ----------------------------------------------------------------------
d = bc._parse_datagram(dgram(3, 1234, [8388607, -8388608, 0], flags=2, seq=9))
check("parse: fields", d and (d["slot"], d["block"], d["n"], d["flags"], d["seq"]) == (3, 1234, 3, 2, 9), d and {k: v for k, v in d.items() if k != "data"})
check("parse: full-scale scaling", d and abs(d["data"][0] - 0.9999999) < 1e-6 and d["data"][1] == -1.0)
check("parse: truncated payload rejected", bc._parse_datagram(dgram(0, 0, [1, 2, 3, 4])[:-3]) is None)
check("parse: foreign magic rejected", bc._parse_datagram(b"XXXX" + bytes(40)) is None)

# strict per contracts/specs/bmaa.md: version 1, byte 5 (dsp) 0, slot 0-7, exact length, samples inside signed 24 bit
setb = lambda pk, i, v: pk[:i] + bytes([v]) + pk[i + 1 :]
good = dgram(2, 4096, [1, -1, 8388607, -8388608], flags=1, seq=5)
check("parse strict: the unmodified datagram is accepted (positive twin of the next rejections)", bc._parse_datagram(good) is not None)
check("parse strict: version 2 rejected", bc._parse_datagram(setb(good, 4, 2)) is None)
check("parse strict: version 0 rejected", bc._parse_datagram(setb(good, 4, 0)) is None)
check(
    "parse strict: non-zero byte 5 (dsp) rejected (1 and 255)", bc._parse_datagram(setb(good, 5, 1)) is None and bc._parse_datagram(setb(good, 5, 255)) is None
)
check(
    "parse strict: slot 7 accepted, slot 8 and slot 255 rejected",
    bc._parse_datagram(dgram(7, 0, [1])) is not None and bc._parse_datagram(dgram(8, 0, [1])) is None and bc._parse_datagram(dgram(255, 0, [1])) is None,
)
check("parse strict: one byte too many rejected (trailing data is not allowed)", bc._parse_datagram(good + b"\0") is None)
check(
    "parse strict: samples 8388607 and -8388608 accepted (already in the full-scale test above)",
    all(bc._parse_datagram(dgram(0, 0, [v])) is not None for v in (8388607, -8388608)),
)
check("parse strict: samples 8388608, -8388609 and 1<<24 rejected", all(bc._parse_datagram(dgram(0, 0, [v])) is None for v in (8388608, -8388609, 1 << 24)))
check("parse strict: an out-of-range sample anywhere in the block rejects it", bc._parse_datagram(dgram(0, 0, [0] * 255 + [8388608])) is None)
check(
    "parse strict: unknown flag bits are ignored, not rejected",
    bc._parse_datagram(setb(good, 7, 0xFF)) is not None and bc._parse_datagram(setb(good, 7, 0xFF))["flags"] == 0xFF,
)
check("parse strict: an empty block (n = 0) is accepted", bc._parse_datagram(dgram(0, 0, [])) is not None)


# ---- run() over real UDP with a recording model --------------------------------------------
class RecordingNet:
    """Stands in for BirdNET: records every window it is asked to classify."""

    windows = []

    def __init__(self, *a, **k):
        self.version = "test"
        self.filtered = []  # what the location filter removed in the last classify(); none without a filter

    @classmethod
    def from_args(cls, args):
        # run() builds its model with BirdNet.from_args(args) since the location filter landed
        return cls()

    def classify(self, audio48k):
        RecordingNet.windows.append(np.array(audio48k, copy=True))
        return []


def run_stream(datagrams, slot=None, fill_ms=0.0, port=47123, idle=0.6):
    RecordingNet.windows = []
    bc.BirdNet = RecordingNet
    args = types.SimpleNamespace(
        wav=None,
        udp="127.0.0.1:%d" % port,
        stdin_raw=False,
        sr=48000,
        channels=1,
        dtype="int24",
        min_conf=0.25,
        gate_dbfs=-120.0,
        ring_s=10.0,
        lat=None,
        lon=None,
        week=-1,
        slot=slot,
        fill_gap_ms=fill_ms,
        idle_exit_s=idle,
    )
    out = io.StringIO()

    def sender():
        time.sleep(0.4)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for p in datagrams:
            s.sendto(p, ("127.0.0.1", port))
            time.sleep(0.0005)

    t = threading.Thread(target=sender)
    t.start()
    with contextlib.redirect_stdout(out):
        bc.run(args)
    t.join()
    events = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
    return events, RecordingNet.windows


SR = 48000


def expect_windows(chunks, win=3 * SR, hop=SR):
    """The documented window rule: nothing until `win` contiguous samples exist; the first window
    fires on the chunk that completes it, then one per `hop` of new samples. `chunks` = chunk
    sizes of ONE contiguous run, in arrival order."""
    total = since = n = 0
    primed = False
    for c in chunks:
        total += c
        if total < win:
            continue
        if not primed:
            since, primed = hop, True
        else:
            since += c
        while since >= hop:
            since -= hop
            n += 1
    return n


# Signal whose value encodes its absolute sample index, so a window that spans a hole shows up as
# a discontinuity in the recorded samples.
def block(start, n=256, slot=0):
    return dgram(slot, start, [(start + i) % 8000000 + 1 for i in range(n)])


# A: 8 s contiguous -> windows exist, every one continuous
dg = [block(i * 256) for i in range(8 * SR // 256)]
ev, win = run_stream(dg, port=47123)
summ = ev[-1]
exp = expect_windows([256] * (8 * SR // 256))
check(
    "contiguous 8 s: one window per hop once 3 s exist (no duplicates of the first)",
    len(win) == exp and exp == 5 and summ["verdict"] != "no_contiguous_window",
    (len(win), exp, summ.get("verdict")),
)
check("contiguous 8 s: no window is classified twice", len({w.tobytes() for w in win}) == len(win), len({w.tobytes() for w in win}))
cont_ok = all(np.all(np.diff(np.round(w * 8388608.0)) == 1) for w in win)
check("contiguous 8 s: every window is gap-free", cont_ok)
check("summary has stream coverage 1.0", summ["stream"]["coverage"] == 1.0 and summ["stream"]["gaps"] == 0, summ.get("stream"))

# B: a 10 ms hole every 40 ms (like the board NIC): no 3 s window may exist, and it must say why
dg = []
pos = 0
for i in range(8 * SR // 256):
    if i % 8 == 7:  # drop one 256-sample block in 8 (~12.5%)
        pos += 256
        continue
    dg.append(block(pos))
    pos += 256
ev, win = run_stream(dg, port=47124)
summ = ev[-1]
check("holey stream: no window reaches the model", len(win) == 0, len(win))
check("holey stream: verdict names the cause", summ["verdict"] == "no_contiguous_window" and "hole" in summ.get("reason", ""), summ)
st = summ["stream"]
check("holey stream: loss accounted exactly", st["gaps"] > 0 and st["lost_samples"] == st["gaps"] * 256, st)
check("holey stream: longest run < one window", st["longest_contiguous_s"] < 3.0, st["longest_contiguous_s"])

# C: one hole in the middle of a long clean stream -> windows only on either side, none across
dg = [block(i * 256) for i in range(0, 5 * SR // 256)]  # 0 .. 5 s
dg += [block(i * 256) for i in range(7 * SR // 256, 12 * SR // 256)]  # 7 .. 12 s (2 s hole)
ev, win = run_stream(dg, port=47125)
summ = ev[-1]
spans = 0
for w in win:
    d = np.diff(np.round(w * 8388608.0))
    if not np.all(d == 1):
        spans += 1
# the two runs are exactly the block ranges built above (937 and 938 blocks: 7*SR/256 floors)
exp = expect_windows([256] * len(range(0, 5 * SR // 256))) + expect_windows([256] * len(range(7 * SR // 256, 12 * SR // 256)))
check("one hole: exactly the windows that fit on each side, none across", len(win) == exp, (len(win), exp))
check("one hole: NO window spans the hole", spans == 0, spans)
check("one hole: exactly one gap of 2 s", summ["stream"]["gaps"] == 1 and summ["stream"]["lost_samples"] == 2 * SR, summ["stream"])

# D: two slots interleaved: only the selected slot is classified, the other is counted not mixed
dg = []
for i in range(4 * SR // 256):
    dg.append(block(i * 256, slot=0))
    dg.append(dgram(5, i * 256, [-7] * 256))
ev, win = run_stream(dg, slot=0, port=47126)
summ = ev[-1]
check("slot demux: other slot never reaches the model", all(np.all(np.diff(np.round(w * 8388608.0)) == 1) for w in win) and len(win) >= 1)
check("slot demux: other slot counted", summ["stream"]["ignored_other_slot_datagrams"] == 4 * SR // 256, summ["stream"])

# E: --fill-gap-ms bridges only what it was told to, and says so
dg = []
pos = 0
for i in range(5 * SR // 256):
    if i % 40 == 39:
        pos += 128  # a 128-sample (2.7 ms) hole every ~213 ms
    dg.append(block(pos))
    pos += 256
ev, win = run_stream(dg, fill_ms=5.0, port=47127)
summ = ev[-1]
check("fill: windows now exist", len(win) >= 1, len(win))
check("fill: every bridged sample is counted", summ["stream"]["filled_samples"] > 0 and summ["stream"]["gaps"] == 0, summ["stream"])

print()
print("FAILED: %s" % FAILED if FAILED else "ALL PASSED")
sys.exit(1 if FAILED else 0)
