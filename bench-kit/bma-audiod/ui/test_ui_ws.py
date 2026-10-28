#!/usr/bin/env python3
"""Tests for GET /api/v2/live (contract ui-live 1.0.0, ui/ws.py): a real bma_ui server on an ephemeral port, real TCP sockets speaking raw RFC 6455, rows fed
into the real spectrum feed, frames compared byte for byte with the contract's reference builder (contracts/ref_binary.py). Connection-level rules (ping,
pong timeout, write timeout) run ws.serve over a real socket pair. Waits are on state (a frame, a closed socket, a counter), never on a fixed sleep.

    python test_ui_ws.py     (PASS/FAIL lines, a final 'N checks, M failure(s)' line; exit 1 on a failure)"""
import base64, json, os, socket, struct, sys, tempfile, threading, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(1, os.path.join(HERE, "..", "contracts"))
import auth as A  # noqa: E402
import bma_ui as U  # noqa: E402
import ref_binary as R  # noqa: E402
import ws as W  # noqa: E402

fails, n_checks = [], 0
TOKEN = "T0ken-" + "a1b2c3d4e5f6" * 4
BASIC = "Basic " + base64.b64encode(("u:" + TOKEN).encode()).decode()
KEY = "dGhlIHNhbXBsZSBub25jZQ=="
ACCEPT = "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
MASK = bytes([0x37, 0xFA, 0x21, 0x3D])
FIX = os.path.join(HERE, "..", "contracts", "fixtures")


def check(name, ok, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)
    if not ok:
        fails.append(name)


def eq(name, got, want):
    check(name, got == want, f"got {got!r}, want {want!r}")


def until(cond, secs=10.0):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return bool(cond())


# ------------------------------------------------------------------------------------------------ the rig
class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def bins_of(k, beam):
    return [(k * 7 + beam * 3 + i) % 200 for i in range(46)]


