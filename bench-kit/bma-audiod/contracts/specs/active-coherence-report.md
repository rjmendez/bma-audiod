contract: active-coherence-report
contract_version: 1.0.0
status: stable
producer: tools/active_coherence.py (run by hand; read only)
consumers: the owner
schema: schemas/active_coherence_report.schema.json; example: fixtures/active_coherence_report.json (regenerate with tools/make_active_coherence_fixtures.py, which runs the real tool on synthetic data)

# active_coherence_report

Event-keyed active beam coherence. Metadata only: no audio, no coordinates, no path.

## Inputs (read only)

* `<data>/analysis/coherence.jsonl` (+ `.1`): beam-coherence 1.0.0. `partial` records skipped, counted in `input.coherence_partial_skipped`.
* `<data>/analysis/birds.jsonl` (+ `.1`): `k:"bird"` rows. Human-voice labels (`labels.is_speech_label`) excluded, counted in `input.excluded_human_voice`.
* `--out` inside `--data`: refused, exit 2.

## Rules

* Event: `labels.iter_events` (same species, gap <= 3 s). Span `[t_start - 3, t_end]`. Used when `len(beams) >= 2` (`input.events_multi_beam`).
* Event pairs: ring pairs p = (beam p, beam p+1), 0-based, with at least one member among the event beams (birds beams are 1-based).
* Overlap: every coherence interval `[t0, t0 + span_s]` that overlaps the span. `event_windows` = overlap_s / 2 (`hop_s`).
* Isolating cell: band `active` cell non-null and `event_windows / n_active >= 0.5` (`isolation_min`). Other cells are not used.
* Quiet reference: per band and pair, corrected `msc_p50` of all `quiet` cells, median weighted by `n`.
* MSC corrected with `coherence_report.correct` (record `msc_floor`).
* Row per (species, band) with at least one isolating pair sample. Pair sample = (event, interval, pair), weight = cell `n`.
  `msc_active`, `msc_quiet`, `msc_diff` (active - quiet reference, per sample), `ph_spread_deg`, `lag_abs`, `dl_abs_db`: weighted medians (lag, dl: absolute values). `ph_deg`: weighted circular mean.
* `status`: `identical_expected` for bands 0-2 (below 500 Hz; beams identical, never judged); `ok` with `events >= min_events` (3); else `insufficient`.
* Row flags: `separates` = `msc_active < 0.5`; `coherent` = `msc_active >= 0.8` and `ph_spread_deg <= 30`; `bearing_dl` = `dl_abs_db >= 3`; `duet_lag` = `lag_abs >= 0.5` samples.
* Species verdict per method, over its `ok` rows: `go` when any row meets the test, `no_go` when none does, `insufficient` without an `ok` row.
  * `beam_gated_classification`: `separates`.
  * `level_ratio_bearing`: `bearing_dl`.
  * `duet`: `coherent` and `duet_lag`.
* `methods.<m>`: `go` / `no_go` species lists; `verdict` `go` when any species is go; `no_go` when no species is go and `no_go` has >= 2 species (`min_no_go_species`); else `not_measurable`.
* `measurability.verdict`: `measurable` when any row is `ok`, else `not_measurable`. Numbers: `interval_s_p50`, `event_s_p50`, `event_windows_p50` (over event-interval overlaps), `event_intervals`, `isolating_event_intervals`, `isolating_events`, `species_ok`, `species_insufficient`.
* Thresholds: constants, printed in `params`.
