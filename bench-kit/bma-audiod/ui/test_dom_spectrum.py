#!/usr/bin/env python3
"""The live spectrum panel (static/spectrum.js, System tab) in real headless Microsoft Edge against the fixture dashboard server (ui/spectrum_fixture.py: the real static
assets and a deterministic /api/spectrum whose band 30 holds -60 dB and every other band -85 dB, compare levels 40 + 10 * beam + band). Real pixels are read back from the
canvases and compared with colours and columns computed here from the contract's band edges (not from the code under test); polling, freezing, states and indicators are
observed through the requests the fixture server saw and the DOM. Exits 77 with SKIP when Edge is missing.

    python test_dom_spectrum.py          (exit 0 and 'N checks, 0 failure(s)')"""

import math, os, shutil, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.environ.get("BMA_UI_DIR", HERE)
sys.path.insert(0, UI_DIR)
sys.path.insert(1, HERE)
import test_dom_edge as D  # noqa: E402
import spectrum_fixture as F  # noqa: E402

check = D.check
ML, MR, MT = 44, 8, 6  # plot margins of the panel (spectrum.js)
VIRIDIS = {
    48: [66, 64, 134],
    72: [55, 91, 141],
    96: [44, 114, 142],
    128: [33, 145, 140],
    183: [76, 194, 108],
    253: [248, 230, 33],
    254: [251, 231, 35],
    255: [253, 231, 37],
}  # matplotlib viridis(i / 255)
MAGMA = {48: [54, 16, 107], 255: [252, 253, 191]}
TOP = [VIRIDIS[253], VIRIDIS[254], VIRIDIS[255]]  # a persistence cell at (or just under) Hnorm: the top of the colour map
FX = F.Fixture()
EDGES = FX.edges


def cols(band, pw, axis="log"):
    """first and last plot column (0-based) whose centre frequency lies in band `band`, computed from the edges"""

    def x(f):
        return (math.log(f / 50.0) / math.log(16000 / 50.0) if axis == "log" else (f - 50.0) / (16000 - 50.0)) * pw

    return math.ceil(x(EDGES[band]) - 0.5), math.ceil(x(EDGES[band + 1]) - 0.5) - 1


PX = """(kind, x, y) => { const c = Spectrum.last.canvases[kind]; return Array.from(c.getContext('2d').getImageData(x, y, 1, 1).data).slice(0, 3); }"""


def px(br, kind, x, y):
    return br.eval("(%s)(%r, %d, %d)" % (PX, kind, x, y))


def setsel(br, id_, v):
    br.eval("(() => { const s = document.getElementById(%r); s.value = %r; s.dispatchEvent(new Event('change')); })()" % (id_, v))


def click(br, id_):
    br.eval("document.getElementById(%r).click()" % id_)


def key(br, k, vk):
    for t in ("keyDown", "keyUp"):
        br.ws.call("Input.dispatchKeyEvent", {"type": t, "key": k, "code": k, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk})


def move(br, x, y):
    br.ws.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})


def wait_for(br, expr, timeout=12.0):
    end = time.time() + timeout
    while time.time() < end:
        v = br.eval(expr)
        if D.truthy(v):
            return v
        time.sleep(0.25)
    return br.eval(expr)


def open_page(prof_name, port, reduced=False, fx=None):
    prof = os.path.join(tempfile.gettempdir(), "bma_ui_spec_edge_%s_%d_%d" % (prof_name, os.getpid(), int(time.time())))
    br = D.Browser(prof, 9400 + (port + len(prof_name)) % 500)
    br.resize(1400, 1900)
    # both branches set the media feature: a runner with OS animations off reports reduce, which must not decide the normal-motion checks
    br.ws.call("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-reduced-motion", "value": "reduce" if reduced else "no-preference"}]})
    br.dump("http://127.0.0.1:%d/#/system" % port, wait=1.5)
    return br, prof


def freeze_with_data(br, min_rows=150):
    wait_for(br, "Spectrum.last && Spectrum.last.S.released >= %d" % min_rows)
    click(br, "sp-freeze")
    br.eval("Spectrum.last.flush()")


