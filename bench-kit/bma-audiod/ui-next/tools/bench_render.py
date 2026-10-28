#!/usr/bin/env python3
"""Waterfall render benchmark in headless Edge: Canvas2D vs tiled DOM canvases vs WebGL2 vs WebGPU, main thread vs OffscreenCanvas worker, three data loads, desktop vs a
4x-CPU-throttled 360 px phone profile. One Edge run at a time. Output: JSON on stdout.
    python bench_render.py [--dur 12] [--only canvas2d,webgl] [--loads std,detail,heavy]
Loads (9 tiles = the 8-beam mean + 8 beams, 600 columns ring each): std 46 bins x 10 col/s, detail 100 bins x 50 col/s, heavy 1024 bins x 50 col/s."""
import argparse, functools, http.server, json, os, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.abspath(os.path.join(HERE, "..", "bench", "static"))
sys.path.insert(0, HERE)
import cdp  # noqa: E402

LOADS = {"std": (46, 10), "detail": (100, 50), "heavy": (1024, 50)}
PROFILES = {"desktop": dict(w=960, view=(1280, 900, False, 1)), "phone_4x": dict(w=360, view=(390, 844, True, 4))}


class H(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map, ".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".html": "text/html"}

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dur", type=float, default=12.0)
    ap.add_argument("--only", default="canvas2d,tiled,webgl,webgpu")
    ap.add_argument("--loads", default="std,detail,heavy")
    ap.add_argument("--profiles", default="desktop,phone_4x")
    ap.add_argument("--wheres", default="main,worker")
    ap.add_argument("--reps", type=int, default=1)
    a = ap.parse_args()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=STATIC))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d/bench.html" % srv.server_address[1]
    br = cdp.Browser(9334)
    res = []
    try:
        for prof, pc in [(k, PROFILES[k]) for k in a.profiles.split(",")]:
            vw, vh, mob, cpu = pc["view"]
            br.viewport(vw, vh, mobile=mob, cpu=cpu)
            for load in a.loads.split(","):
                bins, rate = LOADS[load]
                for kind in a.only.split(","):
                    for where in [w for w in a.wheres.split(",") if kind in ("canvas2d", "webgl") or w == "main"]:
                        for rep in range(a.reps):
                            br.go("about:blank", 0.2)
                            br.ws.call("Page.navigate", {"url": "%s#kind=%s&where=%s&bins=%d&rate=%d&dur=%g&w=%d" % (base, kind, where, bins, rate, a.dur, pc["w"])})
                            br.ws.pump(1.5)
                            m0, t0 = br.metrics(), time.time()
                            out = None
                            while time.time() - t0 < a.dur + 25:
                                br.ws.pump(1.0)
                                out = br.eval("window.__bench && window.__bench.done ? window.__bench : null")
                                if out:
                                    break
                            m1, dt = br.metrics(), time.time() - t0
                            if not out:
                                out = {"error": "timeout"}
                            row = {"profile": prof, "load": load, "kind": kind, "where": where, "rep": rep, "error": out.get("error")}
                            if not out.get("error"):
                                main_cpu = 100 * (m1["TaskDuration"] - m0["TaskDuration"]) / dt
                                row.update({"fps": round(out["fps"], 1), "render_ms_per_frame": round(out["mainMsPerFrame"], 3), "p95_ms": round(out["p95"], 2), "max_ms": round(out["max"], 1),
                                            "main_cpu_pct": round(main_cpu, 1), "worker_busy_pct": round(100 * out["workerBusyMs"] / (out["dur"] * 1000), 1) if out["workerBusyMs"] else 0.0,
                                            "upload_kb_per_s": round(out["uploadBytes"] / out["dur"] / 1024, 1), "heap_mb": round(m1.get("JSHeapUsedSize", 0) / 1e6, 1)})
                            res.append(row)
                            print(json.dumps(row), file=sys.stderr, flush=True)
    finally:
        br.close()
    json.dump(res, sys.stdout, indent=1)


if __name__ == "__main__":
    main()
