#!/usr/bin/env python3
"""Tooltip behaviour (static/tip.js) in real headless Microsoft Edge with real input events (DevTools Input.dispatchKeyEvent / MouseEvent / TouchEvent), against the
fixture dashboard server (ui/spectrum_fixture.py: the real static assets). Rules checked, each with a positive twin: keyboard focus opens the bubble and Escape closes it
without moving focus; hover opens after a delay and leaving closes it; a tap toggles; a tap elsewhere closes; only one bubble is open; aria-describedby wiring on every
button; text is set as text (markup in a tip is not parsed); the bubble stays inside the viewport on a phone-width page.

    python test_dom_tips.py          (exit 0 and 'N checks, 0 failure(s)'; exit 77 with 'SKIP' when Edge is missing)"""

import os, shutil, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.environ.get("BMA_UI_DIR", HERE)
sys.path.insert(0, UI_DIR)
sys.path.insert(1, HERE)
import test_dom_edge as D  # noqa: E402
import spectrum_fixture as F  # noqa: E402

check = D.check


def key(br, k, vk):
    for t in ("keyDown", "keyUp"):
        br.ws.call("Input.dispatchKeyEvent", {"type": t, "key": k, "code": k, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk})


def settle(br):
    """Two animation frames: every synchronous handler and the popover layout it triggers are done (replaces a fixed sleep)."""
    br.eval("new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r(1))))")


def until(br, expr, want=True, timeout=6.0):
    """Poll expr until it equals want (a page error never counts); -> the last value."""
    end = time.time() + timeout
    v = None
    while time.time() < end:
        v = br.eval(expr)
        if v == want:
            return v
        time.sleep(0.1)
    return v


def tab(br):
    key(br, "Tab", 9)


def esc(br):
    key(br, "Escape", 27)


def center(br, sel):
    return br.eval("(() => { const r = document.querySelector(%r).getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })()" % sel)


def move(br, x, y):
    br.ws.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})


def click(br, x, y):
    br.ws.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
    br.ws.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})


