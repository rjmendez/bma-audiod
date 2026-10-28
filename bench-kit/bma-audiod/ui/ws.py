"""ws - the live spectrum WebSocket of the dashboard (contract ui-live 1.0.0, GET /api/v2/live). Standard library only.

RFC 6455 as far as this one endpoint needs it: the opening handshake checks, unmasked server frames (text, binary, ping, close), masked client
control frames (close, ping, pong; nothing else is accepted), BMW1 binary frames of the spectrum rows, a connection cap, a write timeout and a ping /
pong watchdog. No audio, no phase, no sample: rows are the uint8 band levels the dashboard already serves on GET /api/spectrum.

The transport owner (bma_ui.Handler) does the HTTP side: login, Host, Origin, upgrade checks (`upgrade_problem`, `origin_ok`), the 101 answer; then
hands the socket to `serve(sock, source)`. `source.live_rows(since, rev, now)` -> (rev, reset, rows) is the spectrum feed; rows are
(idx, t_us, flags_per_beam[9], bins_per_beam[9] of 46 bytes or None)."""
import base64, hashlib, json, socket, struct, threading, time, urllib.parse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 1, 2, 8, 9, 10
MAX_CONNECTIONS = 8              # open live sockets at once; the next upgrade is a 503
PING_S = 15.0                    # server ping interval
PONG_TIMEOUT_S = 30.0            # no pong (any client frame counts as no answer; only a pong does) within this: dropped
WRITE_TIMEOUT_S = 5.0            # one send that does not finish in this: dropped (a client that does not read)
TICK_S = 0.05                    # how often a connection looks for new rows and for client frames
MAX_CLIENT_PAYLOAD = 125         # client frames are control frames: 125 bytes at most
MAGIC, TYPE_ROW, HEADER = b"BMW1", 1, 24
BEAMS, BINS = 9, 46
F_GATED, F_GAP, F_ZERO, F_MISSING = 0x01, 0x02, 0x04, 0x08
HELLO = {"k": "hello", "v": 1, "tier": "standard", "beams": BEAMS, "n_bins": BINS, "rows_per_s": 10}
CLOSE_NORMAL, CLOSE_PROTOCOL, CLOSE_UNSUPPORTED, CLOSE_POLICY, CLOSE_TOO_BIG = 1000, 1002, 1003, 1008, 1009


class ProtocolError(Exception):
    def __init__(self, code, reason):
        super().__init__(reason)
        self.code, self.reason = code, reason


# ------------------------------------------------------------------------------------------------ handshake
def accept_key(key):
    return base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")


def origin_ok(origin, host):
    """True when an Origin header names this very host:port (http or https). A missing header is the caller's decision; "null" and anything else is False."""
    if not isinstance(origin, str) or not isinstance(host, str):
        return False
    try:
        u = urllib.parse.urlsplit(origin)
        return u.scheme in ("http", "https") and u.netloc.lower() == host.strip().lower() and not u.path.strip("/") and not u.query and not u.fragment and "@" not in u.netloc
    except ValueError:
        return False


def upgrade_problem(get):
    """get(name) -> header value or None. -> None when the request is a valid WebSocket upgrade, else (status, code, title, detail, extra headers)."""
    conn = [t.strip().lower() for t in (get("Connection") or "").split(",")]
    if (get("Upgrade") or "").strip().lower() != "websocket" or "upgrade" not in conn:
        return 426, "upgrade_required", "Upgrade required", "this route speaks WebSocket only (RFC 6455 version 13)", {"Upgrade": "websocket", "Sec-WebSocket-Version": "13"}
    if (get("Sec-WebSocket-Version") or "").strip() != "13":
        return 426, "upgrade_required", "Upgrade required", "Sec-WebSocket-Version must be 13", {"Upgrade": "websocket", "Sec-WebSocket-Version": "13"}
    key = (get("Sec-WebSocket-Key") or "").strip()
    try:
        ok = len(base64.b64decode(key, validate=True)) == 16
    except ValueError:
        ok = False
    if not ok:
        return 400, "invalid_request", "Invalid request", "Sec-WebSocket-Key must be 16 random bytes, base64 coded", {}
    return None


class Gate:
    """The connection cap: acquire() is False when MAX_CONNECTIONS are open."""

    def __init__(self, limit=MAX_CONNECTIONS):
        self.limit, self.n, self.lock = limit, 0, threading.Lock()

    def acquire(self):
        with self.lock:
            if self.n >= self.limit:
                return False
            self.n += 1
            return True

    def release(self):
        with self.lock:
            self.n -= 1


# ------------------------------------------------------------------------------------------------ frames
def frame(op, payload=b""):
    """One unmasked, unfragmented server frame."""
    n = len(payload)
    h = bytes([0x80 | op]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n) if n < 65536 else bytes([127]) + struct.pack(">Q", n))
    return h + payload


