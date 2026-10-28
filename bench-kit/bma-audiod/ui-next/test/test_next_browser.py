#!/usr/bin/env python3
"""Browser test of the next dashboard served by the REAL bma_ui server (--ui-next, a login, a synthetic pipeline dir, BMAS datagrams over real UDP) through the
legacy adapter: every view (Live, Review, Detections, System) against the existing endpoints, the voice rule, the polled waterfall, the blind review flow with
verdicts landing in the label store, and a screenshot check (dimensions exact, the waterfall region is not blank, the eight views differ).
Synthetic data only; nothing outside a temp dir is read or written. Run from ui-next/test (NOT python -I: the fixture imports its neighbours).

    python test_next_browser.py [--browser chrome|edge] [--shots-dir DIR]          exit 77 (SKIP) when the browser is not installed"""
import base64
import hashlib
import json
import math
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import urllib.request
import wave
import zlib

import browsertest as bt

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
for d in ("ui", "analysis", "contracts"):
    sys.path.insert(0, os.path.join(ROOT, d))
import auth as A  # noqa: E402
import bma_ui as U  # noqa: E402
import ref_binary as RB  # noqa: E402
import test_review as R  # noqa: E402

TOKEN = R.TOKEN
BASIC = R.BASIC
T_TIT = R.NOW - 2200
TONE_HZ, TONE_T0, TONE_T1, FS = 3000.0, 1.0, 3.5, 48000
BIN_VALUE = lambda beam: 40 + 6 * beam                      # every bin of beam b (beam 0 = mean) holds this uint8, except LINE_BIN
LINE_BIN, LINE_ADD = 17, 30
DB = lambda v: -100.0 + 0.5 * v


def clip_id(t, key, beam):
    return "%d-%s-b%d" % (int(t * 1000), key, beam)


def write_wav(path):
    n = int(5 * FS)
    frames = bytearray()
    for i in range(n):
        t = i / FS
        v = 0.5 * math.sin(2 * math.pi * TONE_HZ * t) if TONE_T0 <= t < TONE_T1 else 0.0
        frames += struct.pack("<h", int(v * 32767))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(FS)
        w.writeframes(bytes(frames))


def tree(root):
    """The review test world plus a Tufted Titmouse event, an audio clip for the chickadee and PNG-only clips for the jay and the titmouse."""
    pr = R.make_tree(root)
    with open(os.path.join(pr, "analysis", "birds.jsonl"), "ab") as f:
        f.write((json.dumps(R.brow(T_TIT, "Tufted Titmouse", "Baeolophus bicolor", 0.55, 2), separators=(",", ":")) + "\n").encode())
    with open(os.path.join(pr, "analysis", "tags.jsonl"), "ab") as f:
        f.write((json.dumps(R.trow(T_TIT), separators=(",", ":")) + "\n").encode())
    recs = []
    for t, key, sci, com, beam, audio in ((R.T_CHICK, "poecilecarolinensis", "Poecile carolinensis", "Carolina Chickadee", 1, True),
                                          (R.T_JAY, "cyanocittacristata", "Cyanocitta cristata", "Blue Jay", 3, False),
                                          (T_TIT, "baeolophusbicolor", "Baeolophus bicolor", "Tufted Titmouse", 2, False)):
        cid, day = clip_id(t - 1, key, beam), time.strftime("%Y%m%d", time.gmtime(t - 1))
        cd = os.path.join(pr, "analysis", "clips", day)
        os.makedirs(cd, exist_ok=True)
        with open(os.path.join(cd, cid + ".png"), "wb") as f:
            f.write(R.PNG)
        rec = {"k": "clip_stored", "v": 1, "id": cid, "t_start": t - 1, "t_end": t + 4, "species": {"scientific": sci, "common": com}, "group": "bird", "confidence_max": 0.7,
               "beam": beam, "png": day + "/" + cid + ".png"}
        if audio:
            write_wav(os.path.join(cd, cid + ".wav"))
            rec["audio_file"] = day + "/" + cid + ".wav"
        recs.append(rec)
    R.write_jsonl(os.path.join(pr, "analysis", "clips", "index.jsonl"), recs)
    return pr


