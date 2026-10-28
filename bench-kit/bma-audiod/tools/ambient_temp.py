#!/usr/bin/env python3
"""Ambient air temperature feed (contract: ambient-temp, contracts/specs/ambient-temp.md). Standard library only.

  ambient_temp.py --geo-config PATH [--out DIR] [--backfill-days 7] [--timeout-s 15] [--once]

Fetches the hourly 2 m air temperature (plus optional wind_speed_10m m/s, precipitation mm, cloud_cover %) of the last days from the Open-Meteo forecast API and appends one record per COMPLETE
past UTC hour to <out>/ambient_temp.jsonl (default <data-dir>/history), through the shared size-capped writer (pc/history_log.py
RotatingJsonl). An hour is never written twice (the existing file and its backup are read first); a later run fills hours that
were missing, back to --backfill-days. Future hours (the forecast_days=1 response holds the rest of today) are never written.

PRIVACY: the location comes from the county-level geo config (lat / lon keys, classify/geo_county.example.json). Both numbers
are rounded to 0.1 degree (about 11 km) BEFORE the request. Neither the original nor the rounded coordinates, nor the path of the
geo file, appear in any output file, stdout/stderr line or error message; the request carries nothing but those two numbers, the
fixed variable names and a generic User-Agent.

Exit code: 0 for everything except a usage error (2): no geo config, network error, timeout, HTTP error, garbage or null/NaN values
are counted on one stderr line and never fail the run (the supervisor runs this hourly and must not crash-loop).
--once is accepted for the supervisor and is the only behaviour: run once and exit.

Test plumbing, not for production: --api-url (default: the real API) points at a local fake server; --now fixes the clock.
"""

import argparse, calendar, json, math, os, sys, time, urllib.error, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # -I (isolated mode) does not add the script directory
sys.path.insert(1, os.path.join(HERE, "..", "pc"))  # in the repo history_log.py lives in pc/; pinned app\ is flat
from history_log import RotatingJsonl, HISTORY_DIR  # noqa: E402

API_URL = "https://api.open-meteo.com/v1/forecast"
USER_AGENT = "ambient-temp/1"
FILE_NAME = "ambient_temp.jsonl"
CAP_BYTES = 2 * 1024 * 1024  # about 19000 records per generation (110 B each), 2 generations: years of hours
MAX_BACKFILL_DAYS = 92  # the API's past_days limit
MAX_BODY = 1 << 20  # a response larger than this is garbage (7 days of hourly data is about 6 KB)
TEMP_MIN, TEMP_MAX = -90.0, 60.0  # outside this is not an air temperature on Earth: counted as a bad value
# optional weather covariates (ambient-temp 1.1.0): API variable, record field, count key, minimum, maximum. A value that is null,
# NaN, non-numeric or out of range is left out of the record (the record is still written) and counted under its own key.
COVARIATES = (
    ("wind_speed_10m", "wind_ms", "bad_wind", 0.0, 150.0),
    ("precipitation", "precip_mm", "bad_precip", 0.0, 500.0),
    ("cloud_cover", "cloud_pct", "bad_cloud", 0.0, 100.0),
)
NO_GEO = "no geo config: ambient temperature off"


