#!/usr/bin/env python3
"""Tests for --ui-next (ui-http 1.12.0): the next dashboard's static files under /next/, real server on an ephemeral port, real HTTP.
Flag off = plain 404; flag on = exactly the listed paths with exact types, bodies and CSP; the login gate covers them; the shipped page
is CSP-clean (no inline script, no style attribute, no event-handler attribute) and every file it loads is one the server lists.

    python test_bma_ui_next.py     (PASS/FAIL lines, a final 'N checks, 0 failure(s)' line; exit 1 on failure)"""

import base64, html.parser, http.client, json, os, re, shutil, sys, tempfile, threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import auth as A  # noqa: E402
import bma_ui as U  # noqa: E402

fails, n_checks = [], 0
TOKEN = "T0ken-" + "a1b2c3d4e5f6" * 4
BASIC = {"Authorization": "Basic " + base64.b64encode(("u:" + TOKEN).encode()).decode()}
STATIC = os.path.join(HERE, "..", "ui-next", "static")
PATHS = [
    "/next/",
    "/next/index.html",
    "/next/app.css",
    "/next/app.js",
    "/next/review.js",
    "/next/adapter.js",
    "/next/engine/audition.js",
    "/next/engine/colormaps.js",
    "/next/engine/dsp.js",
    "/next/engine/render.js",
    "/next/engine/worklets.js",
]
JS, CSS, HTML = "text/javascript; charset=utf-8", "text/css; charset=utf-8", "text/html; charset=utf-8"
TYPES = {"/next/": HTML, "/next/index.html": HTML, "/next/app.css": CSS}
BASE_CSP = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
NEXT_CSP = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"