def tap(br, x, y):
    br.ws.call("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
    br.ws.call("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


OPEN = "document.querySelectorAll('.tip-bubble:popover-open').length"
HELP_BTN = "#help-slot .tip-btn"


class StubPage:
    """Recording stand-in for a page: eval answers from a list (the last answer repeats)."""

    def __init__(self, *answers):
        self.answers, self.n = list(answers), 0

    def eval(self, expr):
        self.n += 1
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def selfcheck_until():
    err = {"exception": "Uncaught", "detail": "ReferenceError: Tip is not defined"}
    p = StubPage(err, [False, True, True, True], [True, True, True, True])
    r = until(p, "x", [True, True, True, True], 5.0)
    check(
        "until: a page error and a half-laid-out answer are not the wanted value; it polls until the full answer (3 evals)",
        r == [True, True, True, True] and p.n == 3,
        str((r, p.n)),
    )
    p = StubPage(1)
    check(
        "until (positive twin): a wrong value ends at the timeout and returns the last value, not a success", until(p, "x", 2, 0.3) == 1 and p.n >= 2, str(p.n)
    )


def main():
    selfcheck_until()
    if not os.path.exists(D.EDGE):
        print("SKIP msedge not found")
        return 77
    fx = F.Fixture()
    port = fx.start()
    prof = os.path.join(tempfile.gettempdir(), "bma_ui_tips_edge_%d_%d" % (os.getpid(), int(time.time())))
    br = None
    try:
        br = D.Browser(prof, 9400 + port % 500)
        br.dump("http://127.0.0.1:%d/#/system" % port, wait=2.0)
        until(br, "typeof Spectrum !== 'undefined' && !!Spectrum.last", True, 15.0)  # spectrum.js is fetched when the System tab is first shown
        br.ws.call("Emulation.setTouchEmulationEnabled", {"enabled": False})
        br.resize(1400, 1000)  # the viewport must not depend on the runner's default window
        until(br, "document.readyState === 'complete'", True, 15.0)
        br.eval("document.fonts.ready.then(() => true)")  # fonts decide the width of the button and the bubble
        until(br, "document.querySelector('%s').getBoundingClientRect().width >= 24" % HELP_BTN, True, 10.0)

        # ---- markup and wiring
        r = br.eval(
            """(() => { const b = document.querySelector('%s'); const t = document.getElementById(b.getAttribute('aria-describedby'));
          return [b.tagName, b.type, b.getAttribute('aria-label'), b.getAttribute('aria-expanded'), t.getAttribute('role'), t.hasAttribute('popover'), t.textContent.startsWith('Read-only. Detections are model opinions, not confirmed identifications.'), b.getBoundingClientRect().width >= 24]; })()"""
            % HELP_BTN
        )
        check(
            "help button: a real <button type=button> named 'About this dashboard', collapsed, described by a role=tooltip popover bubble holding the exact disclaimer, at least 24 px wide",
            r == ["BUTTON", "button", "About this dashboard", "false", "tooltip", True, True, True],
            str(r)
            + " "
            + str(
                br.eval(
                    "({w: innerWidth, h: innerHeight, ready: document.readyState, sheets: document.styleSheets.length, fonts: document.fonts.status, rect: JSON.stringify(document.querySelector('%s').getBoundingClientRect())})"
                    % HELP_BTN
                )
            ),
        )
        r = br.eval("""(() => { const b = Array.from(document.querySelectorAll('.tip-btn')); const ids = b.map(x => x.getAttribute('aria-describedby'));
          return [b.length >= 3, new Set(ids).size === ids.length, ids.every(i => { const t = document.getElementById(i); return t && t.classList.contains('tip-bubble') && t.getAttribute('role') === 'tooltip' && t.textContent.length > 20; }), b.every(x => x.getAttribute('aria-label').length > 3)]; })()""")
        check(
            "every info button on the System tab has a unique aria-describedby that resolves to a tooltip bubble with text, and an accessible name",
            r == [True, True, True, True],
            str(r),
        )
        r = br.eval(
            "(() => { const f = Tip.tip('<img src=x onerror=window.__tipxss=1><b>bold</b>', { label: 'x' }); const bub = f.querySelector('.tip-bubble'); document.body.append(f); const o = [bub.children.length, bub.textContent, typeof window.__tipxss]; bub.remove(); document.body.lastElementChild.remove(); return o; })()"
        )
        check(
            "tip text is set as text: markup in a tip is shown literally, not parsed (no child element, no handler ran)",
            r == [0, "<img src=x onerror=window.__tipxss=1><b>bold</b>", "undefined"],
            str(r),
        )

        # ---- keyboard: focus opens, Escape closes and focus stays
        br.eval("document.activeElement && document.activeElement.blur(); document.body.focus(); window.scrollTo(0, 0)")
        tab(br)
        settle(br)
        r = br.eval("[document.activeElement.matches('%s'), %s, document.querySelector('%s').getAttribute('aria-expanded')]" % (HELP_BTN, OPEN, HELP_BTN))
        check("keyboard: Tab onto the help button opens its bubble at once (aria-expanded true)", r == [True, 1, "true"], str(r))
        LAYOUT = """(() => { const b = document.querySelector('%s').getBoundingClientRect(), t = document.getElementById(document.querySelector('%s').getAttribute('aria-describedby')).getBoundingClientRect();
          return [t.top >= b.bottom, t.left >= 8, t.right <= document.documentElement.clientWidth - 8, t.width > 0 && t.height > 0]; })()""" % (
            HELP_BTN,
            HELP_BTN,
        )
        r = until(br, LAYOUT, [True, True, True, True])
        check("keyboard: the open bubble sits below the button, inside the viewport, and has a size", r == [True, True, True, True], str(r))
        esc(br)
        settle(br)
        r = br.eval("[document.activeElement.matches('%s'), %s, document.querySelector('%s').getAttribute('aria-expanded')]" % (HELP_BTN, OPEN, HELP_BTN))
        check("keyboard: Escape closes the bubble and focus stays on the button (dismissible without moving focus)", r == [True, 0, "false"], str(r))
        tab(br)
        settle(br)
        r = br.eval("[document.activeElement.matches('%s'), %s]" % (HELP_BTN, OPEN))
        check("keyboard: positive twin - Tab away from the button moves focus on and no bubble is open", r == [False, 0], str(r))
        br.eval("document.activeElement.blur()")
        tab(br)
        settle(br)
        tab(br)
        settle(br)
        br.eval("document.querySelector('%s').focus()" % HELP_BTN)
        esc(br)
        r = br.eval(OPEN)
        check("Escape with nothing open does nothing (no error, still closed)", r == 0, str(r))

        # ---- hover: delay, then open; leaving closes
        x, y = center(br, HELP_BTN)
        early, r = None, None
        for _ in range(5):  # the 80 ms read is only meaningful when this process was fast enough to take it: retry otherwise
            move(br, 700, 700)
            until(br, OPEN, 0)
            move(br, 5, 500)
            t0 = time.time()
            move(br, x, y)
            early = br.eval(OPEN)
            fast = time.time() - t0 < 0.25
            if fast:
                break
        until(br, OPEN, 1)
        r = br.eval("[%s, document.querySelector('%s').getAttribute('aria-expanded')]" % (OPEN, HELP_BTN))
        check(
            "hover: the bubble is not open right after the pointer arrives (delay, read within 250 ms) and is open once the delay has passed",
            fast and early == 0 and r == [1, "true"],
            str((early, fast, r)),
        )
        br.eval("document.activeElement.blur()")  # a focused button stays open (keyboard), so blur it before the pointer leaves
        move(br, 700, 700)
        until(br, OPEN, 0)
        r = br.eval(OPEN)
        check("hover: moving the pointer away closes it (positive twin of the open above)", r == 0, str(r))
        move(br, x, y)
        until(br, OPEN, 1)
        bx, by = br.eval(
            "(() => { const r = document.getElementById(document.querySelector('%s').getAttribute('aria-describedby')).getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })()"
            % HELP_BTN
        )
        move(br, bx, by)
        settle(br)
        r = br.eval(OPEN)
        check("hover: the pointer may move onto the bubble and it stays open (hoverable, WCAG 1.4.13)", r == 1, str(r))
        move(br, 700, 900)
        settle(br)
        esc(br)

        # ---- touch / tap toggles
        br.ws.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
        x, y = center(br, HELP_BTN)
        tap(br, x, y)
        settle(br)
        r1 = br.eval("[%s, document.querySelector('%s').getAttribute('aria-expanded')]" % (OPEN, HELP_BTN))
        tap(br, x, y)
        settle(br)
        r2 = br.eval(OPEN)
        tap(br, x, y)
        settle(br)
        r3 = br.eval(OPEN)
        tap(br, 700, 900)
        settle(br)
        r4 = br.eval(OPEN)
        check(
            "touch: a tap opens the bubble, a second tap closes it, a third opens it again, and a tap elsewhere closes it",
            r1 == [1, "true"] and r2 == 0 and r3 == 1 and r4 == 0,
            str((r1, r2, r3, r4)),
        )
        br.ws.call("Emulation.setTouchEmulationEnabled", {"enabled": False})

        # ---- only one open at a time (mouse click pins)
        sel_all = "document.querySelectorAll('.tip-btn')"
        n = br.eval(sel_all + ".length")
        br.eval("window.scrollTo(0, 0)")
        settle(br)
        a, b2 = center(br, HELP_BTN), center(br, "#spectrum-panel .tip-btn")  # the lower button first: an open bubble covers what lies below its own button
        click(br, *b2)
        settle(br)
        one = br.eval("[%s, document.querySelector('#spectrum-panel .tip-btn').getAttribute('aria-expanded')]" % OPEN)
        click(br, *a)
        settle(br)
        r = br.eval(
            "[%s, document.querySelector('%s').getAttribute('aria-expanded'), document.querySelector('#spectrum-panel .tip-btn').getAttribute('aria-expanded'), document.querySelectorAll('.tip-btn[aria-expanded=true]').length]"
            % (OPEN, HELP_BTN)
        )
        check(
            "only one bubble is open at a time: a click opens the spectrum bubble (positive twin), a click on a second button closes it and opens the second (one popover open, one aria-expanded true)",
            n >= 3 and one == [1, "true"] and r == [1, "true", "false", 1],
            str((n, one, r)),
        )
        click(br, *a)
        settle(br)
        r = br.eval(OPEN)
        check("a click on the open button closes it (toggle)", r == 0, str(r))

        # ---- narrow screen: the bubble stays in the viewport
        br.resize(360, 800)
        settle(br)
        br.eval("window.scrollTo(0, 0)")
        r = br.eval("""(() => { const all = Array.from(document.querySelectorAll('.tip-btn')); const out = [];
          for (const b of all.slice(0, 6)) { b.scrollIntoView({block: 'center'}); }
          return all.length; })()""")
        settle(br)
        res = []
        for i in range(r):
            br.eval("window.scrollTo(0, 0)")
            pos = br.eval(
                "(() => { const b = document.querySelectorAll('.tip-btn')[%d]; b.scrollIntoView({block: 'center'}); const r = b.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })()"
                % i
            )
            settle(br)
            click(br, *pos)
            settle(br)
            until(br, "!!document.querySelector('.tip-bubble:popover-open')")
            res.append(
                br.eval(
                    """(() => { const t = document.querySelector('.tip-bubble:popover-open'); if (!t) return null; const r = t.getBoundingClientRect(); return [r.left >= 7.5, r.right <= document.documentElement.clientWidth - 7.5, r.width <= 360 - 16 + 0.5]; })()"""
                )
            )
            esc(br)
        check(
            "phone width (360 px): each of the 3 bubbles opens by click and stays 8 px inside the viewport on both sides",
            len(res) == 3 and all(v == [True, True, True] for v in res),
            str(res),
        )
        br.resize(1400, 1000)
    finally:
        if br:
            br.close()
        fx.stop()
        shutil.rmtree(prof, ignore_errors=True)
    print(f"\n{D.n_checks} checks, {len(D.fails)} failure(s)")
    return 1 if D.fails else 0


if __name__ == "__main__":
    sys.exit(main())