class World:
    """The real server in this process: login, review store, an enabled spectrum feed fed over UDP with rows at 10 per second."""

    def __init__(self):
        self.rig = R.Rig(auth=True, tree=tree)
        self.rig.srv.shutdown()
        self.rig.srv.server_close()
        self.feed = U.SpectrumFeed(enabled=True)
        self.udp = self.feed.start(0)
        app = self.rig.app
        app.spectrum = self.feed
        app.start_backfill()                                 # main() does this at start: the event list is read from the pipeline files
        self.srv = U.Server(("127.0.0.1", 0), app, set(), U.load_assets(), auth=A.Authenticator(TOKEN, log=lambda m: None), next_assets=U.load_next_assets())
        self.port = self.srv.server_address[1]
        self.srv.allowed_hosts = U.allowed_hosts_for(self.port, [])
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.gate = False
        self.stop = threading.Event()
        threading.Thread(target=self._feeder, daemon=True).start()

    def _feeder(self):
        import socket
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        k = 0
        t0 = time.time()
        while not self.stop.is_set():
            now = time.time()
            for beam in range(9):
                bins = [BIN_VALUE(beam)] * 46
                if not self.gate:                                    # a gated row must be coarse (equal inside each band group, ui-http spectrum contract), so it carries no line
                    bins[LINE_BIN] += LINE_ADD
                tx.sendto(RB.build_spectrum(0, 1 if self.gate else 0, beam, k, int(now * 1e6), bins), ("127.0.0.1", self.udp))
            k += 1
            time.sleep(max(0.0, t0 + k * 0.1 - time.time()))
        tx.close()

    def close(self):
        self.stop.set()
        self.srv.shutdown()
        self.srv.server_close()
        self.feed.close()
        shutil.rmtree(self.rig.root, ignore_errors=True)

    def api(self, path):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        c.request("GET", path, headers=dict({"Host": "localhost:%d" % self.port}, **BASIC))
        r = c.getresponse()
        body = json.loads(r.read())
        c.close()
        return body

    def labels(self):
        return self.rig.lines()


class Fx(bt.Fixture):
    def __enter__(self):
        if not self.exe:
            print("SKIP: %s not found; nothing ran, this is not a pass" % self.args.browser)
            sys.exit(77)
        self.world = World()
        self.base = "http://127.0.0.1:%d" % self.world.port
        self.br = bt.cdp.Browser(exe=self.exe)
        return self

    def __exit__(self, *exc):
        self.br.close()
        self.world.close()


def png_info(path):
    """(width, height, rows of RGB tuples decoded) of an 8-bit PNG written by Chromium (colour types 2 and 6, all five filters)."""
    with open(path, "rb") as f:
        d = f.read()
    w, h, depth, ctype = struct.unpack(">IIBB", d[16:26])
    assert depth == 8 and ctype in (2, 6), (depth, ctype)
    i, idat = 8, b""
    while i < len(d):
        n, typ = struct.unpack(">I4s", d[i:i + 8])
        if typ == b"IDAT":
            idat += d[i + 8:i + 8 + n]
        i += 12 + n
    raw, bpp = zlib.decompress(idat), 3 if ctype == 2 else 4
    stride, rows, prev = w * bpp, [], bytearray(w * bpp)
    for y in range(h):
        ft, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if ft == 1:
                line[x] = (line[x] + a) & 255
            elif ft == 2:
                line[x] = (line[x] + b) & 255
            elif ft == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif ft == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(line)
        prev = line
    return w, h, rows, bpp


def colours(info, x0, y0, x1, y1):
    w, h, rows, bpp = info
    return len({bytes(rows[y][x * bpp:x * bpp + 3]) for y in range(max(0, y0), min(h, y1)) for x in range(max(0, x0), min(w, x1))})


def main():
    ap = bt.parser(__doc__.split("\n")[0])
    ap.add_argument("--shots-dir", default=None, help="keep the screenshots here (default: a temp dir that is removed)")
    args = ap.parse_args()
    with Fx(args) as fx:
        shots = args.shots_dir or tempfile.mkdtemp(prefix="uinext_shots_")
        os.makedirs(shots, exist_ok=True)
        try:
            live_tests(fx)
            poll_tests(fx)
            fallback_tests(fx)
            detections_tests(fx)
            system_tests(fx)
            review_tests(fx)
            shot_tests(fx, shots)
            fx.check("no script error or console error during the whole run", not fx.errors(), "; ".join(fx.errors())[:300])
        finally:
            if not args.shots_dir:
                shutil.rmtree(shots, ignore_errors=True)
        return fx.finish()


