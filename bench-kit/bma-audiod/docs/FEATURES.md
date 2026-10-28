# Feature registry

Every opt-in, off-by-default or non-default-live feature is a row. A feature that is not a row fails `scripts/test_features_registry.py`.

| Term | Meaning |
|---|---|
| Keys | exact flag, marker file or tool path the row covers; the first key identifies the row in `tools/feature_state.py` |
| Default | ON or OFF with no flag, file or marker |
| Approved | Y owner decided; N owner refused; pending no recorded owner decision |
| Live state | not stored here: `python tools/feature_state.py --root <data root>` (OFF rows first, `N features OFF`); the supervisor writes it to `logs\features.log` and `reports\features.txt` at start and daily |
| Cost | measured numbers from this repo; `unmeasured` = no number in the repo |
| Marker files | in `<root>` (the supervisor data dir) unless the row says `app\` |
| Docker | no markers are read; pass the same flags as `command:` arguments (`docker/entrypoint.sh` appends them); the entrypoint uses `config/geo_county.json` and the tonal mask baked in |

## Opt-in / off by default or non-default live setting

| Feature | Keys | What | Default | Enable | Cost | Approved |
|---|---|---|---|---|---|---|
| Mixscore plugin | `--mixscore` `--mixscore-every` `mixscore.json` `--no mixscore` `analysis/mixscore_report.py` | BirdNET on 8-beam mean; owner 2026-10-27 "enable mixscore at the next restart": 24 h experiment, read with `analysis/mixscore_report.py` | OFF | `--mixscore` in `analyzer_args.txt`, or `app\mixscore.json` (`{}` or `{"every": N}`) | 52 ms CPU per s audio at N=1 (158 ms per 3 s window, `analysis/bench_plugins.py`, `contracts/specs/mixscore.md`), 750 MB RAM, divided by N (default 4); jsonl 64 MB x 2 | Y |
| Pad lane (experimental) | `--tags-pad-s` `--clap-pad-s` `tools/pad_lane_report.py` | PANNs / CLAP on the window zero-padded to S s, beside tags / clap; owner delegated 2026-10-27 | OFF | `--tags-pad-s 10` and/or `--clap-pad-s 10` in `analyzer_args.txt`; off again with `--no tags_pad` | measured per window in `status.json` plugins.tags_pad.ms_per_window (and `ms_ref_per_window`); jsonl 64 MB x 2 | Y |
| Perch shadow plugin | `--perch-model` `--perch-labels` `--perch-threads` `--perch-min-logit` `--no perch` `tools/perch_report.py` | Perch 2.0 species shadow; owner 2026-10-26 "enable perch on the GPU" | OFF | `--perch-model <onnx>` line in `analyzer_args.txt` (fetch: `tools/fetch_perch.py`) | CPU/GPU/VRAM unmeasured; jsonl 64 MB x 2 | Y |
| Perch device | `--perch-device` | Perch on GPU; owner 2026-10-26 "enable perch on the GPU" | OFF | `--perch-device cuda:N` or `auto` in `analyzer_args.txt` (default `cpu`) | unmeasured | Y |
| Perch ONNX runtime dir | `--perch-ort-dir` | onnxruntime-gpu overlay path; owner 2026-10-26 "enable perch on the GPU" | OFF | `--perch-ort-dir <dir>` in `analyzer_args.txt` | none | Y |
| Perch embeddings | `--perch-embeddings` | store 1536-d float16 embeddings; owner delegated 2026-10-27: keep; non-speech windows only, capped | OFF | `--perch-embeddings` in `analyzer_args.txt` | disk at most 128 MiB (2 x 64 MiB) | Y |
| Clip audio | `--clip-audio` `enable_clip_audio.txt` | store lossless wildlife audio; owner 2026-10-26/27 "Keep the audio clips and purge confirmed human voices", "opt in to recording clips"; conditions: speech guard, human-voice purge, 24 h retention | OFF | file `<root>\enable_clip_audio.txt` (any content), read at analyzer start | disk cap 512 MB (`--clip-audio-cap-mb`), 24 h retention; stores audio | Y |
| Clip PCEN spectrogram | `--clip-pcen` | PCEN-normalised clip PNG; owner delegated 2026-10-27: review waterfall (owner: no waterfall to review) | OFF | `--clip-pcen` in `analyzer_args.txt` | per clip render only; bench in `analysis/bench_plugins.py` | Y |
| Clip confidence gate | `--clip-min-conf` | clip BirdNET threshold; owner 2026-10-27 "Use 0.3" (strictly above 0.3) | OFF | `--clip-min-conf X` in `analyzer_args.txt` (default 0.85 when absent) | lower value stores more clips: disk, review load | Y |
| Clip out-of-region skip | `--clip-skip-out-of-region` | drop out-of-region clips; owner 2026-10-27 "skip out-of-region clips" | OFF | `--clip-skip-out-of-region` in `analyzer_args.txt` | none | Y |
| Clip path overrides | `--clips-dir` `--clip-exclude` | clip dir and exclusion file; opt-in, stays OFF unless set; owner delegated 2026-10-27 | OFF | line pairs in `analyzer_args.txt` | none | Y |
| Analyzer args file | `analyzer_args.txt` | extra host.py arguments | OFF | file `<root>\analyzer_args.txt`: one argument per line, `#` comments, read at analyzer start | none | Y |
| Tonal mask | `--tonal-mask` `--tonal-mask-tol` `tonal_mask.json` | mask the unit's own lines | OFF | `app\tonal_mask.json` present (launcher adds `--tonal-mask`) | none | Y |
| County species filter | `--geo-config` `geo_county.json` | birds location/date filter | OFF | `app\geo_county.json` present (launcher adds `--geo-config`); also enables the hourly ambient fetch | none | Y |
| Analyzer path overrides | `--taxa` `--history-dir` `--calibration` | taxa, history, calibration paths; opt-in, stays OFF unless set; owner delegated 2026-10-27 | OFF | line pairs in `analyzer_args.txt`; `--history-dir ''` disables history | none | Y |
| Analyzer self-exit timer | `--secs` | exit after N seconds (tests) | OFF | `--secs N`; tests only | none | N |
| Indices without speech gate | `--indices-without-speech-gate` | indices without tagger | OFF | `--indices-without-speech-gate` in `analyzer_args.txt` | removes the speech guard from indices records | N |
| Beam shift | `--beam-shift` | per-beam sample correction; owner delegated 2026-10-27: coherence xc_lag pairs 1/7/8 -1.24/-0.99/+1.69 -> -0.29/-0.05/-0.35 samples after the shift | OFF | `$BMA_BEAM_SHIFT` in `bma.local.ps1` (8 integers, -4..4) or `--beam-shift` in `analyzer_args.txt` | none | Y |
| Spectrum rows to dashboard | `--spectrum-port` | dashboard spectrum rows | OFF | supervisor passes it; manual `--spectrum-port N` | bench (`pc/SPECTRUM.md`): spectrum process 13.4 % of one core, bridge 72.4 %; live audit 2026-10-27: spectrum 15.9 %, bridge 79.9 % | Y |
| Spectrum detail tier | `--detail` `--spectrum-detail` | 50 rows/s detail bands; opt-in, stays OFF unless set; owner delegated 2026-10-27 | OFF | `--detail` on `bma_spectrum.py` and `--spectrum-detail` on the dashboard (needs `--spectrum-port`); supervisor never passes either | bench: spectrum process 22.7 % of one core, bridge 73.2 %; live not measured | Y |
| Next dashboard | `--ui-next` `enable_ui_next.txt` | the ui-next shell under `/next/` over the existing API, rows pushed on the live WebSocket `/api/v2/live` (ui-http 1.13.0, ui-live 1.0.0); opt-in, stays OFF unless set | OFF | `<root>\enable_ui_next.txt` (any content; the supervisor adds `--ui-next`) or `--ui-next` by hand; the installer copies the assets to `<ui dir>\next` | none (static files and the same polled API as the current tabs) | pending |
| Bridge spectrum fan-out | `--all-to` `--to` `--status-file` `--state-file` | bridge extra send targets | OFF | `--all-to HOST:PORT` (supervisor passes three) | 2.3 points of a core per target (`pc/SPECTRUM.md`) | Y |
| Bridge manual addressing | `--iface-ip` `--card` `--bind` | pin card, interface, test bind | OFF | arguments on `dante_bridge.py`; `--bind` is TEST ONLY | none | N |
| Dashboard TLS | `--tls-cert` `--tls-key` `dashboard_cert.pem` `dashboard_key.pem` | serve HTTPS | OFF | `<root>\tls\dashboard_cert.pem` + `dashboard_key.pem` (`pc/setup_tls.ps1`), or the same pair in `<root>` | none | Y |
| Tailnet identity login | `--tailnet-identity` `--allowed-users` `allowed_users.txt` | accept Tailscale login header | OFF | `<root>\allowed_users.txt` with one login per line (supervisor adds the flags) | none | Y |
| Identity only | `--require-identity` | refuse token, Basic, cookie, passkey; opt-in, stays OFF unless set; owner delegated 2026-10-27 | OFF | `--require-identity` with `--tailnet-identity`; supervisor never passes it | none | Y |
| Passkeys | `--passkey-origin` `--passkey-file` `enable_passkeys.txt` `tls_name.txt` | WebAuthn sign-in | OFF | `<root>\enable_passkeys.txt` + `<root>\tls\tls_name.txt` (supervisor adds origins) | none | Y |
| Dashboard IP hosts | `--allow-ip-hosts` `--allow-host` | accept IP-literal Host headers | OFF | supervisor passes `--allow-ip-hosts` and one `--allow-host` per name | none | Y |
| Dashboard token file | `--auth-token-file` | login token path | OFF | supervisor passes `<root>\dashboard_token.txt` | none | Y |
| Dashboard without login | `--no-auth` | UNSAFE: no login | OFF | `--no-auth`; supervisor never passes it | exposes the dashboard | N |
| Dashboard board panel | `--board-url` `--token-file` `--no-board` | dspd board panel | OFF | `--board-url` from `$BMA_BOARD` (supervisor); `--no-board` when unknown | none | Y |
| Dashboard misc | `--labels-dir` `--slot-fix-at` | label dir, slot-fix time; opt-in, stays OFF unless set; owner delegated 2026-10-27 | OFF | arguments on `bma_ui.py`; supervisor passes neither | none | Y |
| Review without clips | `--review-allow-no-clip` | Review queue also serves events with no clip; opt-in, stays OFF unless set; owner delegated 2026-10-27 | OFF | argument on `bma_ui.py`; supervisor does not pass it | none | Y |
| Cable sink recording | `--record` `--seconds` | write the 16-channel stream to a WAV; owner delegated 2026-10-27: manual operator tool only, never supervised, output deleted within 24 h (`tools/audio_sweep_check.py` lists leftovers) | OFF | `bma_cable_sink.py --record <file> --seconds N` by hand, no audio device needed; STORES audio outside the clip policy (no speech guard, no retention) | 2,304,000 B per recorded second (16 ch x 3 B x 48000 Hz, computed) | Y |
| Report tools by hand | `tools/loss_report.py` `tools/burst_source_report.py` `analysis/beam_bands_report.py` `analysis/reliability_report.py` | offline reports | OFF | run by hand: `python -I <tool> ...` | read-only, one pass over the jsonl files | Y |

