#!/usr/bin/env python3
"""Mutation check for the dashboard login: each mutation edits one behaviour in a COPY of ui/, runs test_auth.py against it and
requires exit 1 (the suite must go red). The unmutated copy must exit 0 first (77 = the TLS group was skipped, also refused).
A mutation of the login/spectrum seam (a 5th element SPEC) runs test_auth_spectrum.py instead (needs jsonschema: run it with the contracts venv).

    python mutation_auth.py [name-substring]        Exit 0 only if the baseline is green and every mutation was caught."""

import os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GATE = "            if self._auth_gate(head):"
SPEC = "test_auth_spectrum.py"
A2 = "test_auth2.py"
PKS = "test_passkeys.py"
M = [
    ("auth bypass for static assets", "bma_ui.py", GATE, "            if urllib.parse.urlsplit(self.path).path not in ASSETS and self._auth_gate(head):"),
    ("secret compare with ==", "auth.py", "return hmac.compare_digest(hashlib.sha256(a).digest(), hashlib.sha256(b).digest())", "return a == b"),
    ("lockout threshold 5 -> 6", "auth.py", "LOCKOUT_THRESHOLD = 5 ", "LOCKOUT_THRESHOLD = 6 "),
    ("lockout threshold 5 -> 4", "auth.py", "LOCKOUT_THRESHOLD = 5 ", "LOCKOUT_THRESHOLD = 4 "),
    ("lockout does not double", "auth.py", "LOCKOUT_BASE_S * 2 ** (r[0] - LOCKOUT_THRESHOLD)", "LOCKOUT_BASE_S"),
    ("token logged on failure", "auth.py", '"auth failure ip=%s failures=%d" % (ip, r[0])', '"auth failure ip=%s failures=%d %s" % (ip, r[0], self._token)'),
    ("cookie without HttpOnly", "auth.py", "; HttpOnly; SameSite=Strict;", "; SameSite=Strict;"),
    ("cookie without SameSite", "auth.py", "; HttpOnly; SameSite=Strict;", "; HttpOnly;"),
    (
        "missing token file on 0.0.0.0 allowed",
        "auth.py",
        "    if is_loopback_bind(bind):\n        return None, tls, notices",
        "    if True:\n        return None, tls, notices",
    ),
    ("HEAD skips the auth gate", "bma_ui.py", GATE, '            if self.command != "HEAD" and self._auth_gate(head):'),
    (
        "rejected connection consumes auth state",
        "bma_ui.py",
        "                request.settimeout(1.0)\n",
        "                request.settimeout(1.0)\n                if self.auth.enabled:\n                    self.auth.record_failure(request.getpeername()[0])\n",
    ),
    (
        "a correct token is accepted during lockout",
        "auth.py",
        "        if left:                                                         # locked out",
        "        if left and False:                                               # locked out",
    ),
    (
        "authenticated flag True for every request",
        "bma_ui.py",
        "        self.authenticated = False                                       # per request",
        "        self.authenticated = True                                        # per request",
    ),
    (
        "authenticated flag not reset per request",
        "bma_ui.py",
        "        self.authenticated = False                                       # per request",
        "        pass                                                             # per request",
    ),
    (
        "open server counts as authenticated",
        "bma_ui.py",
        "        if self.server.auth.enabled:                                     # AUTH:",
        "        self.authenticated = not self.server.auth.enabled\n        if self.server.auth.enabled:                                     # AUTH:",
    ),
    ("auth gate after the Host check removed (Host check skipped for 401)", "bma_ui.py", "if not self.server.host_ok(host):", "if False:"),
    # the seam between the login and the live spectrum (suite test_auth_spectrum.py)
    (
        "request_authenticated always True",
        "bma_ui.py",
        '    return getattr(getattr(handler, "server", None), "auth", None) is not None and getattr(handler, "authenticated", False) is True',
        "    return True",
        SPEC,
    ),
    (
        "request_authenticated always False",
        "bma_ui.py",
        '    return getattr(getattr(handler, "server", None), "auth", None) is not None and getattr(handler, "authenticated", False) is True',
        "    return False",
        SPEC,
    ),
    (
        "login gate skipped for /api/spectrum",
        "bma_ui.py",
        GATE,
        '            if urllib.parse.urlsplit(self.path).path != "/api/spectrum" and self._auth_gate(head):',
        SPEC,
    ),
    (
        "auth object name mismatch (handler attribute 'authed')",
        "bma_ui.py",
        'getattr(handler, "authenticated", False) is True',
        'getattr(handler, "authed", False) is True',
        SPEC,
    ),
    (
        "auth object name mismatch (server attribute 'authn')",
        "bma_ui.py",
        'getattr(getattr(handler, "server", None), "auth", None) is not None',
        'getattr(getattr(handler, "server", None), "authn", None) is not None',
        SPEC,
    ),
    (
        "open server counts as authenticated (spectrum)",
        "bma_ui.py",
        "        if self.server.auth.enabled:                                     # AUTH:",
        "        self.authenticated = not self.server.auth.enabled\n        if self.server.auth.enabled:                                     # AUTH:",
        SPEC,
    ),
    ("detail tier served without --spectrum-detail", "bma_ui.py", '    if tier == "detail" and not detail_flag:', "    if False:", SPEC),
    (
        "source network decides (loopback peer counts as authenticated)",
        "bma_ui.py",
        "self.server.app.api_spectrum(q, self.peer_ip(), request_authenticated(self))",
        "self.server.app.api_spectrum(q, self.peer_ip(), request_authenticated(self) or self.peer_ip() == '127.0.0.1')",
        SPEC,
    ),
    # ---- 1.7.0: sign-in page, sessions, CSRF, tailnet identity (suite test_auth2.py), passkeys (suite test_passkeys.py)
    (
        "identity: header trusted from any peer",
        "auth.py",
        "    if not peer_is_loopback(peer):\n        return None\n    vals",
        "    if False:\n        return None\n    vals",
        A2,
    ),
    ("identity: several copies of the header accepted", "auth.py", "    if len(vals) != 1:", "    if len(vals) < 1:", A2),
    ("identity: allow-list ignored", "auth.py", "return v.lower() if v.lower() in allowed else None", "return v.lower()", A2),
    ("identity: RFC 2047 values accepted", "auth.py", 'or v.startswith("=?") or', "or", A2),
    (
        "identity: --require-identity ignored",
        "auth.py",
        "        if self.require_identity:                                        # --require-identity: the token",
        "        if False:                                                        # --require-identity: the token",
        A2,
    ),
    (
        "identity: an identity request counts against the lockout",
        "auth.py",
        '            return Result("ok", via="tailnet", subject="id:" + identity, user=identity)',
        '            self.record_failure(ip)\n            return Result("ok", via="tailnet", subject="id:" + identity, user=identity)',
        A2,
    ),
    ("identity: allow-list never re-read", "auth.py", "            if sig != self._sig:", "            if self._sig is None:", A2),
    ("login: csrf field not checked", "bma_ui.py", '        if not au.login_csrf_ok(f.get("csrf")):', "        if False:", A2),
    ("login: same-origin rule skipped (Sec-Fetch-Site)", "bma_ui.py", 'if sfs is not None and sfs != "same-origin":', "if False:", A2),
    (
        "login: Origin null accepted without Sec-Fetch-Site same-origin",
        "bma_ui.py",
        '            return sfs == "same-origin"                     # trusted only',
        "            return True                                     # trusted only",
        A2,
    ),
    (
        "login: Origin null always refused (browsers send it on their own form posts)",
        "bma_ui.py",
        '            return sfs == "same-origin"                     # trusted only',
        "            return False                                    # trusted only",
        A2,
    ),
    (
        "login: same-origin rule skipped (Origin)",
        "bma_ui.py",
        'return u.scheme in ("http", "https") and u.netloc.lower() == host and not u.path.strip("/")',
        "return True",
        A2,
    ),
    (
        "login: wrong form token not counted in the lockout",
        "bma_ui.py",
        "        au.record_failure(ip)                                            # the same counter and lockout as Basic and ?token=",
        "        pass",
        A2,
    ),
    (
        "login: lockout not checked on the form",
        "bma_ui.py",
        "        if self._locked():\n            self.close_connection = True\n            return\n        body = self._read_body(MAX_FORM_BYTES)",
        "        body = self._read_body(MAX_FORM_BYTES)",
        A2,
    ),
    ("login: a stale login csrf never expires", "auth.py", "fresh = 0 <= self._clock() - int(t) <= LOGIN_CSRF_TTL_S", "fresh = True", A2),
    ("session: the cookie value is not looked up", "auth.py", "            if v and self._session_ok(v):", "            if False:", A2),
    ("session: logout does not end the session", "bma_ui.py", '            au.end_session(self.headers.get_all("Cookie"))', "            pass", A2),
    ("session: csrf token the same for every session", "auth.py", 'b"bma-csrf-v1|" + subject.encode("utf-8", "surrogatepass")', 'b"bma-csrf-v1|"', A2),
    ("session: logout needs no csrf", "bma_ui.py", "            if not self._csrf_ok(form):", "            if False:", A2),
    ("session: login cookie never Secure behind the proxy", "bma_ui.py", '.strip().lower() == "https")', '.strip().lower() == "httpsx")', A2),
    (
        "session: X-Forwarded-Proto trusted from any peer",
        "bma_ui.py",
        "(A.peer_is_loopback(self.client_address[0]) and (self.headers",
        "(True and (self.headers",
        A2,
    ),
    (
        "session: browsers are never sent to /login",
        "bma_ui.py",
        '              and self.headers.get("Sec-Fetch-Mode") == "navigate" and self.headers.get("Sec-Fetch-Dest") == "document"):',
        "              and False):",
        A2,
    ),
    (
        "session: every request without a header is redirected (curl too)",
        "bma_ui.py",
        '              and self.headers.get("Sec-Fetch-Mode") == "navigate" and self.headers.get("Sec-Fetch-Dest") == "document"):',
        "              ):",
        A2,
    ),
    ("session: login.css needs a login", "bma_ui.py", '        if path in PUBLIC_ASSETS and self.command in ("GET", "HEAD"):', "        if False:", A2),
    ("passkeys: ECDSA verify accepts anything", "passkeys.py", "    return pt is not None and pt[0] % N == r", "    return True", PKS),
    ("passkeys: DER padding accepted", "passkeys.py", " or (n > 1 and v[0] == 0 and not v[1] & 0x80):", ":", PKS),
    ("passkeys: CBOR depth unlimited", "passkeys.py", "    if _depth > MAX_CBOR_DEPTH or offset >= len(data):", "    if offset >= len(data):", PKS),
    ("passkeys: user verification not required", "passkeys.py", "    if not flags & FLAG_UV:", "    if False:", PKS),
    ("passkeys: origin not checked", "passkeys.py", '    if cd.get("origin") not in origins:', "    if False:", PKS),
    (
        "passkeys: rp id hash not checked",
        "passkeys.py",
        '    if not secrets.compare_digest(ad[:32], hashlib.sha256(rp_id.encode("utf-8")).digest()):',
        "    if False:",
        PKS,
    ),
    (
        "passkeys: signature counter not checked",
        "passkeys.py",
        '    if (count or cred.get("sign_count", 0)) and count <= cred.get("sign_count", 0):',
        "    if False:",
        PKS,
    ),
    ("passkeys: a challenge can be reused", "passkeys.py", "            rec = self._d.pop(ch, None)", "            rec = self._d.get(ch)", PKS),
    ("passkeys: a challenge is not bound to the session", "passkeys.py", "rec[0] != kind or rec[1] != subject or", "rec[0] != kind or", PKS),
    ("passkeys: challenge never expires", "passkeys.py", " or rec[2] <= self._clock():", ":", PKS),
    (
        "passkeys: failed passkey login not counted",
        "bma_ui.py",
        "        except PK.PasskeyError:\n            au.record_failure(ip)",
        "        except PK.PasskeyError:\n            pass",
        PKS,
    ),
    ("passkeys: registration needs no csrf", "bma_ui.py", "                if not self._csrf_ok():", "                if False:", PKS),
    (
        "X-Forwarded-For trusted as the peer",
        "bma_ui.py",
        "        return self.client_address[0]\n",
        '        return self.headers.get("X-Forwarded-For") or self.client_address[0]\n',
        SPEC,
    ),
]