def login(fx):
    fx.go("/?token=" + TOKEN)                                # the cookie the whole run uses; the old dashboard page is irrelevant here
    fx.br.ws.pump(0.5)


def live_tests(fx):
    """The default: rows over the live WebSocket (ui-live 1.0.0), all nine beams, one tile each."""
    login(fx)
    fx.go("/next/#live")
    ready = fx.wait("window.__ui && __ui.src.kind === 'legacy' && __ui.src.caps.push === true && document.getElementById('state').textContent === 'live' && __ui.rend && __ui.rend.layers.length === 10 "
                    "&& [1,2,3,4,5,6,7,8].every(b => __ui.rend.rings[b].filled >= 30)", 40)
    info = fx.ev("({kind: __ui.src.kind, chips: document.querySelectorAll('#beams button').length, layers: __ui.rend.layers.length, mode: __ui.S.liveMode, "
                 "listenHidden: document.getElementById('listen').hidden, filled: [0,1,2,3,4,5,6,7,8].map(b => __ui.rend.rings[b].filled >= 30)})")
    fx.check("socket boot under /next/: mode ws, state live, 9 chips, 10 layers (9 per-beam strips and the big tile), every one of the 9 beam rings holds 30 or more columns, Listen hidden",
             ready and (info["kind"], info["chips"], info["layers"], info["mode"], info["listenHidden"], info["filled"]) == ("legacy", 9, 10, "ws", True, [True] * 9), json.dumps(info))
    rows = fx.ev("[0,1,2,3,4,5,6,7,8].map(function(b){var r=__ui.rend.rings[b], h=r.head, a=r.raw.subarray(h*46,(h+1)*46); return [a[5], a[17]];})")
    fx.check("socket data: the newest column of every beam holds that beam's exact sent levels (bin 5 and the line at bin 17, beams 0 to 8)",
             rows == [[DB(BIN_VALUE(b)), DB(BIN_VALUE(b) + LINE_ADD)] for b in range(9)], json.dumps(rows))
    net, sock = fx.br.net(), fx.br.ws_bytes()
    polled = [u for u in net if u.startswith("/api/spectrum")]
    fx.check("transport: one WebSocket carries the rows (frames arrived, a multiple of 438 bytes each), /api/spectrum is never polled, no /api/v2 HTTP request", sock[0] >= 30 and not polled and not [u for u in net if u.startswith("/api/v2")], json.dumps([sock, polled]))
    fx.key("3")
    fx.check("key 3: beam 3 shows at once (its ring was already full), the mean ring is not cleared and nothing was re-requested", fx.wait("__ui.S.beam === 3 && __ui.rend.layers[__ui.rend.layers.length - 1].ring === 3", 10)
             and fx.ev("__ui.rend.rings[0].filled") >= 30 and not [u for u in fx.br.net() if u.startswith("/api/spectrum")])
    BARS = "[...document.querySelectorAll('#beams button .bar')].map(b => parseFloat(b.style.transform.replace('scaleX(','')))"
    fx.wait("(function(){var b=%s; return b.every(function(v, i){ return i == 0 || b[i-1] < v; });})()" % BARS, 15)
    bars = fx.ev(BARS)
    fx.check("chips: the nine level bars rise strictly with beam number (the mean is the quietest: its bins are the lowest sent)", all(bars[i] < bars[i + 1] for i in range(8)), json.dumps(bars))
    fx.key("0")
    fx.check("gate: no 'speech suppressed' pill while rows are not gated", fx.ev("document.getElementById('gate').hidden") is True)
    fx.world.gate = True
    shown = fx.wait("!document.getElementById('gate').hidden", 15)
    fx.check("gate: frames flagged gated (bit 0) show the pill (positive twin of the check above)", shown and fx.ev("document.getElementById('gate').textContent") == "speech suppressed")
    fx.world.gate = False
    fx.check("gate: the pill goes away when the gating ends", fx.wait("document.getElementById('gate').hidden", 15))
    fx.check("the server counts one live socket for this page", fx.world.srv.ws_gate.n == 1, str(fx.world.srv.ws_gate.n))


