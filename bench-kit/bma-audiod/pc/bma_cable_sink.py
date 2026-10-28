#!/usr/bin/env python3
"""Windows audio sink: plays the BMA's 8 beams, a sample-accurate timestamp track and the board's control-channel
debug data as ONE 16-channel, 48 kHz WASAPI stream into a virtual audio cable (VB-Cable "CABLE In 16 Ch"). Any
Windows app can record all of it, sample-synchronously, from "CABLE Output" (Audacity, OBS, a DAW, ffmpeg).

Channel layout (cable-stream 2.0.0). Channels 9-13 are SYMBOL channels: a symbol u in 0..4095 is carried as the
sample (u - 2048) << 8 counts (float32 = (u - 2048) / 2^15, peak 0.0625 full scale, a grid of 256 counts). Measured on
VB-Cable: the path loses the low 5 bits of every 24-bit sample and applies a gain (~0.985) to loud content; symbols
this quiet and this coarse come through exactly (a reader rounds, u = ((counts + 128) >> 8) + 2048). Beam audio keeps
~19 bits and may see that gain at very high levels (beams run at -40 dBFS or lower).
    1-8    beams 1-8 (slot 0-7 of the bridge), the card's audio as sent (unchanged)
    9      card sample index of THIS sample (u32), bits 0-11
    10     card sample index, bits 12-23
    11     card sample index, bits 24-31 (u < 256); index = u9 | u10 << 12 | u11 << 24, wraps every 2^32 samples (the
           anchors carry the full 64-bit index). A row of all-zero samples on 9-11 (u = 2048 on ch11, which no index
           can have) marks a row the sink invented for an underrun: no data.
    12     data track, first byte of the row's pair (u = the byte value 0..255)
    13     data track, second byte. 2 bytes per row = 96 kB/s; zero bytes (u = 0) when idle.
           Frames:  b"BMD1", kind u8, length u16 BE, payload, crc32 u32 BE (over kind|length|payload).
           kind 1 anchor   payload ">QQ": card sample index, PC UTC microseconds, sent about once a second
           kind 2 board    payload = one JSON line of the board's control channel (meter / monitor / health /
                           status records exactly as bma_debug_rx.py wrote them, each with its own host_t)
           kind 3 bridge   payload = the bridge's status JSON (packet loss, gaps, state)
           kind 4 bands    payload = one JSON line per second of contiguous audio: {"k":"beam_bands","v":1,
                           "idx": card index of the window's first sample, "t_us": PC UTC us, "n": samples,
                           "zero_frac", "edges_hz": 8 octave bands 50 Hz-16 kHz, "db": [8 beams][8 bands]} where each
                           value is mean-square dB re full scale (-200 = no energy). The same line is appended to the
                           --bands-log JSONL. Audio itself is never stored.
    14-16  reserved, zero
Decode with bma_cable_decode.py (recovers the index track, anchors -> wall clock, and the debug records); verify
the whole path end to end with bma_cable_selftest.py (only with the sink stopped).

Input: the bridge's per-beam BMAA datagrams (start the bridge with --all-to 127.0.0.1:48400). Each datagram is
b"BMAA", 1, 0, slot, flags, <u32 seq, u32 block, u32 n>, then n int32 LE samples (sign-extended 24 bit);
`block` is the absolute index of the first sample. Slots are aligned on it, a missing slot is zero-filled for
that block (counted), and a hole in the index is filled with silence (its index track keeps counting) so the
stream keeps real time.

Clocks: the card and the PC sound engine run on different crystals. The output ring prebuffers; when it grows past
max-ms the OLDEST frames are dropped back to the target (counted as `slips`, visible as a jump in the index
track); an underrun plays silence (all-zero samples on ch9-11, the silent-row marker; index 0 is a live row). Nothing is resampled.

    python bma_cable_sink.py --device "CABLE In 16" --listen 127.0.0.1:48400 --status-file <data-dir>/sink_status.json
"""

