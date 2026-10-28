#!/usr/bin/env python3
"""Hermetic tests for bma_cable_sink + bma_cable_decode (no audio device, no network): BMAA parsing against the
bridge's own packet builder, 8-slot alignment, missing-slot and hole handling, the output ring's prebuffer /
underrun / slip behaviour, and the 16-channel round trip (index track, wall-clock anchors, debug records) through
the decoder (cable-stream 2.0.0: index and data tracks as 12-bit symbols, count (u - 2048) << 8), PortAudio
re-initialisation on the device-open retry, and the operator self-test against fake lossy loopbacks.
Run:  python test_cable_sink.py"""

import json, os, struct, sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bma_cable_sink as S  # noqa: E402
import bma_cable_decode as D  # noqa: E402
from dante_bridge import bmaa  # noqa: E402  (the real packet builder the sink must be able to read)

fails = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def samples(slot, start, n=256):
    """int32 ramp inside the signed 24-bit range (the parser rejects anything else) that encodes the slot (low 3 bits)
    and the absolute sample index (mod 2^20), so a wrong channel or offset is visible."""
    return ((np.arange(start, start + n, dtype=np.int64) * 8 + slot) & 0x7FFFFF).astype(np.int32)


def dgram(slot, block):
    return S.parse_datagram(bmaa(slot, 0, 0, block, samples(slot, block)))[3]


# ---- parse_datagram
pkt = bmaa(3, 2, 7, 512, samples(3, 512))
p = S.parse_datagram(pkt)
check(
    "parse: well-formed datagram -> slot 3, flags 2, block 512, 256 samples",
    p is not None and p[0] == 3 and p[1] == 2 and p[2] == 512 and len(p[3]) == 256,
    str(p and p[:3]),
)
check(
    "parse: sample values are the int32 values divided by 2^23 exactly", p is not None and np.array_equal(p[3], samples(3, 512).astype(np.float32) / 8388608.0)
)
check("parse: bad magic rejected", S.parse_datagram(b"XXXX" + pkt[4:]) is None)
check("parse: truncated body rejected", S.parse_datagram(pkt[:-4]) is None)
check("parse: slot 8 rejected", S.parse_datagram(bmaa(8, 0, 0, 0, samples(0, 0))) is None)
check("parse: slot 7 accepted (positive twin of the slot-8 case)", S.parse_datagram(bmaa(7, 0, 0, 0, samples(7, 0))) is not None)

# strict per contracts/specs/bmaa.md: version 1, reserved 0, exact length, samples inside signed 24 bit
setb = lambda pk, i, v: pk[:i] + bytes([v]) + pk[i + 1 :]
good = bmaa(2, 1, 5, 4096, samples(2, 4096))
check("parse strict: the unmodified datagram is accepted (positive twin of the next rejections)", S.parse_datagram(good) is not None)
check("parse strict: version 2 rejected", S.parse_datagram(setb(good, 4, 2)) is None)
check("parse strict: version 0 rejected", S.parse_datagram(setb(good, 4, 0)) is None)
check("parse strict: non-zero reserved byte rejected (1 and 255)", S.parse_datagram(setb(good, 5, 1)) is None and S.parse_datagram(setb(good, 5, 255)) is None)
check("parse strict: one byte too many rejected", S.parse_datagram(good + b"\0") is None)
check(
    "parse strict: samples 8388607 and -8388608 accepted",
    all(S.parse_datagram(bmaa(0, 0, 0, 0, np.array([v], dtype=np.int32))) is not None for v in (8388607, -8388608)),
)
check(
    "parse strict: samples 8388608, -8388609 and 1<<24 rejected",
    all(S.parse_datagram(bmaa(0, 0, 0, 0, np.array([v], dtype=np.int32))) is None for v in (8388608, -8388609, 1 << 24)),
)
check(
    "parse strict: an out-of-range sample anywhere in the block rejects it (255 good + 1 bad)",
    S.parse_datagram(bmaa(0, 0, 0, 0, np.array([0] * 255 + [8388608], dtype=np.int32))) is None,
)
check(
    "parse strict: unknown flag bits are ignored, not rejected (spec: receivers ignore them)",
    S.parse_datagram(setb(good, 7, 0xFF)) is not None and S.parse_datagram(setb(good, 7, 0xFF))[1] == 0xFF,
)
check("parse strict: an empty block (n = 0) is accepted", S.parse_datagram(bmaa(0, 0, 0, 0, np.array([], dtype=np.int32))) is not None)

# ---- Assembler: alignment
a = S.Assembler()
out = []
for slot in (5, 0, 7, 1, 3, 2, 6):  # arrival order scrambled, slot 4 still missing
    out += a.push(slot, 0, dgram(slot, 0))
check("align: nothing is emitted while one slot is missing", out == [], str(len(out)))
out += a.push(4, 0, dgram(4, 0))
check("align: emitted once all 8 slots are present, one (256, 8) frame starting at index 0", len(out) == 1 and out[0][0] == 0 and out[0][1].shape == (256, 8))
fr = out[0][1]
check(
    "align: column c holds slot c's samples exactly (no channel swap)",
    all(np.array_equal(fr[:, c], samples(c, 0).astype(np.float32) / 8388608.0) for c in range(8)),
)
check("align: counters clean", a.missing_slot_blocks == 0 and a.holes == 0, f"{a.missing_slot_blocks} {a.holes}")

# ---- Assembler: one slot never arrives -> zero column once MAX_PENDING blocks are waiting
a = S.Assembler()
out = []
for b in range(S.MAX_PENDING + 2):
    for slot in range(8):
        if slot != 2:
            out += a.push(slot, b * 256, dgram(slot, b * 256))
check("missing slot: frames are emitted anyway after the pending limit", len(out) >= 1, str(len(out)))
check(
    "missing slot: its column is exactly zero, the others are intact",
    np.all(out[0][1][:, 2] == 0) and np.array_equal(out[0][1][:, 3], samples(3, 0).astype(np.float32) / 8388608.0),
)
check("missing slot: counted once per block emitted", a.missing_slot_blocks == len(out), f"{a.missing_slot_blocks} vs {len(out)}")

# ---- Assembler: a hole in the block index is filled with exactly that much silence, at the right indexes
a = S.Assembler()
out = []
for b in (0, 1000):
    for slot in range(8):
        out += a.push(slot, b, dgram(slot, b))
check("hole: [data, silence, data] emitted", len(out) == 3, str([o[1].shape for o in out]))
check(
    "hole: silence is exactly 1000 - 256 = 744 frames of zeros starting at index 256",
    out[1][0] == 256 and out[1][1].shape == (744, 8) and not out[1][1].any(),
    str((out[1][0], out[1][1].shape)),
)
check("hole: counted once", a.holes == 1, str(a.holes))
check(
    "hole: a late duplicate of already-played audio is dropped (positive twin: fresh block is kept)",
    all(a.push(s, 0, dgram(s, 0)) == [] for s in range(8)) and sum(len(f[1]) for s in range(8) for f in a.push(s, 1256, dgram(s, 1256))) == 256,
)

# ---- u32 block wrap: the BMAA block field wraps every 2^32 samples (~24.9 h); the sink must keep playing
W32 = 1 << 32


def push_blocks(asm_, block, slots=range(8)):
    out_ = []
    for s in slots:
        out_ += asm_.push(s, block, dgram(s, block))
    return out_


a = S.Assembler()
out = []
for blk in (W32 - 512, W32 - 256, 0, 256):  # the last two arrive with wrapped values 0 and 256
    out += push_blocks(a, blk)
check(
    "wrap: blocks across 2^32 - 512, -256, 0, 256 come out as unwrapped starts 2^32-512, -256, 2^32, 2^32+256",
    [o[0] for o in out] == [W32 - 512, W32 - 256, W32, W32 + 256],
    str([o[0] for o in out]),
)
check(
    "wrap: no hole, no missing slot, every frame still 256 samples",
    a.holes == 0 and a.missing_slot_blocks == 0 and all(o[1].shape == (256, 8) for o in out),
    f"{a.holes} {a.missing_slot_blocks}",
)
a = S.Assembler()
out = push_blocks(a, W32 - 512)
out += push_blocks(a, 0, slots=range(0, 4))  # post-wrap block arrives first, only half complete
out += push_blocks(a, W32 - 256)  # then the pre-wrap block completes
out += push_blocks(a, 0, slots=range(4, 8))
check(
    "wrap: reordered across the wrap still comes out in time order with no hole",
    [o[0] for o in out] == [W32 - 512, W32 - 256, W32] and a.holes == 0,
    str([o[0] for o in out]),
)
a = S.Assembler()
out = []
for blk in (1000, 1256, 1512):
    out += push_blocks(a, blk)
check("wrap: positive twin - blocks far from the wrap are unchanged", [o[0] for o in out] == [1000, 1256, 1512])
a = S.Assembler()
out = push_blocks(a, 5000) + push_blocks(a, 5256) + push_blocks(a, 4000)
check(
    "wrap: a genuinely old block (1256 samples back) is still dropped as a late duplicate, not unwrapped forward",
    [o[0] for o in out] == [5000, 5256],
    str([o[0] for o in out]),
)
a = S.Assembler()
steps = (0, 0x70000000, 0xE0000000, W32 + 0x30000000)  # each step < 2^31, but the last is > 2^31 from the first block
out = []
for blk in steps:
    out += push_blocks(a, blk & 0xFFFFFFFF)
starts = [o[0] for o in out if len(o[1]) == 256]
check("wrap: a stream more than 2^31 samples from its first block is still placed correctly (the reference advances)", starts == list(steps), str(starts))
wcomp = S.Composer(S.DataTrack(), anchor_every=10**9)
aw = S.Assembler()
frames_w = [wcomp.compose(o_[0], o_[1]) for o_ in push_blocks(aw, W32 - 156) + push_blocks(aw, 100)]  # 2^32-156 then wrapped 100
dec_w = D.decode(np.rint(np.concatenate(frames_w).astype(np.float64) * 8388608.0).astype(np.int64)) if len(frames_w) == 2 else None
check(
    "wrap: the u32 index track runs on across the wrap - 512 consecutive indexes 2^32-156 .. 2^32+355 read as (index mod 2^32), exactly",
    dec_w is not None and np.array_equal(dec_w["index"], np.arange(W32 - 156, W32 + 356) & 0xFFFFFFFF),
    str(None if dec_w is None else (dec_w["index"][0], dec_w["index"][255], dec_w["index"][256], dec_w["index"][-1])),
)
kw_rows = np.rint(frames_w[0].astype(np.float64) * 8388608.0).astype(np.int64)
check(
    "wrap: the wrap row (index 2^32 -> 0) is a live row, NOT silence: its three index columns are -524288, no row is silent, no jump is reported",
    dec_w is not None and kw_rows[156, 8:11].tolist() == [-524288] * 3 and not dec_w["silent"].any() and dec_w["jumps"] == [] and int(dec_w["index"][156]) == 0,
    str(None if dec_w is None else (kw_rows[156, 8:11].tolist(), dec_w["silent"].sum(), dec_w["jumps"])),
)
k_w = np.concatenate([kw_rows[150:154], np.zeros((1, 16), np.int64), kw_rows[154:156]])
dec_w2 = D.decode(k_w)
check(
    "wrap: positive twin - an all-zero row between rows 2^32-3 and 2^32-2 IS silence (the underrun marker), skipped, with no jump",
    int(dec_w2["silent"].sum()) == 1 and bool(dec_w2["silent"][4]) and dec_w2["jumps"] == [],
    str((dec_w2["silent"].sum(), dec_w2["jumps"])),
)
check(
    "wrap: the rows 2^32-1 / 0 / 1 around the wrap decode as consecutive (no jump, no impossible value)",
    D.decode(kw_rows[155:158])["index"].tolist() == [W32 - 1, 0, 1]
    and D.decode(kw_rows[155:158])["jumps"] == []
    and D.decode(kw_rows[155:158])["impossible"] == 0,
)

