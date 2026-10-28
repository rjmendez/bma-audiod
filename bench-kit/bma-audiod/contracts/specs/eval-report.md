contract: eval-report
contract_version: 1.0.0
status: stable
producer: tools/eval_models.py (run by hand; read only)
consumers: the owner
schema: schemas/eval_report.schema.json; example: fixtures/eval_report.json (regenerate with tools/make_eval_report_fixtures.py, which runs the real tool on synthetic labels)

# eval_report

owner_verdict labels vs each model variant. Metadata only: no audio, no embeddings, no coordinates, no path.

## Inputs (read only)

* `<labels-dir>/labels.jsonl` (`--labels-dir`, default `<data>/labels`): verdict-labels 1.0.0, folded per event by `labels.fold`.
* `<data>/analysis/birds.jsonl` (+ `.1`): events rebuilt by `labels.iter_events`; a label whose `event_id` is not rebuilt counts in `input.unmatched_labels`.
* Variants: `birds.jsonl` (`birdnet`, species, prob), `perch.jsonl` rows `src=loudest` (`perch`, species, logit), `tags.jsonl` (`tags`, class, prob), `clap.jsonl` (`clap`, class, prob). Missing file: no variant. Extra: `--variant NAME=REL[:species|class[:prob|logit]]`, REL under `<data>/analysis`, rows need `t` and `top` `[[label, score], ...]`.
* Provenance: field `mv` (or `model_version`). One file with several `mv` values: one variant per value, named `NAME@mv`. Rows without `mv`: `mv` = [] (`NAME@unrecorded` when mixed with recorded ones).
* `--map` (default tools/eval_neighbourhood.json): class variants, group -> labels. `--taxa` (default ui/taxa.json): truth group. `--confusions` (default analysis/confusions.json): lookalike pairs.
* `--out` inside `--data` or `--labels-dir`: refused, exit 2.

## Truth (per folded label)

* `correct`: species = `species_key`.
* `wrong` + `other_species` + `corrected_key`: species = `corrected_key`.
* `wrong` otherwise (no species named): rejection of the claim `species_key`; not in accuracy.
* `unsure`, `artifact`: excluded, counted.
* Human voice: event or label key hidden (`labels.is_speech_label`, Perch voice classes), or any matched variant row with a hidden label in its `top`: excluded from every variant, counted `input.excluded_human_voice`.

## Matching

* A row of end time `t` belongs to the event when `t_start - 1 <= t <= t_end + 3`; `birdnet` rows: `t_start - 3 <= t <= t_end + 3`.
* Candidates: labels of the matched rows, score = max over rows, ordered by score desc, key asc. Keys lower case.
* No matched row: `no_output`, the event is not scored for that variant.

## Metrics

* species level: top-1 = first candidate equals the truth key; top-5 = truth key among the first 5.
* class level: truth group = `taxa.classify`; candidate group = `--map`; unmapped label = no group; top-1 / top-5 on groups. `near` = 0.
* near: same genus (both keys scientific: seen as a scientific name in birds.jsonl, confusions or a species-level variant, or first word a genus of `--taxa`) or listed lookalike pair, truth key not equal. `top1.near` = events not exact at top-1 and near at top-1; `top5.near` = not exact in top-5 and some candidate near. `with_near_rate` = (exact + near) / n. Reported apart from `exact_rate`.
* `rejections` (species level): `repeats_claim` = top-1 key equals the claimed `species_key`.
* `per_truth`: by truth species (species level) or truth group (class level), ordered by n desc.
* McNemar exact, per pair of variants of one level over events scored by both: `a_only` = A exact, B not; `b_only` = reverse; `p` = min(1, 2 * sum_{i <= min} C(n, i) / 2^n), n = a_only + b_only; n = 0: p = 1. Top-1 and top-5 on the `exact` indicator.
* Calibration: confidence = top-1 candidate score, outcome = top-1 exact, 10 bins `0.0-0.1` .. `0.9-1.0` (upper edge 1.0 in the last bin only). `ece` = sum over measurable bins of n_b / ece_n * |owner_correct_rate - mean_conf|. `logit` variants or scores outside [0, 1]: `not_applicable`.

## not_measurable

* Variant: n < `min_n` (default 20) or class variant without map entry: `status` not_measurable, `top1`/`top5` null, per-truth counts null.
* per_truth row: n < `min_species_n` (default 10).
* Pair: n_paired < `min_n`, or different levels: no `p`.
* Calibration bin: n < `min_bin_n` (default 10): rates null, excluded from ece. ece null when n < `min_n` or ece_n < `min_n`.
* Counts (`n`, `n_paired`, `min_n`) are always given.

## Validity limits

* Events come from `birds.jsonl` (birdnet output); `correct` sets truth = birdnet claim. birdnet top-1 is inflated by construction.
* McNemar pairs against `birdnet` are on birdnet-selected events only; they do not compare the variants on events birdnet missed.
* Events are treated as independent; events of one species are not. `p` is not adjusted for it.
* stdout table ends with the same caveat line; the JSON report does not carry it.
