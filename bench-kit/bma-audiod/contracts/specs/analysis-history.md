contract: analysis-history
contract_version: 1.1.0
status: stable
producer: analysis/host.py with analysis/runmeta.py, rollup.py, daily.py (analysis/hist.py writes them)
consumers: offline analysis
schema: schemas/analysis_history.schema.json; examples: fixtures/config_history.jsonl, hourly_rollup.jsonl, daily_summary.json (regenerate with tools/make_analysis_history_fixtures.py)

# Analysis history

Lives in `<data-dir>/history/` (`--history-dir`, default `../history` beside `--out`, empty string = off). Metadata only: no audio, no location. A writer failure is counted (status.json `meta`) and never stops the analyzer.

## Files

* `config_history.jsonl` (`analysis_config`, cap 4 MB x 2): written at analyzer start and whenever the content fingerprint changes (polled every 60 s).
  * Fingerprint covers: tonal-mask content (`mask.sha12` hashes the sorted lines and tolerance, not the path), calibration file, geo file (a moved cell is detected, never written), model versions.
  * `run_id` = analyzer integer start time; novelty rows carry it as `run`.
  * `calibration`: read from an OPTIONAL dspd-shaped file (`--calibration`, default `calibration.json` next to host.py); without it `source` is `absent`.
  * `beam_shift` (optional, since 1.1.0): 8 integers, |k| <= 4 (`host.py --beam-shift`). Shift k = the beam's window is read k samples LATER in the ring.
  * `beam_shift` is present only when >= 1 entry is non-zero. A change of the vector writes a new record.
* `hourly_rollup.jsonl` (`hourly_rollup`, cap 64 MB x 2): one record per closed UTC hour of the dsp and spatial streams.
  * Content: window count, mean levels per beam, band shares, centroid, noise-floor min/p50/max, transient counts per loudest beam, tonal appeared/gone/state counts and top 30 lines with modal beam and electronic flag, spatial leader counts and mean relative level per beam, mean decorr.
  * An hour closes at its end + 120 s and is appended once, never rewritten; a record for a closed hour is counted (`meta.history_dropped`), not applied.
  * Open hours are kept in `hourly_rollup_state.json` (not a contract file).
  * `electronic_line_s` counts tonal_state line appearances flagged electronic x 10 s.
* `daily_summary.json` (one JSON object `{k, v, days}`, atomic tmp + rename every 60 s and at exit): per UTC day:
  * windows per plugin, records per `plugin.kind`, errors, dropped windows, `audio_s` (dsp windows x 1 s), `ring_reset_s` (5 s status ticks with no contiguous audio);
  * `birds_windows`, `birds_no_beam_over_gate` (neither of the two strongest beams reached the -70 dBFS gate), `tags_speech_suppressed`, `spatial_directional`;
  * `rx_*` deltas (`rx_gaps` sums the 8 ring counters: divide by 8 for loss events), `restarts`.
  * Loaded at start and added to (a restart neither loses nor doubles a day). 400 days kept.

## Retention and bounds

* dsp.jsonl rotates at 192 MB (two generations); other analysis files 64 MB.
* Rotation is `os.replace`, falling back to copy + truncate, then to "keep appending, retry after another cap"; counted as `meta.rotate_fallbacks` and `meta.write_errors`.
* History files add < 140 MB (config 8 MB, hourly 128 MB max, daily < 0.5 MB).

## Not in the contract

`hourly_rollup_state.json` and `*.tmp`. Readers ignore unknown fields; a new field is MINOR (schema, fixtures, CHANGELOG in the same commit).