def poll_tests(fx):
    """?ws=0: the page polls /api/spectrum (one beam at a time, no strips): the phase 1 behaviour, kept as the fallback."""
    fx.go("/next/?ws=0#live")
    ready = fx.wait("window.__ui && __ui.src.kind === 'legacy' && document.getElementById('state').textContent === 'live' && __ui.rend && __ui.rend.rings[0].filled >= 30", 40)
    fx.wait("performance.getEntriesByType('resource').filter(e => e.name.includes('/api/spectrum')).length >= 4", 30)
    info = fx.ev("({kind: __ui.src.kind, chips: document.querySelectorAll('#beams button').length, layers: __ui.rend.layers.length, "
                 "listenHidden: document.getElementById('listen').hidden, note: document.getElementById('listen-note').textContent, filled: __ui.rend.rings[0].filled})")
    fx.check("?ws=0 boot: legacy source, state live, 9 beam chips, one tile (no per-beam strips), Listen hidden with its reason shown",
             ready and (info["kind"], info["chips"], info["layers"], info["listenHidden"], info["note"]) == ("legacy", 9, 1, True, "Live listening is not available on this server."), json.dumps(info))
    row = fx.ev("(function(){var r=__ui.rend.rings[0], h=r.head, a=r.raw.subarray(h*46,(h+1)*46); return [a[5], a[17], a[18]];})()")
    fx.check("live data: the newest column of the mean ring holds the sent levels exactly (bin 5 %.1f dB, the line at bin 17 %.1f dB, its neighbour %.1f dB)" % (DB(40), DB(40 + LINE_ADD), DB(40)),
             row == [DB(40), DB(40 + LINE_ADD), DB(40)], json.dumps(row))
    net = fx.br.net()
    sock = fx.br.ws_bytes()
    fx.check("transport: /api/spectrum is polled (several requests), no WebSocket frames, no /api/v2 request", net.get("/api/spectrum", [0])[0] >= 3 and sock == [0, 0] and not [u for u in net if u.startswith("/api/v2")], json.dumps([net.get("/api/spectrum"), sock, [u for u in net if "v2" in u]]))
    fx.key("3")
    ok = fx.wait("__ui.S.beam === 3 && __ui.rend.rings[3].filled >= 10 && __ui.rend.rings[0].filled === 0", 15)
    row3 = fx.ev("(function(){var r=__ui.rend.rings[3], h=r.head, a=r.raw.subarray(h*46,(h+1)*46); return [a[5], a[17]];})()")
    fx.check("key 3: beam 3 is polled from the start of the ring, the mean ring was cleared (no stale mix), the newest column of beam 3 holds its own exact levels (%.1f, %.1f dB)" % (DB(BIN_VALUE(3)), DB(BIN_VALUE(3) + LINE_ADD)),
             ok and row3 == [DB(BIN_VALUE(3)), DB(BIN_VALUE(3) + LINE_ADD)], json.dumps(row3))
    BARS = "[...document.querySelectorAll('#beams button .bar')].map(b => parseFloat(b.style.transform.replace('scaleX(','')))"
    fx.wait("(function(){var b=%s; return b[1] < b[0] && b[0] < b[8];})()" % BARS, 15)             # bars move on the next painted frame
    bars = fx.ev(BARS)
    fx.check("chips: the eight beam level bars rise with beam number (the sent levels do), the mean bar sits between beam 1 and beam 8",
             all(bars[i] < bars[i + 1] for i in range(1, 8)) and bars[1] < bars[0] < bars[8], json.dumps(bars))
    fx.key("0")
    fx.wait("__ui.S.beam === 0 && __ui.rend.rings[0].filled >= 10", 15)
    fx.check("gate: no 'speech suppressed' pill while the rows are not gated", fx.ev("document.getElementById('gate').hidden") is True)
    fx.world.gate = True
    shown = fx.wait("!document.getElementById('gate').hidden", 15)
    fx.check("gate: rows flagged gated show the 'speech suppressed' pill (positive twin of the check above)", shown and fx.ev("document.getElementById('gate').textContent") == "speech suppressed")
    fx.world.gate = False
    fx.check("gate: the pill goes away when the gating ends", fx.wait("document.getElementById('gate').hidden", 15))
    fx.key("l")
    fx.check("key L does nothing without live listening (no consent dialog opens)", fx.ev("document.getElementById('consent').open") is False)


