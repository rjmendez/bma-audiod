#!/usr/bin/env python3
"""DOM-level checks of the dashboard in real headless Microsoft Edge (no stubs: the real bma_ui server on 127.0.0.1 over a synthetic
pipeline tree stamped with the current time, the real static assets, `msedge --dump-dom`). Asserts the domain rules the pure-function
tests cannot see: the uncalibrated-angle label, greyed out-of-region rows (hidden by default), non-species left out by default,
the per-beam bias note, the neutral 'disabled' board state with no raw error code, value labels on the bias chart.

    python test_dom_edge.py          (exit 0 and a final 'N checks, 0 failure(s)' line; exits 77 with 'SKIP' if Edge is missing)
bma_ui.py is imported from this directory (the mutation scripts copy the whole directory and run the copy)."""

import base64, json, os, re, shutil, socket, struct, subprocess, sys, tempfile, threading, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = HERE
sys.path.insert(0, UI_DIR)
sys.path.insert(1, HERE)
import bma_ui as U  # noqa: E402
import test_bma_ui as T  # noqa: E402


def find_edge():
    """msedge on PATH, else under %ProgramFiles(x86)%; '' when not installed (the suite then SKIPs)."""
    p = shutil.which("msedge") or shutil.which("msedge.exe")
    if p:
        return p
    base = os.environ.get("ProgramFiles(x86)")
    c = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe") if base else ""
    return c if c and os.path.exists(c) else ""


EDGE = find_edge()
fails, n_checks = [], 0


