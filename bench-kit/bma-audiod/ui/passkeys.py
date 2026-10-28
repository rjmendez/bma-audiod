#!/usr/bin/env python3
"""passkeys - WebAuthn (passkey) registration and login for the dashboard, Python standard library only.

Scope, deliberately small (design and reasons in ui/AUTH.md, section Passkeys):
    - ES256 only (COSE alg -7, ECDSA P-256 with SHA-256): every platform authenticator and security key supports it;
    - attestation is NOT verified (fmt "none" is requested; whatever statement an authenticator returns is ignored): the dashboard
      has one owner and needs no proof of authenticator make or model;
    - user verification (PIN / biometric) is REQUIRED for registration and for every login: a passkey replaces the token, so
      presence alone is not enough;
    - this module only VERIFIES signatures with a public key. It never signs and never holds a private key or any other secret,
      so the pure-Python P-256 arithmetic below has no secret-dependent timing to protect. It is checked against signatures made by
      OpenSSL (ui/test_passkeys.py); it is not a general-purpose crypto library.
Stored credentials are public keys only (ids and the point's x/y), in one JSON file under the data directory."""
import base64, collections, hashlib, json, os, secrets, tempfile, threading, time, urllib.parse

MAX_CREDENTIALS = 20
CHALLENGE_TTL_S = 300
MAX_CHALLENGES = 64
MAX_CBOR_DEPTH = 8
MAX_CBOR_ITEMS = 64
ALG_ES256 = -7

# NIST P-256 (secp256r1)
P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
A = P - 3
B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
     0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)


class PasskeyError(Exception):
    """A registration or assertion that must be refused. The message is safe to log and to return (never any secret)."""


def b64u(data):
    return base64.urlsafe_b64encode(bytes(data)).rstrip(b"=").decode("ascii")


def unb64u(text):
    if not isinstance(text, str) or not text or len(text) > 8192 or not all(c.isalnum() or c in "-_" for c in text):
        raise PasskeyError("malformed base64url")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except ValueError:
        raise PasskeyError("malformed base64url") from None


# ------------------------------------------------------------------------------------------------ P-256 verification
def on_curve(pt):
    x, y = pt
    return 0 <= x < P and 0 <= y < P and (y * y - (x * x * x + A * x + B)) % P == 0


def _add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2:
        if (y1 + y2) % P == 0:
            return None
        lam = (3 * x1 * x1 + A) * pow(2 * y1, -1, P) % P
    else:
        lam = (y2 - y1) * pow(x2 - x1, -1, P) % P
    x3 = (lam * lam - x1 - x2) % P
    return x3, (lam * (x1 - x3) - y1) % P


def _mul(k, pt):
    r = None
    while k:
        if k & 1:
            r = _add(r, pt)
        pt = _add(pt, pt)
        k >>= 1
    return r


def ecdsa_verify(pub, digest, r, s):
    """True when (r, s) is a valid ECDSA P-256 signature of the 32-byte `digest` under the public point `pub` = (x, y)."""
    if not (1 <= r < N and 1 <= s < N) or len(digest) != 32 or not on_curve(pub):
        return False
    z = int.from_bytes(digest, "big")
    w = pow(s, -1, N)
    pt = _add(_mul(z * w % N, G), _mul(r * w % N, pub))
    return pt is not None and pt[0] % N == r


def parse_der_signature(sig):
    """Strict DER ECDSA-Sig-Value -> (r, s). Anything else (BER tricks, trailing bytes, negative or padded integers) is refused."""
    def length(b, i):
        if i >= len(b):
            raise PasskeyError("malformed signature")
        n = b[i]
        if n < 0x80:
            return n, i + 1
        if n != 0x81 or i + 1 >= len(b) or b[i + 1] < 0x80:
            raise PasskeyError("malformed signature")
        return b[i + 1], i + 2

    def integer(b, i):
        if i >= len(b) or b[i] != 0x02:
            raise PasskeyError("malformed signature")
        n, i = length(b, i + 1)
        v = b[i:i + n]
        if n == 0 or len(v) != n or v[0] & 0x80 or (n > 1 and v[0] == 0 and not v[1] & 0x80):
            raise PasskeyError("malformed signature")
        return int.from_bytes(v, "big"), i + n

    if not sig or sig[0] != 0x30:
        raise PasskeyError("malformed signature")
    n, i = length(sig, 1)
    if i + n != len(sig):
        raise PasskeyError("malformed signature")
    r, i = integer(sig, i)
    s, i = integer(sig, i)
    if i != len(sig):
        raise PasskeyError("malformed signature")
    return r, s


