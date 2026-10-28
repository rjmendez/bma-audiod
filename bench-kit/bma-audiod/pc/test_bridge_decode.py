#!/usr/bin/env python3
"""Hermetic test of dante_bridge.decode_selected: exact 24-bit BE sign-extended values per channel,
numpy path against the pure-python path. Run:  python -I test_bridge_decode.py   (exit 0 = all pass)"""

import os, struct, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dante_bridge as b  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def packet(nch, sec, fr, value):
    """value(frame, ch) -> signed 24-bit int; frame-major, channel fastest."""
    body = b"".join((value(f, c) & 0xFFFFFF).to_bytes(3, "big") for f in range(b.FRAMES) for c in range(nch))
    return b"\x02" + struct.pack(">II", sec, fr) + body


EXTREMES = [0, 1, -1, 0x7FFFFF, -0x800000, 0x123456, -0x123456]


def val(f, c):
    if c == 0:
        return EXTREMES[f % 7]
    return (c * 1000 + f) * (-1 if c % 2 else 1)


pkt = packet(8, 3, 48000 - 16, val)
r = b.decode_selected(pkt, list(range(8)))
check("abs_frame", r is not None and r[0] == 3 * 48000 + 48000 - 16, r and r[0])
check("nch", r is not None and r[1] == 8)
want = {c: [val(f, c) for f in range(16)] for c in range(8)}
got = {c: [int(x) for x in r[2][c]] for c in range(8)} if r else {}
check("all 8 channels exact incl. 0x7FFFFF/-0x800000/-1", got == want, got.get(0))
check("channel 0 extremes", got.get(0, [])[:7] == [0, 1, -1, 8388607, -8388608, 1193046, -1193046], got.get(0, [])[:7])

sub = b.decode_selected(pkt, [2, 5])
check("subset returns only the requested channels", sub is not None and sorted(sub[2]) == [2, 5], sub and sorted(sub[2]))
check("subset values exact", sub is not None and [int(x) for x in sub[2][5]] == want[5])
check("channel beyond nch is skipped", sorted(b.decode_selected(pkt, [1, 9])[2]) == [1])

np_saved, b.np = b.np, None
try:
    r2 = b.decode_selected(pkt, list(range(8)))
finally:
    b.np = np_saved
check("pure-python path agrees with numpy path", r2 is not None and {c: list(r2[2][c]) for c in range(8)} == want)

check("bad type byte rejected", b.decode_selected(b"\x03" + pkt[1:], [0]) is None)
check("ragged length rejected", b.decode_selected(pkt[:-1], [0]) is None)
check("frame >= RATE rejected", b.decode_selected(packet(8, 3, 48000, val), [0]) is None)
sys.exit(1 if fails else 0)
