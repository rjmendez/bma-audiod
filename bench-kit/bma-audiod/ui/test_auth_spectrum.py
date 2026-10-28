#!/usr/bin/env python3
"""Integration test: the REAL dashboard login (ui/auth.py, a token file read by auth.configure) together with the REAL live spectrum
(GET /api/spectrum fed by BMAS datagrams through the real loopback UDP receiver). Real servers on ephemeral loopback ports, real HTTP.
Servers are assembled exactly as main() does it: make_parser -> build -> auth.configure -> Server. Nothing is stubbed.

    python test_auth_spectrum.py     (run with the contracts venv, which has jsonschema; BMA_UI_DIR as for the other ui tests;
                                       PASS/FAIL lines, a final 'N checks, 0 failure(s)' line; exit 1 on failure)"""
import base64, http.client, json, os, shutil, socket, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.environ.get("BMA_UI_DIR", HERE)
sys.path.insert(0, UI_DIR)
sys.path.insert(1, os.path.join(UI_DIR, "..", "contracts"))
import bma_ui as U  # noqa: E402
import auth as A  # noqa: E402
import ref_binary as R  # noqa: E402
import jsonschema  # noqa: E402
import threading  # noqa: E402

fails, n_checks = [], 0
TOKEN = "T0ken-" + "a1b2c3d4e5f6" * 4
WRONG = "WRONGpw-" + "z9y8x7w6" * 4
SCH = json.load(open(os.path.join(UI_DIR, "..", "contracts", "schemas", "ui_http.schema.json"), encoding="utf-8"))