def fallback_tests(fx):
    """The socket is refused (the connection cap reports 503): the page falls back to polling by itself."""
    fx.world.srv.ws_gate.limit = 0
    try:
        fx.go("/next/#live")
        ok = fx.wait("window.__ui && __ui.S.liveMode === 'poll' && document.getElementById('state').textContent === 'live' && __ui.rend.rings[0].filled >= 20", 40)
        fx.wait("performance.getEntriesByType('resource').filter(e => e.name.includes('/api/spectrum')).length >= 4", 30)
        info = fx.ev("({layers: __ui.rend.layers.length, tiles: __ui.src.caps.tiles, push: __ui.src.caps.push})")
        net = fx.br.net()
        fx.check("socket refused (503 too_many_connections): the page polls /api/spectrum by itself, shows one tile and the live state", ok and info == {"layers": 1, "tiles": False, "push": False} and net.get("/api/spectrum", [0])[0] >= 3, json.dumps([info, net.get("/api/spectrum")]))
    finally:
        fx.world.srv.ws_gate.limit = 8


def detections_tests(fx):
    fx.nav("det")
    fx.wait("document.querySelectorAll('#v-det tbody tr').length >= 3", 15)
    rows = fx.ev("[...document.querySelectorAll('#v-det tbody tr')].map(r => r.children[1].textContent).sort()")
    want = sorted(["Blue Jay", "Carolina Chickadee", "Northern Cardinal", "Tufted Titmouse", "White-breasted Nuthatch"])
    fx.check("detections: the table lists exactly the five visible species (blue jay, chickadee, cardinal, titmouse, nuthatch)", rows == want, json.dumps(rows))
    text = fx.ev("document.getElementById('v-det').textContent")
    api = fx.world.api("/api/detections/events?since=%d&limit=200" % int(time.time() - 86400))
    fx.check("voice rule: no human voice label anywhere in the view, and the server list holds the same five rows (the hidden one never reaches the page)",
             "Human" not in text and "human" not in text and sorted(e["species"] for e in api["events"]) == want, json.dumps(sorted(e["species"] for e in api["events"])))
    fx.check("detections: no row offers 'Open' (these events carry no clip list entry; positive twin lives in test_browser.py where clips exist)", fx.ev("document.querySelectorAll('#v-det tbody button').length") == 0)
    fx.ev("document.querySelector('#v-det .chip').click()")
    one = fx.ev("[...document.querySelectorAll('#v-det tbody tr')].length")
    fx.check("detections: a species chip filters the table to one row", one == 1, str(one))


def system_tests(fx):
    fx.nav("sys")
    fx.wait("document.querySelector('#v-sys .kv')", 15)
    kv = fx.ev("Object.fromEntries([...document.querySelectorAll('#v-sys .kv > div')].map(d => [d.children[0].textContent, d.children[1].textContent]))")
    last_hour = fx.world.api("/api/detections/events?since=%d&limit=1" % int(time.time() - 3600))["total"]
    fx.check("system: figures the server does not have read n/a (never 0); the board is off; detections in the last hour equal the server count (%d: jay, chickadee, cardinal, titmouse, nuthatch; the human one is hidden)" % last_hour,
             kv == {"uptime": "n/a", "receive loss": "n/a", "board": "off · n/a", "clips kept, 24 h": "n/a", "detections, 1 h": "5"} and last_hour == 5, json.dumps([kv, last_hour]))
    strip = fx.ev("document.getElementById('strip').textContent")
    fx.check("status strip: loss and board read n/a, no NaN or undefined text anywhere in the strip or the system view", strip == "loss n/a" + "board n/a" and "NaN" not in fx.ev("document.getElementById('v-sys').textContent")
             and "undefined" not in fx.ev("document.getElementById('v-sys').textContent"), strip)