# ---- Ring (8 columns here so the ramp is easy to read)
r = S.Ring(target_ms=100, max_ms=300, ncols=8)  # target 4800 frames, max 14400
ramp = lambda start, n: np.repeat(np.arange(start, start + n, dtype=np.float32)[:, None], 8, axis=1)
r.put(ramp(0, 2400))
check("ring: below the prebuffer target plays silence and does not consume", not r.get(480).any() and r.frames == 2400)
r.put(ramp(2400, 2400))
g = r.get(480)
check(
    "ring: at the target it starts and returns the oldest frames in order",
    g.shape == (480, 8) and np.array_equal(g[:, 0], np.arange(480, dtype=np.float32)),
    str(g[:3, 0]),
)
check("ring: consumed frames are gone", r.frames == 4800 - 480)
r2 = S.Ring(target_ms=100, max_ms=300, ncols=8)
r2.put(ramp(0, 4800))
r2.get(480)
r2.get(4320)  # drains the rest exactly
r2.get(480)  # nothing left: underrun
check("ring: underrun plays silence, is counted once, and rebuffers", r2.underruns == 1 and not r2.started, f"{r2.underruns}")
r3 = S.Ring(target_ms=100, max_ms=300, ncols=8)
r3.put(ramp(0, 14400))  # exactly max: no slip (positive twin)
check("ring: at max exactly, nothing is dropped", r3.slips == 0 and r3.frames == 14400)
r3.put(ramp(14400, 480))  # 14880 > max -> drop 14880 - 4800 = 10080 oldest frames
check(
    "ring: over max drops the oldest down to the target, counted",
    r3.slips == 1 and r3.slipped_frames == 10080 and r3.frames == 4800,
    f"{r3.slips} {r3.slipped_frames} {r3.frames}",
)
g = r3.get(4)
check(
    "ring: after a slip the next frame played is the first one that was kept (index 10080)",
    np.array_equal(g[:, 0], np.arange(10080, 10084, dtype=np.float32)),
    str(g[:, 0]),
)

# ---- 16-channel composition round trip through the decoder
clock = [1_792_884_000_000_000]  # PC UTC microseconds, advanced by hand
data = S.DataTrack()
comp = S.Composer(data, anchor_every=300, clock_us=lambda: clock[0])
BIG = (3 << 24) | 0x123456  # an index whose high word is non-zero
BEAMS = np.stack([samples(c, 0, 600) for c in range(8)], axis=1).astype(np.float32) / 8388608.0
rec1 = json.dumps({"type": "monitor", "dsp": 4, "slot": 0, "rms_dbfs": -60.0, "host_t": 1792884000.5}).encode()
rec2 = json.dumps({"type": "meter", "seq": 1, "host_t": 1792884001.25}).encode()
blocks = []
data.push(S.K_BOARD, rec1)
blocks.append(comp.compose(BIG, BEAMS[:300]))
clock[0] += 1_000_000
data.push(S.K_BOARD, rec2)
data.push(S.K_BRIDGE, json.dumps({"t": 1792884001.0, "state": "ok", "loss_pct": 0.25}).encode())
blocks.append(comp.compose(BIG + 300, BEAMS[300:]))
out16 = np.concatenate(blocks)
check("compose: 16 channels, beams untouched in 0-7", out16.shape == (600, 16) and np.array_equal(out16[:, :8], BEAMS))
check("compose: reserved channels 14-16 are exactly zero", not out16[:, 13:].any())
k = np.rint(out16.astype(np.float64) * 8388608.0).astype(np.int64)
dec = D.decode(k)
check("decode: index track recovered exactly for every sample", np.array_equal(dec["index"], np.arange(BIG, BIG + 600)), str(dec["index"][:2]))
check("decode: no jumps, no silent rows, no corrupt frames", dec["jumps"] == [] and not dec["silent"].any() and dec["bad_frames"] == 0)
check(
    "decode: two anchors, exact (index, UTC us): the second block starts anchor_every=300 samples after the first",
    dec["anchors"] == [(BIG, 1_792_884_000_000_000), (BIG + 300, 1_792_884_001_000_000)],
    str(dec["anchors"]),
)
check(
    "decode: board records recovered verbatim and in order",
    [{k_: v for k_, v in r_.items() if k_ != "card_index"} for r_ in dec["board"]] == [json.loads(rec1), json.loads(rec2)],
)
check(
    "decode: a board record at host_t = anchor time + 0.5 s lands 24000 samples after the anchor index",
    dec["board"][0]["card_index"] == BIG + 24000,
    str(dec["board"][0]["card_index"]),
)
check(
    "decode: a record at host_t = second anchor + 0.25 s lands 12000 samples after the second anchor index",
    dec["board"][1]["card_index"] == BIG + 300 + 12000,
    str(dec["board"][1]["card_index"]),
)
check("decode: bridge status recovered", len(dec["bridge"]) == 1 and dec["bridge"][0]["loss_pct"] == 0.25)


# ---- cable-stream 2.0.0: exact symbol values (12-bit symbols u, count (u - 2048) << 8)
def rows_of(frames):
    return np.rint(frames.astype(np.float64) * 8388608.0).astype(np.int64)


def cnt(u):
    return (u - 2048) << 8


def idx_cols(i):
    """independent oracle: the three index column counts of a u32 index (12/12/8 bits)"""
    return [cnt(i & 0xFFF), cnt((i >> 12) & 0xFFF), cnt((i >> 24) & 0xFF)]


check(
    "compose: index columns are exact - BIG = 0x3123456 -> u = (0x456, 0x123, 0x03) -> counts (-240128, -449792, -523520)",
    k[0, 8:11].tolist() == [-240128, -449792, -523520],
    str(k[0, 8:11]),
)
check("compose: the next row (index +1, u0 = 0x457) is (-239872, -449792, -523520)", k[1, 8:11].tolist() == [-239872, -449792, -523520], str(k[1, 8:11]))
check("compose: every index column of all 600 rows equals the independent oracle", all(k[r, 8:11].tolist() == idx_cols(BIG + r) for r in range(600)))
check(
    "compose: channels 9-13 carry symbols only - every count is a multiple of 256 and inside +-2048 << 8",
    not (k[:, 8:13] & 255).any() and int(k[:, 8:13].min()) >= -(2048 << 8) and int(k[:, 8:13].max()) <= (2047 << 8),
)
check(
    "compose: reserved channels 14-16 are exactly zero (and ch1-8 are the beams)",
    not k[:, 13:].any() and np.array_equal(k[:, :8], np.rint(BEAMS.astype(np.float64) * 8388608.0).astype(np.int64)),
)
fr0 = S.encode_frame(S.K_BOARD, rec1)
check(
    "compose: data track is one byte per channel - the first row is 'B','M' = (0x42, 0x4D) -> counts (-507392, -504576), the second 'D','1' = (0x44, 0x31)",
    k[0, 11:13].tolist() == [cnt(0x42), cnt(0x4D)] == [-507392, -504576] and k[1, 11:13].tolist() == [cnt(0x44), cnt(0x31)],
    str(k[:2, 11:13]),
)
check(
    "compose: the first 40 rows of ch12/ch13 are the BMD1 frame bytes in order (ch12 first byte, ch13 second)",
    all(k[r, 11:13].tolist() == [cnt(fr0[2 * r]), cnt(fr0[2 * r + 1])] for r in range(40)),
)
check("compose: idle data rows (queue empty) are u = 0 -> count -524288 on both data channels", k[-1, 11:13].tolist() == [-524288, -524288], str(k[-1, 11:13]))
kn = rows_of(S.Composer(S.DataTrack(), anchor_every=10**9).compose(0xFFFF8001, BEAMS[:2]))
check(
    "compose: index 0xFFFF8001 -> u = (0x001, 0xFF8, 0xFF): counts (-524032, 522240, -459008); next row u0 = 2",
    kn[0, 8:11].tolist() == [-524032, 522240, -459008] and kn[1, 8:11].tolist() == [-523776, 522240, -459008],
    str(kn[:, 8:11]),
)
kw = rows_of(S.Composer(S.DataTrack(), anchor_every=10**9).compose(W32 - 2, BEAMS[:4]))
check(
    "compose: the u32 index wraps at 2^32 - rows 2^32-2, 2^32-1, 2^32, 2^32+1 read 0xFFFFFFFE, 0xFFFFFFFF, 0, 1",
    [r_.tolist() for r_ in kw[:, 8:11]] == [idx_cols(W32 - 2), idx_cols(W32 - 1), [-524288] * 3, idx_cols(1)],
    str(kw[:, 8:11].tolist()),
)
k5 = rows_of(S.Composer(S.DataTrack(), anchor_every=10**9).compose(W32 + 5, BEAMS[:1]))
check("compose: a 64-bit index above 2^32 is written as index mod 2^32 (2^32 + 5 -> u = (5, 0, 0))", k5[0, 8:11].tolist() == idx_cols(5), str(k5[0, 8:11]))
allsym = np.arange(4096)
fs = S.sym12_to_f32(allsym)
check(
    "sym12: all 4096 symbols map to counts (u - 2048) << 8 exactly, peak -0.0625 .. 2047/32768 of full scale, and decode back through the rounding reader",
    np.array_equal(rows_of(fs), np.array([cnt(v) for v in range(4096)]))
    and float(fs.min()) == -0.0625
    and float(fs.max()) == 2047 / 32768.0
    and np.array_equal(D.sym12(rows_of(fs))[0], allsym)
    and not D.sym12(rows_of(fs))[1].any(),
)
try:
    S.sym12_to_f32(np.array([4096]))
    raised = None
except ValueError as e_:
    raised = str(e_)
check(
    "sym12: a symbol outside 0..4095 raises ValueError('symbol outside 0..4095'); 0 and 4095 are accepted",
    raised == "symbol outside 0..4095" and S.sym12_to_f32(np.array([0, 4095])).tolist() == [-0.0625, 2047 / 32768.0],
)
rng = np.random.default_rng(5)
starts = [0, 1, 0xFFF, 0x1000, 0xFFFFFF, 0x1000000, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFE, 0xFFFFFFFF, 0xFF000FFF, 0x00FFF000] + [
    int(v) for v in rng.integers(0, 2**32, 3000)
]
cmp1 = S.Composer(S.DataTrack(), anchor_every=10**9)
kr = np.concatenate([rows_of(cmp1.compose(v, BEAMS[:1])) for v in starts])
decr = D.decode(kr)
check(
    "compose: 3012 single rows (edge values, index 0 and 3000 random u32 indexes) decode to exactly the indexes written, none silent, none impossible",
    decr["index"].tolist() == starts and not decr["silent"].any() and decr["impossible"] == 0 and decr["range_errors"] == 0,
    str([(a_, b_) for a_, b_ in zip(decr["index"].tolist(), starts) if a_ != b_][:3]),
)
zr = D.decode(np.zeros((5, 16), np.int64))
check(
    "underrun: all-zero rows (the sink's silence) are exactly the silent rows: 5 of 5, index 0, nothing impossible, no data bytes",
    zr["silent"].all() and zr["index"].tolist() == [0] * 5 and zr["impossible"] == 0 and zr["frames"] == [] and zr["reserved_nonzero"] == 0,
)
r_ = Rows = np.zeros((3, 16), np.int64)
Rows[:, 8:11] = [-524288, -524288, -524288]
check(
    "underrun: positive twin - three rows with index 0 (u = 0 on all index channels, count -524288) are LIVE, not silent",
    not D.decode(Rows)["silent"].any() and D.decode(Rows)["index"].tolist() == [0, 0, 0],
)
r2 = np.zeros((1, 16), np.int64)
r2[0, 10] = 1 << 8  # only ch11 non-zero is still not an index (u3 = 2049 > 255): flagged
d_r2 = D.decode(r2)
check(
    "underrun: a row that is zero on ch9-10 but not on ch11 is not silence: its ch11 byte 2049 and its two zero data bytes (2048) are 3 range errors",
    not d_r2["silent"][0] and d_r2["range_errors"] == 3,
    str((d_r2["silent"], d_r2["range_errors"])),
)

# ---- lossy-path survival: the measured VB-Cable path (low 5 bits lost, +-32 count steps) leaves the 256-count grid decodable
for name, mask in (("~31 (the measured 5-bit loss)", ~31), ("~127 (7 bits lost)", ~127)):
    dm = D.decode(k & mask)
    check(f"lossy mask {name}: index track exact for all 600 rows", np.array_equal(dm["index"], np.arange(BIG, BIG + 600)), str(dm["index"][:3]))
    check(
        f"lossy mask {name}: every data frame identical (bytes), no corrupt frame, no jump, residual 0, nothing impossible",
        dm["frames"] == dec["frames"]
        and dm["bad_frames"] == 0
        and dm["jumps"] == []
        and dm["impossible"] == 0
        and dm["max_residual"] == 0
        and dm["anchors"] == dec["anchors"],
        f"{len(dm['frames'])} {dm['bad_frames']} {dm['jumps'][:2]} {dm['impossible']}",
    )