import argparse, collections, json, math, os, socket, struct, sys, threading, time, zlib

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I (isolated mode) does not add the script directory
from history_log import RotatingJsonl, HISTORY_DIR, DATA_DIR  # noqa: E402

NCH = 8  # beams
RATE = 48000
SCALE = 1.0 / 8388608.0  # int32 holds a sign-extended 24-bit value
SYM_SCALE = 256.0 * SCALE  # a 12-bit symbol u is carried as (u - 2048) << 8 counts = (u - 2048) / 2^15 (exact in float32)
SYM_ZERO = 2048
BYTES_PER_ROW = 2  # data track: two bytes per output row, one per channel
MAX_GAP_FILL = RATE // 2  # never fill more than 0.5 s of silence for one hole
MAX_PENDING = 8  # blocks waiting for the slowest slot before it is declared missing
MAGIC = b"BMD1"
K_ANCHOR, K_BOARD, K_BRIDGE, K_BANDS = 1, 2, 3, 4
BAND_EDGES = [(50, 125), (125, 250), (250, 500), (500, 1000), (1000, 2000), (2000, 4000), (4000, 8000), (8000, 16000)]
BANDS_LOG = os.path.join(DATA_DIR, "reports", "beam_bands.jsonl")  # metadata only
COL_IDX0, COL_IDX1, COL_IDX2, COL_DATA0, COL_DATA1 = 8, 9, 10, 11, 12
OUT_CH = 16
MAX_DATA_FIFO = 1 << 18  # bytes queued for the data track before new records are dropped (2.7 s of the 96 kB/s track)


def parse_datagram(pkt):
    """-> (slot, flags, block, float32 samples) or None for anything that is not a well-formed BMAA datagram
    (contracts/specs/bmaa.md): magic, version 1, reserved 0, slot 0-7, length exactly 20 + 4n, every sample inside
    the signed 24-bit range."""
    if len(pkt) < 20 or pkt[:4] != b"BMAA":
        return None
    if pkt[4] != 1 or pkt[5] != 0:
        return None
    slot, flags = pkt[6], pkt[7]
    _seq, block, n = struct.unpack_from("<III", pkt, 8)
    if slot >= NCH or len(pkt) != 20 + 4 * n:
        return None
    raw = np.frombuffer(pkt, dtype="<i4", count=n, offset=20)
    if n and (int(raw.min()) < -(1 << 23) or int(raw.max()) > (1 << 23) - 1):
        return None
    data = raw.astype(np.float32) * SCALE
    return slot, flags, block, data


def sym12_to_f32(u):
    """12-bit symbols u (0..4095; anything else is a bug and raises) -> float32 of the sample (u - 2048) << 8:
    exactly (u - 2048) / 2^15, a multiple of 256 counts with peak 0.0625 full scale."""
    u = np.asarray(u, dtype=np.int64)
    if u.size and (int(u.min()) < 0 or int(u.max()) > 4095):
        raise ValueError("symbol outside 0..4095")
    return ((u - SYM_ZERO) * SYM_SCALE).astype(np.float32)


def encode_frame(kind, payload):
    """One data-track frame (see the module doc)."""
    body = struct.pack(">BH", kind, len(payload)) + payload
    return MAGIC + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