def check(name, ok, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def eq(name, got, want):
    check(name, got == want, f"got {got!r}, want {want!r}")


class Rig:
    def __init__(self, ui_next, auth, static_dir=None):
        self.root = tempfile.mkdtemp(prefix="bma_next_")
        pr = os.path.join(self.root, "p")
        os.makedirs(os.path.join(pr, "analysis"))
        board = U.Board("http://127.0.0.1:9", os.path.join(self.root, "nt"), enabled=False)
        app = U.App(pr, os.path.join(self.root, "r"), os.path.join(self.root, "ru"), board, window_h=48, log=lambda m: None)
        self.srv = U.Server(
            ("127.0.0.1", 0),
            app,
            set(),
            U.load_assets(),
            auth=A.Authenticator(TOKEN, log=lambda m: None) if auth else None,
            next_assets=U.load_next_assets(static_dir) if ui_next else None,
        )
        self.port = self.srv.server_address[1]
        self.srv.allowed_hosts = U.allowed_hosts_for(self.port, [])
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.root, ignore_errors=True)

    def get(self, path, headers=None, method="GET"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        c.request(method, path, headers=dict({"Host": "localhost:%d" % self.port}, **(headers or {})))
        r = c.getresponse()
        data = r.read()
        hd = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        return r.status, hd, data


def filebytes(path):
    rel = "index.html" if path in ("/next/", "/next/index.html") else path[len("/next/") :]
    with open(os.path.join(STATIC, *rel.split("/")), "rb") as f:
        return f.read()


class Refs(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts, self.links, self.inline, self.style_attrs, self.handlers = [], [], 0, 0, []
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script":
            if a.get("src"):
                self.scripts.append(a["src"])
            else:
                self._in_script = True
        if tag == "link" and a.get("href"):
            self.links.append(a["href"])
        self.style_attrs += "style" in a
        self.handlers += [k for k in a if k.startswith("on")]

    def handle_data(self, data):
        if self._in_script and data.strip():
            self.inline += 1

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False


def main():
    off = Rig(False, False)
    try:
        r = {p: off.get(p) for p in PATHS}
        eq(
            "flag off: every /next/ path is a 404 problem+json not_found",
            {p: (v[0], v[1]["content-type"], json.loads(v[2])["code"] if v[1]["content-type"] == "application/problem+json" else None) for p, v in r.items()},
            {p: (404, "application/problem+json", "not_found") for p in PATHS},
        )
        eq("flag off: the current dashboard is unchanged (GET / is 200 text/html)", off.get("/")[:1] + (off.get("/")[1]["content-type"],), (200, HTML))
    finally:
        off.close()
    on = Rig(True, False)
    try:
        got = {p: on.get(p) for p in PATHS}
        eq("flag on: the 11 listed paths answer 200", {p: v[0] for p, v in got.items()}, {p: 200 for p in PATHS})
        eq("flag on: content types are exact (html, css, javascript)", {p: v[1]["content-type"] for p, v in got.items()}, {p: TYPES.get(p, JS) for p in PATHS})
        eq("flag on: each body equals the file in ui-next/static byte for byte", {p: v[2] == filebytes(p) for p, v in got.items()}, {p: True for p in PATHS})
        eq("flag on: /next/ and /next/index.html are the same page", got["/next/"][2], got["/next/index.html"][2])
        eq(
            "flag on: every /next/ response carries the next CSP, which is the base CSP plus media-src 'self' blob: and nothing else",
            {p: v[1]["content-security-policy"] for p, v in got.items()},
            {p: NEXT_CSP for p in PATHS},
        )
        eq(
            "CSP of the current dashboard stays the exact base CSP (/, /static/app.js, an API problem)",
            [on.get(p)[1]["content-security-policy"] for p in ("/", "/static/app.js", "/api/nope")],
            [BASE_CSP] * 3,
        )
        eq(
            "flag on: no-cache, nosniff, frame DENY, same-origin resource policy on the page",
            [got["/next/"][1].get(k.lower()) for k in ("Cache-Control", "X-Content-Type-Options", "X-Frame-Options", "Cross-Origin-Resource-Policy")],
            ["no-cache", "nosniff", "DENY", "same-origin"],
        )
        eq(
            "flag on: the page's own options in the query are served (positive twin of the asset query refusal)",
            [on.get("/next/?r=canvas2d")[0], on.get("/next/index.html?clip=c001")[0]],
            [200, 200],
        )
        eq(
            "flag on: a query on any other /next/ path is a 404 (like /static/*)",
            [on.get("/next/app.js?x=1")[0], on.get("/next/engine/dsp.js?v=2")[0]],
            [404, 404],
        )
        eq(
            "flag on: /next (no slash), case and dot-dot variants, and files that exist in ui-next/static but are not listed are 404",
            [
                on.get(p)[0]
                for p in (
                    "/next",
                    "/NEXT/",
                    "/next/App.js",
                    "/next/engine/../app.js",
                    "/next/%2e%2e/app.js",
                    "/next/selftest.html",
                    "/next/dev-reload.js",
                    "/next/engine/",
                )
            ],
            [404] * 8,
        )
        eq(
            "flag on: HEAD is served with the same headers and no body, other methods are 405",
            [(lambda s, h, b: (s, h["content-length"], b))(*on.get("/next/app.js", method="HEAD")), on.get("/next/app.js", method="POST")[0]],
            [(200, str(len(filebytes("/next/app.js"))), b""), 405],
        )
        # the shipped page
        p = Refs()
        p.feed(got["/next/"][2].decode("utf-8"))
        eq(
            "page: no inline script, no style attribute, no event-handler attribute (the CSP forbids all three)",
            (p.inline, p.style_attrs, p.handlers),
            (0, 0, []),
        )
        loaded = ["/next/" + u for u in p.links + p.scripts]
        eq(
            "page: the stylesheet, the preloads and the six scripts are exactly these relative URLs, in order",
            p.links + p.scripts,
            ["app.css", "app.js", "review.js", "engine/dsp.js", "engine/colormaps.js", "engine/render.js", "engine/audition.js", "adapter.js", "app.js"],
        )
        eq("page: every file it loads is a listed path that answers 200 on this server", [(u, on.get(u)[0]) for u in loaded], [(u, 200) for u in loaded])
        eq("page: no absolute /-rooted URL (it must work under /next/)", [u for u in p.links + p.scripts if u.startswith("/") or "://" in u], [])
        for name in ("app.js", "review.js", "adapter.js"):
            src = filebytes("/next/" + name).decode("utf-8")
            eq(
                "%s: no eval, no Function constructor, no string timer, no innerHTML assignment other than clearing with ''" % name,
                re.findall(r"\beval\s*\(|new\s+Function\s*\(|setTimeout\s*\(\s*['\"]|\.innerHTML\s*=(?!\s*'')(?!\s*\"\")", src),
                [],
            )
    finally:
        on.close()
    gated = Rig(True, True)
    try:
        eq("login on: /next/ and /next/app.js without a credential are 401 (not public)", [gated.get(p)[0] for p in ("/next/", "/next/app.js")], [401, 401])
        eq("login on: the 401 carries no page", [json.loads(gated.get("/next/")[2])["code"], b"<html" in gated.get("/next/")[2]], ["unauthorized", False])
        s, h, b = gated.get("/next/", BASIC)
        eq(
            "login on: positive twin - with Basic credentials /next/ is 200, the exact page, no-store, next CSP",
            (s, b == filebytes("/next/"), h["cache-control"], h["content-security-policy"]),
            (200, True, "no-store", NEXT_CSP),
        )
        eq("login on: every listed path is 200 with credentials", [gated.get(p, BASIC)[0] for p in PATHS], [200] * len(PATHS))
    finally:
        gated.close()
    empty = tempfile.mkdtemp(prefix="bma_next_empty_")
    try:
        eq(
            "load_next_assets: an empty directory yields no routes and every listed path is reported missing (main() refuses to start with --ui-next then)",
            (U.load_next_assets(empty), U.next_assets_missing(U.load_next_assets(empty))),
            ({}, sorted(PATHS[1:])),
        )
        eq(
            "load_next_assets: the real directory yields the 11 routes and nothing is missing",
            (sorted(U.load_next_assets(STATIC)), U.next_assets_missing(U.load_next_assets(STATIC))),
            (sorted(PATHS), []),
        )
        eq(
            "make_parser: --ui-next is accepted and defaults to off",
            (U.make_parser().parse_args(["--ui-next"]).ui_next, U.make_parser().parse_args([]).ui_next),
            (True, False),
        )
    finally:
        shutil.rmtree(empty, ignore_errors=True)
    print("\n%d checks, %d failure(s)" % (n_checks, len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