kl = k.copy()
flip = np.random.default_rng(9).random(kl[:, 8:13].shape) < 0.10
sign = np.where(np.random.default_rng(10).random(kl[:, 8:13].shape) < 0.5, -32, 32)
kl[:, 8:13] = (kl[:, 8:13] & ~31) + np.where(flip, sign, 0)
dl = D.decode(kl)
check(
    "real path class (low 5 bits masked + a +-32 count step on ~10% of samples): index and every frame still exact; the steps are reported as off-grid (max residual 32)",
    np.array_equal(dl["index"], np.arange(BIG, BIG + 600))
    and dl["frames"] == dec["frames"]
    and dl["max_residual"] == 32
    and dl["impossible"] == int(flip.sum())
    and 200 < int(flip.sum()) < 400
    and dl["range_errors"] == 0,
    f"{dl['impossible']} {int(flip.sum())} {dl['max_residual']}",
)
for dist in (100, -100):
    ko = k.copy()
    ko[:, 8:13] += dist
    dn = D.decode(ko)
    check(
        f"rounding reader: every symbol sample {dist:+d} counts off still decodes exactly (index, frames), and all 3000 are reported (residual {abs(dist)} > 16)",
        np.array_equal(dn["index"], np.arange(BIG, BIG + 600))
        and dn["frames"] == dec["frames"]
        and dn["impossible"] == 3000
        and dn["impossible_first"] == 0
        and dn["max_residual"] == 100,
        f"{dn['impossible']} {dn['impossible_first']} {dn['max_residual']}",
    )
for dist in (129, -129):
    ko = k.copy()
    ko[:, 8:13] += dist
    dn = D.decode(ko)
    check(
        f"{dist:+d} counts is past half a grid step: the symbols move by one, the index track is NOT exact, and it is detected (residual {128 - abs(dist) % 128 if False else 256 - abs(dist)}, all 3000 samples counted)",
        not np.array_equal(dn["index"], np.arange(BIG, BIG + 600)) and dn["impossible"] == 3000 and dn["max_residual"] == 256 - abs(dist),
        f"{dn['impossible']} {dn['max_residual']}",
    )
ko = k.copy()
ko[:, 8:13] += 16
dtol = D.decode(ko)
check(
    "impossible tolerance: +16 counts everywhere is within tolerance (0 reported) and decodes exactly",
    dtol["impossible"] == 0 and np.array_equal(dtol["index"], np.arange(BIG, BIG + 600)) and dtol["frames"] == dec["frames"],
    str(dtol["impossible"]),
)
ko = k.copy()
ko[:, 8:13] += 17
check(
    "impossible tolerance: positive twin - +17 counts everywhere is reported (3000) and still decodes exactly",
    D.decode(ko)["impossible"] == 3000 and D.decode(ko)["frames"] == dec["frames"],
)
kins = np.concatenate([k[:11], np.zeros((30, 16), np.int64), k[11:]])
dins = D.decode(kins)
check(
    "decode: 30 underrun rows inserted INSIDE the first frame are skipped - 30 silent, the frames byte-identical, 0 corrupt, nothing impossible",
    int(dins["silent"].sum()) == 30 and dins["frames"] == dec["frames"] and dins["bad_frames"] == 0 and dins["impossible"] == 0,
    f"{dins['silent'].sum()} {dins['bad_frames']}",
)
for dist in (-128, 127):
    ko = k.copy()
    ko[:, 8:13] += dist
    dn = D.decode(ko)
    check(
        f"asymmetric window: {dist:+d} counts is still INSIDE [-128, +127] - index and frames decode exactly, residual {abs(dist)}",
        np.array_equal(dn["index"], np.arange(BIG, BIG + 600)) and dn["frames"] == dec["frames"] and dn["max_residual"] == abs(dist),
        f"{dn['max_residual']}",
    )
for dist in (128, -129):
    ko = k.copy()
    ko[:, 8:13] += dist
    dn = D.decode(ko)
    check(
        f"asymmetric window: {dist:+d} counts is just OUTSIDE it - the index track is not exact (the symbol moved by one)",
        not np.array_equal(dn["index"], np.arange(BIG, BIG + 600)),
        str(dn["index"][:2]),
    )
ko = k.copy()
ko[10:15, 9] += 40
dsp = D.decode(ko)
check(
    "impossible values are counted per sample and located: 5 samples of ch10 + 40 counts at rows 10-14 -> 5, first row 10, largest distance 40",
    dsp["impossible"] == 5 and dsp["impossible_first"] == 10 and dsp["max_residual"] == 40 and dsp["range_errors"] == 0,
    f"{dsp['impossible']} {dsp['impossible_first']}",
)
ko = k.copy()
ko[10, 9] = cnt(5000)
ko[20, 10] = cnt(300)
ko[30, 11] = cnt(256)
ko[40, 12] = cnt(-5 + 2048 * 0)
dro = D.decode(ko)
check(
    "range errors: symbol 5000 on ch10 (row 10), index byte 300 on ch11 (row 20), data byte 256 on ch12 (row 30) and a symbol below 0 on ch13 (row 40) are 4 range errors, first at row 10",
    dro["range_errors"] == 4 and dro["impossible"] >= 4 and dro["impossible_first"] == 10 and dro["max_residual"] == 0,
    f"{dro['range_errors']} {dro['impossible']} {dro['impossible_first']}",
)
check(
    "clean recording: impossible 0, no first row, residual 0, nothing out of range, nothing on the reserved channels",
    dec["impossible"] == 0 and dec["impossible_first"] is None and dec["max_residual"] == 0 and dec["range_errors"] == 0 and dec["reserved_nonzero"] == 0,
)
ko = k.copy()
ko[7, 13] = 5
ko[8, 15] = -1
dres = D.decode(ko)
check(
    "reserved channels: one non-zero sample on ch14 and one on ch16 are counted (2) and do not disturb the decode",
    dres["reserved_nonzero"] == 2 and dres["impossible"] == 0 and np.array_equal(dres["index"], np.arange(BIG, BIG + 600)),
)
d9 = D.decode(k & ~511)
check(
    "a ~511 mask (9 bits lost) clears bit 8 = bit 0 of the symbol: odd symbols drop by one, the index track steps and frames fail their CRC (no longer exact)",
    not np.array_equal(d9["index"], np.arange(BIG, BIG + 600)) and len(d9["jumps"]) > 0 and d9["frames"] != dec["frames"],
    f"{len(d9['jumps'])} {d9['bad_frames']}",
)

# silence rows (ring underrun) and index jumps (slip)
k2 = np.concatenate([k[:100], np.zeros((50, 16), np.int64), k[300:]])
dec2 = D.decode(k2)
check("decode: underrun rows are flagged silent and skipped", int(dec2["silent"].sum()) == 50, str(int(dec2["silent"].sum())))
check("decode: the slip is reported as one jump (last index before, first index after)", dec2["jumps"] == [(BIG + 99, BIG + 300)], str(dec2["jumps"]))

# corrupt frame: flip a payload byte in the first record; the CRC rejects it and the next frame still decodes
k3 = k.copy()
# the first frame in the FIFO is the first board record; row 3 carries frame bytes 6-7: ch12 the length low byte, ch13 payload byte 0
k3[3, 12] += 256  # ch13 of row 3 is payload byte 0: one symbol step changes it ('{' -> '|')
dec3 = D.decode(k3)
check(
    "decode: a corrupted frame is dropped and counted",
    dec3["bad_frames"] >= 1 and len(dec3["board"]) == 1 and dec3["board"][0]["type"] == "meter",
    f"{dec3['bad_frames']} {len(dec3['board'])}",
)

# data track overflow is counted, not silently lost
t = S.DataTrack()
big = b"x" * 60000
pushed = [t.push(S.K_BOARD, big) for _ in range(6)]
check("data: records beyond the FIFO cap are refused and counted", pushed == [True] * 4 + [False] * 2 and t.dropped == 2, str((pushed, t.dropped)))

# data track: 2 bytes per row, one byte per channel (ch12 first), each a symbol u = byte value, idle u = 0
tk = S.DataTrack()
pl_t = bytes([0xFF, 0x80, 0x7F, 0x00, 0x80, 0x00, 0x01, 0xFE, 0x00])
fr_t = S.encode_frame(S.K_BOARD, pl_t)  # 7 + 9 + 4 = 20 bytes
tk.push(S.K_BOARD, pl_t)
t_first = tk.take(5)
check(
    "datatrack: take(5) returns shape (5, 2), consumes exactly 10 bytes, byte i of the row pair on channel i (u = byte value, count (u - 2048) << 8)",
    t_first.shape == (5, 2) and len(tk.fifo) == len(fr_t) - 10 and rows_of(t_first).tolist() == [[cnt(fr_t[2 * i]), cnt(fr_t[2 * i + 1])] for i in range(5)],
    str(rows_of(t_first)),
)
t_rest = tk.take(7)
check(
    "datatrack: the remaining 10 bytes are 5 rows, then idle rows are exactly (-524288, -524288); bytes 0xFF and 0x00 (the extremes) are exercised",
    rows_of(t_rest).tolist() == [[cnt(fr_t[10 + 2 * i]), cnt(fr_t[11 + 2 * i])] for i in range(5)] + [[-524288, -524288]] * 2
    and len(tk.fifo) == 0
    and 0xFF in fr_t
    and 0x00 in fr_t[:-4],
    str(rows_of(t_rest)),
)
tk.push(S.K_BOARD, b"x" * 50)  # 61 bytes, odd
t_odd = rows_of(tk.take(31))
fr61 = S.encode_frame(S.K_BOARD, b"x" * 50)
check(
    "datatrack: an odd-length frame ends with its last byte on ch12 of the last row and ch13 padded with the idle byte 0",
    t_odd[30].tolist() == [cnt(fr61[60]), cnt(0)] and len(tk.fifo) == 0,
    str(t_odd[28:]),
)
check(
    "datatrack: byte 255 is count (255 - 2048) << 8 = -459008, inside the rows' range",
    cnt(255) == -459008 and S.sym12_to_f32(np.array([255]))[0] * 8388608.0 == -459008.0,
)

# ---- the earlier burst scenario (about 19,000 board-debug records in 60 s) against the 96 kB/s track, simulated in track time
REC = len(S.encode_frame(S.K_BOARD, b"r" * 205))  # 216 bytes, the size of a real board record frame


def burst(n_records, seconds, small=False, headroom=None):
    """Records arrive evenly over `seconds` of track time; the audio takes 480 rows (960 bytes) per 10 ms tick; a record waits
    for room at most 5 s (500 ticks), exactly like push_wait. With `small`, an anchor (16 B payload) is pushed without waiting every 0.5 s and a band record (1500 B)
    every 1 s, like the sink does. -> (delivered, dropped, longest wait in ticks, bytes delivered, small accepted, small attempted)."""
    tr = S.DataTrack(headroom=headroom)
    small_ok = small_all = 0
    waiting, delivered, dropped, longest, taken = [], 0, 0, 0, 0
    ticks, arrived = int(seconds * 100), 0
    t = 0
    while t < ticks or waiting or len(tr.fifo):
        if t < ticks:
            arrived = n_records * (t + 1) // ticks
            while len(waiting) + delivered + dropped < arrived:
                waiting.append(t)
        taken += len(tr.fifo[:960])
        tr.take(480)
        if small and t < ticks:
            if t % 50 == 0:
                small_all += 1
                small_ok += tr.push(S.K_ANCHOR, b"a" * 16)
            if t % 100 == 50:
                small_all += 1
                small_ok += tr.push(S.K_BANDS, b"b" * 1500)
        while waiting:
            if tr.offer(S.K_BOARD, b"r" * 205):
                longest = max(longest, t - waiting.pop(0))
                delivered += 1
            elif t - waiting[0] >= 500:
                waiting.pop(0)
                dropped += 1
            else:
                break
        t += 1
        if t > 100000:
            break
    return delivered, dropped, longest, taken, small_ok, small_all


