# label-store: verdict label store and uncertainty-sampled review queue

Roadmap B3, id `label-store`. Contract: `contracts/specs/verdict-labels.md` 1.0.0.

## 1. Design

### 1.1 Files (bench-kit/bma-audiod)
- `analysis/labels.py`: `event_id`, `iter_events`, `LabelStore` (append, fold, validate), `speech_guard`.
- `analysis/review_queue.py`: `build_queue(events, labels, params) -> list[QueueItem]`, pure.
- `tools/bma_labels.py`: subcommands `add`, `list`, `fold`, `stats`, `queue`, `verify`; runs from a flat app directory (three modules + `taxa.json` side by side).
- `analysis/test_labels.py`, `analysis/test_review_queue.py`, `contracts/tools/make_label_fixtures.py`, `analysis/run_label_mutations.py`.
- Contract: `contracts/specs/verdict-labels.md`, `contracts/schemas/verdict_labels.schema.json`, `contracts/fixtures/verdict_labels.jsonl`, `contracts/fixtures/review_queue.json`, CHANGELOG `verdict-labels 1.0.0`.
- Flags (CLI only): `--data-dir` (default `BMA_DATA_DIR`), `--labels-dir` (`<data-dir>/labels`), `--days 14`, `--now`, `--seed 0`,
  `--per-species-cap 25`, `--limit 200`, `--near-lo 0.5`, `--near-hi 0.85`, `--audit-frac 0.2`, `--audit-per-species-cap 5`,
  `--speech-margin-s 5`, `--speech-p-min 0.15`, `--include-untagged` (off), `--include-unjudged-region` (off),
  `--insect-mode exclude|only`, `--relabel` (off), `--out <file>`.
- Never writes under `analysis/` or `history/`.

### 1.2 Event identity
`event_id = "%d-%s" % (int(t_start*1000), re.sub(r"[^a-z0-9]+", "-", key)[:60])`, key = `(scientific or common).strip().lower()`;
- matches ui-http regex `[0-9]{1,16}-[a-z0-9-]{0,60}`.
- Parity test runs `bma_ui` on the same rows (including a fixture with a bridging out-of-region report, and with `--real-birds FILE` a real file) and compares ids and fields.
- Each label also stores `t_start_ms` and `species_key`.
- The library copies `DetStore.add` (all valid `bird` and `bird_filtered` rows, file order, per species key) and applies eligibility per event afterwards (event `in_region` = any report in region).

### 1.3 Record `labels/labels.jsonl` (verdict-labels 1.0.0)
One compact JSON object per line, LF, UTF-8, append-only, never rotated or rewritten. Readers ignore unknown fields; the writer rejects unknown top-level keys.

```
{"k":"verdict","v":1,
 "label_id":"<16 hex, random>",
 "t_label":1793000000.5,
 "event_id":"1792940400000-red-bellied-woodpecker",
 "t_start_ms":1792940400000,
 "species_key":"melanerpes carolinus",
 "verdict":"correct",          // correct | wrong | unsure | artifact
 "wrong_reason":null,          // only when verdict=wrong: other_species | non_bird_sound | mechanical | insect_or_amphibian | no_sound_seen
 "corrected_key":null,         // only with wrong_reason=other_species; a species_key seen in birds.jsonl; pattern [a-z0-9 .-]{1,60}
 "basis":"field",              // field | clip_visual | metadata_only
 "source":"cli",               // cli | ui
 "stratum":"near", "p_include":0.41, "queue_id":"<12 hex>|null",   // copied from the queue item; null if unsolicited
 "model":{"conf_max":0.78,"n_beams":2,"best_beam":6,"group":"bird","in_region":true},
 "provenance":"owner_verdict"}
```
- `artifact` = non-biological/instrument artifact (scores B1); `non_bird_sound` = real sound that is not a bird. `model.group` nullable; `stats` reports `model_group_null_rate`.
- No free text, note, URL, coordinates, audio reference, clip path.
- Model snapshot copied at label time (birds.jsonl rotates at 64 MB).
- Fold: per `event_id` the record with the greatest `(t_label, line number)` wins; correction = new line; retraction = `unsure`. `fold` never edits the file.
- Trailing partial line skipped and counted; next writer starts a new line.
- Writer: `O_RDWR | O_APPEND | O_CREAT | O_BINARY`, one `os.write` of the whole line (< 512 bytes), `fsync`, serialised by an `O_EXCL` lock file (stale after 60 s); no CR byte.

