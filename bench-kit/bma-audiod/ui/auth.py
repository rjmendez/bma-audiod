#!/usr/bin/env python3
"""auth - the dashboard's login: one shared secret token, HTTP Basic or a cookie, brute-force lockout, optional TLS.

Standard library only. bma_ui.py imports this module; the design and its reasons are in ui/AUTH.md.

    - the credential is ONE token read from a file (a password for HTTP Basic with any user name, or `GET /?token=`);
    - every comparison of a secret goes through ct_equal (hmac.compare_digest on fixed-length digests), never ==;
    - failures are counted per client IP with an exponential lockout and bounded memory;
    - nothing secret (token, cookie value, Authorization header) is ever logged or echoed: log lines carry an IP and a counter."""
import base64, collections, hashlib, hmac, ipaddress, math, os, ssl, threading, time, urllib.parse

MIN_TOKEN_LEN = 24
MAX_TOKEN_FILE_BYTES = 4096
COOKIE_NAME = "bma_auth"
COOKIE_MAX_AGE_S = 30 * 24 * 3600
REALM = "BMA dashboard"
LOCKOUT_THRESHOLD = 5                    # failures before the first lockout
LOCKOUT_BASE_S = 60                      # the first lockout; doubles with every further failure
LOCKOUT_CAP_S = 900                      # ... up to 15 minutes
FORGET_S = 3600                          # an unlocked client's failure count is forgotten after this long without a failure
MAX_TRACKED = 1024                       # clients tracked at once; the least recently failing one is dropped beyond it
DEFAULT_TOKEN_NAME = "dashboard_token.txt"
DEFAULT_ALLOWED_USERS_NAME = "allowed_users.txt"
MAX_SESSIONS = 256                       # server-side sessions (form login, passkey) held at once; the oldest is dropped beyond it
LOGIN_CSRF_TTL_S = 900                   # a login form stays valid for 15 minutes
MAX_LOGIN_LEN = 254
IDENTITY_HEADER = "Tailscale-User-Login"  # https://tailscale.com/kb/1312/serve : set by `tailscale serve` for tailnet traffic, stripped from incoming requests


class AuthConfigError(Exception):
    """Startup configuration the server must refuse to run with. The message is safe to print (it never holds the token)."""


def ct_equal(a, b):
    """Constant-time equality of two secrets (str or bytes) of any length: both are hashed to 32 bytes first, so neither the
    length nor the position of the first difference reaches the timing, then compared with hmac.compare_digest."""
    a = a.encode("utf-8", "surrogatepass") if isinstance(a, str) else bytes(a)
    b = b.encode("utf-8", "surrogatepass") if isinstance(b, str) else bytes(b)
    return hmac.compare_digest(hashlib.sha256(a).digest(), hashlib.sha256(b).digest())


def validate_token(text):
    """The token text as stored in the file: at least 24 printable ASCII characters, no whitespace anywhere (a trailing newline
    from `echo` and a UTF-8 BOM from some editors are the usual accidents, and both are refused with a message that says so)."""
    if not isinstance(text, str):
        raise AuthConfigError("the token must be text")
    if text != text.strip():
        raise AuthConfigError("the token has leading or trailing whitespace (a trailing newline counts): write the file without it")
    if len(text) < MIN_TOKEN_LEN:
        raise AuthConfigError("the token is shorter than %d characters" % MIN_TOKEN_LEN)
    if not all(33 <= ord(c) <= 126 for c in text):
        raise AuthConfigError("the token must be printable ASCII without spaces (a byte order mark or a non-ASCII character is not allowed)")
    return text


def load_token(path):
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_TOKEN_FILE_BYTES + 1)
    except OSError as ex:
        raise AuthConfigError("cannot read the token file %s: %s" % (path, type(ex).__name__)) from None
    if len(raw) > MAX_TOKEN_FILE_BYTES:
        raise AuthConfigError("the token file %s is larger than %d bytes" % (path, MAX_TOKEN_FILE_BYTES))
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise AuthConfigError("the token file %s is not plain ASCII (a UTF-8 byte order mark?)" % path) from None
    try:
        return validate_token(text)
    except AuthConfigError as ex:
        raise AuthConfigError("token file %s: %s" % (path, ex)) from None


def is_loopback_bind(bind):
    b = (bind or "").strip().lower()
    if b == "localhost":
        return True
    try:
        return ipaddress.ip_address(b.strip("[]")).is_loopback
    except ValueError:
        return False                     # "", "0.0.0.0", "::" and every host name listen beyond loopback (or may)


def default_token_path(pipeline_root):
    return os.path.join(pipeline_root, DEFAULT_TOKEN_NAME)


