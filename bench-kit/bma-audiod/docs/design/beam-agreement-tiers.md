# beam-agreement-tiers (roadmap B2)

Normative spec: `contracts/specs/beam-agreement.md` (wins over this file). Phase 0 built: `analysis/beam_agreement.py`, contract beam-agreement 1.0.0. Phase 1 (dashboard fields) and Phase 2 not built.

Statistics: Cochran-Mantel-Haenszel with Breslow-Day (confidence binned); sensitivity: likelihood-ratio test of nested logistic models (confidence vs confidence + tier), Brier score, calibration slope.

## 1. Design

### 1.1 Definitions
- Report: one `bird` row with confidence, excluding non-species classes and hidden human-voice labels; `bird_filtered` excluded.
- Window id: exact `t` float; all beams of one window share t_wall.
- Event: reports of one species key (scientific else common, lower-cased, as DetStore.key_of) chained with gap <= `--gap-s` (default 5.0 offline; live UI 3.0; a 3 s sensitivity run is reported).
- `concurrent_windows`: windows of the event with >= 2 distinct beams reporting the species.
- `evidence_tier`: `confirmed` = concurrent_windows >= 2; `probable` = 1; `single_beam` = 0. sequential-only multi-beam is reported, not a tier.
- `beam_count`: distinct beams; a number, never a tier input.

### 1.2 Phase 0 tool
`analysis/beam_agreement.py` (stdlib; numpy optional), `analysis/test_beam_agreement.py`. Standalone: not imported by analyzer or dashboard; its clusterer copies `DetStore.add` / `Taxa.classify`;
- a parity test feeds the same rows through both and requires identical (key, t_start, t_end, beams, tier, group, non_species, max_conf, max_level) tuples.
```
python beam_agreement.py --birds <birds.jsonl> [--ambient <ambient_temp.jsonl>] [--taxa ui/taxa.json] [--labels FILE] [--gap-s 5] [--generated-s N] [--out <file>] [--private-detail]
```
- Reads only given paths (no env defaults). Output JSON to `--out` or stdout; refuses `--out` inside the directory of `--birds` (exit 2). `generated_s` is a parameter so the golden report is byte-identical.
- Strata {<0.35, 0.35-0.5, 0.5-0.65, 0.65-0.85, >=0.85} (fixed): counts per tier, multi fraction, mean windows per event. Primary test: CMH on {confirmed+probable vs single_beam} vs outcome over 6 strata (conf bin x duration class (1 window / 2-3 / >=4)) plus Breslow-Day;
  - strata with smallest expected cell under 5 give `stratum_too_sparse` and `insufficient_labels`; level tercile (max_level_dbfs) is a reported sensitivity (`level_sensitivity`);
  - nested-logistic LR only with numpy (else `lr_skipped: "no_numpy"`).
- O1: owner verdicts through `--labels` (minimal `beam_label` v1: species key + t_start of the gap-3.0 event, `correct|wrong|unsure`, unsure dropped); labelled analysis always gap 3.0;
  - `sensitivity_gap_3s` repeats descriptive tables. Without labels `"o1":"unavailable"`; decision can never be `ship_tier`.
- O2: recurrence proxy (same-species event starting 5-120 s away), labelled `proxy`, never a pass criterion alone.
- O3: negative control: insect-group events with outdoor temperature < 5 C (ambient reader rule: nearest past hour, at most 3 h old) vs bird-group events at the same confidence bin. `artifact_scores_higher` uses duration x level matched fractions (majority of bins >= 0.5 with n >= 30 in both arms);
  - `matched_*` null when no shared stratum; `adjacent_share`, `confirmed_nonadjacent` per bin and `cold_insect_adjacent_share` report ring-neighbour share.
- Report `beam_agreement_report` v1 (JSON, LF); shareable form = group aggregates only; `--private-detail` adds top-10 species names. `beam_pairs_with_unequal_t` reported. Plain rounding of t does not split windows; only per-beam jitter does.
```
{"k":"beam_agreement_report","v":1,"generated_s":<num>,
 "input":{"reports":<int>,"events":<int>,"gap_s":<num>,"first_t":<num>,"last_t":<num>,"garbage_lines":<int>},
 "definition":{"tier":"concurrent","top2_cap":true},
 "bins":[{"lo":<num>,"hi":<num>,"n":<int>,"tiers":{"confirmed":<int>,"probable":<int>,"single_beam":<int>},"multi_frac":<num>,"mean_windows":<num>}],
 "negative_control":{"n_cold_insect":<int>,"n_bird":<int>,"bins":[{"lo":<num>,"hi":<num>,"cold_insect_single_frac":<num|null>,"bird_single_frac":<num|null>}],"artifact_scores_higher":<bool|null>},
 "outcomes":{"o1":"unavailable"|{"n":<int>,"cmh_p":<num>,"or":<num>,"or_ci95":[<num>,<num>],"breslow_day_p":<num>},
             "o2_proxy":{"n":<int>,"or":<num>,"or_ci95":[<num>,<num>]}},
 "decision":"drop"|"reframe_diffuse"|"ship_tier"|"insufficient_labels"|"insufficient_evidence"|"inconclusive",
 "reasons":[<str>]}
```
- Contract: `contracts/specs/beam-agreement.md` 1.0.0, `contracts/schemas/beam_agreement_report.schema.json`, `contracts/fixtures/beam_agreement_report.json` (synthetic), CHANGELOG; registered in SCHEMAS, SPECS, FIXTURES of `check_contracts.py` (no LIVE entry); ui-http untouched.

