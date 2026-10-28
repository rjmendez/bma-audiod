#!/usr/bin/env python3
"""bma_ui - read-only web dashboard for the BMA pipeline: detections and beam statistics.

    python bma_ui.py [--bind 127.0.0.1] [--port 48500] [--allow-host NAME ...] [--no-board]

Python 3 standard library only. GET and HEAD only; the only files it serves are the embedded static assets that sit in
ui/static/ (and, with --ui-next, the ui-next shell under /next/), loaded once at start, matched by exact name; the only network it touches is HTTP GET to the dspd service
(token read from a file, held in memory, never sent to a client or written to a log). It reads the pipeline's JSONL
files incrementally (per-file offsets, rotation and truncation handled, bounded memory) and persists hourly rollups
under <data-dir>/reports/ui so history survives restarts and log rotation. Contract: contracts/specs/ui-http.md."""
import argparse, base64, bisect, collections, datetime, ipaddress, json, math, os, re, socket, stat, statistics, struct, sys, threading, time
import urllib.error, urllib.parse, urllib.request
from array import array
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))                # python -I does not put the script's directory on the path
import auth as A                                                         # the login (ui/auth.py, design in ui/AUTH.md)
try:
    import passkeys as PK                                               # WebAuthn (ui/passkeys.py, stdlib only); absent = the feature is off, nothing else changes
except ImportError:                                                     # pragma: no cover - exercised by test_passkeys.py with the import blocked
    PK = None
try:
    import review as RV                                                 # label review (ui/review.py + analysis/labels.py, review_queue.py); absent = no Review routes
except ImportError:                                                     # pragma: no cover
    RV = None
try:
    import ws as WS                                                     # the live spectrum WebSocket (ui/ws.py, stdlib only); absent = --ui-next refuses to start
except ImportError:                                                     # pragma: no cover
    WS = None
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "analysis"))   # repo layout; the install copies labels.py next to this file
try:
    import labels as L                                                  # the one speech/voice label guard (analysis/labels.py, pinned to the tagger's SPEECH_LABELS by test_labels.py)
except ImportError:                                                     # pragma: no cover - then no tagger label list is served at all
    L = None
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _load_artifact_gate():
    """analysis/artifact_gate.py (stdlib only, pure). Pinned layout: next to this file (the installers copy it there); repo layout: ../analysis.
    Absent = the dashboard runs exactly as before without the advisory flags (status.artifact_gate.available is false)."""
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, "artifact_gate.py"), os.path.join(here, "..", "analysis", "artifact_gate.py")):
        if os.path.isfile(path):
            try:
                spec = importlib.util.spec_from_file_location("artifact_gate", path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod
            except Exception:                                            # noqa: BLE001 - a broken gate must never stop the dashboard
                return None
    return None


AG = _load_artifact_gate()

UI_VERSION = "1.13.0"
HERE = os.path.dirname(os.path.abspath(__file__))
EDGES_HZ = [[50, 125], [125, 250], [250, 500], [500, 1000], [1000, 2000], [2000, 4000], [4000, 8000], [8000, 16000]]
BEAM_AZ_ARRAY_DEG = [90, 45, 0, 315, 270, 225, 180, 135]       # fallback only: audio beam k -> entry k-1, DSP azimuth (the pattern dspd applied is in /api/status board.pattern_ref)
PANEL_UP_DSP_AZ_DEG = 15                                         # DSP azimuth that points at the panel's up (+-15), provisional; panel azimuth = DSP azimuth - 15
FRAME_KIND_PANEL, FRAME_KIND_ARRAY, FRAME_KIND_COMPASS = "panel", "array", "compass"
SLOT_FIX_UTC = (2026, 10, 25, 21, 47, 0)                         # absolute instant (17:47 EDT on the capture host): detections before it used the wrong beam-slot mapping (45 deg off)


def default_slot_fix_s():
    return datetime.datetime(*SLOT_FIX_UTC, tzinfo=datetime.timezone.utc).timestamp()
FRAME_UNCAL = "array frame (uncalibrated)"
CONTRAST_BANDS = (5, 6)                                          # 2-4 kHz and 4-8 kHz: the 2-8 kHz leader / bias view
MIN_CONTRAST_DB = 3.0
EVENT_GAP_S = 3.0
HOUR_S = 3600
ROLLUP_GRACE_S = 120
MAX_RANGE_S = 90 * 86400
DSP_BACKFILL_BYTES = 48 << 20                                    # transients for the artifact gate: about two days of dsp.jsonl (live file, then the rotated one)
FLOOR_DB = -200.0
DEAD_BEAM_P = 1e-16                                              # two-band 2-8 kHz power below this (-160 dB) = a dead channel
FALLBACK_SLACK_S = 600                                           # a repeated local hour is accepted when it resumes this close after the last stamp
MAX_CONNECTIONS = 64
CONN_TIMEOUT_S = 10.0
P_FLOOR = 1e-20
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/static/app.css": ("app.css", "text/css; charset=utf-8"),
          "/static/tip.js": ("tip.js", "text/javascript; charset=utf-8"), "/static/spectrum.js": ("spectrum.js", "text/javascript; charset=utf-8"),
          "/static/session.js": ("session.js", "text/javascript; charset=utf-8"),
          "/static/review.js": ("review.js", "text/javascript; charset=utf-8"), "/static/review.css": ("review.css", "text/css; charset=utf-8"),
          "/static/login.js": ("login.js", "text/javascript; charset=utf-8"), "/static/login.css": ("login.css", "text/css; charset=utf-8")}
PUBLIC_ASSETS = frozenset({"/static/login.js", "/static/login.css"})   # the sign-in page's own files: served without a login (no data in them)
LOGIN_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
             "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
MAX_FORM_BYTES = 4096
MAX_JSON_BYTES = 16384
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
# --ui-next (ui-http 1.12.0): the next dashboard shell (ui-next/static), served only under /next/ and only when the flag is set. Exact names, loaded once at start.
NEXT_FILES = {"index.html": "text/html; charset=utf-8", "app.css": "text/css; charset=utf-8", "app.js": "text/javascript; charset=utf-8", "review.js": "text/javascript; charset=utf-8",
              "adapter.js": "text/javascript; charset=utf-8", "engine/audition.js": "text/javascript; charset=utf-8", "engine/colormaps.js": "text/javascript; charset=utf-8",
              "engine/dsp.js": "text/javascript; charset=utf-8", "engine/render.js": "text/javascript; charset=utf-8", "engine/worklets.js": "text/javascript; charset=utf-8"}
NEXT_INDEX = frozenset({"/next/", "/next/index.html"})            # the shell reads its own page options (?r=, ?clip=) from the query; every other /next/ path refuses a query like /static/*
NEXT_CSP = CSP.replace("img-src 'self' data:;", "img-src 'self' data:; media-src 'self' blob:;")   # Keep-pitch playback is an <audio> over a blob: URL made in the page; nothing else differs from CSP


def db_to_p(db):
    return 10.0 ** (db / 10.0)


def p_to_db(p):
    return FLOOR_DB if p <= P_FLOOR else max(FLOOR_DB, 10.0 * math.log10(p))


