#!/usr/bin/env python3
"""Mock backend for the next dashboard: no board, no analyzer, no recordings. Serves ui-next/static and a synthetic, deterministic API, so the front end
can be developed, screenshotted, measured and tested on its own. Stdlib + numpy (synth.py), GET/POST, loopback only by default.

    python mock_server.py [--port 48600] [--bind 127.0.0.1] [--results-dir DIR]

Endpoints (the proposed v2 surface; the three marked "benchmark" exist for the measurement tools only):
  GET  /                              the dashboard shell (static/index.html); /engine/* DSP, renderer and audition engine; /bench/* benchmark pages
  GET  /api/v2/live                   WebSocket, binary frames 10/s: 'BMW1' header + 9 beams x 46 uint8 levels (beam 0 = mean); text frames carry events
  GET  /api/v2/live.sse?fmt=b64|json  the same rows as Server-Sent Events (benchmark)
  GET  /api/spectrum?since=N          current-shape JSON rows, one beam + levels[8] (benchmark)
  GET  /api/v2/status                 one strip + health JSON
  GET  /api/v2/clips                  clip list: metadata, synthetic truth boxes, the claimed box; /api/v2/clips/<id>/beams.wav is 8-channel 48 kHz PCM16
  POST /api/v2/review/verdict | /skip in-memory label store (never written anywhere); the verdict record is echoed
  POST /api/v2/listen/consent         -> {token, expires_s}   one listener at a time, 10 minutes
  GET  /api/v2/listen?token=&beams=   WebSocket: 'UILAB1' header frame, then interleaved PCM16 chunks of 20 ms paced in real time (synthetic scene);
                                      403 without a live consent token, 409 while another listener is connected
  POST /api/selftest?browser=  GET /api/selftests     cross-engine self-test results (static/selftest.html), kept in memory and optionally in --results-dir
  GET  /__reload?m=<mtime>            long-poll that answers when a file under static/ changes (hot reload; the page loads dev-reload.js with ?dev)
  POST /__report  GET /__reports      feature-probe results posted by bench/static/features.js
The audio is synthetic (synth.py): tones, sweeps and noise. This server has no login: it refuses a non-loopback bind without --allow-lan."""
import argparse
import base64
import hashlib
import ipaddress
import json
import os
import select
import struct
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import synth  # noqa: E402

STATIC = os.path.join(HERE, "static")
BENCH = os.path.join(HERE, "bench", "static")
BINS, BEAMS, HOP_S = 46, 9, 0.1
FS, DUR = synth.FS, 6.0
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".json": "application/json",
         ".png": "image/png", ".svg": "image/svg+xml"}
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; worker-src 'self'; "
       "object-src 'none'; base-uri 'none'; form-action 'none'")
SPECIES = [("Alpha Wren", "Aus a"), ("Pine Finch", "Fus p"), ("Brook Thrush", "Tus b"), ("Reed Warbler", "Rus w"), ("Zed Owl", "Zus z"), ("Marsh Pipit", "Mus p")]
CONSENT_TTL_S = 600
T0 = time.time() - 65.0                                          # the synthetic stream already has a minute of history at start
LOCK = threading.Lock()
CONSENT = {}                                                    # token -> expiry (epoch s)
LISTENERS = [0]
VERDICTS = {}
SKIPPED = []
SELFTESTS = {}
REPORTS = []
RESULTS_DIR = [None]
LISTEN_STATS = {"resync_s": 0.2, "late_max_ms": 0.0, "resyncs": 0}
CLIP_AUDIO = {}                                                 # id -> cached WAV bytes
_LIVE_BASE = [None]


# ------------------------------------------------------------------ spectrum rows (WebSocket / SSE / polling)
_IDX = np.arange(BINS)