check(
    "burst: one 205-byte board record is a 216-byte frame, so 19,000 records in 60 s need 68.4 kB/s of the 96 kB/s track",
    REC == 216 and 19000 * REC / 60 == 68400.0 and 68400.0 < 96000.0,
)
bd, bdrop, blong, btaken, _, _ = burst(19000, 60)
check(
    "burst: 19,000 records arriving over 60 s are ALL delivered (19000, 0 dropped), none waits more than 5 s, and the audio carried every byte",
    (bd, bdrop) == (19000, 0) and blong < 500 and btaken == 19000 * REC,
    f"{bd} {bdrop} {blong} {btaken}",
)
bd2, bdrop2, blong2, _, _, _ = burst(19000, 30)
check(
    "burst: positive twin - the same 19,000 records in 30 s need 136.8 kB/s > 96 kB/s: some wait the full 5 s and are dropped and counted, none lost silently",
    bdrop2 > 0 and bd2 + bdrop2 == 19000 and blong2 >= 499,
    f"{bd2} {bdrop2} {blong2}",
)

bh = burst(19000, 40, small=True)
check(
    "burst headroom: 19,000 records in 40 s (102 kB/s > the 96 kB/s drain, the queue stays at the high-water mark) are ALL delivered (0 dropped) AND every anchor and band record is accepted (120 of 120)",
    (bh[0], bh[1]) == (19000, 0) and bh[2] < 500 and (bh[4], bh[5]) == (120, 120),
    str(bh),
)
bh0 = burst(19000, 40, small=True, headroom=0)
check(
    "burst headroom: positive twin - with NO reserved headroom the same backlog starves the non-blocking records (fewer than 120 of 120 accepted) although every board record still arrives",
    (bh0[0], bh0[1]) == (19000, 0) and bh0[4] < 120 and bh0[5] == 120,
    str(bh0),
)
tq1 = S.DataTrack(max_bytes=1000, headroom=200)
fill = []
while tq1.offer(S.K_BOARD, b"y" * 50):
    fill.append(1)
pushed_small = [tq1.push(S.K_BOARD, b"y" * 50) for _ in range(4)]
check(
    "headroom: offer() stops at the high-water mark - 1000 B queue, 200 B reserved: 13 frames of 61 B (793 B) are accepted, the 14th is not; push() then fits 3 more into the headroom (976 B) and refuses the 4th",
    len(fill) == 13 and pushed_small == [True, True, True, False] and len(tq1.fifo) == 976,
    str((len(fill), pushed_small, len(tq1.fifo))),
)


# ---- PortAudio re-initialisation on the device-open retry
class StubStream:
    def __init__(self, **kw):
        self.kw = kw
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def close(self):
        self.closed = True


class StubSD:
    """A recording stand-in for the sounddevice module: every call lands in .calls in order."""

    def __init__(self, channels, terminate_raises=False):
        self.calls = []
        self.channels = channels
        self.terminate_raises = terminate_raises
        self.streams = []

    def _terminate(self):
        self.calls.append("_terminate")
        if self.terminate_raises:
            raise RuntimeError("PortAudio not initialized")

    def _initialize(self):
        self.calls.append("_initialize")

    def query_hostapis(self):
        self.calls.append("query_hostapis")
        return [{"name": "MME"}, {"name": "Windows WASAPI"}]

    def query_devices(self):
        self.calls.append("query_devices")
        return [
            {"name": "Speakers", "hostapi": 1, "max_output_channels": 2},
            {"name": "CABLE In 16ch (VB-Audio Virtual Cable)", "hostapi": 1, "max_output_channels": self.channels},
        ]

    def WasapiSettings(self, auto_convert):
        self.calls.append(f"WasapiSettings(auto_convert={auto_convert})")
        return ("wasapi", auto_convert)

    def OutputStream(self, **kw):
        self.calls.append("OutputStream")
        st = StubStream(**kw)
        self.streams.append(st)
        return st


LOGS = []
sd1 = StubSD(2)
op = S.OutputOpener(sd1, "CABLE In 16", lambda *a: None, log=LOGS.append)
op.try_open()
op.try_open()
CYCLE = ["_terminate", "_initialize", "query_hostapis", "query_devices"]
check(
    "reinit: each failed retry runs exactly terminate -> initialize -> query_hostapis -> query_devices, twice in a row",
    sd1.calls == CYCLE * 2 and op.stream is None,
    str(sd1.calls),
)
check(
    "reinit: the 2-channel endpoint is reported once (the same error is not logged again) with the channel count",
    len(LOGS) == 1 and "configured for 2 channel(s), need 16" in LOGS[0] and op.err is not None and "2 channel(s)" in op.err,
    str(LOGS),
)
sd1.channels = 16  # the operator sets the Windows format to 16 channels
sd1.calls.clear()
op.try_open()
check(
    "reinit: after the format change the very next retry sees 16 channels and opens: call order terminate, initialize, queries, WasapiSettings, OutputStream",
    sd1.calls == CYCLE + ["WasapiSettings(auto_convert=False)", "OutputStream"]
    and op.stream is not None
    and op.dev == "CABLE In 16ch (VB-Audio Virtual Cable)",
    str(sd1.calls),
)
st0 = sd1.streams[0]
check(
    "reinit: the stream is opened as the sink always did - device 1, 48000 Hz, 16 ch, float32, latency high, shared-mode auto_convert False - and started",
    (st0.kw["device"], st0.kw["samplerate"], st0.kw["channels"], st0.kw["dtype"], st0.kw["latency"], st0.kw["extra_settings"])
    == (1, 48000, 16, "float32", "high", ("wasapi", False))
    and st0.started
    and op.err is None
    and LOGS[-1] == "playing 16 ch @ 48000 Hz into 'CABLE In 16ch (VB-Audio Virtual Cable)'",
    str(LOGS),
)
sd1.calls.clear()
for _ in range(5):
    op.try_open()
check(
    "reinit: while the stream is open NOTHING touches PortAudio - five retries make zero calls (no terminate, no initialize, no query) and open no second stream",
    sd1.calls == [] and len(sd1.streams) == 1 and op.stream is st0,
    str(sd1.calls),
)
sd2 = StubSD(2, terminate_raises=True)
op2 = S.OutputOpener(sd2, "CABLE In 16", lambda *a: None, log=lambda m: None)
op2.try_open()
check(
    "reinit: a terminate that raises (PortAudio already down) does not skip initialize - the cycle still runs and the error is the channel count",
    sd2.calls == CYCLE and "2 channel(s)" in (op2.err or ""),
    str(sd2.calls),
)


class NoReinitSD(StubSD):
    _terminate = None
    _initialize = None


sd3 = NoReinitSD(16)
op3 = S.OutputOpener(sd3, "CABLE In 16", lambda *a: None, log=lambda m: None)
op3.try_open()
check(
    "reinit: a sounddevice without the private _terminate/_initialize still opens (no re-init, no crash)",
    sd3.calls == ["query_hostapis", "query_devices", "WasapiSettings(auto_convert=False)", "OutputStream"] and op3.stream is not None,
    str(sd3.calls),
)
op4 = S.OutputOpener(None, "CABLE In 16", lambda *a: None, log=lambda m: None)
op4.try_open()
check("reinit: without the sounddevice module try_open is a no-op", op4.stream is None and op4.err is None)

# ---- the operator self-test (pc/bma_cable_selftest.py) against fake loopbacks
import bma_cable_selftest as T  # noqa: E402


class FakeLoop:
    """A cable stand-in: precapture returns `pre`; play_record returns the frames after a loss model on the 24-bit counts
    with leading and trailing silence like a real recording. Models: `mask` (AND on the counts), `steps` (a +-32 count
    step on ~10% of the NON-ZERO samples, the measured real-path class together with the 5-bit mask) and `gain`.
    Records its calls."""

    def __init__(
        self,
        mask=-1,
        pre=None,
        drop_head=0,
        steps=False,
        gain=1.0,
        disturb=0,
        corrupt_data=False,
        fade=0,
        drop_rows=0,
        dup_rows=0,
        noise14=0,
        loud_err=0,
        index_noise=0,
    ):
        self.mask, self.pre, self.drop_head, self.steps, self.gain = mask, pre, drop_head, steps, gain
        self.disturb, self.corrupt_data, self.fade = disturb, corrupt_data, fade
        self.drop_rows, self.dup_rows, self.noise14, self.loud_err = drop_rows, dup_rows, noise14, loud_err
        self.index_noise = index_noise
        self.played = 0

    def lose(self, counts):
        c = counts.copy()
        if self.drop_rows:  # delete rows inside the idle (data-free) part of the verified stretch
            c = np.delete(c, np.s_[T.LEAD + 10000 : T.LEAD + 10000 + self.drop_rows], axis=0)
        if self.dup_rows:  # repeat rows there
            at = T.LEAD + 10000
            c = np.concatenate([c[: at + self.dup_rows], c[at:]], axis=0)
        if self.loud_err:  # beams only, in the loud (INFO-only) segment: the last 24000 verified rows
            c[T.LEAD + 96000 - T.LOUD : T.LEAD + 96000, :8] += self.loud_err
        if self.index_noise:  # ch9 one symbol step on `index_noise` idle rows: ONLY the index differs
            c[T.LEAD + 10000 : T.LEAD + 10000 + self.index_noise, 8] += 256
        c = c & self.mask
        if self.steps:
            r = np.random.default_rng(77)
            flip = (r.random(c.shape) < 0.10) & (c != 0)
            c = c + np.where(flip, np.where(r.random(c.shape) < 0.5, -32, 32), 0)
        if self.gain != 1.0:
            c = np.rint(c * self.gain).astype(np.int64)
        if self.disturb:
            c[:, 8:13] += self.disturb  # symbol channels only: a constant offset, under half a grid step
        if self.corrupt_data:
            c[T.LEAD + 40 : T.LEAD + 60, 11:13] += 256  # one symbol step on 20 rows of the data track, the index untouched
        if self.noise14:
            c[:, 13] += self.noise14  # reserved channel 14 only
        if self.fade:  # the stream-open fade: a linear ramp over the first `fade` rows
            c[: self.fade] = np.rint(c[: self.fade] * (np.arange(self.fade)[:, None] / self.fade)).astype(np.int64)
        return c

    def precapture(self, seconds):
        return np.zeros((48000, 16), np.float32) if self.pre is None else self.pre

    def play_record(self, frames):
        self.played += 1
        kk = self.lose(T.to_counts(frames[self.drop_head :])).astype(np.float64) / 8388608.0
        return np.vstack([np.zeros((777, 16), np.float32), kk.astype(np.float32), np.zeros((5000, 16), np.float32)])


def selftest(loop):
    lines = []
    return T.run(loop, 2.0, out=lines.append), lines