### 1.4 Eligibility (first failing reason recorded in `excluded{}`)
1. `speech_label`: label (common or scientific, case-insensitive) is `Human*` or in analyzer `SPEECH_LABELS` (`analysis/plugins.py`); counted as a number only, never printed. `HUMAN_LABEL_PREFIXES = ("human",)` pinned against the live taxa table.
2. `speech_tag`: a `tags` row with `speech_active == true` or `speech_p >= --speech-p-min` (0.15, half the tagger's 0.30 cut) within `--speech-margin-s` (5 s) of `[t_start, t_end]`, any beam.
3. `speech_unknown` (fail closed): no `tags` row within the margin; lifted by `--include-untagged`.
4. `non_species`: need `non_species != true` and group not `mechanical`.
5. `region`: need `in_region == true` (or `--include-unjudged-region`).
6. `already_labelled`: folded verdict exists (unless `--relabel`).
7. `insect_group_pending`: insect events (genus list from `ui/taxa.json`; the `group` field is null on most rows) excluded (`--insect-mode exclude`). `--include-group insect` lifts it (not implemented).
  - `--insect-mode only` + `--include-unjudged-region` = artifact-probe queue (stratum `artifact_probe`, picks spread over UTC hour and ambient temperature class).

### 1.5 Queue algorithm (pure, deterministic)
Strata (first match wins):
- `disagree`: (a) a `bird_filtered` report of another species within +-5 s of an in-region event; (b) two different in-region species on different beams within +-3 s; (c) conf_max >= near-hi with n_beams == 1 (weak flag: beams identical below 250 Hz, MSC 0.6-0.67 above).
- `near`: `near-lo <= conf_max < near-hi`.
- `low`: `conf_max < near-lo`.
- `hi`: `conf_max >= near-hi` (audit slice only).
`near-lo` 0.5 / `near-hi` 0.85 are flags; the queue carries `conf_quantiles` per species.

Selection (`--limit L`, `--per-species-cap C`, `--audit-frac f`):
1. Uncertainty part, `round(L*(1-f))` slots, from `disagree`, `near`, `low`. Score = distance of `conf_max` to nearest of `{near-lo, near-hi}` (small first);
  - `disagree` first. Round-robin across species, rarest first, until each species reaches C or slots run out. Items carry `selection:"uncertainty"`, `p_include: null`.
2. Audit part: stratified by species over every eligible event not picked by the uncertainty part; species s gets quota q_s (round-robin, rarest first, at most `--audit-per-species-cap`), `p_include = q_s / R_s`;
  - `random.Random(seed)` over events sorted by `event_id`. Recorded: `audit.frame_n`, `audit.species{remaining,quota}`, `params.audit_frame_n`; sum of `1/p_include` over audit items equals frame size.
3. `queue_id` = first 12 hex of sha256 of sorted event ids + params JSON (identical inputs give identical bytes).
4. Output `review_queue.json`:
   `{k:"review_queue", v:1, queue_id, t_built, params, window:{days,t0,t1}, eligible:{events,species,by_stratum},
   excluded:{speech_label,speech_tag,speech_unknown,non_species,region,already_labelled,insect_group_pending},
   depth:{species_key:{near,disagree,low,hi}}, items:[{event_id,species_key,common,t_start_ms,t_end_ms,conf_max,n_beams,best_beam,stratum,selection,p_include,flags[]}]}`.
  - No audio, coordinates, county.

`bma_labels add --event-id ID --verdict correct --basis field` re-resolves the id against birds.jsonl;
- exit 2 with one refusal code `not_labelable` for a speech event, untagged event or unknown id (label never named). `add --queue FILE` copies `stratum`, `p_include`, `queue_id`. The tool never reads `emb_*` or `novelty`.

### 1.6 Degradation
- birds.jsonl missing/empty: `queue` exits 0, `items: []`; `add` exits 2.
- tags.jsonl missing: every event `speech_unknown`, queue empty, stderr says why.
- labels.jsonl corrupt line: counted (`bad_lines`), skipped; unreadable file exits 2, no queue built.

### 1.7 Budget
14-day clustering + sampling < 5 s, < 100 MB RSS. < 512 bytes per verdict. No GPU.

## 2. Contract
- `verdict-labels` 1.0.0: spec, schema `verdict_labels.schema.json` (defs `verdict`, `review_queue`; `x-def-files` equals the table), fixtures, CHANGELOG line `verdict-labels 1.0.0`,
- `LABEL_FIXTURES = {"review_queue.json": "review_queue"}` validated against its own `$defs` entry (`check_label_fixtures`), `labels/labels.jsonl` optional live file, queue fixture only.
  - `test_contracts.py` covers missing fixture, wrong def, a `lat` key.
- Invariants: `verdict` enum; `wrong_reason` required iff verdict is `wrong`; `corrected_key` only with `other_species`; `provenance` const `owner_verdict`; `t_start_ms` consistent with id prefix (tested); no `lat/lon/latitude/longitude`, `audio*`, `path*`, `note*`, `text*` keys.
- No change to analysis-records, ui-http, analysis-history. The future UI endpoint is a separate ui-http minor.

## 3. Tests
Fixtures: synthetic birds.jsonl (40 rows, 6 species, confidences 0.30, 0.55, 0.84, 0.85, 0.90...), tags.jsonl, empty and populated labels.jsonl.
1. `event_id` = `"1792940400123-red-bellied-woodpecker"`; gap 3.0 s merges, 3.001 s splits.
2. Parity with `ui/bma_ui.py`: ids, t_start, count.
3. Strata: 0.499 low, 0.500 near, 0.849 near, 0.850 hi/disagree; exact counts.
4. Cap: 60 eligible with cap 25 yields 25; 10 yields 10.
5. Round-robin: `--limit 12`, 6 species x 30 -> 2 each.
6. Determinism: same seed byte-identical + equal `queue_id`; different seed: same uncertainty picks, different audit picks.
7. Audit `p_include = q_s / R_s` exactly; sum of `1/p_include` = frame size.
8. Fold: later `t_label` wins; reversed, the other; equal, later line.
9. Writer: partial last line skipped/counted, new record parses; two real processes x 500 appends = 1,000 valid lines, none interleaved; lock behaviour has its own tests; no CR byte.
10. Schema: each invariant has a failing and a passing example (`pytest.raises(SpecificError, match=...)`).
11. Speech: Human vocal event, `speech_active` 4.9 s away, speech_p 0.15, no tags row: each excluded with exact reason; nearest speech tag at 5.1 s and speech_p 0.14 included. `add` on each excluded id exits 2 with exact message, file bytes unchanged.
12. No tags file: empty queue, `speech_unknown` = event count; `--include-untagged` queues them.
13. Guard label list contains every entry of `plugins.SPEECH_LABELS`.
14. `check_contracts.py` and `check_private_paths.py` clean; fixture with `lat` rejected.
15. Real-data smoke: event and species counts reported.

Mutations to RUN (each turns the named test red; `analysis/run_label_mutations.py`):
M1 delete `speech_tag` check (11); M2 delete `speech_unknown` branch (11, 12); M3 remove Human guard in `add` (11); M4 gap 5.0 s (1, 2); M5 `<` to `<=` at near-hi (3); M6 drop cap (4); M7 unseeded RNG (6); M8 fold picks earliest (8);
- M9 wrong audit denominator (7); M10 allow `provenance` = `model_opinion` (schema).

## 4. Acceptance

`bma_labels queue --days 14 --now <fixed> --seed 0`:
- >= 5 species with depth >= 20 in `near + disagree + low`; near-only reported separately.
- Zero queued events with a Human label, `speech_active` within 5 s, or no tags row, checked by an independent script; M1 makes the count > 0.
- Two runs identical bytes; wall < 10 s; RSS < 200 MB.
- Eligible counts reported per exclusion reason.
