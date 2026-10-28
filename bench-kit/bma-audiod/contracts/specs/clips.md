contract: clips
contract_version: 1.3.0
status: stable
producer: analysis/host.py with analysis/clips.py, clip_features.py, clip_render.py (analysis/hist.py writes the index)
consumers: offline review (no UI or HTTP route)
schema: schemas/clips.schema.json; fixtures: clips_index.jsonl, clip_sidecar.json, clip_sidecar_audio_expired.json, analysis_status_clips.json (audio mode); clips_index_lowconf.jsonl (--clip-min-conf 0.3); clips_index_visual.jsonl, clip_sidecar_visual.json, analysis_status_clips_visual.json (default); tools/make_clips_fixtures.py

# Wildlife clips

Policy: keep a short clip of a WILDLIFE sound ONLY when certainty is strictly above `--clip-min-conf` (default 0.85, accepted 0.05 to 1; the live analyzer runs 0.3, owner 2026-10-27). Audio is held 24 hours when enabled.

* AUDIO IS OFF BY DEFAULT. Without `--clip-audio` no audio file (flac, wav, PCM) is written and the audio encoder is not loaded.
* Default capture is visual-only: spectrogram PNG + signature JSON + index record. Same events and rules (wildlife group, certainty strictly above `--clip-min-conf`, beam agreement, caps, fail-closed speech guard).
* `pc/install_task.ps1` and the container entrypoint never pass `--clip-audio`; `pc/run_pipeline.ps1` passes it only where `<root>\enable_clip_audio.txt` exists.
* `--clip-audio`: lossless clip, deleted after `--audio-retention-h` (24 h).
* Human-voice purge (1.1.0): while any audio file is stored, each retention sweep re-checks every stored clip (audio and visual-only) against `<out>/tags.jsonl` and `<out>/birds.jsonl` (last 8 MB of each; garbage lines skipped).
* A clip is CONFIRMED human when, within [t_start - guard_s, t_end + guard_s], any holds:
  * a `tags` row (`t` = end of its 5 s window) overlaps with `speech_active` true, or `speech_p` >= `--clip-speech-p`, or a top label of the human set at its threshold (the capture predicate);
  * a birds row with `t` inside the span has a `common` / `scientific` name starting `human`.
* Confirmed: the whole clip (audio, PNG, JSON) is deleted and `clip_evicted` with reason `human_voice` is appended; `meta.clips_purged_human` counts them. Idempotent; no hit = untouched.

## Layout

`<--out>/clips` (`--clips-dir` overrides); names relative to it.

* `YYYYMMDD/<id>.flac`: only with `--clip-audio`; mono, 48000 Hz, PCM_16 (WAV only if the FLAC encoder is missing). `YYYYMMDD` = UTC day of the clip start.
* `YYYYMMDD/<id>.png`: spectrogram, 96 rows from 3500 Hz, one column per 50 ms, 8-bit palette.
* `YYYYMMDD/<id>.json`: the `clip` record (schema `$defs/clip`).
* `index.jsonl`: append-only, `hist.RotatingJsonl` 16 MB x 2. Kinds `clip_stored`, `clip_audio_expired` (audio mode only), `clip_evicted`, `clip_sidecar_expired`.
* `id` = `<capture epoch ms, 13 digits>-<species slug, a-z0-9, <= 24>-b<beam 1..8>`.
* No record contains an absolute path, user name, drive letter or address. `audio_file`, `png`, `json` are `<day>/<id>.<ext>`; `audio_file` is null in the default mode.

## Visual-only: what is stored

Computed after a zero-phase linear FIR high-pass (`clip_features.restrict`: 4097 taps, Kaiser beta 12, cutoff 3550 Hz; >= 100 dB down below 3504 Hz). The first and last 2048 samples are not analysed; every series starts 0.0427 s after clip start.

* Spectrogram PNG: 96 rows log-spaced 3500 Hz (bottom) to 24000 Hz (top), each the MAX over the 20 Hz FFT bins of its band.
* `spectrogram.scale` `pcen` (1.2.0, flag `--clip-pcen`, default off = `log`): same rows, columns, floor and 8 bits; pixel = round(clip(PCEN / vmax * 255, 0, 255)), PCEN (analysis/pcen.py) of row power amp**2 * power_scale (power_scale 32768**2), smoothed over columns, init `first`.
* `spectrogram.pcen` {time_constant_s 0.4, s, alpha 0.98, delta 2, r 0.5, eps 1e-6, power_scale, init, vmax 4}; `db_lo` / `db_hi` absent. `log` records have `db_lo` -110, `db_hi` -30, no `pcen`.
* One column per 50 ms from NON-overlapping 2400-sample Hann frames; columns = (samples - 4096) // 2400. Palette mode P.
* Pixel = (level - (-110)) / 80 * 255 clipped; level = dB re full scale of a sinusoid of that amplitude in the bin. Magnitude only, no phase.
* `envelope` = {n, step_s 0.05, rms_db}: RMS of the high-passed signal per 50 ms frame, dB, 1 decimal.
* `dominant_hz`: per 100 ms non-overlapping frame, peak bin >= 3.5 kHz (`dominant_step_s` 0.1). `centroid_hz`, `bandwidth_hz`: mean / std over those frames.
* `peak_dbfs`, `rms_dbfs`, `snr_db`, `band_db` (3 bands 4-8, 8-16, 16-24 kHz; `band_edges_hz` [4000, 8000, 16000, 24000]): clip-level scalars of the high-passed signal.
* `onsets_s`: clip times on a 25 ms grid from the 50 ms envelope, >= 100 ms apart.
* Test bound (analysis/test_clips.py section 13): Griffin-Lim reconstruction max |NCC| < 0.05.