def close_frame(code, reason=""):
    return frame(OP_CLOSE, struct.pack(">H", code) + reason.encode("utf-8")[:120])


def parse_client(buf):
    """-> (op, payload, consumed) for the first complete client frame in buf, None when it is incomplete. Raises ProtocolError(close code, reason):
    unmasked, RSV bits, fragmented, a non-control opcode (1003), a payload over 125 bytes (1009)."""
    if len(buf) < 2:
        return None
    b0, b1 = buf[0], buf[1]
    if b0 & 0x70:
        raise ProtocolError(CLOSE_PROTOCOL, "reserved bits set")
    if not b0 & 0x80:
        raise ProtocolError(CLOSE_PROTOCOL, "fragmented frames are not accepted")
    if not b1 & 0x80:
        raise ProtocolError(CLOSE_PROTOCOL, "client frames must be masked")
    op, n = b0 & 0x0F, b1 & 0x7F
    if n >= 126:
        raise ProtocolError(CLOSE_TOO_BIG, "client frames carry at most %d bytes" % MAX_CLIENT_PAYLOAD)
    if op not in (OP_CLOSE, OP_PING, OP_PONG):
        raise ProtocolError(CLOSE_UNSUPPORTED, "the client may send close, ping and pong only")
    if len(buf) < 6 + n:
        return None
    key = buf[2:6]
    return op, bytes(b ^ key[i % 4] for i, b in enumerate(buf[6:6 + n])), 6 + n


def live_frame(idx, t_us, flags, bins):
    """BMW1 frame. flags: per-beam flag bytes of the 9 streams; bins: 9 entries, each 46 bytes or None (an absent beam is zero filled and sets bit 3).
    Frame flags = OR of bits 0-2 over the present beams, plus bit 3 when a beam is absent."""
    fl, body = 0, bytearray()
    for f, b in zip(flags, bins):
        if b is None:
            fl |= F_MISSING
            body += bytes(BINS)
        else:
            fl |= f & 7
            body += b
    return MAGIC + struct.pack("<BBHIQHH", TYPE_ROW, BEAMS, BINS, idx & 0xFFFFFFFF, t_us, fl, 0) + bytes(body)


# ------------------------------------------------------------------------------------------------ the connection loop
class Drop(Exception):
    pass


def _send(sock, data, write_timeout_s):
    sock.settimeout(write_timeout_s)
    try:
        sock.sendall(data)
    except (socket.timeout, TimeoutError):
        raise Drop("write_timeout")
    except OSError:
        raise Drop("send_error")


def serve(sock, source, clock=time.time, ping_s=PING_S, pong_timeout_s=PONG_TIMEOUT_S, write_timeout_s=WRITE_TIMEOUT_S, tick_s=TICK_S):
    """Run one live connection until it ends. -> the reason: client_close, client_gone, pong_timeout, write_timeout, send_error, recv_error, protocol:<code>."""
    since, rev, buf = -1, None, b""
    now = clock()
    last_ping = last_pong = now
    try:
        _send(sock, frame(OP_TEXT, json.dumps(HELLO, separators=(",", ":")).encode()), write_timeout_s)
        while True:
            now = clock()
            rev, reset, rows = source.live_rows(since, rev, now)
            if reset:
                _send(sock, frame(OP_TEXT, json.dumps({"k": "reset", "rev": rev}, separators=(",", ":")).encode()), write_timeout_s)
            for idx, t_us, flags, bins in rows:
                _send(sock, frame(OP_BINARY, live_frame(idx, t_us, flags, bins)), write_timeout_s)
                since = idx
            sock.settimeout(tick_s)
            try:
                data = sock.recv(4096)
            except (socket.timeout, TimeoutError):
                data = None
            except OSError:
                return "recv_error"
            if data == b"":
                return "client_gone"
            if data:
                buf += data
                try:
                    while True:
                        got = parse_client(buf)
                        if got is None:
                            break
                        op, payload, used = got
                        buf = buf[used:]
                        if op == OP_PING:
                            _send(sock, frame(OP_PONG, payload), write_timeout_s)
                        elif op == OP_PONG:
                            last_pong = clock()
                        else:
                            code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else CLOSE_NORMAL
                            _send(sock, close_frame(CLOSE_NORMAL if code < 1000 else code), write_timeout_s)
                            return "client_close"
                except ProtocolError as e:
                    try:
                        _send(sock, close_frame(e.code, e.reason), write_timeout_s)
                    except Drop:
                        pass
                    return "protocol:%d" % e.code
            now = clock()
            if now - last_pong > pong_timeout_s:
                try:
                    _send(sock, close_frame(CLOSE_POLICY, "no pong"), write_timeout_s)
                except Drop:
                    pass
                return "pong_timeout"
            if now - last_ping >= ping_s:
                _send(sock, frame(OP_PING, b"bma"), write_timeout_s)
                last_ping = now
    except Drop as e:
        return str(e)