# ------------------------------------------------------------------------------------------------ CBOR (the subset WebAuthn uses)
def cbor_decode(data, offset=0, _depth=0):
    """-> (value, next offset). Definite lengths only; ints, byte and text strings, arrays, maps, true/false/null. Bounded depth and size."""
    if _depth > MAX_CBOR_DEPTH or offset >= len(data):
        raise PasskeyError("malformed CBOR")
    ib = data[offset]
    major, info = ib >> 5, ib & 31
    i = offset + 1
    if info < 24:
        arg = info
    elif info in (24, 25, 26, 27):
        w = 1 << (info - 24)
        if i + w > len(data):
            raise PasskeyError("malformed CBOR")
        arg, i = int.from_bytes(data[i:i + w], "big"), i + w
    else:
        raise PasskeyError("unsupported CBOR (indefinite length or reserved)")
    if major == 0:
        return arg, i
    if major == 1:
        return -1 - arg, i
    if major in (2, 3):
        if i + arg > len(data):
            raise PasskeyError("malformed CBOR")
        raw = bytes(data[i:i + arg])
        if major == 3:
            try:
                return raw.decode("utf-8"), i + arg
            except UnicodeError:
                raise PasskeyError("malformed CBOR text") from None
        return raw, i + arg
    if major == 4:
        if arg > MAX_CBOR_ITEMS:
            raise PasskeyError("CBOR array too long")
        out = []
        for _ in range(arg):
            v, i = cbor_decode(data, i, _depth + 1)
            out.append(v)
        return out, i
    if major == 5:
        if arg > MAX_CBOR_ITEMS:
            raise PasskeyError("CBOR map too long")
        m = {}
        for _ in range(arg):
            k, i = cbor_decode(data, i, _depth + 1)
            v, i = cbor_decode(data, i, _depth + 1)
            if not isinstance(k, (int, str)) or isinstance(k, bool) or k in m:
                raise PasskeyError("bad CBOR map key")
            m[k] = v
        return m, i
    if major == 7 and info in (20, 21, 22):
        return {20: False, 21: True, 22: None}[info], i
    raise PasskeyError("unsupported CBOR type")


def cbor_loads(data):
    v, i = cbor_decode(data, 0)
    if i != len(data):
        raise PasskeyError("trailing bytes after CBOR")
    return v


def cose_es256_key(m):
    """COSE_Key map -> (x, y) of an ES256 P-256 public key, validated on the curve."""
    if not isinstance(m, dict) or m.get(1) != 2 or m.get(3) != ALG_ES256 or m.get(-1) != 1:
        raise PasskeyError("the credential key is not ES256 (P-256)")
    x, y = m.get(-2), m.get(-3)
    if not (isinstance(x, bytes) and isinstance(y, bytes) and len(x) == 32 and len(y) == 32):
        raise PasskeyError("malformed credential key")
    pt = (int.from_bytes(x, "big"), int.from_bytes(y, "big"))
    if not on_curve(pt):
        raise PasskeyError("the credential key is not on the P-256 curve")
    return pt


# ------------------------------------------------------------------------------------------------ WebAuthn ceremonies
FLAG_UP, FLAG_UV, FLAG_AT = 0x01, 0x04, 0x40


def _client_data(raw, want_type, challenge, origins):
    try:
        cd = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise PasskeyError("malformed clientDataJSON") from None
    if not isinstance(cd, dict) or cd.get("type") != want_type:
        raise PasskeyError("wrong ceremony type")
    got = cd.get("challenge")
    if not isinstance(got, str) or not secrets.compare_digest(got.encode("utf-8"), b64u(challenge).encode("ascii")):
        raise PasskeyError("challenge mismatch")
    if cd.get("origin") not in origins:
        raise PasskeyError("origin not allowed")
    if cd.get("crossOrigin") is True:
        raise PasskeyError("cross-origin ceremony")


def _auth_data_head(ad, rp_id):
    if len(ad) < 37:
        raise PasskeyError("authenticatorData too short")
    if not secrets.compare_digest(ad[:32], hashlib.sha256(rp_id.encode("utf-8")).digest()):
        raise PasskeyError("rpIdHash mismatch")
    flags = ad[32]
    if not flags & FLAG_UP:
        raise PasskeyError("user presence flag not set")
    if not flags & FLAG_UV:
        raise PasskeyError("user verification flag not set (a PIN or biometric is required)")
    return flags, int.from_bytes(ad[33:37], "big")


