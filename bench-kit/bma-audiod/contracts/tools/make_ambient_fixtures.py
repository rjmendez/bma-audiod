#!/usr/bin/env python3
"""Regenerate contracts/fixtures/ambient_temp.jsonl by running the REAL tools/ambient_temp.py (and so the real shared
writer) against a local fake weather API on 127.0.0.1 that serves a fixed canned response (standard library only):

    python -I make_ambient_fixtures.py [out_dir]      (default: ../fixtures)

The canned response also carries wind_speed_10m / precipitation / cloud_cover; hour 05 has a null wind and hour 06 a null cloud
cover, so those two records omit that field (ambient-temp 1.1.0).
The canned response has 8 hours (2026-10-26T00:00 .. 07:00 UTC, a cold night rising after sunrise, one value below zero and one
whole-number value), then a null, a NaN and the not yet complete hours; the clock is fixed at 2026-10-26T10:30 UTC, so the tool writes
the 8 complete hours with valid values and skips the rest. The synthetic coordinates are written to a temp geo file that is deleted
afterwards; nothing about them reaches the fixture (the contract test greps for them)."""

import calendar, http.server, json, os, shutil, sys, tempfile, threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "tools")))
import ambient_temp as A  # noqa: E402

NOW = float(calendar.timegm((2026, 10, 26, 10, 30, 0)))
GEO_LAT, GEO_LON = 12.34, 56.78  # synthetic, never real; the tool rounds them to 12.3 / 56.8
TIMES = ["2026-10-26T%02d:00" % h for h in range(0, 12)]
TEMPS = [3.4, 2.9, 2.1, 1.0, -0.6, -0.2, 0.0, 4.5, None, float("nan"), 10.5, 11.2]  # 00..07 valid, 08 null, 09 NaN, 10 and 11 not complete
WINDS = [1.2, 0.8, 0.0, 2.5, 3.1, None, 4.0, 5.6, 1.0, 1.0, 1.0, 1.0]  # 05 null: the record has no wind_ms
PRECIPS = [0.0, 0.0, 0.0, 0.2, 1.4, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
CLOUDS = [100, 90, 75, 40, 0, 10, None, 5, 0, 0, 0, 0]  # 06 null: the record has no cloud_pct


def build(out_dir):
    body = json.dumps({"hourly": {"time": TIMES, "temperature_2m": TEMPS, "wind_speed_10m": WINDS, "precipitation": PRECIPS, "cloud_cover": CLOUDS}}).encode()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    work = tempfile.mkdtemp(prefix="ambfix_")
    try:
        geo = os.path.join(work, "geo.json")
        with open(geo, "w", encoding="utf-8") as f:
            json.dump({"lat": GEO_LAT, "lon": GEO_LON}, f)
        tmp_out = os.path.join(work, "out")
        rc = A.main(
            [
                "--geo-config",
                geo,
                "--out",
                tmp_out,
                "--api-url",
                "http://127.0.0.1:%d/v1/forecast" % srv.server_address[1],
                "--now",
                str(NOW),
                "--backfill-days",
                "1",
            ]
        )
        if rc != 0:
            raise SystemExit("ambient_temp.py failed")
        os.makedirs(out_dir, exist_ok=True)
        dst = os.path.join(out_dir, "ambient_temp.jsonl")
        with open(os.path.join(tmp_out, A.FILE_NAME), "rb") as f:
            data = f.read()
        with open(dst, "wb") as f:
            f.write(data)
        return dst
    finally:
        srv.shutdown()
        srv.server_close()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    print("wrote", build(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "fixtures")))
