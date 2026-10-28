# burst-source-analysis: read-only burst-source scorecard, thresholds frozen before confirmatory data

Roadmap item B4. Output contract: `contracts/specs/burst-scorecard.md`.

## 1. Summary

`tools/burst_source_report.py` (stdlib + numpy, run by hand with `python -I`) reads existing metadata logs and writes `burst_scorecard.json`, `burst_hours.jsonl`, `burst_scorecard.md`.
- Selects burst hours with a frozen rule, computes per-hour interval, periodicity, direction and ambient features.
- Applies a frozen rule table (`tools/burst_source_thresholds.json`, sha256 pinned in the tool and its test): each hypothesis (insect, pulser, housing-thermal, electronics) one verdict: supported / refuted / inconclusive / not-measurable.

## 2. Detector parameters (`analysis/plugins.py` DSPMonitor)
- 20 ms frames, total energy over 8 beams, not band-limited; baseline = median of last 150 frames.
- Transient needs `exc > 8.0 dB`, re-arms after `exc < 3.0 dB`.
- IOI quantised to 20 ms; below 40 ms unobservable; in dense bursts the detector self-suppresses (rate and CV biased).
- Tool records `min_observable_s: 0.04` and `detector_saturation_flag` (rate >= 5/s). Beam statistics say "concentrated vs diffuse", never an azimuth (adjacent beams correlate 0.92-0.96). The `bursts` plugin (w2-bursts) removes the 40 ms floor (3.9).

## 3. Design

### 3.1 Files (bench-kit/bma-audiod)
`tools/burst_source_report.py` (CLI and `main(argv)`); `tools/burst_source_thresholds.json` (committed first, own commit); `tools/test_burst_source_report.py`; `tools/make_burst_source_fixtures.py`; `contracts/specs/burst-scorecard.md`;
- `contracts/schemas/burst_scorecard.schema.json`; `contracts/fixtures/burst_scorecard.json` + `burst_hours.jsonl`.

### 3.2 CLI
```
burst_source_report.py --data-dir DIR --out DIR [--beam-bands FILE] [--since ISO] [--until ISO] [--thresholds FILE]
                       [--jitter-surrogates 200] [--seed 20261026] [--max-surrogate-pairs 600000000] [--no-write-digest] [--self-check]
```
- `--data-dir` default `$BMA_DATA_DIR` or `./data`.
- Inputs: `<data-dir>/analysis/{dsp.jsonl,dsp.jsonl.1,spatial.jsonl,coherence.jsonl,bursts.jsonl}`, `<data-dir>/history/{ambient_temp.jsonl(.1),board_history.jsonl}`, `--beam-bands` (default `<data-dir>/reports/beam_bands.jsonl`, the `pc/bma_cable_sink.py` BANDS_LOG; absent = 8-16 kHz columns null).
- `--out` default `<data-dir>/reports/burst_source`: exit 2 when it equals/lies inside `analysis/` or `history/` or contains an input file (also the beam_bands directory). Files written `*.tmp` then `os.replace`. `--no-write-digest` neither reads nor writes `burst_hours.jsonl`.
- `--max-surrogate-pairs` caps pairs x surrogates per hour; fewer surrogates above it; an hour with fewer than 20 has `peak_z` null.
- Exit: 0 report written (even if all not-measurable); 2 usage / unsafe `--out` / bad thresholds; 3 no dsp input, no hour in range, or numpy missing. `--now` hidden test plumbing.

### 3.3 Per-hour features (record `burst_hour` v1)
Raw features independent of selection thresholds;
- threshold-dependent fields DERIVED on every load. `covered_s` = distinct 1 s slots with a dsp record. Selection: `covered_s >= 1800` and `n / covered_s >= 0.10`. Autocorrelogram for hours with >= 200 events: lags 0.06-5.0 s in 20 ms bins;
- interval-jitter surrogates (each event uniform inside its own 10 s interval, counts per interval preserved), B = 200, RNG seeded by `--seed` and the hour; `peak_z` = z at the smallest-lag bin with z >= 0.8 of max;
- `z = (C - mean_B) / max(sd_B, 1)`. Field list: `contracts/specs/burst-scorecard.md`.