def verify_registration(response, challenge, rp_id, origins):
    """`response` = {id, clientDataJSON, attestationObject} (base64url strings). -> {id, x, y, sign_count} (x, y hex). Raises PasskeyError."""
    if not isinstance(response, dict):
        raise PasskeyError("malformed registration")
    _client_data(unb64u(response.get("clientDataJSON")), "webauthn.create", challenge, origins)
    att = cbor_loads(unb64u(response.get("attestationObject")))
    ad = att.get("authData") if isinstance(att, dict) else None
    if not isinstance(ad, bytes):
        raise PasskeyError("attestationObject has no authData")
    flags, count = _auth_data_head(ad, rp_id)
    if not flags & FLAG_AT or len(ad) < 55:
        raise PasskeyError("no attested credential data")
    cid_len = int.from_bytes(ad[53:55], "big")
    if not 16 <= cid_len <= 1023 or len(ad) < 55 + cid_len:
        raise PasskeyError("bad credential id length")
    cid = ad[55:55 + cid_len]
    key, end = cbor_decode(ad, 55 + cid_len)
    x, y = cose_es256_key(key)
    if unb64u(response.get("id")) != cid:
        raise PasskeyError("credential id mismatch")
    return {"id": b64u(cid), "x": "%064x" % x, "y": "%064x" % y, "sign_count": count}


def verify_assertion(response, cred, challenge, rp_id, origins):
    """`response` = {clientDataJSON, authenticatorData, signature}; `cred` = a stored credential. -> the new signature counter."""
    if not isinstance(response, dict):
        raise PasskeyError("malformed assertion")
    cdj = unb64u(response.get("clientDataJSON"))
    _client_data(cdj, "webauthn.get", challenge, origins)
    ad = unb64u(response.get("authenticatorData"))
    _flags, count = _auth_data_head(ad, rp_id)
    r, s = parse_der_signature(unb64u(response.get("signature")))
    digest = hashlib.sha256(ad + hashlib.sha256(cdj).digest()).digest()
    if not ecdsa_verify((int(cred["x"], 16), int(cred["y"], 16)), digest, r, s):
        raise PasskeyError("signature invalid")
    if (count or cred.get("sign_count", 0)) and count <= cred.get("sign_count", 0):
        raise PasskeyError("signature counter did not increase (cloned authenticator?)")
    return count


# ------------------------------------------------------------------------------------------------ state
class Challenges:
    """Single-use random challenges, 5 minute lifetime, at most 64 outstanding (oldest dropped). `kind` keeps a registration challenge
    from being used for a login and the other way round."""
    def __init__(self, clock=time.monotonic):
        self._clock, self._lock, self._d = clock, threading.Lock(), collections.OrderedDict()

    def issue(self, kind, subject=""):
        ch = os.urandom(32)
        with self._lock:
            self._d[ch] = (kind, subject, self._clock() + CHALLENGE_TTL_S)
            while len(self._d) > MAX_CHALLENGES:
                self._d.popitem(last=False)
        return ch

    def take(self, challenge_b64u, kind, subject=""):
        """-> the raw challenge bytes (consumed whether or not it matches the kind: one try per challenge), or None."""
        try:
            ch = unb64u(challenge_b64u)
        except PasskeyError:
            return None
        with self._lock:
            rec = self._d.pop(ch, None)
        if rec is None or rec[0] != kind or rec[1] != subject or rec[2] <= self._clock():
            return None
        return ch


