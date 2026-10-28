contract: verdict-labels
contract_version: 1.1.0
status: stable
producer: tools/bma_labels.py (`add` writes labels/labels.jsonl; `queue` prints the review queue), libraries analysis/labels.py and analysis/review_queue.py
consumers: offline calibration and reliability work
schema: schemas/verdict_labels.schema.json (defs `verdict`, `review_queue`); examples: fixtures/verdict_labels.jsonl, fixtures/review_queue.json (regenerate with tools/make_label_fixtures.py)

# verdict-labels - owner verdicts and the review queue

Metadata only: no audio, spectrogram or embedding read or referenced.

## labels.jsonl (`<data-dir>/labels/labels.jsonl`)

One compact JSON object per line, LF only, UTF-8/ASCII, append-only, NEVER rotated, NEVER rewritten. Fields, all required:

* `k:"verdict"`, `v:1`, `label_id` (16 hex, random), `t_label` (Unix s).
* `event_id` (ui-http 1.3.0 id; numeric prefix = `t_start_ms`), `t_start_ms`, `species_key`.
* `verdict`: `correct|wrong|unsure|artifact`.
* `wrong_reason`: non-null iff verdict is `wrong`: `other_species|non_bird_sound|mechanical|insect_or_amphibian|no_sound_seen`.
* `corrected_key`: only with `other_species`; a species key seen in birds.jsonl; else null.
* `basis`: `field|clip_visual|metadata_only`. `clip_visual` records only that the owner looked; the tool never opens a clip.
* `source`: `cli|ui`. `stratum`, `p_include`, `queue_id` (null for an unsolicited label).
* `model`: snapshot at label time: `conf_max`, `n_beams`, `best_beam`, `group`, `in_region`. `model.group` is nullable; no logic may depend on it. `bma_labels stats` reports `model_group_null_rate`.
* `provenance:"owner_verdict"` (const).
* `model_sha256` (1.1.0, optional, 64 hex): digest `model.sha256` of the event's `bird` / `bird_filtered` rows (analysis-records 1.3.0); written only when every report of the event carries the same one.
* Join (1.1.0): a verdict applies to an event when `event_id` is equal and, when both carry a digest, the digests are equal. A verdict or event without a digest joins on `event_id` alone. A verdict under another digest is not applied.

Rules:

* `artifact` = non-biological or instrument artifact (electronic burst, housing tick). `non_bird_sound` = a real sound that is not a bird.
* No free text, note, URL, coordinate, audio or clip path field. The writer rejects unknown top-level keys.
* The schema rejects key names starting with `lat`, `lon`, `audio`, `path`, `note`, `text`, `url`, `clip`.
* Readers ignore unknown fields and skip a record whose `v` they do not know.
* Fold: per `event_id` the record with the greatest `(t_label, line number)` wins; earlier lines stay as history. A correction is a new line; a retraction is `unsure`. `bma_labels fold` prints the folded view, never edits the file.

### Writer

1. Create `<labels-dir>` if missing.
2. Take lock `labels.jsonl.lock`: `os.open(..., O_WRONLY|O_CREAT|O_EXCL|O_BINARY)`, retry every 3 ms for 10 s (then fail); a lock older than 60 s is removed.
3. Open `labels.jsonl` with `os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)`.
4. If the file is non-empty and its last byte is not `\n`, prefix the line with one `\n`.
5. ONE `os.write` of `compact-json + b"\n"`, then `fsync`.
6. Release the lock.

### Reader

* Whole lines only. A last line without a newline is partial: skipped and counted.
* A non-JSON-object line: skipped and counted (`bad_lines`), never fatal.
* An unreadable file is an error (exit 2).

## Event identity

`event_id` = `"%d-%s" % (int(t_start*1000), re.sub(r"[^a-z0-9]+", "-", key)[:60])`, key = `(scientific or common).strip().lower()`.

* Clusters exactly like the dashboard's `DetStore.add` (ui/bma_ui.py): ALL valid `bird` and `bird_filtered` rows, in file order, per species key.
* Valid row (`valid_bird`): beam 1..8, confidence 0..1, non-empty common, t > 0.
* A report merges when `t_start - 3.0 <= t <= t_end + 3.0`. `bird_filtered` counts as `in_region=false`.
* Event `in_region`: true when ANY report in region; false when any filtered and none in region; else null.
* Eligibility is applied to the event afterwards. test_labels.py compares the dashboard's id set with the library's.