def check(name, ok, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def eq(name, got, want):
    check(name, got == want, f"got {got!r}, want {want!r}")


def schema_errors(name, obj):
    v = jsonschema.Draft202012Validator({"$schema": SCH["$schema"], "$ref": "#/$defs/" + name, "$defs": SCH["$defs"]})
    return [e.message for e in v.iter_errors(obj)]


def basic(pw, user="anyone"):
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def req(port, path, headers=None, method="GET"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    c.putheader("Host", f"localhost:{port}")
    for k, v in (headers or {}).items():
        c.putheader(k, v)
    c.endheaders()
    r = c.getresponse()
    body = r.read()
    hd = {k.lower(): v for k, v in r.getheaders()}
    c.close()
    return r.status, hd, body


def jreq(port, path, headers=None):
    s, h, b = req(port, path, headers)
    return s, h, json.loads(b)


def fine_bins(row, n):
    return [(40 + row + 3 * i) % 200 for i in range(n)]


class Stack:
    """One dashboard assembled the way main() does: parser -> build -> auth.configure -> Server, plus the spectrum receiver."""

    def __init__(self, root, login, detail):
        self.pr, rp, rd = (os.path.join(root, n) for n in ("pipeline", "reports", "rollups"))
        for d in (os.path.join(self.pr, "analysis"), os.path.join(self.pr, "logs"), rp, rd):
            os.makedirs(d, exist_ok=True)
        now = time.time()
        recs = [self.bird(now - 300, 2, "Alpha Wren", "Aus a", False), self.bird(now - 200, 1, "Human vocal", "Human vocal", True),
                self.bird(now - 199, 1, "Human vocal", "Human vocal", True), self.bird(now - 100, 3, "Human whistle", "Human whistle", True)]
        with open(os.path.join(self.pr, "analysis", "birds.jsonl"), "wb") as f:
            for r in recs:
                f.write((json.dumps(r, separators=(",", ":")) + "\n").encode())
        argv = ["--pipeline-root", self.pr, "--reports-dir", rp, "--rollup-dir", rd, "--no-board", "--bind", "127.0.0.1", "--spectrum-port", "1"]
        self.token_file = os.path.join(root, "token.txt")
        if login:
            with open(self.token_file, "wb") as f:
                f.write(TOKEN.encode())
            argv += ["--auth-token-file", self.token_file]
        if detail:
            argv += ["--spectrum-detail"]
        args = U.make_parser({}).parse_args(argv)
        self.app = U.build(args)
        self.auth, self.tls, self.notices = A.configure(args.bind, args.pipeline_root, args.auth_token_file, args.no_auth, args.tls_cert, args.tls_key, log=lambda m: None)
        self.udp_port = self.app.spectrum.start(0)                       # the real receiver, an ephemeral loopback port
        self.app.start_backfill()
        self.app.housekeeping()
        self.srv = U.Server(("127.0.0.1", 0), self.app, set(), U.load_assets(), auth=self.auth, tls=self.tls)
        self.port = self.srv.server_address[1]
        self.srv.allowed_hosts = U.allowed_hosts_for(self.port, [])
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    @staticmethod
    def bird(t, beam, common, sci, non):
        return {"k": "bird", "t": t, "beam": beam, "level_dbfs": -60.0, "common": common, "scientific": sci, "confidence": 0.9,
                "provenance": "model_opinion", "in_region": True, "non_species": non}

    def send_rows(self, tier, rows, beam=0):
        n = R.SPEC_TIERS[tier]["n_bins"]
        for row in range(rows):
            self.sock.sendto(R.build_spectrum(tier, 0, beam, row, int(time.time() * 1e6), fine_bins(row, n)), ("127.0.0.1", self.udp_port))
        end = time.time() + 10
        while time.time() < end and self.app.spectrum.rows_received < rows:
            time.sleep(0.05)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.app.spectrum.close()
        self.sock.close()


def rows_ok(j, n_bins, n_rows):
    return (j["state"], j["n_bins"], [r["idx"] for r in j["rows"]], [list(base64.b64decode(r["bins"])) for r in j["rows"]]) == \
           ("live", n_bins, list(range(1, n_rows + 1)), [fine_bins(i, n_bins) for i in range(n_rows)])


def main():
    tmp = tempfile.mkdtemp(prefix="bma_auth_spec_")
    stacks = []
    try:
        # ================================================================ login + standard tier
        S = Stack(os.path.join(tmp, "a"), login=True, detail=False)
        stacks.append(S)
        S.send_rows(0, 5)
        eq("setup: the real UDP receiver took 5 BMAS rows and the server has a login (a real token file)", (S.app.spectrum.rows_received, S.auth.enabled), (5, True))
        P = S.port
        SPEC = "/api/spectrum?tier=standard&beam=0"

        # the gate runs before routing: every route, every method
        for path, label in ((SPEC, "/api/spectrum"), ("/api/status", "/api/status"), ("/", "/"), ("/static/spectrum.js", "/static/spectrum.js"), ("/nonexistent", "an unknown path")):
            s, h, b = req(P, path)
            j = json.loads(b)
            eq(f"no credential: GET {label} is 401 unauthorized (problem+json, no-store)", (s, j.get("code"), h["content-type"], h["cache-control"]), (401, "unauthorized", "application/problem+json", "no-store"))
        s, h, b = req(P, SPEC)
        eq("no credential: the 401 body is a schema-valid problem and says nothing about the spectrum feed", (schema_errors("problem", json.loads(b)), b"live" in b or b"rows" in b), ([], False))
        s, h, b = req(P, SPEC, method="POST")
        eq("no credential: POST /api/spectrum is 401, not 405 (the gate precedes the method check)", (s, json.loads(b).get("code")), (401, "unauthorized"))
        s, h, j = jreq(P, SPEC, {"Authorization": basic(WRONG)})
        eq("wrong token (Basic): 401 unauthorized, positive twin of the right token below", (s, j.get("code")), (401, "unauthorized"))
        s, h, j = jreq(P, SPEC, {"Cookie": "bma_auth=" + "0" * 64})
        eq("a stale/forged cookie: 401 unauthorized", (s, j.get("code")), (401, "unauthorized"))

        # Basic with the right token
        s, h, j = jreq(P, SPEC, {"Authorization": basic(TOKEN)})
        eq("Basic with the right token: 200 live, 46 bins, idx 1..5 and the exact bins that were sent", (s, rows_ok(j, 46, 5)), (200, True))
        eq("Basic: the payload validates against the spectrum schema and the response is no-store", (schema_errors("spectrum", j), h["cache-control"]), ([], "no-store"))
        s, h, j = jreq(P, "/api/spectrum?tier=standard&beam=4", {"Authorization": basic(TOKEN)})
        eq("Basic: beam 4 has no rows of its own yet (only beam 0 was fed): live is not claimed for it", (s, j["rows"]), (200, []))

        # cookie: GET /?token= then the cookie
        s, h, b = req(P, "/?token=" + TOKEN)
        cookie = h.get("set-cookie", "").split(";")[0]
        eq("GET /?token= answers 302 to / and sets the bma_auth cookie (64 hex)", (s, h.get("location"), cookie.startswith("bma_auth=") and len(cookie) == len("bma_auth=") + 64), (302, "/", True))
        s, h, j = jreq(P, SPEC, {"Cookie": cookie})
        eq("cookie auth: /api/spectrum is 200 live with the 5 exact rows", (s, rows_ok(j, 46, 5)), (200, True))
        s, h, j = jreq(P, "/api/spectrum?tier=standard&beam=0&since=3", {"Cookie": cookie})
        eq("cookie auth: the since cursor works through the login (rows 4 and 5 only)", [r["idx"] for r in j["rows"]], [4, 5])

        # X-Forwarded-For never matters: neither grants nor denies
        s, h, j = jreq(P, SPEC, {"Authorization": basic(TOKEN), "X-Forwarded-For": "198.51.100.7", "Forwarded": "for=198.51.100.7"})
        eq("authenticated with a foreign-looking X-Forwarded-For: still served (the source network plays no part)", (s, rows_ok(j, 46, 5)), (200, True))
        s, h, j = jreq(P, SPEC, {"X-Forwarded-For": "127.0.0.1"})
        eq("unauthenticated with X-Forwarded-For: 127.0.0.1: still 401 (a header is not a credential)", (s, j.get("code")), (401, "unauthorized"))

        # detail tier needs the flag, even for an authenticated client
        s, h, j = jreq(P, "/api/spectrum?tier=detail&beam=0", {"Authorization": basic(TOKEN)})
        eq("detail tier without --spectrum-detail: authenticated client gets 403 spectrum_detail_disabled", (s, j.get("code"), schema_errors("problem", j)), (403, "spectrum_detail_disabled", []))
        s, h, j = jreq(P, "/api/spectrum?tier=detail&beam=0")
        eq("detail tier, no credential: 401 (the login gate, not 403)", (s, j.get("code")), (401, "unauthorized"))

        # status carries the human-hidden count under the login
        s, h, j = jreq(P, "/api/status", {"Authorization": basic(TOKEN)})
        eq("status under the login: 200, human_hidden_24h is 2 (one merged Human vocal event, one whistle), the spectrum summary says 5 rows received",
           (s, j["human_hidden_24h"], j["spectrum"]["rows_received"], j["spectrum"]["detail_enabled"]), (200, 2, 5, False))
        eq("status under the login validates against the status schema", schema_errors("status", j), [])
        s, h, j = jreq(P, "/api/status", {"Cookie": cookie})
        eq("status through the cookie reports the same version as the server: ui_version is the contract version", (s, j["ui_version"], j["ui_version"] == SCH["x-contract-version"]), (200, U.UI_VERSION, True))
        eq("UI_VERSION equals the schema's x-contract-version (1.13.0)", (U.UI_VERSION, SCH["x-contract-version"]), ("1.13.0", "1.13.0"))
        S.close()
        stacks.pop()

        # the per-client request limit is keyed by the TCP peer, never by a header: rotating X-Forwarded-For does not buy more requests
        R_ = Stack(os.path.join(tmp, "r"), login=True, detail=False)
        stacks.append(R_)
        R_.send_rows(0, 1)
        got = []
        for i in range(12):
            s, h, j = jreq(R_.port, "/api/spectrum?tier=standard&beam=0", {"Authorization": basic(TOKEN), "X-Forwarded-For": "198.51.100.%d" % (i + 1)})
            got.append((s, h.get("retry-after")))
        eq("rate limit: 12 authenticated requests with 12 different X-Forwarded-For values: the burst of 10 is served, the 11th and 12th are 429 with Retry-After 1",
           got, [(200, None)] * 10 + [(429, "1")] * 2)
        eq("rate limit: the 429 is the spectrum problem rate_limited and validates against the problem schema", (j.get("code"), schema_errors("problem", j)), ("rate_limited", []))
        R_.close()
        stacks.remove(R_)

        # ================================================================ login + detail flag
        D = Stack(os.path.join(tmp, "d"), login=True, detail=True)
        stacks.append(D)
        D.send_rows(1, 6)
        eq("setup: the detail receiver took 6 detail rows", D.app.spectrum.rows_received, 6)
        s, h, j = jreq(D.port, "/api/spectrum?tier=detail&beam=0", {"Authorization": basic(TOKEN), "X-Forwarded-For": "203.0.113.50"})
        eq("with --spectrum-detail: an authenticated client (X-Forwarded-For from elsewhere) gets the detail tier live, 100 bands, 6 exact rows", (s, j["tier"], rows_ok(j, 100, 6)), (200, "detail", True))
        eq("detail payload validates against the schema", schema_errors("spectrum", j), [])
        s, h, j = jreq(D.port, "/api/spectrum?tier=detail&beam=0")
        eq("with --spectrum-detail, positive twin of the gate: the unauthenticated client still gets 401", (s, j.get("code")), (401, "unauthorized"))
        s, h, j = jreq(D.port, "/api/spectrum?tier=detail&beam=0", {"Authorization": basic(WRONG)})
        eq("with --spectrum-detail, a wrong token gets 401", (s, j.get("code")), (401, "unauthorized"))
        D.close()
        stacks.pop()

        # ================================================================ open server (no token file, loopback bind)
        O = Stack(os.path.join(tmp, "o"), login=False, detail=True)
        stacks.append(O)
        O.send_rows(0, 4)
        eq("setup: open server (no token file, loopback), 4 rows received, the server's auth is auth.OPEN", (O.app.spectrum.rows_received, O.srv.auth is A.OPEN, O.srv.auth.enabled), (4, True, False))
        s, h, j = jreq(O.port, "/api/status")
        eq("open server: /api/status needs no credential (200) and still counts 2 hidden human events", (s, j["human_hidden_24h"]), (200, 2))
        for hdrs, label in (({}, "no credential"), ({"Authorization": basic(TOKEN)}, "a Basic header with a token"), ({"X-Forwarded-For": "198.51.100.7"}, "a foreign X-Forwarded-For")):
            s, h, j = jreq(O.port, "/api/spectrum?tier=standard&beam=0", hdrs)
            eq(f"open server, {label}: /api/spectrum is 200 state auth_required, no rows, no feed details (open is not authenticated)",
               (s, j["state"], j["rows"], j["n_bins"], j["newest_idx"], j["next_since"], j["newest_age_s"], j["gated_now"], schema_errors("spectrum", j)), (200, "auth_required", [], 0, 0, 0, None, False, []))
        s, h, j = jreq(O.port, "/api/spectrum?tier=detail&beam=0")
        eq("open server with --spectrum-detail: the detail tier is auth_required too (not 403, not rows)", (s, j["state"], j["rows"]), (200, "auth_required", []))
        O.close()
        stacks.pop()
    finally:
        for st in stacks:
            st.close()
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{n_checks} checks, {len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
