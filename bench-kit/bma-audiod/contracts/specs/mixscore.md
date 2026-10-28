contract: mixscore
contract_version: 1.0.0
status: stable
producer: analysis/host.py plugin `mixscore` (analysis/mixscore.py); flags `--mixscore` (default OFF), `--mixscore-every N`, `--no mixscore`
consumers: analysis/mixscore_report.py, offline analysis
schema: schemas/mixscore.schema.json; example: fixtures/mixscore.jsonl (regenerate with tools/make_mixscore_fixtures.py)

# mixscore - BirdNET on the mixed signal versus per-beam BirdNET

Scores the 8-beam mean of every 3 s window with BirdNET and once a minute compares it with what the `birds` plugin reported for the same windows.

* OFF by default: `status.json` lists `plugins.mixscore = {"enabled": false, "reason": "off by default (--mixscore)"}` and no `mixscore.jsonl` is created. `--no mixscore` wins over `--mixscore`.
* The plugin refuses to start (disabled with a reason in status.json) unless the `tags` and `birds` plugins are running.
* A failure inside the running plugin is counted (`plugins.mixscore.errors`, `meta.mixscore_errors`) and never stops the window loop.

## The mix

* `m` = mean of the 8 aligned beams (window already carries `--beam-shift`), scaled by `g = min(sqrt(mean beam power / power(m)), sqrt 8)`.
* Identical beams: g = 1, mix = beam bit for bit. Uncorrelated beams: g = sqrt 8 (9.03 dB), also the cap. An exactly cancelling window gives a zero mix, gain 0.
* `mix_gain_db` = 20 log10 g.
* Gate: loudest beam below `gate_dbfs` (-70): window not scored by either plugin (`windows.gated`).

## Files and records

`<--out>/mixscore.jsonl` via `hist.RotatingJsonl` (64 MB x 2). Compact JSON, LF, discriminator `type`.

* `mixscore`: one per scored window with >= 1 mix detection (>= `min_conf` 0.25, human labels removed): `t` (window end), `level_dbfs`, `mix_dbfs`, `mix_gain_db`, `n_filtered` (detections the county location filter removed; names not recorded), `top`.
* `top`: <= 5 detections, highest confidence first: `common`, `scientific`, `confidence`, `in_region`, `non_species`, `group`.
* Windows without a detection write no record (counted in the minute record).
* Records are written when the window is released (after the speech hold, up to 30 s late): the file is not ordered by `t`.
* `mixscore_compare`: one per UTC minute, written 45 s after the minute ended or at shutdown (`partial` true).

## Comparison definitions

* WINDOW = one 3 s analysis window. PAIRED when the birds plugin reported on it (also with no detection), with a window end within 1.5 s of its own; handed over in-process. A scored window without a birds report after 30 s is UNPAIRED and takes no part in event counts.
* EVENT = one species (scientific name, else common name) in one paired window.
* Per-beam side: species in the window's `bird` records (two loudest beams, in-region only, human labels removed); best-beam confidence = HIGHEST over those beams.
* Mix side: species in the mix result (>= min_conf, in-region, human labels removed).
* `events.both`: species in both sides of a window; `beam_only`; `mix_only`. Counted per window.
* `both_conf` over `both` events: `n`; `best_beam_mean`; `mix_mean`; `diff_mean` = mean (mix confidence - best-beam confidence); `mix_higher` = count with mix confidence strictly greater. All null (mix_higher 0) when n is 0.
* `groups`: the same three counts per `group` (ui/taxa.json stamp; `unknown` when the table is missing).
* `windows`: `gated`, `scored`, `paired`, `unpaired`, `dropped_speech`, `dropped_unknown`.
* Share of per-beam events the mix misses = `beam_only / (both + beam_only)`; share the mix finds that per-beam does not = `mix_only / (both + mix_only)`.
* `mix_gain_db_mean`: mean `mix_gain_db` of scored windows (null when none).

## Speech rule

* A window is released only after the tagger has reported on it.
* DROPPED (`dropped_speech`: no record, no species count) when any tags window [t-5, t] overlapping the mix window [t-3, t] has `speech_active`.
* No speech flag known but the tags windows seen do not cover the window (tags disabled, failing, or slower than 30 s): `dropped_unknown` (fail closed).
* The 30 s hold plus processing means a minute is emitted 45 s after it ends.
* Human-voice labels (BirdNET names beginning `Human `) are dropped on both sides and never reported.

## Privacy

* Metadata only. Audio exists in RAM for the call. No embedding produced or stored.
* No coordinates: the location filter uses the county-level geo config of `birds`, nothing of it written.
* The schema rejects `lat`/`lon`, `audio`, `emb`, `embedding`, `samples` keys.

## Cost

Second BirdNET pass per scored window: 52 ms CPU per second of audio, 750 MB more resident memory. `--mixscore-every N` scores every N-th window (default 4; below 4 the host prints a warning).
