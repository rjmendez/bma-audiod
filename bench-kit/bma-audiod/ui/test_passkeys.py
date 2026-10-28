#!/usr/bin/env python3
"""Tests for ui/passkeys.py (WebAuthn, standard library only) and the passkey routes of bma_ui.py.

Nothing here is a stub of the unit under test: the signatures are made by the OpenSSL binary (an independent ECDSA implementation) through a
small software authenticator that builds real clientDataJSON / authenticatorData / attestationObject bytes, and the verifier is the real module.
NOT covered: a real browser or hardware authenticator (see ui/AUTH.md, Passkeys: designed for them, exercised only with this software one).

    python test_passkeys.py     (PASS/FAIL lines, a final 'N checks, 0 failure(s)' line; exit 77 when openssl is missing: SKIP, never a pass)"""

import hashlib, http.client, json, os, shutil, struct, subprocess, sys, tempfile, threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import auth as A  # noqa: E402
import bma_ui as U  # noqa: E402
import passkeys as PK  # noqa: E402

fails, n_checks = [], 0
TOKEN = "T0ken-" + "a1b2c3d4e5f6" * 4


def check(name, ok, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def cd(body):
    """The problem `code` of a response body (a dict), or None (a failing check, never a crash)."""
    return body.get("code") if isinstance(body, dict) else None


def raises(exc, match, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc as ex:
        return match in str(ex), str(ex)
    except Exception as ex:  # noqa: BLE001
        return False, f"{type(ex).__name__}: {ex}"
    return False, "nothing raised"


def find_openssl():
    ossl = shutil.which("openssl")
    if not ossl:
        cand = os.path.join(os.path.dirname(os.path.dirname(shutil.which("git") or "")), "usr", "bin", "openssl.exe")
        ossl = cand if os.path.isfile(cand) else None
    return ossl


OSSL = find_openssl()
if not OSSL:
    print("SKIP: the openssl binary was not found, so NOTHING here ran (this is not a pass)")
    sys.exit(77)


# ---------------------------------------------------------------------------------------------- a software authenticator (OpenSSL signs)
def cbor_uint(major, n):
    if n < 24:
        return bytes([major << 5 | n])
    for info, w in ((24, 1), (25, 2), (26, 4), (27, 8)):
        if n < 1 << (8 * w):
            return bytes([major << 5 | info]) + n.to_bytes(w, "big")
    raise ValueError(n)


def cbor_bytes(b):
    return cbor_uint(2, len(b)) + b


def cbor_text(t):
    return cbor_uint(3, len(t.encode())) + t.encode()


def cbor_int(n):
    return cbor_uint(0, n) if n >= 0 else cbor_uint(1, -1 - n)


class Soft:
    def __init__(self, d, name="k"):
        self.key = os.path.join(d, name + ".pem")
        subprocess.run([OSSL, "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", self.key], check=True, capture_output=True)
        der = subprocess.run([OSSL, "ec", "-in", self.key, "-pubout", "-outform", "DER"], check=True, capture_output=True).stdout
        pt = der[-65:]
        assert pt[0] == 4
        self.x, self.y = pt[1:33], pt[33:]
        self.cid = os.urandom(32)

    def sign(self, data):
        return subprocess.run([OSSL, "dgst", "-sha256", "-sign", self.key], input=data, check=True, capture_output=True).stdout

    def cose(self, alg=-7):
        return (
            b"\xa5"
            + cbor_int(1)
            + cbor_int(2)
            + cbor_int(3)
            + cbor_int(alg)
            + cbor_int(-1)
            + cbor_int(1)
            + cbor_int(-2)
            + cbor_bytes(self.x)
            + cbor_int(-3)
            + cbor_bytes(self.y)
        )

    def register(self, opts, origin, rp_id=None, flags=0x45, count=0, alg=-7, typ="webauthn.create", challenge=None, cid=None):
        rp = (rp_id or opts["rp"]["id"]).encode()
        cdj = json.dumps({"type": typ, "challenge": challenge or opts["challenge"], "origin": origin, "crossOrigin": False}).encode()
        cid = cid or self.cid
        ad = hashlib.sha256(rp).digest() + bytes([flags]) + struct.pack(">I", count) + bytes(16) + struct.pack(">H", len(cid)) + cid + self.cose(alg)
        att = b"\xa3" + cbor_text("fmt") + cbor_text("none") + cbor_text("attStmt") + b"\xa0" + cbor_text("authData") + cbor_bytes(ad)
        b = PK.b64u
        return {"id": b(self.cid), "clientDataJSON": b(cdj), "attestationObject": b(att)}

    def assertion(self, opts, origin, rp_id=None, flags=0x05, count=1, typ="webauthn.get", challenge=None, tamper=False):
        rp = (rp_id or opts["rpId"]).encode()
        cdj = json.dumps({"type": typ, "challenge": challenge or opts["challenge"], "origin": origin, "crossOrigin": False}).encode()
        ad = hashlib.sha256(rp).digest() + bytes([flags]) + struct.pack(">I", count)
        sig = self.sign(ad + hashlib.sha256(cdj).digest())
        if tamper:
            sig = sig[:-1] + bytes([sig[-1] ^ 1])
        b = PK.b64u
        return {"id": b(self.cid), "clientDataJSON": b(cdj), "authenticatorData": b(ad), "signature": b(sig)}


def run():
    tmp = tempfile.mkdtemp(prefix="bma_pk_")
    try:
        body(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def body(tmp):
    au = Soft(tmp)
    # ------------------------------------------------------------------------------------ ECDSA P-256 against OpenSSL signatures
    msg = b"the quick brown fox"
    digest = hashlib.sha256(msg).digest()
    r, s = PK.parse_der_signature(au.sign(msg))
    pub = (int.from_bytes(au.x, "big"), int.from_bytes(au.y, "big"))
    check("an OpenSSL signature verifies (positive twin of every negative below)", PK.ecdsa_verify(pub, digest, r, s) is True)
    check("the base point and the stored public key are on the curve", PK.on_curve(PK.G) and PK.on_curve(pub))
    check("another message does not verify", PK.ecdsa_verify(pub, hashlib.sha256(msg + b"!").digest(), r, s) is False)
    check("r + 1 does not verify", PK.ecdsa_verify(pub, digest, r + 1, s) is False)
    check("s + 1 does not verify", PK.ecdsa_verify(pub, digest, r, s + 1) is False)
    other = Soft(tmp, "other")
    check("another key does not verify", PK.ecdsa_verify((int.from_bytes(other.x, "big"), int.from_bytes(other.y, "big")), digest, r, s) is False)
    check("a point off the curve is refused", PK.ecdsa_verify((pub[0], pub[1] ^ 1), digest, r, s) is False)
    try:
        degenerate = PK.ecdsa_verify((0, 0), digest, r, s)
    except Exception as ex:  # noqa: BLE001 - the point (0, 0) must be refused up front, not blow up inside the arithmetic
        degenerate = repr(ex)
    check("the degenerate point (0, 0) is refused with False (not an exception from the arithmetic)", degenerate is False, str(degenerate))
    check("r = 0, s = 0 and s = n are refused", [PK.ecdsa_verify(pub, digest, a, b) for a, b in ((0, s), (r, 0), (r, PK.N))] == [False] * 3)
    check("a 31 byte digest is refused", PK.ecdsa_verify(pub, digest[:31], r, s) is False)
    # strict DER
    der = au.sign(msg)
    r2, s2 = PK.parse_der_signature(der)
    mini = lambda v: (b"\x00" if v.bit_length() % 8 == 0 else b"") + v.to_bytes((v.bit_length() + 7) // 8, "big")
    again = b"\x02" + bytes([len(mini(r2))]) + mini(r2) + b"\x02" + bytes([len(mini(s2))]) + mini(s2)
    check(
        "DER: a second OpenSSL signature parses, re-encodes to the identical bytes and verifies",
        der == b"\x30" + bytes([len(again)]) + again and PK.ecdsa_verify(pub, digest, r2, s2) is True,
    )
    for name, bad in (
        ("trailing byte", der + b"\x00"),
        ("truncated", der[:-1]),
        ("empty", b""),
        ("wrong tag", b"\x31" + der[1:]),
        ("negative integer", b"\x30\x06\x02\x01\x80\x02\x01\x01"),
        ("padded integer", b"\x30\x07\x02\x02\x00\x01\x02\x01\x01"),
        ("zero-length integer", b"\x30\x05\x02\x00\x02\x01\x01"),
        ("indefinite length", b"\x30\x80\x02\x01\x01\x02\x01\x01\x00\x00"),
    ):
        ok, why = raises(PK.PasskeyError, "malformed signature", PK.parse_der_signature, bad)
        check("DER: " + name + " is refused with 'malformed signature'", ok, why)
    check("DER: the smallest valid signature (1, 1) parses", PK.parse_der_signature(b"\x30\x06\x02\x01\x01\x02\x01\x01") == (1, 1))

    # ------------------------------------------------------------------------------------ CBOR (RFC 8949 appendix A vectors)
    vec = [
        (b"\x00", 0),
        (b"\x18\x18", 24),
        (b"\x19\x03\xe8", 1000),
        (b"\x20", -1),
        (b"\x38\x63", -100),
        (b"\x61a", "a"),
        (b"\x42\x01\x02", b"\x01\x02"),
        (b"\x83\x01\x02\x03", [1, 2, 3]),
        (b"\xa2\x01\x02\x03\x04", {1: 2, 3: 4}),
        (b"\xf5", True),
        (b"\xf4", False),
        (b"\xf6", None),
    ]
    check("CBOR: the RFC 8949 vectors decode exactly", [PK.cbor_loads(b) for b, _ in vec] == [v for _, v in vec])
    for name, bad, why in (
        (b"indefinite array", b"\x9f\x01\xff", "unsupported"),
        ("truncated", b"\x19\x03", "malformed"),
        ("trailing", b"\x01\x02", "trailing"),
        ("duplicate key", b"\xa2\x01\x02\x01\x03", "bad CBOR map key"),
        ("float", b"\xf9\x3c\x00", "unsupported"),
        ("nesting bomb", b"\x81" * 30 + b"\x00", "malformed"),
    ):
        ok, w = raises(PK.PasskeyError, why, PK.cbor_loads, bad)
        check("CBOR: %s is refused (%s)" % (name if isinstance(name, str) else name.decode(), why), ok, w)
    check(
        "CBOR: an array of exactly the item cap decodes, one more is refused",
        len(PK.cbor_loads(b"\x98\x40" + b"\x00" * 64)) == 64 and raises(PK.PasskeyError, "too long", PK.cbor_loads, b"\x98\x41" + b"\x00" * 65)[0],
    )

    # ------------------------------------------------------------------------------------ ceremonies
    origin = "https://dash.tailnet.example"
    clock = [1000.0]
    path = os.path.join(tmp, "passkeys.json")
    pk = PK.Passkeys(path, [origin], clock=lambda: clock[0])
    check("the rp id is the origin's host", pk.rp_id == "dash.tailnet.example")
    opts = pk.registration_options("s:one")
    check(
        "registration options: ES256 only, no attestation, user verification required, nothing excluded yet",
        (opts["pubKeyCredParams"], opts["attestation"], opts["authenticatorSelection"], opts["excludeCredentials"], opts["rp"]["id"])
        == ([{"type": "public-key", "alg": -7}], "none", {"residentKey": "preferred", "userVerification": "required"}, [], "dash.tailnet.example"),
    )
    reg = au.register(opts, origin)
    cid = pk.register({"challenge": opts["challenge"], "credential": reg, "label": "laptop"}, "s:one")
    stored = json.load(open(path))
    check(
        "registration succeeds and the file holds ONLY public data: id, x, y, counter, label, times",
        cid == PK.b64u(au.cid)
        and stored
        == {
            "version": 1,
            "credentials": [{"id": cid, "x": au.x.hex(), "y": au.y.hex(), "sign_count": 0, "label": "laptop", "created": 1000, "last_used": None}],
        },
        str(stored),
    )
    check("the file is LF-only JSON", b"\r" not in open(path, "rb").read())

    def reg_fail(why, **kw):
        o = pk.registration_options("s:one")
        a = Soft(tmp, "r%d" % len(os.listdir(tmp)))
        c = a.register(o, kw.pop("origin", origin), **kw)
        ok, w = raises(PK.PasskeyError, why, pk.register, {"challenge": o["challenge"], "credential": c}, kw.get("subject", "s:one"))
        return ok, w

    # positive twin first: a fresh authenticator registers (so each refusal below is about its one defect)
    o2 = pk.registration_options("s:one")
    a2 = Soft(tmp, "twin")
    check(
        "positive twin: a second passkey registers, and the options now exclude the first",
        pk.register({"challenge": o2["challenge"], "credential": a2.register(o2, origin)}, "s:one") == PK.b64u(a2.cid)
        and [e["id"] for e in pk.registration_options("s:one")["excludeCredentials"]] == [cid, PK.b64u(a2.cid)],
    )
    for why, kw in (
        ("origin not allowed", {"origin": "https://evil.example"}),
        ("rpIdHash mismatch", {"rp_id": "evil.example"}),
        ("user verification flag not set", {"flags": 0x41}),
        ("user presence flag not set", {"flags": 0x44}),
        ("no attested credential data", {"flags": 0x05}),
        ("not ES256", {"alg": -257}),
        ("wrong ceremony type", {"typ": "webauthn.get"}),
        ("challenge mismatch", {"challenge": PK.b64u(b"x" * 32)}),
        ("credential id mismatch", {"cid": os.urandom(32)}),
    ):
        ok, w = reg_fail(why, **kw)
        check("registration refused: " + why, ok, w)
    o3 = pk.registration_options("s:one")
    a3 = Soft(tmp, "rep")
    c3 = a3.register(o3, origin)
    pk.register({"challenge": o3["challenge"], "credential": c3}, "s:one")
    o8 = pk.registration_options("s:one")
    ok, w = raises(PK.PasskeyError, "already registered", pk.register, {"challenge": o8["challenge"], "credential": a3.register(o8, origin)}, "s:one")
    check("registration refused: the same credential id twice, with a fresh valid challenge", ok, w)
    o4 = pk.registration_options("s:one")
    ok1, w1 = raises(
        PK.PasskeyError,
        "unknown or expired challenge",
        pk.register,
        {"challenge": o4["challenge"], "credential": Soft(tmp, "o4").register(o4, origin)},
        "s:other",
    )
    check("registration refused: a challenge issued to session one cannot be used by session two", ok1, w1)
    o5 = pk.registration_options("s:one")
    clock[0] += PK.CHALLENGE_TTL_S + 1
    ok, w = raises(
        PK.PasskeyError,
        "unknown or expired challenge",
        pk.register,
        {"challenge": o5["challenge"], "credential": Soft(tmp, "o5").register(o5, origin)},
        "s:one",
    )
    check("registration refused: an expired challenge (301 s)", ok, w)
    o6 = pk.registration_options("s:one")
    c6 = Soft(tmp, "o6").register(o6, origin)
    pk.register({"challenge": o6["challenge"], "credential": c6}, "s:one")
    ok, w = raises(PK.PasskeyError, "unknown or expired challenge", pk.register, {"challenge": o6["challenge"], "credential": c6}, "s:one")
    check("a challenge is single use: replaying a successful registration is refused", ok, w)

    # login
    lo = pk.login_options()
    check(
        "login options: discoverable (no allow list), user verification required, rpId",
        (lo["allowCredentials"], lo["userVerification"], lo["rpId"]) == ([], "required", "dash.tailnet.example"),
    )
    check(
        "login succeeds with a counter of 1 and stores it",
        pk.login({"challenge": lo["challenge"], "credential": au.assertion(lo, origin, count=1)}) == cid
        and [c["sign_count"] for c in pk.store.list() if c["id"] == cid] == [1]
        and [c["last_used"] for c in pk.store.list() if c["id"] == cid] == [1301],
    )

    def login_fail(why, **kw):
        o = pk.login_options()
        ok, w = raises(PK.PasskeyError, why, pk.login, {"challenge": o["challenge"], "credential": au.assertion(o, kw.pop("origin", origin), **kw)})
        return ok, w

    for why, kw in (
        ("signature invalid", {"tamper": True}),
        ("origin not allowed", {"origin": "https://evil.example"}),
        ("rpIdHash mismatch", {"rp_id": "evil.example"}),
        ("user verification flag not set", {"flags": 0x01}),
        ("user presence flag not set", {"flags": 0x04}),
        ("wrong ceremony type", {"typ": "webauthn.create"}),
        ("challenge mismatch", {"challenge": PK.b64u(b"y" * 32)}),
        ("did not increase", {"count": 1}),
        ("did not increase", {"count": 0}),
    ):
        ok, w = login_fail(why, **kw)
        check("login refused: " + why + (" (counter %s)" % kw["count"] if "count" in kw else ""), ok, w)
    check(
        "after those refusals the stored counter is still 1 (a refused assertion changes nothing)",
        [c["sign_count"] for c in pk.store.list() if c["id"] == cid] == [1],
    )
    lo = pk.login_options()
    check("positive twin (real): counter 2 is accepted", pk.login({"challenge": lo["challenge"], "credential": au.assertion(lo, origin, count=2)}) == cid)
    lo = pk.login_options()
    stranger = Soft(tmp, "stranger")
    ok, w = raises(PK.PasskeyError, "unknown passkey", pk.login, {"challenge": lo["challenge"], "credential": stranger.assertion(lo, origin)})
    check("login refused: a credential that was never registered", ok, w)
    lo = pk.login_options()
    ok, w = raises(
        PK.PasskeyError, "unknown or expired challenge", pk.login, {"challenge": PK.b64u(b"z" * 32), "credential": au.assertion(lo, origin, count=3)}
    )
    check("login refused: a challenge the server never issued", ok, w)
    lo = pk.login_options()
    pk.login({"challenge": lo["challenge"], "credential": au.assertion(lo, origin, count=3)})
    ok, w = raises(PK.PasskeyError, "unknown or expired challenge", pk.login, {"challenge": lo["challenge"], "credential": au.assertion(lo, origin, count=4)})
    check("login refused: a challenge cannot be used twice", ok, w)
    sync = Soft(tmp, "sync")
    o7 = pk.registration_options("s:one")
    pk.register({"challenge": o7["challenge"], "credential": sync.register(o7, origin)}, "s:one")
    lo = pk.login_options()
    check(
        "a synced passkey whose counter is always 0 can log in repeatedly (0 then 0 is allowed)",
        pk.login({"challenge": lo["challenge"], "credential": sync.assertion(lo, origin, count=0)}) == PK.b64u(sync.cid),
    )
    lo = pk.login_options()
    check(
        "... and a second login with counter 0 works too",
        pk.login({"challenge": lo["challenge"], "credential": sync.assertion(lo, origin, count=0)}) == PK.b64u(sync.cid),
    )
    check(
        "the store removes a credential and reports an unknown one", (pk.store.remove(PK.b64u(sync.cid)), pk.store.remove(PK.b64u(sync.cid))) == (True, False)
    )

    # limits and origin rules
    ch = PK.Challenges(clock=lambda: 0)
    first = ch.issue("login")
    for _ in range(PK.MAX_CHALLENGES):
        ch.issue("login")
    check("at most 64 challenges are outstanding: the oldest is dropped, a newer one still works", ch.take(PK.b64u(first), "login") is None)
    check(
        "origin rules: https ok, http localhost ok, http elsewhere refused, path refused",
        [
            PK.origin_problem(o)
            for o in ("https://a.b.example", "http://localhost:48500", "http://127.0.0.1:1", "http://a.b.example", "https://a.b.example/x", "ftp://a")
        ]
        == [
            None,
            None,
            None,
            "WebAuthn needs HTTPS (plain http only on localhost)",
            "must be scheme://host[:port] with no path",
            "must be scheme://host[:port] with no path",
        ],
    )
    ok, w = raises(PK.PasskeyError, "share one host", PK.Passkeys, os.path.join(tmp, "x.json"), ["https://a.example", "https://b.example"])
    check("two origins with different hosts are refused (one rp id)", ok, w)
    check(
        "two origins with the same host (different ports) are accepted",
        PK.Passkeys(os.path.join(tmp, "x.json"), ["https://a.example", "https://a.example:8443"]).rp_id == "a.example",
    )
    bad = os.path.join(tmp, "bad.json")
    open(bad, "w").write("{not json")
    ok, w = raises(PK.PasskeyError, "unreadable", PK.Store(bad).list)
    check("a corrupt passkey file is an error, never silently 'no passkeys'", ok, w)
    check("a missing passkey file is simply empty", PK.Store(os.path.join(tmp, "nope.json")).list() == [])

    http_level(tmp)


# ---------------------------------------------------------------------------------------------- over real HTTP
class Rig:
    def __init__(self, tmp, passkeys=True, require_identity=False, allowed=None):
        self.root = tempfile.mkdtemp(prefix="bma_pkh_", dir=tmp)
        self.logs, self.t = [], [1000.0]
        pr, rp = os.path.join(self.root, "p"), os.path.join(self.root, "r")
        for d in (os.path.join(pr, "analysis"), os.path.join(pr, "logs"), rp):
            os.makedirs(d, exist_ok=True)
        board = U.Board("http://127.0.0.1:9", os.path.join(self.root, "nt"), enabled=False)
        app = U.App(pr, rp, os.path.join(self.root, "ru"), board, window_h=48, log=self.logs.append)
        self.auth = A.Authenticator(TOKEN, log=self.logs.append, clock=lambda: self.t[0], allowed_users=allowed, require_identity=require_identity)
        self.srv = U.Server(("127.0.0.1", 0), app, set(), U.load_assets(), auth=self.auth)
        self.port = self.srv.server_address[1]
        self.srv.allowed_hosts = U.allowed_hosts_for(self.port, [])
        self.origin = "http://localhost:%d" % self.port
        self.pk = None
        if passkeys:
            self.pk = PK.Passkeys(os.path.join(self.root, "passkeys.json"), [self.origin])
            self.srv.passkeys = self.pk
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()

    def req(self, method, path, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        h = {"Host": "localhost:%d" % self.port}
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        hd = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        return r.status, hd, data

    def post_json(self, path, obj, cookie=None, csrf=None, extra=None):
        h = {"Content-Type": "application/json"}
        if cookie:
            h["Cookie"] = cookie
        if csrf:
            h["X-CSRF-Token"] = csrf
        h.update(extra or {})
        s, hd, b = self.req("POST", path, h, json.dumps(obj))
        return s, hd, (json.loads(b) if b[:1] == b"{" else b)

    def login_session(self):
        s, hd, _ = self.req("POST", "/login", {"Content-Type": "application/x-www-form-urlencoded"}, "token=%s&csrf=%s" % (TOKEN, self.auth.login_csrf()))
        assert s == 303, s
        cookie = hd["set-cookie"].split(";")[0]
        s, _, b = self.req("GET", "/api/session", {"Cookie": cookie})
        return cookie, json.loads(b)["csrf"]


def http_level(tmp):
    r = Rig(tmp)
    soft = Soft(tmp, "http")
    try:
        s, hd, b = r.req("POST", "/api/passkeys/register/options", {"Content-Type": "application/json"}, "{}")
        check("registration options without a login: 401 (never reachable anonymously)", s == 401, str(s))
        cookie, csrf = r.login_session()
        s, hd, b = r.post_json("/api/passkeys/register/options", {}, cookie)
        check("registration options with a session but without the CSRF token: 403 csrf_failed", (s, cd(b)) == (403, "csrf_failed"), str((s, b)))
        s, hd, b = r.post_json("/api/passkeys/register/options", {}, cookie, "0" * 64)
        check("registration options with a WRONG CSRF token: 403 csrf_failed", (s, cd(b)) == (403, "csrf_failed"), str((s, b)))
        s, hd, b = r.post_json("/api/passkeys/register/options", {}, cookie, csrf, {"Origin": "https://evil.example"})
        check("registration options with the right CSRF token but a foreign Origin: 403 csrf_failed", (s, cd(b)) == (403, "csrf_failed"), str((s, b)))
        s, hd, opts = r.post_json("/api/passkeys/register/options", {}, cookie, csrf)
        check(
            "registration options with session + CSRF + same origin: 200 with a 43 character challenge and the rp id localhost",
            s == 200 and len(opts["challenge"]) == 43 and opts["rp"]["id"] == "localhost",
            str((s, opts)),
        )
        s, hd, b = r.post_json(
            "/api/passkeys/register/verify", {"challenge": opts["challenge"], "label": "test", "credential": soft.register(opts, r.origin)}, cookie, csrf
        )
        check("registration verify: 200 and the credential id", (s, b) == (200, {"ok": True, "id": PK.b64u(soft.cid)}), str((s, b)))
        s, hd, b = r.req("GET", "/api/passkeys", {"Cookie": cookie})
        lst = json.loads(b)
        check(
            "the list shows the id and label, no key material",
            s == 200 and set(lst["passkeys"][0]) == {"id", "label", "created_s", "last_used_s"} and lst["passkeys"][0]["label"] == "test",
            str(lst),
        )
        s, hd, b = r.req("GET", "/api/session", {"Cookie": cookie})
        check("/api/session reports one passkey", json.loads(b)["passkeys"] == {"enabled": True, "count": 1}, b.decode())
        o2 = r.post_json("/api/passkeys/register/options", {}, cookie, csrf)[2]
        s, hd, b = r.post_json(
            "/api/passkeys/register/verify", {"challenge": o2["challenge"], "credential": soft.register(o2, r.origin, flags=0x41)}, cookie, csrf
        )
        check(
            "registration verify with no user verification: 400 invalid_request naming the reason",
            (s, cd(b), "user verification" in b["detail"]) == (400, "invalid_request", True),
            str((s, b)),
        )
        cookie2, csrf2 = r.login_session()
        o3 = r.post_json("/api/passkeys/register/options", {}, cookie, csrf)[2]
        s, hd, b = r.post_json(
            "/api/passkeys/register/verify", {"challenge": o3["challenge"], "credential": Soft(tmp, "h3").register(o3, r.origin)}, cookie2, csrf2
        )
        check("a challenge issued to session one cannot be verified by session two: 400", (s, cd(b)) == (400, "invalid_request"), str((s, b)))

        # login without any credential
        s, hd, lo = r.post_json("/api/passkeys/login/options", {})
        check("login options need no login: 200 discoverable", s == 200 and lo["allowCredentials"] == [] and lo["rpId"] == "localhost", str((s, lo)))
        s, hd, b = r.post_json("/api/passkeys/login/verify", {"challenge": lo["challenge"], "credential": soft.assertion(lo, r.origin, count=1)})
        sc = hd.get("set-cookie", "")
        check(
            "login verify: 200 {ok: true} and a session cookie with the standard attributes",
            s == 200
            and b == {"ok": True}
            and sc.endswith("; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000")
            and len(sc.split(";")[0].split("=")[1]) == 64,
            str((s, b, sc)),
        )
        pcookie = sc.split(";")[0]
        check("the passkey session cookie opens the API (positive twin of the refusals)", r.req("GET", "/api/status", {"Cookie": pcookie})[0] == 200)
        lo = r.post_json("/api/passkeys/login/options", {})[2]
        s, hd, b = r.post_json("/api/passkeys/login/verify", {"challenge": lo["challenge"], "credential": soft.assertion(lo, r.origin, count=2, tamper=True)})
        check(
            "a bad assertion: 401 unauthorized, no cookie, one failure counted in the shared lockout",
            (s, cd(b), "set-cookie" in hd, r.auth.failures("127.0.0.1")) == (401, "unauthorized", False, 1),
            str((s, b, hd)),
        )
        s, hd, b = r.post_json(
            "/api/passkeys/login/verify",
            {"challenge": lo["challenge"], "credential": soft.assertion(lo, r.origin, count=2)},
            extra={"Origin": "https://evil.example"},
        )
        check(
            "a login verify with a foreign Origin: 403 csrf_failed (and not counted)",
            (s, cd(b), r.auth.failures("127.0.0.1")) == (403, "csrf_failed", 1),
            str((s, b)),
        )
        for _ in range(3):
            lo = r.post_json("/api/passkeys/login/options", {})[2]
            r.post_json("/api/passkeys/login/verify", {"challenge": lo["challenge"], "credential": soft.assertion(lo, r.origin, count=9, tamper=True)})
        lo_valid = r.post_json("/api/passkeys/login/options", {})[2]  # fetched before the lock: a locked address gets no options either
        lo = r.post_json("/api/passkeys/login/options", {})[2]
        r.post_json(
            "/api/passkeys/login/verify", {"challenge": lo["challenge"], "credential": soft.assertion(lo, r.origin, count=9, tamper=True)}
        )  # the fifth failure
        s, hd, b = r.post_json("/api/passkeys/login/verify", {"challenge": lo_valid["challenge"], "credential": soft.assertion(lo_valid, r.origin, count=9)})
        check(
            "five failed passkey logins lock the address: the sixth, with a VALID assertion, is 429 with Retry-After 60",
            (s, hd.get("retry-after"), cd(b)) == (429, "60", "too_many_attempts"),
            str((s, hd, b)),
        )
        r.t[0] += 60
        lo = r.post_json("/api/passkeys/login/options", {})[2]
        s, hd, b = r.post_json("/api/passkeys/login/verify", {"challenge": lo["challenge"], "credential": soft.assertion(lo, r.origin, count=9)})
        check("positive twin: after the lock expires the same kind of valid assertion signs in", (s, b) == (200, {"ok": True}), str((s, b)))
        s, hd, b = r.post_json("/api/passkeys/delete", {"id": PK.b64u(soft.cid)}, cookie, csrf)
        check(
            "delete removes the passkey (CSRF required), and the list is empty",
            (s, b) == (200, {"ok": True}) and json.loads(r.req("GET", "/api/passkeys", {"Cookie": cookie})[2]) == {"passkeys": []},
            str((s, b)),
        )
    finally:
        r.close()

    r = Rig(tmp, passkeys=False)
    try:
        cookie, csrf = r.login_session()
        got = [
            r.req("GET", "/api/passkeys", {"Cookie": cookie})[0],
            r.post_json("/api/passkeys/login/options", {})[0],
            r.post_json("/api/passkeys/register/options", {}, cookie, csrf)[0],
        ]
        check(
            "without --passkey-origin every passkey route is 404 (the feature is off), the login page has no passkey button",
            got == [404, 404, 404] and b"passkey-login" not in r.req("GET", "/login")[2],
            str(got),
        )
        check(
            "/api/session says passkeys are disabled",
            json.loads(r.req("GET", "/api/session", {"Cookie": cookie})[2])["passkeys"] == {"enabled": False, "count": 0},
        )
    finally:
        r.close()
    r = Rig(tmp, passkeys=True)
    try:
        page = r.req("GET", "/login")[2]
        check(
            "with passkeys on, the login page carries the passkey button and loads /static/login.js",
            b'id="passkey-login"' in page and b'src="/static/login.js"' in page,
        )
    finally:
        r.close()

    # the optional import: a copy of bma_ui.py WITHOUT passkeys.py must refuse the flag with a clear message; the same copy WITH it reaches the origin check
    for with_pk in (False, True):
        d = tempfile.mkdtemp(prefix="bma_pkimp_", dir=tmp)
        for f in ("bma_ui.py", "auth.py", "taxa.json") + (("passkeys.py",) if with_pk else ()):
            shutil.copy(os.path.join(HERE, f), d)
        root = os.path.join(d, "data")
        os.makedirs(os.path.join(root, "analysis"))
        os.makedirs(os.path.join(root, "logs"))
        open(os.path.join(root, "dashboard_token.txt"), "w", newline="").write(TOKEN)
        p = subprocess.run(
            [sys.executable, "-I", os.path.join(d, "bma_ui.py"), "--pipeline-root", root, "--no-board", "--passkey-origin", "http://dash.tailnet.example"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        want = "WebAuthn needs HTTPS" if with_pk else "needs ui/passkeys.py, which could not be imported"
        check(
            "passkeys.py %s: exit 2 and the message names %r" % ("present, plain-http origin" if with_pk else "absent", want),
            p.returncode == 2 and want in p.stderr,
            p.stderr[-300:],
        )


run()
print(f"\n{n_checks} checks, {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
