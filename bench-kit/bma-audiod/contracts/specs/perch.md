contract: perch
contract_version: 1.1.0
status: stable
producer: analysis/host.py plugin `perch` (analysis/perch.py); flags `--perch-model`, `--perch-labels`, `--perch-embeddings`, `--perch-threads`, `--perch-min-logit`; `--no perch` disables it
consumers: offline analysis
schema: schemas/perch.schema.json; example: fixtures/perch.jsonl (regenerate with tools/make_perch_fixtures.py)

# perch - Perch 2.0 shadow second opinion beside BirdNET

Perch 2.0 (14,795 labels) runs on the same live windows as BirdNET and logs METADATA ONLY to `<--out>/perch.jsonl` via `hist.RotatingJsonl` (64 MB x 2).

* Nothing else reads it. Records and aggregates carry RAW logits, not probabilities.
* Write errors and rotate fallbacks are counted in status.json `meta`.

## Signals

Per 5 s window (`win_s` 5, `hop_s` 5; analyzer worker pool, never blocks the receive loop) of the 8 beams at 48 kHz, two mono signals in one batch:

* `mixed`: arithmetic mean of the 8 beams (`beam` null).
* `loudest`: beam with the highest RMS over the window (`beam` 1..8).
* Resample 48 -> 32 kHz (`scipy.signal.resample_poly(x, 2, 3)`: 240,000 -> 160,000 samples).
* Normalise as `perch_hoplite` `normalize_audio`: subtract mean, divide by peak, multiply by 0.25 (all-zero stays zero).
* Model: `inputs` [batch, 160000] float32 -> `embedding` [batch, 1536], `label` [batch, 14795] logits.

## Records (`perch`, one per window and source)

| field | meaning |
|---|---|
| `k`, `v` | `perch`, 1 |
| `t` | window end, wall-clock Unix s (both records of a window share it) |
| `src`, `beam` | `mixed` (beam null) or `loudest` (beam 1..8) |
| `level_dbfs` | RMS of the classified signal before normalisation, dB re full scale, 1 decimal |
| `top` | 5 largest logits `[label, logit]`, largest first, ties by lower label index, logit 3 decimals |
| `top1_group` | group of `top[0]`: `bird`, `insect`, `amphibian`, `mammal`, `other-bio`, `non_species`, `unknown` |
| `best_species` | `[label, logit]` of the best BINOMIAL label over the whole list |
| `mv` | `<model file name>@<first 12 hex of sha256>` |
| `run` | analyzer run id (integer start time) or null |
| `emb_i` | embedding index in the embedding files; null unless `--perch-embeddings` |
| `model` | 1.1.0, optional: {name `Perch`, version `2.0`, variant, sha256 of the model file} |
| `claim` | 1.1.0, optional: {human_verified: false, source: `model_opinion`} |
| `max_unstored_score` | 1.1.0, optional: 6th largest logit (`top` is the top_k) |

Labels:

* `labels.csv`: first line = list name `inat2024_fsd50k`, then one label per line in output order.
* iNaturalist 2024 binomials (`Poecile carolinensis`) and FSD50k classes (`Insect`, `Cricket`, `Alarm`, `Wind_chime`).
* A label is a species when it contains a space and no underscore.
* `group` from `ui/taxa.json`; FSD classes `Insect`, `Cricket` forced to `insect`. `insect` is a lower bound.

## Never written

* No audio anywhere.
* Speech: a window gets NO record, embedding or agreement entry when a tagger window overlapping it was flagged `speech_active`.
* Every window is held until the tagger's windows (5 s, hop 2 s) cover it completely.
* Fail closed: a window the tagger did not cover (dropped or failed window, gap > 0.5 s) is dropped after the guard saw the tagger pass its end, or after 30 s, counted `unknown_dropped`.
* At shutdown unconfirmed windows are dropped. Needs the tags plugin: without it Perch is "not started".
* Human-voice labels: any label of the set `perch.HUMAN_VOICE_LABELS` in EITHER source's top 5 drops the whole window (`voice_skipped`).
* Gate: loudest beam below -70 dBFS produces nothing (`gated`).
* Non-finite or wrongly shaped windows are refused (`nonfinite`).

## `perch_agreement` (one per UTC minute)