def check(name, ok, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


class WS:
    """Minimal RFC 6455 client (text frames) for the Edge DevTools protocol: stdlib only."""

    def __init__(self, url):
        host, rest = url[5:].split("/", 1)
        h, port = host.split(":")
        self.s = socket.create_connection((h, int(port)), timeout=60)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall(
            f"GET /{rest} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.s.recv(4096)
        self.rest = buf.split(b"\r\n\r\n", 1)[1]
        self.id = 0

    def _read(self, n):
        while len(self.rest) < n:
            self.rest += self.s.recv(1 << 20)
        out, self.rest = self.rest[:n], self.rest[n:]
        return out

    def call(self, method, params=None):
        self.id += 1
        data = json.dumps({"id": self.id, "method": method, "params": params or {}}).encode()
        n, mask = len(data), os.urandom(4)
        hdr = bytes([0x81]) + (
            bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", n)
        )
        self.s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
        while True:
            msg = self._recv()
            if msg.get("id") == self.id:
                return msg

    def _recv(self):
        payload = b""
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload += self._read(n)
            if b0 & 0x80:
                break
        return json.loads(payload) if payload else {}


def truthy(v):
    """A wait condition holds on a truthy value; the {'exception': ...} dict Browser.eval returns for a page error (for example the script not loaded yet) never counts."""
    return bool(v) and not (isinstance(v, dict) and "exception" in v)


class Browser:
    def __init__(self, prof, port):
        self.prof = prof
        # Started through PowerShell Start-Process: a headless Edge launched with subprocess.Popen from Python was observed
        # (Edge 156, Windows 11) to never open its debugging port, while the same command through Start-Process opens it in ~2 s.
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"Start-Process -FilePath '{EDGE}' -ArgumentList @('--headless=new','--disable-gpu','--remote-debugging-port={port}','--user-data-dir={prof}','about:blank') -WindowStyle Hidden",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        pages = []
        deadline = time.time() + 45
        while time.time() < deadline and not pages:
            try:
                # Edge also opens edge:// pages (sync dialog) and extension pages: take the about:blank page we asked for
                pages = [
                    t
                    for t in json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2))
                    if t.get("type") == "page" and t.get("url", "").startswith("about:blank")
                ]
            except OSError:
                pass
            if not pages:
                time.sleep(0.5)  # never spin: the port can be up before the page exists
        if not pages:
            self.close()
            raise RuntimeError("headless Edge never exposed an about:blank page on port %d" % port)
        self.ws = WS(pages[0]["webSocketDebuggerUrl"])
        self.ws.call("Page.enable")
        self.ws.call("Emulation.setDeviceMetricsOverride", {"width": 1400, "height": 1000, "deviceScaleFactor": 1, "mobile": False})

    def dump(self, url, wait=None):
        """Fresh page state, load url, let the dashboard fetch and render, return the DOM as HTML."""
        wait = (
            float(os.environ.get("BMA_DOM_WAIT", "6.0")) if wait is None else wait
        )  # the mutation runs shorten it: the load below polls for the first panel anyway
        self.ws.call("Page.navigate", {"url": "about:blank"})
        time.sleep(0.5)
        self.ws.call("Page.navigate", {"url": url})
        deadline = time.time() + 30
        time.sleep(wait)
        while time.time() < deadline:  # wait until a tab has rendered at least one panel
            r = self.ws.call("Runtime.evaluate", {"expression": "document.querySelectorAll('.tab:not([hidden]) .panel').length"})["result"]["result"].get(
                "value"
            )
            if r:
                break
            time.sleep(0.5)
        return self.ws.call("Runtime.evaluate", {"expression": "document.documentElement.outerHTML"})["result"]["result"].get("value", "")

    def eval(self, expr):
        r = self.ws.call("Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True})["result"]
        if "exceptionDetails" in r:
            return {"exception": r["exceptionDetails"].get("text"), "detail": str(r["exceptionDetails"].get("exception", {}).get("description", ""))[:200]}
        return r["result"].get("value")

    def resize(self, w, h):
        self.ws.call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": False})

    def close(self):
        # kill only the Edge trees that use THIS test's profile folder (never a browser the user may have open)
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | Where-Object { $_.CommandLine -like '*"
                + self.prof
                + "*' } | ForEach-Object { taskkill /PID $_.ProcessId /T /F | Out-Null }",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def text_of(dom):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", dom))


def tips_of(dom):
    """The texts of the tooltip bubbles in a DOM dump (explanatory prose lives there, not on the page)."""
    import html

    return [html.unescape(m) for m in re.findall(r'<span class="tip-bubble"[^>]*>(.*?)</span>', dom, re.S)]


# Visible prose = a block of 80+ characters of own text that is on the page (not in a tooltip bubble, svg, table, button, canvas, heading or an empty-state box).
# Explanations live behind the info buttons; data lines (counts, pager, values) are shorter than that and stay.
VISIBLE_PROSE_JS = """(() => { const out = []; for (const e of document.querySelectorAll('.tab:not([hidden]) *, #strip *, header *, footer *')) {
  if (e.closest('.tip-bubble, svg, table, button, canvas, select, h1, h2, h3, .empty, noscript')) continue; if (e.getClientRects().length === 0) continue;
  let own = ''; for (const c of e.childNodes) if (c.nodeType === 3) own += c.textContent; own = own.trim(); if (own.length >= 80) out.push(own.slice(0, 70)); } return out; })()"""


def tip_state(br):
    """[number of info buttons, buttons whose aria-describedby resolves to a role=tooltip bubble, bubbles with no text, open bubbles] of the current page"""
    return br.eval("""(() => { const b = Array.from(document.querySelectorAll('.tip-btn')); const ok = b.filter(x => { const t = document.getElementById(x.getAttribute('aria-describedby')); return t && t.getAttribute('role') === 'tooltip' && t.classList.contains('tip-bubble'); });
      return [b.length, ok.length, Array.from(document.querySelectorAll('.tip-bubble')).filter(t => !t.textContent.trim()).length, document.querySelectorAll('.tip-bubble:popover-open').length]; })()""")


def badges_of(dom):
    """The terse visible warning badges (text only, without the tooltip)."""
    return re.findall(r'<span class="tbadge-text">([^<]*)</span>', dom)


def main():
    if not os.path.exists(EDGE):
        print("SKIP msedge not found")
        return 77
    root = tempfile.mkdtemp(prefix="bma_ui_dom_")
    browsers = []
    edge_profiles = []
    try:
        now = time.time()
        pr, rp = os.path.join(root, "pipeline"), os.path.join(root, "reports")
        for d in (os.path.join(pr, "analysis"), os.path.join(pr, "logs"), rp):
            os.makedirs(d)
        recs = []
        for i in range(120, 0, -1):  # two minutes of 1 s windows: one beam hot at 4-8 kHz, beam 1 a bit louder at 2-4 kHz
            recs.append(T.bands_rec(now - i, None, extra={(7, 6): -20.0, (0, 5): -25.0}))
        T.jl(os.path.join(rp, "beam_bands.jsonl"), recs)
        T.jl(
            os.path.join(pr, "analysis", "birds.jsonl"),
            [
                T.bird(now - 300, 2, 0.8, "Alpha Wren", "Aus a"),
                T.bird(now - 299, 3, 0.7, "Alpha Wren", "Aus a"),
                T.bird(now - 200, 6, 0.9, "Zed Owl", "Zus z", filtered=True),
                T.bird(now - 100, 5, 0.6, "Engine", "Engine", non=True),
            ],
        )
        board = U.Board("http://127.0.0.1:9", os.path.join(root, "none"), enabled=False)
        app = U.App(pr, rp, os.path.join(root, "rollups"), board, window_h=48, slot_fix_at=now - 40 * 86400)
        app.start_backfill()
        threading.Thread(target=app.run_loop, daemon=True).start()  # the 1 s poller, as in production
        srv = U.Server(("127.0.0.1", 0), app, set(), U.load_assets())
        port = srv.server_address[1]
        srv.allowed_hosts = U.allowed_hosts_for(port, [])
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        # NOT inside `root` (made by mkdtemp, which gives the folder an owner-only ACL on Windows): Edge relaunches de-elevated
        # when this shell is elevated and then cannot write its profile there, so it never opens its debugging port.
        edge_prof = os.path.join(tempfile.gettempdir(), "bma_ui_dom_edge_%d_%d" % (os.getpid(), int(time.time())))
        edge_profiles.append(edge_prof)
        br = Browser(edge_prof, 9400 + port % 500)
        browsers.append(br)
        dump = lambda url, prof=None: br.dump(url)  # noqa: E731
        base = f"http://127.0.0.1:{port}/"
        prof = None

        ov = dump(base + "#/overview", prof)
        tov = text_of(ov)
        check("overview: the page rendered (strip + tab present)", 'id="tab-overview"' in ov and "Last 6 h: most recent detections" in tov, tov[:200])
        vp = br.eval(VISIBLE_PROSE_JS)
        check("overview: no prose paragraph is visible by default (every explanation is behind an info button)", vp == [], str(vp))
        ts = tip_state(br)
        check(
            "overview: info buttons exist, each wired by aria-describedby to a role=tooltip bubble with text, and none is open",
            isinstance(ts, list) and ts[0] >= 8 and ts[0] == ts[1] and ts[2] == 0 and ts[3] == 0,
            str(ts),
        )
        check(
            "overview: the frame is a terse visible badge 'Uncalibrated frame', its long text only in the tooltip",
            badges_of(ov).count("Uncalibrated frame") == 1 and "Uncalibrated frame" not in "".join(t for t in tips_of(ov)),
        )
        check("overview: the compass states the angles are 'array frame (uncalibrated)'", "Angles: array frame (uncalibrated)" in tov)
        check(
            "overview: the compass says 0 degrees is the array's axis, not north (in the tooltip of the frame badge)",
            any("Uncalibrated: 0° is the array's own axis, not north." in t for t in tips_of(ov)),
        )
        check(
            "overview: the 5-second question - Alpha Wren, with its best beam and azimuth, is in the last-6-h table",
            "Alpha Wren" in tov and "B2 45°" in tov,
            tov[:300],
        )
        check(
            "overview: out-of-region species and non-species are NOT shown by default (no Zed Owl, no Engine table cell)",
            "Zed Owl" not in tov and "<td>Engine" not in ov,
        )
        check(
            "overview: the band selector starts on 4-8 kHz (the band where the beams differ most)",
            re.search(r'aria-pressed="true"[^>]*>4-8 kHz<', ov) is not None,
        )
        check(
            "overview: with the board polling disabled the card says 'disabled', not 'unreachable', and no raw code is shown",
            "Board disabled" in tov and "unreachable" not in tov and "board_disabled" not in tov,
            re.findall(r"Board \w+", tov)[:2].__str__(),
        )

        de = dump(base + "#/detections", prof)
        tde = text_of(de)
        vp = br.eval(VISIBLE_PROSE_JS)
        check("detections: no prose paragraph is visible by default", vp == [], str(vp))
        ts = tip_state(br)
        check(
            "detections: info buttons wired to tooltip bubbles, none open",
            isinstance(ts, list) and ts[0] >= 4 and ts[0] == ts[1] and ts[2] == 0 and ts[3] == 0,
            str(ts),
        )
        check(
            "detections (default): Alpha Wren listed, Zed Owl and Engine absent from the tables",
            "Alpha Wren" in tde and "Zed Owl" not in tde and "<td>Engine" not in de,
        )
        do = dump(base + "#/detections?out=1", prof)
        ev_tbl = do.split("Best beam", 1)[1].split("</table>", 1)[0]  # the EVENTS table only (the species summary has its own greyed rows)
        check(
            "detections (out-of-region shown): in the events table Zed Owl is a greyed row (class 'out') marked '(out of region)'",
            re.search(r'<tr class="click out[^"]*"[^>]*>(?:(?!</tr>).)*Zed Owl \(out of region\)', ev_tbl, re.S) is not None,
        )
        check(
            "detections (out-of-region shown): in the events table the in-region row is NOT greyed (positive twin)",
            re.search(r'<tr class="click"[^>]*>(?:(?!</tr>).)*Alpha Wren', ev_tbl, re.S) is not None,
        )
        sum_tbl = do.split("Events by beam 1-8", 1)[1].split("</table>", 1)[0]
        check(
            "detections (out-of-region shown): the species summary greys it too",
            re.search(r'<tr class="click out[^"]*"[^>]*>(?:(?!</tr>).)*Zed Owl \(out of region\)', sum_tbl, re.S) is not None,
        )
        dn = dump(base + "#/detections?nonsp=1", prof)
        check("detections (non-species shown): Engine appears marked '(non-species)'", "Engine (non-species)" in text_of(dn))
        check(
            "detections: the in-region Alpha Wren row says best beam B2 45° and lists beams B2 B3 (grouped across beams)",
            "B2 45°" in tde and "B2 B3" in tde,
            tde[:400],
        )

        bm = dump(base + "#/beams", prof)
        tbm = text_of(bm)
        vp = br.eval(VISIBLE_PROSE_JS)
        check("beams: no prose paragraph is visible by default", vp == [], str(vp))
        ts = tip_state(br)
        check(
            "beams: info buttons wired to tooltip bubbles, none open",
            isinstance(ts, list) and ts[0] >= 5 and ts[0] == ts[1] and ts[2] == 0 and ts[3] == 0,
            str(ts),
        )
        check(
            "beams: the bias note says a persistent per-beam offset is a channel gain difference, not a source (read from the tooltip)",
            any(
                "A persistent offset that stays with one beam number when the pattern is rotated is a channel gain difference, not a source." in t
                for t in tips_of(bm)
            ),
        )
        check("beams: the bias chart carries printed value labels (touch cannot hover)", 'class="vlabel"' in bm)
        check("beams: on 4-8 kHz there is no 'show 4-8 kHz' prompt", "show 4-8 kHz" not in tbm)
        check("beams: the data window is stated as whole hours", "Whole hours:" in tbm)
        check(
            "beams: the heatmap legend says the colour scale is auto-fitted (canvas text is not in the DOM, so the label is checked in the source)",
            "colour scale auto-fitted to this view" in open(os.path.join(UI_DIR, "static", "app.js"), encoding="utf-8").read(),
        )

        sy = dump(base + "#/system", prof)
        tsy = text_of(sy)
        vp = br.eval(VISIBLE_PROSE_JS)
        check("system: no prose paragraph is visible by default", vp == [], str(vp))
        ts = tip_state(br)
        check(
            "system: info buttons wired to tooltip bubbles, none open",
            isinstance(ts, list) and ts[0] >= 3 and ts[0] == ts[1] and ts[2] == 0 and ts[3] == 0,
            str(ts),
        )
        check(
            "system: 'Board polling is disabled.' is shown and the raw code board_disabled is not",
            "Board polling is disabled." in tsy and "board_disabled" not in tsy,
        )
        check(
            "system: the loss note says 1-2 % is a known steady condition (read from the tooltip)",
            any("Loss of 1-2 % is a known steady condition" in t for t in tips_of(sy)),
        )
        # ------------------------------------------------------------------ panel calibration, physical directions, slot-mapping flag, scatter
        PANEL = [75, 30, 345, 300, 255, 210, 165, 120]
        fake = T.FakeBoard()
        fake.calibration = {"beam_azimuth_deg": PANEL, "measured_at": "2026-10-25T17:47:00Z"}
        tok = os.path.join(root, "token")
        open(tok, "w").write(T.TOKEN + "\n")
        cal_board = U.Board(f"http://127.0.0.1:{fake.port}", tok, timeout=3.0)
        cal_board.refresh(["info", "state", "monitor", "health", "network", "dsps", "calibration"], now=time.time())

        def serve(brd, fix):
            a = U.App(pr, rp, os.path.join(root, "rollups_%d" % int(fix)), brd, window_h=48, slot_fix_at=fix)
            a.start_backfill()
            threading.Thread(target=a.run_loop, daemon=True).start()
            s = U.Server(("127.0.0.1", 0), a, set(), U.load_assets())
            s.allowed_hosts = U.allowed_hosts_for(s.server_address[1], [])
            threading.Thread(target=s.serve_forever, daemon=True).start()
            return s, f"http://127.0.0.1:{s.server_address[1]}/"

        def fresh_bands():
            t = time.time()
            T.jl(os.path.join(rp, "beam_bands.jsonl"), [T.bands_rec(t - i, None, extra={(7, 6): -20.0, (0, 5): -25.0}) for i in range(60, 0, -1)])
            time.sleep(2.5)  # the server polls once a second

        srv_b, base_b = serve(cal_board, int(now - 250))  # calibrated, fix inside every range: Alpha Wren (now-300) is before it, Zed Owl (now-200) after
        srv_c, base_c = serve(cal_board, int(now - 40 * 86400))  # calibrated, fix long before every range
        dirs = lambda dom: re.findall(r'data-dir="(\w+)"', dom)  # noqa: E731
        lanes = lambda dom: re.findall(r'<text class="lane"[^>]*>([^<]*)</text>', dom)  # noqa: E731
        dots = lambda dom: sorted(re.findall(r'class="ev-dot" data-species="([^"]+)" data-shape="(\w+)"', dom))  # noqa: E731

        fresh_bands()
        ovb = dump(base_b + "#/overview", prof)
        tovb = text_of(ovb)
        check(
            "panel overview: the four physical directions are marked UP, RIGHT, DOWN, LEFT on the compass",
            dirs(ovb) == ["UP", "RIGHT", "DOWN", "LEFT"],
            str(dirs(ovb)),
        )
        check("panel overview: uncalibrated twin (first server) has NO direction marks", dirs(ov) == [], str(dirs(ov)))
        check(
            "panel overview: the frame is named 'calibrated panel frame' and the uncalibrated warning is gone",
            "Angles: calibrated panel frame 2026-10-25T17:47:00Z" in tovb and "Uncalibrated:" not in tovb,
        )
        check(
            "panel overview: the compass aria-label states panel frame, clockwise from the front, 0 = up",
            'aria-label="Beam compass, calibrated panel frame 2026-10-25T17:47:00Z, panel frame, clockwise seen from the front, 0 degrees is panel up"' in ovb,
        )
        check(
            "panel overview: the caption says provisional, not a compass bearing, facing north 90 = west side",
            "provisional. 0° = panel up, angles increase clockwise. These are not compass bearings: with the panel facing north, 90° is the west side and 270° the east side."
            in tovb,
        )
        check(
            "panel overview: the Beam pattern card says unknown when dspd reports none (no 'vendor pattern 105')",
            "dspd reports no applied pattern" in tovb and "vendor pattern" not in tovb,
        )
        check("panel overview: the recent-detections table uses the stored azimuth (Alpha Wren best beam 2 -> B2 30°)", "B2 30°" in tovb, tovb[:300])
        check(
            "slot flag: the hourly chart and the activity-by-group chart each have one hatched region (2) and the note naming the boundary and the 45 degree error",
            ovb.count('class="hatch-region"') == 2
            and badges_of(ovb).count("Unreliable region") == 2
            and sum(t.startswith("Before ") for t in tips_of(ovb)) == 2
            and re.search(
                r" local \(\d\d:\d\d UTC on \d+/\d\d\) the beam slots were mapped 45° off: beam numbers and azimuths in the hatched region are unreliable\.$",
                [t for t in tips_of(ovb) if t.startswith("Before ")][0],
            )
            is not None,
        )
        check(
            "slot flag: the hatched region carries a <title> with the note (not colour only)",
            re.search(r'class="hatch-region"[^>]*><title>Before [^<]*45° off', ovb) is not None,
        )
        check(
            "slot flag: the hatch pattern is defined in the same svg",
            re.search(r'<pattern id="(hatch\d+)"', ovb) is not None and re.search(r'class="hatch-region"[^>]* fill="url\(#hatch\d+\)"', ovb) is not None,
        )
        ovc = dump(base_c + "#/overview", prof)
        check(
            "slot flag: positive twin - a fix long before the range gives no hatched region and no note",
            ovc.count('class="hatch-region"') == 0 and "Unreliable region" not in text_of(ovc),
        )
        check("slot flag: first server (fix long ago) also shows none", ov.count('class="hatch-region"') == 0 and "Unreliable region" not in tov)

        bmb = dump(base_b + "#/beams", prof)
        tbmb = text_of(bmb)
        check(
            "slot flag: the beams tab shows the note once under the beam heatmap and once under the direction strip (the same data, two views), and each canvas carries it in its aria-label",
            badges_of(bmb).count("Unreliable region") == 2
            and sum(t.startswith("Before ") for t in tips_of(bmb)) == 2
            and re.search(r'<canvas[^>]*aria-label="beam, azimuth \(panel frame\)\. Before [^"]*45° off', bmb) is not None
            and re.search(
                r'<canvas[^>]*aria-label="Direction strip, beams ordered by azimuth, level relative to the ring mean, 4-8 kHz\. Before [^"]*45° off', bmb
            )
            is not None,
        )
        check(
            "panel beams: contrast table lists the stored azimuths 75° and 345°", "75°" in tbmb and "345°" in tbmb and "Angles: calibrated panel frame" in tbmb
        )
        check("panel beams: compass directions present here too", dirs(bmb) == ["UP", "RIGHT", "DOWN", "LEFT"], str(dirs(bmb)))
        bmc = dump(base_c + "#/beams", prof)
        check(
            "slot flag: positive twin - no heatmap note when the fix is before the range",
            "Unreliable region" not in text_of(bmc) and "beam, azimuth (panel frame)" in bmc,
        )
        bma = dump(base + "#/beams", prof)
        check("uncalibrated beams: the heatmap axis names the DSP-frame fallback", 'aria-label="beam, azimuth (DSP-frame fallback)"' in bma)

        dsb = dump(base_b + "#/detections?out=1", prof)
        tdsb = text_of(dsb)
        check(
            "scatter: exactly one marker per event (Alpha Wren, Zed Owl) with distinct shapes in count-then-name order",
            dots(dsb) == [("Alpha Wren", "circle"), ("Zed Owl", "square")],
            str(dots(dsb)),
        )
        check(
            "scatter: the geometry follows the species shape (circle for Alpha Wren, square drawn as a rect for Zed Owl), not only the data attribute",
            sorted(re.findall(r'<(\w+) class="ev-dot" data-species="([^"]+)"', dsb)) == [("circle", "Alpha Wren"), ("rect", "Zed Owl")],
            str(re.findall(r'<(\w+) class="ev-dot" data-species="([^"]+)"', dsb)),
        )
        check(
            "scatter: aria-label states the exact event count",
            'aria-label="Detections scatter: 2 events, time by beam direction, 2 species groups, dot size is confidence"' in dsb,
        )
        check(
            "scatter: lanes are ordered by azimuth, labelled with the stored azimuths and the physical directions",
            lanes(dsb) == ["B2 30°", "B1 75° (right)", "B8 120°", "B7 165° (down)", "B6 210°", "B5 255° (left)", "B4 300°", "B3 345° (up)"],
            str(lanes(dsb)),
        )
        check("scatter: legend lists each species with its count", "Alpha Wren (1)" in tdsb and "Zed Owl (1)" in tdsb)
        check(
            "scatter: tooltip of the pre-fix event says beam, stored azimuth and 'slot mapping unreliable'",
            "Alpha Wren conf 0.80 beam B2 30° (slot mapping unreliable)" in dsb,
            "",
        )
        check(
            "scatter: the post-fix out-of-region event has no unreliable flag and says out of region", "Zed Owl conf 0.90 beam B6 210° (out of region)<" in dsb
        )
        check(
            "scatter: only the out-of-region marker is faded with a dashed outline (positive twin: the in-region one is not)",
            dsb.count('fill-opacity="0.35"') == 1
            and dsb.count('stroke-dasharray="2 2"') == 1
            and dsb.count('class="ev-dot"') == 2
            and re.search(r'data-species="Alpha Wren"[^>]*fill-opacity="0.85"', dsb) is not None,
        )
        check(
            "scatter: the pre-fix hatched region and the note are on the scatter too",
            dsb.count('class="hatch-region"') >= 1
            and badges_of(dsb).count("Unreliable region") >= 1
            and sum(t.startswith("Before ") for t in tips_of(dsb)) >= 1,
        )
        dsc = dump(base_c + "#/detections?out=1", prof)
        check(
            "scatter: positive twin - fix before the range: same markers, no hatch, no unreliable tooltip",
            dots(dsc) == [("Alpha Wren", "circle"), ("Zed Owl", "square")] and dsc.count('class="hatch-region"') == 0 and "slot mapping unreliable" not in dsc,
        )
        dsa = dump(base + "#/detections?out=1", prof)
        check(
            "scatter: uncalibrated lanes carry the DSP-frame fallback azimuths and no direction words",
            lanes(dsa) == ["B3 0°", "B2 45°", "B1 90°", "B8 135°", "B7 180°", "B6 225°", "B5 270°", "B4 315°"],
            str(lanes(dsa)),
        )
        srv_b.shutdown()
        srv_c.shutdown()
        fake.close()

        # ------------------------------------------------------------------ interaction (real page, real clicks via the page's own handlers)
        birds_path = os.path.join(pr, "analysis", "birds.jsonl")
        T.jl(birds_path, [T.bird(now - 3600 + 10 * i, 1 + i % 8, 0.7, "Page Pipit", "Anthus paginus") for i in range(60)])
        time.sleep(2.5)  # the server polls once a second
        br.dump(base + "#/detections")
        br.eval(
            """(() => { const i = document.querySelector('input[aria-label="minimum confidence"]'); i.focus(); i.value = "0.55"; window.__inp = i; return true; })()"""
        )
        T.jl(birds_path, [T.bird(now - 20, 4, 0.9, "Gamma Gull", "Larus gamma")])  # a NEW species arrives while the user is typing
        time.sleep(3.0)
        r = br.eval("""(async () => {
          await refreshDetections();
          const j = document.querySelector('input[aria-label="minimum confidence"]');
          return [window.__inp === j, document.activeElement === window.__inp, j.value, document.querySelectorAll('select[aria-label="species"] option').length];
        })()""")
        check(
            "filter bar: a refresh that brings a NEW species does not rebuild the bar under a focused input (same node, still focused, typed value kept, list not yet updated)",
            isinstance(r, list) and r[:3] == [True, True, "0.55"] and r[3] == 3,
            str(r),
        )
        r = br.eval("""(async () => {
          document.activeElement.blur(); await refreshDetections();
          return Array.from(document.querySelectorAll('select[aria-label="species"] option')).map(o => o.textContent);
        })()""")
        check(
            "filter bar: positive twin - once nothing is focused the refresh DOES rebuild and the new species appears in the list",
            isinstance(r, list) and any(x.startswith("Gamma Gull") for x in r),
            str(r),
        )
        r = br.eval("""(async () => {
          const rows = () => Array.from(document.querySelectorAll('#tab-detections .tablewrap tbody tr')).slice(0, 3).map(t => t.textContent);
          S.det.offset = 0; S.det.anchor = null; await refreshDetections();
          const first = rows();
          const older = Array.from(document.querySelectorAll('#tab-detections button')).find(b => b.textContent === 'Older'); older.click();
          await new Promise(r => setTimeout(r, 1500));
          const page2 = rows(), anchor = S.det.anchor, off = S.det.offset;
          return [first.length, off, anchor !== null, page2.length, page2.join('|')];
        })()""")
        check("paging: Older moves to offset 50 and freezes an anchor time", isinstance(r, list) and r[1] == 50 and r[2] is True and r[3] == 3, str(r))
        page2_before = r[4] if isinstance(r, list) else None
        T.jl(birds_path, [T.bird(time.time(), 2, 0.95, "Delta Dipper", "Cinclus delta")])  # newer than the frozen anchor
        time.sleep(2.5)
        r = br.eval("""(async () => {
          await refreshDetections();
          const rows = Array.from(document.querySelectorAll('#tab-detections .tablewrap tbody tr')).slice(0, 3).map(t => t.textContent);
          return [rows.join('|'), document.querySelector('#tab-detections .bar:last-of-type, #tab-detections .panel .bar .muted') ? true : true];
        })()""")
        check(
            "paging: a new event arriving while on page 2 does NOT shift the rows on screen",
            isinstance(r, list) and r[0] == page2_before,
            f"{page2_before!r} vs {r!r}",
        )
        r = br.eval("""(async () => {
          const newer = Array.from(document.querySelectorAll('#tab-detections button')).find(b => b.textContent === 'Newer'); newer.click();
          await new Promise(r => setTimeout(r, 1500));
          const first = document.querySelector('#tab-detections .tablewrap tbody tr').textContent;
          return [S.det.offset, S.det.anchor, first.includes('Delta Dipper')];
        })()""")
        check("paging: positive twin - back on page 1 the anchor is released and the new event is the first row", r == [0, None, True], str(r))
        br.resize(1400, 420)
        br.dump(base + "#/detections")
        r = br.eval("""(async () => {
          window.scrollTo(0, 0);
          const before = DT.tlPanel.getBoundingClientRect().top >= window.innerHeight;
          const detBefore = DT.detail.hidden;
          const rowsAll = Array.from(document.querySelectorAll('#tab-detections .tablewrap tbody tr.click'));
          const row = rowsAll[0]; row.click();
          const other = rowsAll.find(t => t.getAttribute('data-key') !== row.getAttribute('data-key'));
          const marks = [row.getAttribute('aria-selected'), row.classList.contains('sel'), other ? other.getAttribute('aria-selected') : 'no-other-species'];
          await new Promise(r => setTimeout(r, 2500));
          const rect = DT.detail.getBoundingClientRect();
          const sel = Array.from(document.querySelectorAll('#tab-detections tr.click')).filter(t => t.getAttribute('aria-selected') === 'true').length;
          await refreshDetections();
          const evSel = Array.from(document.querySelector('#tab-detections .tablewrap').querySelectorAll('tr.click')).some(t => t.getAttribute('aria-selected') === 'true');
          return [before, rect.top < window.innerHeight && rect.bottom > 0, S.selectedSpecies !== null, DT.tlTitle.textContent.startsWith('Timeline:'), sel > 0, marks, evSel, detBefore, DT.detail.hidden];
        })()""")
        check(
            "click on an event row: the event detail (closed before) opens and is scrolled into view, the timeline below is titled for the species and the clicked row is at once marked selected (and another species' row not)",
            r == [True, True, True, True, True, ["true", True, "false"], True, True, False],
            str(r),
        )
        r = br.eval("""(async () => {
          window.scrollTo(0, 0); await new Promise(r => setTimeout(r, 300));
          const before = DT.tlPanel.getBoundingClientRect().top >= window.innerHeight;
          const row = document.querySelector('#tab-detections .tablewrap tbody tr.click[data-key]:not([data-eid])') || Array.from(DT.summary.querySelectorAll('tbody tr.click'))[0];
          const heading = row.closest('.panel').querySelector('h2').textContent;                  // read before the click: a refresh may replace the row
          row.click(); await new Promise(r => setTimeout(r, 2500));
          const rect = DT.tlPanel.getBoundingClientRect();
          return [before, rect.top < window.innerHeight && rect.bottom > 0, heading];
        })()""")
        check(
            "click on a species summary row: the timeline panel (below the fold before) is scrolled into view (the old behaviour, kept for rows that do not open an event detail)",
            r == [True, True, "Species summary"],
            str(r),
        )
        br.resize(1400, 1000)
        br.close()
        srv.shutdown()
    finally:
        for b in browsers:
            b.close()
        shutil.rmtree(root, ignore_errors=True)
        for p in edge_profiles:
            shutil.rmtree(p, ignore_errors=True)
    print(f"\n{n_checks} checks, {len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
