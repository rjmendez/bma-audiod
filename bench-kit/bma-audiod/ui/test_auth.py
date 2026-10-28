#!/usr/bin/env python3
"""Tests for the dashboard login (ui/auth.py wired into bma_ui.py). Real servers on ephemeral loopback ports, real HTTP requests,
the real process for the startup rules; only the clock of the lockout is replaced by a controllable one and two recorders wrap
(and delegate to) the real compare function and the app's log. See ui/AUTH.md.

    python test_auth.py     (PASS/FAIL lines, a final 'N checks, 0 failure(s)' line; exit 1 on failure; exit 77 when ONLY the
                              TLS checks could not run because openssl is missing: that is reported as SKIP, never as a pass)"""

import base64, hashlib, http.client, json, os, shutil, socket, ssl, subprocess, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bma_ui as U  # noqa: E402
import auth as A  # noqa: E402

fails, skips, n_checks = [], [], 0
TOKEN = "T0ken-" + "a1b2c3d4e5f6" * 4  # 54 characters
WRONG = "WRONGpw-" + "z9y8x7w6" * 4
LOCAL = "192.0.2.77"  # a client address for the direct (non-socket) checks


def check(name, ok, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def basic(user, pw):
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def req(port, path="/", method="GET", headers=None, host="default", raw_conn=None):
    c = raw_conn or http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    c.putheader("Host", f"localhost:{port}" if host == "default" else host)
    for k, v in (headers or {}).items():
        c.putheader(k, v)
    c.endheaders()
    r = c.getresponse()
    body = r.read()
    hd = r.getheaders()
    c.close()
    return r.status, {k.lower(): v for k, v in hd}, body, hd


def tiny_app(root, log):
    pr, rp = os.path.join(root, "pipeline"), os.path.join(root, "reports")
    for d in (os.path.join(pr, "analysis"), os.path.join(pr, "logs"), rp):
        os.makedirs(d, exist_ok=True)
    board = U.Board("http://127.0.0.1:9", os.path.join(root, "no_token"), enabled=False)
    return U.App(pr, rp, os.path.join(root, "rollups"), board, window_h=48, log=log)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Rig:
    """One real server (auth optional) with a recorded log and a controllable lockout clock."""

    def __init__(self, auth=True, token=TOKEN, key=None, **srv_kw):
        self.root = tempfile.mkdtemp(prefix="bma_auth_")
        self.logs, self.clock = [], Clock()
        self.auth = A.Authenticator(token, log=self.logs.append, clock=self.clock, key=key, secure=srv_kw.pop("secure", False)) if auth else None
        self.app = tiny_app(self.root, self.logs.append)
        self.srv = U.Server(("127.0.0.1", 0), self.app, set(), U.load_assets(), auth=self.auth, **srv_kw)
        self.port = self.srv.server_address[1]
        self.srv.allowed_hosts = U.allowed_hosts_for(self.port, [])
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.root, ignore_errors=True)

    def get(self, path="/", headers=None, method="GET", host="default"):
        return req(self.port, path, method, headers, host)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def raises(exc, match, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc as ex:
        return match in str(ex), str(ex)
    except Exception as ex:  # noqa: BLE001
        return False, f"{type(ex).__name__}: {ex}"
    return False, "nothing raised"


ROUTES4 = ["/", "/static/app.js", "/api/status", "/api/detections/events"]


def run():
    asset_js = open(os.path.join(HERE, "static", "app.js"), "rb").read()

    # ---------------------------------------------------------------- 1. open without auth, closed with it
    r = Rig(auth=False)
    got = [r.get(p)[0] for p in ROUTES4]
    check("no auth (loopback, no token file): / , app.js, /api/status, /api/detections/events are all 200", got == [200] * 4, str(got))
    check("no auth: no Authorization needed and no WWW-Authenticate sent", "www-authenticate" not in r.get("/api/status")[1])
    r.close()
    r = Rig()
    got = [r.get(p)[0] for p in ROUTES4]
    check("with a token: / , app.js, /api/status, /api/detections/events are all 401", got == [401] * 4, str(got))
    s, h, b, _ = r.get("/static/app.js")
    check(
        "401 carries WWW-Authenticate Basic with the realm, no-store and the problem media type",
        h.get("www-authenticate") == 'Basic realm="BMA dashboard", charset="UTF-8"'
        and h.get("cache-control") == "no-store"
        and h.get("content-type") == "application/problem+json",
        str(h),
    )
    check(
        "401 body is the exact problem payload (code unauthorized, the app.js bytes are NOT in it)",
        b
        == b'{"type":"urn:bma-ui:problem:unauthorized","title":"Authentication required","status":401,"code":"unauthorized",'
        b'"detail":"send the dashboard token (HTTP Basic password, or open /?token=...)"}'
        and asset_js[:40] not in b,
        b.decode()[:200],
    )
    ch = {m: r.get("/api/status", {"Sec-Fetch-Mode": m})[1].get("www-authenticate") for m in ("navigate", "cors", "same-origin", "no-cors")}
    check(
        "the Basic challenge is sent to navigations and to clients without Sec-Fetch-Mode, and omitted for browser fetches (cors, same-origin, no-cors); all are 401",
        ch == {"navigate": 'Basic realm="BMA dashboard", charset="UTF-8"', "cors": None, "same-origin": None, "no-cors": None}
        and r.get("/api/status", {"Sec-Fetch-Mode": "cors"})[0] == 401
        and "www-authenticate" in r.get("/api/status")[1],
        str(ch),
    )
    check("unknown route and bad query are 401 too (no route probing without a login)", [r.get("/nope")[0], r.get("/api/status?bogus=1")[0]] == [401, 401])
    r.close()

    # ---------------------------------------------------------------- 2. HTTP Basic
    r = Rig()
    outs = {p: r.get(p, {"Authorization": basic("anyone", TOKEN)}) for p in ROUTES4}
    check("Basic with the right token: all four routes 200", [outs[p][0] for p in ROUTES4] == [200] * 4, str({p: outs[p][0] for p in ROUTES4}))
    check(
        "Basic ok: app.js is served byte for byte and every authenticated response is Cache-Control: no-store",
        outs["/static/app.js"][2] == asset_js and all(outs[p][1].get("cache-control") == "no-store" for p in ROUTES4),
        str([outs[p][1].get("cache-control") for p in ROUTES4]),
    )
    check(
        "Basic ok: any user name works (empty user too)",
        [r.get("/api/status", {"Authorization": basic(u, TOKEN)})[0] for u in ("", "owner", "x y")] == [200] * 3,
    )
    bad = {
        "wrong password": basic("u", WRONG),
        "empty password": basic("u", ""),
        "token as the user name only": basic(TOKEN, ""),
        "token with a trailing space": basic("u", TOKEN + " "),
        "wrong scheme (Bearer)": "Bearer " + TOKEN,
        "scheme only": "Basic",
        "not base64": "Basic !!!!",
        "no colon": "Basic " + base64.b64encode(TOKEN.encode()).decode(),
    }
    got = {}
    for name, hv in bad.items():
        got[name] = r.get("/api/status", {"Authorization": hv})[0]
        r.get("/api/status", {"Authorization": basic("u", TOKEN)})  # a good request after each bad one clears the counter: stays under the lockout
    check(
        "Basic with a wrong / empty / wrong-scheme / user-name-only credential is 401 (positive twin above: the right one is 200)",
        set(got.values()) == {401} and len(got) == 8,
        str(got),
    )
    r.close()

    # ---------------------------------------------------------------- 3. cookie flow
    r = Rig()
    s, h, b, hd = r.get("/?token=" + TOKEN)
    sc = [v for k, v in hd if k.lower() == "set-cookie"]
    m = sc[0] if len(sc) == 1 else ""
    value = m.split(";")[0].split("=", 1)[1] if m else ""
    check(
        "GET /?token=<right> is 302 to / with no body and no-store",
        (s, h.get("location"), b, h.get("cache-control")) == (302, "/", b"", "no-store"),
        str((s, h, b)),
    )
    check(
        "Set-Cookie has exactly these attributes: HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000 (no Secure over plain HTTP)",
        m == f"bma_auth={value}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000" and len(value) == 64 and all(c in "0123456789abcdef" for c in value),
        m,
    )
    check("the cookie value is not the token and does not contain it", TOKEN not in m and value != TOKEN)
    cookie = {"Cookie": f"bma_auth={value}"}
    got = [r.get(p, cookie)[0] for p in ROUTES4]
    check("the cookie authenticates /api/status and the other routes (200)", got == [200] * 4, str(got))
    check("the cookie works among other cookies", r.get("/api/status", {"Cookie": f"a=1; bma_auth={value}; b=2"})[0] == 200)
    check(
        "a tampered cookie (last hex digit changed) is 401",
        r.get("/api/status", {"Cookie": "bma_auth=" + value[:-1] + ("0" if value[-1] != "0" else "1")})[0] == 401,
    )
    check("a stale cookie is not counted as a failed login (no lockout state)", r.auth.tracked() == 0, str(r.auth.tracked()))
    s2, h2, b2, hd2 = r.get("/?token=" + WRONG)
    check("GET /?token=<wrong> is 401 and sets no cookie", s2 == 401 and not [1 for k, v in hd2 if k.lower() == "set-cookie"], str((s2, hd2)))
    check(
        "?token= is honoured on / only: /api/status?token=<right> and /index.html?token=<right> are 401",
        [r.get("/api/status?token=" + TOKEN)[0], r.get("/index.html?token=" + TOKEN)[0]] == [401, 401],
    )
    s3, h3, _, _ = r.get("/?token=" + TOKEN, method="HEAD")
    check("HEAD /?token=<right> redirects too", (s3, h3.get("location")) == (302, "/"), str((s3, h3)))
    r.close()
    ra, rb = Rig(), Rig()
    va = ra.auth.cookie_header().split(";")[0]
    vb = rb.auth.cookie_header().split(";")[0]
    check("two server instances with the same token make different cookies (per-process key)", va != vb)
    check(
        "a cookie made by another instance is rejected (401), the same instance accepts its own (200)",
        (rb.get("/api/status", {"Cookie": va})[0], rb.get("/api/status", {"Cookie": vb})[0]) == (401, 200),
    )
    ra.close()
    rb.close()
    key = os.urandom(32)
    rc, rd = Rig(key=key), Rig(key=key)
    check("same key and token give the same cookie (the cookie is a function of the key and the token)", rc.auth.cookie_header() == rd.auth.cookie_header())
    rd2 = Rig(token=TOKEN + "x", key=key)
    check(
        "a different token gives a different cookie under the same key (a changed token invalidates old cookies)",
        rc.auth.cookie_header() != rd2.auth.cookie_header(),
    )
    rc.close()
    rd.close()
    rd2.close()
    check(
        "secure=True adds Secure to the cookie, secure=False does not",
        A.Authenticator(TOKEN, secure=True).cookie_header().endswith("; Path=/; Max-Age=2592000; Secure")
        and not A.Authenticator(TOKEN, secure=False).cookie_header().endswith("Secure"),
    )

    # ---------------------------------------------------------------- 4. HEAD
    r = Rig()
    sg, hg, bg, _ = r.get("/api/status")
    sh, hh, bh, _ = r.get("/api/status", method="HEAD")
    check(
        "HEAD without credentials is 401 with the same challenge and length as GET and no body",
        (sh, bh) == (401, b"")
        and hh.get("www-authenticate") == hg.get("www-authenticate")
        and hh.get("content-length") == hg.get("content-length") == str(len(bg)),
        str((sh, hh)),
    )
    auth = {"Authorization": basic("u", TOKEN)}
    sg2, hg2, bg2, _ = r.get("/static/app.js", auth)
    sh2, hh2, bh2, _ = r.get("/static/app.js", auth, method="HEAD")
    check(
        "HEAD with credentials is 200, empty body, Content-Length equal to the GET body",
        (sh2, bh2) == (200, b"") and hh2.get("content-length") == str(len(bg2)) == str(len(asset_js)),
        str((sh2, hh2)),
    )
    sp, _, _, _ = r.get("/api/status", method="POST")
    sp2, hp2, _, _ = r.get("/api/status", auth, method="POST")
    check(
        "POST without credentials is 401 (not 405); with credentials it is 405 Allow GET, HEAD",
        (sp, sp2, hp2.get("allow")) == (401, 405, "GET, HEAD"),
        str((sp, sp2, hp2)),
    )
    r.close()

    # ---------------------------------------------------------------- 5. lockout
    r = Rig()
    wrong = {"Authorization": basic("u", WRONG)}
    first5 = [r.get("/api/status", wrong)[0] for _ in range(5)]
    s6, h6, b6, _ = r.get("/api/status", wrong)
    check(
        "five wrong credentials are 401 each, the sixth is 429 with Retry-After 60 and a problem body",
        first5 == [401] * 5
        and (s6, h6.get("retry-after"), h6.get("content-type")) == (429, "60", "application/problem+json")
        and b'"code":"too_many_attempts"' in b6,
        str((first5, s6, h6)),
    )
    s7, h7, _, _ = r.get("/api/status", {"Authorization": basic("u", TOKEN)})
    s8, _, _, _ = r.get("/api/status")
    check(
        "during the lockout even the correct token is refused (429), and so is a request without credentials",
        (s7, h7.get("retry-after"), s8) == (429, "60", 429),
        str((s7, s8)),
    )
    r.clock.t += 59
    s9, h9, _, _ = r.get("/api/status", {"Authorization": basic("u", TOKEN)})
    check("59 s later it is still locked (429, Retry-After 1)", (s9, h9.get("retry-after")) == (429, "1"), str((s9, h9)))
    r.clock.t += 1
    s10, _, _, _ = r.get("/api/status", {"Authorization": basic("u", TOKEN)})
    check("at 60 s the lockout is over: the correct token is 200", s10 == 200, str(s10))
    check(
        "a success clears the counter: four more failures do not lock out, the fifth failure locks",
        [r.get("/api/status", wrong)[0] for _ in range(4)] == [401] * 4 and r.get("/api/status", {"Authorization": basic("u", TOKEN)})[0] == 200,
        "",
    )
    r.close()
    r = Rig()
    secs = []
    for i in range(7):  # failures 5..11: each lock doubles up to the 900 s cap
        for _ in range(5 if i == 0 else 1):
            r.get("/api/status", wrong)
        s, h, _, _ = r.get("/api/status", wrong)
        secs.append((s, h.get("retry-after")))
        r.clock.t += int(h.get("retry-after"))
    check(
        "the backoff doubles: 60, 120, 240, 480, 900, 900, 900 (15 minute cap)",
        secs == [(429, x) for x in ("60", "120", "240", "480", "900", "900", "900")],
        str(secs),
    )
    r.close()
    r = Rig()
    for _ in range(4):
        r.get("/api/status", wrong)
    check("four failures are not a lockout (the correct token is still 200)", r.get("/api/status", {"Authorization": basic("u", TOKEN)})[0] == 200)
    check(
        "failures are logged as one line with the IP and a counter, and nothing else",
        [m for m in r.logs if m.startswith("auth ")] == [f"auth failure ip=127.0.0.1 failures={i}" for i in (1, 2, 3, 4)],
        str(r.logs),
    )
    r.close()
    a = A.Authenticator(TOKEN, clock=Clock())
    for _ in range(5):
        a.record_failure("192.0.2.1")
    check(
        "lockout is per client address: another address is not locked, the failing one is",
        (a.check("/", "", None, [], "192.0.2.2").kind, a.check("/", "", None, [], "192.0.2.1").kind) == ("unauthorized", "locked"),
    )
    a = A.Authenticator(TOKEN, clock=Clock())
    for i in range(5000):
        a.record_failure("198.%d.%d.%d" % (i >> 16 & 255, i >> 8 & 255, i & 255))
    check(
        "memory is bounded: 5000 distinct failing addresses leave exactly MAX_TRACKED, the oldest evicted, the newest kept",
        a.tracked() == A.MAX_TRACKED == 1024 and a.failures("198.0.0.0") == 0 and a.failures("198.0.19.135") == 1,
        str(a.tracked()),
    )
    a = A.Authenticator(TOKEN, clock=Clock())
    for i in range(10):
        a.record_failure("198.0.0.%d" % i)
    check(
        "positive twin: under the bound nothing is evicted (10 addresses, 10 tracked, the first still counted)",
        a.tracked() == 10 and a.failures("198.0.0.0") == 1,
    )
    clk = Clock()
    a = A.Authenticator(TOKEN, clock=clk)
    a.record_failure("192.0.2.5")
    clk.t += A.FORGET_S + 1
    a.record_failure("192.0.2.5")
    check("an old failure is forgotten after an hour (the next one counts as the first)", a.failures("192.0.2.5") == 1)

    # ---------------------------------------------------------------- 6. nothing secret in logs or bodies
    r = Rig()
    bodies, hdrs = [], []
    for hv in (
        {"Authorization": basic("owner", WRONG)},
        {"Authorization": basic("owner", TOKEN)},
        {"Authorization": "Bearer " + WRONG},
        {"Cookie": "bma_auth=" + WRONG},
        {},
    ):
        for path in ("/api/status", "/nope?token=" + WRONG, "/?token=" + WRONG, "/api/detections/event?id=" + WRONG):
            s, h, b, hd = r.get(path, hv)
            bodies.append(b)
            hdrs += [(k, v) for k, v in hd if k.lower() != "set-cookie"]
    r.clock.t += 5000  # the wrong attempts above locked this client out; wait it out, then log in for real
    s, h, b, hd = r.get("/?token=" + TOKEN)
    ck = [v for k, v in hd if k.lower() == "set-cookie"][0].split(";")[0].split("=", 1)[1]
    r.get("/api/status", {"Cookie": "bma_auth=" + ck})
    secrets = [TOKEN, WRONG, ck, base64.b64encode(f"owner:{TOKEN}".encode()).decode(), base64.b64encode(f"owner:{WRONG}".encode()).decode()]
    blob = "\n".join(r.logs).encode() + b"\n".join(bodies) + "\n".join(k + ": " + v for k, v in hdrs).encode()
    check(
        "the token, the wrong password, the cookie value and the Authorization values appear in no log line, error body or non-cookie header",
        not [s_ for s_ in secrets if s_.encode() in blob] and len(r.logs) > 5,
        str([s_ for s_ in secrets if s_.encode() in blob]),
    )
    check(
        "positive twin: the log does hold the failure lines (IP and counter) and the 401 lines (method, path without query, code)",
        any(m.startswith("auth failure ip=127.0.0.1 failures=") for m in r.logs) and "GET /api/status 401" in r.logs and "GET /nope 401" in r.logs,
        str(r.logs[:6]),
    )
    r.close()

    # ---------------------------------------------------------------- 7. constant-time compare
    check(
        "ct_equal: equal, different, longer, empty, bytes vs str, non-ASCII",
        [
            A.ct_equal("abc", "abc"),
            A.ct_equal("abc", "abd"),
            A.ct_equal("abc", "abcd"),
            A.ct_equal("", ""),
            A.ct_equal("", "a"),
            A.ct_equal(b"abc", "abc"),
            A.ct_equal("é", "é"),
            A.ct_equal("é", "e"),
        ]
        == [True, False, False, True, False, True, True, False],
    )
    calls = []
    real = A.hmac.compare_digest

    def spy(x, y):
        calls.append((bytes(x), bytes(y)))
        return real(x, y)

    A.hmac.compare_digest = spy  # a recorder that delegates: the real comparison still decides
    try:
        r = Rig()
        th = hashlib.sha256(TOKEN.encode()).digest()
        calls.clear()
        s_ok = r.get("/api/status", {"Authorization": basic("u", TOKEN)})[0]
        ok_calls = list(calls)
        calls.clear()
        s_bad = r.get("/api/status", {"Authorization": basic("u", WRONG)})[0]
        bad_calls = list(calls)
        calls.clear()
        s_tok = r.get("/?token=" + TOKEN)[0]
        tok_calls = list(calls)
        r.close()
    finally:
        A.hmac.compare_digest = real
    check(
        "requests compare secrets with hmac.compare_digest on 32-byte digests: the right Basic password matches the token digest",
        s_ok == 200 and ok_calls and all(len(x) == len(y) == 32 for x, y in ok_calls) and (th, th) in ok_calls,
        str(ok_calls)[:200],
    )
    check(
        "a wrong Basic password still runs the comparisons (no early exit) and none of them matches the token digest",
        s_bad == 401 and len(bad_calls) >= 2 and all(not (x == y == th) for x, y in bad_calls) and any(x == th or y == th for x, y in bad_calls),
        str(len(bad_calls)),
    )
    check("the ?token= check also goes through the constant-time compare", s_tok == 302 and (th, th) in tok_calls)

    # ---------------------------------------------------------------- 8. startup rules
    d = tempfile.mkdtemp(prefix="bma_auth_cfg_")
    try:
        tf = os.path.join(d, "dashboard_token.txt")
        ok, msg = raises(A.AuthConfigError, "refusing to listen on 0.0.0.0 without a login", A.configure, "0.0.0.0", d)
        check("0.0.0.0 with no token file refuses to start", ok, msg)
        check("... and the message names the file to create and the escape hatch", tf in msg and "--auth-token-file" in msg and "--no-auth" in msg, msg)
        check("positive twin: 127.0.0.1 with no token file starts open (no auth, no notices)", A.configure("127.0.0.1", d, log=None)[::2] == (None, []))
        check(
            "[::1] and localhost with no token file also start open; [::] and a host name refuse",
            [A.configure(b, d)[0] for b in ("::1", "localhost")] == [None, None]
            and [raises(A.AuthConfigError, "refusing", A.configure, b, d)[0] for b in ("::", "ui.example", "")] == [True] * 3,
        )
        a_, tls_, notes = A.configure("0.0.0.0", d, no_auth=True)
        check(
            "--no-auth lets 0.0.0.0 start without a login, with a loud WARNING notice",
            a_ is None and len(notes) == 1 and notes[0].startswith("WARNING: --no-auth") and "NO login" in notes[0],
            str(notes),
        )
        open(tf, "wb").write(TOKEN.encode())
        a_, _, notes = A.configure("0.0.0.0", d)
        check("positive twin: with the default token file present 0.0.0.0 starts WITH a login", isinstance(a_, A.Authenticator) and notes == [])
        ok, msg = raises(A.AuthConfigError, "does not exist", A.configure, "127.0.0.1", d, os.path.join(d, "typo.txt"))
        check("an explicit --auth-token-file that does not exist refuses even on loopback", ok, msg)
        for name, content, frag in (
            ("23 characters", b"a" * 23, "shorter than 24"),
            ("trailing newline", TOKEN.encode() + b"\n", "leading or trailing whitespace"),
            ("leading space", b" " + TOKEN.encode(), "leading or trailing whitespace"),
            ("inner space", b"a" * 12 + b" " + b"a" * 12, "printable ASCII"),
            ("UTF-8 BOM", b"\xef\xbb\xbf" + TOKEN.encode(), "not plain ASCII"),
            ("empty", b"", "shorter than 24"),
        ):
            open(tf, "wb").write(content)
            ok, msg = raises(A.AuthConfigError, frag, A.configure, "127.0.0.1", d)
            check(f"token file rejected at startup: {name}", ok and TOKEN not in msg, msg)
        open(tf, "wb").write(b"a" * 24)
        check("positive twin: a 24 character token is accepted", isinstance(A.configure("127.0.0.1", d)[0], A.Authenticator))
        ok, msg = raises(A.AuthConfigError, "go together", A.configure, "127.0.0.1", d, None, False, "c.pem", "")
        check("--tls-cert without --tls-key is refused", ok, msg)
        ok, msg = raises(
            A.AuthConfigError, "cannot load the TLS certificate", A.configure, "127.0.0.1", d, None, False, os.path.join(d, "no.pem"), os.path.join(d, "no.key")
        )
        check("an unreadable certificate is refused", ok, msg)
        # the real process
        py = [sys.executable, "-I", os.path.join(HERE, "bma_ui.py")]
        os.remove(tf)
        port = free_port()
        p = subprocess.run(py + ["--bind", "0.0.0.0", "--port", str(port), "--no-board", "--pipeline-root", d], capture_output=True, text=True, timeout=60)
        check(
            "process: --bind 0.0.0.0 without a token file exits 2 and says how to create the token",
            p.returncode == 2 and "refusing to listen on 0.0.0.0 without a login" in p.stderr and tf in p.stderr and "listening" not in p.stdout,
            f"{p.returncode} {p.stderr[-300:]}",
        )
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            listening = True
        except OSError:
            listening = False
        check("process: and nothing is listening afterwards", not listening)
        open(tf, "wb").write(b"short")
        p = subprocess.run(py + ["--bind", "127.0.0.1", "--port", str(port), "--no-board", "--pipeline-root", d], capture_output=True, text=True, timeout=60)
        check("process: a short token file exits 2 with the reason", p.returncode == 2 and "shorter than 24" in p.stderr, f"{p.returncode} {p.stderr[-200:]}")
    finally:
        shutil.rmtree(d, ignore_errors=True)

    # process with a token: full-output capture, no secrets, 401 vs 200, --no-auth override
    d = tempfile.mkdtemp(prefix="bma_auth_proc_")
    procs = []
    try:
        for sub in ("pipeline\\analysis", "pipeline\\logs", "reports", "rollups"):
            os.makedirs(os.path.join(d, sub), exist_ok=True)
        tf = os.path.join(d, "pipeline", "dashboard_token.txt")
        open(tf, "wb").write(TOKEN.encode())

        def start(tag, extra):
            port = free_port()
            out, err = open(os.path.join(d, tag + ".out"), "wb"), open(os.path.join(d, tag + ".err"), "wb")
            p = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    os.path.join(HERE, "bma_ui.py"),
                    "--bind",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--no-board",
                    "--pipeline-root",
                    os.path.join(d, "pipeline"),
                    "--reports-dir",
                    os.path.join(d, "reports"),
                    "--rollup-dir",
                    os.path.join(d, "rollups"),
                ]
                + extra,
                stdout=out,
                stderr=err,
            )
            procs.append((p, out, err, tag))
            for _ in range(80):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=1).close()
                    return port
                except OSError:
                    time.sleep(0.25)
            return None

        port = start("login", [])
        lg = [req(port, "/api/status")[0], req(port, "/api/status", headers={"Authorization": basic("u", TOKEN)})[0]] if port else None
        check(
            "process: the default token file <pipeline-root>/dashboard_token.txt is picked up: no credentials 401, right Basic 200", lg == [401, 200], str(lg)
        )
        if port:
            req(port, "/api/status", headers={"Authorization": basic("u", WRONG)})
            s, h, b, hd = req(port, "/?token=" + TOKEN)
            ck = [v for k, v in hd if k.lower() == "set-cookie"][0].split(";")[0].split("=", 1)[1]
            req(port, "/api/status", headers={"Cookie": "bma_auth=" + ck})
            req(port, "/?token=" + WRONG)
        port2 = start("noauth", ["--no-auth"])
        na = req(port2, "/api/status")[0] if port2 else None
        check("process: --no-auth serves without a login (200) although a token file exists", na == 200, str(na))
        for p, out, err, tag in procs:
            p.terminate()
            p.wait(timeout=15)
            out.close()
            err.close()
        cap = {tag: open(os.path.join(d, tag + ".out"), "rb").read() + b"\n" + open(os.path.join(d, tag + ".err"), "rb").read() for _, _, _, tag in procs}
        leaked = [s_ for s_ in secrets[:2] + [ck] if s_.encode() in cap["login"] or s_.encode() in cap["noauth"]] if port else ["no server"]
        check(
            "process: the full stdout+stderr never contains the token, the wrong password or the cookie value",
            not leaked and cap["login"].count(b"auth failure ip=127.0.0.1 failures=1") == 2,
            str(leaked) + cap["login"][-300:].decode("utf-8", "replace"),
        )
        check(
            "process: stdout says the login is required; --no-auth prints the loud WARNING on stderr and says login OFF",
            b"(login required;" in cap["login"] and b"WARNING: --no-auth" in cap["noauth"] and b"login OFF" in cap["noauth"],
            cap["noauth"][-300:].decode("utf-8", "replace"),
        )
    finally:
        for p, out, err, tag in procs:
            if p.poll() is None:
                p.kill()
        shutil.rmtree(d, ignore_errors=True)

    # ---------------------------------------------------------------- 8b. the feature-gate contract: handler.authenticated, server.auth.enabled
    seen = []
    orig = U.Handler._handle

    def spy(self, head):  # records the flag after the real handler ran, delegates to it
        orig(self, head)
        seen.append((self.path, self.authenticated))

    U.Handler._handle = spy

    def last(n):  # the spy appends after the response was sent: wait for the n-th record
        for _ in range(200):
            if len(seen) >= n:
                break
            time.sleep(0.01)
        return seen[n - 1]

    try:
        r = Rig()
        good, bad = {"Authorization": basic("u", TOKEN)}, {"Authorization": basic("u", WRONG)}
        ck = r.auth.cookie_header().split(";")[0]
        flags = {}
        for name, path, hd in (
            ("basic", "/api/status", good),
            ("cookie", "/api/status", {"Cookie": ck}),
            ("none", "/api/status", {}),
            ("wrong", "/api/status", bad),
            ("login redirect", "/?token=" + TOKEN, {}),
            ("stale cookie", "/api/status", {"Cookie": "bma_auth=" + "0" * 64}),
        ):
            n0 = len(seen)
            r.get(path, hd)
            flags[name] = last(n0 + 1)[1]
        check(
            "handler.authenticated: True after Basic and after the cookie; False with no credential, a wrong one, a stale cookie and on the ?token= redirect itself",
            flags == {"basic": True, "cookie": True, "none": False, "wrong": False, "login redirect": False, "stale cookie": False},
            str(flags),
        )
        n0 = len(seen)
        r.get("/api/status", good, host="evil.example:%d" % r.port)
        check("handler.authenticated is False for a refused Host even with the right token", last(n0 + 1)[1] is False)
        c = http.client.HTTPConnection("127.0.0.1", r.port, timeout=15)
        got = []
        n0 = len(seen)
        for hd in (good, {}, good):  # one keep-alive connection, one handler object: the flag must not carry over
            c.putrequest("GET", "/api/status", skip_host=True)
            c.putheader("Host", f"localhost:{r.port}")
            for k, v in hd.items():
                c.putheader(k, v)
            c.endheaders()
            resp = c.getresponse()
            resp.read()
            got.append((resp.status, last(n0 + 1 + len(got))[1]))
        c.close()
        check(
            "handler.authenticated is per request: on one keep-alive connection 200/True, then 401/False, then 200/True",
            got == [(200, True), (401, False), (200, True)],
            str(got),
        )
        check("server.auth.enabled is True when a login is configured", r.srv.auth.enabled is True)
        for _ in range(5):
            r.get("/api/status", bad)
        n0 = len(seen)
        s_l = r.get("/api/status", good)[0]
        check("during a lockout the correct token gets 429 and handler.authenticated is False", s_l == 429 and last(n0 + 1)[1] is False, str((s_l, n0)))
        r.close()
        r = Rig(auth=False)
        n0 = len(seen)
        s_o = r.get("/api/status")[0]
        check(
            "no token file (open dashboard): 200 but handler.authenticated is False and server.auth.enabled is False (open is NOT authenticated)",
            s_o == 200 and last(n0 + 1)[1] is False and r.srv.auth.enabled is False,
            str((s_o, n0)),
        )
        r.close()
    finally:
        U.Handler._handle = orig

    # ---------------------------------------------------------------- 9. Host check and the 503 cap keep working, before any auth work
    r = Rig()
    s_h, _, b_h, _ = r.get("/api/status", host="evil.example:%d" % r.port)
    check(
        "a wrong Host is 403 host_not_allowed before auth (not 401) and costs no auth state",
        s_h == 403 and b'"code":"host_not_allowed"' in b_h and r.auth.tracked() == 0,
        str((s_h, b_h)),
    )
    s_h2, _, _, _ = r.get("/api/status", {"Authorization": basic("u", WRONG)}, host="evil.example:%d" % r.port)
    check("a wrong Host with a wrong password is still 403 and is not counted as an auth failure", s_h2 == 403 and r.auth.failures("127.0.0.1") == 0)
    check(
        "positive twin: the right Host without credentials is 401, with a wrong password it is counted",
        r.get("/api/status")[0] == 401 and r.get("/api/status", {"Authorization": basic("u", WRONG)})[0] == 401 and r.auth.failures("127.0.0.1") == 1,
    )
    r.close()
    r = Rig(max_conns=1, conn_timeout=20)
    hold = socket.create_connection(("127.0.0.1", r.port), timeout=5)  # occupies the only worker slot
    seen503 = False
    for _ in range(100):
        if req(r.port, "/api/status")[0] == 503:
            seen503 = True
            break
        time.sleep(0.05)
    rej = [req(r.port, "/api/status", headers={"Authorization": basic("u", WRONG)})[0] for _ in range(8)]
    check(
        "over the connection cap every request is 503 whatever it carries; eight wrong passwords consume no auth state (nothing tracked, no log)",
        seen503 and rej == [503] * 8 and r.auth.tracked() == 0 and not [m for m in r.logs if m.startswith("auth ")],
        str((seen503, rej, r.auth.tracked(), r.logs)),
    )
    hold.close()
    st = None
    for _ in range(200):
        st = req(r.port, "/api/status")[0]
        if st != 503:
            break
        time.sleep(0.05)
    check(
        "positive twin: once the slot is free the same wrong password is a counted 401",
        st == 401 and req(r.port, "/api/status", headers={"Authorization": basic("u", WRONG)})[0] == 401 and r.auth.failures("127.0.0.1") == 1,
    )
    r.close()

    # ---------------------------------------------------------------- 9b. /healthz: the one credential-free liveness route (ui-http 1.10.0)
    for auth_on in (True, False):
        r = Rig(auth=auth_on)
        t0 = int(time.time())
        s_g, h_g, b_g, hd_g = r.get("/healthz")
        t1 = int(time.time())
        doc = json.loads(b_g)
        tag = "auth on" if auth_on else "auth off"
        check(
            "healthz (%s): 200 without credentials, body exactly {ok: true, ts: int seconds}, nothing else" % tag,
            s_g == 200
            and sorted(doc) == ["ok", "ts"]
            and doc["ok"] is True
            and type(doc["ts"]) is int
            and t0 <= doc["ts"] <= t1
            and b_g == json.dumps(doc, separators=(",", ":")).encode(),
            str((s_g, b_g)),
        )
        check(
            "healthz (%s): application/json, Cache-Control no-store, content-length matches" % tag,
            (h_g["content-type"], h_g["cache-control"], h_g["content-length"]) == ("application/json", "no-store", str(len(b_g))),
            str(hd_g),
        )
        s_h, h_h, b_h, _ = r.get("/healthz", method="HEAD")
        check(
            "healthz (%s): HEAD is 200 with the GET length and no body" % tag,
            (s_h, b_h, h_h["content-length"]) == (200, b"", h_h["content-length"])
            and h_h["content-length"].isdigit()
            and abs(int(h_h["content-length"]) - len(b_g)) <= 1,
            str((s_h, h_h)),
        )
        got405 = [r.get("/healthz", method=m) for m in ("POST", "PUT", "DELETE")]
        check(
            "healthz (%s): POST, PUT and DELETE are 405 with Allow: GET, HEAD" % tag,
            [(g[0], g[1]["allow"]) for g in got405] == [(405, "GET, HEAD")] * 3,
            str([g[:2] for g in got405]),
        )
        s_w, _, b_w, _ = r.get("/healthz", host="evil.example:%d" % r.port)
        check("healthz (%s): a wrong Host is still 403 host_not_allowed" % tag, s_w == 403 and b'"code":"host_not_allowed"' in b_w, str((s_w, b_w)))
        s_q, _, b_q, _ = r.get("/healthz/x")
        check(
            "healthz (%s): only the exact path is open: /healthz/x is %s" % (tag, "401" if auth_on else "404"),
            s_q == (401 if auth_on else 404),
            str((s_q, b_q)),
        )
        r.close()
    r = Rig(auth=True)
    check("positive twin: with auth on /api/status stays 401 while /healthz is 200", (r.get("/api/status")[0], r.get("/healthz")[0]) == (401, 200))
    r.close()

    # ---------------------------------------------------------------- 10. TLS
    ossl = shutil.which("openssl")
    if not ossl and shutil.which("git"):
        cand = os.path.join(os.path.dirname(os.path.dirname(shutil.which("git"))), "usr", "bin", "openssl.exe")
        ossl = cand if os.path.isfile(cand) else None
    if not ossl:
        skips.append("tls")
        print("SKIP tls: the openssl binary was not found, so the 4 TLS checks did NOT run (this is not a pass)")
        return
    d = tempfile.mkdtemp(prefix="bma_auth_tls_")
    try:
        crt, key = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
        p = subprocess.run(
            [
                ossl,
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                key,
                "-out",
                crt,
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost,IP:127.0.0.1",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "MSYS_NO_PATHCONV": "1"},
        )
        if p.returncode != 0 or not os.path.isfile(crt):
            skips.append("tls")
            print("SKIP tls: openssl could not make a certificate (%s): the TLS checks did NOT run" % p.stderr.strip()[:200])
            return
        ctx = A.make_tls_context(crt, key)
        check("make_tls_context: TLS 1.2 minimum, no client certificates", ctx.minimum_version == ssl.TLSVersion.TLSv1_2 and ctx.verify_mode == ssl.CERT_NONE)
        r = Rig(tls=ctx, secure=True)
        cctx = ssl.create_default_context(cafile=crt)  # verifies the chain and the name against the self-signed cert
        c = http.client.HTTPSConnection("localhost", r.port, timeout=15, context=cctx)
        c.request("GET", "/api/status", headers={"Authorization": basic("u", TOKEN)})
        resp = c.getresponse()
        body = resp.read()
        ver = c.sock.version()
        c.close()
        check(
            "https GET with the right Basic password is 200 over TLS 1.2 or 1.3 with a verified certificate",
            resp.status == 200 and ver in ("TLSv1.2", "TLSv1.3") and body.startswith(b"{"),
            str((resp.status, ver)),
        )
        c = http.client.HTTPSConnection("localhost", r.port, timeout=15, context=cctx)
        c.request("GET", "/?token=" + TOKEN)
        resp = c.getresponse()
        resp.read()
        sc = resp.getheader("Set-Cookie") or ""
        c.close()
        check("over TLS the login cookie carries Secure as the last attribute", resp.status == 302 and sc.endswith("; Path=/; Max-Age=2592000; Secure"), sc)
        c = http.client.HTTPSConnection("localhost", r.port, timeout=15, context=cctx)
        c.request("GET", "/api/status")
        resp = c.getresponse()
        resp.read()
        c.close()
        check("https without credentials is 401 (auth applies over TLS too)", resp.status == 401)
        got = None
        try:
            c = http.client.HTTPConnection("127.0.0.1", r.port, timeout=10)
            c.request("GET", "/api/status", headers={"Host": f"localhost:{r.port}", "Authorization": basic("u", TOKEN)})
            got = c.getresponse().status
        except (http.client.HTTPException, OSError) as ex:
            got = type(ex).__name__
        check("plain http to the TLS port fails (no HTTP response at all)", isinstance(got, str), str(got))
        r.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


run()
print(f"\n{n_checks} checks, {len(fails)} failure(s)" + (f", {len(skips)} SKIPPED group(s): {', '.join(skips)}" if skips else ""))
sys.exit(1 if fails else (77 if skips else 0))
