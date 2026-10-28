contract: ambient-temp
contract_version: 1.1.0
status: stable
producer: tools/ambient_temp.py (hourly from pc/run_pipeline.ps1 when app\geo_county.json exists; writes through pc/history_log.py RotatingJsonl)
consumers: offline analysis, insect-group plausibility checks
schema: schemas/ambient_temp.schema.json; example: fixtures/ambient_temp.jsonl (regenerate with tools/make_ambient_fixtures.py)

# ambient_temp - outdoor air temperature, one record per complete UTC hour

File: `<data-dir>/history/ambient_temp.jsonl` (rotates to `ambient_temp.jsonl.1`; shared writer: specs/run-history.md, analysis-history.md).

## Record

`{"k":"ambient_temp","v":1,"t_hour_utc":<int>,"temp_c":<float>,"source":"open-meteo","model_grid_km":null[,"wind_ms":<float>][,"precip_mm":<float>][,"cloud_pct":<float>]}` per line, compact JSON, LF. The three covariates are optional (1.1.0), in that order, after `model_grid_km`.

* `t_hour_utc`: Unix seconds of the START of the UTC hour (multiple of 3600). Only COMPLETE past hours are written (`t_hour_utc + 3600 <= now`, PC clock).
* `temp_c`: Celsius, -90..60. Null, NaN, non-numeric or out-of-range: skipped and counted, never written.
* `wind_ms`: 10 m wind speed, m/s, 0..150. `precip_mm`: precipitation, mm, 0..500. `cloud_pct`: total cloud cover, %, 0..100.
* Each covariate is checked alone: null, NaN, non-numeric, out-of-range, a missing list or a list whose length differs from `time` omits that field only (record and other fields still written) and counts `bad_wind`, `bad_precip` or `bad_cloud` (one per hour).
* A 1.0.0 record has no covariates and stays valid; readers MUST tolerate their absence. Hours already in the file are never rewritten.
* No sun elevation or other coordinate-derived value is stored.
* `source`: `open-meteo`. `model_grid_km`: integer or null; the producer writes null.
* At most ONE record per `t_hour_utc`: the tool reads `ambient_temp.jsonl` and `.1` first and writes only hours not in them.
* A last line without a newline is isolated by the writer and skipped by readers.
* Size cap `--max-file-bytes`: 2 MB per generation, 4 MB with backup.

## Privacy

* County-level location only. The producer reads `lat` / `lon` from `<app>\geo_county.json` (format of classify/geo_county.example.json) and ROUNDS BOTH TO 0.1 DEGREE BEFORE THE REQUEST.
* Request carries only fixed variable names and `User-Agent: ambient-temp/1`.
* Coordinates (configured or rounded) and the geo file path are NEVER written to a record, tool output (stdout, stderr, `logs\ambient.log`, `logs\ambient.err`), error message or test fixture.
* The schema rejects a record with `lat`, `lon`, `latitude` or `longitude`.
* No usable geo config (file absent, no numeric `lat` / `lon` in range, or placeholder 0.0 / 0.0): the tool prints `no geo config: ambient temperature off`, makes NO network call, exits 0.

## Request

`GET https://api.open-meteo.com/v1/forecast?latitude=<0.1 deg>&longitude=<0.1 deg>&hourly=temperature_2m,wind_speed_10m,precipitation,cloud_cover&wind_speed_unit=ms&past_days=<N>&forecast_days=1&timezone=UTC`

* N = `--backfill-days` (default 7, max 92). One request per run.
* `--timeout-s` (default 15) per socket operation, twice that overall. `--api-url` and `--now` are test plumbing.
* Hours older than N days before the current hour are not written; later runs fill missing hours inside the window.

## Failures

* Network error, timeout, HTTP error, non-JSON body, or a body without equal-length `hourly.time` / `hourly.temperature_2m` lists: counted on ONE stderr line.
* Format: `ambient_temp: failures: bad_cloud=1 bad_json=1 bad_precip=1 bad_values=2 bad_wind=1 http_errors=1 network_errors=1 timeouts=1 write_errors=1` (non-zero only, sorted by name).
* Exit code 0 in every such case; a usage error is 2.
* A hostile document (400-digit integer, deeply nested array, timestamp off an hour boundary, body > 1 MiB) is a counted `bad_values` or `bad_json`, never an abort.
* A slow-dripping body is cut off after 2 x `--timeout-s` (`timeouts`).
* Rotation at the cap is the shared writer's (os.replace, falling back to copy + truncate).

## Reader rule

Event at Unix time `t`:

1. `h = floor(t / 3600) * 3600`.
2. Take the NEAREST PAST record: largest `t_hour_utc <= h`.
3. Use it only if `h - t_hour_utc <= 3 * 3600`; otherwise UNKNOWN: show nothing, do not judge plausibility. Never use a later hour; never interpolate or extrapolate.
4. If a `t_hour_utc` appears twice, the last line wins.

* Read `ambient_temp.jsonl.1` then `ambient_temp.jsonl`; skip lines that do not parse. A missing file = unknown for every event.
* Code: `rec = latest record with t_hour_utc <= h; temp = rec.temp_c if h - rec.t_hour_utc <= 10800 else unknown`.

## Not in the contract

`logs\ambient.log`, `ambient.err` text and the stdout summary line. Readers ignore unknown fields; a new field is MINOR (schema, fixture, CHANGELOG in the same commit).