def make_tls_context(cert, key):
    """Server-side TLS 1.2+ context, no client certificates."""
    if bool(cert) != bool(key):
        raise AuthConfigError("--tls-cert and --tls-key go together")
    if not cert:
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.load_cert_chain(cert, key)
    except (OSError, ssl.SSLError) as ex:
        raise AuthConfigError("cannot load the TLS certificate and key (%s)" % type(ex).__name__) from None
    return ctx


def configure(bind, pipeline_root, token_file=None, no_auth=False, tls_cert="", tls_key="", log=None,
              tailnet_identity=False, allowed_users=None, require_identity=False):
    """Decide how this server authenticates. Returns (Authenticator or None, TLS context or None, notices).
    Secure by default: a token file that exists makes login mandatory; no file on a non-loopback bind refuses to start unless
    --no-auth was given. A path given explicitly with --auth-token-file must exist (a typo must not silently open the server)."""
    notices = []
    tls = make_tls_context(tls_cert, tls_key)
    if (allowed_users is not None or require_identity) and not tailnet_identity:
        raise AuthConfigError("--allowed-users and --require-identity need --tailnet-identity")
    if tailnet_identity and no_auth:
        raise AuthConfigError("--tailnet-identity cannot be combined with --no-auth")
    au = None
    if tailnet_identity:
        au = AllowedUsers(allowed_users if allowed_users is not None else os.path.join(pipeline_root, DEFAULT_ALLOWED_USERS_NAME))
        if not au.users():
            notices.append("tailnet identity: %s is missing or lists nobody, so no identity header is accepted (the token still works%s)"
                           % (au.path, "" if not require_identity else "; with --require-identity NOBODY can sign in"))
    if no_auth:
        notices.append("WARNING: --no-auth: the dashboard has NO login. Anyone who can reach %s can read everything it shows. "
                       "This is unsafe on any network you do not fully control." % (bind or "this address"))
        return None, tls, notices
    explicit = token_file is not None
    path = token_file if explicit else default_token_path(pipeline_root)
    if os.path.isfile(path):
        tok = load_token(path)
        return Authenticator(tok, log=log, secure=tls is not None, allowed_users=au, require_identity=require_identity), tls, notices
    if explicit:
        raise AuthConfigError("the token file %s does not exist; create it (see ui/AUTH.md) or pass --no-auth to run without a login" % path)
    if is_loopback_bind(bind):
        return None, tls, notices
    raise AuthConfigError(
        "refusing to listen on %s without a login: no token file at %s. Create one holding a random token of at least %d "
        "characters (for example 48 hex characters, no trailing newline), or pass --auth-token-file PATH, or --no-auth "
        "(unsafe) to run without a login. See ui/AUTH.md." % (bind or "all interfaces", path, MIN_TOKEN_LEN))


def parse_basic(header):
    """(user, password) from an `Authorization: Basic ...` value, or None when it is not a well-formed Basic credential."""
    if not header:
        return None
    scheme, _, rest = header.strip().partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        raw = base64.b64decode(rest.strip().encode("ascii"), validate=True).decode("utf-8")
    except (ValueError, UnicodeError):
        return None
    user, sep, pw = raw.partition(":")
    return (user, pw) if sep else None


def cookie_values(cookie_headers, name=COOKIE_NAME):
    out = []
    for h in cookie_headers or []:
        for part in h.split(";"):
            k, sep, v = part.strip().partition("=")
            if sep and k == name:
                out.append(v)
    return out