### 1.3 Phase 1 (only if `ship_tier`; not built)
Additive event fields on `/api/detections/events` and `/api/detections/event` (ui-http next minor; added to `event.properties` AND `required`; regenerate `ui_events.json`, `ui_event.json`):
- `beam_count` (1..8), `concurrent_windows` (>= 0), `evidence_tier` (confirmed|probable|single_beam),
- `tier_basis` (const "concurrent_top2"),
- `tier_truncated` (bool; true when the 500-report MAX_TRACE cap applied; `evidence_tier = null` when truncated, or an incremental window map in `Event.merge`). Computed in `Event` from `trace`;
- no new storage. `tier_of` moves into `ui/bma_ui.py`. UI text "seen on 2 beams at once" with a tooltip;
- "confirmed" not shown to users without owner approval. Files: `ui/bma_ui.py`, `contracts/specs/ui-http.md`, `contracts/schemas/ui_http.schema.json`, `ui/test_bma_ui*.py`.

### 1.4 Phase 2 (deferred)
`paired_conf` (0..1) on the bird row for the weaker top-2 beam; flag `--bird-paired-conf <float>` (default off, no env var); at most one value per reported species-window; analysis-outputs minor.

### 1.5 Degradation
No ambient: `n_cold_insect` 0, `artifact_scores_higher: null`, `insufficient_evidence` reason `no_ambient`. Taxa unusable: `taxa_missing`, control refused, same decision. Garbage lines counted in `input.garbage_lines`. Never a pass.

### 1.6 Budget
CPU only. RAM < 50 MB; report < 20 KB written only to an owner-chosen path.

## 2. Contract changes

## 3. Tests
1. Concurrent counting: t=100.0 beams {2,5} + t=103.0 beam {2}, gap 5 -> one event, concurrent_windows 1, beam_count 2, probable. Twin: both windows {2,5} -> 2, confirmed. Twin: beam 2 at 100.0 and beam 5 at 103.0 -> concurrent_windows 0, beam_count 2, single_beam.
2. Gap: 5.0 s merges at `--gap-s 5`, splits at 4.9; different species keys never merge.
3. Float-equality windowing: identical t forms a window; t differing by 1e-6 does not.
4. Bin edges: 0.5 in 0.5-0.65, 0.65 in 0.65-0.85, each with a neighbour twin.
5. CMH: hand-checked two-stratum table (chi-square, p asserted to 1e-9); Simpson-style confounded table returns OR 1.0, p > 0.05; real within-stratum effect p < 0.05.
6. Negative control: cold-insect events all concurrent, birds mostly single -> `artifact_scores_higher` true, `reframe_diffuse`; swapped -> false. Labels supplied so CMH is significant.
7. Each decision rule on tables exactly either side of its threshold.
8. Inputs: garbage line counted; `--out` inside input dir exits 2 (`pytest.raises(SystemExit)`); missing ambient -> `insufficient_evidence` / `no_ambient`.
9. Schema: output validates; fixture without `decision` fails with the specific error.
10. Golden test on a 40-row synthetic birds fixture: exact counts per bin and tier.
11. Parity test with `DetStore.add`; changing the gap comparison turns it red.
Phase 1 tests: Event fields equal the shared function's output; `tier_truncated` true at 501, false at 500; hidden-label event carries no tier.
Mutations to RUN: `>= 2` to `> 2`; group by rounded t; tier from beam_count; gap `<=` to `<`; pool strata in CMH; flip the artifact comparison; drop human-label exclusion.
Checks: `scripts/check_test_honesty.py`, `scripts/check_private_paths.py`, `contracts/check_contracts.py`, `contracts/test_contracts.py`.

## 4. Decision rules (ordered, first match; spec "Decision")
Primary comparison: owner-labelled events (O1) in conf 0.5-0.85, stratified by duration.
- `ship_tier`: CMH p < 0.05 within strata AND OR >= 2 AND Breslow-Day p >= 0.05 AND the artifact does NOT out-score birds at the same confidence.
- `reframe_diffuse`: CMH significant but negative control fails; any shipped field is a "diffuse / multi-beam" flag, never evidence. Label-free interim can emit it from O3 alone (reason `label_free_o3_only`), never `ship_tier`.
- `drop`: CMH p >= 0.05 or OR < 1.5.
- `insufficient_labels`: sparse strata or no labels and no artifact.
- `insufficient_evidence`: negative control not computable (no ambient, no taxa).
- `inconclusive`: otherwise.
Sample size: 180 labelled events per arm (360 total); below 120 per arm the sparse-stratum rule gives `insufficient_labels`.