* Perch window `P = [t - 5, t]` belongs to the minute containing `t`.
* A BirdNET window `B = [b - 3, b]` counts for P when it overlaps P by >= 2.0 s AND classified >= 1 beam (`beams_classified` > 0).
* BirdNET's opinion on P = highest-confidence `bird` row (not `bird_filtered`, not `non_species`) among counting windows; none = silent.
* Perch's opinion = `best_species` of the `loudest` record when its logit >= `min_logit` (`--perch-min-logit`, default 0.0; stamped in the record), else silent.
* Per window exactly one category:

| field | meaning |
|---|---|
| `both_same_species` | same scientific name (case-insensitive) |
| `both_same_genus` | same genus, different species |
| `both_differ` | both name a species, different genera |
| `perch_only` / `birds_only` | one names a species, the other silent |
| `both_silent` | neither names a species |
| `birds_missing` | no counting BirdNET window: no opinion, NOT silence |

* `windows` = sum of the seven. `perch_top1_groups` counts windows by `top1_group` of the `loudest` record (sums to `windows`).
* `insect` = `{perch, birds, both}`: windows whose Perch top-1 group is `insect`; windows with a BirdNET opinion whose top species has group `insect`; windows where both hold.
* A minute is closed 15 s after its end and written once. `partial: true` = written at shutdown before that grace.
* A minute with no written Perch window has no record. `t` = start of the minute.

## Embeddings (`--perch-embeddings`)

Outside the JSONL schema.

* `perch_emb.f16`: float16, 1536 values = 3,072 bytes each. `perch_emb.jsonl`: `{"i","t","src","beam","run"}`, `i` = the record's `emb_i`.
* Vector `i` is at byte `(i - first i in that file) * 2 * dim`.
* When `perch_emb.f16` would exceed 64 MiB (21,845 vectors) both files rotate to `*.1` (one generation, <= 128 MiB).
* `i` counts from 0 per analyzer run: pair with `run`. Speech windows never reach it. Failures counted (`embedding_errors`), never raised.

## status.json

* `plugins.perch`: `enabled`, `windows`, `records`, `errors`, `dropped`, `last_t`, `last_error`, `gated`, `speech_skipped`, `voice_skipped`, `unknown_dropped`, `nonfinite`, `pending`, `agreement_records`, `mv`, `embeddings_stored`, `embedding_errors`.
* Off shapes (`enabled` false, `reason`): `disabled, no --perch-model` (default), `disabled by --no`, `... not started: ...`.
* `not started` causes: model file, label file or onnxruntime missing or unusable; label count differs from the model output; tags plugin not running.
* The analyzer carries on in every case.

## Deploy

1. `python tools/fetch_perch.py --dest <model-dir>/perch` (stdlib only; pinned sha256; refuses to overwrite).
2. `pip install --target <model-dir>/overlay --no-deps onnxruntime flatbuffers protobuf packaging` (onnxruntime 1.31.1; run_analyzer.sh puts `overlay` on PYTHONPATH when it exists).
3. Start the analyzer with `--perch-model <model-dir>/perch/perch_v2.onnx`. Labels default to `labels.csv` next to it (`--perch-labels` overrides). `--perch-threads` (default 4) bounds ONNX CPU threads; CPU only.

* `analysis/perch.py` must be copied next to host.py (pc/install_task.ps1 lists it; docker/test_install_lists.py pins that).
* host.py imports it only with `--perch-model`; otherwise `plugins.perch` reports "not started: No module named 'perch'" (test_perch.py 12).
* The Docker image does NOT offer Perch (analyzer.Dockerfile does not copy perch.py, requirements-analyzer.txt has no onnxruntime, docker/entrypoint.sh passes no `--perch-*` flags). Use the PC install.

## Licence

* Verified: Hugging Face `justinchuby/Perch-onnx` and `cgeorgiaw/Perch` declare `apache-2.0`; Google `perch` and `perch-hoplite` code is Apache-2.0.
* Not verified: the licence of the WEIGHTS on Kaggle (`google/bird-vocalization-classifier`). Read the Kaggle terms before publishing model files.

## Versioning

MINOR: new optional field or counter. MAJOR: changed meaning (rename, new unit, agreement rule). A reader seeing an unknown `v` skips the record.