## ON by default, removable

| Feature | Keys | What | Default | Enable | Cost | Approved |
|---|---|---|---|---|---|---|
| DSP plugin | `--no dsp` | per-beam DSP records | ON | remove with `--no dsp` | all plugins share about 30 ms CPU per s audio (`analysis/bench_plugins.py`) | Y |
| Spatial plugin | `--no spatial` | direction of arrival | ON | remove with `--no spatial` | in the 30 ms budget | Y |
| Tags plugin | `--no tags` | PANNs tags, speech gate | ON | remove with `--no tags`; the clip speech guard and indices need it | in the 30 ms budget | Y |
| CLAP plugin | `--no clap` | zero-shot audio labels | ON | remove with `--no clap` | in the 30 ms budget | Y |
| Birds plugin | `--no birds` | multi-beam BirdNET | ON | remove with `--no birds`; clips and mixscore need it | in the 30 ms budget | Y |
| Coherence plugin | `--no coherence` `--coherence-interval-s` `analysis/coherence_report.py` | beam coherence records | ON | remove with `--no coherence` | in the 30 ms budget | Y |
| Indices plugin | `--no indices` `--indices-interval-s` `--indices-hop-s` `analysis/indices_report.py` | acoustic indices | ON | remove with `--no indices` | at the 30 ms limit (`analysis/bench_plugins.py`) | Y |
| Bursts plugin | `--no bursts` `--bursts-interval-s` `analysis/bursts_report.py` | impulse burst stats | ON | remove with `--no bursts` | in the 30 ms budget | Y |
| Artifact flags (dashboard) | `--no-artifact-gate` | advisory artifact flags on insect events, `artifact` filter; owner delegated 2026-10-27 | ON | remove with `--no-artifact-gate` on `bma_ui.py`; supervisor does not pass it | read time only, no analyzer CPU | Y |
| Wildlife clips | `--no clips` | wildlife clips: spectrogram PNG + signatures; audio only when Clip audio is ON | ON | remove with `--no clips` | disk cap 256 MB sidecars (`--clip-sidecar-cap-mb`) | Y |
| Legacy classifier | `classify/bma_classify.py` `--udp` `--wav` `--stdin-raw` `--slot` `--subscribe` `--fill-gap-ms` `--wav-chunk` `--idle-exit-s` `--lat` `--lon` | second BirdNET on bridge slot 0, outside the analyzer plugin set; owner delegated 2026-10-27: in use since 2026-10-11, stays until the analyzer birds plugin covers its output | ON | no switch: the supervisor starts it through `pc/run_classifier.sh` (`geo_county.json` adds the county filter); detections go to `logs\detections.jsonl` | outside the analyzer 30 ms budget, CPU unmeasured; `detections.jsonl` and `classifier.log` have no rotation or cap (detections.jsonl 32 MB on 2026-10-27) | Y |
| PANNs embedding store | `analysis/plugins.py` | 2048-d float16 PANNs embedding per non-speech tags window; feeds Novelty; owner delegated 2026-10-27: keep; 14 day retention to implement | ON | no switch: written whenever `tags` runs; `--no tags` removes it, Novelty and the speech gate | 4096 B per window, 50 to 100 MB per day (`emb_YYYYMMDD.f16`, 2026-10-11..27), `.jsonl` 0.2 to 1.1 MB per day; no cap, no retention | Y |