### 3.4 Tests and verdict rules (values in the thresholds file)
`N_b` burst hours; `N_a` covered hours with ambient; night = UTC 18:00 to 18:00.
- T1: cold share (< 5 C) of burst hours with ambient, and of all covered hours (base rate); `lift` = ratio; one-sided Fisher exact p; sensitivity at 8 and 10 C; warm (>= 10 C) burst hours listed. Needs >= 5 burst hours with ambient.
- T2: Spearman rho(rate, temperature), RAW and PARTIAL on hour of day (residuals of ranks on sin/cos of hour); circular block bootstrap (6 h, 2000, seed 1); partial CI used by rules. Needs N_a >= 48.
- T3: rho of rate with -dT/dt, +dT/dt, |dT/dt| at lag 0 and 1 h (6 tests), Holm over 6, best test's CI. Needs >= 72 hours with dT/dt per test; NOT MEASURABLE until then.
- T4: periodicity over burst hours with a periodicity test (>= 200 events; null `peak_z` hours in no denominator): periodic = `peak_z >= 5.0` and `peak_ratio >= 1.5`; `consistent_lag` = share of periodic hours within +-3% of median lag (>= 3 periodic hours).
- T5: transient path `cv_trim <= 0.5` regular, `>= 1.2` clustered; with `bursts` input gap regularity (>= 0.7 regular, <= 0.3 clustered).
- T6 stationarity (level IQR <= 2 dB and spread range <= 3 dB, >= 5 burst hours); T7 diffuseness (descriptive, in no verdict); T8 board thermal (rho of rate with -board dT/dt over burst hours with >= 0.8 coverage, >= 5 hours); T9 coherence (>= 5 burst hours with >= 30 records; no rule).

Rules, first match wins; a rule whose test cannot run gives not-measurable, never a pass. FOR EVERY HYPOTHESIS no supported/refuted when burst hours span < 3 distinct nights or number < 5 (verdict `inconclusive`, basis names the count).

| Hypothesis | supported | refuted | not-measurable |
|---|---|---|---|
| insect | T2 partial CI lower > 0.30 AND T1 share <= 0.50 | T1 share >= 0.80 AND lift >= 1.25 AND Fisher p < 0.05 AND T2 partial CI upper < 0.30 | T1 or T2 cannot run |
| pulser | eligible N >= 5, >= 80% periodic, consistent lag >= 0.80, regular share >= 0.5 (a clocked periodic source, internal OR external; cannot exclude unit electronics) | eligible N >= 5, >= 80% non-periodic, clustered share >= 0.5 | T4 has no eligible hour |
| thermal | T8 runs AND CI lower > 0.30 AND p < 0.05 | T8 CI upper < 0.30; or (T8 cannot run) basis `ambient_proxy_only` when T3 best CI upper < 0.30 AND >= 60% of burst hours have \|dT/dt\| < 0.5 | T3 and T8 cannot run |
| electronics | never from these logs | T2 partial CI lower > 0.30 OR T3 best CI lower > 0.30 | T2 and T3 cannot run |

Electronics otherwise `inconclusive` with `residual: true` when T6 passes; stays in `surviving` when the pulser is supported. `decisive_count` = hypotheses with supported/refuted on >= 5 burst hours.

### 3.5 Exploratory vs confirmatory, digest
- `frozen_utc` in the thresholds file is a LITERAL timestamp (1793062800 = 2026-10-27T01:00:00Z), not the commit time.
- `evidence_class` from this field only: hour_utc >= frozen_utc is confirmatory. Verdicts computed on all hours and on confirmatory hours alone (`confirmatory.verdicts`);
  - `confirmatory` only when >= 5 confirmatory burst hours; `unpinned` when the thresholds file differs from the pinned sha256.
- Digest `burst_hours.jsonl` merged by `hour_utc` (unless `--no-write-digest`): a raw hour replaces a digest hour when its `covered_s` is at least as large;
  - a digest hour with LARGER `covered_s` kept as `source: digest`. Rows trusted only for raw features: `is_burst`, `excluded_reason`, `rate`, `evidence_class`, `ambient`, `ioi_source` recomputed on load; `thresholds_version` recorded per row;
  - a row from another `features_version` is ignored.