def level_row(beam, k):
    """One synthetic spectrum row (BINS uint8, 0.5 dB steps from -100 dB) for beam 0..8 at row counter k: floor, three lines, a sweep, a burst per 4 s on one beam."""
    v = 30 + 6 * np.random.default_rng((k << 4) + beam).random(BINS) + 0.7 * beam
    v[[5, 17, 35]] += 28
    t = k * HOP_S
    v[np.abs(_IDX - int(((t * 0.07) % 1) * BINS)) < 2] += 40
    if beam != 0 and (int(t // 4) % 8) == beam - 1 and (t % 4) < 0.8:
        m = (_IDX > BINS * 0.45) & (_IDX < BINS * 0.8)
        v[m] += 55 * np.maximum(0.0, 1 - np.abs(_IDX[m] - BINS * 0.62) / (BINS * 0.2))
    return np.minimum(255, v.astype(np.int64)).astype(np.uint8).tobytes()


def frame(k):
    """'BMW1' binary frame: magic(4) type(1)=1 beams(1) bins(u16) seq(u32) t_us(u64) flags(u16) reserved(u16) then beams*bins uint8."""
    t_us = int((T0 + k * HOP_S) * 1e6)
    return b"BMW1" + struct.pack("<BBHIQHH", 1, BEAMS, BINS, k & 0xFFFFFFFF, t_us, 0, 0) + b"".join(level_row(b, k) for b in range(BEAMS))


# ------------------------------------------------------------------ clips: 8-beam synthetic scenes with truth boxes
def _test_signal_beams(kind):
    """Eight beams of a test signal (all beams share it with the ring gains/delays of synth.beams, plus independent -60 dBFS noise)."""
    n = int(DUR * FS)
    if kind == "chirp":
        s = synth.burst(synth.chirp(500, 12000, 4.0, 0.2), 1.0, DUR)
    elif kind == "stack":
        s = synth.burst(synth.harmonic_stack(440, 4.0, 10, 0.2), 1.0, DUR)
    elif kind == "modulation":
        s = synth.burst(synth.am_tone(3000, 20, 0.8, 1.5, 0.2), 0.3, DUR) + synth.burst(synth.fm_tone(5000, 8, 600, 1.5, 0.2), 2.2, DUR) + synth.clicks(0.35, DUR, 0.1, 0.4)
    elif kind == "lines":
        s = synth.unit_lines(DUR, -40.0) + synth.burst(synth.chirp(3500, 6000, 0.3, 0.1) * np.hanning(int(0.3 * FS)), 2.0, DUR)
    else:
        raise KeyError(kind)
    out = []
    for b in range(8):
        ang = 2 * np.pi * b / 8
        g = max(0.05, np.cos(ang / 2) ** 2)
        out.append(np.roll(s, int(round(1.5 * np.sin(ang)))) * g + synth.noise(DUR, -60.0, 900 + b))
    return np.clip(np.array(out), -1, 1)[:, :n]


def _build_clips():
    """Test signals first (ids scene-a ... lines, test=True), then 24 detection clips c001..c024 whose claim is one truth box of the scene."""
    truth = synth.scene(DUR, 7)[1]
    claimable = [t for t in truth if t["kind"] in ("bird", "insect", "frog")]
    now = time.time()
    clips = [
        dict(id="scene-a", title="Test scene A: bird-like sweeps, an insect, a frog-like pulse train, 100 Hz hum", test=True, truth=truth, make=lambda: synth.beams(DUR, 8, 7)),
        dict(id="scene-b", title="Test scene B (other noise seed)", test=True, truth=truth, make=lambda: synth.beams(DUR, 8, 11)),
        dict(id="chirp", title="Test: linear chirp 500 Hz to 12 kHz (1 s to 5 s)", test=True, truth=[dict(kind="chirp", t0=1.0, t1=5.0, f0=500, f1=12000)], make=lambda: _test_signal_beams("chirp")),
        dict(id="stack", title="Test: harmonic stack, f0 440 Hz, 10 partials", test=True, truth=[dict(kind="harmonic", t0=1.0, t1=5.0, f0=440, f1=4400)], make=lambda: _test_signal_beams("stack")),
        dict(id="modulation", title="Test: AM 3 kHz (20 Hz), FM 5 kHz (8 Hz, +-600 Hz), 0.35 s clicks", test=True,
             truth=[dict(kind="am", t0=0.3, t1=1.8, f0=3000, f1=3000), dict(kind="fm", t0=2.2, t1=3.7, f0=4400, f1=5600), dict(kind="clicks", t0=0.1, t1=DUR, f0=500, f1=15000)], make=lambda: _test_signal_beams("modulation")),
        dict(id="lines", title="Test: the unit's tonal lines (11.5, 12, 15.3-15.6 kHz) at -40 dBFS and one sweep", test=True,
             truth=[dict(kind="unit-lines", t0=0, t1=DUR, f0=11510, f1=15557), dict(kind="sweep", t0=2.0, t1=2.3, f0=3500, f1=6000)], make=lambda: _test_signal_beams("lines")),
    ]
    for i in range(1, 25):
        sp = SPECIES[i % len(SPECIES)]
        claim = claimable[(i - 1) % len(claimable)]
        conf = round(0.3 + (i * 37 % 65) / 100.0, 2)
        beam = 1 + i % 8
        clips.append(dict(id="c%03d" % i, title="%s | beam %d | %d%%" % (sp[0], beam, round(conf * 100)), test=False, truth=truth, t=round(now - 20 - 600 * (i - 1), 1), beam=beam,
                          common=sp[0], scientific=sp[1], confidence=conf, in_region=i % 7 != 0, retention_h_left=round(24 - i * 0.9, 1),
                          claim={k: claim[k] for k in ("t0", "t1", "f0", "f1")}, make=(lambda s=100 + i: synth.beams(DUR, 8, s))))
    return clips


CLIPS = _build_clips()
CLIP_BY_ID = {c["id"]: c for c in CLIPS}


def clip_meta(c):
    m = dict(id=c["id"], title=c["title"], dur_s=DUR, fs=FS, beams=8, test=c["test"], truth=c["truth"], audio=True)
    if not c["test"]:
        m.update({k: c[k] for k in ("t", "beam", "common", "scientific", "confidence", "in_region", "retention_h_left", "claim")})
    return m


def clip_wav(cid):
    with LOCK:
        if cid not in CLIP_AUDIO:
            CLIP_AUDIO[cid] = synth.to_wav16(CLIP_BY_ID[cid]["make"]())
        return CLIP_AUDIO[cid]


def live_base():
    with LOCK:
        if _LIVE_BASE[0] is None:
            _LIVE_BASE[0] = synth.beams(DUR, 8, 21)
        return _LIVE_BASE[0]


# ------------------------------------------------------------------ HTTP
class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):             # a browser closing a WebSocket or SSE connection raises OSError in the handler thread: not worth a traceback
        if not issubclass(sys.exc_info()[0] or Exception, OSError):
            super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mock"
    sys_version = ""

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, separators=(",", ":")).encode(), "application/json")

    def _ws(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or self.headers.get("Upgrade", "").lower() != "websocket":
            return None
        acc = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", acc)
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        return self.connection

    @staticmethod
    def _client_gone(s):
        """True when the browser closed the WebSocket (a close frame, or the TCP connection ended): a stream that only sends would never notice."""
        if not select.select([s], [], [], 0)[0]:
            return False
        try:
            data = s.recv(512)
        except OSError:
            return True
        return not data or (data[0] & 0x0F) == 8

    @staticmethod
    def _wsframe(op, payload):
        n = len(payload)
        h = bytes([0x80 | op]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n) if n < 65536 else bytes([127]) + struct.pack(">Q", n))
        return h + payload

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        p, q = u.path, urllib.parse.parse_qs(u.query)
        if p == "/api/v2/live":
            return self._live_ws()
        if p == "/api/v2/live.sse":
            return self._live_sse((q.get("fmt") or ["b64"])[0])
        if p == "/api/spectrum":
            return self._spectrum(int((q.get("since") or ["0"])[0]))
        if p == "/api/v2/status":
            up = time.time() - T0
            return self._json({"t": round(time.time(), 1), "state": "live", "uptime_s": int(up), "rx_loss_pct": 0.22, "plugins": {n: "on" for n in ("dsp", "tags", "clap", "birds", "perch", "bursts", "indices")},
                               "detections_1h": 41, "clips": {"stored_24h": 18, "audio": True}, "board": {"state": "ok", "temp_c": 41.0}, "listening": LISTENERS[0] > 0})
        if p == "/api/v2/clips":
            return self._json({"clips": [clip_meta(c) for c in CLIPS]})
        if p.startswith("/api/v2/clips/") and p.endswith("/beams.wav"):
            cid = p.split("/")[4]
            if cid not in CLIP_BY_ID:
                return self._json({"error": "no such clip"}, 404)
            return self._send(200, clip_wav(cid), "audio/wav")
        if p == "/api/v2/listen":
            return self._listen_ws((q.get("token") or [""])[0], (q.get("beams") or ["0,1,2,3,4,5,6,7"])[0])
        if p == "/api/selftests":
            return self._json(SELFTESTS)
        if p == "/__reports":
            return self._json({"reports": REPORTS})
        if p == "/__reload":
            return self._reload(float(q.get("m", ["0"])[0]))
        return self._static(p)

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(min(n, 1 << 20)) or b"{}")
        except ValueError:
            return self._json({"error": "bad json"}, 400)
        if u.path == "/__report":
            REPORTS.append(body)
            return self._json({"ok": True, "n": len(REPORTS)})
        if u.path == "/api/selftest":
            name = (urllib.parse.parse_qs(u.query).get("browser") or ["unknown"])[0][:40]
            SELFTESTS[name] = body
            if RESULTS_DIR[0]:
                with open(os.path.join(RESULTS_DIR[0], "selftest_%s.json" % "".join(c for c in name if c.isalnum() or c in "-_")), "w", encoding="utf-8", newline="\n") as f:
                    json.dump(body, f, indent=1)
            return self._json({"ok": True})
        if u.path == "/api/v2/listen/consent":
            tok = base64.urlsafe_b64encode(os.urandom(12)).decode()
            CONSENT[tok] = time.time() + CONSENT_TTL_S
            return self._json({"token": tok, "expires_s": CONSENT_TTL_S})
        if u.path == "/api/v2/review/verdict":
            with LOCK:
                VERDICTS[body.get("id", "")] = body.get("verdict")
            return self._json({"ok": True, "done": len(VERDICTS), "echo": body.get("record")})
        if u.path == "/api/v2/review/skip":
            with LOCK:
                SKIPPED.append(body.get("id", ""))
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)

    # ---- streams
    def _live_ws(self):
        s = self._ws()
        if s is None:
            return self._send(400, b"websocket required", "text/plain")
        k = int((time.time() - T0) / HOP_S)
        try:
            for kk in range(max(0, k - 599), k):                          # backlog: the last minute, so a fresh page is not an empty waterfall
                s.sendall(self._wsframe(2, frame(kk)))
            while not self._client_gone(s):
                s.sendall(self._wsframe(2, frame(k)))
                if k % 50 == 0:
                    s.sendall(self._wsframe(1, json.dumps({"k": "event", "common": SPECIES[(k // 50) % len(SPECIES)][0], "beam": 1 + (k // 50) % 8, "confidence": 0.62}).encode()))
                k += 1
                time.sleep(max(0.0, T0 + k * HOP_S - time.time()))
        except OSError:
            return

    def _live_sse(self, fmt):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        k = int((time.time() - T0) / HOP_S)
        try:
            while True:
                f = frame(k)
                body = base64.b64encode(f).decode() if fmt == "b64" else json.dumps({"seq": k, "t_us": int((T0 + k * HOP_S) * 1e6), "beams": [list(f[24 + b * BINS:24 + (b + 1) * BINS]) for b in range(BEAMS)]}, separators=(",", ":"))
                self.wfile.write(("data: " + body + "\n\n").encode())
                self.wfile.flush()
                k += 1
                time.sleep(max(0.0, T0 + k * HOP_S - time.time()))
        except OSError:
            return

    def _spectrum(self, since):
        newest = int((time.time() - T0) / HOP_S)
        first = max(since + 1, newest - 599, 0) if since else max(newest - 59, 0)
        rows = [{"idx": k, "t_s": round(T0 + k * HOP_S, 1), "flags": 0, "gated": False, "bins": base64.b64encode(level_row(0, k)).decode(), "levels": [60 + b for b in range(8)]} for k in range(first, newest + 1)]
        return self._json({"spectrum_version": "1.0.0", "state": "live", "tier": "standard", "beam": 0, "now_s": round(time.time(), 1), "n_bins": BINS, "db_floor": -100.0, "db_step": 0.5,
                           "rows_per_s": 10, "hop_ms": 100, "rev": 1, "newest_idx": newest, "next_since": newest, "rows_missed": 0, "newest_age_s": 0.1, "rows": rows})

    def _listen_ws(self, tok, beams_arg):
        """Consent-gated synthetic listening: nothing here reads or stores real audio, and the server keeps no copy of what it sends."""
        if CONSENT.get(tok, 0) < time.time():
            return self._send(403, b"no listening consent for this session", "text/plain")
        with LOCK:
            if LISTENERS[0] >= 1:
                return self._send(409, b"another listener is connected", "text/plain")
            LISTENERS[0] += 1
        try:
            s = self._ws()
            if s is None:
                return self._send(400, b"websocket required", "text/plain")
            beams = [int(x) for x in beams_arg.split(",") if x.strip().isdigit() and int(x) < 8] or list(range(8))
            base = live_base()[beams]
            chunk = 960                                                   # 20 ms
            s.sendall(self._wsframe(2, b"UILAB1\0\0" + struct.pack("<IHH", FS, len(beams), 16)))
            n, pos, sent, t0 = base.shape[1], 0, 0, time.monotonic()
            while CONSENT.get(tok, 0) > time.time() and not self._client_gone(s):
                blk = base[:, np.arange(pos, pos + chunk) % n]
                s.sendall(self._wsframe(2, np.clip(np.round(blk.T * 32767.0), -32768, 32767).astype("<i2").tobytes()))
                pos, sent = (pos + chunk) % n, sent + chunk
                delay = t0 + sent / FS - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                elif delay < -LISTEN_STATS["resync_s"]:                  # a source that fell behind drops time instead of bursting it
                    LISTEN_STATS["late_max_ms"] = max(LISTEN_STATS["late_max_ms"], -delay * 1000.0)
                    LISTEN_STATS["resyncs"] += 1
                    t0 = time.monotonic() - sent / FS
        except OSError:
            return
        finally:
            with LOCK:
                LISTENERS[0] -= 1

    def _reload(self, m0):
        end = time.time() + 25
        m = 0.0
        while time.time() < end:
            m = max((os.path.getmtime(os.path.join(d, f)) for d, _, fs in os.walk(STATIC) for f in fs), default=0.0)
            if m0 == 0 or m > m0 + 1e-6:
                break
            time.sleep(0.4)
        return self._json({"m": m})

    def _static(self, p):
        root, rel = (BENCH, p[len("/bench/"):]) if p.startswith("/bench/") else (STATIC, "index.html" if p in ("/", "") else p.lstrip("/"))
        f = os.path.normpath(os.path.join(root, rel))
        if not f.startswith(root + os.sep) or not os.path.isfile(f):
            return self._json({"error": "not found"}, 404)
        with open(f, "rb") as fh:
            body = fh.read()
        self._send(200, body, TYPES.get(os.path.splitext(f)[1], "application/octet-stream"))


def is_loopback(host):
    try:
        return host in ("localhost",) or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--port", type=int, default=48600)
    ap.add_argument("--bind", default="127.0.0.1", help="default loopback; a LAN or tailnet address needs --allow-lan")
    ap.add_argument("--allow-lan", action="store_true", help="serve beyond loopback (for a phone on the tailnet): synthetic data only, NO login, never run next to real data")
    ap.add_argument("--results-dir", default=None, help="write browser self-test results here (outside the repo)")
    a = ap.parse_args(argv)
    if not is_loopback(a.bind) and not a.allow_lan:
        print("refusing to bind %s: this mock has no login; pass --allow-lan to serve its synthetic data to other devices" % a.bind, file=sys.stderr)
        return 2
    RESULTS_DIR[0] = a.results_dir
    if a.results_dir:
        os.makedirs(a.results_dir, exist_ok=True)
    live_base()                                                           # computed once so the first listening byte is not delayed
    srv = Server((a.bind, a.port), Handler)
    print("mock dashboard (synthetic data, no login) on http://%s:%d/" % (a.bind, srv.server_address[1]), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
