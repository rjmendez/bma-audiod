#!/usr/bin/env python3
"""Tests for the 1.7.0 login upgrades: the sign-in page, sessions, logout and CSRF, the Secure cookie behind a TLS proxy, and the tailnet identity
mode. Real servers on ephemeral ports and real HTTP; only the lockout clock is replaced by a controllable one. (Passkeys: test_passkeys.py.)

    python test_auth2.py     (PASS/FAIL lines, a final 'N checks, 0 failure(s)' line; exit 1 on failure; exit 77 when a group had to be
                              skipped (no non-loopback address to forge a header from, or no openssl for TLS): reported as SKIP, never as a pass)"""

import base64, http.client, json, os, re, shutil, socket, ssl, subprocess, sys, tempfile, threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import auth as A  # noqa: E402
import bma_ui as U  # noqa: E402

fails, skips, n_checks = [], [], 0
TOKEN = "T0ken-" + "a1b2c3d4e5f6" * 4
WRONG = "WRONGpw-" + "z9y8x7w6" * 4
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def check(name, ok, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def code_of(body):
    """The problem `code` of a response body, or None when the body is not a problem JSON (a failing check, never a crash)."""
    try:
        return json.loads(body)["code"]
    except (ValueError, KeyError, TypeError):
        return None


def basic(pw):
    return "Basic " + base64.b64encode(f"u:{pw}".encode()).decode()


class Rig:
    def __init__(self, auth=True, allowed=None, require_identity=False, bind="127.0.0.1", **srv_kw):
        self.root = tempfile.mkdtemp(prefix="bma_a2_")
        self.logs, self.t = [], [1000.0]
        pr, rp = os.path.join(self.root, "p"), os.path.join(self.root, "r")
        for d in (os.path.join(pr, "analysis"), os.path.join(pr, "logs"), rp):
            os.makedirs(d, exist_ok=True)
        board = U.Board("http://127.0.0.1:9", os.path.join(self.root, "nt"), enabled=False)
        app = U.App(pr, rp, os.path.join(self.root, "ru"), board, window_h=48, log=self.logs.append)
        self.users = A.AllowedUsers(allowed) if allowed else None
        self.auth = (
            A.Authenticator(TOKEN, log=self.logs.append, clock=lambda: self.t[0], allowed_users=self.users, require_identity=require_identity) if auth else None
        )
        self.srv = U.Server((bind, 0), app, set(), U.load_assets(), auth=self.auth, allow_ip_hosts=bind != "127.0.0.1", **srv_kw)
        self.port = self.srv.server_address[1]
        self.srv.allowed_hosts = U.allowed_hosts_for(self.port, [])
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.root, ignore_errors=True)

    def req(self, method, path, headers=None, body=None, addr="127.0.0.1", host=None, conn=None):
        c = conn or http.client.HTTPConnection(addr, self.port, timeout=15, source_address=(addr, 0) if addr != "127.0.0.1" else None)
        h = {"Host": host or f"localhost:{self.port}"}
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        hd = {k.lower(): v for k, v in r.getheaders()}
        sc = [v for k, v in r.getheaders() if k.lower() == "set-cookie"]
        c.close()
        return r.status, hd, data, sc

    def form_login(self, token=TOKEN, csrf="auto", headers=None, body=None):
        csrf = self.auth.login_csrf() if csrf == "auto" else csrf
        h = dict(FORM)
        h.update(headers or {})
        return self.req("POST", "/login", h, body if body is not None else f"token={token}&csrf={csrf}")


def cookie_of(sc):
    return sc[0].split(";")[0] if sc else None


def find_lan_ip():
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
    except OSError:
        pass
    return None