### 3.6 Outputs
`burst_scorecard.json` adds `n_nights_burst`, `confirmatory`, `ioi_source` per hour. Fixed strings only: no path, no coordinate; stdout carries counts and verdict words only.

### 3.7 Degradation
No ambient: T1/T2/T3 not-measurable, insect and electronics not-measurable (thermal also without board data); periodicity still runs.
- No spatial: `spatial` null. No beam_bands: `band_8_16k` null.
- Truncated/garbage lines, NaN/inf, timestamps outside 2020..2100, 400-digit integers, lines over 1 MiB, beam_bands records with `zero_frac` 1.0: skipped and counted in `inputs.skipped_lines`. Rotated-away `.1` = absent.

### 3.8 Budget
CPU only, one process, no network. `--max-surrogate-pairs` default 6e8.

### 3.9 Optional `bursts` input
`analysis/bursts.jsonl` (`burst_stats` v1, w2-bursts) when present: an hour with >= 1800 analysed seconds gets a `bursts` block (n_impulses, gap regularity weighted by gap count, median jitter_cv and mode_s, median spread p50, coincident share, 8-16 kHz band p50) and `ioi_source: "bursts"`;
- T5 uses its gap regularity, T6/T7 its spread. The 20 ms transient path remains the fallback and the source of the periodicity test. Unknown fields ignored.

## 4. Contract and repository changes
- NEW `burst-scorecard` 1.0.0: spec, schema (`x-files`: `burst_scorecard.json` -> `burst_scorecard`, `burst_hours.jsonl` -> `burst_hour`), fixtures (real tool on synthetic data, regeneration byte-checked), CHANGELOG entry.
- `contracts/check_contracts.py`: `SCHEMAS`, `SPECS`, `FIXTURES` rows; no `LIVE` row.
- `contracts/test_contracts.py`: pinned versions dict gains `"burst-scorecard": "1.0.0"`; fixture checks, reject pairs with positive twins, file-to-kind mapping, LF/no-coordinate check, schema and CHANGELOG mutations, byte-for-byte regeneration. The `n >= 96` record-count assertion unchanged.
- `scripts/private_paths_allowlist.toml`: `test_contracts.py` coordinate matches = 1.
- Nothing imports or schedules the tool; `docker/run-tests.sh` picks up `tools/test_*.py` (needs numpy).

## 5. Tests

`tools/test_burst_source_report.py`: check()-style, temp dirs, synthetic data, seeded RNG, exact values with twins; only a recording shim around `open`.
- Burst rule boundaries
- coverage by distinct seconds
- IOI (0.5 s train, seeded exponential pinned, 60 s gap trimmed vs 9.9 s included, numpy oracle)
- autocorrelogram (periodic, Poisson twin, 7 s period, < 200 events, surrogate cap, jitter preserves counts)
- calibration (300 seeded Poisson hours: 4 false periodic = 1.33% <= 2%
- 50 periodic hours 50/50)
- determinism and seed
- ambient reader edges and dT/dt
- Spearman, ties, partial, bootstrap, Fisher, Holm
- gates N_a = 47/48/49 and T3 71/72
- T1 base-rate twins
- every verdict row with numbers and twins (nights refusal, pulser with null `peak_z` hours, 4 vs 5 eligible, T8 supported/refuted, electronics residual)
- T9 has no rule
- pinned sha, edited thresholds unpinned
- frozen boundary (one second) and N_b >= 5 boundary
- digest merge (rotated out, larger replaces, smaller does not, stale derived fields, other features_version, `--no-write-digest`)
- read-only guarantees (out dir rules, input hashes, exact new files)
- exact set of opened files
- garbage lines
- privacy
- detector parameters
- bursts input
- beam_bands default
- board coverage edge 47/48
- since/until
- LF
- `--self-check`

## 6. Criteria

- First run: no decisive verdict from 2 nights; decisive verdicts need a third night of >= 5 burst hours.
- Thermal is decided by T8 (the `ambient_proxy_only` rule needs >= 60% of burst hours with |dT/dt| < 0.5 C/h).
- A modal lag with CV > 1 is clustering, not a metronome: the pulser rule also needs the regularity test.