def review_tests(fx):
    first = fx.world.api("/api/review/next")
    fx.nav("review")
    fx.wait("window.__lab && __lab.S.fr && __lab.S.clip && !__lab.S.busy", 40)
    it = first["item"]
    span = it["clip_span"]
    tabs = fx.ev("[...document.querySelectorAll('#v-review #tabs button')].map(b => [b.dataset.tab, b.hidden])")
    fx.check("review: the Clips tab is hidden (the server has no clip list), the Queue tab stays", tabs == [["clips", True], ["review", False]], json.dumps(tabs))
    fx.check("review: the first item is the audio clip (audio before PNG-only), the clip id is the item's, the queue badge equals the server depth (%d)" % first["queue_depth"],
             fx.ev("__lab.S.review[0].id") == it["event_id"] and it["clip_audio"].endswith(clip_id(R.T_CHICK - 1, "poecilecarolinensis", 1)) and fx.ev("document.getElementById('rv-count').textContent") == str(first["queue_depth"]) and first["queue_depth"] == 3,
             json.dumps([it["event_id"], first["queue_depth"], fx.ev("document.getElementById('rv-count').textContent")]))
    hid = fx.ev("document.querySelector('#v-review #list li small').textContent")
    fx.check("review: the model claim is hidden in the list while blind", "model claim hidden" in hid and "Carolina Chickadee" not in hid, hid)
    box = fx.ev("({t0: __lab.S.box.t0, t1: __lab.S.box.t1, f0: __lab.S.box.f0, f1: __lab.S.box.f1.toFixed(3), loop: __lab.S.loopOn})")
    t0, t1 = span["event_start_s"] - span["clip_start_s"], span["event_end_s"] - span["clip_start_s"]
    fx.check("review: the box is the event span inside the clip (%.3f to %.3f s) over 90 Hz to 17050 Hz and the loop is on" % (t0, t1),
             abs(box["t0"] - t0) < 1e-6 and abs(box["t1"] - t1) < 1e-6 and box["f0"] == 90 and box["f1"] == "17050.000" and box["loop"] is True, json.dumps(box))
    peak = json.loads(fx.ev("""(function(){ var S=__lab.S, r=S.fr, f=Math.round((2.0-r.t0)/r.hopS), best=-1e9, bk=0; for (var k=0;k<r.nB;k++){ var v=r.val[f*r.nB+k]; if (v>best){best=v;bk=k;} }
      return JSON.stringify({hz: r.binF ? r.binF[bk] : bk*48000/(2*(r.nB-1)), nF: r.nF}); })()"""))
    fx.check("review: the clip's real audio is decoded and analysed: the 3000 Hz tone peaks at %.0f Hz at 2.0 s (tolerance 130)" % peak["hz"], abs(peak["hz"] - TONE_HZ) <= 130 and peak["nF"] > 400, json.dumps(peak))
    beams = fx.ev("[...document.querySelectorAll('#v-review #rbeams button[data-b]')].map(b => b.disabled)")
    avail = fx.ev("[...__lab.S.avail]")
    fx.check("review: mono audio offers its own beam only: beam %d is the one enabled chip, the other seven are disabled" % it["best_beam"],
             beams.count(False) == 1 and beams.index(False) == it["best_beam"] - 1 and avail == [it["best_beam"] - 1], json.dumps([beams, avail]))
    fx.key("5")
    fx.check("review: key 5 (a beam without a signal) changes nothing (positive twin: the clip's own beam is still the selection)", fx.ev("[...__lab.S.sel]") == [it["best_beam"] - 1])
    # verdict 1: correct, by key, on the audio item
    fx.key("y")
    fx.wait("__lab.S.review.length === 1 && __lab.S.review[0].id !== %s && !__lab.S.busy" % json.dumps(it["event_id"]), 20)
    lines = fx.world.labels()
    fx.check("review: key Y appended exactly one label (verdict correct, event id of the item, basis clip_visual, source ui) to the real store",
             [(l.get("verdict"), l.get("event_id"), l.get("basis"), l.get("source")) for l in lines] == [("correct", it["event_id"], "clip_visual", "ui")], json.dumps(lines)[:300])
    nxt = fx.ev("({id: __lab.S.review[0].id, png: !document.getElementById('clip-png').hidden, src: document.getElementById('clip-png').getAttribute('src'), stage: getComputedStyle(document.getElementById('stage').parentElement).display, "
                "badge: document.getElementById('rv-count').textContent, rev: !document.getElementById('rev').hidden})")
    clip_png = fx.world.api("/api/review/next")["item"]["clip_png"]
    fx.check("review: the next item is PNG-only: the stored picture shows (the server URL), the spectrogram stage is hidden, verdict buttons stay, the badge reads the new depth 2",
             nxt["png"] and nxt["src"] == clip_png and nxt["stage"] == "none" and nxt["rev"] and nxt["badge"] == "2" and nxt["id"] != it["event_id"], json.dumps([nxt, clip_png]))
    # verdict 2: wrong with the default reason, by key
    second = nxt["id"]
    fx.key("n")
    fx.wait("__lab.S.review.length === 1 && __lab.S.review[0].id !== %s && !__lab.S.busy" % json.dumps(second), 20)
    lines = fx.world.labels()
    fx.check("review: key N appended a wrong verdict with the reason picked in the page (non_bird_sound), after the first label, nothing else",
             [(l.get("verdict"), l.get("event_id"), l.get("wrong_reason")) for l in lines] == [("correct", it["event_id"], None), ("wrong", second, "non_bird_sound")], json.dumps(lines)[:400])
    third = fx.ev("__lab.S.review[0].id")
    # skip: no label
    fx.key("s")
    fx.wait("__lab.S.review.length === 0 && !__lab.S.busy", 20)
    lines2 = fx.world.labels()
    empty = fx.ev("({claim: document.getElementById('claim').textContent, png: document.getElementById('clip-png').hidden, badge: document.getElementById('rv-count').textContent})")
    fx.check("review: key S skips the third item without writing a label, the queue is empty: 'Nothing left to review.', no picture, empty badge",
             len(lines2) == 2 and third not in [l.get("event_id") for l in lines2] and empty == {"claim": "Nothing left to review.", "png": True, "badge": ""}, json.dumps([empty, len(lines2)]))
    s = 0
    try:
        req = urllib.request.Request(fx.base + "/api/review/verdict", data=b"{}", method="POST")
        urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as e:
        s = e.code
    fx.check("review: the server still refuses a verdict without a login (401): the page's writes rely on the session, not on an open door", s == 401, str(s))