## `audio_stored` (required in `clip_stored` and the sidecar)

* `false` (default): `audio_file` null, `codec` null, `audio_bytes` 0, `audio_expired` false. No `clip_audio_expired` record is written; `meta.audio_expired` 0; status `audio_written` false, `clips.audio` false, `clips.audio_files` 0.
* `true` (`--clip-audio`): `audio_file` names the file until it expires.

## Reading

Fold `index.jsonl` by `id` in file order; later lines override.

* `clip_stored` creates.
* `clip_audio_expired` sets audio gone (audio mode only).
* `clip_evicted` removes the clip (all files); `reason` is `species_hour_cap` or `human_voice` (1.1.0). A reader MUST tolerate unknown `reason` values.
* `clip_sidecar_expired` leaves only the index history.
* Audio mode rewrites the JSON sidecar atomically once at audio expiry: `audio_expired: true`, `audio_file: null`, `audio_expired_t`, `audio_expired_reason`.
* A reader MUST NOT assume an audio file exists; open files defensively.

## What is stored (ALL hold, strictly)

1. Wildlife.
   * `group` (taxa.json string on the bird / bird_filtered row) is `bird`, `insect`, `amphibian`, `mammal` or `other-bio`. `group` null or missing (`meta.taxa_missing`) is NOT stored.
   * `non_species` must be exactly false.
   * Names in `analysis/clip_exclude.json` are never stored: domestic and farmyard animals, pet parrot / cockatoo / macaw / lovebird / myna genera, mechanical and human classes.
2. Certainty.
   * Event = reports of the same species within 3 s (+0.5 s jitter), over both beams and both kinds (bird, bird_filtered).
   * `confidence_max` = MAX confidence of the event, STRICTLY above `--clip-min-conf` (default 0.85): at the default 0.85 is not stored, 0.8501 is; at 0.3, 0.30 is not stored, 0.31 is. A value outside 0.05 to 1 is refused at start. `conf` carries no threshold: readers must not assume 0.85.
   * Beam agreement (`--clip-min-beams`, default 2; 1 disables): >= that many DISTINCT beams (1-8) reported between first and last report. Only reports of a staged event count (the first report must reach min(0.5, `--clip-min-conf`)).
   * 1-beam 0.90: not stored (`meta.clips_skipped_single_beam`). 2-beam 0.90: stored. 2-beam 0.85: not stored (default `--clip-min-conf`).
   * Recorded as `beams_agreeing` in the sidecar and `clip_stored`.
3. Out of region. A bird_filtered detection is stored under the same rule with `in_region: false`, counted in `meta.clips_out_of_region_stored`. `--clip-skip-out-of-region` stores none.
4. Privacy guard, fail closed. Clip span [start, end]. Nothing is stored unless, over [start - 5 s, end + 5 s] (`--clip-guard-s`):
   * the tags (PANNs) timeline covers the whole span without a hole (windows [t-5, t]);
   * no tags row has `speech_active` true or `speech_p` >= `--clip-speech-p` (0.10);
   * no tags row's `top` list holds a human-sound label at p >= 0.05 (list = `human_top_labels` in `clip_exclude.json`);
   * no emitted BirdNET row is named `Human*` (both kinds).
5. Guard failure: tagger off or failing, a hole, or no verdict by span end + 10 s + `--clip-hold-s` (60 s) stores nothing (`meta.clips_skipped_tagger_unavailable / _guard_hole / _no_verdict`). Until the verdict the audio is held in RAM only. CLAP is not a speech flag.
6. Source.
   * Beam with the highest-confidence report (`beam`); mono 16-bit at 48 kHz in RAM.
   * `beams_reported` lists every reporting beam (`beams_agreeing` = its length).
   * Length: event span + `--clip-pre-s` (1 s) + `--clip-post-s` (1 s), <= `--clip-max-s` (12 s, the RAM ring depth). Longer = the 12 s part centred on the peak window (`capped: true`).
   * Shorter roll when the ring lacks it (`pre_roll_s`, `post_roll_s`, `ring_truncated: true`).
   * A non-contiguous run is cut to the contiguous part holding the peak window; < 1 s stores nothing.