class Rig:
    def __init__(self, auth=True, ui_next=True, feed_on=True, age_s=100.0):
        self.tmp = tempfile.mkdtemp(prefix="bma_ws_")
        pr = os.path.join(self.tmp, "p")
        os.makedirs(os.path.join(pr, "analysis"))
        self.clk = Clock(time.time() - age_s)                              # rows arrived long ago: a row still missing beams is not held back
        self.feed = U.SpectrumFeed(enabled=feed_on, clock=self.clk)
        self.app = U.App(pr, os.path.join(self.tmp, "r"), os.path.join(self.tmp, "ru"), U.Board("http://127.0.0.1:9", os.path.join(self.tmp, "nt"), enabled=False),
                         log=lambda m: None, spectrum=self.feed)
        self.srv = U.Server(("127.0.0.1", 0), self.app, set(), U.load_assets(), auth=A.Authenticator(TOKEN, log=lambda m: None) if auth else None,
                            next_assets=U.load_next_assets() if ui_next else None)
        self.port = self.srv.server_address[1]
        self.srv.allowed_hosts = U.allowed_hosts_for(self.port, [])
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.sent = {}                                                      # row k -> (t_us, flags, [bins per beam or None])
        self.k = 0

    def row(self, beams=range(9), gated=False):
        """Feed one row (all `beams`) into the real feed; the expected BMW1 frame body is remembered."""
        k, t_us, got = self.k, int(self.clk.t * 1e6), [None] * 9
        for b in beams:
            bins = [90] * 46 if gated else bins_of(k, b)                    # a gated row must be coarse (constant inside a group; constant everywhere is)
            assert self.feed.handle(R.build_spectrum(0, 1 if gated else 0, b, k, t_us, bins), "127.0.0.1")
            got[b] = bins
        self.sent[k] = (t_us, 1 if gated else 0, got)
        self.k += 1
        self.clk.t += 0.1
        return k

    def rows(self, n, **kw):
        return [self.row(**kw) for _ in range(n)]

    def expected(self, k, idx):
        t_us, fl, beams = self.sent[k]
        missing = any(b is None for b in beams)
        return R.build_live(idx, t_us, fl | (R.LIVE_FLAG_MISSING if missing else 0), [b if b is not None else [0] * 46 for b in beams])

    def idxs(self):
        return [s.idx for s in self.feed.rings["standard"].slots]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Client:
    def __init__(self, port, path="/api/v2/live", headers=None, key=KEY, auth=True):
        h = {"Host": "localhost:%d" % port, "Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Key": key, "Sec-WebSocket-Version": "13"}
        if auth:
            h["Authorization"] = BASIC
        h.update(headers or {})
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=15)
        self.sock.sendall(("GET %s HTTP/1.1\r\n" % path + "".join("%s: %s\r\n" % (k, v) for k, v in h.items() if v is not None) + "\r\n").encode())
        raw = b""
        while b"\r\n\r\n" not in raw:
            c = self.sock.recv(4096)
            if not c:
                break
            raw += c
        head, _, self.buf = raw.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        self.status = int(lines[0].split()[1])
        self.h = {k.lower(): v for k, v in (ln.split(": ", 1) for ln in lines[1:])}
        self.body = b""
        if self.status != 101:
            n = int(self.h.get("content-length", "0"))
            while len(self.buf) < n:
                self.buf += self.sock.recv(4096)
            self.body, self.buf = self.buf[:n], self.buf[n:]

    def problem(self):
        return json.loads(self.body)["code"]

    def frame(self, timeout=10.0):
        """-> (opcode, payload) of the next server frame, or None at EOF / timeout."""
        self.sock.settimeout(timeout)
        while True:
            if len(self.buf) >= 2:
                n, off = self.buf[1] & 0x7F, 2
                if n == 126 and len(self.buf) >= 4:
                    n, off = struct.unpack(">H", self.buf[2:4])[0], 4
                elif n == 127 and len(self.buf) >= 10:
                    n, off = struct.unpack(">Q", self.buf[2:10])[0], 10
                if (self.buf[1] & 0x7F) < 126 or off > 2:
                    if len(self.buf) >= off + n:
                        op, payload, self.buf = self.buf[0] & 0x0F, self.buf[off:off + n], self.buf[off + n:]
                        return op, payload
            try:
                c = self.sock.recv(65536)
            except (socket.timeout, ConnectionError):
                return None
            if not c:
                return None
            self.buf += c

    def frames(self, n, timeout=10.0):
        return [self.frame(timeout) for _ in range(n)]

    def send(self, op, payload=b"", mask=MASK):
        self.sock.sendall(R.ws_encode(op, payload, mask))

    def raw(self, data):
        self.sock.sendall(data)

    def eof(self, timeout=10.0):
        """True once the server has closed the connection (after any pending frames)."""
        self.sock.settimeout(timeout)
        try:
            while True:
                if not self.sock.recv(65536):
                    return True
        except (socket.timeout, ConnectionError):
            return isinstance(sys.exc_info()[1], ConnectionError)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


HELLO = json.dumps({"k": "hello", "v": 1, "tier": "standard", "beams": 9, "n_bins": 46, "rows_per_s": 10}, separators=(",", ":")).encode()


