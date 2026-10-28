#!/usr/bin/env python3
"""Wire bytes per second of each live-data transport against ui-next/mock_server.py, measured on raw sockets (HTTP headers included for polling, WebSocket
frame headers included). All carry the same content: 9 beams x 46 uint8 bands at 10 rows/s (the standard tier). JSON on stdout.
    python bench_transport.py [--secs 20]"""
import argparse, base64, json, os, socket, struct, sys, threading, time
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))
import mock_server as M  # noqa: E402


def recv_until(s, marker, buf=b""):
    while marker not in buf:
        d = s.recv(65536)
        if not d:
            break
        buf += d
    return buf


def poll(port, secs):
    s = socket.create_connection(("127.0.0.1", port)); total, n, since, t0 = 0, 0, 0, time.time()
    while time.time() - t0 < secs:
        s.sendall(("GET /api/spectrum?since=%d HTTP/1.1\r\nHost: x\r\nAccept: application/json\r\n\r\n" % since).encode())
        buf = recv_until(s, b"\r\n\r\n"); head, rest = buf.split(b"\r\n\r\n", 1)
        ln = int([l for l in head.split(b"\r\n") if l.lower().startswith(b"content-length")][0].split(b":")[1])
        while len(rest) < ln:
            rest += s.recv(65536)
        total += len(head) + 4 + ln; n += 1
        since = json.loads(rest[:ln])["next_since"]
        time.sleep(0.5)
    return total, n, "one beam (the mean) only; 2 requests/s; levels[8] for the octave-band compare strip not requested"


def sse(port, secs, fmt):
    s = socket.create_connection(("127.0.0.1", port)); s.sendall(("GET /api/v2/live.sse?fmt=%s HTTP/1.1\r\nHost: x\r\n\r\n" % fmt).encode())
    buf = recv_until(s, b"\r\n\r\n"); hdr = len(buf.split(b"\r\n\r\n", 1)[0]) + 4; body = len(buf.split(b"\r\n\r\n", 1)[1]); t0 = time.time(); s.settimeout(1)
    while time.time() - t0 < secs:
        try:
            d = s.recv(1 << 20)
        except socket.timeout:
            continue
        if not d:
            break
        body += len(d)
    s.close()
    return body, int(secs * 10), "9 beams, 10 events/s; handshake headers %d B once" % hdr


def ws(port, secs):
    s = socket.create_connection(("127.0.0.1", port)); key = base64.b64encode(os.urandom(16)).decode()
    s.sendall(("GET /api/v2/live HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % key).encode())
    buf = recv_until(s, b"\r\n\r\n"); rest = buf.split(b"\r\n\r\n", 1)[1]; hdr = len(buf) - len(rest); s.settimeout(1)
    # backlog: 599 frames sent at once on connect (the last minute); count it separately, then steady state
    t_connect = time.time(); back = 0; steady = 0; frames = 0; t_steady = None
    while time.time() - t_connect < secs + 3:
        try:
            d = s.recv(1 << 20)
        except socket.timeout:
            d = b""
        rest += d
        while len(rest) >= 2:
            n = rest[1] & 0x7F; h = 2
            if n == 126:
                if len(rest) < 4:
                    break
                n = struct.unpack(">H", rest[2:4])[0]; h = 4
            if len(rest) < h + n:
                break
            fr = h + n; rest = rest[fr:]; frames += 1
            if frames <= 599:
                back += fr
            else:
                if t_steady is None:
                    t_steady = time.time()
                steady += fr
        if t_steady and time.time() - t_steady >= secs:
            break
    s.close()
    dt = max(0.1, time.time() - (t_steady or time.time()))
    return steady, int(frames - 599), "9 beams, 10 frames/s, 442 B per frame (438 payload + 4 WebSocket header); handshake %d B once; backlog %d B once (599 frames, uncompressed)" % (hdr, back)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=20.0)
    a = ap.parse_args()
    srv = M.Server(("127.0.0.1", 0), M.Handler); srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start(); port = srv.server_address[1]
    out = {}
    for name, fn in (("poll_json_current_shape", lambda: poll(port, a.secs)), ("sse_json", lambda: sse(port, a.secs, "json")), ("sse_base64", lambda: sse(port, a.secs, "b64")), ("websocket_binary", lambda: ws(port, a.secs))):
        total, n, note = fn()
        out[name] = {"bytes_per_s": round(total / a.secs), "messages": n, "note": note}
    json.dump(out, sys.stdout, indent=1)


if __name__ == "__main__":
    main()