## Bounds and retention

* Audio retention (`--clip-audio` only): audio deleted when `now - capture time >= --audio-retention-h` (24 h; 23h59m00s kept, 24h00m00s deleted). Age = capture time in the id, never mtime.
* Sweep at analyzer start and every `--clip-sweep-s` (300 s, clamped 1..600): also deletes orphaned audio / PNG / JSON (no sidecar record, > 10 min old) and `*.tmp` / `*.part` > 10 min old.
* A file the OS refuses to delete is retried (`meta.audio_expire_retry`). A backward clock jump gives negative age: file kept.
* Sidecars (PNG, JSON, index lines) stay `--sidecar-retention-days` (14; 1 = expire with the audio). After expiry `clip_sidecar_expired` is appended and the empty day directory removed.
* Sidecar cap `--clip-sidecar-cap-mb` (256), oldest first.
* Per species per UTC hour: <= `--clip-cap-per-species-hour` (6). A new clip replaces the lowest stored one only with strictly higher confidence (ties keep the older); the loser's files are deleted and `clip_evicted` appended.
* Audio cap (`--clip-audio` only) `--clip-audio-cap-mb` (512): lowest confidence then oldest loses its audio (`clip_audio_expired`, reason `size_cap`).
* The analyzer is never slowed: finished clips go through a queue of 8 to a writer thread (full = clip dropped, `meta.clips_dropped_queue`).
* Files are written tmp + `os.replace`; disk-full and OS errors are counted (`meta.clip_write_errors`, `clip_sidecar_errors`, `clip_index_errors`), never raised. Pending RAM <= 8 clips of <= 12 s.

## status.json (analysis-status 2.0.0)

* `audio_written`: true once any audio file was written in this run.
* `clips` = {enabled, audio (the `--clip-audio` flag), stored, pending, queue_depth, audio_files, audio_bytes, last_stored_t}.
* `meta` counters: `clips_skipped_{not_wildlife, low_conf, single_beam, no_group, excluded, out_of_region, speech, human_label, tagger_unavailable, guard_hole, no_verdict, ring_gap}`.
* `meta` counters: `clips_dropped_{queue, pending_full, cap}`, `clips_evicted_{cap, size, sidecar_size}`, `clips_purged_human`, `clips_out_of_region_stored`, `clips_stored`.
* `meta` counters: `clip_write_errors`, `clip_sidecar_errors`, `clip_index_errors`, `clip_service_errors`, `audio_expired`, `audio_expire_retry`, `orphans_deleted`, `sweep_runs`, `sweep_errors`.

## Signatures

`signatures` and `envelope` in the JSON, from the high-passed signal above 3.5 kHz.

* `duration_s` (whole clip); `peak_dbfs`, `rms_dbfs` (re int16 full scale; full-scale sine reads -3.01); `snr_db` (RMS over RMS of the quietest 10 % of 50 ms frames, floor -100 dBFS).
* `dominant_hz` (per 100 ms Hann frame, parabolic interpolation); `centroid_hz`, `bandwidth_hz` ({mean, std}).
* `band_db` over `band_edges_hz`: mean-square dB re full scale (sine of amplitude A reads 20 log10(A / sqrt 2); silence -200).
* `onsets_s` (jumps >= 6 dB of the 50 ms RMS envelope at a 25 ms hop, >= 100 ms apart) and `ioi` (n, mean_s, median_s, cv, rate_hz).
* `beam_energies_db[8]` (mean-square per beam, dB, peak window); `beam_azimuth_deg[8]` from `profile.beam_azimuth_deg` or null.
* `envelope` = {n, step_s 0.05, rms_db[n]}.
* Metadata: `species`, `group`, `confidence_max`, `reports`, `beam`, `in_region`, `model` BirdNET + `model_version`, `t_start` / `t_end` (Unix s), `t_start_utc` / `t_end_utc` (UTC only), `provenance` always `model_opinion`.
* Metadata: `speech_guard` {tags_windows, max_speech_p, guard_s, speech_p_threshold, human_labels_seen false}, `start_sample` (absolute stream index of the first sample).

## Compatibility

* Receivers ignore unknown fields. A new optional field = MINOR (schema, fixtures, CHANGELOG in the same commit).
* A threshold or retention change goes in the CHANGELOG. analysis-status 2.0.0 (`audio_written` boolean) is the breaking change.
* 1.1.0: `clip_evicted.reason` gains `human_voice` (MINOR).
* 1.2.0: `spectrogram.scale` gains `pcen` with a `pcen` parameter object; `db_lo` / `db_hi` only for `log` (MINOR).
