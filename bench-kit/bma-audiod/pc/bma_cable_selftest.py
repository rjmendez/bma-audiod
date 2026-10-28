#!/usr/bin/env python3
"""End-to-end self-test of the BMA cable path (operator tool). Builds a few seconds of 16-channel cable output with the
real Composer (synthetic 8-beam signal + real BMD1 data frames: board records, bridge status, anchors, beam-band
records), plays it into the virtual cable exactly as the sink does (WASAPI shared, float32, 16 ch), records the other
end ("CABLE Output", float32), decodes it with the real decoder and checks

  * the index track: every one of the rows comes back as the expected card sample index, in one unbroken run
  * the data track: the decoded BMD1 frames equal the frames that were sent, byte for byte, in order
  * the beams: equal to what was sent, exactly or within the known path loss (the low 5 bits, plus a step of +-32
    counts: |error| <= 64)
  * the symbol channels sit on their 256-count grid (largest distance <= 64), no symbol outside 0..4095, nothing on
    the reserved channels. A path that applies a gain (VB-Cable shows ~0.985 on loud content) fails here and on the index

Exit code: 0 all good, 1 the path does not carry the format, 2 not run (cannot reach the devices, or another stream is
already writing on channels 9-11, which means the live sink is running: stop it first).

    python bma_cable_selftest.py [--device "CABLE In 16"] [--input "CABLE Output"] [--seconds 4]

Run it ONLY while the sink is stopped; it refuses to start when it sees non-zero data on channels 9-11."""

import argparse, json, os, sys, time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bma_cable_sink as S  # noqa: E402
import bma_cable_decode as D  # noqa: E402

RATE = S.RATE
SCALE24 = 8388608.0
LOSS_TOL = 64  # counts: the low 5 bits lost (up to 31) plus a step of +-32
START_INDEX = 0x12345600  # a card index with every field (bits 0-11, 12-23, 24-31) non-trivial; far from the u32 wrap
CHUNK = 480  # rows per Composer call (10 ms), like the sink's datagram blocks
TAIL = RATE // 5  # extra rows composed after the verified ones: the ring never plays a final partial block
PRECAPTURE_S = 1.0
LEAD = 1000  # all-zero rows played BEFORE the first index: a new stream fades in over its first ~480 rows (10 ms),
# which on a real path destroys them; all-zero rows on ch9-11 are the silent-row marker, so the
# decoder skips them and verification starts at START_INDEX
QUIET_AMP = 0.01  # beams at -40 dBFS like production (beams run at about -40 dBFS or lower)
LOUD_AMP = 0.3  # the loud segment: reported as INFO only, it never changes the verdict (0.3 FS is unmeasured on the real path)
LOUD = RATE // 2  # rows of the loud segment, the last ones of the verified stretch


def beam_counts(total, n):
    """Synthetic 8-beam signal as exact signed 24-bit integers with every low bit populated (a sine per beam plus noise):
    -40 dBFS everywhere except rows n - LOUD .. n - 1 (the INFO-only loud segment, 0.3 FS)."""
    rng = np.random.default_rng(20261025)
    t = np.arange(total, dtype=np.float64) / RATE
    amp = np.full(total, QUIET_AMP)
    amp[max(0, n - LOUD) : n] = LOUD_AMP
    cols = []
    for b in range(S.NCH):
        x = np.rint(amp * (1 << 23) * np.sin(2 * np.pi * (300.0 + 170.0 * b) * t)).astype(np.int64)
        x += rng.integers(-4000, 4000, size=total)
        cols.append(np.clip(x, -(1 << 23), (1 << 23) - 1))
    return np.stack(cols, axis=1)