def shot_tests(fx, shots):
    paths, overflow, rects = {}, {}, {}
    for view in ("live", "review", "det", "sys"):
        for w, h, mob in ((1280, 800, False), (390, 844, True)):
            fx.go("/next/#" + view, w, h, mobile=mob)
            fx.wait("window.__ui && __ui.rend && __ui.rend.rings[0].filled >= 30", 40)
            fx.br.ws.pump(1.0)
            p = os.path.join(shots, "%s_%d.png" % (view, w))
            if view == "live":
                rects[w] = fx.rect("#wf")                      # measured in the same page state as the picture
            fx.br.shot(p)
            paths[(view, w)] = p
            overflow[(view, w)] = fx.ev("document.documentElement.scrollWidth <= innerWidth")
    infos = {k: png_info(p) for k, p in paths.items()}
    fx.check("screenshots: all eight are exactly the viewport size (1280x800, 390x844)", {k: (v[0], v[1]) for k, v in infos.items()} == {k: ((1280, 800) if k[1] == 1280 else (390, 844)) for k in paths},
             json.dumps({str(k): (v[0], v[1]) for k, v in infos.items()}))
    digests = {k: hashlib.sha256(open(p, "rb").read()).hexdigest() for k, p in paths.items()}
    fx.check("screenshots: the eight views are eight different pictures", len(set(digests.values())) == 8, str(len(set(digests.values()))))
    fx.check("screenshots: no view overflows horizontally at 390 or 1280 px", all(overflow.values()), json.dumps({str(k): v for k, v in overflow.items() if not v}))
    for w in (1280, 390):
        rc, info = rects[w], infos[("live", w)]
        n = colours(info, int(rc["x"]) + 2, int(rc["y"]) + 2, int(rc["x"] + rc["w"]) - 2, int(rc["y"] + rc["h"]) - 2)
        fx.check("screenshot: the %d px waterfall region shows a drawn spectrogram (at least 8 distinct colours)" % w, n >= 8, "%d colours in %s" % (n, json.dumps(rc)))
    fx.check("screenshot: the page background corner is one flat colour (positive twin: the colour count measures drawing, not noise)", colours(infos[("live", 1280)], 0, 0, 4, 4) == 1)


if __name__ == "__main__":
    sys.exit(main())
