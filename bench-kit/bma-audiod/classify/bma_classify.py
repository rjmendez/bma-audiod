#!/usr/bin/env python3
"""BMA360 streaming acoustic classifier.

A real streaming classifier (not the heuristic stub in scripts/compact_event_classifier.py): it
maintains a bounded sliding window of mono audio, gates on activity, runs BirdNET on each window at
its native 48 kHz, and emits labelled detections with provenance. A detection is a MODEL OPINION,
never ground truth.

Sources (one of):
  --wav FILE        a WAV file (offline / proof)
  --stdin-raw       raw interleaved PCM on stdin (see --sr/--channels/--dtype)
  --udp HOST:PORT   bma-audiod datagrams (the board driver's output; format in _parse_datagram)

Design rules honoured:
  * bounded ring with explicit drop + loud log if a producer outruns the model (no silent backlog);
  * the activity gate logs every window it skips and why (shadows are loud), and the summary always
    states whether anything was actually classified, not just that the pipeline ran;
  * 48 kHz is the contract; any resample is explicit and logged.

Output: one JSON object per line on stdout (detections + periodic status); logs on stderr.
"""

from __future__ import annotations
import argparse, collections, contextlib, datetime, hashlib, json, logging, math, os, socket, struct, sys, time
import numpy as np

log = logging.getLogger("bma_classify")
SR_NATIVE = 48000
COUNTY_STEP = 0.25  # degrees (~ a county): the standing rule is that nothing finer is ever stored or used
COVERAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "birdnet_coverage.json")
WINDOW_S = 3.0  # BirdNET analyses 3 s segments
HOP_S = 1.0


class Ring:
    """Bounded mono float32 ring. Normal sliding eviction is NOT a drop; true drops (a producer
    outpacing the model) are tracked by run() via the unconsumed-backlog counter, not here."""

    def __init__(self, capacity_samples: int):
        self.cap = capacity_samples
        self.buf = collections.deque(maxlen=capacity_samples)
        self.dropped = 0  # true overrun only; set by run()
        self.total = 0

    def push(self, samples: np.ndarray):
        self.total += len(samples)
        self.buf.extend(samples.tolist())

    def __len__(self):
        return len(self.buf)

    def clear(self):
        """Start a new contiguous run (a hole in the stream): nothing before it may be windowed
        together with anything after it."""
        self.buf.clear()

    def window(self, n: int) -> np.ndarray:
        if len(self.buf) < n:
            return np.empty(0, dtype=np.float32)
        return np.fromiter(list(self.buf)[-n:], dtype=np.float32, count=n)


def dbfs(x: np.ndarray) -> float:
    if x.size == 0:
        return -999.0
    r = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    return 20.0 * np.log10(r + 1e-12)


def resample_to_48k(x: np.ndarray, sr: int) -> np.ndarray:
    if sr == SR_NATIVE:
        return x
    import librosa

    log.info("resampling %d Hz -> %d Hz (explicit, BirdNET contract)", sr, SR_NATIVE)
    return librosa.resample(x.astype(np.float32), orig_sr=sr, target_sr=SR_NATIVE)


def county_level(lat: float, lon: float) -> tuple[float, float]:
    """Snap a position to the CENTRE of its 0.25 degree cell (about a county). Idempotent. Every position
    that enters this module goes through here first, so a precise one is never held, logged or written."""
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise ValueError("lat/lon out of range")
    snap = lambda v, lim: max(-lim, min(lim, math.floor(v / COUNTY_STEP) * COUNTY_STEP + COUNTY_STEP / 2))
    return snap(lat, 90.0 - COUNTY_STEP / 2), snap(lon, 180.0 - COUNTY_STEP / 2)


def load_geo_config(path: str) -> dict:
    """{"lat", "lon"[, "week", "threshold"]} JSON -> county-level dict. The file should already hold the
    rounded value; it is rounded again here so a precise one pasted by mistake is still coarsened at once."""
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    lat, lon = county_level(float(cfg["lat"]), float(cfg["lon"]))
    return {"lat": lat, "lon": lon, "week": int(cfg.get("week", -1)), "threshold": cfg.get("threshold")}