def run(d, suite="test_auth.py"):
    p = subprocess.run([sys.executable, "-I", os.path.join(d, suite)], capture_output=True, text=True, timeout=600)
    return p.returncode, [l for l in p.stdout.splitlines() if l.startswith("FAIL")][:3]


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    tmp = tempfile.mkdtemp(prefix="bma_auth_mut_")
    bad = 0
    try:
        base = os.path.join(tmp, "base")
        shutil.copytree(HERE, base, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(
            os.path.join(HERE, "..", "contracts"), os.path.join(tmp, "contracts"), ignore=shutil.ignore_patterns("__pycache__")
        )  # test_auth_spectrum.py validates against the contract
        for suite in ("test_auth.py", SPEC, A2, PKS):
            rc, _ = run(base, suite)
            print(f"baseline {suite} exit {rc} (must be 0)")
            if rc != 0:
                return 1
        for name, f, a, b, *suite in M:
            suite = suite[0] if suite else "test_auth.py"
            if only and only not in name:
                continue
            d = os.path.join(tmp, "m")
            shutil.rmtree(d, ignore_errors=True)
            shutil.copytree(base, d)
            p = os.path.join(d, f)
            s = open(p, newline="", encoding="utf-8").read()
            if s.count(a) != 1:
                print(f"BROKEN mutation (pattern found {s.count(a)} times): {name}")
                bad += 1
                continue
            open(p, "w", newline="", encoding="utf-8").write(s.replace(a, b))
            rc, fl = run(d, suite)
            print(("RED  " if rc == 1 else "NOT RED (exit %d) " % rc) + name + ("  <- " + fl[0][:110] if fl else ""))
            bad += rc != 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if bad else 0


sys.exit(main())
