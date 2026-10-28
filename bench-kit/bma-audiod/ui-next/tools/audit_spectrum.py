#!/usr/bin/env python3
"""Cost of the CURRENT live spectrum panel with data: the repo's spectrum fixture server (real static assets, deterministic /api/spectrum at 10 rows/s)
in headless Edge, at desktop and a 4x-throttled phone. The audit server has no spectrum source, so this fills that gap. JSON on stdout."""
import json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "ui")))
sys.path.insert(0, HERE)
import spectrum_fixture as SF   # noqa: E402
import cdp                      # noqa: E402


def main():
    fx = SF.Fixture()
    port = fx.start()
    br = cdp.Browser(9336)
    out = {}
    try:
        for label, (w, h, mob, cpu) in {"desktop": (1280, 900, False, 1), "phone_4x": (390, 844, True, 4)}.items():
            br.viewport(w, h, mobile=mob, cpu=cpu)
            for tab in ("system", "beams", "overview"):
                br.go("http://127.0.0.1:%d/" % port, 2.0)
                br.eval("(() => { const b = document.querySelector('[data-tab=%s]'); if (b) b.click(); })()" % tab)
                br.ws.pump(3.0)
                br.ws.events.clear()
                m0, t0 = br.metrics(), time.time()
                br.ws.pump(21.0)
                m1, dt = br.metrics(), time.time() - t0
                net = br.net()
                d = lambda k: m1.get(k, 0) - m0.get(k, 0)
                canv = br.eval("document.querySelectorAll('.tab:not([hidden]) canvas').length")
                out.setdefault(tab, {})[label] = {
                    "cpu_pct": round(100 * d("TaskDuration") / dt, 1), "script_pct": round(100 * d("ScriptDuration") / dt, 1), "layouts_per_s": round(d("LayoutCount") / dt, 2),
                    "heap_mb": round(m1.get("JSHeapUsedSize", 0) / 1e6, 1), "canvases": canv, "requests": sum(v[0] for v in net.values()), "bytes_per_s": round(sum(v[1] for v in net.values()) / dt),
                    "spectrum_req_per_s": round(net.get("/api/spectrum", [0, 0])[0] / dt, 2), "spectrum_bytes_per_req": round(net.get("/api/spectrum", [0, 0])[1] / max(1, net.get("/api/spectrum", [0, 0])[0]))}
    finally:
        br.close()
        fx.stop()
    json.dump(out, sys.stdout, indent=1)


if __name__ == "__main__":
    main()