def load_uncovered(path: str = COVERAGE_FILE):
    """Labels the BirdNET location model cannot judge (insects, frogs, mammals and rare tropical birds: its
    score is ~0.001 everywhere), from tools/make_birdnet_coverage.py. None when the file is missing: callers
    must then never DROP anything, only flag."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return frozenset(json.load(f)["uncovered"])
    except (OSError, ValueError, KeyError):
        return None


def is_non_species(scientific: str, common: str) -> bool:
    """BirdNET's non-bird sound classes (Engine, Dog, Siren, Human vocal, ...) have the same string as scientific
    and common name; they are real sound classes, not species, so a location list cannot apply to them."""
    return scientific == common


class BirdNet:
    """Lazy BirdNET wrapper so --help and source setup work without the heavy import.

    Optional location/date species filter (only when lat+lon are given, county level): BirdNET's own
    location model (documented birdnetlib `lat`, `lon`, `date`/`week_48`) says which species are plausible. With
    it on, every detection is returned with `non_species` (Engine, Dog, ...) and `in_region` (True/False, or None
    when the location model cannot judge that label). Detections with in_region False are NOT returned: they are
    kept in `self.filtered` (the last classify() call) so callers can log what was removed. Without lat/lon the
    behaviour and the output keys are exactly as before."""

    def __init__(self, min_conf: float, lat=None, lon=None, week=-1, filter_threshold=None, uncovered="auto"):
        self.min_conf = min_conf
        if lat is not None and lon is not None:
            lat, lon = county_level(lat, lon)
        self.lat, self.lon, self.week = lat, lon, week
        self.geo = lat is not None and lon is not None
        self.filter_threshold = filter_threshold
        self.uncovered = (load_uncovered() if uncovered == "auto" else uncovered) if self.geo else None
        self.filtered: list[dict] = []
        self._an = None
        self.version = None

    @classmethod
    def from_args(cls, args):
        path = getattr(args, "geo_config", None)
        if path:
            g = load_geo_config(path)
            log.info("BirdNET location filter on: county-level cell centre %.3f,%.3f", g["lat"], g["lon"])
            return cls(args.min_conf, g["lat"], g["lon"], g["week"], g["threshold"])
        return cls(args.min_conf, args.lat, args.lon, args.week)

    def _ensure(self):
        if self._an is None:
            from birdnetlib.analyzer import Analyzer
            import birdnetlib

            with contextlib.redirect_stdout(sys.stderr):  # keep birdnetlib chatter off our JSON stdout
                self._an = Analyzer()
            try:
                import importlib.metadata as _md

                self.version = _md.version("birdnetlib")
            except Exception:
                self.version = getattr(birdnetlib, "__version__", "?")
            log.info("BirdNET analyzer loaded (birdnetlib %s)", self.version)

    MODEL_FILE_ATTRS = ("classifier_model_path", "model_path")

    def model_info(self) -> dict:
        """{name, version, sha256} of the loaded BirdNET model, read once after the analyzer is loaded: version = the birdnetlib version
        (as the clip sidecars), sha256 = of the model file the analyzer loaded, None when the file cannot be found or read."""
        if getattr(self, "_info", None) is None:
            self._ensure()
            sha = None
            for a in self.MODEL_FILE_ATTRS:
                p = getattr(self._an, a, None)
                if isinstance(p, str) and os.path.isfile(p):
                    h = hashlib.sha256()
                    with open(p, "rb") as f:
                        for blk in iter(lambda: f.read(1 << 20), b""):
                            h.update(blk)
                    sha = h.hexdigest()
                    break
            self._info = {"name": "BirdNET", "version": self.version, "sha256": sha}
        return self._info

    def classify(self, audio48k: np.ndarray, when=None) -> list[dict]:
        """`when`: the window's date/datetime for the location filter's week (default today)."""
        self._ensure()
        from birdnetlib import RecordingBuffer

        kw = dict(min_conf=self.min_conf)
        if self.geo:
            # return_all_detections keeps every detection and adds is_predicted_for_location_and_date, so
            # non-species classes (never in the location list) are not silently dropped by the library
            kw.update(lat=self.lat, lon=self.lon, return_all_detections=True)
            if self.week and self.week > 0:
                kw["week_48"] = self.week
            else:
                kw["date"] = when or datetime.date.today()
            if self.filter_threshold is not None:
                kw["filter_threshold"] = float(self.filter_threshold)
        with contextlib.redirect_stdout(sys.stderr):
            rec = RecordingBuffer(self._an, audio48k.astype(np.float32), SR_NATIVE, **kw)
            rec.analyze()
        out, self.filtered = [], []
        for d in rec.detections:
            item = {
                "common": d["common_name"],
                "scientific": d["scientific_name"],
                "confidence": round(float(d["confidence"]), 4),
                "start_s": d["start_time"],
                "end_s": d["end_time"],
            }
            if self.geo:
                non = is_non_species(d["scientific_name"], d["common_name"])
                judged = (not non) and self.uncovered is not None and f"{d['scientific_name']}_{d['common_name']}" not in self.uncovered
                item["non_species"] = non
                item["in_region"] = bool(d["is_predicted_for_location_and_date"]) if judged else None
                if item["in_region"] is False:
                    self.filtered.append(item)
                    continue
            out.append(item)
        return out


def emit(obj: dict):
    obj["t_wall"] = round(time.time(), 3)
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def emit_filtered(net, level: float):
    """Log what the location filter removed from the last classify() (an audit trail, not a detection)."""
    for d in net.filtered:
        emit({"event": "filtered_out_of_region", "level_dbfs": round(level, 1), **d})


# ---- sources: each yields (source_sr, np.float32 mono chunk) ------------------------------------
def src_wav(path: str, chunk: int = 0):
    """`chunk` > 0 feeds the file in N-sample chunks (default ~1 s). A live stream arrives in small
    datagrams, and window positions depend on chunk boundaries, so a test that compares a file
    against a stream feeds the file in the stream's chunk size."""
    import soundfile as sf

    with sf.SoundFile(path) as f:
        sr = f.samplerate
        while True:
            block = f.read(chunk or sr, dtype="float32", always_2d=True)
            if len(block) == 0:
                break
            yield sr, block.mean(axis=1), False


