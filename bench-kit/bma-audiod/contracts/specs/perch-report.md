contract: perch-report
contract_version: 1.0.0
status: stable
producer: tools/perch_report.py (run by hand; read only)
consumers: the owner
schema: schemas/perch_report.schema.json; example: fixtures/perch_report.json (regenerate with tools/make_perch_report_fixtures.py, which runs the real tool on synthetic data)

# perch_report

Perch vs BirdNET top-1 agreement. Metadata only: no audio, no embeddings, no coordinates, no path.

## Inputs (read only)

* `<data>/analysis/perch.jsonl` (+ `.1`): perch 1.0.0.
* `<data>/analysis/birds.jsonl` (+ `.1`): `k:"bird"` rows without `non_species`.
* `--out` inside `--data`: refused, exit 2.

## Rules

* Perch window: the `loudest` record of one `t` (end of a 5 s window). `mixed` records only feed `coverage`.
* Perch names a species when `best_species` logit >= `min_logit` (`--min-logit`, default the latest `perch_agreement` record's `min_logit`; null when none).
* BirdNET opinion: the highest-confidence bird row `b` with `min(t, b) - max(t - 5, b - 3) >= 2`.
* Categories: `both_same_species`, `both_same_genus`, `both_differ` (both name a species), `perch_only`, `birds_only`, `both_silent`.
* `agreement.rate` = `both_same_species` / (`both_same_species` + `both_same_genus` + `both_differ`). Null at 0 compared.
* Tables `per_species` (BirdNET species key), `by_beam` (Perch loudest beam), `by_confidence_bin` (BirdNET confidence: 0.00-0.50, 0.50-0.70, 0.70-0.85, 0.85-1.00), `by_hour_utc`: rows `{key, compared, agree, rate}` over compared windows.
* `disagreements.items`: compared windows with different species, ordered by `t`: `event_id` (labels.py event of the BirdNET row, null when none), both species, BirdNET score, Perch logit, `same_genus`. `listed` <= `--max-disagreements`.
* `coverage`: BirdNET species seen inside the Perch time span; `never_named`: those no Perch record (either source) names at logit >= `min_logit`.
* Hidden labels: a Perch window whose top 5 holds a human-voice label, or that overlaps a BirdNET row with a human-voice label, is excluded and counted in `input.excluded_human_voice`. Rule: `labels.is_speech_label` + the Perch human-voice classes of perch.py.
