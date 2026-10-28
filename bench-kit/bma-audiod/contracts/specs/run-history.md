contract: run-history
contract_version: 1.0.0
status: stable
producers: pc/bma_cable_sink.py (sink_history), analysis/host.py + analysis/analyzer_history.py (analyzer_history), tools/bma_debug_rx.py (board_history), pc/run_pipeline.ps1 + pc/run_ledger.ps1 (run_ledger)
consumers: offline analysis
schema: schemas/run_history.schema.json; examples: fixtures/sink_history.jsonl, analyzer_history.jsonl, board_history.jsonl, run_ledger.jsonl (regenerate with tools/make_history_fixtures.py)

# Run history

Files live in `<data-dir>/history/`, apart from `analysis/`.

Rules for every file:

* JSON Lines, append-only, one compact object per line (`separators=(",",":")`), LF, UTF-8 without BOM, discriminated by `k`; every record has `v` (1) and `t` (Unix seconds, PC clock).
* Metadata only: no audio, no location.
* Bounded: each file rotates at its cap to `FILE.1` (one backup, disk use <= 2 x cap): sink_history 32 MB, analyzer_history 48 MB, board_history 16 MB, run_ledger 8 MB (PowerShell helper).
* Rotation never stops a writer: rename first; if refused, copy-to-`FILE.1` + truncate in place (a tailer sees the size drop and restarts at 0); if that fails, keep appending and retry after another cap.
* Failures are counted by the producer (`write_errors`, `rotate_fallbacks`), never raised.
* Counters are cumulative since the producer started. A reader takes deltas and treats a changed `started_t` (sink) or `started` (analyzer) as a counter reset, never a negative delta.
* Readers MUST ignore unknown fields and unknown `k`.

| file | `k` | cadence |
|---|---|---|
| sink_history.jsonl | sink_sample | every 30 s (`--stats-s`) |
| analyzer_history.jsonl | analyzer_sample | every 30 s |
| board_history.jsonl | board_sample, board_boot | one sample per board @health record (60 s); board_boot at each reboot |
| run_ledger.jsonl | run_event | every supervised process start and exit |

## sink_sample

`sink_status.json` counters:

* `holes`, `slips`, `slipped_frames`, `underruns`, `missing_slot_blocks`, `data_dropped`, `data_frames`, `datagrams`, `bad`.
* Also `playing`, `audio_output`, `device_error`, `buffered_ms`, `started_t` (sink process start). `sink_status.json` keeps its own contract (sink-status 1.2.0).

## analyzer_sample

Compact sample of `analysis\status.json` every 30 s: `rx` {datagrams, bad, gaps}, `contiguous_s`, `embeddings_stored`, `device`, `plugins` as `{name: [windows, records, errors, dropped]}` for enabled plugins with counters.

* `rx.gaps` = SUM of the eight per-ring gap counters: divide by 8 for loss events.
* `dropped` = windows skipped because the analyzer fell behind.

## board_sample and board_boot

From the `type: health` records `tools/bma_debug_rx.py` already receives (no board write).

* `up` = board uptime s; `boot_t = t - up` = PC-clock time of the boot at that sample. The board clock free-runs: NEVER compare `boot_t` exactly.
* `boot_id = int(boot_t)` at the first sample of the boot, fixed thereafter.
* A new boot starts (a `board_boot` is written just before its first sample) when:
  1. uptime falls by more than 1 s against the previous sample; or
  2. on the first sample after the collector restarted, `boot_t` differs from the last stored `boot_t` by more than 120 s; or
  3. it is the first record ever (`prev_*` and `gap_s` null).
* `board_boot.prev_last_up` = last uptime of the previous boot; `gap_s` = time between the two samples.
* Unreadable sensors: `temp_*_c: null`. A health record with no numeric `up` is not written (counted in the collector's end-of-run summary).

## run_event

Written by `run_pipeline.ps1` through `run_ledger.ps1`:

* `proc` (analysis, classifier, bridge, board_debug, sink, ui, supervisor, ambient).
* `ev` (`start`, `exit`, `supervisor_up`, `suppressed`).
* `pid`, `exit_code`, `exit_code_known`, `uptime_s` (on exit), `version` (first line of `app\VERSION` when written).

* `exit_code` null with `exit_code_known: false` when Windows did not report one. `0` is a real code. WSL exits carry wsl.exe's code (127 = command not found inside the distro).
* Crash-loop guard: at most 60 `start`/`exit` events per process per UTC hour are written; further ones are counted and summarised by one `suppressed` record (`count` = events dropped, `t` = end of that hour). `supervisor_up` is never suppressed.

## Versioning

Adding a field or kind is minor; readers ignore what they do not know. Producers and this schema change in the same commit (CHANGELOG entry, fixtures regenerated).
