contract: beam-agreement
contract_version: 1.1.0
status: stable
producer: analysis/beam_agreement.py (offline, run by the owner on a COPY of the data; never started by the supervisor)
consumers: the owner; design: docs/design/beam-agreement-tiers.md
schema: schemas/beam_agreement_report.schema.json; example: fixtures/beam_agreement_report.json (regenerate with tools/make_beam_agreement_fixtures.py)

# beam_agreement_report

BirdNET scores only the TWO strongest beams of each 3 s window (analysis/plugins.py, `MultiBeamBirds`): a two-beam report = the second-strongest beam also scored >= min_conf.

The tool tests whether agreement carries more information than confidence, against a negative control. It writes a report and a pre-registered `decision`; nothing live changes.

## Inputs

Read only; paths from flags, never the environment.

* `--birds`: `birds.jsonl`. Only `k:"bird"` rows used (`bird_filtered` counted in `input.filtered_skipped`). Torn or non-JSON lines counted in `input.garbage_lines`.
* `--ambient`: `history/ambient_temp.jsonl`. Missing, unreadable or empty: `decision` `insufficient_evidence`, reason `no_ambient`.
* Ambient reader rule: newest record whose hour started at or before the event's first report, <= 3 h old, else unknown and the event is not a cold-night event.
* `--taxa`: `ui/taxa.json` (default: the repository's). Unusable: `insufficient_evidence`, reason `taxa_unusable`.
* `--labels`: owner verdicts. Absent: outcome O1 is `"unavailable"`.
* `--out` is refused (exit 2) when inside the directory of ANY input.

### Owner verdicts (`--labels`, JSONL)

`{"k":"beam_label","v":1,"species_key":"<scientific name, else common name, lower case>","t_start":<unix s of the event's first report>,"verdict":"correct"|"wrong"|"unsure"}`

* Event key = species key + `t_start` of the DASHBOARD's event (gap 3.0 s, `EVENT_GAP_S`), compared at 6 decimals.
* Label-joined analysis builds events at gap 3.0; `--gap-s` (default 5.0) shapes only the descriptive tables; `sensitivity_gap_3s` repeats them at 3.0.
* `unsure` dropped (`input.labels.unsure_dropped`); a repeated key keeps the last verdict (`duplicates`); malformed line = `garbage_lines`; a label matching no event is unused.
* Human-voice and other non-species labels are never analysed.

## Definitions

* Window: the exact float `t` of a row (all beams of one window share it); equality EXACT.
* `input.beam_pairs_with_unequal_t` counts consecutive windows of one event whose `t` differ by < 0.5 s without being equal; > 0 = tiers are lower bounds.
* Event: reports of one species key chained like the dashboard's `DetStore.add` (file order, newest event per key, `t_start - gap <= t <= t_end + gap`).
* `concurrent_windows`: windows of the event where >= 2 distinct beams reported the species.
* `evidence_tier`: `confirmed` (>= 2 concurrent windows), `probable` (1), `single_beam` (0). `beam_count` is never a tier input. Computed over the whole event.
* Analysed events: species (not non-species), `in_region` true.
* Confidence = event maximum; bins `<0.35, 0.35-0.5, 0.5-0.65, 0.65-0.85, >=0.85` (lower edge inclusive, last bin includes 1.0).
* Duration class: distinct windows `1`, `2-3`, `>=4`. Level tercile of `max_level_dbfs` inside the compared set.
* Adjacent beams: ring neighbours (numbers differing by 1 or 7, mod 8).
* Per bin: `adjacent_share` (concurrent windows with an adjacent pair / all concurrent windows) and `confirmed_nonadjacent` (confirmed events with >= 1 non-adjacent concurrent window).

## Outcomes

* O1 owner verdict (the only pass criterion): primary band (conf 0.5-0.85, gap 3.0 events), exposed = `concurrent_windows >= 1` vs outcome = `correct`.
  * Cochran-Mantel-Haenszel (continuity corrected) over 6 strata = conf bin (0.5-0.65, 0.65-0.85) x duration class.
  * Mantel-Haenszel common odds ratio (Robins-Breslow-Greenland 95% CI); Breslow-Day test (Tarone corrected, needs >= 2 strata). Strata with an empty margin dropped.
  * `status`: `ok`; `stratum_too_sparse` (any used stratum has smallest expected cell count < 5); `no_variation`; `no_labelled_events`.
  * Level-tercile stratification (18 strata) is `level_sensitivity` only, never changes the decision.
* O2 recurrence proxy (`o2_proxy`): another event of the same species starts within 120 s after this one ends (events whose look-ahead passes the end of data dropped). Unstratified, Haldane-corrected Woolf odds ratio. Descriptive only.
* O3 negative control: cold-night insect events (group insect, temperature < 5 C by the ambient rule) vs in-region bird events of the same bin.
* `artifact_scores_higher` is decided on duration-class x level-tercile MATCHED single-beam fractions (direct standardisation over strata both arms populate).
* It is true when, in the majority of bins >= 0.5 with >= 30 events in BOTH arms, the cold-insect single-beam fraction is below the bird one; null when no bin votes.

## Decision (ordered, first match wins)

1. `insufficient_evidence`: `taxa_unusable`, or `no_ambient`, or `negative_control_not_computable` (no voting bin).
2. Label-free (O1 `"unavailable"`): `artifact_scores_higher` true -> `reframe_diffuse` (reasons `label_free_o3_only`, `artifact_scores_higher`); false -> `insufficient_labels` (`no_labels`).
3. `insufficient_labels`: O1 `status` not `ok` (the status is the reason), or < 90 labelled events in either arm (`fewer_than_90_labels_per_arm`).
4. `drop`: CMH p >= 0.05 or odds ratio < 1.5.
5. `reframe_diffuse`: `artifact_scores_higher` true.
6. `ship_tier`: CMH p < 0.05 and odds ratio >= 2 and Breslow-Day p >= 0.05 (computed) and the control does not out-score birds.
7. `inconclusive`: none of the above.

## Report

* One compact JSON object, LF, one line (`beam_agreement_report` v1; fields in the schema). `generated_s` is a parameter.
* Default report = group aggregates only; `--private-detail` adds `private_detail.top_species` (top-10 common names).
* Coordinates, county and file paths never appear. Metadata only: no audio read or written.

## Not a live interface

Nothing in the pipeline reads or writes this report; no LIVE entry in the checker.

## Shuffle null (1.1.0, optional block `shuffle_null`, report-only)

* Flags: `--null-trials N` (default 200; 0 omits the block), `--null-seed S` (default 20261027). Same inputs, same seed: same bytes.
* Statistic: pass = in-region species event with `concurrent_windows` >= 1 (a window with >= 2 beams).
* Null draw: within each (species key, UTC hour) group every beam keeps its report count, confidences and levels and takes a sample without
  replacement of the group's distinct report times; events are re-clustered with `--gap-s`. A beam with more reports than the group has
  distinct times keeps the surplus at their real times (`reports_unshuffled`).
* Per confidence bin (the fixed five), pooled (`all`) and per measured quantile band: `real_pass_rate`, `null_pass_rate` (mean over draws with
  `n` > 0), `null_min`, `null_max`, `null_n_mean`, `excess` = real - null, `runs`. Empty bin: rates null.
* `quantile_bands`: cut points at quantiles 0.2 0.4 0.6 0.8 of the real in-region species event confidences (`cuts`, band i = [cut i-1, cut i)),
  `arbitrary_cuts` = fraction of events below 0.5 and 0.85. `gating` is always false.
* No tier, `decision` or `reasons` value reads `shuffle_null`.