def main():
    rig = Rig()
    try:
        # ---- handshake, hello, backlog, live tail
        ks = rig.rows(25)
        c = Client(rig.port)
        eq("handshake: 101 with the RFC 6455 accept value for the sample key, Upgrade websocket, Connection Upgrade", (c.status, c.h.get("sec-websocket-accept"), c.h.get("upgrade"), c.h.get("connection")), (101, ACCEPT, "websocket", "Upgrade"))
        eq("first frame: the text hello, exact bytes", c.frame(), (W.OP_TEXT, HELLO))
        idx = rig.idxs()
        got = c.frames(25)
        eq("backlog: 25 binary frames, oldest first, each byte for byte what the reference builder makes of the fed row (seq = the feed's row index)",
           got, [(W.OP_BINARY, rig.expected(k, i)) for k, i in zip(ks, idx)])
        eq("backlog: 25 distinct, rising seq values (fed rows are distinct: the check cannot pass on a repeated frame)", [R.parse_live(p)["seq"] for _, p in got], idx)
        more = rig.rows(3)
        idx2 = rig.idxs()[-3:]
        eq("live tail: 3 rows fed after the connect arrive as 3 frames, in order, exact", c.frames(3), [(W.OP_BINARY, rig.expected(k, i)) for k, i in zip(more, idx2)])
        eq("positive twin of the tail: nothing else is pending (the next read times out)", c.frame(0.4), None)
        # ---- /api/spectrum stays
        req = urllib.request.Request("http://127.0.0.1:%d/api/spectrum?beam=2" % rig.port, headers={"Authorization": BASIC})
        sp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        eq("/api/spectrum is unchanged beside the socket: 28 rows for beam 2, the last one's bins equal the fed row", (len(sp["rows"]), list(base64.b64decode(sp["rows"][-1]["bins"]))), (28, bins_of(27, 2)))
        # ---- ping / pong
        c.send(W.OP_PING, b"hello")
        eq("client ping (masked) is answered with a pong carrying the same payload", c.frame(), (W.OP_PONG, b"hello"))
        c.send(W.OP_PING, bytes(range(125)))
        eq("client ping of exactly 125 bytes (the limit) is answered", c.frame(), (W.OP_PONG, bytes(range(125))))
        c.send(W.OP_PONG, b"x")
        c.send(W.OP_PING, b"after")
        eq("an unsolicited pong is ignored: the connection goes on (next ping answered)", c.frame(), (W.OP_PONG, b"after"))
        # ---- close handshake and the gate
        eq("one live socket is counted", rig.srv.ws_gate.n, 1)
        c.send(W.OP_CLOSE, struct.pack(">H", 1000))
        eq("client close 1000: the server answers close 1000 and closes the socket", (c.frame(), c.eof()), ((W.OP_CLOSE, struct.pack(">H", 1000)), True))
        check("the gate count returns to 0 after the close", until(lambda: rig.srv.ws_gate.n == 0), str(rig.srv.ws_gate.n))
        c.close()
        # ---- reconnect: the full backlog again
        c2 = Client(rig.port)
        c2.frame()
        eq("reconnect: a new socket gets the whole ring again (28 frames, the first is the first row)", (len(c2.frames(28)), c2.frame(0.3)), (28, None))
        c2.close()
        # ---- masking, reserved bits, fragments, data frames, oversize: each ends the connection with the exact close code
        for name, data, code in (("unmasked client frame", R.ws_encode(W.OP_PING, b"hi"), 1002), ("RSV1 set", bytes([0xC9]) + R.ws_encode(W.OP_PING, b"hi", MASK)[1:], 1002),
                                 ("FIN clear (a fragment)", bytes([0x09]) + R.ws_encode(W.OP_PING, b"hi", MASK)[1:], 1002), ("a masked text frame (data from the client)", R.ws_encode(W.OP_TEXT, b"x", MASK), 1003),
                                 ("a masked binary frame", R.ws_encode(W.OP_BINARY, b"x", MASK), 1003), ("a masked continuation frame", R.ws_encode(0, b"x", MASK), 1003),
                                 ("a 126-byte ping (16-bit length)", R.ws_encode(W.OP_PING, bytes(126), MASK), 1009), ("a ping that claims a 64-bit length", bytes([0x89, 0xFF]) + struct.pack(">Q", 1 << 40) + MASK, 1009)):
            x = Client(rig.port)
            x.frame()
            x.frames(28)
            x.raw(data)
            op, payload = x.frame()
            check("%s: close %d, then the socket is closed" % (name, code), op == W.OP_CLOSE and struct.unpack(">H", payload[:2])[0] == code and x.eof(), repr((op, payload[:2])))
            x.close()
        check("every refused connection released its slot", until(lambda: rig.srv.ws_gate.n == 0), str(rig.srv.ws_gate.n))
        # ---- the cap
        socks = [Client(rig.port) for _ in range(W.MAX_CONNECTIONS)]
        eq("cap: %d sockets are open and accepted (101)" % W.MAX_CONNECTIONS, [s.status for s in socks], [101] * W.MAX_CONNECTIONS)
        over = Client(rig.port)
        eq("cap: the next upgrade is 503 too_many_connections with Retry-After 5", (over.status, over.problem(), over.h.get("retry-after")), (503, "too_many_connections", "5"))
        over.close()
        socks[0].close()
        check("cap: after one socket closes a new upgrade is accepted again (positive twin)", until(lambda: rig.srv.ws_gate.n == W.MAX_CONNECTIONS - 1))
        again = Client(rig.port)
        eq("cap: the freed slot is reusable (101)", again.status, 101)
        for s in socks[1:] + [again]:
            s.close()
        check("cap: every slot is released at the end", until(lambda: rig.srv.ws_gate.n == 0), str(rig.srv.ws_gate.n))
        # ---- refusals, each with its code, each with a positive twin (the valid request above)
        r = Client(rig.port, auth=False)
        eq("no credentials: 401 unauthorized (and no upgrade)", (r.status, r.problem()), (401, "unauthorized"))
        r.close()
        r = Client(rig.port, headers={"Authorization": "Basic " + base64.b64encode(b"u:wrong-token-value-1234567890").decode()})
        eq("a wrong token: 401 unauthorized", (r.status, r.problem()), (401, "unauthorized"))
        r.close()
        host = "localhost:%d" % rig.port
        for name, origin, want in (("wrong origin", "http://evil.example", 403), ("origin null", "null", 403), ("same host, other port", "http://localhost:1", 403), ("host as a prefix of the origin host", "http://%s.evil.example" % host, 403),
                                   ("same origin over http", "http://" + host, 101), ("same origin over https", "https://" + host, 101), ("no Origin header (a non-browser client)", None, 101)):
            r = Client(rig.port, headers={"Origin": origin})
            eq("Origin %s: %d%s" % (name, want, " origin_not_allowed" if want == 403 else ""), (r.status, r.problem() if want == 403 else None), (want, "origin_not_allowed" if want == 403 else None))
            r.close()
        r = Client(rig.port, headers={"Upgrade": None, "Connection": "keep-alive"})
        eq("not an upgrade: 426 upgrade_required with Upgrade and Sec-WebSocket-Version 13", (r.status, r.problem(), r.h.get("upgrade"), r.h.get("sec-websocket-version")), (426, "upgrade_required", "websocket", "13"))
        r.close()
        r = Client(rig.port, headers={"Sec-WebSocket-Version": "8"})
        eq("version 8: 426 upgrade_required", (r.status, r.problem()), (426, "upgrade_required"))
        r.close()
        for name, key in (("a key that is not base64", "!!!!"), ("a key of 8 bytes", base64.b64encode(b"12345678").decode()), ("a missing key", None)):
            r = Client(rig.port, key=key)
            eq("%s: 400 invalid_request" % name, (r.status, r.problem()), (400, "invalid_request"))
            r.close()
        r = Client(rig.port, path="/api/v2/live?since=1")
        eq("a query string: 422 invalid_parameter", (r.status, r.problem()), (422, "invalid_parameter"))
        r.close()
        for method in ("HEAD", "POST"):
            s = socket.create_connection(("127.0.0.1", rig.port), timeout=10)
            s.sendall(("%s /api/v2/live HTTP/1.1\r\nHost: localhost:%d\r\nAuthorization: %s\r\nContent-Length: 0\r\nConnection: close\r\n\r\n" % (method, rig.port, BASIC)).encode())
            first = s.recv(4096).split(b"\r\n")[0]
            s.close()
            eq("%s: 405" % method, first, b"HTTP/1.1 405 Method Not Allowed")
        # ---- partial rows
        rig.row(beams=range(8))
        rr = Client(rig.port)
        rr.frame()
        frames = rr.frames(29)
        last = R.parse_live(frames[-1][1])
        eq("a row without beam 8 (arrived long ago) is sent with flag bit 3 and 46 zero bytes for beam 8; the 28 complete rows before it have flags 0",
           (last["flags"], last["beams"][8], [R.parse_live(p)["flags"] for _, p in frames[:-1]] == [0] * 28, frames[-1][1] == rig.expected(rig.k - 1, rig.idxs()[-1])), (8, [0] * 46, True, True))
        rr.close()
        # ---- gated rows, the tag latch, reset
        rig.rows(10, gated=True)
        g = Client(rig.port)
        g.frame()
        fr = g.frames(39)
        eq("a gated row carries flag bit 0 and its coarse bins (positive twin: the rows before it have flags 0 or 8)", ([R.parse_live(p)["flags"] for _, p in fr[-10:]], [R.parse_live(p)["flags"] for _, p in fr[:28]]), ([1] * 10, [0] * 28))
        t0 = rig.sent[5][0] / 1e6
        rev0 = rig.feed.rings["standard"].rev
        changed = rig.feed.latch(t0, t0 + 0.05)
        rev1 = rig.feed.rings["standard"].rev
        check("setup: the tag latch rewrote rows in the ring and moved the revision", changed > 0 and rev1 == rev0 + 1, "%s %s %s" % (changed, rev0, rev1))
        op, payload = g.frame()
        eq("revision moved: a text reset {k reset, rev} comes first", (op, json.loads(payload)), (W.OP_TEXT, {"k": "reset", "rev": rev1}))
        again = g.frames(39)
        flags_again = [R.parse_live(p)["flags"] for _, p in again]
        check("after the reset the whole ring (39 rows) follows and the latched block is gated (flag bit 0 set on rows 0-9 of the block holding row 5), the first 28 rows are not all 0 any more",
              len(again) == 39 and all(op == W.OP_BINARY for op, _ in again) and flags_again[:10] == [1] * 10 and flags_again != [R.parse_live(p)["flags"] for _, p in fr], str(flags_again[:12]))
        g.close()
    finally:
        rig.close()

    # ---- the route needs the flag, a feed and a login
    off = Rig(ui_next=False)
    try:
        r = Client(off.port)
        eq("without --ui-next the route is a plain 404 not_found", (r.status, r.problem()), (404, "not_found"))
        r.close()
    finally:
        off.close()
    nofeed = Rig(feed_on=False)
    try:
        r = Client(nofeed.port)
        eq("with --ui-next but no spectrum feed: 404 not_found", (r.status, r.problem()), (404, "not_found"))
        r.close()
    finally:
        nofeed.close()
    openrig = Rig(auth=False)
    try:
        openrig.rows(3)
        r = Client(openrig.port, auth=False)
        eq("a server without a login is not authenticated: 401 unauthorized (same rule as /api/spectrum)", (r.status, r.problem()), (401, "unauthorized"))
        r.close()
    finally:
        openrig.close()
    # ---- a row that is still missing beams waits (feed.live_rows)
    young = Rig(age_s=0.0)
    try:
        young.row(beams=range(8))
        now = young.clk.t - 0.1
        _, _, held = young.feed.live_rows(-1, None, now + 0.05)
        _, _, late = young.feed.live_rows(-1, None, now + 0.5)
        eq("live_rows: a row missing a beam is held back for 0.3 s after it arrived (0 rows at +0.05 s) and sent after it (1 row at +0.5 s)", (len(held), len(late)), (0, 1))
        young.row()
        _, _, both = young.feed.live_rows(-1, None, now + 0.05)
        eq("live_rows: positive twin - the next complete row releases the earlier incomplete one (2 rows)", len(both), 2)
        rev, reset, rows = young.feed.live_rows(rows_after := both[-1][0], None, now + 1)
        eq("live_rows: nothing newer than the cursor means no rows and no reset", (reset, rows), (False, []))
    finally:
        young.close()
    connection_tests()
    fixture_tests()
    print("\n%d checks, %d failure(s)" % (n_checks, len(fails)))
    return 1 if fails else 0