def finite_number(v):
    """True for a real (non-bool) int / float that is finite. A JSON integer too large for a float (10**400) is not finite here:
    math.isfinite raises OverflowError for it, which must not take the whole run down."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    try:
        return math.isfinite(v)
    except OverflowError:
        return False


def round_coord(x):
    """0.1 degree. Formatted with one decimal, so -0.04 gives '0.0' not '-0.0'."""
    r = round(float(x), 1)
    return "%.1f" % (r + 0.0)


def read_location(path):
    """-> (lat, lon) floats from the geo config, or None when the file is absent, unreadable or has no usable lat/lon
    (missing, not a finite number in range, or the example placeholder 0.0 / 0.0)."""
    try:
        with open(path, "rb") as f:
            j = json.loads(f.read(1 << 16).decode("utf-8-sig"))
        lat, lon = j["lat"], j["lon"]
    except (OSError, ValueError, KeyError, TypeError, RecursionError):  # RecursionError: a deeply nested file
        return None
    for v in (lat, lon):
        if not finite_number(v):
            return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0) or (lat == 0.0 and lon == 0.0):
        return None
    return float(lat), float(lon)


def build_url(api_url, lat, lon, past_days):
    q = [
        ("latitude", round_coord(lat)),
        ("longitude", round_coord(lon)),
        ("hourly", "temperature_2m,wind_speed_10m,precipitation,cloud_cover"),
        ("wind_speed_unit", "ms"),
        ("past_days", str(past_days)),
        ("forecast_days", "1"),
        ("timezone", "UTC"),
    ]
    return api_url + "?" + urllib.parse.urlencode(q)


def fetch(url, timeout_s, counts):
    """-> parsed JSON object or None (the failure is counted, its text never kept: it could carry the URL)."""
    host = urllib.parse.urlsplit(url).hostname or ""
    # a loopback server (the tests) is never reached through a system proxy; the real API follows the system settings
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if host in ("127.0.0.1", "localhost", "::1") else urllib.request.build_opener()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    deadline = time.monotonic() + timeout_s * 2
    try:
        with opener.open(req, timeout=timeout_s) as r:
            body = b""
            while len(body) <= MAX_BODY:
                if time.monotonic() > deadline:
                    counts["timeouts"] += 1
                    return None
                chunk = r.read1(4096)  # read(n) waits for n bytes: a slow drip would never return to the deadline check
                if not chunk:
                    break
                body += chunk
    except urllib.error.HTTPError:
        counts["http_errors"] += 1
        return None
    except (TimeoutError, OSError) as ex:  # URLError, ConnectionError and socket.timeout are OSErrors
        reason = getattr(ex, "reason", ex)
        counts["timeouts" if isinstance(reason, TimeoutError) or isinstance(ex, TimeoutError) else "network_errors"] += 1
        return None
    except Exception:  # noqa: BLE001  http.client errors (IncompleteRead, BadStatusLine)
        counts["network_errors"] += 1
        return None
    if len(body) > MAX_BODY:
        counts["bad_json"] += 1
        return None
    try:
        j = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        counts["bad_json"] += 1
        return None
    if not isinstance(j, dict):
        counts["bad_json"] += 1
        return None
    return j


def parse_hour(s):
    """'2026-10-26T13:00' (UTC) -> epoch seconds of the hour start, or None."""
    try:
        t = calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M"))
    except (ValueError, TypeError):
        return None
    return t if t % 3600 == 0 else None


def parse_hourly(j, counts):
    """-> [(hour_epoch, temp_c, {field: value})] of the entries with a valid hour and temperature; null / NaN / out-of-range /
    unparseable ones are counted, not returned. The dict holds the covariates that are valid for that hour; an invalid one is counted
    under its own key and left out. A covariate list that is absent, not a list or of another length than `time` makes every hour
    count as invalid for that covariate."""
    try:
        h = j["hourly"]
        times, temps = h["time"], h["temperature_2m"]
        if not isinstance(times, list) or not isinstance(temps, list) or len(times) != len(temps):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        counts["bad_json"] += 1
        return []
    cov = []
    for api, field, key, lo, hi in COVARIATES:
        vals = h.get(api)
        cov.append((field, key, lo, hi, vals if isinstance(vals, list) and len(vals) == len(times) else [None] * len(times)))
    out = []
    for n, (s, v) in enumerate(zip(times, temps)):
        t = parse_hour(s)
        if t is None or not finite_number(v) or not TEMP_MIN <= v <= TEMP_MAX:
            counts["bad_values"] += 1
            continue
        extra = {}
        for field, key, lo, hi, vals in cov:
            x = vals[n]
            if finite_number(x) and lo <= x <= hi:
                extra[field] = float(x)
            else:
                counts[key] += 1
        out.append((t, float(v), extra))
    return out


def known_hours(path):
    """Hours already in the file or its backup (a damaged line is skipped)."""
    seen = set()
    for p in (path + ".1", path):
        try:
            with open(p, "rb") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                        if r.get("k") == "ambient_temp" and isinstance(r.get("t_hour_utc"), int):
                            seen.add(r["t_hour_utc"])
                    except (ValueError, AttributeError, RecursionError):
                        pass
        except OSError:
            pass
    return seen


def make_record(hour, temp_c, extra=None):
    r = {"k": "ambient_temp", "v": 1, "t_hour_utc": hour, "temp_c": temp_c, "source": "open-meteo", "model_grid_km": None}
    for _, field, _, _, _ in COVARIATES:  # fixed order; only the valid ones
        if extra and field in extra:
            r[field] = extra[field]
    return r


def run(a):
    loc = read_location(a.geo_config)
    if loc is None:
        print(NO_GEO)
        return 0
    counts = {
        "timeouts": 0,
        "network_errors": 0,
        "http_errors": 0,
        "bad_json": 0,
        "bad_values": 0,
        "write_errors": 0,
        "bad_wind": 0,
        "bad_precip": 0,
        "bad_cloud": 0,
    }
    now = time.time() if a.now is None else a.now
    cur_hour = int(now // 3600) * 3600
    oldest = cur_hour - a.backfill_days * 86400
    path = os.path.join(a.out, FILE_NAME)
    j = fetch(build_url(a.api_url, loc[0], loc[1], a.backfill_days), a.timeout_s, counts)
    written = dup = future = old = 0
    if j is not None:
        rows = parse_hourly(j, counts)
        seen = known_hours(path)
        log = RotatingJsonl(path, a.max_file_bytes)
        for hour, temp, extra in sorted(rows, key=lambda r: (r[0], r[1])):
            if hour + 3600 > now:  # the hour is not complete yet
                future += 1
            elif hour < oldest:
                old += 1
            elif hour in seen:
                dup += 1
            else:
                if log.write(make_record(hour, temp, extra)):
                    seen.add(hour)
                    written += 1
        counts["write_errors"] = log.write_errors
    print("ambient_temp: wrote %d, already present %d, future %d, older than the backfill window %d" % (written, dup, future, old))
    failed = {k: v for k, v in counts.items() if v}
    if failed:
        print("ambient_temp: failures: " + " ".join("%s=%d" % kv for kv in sorted(failed.items())), file=sys.stderr)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--geo-config", required=True, help="county-level geo config (lat, lon); absent or without lat/lon: the feed is off")
    ap.add_argument("--out", default=HISTORY_DIR, help="directory of the history files (default <data-dir>/history, data-dir = $BMA_DATA_DIR or ./data)")
    ap.add_argument("--backfill-days", type=int, default=7, help="fill missing past hours back this many days (0..%d, default 7)" % MAX_BACKFILL_DAYS)
    ap.add_argument("--timeout-s", type=float, default=15.0)
    ap.add_argument("--max-file-bytes", type=int, default=CAP_BYTES, help="size cap of ambient_temp.jsonl before it rotates to .1")
    ap.add_argument("--once", action="store_true", help="run once and exit (the only behaviour; the supervisor schedules it)")
    ap.add_argument("--api-url", default=API_URL, help="TEST PLUMBING: the weather API endpoint (default: Open-Meteo)")
    ap.add_argument("--now", type=float, default=None, help="TEST PLUMBING: the clock, Unix seconds")
    a = ap.parse_args(argv)
    if not 0 <= a.backfill_days <= MAX_BACKFILL_DAYS:
        ap.error("--backfill-days must be 0..%d" % MAX_BACKFILL_DAYS)
    if not 0 < a.timeout_s <= 120 or a.max_file_bytes < 1:
        ap.error("--timeout-s must be 0..120 and --max-file-bytes positive")
    try:
        return run(a)
    except Exception as ex:  # noqa: BLE001  never crash-loop the supervisor
        print("ambient_temp: unexpected error: %s" % type(ex).__name__, file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
