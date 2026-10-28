#!/usr/bin/env python3
"""A fixture dashboard server for the spectrum panel and tooltip DOM tests: the real static assets of ui/static plus a deterministic
GET /api/spectrum built from the contract's band edges (contracts/fixtures/ui_spectrum.json). Stdlib only, 127.0.0.1, GET only.
Every other /api path answers 404 problem+json, so the dashboard shows its normal 'error' states. The row generator is a pure function of
the clock: row k has t = T0 + k * 0.1 s, every band at VALUE_NOISE except PEAK_BIN at VALUE_PEAK, so a test knows exactly which pixel column and
colour must show the peak. `mode` is mutated by the tests (state, gating, detail, rate limit); `log` records every /api/spectrum query string."""

import base64, json, os, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "..", "contracts", "fixtures", "ui_spectrum.json")
PEAK_BIN, VALUE_PEAK, VALUE_NOISE, GATED_VALUE = 30, 80, 30, 60  # -60 dB / -85 dB / -70 dB at db_floor -100, db_step 0.5
RING_S, HOP = 60.0, 0.1
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}


def level_of(beam_idx, band):
    """compare level (uint8) of beam beam_idx (0..7) in octave band `band`: beam 8 is the loudest, so the 'Loudest' mode must pick beam 8"""
    return 40 + 10 * beam_idx + band


class Fixture:
    def __init__(self, static_dir=None, detail_ok=False):
        self.static = static_dir or os.path.join(HERE, "static")
        with open(FIX, encoding="utf-8") as f:
            self.edges = json.load(f)["edges_hz"]
        self.t0 = time.time() - 65.0
        self.mode = {"state": "live", "freeze_at": None, "gate_from": None, "detail_ok": detail_ok, "rate_limit": False, "rev": 1}
        self.log = []
        self.lock = threading.Lock()
        self.srv = None

    # --- rows
    def row(self, k, compare, band):
        t = self.t0 + k * HOP
        gated = self.mode["gate_from"] is not None and t >= self.mode["gate_from"]
        bins = bytes([GATED_VALUE] * len(self.edges[:-1])) if gated else bytes(VALUE_PEAK if b == PEAK_BIN else VALUE_NOISE for b in range(len(self.edges) - 1))
        r = {"idx": k + 1, "t_s": round(t, 3), "flags": 1 if gated else 0, "gated": gated, "bins": base64.b64encode(bins).decode()}
        if compare:
            r["levels"] = [level_of(b, band) for b in range(8)]
        return r

    def body(self, q):
        now = time.time()
        m = self.mode
        if m["state"] == "auth_required":  # what bma_ui answers a non-loopback client (contracts/fixtures/ui_spectrum_auth_required.json)
            return {
                "spectrum_version": "1.0.0",
                "state": "auth_required",
                "tier": "standard",
                "beam": 0,
                "now_s": round(now, 3),
                "n_bins": 0,
                "edges_hz": [],
                "db_floor": -100.0,
                "db_step": 0.5,
                "rows_per_s": 10,
                "hop_ms": 100,
                "rev": 0,
                "newest_idx": 0,
                "next_since": 0,
                "rows_missed": 0,
                "newest_age_s": None,
                "gated_now": False,
                "limits": {"ring_s": RING_S, "max_rows": 600, "requests_per_s": 5.0},
                "rows": [],
            }
        if m["state"] == "disabled":
            return {
                "spectrum_version": "1.0.0",
                "state": "disabled",
                "tier": "standard",
                "beam": 0,
                "now_s": round(now, 3),
                "n_bins": 0,
                "edges_hz": [],
                "rows": [],
                "next_since": 0,
                "rev": 0,
            }
        end = now if m["freeze_at"] is None else m["freeze_at"]
        newest_k = int((end - self.t0) / HOP)
        since = int(q.get("since", ["0"])[0])
        limit = int(q.get("limit", ["600"])[0])
        first = max(newest_k - limit, since) if since else newest_k - limit
        compare = q.get("compare", ["false"])[0] == "true"
        band = int(q.get("band", ["5"])[0])
        rows = [] if m["state"] == "no_data" else [self.row(k, compare, band) for k in range(max(0, first), newest_k)]
        age = now - (self.t0 + (newest_k - 1) * HOP)
        state = m["state"] if m["state"] == "no_data" else ("stale" if age > 3.0 else "live")
        gated_now = bool(rows and rows[-1]["gated"])
        return {
            "spectrum_version": "1.0.0",
            "state": state,
            "tier": q.get("tier", ["standard"])[0],
            "beam": int(q.get("beam", ["0"])[0]),
            "now_s": round(now, 3),
            "n_bins": len(self.edges) - 1,
            "edges_hz": self.edges,
            "db_floor": -100.0,
            "db_step": 0.5,
            "rows_per_s": 10,
            "hop_ms": 100,
            "rev": m["rev"],
            "newest_idx": newest_k,
            "next_since": newest_k,
            "rows_missed": 0,
            "newest_age_s": round(age, 3),
            "gated_now": gated_now,
            "limits": {"ring_s": RING_S, "max_rows": 600, "requests_per_s": 5.0},
            "rows": rows,
        }

    def start(self):
        fx = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, data, ctype, extra=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                from urllib.parse import urlparse, parse_qs

                u = urlparse(self.path)
                if u.path == "/api/spectrum":
                    q = parse_qs(u.query)
                    with fx.lock:
                        fx.log.append(u.query)
                    if fx.mode["rate_limit"]:
                        return self._send(429, b'{"title":"rate_limited","detail":"slow down"}', "application/json", {"Retry-After": "1"})
                    if q.get("tier", ["standard"])[0] == "detail" and not fx.mode["detail_ok"]:
                        return self._send(403, b'{"title":"spectrum_detail_disabled","detail":"off"}', "application/json")
                    return self._send(200, json.dumps(fx.body(q)).encode(), "application/json")
                if u.path.startswith("/api/"):
                    return self._send(404, b'{"title":"not_found","detail":"fixture server"}', "application/json")
                name = {"/": "index.html", "/index.html": "index.html"}.get(u.path) or (u.path[len("/static/") :] if u.path.startswith("/static/") else None)
                p = os.path.join(fx.static, name) if name and "/" not in name and name != ".." else None
                if not p or not os.path.isfile(p):
                    return self._send(404, b"not found", "text/plain")
                with open(p, "rb") as f:
                    return self._send(200, f.read(), TYPES.get(os.path.splitext(p)[1], "application/octet-stream"))

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self.srv.server_address[1]

    def stop(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