def peer_is_loopback(peer):
    """True for a loopback TCP peer (127.0.0.0/8, ::1, ::ffff:127.x). Anything unparseable is NOT loopback (fail closed)."""
    try:
        ip = ipaddress.ip_address(str(peer).strip().strip("[]").split("%")[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def identity_login(peer, header_values, allowed):
    """THE trust decision for the tailnet identity mode (ui/AUTH.md, ui-http 1.7.0). Returns the allow-listed login, or None.

    Fail closed: the identity header is believed ONLY when ALL of these hold - the TCP peer is loopback (that is where `tailscale serve`
    connects from; Serve strips any client-supplied copy of the header and sets its own, https://tailscale.com/kb/1312/serve, so from any
    other peer the header is attacker-controlled and ignored), the header is present exactly once, its value is a plain ASCII login
    (no RFC 2047 encoded word, no control characters, at most 254 characters) and it is in `allowed` (lower-case set, empty = nobody)."""
    if not peer_is_loopback(peer):
        return None
    vals = list(header_values or [])
    if len(vals) != 1:
        return None
    v = vals[0].strip()
    if not v or len(v) > MAX_LOGIN_LEN or v.startswith("=?") or not all(33 <= ord(c) <= 126 for c in v):
        return None
    return v.lower() if v.lower() in allowed else None


def parse_allowed_users(text):
    """One login per line; blank lines and lines starting with # are ignored; compared case-insensitively."""
    out = set()
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(line.lower())
    return out


class AllowedUsers:
    """The --allowed-users file, re-read whenever its size or modification time changes (revoking a login needs no restart). A missing,
    unreadable or oversized file means NOBODY (fail closed)."""
    MAX_BYTES = 65536

    def __init__(self, path):
        self.path, self._sig, self._users = path, None, frozenset()
        self._lock = threading.Lock()

    def users(self):
        with self._lock:
            try:
                st = os.stat(self.path)
                sig = (st.st_mtime_ns, st.st_size)
            except OSError:
                self._sig, self._users = None, frozenset()
                return self._users
            if sig != self._sig:
                try:
                    with open(self.path, "rb") as f:
                        raw = f.read(self.MAX_BYTES + 1)
                    self._users = frozenset(parse_allowed_users(raw.decode("utf-8")) if len(raw) <= self.MAX_BYTES else ())
                except (OSError, UnicodeError):
                    self._users = frozenset()
                self._sig = sig
            return self._users


class _Open:
    """server.auth of a server with no login configured: `enabled` is False and nothing is ever authenticated."""
    enabled = False
    allowed_users = None
    require_identity = False


OPEN = _Open()


class Result:
    """kind: ok | login (valid ?token=: redirect and set the cookie) | unauthorized | locked."""
    __slots__ = ("kind", "set_cookie", "retry_after", "via", "subject", "user")

    def __init__(self, kind, set_cookie=None, retry_after=0, via=None, subject=None, user=None):
        self.kind, self.set_cookie, self.retry_after = kind, set_cookie, retry_after
        self.via, self.subject, self.user = via, subject, user         # via: session | cookie | basic | tailnet; subject keys the CSRF token


class Authenticator:
    def __init__(self, token, log=None, clock=time.monotonic, secure=False, key=None, allowed_users=None, require_identity=False):
        self._token = validate_token(token)
        self._key = key if key is not None else os.urandom(32)          # per process: a cookie from another run is worthless
        self._cookie = hmac.new(self._key, b"bma-auth-v1|" + hashlib.sha256(self._token.encode("ascii")).hexdigest().encode("ascii"),
                                hashlib.sha256).hexdigest()
        self.secure = secure
        self.enabled = True
        self._log = log or (lambda m: None)
        self._clock = clock
        self._lock = threading.Lock()
        self._rec = collections.OrderedDict()                            # ip -> [failures, locked_until, last_failure]; oldest first
        self.allowed_users = allowed_users                               # AllowedUsers (tailnet identity mode on) or None (off)
        self.require_identity = bool(require_identity and allowed_users is not None)
        self._sessions = collections.OrderedDict()                       # sha256(session id) -> expiry; the id itself is never stored

    # ---- state (exposed for tests and for the rejected-connection guarantee: nothing else touches it)
    def tracked(self):
        with self._lock:
            return len(self._rec)

    def failures(self, ip):
        with self._lock:
            r = self._rec.get(ip)
            return r[0] if r else 0

    def cookie_header(self, secure=None, value=None, max_age=COOKIE_MAX_AGE_S):
        """The Set-Cookie value. `secure` None = the server's own TLS setting; a request behind a TLS-terminating proxy passes True."""
        a = "%s=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=%d" % (COOKIE_NAME, self._cookie if value is None else value, max_age)
        return a + "; Secure" if (self.secure if secure is None else secure) else a

    # ---- sessions (form login, passkey): a random 256-bit id in the cookie, held server side so a logout really ends it
    def new_session(self, secure=None):
        """-> (Set-Cookie header value, session id). In memory only: a restart ends every session, as it does the legacy cookie."""
        sid = os.urandom(32).hex()
        with self._lock:
            self._sessions[hashlib.sha256(sid.encode("ascii")).digest()] = self._clock() + COOKIE_MAX_AGE_S
            while len(self._sessions) > MAX_SESSIONS:
                self._sessions.popitem(last=False)
        return self.cookie_header(secure=secure, value=sid), sid

    def _session_ok(self, sid):
        h = hashlib.sha256(sid.encode("utf-8", "surrogatepass")).digest()
        with self._lock:
            exp = self._sessions.get(h)
            if exp is not None and exp <= self._clock():
                del self._sessions[h]
                exp = None
            return exp is not None

    def end_session(self, cookie_headers):
        """Forget every session named in the Cookie headers. Returns how many were server-side sessions."""
        n = 0
        with self._lock:
            for v in cookie_values(cookie_headers):
                if self._sessions.pop(hashlib.sha256(v.encode("utf-8", "surrogatepass")).digest(), None) is not None:
                    n += 1
        return n

    def clear_cookie_header(self, secure=None):
        return self.cookie_header(secure=secure, value="", max_age=0)

    def session_count(self):
        with self._lock:
            return len(self._sessions)

    # ---- CSRF: a token bound to the session (or, for Basic / tailnet identity, to that subject); only a same-origin script can read it
    def csrf_token(self, subject):
        return hmac.new(self._key, b"bma-csrf-v1|" + subject.encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()

    def csrf_ok(self, subject, presented):
        return bool(subject) and isinstance(presented, str) and ct_equal(presented, self.csrf_token(subject))

    def login_csrf(self):
        """Token for the (not yet authenticated) login form: `<unix-ish time>.<hmac>`, valid LOGIN_CSRF_TTL_S, no server state."""
        t = str(int(self._clock()))
        return t + "." + hmac.new(self._key, b"bma-login-csrf-v1|" + t.encode("ascii"), hashlib.sha256).hexdigest()

    def login_csrf_ok(self, tok):
        t, _, mac = (tok or "").partition(".")
        if not t.isdigit() or len(t) > 12:
            return False
        good = hmac.new(self._key, b"bma-login-csrf-v1|" + t.encode("ascii"), hashlib.sha256).hexdigest()
        fresh = 0 <= self._clock() - int(t) <= LOGIN_CSRF_TTL_S
        return ct_equal(mac, good) and fresh

    def token_ok(self, presented):
        return isinstance(presented, str) and ct_equal(presented, self._token)

    def record_failure(self, ip):
        now = self._clock()
        with self._lock:
            r = self._rec.pop(ip, None)
            if r is not None and r[1] <= now and now - r[2] > FORGET_S:
                r = None
            if r is None:
                r = [0, 0.0, now]
            r[0] += 1
            r[2] = now
            if r[0] >= LOCKOUT_THRESHOLD:
                dur = min(LOCKOUT_BASE_S * 2 ** (r[0] - LOCKOUT_THRESHOLD), LOCKOUT_CAP_S)
                r[1] = now + dur
                self._log("auth lockout ip=%s failures=%d seconds=%d" % (ip, r[0], dur))
            else:
                self._log("auth failure ip=%s failures=%d" % (ip, r[0]))
            self._rec[ip] = r
            while len(self._rec) > MAX_TRACKED:
                self._rec.popitem(last=False)

    def lock_left(self, ip):
        """Seconds until this address may try again (0 = not locked out)."""
        return self._locked_for(ip)

    def _locked_for(self, ip):
        with self._lock:
            r = self._rec.get(ip)
            if r is None:
                return 0
            left = r[1] - self._clock()
            return math.ceil(left) if left > 0 else 0

    def _clear(self, ip):
        with self._lock:
            self._rec.pop(ip, None)

    def check(self, path, query, authorization, cookie_headers, ip, identity=None, secure=None):
        """Authenticate one request (after the Host check, before anything else is answered). `identity` is an allow-listed tailnet login
        already decided by identity_login() (never a raw header): it authenticates without touching the lockout state."""
        if identity:
            return Result("ok", via="tailnet", subject="id:" + identity, user=identity)
        if self.require_identity:                                        # --require-identity: the token, Basic and cookies are not accepted at all
            return Result("unauthorized")
        left = self._locked_for(ip)
        if left:                                                         # locked out: even a correct credential is refused
            return Result("locked", retry_after=left)
        # every credential is compared, none short-circuits the others
        cookie_ok, sid = False, None
        for v in cookie_values(cookie_headers) or [""]:
            cookie_ok |= ct_equal(v, self._cookie)
            if v and self._session_ok(v):
                sid = v
        basic = parse_basic(authorization)
        basic_ok = ct_equal(basic[1] if basic else "", self._token) and basic is not None
        tok = None
        if path == "/" and query:
            vals = urllib.parse.parse_qs(query, keep_blank_values=True).get("token")
            tok = vals[0] if vals else None
        tok_ok = tok is not None and ct_equal(tok, self._token)
        if tok_ok:
            self._clear(ip)
            return Result("login", set_cookie=self.cookie_header(secure=secure))
        if sid is not None or cookie_ok or basic_ok:
            self._clear(ip)
            if sid is not None:
                return Result("ok", via="session", subject="s:" + sid)
            if cookie_ok:
                return Result("ok", via="cookie", subject="c:" + self._cookie)
            return Result("ok", via="basic", subject="basic")
        if tok is not None or authorization:                             # a credential was presented and it is wrong: count it
            self.record_failure(ip)
        return Result("unauthorized")                                    # no credential (or a stale cookie): a plain challenge, not counted