class Assembler:
    """Aligns the 8 per-beam streams on the absolute block index and yields (start_index, (n, 8) float32 frame).

    The BMAA `block` field is a u32 and wraps every 2^32 samples (about 24.9 h at 48 kHz). It is unwrapped here to a
    monotonic 64-bit index (relative to the highest index seen, so reordering across the wrap is handled), and every
    index the sink emits - the index track and the band records - is the unwrapped one."""

    def __init__(self):
        self.pending = {}  # unwrapped block index -> {slot: samples}
        self.next_block = None  # unwrapped index expected next
        self.ref = None  # highest unwrapped index seen
        self.missing_slot_blocks = 0
        self.holes = 0

    def _unwrap(self, block):
        if self.ref is None:
            self.ref = block
            return block
        d = (block - self.ref) & 0xFFFFFFFF
        if d >= 1 << 31:
            d -= 1 << 32
        u = self.ref + d
        if u > self.ref:
            self.ref = u
        return u

    def push(self, slot, block, data):
        self.pending.setdefault(self._unwrap(block), {})[slot] = data
        return self._pop()

    def _pop(self):
        out = []
        while self.pending:
            k = min(self.pending)
            have = self.pending[k]
            if len(have) < NCH and len(self.pending) <= MAX_PENDING:
                break
            n = max(len(v) for v in have.values())
            fr = np.zeros((n, NCH), np.float32)
            for s, v in have.items():
                fr[: len(v), s] = v
            self.missing_slot_blocks += NCH - len(have)
            if self.next_block is not None and k > self.next_block:
                self.holes += 1
                gap = min(k - self.next_block, MAX_GAP_FILL)
                out.append((k - gap, np.zeros((gap, NCH), np.float32)))
            if self.next_block is None or k >= self.next_block:  # a late duplicate of played audio is dropped
                out.append((k, fr))
                self.next_block = k + n
            del self.pending[k]
        return out


class DataTrack:
    """Byte FIFO for the data track; `take(n)` returns an (n, 2) float32 array: per row two bytes, one per channel
    (ch12 first, ch13 second), each as a 12-bit symbol u = the byte value (idle = zero bytes, u = 0).
    Capacity is 2 bytes per row = 96 kB/s at 48 kHz. The last `headroom` bytes of the queue (default 1/8) are reserved for
    the non-blocking records (anchors, bridge status, band records): a board-record backlog that waits for room stops
    at the high-water mark `max - headroom`, so it can never starve them."""

    def __init__(self, max_bytes=MAX_DATA_FIFO, headroom=None):
        self.fifo = bytearray()
        self.lock = threading.Lock()
        self.frames = 0
        self.dropped = 0
        self.max = max_bytes
        self.headroom = max_bytes // 8 if headroom is None else headroom

    def _try(self, kind, payload, limit=None):
        fr = encode_frame(kind, payload[:65535])
        with self.lock:
            if len(self.fifo) + len(fr) > (self.max if limit is None else limit):
                return False
            self.fifo += fr
            self.frames += 1
            return True

    def offer(self, kind, payload):
        """One attempt for a backpressured source: accepted only below the high-water mark (max - headroom). No counting."""
        return self._try(kind, payload, self.max - self.headroom)

    def push(self, kind, payload):
        """Never blocks (anchors, band records and bridge status come from threads that must not stall); a record that
        does not fit is refused and counted in `dropped`."""
        if self._try(kind, payload):
            return True
        with self.lock:
            self.dropped += 1
        return False

    def push_wait(self, kind, payload, timeout_s=5.0, poll_s=0.02):
        """Backpressure for a source that can be re-read (the board debug log being followed): wait for room instead of
        dropping, so a burst of backlog is delivered as the audio drains the queue (up to 96 KB/s). Only a queue
        that stays full for `timeout_s` (the audio has stopped) drops the record, counted in `dropped`."""
        end = time.monotonic() + timeout_s
        while not self.offer(kind, payload):
            if time.monotonic() >= end:
                with self.lock:
                    self.dropped += 1
                return False
            time.sleep(poll_s)
        return True

    def take(self, n):
        with self.lock:
            chunk = bytes(self.fifo[: BYTES_PER_ROW * n])
            del self.fifo[: BYTES_PER_ROW * n]
        chunk += b"\x00" * (BYTES_PER_ROW * n - len(chunk))
        b = np.frombuffer(chunk, dtype=np.uint8).reshape(n, BYTES_PER_ROW).astype(np.int64)
        return sym12_to_f32(b)


