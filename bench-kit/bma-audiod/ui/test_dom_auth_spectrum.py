#!/usr/bin/env python3
"""Browser test (real headless Microsoft Edge) of the dashboard with the REAL login AND the REAL live spectrum together: servers are
assembled as in test_auth_spectrum.py (token file, auth.configure, the loopback BMAS receiver fed by a thread of real packets).

 - no credential: the browser gets no page at all (headless Edge shows an empty page for the 401 Basic challenge),
 - the one-URL cookie login: the System tab shows the spectrum panel LIVE, without the 'sign in required' badge,
 - the session stops being accepted: the dashboard strip says 'Signed out' and the spectrum pill turns 'unavailable' (it never shows stale rows as live),
 - twin: an open server (no token file) shows the 'sign in required' badge and its info button, and no rows.

    python test_dom_auth_spectrum.py     (contracts venv for jsonschema; exit 0 and 'N checks, 0 failure(s)'; exit 77 with SKIP if Edge is missing)"""

import os, shutil, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_dom_edge as D  # noqa: E402
import test_auth_spectrum as TA  # noqa: E402
import ref_binary as R  # noqa: E402

check, text_of, Browser, EDGE = D.check, D.text_of, D.Browser, D.EDGE
SIGNED_OUT = "Signed out: reopen the link with ?token=..."


def wait_for(br, expr, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        v = br.eval(expr)
        if D.truthy(v):
            return v
        time.sleep(0.25)
    return br.eval(expr)


class Feeder(threading.Thread):
    """Real BMAS datagrams (standard tier, mean beam 0) to the stack's real receiver at 10 rows/s, until stopped."""

    def __init__(self, stack):
        super().__init__(daemon=True)
        self.stack, self.stop, self.row = stack, threading.Event(), 0

    def run(self):
        n = R.SPEC_TIERS[0]["n_bins"]
        while not self.stop.is_set():
            self.stack.sock.sendto(R.build_spectrum(0, 0, 0, self.row, int(time.time() * 1e6), TA.fine_bins(self.row, n)), ("127.0.0.1", self.stack.udp_port))
            self.row += 1
            self.stop.wait(0.1)


def main():
    if not os.path.exists(EDGE):
        print("SKIP msedge not found")
        return 77
    root = tempfile.mkdtemp(prefix="bma_ui_domas_")
    browsers, profiles, stacks, feeders = [], [], [], []
    try:

        def new_browser(tag, port):
            prof = os.path.join(tempfile.gettempdir(), "bma_ui_domas_edge_%s_%d_%d" % (tag, os.getpid(), int(time.time())))
            profiles.append(prof)
            br = Browser(prof, 9400 + (port + len(tag) * 7) % 500)
            browsers.append(br)
            return br

        # ------------------------------------------------------------------ login on
        S = TA.Stack(os.path.join(root, "login"), login=True, detail=False)
        stacks.append(S)
        f = Feeder(S)
        feeders.append(f)
        f.start()
        base = f"http://127.0.0.1:{S.port}/"
        br = new_browser("login", S.port)
        dom = br.dump(base + "#/system", wait=2.0)
        t = text_of(dom)
        check(
            "before login: the browser gets no dashboard and no spectrum panel (empty page for the 401 challenge)",
            'id="sp-state"' not in dom and "Live spectrum" not in t and br.eval("document.querySelectorAll('.panel').length") == 0,
            t[:200],
        )
        dom = br.dump(base + "?token=" + TA.TOKEN + "#/system")
        t = text_of(dom)
        live = wait_for(br, "document.getElementById('sp-state') && document.getElementById('sp-state').textContent === 'live'")
        check(
            "after the cookie login the System tab shows the spectrum panel and its state pill reads exactly 'live'",
            live is True or bool(live),
            str(br.eval("document.getElementById('sp-state') && document.getElementById('sp-state').textContent")),
        )
        r = br.eval(
            "[document.getElementById('sp-state').getAttribute('data-state'), document.getElementById('sp-authtip').hidden, document.getElementById('sp-authtip').childElementCount, "
            "document.body.innerText.toLowerCase().includes('sign in required'), document.querySelector('.sp-info').textContent]"
        )
        check(
            "after the cookie login: data-state live, the auth info button is hidden and empty, the page never says 'sign in required', the info line names the standard tier",
            r == ["live", True, 0, False, "standard tier, 46 bands, 10 rows/s"],
            str(r),
        )
        rel = wait_for(br, "Spectrum.last && Spectrum.last.S.released >= 5")
        check(
            "after the cookie login the panel received and released rows (5+): the page's own fetch carried the cookie",
            bool(rel),
            str(br.eval("Spectrum.last && Spectrum.last.S")),
        )
        check(
            "the address bar no longer holds the token and the cookie is not readable from script",
            TA.TOKEN not in str(br.eval("location.href")) and TA.TOKEN not in str(br.eval("document.cookie")),
            str(br.eval("location.href")),
        )
        # the session stops being accepted (what a server restart does): the panel must not keep showing live
        S.auth._cookie = "0" * 64
        gone = wait_for(br, "document.getElementById('sp-state').textContent === 'unavailable'", timeout=20.0)
        check(
            "session no longer accepted: the spectrum pill turns exactly 'unavailable' (never 'live', never a stale-looking success)",
            bool(gone),
            str(br.eval("document.getElementById('sp-state').textContent")),
        )
        t2 = ""
        for _ in range(40):
            t2 = text_of(br.eval("document.documentElement.outerHTML") or "")
            if SIGNED_OUT in t2:
                break
            time.sleep(0.5)
        check("session no longer accepted: the dashboard strip shows the Signed out state (positive twin of the live state above)", SIGNED_OUT in t2, t2[:300])
        f.stop.set()
        br.close()
        browsers.remove(br)
        S.close()
        stacks.remove(S)

        # ------------------------------------------------------------------ open server: twin
        O = TA.Stack(os.path.join(root, "open"), login=False, detail=False)
        stacks.append(O)
        f2 = Feeder(O)
        feeders.append(f2)
        f2.start()
        br2 = new_browser("open", O.port)
        br2.dump(f"http://127.0.0.1:{O.port}/#/system")
        shown = wait_for(br2, "document.getElementById('sp-state') && document.getElementById('sp-state').textContent === 'sign in required'")
        r = br2.eval(
            "[document.getElementById('sp-state').textContent, document.getElementById('sp-state').getAttribute('data-state'), document.getElementById('sp-authtip').hidden, "
            "document.getElementById('sp-authtip').childElementCount > 0, Spectrum.last.S.released]"
        )
        check(
            "open server (no login): the same page shows 'sign in required' with its info button and no rows (open is not authenticated)",
            bool(shown) and r == ["sign in required", "auth_required", False, True, 0],
            str(r),
        )
        f2.stop.set()
    finally:
        for f in feeders:
            f.stop.set()
        for b in browsers:
            b.close()
        for s in stacks:
            s.close()
        shutil.rmtree(root, ignore_errors=True)
        for p in profiles:
            shutil.rmtree(p, ignore_errors=True)
    print(f"\n{D.n_checks} checks, {len(D.fails)} failure(s)")
    return 1 if D.fails else 0


if __name__ == "__main__":
    sys.exit(main())
