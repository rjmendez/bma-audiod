contract: bridge-status
contract_version: 1.0.0
status: stable
producer: pc/dante_bridge.py (`--status-file`, default <data-dir>/bridge_status.json)
consumers: pc/bma_cable_sink.py (data track kind 3), analysis/summarize.py
schema: schemas/bridge_status.schema.json; example: fixtures/bridge_status.json

# bridge_status.json

A single JSON object, replaced atomically (`<file>.tmp`, then rename) every `--stats-s` seconds (30 s in production).

* Last interval: `pps`, `loss_pct`, `blocks_all_zero_frac`, `state`.
* Cumulative since process start: `pkts`, `gaps`, `backwards`, `bad`, `blocks`, `uptime_s`. A restart resets every counter.
* `state`: `ok`; `NOPKT` = no packet arrived in the interval; `UNFED` = more than 99.9% of blocks were all-zero.
* 8-channel beam flow: `pps` 3000, `loss_pct` below 1, `nch` 8.
* `targets` lists only the `--to` targets (the classifier); the `--all-to` fan-out is not reported.

## Compatibility

Fields may be added in a minor version; readers ignore unknown fields. `state` is a closed enum: adding a value is MINOR and readers MUST treat an unknown state as "not ok".