class Store:
    """The credential file: {"version": 1, "credentials": [{id, x, y, sign_count, label, created, last_used}]}. Public keys only."""
    def __init__(self, path):
        self.path, self._lock = path, threading.Lock()

    def _load(self):
        try:
            with open(self.path, "rb") as f:
                d = json.loads(f.read(1 << 20).decode("utf-8"))
            return [c for c in d.get("credentials", []) if isinstance(c, dict) and all(k in c for k in ("id", "x", "y"))]
        except FileNotFoundError:
            return []
        except (OSError, ValueError, UnicodeError, AttributeError):
            raise PasskeyError("the passkey file is unreadable") from None

    def _save(self, creds):
        d = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(prefix=".passkeys-", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(json.dumps({"version": 1, "credentials": creds}, indent=1, sort_keys=True) + "\n")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def list(self):
        with self._lock:
            return self._load()

    def get(self, cid):
        return next((c for c in self.list() if c["id"] == cid), None)

    def add(self, cred, label, now):
        with self._lock:
            creds = self._load()
            if any(c["id"] == cred["id"] for c in creds):
                raise PasskeyError("this passkey is already registered")
            if len(creds) >= MAX_CREDENTIALS:
                raise PasskeyError("the passkey limit (%d) is reached" % MAX_CREDENTIALS)
            creds.append({"id": cred["id"], "x": cred["x"], "y": cred["y"], "sign_count": cred["sign_count"],
                          "label": (label or "")[:40], "created": int(now), "last_used": None})
            self._save(creds)

    def update_counter(self, cid, count, now):
        with self._lock:
            creds = self._load()
            for c in creds:
                if c["id"] == cid:
                    c["sign_count"], c["last_used"] = count, int(now)
            self._save(creds)

    def remove(self, cid):
        with self._lock:
            creds = self._load()
            keep = [c for c in creds if c["id"] != cid]
            if len(keep) == len(creds):
                return False
            self._save(keep)
            return True


def origin_problem(origin):
    """None when `origin` ("https://host[:port]", or http only for localhost / 127.0.0.1 / [::1]) is acceptable for WebAuthn, else why not."""
    try:
        u = urllib.parse.urlsplit(origin)
        host = u.hostname
        u.port
    except ValueError:
        return "not a valid origin"
    if u.scheme not in ("https", "http") or not host or u.path not in ("", "/") or u.query or u.fragment or u.username or u.password:
        return "must be scheme://host[:port] with no path"
    if u.scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"):
        return "WebAuthn needs HTTPS (plain http only on localhost)"
    return None


def normalize_origin(origin):
    u = urllib.parse.urlsplit(origin)
    return "%s://%s%s" % (u.scheme, ("[%s]" % u.hostname) if ":" in u.hostname else u.hostname, ":%d" % u.port if u.port else "")


class Passkeys:
    """What bma_ui.py holds when passkeys are on: the credential store, the challenges and the relying party (rp_id = the origin's host)."""
    def __init__(self, path, origins, clock=time.time):
        self.origins = frozenset(normalize_origin(o) for o in origins)
        hosts = {urllib.parse.urlsplit(o).hostname for o in self.origins}
        if len(hosts) != 1:
            raise PasskeyError("all --passkey-origin values must share one host name (it becomes the WebAuthn rp id)")
        self.rp_id = hosts.pop()
        self.store, self.challenges, self.clock = Store(path), Challenges(clock=clock), clock

    def registration_options(self, subject):
        ch = self.challenges.issue("register", subject)
        existing = [{"type": "public-key", "id": c["id"]} for c in self.store.list()]
        return {"challenge": b64u(ch), "rp": {"id": self.rp_id, "name": "BMA dashboard"},
                "user": {"id": b64u(hashlib.sha256(b"bma-owner|" + self.rp_id.encode()).digest()), "name": "owner", "displayName": "Dashboard owner"},
                "pubKeyCredParams": [{"type": "public-key", "alg": ALG_ES256}], "timeout": CHALLENGE_TTL_S * 1000, "attestation": "none",
                "authenticatorSelection": {"residentKey": "preferred", "userVerification": "required"}, "excludeCredentials": existing}

    def register(self, body, subject):
        ch = self.challenges.take(_challenge_of(body), "register", subject)
        if ch is None:
            raise PasskeyError("unknown or expired challenge")
        cred = verify_registration(body.get("credential"), ch, self.rp_id, self.origins)
        self.store.add(cred, body.get("label"), self.clock())
        return cred["id"]

    def login_options(self):
        ch = self.challenges.issue("login")
        return {"challenge": b64u(ch), "rpId": self.rp_id, "timeout": CHALLENGE_TTL_S * 1000, "userVerification": "required", "allowCredentials": []}

    def login(self, body):
        """-> the credential id. Raises PasskeyError (the caller counts it as a failed login)."""
        ch = self.challenges.take(_challenge_of(body), "login")
        if ch is None:
            raise PasskeyError("unknown or expired challenge")
        resp = body.get("credential")
        cid = unb64u(resp.get("id")) if isinstance(resp, dict) else None
        cred = self.store.get(b64u(cid)) if cid else None
        if cred is None:
            raise PasskeyError("unknown passkey")
        count = verify_assertion(resp, cred, ch, self.rp_id, self.origins)
        self.store.update_counter(cred["id"], count, self.clock())
        return cred["id"]


def _challenge_of(body):
    return body.get("challenge") if isinstance(body, dict) else None
