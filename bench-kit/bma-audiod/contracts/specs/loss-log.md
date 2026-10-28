contract: loss-log
contract_version: 1.0.1
status: stable
producer: tools/loss_log.py (supervised by pc/run_pipeline.ps1; writes through pc/history_log.py RotatingJsonl)
consumers: tools/loss_report.py, offline analysis
schema: schemas/loss_log.schema.json; example: fixtures/loss_log.jsonl (regenerate with tools/make_loss_fixtures.py, which runs the real tool)

# loss - packet-loss telemetry over time

File: `<data-dir>/history/loss.jsonl` (rotates to `loss.jsonl.1`, 2 MB x 2). One compact JSON line per tick, LF.

## Inputs (read-only)

| source | file | counters | gauges |
|---|---|---|---|
| bridge | `<data-dir>/bridge_status.json` | `datagrams` = `pkts`, `gaps` | `loss_pct`, `uptime_s` |
| sink | `<data-dir>/sink_status.json` | `datagrams`, `rx_errors` | |
| analyzer | `<data-dir>/analysis/status.json` | `datagrams` = `rx.datagrams`, `bad` = `rx.bad`, `gaps` = `rx.gaps` | `uptime_s` |

## Record

`{"k":"loss","v":1,"t":<host Unix s>,"restart":<bool>,"bridge":S,"sink":S,"analyzer":S}`

S = `{"age_s","reason","span_s","restart", <counters>, <gauges>}`.

* `age_s`: host clock minus file mtime; null when the file is absent.
* `reason`: null | `first` | `missing` | `stale` | `unreadable` | `restart`.
  * null: counters are exact deltas (current minus last good reading of that source), `span_s` is the host seconds between the two readings.
  * `first`: no earlier good reading in this process; counters and `span_s` null, gauges as read.
  * `missing`: no file. `stale`: `age_s` > `--stale-s` (default 60: twice the 30 s status writer interval). `unreadable`: read error, not JSON, not an object, or a counter absent / not a non-negative integer. Counters, `span_s` and gauges null.
  * `restart`: any counter or `uptime_s` lower than the previous good reading; counters and `span_s` null, gauges as read. The new reading becomes the baseline.
* `restart`: S.restart is true exactly when `reason` is `restart`; the top-level `restart` is true when any S.restart is.
* A stale, missing or unreadable tick does not move the baseline: the next good reading spans the gap (`span_s` > tick interval).
* Gauges are values as read, not deltas.

## Tick

`--every-s` (default 30, 1..3600), monotonic schedule, no catch-up burst after a stall. A tick error is one stderr line (`loss_log: tick error: <type>`); the loop continues. A write failure is counted by the writer and dropped.

## Privacy

Records carry counters, ages and times only: no audio, path, address, host name or device name. The schema is closed (`additionalProperties: false`).

## Not in the contract

stderr text; `tools/loss_report.py` output. A new field is a MINOR version with schema, fixture and CHANGELOG in the same commit.
