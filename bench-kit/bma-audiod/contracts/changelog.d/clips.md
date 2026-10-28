## clips 1.3.0 - 2026-10-27 (configurable certainty threshold; analysis/clips.py)

- clips 1.3.0 - minor: `confidence_max` / `conf` is strictly above the configured `--clip-min-conf` (default 0.85, owner live value 0.3), schema `exclusiveMinimum` 0.85 -> 0. Readers must not assume 0.85. Events are staged from `min(0.5, --clip-min-conf)` (default behaviour unchanged). `--clip-min-conf` outside 0.05 to 1 is refused at start. `--clip-skip-out-of-region` unchanged. Fixture `clips_index_lowconf.jsonl` (confidence_max 0.31), tools/make_clips_fixtures.py.