## Speech guard

* An event is never queued and `add` refuses it when:
  * its label (common or scientific, case-insensitive) starts with `human` (`HUMAN_LABEL_PREFIXES`) or is one of the tagger's `SPEECH_LABELS`;
  * a `tags` row with `speech_active` or `speech_p >= 0.15` lies within 5 s of `[t_start, t_end]`;
  * NO `tags` row lies within that margin (fail closed; `--include-untagged` lifts only this rule).
* `add` answers every refusal (speech, untagged, unknown or rotated-out id) with the SAME message `not_labelable` and exit 2.
* Reasons exist only as counters in the queue (`excluded`), which never name a label.
* The tool reads only `birds` and `tags` (and `history/ambient_temp.jsonl` for the artifact probe); never `emb_*` or `novelty`.
* Human rows already in birds.jsonl stay: a BLOCKER for any later UI or listening endpoint.

## Review queue (`bma_labels queue`, shape `review_queue`)

Tool output, fixture only, never a LIVE file. Deterministic: equal inputs, `--now` and `--seed` give identical bytes and `queue_id`.

Strata (first match wins):

* `disagree`, flag `filtered_overlap` (geo-filtered report of another species within 5 s), `two_species_beams` (another in-region species on a disjoint beam set within 3 s), or `single_beam_high` (`conf_max >= near_hi` on one beam).
* `near`: `near_lo <= conf_max < near_hi`.
* `low`: `< near_lo`.
* `hi`: `>= near_hi`; reachable only through the audit slice.
* Defaults `near_lo` 0.5, `near_hi` 0.85.
* `conf_quantiles`: per species n, p25, p50, p75 of events' max confidence, nearest rank.

Eligibility (counted in `excluded` by the FIRST failing reason): `speech_label`, `speech_tag`, `speech_unknown`, `non_species` (flag or mechanical group), `region`, `already_labelled` (unless `--relabel`), `insect_excluded` / `not_insect`.

* `region`: `in_region` true, or null with `--include-unjudged-region`; geo-filtered never.
* The insect test uses the taxa table (`ui/taxa.json` genus list; `--taxa`), NEVER the records' `group`; without a usable table nothing is insect.

Selection of `--limit` slots:

1. Uncertainty part `round(limit*(1-audit_frac))`.
   * Candidates `disagree`, `near`, `low`. Within a species: `disagree` first, then distance of `conf_max` to the nearest of `{near_lo, near_hi}`.
   * Round-robin across species, rarest first, <= `--per-species-cap` per species; `p_include` null.
2. Audit part (remaining slots), stratified by species.
   * Frame = every eligible event not picked in (1), all strata (`hi` included).
   * Species s with R_s remaining events gets quota q_s by round-robin (rarest first, `q_s <= min(R_s, --audit-per-species-cap)`).
   * q_s events drawn uniformly without replacement; each has inclusion probability exactly `q_s / R_s`, recorded as `p_include`.
   * Frame in `audit.frame_n`, `audit.species{remaining,quota}`, `params.audit_frame_n`. Estimators use weights 1 / `p_include` over audit items.
   * Uncertainty picks MUST NOT be used for precision estimates.

Artifact probe (`--insect-mode only`, normally with `--include-unjudged-region`):

* Only insect events are eligible; stratum `artifact_probe`, selection `probe`.
* Picks spread round-robin over (UTC hour of day, temperature class cold < 5 C / warm / unknown); temperature by the rule of specs/ambient-temp.md.
* Geo-filter misses of European species come along only if `region` is also lifted (separate flags).

Output: exclusions are counts only; species common names appear; no audio, coordinates, county.

## Degradation

* birds missing or empty: `queue` exits 0 with `items: []`; `add` exits 2.
* tags missing: every event is `speech_unknown`, queue empty (fail closed), stderr says why.
* Corrupt label line: counted, skipped. Unreadable labels file: exit 2.
* The CLI never writes under `analysis/` or `history/` (`--out` there is refused).

## CLI

`bma_labels.py [--data-dir D] [--labels-dir D] [--history-dir D] [--taxa F] [--now T] queue|add|list|fold|stats|verify ...`

* Flags only; the only environment variable is the existing `BMA_DATA_DIR` default.
* Exit 0 ok, 1 `verify` found problems, 2 refused / unreadable / usage.
