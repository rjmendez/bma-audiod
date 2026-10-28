# BMA360 streaming acoustic classifier

`bma_classify.py` (replaces the heuristic stub `scripts/compact_event_classifier.py`): bounded sliding window of mono audio, activity gate, **BirdNET** (48 kHz native) on each 3 s window, labelled detections with provenance as JSON lines on stdout. A detection is a model opinion, never ground truth.

## Install (WSL `audioeval` venv)
```
python -m pip install -r requirements.txt
```
ffmpeg optional (WAV/raw use soundfile/numpy).

## Run
```
python bma_classify.py --wav clip.wav --min-conf 0.25                    # offline: WAV file
python bma_classify.py --udp 0.0.0.0:48001 --subscribe BOARD:48000 --slot 0 --sr 48000   # live: registers, renews lease, segments on holes
python bma_classify.py --udp 0.0.0.0:48000 --sr 48000                    # BMAA datagrams from any source
some-source | python bma_classify.py --stdin-raw --sr 48000 --dtype int24 --channels 1
```
- stdout: JSON lines only (`detection` / `no_detection` / `summary`); logs and model chatter to stderr.
- Flags: `--gate-dbfs` (skip quieter windows, logged), `--ring-s`, `--lat/--lon/--week` (BirdNET location filter), `--idle-exit-s N` (end a UDP stream and print the summary after N s without a datagram), `--wav-chunk N` (feed a WAV in N-sample chunks so window offsets match a stream's).
- Window rule: nothing classified until 3 s of contiguous audio exists; the first window fires once, on the chunk that completes it; one more per 1 s of new samples. Model loaded before the stream opens;
  - a reader thread drains the socket and renews the daemon lease while the model is busy; the hand-off queue is bounded, overflow counted (`queue_overflow_datagrams`) and shows as a hole.

## Holes
The board's NIC drops packets above ~8 Mb/s (8-channel flow loses ~34%).
- `block` in each datagram is the absolute sample index; `block != previous block + previous n` is a discontinuity: the run ends and the ring is cleared. A window is never built from audio on both sides of a hole.
- `--slot N` selects one channel position (default: first slot seen); other slots counted in the summary, never mixed.
- `--fill-gap-ms M` (opt-in, default 0): holes up to M ms zero-filled; filled samples counted, coverage drops.
- Summary: `stream: {slot, samples_received, gaps, lost_samples, lost_s, reordered, filled_samples, segments, longest_contiguous_s, coverage, ignored_other_slot_datagrams, bad_datagrams}` plus `daemon_status` when subscribed. No hole-free 3 s ever: `verdict` = `no_contiguous_window` with a `reason`.

## Datagram format
Little-endian: `magic 'BMAA'(4) | ver u8 | dsp u8 | slot u8 | flags u8 | seq u32 | block u32 | n_samples u32 | n_samples x s24-in-s32`. flags bit0 = all-zero block, bit1 = hole before this block. See `_parse_datagram`, `../daemon/src/proto.rs`.

## Tests
`python test_segment.py` (27 checks, no model): real UDP over loopback with a recording stand-in for BirdNET: contiguous stream -> windows, all gap-free; 12.5% loss -> no window reaches the model, verdict says why;
- one 2 s hole -> windows on both sides, none across; slot demux; fill accounting. End-to-end with the real daemon and model: `../daemon/tests/hermetic_test.py`, `../daemon/tests/e2e_audio_test.py`.

## Status
Real audio (2026-10-09): WAV 2.8 s clip -> `Dog` 0.447; 90 s clip @0.1 -> 137 detections. The live source is the daemon's UDP stream;
- its channels are currently unfed (exact zero) and the NIC loses packets at 8 channels, so on the real board the expected verdict is `no_audio_classified` or `no_contiguous_window`. See `../findings/`, Loci `bma360d-firmware-teardown`.

## Location / date species filter (optional, county level)
`--geo-config <file>` on `bma_classify.py` and on `analysis/host.py` (birds plugin); absent = no filter. Start from `geo_county.example.json`; keep your copy untracked as `geo_county.local.json`. Uses birdnetlib `lat`, `lon`, `date`/`week_48` (date = the window's date).
- Privacy: every position goes through `county_level()` (centre of a 0.25 degree cell, ~county); config holds only the rounded value; nothing finer stored, logged or written.
- With it on, each detection gains `non_species` (Engine, Dog, Siren, ... scientific == common name) and `in_region` (true / false / null = location model cannot judge).
  - `in_region: false` is removed from the stream and logged as `filtered_out_of_region` (classifier) / `k: bird_filtered` (plugin). Non-species classes kept and flagged.
- Not judged, kept: the location model knows birds only (insects, frogs, mammals score ~0.001 everywhere). `birdnet_coverage.json` (built by `make_birdnet_coverage.py` in the audioeval venv, ~40 s, rebuild when birdnetlib changes) lists the 379 labels it cannot judge;
  - without it the filter never drops anything and only flags.
- Tune: optional `"threshold"` (library default 0.03; higher = stricter) and `"week"`.
- Tests: `python test_geo.py` in the audioeval venv (recording stand-in for birdnetlib + the real species list). Installer pins `birdnet_coverage.json` and `geo_county.local.json` (as `geo_county.json`).
