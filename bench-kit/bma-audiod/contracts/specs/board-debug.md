contract: board-debug
contract_version: 1.0.0
status: stable
consumers: pc/bma_cable_sink.py (data track kind 2), analysis/summarize.py, offline analysis
schema: schemas/board_debug.schema.json; examples: fixtures/board_debug.jsonl

# board_debug.jsonl

JSON Lines, UTF-8, one object per line, appended to `<data-dir>/analysis/board_debug.jsonl` (rotated at 64 MB). Every record has `type` and `host_t` (PC Unix seconds at receipt). `ms` (meter) and `t` (monitor) are board ticks, not wall-clock.

| `type` | what | cadence |
|---|---|---|
| `meter` | FPGA meter registers, selector -> 4-hex-digit value | 2 per second |
| `monitor` | one raw-receive level line from a DSP: DSPs 4-9 x slots 0-6 (42 lines per sweep) | sweep every 30 s |
| `health` | board housekeeping (temps, load, FPGA/GPIO/LED state, NIC counters, route state) | 60 s |
| `status` | daemon status; `json` is a JSON-encoded STRING (parse it again) | 2 per second |
| `health_unparsed` | an `@health` line that was not valid JSON | rare |

## Rules

* `monitor` levels are relative to 24-bit full scale: `peak_dbfs = 20 log10(peak/2^23)`, `rms_dbfs = 10 log10(meansq/2^46)`.
* Slot 4 is not an audio channel: `rms_dbfs` is null for it (6 of 42 lines per sweep; 36 audio channels). `rms_dbfs` is also null when `meansq` is 0.
* A monitor line that is not five integers is kept as `{type, seq, line, host_t}` only.
* `health` carries BOTH `k` (board) and `type` (collector). The collector adds `seq`, `temp_local_c`, `temp_remote_c` (float32 decoded from the `*_hex` fields, null if not decodable).
* `eth0` has five comma-separated counters, `eth1` three, as the board prints them.
* Records from 2026-10-11 16:01-16:22 have empty `*_hex` strings and null temperatures.
* `health_unparsed`: a few hundred NUL bytes followed by the intact JSON object. A reader may recover it by stripping NULs and parsing from the first `{`.
* `status` inner object: right after daemon start it is only `{"state":"starting"}`; afterwards all five fields.
* New JSONL interfaces MUST discriminate with `type`, not `k`.

## Compatibility

Readers ignore unknown fields. A new `type` is a MINOR addition with a schema `$defs` entry and a fixture line in the same commit; the checker reports an unknown `type` as drift until then.