def build(seconds, start=START_INDEX):
    """-> (frames, n, counts): frames float32 (LEAD + n + TAIL, 16): LEAD all-zero rows, then the real Composer output of which
    the first n rows are verified; counts are the n + TAIL beam counts sent after the lead-in.
    The data track carries 40 board records, a bridge status, anchors every 0.5 s and the band records the BandMeter
    produces from the beams."""
    n = int(round(seconds * RATE))
    total = n + TAIL
    counts = beam_counts(total, n)
    beams = (counts * (1.0 / SCALE24)).astype(np.float32)
    data = S.DataTrack()
    comp = S.Composer(data, anchor_every=RATE // 2, clock_us=lambda: 1_792_884_000_000_000)
    meter = S.BandMeter(clock_us=lambda: 1_792_884_000_000_000)
    chunks = (total + CHUNK - 1) // CHUNK
    rec_at = {int(i * (chunks * 0.6) / 40): i for i in range(40)}
    out = []
    for c in range(chunks):
        lo, hi = c * CHUNK, min(total, (c + 1) * CHUNK)
        if c in rec_at:
            i = rec_at[c]
            data.push(
                S.K_BOARD,
                json.dumps(
                    {
                        "type": "monitor",
                        "seq": i,
                        "dsp": 4 + i % 6,
                        "slot": i % 7,
                        "rms_dbfs": -60.0 + 0.25 * i,
                        "peak": 16390 + i,
                        "host_t": 1792884000.0 + 0.1 * i,
                    },
                    separators=(",", ":"),
                ).encode(),
            )
        if c == 3:
            data.push(S.K_BRIDGE, json.dumps({"t": 1792884000.0, "state": "ok", "loss_pct": 0.25}, separators=(",", ":")).encode())
        S.feed_bands(meter, start + lo, beams[lo:hi], data, None)
        out.append(comp.compose(start + lo, beams[lo:hi]))
    return np.concatenate([np.zeros((LEAD, S.OUT_CH), np.float32)] + out), n, counts


def to_counts(x):
    return np.rint(np.asarray(x, dtype=np.float64) * SCALE24).astype(np.int64)


def verify(sent, n, counts, recorded, start=START_INDEX):
    """Compare what was recorded with what was sent. -> (ok, list of report lines)."""
    lines, ok = [], True
    k = to_counts(recorded)
    d0 = D.decode(k)
    live = np.nonzero(d0["index"] == (start & 0xFFFFFFFF))[0]
    if not len(live):
        return False, [
            "FAIL index: the first sent index %d never appears in the recording (%d rows recorded; largest distance from "
            "the symbol grid %d counts, %d off-grid samples)" % (start, len(k), d0["max_residual"], d0["impossible"])
        ]
    p = int(live[0])
    seg = k[p : p + n]
    if len(seg) < n:
        ok = False
        lines.append(f"FAIL index: only {len(seg)} of {n} rows were recorded after the stream start")
    d = D.decode(seg)
    want = (np.arange(start, start + len(seg), dtype=np.int64)) & 0xFFFFFFFF
    bad = np.nonzero(d["index"] != want)[0]
    if len(bad):
        ok = False
        j = int(bad[0])
        lines.append(f"FAIL index: {len(bad)} of {len(seg)} rows differ, first at row {j}: got {int(d['index'][j])}, expected {int(want[j])}")
    else:
        lines.append(f"PASS index: all {len(seg)} rows recovered exactly (u32 index {start} .. {start + len(seg) - 1})")
    ref = D.decode(to_counts(sent[LEAD : LEAD + len(seg)]))
    if d["frames"] == ref["frames"] and len(ref["frames"]) > 0:
        lines.append(f"PASS data: all {len(ref['frames'])} BMD1 frames recovered, byte for byte, in order, 0 corrupt")
    else:
        ok = False
        got = {kk: sum(1 for kd, _ in d["frames"] if kd == kk) for kk in (1, 2, 3, 4)}
        want_n = {kk: sum(1 for kd, _ in ref["frames"] if kd == kk) for kk in (1, 2, 3, 4)}
        lines.append(f"FAIL data: frames by kind {got} recovered, {want_n} sent; corrupt frames {d['bad_frames']}")
    out_of_grid = d["max_residual"] > LOSS_TOL
    if out_of_grid or d["range_errors"] or d["reserved_nonzero"]:
        ok = False
        lines.append(
            f"FAIL format: largest distance from the symbol grid {d['max_residual']} counts (limit {LOSS_TOL}), "
            f"{d['range_errors']} out-of-range symbols/bytes, {d['impossible']} off-grid/impossible samples (first at row {d['impossible_first']}), "
            f"{d['reserved_nonzero']} non-zero reserved samples"
        )
    else:
        lines.append(f"PASS format: symbols on the grid (largest distance {d['max_residual']} counts, limit {LOSS_TOL}), reserved channels 14-16 all zero")
    quiet = max(0, min(len(seg), n - LOUD))  # the verdict covers the -40 dBFS rows only
    err = k[p : p + quiet, : S.NCH] - counts[:quiet]
    worst = int(np.abs(err).max()) if err.size else 0
    if worst == 0:
        lines.append("PASS beams: bit-exact (the path preserved all 24 bits)")
    elif worst <= LOSS_TOL:
        lines.append(f"PASS beams: within the known path loss (max error {worst} counts, limit {LOSS_TOL})")
    else:
        ok = False
        lines.append(f"FAIL beams: max error {worst} counts exceeds the known path loss (limit {LOSS_TOL})")
    if len(seg) > quiet:
        loud = k[p + quiet : p + len(seg), : S.NCH] - counts[quiet : len(seg)]
        lines.append(f"INFO loud segment (beams at 0.3 FS, {len(loud)} rows, not part of the verdict): max error {int(np.abs(loud).max())} counts")
    return ok, lines


class SoundDeviceLoopback:
    """The real device pair (`sd` and `sleep` can be replaced by recording stand-ins in the tests)."""

    def __init__(self, out_needle, in_needle, sd=None, sleep=time.sleep):
        if sd is None:
            import sounddevice as sd
        self.sd, self.sleep = sd, sleep
        self.teardown_errors = []
        S.reinit_portaudio(sd)  # nothing is open in this process: a fresh device list
        self.out_needle, self.in_needle = out_needle, in_needle
        wasapi = [i for i, a in enumerate(sd.query_hostapis()) if a["name"] == "Windows WASAPI"]
        if not wasapi:
            raise RuntimeError("no WASAPI host API")
        self.in_dev = None
        for i, dev in enumerate(sd.query_devices()):
            if dev["hostapi"] == wasapi[0] and dev["max_input_channels"] >= S.OUT_CH and in_needle.lower() in dev["name"].lower():
                self.in_dev = i
                break
        if self.in_dev is None:
            raise RuntimeError(f"no WASAPI input device matching {in_needle!r} with {S.OUT_CH} channels (set the cable's format to {S.OUT_CH} channels)")

    def _input(self, got):
        return self.sd.InputStream(
            device=self.in_dev, samplerate=RATE, channels=S.OUT_CH, dtype="float32", callback=lambda ind, n, t, st: got.append(ind.copy())
        )

    def _teardown(self, steps):
        """Run every step even when an earlier one raises; the errors are kept, never allowed to skip a later step."""
        for name, fn in steps:
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                self.teardown_errors.append(f"{name}: {e}")

    def precapture(self, seconds):
        got = []
        inp = self._input(got)
        try:
            inp.start()
            self.sleep(seconds)
        finally:
            self._teardown([("input.stop", inp.stop), ("input.close", inp.close)])
        return np.concatenate(got) if got else np.zeros((0, S.OUT_CH), np.float32)

    def play_record(self, frames):
        ring = S.Ring(target_ms=150, max_ms=10**7)
        ring.put(frames)
        got = []
        inp = self._input(got)
        stream = None
        try:
            inp.start()
            stream, _dev = S.open_output_stream(self.sd, self.out_needle, lambda outdata, n, _t, _s: outdata.__setitem__(slice(None), ring.get(n)))
            self.sleep(len(frames) / RATE + 1.5)
        finally:
            steps = [("output.stop", stream.stop), ("output.close", stream.close)] if stream is not None else []
            self._teardown(steps + [("input.stop", inp.stop), ("input.close", inp.close)])
        return np.concatenate(got) if got else np.zeros((0, S.OUT_CH), np.float32)


def run(loop, seconds=4.0, out=print):
    """-> exit code 0 / 1 / 2 (see the module doc)."""
    pre = loop.precapture(PRECAPTURE_S)
    if len(pre) < RATE // 10:
        out(f"NOT RUN: only {len(pre)} rows could be recorded from the cable in {PRECAPTURE_S} s; is the input device alive?")
        return 2
    busy = int(np.count_nonzero(to_counts(pre[:, 8:11])))
    if busy:
        out(
            f"NOT RUN: {busy} non-zero samples on channels 9-11 of the cable before the test started: another stream "
            f"(the live sink?) is writing on this device. Stop it, then run this test again."
        )
        return 2
    sent, n, counts = build(seconds)
    rec = loop.play_record(sent)
    ok, lines = verify(sent, n, counts, rec)
    for ln in lines:
        out(ln)
    out("SELFTEST " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--device", default="CABLE In 16", help="substring of the WASAPI output device name (the sink's --device)")
    ap.add_argument("--input", default="CABLE Output", help="substring of the WASAPI input device name")
    ap.add_argument("--seconds", type=float, default=4.0)
    a = ap.parse_args()
    try:
        loop = SoundDeviceLoopback(a.device, a.input)
    except Exception as e:  # noqa: BLE001
        print(f"NOT RUN: {e}")
        return 2
    try:
        return run(loop, a.seconds)
    except Exception as e:  # noqa: BLE001 - e.g. the output endpoint is not configured for 16 channels
        print(f"NOT RUN: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
