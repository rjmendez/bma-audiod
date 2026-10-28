#!/usr/bin/env python3
"""Browser test of the dashboard (headless Edge or Chrome over DevTools, stdlib only) against the mock server: boot, live data, renderer fallback and
context loss, the clip viewer's linked views and transforms against the synthetic truth, the blind review flow, consent-gated listening with Freeze, a phone pass
and the engine self-test. Prints PASS/FAIL/SKIP lines and 'N checks, M failure(s), K skipped'.

    python test_browser.py [--browser chrome|edge] [--require-webgl]          exit 77 (SKIP) when the browser is not installed"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

import browsertest as bt

BIRD_PEAK_HZ = 5000                      # scene-a: the first sweep runs 6000 -> 3500 Hz over 0.4-0.65 s, so it is at 5000 Hz at t = 0.5 s
BAND_TOL = {"mel": 350, "erb": 350, "bark": 350, "pcen": 350}                # band views are coarser than the STFT bins
PEAK_JS = """(function(tf){ var s=document.querySelector('#v-review #tf'); s.value=tf; s.onchange(); var S=__lab.S, r=S.fr, f=Math.round((0.5-r.t0)/r.hopS), best=-1e9, bk=0;
  for (var k=0;k<r.nB;k++){ var v=r.val[f*r.nB+k]; if (v>best){best=v;bk=k;} }
  return JSON.stringify({peakHz: r.binF ? r.binF[bk] : bk*48000/(2*(r.nB-1)), nF: r.nF, nB: r.nB, kind: S.wf.kind}); })("%s")"""


def main():
    args = bt.parser(__doc__.split("\n")[0]).parse_args()
    with bt.Fixture(args) as fx:
        live_tests(fx, args)
        viewer_tests(fx)
        review_tests(fx)
        listen_tests(fx)
        phone_tests(fx)
        selftest(fx, args)
        fx.check("no script error or console error during the whole run", not fx.errors(), "; ".join(fx.errors())[:300])
        return fx.finish()


def live_tests(fx, args):
    fx.go("/#live")
    ready = fx.wait("window.__ui && __ui.S.clips.length && document.getElementById('state').textContent === 'live' && __ui.rend.rings[1].filled >= 599", 30)
    info = fx.ev("({kind: __ui.renderer, state: document.getElementById('state').textContent, beams: document.querySelectorAll('#beams button').length, clips: __ui.S.clips.length, "
                 "tests: __ui.S.clips.filter(c=>c.test).length, filled: __ui.rend.rings[1].filled, layers: __ui.rend.layers.length, big: __ui.rend.layers[__ui.rend.layers.length-1].ring})")
    fx.check("boot: live state, 9 beam chips, 30 clips (6 test signals + 24 detections), the 599-frame backlog is in the ring, 10 layers (9 tiles + the big one)",
             ready and info["state"] == "live" and info["beams"] == 9 and info["clips"] == 30 and info["tests"] == 6 and info["filled"] >= 599 and info["layers"] == 10 and info["big"] == 0, json.dumps(info))
    webgl_ok = info.get("kind") == "webgl"
    if args.require_webgl:
        fx.check("renderer is WebGL2", webgl_ok, json.dumps(info))
    elif not webgl_ok:
        fx.skip("WebGL checks", "this browser run has no WebGL2 (renderer %s)" % info.get("kind"))
    # the synthetic row has lines at bands 5, 17 and 35 (+14 dB over the floor): the newest column of beam 1 must show them
    row = fx.ev("(function(){var r=__ui.rend.rings[1], h=r.head, a=r.raw.subarray(h*46,(h+1)*46); return [a[5]-a[6], a[17]-a[18], a[35]-a[36], a[10]-a[11]];})()")
    fx.check("live data: the three spectral lines stand 8 dB or more above their neighbour band, an ordinary band pair differs by under 6 dB", all(v >= 8 for v in row[:3]) and abs(row[3]) < 6, json.dumps(row))
    fx.key("3")
    sel = fx.ev("({beam: __ui.S.beam, ring: __ui.rend.layers[__ui.rend.layers.length-1].ring, checked: document.querySelectorAll('#beams button')[3].getAttribute('aria-checked')})")
    fx.check("key 3: beam 3 is chosen, the big tile shows ring 3 and its chip is checked", sel == {"beam": 3, "ring": 3, "checked": "true"}, json.dumps(sel))
    fx.key("0")
    # time selection by pointer, and an interrupted gesture leaves nothing stuck
    r = fx.rect("#wf")
    x0, x1, y = r["x"] + r["w"] * 0.5, r["x"] + r["w"] * 0.8, r["y"] + r["h"] * 0.25
    fx.mouse("mousePressed", x0, y); fx.mouse("mouseMoved", (x0 + x1) / 2, y); fx.mouse("mouseMoved", x1, y)
    shown = fx.ev("!document.getElementById('wf-sel').hidden")
    fx.mouse("mouseReleased", x1, y)
    txt = fx.ev("document.getElementById('inspect').textContent")
    fx.check("time selection: the box shows while dragging and the result names the selected seconds", shown is True and " s selected" in txt, txt)
    fx.mouse("mousePressed", x0, y); fx.mouse("mouseMoved", x1, y)
    fx.ev("document.getElementById('wf').dispatchEvent(new PointerEvent('pointercancel', {pointerId: 1, bubbles: true}))")
    fx.check("time selection: an interrupted gesture (pointercancel) clears the box and the next move starts nothing", fx.ev("document.getElementById('wf-sel').hidden") is True)
    fx.mouse("mouseReleased", x1, y)
    # renderer fallback
    fx.go("/?r=canvas2d#live")
    ok = fx.wait("window.__ui && __ui.rend && __ui.rend.rings[1].filled >= 599", 30)
    fx.check("?r=canvas2d: the Canvas2D back end is used and holds the same 599-frame backlog", ok and fx.ev("__ui.renderer") == "canvas" and fx.ev("__ui.rend.layers.length") == 10, str(fx.ev("__ui.renderer")))
    if webgl_ok:
        fx.go("/#live")
        fx.wait("window.__ui && __ui.rend && __ui.rend.rings[1].filled >= 599", 30)
        fx.ev("__ui.rend.gl.getExtension('WEBGL_lose_context').loseContext()")
        fell = fx.wait("__ui.renderer === 'canvas'", 6)
        fx.check("a lost WebGL context: the dashboard switches to Canvas2D and the history is replayed into it (ring still holds 599 or more columns)", fell and fx.ev("__ui.rend.rings[1].filled") >= 599, str(fx.ev("__ui.renderer")))
        fx.go("/#live")
        fx.wait("window.__ui && __ui.rend && __ui.rend.rings[1].filled >= 599", 30)


def viewer_tests(fx):
    fx.go("/#review")
    fx.check("review view boots the clip viewer on the first clip (scene-a) and renders its analysis", fx.wait("window.__lab && __lab.S.fr && __lab.S.clip && __lab.S.clip.id === 'scene-a'", 30)
             and fx.ev("__lab.S.fr.nF") > 500, str(fx.ev("window.__lab && __lab.S.clip && __lab.S.clip.id")))
    for tf in ("stft", "mt", "reas", "cqt", "morlet", "mel", "erb", "bark", "pcen", "bgn", "harm", "perc"):
        r = json.loads(fx.ev(PEAK_JS % tf))
        if tf in ("harm", "perc"):
            fx.check("view %s renders %d x %d without error" % (tf, r["nF"], r["nB"]), r["nF"] > 500, json.dumps(r))
        else:
            tol = BAND_TOL.get(tf, 130)
            fx.check("view %s: the 5000 Hz point of the 6000-3500 Hz sweep peaks at %.0f Hz (tolerance %d)" % (tf, r["peakHz"], tol), abs(r["peakHz"] - BIRD_PEAK_HZ) <= tol, json.dumps(r))
    fx.ev("(function(){var s=document.querySelector('#v-review #tf'); s.value='stft'; s.onchange();})()")
    # linked views: a box sets the band-pass, L the loop, Escape clears both
    r = fx.rect("#v-review #ov")
    x0, x1, y0, y1 = r["x"] + r["w"] * 0.30, r["x"] + r["w"] * 0.45, r["y"] + r["h"] * 0.30, r["y"] + r["h"] * 0.55
    fx.mouse("mousePressed", x0, y0); fx.mouse("mouseMoved", (x0 + x1) / 2, (y0 + y1) / 2); fx.mouse("mouseMoved", x1, y1); fx.mouse("mouseReleased", x1, y1)
    b = fx.ev("({box: __lab.S.box, band: __lab.S.aud.cfg.band})")
    fx.check("box drag: a time-frequency box is set and the audition band-pass equals its frequency edges exactly",
             b["box"] and b["box"]["t1"] > b["box"]["t0"] and b["box"]["f1"] > b["box"]["f0"] and b["band"] == [b["box"]["f0"], b["box"]["f1"]], json.dumps(b))
    fx.key("l")
    lp = fx.ev("({loop: __lab.S.aud.loop, box: __lab.S.box})")
    fx.check("key L: the loop of the audition engine equals the box time span", lp["loop"] == [lp["box"]["t0"], lp["box"]["t1"]], json.dumps(lp))
    fx.key("Escape")
    fx.check("key Escape clears the box and the band-pass (positive twin of the drag)", fx.ev("__lab.S.box === null && __lab.S.aud.cfg.band === null"))
    fx.key("3")
    fx.check("key 3: beam 3 is soloed in the audition weights", fx.ev("__lab.S.aud.cfg.weights") == [0, 0, 1, 0, 0, 0, 0, 0], str(fx.ev("__lab.S.aud.cfg.weights")))
    fx.key("m")
    w8 = fx.ev("__lab.S.aud.cfg.weights")
    fx.check("key M: all 8 beams are mixed with weight 1/8 each", all(abs(v - 0.125) < 1e-9 for v in w8) and len(w8) == 8, json.dumps(w8))
    fx.ev("__lab.S.a = 0; __lab.S.b = 4; document.querySelector('#rbeams button[data-b=\"0\"]').click();")
    fx.key("Tab")
    fx.check("Tab: A/B switches from beam 1 (A) to beam 5 (B)", fx.ev("[...__lab.S.sel]") == [4], str(fx.ev("[...__lab.S.sel]")))
    # touch behaviour follows the mode: Select owns both axes, Pan leaves vertical drags to the page
    fx.ev("document.querySelector('#v-review #mode').click()")
    pan = fx.ev("document.getElementById('stage').style.touchAction")
    fx.ev("document.querySelector('#v-review #mode').click()")
    sel = fx.ev("document.getElementById('stage').style.touchAction")
    fx.check("stage touch-action: Pan leaves vertical drags to the page (pan-y), Select owns the gesture (none)", (pan, sel) == ("pan-y", "none"), json.dumps([pan, sel]))


def review_tests(fx):
    fx.go("/#review")
    fx.wait("window.__lab && __lab.S.fr", 30)
    before = fx.ev("__ui.viewer.pending()")
    fx.ev("document.querySelector('#v-review #tabs button[data-tab=review]').click()")
    fx.wait("__lab.S.rvi >= 0 && !__lab.S.busy && __lab.S.box && __lab.S.clip.id === __lab.S.review[__lab.S.rvi].clip", 15)
    hid = fx.ev("document.querySelector('#v-review #list li small').textContent")
    species = ("Alpha Wren", "Pine Finch", "Brook Thrush", "Reed Warbler", "Zed Owl", "Marsh Pipit")
    fx.check("review: the model claim is hidden in the list while blind", "model claim hidden" in hid and not any(s in hid for s in species), hid)
    fx.key("y")
    fx.br.ws.pump(0.5)
    rec = fx.ev("document.getElementById('rec').textContent")
    try:
        doc = json.loads(rec)
    except ValueError:
        doc = None
    r = (doc or {}).get("record", {})
    fx.check("review: key Y writes a verdict record (k verdict, v 1, verdict correct, basis clip_visual, source ui, provenance owner_verdict)",
             (r.get("k"), r.get("v"), r.get("verdict"), r.get("basis"), r.get("source"), r.get("provenance")) == ("verdict", 1, "correct", "clip_visual", "ui", "owner_verdict"), rec[:200])
    ev = (doc or {}).get("proposed_evidence", {})
    fx.check("review: the proposed evidence carries the views used and the box in whole milliseconds and hertz", isinstance(ev.get("views"), list) and ev.get("box") is not None
             and all(isinstance(ev["box"].get(k), int) for k in ("t0_ms", "t1_ms", "f0_hz", "f1_hz")), json.dumps(ev)[:200])
    try:
        sys.path.insert(0, os.path.join(bt.UI, "..", "analysis"))
        import labels
    except ImportError as e:
        fx.skip("record validated by analysis/labels.py", "cannot import it here: %s" % e)
    else:
        try:
            labels.validate_record(r)
            why = ""
        except labels.LabelError as e:
            why = str(e)
        fx.check("review: the record validates against analysis/labels.validate_record (contract verdict-labels 1.1.0)", why == "", why)
    after = fx.ev("__ui.viewer.pending()")
    fx.check("review: the pending count fell by one and the nav badge shows it (%s -> %s)" % (before, after), after == before - 1 and fx.ev("document.getElementById('rv-count').textContent") == str(after), str(fx.ev("document.getElementById('rv-count').textContent")))
    fx.check("review: the claim is revealed after the verdict", "model claimed:" in fx.ev("document.getElementById('claim').textContent"))
    sent = bt.mock_server.VERDICTS
    fx.check("review: the mock server received the verdict for the item (in memory only)", len(sent) >= 1 and "correct" in sent.values(), json.dumps(sent))


def listen_tests(fx):
    fx.go("/#live")
    fx.wait("window.__ui && __ui.rend && __ui.S.clips.length", 30)
    nav_before = fx.ev("document.title")
    fx.ev("document.getElementById('listen').click()")
    fx.wait("document.getElementById('consent').open", 5)
    off = fx.ev("document.getElementById('consent-go').disabled")
    fx.ev("document.getElementById('consent-ok').click()")
    on = fx.ev("document.getElementById('consent-go').disabled")
    fx.check("listen: the consent dialog opens with the start button off until the box is ticked (off %s, then %s)" % (off, on), off is True and on is False)
    fx.ev("document.getElementById('consent-go').click()")
    started = fx.wait("__ui.S.listening && __ui.listen.ws && __ui.listen.ws.readyState === 1", 8)
    ind = fx.ev("(function(){var i=document.getElementById('listen-ind'); return {hidden: i.hidden, text: i.textContent, title: document.title};})()")
    fx.check("listen: a session starts with a permanent indicator (text and tab title start with a bullet and LISTENING)", started and ind["hidden"] is False and ind["text"].startswith("● LISTENING") and ind["title"].startswith("● LISTENING"), json.dumps(ind))
    fx.check("listen: the server counts one listener", bt.mock_server.LISTENERS[0] == 1, str(bt.mock_server.LISTENERS[0]))
    tok = json.loads(urllib.request.urlopen(urllib.request.Request(fx.base + "/api/v2/listen/consent", data=b"{}", method="POST"), timeout=5).read())["token"]
    try:
        urllib.request.urlopen(fx.base + "/api/v2/listen?token=" + tok, timeout=5)
        code = 200
    except urllib.error.HTTPError as e:
        code = e.code
    fx.check("listen: a second listener is refused while one is connected (409)", code == 409, str(code))
    try:
        urllib.request.urlopen(fx.base + "/api/v2/listen?token=not-a-token", timeout=5)
        code = 200
    except urllib.error.HTTPError as e:
        code = e.code
    fx.check("listen: a request without a live consent token is refused (403)", code == 403, str(code))
    fx.wait("__ui.listen.aud.live && __ui.listen.aud.live.stats && __ui.listen.aud.live.stats.fill > 0", 6)      # the worklet reports once a second
    state = fx.ev("(function(){var a=__ui.listen.aud; return {ctx: a.ctx && a.ctx.state, fill: a.live && a.live.stats && a.live.stats.fill, listening: a.listening};})()")
    if state["ctx"] == "running":
        fx.check("listen: audio reaches the live ring (fill %s frames) and the chain is running" % state["fill"], state["listening"] is True and (state["fill"] or 0) > 0, json.dumps(state))
    else:
        fx.skip("audio reaches the live ring", "AudioContext is %s in this browser run (autoplay policy)" % state["ctx"])
    fx.ev("document.getElementById('freeze').click()")
    got = fx.wait("window.__lab && __lab.S.clip && __lab.S.clip.id === 'live-freeze' && __lab.S.fr", 15)
    frz = fx.ev("({view: __ui.S.view, n: __lab.S.beams[0].length, ch: __lab.S.beams.length, listening: __ui.S.listening})")
    fx.check("freeze: the last 6 s of 8 beams (288000 samples each) open in the viewer, listening has stopped", got and frz["view"] == "review" and frz["n"] == 288000 and frz["ch"] == 8 and frz["listening"] is None, json.dumps(frz))
    gone = fx.wait("document.getElementById('listen-ind').hidden", 3)
    end = time.time() + 5
    while bt.mock_server.LISTENERS[0] and time.time() < end:
        time.sleep(0.2)
    fx.check("freeze: the indicator is gone, the title is restored and the server counts no listener", gone and fx.ev("document.title") == nav_before and bt.mock_server.LISTENERS[0] == 0, str(bt.mock_server.LISTENERS[0]))


def phone_tests(fx):
    fx.go("/#live", 390, 844, mobile=True)
    fx.wait("window.__ui && __ui.rend && __ui.rend.rings[1].filled >= 599", 30)
    info = fx.ev("({layers: __ui.rend.layers.length, w: innerWidth, sw: document.documentElement.scrollWidth, coarse: matchMedia('(pointer: coarse)').matches})")
    fx.check("phone: the live view uses one tile and does not overflow horizontally (390 px)", info["layers"] == 1 and info["sw"] <= info["w"], json.dumps(info))
    fx.nav("review")
    fx.wait("window.__lab && __lab.S.fr", 30)
    ta = fx.ev("document.getElementById('stage').style.touchAction")
    fx.check("phone: the viewer stage starts in Pan (pan-y) when the pointer is coarse and in Select (none) otherwise", ta == ("pan-y" if info["coarse"] else "none"), "%s coarse=%s" % (ta, info["coarse"]))
    fx.check("phone: the clip list folds away so the picture comes first", fx.ev("document.getElementById('queue').open") is False)


def selftest(fx, args):
    name = args.browser + "-test"
    fx.go("/selftest.html?b=" + name)
    done = fx.wait("document.title === 'selftest done'", 120)
    res = bt.mock_server.SELFTESTS.get(name)
    fx.check("engine self-test page finished and posted its result", done and res is not None, str(res)[:100])
    if not res:
        return
    s = res["summary"]
    bad = [c["id"] + ": " + c.get("detail", "") for c in res["checks"] if not c["ok"] and not c.get("skip")]
    fx.check("engine self-test: no failed check (%d passed, %d skipped)" % (s["passed"], s["skipped"]), not bad, "; ".join(bad)[:400])
    names = {c["id"]: c for c in res["checks"]}
    for need in ("parity.layers", "webgl.context_restore", "stream.ws"):
        c = names.get(need)
        fx.check("engine self-test ran %s" % need, c is not None and (c["ok"] or c.get("skip")), json.dumps(c)[:200])


if __name__ == "__main__":
    sys.exit(main())
