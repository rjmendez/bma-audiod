#!/usr/bin/env python3
"""First contentful paint and load time of the current dashboard and the prototype, loaded alternately N times each in the same headless Edge (cache disabled),
so machine noise hits both equally. JSON on stdout.   python first_paint.py [--n 7]"""
import argparse, json, os, statistics, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))
import audit_current as AC   # noqa: E402
import cdp                   # noqa: E402
import mock_server           # noqa: E402

JS = "(() => { const p = {}; performance.getEntriesByType('paint').forEach(e => p[e.name] = Math.round(e.startTime)); const n = performance.getEntriesByType('navigation')[0]; p.load = Math.round(n.loadEventEnd); p.dcl = Math.round(n.domContentLoadedEventEnd); return p; })()"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=7)
    a = ap.parse_args()
    cur = AC.build_tree(tempfile.mkdtemp(prefix="uinext_fp_"))
    pro = mock_server.Server(("127.0.0.1", 0), mock_server.Handler)
    threading.Thread(target=pro.serve_forever, daemon=True).start()
    time.sleep(3)
    urls = {"current": "http://127.0.0.1:%d/" % cur.server_address[1], "prototype": "http://127.0.0.1:%d/" % pro.server_address[1]}
    br = cdp.Browser(9339)
    res = {k: [] for k in urls}
    try:
        br.viewport(1280, 900)
        br.ws.call("Network.setCacheDisabled", {"cacheDisabled": True})
        for i in range(a.n):
            for k, u in urls.items():
                br.go(u, 3.0)
                res[k].append(br.eval(JS))
    finally:
        br.close()
    out = {}
    for k, rows in res.items():
        f = [r.get("first-contentful-paint", 0) for r in rows if r.get("first-contentful-paint")]
        out[k] = {"fcp_ms": f, "fcp_median": statistics.median(f) if f else None, "load_median": statistics.median([r["load"] for r in rows]), "dcl_median": statistics.median([r["dcl"] for r in rows])}
    json.dump(out, sys.stdout, indent=1)


if __name__ == "__main__":
    main()