rc, ln = selftest(FakeLoop())
sent2, n2, counts2 = T.build(2.0)
L0, QUIET = T.LEAD, 96000 - T.LOUD
nframes = len(D.decode(T.to_counts(sent2[L0 : L0 + 96000]))["frames"])
check(
    "selftest: a bit-exact path passes (exit 0) with the exact index, data, format and beam lines",
    rc == 0
    and ln
    == [
        "PASS index: all 96000 rows recovered exactly (u32 index 305419776 .. 305515775)",
        f"PASS data: all {nframes} BMD1 frames recovered, byte for byte, in order, 0 corrupt",
        "PASS format: symbols on the grid (largest distance 0 counts, limit 64), reserved channels 14-16 all zero",
        "PASS beams: bit-exact (the path preserved all 24 bits)",
        "INFO loud segment (beams at 0.3 FS, 24000 rows, not part of the verdict): max error 0 counts",
        "SELFTEST PASSED",
    ],
    str((rc, ln)),
)
real = FakeLoop(mask=~31, steps=True)
lost = real.lose(T.to_counts(sent2))
worst = int(np.abs(lost[L0 : L0 + QUIET, :8] - counts2[:QUIET]).max())
worst_loud = int(np.abs(lost[L0 + QUIET : L0 + 96000, :8] - counts2[QUIET:96000]).max())
rc, ln = selftest(FakeLoop(mask=~31, steps=True))
check(
    "selftest: the REAL measured class (low 5 bits masked AND a +-32 count step on ~10% of samples) passes (exit 0): index, data exact, symbols 32 counts from the grid, beams within the limit",
    rc == 0
    and 32 < worst <= 64
    and ln
    == [
        "PASS index: all 96000 rows recovered exactly (u32 index 305419776 .. 305515775)",
        f"PASS data: all {nframes} BMD1 frames recovered, byte for byte, in order, 0 corrupt",
        "PASS format: symbols on the grid (largest distance 32 counts, limit 64), reserved channels 14-16 all zero",
        f"PASS beams: within the known path loss (max error {worst} counts, limit 64)",
        f"INFO loud segment (beams at 0.3 FS, 24000 rows, not part of the verdict): max error {worst_loud} counts",
        "SELFTEST PASSED",
    ],
    str((rc, worst, ln)),
)
rc, ln = selftest(FakeLoop(mask=~31))
check(
    "selftest: the 5-bit mask alone passes (exit 0) with beam error 31 and symbols exactly on the grid",
    rc == 0
    and "PASS beams: within the known path loss (max error 31 counts, limit 64)" in ln
    and ln[2].startswith("PASS format: symbols on the grid (largest distance 0 counts"),
    str((rc, ln)),
)
rc, ln = selftest(FakeLoop(gain=0.985))
check(
    "selftest: a path with the measured 0.985 gain FAILS (exit 1): the stream start index is not found in the recording",
    rc == 1 and ln[-1] == "SELFTEST FAILED" and ln[0].startswith("FAIL index:"),
    str((rc, ln)),
)
rc, ln = selftest(FakeLoop(gain=0.9999))
check(
    "selftest: positive twin - a 0.9999 gain (error under half a grid step on the symbols) still decodes the index and data exactly; only the format residual or beams may complain",
    ln[0].startswith("PASS index: all 96000 rows") and ln[1].startswith(f"PASS data: all {nframes} BMD1 frames"),
    str((rc, ln)),
)
rc, ln = selftest(FakeLoop(disturb=100))
check(
    "selftest: a +100 count offset on the symbol channels (symbols still decode, 100 counts off the grid, limit 64) FAILS on the format line, with index and data PASS",
    rc == 1
    and ln[0].startswith("PASS index:")
    and ln[1].startswith("PASS data:")
    and ln[2]
    == "FAIL format: largest distance from the symbol grid 100 counts (limit 64), 0 out-of-range symbols/bytes, 480000 off-grid/impossible samples (first at row 0), 0 non-zero reserved samples",
    str((rc, ln)),
)
rc, ln = selftest(FakeLoop(corrupt_data=True))
check(
    "selftest: one symbol step on 20 rows of the data track (index intact) FAILS (exit 1) on the data line only, and the data line says which kinds were lost",
    rc == 1
    and ln[0].startswith("PASS index:")
    and ln[1].startswith("FAIL data: frames by kind")
    and ln[2].startswith("PASS format:")
    and ln[-1] == "SELFTEST FAILED",
    str((rc, ln)),
)
rc, ln = selftest(FakeLoop(mask=~31, steps=True, fade=480))
check(
    "selftest: a HEALTHY real-class path whose new stream fades in over the first 480 rows still passes (exit 0): the 1000-row zero lead-in absorbs the fade",
    rc == 0 and ln[-1] == "SELFTEST PASSED" and ln[0].startswith("PASS index: all 96000 rows") and ln[1].startswith(f"PASS data: all {nframes} BMD1 frames"),
    str((rc, ln)),
)
T.LEAD = 0
sent0, n0, counts0 = T.build(2.0)
rc0, ln0 = selftest(FakeLoop(mask=~31, steps=True, fade=480))
T.LEAD = 1000
check(
    "selftest: regression - WITHOUT the lead-in the same fade destroys row 0 (the first index) and the test exits 1, 'first sent index ... never appears'",
    rc0 == 1 and ln0[0].startswith("FAIL index: the first sent index 305419776 never appears") and len(sent0) == 96000 + T.TAIL,
    str((rc0, ln0, len(sent0))),
)
rc, ln = selftest(FakeLoop(index_noise=50))
check(
    "selftest: ONLY the index differs (50 rows with ch9 one symbol step off; data, format and beams all fine) -> exit 1 on the index line alone",
    rc == 1
    and ln[0].startswith("FAIL index: 50 of 96000 rows differ, first at row 10000")
    and ln[1].startswith("PASS data:")
    and ln[2].startswith("PASS format:")
    and ln[3].startswith("PASS beams:")
    and ln[-1] == "SELFTEST FAILED",
    str((rc, ln)),
)
rc, ln = selftest(FakeLoop(drop_rows=50))
check(
    "selftest: 50 rows DELETED mid-recording with every data frame intact fails (exit 1) on the index line",
    rc == 1 and ln[0].startswith("FAIL index:") and ln[-1] == "SELFTEST FAILED",
    str((rc, ln)),
)
rc, ln = selftest(FakeLoop(dup_rows=50))
check(
    "selftest: 50 rows DUPLICATED mid-recording with every data frame intact fails (exit 1) on the index line",
    rc == 1 and ln[0].startswith("FAIL index:") and ln[-1] == "SELFTEST FAILED",
    str((rc, ln)),
)
rc, ln = selftest(FakeLoop(noise14=5))
check(
    "selftest: noise only on reserved channel 14 (5 counts) fails (exit 1) on the format line with 96000 non-zero reserved samples; index, data, beams PASS",
    rc == 1
    and ln[0].startswith("PASS index:")
    and ln[1].startswith("PASS data:")
    and ln[3].startswith("PASS beams:")
    and ln[2].endswith("0 out-of-range symbols/bytes, 0 off-grid/impossible samples (first at row None), 96000 non-zero reserved samples")
    and ln[2].startswith("FAIL format:"),
    str((rc, ln)),
)
rc, ln = selftest(FakeLoop(loud_err=1000))
check(
    "selftest: an error of 1000 counts on the loud (0.3 FS) beam segment is INFO ONLY - exit 0, PASS beams on the -40 dBFS rows, and the INFO line shows max error 1000",
    rc == 0
    and ln[3] == "PASS beams: bit-exact (the path preserved all 24 bits)"
    and ln[4] == "INFO loud segment (beams at 0.3 FS, 24000 rows, not part of the verdict): max error 1000 counts"
    and ln[-1] == "SELFTEST PASSED",
    str((rc, ln)),
)
rc, ln = selftest(FakeLoop(mask=~511))
check(
    "selftest: a stand-in that loses 9 bits (counts & ~511) FAILS with exit 1 on the index",
    rc == 1 and ln[-1] == "SELFTEST FAILED" and ln[0].startswith("FAIL index:"),
    str((rc, ln)),
)
rc, ln = selftest(FakeLoop(mask=~255))
check(
    "selftest: 8 bits lost (counts & ~255) keeps the symbols (index, data, format PASS) but FAILS exit 1 on the beams (error 255 > 64)",
    rc == 1
    and ln[0].startswith("PASS index:")
    and ln[1].startswith("PASS data:")
    and ln[2].startswith("PASS format:")
    and ln[3] == "FAIL beams: max error 255 counts exceeds the known path loss (limit 64)",
    str((rc, ln)),
)
rc, ln = selftest(FakeLoop(drop_head=3000))
check(
    "selftest: a recording that lost the start of the stream FAILS (exit 1) because the first index never appears",
    rc == 1 and len(ln) == 2 and ln[0].startswith("FAIL index: the first sent index 305419776 never appears") and ln[1] == "SELFTEST FAILED",
    str((rc, ln)),
)
for col, nm in ((8, "ch9"), (9, "ch10"), (10, "ch11")):
    pre = np.zeros((48000, 16), np.float32)
    pre[30000, col] = 256 / 8388608.0
    lp = FakeLoop(pre=pre)
    rc, ln = selftest(lp)
    check(
        f"selftest: one non-zero sample on {nm} before the test (another stream is writing) -> refuses, exit 2, nothing played",
        rc == 2 and lp.played == 0 and len(ln) == 1 and ln[0].startswith("NOT RUN: 1 non-zero samples on channels 9-11") and "Stop it" in ln[0],
        str((rc, ln, lp.played)),
    )
pre = np.zeros((48000, 16), np.float32)
pre[:, 0] = 0.01
pre[:, 11:] = 0.5
lp = FakeLoop(pre=pre)
rc, ln = selftest(lp)
check(
    "selftest: positive twin - noise on beam channel 1 and the reserved channels only does NOT refuse: it runs once and passes",
    rc == 0 and lp.played == 1 and ln[-1] == "SELFTEST PASSED",
    str((rc, ln[-1:], lp.played)),
)
lp = FakeLoop(pre=np.zeros((100, 16), np.float32))
rc, ln = selftest(lp)
check(
    "selftest: a pre-capture of only 100 rows (the input is not delivering) -> exit 2, nothing played",
    rc == 2 and lp.played == 0 and ln[0].startswith("NOT RUN: only 100 rows"),
    str((rc, ln)),
)


def tone(beam, hz, amp, n=48000, offset=0):
    x = np.zeros((n, 8), np.float32)
    x[:, beam] = (amp * np.sin(2 * np.pi * hz * (np.arange(n) + offset) / 48000.0)).astype(np.float32)
    return x


bm = S.BandMeter(clock_us=lambda: 1_792_900_000_000_000)
recs = bm.push(1_000_000, tone(2, 1500.0, 0.1))
check(
    "bands: one record per 48000 contiguous samples, idx = first sample, 8 beams x 8 bands",
    len(recs) == 1 and recs[0]["idx"] == 1_000_000 and len(recs[0]["db"]) == 8 and all(len(r) == 8 for r in recs[0]["db"]),
    str(recs and recs[0]["idx"]),
)
r0 = recs[0]
check(
    "bands: a 0.1-amplitude 1.5 kHz tone in beam 3 reads -23.0 dB (mean-square 0.005) in the 1-2 kHz band",
    abs(r0["db"][2][4] - (-23.01)) < 0.15,
    str(r0["db"][2][4]),
)
check("bands: that beam has nothing (< -90 dB) in the other seven bands", all(r0["db"][2][b] < -90 for b in range(8) if b != 4), str(r0["db"][2]))
check("bands: the other seven beams read exactly -200.0 in every band (no energy)", all(r0["db"][b][k] == -200.0 for b in range(8) if b != 2 for k in range(8)))
check("bands: record carries the PC clock and the band edges", r0["t_us"] == 1_792_900_000_000_000 and r0["edges_hz"][4] == [1000, 2000] and r0["n"] == 48000)
r1 = S.BandMeter().push(0, tone(5, 1500.0, 0.1))[0]
check(
    "bands: positive twin - the same tone in beam 6 moves the energy to row 5 and empties row 2",
    abs(r1["db"][5][4] - (-23.01)) < 0.15 and all(r1["db"][2][k] == -200.0 for k in range(8)),
    str(r1["db"][5][4]),
)
r2 = S.BandMeter().push(0, tone(0, 300.0, 0.1))[0]
check(
    "bands: a 300 Hz tone lands in the 250-500 Hz band (index 2), not its neighbours",
    abs(r2["db"][0][2] - (-23.01)) < 0.15 and r2["db"][0][1] < -60 and r2["db"][0][3] < -60,
    str(r2["db"][0][:4]),
)
r3 = S.BandMeter().push(0, tone(0, 1500.0, 0.2))[0]
check(
    "bands: doubling the amplitude (0.1 -> 0.2) adds 6.02 dB in the same band",
    abs((r3["db"][0][4] - r0["db"][2][4]) - 6.02) < 0.05,
    str(r3["db"][0][4] - r0["db"][2][4]),
)

