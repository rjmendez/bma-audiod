#!/usr/bin/env python3
"""Hermetic tests for bma_spectrum: band values, quantization, contract round trips, pacing, loss and wrap, phase blindness, no gating or
coarsening by the producer, loopback-only CLI, end-to-end over UDP. Audio is synthetic and stays in memory.

    python -I test_spectrum.py          (numpy; exit 0 and a final 'N checks, 0 failure(s)' line)"""

import math, os, socket, struct, subprocess, sys, threading, time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.environ.get("BMA_PC_DIR", HERE))
sys.path.insert(1, os.path.join(HERE, "..", "contracts"))
import bma_spectrum as S  # noqa: E402
import ref_binary as R  # noqa: E402

fails, n_checks = [], 0


def check(name, ok, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def eq(name, got, want):
    check(name, got == want, f"got {got!r}, want {want!r}")


T0 = 1_000_000_000.0


def noise_band(n, lo, hi, seed, rms=0.1):
    r = np.random.default_rng(seed).standard_normal(n)
    x = np.fft.rfft(r)
    f = np.fft.rfftfreq(n, 1 / S.RATE)
    x[(f < lo) | (f >= hi)] = 0
    y = np.fft.irfft(x, n)
    return y / math.sqrt(float(np.mean(y**2))) * rms


def to_int(x):
    return np.clip(np.rint(x * 8388608.0), -8388608, 8388607).astype(np.int32)


def run(sig, detail=False, start=5000, skip=None, dup=None, late=None, engine=None):
    """sig: (8, n) float or (n,) shared by all 8 slots. Feeds 256-sample BMAA blocks. -> (engine, [datagrams]).
    skip = (slot, first_block, count): those blocks never arrive on that slot; dup = (slot, block): that block arrives twice in a row;
    late = (slot, block): that block is delivered again two blocks later (a re-ordered duplicate)."""
    sig = np.asarray(sig)
    if sig.ndim == 1:
        sig = np.tile(sig, (8, 1))
    e = engine or S.Engine(detail=detail)
    out = []
    n = sig.shape[1]
    for k, b0 in enumerate(range(0, n - 255, 256)):
        for s in range(8):
            if skip and s == skip[0] and skip[1] <= k < skip[1] + skip[2]:
                continue
            x = to_int(sig[s][b0 : b0 + 256])
            out += e.feed(s, (start + b0) & 0xFFFFFFFF, 0, x, now=T0 + b0 / S.RATE)
            if dup and s == dup[0] and k == dup[1]:
                out += e.feed(s, (start + b0) & 0xFFFFFFFF, 0, x, now=T0 + b0 / S.RATE)
            if late and s == late[0] and k == late[1] + 2:
                b_old = (k - 2) * 256
                out += e.feed(s, (start + b_old) & 0xFFFFFFFF, 0, to_int(sig[s][b_old : b_old + 256]), now=T0 + b0 / S.RATE)
    return e, out


def rows_of(dgrams, tier=0, errors=None):
    """{(beam, row): parsed dict} using the CONTRACT reference parser (it also enforces every rule of the packet spec).
    With `errors` a list, a datagram the parser rejects is recorded there (and skipped) instead of raising."""
    out = {}
    for d in dgrams:
        try:
            p = R.parse_spectrum(d)
        except R.ContractError as ex:
            if errors is None:
                raise
            errors.append(str(ex))
            continue
        if p["tier"] == tier:
            out[(p["beam"], p["row"])] = p
    return out


def tone(f, a, n, phase=0.0):
    return a * np.sin(2 * np.pi * f * np.arange(n) / S.RATE + phase)


# ------------------------------------------------------------------------------------------------ band values
n6 = 6 * S.RATE
e, out = run(tone(1100, 0.5, n6))
r = rows_of(out)
want = [0] * 46
want[7] = 182
eq("tone 1100 Hz amplitude 0.5 (mean-square -9.03 dB): band 7 (1000-1260 Hz) = 182 (-9.0 dB), every other band 0 (floor)", r[(0, 10)]["bins"], want)
eq("the same tone on a single beam row (beam 5) gives the same bands", r.get((5, 10), {}).get("bins"), want)
eq("every beam 0-8 has its own row 10 (the header carries the beam)", sorted(b for (b, row) in r if row == 10), list(range(9)))
e, out = run(tone(1100, 1.0, n6))
eq("full-scale sine (-3.01 dB): band 7 = 194 (-3.0 dB)", rows_of(out)[(0, 10)]["bins"][7], 194)
e, out = run(tone(3000, 0.5, n6))
want = [0] * 46
want[16], want[17] = 166, 180
eq(
    "tone 3000 Hz (bin 300): Hann leakage 1/6 of the power into the band below (166 = -16.8 dB), 5/6 in its own band (180 = -9.8 dB), nothing else",
    rows_of(out)[(0, 10)]["bins"],
    want,
)
e, out = run(tone(3000, 0.5, n6), detail=True)
want = [0] * 100
want[41], want[42] = 166, 180
eq("detail tier, tone 3000 Hz (20 Hz bins, 1/24 octave bands 41 and 42): same 166 / 180", rows_of(out, 1)[(0, 20)]["bins"], want)
sq = np.where(np.sin(2 * np.pi * 500 * np.arange(n6) / S.RATE) >= 0, 8388607.0 / 8388608.0, -1.0)
e, out = run(sq)
b = rows_of(out)[(0, 10)]["bins"]
eq("full-scale clipped square wave 500 Hz: the fundamental (power 8/pi^2 * 5/6 = 0.6755, -1.70 dB) = 197 in band 4", b[4], 197)
check("clipped square wave: no band wraps or exceeds the strongest value (nothing above 197)", max(b) == 197 and max(b[:4] + b[5:]) < 197, str(b))
e, out = run(np.zeros(n6))
r = rows_of(out)
check(
    "digital silence: every beam row (0-8) has flag 'zero' (4) and all-zero bands",
    all(p["flags"] == 4 and p["bins"] == [0] * 46 for p in r.values()) and len(r) == 9 * 59,
    f"{len(r)} rows",
)
sig = np.tile(tone(1100, 0.5, n6), (8, 1))
sig[2] = 0.0
e, out = run(sig)
r = rows_of(out)
eq("muted beam 3 (slot 2): beam-3 row has flag 'zero', bands 0", (r[(3, 10)]["flags"], r[(3, 10)]["bins"]), (4, [0] * 46))
eq("muted beam 3: the other beams keep their 182", (r[(4, 10)]["flags"], r[(4, 10)]["bins"][7]), (0, 182))
eq("muted beam 3: the mean of 8 beams' POWER = 7/8 * 0.125 = -9.61 dB -> 181 (a mean of dB values would be wrong)", r[(0, 10)]["bins"][7], 181)

# ------------------------------------------------------------------------------------------------ quantization
for db, want in ((-100.0, 0), (-99.75, 0), (-99.25, 2), (-99.5, 1), (-3.0103, 194), (27.5, 255), (40.0, 255), (-200.0, 0)):
    eq(f"quantize {db} dB -> {want} (round half even, clamped)", int(S.quantize(db)), want)
grid = [-100.0 + 0.0625 * i for i in range(0, 2100)]
check("quantize agrees with the contract reference on a 0.0625 dB grid (incl. every half-step tie)", all(int(S.quantize(g)) == R.quantize_db(g) for g in grid))
check("quantize then dequantize is within 0.25 dB for -100..27.5 dB", all(abs(R.dequantize(int(S.quantize(g))) - g) <= 0.25 + 1e-9 for g in grid if g <= 27.5))
check("band edges equal the contract reference for both tiers", all(S.band_edges(t) == R.spectrum_band_edges(t) for t in (0, 1)))

# ------------------------------------------------------------------------------------------------ packets, pacing, counts
e, out = run(tone(1100, 0.5, n6))
eq(
    "6 s of audio: 59 rows x 9 streams are due at once (a row leaves as soon as its 4800-sample window is complete; the last window needs 1024 samples of slack: (288000 - 1024) // 4800 = 59)",
    len(out),
    531,
)
eq("every datagram is exactly 24 + 46 = 70 bytes", {len(d) for d in out}, {70})
r = rows_of(out)
eq("row numbers 0..58 for each of the 9 streams, none missing", sorted(k[1] for k in r if k[0] == 0), list(range(59)))
eq("the 9 streams of one row are emitted back to back as beams 0..8", [d[7] for d in out[:9]], list(range(9)))
dt = [(r[(0, k + 1)]["t_us"] - r[(0, k)]["t_us"]) / 1e6 for k in range(58)]
check(
    "row timestamps advance by about 100 ms (one 256-sample block of slack: 0.09..0.11 s)",
    all(0.09 <= x <= 0.11 for x in dt),
    str(min(dt)) + " " + str(max(dt)),
)
check("t_us is the host clock in microseconds since the epoch", abs(r[(0, 0)]["t_us"] / 1e6 - (T0 + 4800 / S.RATE)) < 0.02, str(r[(0, 0)]["t_us"]))
rate_per_beam = sum(1 for k in r if k[0] == 0) / 6.0
check(
    "information rate: at most 10 rows/s per stream and 9 x 10 x 70 = 6300 B/s in total",
    rate_per_beam <= 10.0 and len(out) * 70 / 6.0 <= 6300.0,
    f"{rate_per_beam}",
)
eq(
    "standard rows and valid examples: the production builder is byte-identical to the contract's build_spectrum",
    S.build_row(0, 4, 3, 1235, 1792951400200000, [0] * 46),
    R.build_spectrum(0, 4, 3, 1235, 1792951400200000, [0] * 46),
)
import json

ex = json.load(open(os.path.join(HERE, "..", "contracts", "fixtures", "spectrum_examples.json")))
check(
    "every VALID contract example is reproduced byte for byte by S.build_row and parses back",
    all(S.build_row(v["tier"], v["flags"], v["beam"], v["row"], v["t_us"], v["bins"]) == bytes.fromhex(v["hex"]) for v in ex["valid"]),
)

# ------------------------------------------------------------------------------------------------ detail tier counts
e, out = run(tone(1100, 0.5, n6), detail=True)
d1 = [d for d in out if d[5] == 1]
eq("detail tier: every datagram is 24 + 100 = 124 bytes", {len(d) for d in d1}, {124})
r1 = rows_of(out, 1)
check("detail tier: 50 rows/s hop (20 ms): rows 0..N contiguous, per stream", sorted(k[1] for k in r1 if k[0] == 0) == list(range(max(k[1] for k in r1) + 1)))
dt = [(r1[(0, k + 1)]["t_us"] - r1[(0, k)]["t_us"]) / 1e6 for k in range(20, 120)]
check("detail tier: hop 20 ms (0.01..0.03 s between rows)", all(0.0 <= x <= 0.03 for x in dt) and abs(sum(dt) / len(dt) - 0.02) < 0.002, str(sum(dt) / len(dt)))
e, out = run(tone(1100, 0.5, n6))
check("without detail=True no detail datagram exists (positive twin of the above: it exists with detail=True)", all(d[5] == 0 for d in out) and len(d1) > 0)

# ------------------------------------------------------------------------------------------------ privacy: magnitude only, no phase
base = rows_of(run(tone(1100, 0.5, n6))[1])
shift = rows_of(run(tone(1100, 0.5, n6, phase=1.234))[1])
inv = rows_of(run(-tone(1100, 0.5, n6))[1])
check("a tone shifted by 1.234 rad produces byte-identical rows (phase is not carried)", all(base[k]["bins"] == shift[k]["bins"] for k in base))
check("a sign-inverted signal produces byte-identical rows", all(base[k]["bins"] == inv[k]["bins"] for k in base))
rng = np.random.default_rng(3)
nz = rng.standard_normal(n6) * 0.05
nz_rows = rows_of(run(nz)[1])
check("a row carries 46 one-byte values per 4800 samples (0.96 %), never the samples", all(len(p["bins"]) == 46 and len(p) == 11 for p in nz_rows.values()))
a = rows_of(run(rng.standard_normal(n6) * 0.05)[1])
bb = rows_of(run(rng.standard_normal(n6) * 0.05)[1])
check(
    "positive twin: two different noise signals of the same power DO give different rows (the rows are not a constant)",
    any(a[k]["bins"] != bb[k]["bins"] for k in a),
)

# ------------------------------------------------------------------------------------------------ loss, duplicates, wrap
_, out_ref = run(tone(1100, 0.5, 8 * S.RATE))
ref = rows_of(out_ref)
_, out_gap = run(tone(1100, 0.5, 8 * S.RATE), skip=(2, 100, 1))  # slot 2 loses block 100 (samples 25600..25855 of the run)
g = rows_of(out_gap)
gap_rows = sorted(k[1] for k, p in g.items() if k[0] == 3 and p["gap"])
eq("loss: beam 3 rows whose window holds the missing block get flag 'gap' (2) and zero bands: rows 5 only (window 24000..28800)", gap_rows, [5])
eq("loss: the gap row has all-zero bands", g[(3, 5)]["bins"], [0] * 46)
eq(
    "loss: beam 4 (no loss) is identical to the lossless run at row 5, and the mean over the 7 valid beams (a gap beam is left out, not counted as silent) stays 182",
    (g[(4, 5)]["bins"], g[(0, 5)]["bins"][7]),
    (ref[(4, 5)]["bins"], 182),
)
check(
    "loss: rows before and after the gap on beam 3 equal the lossless run",
    all(g[(3, k)]["bins"] == ref[(3, k)]["bins"] for k in range(0, 5)) and all(g[(3, k)]["bins"] == ref[(3, k)]["bins"] for k in range(6, 20)),
)
e_dup, out_dup = run(tone(1100, 0.5, 8 * S.RATE), dup=(1, 50))
d = rows_of(out_dup)
check(
    "a duplicated block is dropped and counted once: rows equal the clean run, dups == 1",
    all(d[k]["bins"] == ref[k]["bins"] for k in ref) and e_dup.dups == 1,
    str(e_dup.dups),
)
_, out_w = run(tone(1100, 0.5, 8 * S.RATE), start=(1 << 32) - 3000)
w = rows_of(out_w)
check(
    "block index wrap at 2^32 in mid-stream: rows are identical to the unwrapped run (the index is unwrapped to 64 bit)",
    all(k in w and w[k]["bins"] == ref[k]["bins"] for k in ref) and len(w) == len(ref),
    f"{len(w)} rows, want {len(ref)}",
)
e_late, out_late = run(tone(1100, 0.5, 8 * S.RATE), start=(1 << 32) - 3000, late=(4, 11))
wl = rows_of(out_late)
check(
    "a re-ordered duplicate delivered two blocks late across the 2^32 wrap (a backward step of 512 samples) is dropped: rows equal the clean run, no reset",
    all(k in wl and wl[k]["bins"] == ref[k]["bins"] for k in ref) and len(wl) == len(ref) and e_late.dups == 1 and e_late.resets == 0,
    f"dups {e_late.dups} resets {e_late.resets} rows {len(wl)} want {len(ref)}",
)
eng = S.Engine()
bad = [S.parse_bmaa(x) for x in (b"", b"BMAA" + bytes(10), b"XXXX" + bytes(60))]
check("parse_bmaa rejects short, truncated and wrong-magic datagrams", bad == [None, None, None])
pk = b"BMAA" + bytes([1, 0, 3, 0]) + struct.pack("<III", 7, 9999, 256) + np.arange(256, dtype="<i4").tobytes()
q = S.parse_bmaa(pk)
check("positive twin: a well-formed BMAA datagram parses (slot 3, block 9999, 256 samples)", q is not None and q[0] == 3 and q[2] == 9999 and len(q[3]) == 256)
check(
    "a BMAA datagram with a non-zero reserved byte is malformed (positive twin: the same bytes with 0 parse)",
    S.parse_bmaa(pk[:5] + b"\x01" + pk[6:]) is None and S.parse_bmaa(pk[:5] + b"\x00" + pk[6:]) is not None,
)
check("a BMAA datagram one sample too long or one sample short for its n is malformed", S.parse_bmaa(pk + bytes(4)) is None and S.parse_bmaa(pk[:-4]) is None)
pk_bad = b"BMAA" + bytes([1, 0, 3, 0]) + struct.pack("<III", 7, 9999, 256) + (np.arange(256, dtype="<i4") + 9000000).tobytes()
check("a sample outside the 24-bit range makes the datagram malformed", S.parse_bmaa(pk_bad) is None)

# ------------------------------------------------------------------------------------------------ no speech detection in the producer
n10 = 10 * S.RATE
t = np.arange(n10) / S.RATE
base_noise = noise_band(n10, 300, 3400, 11, rms=0.1)
speech_like = base_noise * np.where(
    (t >= 3) & (t < 9), 0.55 + 0.45 * np.sin(2 * np.pi * 4 * t), 0.55
)  # 4 Hz amplitude modulation 3..9 s: what the removed detector fired on
errs = []
_, out_sp = run(speech_like)
r_sp = rows_of(out_sp, errors=errs)
groups = R.spectrum_coarse_groups(0)


def fine(p):
    """True when a row has structure INSIDE at least one coarse group (a coarsened row is constant inside every group)."""
    b = p["bins"]
    return any(len({b[i] for i in range(len(b)) if groups[i] == g}) > 1 for g in set(groups))


check(
    "a 4 Hz amplitude-modulated 300-3400 Hz signal: no datagram is rejected by the contract parser and none is flagged gated, in any of the 9 streams",
    errs == [] and len(r_sp) > 0 and not any(p["gated"] for p in r_sp.values()),
    str((errs[:2], sum(1 for p in r_sp.values() if p["gated"]))),
)
mid = [p for (beam, k), p in r_sp.items() if beam == 0 and 40 <= k <= 80]
check(
    "... and the rows inside that signal (rows 40..80 of the mean beam) all keep fine detail: bins differ inside a coarse group",
    len(mid) == 41 and all(fine(p) for p in mid),
    str(len(mid)),
)
_, out_st = run(base_noise)
r_st = rows_of(out_st)
check(
    "positive twin: steady noise gives the same: fine rows, none gated",
    len(r_st) > 0 and not any(p["gated"] for p in r_st.values()) and all(fine(p) for (beam, k), p in r_st.items() if beam == 0 and k >= 5),
)
coarse_row = R.build_spectrum(0, 1, 0, 0, 0, R.coarsen_block(0, [list(r_st[(0, k)]["bins"]) for k in range(20, 30)]))
check(
    "positive twin of the 'none gated' checks: a gated row (flag bit 0, coarse values) built by the contract reference IS parsed as gated and has no fine structure, so those checks can fail",
    R.parse_spectrum(coarse_row)["gated"] is True and not fine(R.parse_spectrum(coarse_row)),
)

# ------------------------------------------------------------------------------------------------ producer -> dashboard feed (two independent parsers)
sys.path.insert(2, os.path.join(HERE, "..", "ui"))
import bma_ui as U  # noqa: E402

_, out_sp = run(speech_like)
tclk = [0.0]
feed = U.SpectrumFeed(enabled=True, clock=lambda: tclk[0])
acc = 0
for d in out_sp:
    tclk[0] = struct.unpack_from("<Q", d, 12)[0] / 1e6  # the dashboard's clock follows the producer's row times
    acc += bool(feed.handle(d))
eq(
    "the dashboard's receiver accepts every datagram of the producer's 10 s run of the former 'speech' signal (none bad, none rate-capped)",
    (acc, feed.bad_packets, feed.rows_dropped_rate),
    (len(out_sp), 0, 0),
)
snap = feed.snapshot("standard", 0, 0, 0, False, 5, 600)
eq(
    "... and serves them with no gated row (the producer never gates; the dashboard's slower tag latch is the only gate)",
    (sum(1 for r_ in snap["rows"] if r_["gated"]), len(snap["rows"]) == len(out_sp) // 9),
    (0, True),
)


# ------------------------------------------------------------------------------------------------ loopback-only CLI
def refuses(addr):
    try:
        S.parse_loopback(addr)
    except ValueError as ex:
        return "only loopback" in str(ex)
    return False


NON_LOOPBACK = ["198.51.100.5:48520", "0.0.0.0:48410", "203.0.113.2:1", "example.com:1"]
check(
    "parse_loopback raises ValueError('... only loopback addresses are allowed') for each of " + ", ".join(NON_LOOPBACK),
    all(refuses(a) for a in NON_LOOPBACK),
    str([a for a in NON_LOOPBACK if not refuses(a)]),
)
for argv in (
    ["--to", "198.51.100.5:48520"],
    ["--listen", "0.0.0.0:48410"],
    ["--to", "203.0.113.2:1", "--listen", "127.0.0.1:48410"],
    ["--listen", "example.com:1"],
):
    if all(refuses(a) for a in NON_LOOPBACK):  # main() would bind and serve forever if the check above is red: do not call it then
        eq(f"main{argv} refuses a non-loopback address (exit 2, nothing bound)", S.main(argv), 2)
    else:
        check(f"main{argv} refuses a non-loopback address (not run: parse_loopback accepts a non-loopback address)", False)
check(
    "positive twin: loopback addresses are accepted (127.0.0.1, localhost, ::1)",
    S.parse_loopback("127.0.0.1:48410") == ("127.0.0.1", 48410)
    and S.parse_loopback("localhost:5") == ("127.0.0.1", 5)
    and S.parse_loopback("[::1]:7") == ("::1", 7),
)


# ------------------------------------------------------------------------------------------------ the real process over UDP
def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
rx.bind(("127.0.0.1", 0))
rx.settimeout(0.5)
lp = free_port()
proc = subprocess.Popen(
    [
        sys.executable,
        "-I",
        os.path.join(os.environ.get("BMA_PC_DIR", HERE), "bma_spectrum.py"),
        "--listen",
        f"127.0.0.1:{lp}",
        "--to",
        f"127.0.0.1:{rx.getsockname()[1]}",
    ],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
got = []
stop = threading.Event()


def drain():
    """Collect datagrams while the test is still transmitting: the kernel receive buffer (rmem_max) is never what holds them."""
    while not stop.is_set() and len(got) < 315:
        try:
            got.append(rx.recv(2048))
        except socket.timeout:
            pass


reader = threading.Thread(target=drain, daemon=True)
reader.start()
try:
    time.sleep(1.0)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sig = np.tile(tone(1100, 0.5, n6), (8, 1))
    for k, b0 in enumerate(range(0, n6 - 255, 256)):
        for s in range(8):
            tx.sendto(
                b"BMAA" + bytes([1, 0, s, 0]) + struct.pack("<III", k, 5000 + b0, 256) + to_int(sig[s][b0 : b0 + 256]).astype("<i4").tobytes(),
                ("127.0.0.1", lp),
            )
        if k % 50 == 49:
            time.sleep(0.01)
    reader.join(5)
finally:
    stop.set()
    reader.join(2)
    proc.kill()
    proc.wait()
    out_, err_ = proc.stdout.read(), proc.stderr.read()
    proc.stdout.close()
    proc.stderr.close()
ur = rows_of(got)
eq("end to end over UDP: the process emits 315 datagrams for 6 s of audio and they all parse with the contract reference", (len(got), len(ur)), (315, 315))
eq("end to end: band 7 of the mean beam = 182, every other band 0", ur[(0, 10)]["bins"], [0] * 7 + [182] + [0] * 38)
check("the process printed no audio: its stdout/stderr carry only status text", b"Traceback" not in err_ and len(out_) < 400, err_[-200:])

print(f"\n{n_checks} checks, {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
