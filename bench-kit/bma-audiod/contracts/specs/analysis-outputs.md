contract: analysis-outputs
contract_version: 2.0.1
status: stable
producer: analysis/host.py + analysis/plugins.py
consumers: analysis/summarize.py, offline analysis
schemas: schemas/analysis_records.schema.json (analysis-records 1.3.0), schemas/analysis_status.schema.json (analysis-status 2.0.0); history: specs/analysis-history.md; clips (the one audio exception): specs/clips.md
examples: fixtures/dsp.jsonl, spatial.jsonl, tags.jsonl, clap.jsonl, birds.jsonl, novelty.jsonl, analysis_status.json, analysis_status_clips.json

# Analysis outputs

Files under <data-dir>/analysis (JSON Lines, one record per line, discriminated by `k`) plus status.json:

| file | `k` values |
|---|---|
| dsp.jsonl | dsp (1 Hz levels), transient, tonal_appeared, tonal_gone, tonal_state (every 10 s) |
| spatial.jsonl | spatial (1 Hz azimuth estimate), source |
| tags.jsonl | tags (PANNs AudioSet, 5 s windows per beam) |
| clap.jsonl | clap (zero-shot, top 3 prompts) |
| birds.jsonl | bird (BirdNET detections on the two strongest beams), bird_filtered (detections the location filter removed) |
| novelty.jsonl | novelty (scored, or the short warming_up shape), novel_event |

## Rules

* Scope: classification, embeddings and metadata. NO audio is written unless the analyzer starts with `--clip-audio`: flag default OFF; `pc/run_pipeline.ps1` passes it when `<root>\enable_clip_audio.txt` exists; `pc/install_task.ps1` and the container never pass it.
* Default clip capture (specs/clips.md) is visual-only: spectrogram PNG and signatures, BirdNET confidence strictly above 0.85, fail-closed speech guard, nothing below 3.5 kHz, 50 ms columns.
* With `--clip-audio` a short lossless clip is also written, deleted 24 h after capture, behind the fail-closed speech guard; a stored clip later confirmed to hold a human voice is purged (specs/clips.md).
* status.json `audio_written` is false unless such a file was written in this run; `status.json.clips` and `meta` counters report the capture. Record streams never contain audio.
* A window whose tagger speech probability is >= 0.30 stores no embedding (`speech_active` true iff `emb_stored` false).
* Beams are 1-based: beam b = bridge slot b-1 = Dante Tx b = FPGA matrix input b = vendor "Beam b+1". In per-beam arrays (`rms_dbfs`, `e_rel_db`) index 0 is beam 1.
* `t` = PC wall-clock Unix seconds of the analysis window.
* spatial is UNCALIBRATED: `calibrated` is false; `az_deg` is in the array's own frame. A calibrated value is a MINOR change.
* `bird.provenance` is always "model_opinion"; common/scientific/confidence may be null.
* `bird_filtered` (1.1.0): written by the birds plugin only with the county-level species filter on (`--geo-config`); kept and flagged, not dropped.
* `bird_filtered` fields: those of `bird` except `k` = "bird_filtered", `in_region` present and always false, plus `reject_reason`, `non_species`, `group`. No location. Readers wanting accepted detections read `bird`.
* novelty.score is rounded to 2 decimals after the decision novel = score > 3.0, so a novel record can read exactly 3.0.
* status.json: one entry per plugin: running (windows/records/errors/dropped/last_t/last_error), derived (novelty: only `note`) or off (`reason`). `device` is cpu or cuda:N.

### 1.2.0 additions (optional; readers ignore unknown fields)

* `bird` and `bird_filtered`: `in_region` (true / false / null = filter off or label not judged), `non_species` (sound class such as Engine or Dog).
* `group` = the `ui/taxa.json` string (bird, insect, amphibian, mammal, mechanical), `other-bio` for an unlisted genus, or null when the table was missing (counted in `meta.taxa_missing` / `meta.taxa_unmapped`).
* taxa.json owns the vocabulary; the analyzer reads it from `--taxa` or `taxa.json` next to host.py.
* `bird_filtered.reject_reason` = `geo_not_predicted`.
* Tonal lines (`tonal_appeared`, `tonal_gone`, `tonal_state`): `beam` (1..8 or null).
* `tonal_appeared.beam_margin_db` = per-beam excess of the line over its local floor on the last 1.37 s of the 10 s window. `beam` = beam with the highest excess when it leads the second by >= 1.5 dB and shows the line >= 10 dB over its floor, else null. Sticky per line: last confident beam.
* Masked (electronic) lines never produce appeared/gone, so `tonal_gone` has no electronic flag; the flag stays on `tonal_state` lines and in the hourly rollup (`electronic_line_s`).
* `spatial.decorr`: 1 - mean zero-lag Pearson correlation over the 8 ring-adjacent beam pairs (i, i+1 mod 8) of the 200-4000 Hz filtered 1 s window; [0, 1], 3 decimals. Unvalidated wind/rain proxy.
* `novelty` and `novel_event`: `run` (analyzer start, int), `mv` (model version string).
* status.json `meta` (analysis-status 1.1.0): history writer counters; see the schema.

## Not in the contract

`electronic` (tonal lines) is not typed here. Readers ignore unknown fields; adding them is MINOR (schema, fixtures, CHANGELOG in the same commit).