# ------------------------------------------------------------------------------------------------ ws.serve over a real socket pair
class Src:
    """A scripted source: rows(since) yields n rows once; reset_rev makes the first call a reset."""

    def __init__(self, n=0, reset=False):
        self.n, self.calls, self.reset, self.sent = n, 0, reset, False

    def live_rows(self, since, rev, now):
        self.calls += 1
        if self.sent:
            return 7, False, []
        self.sent = True
        return 7, self.reset, [(i, 1000 + i, [0] * 9, [bytes([i % 256]) * 46] * 9) for i in range(self.n)]


def pair(sndbuf=None):
    a, b = socket.socketpair()
    if sndbuf:
        a.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, sndbuf)
        b.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, sndbuf)
    return a, b


class Peer(Client):
    def __init__(self, sock):
        self.sock, self.buf = sock, b""


def run_serve(srv_sock, src, out, **kw):
    t = threading.Thread(target=lambda: out.append(W.serve(srv_sock, src, **kw)), daemon=True)
    t.start()
    return t


def connection_tests():
    # ping interval and the pong watchdog
    a, b = pair()
    out = []
    t = run_serve(a, Src(), out, ping_s=0.2, pong_timeout_s=30, tick_s=0.02)
    p = Peer(b)
    p.frame()
    eq("server ping: a ping frame with payload 'bma' arrives within the interval", p.frame(5), (W.OP_PING, b"bma"))
    p.send(W.OP_PONG, b"bma")
    p.send(W.OP_CLOSE, struct.pack(">H", 1000))
    t.join(10)
    eq("a pong keeps the connection; the client close ends it with client_close", out, ["client_close"])
    a.close(), b.close()

    a, b = pair()
    out = []
    t = run_serve(a, Src(), out, ping_s=0.1, pong_timeout_s=0.5, tick_s=0.02)
    p = Peer(b)
    frames, end = [], time.time() + 4
    while time.time() < end:
        f = p.frame(1)
        if f is None or f[0] == W.OP_CLOSE:
            frames.append(f)
            break
    else:
        frames.append("no close within 4 s")
    t.join(10)
    eq("no pong within the timeout: close 1008 'no pong' and the connection ends with pong_timeout", (frames[-1], out), ((W.OP_CLOSE, struct.pack(">H", 1008) + b"no pong"), ["pong_timeout"]))
    a.close(), b.close()

    a, b = pair()
    out = []
    t = run_serve(a, Src(), out, ping_s=0.1, pong_timeout_s=0.5, tick_s=0.02)
    p = Peer(b)
    p.frame()
    end = time.time() + 1.5
    answered = 0
    while time.time() < end:
        f = p.frame(0.3)
        if f and f[0] == W.OP_PING:
            p.send(W.OP_PONG, f[1])
            answered += 1
    alive = t.is_alive()
    p.send(W.OP_CLOSE, struct.pack(">H", 1000))
    t.join(10)
    check("positive twin: a client that answers every ping stays connected well past the pong timeout (%d pongs in 1.5 s)" % answered, alive and answered >= 5 and out == ["client_close"], str((alive, answered, out)))
    a.close(), b.close()

    # write timeout: a client that never reads; positive twin: the same rows to a client that reads
    a, b = pair(4096)
    out = []
    t = run_serve(a, Src(n=20000), out, write_timeout_s=0.3, tick_s=0.02)
    t.join(30)
    eq("slow client: 20000 rows into a socket nobody reads end with write_timeout, and the thread is gone", (out, t.is_alive()), (["write_timeout"], False))
    a.close(), b.close()
    a, b = pair(4096)
    out = []
    t = run_serve(a, Src(n=300), out, write_timeout_s=0.3, tick_s=0.02)
    p = Peer(b)
    p.frame()
    got = p.frames(300)
    p.send(W.OP_CLOSE, struct.pack(">H", 1000))
    t.join(10)
    eq("positive twin: the same kind of rows to a client that reads are all delivered (300 frames of 438 bytes) and the end is client_close", ([len(x[1]) for x in got] == [438] * 300, out), (True, ["client_close"]))
    a.close(), b.close()
    # reset text first, then the rows
    a, b = pair()
    out = []
    t = run_serve(a, Src(n=2, reset=True), out, tick_s=0.02)
    p = Peer(b)
    p.frame()
    eq("a reset from the source is a text reset frame before the rows", p.frames(3), [(W.OP_TEXT, b'{"k":"reset","rev":7}'), (W.OP_BINARY, R.build_live(0, 1000, 0, [[0] * 46] * 9)), (W.OP_BINARY, R.build_live(1, 1001, 0, [[1] * 46] * 9))])
    p.send(W.OP_CLOSE, struct.pack(">H", 1000))
    t.join(10)
    a.close(), b.close()
    # a vanished client
    a, b = pair()
    out = []
    t = run_serve(a, Src(), out, tick_s=0.02)
    Peer(b).frame()
    b.close()
    t.join(10)
    check("a client that just closes its socket ends the connection (client_gone or send_error, never a hang)", out in (["client_gone"], ["recv_error"], ["send_error"]), str(out))
    a.close()