# windows are chunked on contiguous audio only
bm = S.BandMeter()
got = []
pos = 5000
for _ in range(100000 // 256):  # 99,840 samples in 256-sample frames
    got += bm.push(pos, tone(1, 1500.0, 0.1, n=256, offset=pos))
    pos += 256
check("bands: 99,840 contiguous samples -> exactly 2 records at idx 5000 and 53000", [g["idx"] for g in got] == [5000, 53000], str([g["idx"] for g in got]))
bm = S.BandMeter()
a1 = bm.push(0, tone(1, 1500.0, 0.1, n=30000))
a2 = bm.push(90000, tone(1, 1500.0, 0.1, n=48000))  # a hole: the 30000 buffered samples are discarded
check(
    "bands: a hole restarts the window - no record from the 30000 stale + new, one record at the post-hole idx",
    a1 == [] and len(a2) == 1 and a2[0]["idx"] == 90000,
    str(a2 and a2[0]["idx"]),
)
bm = S.BandMeter()
b1 = bm.push(0, tone(1, 1500.0, 0.1, n=30000))
b2 = bm.push(30000, tone(1, 1500.0, 0.1, n=18000))
check("bands: positive twin - the same two pieces, contiguous, do make one record at idx 0", b1 == [] and len(b2) == 1 and b2[0]["idx"] == 0)
zx = tone(3, 1500.0, 0.1)
zx[:12000] = 0
rz = S.BandMeter().push(0, zx)[0]
check("bands: zero_frac is the share of all-zero rows (12000 of 48000 = 0.25, not 0.75)", abs(rz["zero_frac"] - 0.25) < 0.001, str(rz["zero_frac"]))


# the band record rides the data track and is logged identically
class RecLog:
    def __init__(self):
        self.lines = []

    def write(self, line):
        self.lines.append(line)


data = S.DataTrack()
comp = S.Composer(data, anchor_every=10**9, clock_us=lambda: 1_792_900_000_000_000)
log = RecLog()
bm = S.BandMeter(window=4800, clock_us=lambda: 1_792_900_000_000_000)
START = (2 << 24) | 777
beams = tone(4, 1500.0, 0.1, n=4800)
S.feed_bands(bm, START, beams, data, log)
frame = comp.compose(START, beams)
dec = D.decode(np.rint(frame.astype(np.float64) * 8388608.0).astype(np.int64))
check(
    "bands: one line logged and one band record recovered from the data track",
    len(log.lines) == 1 and len(dec["bands"]) == 1,
    f"{len(log.lines)} {len(dec['bands'])}",
)
logged = json.loads(log.lines[0])
check(
    "bands: the data-track record equals the logged line and is placed on its own card index",
    {k: v for k, v in dec["bands"][0].items() if k != "card_index"} == logged and dec["bands"][0]["card_index"] == START,
    str(dec["bands"][0]["card_index"]),
)
check("bands: the 1.5 kHz tone is in beam 5 / band 4 after the round trip", abs(logged["db"][4][4] - (-23.01)) < 0.3, str(logged["db"][4][4]))
data2 = S.DataTrack()
S.feed_bands(S.BandMeter(window=4800), START, beams[:2400], data2, None)
check("bands: positive twin - half a window yields no record and nothing on the data track", data2.frames == 0 and len(data2.fifo) == 0)

# ---- data track backpressure: a burst of board-debug backlog must be delivered, not dropped
import tempfile, threading, time


def timed(fn):
    t = time.monotonic()
    r = fn()
    return r, time.monotonic() - t


FRAME = len(S.encode_frame(S.K_BOARD, b"x" * 50))  # 61 bytes
small = S.DataTrack(max_bytes=3 * FRAME)
check(
    "data: a queue sized for exactly 3 frames accepts 3 and refuses the 4th without waiting (push never blocks)",
    [small.push(S.K_BOARD, b"x" * 50) for _ in range(3)] == [True] * 3
    and timed(lambda: small.push(S.K_BOARD, b"x" * 50))[0] is False
    and small.dropped == 1
    and small.frames == 3,
    f"{small.frames} {small.dropped}",
)
res, took = timed(lambda: small.push_wait(S.K_BOARD, b"x" * 50, timeout_s=0.3))
check(
    "data: push_wait on a queue that never drains gives up after its timeout and counts exactly one more drop",
    res is False and 0.25 <= took < 1.0 and small.dropped == 2,
    f"{res} {took:.2f} {small.dropped}",
)
small.dropped = 0
res, took = timed(lambda: small.push_wait(S.K_BOARD, b"x" * 50))  # the default timeout: 5 s of a full queue = the audio has stopped
check(
    "data: the default push_wait timeout is 5 s - on a queue that never drains it gives up after 5.0-5.5 s and counts one drop",
    res is False and 4.95 <= took < 5.5 and small.dropped == 1,
    f"{res} {took:.2f} {small.dropped}",
)
small.dropped = 2
drain = threading.Timer(0.2, lambda: small.take(100))  # the audio consuming 200 bytes (100 rows) after 0.2 s
drain.start()
res, took = timed(lambda: small.push_wait(S.K_BOARD, b"y" * 50, timeout_s=3.0))
drain.join()
check(
    "data: positive twin - push_wait on a full queue waits for the drain and then succeeds with no new drop",
    res is True and 0.15 <= took < 2.0 and small.dropped == 2 and small.frames == 4,
    f"{res} {took:.2f} {small.dropped} {small.frames}",
)
check("data: what push_wait queued is the frame, intact (CRC-checked frame decodes back)", bytes(small.fifo) == S.encode_frame(S.K_BOARD, b"y" * 50))

tmpd = tempfile.mkdtemp()
logp = os.path.join(tmpd, "board_debug.jsonl")
with open(logp, "wb") as f:
    f.write(b'{"old":1}\n{"old":2}\n')
got, stop = [], threading.Event()
th = threading.Thread(target=S.tail_lines, args=(logp, stop, got.append, 0.05), daemon=True)
th.start()
time.sleep(0.4)
lines = [b'{"i":%d,"pad":"%s"}' % (i, b"p" * 80) for i in range(5000)]  # about 500 KB appended in one write
with open(logp, "ab") as f:
    f.write(b"\n".join(lines) + b"\n")
t0 = time.monotonic()
while len(got) < 5000 and time.monotonic() - t0 < 10:
    time.sleep(0.05)
stop.set()
check(
    "tail: a 500 KB burst appended in one write is delivered completely and in order, and the older lines are not",
    got == lines,
    f"{len(got)} lines, first {got[:1]}",
)

# end to end: tail -> push_wait -> a queue far smaller than the burst, drained like the audio drains it
tq = S.DataTrack(max_bytes=64 * 1024)
logp2 = os.path.join(tmpd, "board_debug2.jsonl")
open(logp2, "wb").close()
stop2 = threading.Event()
th2 = S.follow_board_debug(logp2, stop2, tq, 0.05)  # the same helper main() and --record use
time.sleep(0.3)
with open(logp2, "ab") as f:
    f.write(b"\n".join(lines[:3000]) + b"\n")  # about 300 KB into a 64 KB queue
taken = bytearray()
t0 = time.monotonic()
while tq.frames + tq.dropped < 3000 or len(tq.fifo) > 0:
    with tq.lock:
        taken += tq.fifo[:1920]
        del tq.fifo[:1920]  # the audio taking 1920 bytes every 20 ms = 96 kB/s
    time.sleep(0.02)
    if time.monotonic() - t0 > 20:
        break
stop2.set()
check(
    "data: end to end - a 300 KB burst through a 64 KB queue loses nothing (3000 accepted, 0 dropped) and arrives intact",
    tq.frames == 3000 and tq.dropped == 0 and len(taken) == sum(len(S.encode_frame(S.K_BOARD, l)) for l in lines[:3000]),
    f"{tq.frames} {tq.dropped} {len(taken)}",
)
tq0 = S.DataTrack(max_bytes=64 * 1024)
for ln in lines[:3000]:
    tq0.push(S.K_BOARD, ln)
check(
    "data: positive twin of the above - the same burst pushed with the non-waiting push into the same queue DOES drop",
    tq0.dropped > 0 and tq0.frames + tq0.dropped == 3000,
    f"{tq0.frames} {tq0.dropped}",
)


# ---- the retry schedule of the output open (OpenRetry) with a fake clock
class FakeOpener:
    def __init__(self):
        self.stream = None
        self.calls = 0
        self.raises = False

    def try_open(self):
        self.calls += 1
        if self.raises:
            raise RuntimeError("boom")


fo = FakeOpener()
rt = S.OpenRetry(fo)
r_ = [rt.tick(100.0), rt.tick(114.9), rt.tick(115.0), rt.tick(120.0), rt.tick(129.9), rt.tick(130.0)]
check(
    "retry: attempts at t = 100, 115 and 130 only - never earlier than 15 s after the previous one (14.9 s and 129.9 s are not due)",
    r_ == [True, False, True, False, False, True] and fo.calls == 3,
    str((r_, fo.calls)),
)
fo.raises = True
try:
    rt.tick(145.0)
    exc = None
except RuntimeError as e_:
    exc = str(e_)
r_ = [rt.tick(145.1), rt.tick(159.9)]
check(
    "retry: an exception inside try_open still backs off 15 s (the call at 145 raised, 145.1 and 159.9 are not due)",
    exc == "boom" and r_ == [False, False] and fo.calls == 4,
    str((exc, r_, fo.calls)),
)
fo.raises = False
check("retry: positive twin - 160.0 is due again", rt.tick(160.0) is True and fo.calls == 5)
fo.stream = object()
r_ = [rt.tick(200.0), rt.tick(215.0), rt.tick(300.0)]
fo.stream = None
check(
    "retry: while the stream is open nothing is called, and when it is lost the very next tick retries at once (schedule reset)",
    r_ == [False, False, False] and fo.calls == 5 and rt.tick(300.1) is True and fo.calls == 6,
    str((r_, fo.calls)),
)


# ---- open_output_stream: a stream that fails to start is closed, and the next retry works
class FailStartSD(StubSD):
    def __init__(self, channels, fail_first=True):
        super().__init__(channels)
        self.fail_first = fail_first

    def OutputStream(self, **kw):
        self.calls.append("OutputStream")
        st = StubStream(**kw)
        if self.fail_first and not self.streams:

            def bad_start():
                raise RuntimeError("start boom")

            st.start = bad_start
        self.streams.append(st)
        return st


sdf = FailStartSD(16)
opf = S.OutputOpener(sdf, "CABLE In 16", lambda *a: None, log=lambda m: None)
opf.try_open()
check(
    "open: a stream whose start() raises is closed (once) and not kept: no stream, error 'start boom', one OutputStream constructed",
    sdf.streams[0].closed and not sdf.streams[0].started and opf.stream is None and opf.err == "start boom" and len(sdf.streams) == 1,
    str((opf.err, len(sdf.streams))),
)
opf.try_open()
check(
    "open: the next retry opens a second stream that is started and NOT closed",
    len(sdf.streams) == 2 and sdf.streams[1].started and not sdf.streams[1].closed and opf.stream is sdf.streams[1] and opf.err is None,
)
sdg = FailStartSD(16)
sdg.fail_first = False
opg = S.OutputOpener(sdg, "CABLE In 16", lambda *a: None, log=lambda m: None)
opg.try_open()
check("open: positive twin - a stream that starts fine is not closed", sdg.streams[0].started and not sdg.streams[0].closed and opg.stream is sdg.streams[0])


# ---- selftest loopback teardown: every stream stopped and closed on every path (recording stand-ins that raise at each step)
class RecStream:
    def __init__(self, name, events, boom):
        self.name, self.events, self.boom = name, events, boom

    def _do(self, what):
        self.events.append(f"{self.name}.{what}")
        if f"{self.name}.{what}" in self.boom:
            raise RuntimeError(f"{self.name}.{what} boom")

    def start(self):
        self._do("start")

    def stop(self):
        self._do("stop")

    def close(self):
        self._do("close")


class RecSD:
    def __init__(self, boom=(), out_channels=16):
        self.events, self.boom, self.out_channels = [], set(boom), out_channels

    def _terminate(self):
        self.events.append("_terminate")

    def _initialize(self):
        self.events.append("_initialize")

    def query_hostapis(self):
        return [{"name": "Windows WASAPI"}]

    def query_devices(self):
        return [
            {"name": "CABLE Output (VB-Audio Virtual Cable)", "hostapi": 0, "max_input_channels": 16, "max_output_channels": 0},
            {"name": "CABLE In 16ch (VB-Audio Virtual Cable)", "hostapi": 0, "max_input_channels": 0, "max_output_channels": self.out_channels},
        ]

    def WasapiSettings(self, auto_convert):
        return ("wasapi", auto_convert)

    def InputStream(self, **kw):
        self.events.append("InputStream")
        return RecStream("in", self.events, self.boom)

    def OutputStream(self, **kw):
        self.events.append("OutputStream")
        return RecStream("out", self.events, self.boom)


def loop_run(boom=(), out_channels=16, sleeper=None, method="play"):
    sd_ = RecSD(boom, out_channels)
    lp = T.SoundDeviceLoopback("CABLE In 16", "CABLE Output", sd=sd_, sleep=sleeper or (lambda sec: sd_.events.append("sleep")))
    sd_.events.clear()
    err_ = None
    res = None
    try:
        res = lp.play_record(np.zeros((480, 16), np.float32)) if method == "play" else lp.precapture(1.0)
    except Exception as e:  # noqa: BLE001
        err_ = str(e)
    return sd_.events, err_, lp.teardown_errors, res


ev, err_, te, res = loop_run()
check(
    "loopback: normal play_record order - input start, output opened + started, sleep, output stop/close, input stop/close; returns an empty (0, 16) recording from the stub",
    ev == ["InputStream", "in.start", "OutputStream", "out.start", "sleep", "out.stop", "out.close", "in.stop", "in.close"]
    and err_ is None
    and te == []
    and res.shape == (0, 16),
    str(ev),
)
ev, err_, te, _ = loop_run(boom=("in.start",))
check(
    "loopback: input.start raises - the input is stopped and closed, no output stream is ever created, the error propagates",
    ev == ["InputStream", "in.start", "in.stop", "in.close"] and err_ == "in.start boom",
    str((ev, err_)),
)
ev, err_, te, _ = loop_run(out_channels=2)
check(
    "loopback: the output endpoint is not 16 channels (open raises) - the already started input is stopped and closed, the error propagates",
    ev == ["InputStream", "in.start", "in.stop", "in.close"] and err_ is not None and "configured for 2 channel(s)" in err_,
    str((ev, err_)),
)
ev, err_, te, _ = loop_run(boom=("out.start",))
check(
    "loopback: output.start raises - that stream is closed, the input is stopped and closed, the error propagates",
    ev == ["InputStream", "in.start", "OutputStream", "out.start", "out.close", "in.stop", "in.close"] and err_ == "out.start boom",
    str((ev, err_)),
)
ev, err_, te, res = loop_run(boom=("out.stop",))
check(
    "loopback: output.stop raises - output.close, input.stop and input.close are STILL called; the error is kept in teardown_errors, the recording returned",
    ev == ["InputStream", "in.start", "OutputStream", "out.start", "sleep", "out.stop", "out.close", "in.stop", "in.close"]
    and te == ["output.stop: out.stop boom"]
    and err_ is None
    and res is not None,
    str((ev, te)),
)
ev, err_, te, res = loop_run(boom=("in.stop",))
check("loopback: input.stop raises - input.close is still called", ev[-2:] == ["in.stop", "in.close"] and te == ["input.stop: in.stop boom"], str((ev, te)))


def bad_sleep(sec):
    raise RuntimeError("sleep boom")


ev, err_, te, _ = loop_run(sleeper=bad_sleep)
check(
    "loopback: an error during the playing wait still tears down both streams, output first",
    ev[-4:] == ["out.stop", "out.close", "in.stop", "in.close"] and err_ == "sleep boom",
    str((ev, err_)),
)
ev, err_, te, res = loop_run(method="pre")
check(
    "loopback: precapture order - start, sleep, stop, close",
    ev == ["InputStream", "in.start", "sleep", "in.stop", "in.close"] and err_ is None and res.shape == (0, 16),
    str(ev),
)
ev, err_, te, _ = loop_run(boom=("in.start",), method="pre")
check(
    "loopback: precapture with input.start raising still stops and closes the input",
    ev == ["InputStream", "in.start", "in.stop", "in.close"] and err_ == "in.start boom",
    str((ev, err_)),
)

# ---- the decoder CLI on generated WAVs (hostile input)
import io, soundfile as sf_  # noqa: E402


def cli(path):
    o, e = [], []
    rc_ = D.run([path], out=o.append, err=e.append)
    return rc_, o, e


wd = tempfile.mkdtemp()
good = np.zeros((4800, 16))
good[:, 8:11] = -524288 / 8388608.0
rows_g = np.concatenate(
    [
        np.rint(S.Composer(S.DataTrack(), anchor_every=10**9).compose(5000 + 480 * i, np.zeros((480, 8), np.float32)).astype(np.float64) * 8388608.0)
        for i in range(10)
    ]
)
sf_.write(os.path.join(wd, "ok.wav"), rows_g / 8388608.0, 48000, subtype="PCM_24")
rc_, o_, e_ = cli(os.path.join(wd, "ok.wav"))
check(
    "cli: a clean 24-bit 16-channel recording exits 0 with no warning, 4800 samples, 10 anchors (one per composer call)",
    rc_ == 0 and e_ == [] and o_[0].startswith("4800 samples") and "anchors 10;" in o_[1],
    str((rc_, o_, e_)),
)
bad_rows = rows_g.copy()
bad_rows[7, 14] = 300  # a non-zero sample on reserved ch15
sf_.write(os.path.join(wd, "res.wav"), bad_rows / 8388608.0, 48000, subtype="PCM_24")
rc_, o_, e_ = cli(os.path.join(wd, "res.wav"))
check(
    "cli: a non-zero reserved channel exits 3 and prints the WARNING (still printed)",
    rc_ == 3 and len(e_) == 1 and "non-zero reserved samples 1" in e_[0],
    str((rc_, e_)),
)
bad_rows = rows_g.copy()
bad_rows[10, 8] += 40
sf_.write(os.path.join(wd, "imp.wav"), bad_rows / 8388608.0, 48000, subtype="PCM_24")
rc_, o_, e_ = cli(os.path.join(wd, "imp.wav"))
check(
    "cli: one impossible value (+40 counts) exits 3 with 'impossible values 1 (first at row 10'",
    rc_ == 3 and "impossible values 1 (first at row 10" in e_[0],
    str((rc_, e_)),
)
bad_rows = rows_g.copy()
bad_rows[3, 12] += 256  # corrupts a data byte -> CRC failure
sf_.write(os.path.join(wd, "crc.wav"), bad_rows / 8388608.0, 48000, subtype="PCM_24")
rc_, o_, e_ = cli(os.path.join(wd, "crc.wav"))
check(
    "cli: a corrupt data frame alone exits 3 (corrupt frames >= 1 in the output)",
    rc_ == 3 and "corrupt frames 1" in o_[1] or (rc_ == 3 and "corrupt frames" in o_[1]),
    str((rc_, o_, e_)),
)
sf_.write(os.path.join(wd, "w8.wav"), rows_g / 8388608.0, 48000, subtype="PCM_U8")
rc_, o_, e_ = cli(os.path.join(wd, "w8.wav"))
check(
    "cli: an 8-bit WAV is rejected with exit 5 and a message naming PCM_U8 (it used to decode silently with counts in multiples of 65536)",
    rc_ == 5 and "PCM_U8" in e_[0] and o_ == [],
    str((rc_, e_)),
)
sf_.write(os.path.join(wd, "w16.wav"), rows_g / 8388608.0, 48000, subtype="PCM_16")
rc_, o_, e_ = cli(os.path.join(wd, "w16.wav"))
check("cli: positive twin - a 16-bit WAV of the same rows decodes exactly (exit 0, no warning)", rc_ == 0 and e_ == [], str((rc_, e_)))
open(os.path.join(wd, "junk.wav"), "wb").write(b"this is not a wav file at all " * 8)
rc_, o_, e_ = cli(os.path.join(wd, "junk.wav"))
check("cli: a junk file exits 4 with 'cannot read as audio' instead of a traceback", rc_ == 4 and "cannot read as audio" in e_[0] and o_ == [], str((rc_, e_)))
rc_, o_, e_ = cli(os.path.join(wd, "missing.wav"))
check("cli: a missing file exits 4 with 'cannot read as audio'", rc_ == 4 and "cannot read as audio" in e_[0], str((rc_, e_)))
raw_ok = open(os.path.join(wd, "ok.wav"), "rb").read()
open(os.path.join(wd, "trunc.wav"), "wb").write(raw_ok[: len(raw_ok) - 6000])
rc_, o_, e_ = cli(os.path.join(wd, "trunc.wav"))
check(
    "cli: a WAV whose header claims more audio than the file holds warns with both byte counts and still decodes what is there",
    any("header claims 230400 bytes of audio but the file holds 224400" in m for m in e_) and o_[0].startswith("4675 samples"),
    str((rc_, e_, o_[:1])),
)
check(
    "cli: positive twin - the untruncated file has no truncation warning",
    D.header_shortfall(os.path.join(wd, "ok.wav")) is None and D.header_shortfall(os.path.join(wd, "trunc.wav")) == (230400, 224400),
)
sf_.write(os.path.join(wd, "mono.wav"), np.zeros(4800), 48000, subtype="PCM_24")
rc_, o_, e_ = cli(os.path.join(wd, "mono.wav"))
check("cli: mono keeps its exit code 1 with the channel message", rc_ == 1 and "1 channel(s)" in e_[0], str((rc_, e_)))
sf_.write(os.path.join(wd, "r44.wav"), np.zeros((4410, 16)), 44100, subtype="PCM_24")
rc_, o_, e_ = cli(os.path.join(wd, "r44.wav"))
check("cli: 44.1 kHz keeps its exit code 1 with the rate message", rc_ == 1 and "44100 Hz, expected 48000" in e_[0], str((rc_, e_)))
sf_.write(os.path.join(wd, "c14.wav"), np.zeros((4800, 14)), 48000, subtype="PCM_24")
rc_, o_, e_ = cli(os.path.join(wd, "c14.wav"))
check(
    "cli: 14 channels warn that the reserved channels are not checked (not a failure by itself: silent rows, exit 0)",
    rc_ == 0 and any("reserved channels not checked" in m for m in e_),
    str((rc_, e_)),
)

# ---- rx_loop: the receive thread must survive a failing packet (observed live: MemoryError in BandMeter killed it silently)
import contextlib, socket as socket_, subprocess


class Stop(BaseException):
    """Ends a fake socket's script (not an Exception, so rx_loop does not swallow it)."""


class FakeSock:
    """recvfrom yields the scripted items in order (bytes, or an exception instance to raise); then raises Stop."""

    def __init__(self, items):
        self.items = list(items)

    def recvfrom(self, n):
        if not self.items:
            raise Stop()
        it = self.items.pop(0)
        if isinstance(it, BaseException):
            raise it
        return it, ("127.0.0.1", 1)


escaped = [None]  # the BaseException type that left rx_loop in the last run_rx, or None


def run_rx(items, fail_on=(), exc=MemoryError, max_err=200, handle=None):
    """Run the real rx_loop over `items`; the handler raises exc(...) for the packets in `fail_on`. Returns the observations."""
    seen, sleeps, logs, deaths = [], [], [], []
    stats = {"datagrams": 0, "bad": 0, "rx_errors": 0}

    def h(pkt):
        seen.append(pkt)
        if pkt in fail_on:
            raise exc("Unable to allocate 2.93 MiB for an array with shape (48000, 8)")

    stopped = False
    escaped[0] = None
    try:
        S.rx_loop(FakeSock(items), handle or h, stats, logs.append, max_consecutive_errors=max_err, sleep=sleeps.append, die=deaths.append)
    except BaseException as e:  # Stop ends the script; anything else escaped rx_loop (the tests assert on it)
        stopped = isinstance(e, Stop)
        escaped[0] = type(e)
    return seen, sleeps, logs, deaths, stats, stopped


good_a, good_b = [b"a%d" % i for i in range(4)], [b"b%d" % i for i in range(3)]
bad_k = [b"x%d" % i for i in range(5)]
seen, sleeps, logs, deaths, st_, stopped = run_rx(good_a + bad_k + good_b, fail_on=set(bad_k))
check(
    "rx_loop: good packets before and after 5 MemoryError packets are all handled, in order, and the loop reached the end of the socket",
    seen == good_a + bad_k + good_b and stopped,
    str(seen),
)
check("rx_loop: exactly 5 errors counted in stats['rx_errors'], die never called", st_["rx_errors"] == 5 and deaths == [], str((st_, deaths)))
check("rx_loop: it paused once per error with the 0.05 s backoff (no busy loop), and not after a good packet", sleeps == [0.05] * 5, str(sleeps))
_clean = run_rx(good_a + good_b)
check(
    "rx_loop: positive twin - with no failing packet nothing is counted, logged, slept or killed",
    _clean[1:5] == ([], [], [], {"datagrams": 0, "bad": 0, "rx_errors": 0}) and _clean[0] == good_a + good_b,
    str(_clean[1:5]),
)
seen, sleeps, logs, deaths, st_, stopped = run_rx(good_a, exc=OSError, fail_on=set(good_a[:2]))
check(
    "rx_loop: an OSError from the handler is survived the same way (2 counted, 2 sleeps, all 4 attempted)",
    st_["rx_errors"] == 2 and sleeps == [0.05] * 2 and len(seen) == 4 and stopped,
)
seen, sleeps, logs, deaths, st_, stopped = run_rx([OSError("recv boom"), b"g1", OSError("recv boom"), b"g2"], fail_on=())
check(
    "rx_loop: a socket-level OSError from recvfrom counts as an error, sleeps, and the good packets around it are handled",
    seen == [b"g1", b"g2"] and st_["rx_errors"] == 2 and sleeps == [0.05] * 2 and deaths == [] and stopped,
    str((seen, st_, sleeps)),
)

# persistent failure: exits non-zero after exactly max consecutive errors
seen, sleeps, logs, deaths, st_, stopped = run_rx([b"p"] * 50, fail_on={b"p"}, max_err=7)
check(
    "rx_loop: persistent failure calls die(3) exactly once, on the 7th consecutive error, and stops reading (7 packets taken, the rest never read)",
    deaths == [3] and st_["rx_errors"] == 7 and len(seen) == 7 and not stopped,
    str((deaths, st_, len(seen))),
)
check("rx_loop: the dying iteration logs the exit line", logs[-1] == "rx: 7 consecutive errors, exiting 3 for a supervisor restart", logs[-1])
seen, sleeps, logs, deaths, st_, stopped = run_rx([b"p"] * 6 + [b"ok"], fail_on={b"p"}, max_err=7)
check("rx_loop: positive twin - 6 consecutive errors (one below the limit) then a good packet does not die", deaths == [] and st_["rx_errors"] == 6 and stopped)
seen, sleeps, logs, deaths, st_, stopped = run_rx([b"p"] * 6 + [b"ok"] + [b"p"] * 6 + [b"ok"], fail_on={b"p"}, max_err=7)
check(
    "rx_loop: a success resets the consecutive count - 6+6 errors split by a good packet never die, 12 are counted",
    deaths == [] and st_["rx_errors"] == 12 and stopped,
)
_seen, _sl, _lg, deaths, st_, _ = run_rx([b"p"] * 400, fail_on={b"p"})
check(
    "rx_loop: the default limit is 200 consecutive errors", deaths == [3] and st_["rx_errors"] == 200 and S.RX_MAX_CONSECUTIVE_ERRORS == 200, str((deaths, st_))
)

# KeyboardInterrupt / SystemExit are not swallowed
for ex_ in (KeyboardInterrupt, SystemExit):
    seen, sleeps, logs, deaths, st_, stopped = run_rx([b"a", b"x", b"b"], fail_on={b"x"}, exc=ex_)
    check(
        f"rx_loop: {ex_.__name__} raised by the handler propagates out (Stop not reached), nothing counted, no sleep, b never read",
        not stopped and escaped[0] is ex_ and seen == [b"a", b"x"] and st_["rx_errors"] == 0 and sleeps == [],
        str((seen, st_, stopped, escaped)),
    )
seen, sleeps, logs, deaths, st_, stopped = run_rx([b"a", b"x", b"b"], fail_on={b"x"}, exc=RuntimeError)
check(
    "rx_loop: positive twin - an ordinary Exception subclass (RuntimeError) is swallowed and counted",
    stopped and st_["rx_errors"] == 1 and seen == [b"a", b"x", b"b"],
)

# the stderr line: first 5, then every 1000th, no packet bytes
PKT = b"SECRET-PACKET-BYTES-\x01\x02\xfe"
seen, sleeps, logs, deaths, st_, stopped = run_rx([PKT] * 2100, fail_on={PKT}, max_err=10**9)
check(
    "rx_loop: 2100 consecutive errors log exactly 7 lines: errors 1-5, 1000 and 2000",
    [l_.split(":")[0] for l_ in logs] == ["rx error #%d (%d in a row)" % (i, i) for i in (1, 2, 3, 4, 5, 1000, 2000)],
    str([l_[:30] for l_ in logs]),
)
check(
    "rx_loop: a log line is one short line naming the exception and its message, with no packet bytes",
    logs[0] == "rx error #1 (1 in a row): MemoryError: Unable to allocate 2.93 MiB for an array with shape (48000, 8)"
    and all("SECRET" not in l_ and "\n" not in l_ and len(l_) < 200 for l_ in logs),
    logs[0],
)
seen, sleeps, logs, deaths, st_, stopped = run_rx([PKT] * 5, handle=lambda p: (_ for _ in ()).throw(ValueError("x" * 5000)))
check(
    "rx_loop: a huge exception message is truncated to 160 chars in the log",
    all(len(l_) < 230 for l_ in logs) and logs[0].endswith(": ValueError: " + "x" * 160),
    str(len(logs[0])),
)
seen, sleeps, logs, deaths, st_, stopped = run_rx([b"p"] * 5 + [b"ok"] + [b"p"] * 2, fail_on={b"p"})
check("rx_loop: the cumulative counter keeps numbering across a success: 7 errors, 5 logged lines (#1-#5)", st_["rx_errors"] == 7 and len(logs) == 5)


# happy-path ordering through the real handler: parse -> stats -> feed_bands -> compose -> ring.put (only when the output is open)
def handler_run(stream_open):
    order = []
    stats_ = {"datagrams": 0, "bad": 0, "rx_errors": 0}
    fr_ = np.zeros((256, 8), np.float32)

    class Asm:
        def push(self, slot, block, d):
            order.append("asm")
            return [(100, fr_)]

    class Meter:
        def push(self, start, beams):
            order.append("meter")
            return [{"k": "x"}]

    class Data:
        def push(self, kind, line):
            order.append("data")

    class Comp:
        def compose(self, start, fr):
            order.append("compose")
            return "frame16"

    class Ring_:
        def put(self, fr):
            order.append("ring")

    class Out:
        stream = object() if stream_open else None

    h = S.make_handler(Asm(), Meter(), Comp(), Data(), Ring_(), Out(), None, stats_)
    h(bmaa(3, 0, 0, 512, samples(3, 512)))
    h(b"garbage")
    return order, stats_


order_, st_ = handler_run(True)
check(
    "handler: happy-path order is asm, meter (feed_bands -> data track), compose, ring.put; one datagram, one bad",
    order_ == ["asm", "meter", "data", "compose", "ring"] and st_ == {"datagrams": 1, "bad": 1, "rx_errors": 0},
    str((order_, st_)),
)
order_, st_ = handler_run(False)
check(
    "handler: with the audio output closed ring.put is skipped but compose still runs (the data track drains)",
    order_ == ["asm", "meter", "data", "compose"],
    str(order_),
)


# the real handler and a real BandMeter that fails like the live incident: MemoryError inside BandMeter._record
class FlakyMeter(S.BandMeter):
    fail_next = 0

    def _record(self, *a, **k):
        if FlakyMeter.fail_next > 0:
            FlakyMeter.fail_next -= 1
            raise MemoryError("Unable to allocate 2.93 MiB for an array with shape (48000, 8)")
        return super()._record(*a, **k)


def real_pipeline(fail_windows):
    """Real Assembler/Composer/BandMeter/handler/rx_loop over a real UDP loopback socket, 12 windows of beam audio."""
    FlakyMeter.fail_next = fail_windows
    data_ = S.DataTrack()
    stats_ = {"datagrams": 0, "bad": 0, "rx_errors": 0}
    out_ = type("O", (), {"stream": None})()
    h = S.make_handler(S.Assembler(), FlakyMeter(window=4800), S.Composer(data_), data_, S.Ring(10, 400), out_, None, stats_)
    rx_sk = socket_.socket(socket_.AF_INET, socket_.SOCK_DGRAM)
    rx_sk.setsockopt(socket_.SOL_SOCKET, socket_.SO_RCVBUF, 4 << 20)
    rx_sk.bind(("127.0.0.1", 0))
    rx_sk.settimeout(2.0)
    tx = socket_.socket(socket_.AF_INET, socket_.SOCK_DGRAM)
    n_blocks = 12 * 4800 // 256
    total = n_blocks * 8
    logs, sleeps, deaths = [], [], []

    class CountingSock:
        n = 0

        def recvfrom(self, k):
            if CountingSock.n >= total:
                raise Stop()
            if CountingSock.n % 8 == 0:  # one block of 8 slots in flight at a time: never more than 8 datagrams queued
                blk = CountingSock.n // 8
                for slot in range(8):
                    tx.sendto(bmaa(slot, 0, 0, blk * 256, samples(slot, blk * 256)), rx_sk.getsockname())
            CountingSock.n += 1
            return rx_sk.recvfrom(k)

    try:
        S.rx_loop(CountingSock(), h, stats_, logs.append, sleep=sleeps.append, die=deaths.append)
    except Stop:
        pass
    rx_sk.close()
    tx.close()
    return stats_, logs, sleeps, data_, total, deaths


st_, logs, sleeps, dt_, total_, deaths_ = real_pipeline(0)
check(
    "e2e: real UDP socket, real Assembler/BandMeter/Composer, no failure: every datagram processed, 0 rx_errors, no sleep, data track filled",
    st_["datagrams"] == total_ and st_["rx_errors"] == 0 and sleeps == [] and deaths_ == [] and dt_.frames > 0,
    str((st_, total_, dt_.frames, deaths_)),
)
frames_ok = dt_.frames
st_, logs, sleeps, dt_, total_, deaths_ = real_pipeline(3)
check(
    "e2e: three MemoryErrors raised by the real BandMeter._record are survived: exactly 3 rx_errors, 3 backoffs, 3 log lines, every datagram still taken, data frames still produced",
    st_["rx_errors"] == 3 and sleeps == [0.05] * 3 and len(logs) == 3 and 0 < dt_.frames and st_["datagrams"] == total_ - 0 and deaths_ == [],
    str((st_, sleeps, len(logs), dt_.frames, frames_ok, deaths_)),
)

# the real process: the sink script over loopback UDP writes rx_errors (0) into its status file
sd_ = tempfile.mkdtemp()
free = socket_.socket(socket_.AF_INET, socket_.SOCK_DGRAM)
free.bind(("127.0.0.1", 0))
port_ = free.getsockname()[1]
free.close()
stf = os.path.join(sd_, "status.json")
proc = subprocess.Popen(
    [
        sys.executable,
        "-I",
        os.path.join(HERE, "bma_cable_sink.py"),
        "--listen",
        f"127.0.0.1:{port_}",
        "--board-debug",
        "",
        "--bands-log",
        "",
        "--history-dir",
        "",
        "--bridge-status",
        os.path.join(sd_, "none.json"),
        "--status-file",
        stf,
        "--stats-s",
        "0.3",
    ],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
got_ = None
try:
    tx = socket_.socket(socket_.AF_INET, socket_.SOCK_DGRAM)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 20:
        for slot in range(8):
            tx.sendto(bmaa(slot, 0, 0, 0, samples(slot, 0)), ("127.0.0.1", port_))
        time.sleep(0.2)
        try:
            with open(stf) as f_:
                got_ = json.load(f_)
            if got_["datagrams"] >= 8:
                break
        except (OSError, ValueError):
            pass
finally:
    proc.kill()
    _o, _e = proc.communicate(timeout=10)
check(
    "e2e process: the real sink process receives the datagrams over UDP and its status file has rx_errors 0 beside datagrams/bad",
    got_ is not None and got_["datagrams"] >= 8 and got_["rx_errors"] == 0 and got_["bad"] == 0,
    str(got_) + _e.decode(errors="replace")[-300:],
)

print(f"\n{len(fails)} failure(s)")
sys.exit(1 if fails else 0)
