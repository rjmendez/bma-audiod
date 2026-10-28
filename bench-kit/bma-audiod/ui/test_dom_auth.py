#!/usr/bin/env python3
"""Browser test (real headless Microsoft Edge, the real bma_ui server with a login over synthetic pipeline files) of the dashboard
when the login is on: the one-URL cookie flow renders the dashboard, no credential shows the 401 problem instead of it, and a
session that stops being accepted shows 'Signed out: reopen the link with ?token=...' instead of a raw error.

    python test_dom_auth.py          (exit 0 and a final 'N checks, 0 failure(s)' line; exits 77 with 'SKIP' if Edge is missing)"""

import os, shutil, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bma_ui as U  # noqa: E402
import auth as A  # noqa: E402
import test_bma_ui as T  # noqa: E402
import test_dom_edge as D  # noqa: E402

check, text_of, Browser, EDGE = D.check, D.text_of, D.Browser, D.EDGE
TOKEN = "T0ken-" + "a1b2c3d4e5f6" * 4
SIGNED_OUT = "Signed out: reopen the link with ?token=..."


def main():
    if not os.path.exists(EDGE):
        print("SKIP msedge not found")
        return 77
    root = tempfile.mkdtemp(prefix="bma_ui_doma_")
    browsers, profiles = [], []
    try:
        now = time.time()
        pr, rp = os.path.join(root, "pipeline"), os.path.join(root, "reports")
        for d in (os.path.join(pr, "analysis"), os.path.join(pr, "logs"), rp):
            os.makedirs(d)
        T.jl(os.path.join(rp, "beam_bands.jsonl"), [T.bands_rec(now - i, None) for i in range(60, 0, -1)])
        T.jl(os.path.join(pr, "analysis", "birds.jsonl"), [T.bird(now - 300, 2, 0.8, "Alpha Wren", "Aus a")])
        board = U.Board("http://127.0.0.1:9", os.path.join(root, "none"), enabled=False)
        app = U.App(pr, rp, os.path.join(root, "rollups"), board, window_h=48, slot_fix_at=now - 40 * 86400)
        app.start_backfill()
        threading.Thread(target=app.run_loop, daemon=True).start()
        auth = A.Authenticator(TOKEN)
        srv = U.Server(("127.0.0.1", 0), app, set(), U.load_assets(), auth=auth)
        port = srv.server_address[1]
        srv.allowed_hosts = U.allowed_hosts_for(port, [])
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        prof = os.path.join(tempfile.gettempdir(), "bma_ui_doma_edge_%d_%d" % (os.getpid(), int(time.time())))
        profiles.append(prof)
        br = Browser(prof, 9400 + port % 500)
        browsers.append(br)
        base = f"http://127.0.0.1:{port}/"

        # no credential: the problem body, not the dashboard (a fresh profile has no cookie)
        dom = br.dump(base + "#/overview", wait=2.0)
        t = text_of(dom)
        check(
            "no credential: the browser gets no dashboard (headless Edge shows an empty page for a 401 Basic challenge; the problem body itself is asserted in test_auth.py)",
            "Last 6 h: most recent detections" not in t
            and "Alpha Wren" not in t
            and 'id="tab-overview"' not in dom
            and br.eval("document.querySelectorAll('.panel').length") == 0,
            t[:200],
        )

        # the bookmarkable one-URL login: ?token= sets the cookie, redirects to /, the dashboard renders with its data
        dom = br.dump(base + "?token=" + TOKEN + "#/overview")
        t = text_of(dom)
        check(
            "?token= flow: the dashboard rendered after the redirect (panel title and the detected species are present)",
            "Last 6 h: most recent detections" in t and "Alpha Wren" in t,
            t[:200],
        )
        check(
            "?token= flow: the address bar no longer holds the token",
            br.eval("location.href") == base + "#/overview" and TOKEN not in str(br.eval("location.href")),
            str(br.eval("location.href")),
        )
        check(
            "?token= flow: the token is not readable from script (HttpOnly cookie) and not in the page",
            TOKEN not in str(br.eval("document.cookie")) and TOKEN not in dom and "bma_auth" not in str(br.eval("document.cookie")),
        )
        ok = br.eval("api('/api/status').then(r => r.ok === true && r.data && typeof r.data.ui_version === 'string')")
        check("authenticated: the page's own api() helper gets /api/status through the cookie", ok is True, str(ok))

        # the session stops being accepted (server restart, changed token): the helper says so, the page shows it
        auth._cookie = "0" * 64  # what a restart does: the old cookie no longer matches
        r = br.eval("api('/api/status')")
        check("signed out: api() returns exactly the short 'Signed out' error, not the raw problem text", r == {"ok": False, "error": SIGNED_OUT}, str(r))
        t2 = ""
        for _ in range(40):  # the dashboard refreshes every 7 s
            t2 = text_of(br.eval("document.documentElement.outerHTML") or "")
            if SIGNED_OUT in t2:
                break
            time.sleep(0.5)
        check(
            "signed out: the dashboard strip shows the Signed out state after its next refresh, without 'Authentication required'",
            SIGNED_OUT in t2 and "Authentication required" not in t2,
            t2[:300],
        )

        # the sign-in FORM in a real browser: the page is served with Referrer-Policy: no-referrer, so Edge sends Origin: null on its own form post
        prof2 = os.path.join(tempfile.gettempdir(), "bma_ui_doma_edge2_%d_%d" % (os.getpid(), int(time.time())))
        profiles.append(prof2)
        br2 = Browser(prof2, 9900 + port % 90)
        browsers.append(br2)
        fill = "document.querySelector('input[name=token]').value = %r; document.querySelector('form').submit(); 1"
        br2.dump(base + "login", wait=2.0)
        br2.eval(fill % "not-the-token")
        t3 = ""
        for _ in range(40):  # wait for the page the post lands on, not a fixed 3 s
            t3 = text_of(br2.eval("document.documentElement.outerHTML") or "")
            if "Wrong token." in t3:
                break
            time.sleep(0.25)
        check(
            "sign-in form, wrong token: the page says 'Wrong token.' (the post reached the login, it was not refused as foreign)",
            "Wrong token." in t3 and "did not come from this page" not in t3,
            t3[:200],
        )
        br2.eval(fill % TOKEN)
        t4 = ""
        for _ in range(30):
            time.sleep(0.5)
            t4 = text_of(br2.eval("document.documentElement.outerHTML") or "")
            if "Alpha Wren" in t4:
                break
        check(
            "sign-in form, right token: Edge's own form post signs in and the dashboard renders with its data (no 'did not come from this page')",
            "Alpha Wren" in t4 and "did not come from this page" not in t4 and TOKEN not in str(br2.eval("location.href")),
            t4[:200],
        )
    finally:
        for b in browsers:
            b.close()
        shutil.rmtree(root, ignore_errors=True)
        for p in profiles:
            shutil.rmtree(p, ignore_errors=True)
    print(f"\n{D.n_checks} checks, {len(D.fails)} failure(s)")
    return 1 if D.fails else 0


if __name__ == "__main__":
    sys.exit(main())