def run():
    # ============================================================================================ 1. the sign-in page
    r = Rig()
    s, h, b, _ = r.req("GET", "/login")
    page = b.decode()
    m = re.search(r'name="csrf" value="(\d+\.[0-9a-f]{64})"', page)
    check(
        "GET /login without a credential: 200 text/html, no-store, the form posts to /login with a token field and a csrf field",
        (s, h["content-type"], h["cache-control"]) == (200, "text/html; charset=utf-8", "no-store")
        and 'action="/login"' in page
        and 'name="token" autocomplete="current-password"' in page
        and m is not None,
        str((s, h, page[:200])),
    )
    check(
        "the login page's CSP is the standard one with exactly one change: form-action 'self'",
        h["content-security-policy"] == U.CSP.replace("form-action 'none'", "form-action 'self'") and "form-action 'none'" in U.CSP,
    )
    check(
        "the page needs no inline script or style (CSP forbids both): only /static/login.css",
        "<style" not in page and "<script" not in page and 'href="/static/login.css"' in page,
    )
    s1, h1, b1, _ = r.req("GET", "/static/login.css")
    check(
        "/static/login.css is served without a credential, byte for byte",
        (s1, b1) == (200, open(os.path.join(HERE, "static", "login.css"), "rb").read()),
        str(s1),
    )
    check(
        "positive twin: every other route and asset still needs one (app.js, session.js, /api/status are 401)",
        [r.req("GET", p)[0] for p in ("/static/app.js", "/static/session.js", "/api/status", "/api/session", "/logout")] == [401] * 5,
    )
    sh, hh, bh, _ = r.req("HEAD", "/login")
    check(
        "HEAD /login is 200 with an empty body and the length of the GET page's shape (not zero)",
        (sh, bh) == (200, b"") and int(hh["content-length"]) > 300,
        str((sh, hh)),
    )
    check("PUT /login is 405 with Allow: GET, HEAD, POST", (lambda x: (x[0], x[1].get("allow")))(r.req("PUT", "/login")) == (405, "GET, HEAD, POST"))
    ro = Rig(auth=False)
    check(
        "an open server (no login) has no sign-in page or session: /login and /api/session are 404, /logout is 405 as any POST; the inert login.css is a plain asset (200)",
        [ro.req("GET", p)[0] for p in ("/login", "/api/session", "/static/login.css")] + [ro.req("POST", "/logout")[0]] == [404, 404, 200, 405],
    )
    check("open server: /login is 404 (positive twin: / is 200)", (ro.req("GET", "/login")[0], ro.req("GET", "/")[0]) == (404, 200))
    ro.close()

    # ============================================================================================ 2. form login, sessions
    s, h, b, sc = r.form_login()
    ck = cookie_of(sc)
    value = ck.split("=", 1)[1]
    check(
        "POST /login with the right token and csrf: 303 to / with no body",
        (s, h["location"], b, h["cache-control"]) == (303, "/", b"", "no-store"),
        str((s, h, b)),
    )
    check(
        "the session cookie has exactly these attributes: HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000, 64 hex characters, no Secure over plain HTTP",
        sc == [f"bma_auth={value}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000"] and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
        str(sc),
    )
    check("the cookie is not the token and not the ?token= cookie", value != TOKEN and ck != r.auth.cookie_header().split(";")[0])
    check("the session cookie opens /api/status and the page (200, 200)", [r.req("GET", p, {"Cookie": ck})[0] for p in ("/api/status", "/")] == [200, 200])
    s2, h2, b2, sc2 = r.form_login()
    ck2 = cookie_of(sc2)
    check(
        "a second login gets a different session (per-session cookies) and both work",
        ck2 != ck and [r.req("GET", "/api/status", {"Cookie": c})[0] for c in (ck, ck2)] == [200, 200],
    )
    check("a tampered session cookie (last digit changed) is 401", r.req("GET", "/api/status", {"Cookie": ck[:-1] + ("0" if ck[-1] != "0" else "1")})[0] == 401)
    check("a successful login leaves no failure state", r.auth.tracked() == 0, str(r.auth.tracked()))
    r2 = Rig()
    check("a session cookie from another server instance is refused (sessions live in memory)", r2.req("GET", "/api/status", {"Cookie": ck})[0] == 401)
    r2.close()

    # wrong token, lockout shared with Basic
    r.t[0] = 5000.0
    s, h, b, sc = r.form_login(token=WRONG)
    check(
        "a wrong token: 401 with the page again, no cookie, one failure counted",
        (s, sc, r.auth.failures("127.0.0.1"), b"Wrong token." in b) == (401, [], 1, True),
        str((s, sc)),
    )
    check("positive twin: the right token right after still signs in (303)", r.form_login()[0] == 303 and r.auth.failures("127.0.0.1") == 0)
    for _ in range(3):
        r.req("GET", "/api/status", {"Authorization": basic(WRONG)})
    codes = [r.form_login(token=WRONG)[0], r.form_login(token=WRONG)[0]]
    check(
        "three wrong Basic passwords then two wrong form tokens: the counter is SHARED (5 failures: both are 401, the lock is now on)",
        codes == [401, 401] and r.auth.failures("127.0.0.1") == 5,
        str(codes),
    )
    s, h, b, sc = r.form_login()
    check(
        "locked: even the RIGHT token on the form is 429 with Retry-After 60 and sets no cookie",
        (s, h.get("retry-after"), sc, code_of(b)) == (429, "60", [], "too_many_attempts"),
        str((s, h, b)),
    )
    check("locked: the right Basic password is 429 as well (one lock for all routes)", r.req("GET", "/api/status", {"Authorization": basic(TOKEN)})[0] == 429)
    r.t[0] += 60
    check("positive twin: 60 s later the right token signs in (303)", r.form_login()[0] == 303)

    # CSRF
    r.t[0] += 10
    for name, kw in (
        ("a forged csrf field", {"csrf": "123." + "0" * 64}),
        ("an empty csrf field", {"csrf": ""}),
        (
            "a csrf with a wrong mac",
            {"csrf": r.auth.login_csrf()[:-1] + "0"} if not r.auth.login_csrf().endswith("0") else {"csrf": r.auth.login_csrf()[:-1] + "1"},
        ),
    ):
        s, h, b, sc = r.form_login(**kw)
        check(
            "CSRF: %s is refused with 403 (the page again), no cookie, and is NOT counted as a failed login" % name,
            (s, sc, r.auth.failures("127.0.0.1"), b"expired" in b) == (403, [], 0, True),
            str((s, sc)),
        )
    old = r.auth.login_csrf()
    r.t[0] += A.LOGIN_CSRF_TTL_S + 1
    check("CSRF: a login form older than 15 minutes (901 s) is refused with 403", r.form_login(csrf=old)[0] == 403)
    old = r.auth.login_csrf()
    r.t[0] += A.LOGIN_CSRF_TTL_S
    check("CSRF positive twin: a form exactly 900 s old is still accepted (303)", r.form_login(csrf=old)[0] == 303)
    for name, hdr in (
        ("a foreign Origin", {"Origin": "https://evil.example"}),
        ("Origin: null", {"Origin": "null"}),
        ("an Origin with another port", {"Origin": "http://localhost:1"}),
        ("Sec-Fetch-Site: cross-site", {"Sec-Fetch-Site": "cross-site"}),
        ("Sec-Fetch-Site: same-site", {"Sec-Fetch-Site": "same-site"}),
    ):
        s, h, b, sc = r.form_login(headers=hdr)
        check(
            "same-origin rule: %s with the right token and a valid csrf is refused (403, no cookie, not counted)" % name,
            (s, sc, r.auth.failures("127.0.0.1")) == (403, [], 0),
            str((s, sc)),
        )
    for name, hdr in (
        ("Origin: null with Sec-Fetch-Site: cross-site", {"Origin": "null", "Sec-Fetch-Site": "cross-site"}),
        ("Origin: null with Sec-Fetch-Site: same-site", {"Origin": "null", "Sec-Fetch-Site": "same-site"}),
        ("Origin: null with Sec-Fetch-Site: none", {"Origin": "null", "Sec-Fetch-Site": "none"}),
    ):
        s, h, b, sc = r.form_login(headers=hdr)
        check(
            "Origin: null is not enough: %s is refused (403, no cookie, not counted)" % name,
            (s, sc, r.auth.failures("127.0.0.1")) == (403, [], 0),
            str((s, sc)),
        )
    s, h, b, sc = r.form_login(headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"})
    check(
        "a real browser: the page's own form post under Referrer-Policy: no-referrer carries Origin: null AND Sec-Fetch-Site: same-origin and signs in (303, a cookie)",
        s == 303 and len(sc) >= 1,
        str((s, sc)),
    )
    own = {"Origin": f"http://localhost:{r.port}", "Sec-Fetch-Site": "same-origin"}
    check("same-origin positive twin: the page's own Origin and Sec-Fetch-Site: same-origin sign in (303)", r.form_login(headers=own)[0] == 303)
    check("a script with no Origin and no Sec-Fetch-Site (curl) still signs in when it has a csrf and the token", r.form_login()[0] == 303)
    s, h, b, _ = r.form_login(body="token=" + "x" * 5000)
    check("a form body over 4096 bytes is 400 invalid_request", (s, code_of(b)) == (400, "invalid_request"), str((s, b[:80])))
    s, h, b, _ = r.form_login(body=f"token={TOKEN}")
    check("a form without a csrf field is 403 even with the right token", s == 403)
    check(
        "a JSON content type does not bypass the form rules: the same body without csrf is 403",
        r.req("POST", "/login", {"Content-Type": "application/json"}, f"token={TOKEN}")[0] == 403,
    )

    # navigation
    nav = {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
    rr = Rig()
    got = [
        (lambda x: (x[0], x[1].get("location"), x[1].get("www-authenticate")))(rr.req("GET", "/", nav)),
        (lambda x: (x[0], x[1].get("location")))(rr.req("GET", "/index.html", nav)),
    ]
    check(
        "a browser navigation to / or /index.html without credentials is 302 to /login (no Basic prompt)",
        got == [(302, "/login", None), (302, "/login")],
        str(got),
    )
    twins = [
        rr.req("GET", "/")[0],
        rr.req("GET", "/", {"Sec-Fetch-Mode": "navigate"})[0],
        rr.req("GET", "/api/status", nav)[0],
        rr.req("GET", "/?x=1", nav)[0],
        rr.req("POST", "/", nav)[0],
    ]
    check("positive twins: curl (no Sec-Fetch), navigate without Dest, an API path, a query and a POST are all still 401", twins == [401] * 5, str(twins))
    sn, hn, _, _ = rr.req("GET", "/?token=" + TOKEN, nav)
    check("a right ?token= on a navigation still logs in (302 to /), the old route keeps working", (sn, hn.get("location")) == (302, "/"))
    rr.close()

    # logout and per-session CSRF
    ra, rb_ = None, None
    s, _, _, sc = r.form_login()
    ca = cookie_of(sc)
    cb = cookie_of(r.form_login()[3])
    sa = json.loads(r.req("GET", "/api/session", {"Cookie": ca})[2])
    sb = json.loads(r.req("GET", "/api/session", {"Cookie": cb})[2])
    check(
        "/api/session: exact keys, via session, user null, a 64 hex csrf that differs per session",
        set(sa) == {"authenticated", "via", "user", "csrf", "passkeys", "tailnet_identity"}
        and (sa["authenticated"], sa["via"], sa["user"], sa["passkeys"], sa["tailnet_identity"])
        == (True, "session", None, {"enabled": False, "count": 0}, False)
        and re.fullmatch(r"[0-9a-f]{64}", sa["csrf"])
        and sa["csrf"] != sb["csrf"],
        str((sa, sb)),
    )
    try:
        import jsonschema

        sch = json.load(open(os.path.join(HERE, "..", "contracts", "schemas", "ui_http.schema.json")))
        v = jsonschema.Draft202012Validator({"$schema": sch["$schema"], "$ref": "#/$defs/session", "$defs": sch["$defs"]})
        check(
            "/api/session validates against the ui-http schema `session` (and so does the shipped example)",
            not list(v.iter_errors(sa)) and not list(v.iter_errors(json.load(open(os.path.join(HERE, "..", "contracts", "fixtures", "ui_session.json"))))),
        )
    except ImportError:
        skips.append("session schema (jsonschema missing)")
        print("SKIP session schema: jsonschema is not installed")
    s, h, b, _ = r.req("POST", "/logout", {"Cookie": ca})
    check(
        "POST /logout without the CSRF token: 403 csrf_failed, and the session still works",
        (s, code_of(b), r.req("GET", "/api/status", {"Cookie": ca})[0]) == (403, "csrf_failed", 200),
    )
    s, h, b, _ = r.req("POST", "/logout", {"Cookie": ca, "X-CSRF-Token": sb["csrf"]})
    check("POST /logout with ANOTHER session's csrf token: 403 (the token is per session)", s == 403 and r.req("GET", "/api/status", {"Cookie": ca})[0] == 200)
    s, h, b, _ = r.req("POST", "/logout", {"Cookie": ca, "X-CSRF-Token": sa["csrf"], "Origin": "https://evil.example"})
    check("POST /logout with the right csrf but a foreign Origin: 403", s == 403 and r.req("GET", "/api/status", {"Cookie": ca})[0] == 200)
    s, h, b, sc = r.req("GET", "/logout", {"Cookie": ca})
    check("GET /logout is 405 (Allow: POST) and logs nothing out", (s, h.get("allow"), r.req("GET", "/api/status", {"Cookie": ca})[0]) == (405, "POST", 200))
    s, h, b, sc = r.req("POST", "/logout", {"Cookie": ca, "X-CSRF-Token": sa["csrf"], **FORM}, "")
    check(
        "POST /logout with the right csrf: 303 to /login and the cookie is cleared (Max-Age=0)",
        (s, h["location"], sc) == (303, "/login", ["bma_auth=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"]),
        str((s, h, sc)),
    )
    check(
        "the logged-out session is really dead server side (401 with the old cookie value), the other session is untouched (200)",
        (r.req("GET", "/api/status", {"Cookie": ca})[0], r.req("GET", "/api/status", {"Cookie": cb})[0]) == (401, 200),
    )
    s, h, b, sc = r.req("POST", "/logout", {"Cookie": cb, **FORM}, "csrf=" + sb["csrf"])
    check("the csrf may also be a form field: logout of session two works that way (303)", (s, r.req("GET", "/api/status", {"Cookie": cb})[0]) == (303, 401))
    s, h, b, sc = r.req("POST", "/logout", {"Authorization": basic(TOKEN)})
    check("Basic can not log out without a csrf token either (403), a script gets one from /api/session", s == 403)
    sj = json.loads(r.req("GET", "/api/session", {"Authorization": basic(TOKEN)})[2])
    s, h, b, sc = r.req("POST", "/logout", {"Authorization": basic(TOKEN), "X-CSRF-Token": sj["csrf"]})
    check("positive twin: Basic with its csrf from /api/session gets the 303 (nothing server-side to end)", (sj["via"], s) == ("basic", 303))
    legacy = r.auth.cookie_header().split(";")[0]
    check(
        "the old ?token= cookie reports via cookie and still works (compatible)",
        (json.loads(r.req("GET", "/api/session", {"Cookie": legacy})[2])["via"], r.req("GET", "/api/status", {"Cookie": legacy})[0]) == ("cookie", 200),
    )

    # Secure behind a TLS-terminating loopback proxy
    s, h, b, sc = r.form_login(headers={"X-Forwarded-Proto": "https"})
    check("a loopback peer that says X-Forwarded-Proto: https gets a Secure cookie", sc[0].endswith("; Path=/; Max-Age=2592000; Secure"), str(sc))
    s, h, b, sc = r.form_login(headers={"X-Forwarded-Proto": "http"})
    check("positive twin: X-Forwarded-Proto: http (or absent) gets no Secure", not sc[0].endswith("Secure") and not r.form_login()[3][0].endswith("Secure"))
    s, h, b, sc = r.req("GET", "/?token=" + TOKEN, {"X-Forwarded-Proto": "https"})
    check("the ?token= cookie is Secure behind the proxy as well", sc[0].endswith("; Secure"), str(sc))
    lan = find_lan_ip()
    r.close()

    # ============================================================================================ 3. tailnet identity
    PEERS_OK = ["127.0.0.1", "127.9.9.9", "::1", "::ffff:127.0.0.1", "[::1]"]
    PEERS_NO = ["192.0.2.5", "198.51.100.7", "fd00::1", "::ffff:192.0.2.5", "", "not-an-ip", "0.0.0.0", "::"]
    allowed = {"alice@example.com", "bob@example.com"}
    check(
        "identity_login from every loopback form believes an allow-listed header (and lower-cases it)",
        [A.identity_login(p, ["Alice@Example.com"], allowed) for p in PEERS_OK] == ["alice@example.com"] * 5,
    )
    check(
        "identity_login from every non-loopback or unparseable peer ignores the same header (fail closed)",
        [A.identity_login(p, ["alice@example.com"], allowed) for p in PEERS_NO] == [None] * 8,
    )
    rows = {
        "absent": [],
        "two copies": ["alice@example.com", "alice@example.com"],
        "not in the list": ["mallory@example.com"],
        "empty": [""],
        "blank": ["   "],
        "RFC 2047": ["=?utf-8?q?alice=40example.com?="],
        "inner space": ["alice @example.com"],
        "control character": ["alice@example.com\x01"],
        "non-ASCII": ["alïce@example.com"],
        "255 characters": ["a" * 243 + "@example.com"],
    }
    check("identity_login refuses: " + ", ".join(rows), [A.identity_login("127.0.0.1", v, allowed) for v in rows.values()] == [None] * len(rows))
    check(
        "identity_login positive twin: one copy, plain ASCII, in the list, from loopback is accepted; an empty allow list accepts nobody",
        (
            A.identity_login("127.0.0.1", ["bob@example.com"], allowed),
            A.identity_login("127.0.0.1", ["bob@example.com"], set()),
            A.identity_login("127.0.0.1", ["bob@example.com"], frozenset()),
        )
        == ("bob@example.com", None, None),
    )
    enc = "=?utf-8?q?alice=40example.com?="
    check(
        "identity_login refuses an RFC 2047 encoded word even when the allow list names that exact string (the rule is the form, not only the list); the same list accepts the plain login",
        (A.identity_login("127.0.0.1", [enc], allowed | {enc}), A.identity_login("127.0.0.1", ["alice@example.com"], allowed | {enc}))
        == (None, "alice@example.com"),
    )
    check(
        "allow-list parsing: comments, blanks, case and surrounding space",
        A.parse_allowed_users("# owner\n\n Alice@Example.com \n#bob@example.com\r\nCarol@x.org\n") == {"alice@example.com", "carol@x.org"},
    )

    d = tempfile.mkdtemp(prefix="bma_a2u_")
    af = os.path.join(d, "allowed_users.txt")
    open(af, "w", newline="").write("# the owner\nalice@example.com\n")
    ri = Rig(allowed=af)
    ident = {"Tailscale-User-Login": "alice@example.com"}
    s, h, b, sc = ri.req("GET", "/api/session", ident)
    j = json.loads(b) if s == 200 else {}
    check(
        "a loopback request with an allow-listed Tailscale-User-Login is authenticated: via tailnet, user alice, no cookie set",
        (s, j.get("via"), j.get("user"), j.get("tailnet_identity"), sc) == (200, "tailnet", "alice@example.com", True, []),
        str((s, b[:200])),
    )
    check("... and it opens the data API and the page", [ri.req("GET", p, ident)[0] for p in ("/api/status", "/")] == [200, 200])
    rows = {
        "no header": {},
        "a login not in the list": {"Tailscale-User-Login": "mallory@example.com"},
        "an encoded login": {"Tailscale-User-Login": "=?utf-8?q?alice=40example.com?="},
    }
    check(
        "loopback without the header, with another login, or with an encoded one: 401 (positive twin above: the allow-listed login is 200)",
        [ri.req("GET", "/api/status", hd)[0] for hd in rows.values()] == [401] * 3,
    )
    c = http.client.HTTPConnection("127.0.0.1", ri.port, timeout=10)
    c.putrequest("GET", "/api/status", skip_host=True)
    c.putheader("Host", f"localhost:{ri.port}")
    c.putheader("Tailscale-User-Login", "alice@example.com")
    c.putheader("Tailscale-User-Login", "alice@example.com")
    c.endheaders()
    check("two copies of the header: 401", c.getresponse().status == 401)
    c.close()
    check("the Tailscale-User-Name header alone authenticates nobody", ri.req("GET", "/api/status", {"Tailscale-User-Name": "Alice"})[0] == 401)
    check(
        "X-Forwarded-For does not make a peer trusted or untrusted (it is never read): still 200 with the identity header",
        ri.req("GET", "/api/status", {**ident, "X-Forwarded-For": "192.0.2.9"})[0] == 200,
    )
    check(
        "the token stays a fallback in identity mode: Basic 200, form login 303",
        (ri.req("GET", "/api/status", {"Authorization": basic(TOKEN)})[0], ri.form_login()[0]) == (200, 303),
    )
    for _ in range(5):
        ri.req("GET", "/api/status", {"Authorization": basic(WRONG)})
    check(
        "after 5 wrong tokens the address is locked (429) for the token, but the identity request is still 200 and the lockout state is untouched by it",
        (ri.req("GET", "/api/status", {"Authorization": basic(TOKEN)})[0], ri.req("GET", "/api/status", ident)[0], ri.auth.failures("127.0.0.1"))
        == (429, 200, 5),
    )
    ri.t[0] += 60
    check("positive twin: after the lock expires the token works again", ri.req("GET", "/api/status", {"Authorization": basic(TOKEN)})[0] == 200)
    open(af, "w", newline="").write("# the owner\nbob@example.com\n")
    os.utime(af, ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))
    check(
        "revoking a login in the file takes effect on the next request (no restart): alice 401, bob 200",
        (ri.req("GET", "/api/status", ident)[0], ri.req("GET", "/api/status", {"Tailscale-User-Login": "bob@example.com"})[0]) == (401, 200),
    )
    os.remove(af)
    check(
        "a missing allow-list file means nobody (fail closed): bob is 401, the token still works",
        (ri.req("GET", "/api/status", {"Tailscale-User-Login": "bob@example.com"})[0], ri.req("GET", "/api/status", {"Authorization": basic(TOKEN)})[0])
        == (401, 200),
    )
    open(af, "wb").write(b"\xff\xfe alice@example.com\n")
    check("a non-UTF-8 allow-list file means nobody", ri.req("GET", "/api/status", ident)[0] == 401)
    ri.close()

    # a forged header from a NON-loopback source address
    if lan is None:
        skips.append("forged header from a non-loopback source")
        print("SKIP forged identity header from a non-loopback address: this machine has no non-loopback IPv4 address")
    else:
        open(af, "w", newline="").write("alice@example.com\n")
        rf = Rig(allowed=af, bind="0.0.0.0")
        try:
            try:
                s, h, b, sc = rf.req("GET", "/api/status", ident, addr=lan, host=f"{lan}:{rf.port}")
                reached = True
            except OSError as ex:
                reached, s = False, repr(ex)
            if not reached:
                skips.append("forged header from a non-loopback source")
                print("SKIP forged identity header: could not connect to %s from itself (%s)" % (lan, s))
            else:
                peer_seen = []
                orig = U.Handler._auth_gate

                def spy(self, head):  # records the peer address the real gate saw, then delegates to it
                    peer_seen.append(self.client_address[0])
                    return orig(self, head)

                U.Handler._auth_gate = spy
                try:
                    s, h, b, sc = rf.req("GET", "/api/status", ident, addr=lan, host=f"{lan}:{rf.port}")
                    s_tok, _, _, _ = rf.req("GET", "/api/status", {"Authorization": basic(TOKEN)}, addr=lan, host=f"{lan}:{rf.port}")
                    s_loop, _, _, _ = rf.req("GET", "/api/status", ident, host=f"localhost:{rf.port}")
                finally:
                    U.Handler._auth_gate = orig
                check(
                    "FORGED header: the real request came from the non-loopback address %s and was refused (401)" % "<this machine's LAN address>",
                    peer_seen[0] == lan and s == 401,
                    str((peer_seen, s)),
                )
                s_x, _, _, sc_x = rf.req(
                    "POST", "/login", {**FORM, "X-Forwarded-Proto": "https"}, f"token={TOKEN}&csrf={rf.auth.login_csrf()}", addr=lan, host=f"{lan}:{rf.port}"
                )
                check(
                    "FORGED X-Forwarded-Proto: https from the non-loopback address is ignored: the login works (303) and the cookie is NOT Secure",
                    (s_x, sc_x[0].endswith("Secure") if sc_x else None) == (303, False),
                    str((s_x, sc_x)),
                )
                check(
                    "positive twins from the same address: the token works (200), and the same header over loopback is accepted (200)",
                    (s_tok, s_loop) == (200, 200),
                    str((s_tok, s_loop)),
                )
        finally:
            rf.close()

    # --require-identity
    open(af, "w", newline="").write("alice@example.com\n")
    rq = Rig(allowed=af, require_identity=True)
    got = [
        rq.req("GET", "/api/status", ident)[0],
        rq.req("GET", "/api/status", {"Authorization": basic(TOKEN)})[0],
        rq.req("GET", "/?token=" + TOKEN)[0],
        rq.req("GET", "/api/status", {"Cookie": rq.auth.cookie_header().split(";")[0]})[0],
    ]
    check("--require-identity: the identity is 200, but Basic, ?token= and the legacy cookie are all 401", got == [200, 401, 401, 401], str(got))
    s, h, b, sc = rq.form_login()
    check(
        "--require-identity: POST /login with the right token and csrf is 403 login_disabled, no cookie",
        (s, code_of(b), sc) == (403, "login_disabled", []),
        str((s, b)),
    )
    s, h, b, _ = rq.req("GET", "/login")
    check(
        "--require-identity: the login page has no token form and says so; navigations to / are 401, not redirected",
        (s, b'name="token"' in b, b"tailnet identity" in b, rq.req("GET", "/", nav)[0]) == (200, False, True, 401),
    )
    rq.close()
    r0 = Rig()
    check(
        "positive twin: without --require-identity the same navigation is redirected and Basic is accepted",
        (r0.req("GET", "/", nav)[0], r0.req("GET", "/api/status", {"Authorization": basic(TOKEN)})[0]) == (302, 200),
    )
    r0.close()

    # configuration rules
    def conf(**kw):
        try:
            return A.configure(kw.pop("bind", "127.0.0.1"), d, kw.pop("token_file", tf), **kw)[2], None
        except A.AuthConfigError as ex:
            return None, str(ex)

    tf = os.path.join(d, "tok.txt")
    open(tf, "w", newline="").write(TOKEN)
    n, e = conf(tailnet_identity=True, allowed_users=af)
    check("--tailnet-identity with an allow-list that names someone: no notice", (n, e) == ([], None), str((n, e)))
    n, e = conf(tailnet_identity=True, allowed_users=os.path.join(d, "missing.txt"))
    check(
        "--tailnet-identity with a missing list: a notice that nobody is accepted and the token still works",
        e is None and len(n) == 1 and "lists nobody" in n[0] and "token still works" in n[0],
        str((n, e)),
    )
    n, e = conf(tailnet_identity=True, allowed_users=os.path.join(d, "missing.txt"), require_identity=True)
    check("... with --require-identity the notice says NOBODY can sign in", e is None and "NOBODY can sign in" in n[0], str((n, e)))
    n, e = conf(allowed_users=af)
    check("--allowed-users without --tailnet-identity refuses to start", e is not None and "need --tailnet-identity" in e, str((n, e)))
    n, e = conf(require_identity=True)
    check("--require-identity without --tailnet-identity refuses to start", e is not None and "need --tailnet-identity" in e, str((n, e)))
    n, e = conf(tailnet_identity=True, no_auth=True)
    check("--tailnet-identity with --no-auth refuses to start", e is not None and "cannot be combined with --no-auth" in e, str((n, e)))
    shutil.rmtree(d, ignore_errors=True)

    tls_group()


def tls_group():
    ossl = shutil.which("openssl")
    if not ossl:
        cand = os.path.join(os.path.dirname(os.path.dirname(shutil.which("git") or "")), "usr", "bin", "openssl.exe")
        ossl = cand if os.path.isfile(cand) else None
    if not ossl:
        skips.append("tls")
        print("SKIP tls: openssl was not found, so the TLS cookie check did NOT run")
        return
    d = tempfile.mkdtemp(prefix="bma_a2t_")
    try:
        crt, key = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
        p = subprocess.run(
            [
                ossl,
                "req",
                "-x509",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:prime256v1",
                "-nodes",
                "-keyout",
                key,
                "-out",
                crt,
                "-days",
                "2",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost",
            ],
            capture_output=True,
            text=True,
        )
        if p.returncode:
            skips.append("tls")
            print("SKIP tls: openssl could not make a certificate")
            return
        r = Rig(tls=A.make_tls_context(crt, key))
        cctx = ssl.create_default_context(cafile=crt)
        c = http.client.HTTPSConnection("localhost", r.port, timeout=15, context=cctx)
        c.request("POST", "/login", headers={"Host": f"localhost:{r.port}", **FORM}, body=f"token={TOKEN}&csrf={r.auth.login_csrf()}")
        resp = c.getresponse()
        resp.read()
        sc = resp.getheader("Set-Cookie") or ""
        c.close()
        check(
            "over TLS the FORM login cookie ends with Secure (same attributes as plain HTTP plus Secure)",
            resp.status == 303 and re.fullmatch(r"bma_auth=[0-9a-f]{64}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000; Secure", sc) is not None,
            f"{resp.status} {sc}",
        )
        r.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


nav = {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
run()
print(f"\n{n_checks} checks, {len(fails)} failure(s)" + (f", {len(skips)} SKIPPED group(s): {', '.join(skips)}" if skips else ""))
sys.exit(1 if fails else (77 if skips else 0))
