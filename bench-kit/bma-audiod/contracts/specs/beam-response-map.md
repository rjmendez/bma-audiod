contract: beam-response-map
contract_version: 1.0.0
status: stable
producer: analysis/beam_response_map.py (offline, stdlib only; manual run, never a service)
consumers: the owner; nothing reads it automatically
schema: schemas/beam_response_map.schema.json; example: fixtures/beam_response_map.json (regenerate with tools/make_beam_response_map_fixtures.py)

# beam_response_map - repeatable 8-beam level pattern of natural sources

* One JSON object, one line, LF newline, `ensure_ascii`, written atomically by `--out FILE` (temp file in the target directory, then rename; refused when FILE is inside `--analysis-dir`).
* Default `<data-dir>/reports/beam_response_map.json`. Carries species names: private, never publish.
* `check_contracts.py --brm-report FILE` validates it (optional; no LIVE entry).
* The tool never estimates a bearing. `no_go` = "not supported by natural sources"; only a controlled source can prove the negative (`confound` says so in every report).

## Inputs and `t`

* `analysis/spatial.jsonl` (1 Hz, `e_rel_db[8]`), `analysis/birds.jsonl` (labels and times only), optional `reports/beam_bands.jsonl`, taxa table (`--taxa`, default `analysis/taxa.json`, else `ui/taxa.json`).
* Rotated `.1` of spatial and birds read first when present (`--no-rotated` skips).
* beam_bands rows sorted by time on load (warning); only the last `bands_max_rows` (60000) rows unless `--bands-max-rows 0`; `--bands-since ISO` drops earlier ones.
* A band block counts only events inside the period its rows cover (warning when the cap bit).
* `analysis/tags.jsonl` (and `.1`) is read for `k`, `t`, `speech_active` ONLY. Never read: audio, embeddings, tag labels.
* `t` of a spatial or birds record = wall time of the END of its window (spatial 1 s, birds 3 s): a birds event at t covers (t - 3, t]; spatial rows inside it have `t` in [t - 2, t].
* Default join `join_before_s` 2.0, `join_after_s` 0.0. beam_bands `t_us` = production time, after the window's last sample.

## Statistic

1. Level vector: `e_rel_db` = `e_db - max(e_db)` per row; centred on its beam mean (`e_db - mean(e_db)`). beam_bands rows (absolute dB) are centred per band the same way.
2. Baseline: per beam, median of centred rows within +-`baseline_s` (900) of the event, same segment; needs `min_baseline_windows` (50) rows, else `events_no_baseline`. Excess = mean of joined rows - baseline.
3. A1 = (2/8) |sum_k x_k exp(i 2 pi k / 8)| = amplitude (dB) of the first harmonic over the ring (beam k at 45 k degrees; `cos(2 pi (k-1)/8)` of amplitude 4 gives A1 4.0, phase 45.0).
   * R = length of the mean unit vector of the event phases.
   * Rayleigh p = Zar's approximation exp(sqrt(1 + 4n + 4(n^2 - (nR)^2)) - (1 + 2n)).
4. Controls: windows drawn at random from the segment, per UTC hour of day in proportion to the source's events, >= `exclude_s` (10) from every bird record at `min_conf`. The j-th control of an hour depends only on (seed, segment, hour). Same join and baseline.
   * `perm_p`: median A1 of bouts replaced by controls of their own hour, `perms` times, (count + 1) / (perms + 1).
   * `a1_ci95`: bootstrap of the median A1 over bouts.
5. Bouts: joined events of one source <= `bout_gap_s` (60) apart form one bout; the bout mean vector is one observation.
   * `a1_db`, `a1_ci95`, `phase_deg`, `R`, `rayleigh_p`, `perm_p` and the Rayleigh n use bouts (`bouts` per block); < `min_bouts` (10) = `insufficient`.
   * Histograms, `top2_db`, `ratio_by_dominant`, `lag_scan` stay per event.
6. Speech: an event or control window within `speech_guard_s` (5) of a human-voice bird record (any confidence) or a `tags` row with `speech_active` true is never used; no human-voice count reported.
7. `lag_scan`: median A1 with the join window shifted -10..+10 s (21 entries).

## Segments

* Recordings separated by gaps > `cluster_gap_s` (86400) are analysed separately, as are `--epoch ISO=LABEL` starts (naive times are UTC). Baselines never cross a segment.
* A source-band verdict aggregates its segments: only segments with >= `min_events` joined events vote; all voters must agree, else `inconsistent`; none = `insufficient`.

## Sources

* Events = distinct (common name, second) of bird records with confidence >= `min_conf`, `in_region` not false.
* Human voice (every taxa explicit label starting with `human`; lower-case lookup on common and scientific name) is never a source and only marks a speech window.
* A missing or unusable taxa table = exit 2 (fail closed).
* Auto mode takes every source with >= `min_events` events except taxa `non_species` or group `mechanical` (listed in `skipped_sources`; an explicit `--source Engine` evaluates it with `kind: non_biological`).
* Group `insect` sources carry `suspected_artifact: true`. Flagged and non-biological sources are evaluated but never counted in `overall`.

## Verdict rules

Printed in `params.thresholds`; changing one is a version change.

* `insufficient`: < `min_events` joined events, < `min_bouts` bouts, > 30% of events unjoined or without baseline, or no control windows.
* `go`: ALL of median A1 >= max(`a1_min_db` 1.0, `a1_control_factor` 3 x control p95 of A1); R >= `r_min` 0.5; Rayleigh p < alpha_adjusted; bootstrap 95% CI of the median A1 above the control median A1.
* `weak`: Rayleigh p < alpha_adjusted and permutation p at its floor (or < alpha_adjusted) but a `go` condition fails.
* `no_go`: otherwise.
* alpha_adjusted = `alpha` 0.01 / `tests` (Bonferroni over (source, band) pairs that reached statistics). Bands 50-500 Hz are `identical_expected`, not evaluated.
* `overall` = best verdict among non-flagged biological source-bands (go > weak > inconsistent > no_go > insufficient).

## Fields

Schema is closed (`additionalProperties` false).

* Per source-band entry: `name`, `band` (`spatial_200_4000` or `b_<lo>_<hi>`), `kind`, `suspected_artifact`, `events`, `epochs[]`, `verdict`, `reason`.
* `epochs[]` block per segment with events: counts, `dominant_hist` vs `control_dominant_hist`, `ratio_by_dominant`, `top2_db`, `a1_db`, `a1_ci95`, `phase_deg`, `R`, `rayleigh_p`, `perm_p`, `control`, `lag_scan`, `verdict`, `reason`.
* `ratio_by_dominant`: adjacent-beam excess difference quantiles per dominant beam, n >= 5.
* An `insufficient` block carries only counts.
* `filters`: spread_over_max (beam spread > `max_spread_db`, dead windows), bad_vector, no_baseline, bad_json. Aggregates only: no per-event vectors.

## Privacy

No audio, embedding, coordinate, county, file path or host name is read into or written to the report; free-text fields cannot contain a path separator (schema pattern). No network call.

## Compatibility

Closed schema: a new field is MINOR with schema, fixture and CHANGELOG in the same commit.
