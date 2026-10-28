#!/usr/bin/env python3
"""Same measurements as audit_current.py, for the prototype served by ui-next/mock_server.py: first load (requests, bytes, paint), per view steady cost
(CPU, layouts, heap, DOM nodes, network incl. WebSocket payload) at desktop and a 4x-throttled phone, layout probes at five widths. JSON on stdout.
    python measure_proto.py [--window 21] [--r webgl|canvas2d|webgpu]"""
import argparse, json, os, sys, threading, time
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))
import cdp           # noqa: E402
import mock_server   # noqa: E402

VIEWS = ["live", "review", "det", "sys"]
SIZES = [(360, 740, True), (390, 844, True), (768, 1024, False), (1280, 900, False), (1920, 1080, False)]
PROBE = """(() => {
  const vis = e => e.getClientRects().length > 0, q = s => Array.from(document.querySelectorAll(s));
  const v = document.querySelector('.view:not([hidden])');
  const small = q('button, a, input, select, summary').filter(vis).filter(e => { const r = e.getBoundingClientRect(); return r.width < 44 || r.height < 44; }).length;
  return { nodes: document.querySelectorAll('*').length, viewNodes: v ? v.querySelectorAll('*').length : 0, panels: v ? v.querySelectorAll('.panel').length : 0,
    canvases: v ? v.querySelectorAll('canvas').length : 0, overflowX: document.documentElement.scrollWidth > innerWidth + 1, smallTargets: small,
    scrollH: document.documentElement.scrollHeight, renderer: document.documentElement.dataset.renderer || '' };
})()"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=float, default=21.0)
    ap.add_argument("--r", default="webgl")
    ap.add_argument("--browser", default="edge", choices=("edge", "chrome"))
    a = ap.parse_args()
    srv = mock_server.Server(("127.0.0.1", 0), mock_server.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d/" % srv.server_address[1]
    q = "?r=" + a.r
    br = cdp.Browser(9337, exe=cdp.browser_exe(a.browser))
    out = {"load": {}, "views": {}, "steady": {}}
    try:
        br.viewport(1280, 900)
        paints = []
        for i in range(5):                                           # first load: median of five, the machine is noisy
            br.go(base + q, 4)
            n = br.net()
            paints.append(br.eval("(() => { const p = {}; performance.getEntriesByType('paint').forEach(e => p[e.name] = Math.round(e.startTime)); return p; })()"))
        fcp = sorted(p.get("first-contentful-paint", 0) for p in paints)
        out["load"] = {"requests": sum(v[0] for v in n.values()), "bytes": sum(v[1] for v in n.values()), "by_url": n, "ws": br.ws_bytes(),
                       "paint": {"first-paint": fcp[len(fcp) // 2], "all_fcp": fcp}}
        for (w, h, mob) in SIZES:
            br.viewport(w, h, mobile=mob)
            for v in VIEWS:
                br.go(base + q + "#" + v, 3)
                br.eval("document.querySelector('[data-view=%s]').click()" % v); br.ws.pump(1.5)
                out["views"].setdefault("%dx%d" % (w, h), {})[v] = br.eval(PROBE)
        for label, (w, h, mob, cpu) in {"desktop": (1280, 900, False, 1), "phone_4x": (390, 844, True, 4)}.items():
            br.viewport(w, h, mobile=mob, cpu=cpu)
            for v in VIEWS:
                br.go(base + q + "#" + v, 3)
                br.eval("document.querySelector('[data-view=%s]').click()" % v); br.ws.pump(2.0)
                br.ws.events.clear()
                m0, t0 = br.metrics(), time.time()
                br.ws.pump(a.window)
                m1, dt = br.metrics(), time.time() - t0
                net, wsb = br.net(), br.ws_bytes()
                d = lambda k: m1.get(k, 0) - m0.get(k, 0)
                out["steady"].setdefault(v, {})[label] = {
                    "cpu_pct": round(100 * d("TaskDuration") / dt, 1), "script_pct": round(100 * d("ScriptDuration") / dt, 1), "layouts_per_s": round(d("LayoutCount") / dt, 2),
                    "heap_mb": round(m1.get("JSHeapUsedSize", 0) / 1e6, 1), "dom_nodes": int(m1.get("Nodes", 0)), "requests": sum(x[0] for x in net.values()),
                    "http_bytes_per_s": round(sum(x[1] for x in net.values()) / dt), "ws_frames_per_s": round(wsb[0] / dt, 1), "ws_bytes_per_s": round(wsb[1] / dt),
                    "bytes_per_s": round((sum(x[1] for x in net.values()) + wsb[1]) / dt)}
    finally:
        br.close()
    root = os.path.join(os.path.dirname(HERE), "static")
    out["static_bytes"] = {os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/"): os.path.getsize(os.path.join(d, f))
                           for d, _, fs in os.walk(root) for f in fs if f.endswith((".html", ".css", ".js"))}
    json.dump(out, sys.stdout, indent=1)


if __name__ == "__main__":
    main()