def r2(x):
    return None if x is None else round(x, 2) + 0.0


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def hour_of(t):
    return int(t // HOUR_S) * HOUR_S


def ceil_hour(t):
    return int(-(-t // HOUR_S)) * HOUR_S


def iso_z(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean(o):
    """Replace non-finite floats by None (JSON has no NaN)."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    return o


# ----------------------------------------------------------------------------------------------------- tailing
class Tailer:
    """Incremental reader of one append-only file: offset + identity, partial last line kept until its newline arrives,
    garbage lines counted, rotation (identity change; the old file is finished from `<path>.1` when that is where it went)
    and truncation (size below offset) restart from 0. Never reads a whole file into memory."""

    CHUNK = 1 << 20

    def __init__(self, path, handler, parse=None, name=None):
        self.path, self.handler, self.parse, self.name = path, handler, parse or json.loads, name or os.path.basename(path)
        self.key, self.offset, self.buf = None, 0, b""
        self.lines = self.garbage = self.rejected = self.skipped = self.resets = 0
        self.last_read_s = None
        self.last_error = None
        self.lock = threading.Lock()

    @staticmethod
    def _key(st):
        return (st.st_ino, st.st_dev)

    def _line(self, ln):
        ln = ln.strip()
        if not ln:
            return
        self.lines += 1
        try:
            obj = self.parse(ln)
        except (ValueError, UnicodeDecodeError):
            self.garbage += 1
            return
        if obj is None:
            self.skipped += 1
            return
        try:
            ok = self.handler(obj)
        except Exception:                                                # noqa: BLE001 - a bad record is never fatal
            ok = False
        if ok is False:
            self.rejected += 1

    def _feed(self, chunk):
        parts = (self.buf + chunk).split(b"\n")
        self.buf = parts.pop()
        for ln in parts:
            self._line(ln)
        if len(self.buf) > (4 << 20):                                    # a "line" this long is not a record
            self.buf = b""
            self.garbage += 1

    def _read_file(self, path, start, limit):
        """Feed up to `limit` bytes of `path` from `start`; return the offset reached."""
        done = 0
        with open(path, "rb") as f:
            f.seek(start)
            while done < limit:
                chunk = f.read(min(self.CHUNK, limit - done))
                if not chunk:
                    break
                done += len(chunk)
                self._feed(chunk)
            return start + done

    def backfill(self, max_bytes, with_rotated=False):
        """Start of life: read at most the last `max_bytes` of the file (from the first full line), optionally the tail of
        `<path>.1` first when the live file is smaller than that. Leaves the offset at the end of what was read."""
        with self.lock:
            try:
                st = os.stat(self.path)
            except OSError:
                return
            if not stat.S_ISREG(st.st_mode):
                self.last_error = "not_a_regular_file"
                return
            try:
                self._backfill_locked(st, max_bytes, with_rotated)
                self.last_error = None
            except OSError as ex:                                        # e.g. a sharing violation: resume at the end, never from 0
                self.last_error = type(ex).__name__
                self.key, self.offset, self.buf = self._key(st), st.st_size, b""

    def _backfill_locked(self, st, max_bytes, with_rotated):
        if True:
            if with_rotated and st.st_size < max_bytes:
                old = self.path + ".1"
                try:
                    so = os.stat(old)
                    budget = max_bytes - st.st_size
                    start = max(0, so.st_size - budget)
                    saved = (self.buf,)
                    self.buf = b""
                    if start:
                        with open(old, "rb") as f:
                            f.seek(start)
                            f.readline()
                            start = f.tell()
                    self._read_file(old, start, so.st_size)
                    self.buf = saved[0]
                except OSError:
                    pass
            self.key = self._key(st)
            start = max(0, st.st_size - max_bytes)
            self.buf = b""
            if start:
                with open(self.path, "rb") as f:
                    f.seek(start)
                    f.readline()
                    start = f.tell()
            self.offset = self._read_file(self.path, start, max(0, st.st_size - start) if st.st_size >= start else 0)
            while True:                                                  # the file kept growing while we read it
                try:
                    size = os.stat(self.path).st_size
                except OSError:
                    break
                if size <= self.offset:
                    break
                self.offset = self._read_file(self.path, self.offset, size - self.offset)
            self.last_read_s = time.time()

    def poll(self, max_bytes=32 << 20):
        with self.lock:
            try:
                st = os.stat(self.path)
            except OSError:
                return False
            if not stat.S_ISREG(st.st_mode):
                self.last_error = "not_a_regular_file"
                return False
            key = self._key(st)
            if self.key is None:
                self.key, self.offset, self.buf = key, 0, b""
            elif key != self.key:                                        # rotated (or replaced)
                old = self.path + ".1"
                try:
                    so = os.stat(old)
                    if self._key(so) == self.key and so.st_size > self.offset:
                        self._read_file(old, self.offset, so.st_size - self.offset)
                except OSError:
                    pass
                self.resets += 1
                self.key, self.offset, self.buf = key, 0, b""
            elif st.st_size < self.offset:                               # truncated
                self.resets += 1
                self.offset, self.buf = 0, b""
            if st.st_size > self.offset:
                try:
                    self.offset = self._read_file(self.path, self.offset, min(max_bytes, st.st_size - self.offset))
                except OSError as ex:                                    # retried on the next poll from the same offset
                    self.last_error = type(ex).__name__
                    return False
                self.last_read_s = time.time()
            self.last_error = None
            return True

    def counters(self):
        return {"lines": self.lines, "garbage_lines": self.garbage, "rejected_records": self.rejected, "resets": self.resets,
                "last_error": self.last_error}


def resolve_fallback(t, last_t):
    """Stamp of a zone-less local wall-clock line. When the local clock falls back (DST end) the repeated hour parses to
    stamps one hour BEFORE the lines already seen; a stamp that is not after the previous one but would be a moment after
    it when shifted by one hour is the second pass of that hour. Anything else (a real duplicate or an old line) is kept
    as parsed and left to the stream store to drop."""
    if last_t is not None and t <= last_t and 0 < t + HOUR_S - last_t <= FALLBACK_SLACK_S:
        return t + HOUR_S
    return t


class BridgeLogParser:
    """Tailer parse hook for bridge.log stats lines (stateful: it remembers the previous stamp). `mktime` converts the
    local wall clock to epoch seconds."""
    RE = re.compile(rb"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) stats: (\S+) pkt/s loss ([\d.]+)% gaps=(\d+) .* state=(\w+)")

    def __init__(self, mktime=time.mktime):
        self.mktime, self.last_t = mktime, None

    def __call__(self, ln):
        m = self.RE.match(ln)
        if not m:
            return None
        t = resolve_fallback(self.mktime(time.strptime(m.group(1).decode(), "%Y-%m-%d %H:%M:%S")), self.last_t)
        self.last_t = t
        return {"t": t, "pps": float(m.group(2)), "loss_pct": float(m.group(3)), "gaps": int(m.group(4)), "state": m.group(5).decode()}


# ------------------------------------------------------------------------------------------------- aggregation
class BeamAgg:
    """One hour of beam_bands windows. psum is the sum of linear mean-square power, so the power-mean dB of any set of
    hours is recoverable exactly."""
    __slots__ = ("n", "zf", "psum", "maxdb", "nc", "lead", "rel")

    def __init__(self):
        self.n = 0
        self.zf = 0.0
        self.psum = [[0.0] * 8 for _ in range(8)]
        self.maxdb = [[FLOOR_DB] * 8 for _ in range(8)]
        self.nc = 0
        self.lead = [0] * 8
        self.rel = [0.0] * 8

    def add(self, db, zf):
        self.n += 1
        self.zf += zf
        ps, mx = self.psum, self.maxdb
        pw = []
        for b in range(8):
            row = db[b]
            for k in range(8):
                v = row[k]
                ps[b][k] += 10.0 ** (v / 10.0)
                if v > mx[b][k]:
                    mx[b][k] = v
        for b in range(8):
            pw.append(10.0 ** (db[b][CONTRAST_BANDS[0]] / 10.0) + 10.0 ** (db[b][CONTRAST_BANDS[1]] / 10.0))
        ring = sum(pw) / 8.0
        if ring < 1e-15 or min(pw) < DEAD_BEAM_P:                        # silence, or a dead channel: no leader, no bias sample
            return
        lv = [p_to_db(p) for p in pw]
        if max(lv) - min(lv) < MIN_CONTRAST_DB:
            return
        self.nc += 1
        self.lead[lv.index(max(lv))] += 1
        rd = p_to_db(ring)
        for b in range(8):
            self.rel[b] += lv[b] - rd

    def norm(self, hour, rounded=False):
        n, nc = self.n, self.nc
        rd = (lambda x: round(x, 2)) if rounded else (lambda x: x)
        return {"hour": hour, "n": n, "zf_mean": (self.zf / n if n else 0.0),
                "power_db": [[rd(p_to_db(self.psum[b][k] / n)) for k in range(8)] for b in range(8)],
                "max_db": [[rd(self.maxdb[b][k]) for k in range(8)] for b in range(8)],
                "nc": nc, "lead": list(self.lead), "rel_mean": [rd(self.rel[b] / nc) for b in range(8)] if nc else None}


def norm_from_rollup(rec):
    c = rec.get("contrast") or {}
    return {"hour": rec["hour_s"], "n": rec["windows"], "zf_mean": rec["zero_frac_mean"], "power_db": rec["power_mean_db"],
            "max_db": rec["max_db"], "nc": c.get("windows", 0), "lead": c.get("leader_count") or [0] * 8,
            "rel_mean": c.get("mean_rel_ring_db_2_8khz")}


def _int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def _grid(g):
    return isinstance(g, list) and len(g) == 8 and all(isinstance(r, list) and len(r) == 8 and all(is_num(v) for v in r) for r in g)


def valid_rollup(r):
    """Shape check of one persisted rollup record (header, hour and the payload of its kind); anything else is garbage."""
    if not isinstance(r, dict) or r.get("k") != "ui_rollup" or r.get("v") != 1:
        return False
    h = r.get("hour_s")
    if not _int(h) or h <= 0 or h % HOUR_S:
        return False
    kind = r.get("kind")
    if kind == "beams":
        c = r.get("contrast")
        if not (_int(r.get("windows")) and r["windows"] > 0 and is_num(r.get("zero_frac_mean")) and _grid(r.get("power_mean_db"))
                and _grid(r.get("max_db")) and isinstance(c, dict) and _int(c.get("windows")) and c["windows"] >= 0):
            return False
        lc, mr = c.get("leader_count"), c.get("mean_rel_ring_db_2_8khz")
        return (isinstance(lc, list) and len(lc) == 8 and all(_int(x) for x in lc)
                and (mr is None or (isinstance(mr, list) and len(mr) == 8 and all(is_num(x) for x in mr))))
    if kind == "detections":
        sp = r.get("species")
        return isinstance(sp, list) and all(
            isinstance(x, dict) and isinstance(x.get("common"), str) and x["common"] != "" and _int(x.get("events")) and _int(x.get("reports"))
            and is_num(x.get("max_confidence")) and isinstance(x.get("beam_events"), list) and len(x["beam_events"]) == 8
            and all(_int(b) for b in x["beam_events"]) and (x.get("scientific") is None or isinstance(x.get("scientific"), str)) for x in sp)
    if kind == "stream":
        return (_int(r.get("samples")) and r["samples"] > 0 and all(is_num(r.get(k)) for k in ("loss_pct_mean", "loss_pct_min", "loss_pct_max"))
                and (r.get("gaps_delta") is None or _int(r.get("gaps_delta"))) and isinstance(r.get("state_counts"), dict))
    return False


def valid_bands(o):
    if not isinstance(o, dict) or o.get("k") != "beam_bands" or o.get("v") != 1:
        return False
    t = o.get("t_us")
    if not isinstance(t, int) or isinstance(t, bool) or t <= 0 or not is_num(o.get("zero_frac")) or not 0 <= o["zero_frac"] <= 1:
        return False
    db = o.get("db")
    if not isinstance(db, list) or len(db) != 8:
        return False
    for row in db:
        if not isinstance(row, list) or len(row) != 8:
            return False
        for v in row:
            if not is_num(v) or v < FLOOR_DB or v > 0:
                return False
    return True


class BeamStore:
    def __init__(self, window_s):
        self.window_s = window_s
        self.recs = collections.deque()                                  # (t_s, zero_frac, array('f') of db[beam*8+band])
        self.aggs = {}
        self.lock = threading.Lock()
        self.cutoff_s = 0.0
        self.agg_floor_s = 0.0
        self.newest_t = None
        self.rolled = set()
        self.set_cutoff(0.0)

    def set_cutoff(self, cutoff):
        self.cutoff_s = cutoff
        self.agg_floor_s = ceil_hour(cutoff) if cutoff > 0 else 0.0

    def add(self, o):
        if not valid_bands(o):
            return False
        t = o["t_us"] / 1e6
        if t < self.cutoff_s:
            return True
        flat = array("f", [v for row in o["db"] for v in row])
        with self.lock:
            self.recs.append((t, o["zero_frac"], flat))
            if self.newest_t is None or t > self.newest_t:
                self.newest_t = t
            if t >= self.agg_floor_s:
                h = hour_of(t)
                a = self.aggs.get(h)
                if a is None:
                    a = self.aggs[h] = BeamAgg()
                a.add(o["db"], o["zero_frac"])
        return True

    def housekeeping(self, now):
        cutoff = now - self.window_s
        with self.lock:
            self.set_cutoff(cutoff)
            while self.recs and self.recs[0][0] < cutoff:
                self.recs.popleft()
            for h in [h for h in self.aggs if h < self.agg_floor_s]:
                del self.aggs[h]

    def closed_rollups(self, now, have):
        """Rollup records for closed hours that are not persisted yet."""
        out = []
        with self.lock:
            for h in sorted(self.aggs):
                if h + HOUR_S + ROLLUP_GRACE_S > now or ("beams", h) in have or h < self.agg_floor_s:
                    continue
                a = self.aggs[h]
                if a.n == 0:
                    continue
                nm = a.norm(h, rounded=True)
                out.append({"k": "ui_rollup", "v": 1, "kind": "beams", "hour_s": h, "windows": nm["n"],
                            "zero_frac_mean": round(nm["zf_mean"], 4), "power_mean_db": nm["power_db"], "max_db": nm["max_db"],
                            "contrast": {"windows": nm["nc"], "leader_count": nm["lead"], "mean_rel_ring_db_2_8khz": nm["rel_mean"]}})
        return out

    def hourly(self, since, until, rollups):
        """{hour: norm row} for hours overlapping [since, until): in-memory when held, else the persisted rollup."""
        out = {}
        h0, h1 = hour_of(since), until
        with self.lock:
            for h, a in self.aggs.items():
                if h0 <= h < h1 and a.n:
                    out[h] = a.norm(h)
        for (kind, h), rec in rollups.items():
            if kind == "beams" and h0 <= h < h1 and h not in out:
                out[h] = norm_from_rollup(rec)
        return dict(sorted(out.items()))

    def window_recs(self, since, until):
        with self.lock:
            return [r for r in self.recs if since <= r[0] < until]

    def last_recs(self, ref, span):
        out = []
        with self.lock:
            for r in reversed(self.recs):
                if r[0] < ref - span:
                    break
                out.append(r)
        out.reverse()
        return out

    def size(self):
        return len(self.recs)


GROUPS_BIO = ("bird", "insect", "amphibian", "mammal", "other-bio")
GROUP_ORDER = GROUPS_BIO + ("mechanical",)
GROUP_MODES = GROUP_ORDER + ("bio", "any")
ARTIFACT_MODES = ("any", "none", "only")
CHORUS_MIN_PER_ACTIVE_HOUR = 60              # events per active hour at or above which a species is flagged a chorus (real 24 h: katydid 90.3, the next species 30.7)
CHORUS_MIN_ACTIVE_HOURS = 3                  # ... and it was active in at least this many distinct hours
UNCLASSIFIED_LIST_MAX = 20
TAXA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "taxa.json")
NOT_STEERABLE_BANDS = tuple(i for i, (_lo, hi) in enumerate(EDGES_HZ) if hi <= 250)   # 50-125 and 125-250 Hz: the array cannot steer below 250 Hz (identical on all beams)


class Taxa:
    """Taxonomic group of a detection label. Resolution order: explicit whole labels (Dog, Engine ...), then the genus table
    (first word of the scientific name), else ('other-bio', 'unmapped'). A file that is missing or malformed loads as an EMPTY table
    with `error` set (shown in status.taxa): every label is then 'unmapped' and counted, nothing is dropped and nothing crashes."""

    def __init__(self, path=None):
        self.explicit, self.genus, self.error = {}, {}, None
        if path is None:
            return
        try:
            with open(path, "rb") as f:
                d = json.loads(f.read())
            if not isinstance(d, dict) or d.get("version") != 1 or not isinstance(d.get("genera"), dict) or not isinstance(d.get("explicit_labels"), dict):
                raise ValueError("unexpected structure")
            genus, explicit = {}, {}
            for grp, names in d["genera"].items():
                if grp not in GROUP_ORDER or not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
                    raise ValueError("bad genera group")
                for n in names:
                    genus[n.lower()] = grp
            for lab, e in d["explicit_labels"].items():
                if not isinstance(e, dict) or e.get("group") not in GROUP_ORDER or not isinstance(e.get("non_species"), bool):
                    raise ValueError("bad explicit label")
                explicit[lab.lower()] = (e["group"], e["non_species"])
            self.explicit, self.genus = explicit, genus
        except (OSError, ValueError, TypeError) as ex:
            self.error = "taxa.json unusable (%s): every label is filed under other-bio" % type(ex).__name__

    def classify(self, scientific, common, flag):
        """-> (group, how, non_species). A mapped label's class decides non_species (so an old record with no flag, or a cricket the
        producer flagged because scientific == common, is classed correctly); an unmapped label keeps the producer's flag."""
        for lab in (common, scientific):
            hit = self.explicit.get((lab or "").strip().lower())
            if hit:
                return hit[0], "explicit", hit[1]
        parts = (scientific or "").split()
        if len(parts) >= 2 and parts[0].lower() in self.genus:
            return self.genus[parts[0].lower()], "genus", False
        return "other-bio", "unmapped", bool(flag)


def load_taxa(path=TAXA_PATH):
    return Taxa(path)


# Human voices are hidden from EVERY view (owner decision, ui-http 1.6.0): events carrying one of these labels (case-insensitive,
# matched on the common or the scientific label) are dropped when they are read, before any store, list, count, chart or rollup
# can see them, whatever non_species / group says. This is the ONE list; ui/taxa.json and make_taxa.py name the same three labels
# as mechanical, and test_bma_ui.py asserts the three agree. The only trace is `status.human_hidden_24h`, a count (no label, no time).
HUMAN_LABELS = frozenset(("human vocal", "human non-vocal", "human whistle"))


def is_human_label(common, scientific=""):
    return any((lab or "").strip().lower() in HUMAN_LABELS for lab in (common, scientific))


def visible_top(top):
    """A tagger / CLAP `top` list [[label, p], ...] without human-voice labels (L.is_speech_label); None when it is not a list or the guard is absent."""
    if not isinstance(top, list) or L is None:
        return None
    return [x for x in top if isinstance(x, (list, tuple)) and x and isinstance(x[0], str) and not L.is_speech_label(x[0])]


MAX_TRACE = 500                                                          # reports kept per event for the detail trace (an event is a few reports; this bounds a pathological one)
MAX_REJECTS = 64                                                         # concurrent 503 drains (each lives <= 1 s); beyond it a refused connection is closed at once
EVENT_ID_RE = re.compile(r"[0-9]{1,16}-[a-z0-9-]{0,60}")


class Event:
    __slots__ = ("key", "common", "scientific", "t_start", "t_end", "best_beam", "best_conf", "beams", "reports", "max_conf",
                 "max_level", "has_true", "has_false", "non_species", "group", "how", "trace", "gate")

    def __init__(self, key, rep, taxa=None):
        self.key, self.common, self.scientific = key, rep["common"], rep.get("scientific") or ""
        self.group, self.how, self.non_species = (taxa or Taxa()).classify(self.scientific, self.common, rep.get("non_species"))
        self.t_start = self.t_end = rep["t"]
        self.best_beam, self.best_conf = rep["beam"], rep["confidence"]
        self.beams = {rep["beam"]}
        self.reports, self.max_conf = 1, rep["confidence"]
        lv = rep.get("level_dbfs")
        self.max_level = lv if is_num(lv) else None
        self.has_true = rep.get("in_region") is True
        self.has_false = rep.get("in_region") is False
        self.trace = [(rep["t"], rep["beam"], rep["confidence"], self.max_level)]       # every report: the per-beam confidence / level trace of the event
        self.gate = None                                                 # (ambient revision, artifact_flags or None) once the verdict is final

    def merge(self, rep):
        self.t_start, self.t_end = min(self.t_start, rep["t"]), max(self.t_end, rep["t"])
        self.beams.add(rep["beam"])
        self.reports += 1
        if len(self.trace) < MAX_TRACE:
            self.trace.append((rep["t"], rep["beam"], rep["confidence"], rep.get("level_dbfs") if is_num(rep.get("level_dbfs")) else None))
        if rep["confidence"] > self.best_conf:
            self.best_conf, self.best_beam = rep["confidence"], rep["beam"]
        self.max_conf = max(self.max_conf, rep["confidence"])
        lv = rep.get("level_dbfs")
        if is_num(lv) and (self.max_level is None or lv > self.max_level):
            self.max_level = lv
        self.has_true = self.has_true or rep.get("in_region") is True
        self.has_false = self.has_false or rep.get("in_region") is False
        if self.how == "unmapped":                                       # a mapped label's class is fixed; an unmapped one keeps the producer's flag
            self.non_species = self.non_species or bool(rep.get("non_species"))

    @property
    def in_region(self):
        return True if self.has_true else (False if self.has_false else None)


def valid_bird(o):
    return (isinstance(o, dict) and o.get("k") in ("bird", "bird_filtered") and is_num(o.get("t")) and o["t"] > 0
            and isinstance(o.get("beam"), int) and not isinstance(o.get("beam"), bool) and 1 <= o["beam"] <= 8
            and is_num(o.get("confidence")) and 0 <= o["confidence"] <= 1
            and isinstance(o.get("common"), str) and o["common"] != "")


class DetStore:
    def __init__(self, window_s, taxa=None):
        self.window_s = window_s
        self.taxa = taxa or Taxa()
        self.events = []
        self.last = {}                                                   # species key -> newest Event
        self.cutoff_s = 0.0
        self.lock = threading.Lock()
        self.newest_t = None
        self.hidden_open = {}                                            # human-voice events: key -> [t_start, t_end] of the newest one (merged like Event)
        self.hidden_starts = []                                          # the start time of every hidden event in the window (a count is all that is ever served)

    @staticmethod
    def key_of(o):
        return (o.get("scientific") or o["common"]).strip().lower()

    def add(self, o):
        if not valid_bird(o):
            return False
        if o["k"] == "bird_filtered":                                    # the plugin's out-of-region record: kept, flagged false
            o = dict(o, in_region=False)
        if o["t"] < self.cutoff_s:
            return True
        key = self.key_of(o)
        if is_human_label(o["common"], o.get("scientific")):
            with self.lock:
                cur = self.hidden_open.get(key)
                if cur is not None and cur[0] - EVENT_GAP_S <= o["t"] <= cur[1] + EVENT_GAP_S:
                    cur[0], cur[1] = min(cur[0], o["t"]), max(cur[1], o["t"])
                else:
                    self.hidden_open[key] = [o["t"], o["t"]]
                    self.hidden_starts.append(o["t"])
            return True
        with self.lock:
            ev = self.last.get(key)
            if ev is not None and ev.t_start - EVENT_GAP_S <= o["t"] <= ev.t_end + EVENT_GAP_S:
                ev.merge(o)
            else:
                ev = Event(key, o, self.taxa)
                self.events.append(ev)
                if ev.key not in self.last or ev.t_start >= self.last[ev.key].t_start:
                    self.last[ev.key] = ev
            if self.newest_t is None or o["t"] > self.newest_t:
                self.newest_t = o["t"]
        return True

    def hidden_count(self, since):
        with self.lock:
            return sum(1 for t in self.hidden_starts if t >= since)

    def housekeeping(self, now):
        cutoff = now - self.window_s
        with self.lock:
            self.cutoff_s = cutoff
            self.hidden_starts = [t for t in self.hidden_starts if t >= cutoff]
            self.hidden_open = {k: v for k, v in self.hidden_open.items() if v[1] >= cutoff}
            if self.events and min(e.t_start for e in self.events) < cutoff:
                self.events = [e for e in self.events if e.t_end >= cutoff]
                self.last = {}
                for e in sorted(self.events, key=lambda e: e.t_start):
                    self.last[e.key] = e

    def snapshot(self):
        with self.lock:
            return list(self.events)

    @staticmethod
    def hour_rows(events):
        """{hour: {species key: row}} with events attributed to the hour of their first report."""
        out = {}
        for e in events:
            h = hour_of(e.t_start)
            row = out.setdefault(h, {}).setdefault(e.key, {"scientific": e.scientific, "common": e.common, "events": 0, "reports": 0,
                                                           "max_confidence": 0.0, "beam_events": [0] * 8, "in_region": None,
                                                           "non_species": False})
            row["events"] += 1
            row["reports"] += e.reports
            row["max_confidence"] = max(row["max_confidence"], e.max_conf)
            for b in e.beams:
                row["beam_events"][b - 1] += 1
            if e.in_region is True or (e.in_region is False and row["in_region"] is None):
                row["in_region"] = e.in_region
            row["non_species"] = row["non_species"] or e.non_species
        return out

    def closed_rollups(self, now, have, floor):
        out = []
        rows = self.hour_rows(self.snapshot())
        for h in sorted(rows):
            if h + HOUR_S + ROLLUP_GRACE_S > now or ("detections", h) in have or h < floor:
                continue
            sp = [dict(v, max_confidence=round(v["max_confidence"], 4)) for _, v in sorted(rows[h].items())]
            out.append({"k": "ui_rollup", "v": 1, "kind": "detections", "hour_s": h, "species": sp})
        return out


GATE_GROUPS = ("insect", "mechanical")                           # the only groups that can carry artifact_flags (the gate itself decides the rest: none for every other group)
GATE_HALF_S = 30.0
AMBIENT_RELOAD_S = 30.0
AMBIENT_READ_BYTES = 1 << 20                                           # tail of each ambient file: hourly records, a few KB per week
FLAGGED_LEVELS = ("suspect", "likely")


class LiveTransients:
    """The dsp `transient` records (time, beam spread) of the in-memory window, plus the span the dsp file has been seen to cover.
    window() answers like artifact_gate.TransientIndex.window; covers() says whether a window lies fully inside what was read, so an event
    older than the backfill (or newer than the last dsp record) gets NO burst evidence instead of a false 'no burst'."""

    def __init__(self):
        self.t, self.s = array("d"), array("d")
        self.first_t = self.last_t = None
        self.lock = threading.Lock()

    def add(self, o):
        if not isinstance(o, dict) or not is_num(o.get("t")) or o["t"] <= 0:
            return False
        t = float(o["t"])
        with self.lock:
            self.first_t = t if self.first_t is None else min(self.first_t, t)
            self.last_t = t if self.last_t is None else max(self.last_t, t)
            if o.get("k") == "transient":
                sp = o.get("beam_spread_db")
                if not is_num(sp) or sp < 0:
                    return False
                if not self.t or t >= self.t[-1]:
                    self.t.append(t)
                    self.s.append(float(sp))
                else:
                    i = bisect.bisect_right(self.t, t)
                    self.t.insert(i, t)
                    self.s.insert(i, float(sp))
        return True

    def covers(self, t, half_s):
        with self.lock:
            return self.first_t is not None and self.first_t <= t - half_s and t + half_s <= self.last_t

    def window(self, t, half_s):
        with self.lock:
            lo, hi = bisect.bisect_left(self.t, t - half_s), bisect.bisect_right(self.t, t + half_s)
            vals = self.s[lo:hi].tolist()
        return len(vals), (statistics.median(vals) if vals else None)

    def housekeeping(self, cutoff):
        with self.lock:
            i = bisect.bisect_left(self.t, cutoff - GATE_HALF_S)
            if i:
                del self.t[:i]
                del self.s[:i]
            if self.first_t is not None and self.first_t < cutoff:
                self.first_t = cutoff

    def summary(self):
        with self.lock:
            return {"in_memory": len(self.t), "first_t_s": None if self.first_t is None else round(self.first_t, 1),
                    "last_t_s": None if self.last_t is None else round(self.last_t, 1)}


class AmbientSource:
    """<root>/history/ambient_temp.jsonl(.1) -> artifact_gate.AmbientIndex (the reader rule of specs/ambient-temp.md, nearest past hour at most
    3 h old, is applied by the index). Re-read when a file's (mtime, size) changes, checked at most every AMBIENT_RELOAD_S. A missing or
    unreadable file is an EMPTY index: every insect gets `temp_unknown`, nothing is hidden, nothing raises."""

    def __init__(self, directory):
        self.paths = [os.path.join(directory, "ambient_temp.jsonl.1"), os.path.join(directory, "ambient_temp.jsonl")]
        self.sig, self.checked, self.rev, self.index_ = None, None, 0, None
        self.lock = threading.Lock()

    def _signature(self):
        out = []
        for p in self.paths:
            try:
                st = os.stat(p)
                out.append((st.st_mtime_ns, st.st_size))
            except OSError:
                out.append(None)
        return tuple(out)

    def _read(self):
        recs = []
        for p in self.paths:
            try:
                with open(p, "rb") as f:
                    f.seek(0, 2)
                    size = f.tell()
                    f.seek(max(0, size - AMBIENT_READ_BYTES))
                    data = f.read(AMBIENT_READ_BYTES)
            except OSError:
                continue
            for ln in data.split(b"\n"):
                try:
                    recs.append(json.loads(ln))
                except (ValueError, UnicodeDecodeError, RecursionError):
                    pass
        return AG.AmbientIndex(recs)

    def index(self, now):
        """-> (AmbientIndex, revision); the revision changes only when the files' content signature did."""
        with self.lock:
            if self.index_ is None or self.checked is None or now - self.checked >= AMBIENT_RELOAD_S:
                self.checked = now
                sig = self._signature()
                if self.index_ is None or sig != self.sig:
                    self.index_, self.sig = self._read(), sig
                    self.rev += 1
            return self.index_, self.rev


class StreamStore:
    def __init__(self):
        self.samples = collections.deque(maxlen=20000)                   # (t_s, loss_pct, gaps, pps, state)
        self.aggs = {}
        self.lock = threading.Lock()
        self.floor_s = 0.0

    def add(self, o):
        t, loss = o.get("t"), o.get("loss_pct")
        if not is_num(t) or not is_num(loss) or not is_num(o.get("gaps")):
            return False
        with self.lock:
            if self.samples and t <= self.samples[-1][0]:
                return True
            self.samples.append((t, loss, o["gaps"], o.get("pps"), o.get("state")))
            if t < self.floor_s:
                return True
            h = hour_of(t)
            a = self.aggs.setdefault(h, {"n": 0, "sum": 0.0, "min": None, "max": None, "g0": None, "g1": None, "states": {}})
            a["n"] += 1
            a["sum"] += loss
            a["min"] = loss if a["min"] is None else min(a["min"], loss)
            a["max"] = loss if a["max"] is None else max(a["max"], loss)
            if a["g0"] is None:
                a["g0"] = o["gaps"]
            a["g1"] = o["gaps"]
            st = str(o.get("state"))
            a["states"][st] = a["states"].get(st, 0) + 1
        return True

    @staticmethod
    def row(h, a):
        gd = a["g1"] - a["g0"] if a["g0"] is not None and a["g1"] >= a["g0"] else None
        return {"hour_s": h, "samples": a["n"], "loss_pct_mean": round(a["sum"] / a["n"], 3), "loss_pct_min": round(a["min"], 3),
                "loss_pct_max": round(a["max"], 3), "gaps_delta": gd, "state_counts": dict(sorted(a["states"].items()))}

    def housekeeping(self, now, window_s):
        with self.lock:
            fl = ceil_hour(now - window_s)
            self.floor_s = fl
            for h in [h for h in self.aggs if h < fl]:
                del self.aggs[h]

    def closed_rollups(self, now, have):
        out = []
        with self.lock:
            for h in sorted(self.aggs):
                if h + HOUR_S + ROLLUP_GRACE_S > now or ("stream", h) in have:
                    continue
                out.append(dict({"k": "ui_rollup", "v": 1, "kind": "stream"}, **self.row(h, self.aggs[h])))
        return out

    def hourly(self, since, until, rollups):
        out = {}
        with self.lock:
            for h, a in self.aggs.items():
                if hour_of(since) <= h < until:
                    out[h] = self.row(h, a)
        for (kind, h), rec in rollups.items():
            if kind == "stream" and hour_of(since) <= h < until and h not in out:
                out[h] = {k: v for k, v in rec.items() if k not in ("k", "v", "kind")}
        return [out[h] for h in sorted(out)]


# ------------------------------------------------------------------------------------------------------ rollups
class RollupStore:
    """Append-only hourly rollups: rollup_YYYYMMDD.jsonl (UTC day of the hour), one JSON line per (kind, hour), written
    once the hour is closed and never rewritten. One os.write per file per batch (O_APPEND); a torn tail left by a crash
    is terminated with a newline before the next batch and skipped on load."""

    def __init__(self, directory):
        self.dir = directory
        self.recs = {}
        self.garbage = 0
        self.write_errors = 0
        self.load_errors = 0
        self.last_error = None
        self.lock = threading.Lock()

    def load(self):
        self.recs, self.garbage = {}, 0
        if not self.dir or not os.path.isdir(self.dir):
            return
        for fn in sorted(os.listdir(self.dir)):
            if not re.fullmatch(r"rollup_\d{8}\.jsonl", fn):
                continue
            try:
                with open(os.path.join(self.dir, fn), "rb") as f:
                    for ln in f:
                        ln = ln.strip()
                        if not ln:
                            continue
                        try:
                            r = json.loads(ln)
                            ok = valid_rollup(r)
                        except (ValueError, OverflowError, RecursionError):
                            ok = False
                        if not ok:
                            self.garbage += 1
                            continue
                        self.recs.setdefault((r["kind"], r["hour_s"]), r)  # first write wins: idempotent by (kind, hour)
            except OSError:
                self.write_errors += 1

    def have(self):
        return self.recs

    def write(self, batch):
        """Append the records whose key is new; returns how many were written."""
        if not self.dir:
            return 0
        with self.lock:
            fresh, seen = [], set()
            for r in batch:
                k = (r["kind"], r["hour_s"])
                if k not in self.recs and k not in seen:                 # idempotent by (kind, hour), also inside one batch
                    seen.add(k)
                    fresh.append(r)
            if not fresh:
                return 0
            by_file = {}
            for r in fresh:
                day = datetime.datetime.fromtimestamp(r["hour_s"], datetime.timezone.utc).strftime("%Y%m%d")
                by_file.setdefault(day, []).append(r)
            written = 0
            try:
                os.makedirs(self.dir, exist_ok=True)
                for day, recs in by_file.items():
                    path = os.path.join(self.dir, f"rollup_{day}.jsonl")
                    data = b"".join(json.dumps(clean(r), separators=(",", ":"), allow_nan=False).encode() + b"\n" for r in recs)
                    try:
                        with open(path, "rb") as f:
                            f.seek(0, 2)
                            if f.tell():
                                f.seek(-1, 2)
                                if f.read(1) != b"\n":
                                    data = b"\n" + data                  # terminate a torn tail
                    except FileNotFoundError:
                        pass
                    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
                    try:
                        os.write(fd, data)
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    for r in recs:
                        self.recs[(r["kind"], r["hour_s"])] = r
                    written += len(recs)
            except OSError as ex:
                self.write_errors += 1
                self.last_error = type(ex).__name__
            return written


# --------------------------------------------------------------------------------------------------------- board
BOARD_PATHS = {"info": "/v1/info", "state": "/v1/state", "monitor": "/v1/telemetry/monitor", "health": "/v1/telemetry/health?limit=1",
               "network": "/v1/telemetry/network", "dsps": "/v1/dsps", "calibration": "/v1/calibration"}


class Board:
    """Read-only client of the dspd service: HTTP GET only, fixed path allowlist, bearer token read from a file and kept
    in memory. Errors are reduced to a short code; nothing from a request (headers, URL with credentials) is surfaced."""

    def __init__(self, url, token_file, timeout=4.0, enabled=True):
        self.url, self.token_file, self.timeout, self.enabled = url.rstrip("/"), token_file, timeout, enabled
        self.token = None
        self.data = {}
        self.temps = collections.deque(maxlen=20000)                     # (t_s, local_c, remote_c)
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def _load_token(self):
        try:
            with open(self.token_file, encoding="utf-8") as f:
                self.token = f.read().strip() or None
        except OSError:
            self.token = None
        return self.token

    def get(self, name):
        """-> (ok, body, error_code). 404 calibration_empty is ok with body None."""
        path = BOARD_PATHS[name]
        if not (self.token or self._load_token()):
            return False, None, "token_unavailable"
        req = urllib.request.Request(self.url + path, headers={"Authorization": "Bearer " + self.token, "Accept": "application/json"}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return True, json.loads(r.read(1 << 20)), None
        except urllib.error.HTTPError as e:
            code = None
            try:
                code = json.loads(e.read(65536)).get("code")
            except (ValueError, AttributeError, OSError):
                pass
            if e.code == 404 and code == "calibration_empty":
                return True, None, None
            code = (str(code).replace(self.token, "") if self.token else str(code)) if code else ""
            if e.code in (401, 403):
                self.token = None                                        # re-read the file on the next call
            return False, None, ("http_%d" % e.code) + ("_" + re.sub(r"[^a-z_]", "", code)[:40] if code else "")
        except (urllib.error.URLError, OSError, TimeoutError):
            return False, None, "unreachable"
        except ValueError:
            return False, None, "bad_json"

    def refresh(self, names, now=None):
        now = now or time.time()
        for n in names:
            ok, body, err = self.get(n)
            with self.lock:
                if ok:
                    self.data[n] = {"ok": True, "t": now, "body": body, "error": None}
                    if n == "health" and body and body.get("entries"):
                        e = body["entries"][0]
                        self.temps.append((now, e.get("temp_local_c"), e.get("temp_remote_c")))
                else:
                    prev = self.data.get(n, {})
                    self.data[n] = {"ok": False, "t": prev.get("t"), "body": prev.get("body"), "error": err}

    def run(self):
        slow, mid = 0.0, 0.0
        while not self.stop.is_set():
            now = time.time()
            names = ["monitor", "health", "network"]
            if now >= mid:
                names += ["calibration", "state"]
                mid = now + 60
            if now >= slow:
                names += ["info", "dsps"]
                slow = now + 300
            self.refresh(names, now)
            self.stop.wait(20.0)

    def snap(self, name):
        with self.lock:
            return dict(self.data.get(name) or {"ok": False, "t": None, "body": None, "error": "not_polled"})


# ----------------------------------------------------------------------------------------------------- params
class NotFound(Exception):
    def __init__(self, msg):
        super().__init__(msg)
        self.msg = msg


class Bad(Exception):
    def __init__(self, param, msg):
        super().__init__(msg)
        self.param, self.msg = param, msg


class Refuse(Exception):
    """A request that parsed but is refused: 403 for tier access, 429 for the per-address rate limit."""

    def __init__(self, code, name, title, detail, extra=None):
        super().__init__(detail)
        self.code, self.name, self.title, self.detail, self.extra = code, name, title, detail, extra


def norm_peer(peer):
    try:
        a = ipaddress.ip_address(peer)
        return str(getattr(a, "ipv4_mapped", None) or a)
    except ValueError:
        return str(peer)


def spectrum_client_allowed(authenticated, tier, detail_flag):
    """THE rule for GET /api/spectrum (ui/DESIGN.md section 9, ui-http 1.7.0): the spectrum is gated by AUTHENTICATION, never by the source
    network. Returns (allowed, reason). An unauthenticated client is refused for both tiers (reason `spectrum_auth_required`: the API answers
    state `auth_required`, no rows); the detail tier additionally needs the server flag --spectrum-detail (reason `spectrum_detail_disabled`).
    The peer address and the Host header play no part."""
    if not authenticated:
        return False, "spectrum_auth_required"
    if tier == "detail" and not detail_flag:
        return False, "spectrum_detail_disabled"
    return True, None


def request_authenticated(handler):
    """True only when this server has an auth layer (`handler.server.auth`, always set by Server: the login of ui/auth.py, or `auth.OPEN` when no
    login is configured) AND the current request passed it (`_handle` sets the bool attribute `handler.authenticated` to True only after the login
    gate accepted a credential; the default, in Handler, is False). A server without a login (A.OPEN) never marks a request, so the spectrum shows
    state `auth_required` there: an open server is not an authenticated one."""
    return getattr(getattr(handler, "server", None), "auth", None) is not None and getattr(handler, "authenticated", False) is True


RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$")


def p_time(name, v):
    if re.fullmatch(r"\d{1,10}(\.\d{1,6})?", v):
        return float(v)
    if RFC3339.match(v):
        try:
            return datetime.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    raise Bad(name, "expected epoch seconds or an RFC 3339 timestamp such as 2026-10-25T12:00:00Z")


def p_int(name, v, lo, hi):
    if not re.fullmatch(r"\d{1,9}", v) or not lo <= int(v) <= hi:
        raise Bad(name, f"expected an integer in [{lo}, {hi}]")
    return int(v)


def p_float(name, v, lo, hi):
    if not re.fullmatch(r"\d{1,3}(\.\d{1,6})?", v) or not lo <= float(v) <= hi:
        raise Bad(name, f"expected a number in [{lo}, {hi}]")
    return float(v)


def p_str(name, v, maxlen=100):
    if not v or len(v) > maxlen or any(ord(c) < 32 for c in v):
        raise Bad(name, f"expected 1-{maxlen} printable characters")
    return v


def p_enum(name, v, allowed):
    if v not in allowed:
        raise Bad(name, "expected one of " + ", ".join(allowed))
    return v


def parse_query(qs, allowed):
    out = {}
    if not qs:
        return out
    for part in qs.split("&"):
        if not part:
            continue
        k, _, v = part.partition("=")
        try:
            k, v = urllib.parse.unquote(k, errors="strict"), urllib.parse.unquote(v.replace("+", " "), errors="strict")
        except UnicodeDecodeError:
            raise Bad("query", "query string is not valid UTF-8 percent-encoding")
        if k not in allowed:
            raise Bad(k, "unknown parameter; this endpoint accepts: " + (", ".join(sorted(allowed)) or "none"))
        if k in out:
            raise Bad(k, "parameter given more than once")
        out[k] = v
    return out


# ------------------------------------------------------------------------------------------------ spectrum feed
# Magnitude-only band rows computed OUTSIDE this process (pc/bma_spectrum.py) arrive as BMAS datagrams on a loopback UDP socket.
# This process never sees samples. Contract: contracts/specs/spectrum-packet.md (1.0.0) and ui-http.md GET /api/spectrum.
SPECTRUM_VERSION = "1.0.0"
SPEC_TIERS = {"standard": {"id": 0, "hop_ms": 100, "n_bins": 46, "rate": 10, "ring": 600, "cr": 10, "max_limit": 600, "ring_s": 60.0},
              "detail": {"id": 1, "hop_ms": 20, "n_bins": 100, "rate": 50, "ring": 1000, "cr": 50, "max_limit": 500, "ring_s": 20.0}}
SPEC_BY_ID = {v["id"]: k for k, v in SPEC_TIERS.items()}
SPEC_HEADER = 24
SPEC_DB_FLOOR, SPEC_DB_STEP = -100.0, 0.5
SPEC_STALE_S = 3.0
SPEC_LOOKAHEAD_S = 1.5
SPEC_BURST_S = 1.0                                               # seconds of rows a (tier, beam) stream may save up (burst = rate x this: 10 standard, 50 detail); the sustained rate is the tier's cap
SPEC_REQ_RATE, SPEC_REQ_BURST, SPEC_REQ_ADDRS = 5.0, 10.0, 256   # per client address
SPEC_BAND_DEFAULT = 5
SPEC_SAME_WINDOW_US = 1_000_000                                    # beams of one window carry the same t_us; a row number at another time is a producer restart
SPEC_LATCH_BEFORE_S = 5.0                                        # the tagger speech_active covers the 5 s ending at its t


def spec_edges(tier):
    if tier == 0:
        e = [(50.0, 250.0)] + [(250.0 * 2 ** (k / 3), 250.0 * 2 ** ((k + 1) / 3)) for k in range(9)]
        return e + [(2000.0 * 2 ** (k / 12), 2000.0 * 2 ** ((k + 1) / 12)) for k in range(36)]
    e = [(250.0 * k, 250.0 * (k + 1)) for k in range(4)]
    return e + [(1000.0 * 2 ** (k / 24), 1000.0 * 2 ** ((k + 1) / 24)) for k in range(96)]


def spec_groups(tier):
    if tier == 0:
        return [0] + list(range(1, 10)) + [10 + (i // 4) for i in range(36)]
    return list(range(4)) + [4 + (i // 8) for i in range(96)]


SPEC_EDGES_HZ = {name: [round(spec_edges(t["id"])[0][0], 1)] + [round(hi, 1) for _lo, hi in spec_edges(t["id"])] for name, t in SPEC_TIERS.items()}


def spec_band_bins(tier, band):
    """Indices of the spectrum bands whose geometric centre lies in octave band `band` of EDGES_HZ (the `compare` levels)."""
    lo, hi = EDGES_HZ[band]
    return [i for i, (a, b) in enumerate(spec_edges(tier)) if a > 0 and lo <= math.sqrt(a * b) < hi]


SPEC_BAND_BINS = {(name, band): spec_band_bins(t["id"], band) for name, t in SPEC_TIERS.items() for band in range(8)}


def spec_quantize(db):
    return max(0, min(255, round((db - SPEC_DB_FLOOR) / SPEC_DB_STEP)))


def spec_power(v):
    return 10.0 ** ((v * SPEC_DB_STEP + SPEC_DB_FLOOR) / 10.0)


def spec_coarsen(tier, rows):
    """rows: byte strings of uint8 (n_bins each) of ONE block -> one uint8 row, the mean linear power per coarse group (the gating rule)."""
    groups = spec_groups(tier)
    out = []
    for g in sorted(set(groups)):
        idx = [i for i, x in enumerate(groups) if x == g]
        vals = [spec_power(r[i]) for r in rows for i in idx]
        out.append(spec_quantize(10.0 * math.log10(sum(vals) / len(vals))))
    return bytes(out[g] for g in groups)


class SpecError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def parse_spectrum_packet(pkt):
    """-> (tier_name, flags, beam, row, t_us, bins bytes) or raises SpecError(code); the codes are the contract's rejection codes."""
    if len(pkt) < SPEC_HEADER:
        raise SpecError("short")
    if pkt[:4] != b"BMAS":
        raise SpecError("magic")
    if pkt[4] != 1:
        raise SpecError("version")
    tier, flags, beam = pkt[5], pkt[6], pkt[7]
    if tier not in SPEC_BY_ID:
        raise SpecError("tier")
    if flags & ~0x07:
        raise SpecError("flags")
    if beam > 8:
        raise SpecError("beam")
    row, t_us, hop, n = struct.unpack_from("<IQHH", pkt, 8)
    t = SPEC_TIERS[SPEC_BY_ID[tier]]
    if hop != t["hop_ms"]:
        raise SpecError("hop")
    if n != t["n_bins"]:
        raise SpecError("n_bins")
    if len(pkt) != SPEC_HEADER + n:
        raise SpecError("length")
    bins = bytes(pkt[SPEC_HEADER:])
    if flags & 1:
        g = spec_groups(tier)
        if any(g[i] == g[i - 1] and bins[i] != bins[i - 1] for i in range(1, n)):
            raise SpecError("gated_not_coarse")
    return SPEC_BY_ID[tier], flags, beam, row, t_us, bins


class SpecSlot:
    __slots__ = ("idx", "run", "row", "t_us", "arr", "flags", "bins", "rev_at")

    def __init__(self, idx, run, row, t_us, arr):
        self.idx, self.run, self.row, self.t_us, self.arr = idx, run, row, t_us, arr
        self.flags, self.bins, self.rev_at = [0] * 9, [None] * 9, 0


class SpecRing:
    def __init__(self, name):
        self.name, self.t = name, SPEC_TIERS[name]
        self.slots = collections.deque()
        self.by_row = {}
        self.run, self.run_base, self.newest_idx, self.last_row, self.last_t_us, self.rev = 0, 0, 0, None, 0, 0


class RequestLimiter:
    """Per client address token bucket: SPEC_REQ_RATE tokens/s, burst SPEC_REQ_BURST, at most SPEC_REQ_ADDRS addresses tracked (LRU)."""

    def __init__(self, rate=SPEC_REQ_RATE, burst=SPEC_REQ_BURST, max_addrs=SPEC_REQ_ADDRS):
        self.rate, self.burst, self.max_addrs = rate, burst, max_addrs
        self.b = collections.OrderedDict()
        self.lock = threading.Lock()

    def allow(self, addr, now):
        with self.lock:
            tok, last = self.b.pop(addr, (self.burst, now))
            tok = min(self.burst, tok + max(0.0, now - last) * self.rate)
            ok = tok >= 1.0
            self.b[addr] = (tok - 1.0 if ok else tok, now)
            while len(self.b) > self.max_addrs:
                self.b.popitem(last=False)
            return ok


class SpectrumFeed:
    """The bounded in-memory ring of spectrum rows (nothing persisted) and the loopback UDP receiver that fills it."""

    def __init__(self, enabled=False, detail=False, clock=time.time):
        self.enabled, self.detail_enabled, self.clock = enabled, detail, clock
        self.rings = {n: SpecRing(n) for n in SPEC_TIERS}
        self.lock = threading.Lock()
        self.buckets = {}                                         # (tier, beam) -> (tokens, last)
        self.windows = collections.deque(maxlen=64)               # tag latch windows (t0, t1)
        self.limiter = RequestLimiter()
        self.rows_received = self.rows_dropped_rate = self.bad_packets = self.foreign = self.detail_ignored = 0
        self.sock, self.thread, self.port = None, None, None

    # ---- receiver
    def start(self, port):
        """Bind the loopback socket (only 127.0.0.1) and start the receiver thread. port 0 picks a free one (tests)."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        s.bind(("127.0.0.1", port))
        s.settimeout(0.5)
        self.sock, self.port = s, s.getsockname()[1]
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return self.port

    def close(self):
        s, self.sock = self.sock, None
        if s:
            s.close()

    def _loop(self):
        while self.sock is not None:
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            self.handle(data, addr[0])

    def handle(self, data, src="127.0.0.1"):
        """One datagram. Rows from a non-loopback source, malformed rows and rows over the tier's rate cap are dropped and counted."""
        now = self.clock()
        with self.lock:
            try:
                if not ipaddress.ip_address(src).is_loopback:
                    self.foreign += 1
                    return False
                tier, flags, beam, row, t_us, bins = parse_spectrum_packet(data)
            except (SpecError, ValueError):
                self.bad_packets += 1
                return False
            if tier == "detail" and not self.detail_enabled:
                self.detail_ignored += 1
                return False
            ring, t = self.rings[tier], SPEC_TIERS[tier]
            burst = t["rate"] * SPEC_BURST_S
            tok, last = self.buckets.get((tier, beam), (burst, now))
            tok = min(burst, tok + max(0.0, now - last) * t["rate"])
            if tok < 1.0:
                self.buckets[(tier, beam)] = (tok, now)
                self.rows_dropped_rate += 1
                return False
            self.buckets[(tier, beam)] = (tok - 1.0, now)
            s = ring.by_row.get((ring.run, row))
            restart = False
            if s is not None and abs(s.t_us - t_us) > SPEC_SAME_WINDOW_US:
                s, restart = None, True                           # the same row number at another time: the producer restarted
            elif s is None and ring.last_row is not None and row <= ring.last_row:
                if t_us <= ring.last_t_us:                        # a late row of a window that was never opened: drop
                    self.bad_packets += 1
                    return False
                restart = True
            if s is None:
                if restart:
                    ring.run, ring.run_base, ring.last_row = ring.run + 1, ring.newest_idx, None      # a new run continues the idx
                s = SpecSlot(ring.run_base + row + 1, ring.run, row, t_us, now)
                ring.slots.append(s)
                ring.by_row[(ring.run, row)] = s
                ring.last_row, ring.last_t_us, ring.newest_idx = row, t_us, s.idx
                while len(ring.slots) > t["ring"]:
                    old = ring.slots.popleft()
                    ring.by_row.pop((old.run, old.row), None)
            if s.bins[beam] is not None:
                self.bad_packets += 1                              # a duplicate
                return False
            s.bins[beam], s.flags[beam] = bins, flags
            self.rows_received += 1
            if self.windows and not flags & 7 and self._in_window(s.t_us / 1e6):
                blk = [x for x in ring.slots if x.run == s.run and x.row // t["cr"] == s.row // t["cr"]]
                if self._gate_block(ring, blk):
                    ring.rev += 1
                    self._stamp(ring)
            return True

    # ---- the tag latch
    def _in_window(self, t):
        return any(a <= t <= b for a, b in self.windows)

    def latch(self, t0, t1):
        """The analyzer tagger flagged speech over [t0, t1]: rows inside are replaced by their block's coarse value and gated (rev + 1)."""
        if not self.enabled:
            return 0
        n = 0
        with self.lock:
            if t1 < self.clock() - 2 * max(r["ring_s"] for r in SPEC_TIERS.values()):   # an old tag (backfill): nothing in the rings to rewrite
                return 0
            self.windows.append((t0, t1))
            for ring in self.rings.values():
                blocks = {}
                for s in ring.slots:
                    blocks.setdefault((s.run, s.row // ring.t["cr"]), []).append(s)
                changed = 0
                for ss in blocks.values():
                    if any(t0 <= s.t_us / 1e6 <= t1 for s in ss):
                        changed += self._gate_block(ring, ss)
                if changed:
                    ring.rev += 1
                    self._stamp(ring)
                n += changed
        return n

    @staticmethod
    def _stamp(ring):
        for s in ring.slots:
            if s.rev_at == -1:
                s.rev_at = ring.rev

    def _gate_block(self, ring, ss):
        """Replace the not yet gated rows of one block by the block's coarse value, per beam. Changed slots get rev_at = -1 (see _stamp)."""
        tid, changed = ring.t["id"], 0
        for beam in range(9):
            fresh = [s for s in ss if s.bins[beam] is not None and not s.flags[beam] & 7]
            if not fresh:
                continue
            c = spec_coarsen(tid, [s.bins[beam] for s in fresh])
            for s in fresh:
                s.bins[beam], s.flags[beam] = c, s.flags[beam] | 1
                s.rev_at = -1
            changed += len(fresh)
        return changed

    # ---- reading
    def state(self, ring, now):
        if not self.enabled:
            return "disabled"
        if not ring.slots:
            return "no_data"
        return "stale" if now - ring.slots[-1].arr > SPEC_STALE_S else "live"

    LIVE_PARTIAL_S = 0.3                                                  # a row still missing beams this long after it arrived is sent as it is (flag bit 3)

    def live_rows(self, since, rev, now):
        """The WebSocket's view of the standard ring (ui/ws.py): -> (rev, reset, rows). `since` is the idx of the last row sent (-1 = none), `rev` the revision
        last seen (None = first call). reset is True when the revision moved (rows were re-gated) or the cursor is ahead of the ring: the whole ring comes
        back and the client starts over. rows = [(idx, t_us, flags[9], bins[9] of bytes or None)] in idx order; the newest slot waits for its missing beams."""
        with self.lock:
            ring = self.rings["standard"]
            reset = (rev is not None and rev != ring.rev) or since > ring.newest_idx
            if reset:
                since = -1
            out = []
            for i, sl in enumerate(ring.slots):
                if sl.idx <= since:
                    continue
                if i == len(ring.slots) - 1 and None in sl.bins and now - sl.arr < self.LIVE_PARTIAL_S:
                    break
                out.append((sl.idx, sl.t_us, list(sl.flags), list(sl.bins)))
            return ring.rev, reset, out

    def summary(self):
        now = self.clock()
        with self.lock:
            ring = self.rings["standard"]
            return {"enabled": self.enabled, "detail_enabled": self.detail_enabled, "state": self.state(ring, now),
                    "rows_received": self.rows_received, "rows_dropped_rate": self.rows_dropped_rate, "bad_packets": self.bad_packets,
                    "newest_age_s": round(now - ring.slots[-1].arr, 1) if ring.slots else None,
                    "gated_now": bool(ring.slots and ring.slots[-1].flags[0] & 1)}

    @staticmethod
    def _level(s, beam, bins_idx):
        if s.bins[beam] is None or not bins_idx:
            return 0
        return spec_quantize(10.0 * math.log10(sum(spec_power(s.bins[beam][i]) for i in bins_idx)))

    def auth_required(self, tier, beam):
        """The answer for a client that may not see the spectrum: state auth_required, HTTP 200, no rows and nothing about the feed (no cursor, no
        gate state, no revision), so the reply says only that the data exists somewhere else."""
        t = SPEC_TIERS[tier]
        return {"spectrum_version": SPECTRUM_VERSION, "state": "auth_required", "tier": tier, "beam": beam, "now_s": round(self.clock(), 3), "n_bins": 0,
                "edges_hz": [], "db_floor": SPEC_DB_FLOOR, "db_step": SPEC_DB_STEP, "rows_per_s": t["rate"], "hop_ms": t["hop_ms"], "rev": 0,
                "newest_idx": 0, "next_since": 0, "rows_missed": 0, "newest_age_s": None, "gated_now": False,
                "limits": {"ring_s": t["ring_s"], "max_rows": t["max_limit"], "requests_per_s": SPEC_REQ_RATE}, "rows": []}

    def snapshot(self, tier, beam, since, rev, compare, band, limit):
        now = self.clock()
        t = SPEC_TIERS[tier]
        with self.lock:
            ring = self.rings[tier]
            st = self.state(ring, now)
            slots = [s for s in ring.slots if s.bins[beam] is not None]
            newest = ring.slots[-1] if ring.slots else None
            if since > ring.newest_idx:                           # the client's cursor is from an older server run
                since = 0
            if since:
                slots = [s for s in slots if s.idx > since or s.rev_at > rev]
            oldest = ring.slots[0].idx if ring.slots else 0
            missed = oldest - 1 - since if since and ring.slots and since < oldest - 1 else 0
            bins_idx = SPEC_BAND_BINS[(tier, band)]
            rows = []
            for s in slots[-limit:]:
                r = {"idx": s.idx, "t_s": round(s.t_us / 1e6, 3), "flags": s.flags[beam], "gated": bool(s.flags[beam] & 1),
                     "bins": base64.b64encode(s.bins[beam]).decode("ascii")}
                if compare:
                    r["levels"] = [self._level(s, b, bins_idx) for b in range(1, 9)]
                rows.append(r)
            gated_rows = sum(1 for s in ring.slots if s.bins[beam] is not None and s.flags[beam] & 1)
            gnow = bool(newest and newest.flags[beam if newest.bins[beam] is not None else 0] & 1)
            return {"spectrum_version": SPECTRUM_VERSION, "state": st, "tier": tier, "beam": beam, "now_s": round(now, 3),
                    "n_bins": t["n_bins"] if self.enabled else 0, "edges_hz": SPEC_EDGES_HZ[tier] if self.enabled else [],
                    "db_floor": SPEC_DB_FLOOR, "db_step": SPEC_DB_STEP, "rows_per_s": t["rate"], "hop_ms": t["hop_ms"], "rev": ring.rev,
                    "newest_idx": ring.newest_idx, "next_since": ring.newest_idx, "rows_missed": missed,
                    "newest_age_s": round(now - newest.arr, 1) if newest else None, "gated_now": gnow,
                    "gate": {"lookahead_s": SPEC_LOOKAHEAD_S, "gated_rows_in_ring": gated_rows, "tag_latch": True},
                    "limits": {"ring_s": t["ring_s"], "max_rows": t["max_limit"], "requests_per_s": SPEC_REQ_RATE}, "rows": rows}


# -------------------------------------------------------------------------------------------------------- the app
class App:
    SOURCES = [  # name, stale_after_s (None = event driven, no staleness)
        ("beam_bands", 15), ("birds", None), ("tags", None), ("clap", None), ("spatial", 30), ("dsp", 30), ("detections", None),
        ("bridge_log", 90), ("bridge_status", 90), ("sink_status", 90), ("analysis_status", 90)]

    def __init__(self, pipeline_root, reports_dir, rollup_dir, board, window_h=48, clock=time.time, log=None, slot_fix_at=None, taxa_path=TAXA_PATH, spectrum=None, labels_dir=None, review_allow_no_clip=False, artifact_gate=True):
        self.slot_fix_s = float(default_slot_fix_s() if slot_fix_at is None else slot_fix_at)
        self.root, self.reports, self.clock = pipeline_root, reports_dir, clock
        self.log = log or (lambda m: print(m, file=sys.stderr, flush=True))
        self.window_s = int(window_h * 3600)
        self.taxa = load_taxa(taxa_path)
        self.beams, self.dets, self.stream = BeamStore(self.window_s), DetStore(self.window_s, self.taxa), StreamStore()
        self.rollups = RollupStore(rollup_dir)
        self.gate_on = bool(artifact_gate) and AG is not None                   # advisory flags (specs/artifact-gate.md); off = no field anywhere
        self.trans = LiveTransients()
        self.ambient = AmbientSource(os.path.join(pipeline_root, "history"))
        self.review = RV.Review(pipeline_root, labels_dir, taxa_path, clock, allow_no_clip=review_allow_no_clip) if RV else None
        self.board = board
        self.spectrum = spectrum or SpectrumFeed(enabled=False, clock=clock)
        self.latest = {"tags": None, "clap": None, "spatial": None, "dsp": None, "detections": None}
        self.spatial = collections.deque(maxlen=3600)
        self.paths = {"beam_bands": os.path.join(reports_dir, "beam_bands.jsonl"),
                      "birds": os.path.join(pipeline_root, "analysis", "birds.jsonl"),
                      "tags": os.path.join(pipeline_root, "analysis", "tags.jsonl"),
                      "clap": os.path.join(pipeline_root, "analysis", "clap.jsonl"),
                      "spatial": os.path.join(pipeline_root, "analysis", "spatial.jsonl"),
                      "dsp": os.path.join(pipeline_root, "analysis", "dsp.jsonl"),
                      "detections": os.path.join(pipeline_root, "logs", "detections.jsonl"),
                      "bridge_log": os.path.join(pipeline_root, "logs", "bridge.log"),
                      "bridge_status": os.path.join(pipeline_root, "bridge_status.json"),
                      "sink_status": os.path.join(pipeline_root, "sink_status.json"),
                      "analysis_status": os.path.join(pipeline_root, "analysis", "status.json")}
        self.tailers = {
            "beam_bands": Tailer(self.paths["beam_bands"], self.beams.add),
            "birds": Tailer(self.paths["birds"], self.dets.add),
            "tags": Tailer(self.paths["tags"], self._tag),
            "clap": Tailer(self.paths["clap"], lambda o: self._latest("clap", o, "clap")),
            "spatial": Tailer(self.paths["spatial"], self._spatial),
            "dsp": Tailer(self.paths["dsp"], self._dsp),
            "detections": Tailer(self.paths["detections"], self._detection_line),
            "bridge_log": Tailer(self.paths["bridge_log"], self.stream.add, parse=BridgeLogParser())}
        self.backfill_done, self.backfill_s, self.started_s = False, None, clock()
        self.json_cache = {}
        self.cache = {}
        self.cache_lock = threading.Lock()
        self.stop = threading.Event()

    # ---- record sinks
    def _latest(self, slot, o, kind):
        if not isinstance(o, dict) or o.get("k") != kind or not is_num(o.get("t")):
            return False
        cur = self.latest[slot]
        if cur is None or o["t"] >= cur["t"]:
            self.latest[slot] = o
        return True

    def _tag(self, o):
        if not self._latest("tags", o, "tags"):
            return False
        if o.get("speech_active") is True:                               # the slower speech flag re-gates rows already in the spectrum ring
            self.spectrum.latch(o["t"] - SPEC_LATCH_BEFORE_S, o["t"])
        return True

    def _dsp(self, o):
        if self.gate_on and isinstance(o, dict) and o.get("k") in ("transient", "dsp", "tonal_state", "tonal_appeared", "tonal_gone"):
            self.trans.add(o)                                            # transients for the artifact gate, and the span the file has covered
            if o.get("k") != "dsp":
                return True                                              # a known dsp record kind this reader uses for the gate, not a rejected line
        return self._latest("dsp", o, "dsp")

    def _spatial(self, o):
        if not self._latest("spatial", o, "spatial"):
            return False
        self.spatial.append((o["t"], o.get("dominant_beam")))
        return True

    def _detection_line(self, o):
        if not isinstance(o, dict) or o.get("event") != "detection" or not is_num(o.get("t_wall")):
            return False
        cur = self.latest["detections"]
        if cur is None or o["t_wall"] >= cur["t_wall"]:
            self.latest["detections"] = o
        return True

    # ---- life cycle
    def start_backfill(self):
        t0 = time.time()
        now = self.clock()
        self.beams.set_cutoff(now - self.window_s)
        self.dets.cutoff_s = now - self.window_s
        self.stream.floor_s = ceil_hour(now - self.window_s)
        try:
            self.rollups.load()
        except Exception as ex:                                          # noqa: BLE001 - history is optional, the live data is not
            self.rollups.load_errors += 1
            self.log("rollup load failed: " + type(ex).__name__)
        per_day = 700 * 86400                                            # generous bytes per record-second bound
        plan = {"beam_bands": (int(self.window_s * 700) + (1 << 20), True), "birds": (64 << 20, False), "tags": (256 << 10, False),
                "clap": (256 << 10, False), "spatial": (1 << 20, False), "dsp": (DSP_BACKFILL_BYTES, True) if self.gate_on else (128 << 10, False), "detections": (256 << 10, False),
                "bridge_log": (4 << 20, False)}
        del per_day
        threads = []
        for name, (nbytes, rot) in plan.items():
            th = threading.Thread(target=self._backfill_one, args=(name, nbytes, rot), daemon=True)
            th.start()
            threads.append(th)
        for th in threads:
            th.join()
        self.housekeeping()
        self.backfill_s = time.time() - t0
        self.backfill_done = True

    def _backfill_one(self, name, nbytes, rot):
        try:
            self.tailers[name].backfill(nbytes, rot)
        except Exception as ex:                                          # noqa: BLE001 - one source failing must not stop the rest
            self.tailers[name].last_error = type(ex).__name__
            self.log("backfill %s failed: %s" % (name, type(ex).__name__))

    def housekeeping(self):
        now = self.clock()
        self.beams.housekeeping(now)
        self.dets.housekeeping(now)
        self.trans.housekeeping(now - self.window_s)
        self.stream.housekeeping(now, self.window_s)
        have = self.rollups.have()
        batch = (self.beams.closed_rollups(now, have) + self.dets.closed_rollups(now, have, ceil_hour(now - self.window_s))
                 + self.stream.closed_rollups(now, have))
        if batch:
            self.rollups.write(batch)

    def run_loop(self):
        tick = 0
        while not self.stop.wait(1.0):
            tick += 1
            for t in self.tailers.values():
                try:
                    t.poll()
                except Exception:                                        # noqa: BLE001
                    pass
            if tick % 30 == 0:
                try:
                    self.housekeeping()
                except Exception as ex:                                  # noqa: BLE001
                    self.log("housekeeping error: " + type(ex).__name__)

    # ---- helpers
    def cached(self, key, ttl, fn):
        now = time.time()
        with self.cache_lock:
            hit = self.cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        val = fn()
        with self.cache_lock:
            if len(self.cache) > 200:
                self.cache.clear()
            self.cache[key] = (now, val)
        return val

    def read_json_file(self, name):
        p = self.paths[name]
        try:
            st = os.stat(p)
            if st.st_size > (1 << 20):
                return None, st
            with open(p, "rb") as f:
                return json.loads(f.read()), st
        except (OSError, ValueError):
            return None, None

    def calibration(self):
        snap = self.board.snap("calibration") if self.board.enabled else {"ok": False, "body": None, "error": "board_disabled"}
        prof = (snap.get("body") or {}).get("profile") if snap.get("ok") else None
        az = None
        if isinstance(prof, dict) and isinstance(prof.get("beam_azimuth_deg"), list) and len(prof["beam_azimuth_deg"]) == 8 \
                and all(is_num(a) for a in prof["beam_azimuth_deg"]):
            az = [round(float(a), 2) for a in prof["beam_azimuth_deg"]]
        if az:
            m = prof.get("measured_at") if isinstance(prof.get("measured_at"), str) and RFC3339.match(prof["measured_at"]) else None
            nref = prof.get("north_reference") if prof.get("north_reference") in ("compass", "array") else None
            base = {"calibrated": True, "beam_azimuth_deg": az, "provisional": True, "north_reference": nref, "measured_at": m, "board_reachable": True, "board_error": None}
            if nref == "compass":                                        # the board says these are compass bearings: no panel up/down marks, no panel conversion
                return dict(base, frame="calibrated compass frame " + (m or "(time not recorded)"), frame_kind=FRAME_KIND_COMPASS, panel_up_dsp_azimuth_deg=None,
                            note="Stored on the board as metadata; it is not applied to any DSP. The board marks these azimuths as compass bearings (0 = north, clockwise); provisional.")
            return dict(base, frame="calibrated panel frame " + (m or "(time not recorded)"), frame_kind=FRAME_KIND_PANEL, panel_up_dsp_azimuth_deg=PANEL_UP_DSP_AZ_DEG,
                        note="Stored on the board as metadata; it is not applied to any DSP. Azimuths are in the panel frame (the array is mounted upright): 0 = panel up, increasing clockwise seen from the front; provisional.")
        return {"calibrated": False, "frame": FRAME_UNCAL, "beam_azimuth_deg": list(BEAM_AZ_ARRAY_DEG), "north_reference": None,
                "frame_kind": FRAME_KIND_ARRAY, "provisional": False, "panel_up_dsp_azimuth_deg": None, "measured_at": None, "board_reachable": bool(snap.get("ok")), "board_error": snap.get("error"),
                "note": "Fallback layout: audio beam k at entry k of these DSP-frame azimuths; no calibration is stored, so no physical direction is known."}

    @staticmethod
    def _range(q, default_span=86400.0, now=None):
        until = p_time("until", q["until"]) if "until" in q else now
        since = p_time("since", q["since"]) if "since" in q else until - default_span
        if since >= until:
            raise Bad("since", "since must be before until")
        if until - since > MAX_RANGE_S:
            raise Bad("since", "range is limited to 90 days")
        return since, until

    # ---- endpoints
    def api_status(self, q):
        now = self.clock()
        srcs = []
        for name, stale_after in self.SOURCES:
            p = self.paths[name]
            try:
                st = os.stat(p)
                age = max(0.0, now - st.st_mtime)
                row = {"name": name, "present": True, "size_bytes": st.st_size, "mtime_s": round(st.st_mtime, 3), "age_s": round(age, 1)}
            except OSError:
                row = {"name": name, "present": False, "size_bytes": None, "mtime_s": None, "age_s": None}
            row["stale_after_s"] = stale_after
            row["stale"] = True if not row["present"] else (None if stale_after is None else row["age_s"] > stale_after)
            t = self.tailers.get(name)
            row.update(t.counters() if t else {"lines": 0, "garbage_lines": 0, "rejected_records": 0, "resets": 0, "last_error": None})
            srcs.append(row)
        br, _ = self.read_json_file("bridge_status")
        sk, _ = self.read_json_file("sink_status")
        an, _ = self.read_json_file("analysis_status")

        def age_of(t):
            return round(max(0.0, now - t), 1) if is_num(t) else None
        bridge = None
        if isinstance(br, dict):
            bridge = {"state": br.get("state"), "loss_pct": br.get("loss_pct"), "gaps": br.get("gaps"), "pkts": br.get("pkts"),
                      "pps": br.get("pps"), "uptime_s": br.get("uptime_s"), "nch": br.get("nch"), "age_s": age_of(br.get("t"))}
        sink = None
        if isinstance(sk, dict):
            sink = {"audio_output": sk.get("audio_output"), "playing": sk.get("playing"), "holes": sk.get("holes"),
                    "data_dropped": sk.get("data_dropped"), "underruns": sk.get("underruns"), "age_s": age_of(sk.get("t"))}
        analysis = None
        if isinstance(an, dict):
            plugins = {}
            for n, p in (an.get("plugins") or {}).items():
                if isinstance(p, dict):
                    plugins[n] = {"enabled": p.get("enabled"), "records": p.get("records"), "errors": p.get("errors"),
                                  "dropped": p.get("dropped"), "last_t_age_s": age_of(p.get("last_t"))}
            analysis = {"device": an.get("device"), "uptime_s": an.get("uptime_s"), "rx": an.get("rx"), "plugins": plugins,
                        "age_s": age_of(an.get("t"))}
        evs = self.dets.snapshot()
        last_ev = max(evs, key=lambda e: e.t_end) if evs else None
        info = self.board.snap("info") if self.board.enabled else {"ok": False, "error": "board_disabled", "body": None, "t": None}
        health = self.board.snap("health") if self.board.enabled else {"ok": False, "body": None}
        state = self.board.snap("state") if self.board.enabled else {"ok": False, "body": None}
        hb = ((health.get("body") or {}).get("entries") or [{}])[0]
        sb = state.get("body") or {}
        beams_state = sb.get("beams") or {}
        board = {"reachable": bool(info.get("ok") or health.get("ok")), "enabled": self.board.enabled,
                 "error": None if (info.get("ok") or health.get("ok")) else (health.get("error") or info.get("error")),
                 "age_s": age_of(health.get("t")) if health.get("ok") else None,
                 "api_version": (info.get("body") or {}).get("api_version"), "uptime_s": (info.get("body") or {}).get("uptime_s"),
                 "temp_local_c": hb.get("temp_local_c"), "temp_remote_c": hb.get("temp_remote_c"),
                 "pattern_ref": beams_state.get("pattern_ref"), "beams_applied_by_dspd": beams_state.get("applied")}
        lat = self.latest
        spat = lat["spatial"]
        unc24 = sum(1 for e in evs if e.how == "unmapped" and e.t_start >= now - 86400)
        return {"ui_version": UI_VERSION, "taxa": {"loaded": self.taxa.error is None and bool(self.taxa.genus), "n_genera": len(self.taxa.genus), "error": self.taxa.error,
                                                   "unclassified_events_24h": unc24}, "human_hidden_24h": self.dets.hidden_count(now - 86400), "artifact_gate": self.gate_status(now), "now_s": round(now, 3), "now_utc": iso_z(now), "frame": self.calibration()["frame"],
                "slot_mapping_reliable_from_s": self.slot_fix_s, "bridge": bridge, "sink": sink, "analysis": analysis, "board": board, "sources": srcs,
                "any_stale": any(s["stale"] is True for s in srcs), "spectrum": self.spectrum.summary(),
                "last_event": None if not last_ev else {"t_s": round(last_ev.t_end, 3), "age_s": round(max(0.0, now - last_ev.t_end), 1),
                                                         "species": last_ev.common, "confidence": round(last_ev.max_conf, 4),
                                                         "in_region": last_ev.in_region},
                "latest": {"tags": visible_top((lat["tags"] or {}).get("top")), "tags_t_s": (lat["tags"] or {}).get("t"),
                           "clap": visible_top((lat["clap"] or {}).get("top")), "clap_t_s": (lat["clap"] or {}).get("t"),
                           "spatial": None if not spat else {"t_s": spat["t"], "az_deg": spat.get("az_deg"), "dominant_beam": spat.get("dominant_beam"),
                                                             "spread_db": spat.get("spread_db"), "directional": spat.get("directional"),
                                                             "calibrated": spat.get("calibrated")},
                           "dsp_rms_dbfs": (lat["dsp"] or {}).get("rms_dbfs"), "dsp_t_s": (lat["dsp"] or {}).get("t")},
                "ingest": {"backfill_done": self.backfill_done, "backfill_s": None if self.backfill_s is None else round(self.backfill_s, 2),
                           "window_h": self.window_s / 3600.0, "events_in_memory": len(self.dets.snapshot()),
                           "beam_windows_in_memory": self.beams.size(), "rollup_records": len(self.rollups.recs),
                           "rollup_garbage_lines": self.rollups.garbage, "rollup_write_errors": self.rollups.write_errors,
                           "rollup_load_errors": self.rollups.load_errors}}

    def _gate(self, e, now):
        """The advisory artifact_flags of an event (evaluated at its first report), or None: the gate is off, the group is out of scope or
        a human label (never stored here). Cached on the event once final (its +-30 s window is complete and the ambient files are unchanged)."""
        if not self.gate_on or e.group not in GATE_GROUPS:
            return None
        amb, rev = self.ambient.index(now)
        if e.gate is not None and e.gate[0] == rev:
            return e.gate[1]
        covered = self.trans.covers(e.t_start, GATE_HALF_S)
        v = AG.evaluate(group=e.group, label=e.common, scientific=e.scientific, t=e.t_start, ambient=amb,
                        transients=self.trans if covered else None)
        if covered and self.backfill_done:
            e.gate = (rev, v)
        return v

    def gate_status(self, now):
        """status.artifact_gate: what the advisory flags are running on, so a reader can tell 'no flags' from 'no data to flag with'."""
        if not self.gate_on:
            return {"enabled": False, "available": AG is not None, "ambient": None, "transients": None, "events_scored_24h": None,
                    "events_flagged_24h": None}
        amb, _rev = self.ambient.index(now)
        lo, hi = amb.span()
        evs = [e for e in self.dets.snapshot() if e.group in GATE_GROUPS and e.t_start >= now - 86400]
        flagged = sum(1 for e in evs if self._flagged(e, now))
        return {"enabled": True, "available": True, "floor_c": AG.DEFAULT.floor_for("insect"), "temp_max_age_s": AG.DEFAULT.temp_max_age_s,
                "burst_window_s": AG.DEFAULT.burst_window_s, "burst_min_transients": AG.DEFAULT.burst_min_transients,
                "ambient": {"records": len(amb), "first_hour_s": lo, "newest_hour_s": hi,
                            "newest_age_s": None if hi is None else round(max(0.0, now - hi), 1), "skipped": amb.skipped},
                "transients": self.trans.summary(), "events_scored_24h": len(evs), "events_flagged_24h": flagged}

    def _flagged(self, e, now):
        v = self._gate(e, now)
        return v is not None and v["level"] in FLAGGED_LEVELS

    @staticmethod
    def _event_id(e):
        return "%d-%s" % (int(e.t_start * 1000), re.sub(r"[^a-z0-9]+", "-", e.key)[:60])

    def _event_json(self, e, az, now=None):
        j = self._event_json_base(e, az)
        v = self._gate(e, self.clock() if now is None else now)
        if v is not None:
            j["artifact_flags"] = v
        return j

    def _event_json_base(self, e, az):
        return {"id": self._event_id(e), "species": e.common,
                "scientific": e.scientific, "t_start_s": round(e.t_start, 3), "t_end_s": round(e.t_end, 3), "best_beam": e.best_beam,
                "best_beam_azimuth_deg": az[e.best_beam - 1], "best_confidence": round(e.best_conf, 4), "beams": sorted(e.beams),
                "reports": e.reports, "max_confidence": round(e.max_conf, 4),
                "max_level_dbfs": None if e.max_level is None else round(e.max_level, 1), "in_region": e.in_region,
                "group": e.group, "non_species": e.non_species, "slot_mapping_unreliable": e.t_start < self.slot_fix_s, "provenance": "model_opinion"}

    @staticmethod
    def _in_region_ok(e, mode):
        return mode == "any" or (e.in_region is not False if mode == "true" else e.in_region is False)

    @staticmethod
    def _class_ok(non_species, mode):
        """non_species=false (default): real species only; true: only the non-species classes (Engine, Dog...); any: both."""
        return mode == "any" or (bool(non_species) is (mode == "true"))

    @staticmethod
    def _group_ok(group, mode):
        """group=any: every group; bio: the five biological groups (not 'mechanical'); else exactly that group."""
        return mode == "any" or (group in GROUPS_BIO if mode == "bio" else group == mode)

    def _event_rows(self, q, now):
        """The filtered, newest-first events shared by /events and /density."""
        since, until = self._range(q, now=now)
        sp = p_str("species", q["species"]).strip().lower() if "species" in q else None
        mc = p_float("min_conf", q["min_conf"], 0, 1) if "min_conf" in q else 0.0
        beam = p_int("beam", q["beam"], 1, 8) if "beam" in q else None
        mode = p_enum("in_region", q["in_region"], ("true", "false", "any")) if "in_region" in q else "true"
        nsm = p_enum("non_species", q["non_species"], ("true", "false", "any")) if "non_species" in q else "false"
        gm = p_enum("group", q["group"], GROUP_MODES) if "group" in q else "any"
        am = p_enum("artifact", q["artifact"], ARTIFACT_MODES) if "artifact" in q else "any"
        rows = [e for e in self.dets.snapshot() if since <= e.t_start < until and self._in_region_ok(e, mode) and self._class_ok(e.non_species, nsm) and e.max_conf >= mc
                and self._group_ok(e.group, gm) and (beam is None or beam in e.beams) and (sp is None or sp in (e.key, e.common.lower(), e.scientific.lower()))]
        if am != "any":                                                  # advisory filter: `none` leaves out the flagged events, `only` keeps just them; default keeps all
            rows = [e for e in rows if self._flagged(e, now) is (am == "only")]
        rows.sort(key=lambda e: (-e.t_start, e.key))
        return since, until, mode, nsm, gm, am, rows

    def api_events(self, q):
        now = self.clock()
        limit = p_int("limit", q["limit"], 1, 500) if "limit" in q else 100
        offset = p_int("offset", q["offset"], 0, 100000) if "offset" in q else 0
        since, until, mode, nsm, gm, am, rows = self._event_rows(q, now)
        az = self.calibration()["beam_azimuth_deg"]
        win = self.dets.cutoff_s
        return {"since_s": since, "until_s": until, "in_region": mode, "non_species": nsm, "group": gm, "artifact": am, "total": len(rows), "limit": limit, "offset": offset,
                "frame": self.calibration()["frame"], "slot_mapping_reliable_from_s": self.slot_fix_s, "window_start_s": round(win, 3),
                "truncated_by_window": since < win, "events": [self._event_json(e, az, now) for e in rows[offset:offset + limit]]}

    def api_event(self, q):
        """One event by id, with its reports (the per-beam confidence / level trace). 404 when it is not in the in-memory window."""
        if "id" not in q:
            raise Bad("id", "required: the id of an event from /api/detections/events")
        eid = q["id"]
        if not EVENT_ID_RE.fullmatch(eid):
            raise Bad("id", "expected an event id such as 1792940400000-common-virtuoso-katydid")
        hit = next((e for e in self.dets.snapshot() if self._event_id(e) == eid), None)
        if hit is None:
            raise NotFound("no such event in the in-memory window")
        az = self.calibration()["beam_azimuth_deg"]
        reports = [{"t_s": round(t, 3), "beam": b, "beam_azimuth_deg": az[b - 1], "confidence": round(c, 4), "level_dbfs": None if lv is None else round(lv, 1)}
                   for t, b, c, lv in sorted(hit.trace, key=lambda r: (r[0], r[1]))]
        return {"event": self._event_json(hit, az, self.clock()), "frame": self.calibration()["frame"], "slot_mapping_reliable_from_s": self.slot_fix_s,
                "trace_truncated": hit.reports > len(hit.trace), "reports": reports}

    def api_density(self, q):
        """Events aggregated per (time bucket, species, best beam): the exact counts behind a density view of a range too large to list.
        Built from the in-memory events only (the same set /events lists), so rows <= events in memory; buckets start at `since`."""
        now = self.clock()
        since, until, mode, nsm, gm, am, rows = self._event_rows(q, now)
        bucket = p_int("bucket_s", q["bucket_s"], 60, 86400) if "bucket_s" in q else min(86400, max(60, int(-(-((until - since) / 200.0) // 60)) * 60))
        cells = {}
        for e in rows:
            i = int((e.t_start - since) // bucket)
            c = cells.setdefault((i, e.key, e.best_beam), {"t_s": since + i * bucket, "species": e.common, "scientific": e.scientific, "group": e.group,
                                                          "best_beam": e.best_beam, "events": 0, "max_confidence": 0.0})
            c["events"] += 1
            c["max_confidence"] = max(c["max_confidence"], e.max_conf)
        out = [dict(c, max_confidence=round(c["max_confidence"], 4)) for _k, c in sorted(cells.items(), key=lambda kv: kv[0])]
        win = self.dets.cutoff_s
        starts = [e.t_start for e in rows]
        return {"since_s": since, "until_s": until, "bucket_s": bucket, "in_region": mode, "non_species": nsm, "group": gm, "artifact": am, "total": len(rows),
                "n_rows": len(out), "data_first_s": round(min(starts), 3) if starts else None, "data_last_s": round(max(starts), 3) if starts else None,
                "frame": self.calibration()["frame"], "slot_mapping_reliable_from_s": self.slot_fix_s, "window_start_s": round(win, 3),
                "truncated_by_window": since < win, "rows": out}

    def api_species(self, q):
        now = self.clock()
        since, until = self._range(q, now=now)
        mode = p_enum("in_region", q["in_region"], ("true", "false", "any")) if "in_region" in q else "true"
        nsm = p_enum("non_species", q["non_species"], ("true", "false", "any")) if "non_species" in q else "false"
        gm = p_enum("group", q["group"], GROUP_MODES) if "group" in q else "any"
        am = p_enum("artifact", q["artifact"], ARTIFACT_MODES) if "artifact" in q else "any"
        floor = ceil_hour(self.dets.cutoff_s) if self.dets.cutoff_s > 0 else 0
        acc = {}
        flagged_total = unscored = 0

        def slot(key, common, sci, group, how):
            return acc.setdefault(key, {"species": common, "scientific": sci, "group": group, "events": 0, "reports": 0, "first_seen_s": None,
                                        "last_seen_s": None, "bounds_exact": True, "max_confidence": 0.0, "in_region": None,
                                        "non_species": False, "beam_events": [0] * 8, "hourly": {}, "how": how,
                                        "artifact_events": 0})

        def reg(s, in_region):
            if in_region is True or (in_region is False and s["in_region"] is None):
                s["in_region"] = in_region
        for e in self.dets.snapshot():
            if e.t_start < max(since, floor) or e.t_start >= until or not self._in_region_ok(e, mode)                     or not self._class_ok(e.non_species, nsm):
                continue
            flagged = self._flagged(e, now)
            flagged_total += flagged                                    # every flagged event matching the other filters, whatever the artifact mode hides
            if (am == "none" and flagged) or (am == "only" and not flagged):
                continue
            s = slot(e.key, e.common, e.scientific, e.group, e.how)
            s["artifact_events"] += flagged
            s["events"] += 1
            s["reports"] += e.reports
            s["first_seen_s"] = e.t_start if s["first_seen_s"] is None else min(s["first_seen_s"], e.t_start)
            s["last_seen_s"] = e.t_end if s["last_seen_s"] is None else max(s["last_seen_s"], e.t_end)
            s["max_confidence"] = max(s["max_confidence"], e.max_conf)
            s["non_species"] = s["non_species"] or e.non_species
            reg(s, e.in_region)
            for b in e.beams:
                s["beam_events"][b - 1] += 1
            h = hour_of(e.t_start)
            s["hourly"][h] = s["hourly"].get(h, 0) + 1
        for (kind, h), rec in sorted(self.rollups.recs.items()):
            if kind != "detections" or h >= floor or not (hour_of(since) <= h < until):
                continue
            for r in rec.get("species", []):
                ir = r.get("in_region")
                if is_human_label(r["common"], r.get("scientific")):     # a rollup written before the voices were hidden
                    continue
                grp, how, non = self.taxa.classify(r.get("scientific") or "", r["common"], r.get("non_species"))
                if not (mode == "any" or (ir is not False if mode == "true" else ir is False))                         or not self._class_ok(non, nsm):
                    continue
                key = (r.get("scientific") or r["common"]).strip().lower()
                if grp in GATE_GROUPS:
                    unscored += r["events"]                              # hourly rollups carry no flags: these events were never scored, so `artifact=none` cannot leave them out
                if am == "only":
                    continue
                s = slot(key, r["common"], r.get("scientific") or "", grp, how)
                s["events"] += r["events"]
                s["reports"] += r["reports"]
                s["bounds_exact"] = False
                s["first_seen_s"] = h if s["first_seen_s"] is None else min(s["first_seen_s"], h)
                s["last_seen_s"] = h + HOUR_S - 1 if s["last_seen_s"] is None else max(s["last_seen_s"], h + HOUR_S - 1)
                s["max_confidence"] = max(s["max_confidence"], r["max_confidence"])
                s["non_species"] = s["non_species"] or non
                reg(s, ir)
                for i, c in enumerate(r["beam_events"]):
                    s["beam_events"][i] += c
                s["hourly"][h] = s["hourly"].get(h, 0) + r["events"]
        everyone = sorted(acc.values(), key=lambda s: (-s["events"], s["species"]))
        groups = [{"group": g, "species": sum(1 for s in everyone if s["group"] == g), "events": sum(s["events"] for s in everyone if s["group"] == g),
                   "reports": sum(s["reports"] for s in everyone if s["group"] == g)} for g in GROUP_ORDER]     # before the group filter: the chip counts
        unc = [{"species": s["species"], "scientific": s["scientific"], "events": s["events"]} for s in everyone if s["how"] == "unmapped"]
        out = []
        for s in everyone:
            if not self._group_ok(s["group"], gm):
                continue
            s.pop("how")
            hourly = [{"hour_s": h, "events": c} for h, c in sorted(s.pop("hourly").items())]
            s["max_confidence"] = round(s["max_confidence"], 4)
            s["first_seen_s"], s["last_seen_s"] = round(s["first_seen_s"], 3), round(s["last_seen_s"], 3)
            s["hourly_counts"] = hourly
            s["active_hours"] = len(hourly)
            s["events_per_active_hour"] = round(s["events"] / len(hourly), 1)
            s["chorus"] = s["events_per_active_hour"] >= CHORUS_MIN_PER_ACTIVE_HOUR and s["active_hours"] >= CHORUS_MIN_ACTIVE_HOURS
            out.append(s)
        return {"since_s": since, "until_s": until, "in_region": mode, "non_species": nsm, "group": gm, "artifact": am, "frame": self.calibration()["frame"], "species": out,
                "events_total": sum(s["events"] for s in out), "artifact_events_total": flagged_total, "artifact_unscored_events": unscored, "groups": groups, "unclassified": unc[:UNCLASSIFIED_LIST_MAX],
                "unclassified_total": sum(u["events"] for u in unc)}

    def _live(self):
        now = self.clock()
        newest = self.beams.newest_t
        recs = self.beams.last_recs(now, 60.0)
        cal = self.calibration()
        bands = []
        for k in range(8):
            if recs:
                ps = [sum(db_to_p(r[2][b * 8 + k]) for r in recs) / len(recs) for b in range(8)]
                pm = [p_to_db(p) for p in ps]
                ring = p_to_db(sum(ps) / 8.0)
                lead = pm.index(max(pm)) + 1 if max(pm) > -150 else None
                bands.append({"band": k, "lo_hz": EDGES_HZ[k][0], "hi_hz": EDGES_HZ[k][1],
                              "power_mean_db": [r2(x) for x in pm], "rel_ring_db": [r2(x - ring) for x in pm],
                              "ring_power_mean_db": r2(ring), "leader_beam": lead, "contrast_db": r2(max(pm) - min(pm))})
            else:
                bands.append({"band": k, "lo_hz": EDGES_HZ[k][0], "hi_hz": EDGES_HZ[k][1], "power_mean_db": None, "rel_ring_db": None,
                              "ring_power_mean_db": None, "leader_beam": None, "contrast_db": None})
        return {"window_s": 60, "windows": len(recs), "newest_age_s": None if newest is None else round(max(0.0, now - newest), 1),
                "edges_hz": EDGES_HZ, "beam_azimuth_deg": cal["beam_azimuth_deg"], "frame": cal["frame"], "frame_kind": cal["frame_kind"], "calibrated": cal["calibrated"],
                "bands": bands, "zero_frac_mean": round(sum(r[1] for r in recs) / len(recs), 4) if recs else None,
                "note": "A persistent offset that stays with one beam number when the pattern is rotated is a channel gain difference, not a direction. Angles are DSP-frame fallback values until a calibration is stored."}

    def api_beams_live(self, q):
        return self._live()

    def _band(self, q):
        return p_int("band", q["band"], 0, 7) if "band" in q else 5

    def api_beams_stats(self, q):
        now = self.clock()
        since, until = self._range(q, now=now)
        band = self._band(q)
        rows = list(self.beams.hourly(since, until, self.rollups.recs).values())
        az = self.calibration()["beam_azimuth_deg"]
        n = sum(r["n"] for r in rows)
        out = {"band": band, "band_lo_hz": EDGES_HZ[band][0], "band_hi_hz": EDGES_HZ[band][1], "steerable": band not in NOT_STEERABLE_BANDS, "since_s": since, "until_s": until,
               "granularity": "whole hours overlapping the range",
               "effective_since_s": hour_of(since), "effective_until_s": ceil_hour(until), "frame": self.calibration()["frame"], "windows": n, "hours": len(rows),
               "contrast_min_db": MIN_CONTRAST_DB, "leader_band_hz": [EDGES_HZ[CONTRAST_BANDS[0]][0], EDGES_HZ[CONTRAST_BANDS[1]][1]],
               "windows_with_contrast": sum(r["nc"] for r in rows), "zero_frac_mean": None, "ring_power_mean_db": None, "beams": [],
               "hourly": [], "note": "Relative-to-ring is each beam's long-run power-mean minus the mean power of all 8 beams."}
        if not n:
            return out
        out["zero_frac_mean"] = round(sum(r["zf_mean"] * r["n"] for r in rows) / n, 4)
        ps = [sum(r["n"] * db_to_p(r["power_db"][b][band]) for r in rows) / n for b in range(8)]
        ring = sum(ps) / 8.0
        out["ring_power_mean_db"] = r2(p_to_db(ring))
        nc = out["windows_with_contrast"]
        for b in range(8):
            rel = (sum((r["rel_mean"][b] or 0.0) * r["nc"] for r in rows if r["rel_mean"]) / nc) if nc else None
            out["beams"].append({"beam": b + 1, "azimuth_deg": az[b], "power_mean_db": r2(p_to_db(ps[b])),
                                 "max_db": r2(max(r["max_db"][b][band] for r in rows)), "rel_ring_db": r2(p_to_db(ps[b]) - p_to_db(ring)),
                                 "leader_share": None if not nc else round(sum(r["lead"][b] for r in rows) / nc, 4),
                                 "mean_rel_ring_db_2_8khz": r2(rel)})
        for r in rows:
            hp = [db_to_p(r["power_db"][b][band]) for b in range(8)]
            hr = p_to_db(sum(hp) / 8.0)
            out["hourly"].append({"hour_s": r["hour"], "windows": r["n"], "zero_frac_mean": round(r["zf_mean"], 4),
                                  "power_mean_db": [r2(r["power_db"][b][band]) for b in range(8)],
                                  "rel_ring_db": [r2(r["power_db"][b][band] - hr) for b in range(8)],
                                  "contrast_windows": r["nc"],
                                  "leader_share": None if not r["nc"] else [round(c / r["nc"], 4) for c in r["lead"]]})
        return out

    def api_beams_heatmap(self, q):
        now = self.clock()
        since, until = self._range(q, now=now)
        band = self._band(q)
        bucket = p_int("bucket_s", q["bucket_s"], 10, 86400) if "bucket_s" in q else 300
        t0 = int(since // bucket) * bucket
        nb = int(-(-(until - t0) // bucket))
        if nb > 2000:
            raise Bad("bucket_s", "range / bucket_s would give %d buckets; the limit is 2000" % nb)
        ps = [[0.0] * nb for _ in range(8)]
        cnt = [0] * nb
        if bucket % HOUR_S == 0:
            basis = "hourly"
            for h, r in self.beams.hourly(since, until, self.rollups.recs).items():
                i = int((h - t0) // bucket)
                if 0 <= i < nb:
                    cnt[i] += r["n"]
                    for b in range(8):
                        ps[b][i] += r["n"] * db_to_p(r["power_db"][b][band])
        else:
            basis = "fine"
            if since < self.beams.cutoff_s - 1:
                raise Bad("since", "buckets under 3600 s need the fine window (since >= %d); use bucket_s as a multiple of 3600 for older ranges"
                          % int(self.beams.cutoff_s))

            def build():
                c2, p2 = [0] * nb, [[0.0] * nb for _ in range(8)]
                for t, _zf, a in self.beams.window_recs(since, until):
                    i = int((t - t0) // bucket)
                    if 0 <= i < nb:
                        c2[i] += 1
                        for b in range(8):
                            p2[b][i] += 10.0 ** (a[b * 8 + band] / 10.0)
                return c2, p2
            cnt, ps = self.cached(("hm", since, until, band, bucket, self.beams.newest_t), 10, build)
        vals = [[None if not cnt[i] else r2(p_to_db(ps[b][i] / cnt[i])) for i in range(nb)] for b in range(8)]
        return {"band": band, "band_lo_hz": EDGES_HZ[band][0], "band_hi_hz": EDGES_HZ[band][1], "since_s": since, "until_s": until,
                "steerable": band not in NOT_STEERABLE_BANDS, "bucket_s": bucket, "t0_s": t0, "n_buckets": nb, "basis": basis, "frame": self.calibration()["frame"],
                "slot_mapping_reliable_from_s": self.slot_fix_s,
                "beam_azimuth_deg": self.calibration()["beam_azimuth_deg"], "windows": sum(cnt), "values_power_mean_db": vals}

    def api_system(self, q):
        now = self.clock()
        since = p_time("since", q["since"]) if "since" in q else now - 86400
        if since >= now or now - since > MAX_RANGE_S:
            raise Bad("since", "since must be in the past and within 90 days")
        with self.stream.lock:
            samples = [s for s in self.stream.samples if s[0] >= since]
        step = max(1, -(-len(samples) // 720))
        pts = [{"t_s": round(s[0], 1), "loss_pct": s[1], "gaps": s[2], "pps": s[3], "state": s[4]} for s in samples[::step]]
        mean = round(sum(s[1] for s in samples) / len(samples), 3) if samples else None
        hourly = self.stream.hourly(since, now + HOUR_S, self.rollups.recs)
        board = {}
        for n in ("info", "dsps", "monitor", "health", "network"):
            board[n] = self.board.snap(n) if self.board.enabled else {"ok": False, "body": None, "error": "board_disabled", "t": None}
        info, dsps, mon, health, net = (board[n].get("body") or {} for n in ("info", "dsps", "monitor", "health", "network"))
        hb = (health.get("entries") or [{}])[0]
        with self.board.lock:
            temps = [t for t in self.board.temps if t[0] >= since]
        tstep = max(1, -(-len(temps) // 720))
        an, _ = self.read_json_file("analysis_status")
        plugins = []
        for n, p in sorted(((an or {}).get("plugins") or {}).items()):
            if isinstance(p, dict):
                plugins.append({"name": n, "enabled": p.get("enabled"), "records": p.get("records"), "errors": p.get("errors"),
                                "dropped": p.get("dropped"), "last_error": p.get("last_error"),
                                "last_t_age_s": round(max(0.0, now - p["last_t"]), 1) if is_num(p.get("last_t")) else None})
        mem_free = hb.get("memfree_kb")
        return {"since_s": since, "now_s": round(now, 3),
                "stream": {"samples": pts, "n_samples": len(samples), "loss_pct_mean": mean,
                           "latest": None if not samples else {"t_s": samples[-1][0], "loss_pct": samples[-1][1], "gaps": samples[-1][2],
                                                                "state": samples[-1][4]},
                           "hourly": hourly,
                           "note": "Loss of 1-2 % of stream packets is a known steady condition; read it as a trend, not an alarm."},
                "board": {"reachable": any(board[n].get("ok") for n in board), "api_version": info.get("api_version"),
                          "uptime_s": info.get("uptime_s"), "errors": {n: board[n].get("error") for n in board if board[n].get("error")},
                          "temperatures": {"local_c": hb.get("temp_local_c"), "remote_c": hb.get("temp_remote_c"),
                                           "history": [{"t_s": round(t[0], 1), "local_c": t[1], "remote_c": t[2]} for t in temps[::tstep]]},
                          "memory": {"memfree_kb": mem_free, "fwlog_free_kb": hb.get("fwlog_free_kb"),
                                     "note": "memfree_kb is MemFree: it excludes the page cache, so a small value does not mean the board is short of memory."},
                          "load": hb.get("load"), "network": None if not net else {"interface": net.get("interface"), "per_s": net.get("per_s")},
                          "monitor": None if not mon else {"count": len(mon.get("samples") or []), "cells_expected": mon.get("cells_expected"),
                                                          "stale": (mon.get("file") or {}).get("stale"),
                                                          "samples": [{"dsp": s.get("dsp"), "slot": s.get("slot"), "mean_square_raw": s.get("mean_square"),
                                                                       "peak_raw": s.get("peak")} for s in (mon.get("samples") or [])],
                                                          "note": "Unit and scale of mean_square and peak are UNKNOWN; raw integers, never converted or labelled in dB."},
                          "dsps": [{"dsp": d.get("dsp"), "role": d.get("role"), "observed_version": d.get("observed_version")}
                                   for d in (dsps.get("dsps") or [])]},
                "pipeline": {"plugins": plugins, "analysis_rx": (an or {}).get("rx")}}

    def api_spectrum(self, q, peer="", authenticated=False):
        """GET /api/spectrum. `peer` (the TCP peer address) only keys the per-client rate limit; `authenticated` (request_authenticated) decides access.
        """
        tier = p_enum("tier", q.get("tier", "standard"), tuple(SPEC_TIERS))
        beam = p_int("beam", q.get("beam", "0"), 0, 8)
        since = p_int("since", q.get("since", "0"), 0, 999999999)
        rev = p_int("rev", q.get("rev", "0"), 0, 999999999)
        compare = p_enum("compare", q.get("compare", "false"), ("false", "true")) == "true"
        band = p_int("band", q.get("band", str(SPEC_BAND_DEFAULT)), 0, 7)
        cap = SPEC_TIERS[tier]["max_limit"]
        limit = p_int("limit", q.get("limit", str(cap)), 1, cap)
        feed = self.spectrum
        if feed.enabled:
            ok, why = spectrum_client_allowed(authenticated, tier, feed.detail_enabled)
            if why == "spectrum_auth_required":
                return feed.auth_required(tier, beam)
            if not ok:
                raise Refuse(403, why, "Detail tier disabled", "the detail tier needs the server flag --spectrum-detail")
        if not feed.limiter.allow(norm_peer(peer), self.clock()):
            raise Refuse(429, "rate_limited", "Too many requests", "at most %g requests/s per client address (burst %g)" % (SPEC_REQ_RATE, SPEC_REQ_BURST),
                         extra={"Retry-After": "1"})
        return feed.snapshot(tier, beam, since, rev, compare, band, limit)

    def api_calibration(self, q):
        return self.calibration()


ROUTES = {"/api/status": ("api_status", ()), "/api/detections/events": ("api_events", ("since", "until", "species", "min_conf", "beam", "in_region", "non_species", "group", "artifact", "limit", "offset")),
          "/api/detections/event": ("api_event", ("id",)),
          "/api/detections/density": ("api_density", ("since", "until", "species", "min_conf", "beam", "in_region", "non_species", "group", "artifact", "bucket_s")),
          "/api/detections/species": ("api_species", ("since", "until", "in_region", "non_species", "group", "artifact")), "/api/beams/live": ("api_beams_live", ()),
          "/api/beams/stats": ("api_beams_stats", ("since", "until", "band")), "/api/beams/heatmap": ("api_beams_heatmap", ("since", "until", "band", "bucket_s")),
          "/api/system": ("api_system", ("since",)), "/api/spectrum": ("api_spectrum", ("tier", "beam", "since", "rev", "compare", "band", "limit")), "/api/calibration": ("api_calibration", ())}


class Handler(BaseHTTPRequestHandler):
    server_version = "bma-ui"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    auth_via = auth_subject = auth_user = None                           # how this request authenticated (session | cookie | basic | tailnet), the CSRF subject, the tailnet login
    authenticated = False                                                # True only once this request passed the login (never for an open, login-less server); set per request by _handle

    def setup(self):
        self.timeout = self.server.conn_timeout                          # per-recv socket timeout: an idle or stalled client is dropped
        super().setup()

    def log_message(self, fmt, *args):                                   # method + path (no query) + code only
        pass

    def _send(self, code, body, ctype, extra=None, head=False, csp=None):
        if self.server.auth.enabled:                                     # a login-protected dashboard is never cached
            extra = dict(extra or {}, **{"Cache-Control": "no-store"})
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", csp or CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head:
            self.wfile.write(body)
        if code >= 400:                                                  # method + path (no query) + code; successes are not logged
            self.server.app.log("%s %s %d" % (self.command, urllib.parse.urlsplit(self.path).path[:120], code))

    def _problem(self, code, name, title, detail, head=False, param=None, extra=None):
        body = {"type": "urn:bma-ui:problem:" + name, "title": title, "status": code, "code": name, "detail": detail}
        if param:
            body["param"] = param
        h = {"Cache-Control": "no-store"}
        h.update(extra or {})
        self._send(code, json.dumps(body, separators=(",", ":")).encode(), "application/problem+json", h, head)

    def peer_ip(self):
        """The TCP peer address of this connection. Never taken from a header (X-Forwarded-* is not trusted)."""
        return self.client_address[0]

    def _auth_gate(self, head):
        """True when the request was answered here (401, 429 or the login redirect); False when it is authenticated."""
        parts = urllib.parse.urlsplit(self.path)
        au = self.server.auth
        ident = None
        if au.allowed_users is not None:                                 # --tailnet-identity: believed from a loopback peer only (auth.identity_login)
            ident = A.identity_login(self.client_address[0], self.headers.get_all(A.IDENTITY_HEADER), au.allowed_users.users())
        r = au.check(parts.path, parts.query, self.headers.get("Authorization"), self.headers.get_all("Cookie"), self.client_address[0],
                     identity=ident, secure=self._secure_cookie())
        if r.kind == "ok":
            self.auth_via, self.auth_subject, self.auth_user = r.via, r.subject, r.user
            return False
        if r.kind == "login":
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", r.set_cookie)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            return True
        if r.kind == "locked":
            self._problem(429, "too_many_attempts", "Too many failed attempts", "this address is locked out; retry later", head,
                          extra={"Retry-After": str(r.retry_after)})
        elif (parts.path in ("/", "/index.html") and not parts.query and self.command in ("GET", "HEAD") and not au.require_identity
              and self.headers.get("Sec-Fetch-Mode") == "navigate" and self.headers.get("Sec-Fetch-Dest") == "document"):
            self.send_response(302)                                      # a browser opening the dashboard: to the sign-in page, not a Basic prompt
            self.send_header("Location", "/login")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            mode = self.headers.get("Sec-Fetch-Mode")                    # a browser's own password prompt would hang the page's fetch(): challenge navigations and non-browsers only
            self._problem(401, "unauthorized", "Authentication required", "send the dashboard token (HTTP Basic password, or open /?token=...)", head,
                          extra=None if mode and mode != "navigate" else {"WWW-Authenticate": 'Basic realm="%s", charset="UTF-8"' % A.REALM})
        return True

    # ---- sign-in page, sessions, CSRF, passkeys (ui-http 1.7.0; ui/AUTH.md)
    def _secure_cookie(self):
        """Mark the cookie Secure when the browser sees HTTPS: this server speaks TLS itself, or a loopback peer (`tailscale serve`) says it terminated TLS."""
        return self.server.tls is not None or (A.peer_is_loopback(self.client_address[0]) and (self.headers.get("X-Forwarded-Proto") or "").strip().lower() == "https")

    def _read_body(self, limit):
        """The request body (at most `limit` bytes) or None after answering 400. The connection is closed when a body is refused unread."""
        try:
            n = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            n = -1
        if n < 0 or n > limit or self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            self._problem(400, "invalid_request", "Invalid request", "a Content-Length of at most %d bytes is required" % limit)
            return None
        return self.rfile.read(n)

    def _origin_ok(self):
        """Same-origin check for a state-changing POST: Sec-Fetch-Site, when present, must be same-origin; an Origin header, when present, must be this host (Origin: null only together with Sec-Fetch-Site: same-origin)."""
        sfs = self.headers.get("Sec-Fetch-Site")
        if sfs is not None and sfs != "same-origin":
            return False
        org = self.headers.get("Origin")
        if org is None:
            return True
        if org == "null":                                   # the Referrer-Policy: no-referrer pages make browsers send Origin: null on their OWN form posts and fetches:
            return sfs == "same-origin"                     # trusted only with Sec-Fetch-Site: same-origin (a forbidden header, a page script cannot set it)
        host = (self.headers.get("Host") or "").strip().lower()
        u = urllib.parse.urlsplit(org)
        return u.scheme in ("http", "https") and u.netloc.lower() == host and not u.path.strip("/")

    def _login_page(self, code, message="", head=False):
        au = self.server.auth
        ident_only = au.require_identity
        pk = self.server.passkeys is not None and not ident_only
        e = lambda t: t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        parts = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
                 '<title>BMA dashboard - sign in</title><link rel="stylesheet" href="/static/login.css"></head><body><main><h1>BMA dashboard</h1>']
        if message:
            parts.append('<p class="msg" role="alert">%s</p>' % e(message))
        if ident_only:
            parts.append("<p>This dashboard accepts only tailnet identity: open it through the tailnet address.</p>")
        else:
            parts.append('<form method="post" action="/login" autocomplete="off"><input type="hidden" name="csrf" value="%s">'
                         '<label>Access token<input type="password" name="token" autocomplete="current-password" required></label>'
                         '<button type="submit">Sign in</button></form>' % e(au.login_csrf()))
        if pk:
            parts.append('<hr><button id="passkey-login" type="button" hidden>Sign in with a passkey</button>')
        parts.append('<script src="/static/login.js" defer></script></main></body></html>' if pk else "</main></body></html>")
        self._send(code, "".join(parts).encode("utf-8"), "text/html; charset=utf-8", {"Cache-Control": "no-store"}, head, csp=LOGIN_CSP)

    def _redirect(self, location, cookie=None):
        self.send_response(303)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()

    def _locked(self, head=False):
        left = self.server.auth.lock_left(self.client_address[0])
        if left:
            self._problem(429, "too_many_attempts", "Too many failed attempts", "this address is locked out; retry later", head, extra={"Retry-After": str(left)})
        return bool(left)

    def _public_route(self, head):
        """Routes that must work BEFORE a login. True when answered here."""
        path = urllib.parse.urlsplit(self.path).path
        au = self.server.auth
        if path in PUBLIC_ASSETS and self.command in ("GET", "HEAD"):
            name, ctype = ASSETS[path]
            data = self.server.assets.get(name)
            if data is None or "?" in self.path:
                self._problem(404, "not_found", "Not found", "no such resource", head)
            else:
                self._send(200, data, ctype, {"Cache-Control": "no-cache"}, head)
            return True
        if path == "/login":
            if self.command not in ("GET", "HEAD", "POST"):
                self._problem(405, "method_not_allowed", "Method not allowed", "GET, HEAD and POST only", head, extra={"Allow": "GET, HEAD, POST"})
            elif self.command != "POST":
                self._login_page(200, head=head)
            else:
                self._post_login()
            return True
        if path in ("/api/passkeys/login/options", "/api/passkeys/login/verify"):
            if self.server.passkeys is None or au.require_identity:
                self._problem(404, "not_found", "Not found", "no such resource", head)
            elif self.command != "POST":
                self._problem(405, "method_not_allowed", "Method not allowed", "POST only", head, extra={"Allow": "POST"})
            else:
                self._passkey_login(path.endswith("/verify"))
            return True
        return False

    def _post_login(self):
        au, ip = self.server.auth, self.client_address[0]
        if au.require_identity:
            return self._problem(403, "login_disabled", "Login disabled", "this dashboard accepts tailnet identity only")
        if self._locked():
            self.close_connection = True
            return
        body = self._read_body(MAX_FORM_BYTES)
        if body is None:
            return
        if not self._origin_ok():
            return self._login_page(403, "The request did not come from this page.")
        try:
            f = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=8).items()}
        except (UnicodeError, ValueError):
            return self._problem(400, "invalid_request", "Invalid request", "the form body is malformed")
        if not au.login_csrf_ok(f.get("csrf")):
            return self._login_page(403, "The sign-in page expired: try again.")
        if au.token_ok(f.get("token")):
            au._clear(ip)
            cookie, _sid = au.new_session(secure=self._secure_cookie())
            return self._redirect("/", cookie)
        au.record_failure(ip)                                            # the same counter and lockout as Basic and ?token=
        self._login_page(401, "Wrong token.")

    def _json_body(self):
        raw = self._read_body(MAX_JSON_BYTES)
        if raw is None:
            return None
        try:
            d = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeError, ValueError):
            d = None
        if not isinstance(d, dict):
            self._problem(400, "invalid_request", "Invalid request", "a JSON object body is required")
            return None
        return d

    def _csrf_ok(self, form=None):
        if not self._origin_ok():
            return False
        return self.server.auth.csrf_ok(self.auth_subject, self.headers.get("X-CSRF-Token") or (form or {}).get("csrf"))

    def _passkey_login(self, verify):
        au, ip = self.server.auth, self.client_address[0]
        if self._locked():
            return
        if not self._origin_ok():
            return self._problem(403, "csrf_failed", "Request refused", "cross-origin request")
        body = self._json_body()
        if body is None:
            return
        pk = self.server.passkeys
        if not verify:
            return self._send(200, json.dumps(pk.login_options(), separators=(",", ":")).encode(), "application/json")
        try:
            pk.login(body)
        except PK.PasskeyError:
            au.record_failure(ip)
            self.server.app.log("passkey login refused ip=%s" % ip)
            return self._problem(401, "unauthorized", "Authentication required", "the passkey was refused")
        au._clear(ip)
        cookie, _sid = au.new_session(secure=self._secure_cookie())
        self._send(200, b'{"ok":true}', "application/json", {"Set-Cookie": cookie})

    def _session_route(self, head):
        """Routes that need a login and are not part of the read-only data API. True when answered here."""
        au, path, pk = self.server.auth, urllib.parse.urlsplit(self.path).path, self.server.passkeys
        if path == "/api/session":
            if self.command not in ("GET", "HEAD"):
                self._problem(405, "method_not_allowed", "Method not allowed", "GET and HEAD only", head, extra={"Allow": "GET, HEAD"})
                return True
            out = {"authenticated": True, "via": self.auth_via, "user": self.auth_user, "csrf": au.csrf_token(self.auth_subject),
                   "passkeys": {"enabled": pk is not None, "count": len(pk.store.list()) if pk is not None else 0},
                   "tailnet_identity": au.allowed_users is not None}
            self._send(200, json.dumps(out, separators=(",", ":")).encode(), "application/json", {"Cache-Control": "no-store"}, head)
            return True
        if path == "/logout":
            if self.command != "POST":
                self._problem(405, "method_not_allowed", "Method not allowed", "POST only", head, extra={"Allow": "POST"})
                return True
            body = self._read_body(MAX_FORM_BYTES)
            if body is None:
                return True
            try:
                form = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode("utf-8"), max_num_fields=8).items()}
            except (UnicodeError, ValueError):
                form = {}
            if not self._csrf_ok(form):
                self._problem(403, "csrf_failed", "Request refused", "missing or wrong CSRF token (see GET /api/session)")
                return True
            au.end_session(self.headers.get_all("Cookie"))
            self._redirect("/login", au.clear_cookie_header(secure=self._secure_cookie()))
            return True
        if path.startswith("/api/review/"):
            return self._review(path, head)
        if path == "/api/passkeys" or path.startswith("/api/passkeys/"):
            if pk is None:
                self._problem(404, "not_found", "Not found", "no such resource", head)
                return True
            if path == "/api/passkeys" and self.command in ("GET", "HEAD"):
                rows = [{"id": c["id"], "label": c.get("label", ""), "created_s": c.get("created"), "last_used_s": c.get("last_used")} for c in pk.store.list()]
                self._send(200, json.dumps({"passkeys": rows}, separators=(",", ":")).encode(), "application/json", {"Cache-Control": "no-store"}, head)
                return True
            if path in ("/api/passkeys/register/options", "/api/passkeys/register/verify", "/api/passkeys/delete") and self.command == "POST":
                body = self._json_body()
                if body is None:
                    return True
                if not self._csrf_ok():
                    self._problem(403, "csrf_failed", "Request refused", "missing or wrong CSRF token (see GET /api/session)")
                    return True
                try:
                    if path.endswith("/options"):
                        out = pk.registration_options(self.auth_subject)
                    elif path.endswith("/verify"):
                        out = {"ok": True, "id": pk.register(body, self.auth_subject)}
                    else:
                        out = {"ok": pk.store.remove(str(body.get("id", "")))}
                except PK.PasskeyError as ex:
                    self._problem(400, "invalid_request", "Invalid request", str(ex))
                    return True
                self._send(200, json.dumps(out, separators=(",", ":")).encode(), "application/json", {"Cache-Control": "no-store"})
                return True
            self._problem(405, "method_not_allowed", "Method not allowed", "see ui-http 1.7.0", head)
            return True
        return False

    def _review(self, path, head):
        """GET /api/review/next|stats|clip|audio, POST /api/review/verdict|skip (ui-http 1.9.0).  Only reached with a login. True when answered."""
        rv = self.server.app.review
        gets, posts = ("/api/review/next", "/api/review/stats", "/api/review/clip", "/api/review/audio"), ("/api/review/verdict", "/api/review/skip")
        if rv is None or path not in gets + posts:
            self._problem(404, "not_found", "Not found", "no such resource", head)
            return True
        if (path in gets and self.command not in ("GET", "HEAD")) or (path in posts and self.command != "POST"):
            self._problem(405, "method_not_allowed", "Method not allowed", "see ui-http 1.9.0", head, extra={"Allow": "GET, HEAD" if path in gets else "POST"})
            return True
        try:
            if path in posts:
                body = self._json_body()
                if body is None:
                    return True
                if not self._csrf_ok(body):
                    self._problem(403, "csrf_failed", "Request refused", "missing or wrong CSRF token (see GET /api/session)")
                    return True
                out = rv.verdict(body.get("event_id"), body.get("verdict"), body.get("wrong_reason")) if path.endswith("verdict") else rv.skip(body.get("event_id"))
            elif path.endswith("clip"):
                q = parse_query(urllib.parse.urlsplit(self.path).query, {"id"})
                self._send(200, rv.clip_png(q.get("id")), "image/png", {"Cache-Control": "no-store"}, head)
                return True
            elif path.endswith("audio"):
                q = parse_query(urllib.parse.urlsplit(self.path).query, {"id"})
                code, ctype, data, extra = rv.clip_audio(q.get("id"), self.headers.get("Range"))
                self._send(code, data, ctype, dict(extra, **{"Cache-Control": "no-store"}), head)
                return True
            else:
                out = rv.next() if path.endswith("next") else rv.stats()
        except RV.ReviewError as ex:
            self._problem(ex.status, ex.code, ex.title, ex.detail, head, param=ex.param, extra=ex.extra)
            return True
        except Bad as ex:
            self._problem(422, "invalid_parameter", "Invalid parameter", ex.msg, head, param=ex.param)
            return True
        self._send(200, json.dumps(out, separators=(",", ":"), allow_nan=False).encode(), "application/json", {"Cache-Control": "no-store"}, head)
        return True

    def _live(self, parts, head):
        """GET /api/v2/live: the spectrum rows as BMW1 frames over a WebSocket (ui-live 1.0.0). Order of refusals: method, query, feed off, no login, wrong Origin,
        not an upgrade, connection cap. Same login as /api/spectrum: an open server is not an authenticated one."""
        if head:
            return self._problem(405, "method_not_allowed", "Method not allowed", "GET only", head, extra={"Allow": "GET"})
        try:
            parse_query(parts.query, set())
        except Bad as ex:
            return self._problem(422, "invalid_parameter", "Invalid parameter", ex.msg, head, param=ex.param)
        feed = self.server.app.spectrum
        if not feed.enabled:
            return self._problem(404, "not_found", "Not found", "no such resource", head)
        if not request_authenticated(self):
            return self._problem(401, "unauthorized", "Authentication required", "the live spectrum needs a login", head)
        origin = self.headers.get("Origin")
        if origin is not None and not WS.origin_ok(origin, self.headers.get("Host")):
            return self._problem(403, "origin_not_allowed", "Origin not allowed", "the Origin header does not name this host", head)
        bad = WS.upgrade_problem(self.headers.get)
        if bad:
            return self._problem(bad[0], bad[1], bad[2], bad[3], head, extra=bad[4] or None)
        if not self.server.ws_gate.acquire():
            return self._problem(503, "too_many_connections", "Too many connections", "at most %d live sockets are open" % WS.MAX_CONNECTIONS, head, extra={"Retry-After": "5"})
        try:
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", WS.accept_key(self.headers.get("Sec-WebSocket-Key").strip()))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.flush()
            self.close_connection = True
            WS.serve(self.connection, feed)
        finally:
            self.server.ws_gate.release()

    def _handle(self, head):
        self.authenticated = False                                       # per request: one connection serves many
        self.auth_via = self.auth_subject = self.auth_user = None
        host = (self.headers.get("Host") or "").strip().lower()
        if not self.server.host_ok(host):
            return self._problem(403, "host_not_allowed", "Host not allowed", "the Host header is not in the allowlist", head)
        if urllib.parse.urlsplit(self.path).path == "/healthz":           # credential-free liveness: the body says nothing but the time (ui-http 1.10.0)
            if self.command not in ("GET", "HEAD"):
                return self._problem(405, "method_not_allowed", "Method not allowed", "GET and HEAD only", head, extra={"Allow": "GET, HEAD"})
            return self._send(200, json.dumps({"ok": True, "ts": int(time.time())}, separators=(",", ":")).encode(), "application/json", {"Cache-Control": "no-store"}, head)
        if self.server.auth.enabled:                                     # AUTH: every route, every method, after the Host check
            if self._public_route(head):                                 # the sign-in page and its own files: the only routes without a login
                return
            if self._auth_gate(head):
                return
            self.authenticated = True                                    # the ONE flag feature gates read (ui/AUTH.md, 'For feature gates')
            if self._session_route(head):                                # /api/session, /logout, /api/passkeys...: need a login, handled here
                return
        if self.command not in ("GET", "HEAD"):
            return self._problem(405, "method_not_allowed", "Method not allowed", "this service is read-only: GET and HEAD only", head,
                                 extra={"Allow": "GET, HEAD"})
        parts = urllib.parse.urlsplit(self.path)
        path = parts.path
        if path in ASSETS:
            if parts.query:
                return self._problem(404, "not_found", "Not found", "no such resource", head)
            name, ctype = ASSETS[path]
            data = self.server.assets.get(name)
            if data is None:
                return self._problem(404, "not_found", "Not found", "no such resource", head)
            return self._send(200, data, ctype, {"Cache-Control": "no-cache"}, head)
        if path == "/api/v2/live" and self.server.next_assets and WS:     # --ui-next only (ui-live 1.0.0); without the flag it is a plain 404 below
            return self._live(parts, head)
        nxt = self.server.next_assets.get(path) if self.server.next_assets else None
        if nxt is not None:                                              # --ui-next only; without the flag every /next/ path is a plain 404 below
            if parts.query and path not in NEXT_INDEX:
                return self._problem(404, "not_found", "Not found", "no such resource", head)
            return self._send(200, nxt[0], nxt[1], {"Cache-Control": "no-cache"}, head, csp=NEXT_CSP)
        route = ROUTES.get(path)
        if route is None:
            return self._problem(404, "not_found", "Not found", "no such resource", head)
        try:
            q = parse_query(parts.query, set(route[1]))
            if route[0] == "api_spectrum":
                payload = self.server.app.api_spectrum(q, self.peer_ip(), request_authenticated(self))
            else:
                payload = getattr(self.server.app, route[0])(q)
        except Refuse as ex:
            return self._problem(ex.code, ex.name, ex.title, ex.detail, head, extra=ex.extra)
        except NotFound as ex:
            return self._problem(404, "not_found", "Not found", ex.msg, head)
        except Bad as ex:
            return self._problem(422, "invalid_parameter", "Invalid parameter", ex.msg, head, param=ex.param,
                                 extra={"Content-Language": "en"})
        except Exception as ex:                                          # noqa: BLE001 - never leak internals
            self.server.app.log("500 %s %s" % (path, type(ex).__name__))
            return self._problem(500, "internal_error", "Internal error", "the request could not be served", head)
        try:
            data = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
        except ValueError:
            data = json.dumps(clean(payload), separators=(",", ":"), allow_nan=False).encode()
        self._send(200, data, "application/json", {"Cache-Control": "no-store"}, head)

    def do_GET(self):
        self._handle(False)

    def do_HEAD(self):
        self._handle(True)

    def do_POST(self):
        self._handle(False)

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = do_POST


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, app, allowed_hosts, assets, max_conns=MAX_CONNECTIONS, conn_timeout=CONN_TIMEOUT_S, allow_ip_hosts=False, auth=None, tls=None, passkeys=None, next_assets=None):
        super().__init__(addr, Handler)
        self.ws_gate = WS.Gate() if WS else None                         # open live sockets (ui/ws.py MAX_CONNECTIONS)
        self.next_assets = next_assets or {}                           # /next/* routes (load_next_assets), empty = the next dashboard is off
        self.passkeys = passkeys                                         # passkeys.Passkeys or None (feature off)
        self.auth, self.tls = auth or A.OPEN, tls                        # A.Authenticator, or A.OPEN (.enabled False) when no login is configured; ssl.SSLContext or None (plain HTTP)
        self.app, self.allowed_hosts, self.assets, self.conn_timeout = app, allowed_hosts, assets, conn_timeout
        self.allow_ip_hosts = allow_ip_hosts
        self.slots = threading.BoundedSemaphore(max_conns)
        self.refused = 0
        self.reject_slots = threading.BoundedSemaphore(MAX_REJECTS)

    def host_ok(self, host):
        """The Host header must be an allowlisted name:port, or - only with --allow-ip-hosts - an IP literal on our port."""
        return host in self.allowed_hosts or (self.allow_ip_hosts and is_ip_literal_host(host, self.server_address[1]))

    def _reject(self, request):
        """Answer 503 and drain the request off the socket. Runs off the accept thread: an idle client costs this thread up to 1 s, never the accept loop."""
        try:
            try:
                request.settimeout(1.0)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\nRetry-After: 5\r\n\r\n")
                request.shutdown(socket.SHUT_WR)
                request.recv(65536)                                      # take the client's request off the socket: closing with unread data sends a RST that can eat the 503 on the client
            except OSError:
                pass
            self.shutdown_request(request)
        finally:
            self.reject_slots.release()

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], ConnectionError):               # a client that left or was refused unread (aborted/reset): not worth a traceback
            return
        super().handle_error(request, client_address)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):                       # over the cap: answer 503 off the accept thread, hold no worker slot
            self.refused += 1
            if self.reject_slots.acquire(blocking=False):
                try:
                    threading.Thread(target=self._reject, args=(request,), daemon=True).start()
                    return
                except BaseException:
                    self.reject_slots.release()
            self.shutdown_request(request)                               # too many rejections in flight: just close
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            if self.tls is not None:                                     # TLS handshake in the worker thread, never in the accept loop
                try:
                    request.settimeout(self.conn_timeout)
                    request = self.tls.wrap_socket(request, server_side=True)
                except (OSError, ValueError):                            # ssl.SSLError is an OSError: a failed handshake just drops the connection
                    self.shutdown_request(request)
                    return
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def load_assets(static_dir=None):
    d = static_dir or os.path.join(HERE, "static")
    out = {}
    for name in {v[0] for v in ASSETS.values()}:
        try:
            with open(os.path.join(d, name), "rb") as f:
                out[name] = f.read()
        except OSError:
            pass
    return out


def next_static_dir():
    """The ui-next assets: <ui dir>/next (installed layout) or ../ui-next/static (repository layout); None when neither exists."""
    for d in (os.path.join(HERE, "next"), os.path.join(HERE, "..", "ui-next", "static")):
        if os.path.isdir(d):
            return d
    return None


def load_next_assets(static_dir=None):
    """-> {"/next/": (bytes, ctype), "/next/app.js": ..., ...} for the files of NEXT_FILES that exist."""
    d = static_dir or next_static_dir()
    out = {}
    for name, ctype in NEXT_FILES.items():
        try:
            with open(os.path.join(d, *name.split("/")), "rb") as f:
                data = f.read()
        except (OSError, TypeError):
            continue
        out["/next/" + name] = (data, ctype)
        if name == "index.html":
            out["/next/"] = (data, ctype)
    return out


def next_assets_missing(assets):
    """The /next/ paths of NEXT_FILES that `assets` (load_next_assets) does not hold, sorted."""
    return sorted("/next/" + n for n in NEXT_FILES if "/next/" + n not in assets)


def is_ip_literal_host(host, port):
    """True for a Host header that is an IPv4 or IPv6 literal on `port` ("192.0.2.5:48500", "[fd00::1]:48500").
    DNS rebinding needs the browser to send an attacker-controlled NAME as Host; an IP literal cannot be rebound, so a LAN
    dashboard can accept any literal (addresses change with DHCP) while names stay on the explicit allowlist."""
    host = host.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        if end < 0 or host[end + 1:] != ":%d" % port:
            return False
        lit = host[1:end]
    else:
        lit, sep, p = host.rpartition(":")
        if not sep or p != str(port) or ":" in lit:
            return False
    if "%" in lit:                       # a zone id is not valid in a Host header
        return False
    try:
        ipaddress.ip_address(lit)
    except ValueError:
        return False
    return True


def allowed_hosts_for(port, extra):
    names = ["localhost", "127.0.0.1"] + list(extra or [])
    out = set()
    for n in names:
        n = n.strip().lower()
        if n:
            out.add(f"{n}:{port}")
    return out


def parse_slot_fix(text):
    """ISO 8601 with offset (or Z), or a naive time read as the host's local time -> epoch seconds."""
    d = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return d.timestamp()


def build(args):
    board = Board(args.board_url, args.token_file, enabled=not args.no_board and bool(args.board_url))
    feed = SpectrumFeed(enabled=True, detail=args.spectrum_detail) if args.spectrum_port is not None else None
    return App(args.pipeline_root, args.reports_dir, args.rollup_dir, board, window_h=args.window_h, slot_fix_at=args.slot_fix_at, spectrum=feed, labels_dir=args.labels_dir,
               review_allow_no_clip=args.review_allow_no_clip, artifact_gate=not args.no_artifact_gate)


def data_dir(env=None):
    """<data-dir>: analysis/, logs/, reports/ live under it ($BMA_DATA_DIR, default ./data)."""
    return (os.environ if env is None else env).get("BMA_DATA_DIR") or "data"


DSPD_PORT = 8470          # the board-side service listens here (firmware constant)


def board_url_from_env(env=None):
    """http://<BMA_BOARD>:8470 when BMA_BOARD is an IP address; a MAC or nothing gives "" (no board panel)."""
    v = ((os.environ if env is None else env).get("BMA_BOARD") or "").strip()
    try:
        ip = ipaddress.ip_address(v)
    except ValueError:
        return ""
    return f"http://[{ip}]:{DSPD_PORT}" if ip.version == 6 else f"http://{ip}:{DSPD_PORT}"


def make_parser(env=None):
    env = os.environ if env is None else env
    data = data_dir(env)
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=48500)
    ap.add_argument("--allow-host", action="append", default=[], help="extra Host header name accepted (with the port)")
    ap.add_argument("--allow-ip-hosts", action="store_true", help="also accept any IPv4/IPv6 literal Host header on the listening port (for a LAN or tailnet bind; names still need --allow-host)")
    ap.add_argument("--pipeline-root", default=data, help="root holding analysis/, logs/ and the status files (default $BMA_DATA_DIR or ./data)")
    ap.add_argument("--labels-dir", default=None, help="verdict label store written by the Review tab (default <pipeline-root>/labels)")
    ap.add_argument("--review-allow-no-clip", action="store_true", help="Review queue also serves events without a stored clip (default: only events with a clip PNG and/or audio)")
    ap.add_argument("--reports-dir", default=os.path.join(data, "reports"), help="default <data-dir>/reports")
    ap.add_argument("--rollup-dir", default=os.path.join(data, "reports", "ui"), help="default <data-dir>/reports/ui")
    ap.add_argument("--board-url", default=board_url_from_env(env), help="dspd base URL (default http://$BMA_BOARD:8470 when BMA_BOARD is an IP address; empty = no board panel)")
    ap.add_argument("--token-file", default=env.get("BMA_DSPD_TOKEN_FILE", ""), help="file holding the dspd bearer token (default $BMA_DSPD_TOKEN_FILE; empty = no token)")
    ap.add_argument("--no-board", action="store_true")
    ap.add_argument("--window-h", type=float, default=48.0)
    ap.add_argument("--auth-token-file", default=None, metavar="PATH", help="file holding the dashboard login token (default <pipeline-root>/dashboard_token.txt); when it exists every request needs it")
    ap.add_argument("--no-auth", action="store_true", help="UNSAFE: serve without a login (needed to bind beyond loopback without a token file)")
    ap.add_argument("--tls-cert", default="", metavar="PEM", help="serve HTTPS with this certificate chain (with --tls-key)")
    ap.add_argument("--tls-key", default="", metavar="PEM", help="private key for --tls-cert")
    ap.add_argument("--tailnet-identity", action="store_true", help="also accept the Tailscale-User-Login header, but ONLY from a loopback peer (`tailscale serve`) and only for a login in --allowed-users")
    ap.add_argument("--allowed-users", default=None, metavar="PATH", help="with --tailnet-identity: file with one allowed Tailscale login per line (default <pipeline-root>/allowed_users.txt; missing or empty = nobody)")
    ap.add_argument("--require-identity", action="store_true", help="with --tailnet-identity: accept ONLY tailnet identity (no token, no Basic, no cookie, no passkey)")
    ap.add_argument("--passkey-origin", action="append", default=[], metavar="ORIGIN", help="enable passkeys (WebAuthn) for this exact origin, e.g. https://NAME.TAILNET-DOMAIN (https required; plain http only for localhost); repeatable, one host name")
    ap.add_argument("--passkey-file", default=None, metavar="PATH", help="passkey public keys (default <pipeline-root>/passkeys.json)")
    ap.add_argument("--slot-fix-at", type=parse_slot_fix, default=None, metavar="ISO8601",
                    help="detections before this time used the wrong beam-slot mapping (default 2026-10-25T21:47:00Z, which is 17:47 EDT on the capture host)")
    ap.add_argument("--spectrum-port", type=int, default=None, metavar="PORT",
                    help="receive spectrum rows (BMAS datagrams from pc/bma_spectrum.py) on 127.0.0.1:PORT only and serve them on GET /api/spectrum; off when absent")
    ap.add_argument("--ui-next", action="store_true", help="also serve the next dashboard under /next/ (default off; reads the same API, ui-next/static)")
    ap.add_argument("--no-artifact-gate", action="store_true", help="do not annotate insect / mechanical events with the advisory artifact_flags (contracts/specs/artifact-gate.md)")
    ap.add_argument("--spectrum-detail", action="store_true",
                    help="also allow the detail tier (50 rows/s, finer bands) for authenticated clients; needs --spectrum-port")
    return ap


def main(argv=None):
    ap = make_parser()
    args = ap.parse_args(argv)
    if not 1 <= args.window_h <= 168:
        ap.error("--window-h must be 1-168")
    if args.spectrum_detail and args.spectrum_port is None:
        ap.error("--spectrum-detail needs --spectrum-port")
    if args.spectrum_port is not None and not 1 <= args.spectrum_port <= 65535:
        ap.error("--spectrum-port must be 1-65535")
    next_assets = {}
    if args.ui_next:
        next_assets = load_next_assets()
        if WS is None:
            ap.error("--ui-next needs ui/ws.py next to bma_ui.py (the live socket), which could not be imported")
        missing = next_assets_missing(next_assets)
        if missing:
            ap.error("--ui-next: assets not found (%s); expected <ui dir>/next or ../ui-next/static" % ", ".join(missing))
    app = build(args)
    try:
        auth, tls, notices = A.configure(args.bind, args.pipeline_root, args.auth_token_file, args.no_auth, args.tls_cert, args.tls_key, log=app.log,
                                          tailnet_identity=args.tailnet_identity, allowed_users=args.allowed_users, require_identity=args.require_identity)
    except A.AuthConfigError as ex:
        ap.error(str(ex))
    passkeys = None
    if args.passkey_origin:
        if PK is None:
            ap.error("--passkey-origin needs ui/passkeys.py, which could not be imported")
        if auth is None:
            ap.error("--passkey-origin needs a login (registration happens only from an authenticated session)")
        for o in args.passkey_origin:
            why = PK.origin_problem(o)
            if why:
                ap.error("--passkey-origin %s: %s" % (o, why))
        try:
            passkeys = PK.Passkeys(args.passkey_file or os.path.join(args.pipeline_root, "passkeys.json"), args.passkey_origin)
        except PK.PasskeyError as ex:
            ap.error(str(ex))
    for n in notices:
        print(n, file=sys.stderr, flush=True)
    if args.spectrum_port is not None:
        app.spectrum.start(args.spectrum_port)
    srv = Server((args.bind, args.port), app, allowed_hosts_for(args.port, args.allow_host), load_assets(), allow_ip_hosts=args.allow_ip_hosts, auth=auth, tls=tls, passkeys=passkeys, next_assets=next_assets)
    print(f"bma_ui {UI_VERSION} listening on {'https' if tls else 'http'}://{args.bind}:{args.port}/ (login {'required' if auth else 'OFF'}; backfilling)", flush=True)

    def boot():
        try:
            app.start_backfill()
        except Exception as ex:                                          # noqa: BLE001 - the pollers must start regardless
            app.log("backfill failed: " + type(ex).__name__)
            app.backfill_s, app.backfill_done = 0.0, True
        print(f"backfill done in {app.backfill_s:.1f} s: {app.beams.size()} beam windows, {len(app.dets.snapshot())} events", flush=True)
        threading.Thread(target=app.run_loop, daemon=True).start()
        if app.board.enabled:
            threading.Thread(target=app.board.run, daemon=True).start()
    threading.Thread(target=boot, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