class StubPage:
    """Recording stand-in for a page: eval answers from a list (the last answer repeats), like a page whose script loads late."""

    def __init__(self, *answers):
        self.answers, self.n = list(answers), 0

    def eval(self, expr):
        self.n += 1
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def selfcheck_wait():
    err = {"exception": "Uncaught", "detail": "ReferenceError: Spectrum is not defined"}
    p = StubPage(err, err, True)
    check(
        "wait_for: a page error (script not loaded yet) is not a success; it keeps waiting and returns the later True after 3 evals",
        wait_for(p, "x", 5.0) is True and p.n == 3,
        str(p.n),
    )
    p = StubPage(0, 7)
    check("wait_for (positive twin): the first truthy value returns at once", wait_for(p, "x", 5.0) == 7 and p.n == 2, str(p.n))


def main():
    selfcheck_wait()
    if not os.path.exists(D.EDGE):
        print("SKIP msedge not found")
        return 77
    fx = F.Fixture()
    port = fx.start()
    profs, brs = [], []
    try:
        br, prof = open_page("live", port)
        profs.append(prof)
        brs.append(br)
        t_load = time.time()
        ok = wait_for(br, "Spectrum.last && Spectrum.last.S.released >= 150")
        check(
            "data flows: the panel mounted on the System tab and released 150+ rows of the 60 s history",
            bool(ok),
            str(br.eval("Spectrum.last && Spectrum.last.S")),
        )
        # ---- frame rate, before freezing: the display updates at 10+ fps
        f0 = br.eval("Spectrum.last.S.frames")
        time.sleep(2.0)
        f1 = br.eval("Spectrum.last.S.frames")
        check("live display updates at 10+ frames per second (%.1f fps measured over 2 s)" % ((f1 - f0) / 2.0), (f1 - f0) / 2.0 >= 10.0, str((f0, f1)))
        r = br.eval(
            "[document.getElementById('sp-state').textContent, document.getElementById('sp-state').getAttribute('data-state'), getComputedStyle(document.getElementById('sp-gated')).display, document.querySelector('.sp-info').textContent]"
        )
        check(
            "indicators: state pill text 'live' (data-state live), the 'speech suppressed' pill is not displayed, the info line says 'standard tier, 46 bands, 10 rows/s'",
            r == ["live", "live", "none", "standard tier, 46 bands, 10 rows/s"],
            str(r),
        )
        n1 = len(fx.log)
        time.sleep(1.6)
        n2 = len(fx.log)
        check("polling about every 500 ms while the System tab is visible (3 to 5 requests in 1.6 s)", 3 <= n2 - n1 <= 5, str(n2 - n1))
        q = [x for x in fx.log if "since=" in x][-1]
        check(
            "polling is incremental: later requests carry since=<last idx>, rev=1, compare=true and band=5; the first request had no since",
            "since=" in q
            and "rev=1" in q
            and "compare=true" in q
            and "band=5" in q
            and "tier=standard" in q
            and "beam=0" in q
            and "since=" not in [x for x in fx.log if "tier=standard" in x][0],
            q + " | " + [x for x in fx.log if "tier=standard" in x][0],
        )

        # ---- controls through the real UI, then freeze and read pixels
        setsel(br, "sp-range", "-100,-20")
        freeze_with_data(br)
        r = br.eval("[document.getElementById('sp-freeze').getAttribute('aria-pressed'), document.getElementById('sp-state').textContent]")
        check("freeze: the button is pressed and the state line says 'live, frozen'", r == ["true", "live, frozen"], str(r))
        W = br.eval("Spectrum.last.canvases.waterfall.width")
        pw = W - ML - MR
        a, b = cols(30, pw)
        pk = [px(br, "waterfall", ML + c, MT + 1) for c in (a, (a + b) // 2, b)]
        nz = [px(br, "waterfall", ML + a - 1, MT + 1), px(br, "waterfall", ML + b + 1, MT + 1), px(br, "waterfall", ML + cols(10, pw)[0] + 2, MT + 1)]
        check(
            "waterfall: the -60 dB peak of band 30 fills exactly its columns %d..%d (log axis, %d px wide) with viridis(128) = %s: first, middle and last column"
            % (a, b, pw, VIRIDIS[128]),
            pk == [VIRIDIS[128]] * 3,
            str(pk),
        )
        check(
            "waterfall: positive twin - the columns just outside the band and a column of band 10 hold the -85 dB noise colour viridis(48) = %s" % VIRIDIS[48],
            nz == [VIRIDIS[48]] * 3,
            str(nz),
        )
        # compare strip: beam 1 and beam 8 at the newest column
        cx = ML + pw - 2
        c1, c8 = px(br, "compare", cx, MT + 3), px(br, "compare", cx, MT + 7 * 14 + 3)
        check(
            "compare strip: beam 1 (level 45 = -77.5 dB) is viridis(72) %s and beam 8 (115 = -42.5 dB) is viridis(183) %s at the newest column"
            % (VIRIDIS[72], VIRIDIS[183]),
            [c1, c8] == [VIRIDIS[72], VIRIDIS[183]],
            str([c1, c8]),
        )
        r = br.eval(
            "[Spectrum.last.canvases.compare.height, Array.from(document.querySelectorAll('.sp-canv')).map(c => [c.getAttribute('role'), c.getAttribute('tabindex'), c.getAttribute('aria-label')])]"
        )
        check(
            "canvases: three focusable role=img canvases with exact accessible names",
            r
            == [
                132,
                [
                    ["img", "0", "Persistence spectrum, mean of 8 beams, level against frequency"],
                    ["img", "0", "Waterfall spectrogram, mean of 8 beams, frequency across, time down, newest at the top"],
                    ["img", "0", "Beam compare strip, octave band 2-4 kHz, beams 1 to 8 over time"],
                ],
            ],
            str(r),
        )
        # persistence (live mode: decaying, so only presence / absence is exact)
        pc = (a + b) // 2
        bgc = br.eval(
            "(() => { const m = /rgba?\\((\\d+)[ ,]+(\\d+)[ ,]+(\\d+)/.exec(getComputedStyle(Spectrum.last.canvases.persistence).backgroundColor); return [+m[1], +m[2], +m[3]]; })()"
        )
        pe = [
            px(br, "persistence", ML + pc, MT + 82),
            px(br, "persistence", ML + pc, MT + 40),
            px(br, "persistence", ML + cols(29, pw)[0] + 1, MT + 82),
            px(br, "persistence", ML + cols(20, pw)[0] + 2, MT + 134),
        ]
        check(
            "persistence: the peak band lights its level row (y 82 = -60 dB) and nothing at y 40; the neighbouring band is empty there; a noise band lights its own row (y 134 = -85 dB)",
            pe[0] not in (bgc, None) and pe[1] == bgc and pe[2] == bgc and pe[3] != bgc,
            str((pe, bgc)),
        )

        # ---- readout (hover) with real mouse events
        rect = br.eval(
            "(() => { Spectrum.last.canvases.waterfall.scrollIntoView(); const r = Spectrum.last.canvases.waterfall.getBoundingClientRect(); return [r.left, r.top, r.width]; })()"
        )
        time.sleep(0.3)
        rect = br.eval("(() => { const r = Spectrum.last.canvases.waterfall.getBoundingClientRect(); return [r.left, r.top, r.width]; })()")
        move(br, rect[0] + ML + pc, rect[1] + MT + 1)
        time.sleep(0.2)
        r = br.eval("document.querySelector('.sp-readout').textContent")
        check("hover on the waterfall prints band, level and age: '6.35-6.73 kHz, -60 dB, 0.2 s ago'", r == "6.35-6.73 kHz, -60 dB, 0.2 s ago", r)
        move(br, rect[0] + ML + cols(10, pw)[0] + 2, rect[1] + MT + 1)
        time.sleep(0.2)
        r = br.eval("document.querySelector('.sp-readout').textContent")
        check("hover on a noise band reads -85 dB (positive twin: not the peak value)", r.endswith(", -85 dB, 0.2 s ago") and not r.startswith("6.35"), r)
        prect = br.eval("(() => { const r = Spectrum.last.canvases.persistence.getBoundingClientRect(); return [r.left, r.top]; })()")
        move(br, prect[0] + ML + pc, prect[1] + MT + 83)
        time.sleep(0.2)
        r = br.eval("document.querySelector('.sp-readout').textContent")
        check(
            "hover on the persistence plot at the peak row: band, the level under the cursor (-60.2 dB) and the newest row's value (-60 dB)",
            r == "6.35-6.73 kHz, -60.2 dB (newest row -60 dB)",
            r,
        )
        move(br, 5, 5)
        time.sleep(0.2)
        check(
            "leaving the canvas clears the readout (positive twin of the readings above)",
            br.eval("document.querySelector('.sp-readout').textContent") == "",
            br.eval("document.querySelector('.sp-readout').textContent"),
        )
        br.eval("document.querySelector('.sp-readout').textContent = ''; Spectrum.last.canvases.waterfall.focus()")
        key(br, "ArrowDown", 40)
        time.sleep(0.15)
        r1 = br.eval("document.querySelector('.sp-readout').textContent")
        for _ in range(20):
            key(br, "ArrowRight", 39)
        time.sleep(0.15)
        r2 = br.eval("document.querySelector('.sp-readout').textContent")
        import re

        check(
            "keyboard: arrow keys on the focused waterfall move a crosshair and print band, level and age (and the band changes when it moves 80 px right)",
            re.match(r"^[\d.]+(-[\d.]+ k?Hz| Hz-[\d.]+ kHz|-[\d.]+ kHz), -\d+(\.\d)? dB, [\d.]+ s ago$", r1 or "") is not None
            and re.match(r"^[\d.]+", r2 or "") is not None
            and r1.split(",")[0] != r2.split(",")[0],
            str((r1, r2)),
        )

        # ---- summary and tables
        r = br.eval("document.querySelector('.sp-summary').textContent")
        check(
            "accessible summary: exact text from the newest row",
            r == "Live, mean of 8 beams. Loudest band 6.35-6.73 kHz at -60 dB, 25 dB above the median band. Speech suppressed 0 s of the last 30 s.",
            r,
        )
        br.eval("document.querySelector('.sp-details').open = true")
        time.sleep(0.3)
        rows = br.eval("Array.from(document.querySelectorAll('.sp-details tbody tr')).map(t => [t.children[0].textContent, t.children[1].textContent])")
        check(
            "accessible table: 46 rows, band 30 reads -60, band 0 reads -85 (text, not colour)",
            isinstance(rows, list) and len(rows) == 46 and rows[30] == ["6.35-6.73 kHz", "-60"] and rows[0] == ["50-250 Hz", "-85"],
            str(rows[:2]) + str(len(rows) if rows else rows),
        )

        # ---- axis, colour map, range: the same peak moves / recolours
        setsel(br, "sp-axis", "lin")
        br.eval("Spectrum.last.renderNow()")
        time.sleep(0.2)
        la, lb = cols(30, pw, "lin")
        pkl = [px(br, "waterfall", ML + c, MT + 1) for c in (la, (la + lb) // 2, lb)]
        old = px(br, "waterfall", ML + pc, MT + 1)
        check(
            "linear axis: the peak is now at columns %d..%d (frequency across 50-16000 Hz) and the old log column %d no longer shows it" % (la, lb, pc),
            pkl == [VIRIDIS[128]] * 3 and old != VIRIDIS[128],
            str((pkl, old)),
        )
        setsel(br, "sp-axis", "log")
        setsel(br, "sp-cmap", "magma")
        pkm, nzm = px(br, "waterfall", ML + pc, MT + 1), px(br, "waterfall", ML + a - 1, MT + 1)
        check(
            "colour map magma: the -85 dB noise is magma(48) %s (the peak -60 dB is magma(128), a different colour from the noise)" % MAGMA[48],
            nzm == MAGMA[48] and pkm != nzm,
            str((pkm, nzm)),
        )
        setsel(br, "sp-cmap", "grey")
        pkg = px(br, "waterfall", ML + pc, MT + 1)
        check("colour map grey: the peak (index 128) is grey (128,128,128)", pkg == [128, 128, 128], str(pkg))
        setsel(br, "sp-cmap", "viridis")
        setsel(br, "sp-range", "custom")
        br.eval(
            "(() => { const lo = document.getElementById('sp-lo'), hi = document.getElementById('sp-hi'); lo.value = '-100'; hi.value = '-60'; hi.dispatchEvent(new Event('change')); })()"
        )
        pkr, nzr = px(br, "waterfall", ML + pc, MT + 1), px(br, "waterfall", ML + a - 1, MT + 1)
        check(
            "custom dB range -100..-60: the -60 dB peak is the top of the map viridis(255) %s and the -85 dB noise is viridis(96) %s (96 = round(0.375 * 255))"
            % (VIRIDIS[255], VIRIDIS[96]),
            [pkr, nzr] == [VIRIDIS[255], VIRIDIS[96]],
            str((pkr, nzr)),
        )
        check(
            "legend labels follow the range",
            br.eval("Array.from(document.querySelectorAll('.sp-legendbar span')).map(s => s.textContent)") == ["-100 dB", "-60 dB"],
            str(br.eval("Array.from(document.querySelectorAll('.sp-legendbar span')).map(s => s.textContent)")),
        )

        # ---- frame time on the fixture
        bm = br.eval("(() => { const r = Spectrum.last.bench(60); return r; })()")
        print(
            "INFO frame time on the fixture (full redraw of persistence + waterfall + compare, %s px wide, %s bands): mean %.2f ms, p95 %.2f ms, max %.2f ms over %s frames"
            % (bm["width"], bm["bins"], bm["mean"], bm["p95"], bm["max"], bm["n"])
        )
        check("frame budget: the full redraw averages under 12 ms (a 30 fps cap needs 33 ms) at the test width", bm["mean"] < 12.0, str(bm))

        # ---- freeze stops polling; resume fetches one newest row only
        n1 = len(fx.log)
        time.sleep(1.5)
        n2 = len(fx.log)
        click(br, "sp-freeze")
        time.sleep(1.2)
        n3 = len(fx.log)
        first_after = fx.log[n2:][0] if len(fx.log) > n2 else ""
        check(
            "freeze stops polling (0 requests in 1.5 s while frozen) and resume polls again, the first request asking for limit=1 (rows received while frozen are dropped)",
            n2 == n1 and n3 > n2 and "limit=1" in first_after and "since=" not in first_after,
            str((n1, n2, n3, first_after)),
        )
        # ---- beam selection and compare band reach the server
        setsel(br, "sp-beam", "3")
        time.sleep(1.2)
        last = fx.log[-1]
        check(
            "beam select: choosing Beam 3 requests beam=3 and restarts the cursor (the first such request has no since)",
            "beam=3" in last and "beam=3" in [x for x in fx.log if "beam=3" in x][0] and "since=" not in [x for x in fx.log if "beam=3" in x][0],
            last,
        )
        setsel(br, "sp-beam", "loudest")
        time.sleep(2.5)
        last = fx.log[-1]
        check(
            "beam select 'Loudest': the client picks beam 8 (the loudest compare levels) and requests beam=8; positive twin - a fixed choice stays (Beam 3 above did not become 8)",
            "beam=8" in last,
            last,
        )
        setsel(br, "sp-beam", "mean")
        setsel(br, "sp-band", "0")
        time.sleep(1.2)
        bq = [x for x in fx.log if "band=0" in x]
        check("compare band select: choosing 50-125 Hz requests band=0 from scratch", bool(bq) and "since=" not in bq[0] and "beam=0" in bq[0], str(bq[:1]))
        # ---- tier: detail is offered only when the server allows it (this fixture answers 403)
        check(
            "detail tier: hidden when the server answers 403 (positive twin below with a server that allows it)",
            br.eval("Spectrum.last.controls.tierField.hidden") is True,
            "",
        )
        # ---- inactive tab pauses polling
        br.eval("location.hash = '#/overview'")
        time.sleep(1.2)
        n1 = len(fx.log)
        time.sleep(1.6)
        n2 = len(fx.log)
        br.eval("location.hash = '#/system'")
        time.sleep(1.5)
        n3 = len(fx.log)
        check(
            "polling stops while the System tab is not shown (0 requests in 1.6 s) and resumes when it is shown again", n2 == n1 and n3 > n2, str((n1, n2, n3))
        )

        # ---- gating: rows flagged gated are hatched, marked at the left edge, and announced
        if br.eval("Spectrum.last.S.frozen"):
            click(br, "sp-freeze")
        setsel(br, "sp-range", "-100,-20")
        fx.mode["gate_from"] = time.time()
        # freeze once 30 gated rows (3 s of the 10 rows/s stream) are in the store, counted in rows and not in wall time: the newest rows are gated,
        # the oldest still are not (a fixed sleep let a slow runner gate the whole buffer, or too few rows arrive)
        rows_g = wait_for(br, "Spectrum.last.newest && Spectrum.last.newest.gated && Spectrum.last.store.rows.filter(r => r.gated).length >= 30", 30)
        click(br, "sp-freeze")
        br.eval("Spectrum.last.flush()")
        r = br.eval(
            "[document.getElementById('sp-gated').hidden, getComputedStyle(document.getElementById('sp-gated')).display !== 'none', document.getElementById('sp-gated').textContent, document.getElementById('sp-gated').getAttribute('data-state')]"
        )
        check(
            "gated: the 'speech suppressed' indicator is shown as text (not hidden) when the newest row is gated",
            rows_g and r == [False, True, "speech suppressed", "gated"],
            str(r),
        )
        bad = br.eval(
            "(() => { const m = getComputedStyle(document.documentElement).getPropertyValue('--bad').trim(); const e = document.createElement('i'); e.style.color = m; document.body.append(e); const c = getComputedStyle(e).color; e.remove(); const x = /(\\d+)[ ,]+(\\d+)[ ,]+(\\d+)/.exec(c); return [+x[1], +x[2], +x[3]]; })()"
        )
        fgc = br.eval(
            "(() => { const x = /(\\d+)[ ,]+(\\d+)[ ,]+(\\d+)/.exec(getComputedStyle(document.getElementById('spectrum-panel')).color); return [+x[1], +x[2], +x[3]]; })()"
        )
        top_row = [px(br, "waterfall", ML + c, MT + 14) for c in range(0, 16)]
        gated_cols = [c for c in range(16) if top_row[c] == [44, 114, 142]]
        hatch_cols = [c for c in range(16) if top_row[c] == fgc]
        marker = px(br, "waterfall", ML - 5, MT + 14)
        check(
            "gated waterfall rows: coarse values (-70 dB = viridis(96) %s) with a hatch in the text colour on every 8th pixel (%d hatch px in 16 columns), and a marker strip in the left margin in the --bad colour %s"
            % (VIRIDIS[96], len(hatch_cols), bad),
            len(hatch_cols) == 2 and len(gated_cols) == 14 and marker == bad,
            str((top_row, marker, bad, fgc)),
        )
        old_row = px(br, "waterfall", ML + 5, MT + 190)
        check(
            "positive twin: the oldest (non-gated) rows at the bottom of the waterfall have no hatch and no marker",
            old_row in (VIRIDIS[48], VIRIDIS[128]) and px(br, "waterfall", ML - 5, MT + 190) != bad,
            str(old_row),
        )
        r = br.eval("document.querySelector('.sp-summary').textContent")
        check(
            "gated: the summary reports the seconds of suppressed speech (more than 0)",
            re.search(r"Speech suppressed [1-9][\d.]* s of the last 30 s\.$", r) is not None,
            r,
        )
        fx.mode["gate_from"] = None

        # ---- stale, no data, disabled, rate limit
        click(br, "sp-freeze")
        fx.mode["freeze_at"] = time.time() - 4.0
        wait_for(br, "document.getElementById('sp-state').getAttribute('data-state') === 'stale'", 8)
        r = br.eval("[document.getElementById('sp-state').textContent, document.getElementById('sp-state').getAttribute('data-state')]")
        check("stale: the newest row older than 3 s gives the 'stale' pill (text, not colour only)", r == ["stale", "stale"], str(r))
        fx.mode["freeze_at"] = None
        wait_for(br, "document.getElementById('sp-state').getAttribute('data-state') === 'live'", 8)
        check(
            "stale: positive twin - fresh rows bring 'live' back",
            br.eval("document.getElementById('sp-state').textContent") == "live",
            br.eval("document.getElementById('sp-state').textContent"),
        )
        fx.mode["rate_limit"] = True
        wait_for(br, "document.getElementById('sp-state').getAttribute('data-state') === 'unavailable'", 8)
        n1 = len(fx.log)
        time.sleep(2.6)
        n2 = len(fx.log)
        r = br.eval("[document.getElementById('sp-state').textContent, document.querySelector('.sp-info').textContent.endsWith('rate limited')]")
        check(
            "429: the pill says 'unavailable' and the info line 'rate limited', and the client backs off (at most 3 requests in 2.6 s instead of about 5)",
            r == ["unavailable", True] and n2 - n1 <= 3,
            str((r, n2 - n1)),
        )
        fx.mode["rate_limit"] = False
        wait_for(br, "document.getElementById('sp-state').getAttribute('data-state') === 'live'", 8)
        check(
            "429: positive twin - after the limit is lifted the panel is live again",
            br.eval("document.getElementById('sp-state').textContent") == "live",
            br.eval("document.getElementById('sp-state').textContent"),
        )
        fx.mode["state"] = "no_data"
        wait_for(br, "document.getElementById('sp-state').getAttribute('data-state') === 'nodata'", 8)
        check(
            "no data: the pill says 'no data'",
            br.eval("document.getElementById('sp-state').textContent") == "no data",
            br.eval("document.getElementById('sp-state').textContent"),
        )
        fx.mode["state"] = "live"
        wait_for(br, "document.getElementById('sp-state').getAttribute('data-state') === 'live'", 8)
        fx.mode["state"] = "disabled"
        wait_for(br, "document.getElementById('sp-state').getAttribute('data-state') === 'disabled'", 8)
        br.eval("Spectrum.last.flush()")
        r = br.eval("[document.getElementById('sp-state').textContent, document.querySelector('.sp-summary').textContent]")
        check(
            "disabled: the pill says 'not enabled' and the summary says so in words",
            r == ["not enabled", "The live spectrum is not enabled on this server."],
            str(r),
        )
        fx.mode["state"] = "live"
        wait_for(br, "document.getElementById('sp-state').getAttribute('data-state') === 'live'", 8)
        check(
            "auth_required: positive twin before the state is set - live pill and the tooltip button is hidden",
            br.eval("[document.getElementById('sp-state').textContent, document.getElementById('sp-authtip').hidden]") == ["live", True],
        )
        fx.mode["state"] = "auth_required"
        wait_for(br, "document.getElementById('sp-state').getAttribute('data-state') === 'auth_required'", 8)
        br.eval("Spectrum.last.flush()")
        r = br.eval(
            "[document.getElementById('sp-state').textContent, document.getElementById('sp-authtip').hidden, document.querySelector('.sp-summary').textContent]"
        )
        check(
            "auth_required: the pill says 'sign in required' (text), the tooltip button is shown and the summary says so in words",
            r == ["sign in required", False, "Sign in to see the live spectrum."],
            str(r),
        )
        r = br.eval(
            "(() => { const b = document.querySelector('#sp-authtip .tip-btn'); const t = document.getElementById(b.getAttribute('aria-describedby')); return [b.getAttribute('aria-label'), t.getAttribute('role'), t.textContent.includes('signed-in user'), t.textContent.includes('Sign in to the dashboard'), document.getElementById('sp-state').title === t.textContent]; })()"
        )
        check(
            "auth_required: the tooltip button is labelled, its bubble is a role=tooltip that names the sign-in, and the pill carries the same text as its title",
            r == ["About: sign in required", "tooltip", True, True, True],
            str(r),
        )
        n1 = len(fx.log)
        time.sleep(2.6)
        check("auth_required: the client asks rarely (at most 1 request in 2.6 s instead of about 5)", len(fx.log) - n1 <= 1, str(len(fx.log) - n1))
        fx.mode["state"] = "live"
        br.eval("Spectrum.last.flush()")
        wait_for(br, "document.getElementById('sp-state').getAttribute('data-state') === 'live'", 12)
        check(
            "auth_required: positive twin after it - live again, the tooltip button hidden, the pill title gone",
            br.eval(
                "[document.getElementById('sp-state').textContent, document.getElementById('sp-authtip').hidden, document.getElementById('sp-state').hasAttribute('title')]"
            )
            == ["live", True, False],
        )

        # ---- reduced motion: no scrolling frames, a still snapshot refreshed every 2 s
        brr, profr = open_page("reduced", port, reduced=True)
        profs.append(profr)
        brs.append(brr)
        wait_for(brr, "Spectrum.last && Spectrum.last.S.released >= 150")
        time.sleep(2.6)
        setsel(brr, "sp-range", "-100,-20")
        time.sleep(2.4)
        fr = brr.eval("[Spectrum.last.S.reduced, Spectrum.last.S.frames]")
        check(
            "reduced motion: the panel reports reduced motion and has drawn 0 animation frames (no rAF loop) yet rows were released and drawn",
            fr == [True, 0] and brr.eval("Spectrum.last.S.released") >= 150,
            str(fr),
        )
        W = brr.eval("Spectrum.last.canvases.waterfall.width")
        pw = W - ML - MR
        a, b = cols(30, pw)
        pc = (a + b) // 2
        wf = [px(brr, "waterfall", ML + c, MT + 1) for c in (a, pc, b)]
        check("reduced motion: the still snapshot shows the same peak in the same columns (viridis(128))", wf == [VIRIDIS[128]] * 3, str(wf))
        pe = [px(brr, "persistence", ML + pc, MT + 82), px(brr, "persistence", ML + cols(20, pw)[0] + 2, MT + 134)]
        check(
            "reduced motion persistence: the peak row (y 82) and the noise row (y 134) are at the top of the colour map (viridis 253..255) after the snapshot rebuild",
            pe[0] in TOP and pe[1] in TOP,
            str(pe),
        )
        bgc = brr.eval(
            "(() => { const m = /rgba?\\((\\d+)[ ,]+(\\d+)[ ,]+(\\d+)/.exec(getComputedStyle(Spectrum.last.canvases.persistence).backgroundColor); return [+m[1], +m[2], +m[3]]; })()"
        )
        pe2 = [px(brr, "persistence", ML + pc, MT + 40), px(brr, "persistence", ML + cols(29, pw)[0] + 1, MT + 82)]
        check("reduced motion persistence: positive twin - empty above the peak row and in the neighbouring band", pe2 == [bgc, bgc], str((pe2, bgc)))
        fgr = brr.eval(
            "(() => { const x = /(\\d+)[ ,]+(\\d+)[ ,]+(\\d+)/.exec(getComputedStyle(document.getElementById('spectrum-panel')).color); return [+x[1], +x[2], +x[3]]; })()"
        )
        hold_on = px(brr, "persistence", ML + pc, MT + 83)
        click(brr, "sp-hold")
        hold_off = px(brr, "persistence", ML + pc, MT + 83)
        click(brr, "sp-hold")
        click(brr, "sp-reset")
        res = brr.eval("[Spectrum.last.pers.hasPeak, Array.from(Spectrum.last.pers.P).every(v => v === 0)]")
        hold_reset = px(brr, "persistence", ML + pc, MT + 83)
        check(
            "peak hold: the held peak is a line in the text colour at -60 dB (y 83); toggling Peak hold off removes it (positive twin); Reset peak clears the held values and the line",
            hold_on == fgr and hold_off != fgr and res == [False, True] and hold_reset != fgr,
            str((hold_on, hold_off, res, hold_reset, fgr)),
        )
        check(
            "reduced motion: the pulse animation is off (the live pill dot has no animation)",
            brr.eval("getComputedStyle(document.getElementById('sp-state'), '::before').animationName") == "none",
            brr.eval("getComputedStyle(document.getElementById('sp-state'), '::before').animationName"),
        )
        # normal mode twin: the dot animates
        wait_for(
            br, "document.getElementById('sp-state').getAttribute('data-state') === 'live'", 20
        )  # the dot pulses only in the live state; a slow poll can leave it stale
        check(
            "normal motion (positive twin): the live pill dot pulses",
            br.eval("getComputedStyle(document.getElementById('sp-state'), '::before').animationName") == "sp-pulse",
            br.eval("getComputedStyle(document.getElementById('sp-state'), '::before').animationName"),
        )

        # ---- detail tier offered when the server allows it
        fx2 = F.Fixture(detail_ok=True)
        port2 = fx2.start()
        brd, profd = open_page("detail", port2)
        profs.append(profd)
        brs.append(brd)
        wait_for(brd, "Spectrum.last && Spectrum.last.controls.tierField.hidden === false", 8)
        check(
            "detail tier: the 'Resolution' control is offered when the probe request is allowed (positive twin of the hidden state above)",
            brd.eval("Spectrum.last.controls.tierField.hidden") is False,
            "",
        )
        fx2.stop()
        # ---- a phone-width page keeps the panel inside the viewport
        br.resize(360, 900)
        br.eval("location.hash = '#/system'")
        time.sleep(1.2)
        r = br.eval(
            "(() => { const p = document.getElementById('spectrum-panel').getBoundingClientRect(); return [document.documentElement.scrollWidth <= 360, p.right <= 360 + 0.5, Spectrum.last.canvases.waterfall.width <= 360]; })()"
        )
        check("phone width (360 px): no horizontal scrolling and the panel and canvases fit the viewport", r == [True, True, True], str(r))
        shots = sys.argv[sys.argv.index("--shots") + 1] if "--shots" in sys.argv[:-1] else None
        if shots:
            os.makedirs(shots, exist_ok=True)
            import base64

            for name, w, h in (("system-desktop", 1400, 1900), ("system-phone", 390, 2400)):
                br.resize(w, h)
                time.sleep(1.0)
                data = br.ws.call("Page.captureScreenshot", {"format": "png"})["result"]["data"]
                with open(os.path.join(shots, name + ".png"), "wb") as f:
                    f.write(base64.b64decode(data))
    finally:
        for b in brs:
            b.close()
        fx.stop()
        for p in profs:
            shutil.rmtree(p, ignore_errors=True)
    print(f"\n{D.n_checks} checks, {len(D.fails)} failure(s)")
    return 1 if D.fails else 0


if __name__ == "__main__":
    sys.exit(main())