class Composer:
    """Turns assembled beam frames into the 16-channel output frames (index track + data track) as 12-bit symbols
    on the 256-count grid (cable-stream 2.0.0)."""

    def __init__(self, data, anchor_every=RATE, clock_us=lambda: time.time_ns() // 1000):
        self.data = data
        self.anchor_every = anchor_every
        self.clock_us = clock_us
        self.last_anchor = None

    def compose(self, start, beams):
        n = len(beams)
        if self.last_anchor is None or start - self.last_anchor >= self.anchor_every:
            self.data.push(K_ANCHOR, struct.pack(">QQ", start, self.clock_us()))
            self.last_anchor = start
        out = np.zeros((n, OUT_CH), np.float32)
        out[:, :NCH] = beams
        idx = np.arange(start, start + n, dtype=np.int64) & 0xFFFFFFFF  # the u32 card index; anchors carry all 64 bits
        out[:, COL_IDX0] = sym12_to_f32(idx & 0xFFF)
        out[:, COL_IDX1] = sym12_to_f32((idx >> 12) & 0xFFF)
        out[:, COL_IDX2] = sym12_to_f32(idx >> 24)
        out[:, COL_DATA0 : COL_DATA1 + 1] = self.data.take(n)
        return out


class BandMeter:
    """Per-beam octave-band levels, one record per second of CONTIGUOUS aligned audio (kind 4 on the data track).

    Level = mean-square in the band (Hann-windowed FFT, Parseval-scaled), in dB re full scale (a full-scale sine
    reads -3.01 dB); -200 means no energy. Audio is only held for the current window and never stored."""

    def __init__(self, window=RATE, edges=BAND_EDGES, clock_us=lambda: time.time_ns() // 1000):
        self.win = window
        self.edges = edges
        self.clock_us = clock_us
        freqs = np.fft.rfftfreq(window, 1.0 / RATE)
        self.masks = [(freqs >= lo) & (freqs < hi) for lo, hi in edges]
        self.hann = np.hanning(window).astype(np.float64)
        self.scale = 2.0 / (window * window * float(np.mean(self.hann**2)))
        self.buf = []
        self.n = 0
        self.start = None  # absolute index of the first buffered sample
        self.expected = None  # index the next frame must start at to stay contiguous

    def push(self, start, beams):
        out = []
        if self.expected is not None and start != self.expected:  # a hole bigger than the gap fill: start over
            self.buf, self.n, self.start = [], 0, None
        if self.start is None:
            self.start = start
        self.expected = start + len(beams)
        self.buf.append(beams)
        self.n += len(beams)
        while self.n >= self.win:
            x = np.concatenate(self.buf)
            win, rest = x[: self.win], x[self.win :]
            out.append(self._record(self.start, win))
            self.buf = [rest] if len(rest) else []
            self.n = len(rest)
            self.start += self.win
        return out

    def _record(self, start, x):
        spec = np.abs(np.fft.rfft(x.astype(np.float64) * self.hann[:, None], axis=0)) ** 2  # (bins, 8)
        db = [[round(10.0 * math.log10(float(self.scale * spec[m, b].sum()) + 1e-20), 1) for m in self.masks] for b in range(NCH)]
        return {
            "k": "beam_bands",
            "v": 1,
            "idx": int(start),
            "t_us": int(self.clock_us()),
            "n": self.win,
            "zero_frac": round(float(np.mean(~x.any(axis=1))), 4),
            "edges_hz": [list(e) for e in self.edges],
            "db": db,
        }


class JsonlLog(RotatingJsonl):
    """Append-only JSONL with one-backup size rotation (metadata only). Rotation falls back to copy + truncate when the
    rename is refused (a reader holds the file); it used to give up for good after the first refused rename."""

    def __init__(self, path, cap_bytes=256 << 20):
        super().__init__(path, cap_bytes)

    def write(self, line):
        self.write_line(line)


SINK_HISTORY_CAP = 32 << 20  # sink_history.jsonl; with the one backup 64 MB, about 0.66 MB/day = months


def build_sink_sample(st, started_t):
    """One sink_status history record (contract run-history, k sink_sample) from the status dict `st` the sink prints every
    stats interval. The counters are cumulative since `started_t` (the sink process start): a reader takes deltas and
    treats a changed started_t as a counter reset."""
    return {
        "k": "sink_sample",
        "v": 1,
        "t": round(st["t"], 3),
        "started_t": round(started_t, 3),
        "holes": st["holes"],
        "slips": st["slips"],
        "slipped_frames": st["slipped_frames"],
        "underruns": st["underruns"],
        "missing_slot_blocks": st["missing_slot_blocks"],
        "data_dropped": st["data_dropped"],
        "data_frames": st["data_frames"],
        "playing": st["playing"],
        "audio_output": st["audio_output"],
        "device_error": st["device_error"],
        "buffered_ms": st["buffered_ms"],
        "datagrams": st["datagrams"],
        "bad": st["bad"],
    }


def feed_bands(meter, start, beams, data, log):
    """Run the band meter on one aligned frame; each record goes to the data track and the log."""
    for rec in meter.push(start, beams):
        line = json.dumps(rec, separators=(",", ":")).encode()
        data.push(K_BANDS, line)
        if log:
            log.write(line)


class Ring:
    """Thread-safe frame FIFO with prebuffer, slip-on-overrun and silence-on-underrun."""

    def __init__(self, target_ms, max_ms, ncols=OUT_CH):
        self.q = collections.deque()
        self.ncols = ncols
        self.frames = 0
        self.lock = threading.Lock()
        self.target = RATE * target_ms // 1000
        self.max = RATE * max_ms // 1000
        self.started = False
        self.underruns = 0
        self.slips = 0
        self.slipped_frames = 0

    def put(self, fr):
        with self.lock:
            self.q.append(fr)
            self.frames += len(fr)
            if self.frames > self.max:
                drop = self.frames - self.target
                self.slips += 1
                self.slipped_frames += drop
                while drop > 0 and self.q:
                    h = self.q[0]
                    if len(h) <= drop:
                        drop -= len(h)
                        self.frames -= len(h)
                        self.q.popleft()
                    else:
                        self.q[0] = h[drop:]
                        self.frames -= drop
                        drop = 0

    def get(self, n):
        out = np.zeros((n, self.ncols), np.float32)
        with self.lock:
            if not self.started:
                if self.frames < self.target:
                    return out
                self.started = True
            if self.frames < n:
                self.underruns += 1
                self.started = False  # rebuffer to the target before playing again
                return out
            got = 0
            while got < n:
                h = self.q[0]
                take = min(len(h), n - got)
                out[got : got + take] = h[:take]
                got += take
                if take == len(h):
                    self.q.popleft()
                else:
                    self.q[0] = h[take:]
            self.frames -= n
        return out


def tail_lines(path, stop, on_line, poll_s=0.5):
    """Follow `path` from its current end (like tail -F); calls on_line(bytes) per complete line."""
    f = None
    pos = 0
    buf = b""
    while not stop.is_set():
        try:
            if f is None:
                f = open(path, "rb")
                f.seek(0, 2)
                pos = f.tell()
            if os.path.getsize(path) < pos:  # rotated or truncated
                f.seek(0)
                pos = 0
            f.seek(pos)
            chunk = f.read(1 << 16)  # bounded: a backlog is delivered in pieces, not as one burst
            pos = f.tell()
        except OSError:
            if f:
                f.close()
                f = None
            stop.wait(poll_s)
            continue
        if not chunk:
            stop.wait(poll_s)
            continue
        buf += chunk
        *lines, buf = buf.split(b"\n")
        for ln in lines:
            ln = ln.strip()
            if ln:
                on_line(ln)


def follow_board_debug(path, stop, data, poll_s=0.5):
    """Follow the board debug JSONL into the data track (kind 2) WITH backpressure: a backlog is delivered as the audio
    drains the queue instead of overflowing it. Used by both the live sink and --record."""
    th = threading.Thread(target=tail_lines, args=(path, stop, lambda ln: data.push_wait(K_BOARD, ln), poll_s), daemon=True)
    th.start()
    return th


def pick_device(sd, needle):
    wasapi = [i for i, a in enumerate(sd.query_hostapis()) if a["name"] == "Windows WASAPI"]
    if not wasapi:
        raise RuntimeError("no WASAPI host API")
    for i, d in enumerate(sd.query_devices()):
        if d["hostapi"] == wasapi[0] and d["max_output_channels"] > 0 and needle.lower() in d["name"].lower():
            return i, d
    raise RuntimeError(f"no WASAPI output device matching {needle!r}")


def reinit_portaudio(sd):
    """PortAudio snapshots the device list when it is initialised, so a running process never sees a Windows format
    change (an endpoint switched from 2 to 16 channels still reads as 2). Terminate and initialise it again; only
    ever call this while NO stream is open (OutputOpener guarantees that)."""
    term, init = getattr(sd, "_terminate", None), getattr(sd, "_initialize", None)
    if term is None or init is None:
        return False
    try:
        term()
    except Exception:  # noqa: BLE001 - not initialised (a previous re-init failed half way): initialise anyway
        pass
    init()
    return True


def open_output_stream(sd, needle, callback):
    """Open and start the 16-channel WASAPI shared-mode float32 output exactly as the sink plays it.
    -> (stream, device dict). Raises when the endpoint is missing or not configured for OUT_CH channels."""
    idx, dev = pick_device(sd, needle)
    if dev["max_output_channels"] < OUT_CH:
        raise RuntimeError(
            f"{dev['name']!r} is configured for {dev['max_output_channels']} channel(s), need "
            f"{OUT_CH}: set its default format to {OUT_CH} channels, 24 bit, 48000 Hz"
        )
    s = sd.OutputStream(
        device=idx, samplerate=RATE, channels=OUT_CH, dtype="float32", callback=callback, latency="high", extra_settings=sd.WasapiSettings(auto_convert=False)
    )
    try:
        s.start()
    except BaseException:
        try:  # a constructed stream that never started must not be left for sounddevice's exit handler
            s.close()
        except Exception:  # noqa: BLE001
            pass
        raise
    return s, dev


class OutputOpener:
    """The sink's retried output open. Every retry first re-initialises PortAudio (fresh device list); while a stream
    is open nothing is touched - a re-init would tear the healthy stream down."""

    def __init__(self, sd, needle, callback, log=print):
        self.sd, self.needle, self.callback, self.log = sd, needle, callback, log
        self.stream = None
        self.dev = None
        self.err = None

    def try_open(self):
        if self.sd is None or self.stream is not None:
            return
        try:
            reinit_portaudio(self.sd)
            s, dev = open_output_stream(self.sd, self.needle, self.callback)
        except Exception as e:  # noqa: BLE001 - any failure just means "not ready yet"
            if str(e) != self.err:
                self.log(f"audio output not available: {e}")
            self.err = str(e)
            return
        self.stream, self.dev, self.err = s, dev["name"], None
        self.log(f"playing {OUT_CH} ch @ {RATE} Hz into {dev['name']!r}")


class OpenRetry:
    """Retry schedule of the output open: try_open at most once per `period` seconds, whatever happens inside it (the
    next attempt is scheduled BEFORE the call, so an exception cannot turn the 15 s back-off into a busy loop). While the
    stream is open nothing is called and the schedule stays due, so a later loss is retried at once."""

    def __init__(self, opener, period=15.0):
        self.opener, self.period = opener, period
        self.next = 0.0

    def tick(self, now):
        if self.opener.stream is not None:
            self.next = now
            return False
        if now < self.next:
            return False
        self.next = now + self.period
        self.opener.try_open()
        return True


def record(a):
    """--record: write the same 16-channel stream to a 24-bit WAV for --seconds, no audio device involved."""
    import soundfile as sf

    asm, data = Assembler(), DataTrack()
    comp = Composer(data)
    meter = BandMeter()
    host, port = a.listen.rsplit(":", 1)
    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sk.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    sk.bind((host, int(port)))
    sk.settimeout(0.5)
    stop = threading.Event()
    if a.board_debug:
        follow_board_debug(a.board_debug, stop, data)
    frames = bad = datagrams = 0
    t_end = time.time() + a.seconds
    t_bridge = 0.0
    with sf.SoundFile(a.record, "w", samplerate=RATE, channels=OUT_CH, subtype="PCM_24") as w:
        while time.time() < t_end:
            if time.time() >= t_bridge:
                try:
                    with open(a.bridge_status, "rb") as f:
                        data.push(K_BRIDGE, f.read(4096).strip())
                except OSError:
                    pass
                t_bridge = time.time() + 10
            try:
                pkt, _ = sk.recvfrom(65535)
            except socket.timeout:
                continue
            p = parse_datagram(pkt)
            if p is None:
                bad += 1
                continue
            datagrams += 1
            for start, fr in asm.push(p[0], p[2], p[3]):
                feed_bands(meter, start, fr, data, None)  # the recording carries them in its data track
                out = comp.compose(start, fr)
                w.write(out)
                frames += len(out)
    stop.set()
    print(
        json.dumps(
            {
                "recorded": a.record,
                "frames": frames,
                "seconds": frames / RATE,
                "datagrams": datagrams,
                "bad": bad,
                "missing_slot_blocks": asm.missing_slot_blocks,
                "holes": asm.holes,
                "data_frames": data.frames,
                "data_dropped": data.dropped,
            }
        ),
        flush=True,
    )


RX_BACKOFF_S = 0.05  # pause after a failed packet, so a persistent failure cannot busy-loop
RX_MAX_CONSECUTIVE_ERRORS = 200  # this many failures in a row and the process exits (a stuck receiver is worse than a restart)
RX_EXIT_CODE = 3


def make_handler(asm, meter, comp, data, ring, out, bands_log, stats):
    """The per-datagram work of the receive thread: parse, align, band-meter, compose, queue for playback."""

    def handle(pkt):
        p = parse_datagram(pkt)
        if p is None:
            stats["bad"] += 1
            return
        stats["datagrams"] += 1
        for start, fr in asm.push(p[0], p[2], p[3]):
            feed_bands(meter, start, fr, data, bands_log)
            frame16 = comp.compose(start, fr)  # always runs, so the data track drains
            if out.stream is not None:
                ring.put(frame16)

    return handle


def _die(code):
    sys.stderr.flush()
    os._exit(code)


def rx_loop(sk, handle, stats, log, max_consecutive_errors=RX_MAX_CONSECUTIVE_ERRORS, sleep=time.sleep, die=_die):
    """The UDP receive thread body. A failure on one packet (MemoryError under commit exhaustion, a numpy error, a socket
    error) must never end the thread: it died silently once, the process kept printing stats and processed nothing.
    Every Exception is counted (stats['rx_errors'], cumulative), logged as one short line for the first 5 and then every
    1000th (exception type and message only, never packet bytes), followed by a RX_BACKOFF_S pause. After
    `max_consecutive_errors` failures in a row (any success resets the run) the process exits with code 3 through
    `die`, so the supervisor restarts it. KeyboardInterrupt and SystemExit are not Exceptions and propagate.
    `die` returns only in tests; the loop then ends."""
    consecutive = 0
    while True:
        try:
            pkt, _ = sk.recvfrom(65535)
            handle(pkt)
        except Exception as e:
            consecutive += 1
            stats["rx_errors"] += 1
            n = stats["rx_errors"]
            if n <= 5 or n % 1000 == 0:
                log(f"rx error #{n} ({consecutive} in a row): {type(e).__name__}: {str(e)[:160]}")
            if consecutive >= max_consecutive_errors:
                log(f"rx: {consecutive} consecutive errors, exiting {RX_EXIT_CODE} for a supervisor restart")
                die(RX_EXIT_CODE)
                return
            sleep(RX_BACKOFF_S)
        else:
            consecutive = 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--device", default="CABLE In 16", help="substring of the WASAPI output device name")
    ap.add_argument("--listen", default="127.0.0.1:48400", help="UDP HOST:PORT for the bridge's --all-to datagrams")
    ap.add_argument(
        "--board-debug",
        default=os.path.join(DATA_DIR, "analysis", "board_debug.jsonl"),
        help="the board debug JSONL (bma_debug_rx.py) to follow into the data track; '' to disable",
    )
    ap.add_argument("--bridge-status", default=os.path.join(DATA_DIR, "bridge_status.json"))
    ap.add_argument("--target-ms", type=int, default=150)
    ap.add_argument("--max-ms", type=int, default=400)
    ap.add_argument("--status-file")
    ap.add_argument("--bands-log", default=BANDS_LOG, help="JSONL of the per-beam octave-band levels (metadata only, rotated at 256 MB); '' to disable")
    ap.add_argument("--stats-s", type=float, default=30.0)
    ap.add_argument(
        "--history-dir",
        default=HISTORY_DIR,
        help="directory of the run-history files: sink_history.jsonl gets one sink_sample per stats interval "
        "(metadata only, 32 MB cap + one backup); '' to disable",
    )
    ap.add_argument("--record", help="write the 16-channel stream to this WAV (24-bit PCM) instead of playing it; no audio device needed")
    ap.add_argument("--seconds", type=float, default=30.0, help="with --record: how long to record")
    a = ap.parse_args()
    if a.record:
        return record(a)

    try:
        import sounddevice as sd
    except ImportError as e:
        sd = None
        print(f"no audio output ({e}); metering only", flush=True)
    ring = Ring(a.target_ms, a.max_ms)
    asm = Assembler()
    data = DataTrack()
    comp = Composer(data)
    meter = BandMeter()
    bands_log = JsonlLog(a.bands_log) if a.bands_log else None
    history = RotatingJsonl(os.path.join(a.history_dir, "sink_history.jsonl"), SINK_HISTORY_CAP) if a.history_dir else None
    started_t = time.time()
    host, port = a.listen.rsplit(":", 1)
    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sk.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    sk.bind((host, int(port)))
    stats = {"datagrams": 0, "bad": 0, "rx_errors": 0}
    stop = threading.Event()

    def cb(outdata, frames, _t, _status):
        outdata[:] = ring.get(frames)

    out = OutputOpener(sd, a.device, cb, log=lambda m: print(m, flush=True))  # optional, retried; metering never stops

    handle = make_handler(asm, meter, comp, data, ring, out, bands_log, stats)
    err_log = lambda m: print(m, file=sys.stderr, flush=True)

    def rx():
        rx_loop(sk, handle, stats, err_log)

    threading.Thread(target=rx, daemon=True).start()
    if a.board_debug:
        follow_board_debug(a.board_debug, stop, data)

    print(f"listening udp {a.listen}; band meter logging to {a.bands_log or '(no log)'}", flush=True)
    t_stat = 0.0
    retry = OpenRetry(out)
    while True:
        now = time.time()
        retry.tick(now)
        if now >= t_stat:
            t_stat = now + a.stats_s
            try:
                with open(a.bridge_status, "rb") as f:
                    data.push(K_BRIDGE, f.read(4096).strip())
            except OSError:
                pass
            st = {
                "t": now,
                "device": out.dev,
                "audio_output": out.stream is not None,
                "device_error": out.err,
                **stats,
                "buffered_ms": 1000 * ring.frames // RATE,
                "underruns": ring.underruns,
                "slips": ring.slips,
                "slipped_frames": ring.slipped_frames,
                "missing_slot_blocks": asm.missing_slot_blocks,
                "holes": asm.holes,
                "playing": ring.started,
                "data_frames": data.frames,
                "data_dropped": data.dropped,
            }
            print(json.dumps(st), flush=True)
            if history:
                history.write(build_sink_sample(st, started_t))  # best effort: counts its own errors, never raises
            if a.status_file:
                with open(a.status_file, "w") as f:
                    json.dump(st, f)
        time.sleep(1.0)


if __name__ == "__main__":
    main()