def src_stdin_raw(sr: int, channels: int, dtype: str):
    npdt = {"int16": np.int16, "int24": None, "int32": np.int32, "float32": np.float32}[dtype]
    frame = channels * (3 if dtype == "int24" else np.dtype(npdt).itemsize)
    scale = {"int16": 1 / 32768, "int32": 1 / 2147483648, "int24": 1 / 8388608, "float32": 1.0}[dtype]
    buf = b""
    while True:
        chunk = sys.stdin.buffer.read(frame * sr)
        if not chunk:
            break
        buf += chunk
        n = (len(buf) // frame) * frame
        raw, buf = buf[:n], buf[n:]
        if dtype == "int24":
            a = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
            v = a[:, 0] | a[:, 1] << 8 | a[:, 2] << 16
            v = np.where(v & 0x800000, v - (1 << 24), v).astype(np.float32)
        else:
            v = np.frombuffer(raw, dtype=npdt).astype(np.float32)
        v = v.reshape(-1, channels).mean(axis=1) * scale
        yield sr, v, False


def _parse_datagram(pkt: bytes):
    """bma-audiod datagram (contracts/specs/bmaa.md): little-endian
    magic 'BMAA'(4) | ver u8 | dsp u8 | slot u8 | flags u8 | seq u32 | block u32 | n u32 |
    n x s24-in-s32.  `block` is the ABSOLUTE sample index of sample 0 on the card's stream clock
    (low 32 bits); flags bit0 = all-zero block, bit1 = a hole sits just before this block.
    Returns a dict, or None if it is not a well-formed BMAA datagram. Strict, as contracts/specs/bmaa.md requires:
    version must be 1, byte 5 (the `dsp` field, always 0 from the bridge and from the board daemon) must be 0,
    slot 0-7, length exactly 20 + 4n, and every sample inside the signed 24-bit range."""
    if len(pkt) < 20 or pkt[:4] != b"BMAA":
        return None
    ver, dsp, slot, flags = pkt[4], pkt[5], pkt[6], pkt[7]
    if ver != 1 or dsp != 0 or slot > 7:
        return None
    seq, block, nsamp = struct.unpack_from("<III", pkt, 8)
    if len(pkt) != 20 + 4 * nsamp:
        return None
    raw = np.frombuffer(pkt, dtype="<i4", count=nsamp, offset=20)
    if nsamp and (int(raw.min()) < -(1 << 23) or int(raw.max()) > (1 << 23) - 1):
        return None
    data = raw.astype(np.float32) / 8388608.0
    return {"dsp": dsp, "slot": slot, "flags": flags, "seq": seq, "block": block, "n": nsamp, "data": data}


class Segmenter:
    """Block-counter continuity for ONE slot. The board's NIC drops ~34% of an 8-channel stream, so
    holes are normal; audio on opposite sides of a hole must never be joined (a splice is a click,
    and BirdNET would classify the click). feed() says what each datagram is relative to the last:
      start    first datagram seen
      cont     exactly follows the previous one
      gap      samples are missing in between (a new contiguous run starts here)
      fill     gap of <= fill_gap_samples, zero-filled by the caller (opt-in, counted, never silent)
      reorder  block index went backwards (duplicate/restart): a new run starts here
    """

    MOD = 1 << 32

    def __init__(self, fill_gap_samples: int = 0):
        self.fill = fill_gap_samples
        self.expected = None
        self.segments = 0
        self.gaps = 0
        self.lost = 0
        self.reordered = 0
        self.filled = 0
        self.got = 0
        self.run = 0
        self.longest = 0

    def feed(self, block: int, n: int):
        lost = 0
        if self.expected is None:
            kind = "start"
            self.segments = 1
        else:
            d = (block - self.expected) % self.MOD
            if d == 0:
                kind = "cont"
            elif d < self.MOD // 2:
                lost = d
                kind = "fill" if d <= self.fill else "gap"
            else:
                kind = "reorder"
                lost = 0
        if kind == "fill":
            self.filled += lost
            self.run += lost
        elif kind in ("gap", "reorder"):
            self.longest = max(self.longest, self.run)
            self.run = 0
            self.segments += 1
            if kind == "gap":
                self.gaps += 1
                self.lost += lost
            else:
                self.reordered += 1
        self.expected = (block + n) % self.MOD
        self.run += n
        self.got += n
        self.longest = max(self.longest, self.run)
        return kind, lost

    def coverage(self) -> float:
        """Fraction of the stream's timeline that is REAL samples (zero-filled holes do not count)."""
        tot = self.got + self.lost + self.filled
        return self.got / tot if tot else 1.0


QUEUE_MAX = 20000  # ~100 s of one slot; bounded on purpose, overflow is counted and shows as a hole


def src_udp(hostport: str, sr: int, slot=None, fill_gap_samples: int = 0, info=None, idle_exit_s: float = 0.0, subscribe=None):
    """Yields (sr, chunk, brk). brk=True means a hole sits before this chunk: the consumer must
    start a new contiguous run. `slot` selects one channel position (None = first one seen);
    other slots are counted in info, never mixed in. With `subscribe` ("BOARD:PORT") this socket
    registers itself with the bma-audiod daemon and renews its lease, so it is a complete client.

    A reader thread owns the socket: it drains it continuously and renews the lease even while the
    model is busy, so a slow window cannot overflow the OS buffer (which Linux caps at rmem_max
    regardless of SO_RCVBUF) or let the daemon's 10 s lease lapse. The hand-off queue is bounded;
    overflow is counted (info["queue_overflow"]) and appears as a hole in the block counter."""
    import queue
    import threading

    info = info if info is not None else {}
    seg = Segmenter(fill_gap_samples)
    info["seg"] = seg
    info.update(ignored_other_slot=0, bad_datagrams=0, slot=slot, queue_overflow=0)
    host, port = hostport.rsplit(":", 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    want = 8 << 20
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, want)
    except OSError:
        pass
    s.bind((host, int(port)))
    got_buf = s.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    log.info("listening for bma-audiod datagrams on %s (SO_RCVBUF=%d)", hostport, got_buf)
    if got_buf < want // 4:
        log.warning("OS capped the receive buffer at %d bytes (net.core.rmem_max); the reader thread and queue absorb model stalls instead", got_buf)
    sub_dst = None
    if subscribe:
        h, p_ = subscribe.rsplit(":", 1)
        sub_dst = (h, int(p_))
    q = queue.Queue(maxsize=QUEUE_MAX)
    stop = threading.Event()
    sub_mask = [(1 << slot) if slot is not None else 0xFF]

    def reader():
        next_renew = 0.0
        s.settimeout(0.5)
        while not stop.is_set():
            now = time.time()
            if sub_dst and now >= next_renew:
                # register (and renew the 10 s lease) from THIS socket so the daemon replies to it
                s.sendto(b"BMAS\x01\x01" + bytes([sub_mask[0] & 0xFF, 0]), sub_dst)
                next_renew = now + 4.0
            try:
                pkt, _ = s.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                q.put_nowait((time.time(), pkt))
            except queue.Full:
                info["queue_overflow"] += 1

    threading.Thread(target=reader, daemon=True, name="udp-reader").start()
    last_rx = time.time()
    last_warn = 0.0
    try:
        while True:
            try:
                t_rx, pkt = q.get(timeout=0.5)
            except queue.Empty:
                if idle_exit_s > 0 and time.time() - last_rx > idle_exit_s:
                    log.info("no datagrams for %.1f s: ending the stream (--idle-exit-s)", idle_exit_s)
                    return
                continue
            last_rx = t_rx
            if pkt[:4] == b"BMAS":
                try:
                    info["daemon_status"] = json.loads(pkt[4:])
                except ValueError:
                    pass
                continue
            d = _parse_datagram(pkt)
            if d is None:
                info["bad_datagrams"] += 1
                log.warning("dropped malformed or non-BMAA datagram (%d bytes)", len(pkt))
                continue
            if slot is None:
                slot = d["slot"]
                info["slot"] = slot
                sub_mask[0] = 1 << slot
                log.info("using slot %d (first seen); other slots are ignored, not mixed", slot)
            if d["slot"] != slot:
                info["ignored_other_slot"] += 1
                continue
            if d["n"] == 0:
                continue
            kind, lost = seg.feed(d["block"], d["n"])
            if kind == "fill":
                yield sr, np.zeros(lost, dtype=np.float32), False
                yield sr, d["data"], False
                continue
            if kind in ("gap", "reorder"):
                if time.time() - last_warn > 10.0:
                    log.warning(
                        "HOLE in stream (%s, %d samples lost); %d gaps / %.2f s lost so far, coverage %.1f%%; windows never span a hole",
                        kind,
                        lost,
                        seg.gaps,
                        seg.lost / sr,
                        100 * seg.coverage(),
                    )
                    last_warn = time.time()
                yield sr, d["data"], True
                continue
            yield sr, d["data"], False
    finally:
        stop.set()
        if sub_dst:
            with contextlib.suppress(OSError):
                s.sendto(b"BMAS\x01\x02\x00\x00", sub_dst)


def run(args):
    info = {}
    fill = int(getattr(args, "fill_gap_ms", 0) * args.sr / 1000)
    gen = (
        src_wav(args.wav, getattr(args, "wav_chunk", 0))
        if args.wav
        else src_udp(args.udp, args.sr, getattr(args, "slot", None), fill, info, getattr(args, "idle_exit_s", 0.0), getattr(args, "subscribe", None))
        if args.udp
        else src_stdin_raw(args.sr, args.channels, args.dtype)
    )
    net = BirdNet.from_args(args)
    # Load the model before the stream is opened: a lazy load on the first window stalls the
    # consumer for seconds while a live source keeps sending.
    getattr(net, "_ensure", lambda: None)()
    ring = None
    win_n = hop_n = src_sr = None
    since_hop = 0
    primed = False
    windows = classified = skipped_silent = detections = 0
    t0 = time.time()
    for sr, chunk, brk in gen:
        if ring is None:
            src_sr = sr
            win_n, hop_n = int(WINDOW_S * sr), int(HOP_S * sr)
            ring = Ring(int(args.ring_s * sr))
            log.info("source %d Hz; window %.1fs hop %.1fs ring %.1fs", sr, WINDOW_S, HOP_S, args.ring_s)
        if brk:
            # a hole precedes this chunk: nothing already buffered may be windowed with it
            ring.clear()
            since_hop = 0
            primed = False
        ring.push(chunk)
        if len(ring) < win_n:
            # not enough contiguous audio for a window yet: do not bank hops, or the first full
            # window would fire several times on identical audio
            since_hop = 0
            primed = False
        elif not primed:
            since_hop = hop_n  # the first full window fires now, then one per hop
            primed = True
        else:
            since_hop += len(chunk)
        # true overrun: unconsumed backlog exceeded the ring, so audio was evicted before a window
        # could cover it (only happens in live --udp/--stdin when the model can't keep up).
        if since_hop > ring.cap:
            lost = since_hop - ring.cap
            ring.dropped += lost
            since_hop = ring.cap
            log.warning("OVERRUN: model behind, dropped %d samples (%.2f s); total dropped %d", lost, lost / src_sr, ring.dropped)
        while since_hop >= hop_n and len(ring) >= win_n:
            since_hop -= hop_n
            windows += 1
            w = ring.window(win_n)
            level = dbfs(w)
            if level < args.gate_dbfs:
                skipped_silent += 1
                log.info("window %d skipped: %.1f dBFS < gate %.1f (silence)", windows, level, args.gate_dbfs)
                continue
            w48 = resample_to_48k(w, src_sr)
            dets = net.classify(w48)
            classified += 1
            emit_filtered(net, level)
            if dets:
                detections += len(dets)
                for d in dets:
                    d.update(level_dbfs=round(level, 1), model="BirdNET", model_version=net.version, provenance="model_opinion")
                    emit({"event": "detection", **d})
            else:
                emit({"event": "no_detection", "level_dbfs": round(level, 1), "min_conf": args.min_conf})
    # flush: a stream shorter than one window still gets one classification (zero-padded to 3 s).
    # Not after holes: the tail left behind by a hole is a fragment, not a short clip.
    seg = info.get("seg")
    holes = bool(seg and (seg.gaps or seg.reordered))
    if windows == 0 and ring is not None and len(ring) > 0 and not holes:
        have = ring.window(len(ring)) if len(ring) >= 1 else np.empty(0, np.float32)
        have = np.fromiter(list(ring.buf), dtype=np.float32, count=len(ring))
        level = dbfs(have)
        if level >= args.gate_dbfs:
            w = np.zeros(win_n, dtype=np.float32)
            w[: len(have)] = have[:win_n]
            windows += 1
            classified += 1
            dets = net.classify(resample_to_48k(w, src_sr))
            emit_filtered(net, level)
            if dets:
                detections += len(dets)
                for d in dets:
                    d.update(level_dbfs=round(level, 1), model="BirdNET", model_version=net.version, provenance="model_opinion", note="short_clip_zero_padded")
                    emit({"event": "detection", **d})
            else:
                emit({"event": "no_detection", "level_dbfs": round(level, 1), "min_conf": args.min_conf, "note": "short_clip_zero_padded"})
        else:
            skipped_silent += 1
            log.info("short clip skipped: %.1f dBFS < gate %.1f", level, args.gate_dbfs)
    verdict = "classified_audio" if classified else "no_audio_classified"
    summary = {
        "event": "summary",
        "windows": windows,
        "classified": classified,
        "skipped_silent": skipped_silent,
        "detections": detections,
        "ring_dropped_samples": (ring.dropped if ring else 0),
        "elapsed_s": round(time.time() - t0, 2),
    }
    if seg is not None:
        sr_ = src_sr or args.sr
        summary["stream"] = {
            "slot": info.get("slot"),
            "samples_received": seg.got,
            "gaps": seg.gaps,
            "lost_samples": seg.lost,
            "lost_s": round(seg.lost / sr_, 3),
            "reordered": seg.reordered,
            "filled_samples": seg.filled,
            "segments": seg.segments,
            "longest_contiguous_s": round(seg.longest / sr_, 3),
            "coverage": round(seg.coverage(), 4),
            "ignored_other_slot_datagrams": info.get("ignored_other_slot", 0),
            "bad_datagrams": info.get("bad_datagrams", 0),
            "queue_overflow_datagrams": info.get("queue_overflow", 0),
        }
        if info.get("daemon_status") is not None:
            summary["daemon_status"] = info["daemon_status"]
        if not classified and win_n and seg.longest < win_n:
            # say WHY nothing was classified instead of reporting a quiet scene
            verdict = "no_contiguous_window"
            summary["reason"] = "the stream never held %.1f s without a hole (longest %.3f s); windows are never built across holes" % (
                WINDOW_S,
                seg.longest / sr_,
            )
    summary["verdict"] = verdict
    emit(summary)
    log.info(
        "done: %d windows, %d classified, %d skipped silent, %d detections, %d dropped samples",
        windows,
        classified,
        skipped_silent,
        detections,
        ring.dropped if ring else 0,
    )


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--wav")
    g.add_argument("--stdin-raw", action="store_true", dest="stdin_raw")
    g.add_argument("--udp", metavar="HOST:PORT")
    p.add_argument("--sr", type=int, default=48000, help="source rate for --stdin-raw/--udp")
    p.add_argument("--channels", type=int, default=1)
    p.add_argument(
        "--slot",
        type=int,
        default=None,
        help="--udp: channel position to classify (default: the first slot seen); other slots are ignored and counted, never mixed in",
    )
    p.add_argument(
        "--fill-gap-ms",
        type=float,
        default=0.0,
        help="--udp: zero-fill holes up to this long instead of starting a new run. Default 0 = never bridge a hole; any fill is counted in the summary",
    )
    p.add_argument(
        "--wav-chunk",
        type=int,
        default=0,
        metavar="N",
        help="--wav: feed the file in N-sample chunks instead of ~1 s (window positions depend on chunk boundaries; used to compare a file against a stream)",
    )
    p.add_argument(
        "--subscribe",
        metavar="BOARD:PORT",
        default=None,
        help="--udp: register with the bma-audiod daemon at BOARD:PORT from the listening socket and renew the lease (the daemon only sends to subscribers)",
    )
    p.add_argument(
        "--idle-exit-s",
        type=float,
        default=0.0,
        help="--udp: end the stream (and print the summary) after this many seconds without a datagram. Default 0 = run until killed",
    )
    p.add_argument("--dtype", default="int24", choices=["int16", "int24", "int32", "float32"])
    p.add_argument("--min-conf", type=float, default=0.25)
    p.add_argument("--gate-dbfs", type=float, default=-70.0, help="skip windows quieter than this")
    p.add_argument("--ring-s", type=float, default=10.0)
    p.add_argument("--lat", type=float, help="location filter (rounded to a 0.25 degree county-level cell centre at once)")
    p.add_argument("--lon", type=float)
    p.add_argument("--week", type=int, default=-1, help="BirdNET week 1-48; default -1 = the window's date")
    p.add_argument(
        "--geo-config",
        default=None,
        metavar="JSON",
        help='location filter from a JSON file {"lat":..,"lon":..} (county level; default: off). Absent = no location filter, exactly the old behaviour',
    )
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    run(args)


if __name__ == "__main__":
    main()
