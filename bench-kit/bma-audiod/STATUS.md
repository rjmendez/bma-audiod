# BMA 360DX live-audio pipeline: status (2026-10-27)

Goal: continuous audio off the BMA 360DX for wildlife classification, replacing the vendor stack. Per-feature state: `docs/FEATURES.md` (registry), `python tools/feature_state.py --root <data root>` (live).

## Current state
| Area | State |
|---|---|
| Capture route | Dante live: `pc/dante_bridge.py` receives the 8 processed (gated) beams and fans out BMAA datagrams to the analyzer, the legacy classifier, the spectrum process and the cable sink |
| Analyzer plugins ON by default | dsp, spatial, tags, clap, birds, coherence, novelty, indices, bursts; CPU budget 30 ms per s audio for all plugins together |
| Perch shadow | opt-in via `analyzer_args.txt`; GPU through `--perch-device` and `--perch-ort-dir` (owner 2026-10-26) |
| Mixscore | opt-in; 52 ms CPU per s audio at N=1, over the budget alone; default N=4 |
| Clip audio | opt-in via `<root>\enable_clip_audio.txt`; 24 h retention, speech guard, human-voice purge (owner 2026-10-26/27) |
| Dashboard | HTTPS, login, optional tailnet identity and passkeys (`ui/`) |

## Open items
| Item | Detail |
|---|---|
| Raw ungated audio | not available over Dante; SPI bulk stream needs a DSP-side streaming responder (~1 MB/s, ~10 channels); FPGA/EMIFA tap is JTAG-gated |
| Registry rows pending owner | the `Approved = pending` rows in `docs/FEATURES.md` |
| Uncapped stores | PANNs embeddings 50 to 100 MB per day; `detections.jsonl` and `classifier.log` of the legacy classifier |
| Legacy classifier CPU | unmeasured; outside the analyzer budget |

## Egress findings (historical, 2026-10-09)
- Raw 24-bit mic frames sit at the fixed receive buffers, frame base `0x256dcc`: 21-word frames, slots 0-6 data, sync `0x00ff00`/`0x123456` at words 19/20. `DM(0x256dc4)` is a descriptor pointer, not the base.
- SPI egress: ring ~0.045 s of DSP free RAM; per-word peeks ~4.5 KB/s; read-clock ceiling ~8-12 MHz; bulk ~1 MB/s needs the DSP-side responder.
- Loci investigation `bma360d-firmware-teardown` holds the evidence.

Telnet is control-only (owner rule): audio egress is a binary socket, never telnet.