# ------------------------------------------------------------------------------------------------ ws.py against the contract's fixtures
def fixture_tests():
    d = json.load(open(os.path.join(FIX, "ui_live_examples.json"), encoding="utf-8"))
    ok = True
    for ex in d["frames"]["valid"]:
        beams = [[first] + [fill] * 45 for fill, first in ex["beams"]]
        present = [None if (fill == 0 and first == 0 and ex["flags"] & 8 and i == 8) else bytes(b) for i, ((fill, first), b) in enumerate(zip(ex["beams"], beams))]
        flags = [ex["flags"] & 7] * 9
        ok &= W.live_frame(ex["seq"], ex["t_us"], flags, present) == bytes.fromhex(ex["hex"])
    check("ws.live_frame reproduces all 4 contract BMW1 examples byte for byte (complete, gated, beam absent, gap+zero at the u32 limit)", ok)
    eq("ws.accept_key equals the contract's handshake vector", W.accept_key(d["handshake"]["key"]), d["handshake"]["accept"])
    eq("ws.frame reproduces the 7 contract server frames byte for byte", [W.frame(e["opcode"], bytes.fromhex(e["payload_hex"])) == bytes.fromhex(e["hex"]) for e in d["server_frames"]], [True] * 7)
    eq("ws.parse_client decodes the 4 valid contract client frames", [W.parse_client(bytes.fromhex(e["hex"])) for e in d["client_frames"]["valid"]],
       [(e["opcode"], bytes.fromhex(e["payload_hex"]), len(bytes.fromhex(e["hex"]))) for e in d["client_frames"]["valid"]])
    got = []
    for e in d["client_frames"]["invalid"]:
        try:
            W.parse_client(bytes.fromhex(e["hex"]))
            got.append("accepted")
        except W.ProtocolError as ex:
            got.append(ex.code)
    eq("ws.parse_client refuses the 6 invalid contract client frames with the close codes the contract names", got, [e["close_code"] for e in d["client_frames"]["invalid"]])
    eq("ws.parse_client: an incomplete frame is None (positive twin of the refusals: nothing is refused for being short)", W.parse_client(bytes.fromhex(d["client_frames"]["valid"][0]["hex"])[:5]), None)
    eq("ws.origin_ok: exact host and port only", [W.origin_ok(o, "h:1") for o in ("http://h:1", "https://H:1", "http://h:1/", "http://h:2", "http://h:1.evil", "null", "ftp://h:1", "http://u@h:1", "http://h:1/x", None)],
       [True, True, True, False, False, False, False, False, False, False])
    hdr = {"Connection": "keep-alive, Upgrade", "Upgrade": "WebSocket", "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": KEY}
    eq("ws.upgrade_problem: a valid upgrade (mixed case, Connection with several tokens) is None; each defect has its status", [W.upgrade_problem(hdr.get)] + [(W.upgrade_problem(dict(hdr, **{k: v}).get) or (0,))[0] for k, v in (("Upgrade", "h2c"), ("Connection", "close"), ("Sec-WebSocket-Version", "12"), ("Sec-WebSocket-Key", "x"))],
       [None, 426, 426, 426, 400])


if __name__ == "__main__":
    sys.exit(main())
