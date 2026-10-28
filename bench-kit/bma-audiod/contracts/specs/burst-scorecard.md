contract: burst-scorecard
contract_version: 1.0.0
status: stable
producer: tools/burst_source_report.py (run by hand)
consumers: the owner and offline analysis
schema: schemas/burst_scorecard.schema.json; examples: fixtures/burst_scorecard.json, fixtures/burst_hours.jsonl (regenerate with tools/make_burst_source_fixtures.py)

# burst_scorecard / burst_hour

Hypotheses: insect, pulser (nearby periodic device), housing thermal, unit electronics.

Per-hour feature table and one verdict per hypothesis by rules FROZEN BEFORE CONFIRMATORY DATA (tools/burst_source_thresholds.json; sha256 pinned in the tool and its test). Design: docs/design/burst-source-analysis.md.

* No audio, coordinate, path or text copied from an input.
* Reads only dsp.jsonl(.1), spatial.jsonl, coherence.jsonl, ambient_temp.jsonl(.1), board_history.jsonl, optional beam_bands.jsonl (default `<data-dir>/reports/beam_bands.jsonl`) and bursts.jsonl.
* Never opens tags, clap, birds, novelty or embedding files.

## Files

`--out` must not equal, lie inside or contain an input.

* `burst_scorecard.json`: ONE compact JSON object (`k: burst_scorecard`), LF.
* `burst_hours.jsonl`: one `burst_hour` record per UTC hour with dsp coverage, ascending; also the DIGEST.
  * The next run merges it by `hour_utc`: the raw hour replaces a digest hour when its `covered_s` is >=; a digest hour with LARGER `covered_s` is kept (`source: digest`).
  * Only raw per-hour features are trusted from it; `is_burst`, `excluded_reason`, `rate_per_covered_s`, `evidence_class`, `ambient`, `ioi_source` are recomputed on every load.
  * A row with another `features_version` is ignored. `--no-write-digest` neither reads nor writes it.
* `burst_scorecard.md`: the same as a table (fixed strings only).

## burst_hour

Raw features:

* `covered_s` = number of DISTINCT 1 s slots holding a dsp record; `n_transients`.
* `ioi` (intervals <= 10 s): median, p10, p90, `cv_trim` = population std / mean, lag-1 serial correlation, Fano factors at 1 s and 10 s over covered bins, `min_observable_s` 0.04.
* `acorr`: event-time autocorrelogram, lags 0.06-5.0 s in 20 ms bins, against `n_surrogates` interval-jitter surrogates (each event moved uniformly inside its own 10 s interval; seeded by `--seed` and the hour).
  * `peak_z` = (C - mean) / max(sd, 1) at the smallest-lag bin whose z >= 0.8 of the maximum; `peak_ratio` = C / mean there.
  * All null for < 200 events or when `--max-surrogate-pairs` would leave < 20 surrogates.
* `level_db`, `excess_db_median`.
* `beam`: counts per loudest beam, top share, normalised entropy, median beam spread.
* `spatial`.
* `band_8_16k`: 8-beam mean 8-16 kHz level of 1 s `beam_bands` slots with and without a transient.
* `bursts`: aggregate of optional `burst_stats` records; needs >= 1800 s analysed in the hour; then `ioi_source` is `bursts` and its gap regularity and spread replace `cv_trim` and the transient spread in T5-T7.
* `board`: median, slope in C/h only with >= 80% of the 60 one-minute slots.
* `coherence.n_records`.

Derived on load:

* `is_burst` = `covered_s >= 1800` and `n_transients / covered_s >= 0.10`.
* `excluded_reason` `low_coverage` below 1800 s; `detector_saturation_flag` at >= 5 per covered second.
* `evidence_class` `confirmatory` when `hour_utc >= frozen_utc` else `exploratory`.
* `ambient.temp_c` by the reader rule of specs/ambient-temp.md; `dtdt_c_per_h` = (T[h+1] - T[h-1]) / 2 from the exact records either side.

## burst_scorecard

`tests` T1..T9, each `{value, n, status ok|not_measurable, reason, numbers}`:

* T1 cold share (< 5 C) of burst hours vs share of ALL covered hours with ambient (lift, one-sided Fisher exact p, sensitivity at 8 and 10 C, warm burst hours listed).
* T2 Spearman of rate vs temperature with circular block bootstrap (6 h blocks, 2000 resamples), partial on time of day; needs 48 hours.
* T3 rate vs dT/dt (6 tests, Holm); needs 72 hours with dT/dt.
* T4 periodicity, T5 regularity, T6 stationarity, T7 diffuseness (descriptive).
* T8 board thermal (>= 5 burst hours with board coverage).
* T9 coherence (>= 5 burst hours with >= 30 records; no rule uses it).

`verdicts` (`insect`, `pulser`, `thermal`, `electronics`): `supported | refuted | inconclusive | not-measurable`, with `basis`, `rule`, `numbers`, `evidence_class` and, for electronics, `residual`.

* Rules: first match wins; a rule whose test cannot run gives not-measurable, never a pass.
* No supported or refuted verdict for burst hours spanning < 3 distinct nights (UTC 18:00 to 18:00) or < 5 burst hours.
* `pulser: supported` = clocked periodic source, internal OR external, so `electronics` stays in `surviving`; `electronics` is never supported from these logs.
* `confirmatory` repeats the verdicts on confirmatory hours only.
* A verdict's `evidence_class` is `unpinned` when the thresholds file differs from the pre-registered sha256, `confirmatory` when the confirmatory set holds >= 5 burst hours, else `exploratory`.
* `decisive_count` = hypotheses with supported/refuted on >= 5 burst hours. `surviving` = not refuted. `caveats` are fixed sentences.

## Versioning

A threshold or rule change bumps `thresholds_version` and the pinned sha in the tool and its test. A change to what is stored per hour bumps `features_version`. Adding a key to a `numbers` object is MINOR.
