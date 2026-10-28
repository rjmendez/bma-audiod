#!/usr/bin/env python3
"""Tests for tools/ambient_temp.py. The tool runs as a real subprocess against a local fake weather API (127.0.0.1, ephemeral
port); the fake records every request, so the exact parameters, the number of requests and the headers are asserted. All
coordinates in here are synthetic. Temp directories only. Run:  python -I test_ambient_temp.py"""

import calendar, http.server, json, os, shutil, socket, subprocess, sys, tempfile, threading, time, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "ambient_temp.py")
HISTORY_LOG = os.path.join(HERE, "..", "pc", "history_log.py")

n_checks, fails = 0, []


def check(name, cond, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  [" + str(detail) + "]"))
    if not cond:
        fails.append(name)


class Fake:
    """The weather API: answers with `body` (mode ok), HTTP 500, a hang, or garbage; records every request."""

    def __init__(self):
        self.requests, self.mode, self.body = [], "ok", b"{}"
        self.release = threading.Event()
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                fake.requests.append({"path": self.path, "headers": {k: v for k, v in self.headers.items()}})
                if fake.mode == "hang":
                    fake.release.wait(10)
                    return
                if fake.mode == "drip":  # a valid 200 whose body arrives one byte every 0.3 s: never trips a per-read timeout
                    self.send_response(200)
                    self.send_header("Content-Length", "100000")
                    self.end_headers()
                    try:
                        for _ in range(100):
                            self.wfile.write(b" ")
                            self.wfile.flush()
                            if fake.release.wait(0.3):
                                break
                    except OSError:
                        pass
                    return
                if fake.mode == "500":
                    self.send_response(500)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                data = fake.body if fake.mode == "ok" else b"this is not json {{{"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        class S(http.server.ThreadingHTTPServer):
            daemon_threads = True

        self.srv = S(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d/v1/forecast" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def set(self, mode, body=None):
        self.mode = mode
        if body is not None:
            self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.requests.clear()

    def close(self):
        self.release.set()
        self.srv.shutdown()
        self.srv.server_close()


def run(args, env_extra=None, drop_env=(), cwd=None, tool=TOOL):
    env = {k: v for k, v in os.environ.items() if k not in drop_env}
    env.update(env_extra or {})
    p = subprocess.run([sys.executable, "-I", tool] + args, capture_output=True, text=True, timeout=60, env=env, cwd=cwd)
    return p.returncode, p.stdout, p.stderr


def lines(path):
    with open(path, "rb") as f:
        return [json.loads(x) for x in f.read().splitlines() if x]


def raw(path):
    with open(path, "rb") as f:
        return f.read()


def epoch(y, mo, d, h):
    return calendar.timegm((y, mo, d, h, 0, 0))


NOW = float(calendar.timegm((2026, 10, 26, 13, 30, 0)))  # 1793021400: the 13:00 hour is not complete
LAT, LON = 12.34, 56.78  # synthetic; rounded: 12.3 and 56.8
RLAT, RLON = "12.3", "56.8"
CANNED = {
    "hourly": {
        "time": [
            "2026-10-25T12:00",
            "2026-10-25T13:00",
            "2026-10-26T10:00",
            "2026-10-26T11:00",
            "2026-10-26T12:00",
            "2026-10-26T13:00",
            "2026-10-26T14:00",
            "2026-10-26T15:00",
        ],
        "temperature_2m": [4.0, 5.5, 8.25, None, 9, 11.0, 12.0, float("nan")],  # json.dumps writes NaN
        "wind_speed_10m": [1.0, 2.5, 3.0, 9.9, None, 4.0, 5.0, 6.0],
        "precipitation": [0.0, 0.4, 1.5, 2.0, 0.0, -1.0, 0.1, 0.0],
        "cloud_cover": [10, 20, float("nan"), 40, 150, 60, 70, 80],
    }
}
REC_OLDEST = {
    "k": "ambient_temp",
    "v": 1,
    "t_hour_utc": epoch(2026, 10, 25, 13),
    "temp_c": 5.5,
    "source": "open-meteo",
    "model_grid_km": None,
    "wind_ms": 2.5,
    "precip_mm": 0.4,
    "cloud_pct": 20.0,
}
REC_10 = {
    "k": "ambient_temp",
    "v": 1,
    "t_hour_utc": epoch(2026, 10, 26, 10),
    "temp_c": 8.25,
    "source": "open-meteo",
    "model_grid_km": None,
    "wind_ms": 3.0,
    "precip_mm": 1.5,
}
REC_12 = {"k": "ambient_temp", "v": 1, "t_hour_utc": epoch(2026, 10, 26, 12), "temp_c": 9.0, "source": "open-meteo", "model_grid_km": None, "precip_mm": 0.0}
REC_13 = {
    "k": "ambient_temp",
    "v": 1,
    "t_hour_utc": epoch(2026, 10, 26, 13),
    "temp_c": 11.0,
    "source": "open-meteo",
    "model_grid_km": None,
    "wind_ms": 4.0,
    "cloud_pct": 60.0,
}
WX = {"wind_ms": 1.0, "precip_mm": 0.0, "cloud_pct": 50.0}  # the default valid covariates of hourly() and of the bulk bodies
REC_PRE = {
    "k": "ambient_temp",
    "v": 1,
    "t_hour_utc": epoch(2026, 10, 25, 12),
    "temp_c": 4.0,
    "source": "open-meteo",
    "model_grid_km": None,
    "wind_ms": 1.0,
    "precip_mm": 0.0,
    "cloud_pct": 10.0,
}

CANNED_ERR = "ambient_temp: failures: bad_cloud=2 bad_precip=1 bad_values=2 bad_wind=1\n"
tmp = tempfile.mkdtemp(prefix="ambient_temp_test_")
fake = Fake()
try:
    geo = os.path.join(tmp, "geo_probe_%d.json" % os.getpid())
    with open(geo, "w", encoding="utf-8") as f:
        json.dump({"lat": LAT, "lon": LON, "note": "synthetic", "threshold": 0.002}, f)

    def go(out, extra=(), now=NOW, geo_path=geo, **kw):
        return run(["--geo-config", geo_path, "--out", out, "--api-url", fake.url, "--now", str(now), "--backfill-days", "1"] + list(extra), **kw)

    def alltext(out, rc, so, se):
        s = so + se
        for n in ("ambient_temp.jsonl", "ambient_temp.jsonl.1"):
            if os.path.exists(os.path.join(out, n)):
                s += raw(os.path.join(out, n)).decode()
        return s

    # ---------------------------------------------------------------- the request, the parsing, the records
    fake.set("ok", CANNED)
    o1 = os.path.join(tmp, "o1")
    rc, so, se = go(o1)
    req = fake.requests
    check("one request per run", len(req) == 1, len(req))
    u = urllib.parse.urlsplit(req[0]["path"])
    check(
        "request path is /v1/forecast with the exact query: rounded coordinates, hourly, past_days, forecast_days, UTC",
        u.path == "/v1/forecast"
        and u.query
        == "latitude=12.3&longitude=56.8&hourly=temperature_2m%2Cwind_speed_10m%2Cprecipitation%2Ccloud_cover&wind_speed_unit=ms&past_days=1&forecast_days=1&timezone=UTC",
        u.path + "?" + u.query,
    )
    check(
        "the request carries nothing else: User-Agent is the generic ambient-temp/1 and the headers are only the plain urllib set",
        req[0]["headers"].get("User-Agent") == "ambient-temp/1"
        and {k.lower() for k in req[0]["headers"]} <= {"host", "user-agent", "accept", "accept-encoding", "connection"},
        req[0]["headers"],
    )
    check(
        "positive twin: a full-precision latitude / longitude never reached the server",
        "12.34" not in req[0]["path"] and "56.78" not in req[0]["path"],
        req[0]["path"],
    )
    recs = lines(os.path.join(o1, "ambient_temp.jsonl"))
    check(
        "exit 0 and exactly the three complete in-window hours are written, ascending, with exact fields (null and NaN skipped, future skipped, older than 1 day skipped)",
        rc == 0 and recs == [REC_OLDEST, REC_10, REC_12],
        (rc, recs),
    )
    check(
        "the record line is compact JSON with LF",
        raw(os.path.join(o1, "ambient_temp.jsonl")).split(b"\n")[0]
        == b'{"k":"ambient_temp","v":1,"t_hour_utc":1792933200,"temp_c":5.5,"source":"open-meteo","model_grid_km":null,"wind_ms":2.5,"precip_mm":0.4,"cloud_pct":20.0}',
        raw(os.path.join(o1, "ambient_temp.jsonl"))[:160],
    )
    check(
        "future hours (13:00 incomplete, 14:00) are not in the file",
        all(r["t_hour_utc"] + 3600 <= NOW for r in recs) and REC_13["t_hour_utc"] not in {r["t_hour_utc"] for r in recs},
        recs,
    )
    check(
        "stdout counts: wrote 3, present 0, future 2, older 1",
        so == "ambient_temp: wrote 3, already present 0, future 2, older than the backfill window 1\n",
        so,
    )
    check(
        "null and NaN temperatures and the invalid covariates (wind null, cloud NaN and 150, precip -1) are counted per field on ONE stderr line",
        se == CANNED_ERR,
        se,
    )
    check(
        "covariates are per field: 10:00 has no cloud_pct (NaN); 12:00 has neither wind_ms (null) nor cloud_pct (150) and keeps precip_mm 0.0; the records are still written",
        recs[1] == REC_10
        and "cloud_pct" not in recs[1]
        and recs[2] == REC_12
        and set(recs[2]) == {"k", "v", "t_hour_utc", "temp_c", "source", "model_grid_km", "precip_mm"},
        recs,
    )

    # ---------------------------------------------------------------- dedupe by hour across runs; an hour that became complete is added
    before = raw(os.path.join(o1, "ambient_temp.jsonl"))
    rc, so, se = go(o1)
    check(
        "second identical run: exit 0, nothing appended (the file is byte for byte the same)",
        rc == 0 and raw(os.path.join(o1, "ambient_temp.jsonl")) == before,
        (rc, so),
    )
    check("second run reports 0 written and 3 already present", so.startswith("ambient_temp: wrote 0, already present 3, "), so)
    rc, so, se = go(o1, now=NOW + 3600)
    recs = lines(os.path.join(o1, "ambient_temp.jsonl"))
    check(
        "positive twin: an hour later the 13:00 hour is complete and is written once, the others stay (4 records, no duplicate hour)",
        rc == 0 and recs[:3] == [REC_OLDEST, REC_10, REC_12] and recs[3] == REC_13 and len(recs) == 4 and len({r["t_hour_utc"] for r in recs}) == 4,
        (rc, recs),
    )

    # ---------------------------------------------------------------- backfill fills only missing past hours
    o2 = os.path.join(tmp, "o2")
    hole = {"hourly": {k_: [v for t, v in zip(CANNED["hourly"]["time"], vs_) if t != "2026-10-26T10:00"] for k_, vs_ in CANNED["hourly"].items()}}
    fake.set("ok", hole)
    go(o2)
    check(
        "first run with a hole writes the two hours it has",
        lines(os.path.join(o2, "ambient_temp.jsonl")) == [REC_OLDEST, REC_12],
        lines(os.path.join(o2, "ambient_temp.jsonl")),
    )
    fake.set("ok", CANNED)
    rc, so, se = go(o2)
    check(
        "a later run appends ONLY the missing 10:00 hour (the two present hours are not rewritten)",
        rc == 0
        and lines(os.path.join(o2, "ambient_temp.jsonl")) == [REC_OLDEST, REC_12, REC_10]
        and so.startswith("ambient_temp: wrote 1, already present 2, "),
        (so, lines(os.path.join(o2, "ambient_temp.jsonl"))),
    )
    o3 = os.path.join(tmp, "o3")
    fake.set("ok", CANNED)
    rc, so, se = go(o3, extra=["--backfill-days", "2"])
    check(
        "positive twin: with --backfill-days 2 the older 25T12:00 hour is inside the window and past_days=2 is requested",
        lines(os.path.join(o3, "ambient_temp.jsonl")) == [REC_PRE, REC_OLDEST, REC_10, REC_12] and "past_days=2" in fake.requests[0]["path"],
        (lines(os.path.join(o3, "ambient_temp.jsonl")), fake.requests[0]["path"]),
    )

    # ---------------------------------------------------------------- failures: exit 0, nothing written, counted
    for mode, key, label in (("500", "http_errors=1", "HTTP 500"), ("garbage", "bad_json=1", "garbage body")):
        fo = os.path.join(tmp, "f_" + mode)
        fake.set(mode)
        rc, so, se = go(fo)
        check(
            label + ": exit 0, zero records, one counted failure on one stderr line, one request",
            rc == 0 and not os.path.exists(os.path.join(fo, "ambient_temp.jsonl")) and se == "ambient_temp: failures: %s\n" % key and len(fake.requests) == 1,
            (rc, se, so),
        )
        check(
            label + ": privacy, no original or rounded coordinate and no geo path in stdout, stderr or files",
            not any(s in alltext(fo, rc, so, se) for s in ("12.34", "56.78", RLAT, RLON, geo, "geo_probe")),
            alltext(fo, rc, so, se),
        )
    fake.set("ok", {"hourly": {"time": ["2026-10-26T10:00"]}})
    rc, so, se = go(os.path.join(tmp, "f_shape"))
    check(
        "valid JSON of the wrong shape (no temperature_2m): exit 0, counted as bad_json, nothing written",
        rc == 0 and se == "ambient_temp: failures: bad_json=1\n" and not os.path.exists(os.path.join(tmp, "f_shape", "ambient_temp.jsonl")),
        (rc, se),
    )
    fake.set("hang")
    t0 = time.time()
    rc, so, se = go(os.path.join(tmp, "f_hang"), extra=["--timeout-s", "0.5"])
    check(
        "a server that never answers: exit 0 after the timeout, zero records, timeouts=1",
        rc == 0
        and se == "ambient_temp: failures: timeouts=1\n"
        and time.time() - t0 < 8
        and not os.path.exists(os.path.join(tmp, "f_hang", "ambient_temp.jsonl")),
        (rc, se, time.time() - t0),
    )
    fake.release.set()
    s_ = socket.socket()
    s_.bind(("127.0.0.1", 0))
    dead = "http://127.0.0.1:%d/v1/forecast" % s_.getsockname()[1]
    s_.close()
    rc, so, se = run(["--geo-config", geo, "--out", os.path.join(tmp, "f_dead"), "--api-url", dead, "--now", str(NOW)])
    check(
        "connection refused: exit 0, network_errors=1, and the error text carries no coordinates",
        rc == 0 and se == "ambient_temp: failures: network_errors=1\n",
        (rc, se),
    )
    fake.set("ok", CANNED)
    rc, so, se = go(os.path.join(tmp, "f_twin"))
    check(
        "positive twin of the failures: the same call against a working server writes 3 records and exit 0",
        rc == 0 and len(lines(os.path.join(tmp, "f_twin", "ambient_temp.jsonl"))) == 3,
        (rc, se),
    )

    # ---------------------------------------------------------------- missing / unusable geo config: no network call
    def gj(a_, b_):
        return json.dumps({"lat": a_, "lon": b_})

    bads = {
        "absent": None,
        "empty object": "{}",
        "lat only": json.dumps({"lat": LAT}),
        "string coordinates": gj("5.5", "6.5"),
        "garbage": "not json",
        "example placeholder 0/0": gj(0.0, 0.0),
        "out of range": gj(95.0, 10.0),
        "boolean": gj(True, 1.5),
        "integer too large for a float": gj(10**400, 1.5),
        "deeply nested": "[" * 70000,
    }
    for name, text in bads.items():
        gp = os.path.join(tmp, "bad_geo_%s.json" % name.replace(" ", "_").replace("/", "_"))
        if text is not None:
            with open(gp, "w", encoding="utf-8") as f:
                f.write(text)
        fake.set("ok", CANNED)
        bo = os.path.join(tmp, "bad_out_%s" % name.replace(" ", "_").replace("/", "_"))
        rc, so, se = go(bo, geo_path=gp)
        check(
            "geo config %s: exit 0, one line 'no geo config: ambient temperature off', stderr empty, ZERO requests, no file, no path in the output" % name,
            rc == 0 and so == "no geo config: ambient temperature off\n" and se == "" and len(fake.requests) == 0 and not os.path.exists(bo),
            (rc, so, se, len(fake.requests)),
        )
    fake.set("ok", CANNED)
    rc, so, se = go(os.path.join(tmp, "good_out"))
    check("positive twin: a valid geo config makes exactly one request", rc == 0 and len(fake.requests) == 1, len(fake.requests))
    gneg = os.path.join(tmp, "geo_neg.json")
    with open(gneg, "w", encoding="utf-8") as f:
        f.write(gj(-0.04, -179.96))
    fake.set("ok", CANNED)
    go(os.path.join(tmp, "neg_out"), geo_path=gneg)
    check(
        "rounding of negative values: -0.04 -> 0.0 (not -0.0) and -179.96 -> -180.0",
        "latitude=0.0&longitude=-180.0&" in fake.requests[0]["path"],
        fake.requests[0]["path"],
    )

    # ---------------------------------------------------------------- privacy over a whole successful run
    txt = alltext(o1, 0, "", "")
    check(
        "privacy: the output file never contains the original or rounded coordinates or the geo path",
        not any(s in txt for s in ("12.34", "56.78", RLAT, RLON, geo, "geo_probe", "lat", "lon")),
        txt[:200],
    )
    rc, so, se = go(os.path.join(tmp, "o_priv"))
    check(
        "privacy: stdout and stderr of a successful run carry no coordinates or geo path",
        not any(s in so + se for s in ("12.34", "56.78", RLAT, RLON, geo, "geo_probe", tmp)),
        so + se,
    )

    # ---------------------------------------------------------------- the shared size-capped writer
    hours = [
        epoch(2026, 10, 19, 13) + 3600 * i for i in range(24 * 8)
    ]  # 8 days of hours: 168 complete ones (the last is 2026-10-26T12:00) and 24 in the future
    big = {
        "hourly": {
            "time": [time.strftime("%Y-%m-%dT%H:%M", time.gmtime(h)) for h in hours],
            "temperature_2m": [round(10 + (i % 17) * 0.5, 1) for i in range(len(hours))],
            "wind_speed_10m": [1.0] * len(hours),
            "precipitation": [0.0] * len(hours),
            "cloud_cover": [50.0] * len(hours),
        }
    }
    fake.set("ok", big)
    ro = os.path.join(tmp, "rot")
    rc, so, se = run(["--geo-config", geo, "--out", ro, "--api-url", fake.url, "--now", str(NOW), "--max-file-bytes", "1500"])
    cur, bak = os.path.join(ro, "ambient_temp.jsonl"), os.path.join(ro, "ambient_temp.jsonl.1")
    longest = max(len(l) + 1 for l in raw(bak).splitlines())
    check(
        "size cap: 168 records written in one run, the file rotated to .1 and neither file is larger than the cap plus one record",
        rc == 0
        and so.startswith("ambient_temp: wrote 168, ")
        and os.path.exists(bak)
        and os.path.getsize(cur) <= 1500 + longest
        and os.path.getsize(bak) <= 1500 + longest,
        (so, os.path.getsize(cur), os.path.getsize(bak) if os.path.exists(bak) else None),
    )
    kept = lines(bak) + lines(cur)
    check(
        "size cap: what is kept is the newest, in order, every line valid, no duplicate hour, and ends at 12:00 of the last day",
        [r["t_hour_utc"] for r in kept] == hours[168 - len(kept) : 168] and 10 <= len(kept) <= 20 and kept[-1]["t_hour_utc"] == epoch(2026, 10, 26, 12),
        (len(kept), kept[-1]),
    )
    ro2 = os.path.join(tmp, "norot")
    rc, so, se = run(["--geo-config", geo, "--out", ro2, "--api-url", fake.url, "--now", str(NOW)])
    check(
        "positive twin: with the default cap the same 168 records stay in one file and there is no .1",
        rc == 0 and len(lines(os.path.join(ro2, "ambient_temp.jsonl"))) == 168 and not os.path.exists(os.path.join(ro2, "ambient_temp.jsonl.1")),
        (rc, so),
    )
    # a torn last line (a writer that died mid-line) does not stop the run, hide the other hours or swallow the next record
    with open(os.path.join(ro2, "ambient_temp.jsonl"), "ab") as f:
        f.write(b'{"k":"ambient_temp","v":1,"t_hour_utc"')
    big2 = {
        "hourly": {
            "time": big["hourly"]["time"] + ["2026-10-26T13:00"],
            "temperature_2m": big["hourly"]["temperature_2m"] + [7.5],
            "wind_speed_10m": [1.0] * 193,
            "precipitation": [0.0] * 193,
            "cloud_cover": [50.0] * 193,
        }
    }
    fake.set("ok", big2)
    rc, so, se = run(["--geo-config", geo, "--out", ro2, "--api-url", fake.url, "--now", str(NOW + 3600)])
    good = []
    for l in raw(os.path.join(ro2, "ambient_temp.jsonl")).splitlines():
        try:
            good.append(json.loads(l))
        except ValueError:
            pass
    check(
        "torn last line: the 13:00 hour (complete an hour later) is written intact after it, 168 present hours are not duplicated, the fragment stays an isolated skipped line",
        rc == 0 and so.startswith("ambient_temp: wrote 1, already present 168, ") and len(good) == 169 and good[-1] == dict(REC_13, temp_c=7.5, **WX),
        (rc, so, len(good), good[-1]),
    )

    # ---------------------------------------------------------------- the default output directory, and the pinned flat layout
    fake.set("ok", CANNED)
    cwd = os.path.join(tmp, "cwd")
    os.makedirs(cwd)
    rc, so, se = run(["--geo-config", geo, "--api-url", fake.url, "--now", str(NOW), "--backfill-days", "1"], drop_env=("BMA_DATA_DIR",), cwd=cwd)
    check(
        "default --out without BMA_DATA_DIR is ./data/history (the convention of the other tools)",
        rc == 0 and os.path.exists(os.path.join(cwd, "data", "history", "ambient_temp.jsonl")),
        (rc, se),
    )
    dd = os.path.join(tmp, "datadir")
    rc, so, se = run(["--geo-config", geo, "--api-url", fake.url, "--now", str(NOW), "--backfill-days", "1"], env_extra={"BMA_DATA_DIR": dd}, cwd=cwd)
    check("default --out with BMA_DATA_DIR set is <data-dir>/history", rc == 0 and len(lines(os.path.join(dd, "history", "ambient_temp.jsonl"))) == 3, (rc, se))
    flat = os.path.join(tmp, "flat_app")
    os.makedirs(flat)
    shutil.copy(TOOL, flat)
    shutil.copy(HISTORY_LOG, flat)
    rc, so, se = run(
        ["--geo-config", geo, "--out", os.path.join(tmp, "flat_out"), "--api-url", fake.url, "--now", str(NOW), "--backfill-days", "1"],
        tool=os.path.join(flat, "ambient_temp.py"),
    )
    check(
        "pinned app layout (tool and history_log.py side by side, no repo): runs and writes 3 records",
        rc == 0 and len(lines(os.path.join(tmp, "flat_out", "ambient_temp.jsonl"))) == 3,
        (rc, se),
    )

    # ---------------------------------------------------------------- hostile input: one guard per case, each with a positive twin
    def hourly(times, temps, wind=None, precip=None, cloud=None):
        n = len(times)
        return {
            "hourly": {
                "time": times,
                "temperature_2m": temps,
                "wind_speed_10m": wind if wind is not None else [1.0] * n,
                "precipitation": precip if precip is not None else [0.0] * n,
                "cloud_cover": cloud if cloud is not None else [50.0] * n,
            }
        }

    def rec_of(out):
        return lines(os.path.join(out, "ambient_temp.jsonl")) if os.path.exists(os.path.join(out, "ambient_temp.jsonl")) else None

    # timestamp not on an hour boundary: counted as a bad value, never written (the schema says multipleOf 3600)
    fake.set("ok", hourly(["2026-10-26T10:30", "2026-10-26T11:00"], [3.0, 4.0]))
    rc, so, se = go(os.path.join(tmp, "h_half"))
    check(
        "a timestamp on :30 is skipped (bad_values=1), the on-the-hour one beside it is written",
        rc == 0
        and se == "ambient_temp: failures: bad_values=1\n"
        and rec_of(os.path.join(tmp, "h_half")) == [dict(REC_10, t_hour_utc=epoch(2026, 10, 26, 11), temp_c=4.0, **WX)],
        (rc, se, rec_of(os.path.join(tmp, "h_half"))),
    )
    # unequal lengths: bad_json, nothing written
    fake.set("ok", hourly(["2026-10-26T10:00", "2026-10-26T11:00", "2026-10-26T12:00"], [1.0, 2.0]))
    rc, so, se = go(os.path.join(tmp, "h_len"))
    check(
        "time and temperature lists of different length: bad_json=1 and NOTHING written (not even the pairs that line up)",
        rc == 0 and se == "ambient_temp: failures: bad_json=1\n" and rec_of(os.path.join(tmp, "h_len")) is None,
        (rc, se),
    )
    fake.set("ok", hourly(["2026-10-26T10:00", "2026-10-26T11:00", "2026-10-26T12:00"], [1.0, 2.0, 3.0]))
    rc, so, se = go(os.path.join(tmp, "h_len2"))
    check(
        "positive twin: the same three hours with three temperatures write three records and no failure line",
        rc == 0 and se == "" and len(rec_of(os.path.join(tmp, "h_len2"))) == 3,
        (rc, se),
    )
    # an oversize body is rejected even when it is complete valid JSON (MAX_BODY is 1 MiB)
    base = json.dumps(hourly(["2026-10-26T10:00"], [6.5])).encode()
    for size, label, exp_se, exp_recs in (
        (1048577, "1 MiB + 1 byte", "ambient_temp: failures: bad_json=1\n", None),
        (1048576, "exactly 1 MiB", "", [dict(REC_10, temp_c=6.5, **WX)]),
    ):
        fake.set("ok", base + b" " * (size - len(base)))
        oo = os.path.join(tmp, "h_big_%d" % size)
        rc, so, se = go(oo)
        check(
            "body of %s of valid JSON: %s" % (label, "rejected as bad_json, nothing written" if exp_recs is None else "accepted, one record"),
            rc == 0 and se == exp_se and rec_of(oo) == exp_recs,
            (rc, se, rec_of(oo)),
        )
    # a slow drip is cut off by the tool's own deadline (2 x --timeout-s), not by the supervisor's kill
    fake.release.clear()
    fake.set("drip")
    t0 = time.time()
    rc, so, se = go(os.path.join(tmp, "h_drip"), extra=["--timeout-s", "1"])
    el = time.time() - t0
    fake.release.set()
    check(
        "a body dripping one byte per 0.3 s: the run ends by itself near 2 x timeout (under 6 s) with timeouts=1 and nothing written",
        rc == 0 and se == "ambient_temp: failures: timeouts=1\n" and el < 6 and rec_of(os.path.join(tmp, "h_drip")) is None,
        (rc, se, el),
    )
    fake.release.clear()
    # an internal error never crashes the supervisor: --now inf makes int() raise inside the run, the outer guard prints one line, exit 0
    fake.set("ok", CANNED)
    rc, so, se = go(os.path.join(tmp, "h_err"), now="inf")
    check(
        "internal error (--now inf): exit 0, stdout empty, one stderr line naming only the exception type, no request, no file",
        rc == 0
        and so == ""
        and se == "ambient_temp: unexpected error: ValueError\n"
        and len(fake.requests) == 0
        and rec_of(os.path.join(tmp, "h_err")) is None,
        (rc, so, se),
    )
    rc, so, se = go(os.path.join(tmp, "h_err2"))
    check(
        "positive twin: a finite --now on the same server makes the request and writes 3 records",
        rc == 0 and len(fake.requests) == 1 and len(rec_of(os.path.join(tmp, "h_err2"))) == 3,
        (rc, se),
    )
    # a geo config written by PowerShell's Set-Content -Encoding UTF8 starts with a BOM
    gbom = os.path.join(tmp, "geo_bom.json")
    with open(gbom, "wb") as f:
        f.write(b"\xef\xbb\xbf" + json.dumps({"lat": LAT, "lon": LON}).encode())
    fake.set("ok", CANNED)
    rc, so, se = go(os.path.join(tmp, "h_bom"), geo_path=gbom)
    check(
        "geo config with a UTF-8 BOM is accepted: one request with the rounded coordinates, 3 records",
        rc == 0 and len(fake.requests) == 1 and "latitude=12.3&longitude=56.8&" in fake.requests[0]["path"] and len(rec_of(os.path.join(tmp, "h_bom"))) == 3,
        (rc, se, fake.requests),
    )
    # one oversized integer among the temperatures costs one value, not the run
    fake.set(
        "ok",
        b'{"hourly":{"time":["2026-10-26T09:00","2026-10-26T10:00","2026-10-26T11:00"],"wind_speed_10m":[1,1,1],"precipitation":[0,0,0],"cloud_cover":[50,50,50],"temperature_2m":[1.5,1'
        + b"0" * 400
        + b",2.5]}}",
    )
    oo = os.path.join(tmp, "h_huge")
    rc, so, se = go(oo)
    check(
        "a 401-digit integer temperature: skipped as bad_values=1, the two other hours are written exactly",
        rc == 0
        and se == "ambient_temp: failures: bad_values=1\n"
        and rec_of(oo)
        == [dict(REC_10, t_hour_utc=epoch(2026, 10, 26, 9), temp_c=1.5, **WX), dict(REC_10, t_hour_utc=epoch(2026, 10, 26, 11), temp_c=2.5, **WX)],
        (rc, se, rec_of(oo)),
    )
    # deep nesting: in the API response (bad_json) and in the existing history file (the damaged line is skipped, the run still writes)
    fake.set("ok", b"[" * 200000)
    rc, so, se = go(os.path.join(tmp, "h_nest"))
    check(
        "a 200000-deep nested response: bad_json=1, exit 0, nothing written",
        rc == 0 and se == "ambient_temp: failures: bad_json=1\n" and rec_of(os.path.join(tmp, "h_nest")) is None,
        (rc, se),
    )
    fake.set("ok", CANNED)
    on = os.path.join(tmp, "h_nestfile")
    os.makedirs(on)
    with open(os.path.join(on, "ambient_temp.jsonl"), "wb") as f:
        f.write(b"[" * 200000 + b"\n")
    rc, so, se = go(on)
    good = [json.loads(x) for x in raw(os.path.join(on, "ambient_temp.jsonl")).splitlines() if x.startswith(b"{")]
    check(
        "a deeply nested line in the existing history file is skipped: 3 records still written, only the usual bad_values line",
        rc == 0 and se == CANNED_ERR and so.startswith("ambient_temp: wrote 3, ") and good == [REC_OLDEST, REC_10, REC_12],
        (rc, so, se, good),
    )

    # ---------------------------------------------------------------- dedupe also reads the .1 backup (an hour that exists only there)
    ob = os.path.join(tmp, "h_backup")
    os.makedirs(ob)
    with open(os.path.join(ob, "ambient_temp.jsonl.1"), "wb") as f:
        f.write(b"".join(json.dumps(r, separators=(",", ":")).encode() + b"\n" for r in (REC_OLDEST, REC_10)))
    fake.set("ok", CANNED)
    rc, so, se = go(ob)
    check(
        "hours present only in ambient_temp.jsonl.1 are not written again: only 12:00 lands in the (new) current file",
        rc == 0 and so.startswith("ambient_temp: wrote 1, already present 2, ") and rec_of(ob) == [REC_12],
        (rc, so, rec_of(ob)),
    )
    ob2 = os.path.join(tmp, "h_backup2")
    os.makedirs(ob2)
    with open(os.path.join(ob2, "ambient_temp.jsonl.1"), "wb") as f:
        f.write(json.dumps(REC_PRE, separators=(",", ":")).encode() + b"\n")  # an hour the response does not need (older than the window)
    rc, so, se = go(ob2)
    check(
        "positive twin: a backup holding none of the response's hours leaves all 3 to be written",
        rc == 0 and so.startswith("ambient_temp: wrote 3, already present 0, ") and rec_of(ob2) == [REC_OLDEST, REC_10, REC_12],
        (rc, so, rec_of(ob2)),
    )

    # ---------------------------------------------------------------- weather covariates (ambient-temp 1.1.0): per-field handling
    H1 = ["2026-10-26T10:00"]
    base10 = {"k": "ambient_temp", "v": 1, "t_hour_utc": epoch(2026, 10, 26, 10), "temp_c": 6.0, "source": "open-meteo", "model_grid_km": None}
    fake.set("ok", hourly(H1, [6.0], wind=[7.5], precip=[2.25], cloud=[80]))
    oc = os.path.join(tmp, "c_ok")
    rc, so, se = go(oc)
    check(
        "covariates: valid wind 7.5, precip 2.25, cloud 80 are written as floats, no failure line (positive twin of the per-field cases)",
        rc == 0 and se == "" and rec_of(oc) == [dict(base10, wind_ms=7.5, precip_mm=2.25, cloud_pct=80.0)],
        (rc, se, rec_of(oc)),
    )
    check(
        "covariates: the record line is compact JSON in the fixed field order",
        raw(os.path.join(oc, "ambient_temp.jsonl"))
        == b'{"k":"ambient_temp","v":1,"t_hour_utc":1793008800,"temp_c":6.0,"source":"open-meteo","model_grid_km":null,"wind_ms":7.5,"precip_mm":2.25,"cloud_pct":80.0}\n',
        raw(os.path.join(oc, "ambient_temp.jsonl")),
    )
    cases = (
        ("wind_ms", "bad_wind", "wind", 150.0, [None, float("nan"), "7", True, -0.1, 150.5, 10**400]),
        ("precip_mm", "bad_precip", "precip", 500.0, [None, float("nan"), "2", True, -0.1, 500.5, 10**400]),
        ("cloud_pct", "bad_cloud", "cloud", 100.0, [None, float("nan"), "8", True, -0.1, 100.5, 10**400]),
    )
    n_c = 0
    for field, key, kw, top, bads in cases:
        full = {"wind": [7.5], "precip": [2.25], "cloud": [80]}
        for bad in bads:
            args = dict(full)
            args[kw] = [bad]
            fake.set("ok", hourly(H1, [6.0], **args))
            n_c += 1
            oc = os.path.join(tmp, "c_%d" % n_c)
            rc, so, se = go(oc)
            exp = dict(base10, wind_ms=7.5, precip_mm=2.25, cloud_pct=80.0)
            del exp[field]
            check(
                "covariate %s = %s: only that field is left out, the record and the other two are written, counted as %s=1" % (field, repr(bad)[:12], key),
                rc == 0 and se == "ambient_temp: failures: %s=1\n" % key and rec_of(oc) == [exp],
                (rc, se, rec_of(oc)),
            )
        for v in (0.0, top):
            args = dict(full)
            args[kw] = [v]
            fake.set("ok", hourly(H1, [6.0], **args))
            n_c += 1
            oc = os.path.join(tmp, "c_%d" % n_c)
            rc, so, se = go(oc)
            check(
                "covariate %s at the bound %s is valid (positive twin of the out-of-range cases)" % (field, v),
                rc == 0 and se == "" and rec_of(oc)[0][field] == v and len(rec_of(oc)[0]) == 9,
                (rc, se, rec_of(oc)),
            )
    # a response without the covariate lists (or with a list of another length): the record has none of them, counted for every hour
    two = ["2026-10-26T09:00", "2026-10-26T10:00"]
    fake.set("ok", {"hourly": {"time": two, "temperature_2m": [5.0, 6.0]}})
    oc = os.path.join(tmp, "c_absent")
    rc, so, se = go(oc)
    check(
        "no covariate lists in the response: two plain records without covariates, bad_cloud=2 bad_precip=2 bad_wind=2",
        rc == 0
        and se == "ambient_temp: failures: bad_cloud=2 bad_precip=2 bad_wind=2\n"
        and rec_of(oc) == [dict(base10, t_hour_utc=epoch(2026, 10, 26, 9), temp_c=5.0), base10],
        (rc, se, rec_of(oc)),
    )
    fake.set("ok", hourly(two, [5.0, 6.0], wind=[1.0, 2.0, 3.0], precip=[0.5], cloud=[10, 20]))
    oc = os.path.join(tmp, "c_len")
    rc, so, se = go(oc)
    check(
        "covariate lists of the wrong length (wind 3, precip 1 for 2 hours): both dropped for both hours, cloud (2) is kept, temperature unaffected",
        rc == 0
        and se == "ambient_temp: failures: bad_precip=2 bad_wind=2\n"
        and rec_of(oc) == [dict(base10, t_hour_utc=epoch(2026, 10, 26, 9), temp_c=5.0, cloud_pct=10.0), dict(base10, cloud_pct=20.0)],
        (rc, se, rec_of(oc)),
    )
    # an invalid temperature drops the hour whatever the covariates are (they are not counted for it)
    fake.set("ok", hourly(two, [None, 6.0], wind=[1.0, 2.0], precip=[0.5, 1.0], cloud=[10, 20]))
    oc = os.path.join(tmp, "c_tempbad")
    rc, so, se = go(oc)
    check(
        "null temperature with valid covariates: the hour is not written (bad_values=1), the next hour is written with its covariates",
        rc == 0 and se == "ambient_temp: failures: bad_values=1\n" and rec_of(oc) == [dict(base10, wind_ms=2.0, precip_mm=1.0, cloud_pct=20.0)],
        (rc, se, rec_of(oc)),
    )
    # an old (1.0.0) record is kept byte for byte and its hour is not written again; new hours get covariates
    oo = os.path.join(tmp, "c_old")
    os.makedirs(oo)
    old_line = b'{"k":"ambient_temp","v":1,"t_hour_utc":1793008800,"temp_c":6.0,"source":"open-meteo","model_grid_km":null}\n'
    with open(os.path.join(oo, "ambient_temp.jsonl"), "wb") as f:
        f.write(old_line)
    fake.set("ok", hourly(two, [5.0, 6.5], wind=[1.5, 2.0], precip=[0.0, 0.0], cloud=[10, 20]))
    rc, so, se = go(oo)
    check(
        "an old-format record for 10:00 is not rewritten and not duplicated (already present 1); the new 09:00 hour is appended with its covariates",
        rc == 0
        and so.startswith("ambient_temp: wrote 1, already present 1, ")
        and raw(os.path.join(oo, "ambient_temp.jsonl"))
        == old_line
        + b'{"k":"ambient_temp","v":1,"t_hour_utc":1793005200,"temp_c":5.0,"source":"open-meteo","model_grid_km":null,"wind_ms":1.5,"precip_mm":0.0,"cloud_pct":10.0}\n',
        (rc, so, raw(os.path.join(oo, "ambient_temp.jsonl"))),
    )
    # privacy over the covariate runs: no location key and no coordinate in any record
    txt = "".join(
        raw(os.path.join(tmp, d, "ambient_temp.jsonl")).decode()
        for d in sorted(os.listdir(tmp))
        if d.startswith("c_") and os.path.exists(os.path.join(tmp, d, "ambient_temp.jsonl"))
    )
    check(
        "privacy: no record of the covariate runs has a lat / lon / latitude / longitude key or a coordinate",
        len(txt) > 500 and not any(s_ in txt for s_ in ('"lat', '"lon', "12.34", "56.78", "12.3,", "56.8,")),
        txt[:200],
    )

    # ---------------------------------------------------------------- usage errors are the only nonzero exit
    rc, so, se = run(["--out", os.path.join(tmp, "u")])
    check("usage error (no --geo-config): exit 2", rc == 2 and "--geo-config" in se, (rc, se))
    rc, so, se = run(["--geo-config", geo, "--backfill-days", "500"])
    check(
        "usage error (--backfill-days 500, over the API's 92): exit 2, and the geo path is not echoed",
        rc == 2 and "backfill-days" in se and geo not in se,
        (rc, se),
    )
    rc, so, se = go(os.path.join(tmp, "once"), extra=["--once"])
    check("positive twin: --once is accepted and behaves as the default run", rc == 0, (rc, se))
finally:
    fake.close()
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{n_checks} checks, {len(fails)} failure(s)")
for f in fails:
    print("  FAILED:", f)
sys.exit(1 if fails else 0)
