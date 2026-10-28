contract: pad-lane
contract_version: 1.1.0
status: experimental
producer: analysis/host.py plugins `tags_pad`, `clap_pad` (analysis/pad_lane.py); flags `--tags-pad-s S`, `--clap-pad-s S` (default 0 = off), `--no tags_pad`, `--no clap_pad`
consumers: tools/pad_lane_report.py, tools/eval_models.py (branch evaluator)
schema: schemas/pad_lane.schema.json; examples: fixtures/tags_pad.jsonl, fixtures/clap_pad.jsonl (regenerate with tools/make_pad_lane_fixtures.py)

# pad-lane

* Files (optional): `analysis/tags_pad.jsonl` (k `tags`), `analysis/clap_pad.jsonl` (k `clap`). Absent unless the flag is set. tags.jsonl / clap.jsonl are never written or changed by the lane.
* Input: the production 5 s window, best beam, same model and weights (loaded once, shared with the production plugin when it runs). tags_pad: 32 kHz window + zeros to S s. clap_pad: 48 kHz window + zeros to S s (production clap: processor repeat-pad). No pre-cleaning.
* Record = production tags / clap record (with its analysis-records 1.3.0 provenance since 1.1.0: `model` {name, version, variant, sha256}, `claim`, `top_k`, `max_unstored_score`) + `variant` "pad10", `pad_s` (S), `ms` (wall ms of the padded model call: tags = PANNs inference; clap = processor + audio tower + softmax).
* tags_pad: padded speech_p >= 0.30 or unpadded speech_p >= 0.30 (second inference on the same window, as production) -> no record, no embedding. `speech_p` in a record = padded value. Records always speech_active false, emb_stored false. clap_pad: no speech guard (as production clap; prompts contain no speech).
* Cost: status.json plugins.tags_pad / clap_pad gain `pad_s`, `ms_per_window`, `ms_per_audio_s` (mean over all windows), tags_pad also `ms_ref_per_window` (mean unpadded call, same process). Not part of the 30 ms/s always-on budget: opt-in.
* Same window grid as production (5 s / 2 s tags, 5 s / 5 s clap); pair by nearest `t` (tools/pad_lane_report.py, 0.5 s).
* Records carry no audio, no embedding. Unknown fields ignored by readers.
* `model` = the production plugin's own block (same weights: name, version, sha256 identical to the unpadded record's) with `variant` "pad10" (= `variant`). `top_k` = the 5 best labels of the padded output, no floor (tags: human-voice classes not listed); `max_unstored_score` = the 6th best (null when the model has no more labels). The new fields are optional for readers: 1.0.0 files lack them.
