#!/usr/bin/env python3
"""Measured audit of the CURRENT dashboard (ui/bma_ui.py + ui/static) in headless Edge over a synthetic pipeline tree stamped with the current
time (the repo's own test builders). Never contacts the live dashboard. Output: JSON on stdout (+ screenshots in --shots DIR).
    python audit_current.py [--shots DIR] [--window 21]"""
import argparse, json, os, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
UI = os.path.abspath(os.path.join(HERE, "..", "..", "ui"))
sys.path.insert(0, UI)
sys.path.insert(0, HERE)
import bma_ui as U          # noqa: E402
import test_bma_ui as T     # noqa: E402
import cdp                  # noqa: E402

VIEWS = [(360, 740, True), (390, 844, True), (768, 1024, False), (1280, 900, False), (1920, 1080, False)]
TABS = ["overview", "detections", "beams", "system", "review"]

PROBE = """(() => {
  const vis = e => e.getClientRects().length > 0;
  const q = s => Array.from(document.querySelectorAll(s));
  const act = document.querySelector('.tab:not([hidden])');
  const small = q('button, a, input, select, summary, [role=button]').filter(vis).filter(e => { const r = e.getBoundingClientRect(); return r.width < 44 || r.height < 44; }).length;
  const wide = q('*').filter(vis).filter(e => e.getBoundingClientRect().right > innerWidth + 1).length;
  return { nodes: document.querySelectorAll('*').length, tabNodes: act ? act.querySelectorAll('*').length : 0,
    scrollW: document.documentElement.scrollWidth, innerW: innerWidth, scrollH: document.documentElement.scrollHeight,
    overflowX: document.documentElement.scrollWidth > innerWidth + 1, offscreen: wide, smallTargets: small,
    panels: act ? act.querySelectorAll('.panel').length : 0, canvases: act ? act.querySelectorAll('canvas').length : 0, svgs: act ? act.querySelectorAll('svg').length : 0,
    tables: act ? act.querySelectorAll('table').length : 0, buttons: q('button').filter(vis).length,
    titles: act ? Array.from(act.querySelectorAll('.panel h2, .panel h3, .ptitle')).map(e => e.textContent.trim().replace(/\\s+/g, ' ').slice(0, 50)) : [],
    th: act ? Array.from(act.querySelectorAll('th')).map(e => e.textContent.trim().slice(0, 30)) : [],
    stripCards: q('#strip > *').map(e => e.textContent.trim().replace(/\\s+/g, ' ').slice(0, 60)),
    textChars: act ? act.innerText.length : 0 };
})()"""


def build_tree(root):
    now = time.time()
    pr, rp = os.path.join(root, "pipeline"), os.path.join(root, "reports")
    for d in (os.path.join(pr, "analysis"), os.path.join(pr, "logs"), rp):
        os.makedirs(d)
    recs = [T.bands_rec(now - i, None, extra={(7, 6): -20.0, (0, 5): -25.0}) for i in range(7200, 0, -1)]
    T.jl(os.path.join(rp, "beam_bands.jsonl"), recs)
    species = [("Alpha Wren", "Aus a"), ("Zed Owl", "Zus z"), ("Pine Finch", "Fus p"), ("Brook Thrush", "Tus b"), ("Reed Warbler", "Rus w")]
    birds = []
    for k in range(400):
        c, s = species[k % len(species)]
        birds.append(T.bird(now - 60 * k - 5, 1 + k % 8, 0.4 + (k % 6) / 10.0, c, s, filtered=(k % 11 == 0)))
        birds.append(T.bird(now - 60 * k - 4, 1 + (k + 1) % 8, 0.35 + (k % 5) / 10.0, c, s))
    birds.append(T.bird(now - 100, 5, 0.6, "Engine", "Engine", non=True))
    T.jl(os.path.join(pr, "analysis", "birds.jsonl"), birds)
    board = U.Board("http://127.0.0.1:9", os.path.join(root, "none"), enabled=False)
    app = U.App(pr, rp, os.path.join(root, "rollups"), board, window_h=48, slot_fix_at=now - 40 * 86400)
    app.start_backfill()
    threading.Thread(target=app.run_loop, daemon=True).start()
    srv = U.Server(("127.0.0.1", 0), app, set(), U.load_assets())
    srv.allowed_hosts = U.allowed_hosts_for(srv.server_address[1], [])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def click_tab(br, tab):
    br.eval("(() => { const b = document.querySelector('[data-tab=%s]'); if (b) b.click(); })()" % tab)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default="")
    ap.add_argument("--window", type=float, default=21.0)
    a = ap.parse_args()
    root = tempfile.mkdtemp(prefix="uinext_audit_")
    srv = build_tree(root)
    port = srv.server_address[1]
    time.sleep(3)
    br = cdp.Browser()
    out = {"static": {}, "tabs": {}, "views": {}}
    try:
        base = "http://127.0.0.1:%d/" % port
        br.viewport(1280, 900)
        br.go(base, 4)
        n = br.net()
        out["static"]["first_load_requests"] = n
        out["static"]["first_load_total"] = [sum(v[0] for v in n.values()), sum(v[1] for v in n.values())]
        out["paint"] = br.eval("(() => { const p = {}; performance.getEntriesByType('paint').forEach(e => p[e.name] = Math.round(e.startTime)); const nav = performance.getEntriesByType('navigation')[0]; p.domContentLoaded = Math.round(nav.domContentLoadedEventEnd); p.load = Math.round(nav.loadEventEnd); return p; })()")
        for (w, h, mob) in VIEWS:
            br.viewport(w, h, mobile=mob, dpr=1)
            for tab in TABS:
                br.go(base, 2.5)
                click_tab(br, tab)
                br.ws.pump(3.5)
                probe = br.eval(PROBE)
                out["views"].setdefault("%dx%d" % (w, h), {})[tab] = probe
                if a.shots and w in (390, 1280):
                    os.makedirs(a.shots, exist_ok=True)
                    probe["shot_bytes"] = br.shot(os.path.join(a.shots, "current_%s_%d.png" % (tab, w)))
        for label, (w, h, mob, cpu) in {"desktop": (1280, 900, False, 1), "phone_4x": (390, 844, True, 4)}.items():
            br.viewport(w, h, mobile=mob, cpu=cpu)
            for tab in TABS:
                br.go(base, 2.0)
                click_tab(br, tab)
                br.ws.pump(2.0)
                br.ws.events.clear()
                m0, t0 = br.metrics(), time.time()
                br.ws.pump(a.window)
                m1, dt = br.metrics(), time.time() - t0
                net = br.net()
                d = lambda k: m1.get(k, 0) - m0.get(k, 0)
                out["tabs"].setdefault(tab, {})[label] = {
                    "window_s": round(dt, 1), "cpu_pct": round(100 * d("TaskDuration") / dt, 1), "script_pct": round(100 * d("ScriptDuration") / dt, 1),
                    "layouts_per_s": round(d("LayoutCount") / dt, 2), "style_recalcs_per_s": round(d("RecalcStyleCount") / dt, 2),
                    "heap_mb": round(m1.get("JSHeapUsedSize", 0) / 1e6, 1), "dom_nodes": int(m1.get("Nodes", 0)),
                    "requests": sum(v[0] for v in net.values()), "bytes": sum(v[1] for v in net.values()),
                    "bytes_per_s": round(sum(v[1] for v in net.values()) / dt),
                    "endpoints": {u: [v[0], v[1], round(v[0] / dt, 2)] for u, v in sorted(net.items())}}
    finally:
        br.close()
    json.dump(out, sys.stdout, indent=1)


if __name__ == "__main__":
    main()
