#!/usr/bin/env python3
"""Tests of the mock backend (mock_server.py) over real sockets, no browser: the clip API, the 8-channel WAV, the spectrum WebSocket, the consent-gated listening
stream (header, rate, one listener at a time, a closed client frees the slot, expiry), the in-memory review endpoints, static serving, the loopback guard and the
response headers.   python test_mock_server.py     (PASS/FAIL lines, 'N checks, M failure(s)', exit 1 on a failure)"""
import base64
import json
import os
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import mock_server as M  # noqa: E402

n, fails = 0, []


def check(name, ok, detail=""):
    global n
    n += 1
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  [%s]" % detail), flush=True)
    if not ok:
        fails.append(name)


srv = M.Server(("127.0.0.1", 0), M.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]
BASE = "http://127.0.0.1:%d" % PORT


def get(path, data=None, method=None):
    req = urllib.request.Request(BASE + path, data=data, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


class Conn:
    """A client WebSocket connection: the socket plus the bytes read past the handshake."""

    def __init__(self, sock, rest):
        self.sock, self.rest = sock, rest

    def settimeout(self, t):
        self.sock.settimeout(t)


def ws_open(path):
    """Minimal RFC 6455 client: -> (Conn, status line), positioned after the response headers."""
    s = socket.create_connection(("127.0.0.1", PORT), timeout=10)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall(("GET %s HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (path, key)).encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(4096)
    head, rest = buf.split(b"\r\n\r\n", 1)
    return Conn(s, rest), head.split(b"\r\n")[0].decode()


def ws_frame(c):
    """-> (opcode, payload) of the next server frame."""
    def need(k):
        while len(c.rest) < k:
            chunk = c.sock.recv(1 << 16)
            if not chunk:
                raise ConnectionError("closed")
            c.rest += chunk
        out, c.rest = c.rest[:k], c.rest[k:]
        return out
    b0, b1 = need(2)
    ln = b1 & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", need(2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", need(8))[0]
    return b0 & 0x0F, need(ln)


def ws_close(c):
    c.sock.sendall(bytes([0x88, 0x80]) + os.urandom(4))          # a masked, empty close frame
    c.sock.close()


# ---------------------------------------------------------------- clips
code, body, hdr = get("/api/v2/clips")
clips = json.loads(body)["clips"]
real, test = [c for c in clips if not c["test"]], [c for c in clips if c["test"]]
check("clips: 30 in all, 6 test signals and 24 detections", (code, len(clips), len(test), len(real)) == (200, 30, 6, 24), str((code, len(clips), len(test), len(real))))
check("clips: test signals first, then c001..c024 in order", [c["id"] for c in clips[:6]] == ["scene-a", "scene-b", "chirp", "stack", "modulation", "lines"] and [c["id"] for c in real] == ["c%03d" % i for i in range(1, 25)])
check("clips: every detection has a claim box that is one of its truth boxes, a confidence in [0.3, 0.95) and a beam 1..8",
      all(c["claim"] in [{k: t[k] for k in ("t0", "t1", "f0", "f1")} for t in c["truth"]] and 0.3 <= c["confidence"] < 0.95 and 1 <= c["beam"] <= 8 for c in real))
check("clips: test signals carry truth but no claim (positive twin of the previous check)", all(c["truth"] and "claim" not in c for c in test))
code, wav, hdr = get("/api/v2/clips/chirp/beams.wav")
riff, fmt = wav[:4], struct.unpack("<HHIIHH", wav[20:36])
frames = (len(wav) - 44) // (8 * 2)
check("beams.wav: RIFF, PCM16, 8 channels, 48000 Hz, 288000 frames (6 s)", (code, riff, fmt[0], fmt[1], fmt[2], fmt[5], frames) == (200, b"RIFF", 1, 8, 48000, 16, 288000), str((code, riff, fmt, frames)))
pcm = struct.unpack("<%dh" % (8 * 4800), wav[44 + 8 * 2 * 48000:44 + 8 * 2 * 48000 + 8 * 4800 * 2])
check("beams.wav: the chirp window (1 s to 1.1 s) is not silence on beam 1 (peak above 1000 of 32768)", max(abs(v) for v in pcm[0::8]) > 1000, str(max(abs(v) for v in pcm[0::8])))
check("beams.wav: an unknown clip is 404", get("/api/v2/clips/nope/beams.wav")[0] == 404)
check("clips: the same clip is byte-identical on a second request (deterministic synthetic audio)", get("/api/v2/clips/chirp/beams.wav")[1] == wav)

# ---------------------------------------------------------------- spectrum WebSocket
s, status = ws_open("/api/v2/live")
check("live: the WebSocket upgrade is accepted (101)", status.startswith("HTTP/1.1 101"), status)
first = ws_frame(s)
check("live: frames are binary 'BMW1', type 1, 9 beams x 46 bins, 24 + 414 = 438 bytes", first[0] == 2 and first[1][:4] == b"BMW1" and first[1][4] == 1 and first[1][5] == 9 and struct.unpack("<H", first[1][6:8])[0] == 46 and len(first[1]) == 438, str(len(first[1])))
got = 1
while got < 600:
    ws_frame(s)
    got += 1
check("live: a fresh connection gets the 599-frame backlog at once (600 frames read well under 3 s)", got == 600)
seqs = []
for _ in range(3):
    op, pl = ws_frame(s)
    if op == 2:
        seqs.append(struct.unpack("<I", pl[8:12])[0])
check("live: sequence numbers of consecutive frames advance by one", len(seqs) == 3 and seqs[1] - seqs[0] == 1 and seqs[2] - seqs[1] == 1, str(seqs))
ws_close(s)

# ---------------------------------------------------------------- listening: consent, header, rate, one listener, free on close
check("listen: no token is refused (403)", get("/api/v2/listen")[0] == 403)
check("listen: a wrong token is refused (403)", get("/api/v2/listen?token=abc")[0] == 403)
code, body, _ = get("/api/v2/listen/consent", b"{}", "POST")
tok = json.loads(body)["token"]
check("consent: POST returns a token and a 600 s lifetime", code == 200 and len(tok) >= 16 and json.loads(body)["expires_s"] == 600, body.decode()[:80])
s, status = ws_open("/api/v2/listen?token=%s&beams=0,1,2,3,4,5,6,7" % tok)
op, hdr_frame = ws_frame(s)
magic, fs, ch, bits = hdr_frame[:6], *struct.unpack("<IHH", hdr_frame[8:16])
check("listen: the first frame is the 'UILAB1' header: 48000 Hz, 8 channels, 16 bits", (status.split()[1], magic, fs, ch, bits) == ("101", b"UILAB1", 48000, 8, 16), str((status, magic, fs, ch, bits)))
t0, got_bytes = time.monotonic(), 0
while time.monotonic() - t0 < 1.5:
    op, pl = ws_frame(s)
    got_bytes += len(pl)
rate = got_bytes / (time.monotonic() - t0)
check("listen: the stream runs in real time, 768000 bytes per second within 5 percent (%.0f)" % rate, abs(rate - 768000) < 38400, str(rate))
check("listen: the server counts one listener", M.LISTENERS[0] == 1, str(M.LISTENERS[0]))
code, body, _ = get("/api/v2/listen/consent", b"{}", "POST")
tok2 = json.loads(body)["token"]
check("listen: a second listener, even with a valid token, is refused while one is connected (409)", get("/api/v2/listen?token=" + tok2)[0] == 409)
ws_close(s)
end = time.time() + 5
while M.LISTENERS[0] and time.time() < end:
    time.sleep(0.1)
check("listen: a closed client frees the slot within 5 s (the stream notices the close frame)", M.LISTENERS[0] == 0, str(M.LISTENERS[0]))
M.CONSENT[tok2] = time.time() + 1.0
s, status = ws_open("/api/v2/listen?token=%s&beams=2" % tok2)
ws_frame(s)
op, pl = ws_frame(s)
check("listen: beams=2 sends one channel (1920 bytes per 20 ms chunk)", len(pl) == 960 * 2, str(len(pl)))
time.sleep(1.4)
s.settimeout(3)
ended = False
try:
    while True:
        ws_frame(s)
except (OSError, ConnectionError, struct.error, ValueError):
    ended = True
check("listen: the stream ends when the consent expires", ended)
end = time.time() + 5
while M.LISTENERS[0] and time.time() < end:
    time.sleep(0.1)
check("listen: an expired session frees the slot", M.LISTENERS[0] == 0)

# ---------------------------------------------------------------- review, status, benchmark routes
rec = {"k": "verdict", "v": 1, "verdict": "correct"}
code, body, _ = get("/api/v2/review/verdict", json.dumps({"id": "c001", "verdict": "correct", "record": rec}).encode(), "POST")
check("verdict: the record is echoed and kept in memory only", code == 200 and json.loads(body)["echo"] == rec and M.VERDICTS == {"c001": "correct"}, body.decode()[:100])
check("verdict: malformed JSON is 400", get("/api/v2/review/verdict", b"{nope", "POST")[0] == 400)
st = json.loads(get("/api/v2/status")[1])
check("status: live, loss and board fields present, no listener", st["state"] == "live" and "rx_loss_pct" in st and st["board"]["state"] == "ok" and st["listening"] is False, json.dumps(st)[:120])
sp = json.loads(get("/api/spectrum")[1])
check("spectrum (benchmark): rows of 46 base64 bins", sp["n_bins"] == 46 and sp["rows"] and len(base64.b64decode(sp["rows"][-1]["bins"])) == 46)
check("level_row is deterministic and bounded: the same call twice is identical, values are 0..255, lines stand out", M.level_row(3, 100) == M.level_row(3, 100) and M.level_row(3, 100) != M.level_row(3, 101) and M.level_row(2, 7)[5] > M.level_row(2, 7)[6] + 20)

# ---------------------------------------------------------------- static serving and headers
code, body, hdr = get("/")
check("static: / serves the shell with a Content-Security-Policy that has no 'unsafe-inline' and nosniff", code == 200 and b"<title>BMA array</title>" in body and "unsafe-inline" not in hdr.get("Content-Security-Policy", "x") and hdr.get("X-Content-Type-Options") == "nosniff", str(hdr))
check("static: /engine/dsp.js and /app.js are served as JavaScript", all(get(p)[2].get("Content-Type", "").startswith("text/javascript") for p in ("/engine/dsp.js", "/app.js")))
check("static: path traversal is refused", all(get(p)[0] == 404 for p in ("/../mock_server.py", "/%2e%2e/mock_server.py", "/engine/../../mock_server.py")))
check("static: a missing file is 404 JSON, not a stack trace", get("/nope.js")[0] == 404 and json.loads(get("/nope.js")[1]) == {"error": "not found"})
check("static: /bench/ pages are served from bench/static", get("/bench/features.html")[0] == 200)
check("Cache-Control is no-store on every answer", get("/api/v2/status")[2].get("Cache-Control") == "no-store")

# ---------------------------------------------------------------- loopback guard
check("guard: 127.0.0.1, ::1, localhost and 127.0.0.2 are loopback", all(M.is_loopback(h) for h in ("127.0.0.1", "::1", "localhost", "127.0.0.2")))
check("guard: 0.0.0.0, a LAN address and a name are not loopback", not any(M.is_loopback(h) for h in ("0.0.0.0", "203.0.113.5", "example.org")))
check("guard: a non-loopback bind without --allow-lan is refused with exit code 2", M.main(["--bind", "192.0.2.1", "--port", "0"]) == 2)


# the browser tests' poll helper: a page error is not a satisfied condition
import browsertest as BT  # noqa: E402


class Page:
    def __init__(self, *answers):
        self.answers, self.calls, self.pumps = list(answers), [], 0

    def eval(self, expr):
        self.calls.append(expr)
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]

    def pump(self, step):
        self.pumps += 1


err = {"exception": "ReferenceError: __lab is not defined"}
pg = Page(err, False, True)
check("poll: a page error and a false answer are not success; it returns True on the third answer", BT.poll(pg.eval, pg.pump, "window.__lab", 5.0, 0.0) is True and len(pg.calls) == 3 and pg.calls[0] == "!!(window.__lab)", str(pg.calls))
pg = Page(err)
check("poll (positive twin): a page that keeps failing ends at the timeout with False", BT.poll(pg.eval, pg.pump, "x", 0.3, 0.0) is False and len(pg.calls) > 1, str(len(pg.calls)))
pg = Page(True)
check("poll (positive twin): a true condition returns at once after one pump", BT.poll(pg.eval, pg.pump, "x", 5.0, 0.0) is True and pg.pumps == 1, str(pg.pumps))
print("\n%d checks, %d failure(s)" % (n, len(fails)))
sys.exit(1 if fails else 0)
