contract: spectrum-packet
contract_version: 1.0.0
status: draft (interface fixed; thresholds marked "initial" are tuned on real data without a version bump)
producer: pc/bma_spectrum.py (loopback spectrum-row process; fed by the bridge's `--all-to 127.0.0.1:PORT` BMAA stream)
consumers: ui/bma_ui.py (`--spectrum-port`, loopback UDP receiver behind `GET /api/spectrum`)
reference: contracts/ref_binary.py (`build_spectrum`, `parse_spectrum`, `spectrum_band_edges`, `spectrum_coarse_groups`, `quantize_db`, `coarsen_block`); examples: contracts/fixtures/spectrum_examples.json

# BMAS - spectrum-row datagram (UDP, loopback only)

One datagram = ONE row of ONE beam (or the 8-beam power mean) at ONE tier. Magnitudes only: no phase, sample or PCM in any packet, file, log, fixture or test. Audio (BMAA) goes only to `pc/bma_spectrum.py`; only rows leave it.

## Hard rules

1. Loopback only. `bma_spectrum.py --to HOST:PORT` refuses a HOST other than `127.0.0.1` / `::1` / `localhost` (exit 2); the dashboard's `--spectrum-port` receiver binds `127.0.0.1` only and ignores non-loopback sources. No flag relaxes this.
2. Off unless asked. No `--to` / no `--spectrum-port` = nothing computed / nothing listened on.
3. Magnitudes, quantized. Power in bands (never complex bins), uint8 dB in 0.5 dB steps, Hann window. Standard tier: window = hop. Detail tier: window longer than hop (2400 vs 960 samples).
4. Over HTTP both tiers are served only to an AUTHENTICATED client from any network (ui-http); detail also needs `--spectrum-detail`. Rule: `spectrum_client_allowed(authenticated, tier, detail_flag)`. The packet is unaffected.
5. No audio is stored by either end (producer: a few seconds of samples in memory; dashboard: 60 s ring of magnitude rows).
6. The producer never gates; the dashboard replaces rows covered by the analyzer's speech tag with coarse values and flags them `gated`.

## Byte layout (little-endian)

| offset | size | field | rule |
|---|---|---|---|
| 0 | 4 | magic | ASCII `BMAS` |
| 4 | 1 | version | `1`; a receiver MUST reject another value |
| 5 | 1 | tier | `0` standard, `1` detail; else rejected |
| 6 | 1 | flags | bit 0 `gated` (coarse one-third-octave average), bit 1 `gap` (audio lost inside the window; all bins 0), bit 2 `zero` (every sample 0; all bins 0). Bits 3-7 MUST be 0; a receiver MUST reject a row that sets them |
| 7 | 1 | beam | `0` = power mean of the 8 beams (mean of the 8 linear band powers, then dB; a beam with a gap is left out, a muted beam counts as zero power; row is `gap` when every beam has a gap); `1..8` = BMAA slot + 1; above 8 rejected |
| 8 | 4 | row | u32 per tier and beam row counter of this process run, from 0, wraps modulo 2^32. All 9 beam streams of a tier use the SAME row number for the same window |
| 12 | 8 | t_us | u64 microseconds since the Unix epoch (UTC) of the END of the window, host clock when the window's last block arrived |
| 20 | 2 | hop_ms | u16, `100` tier 0, `20` tier 1 (receiver checks) |
| 22 | 2 | n_bins | u16, `46` tier 0, `100` tier 1 (receiver checks) |
| 24 | n_bins | bins | n_bins x uint8, band 0 first |

* Total length = 24 + n_bins (70 standard, 124 detail); any other length rejected. Never fragmented.
* Rejection codes: `short`, `magic`, `version`, `tier`, `flags`, `beam`, `hop`, `n_bins`, `length`, `gated_not_coarse`.
* A `gated` row whose values differ inside one coarse group is rejected `gated_not_coarse`.

## Values

* Band power = sum, over windowed one-sided power-spectrum bins with centre frequency in `[lo, hi)`, of the bin's contribution to the window's mean-square (Parseval; samples scaled `sample / 8388608.0`).
* If no bin centre lies in the band (detail bands near 1 kHz) the nearest bin's value is used. A full-scale sine is -3.0 dB.
* uint8: `v = clamp(round_half_even((db + 100) * 2), 0, 255)`; `db = v * 0.5 - 100`. 0 = at or below -100 dB; 255 = +27.5 dB or more.
* Beam 0 = mean of the 8 linear band powers, not of dB values.

## Tiers

| | standard (tier 0) | detail (tier 1) |
|---|---|---|
| served to | an authenticated client | an authenticated client, only with `--spectrum-detail` |
| window / hop | 4800 / 4800 samples = 100 / 100 ms | 2400 / 960 samples = 50 / 20 ms (60 % overlap) |
| rows per second | 10 (hard cap) | 50 (hard cap) |
| FFT bin spacing | 10 Hz | 20 Hz |
| bands (n_bins) | 46 | 100 |
| band layout | band 0: 50-250 Hz; bands 1-9: 1/3 octave 250-2000 Hz; bands 10-45: 1/12 octave 2000-16000 Hz (edges `250*2^(k/3)`, `2000*2^(k/12)`) | bands 0-3: 250 Hz wide 0-1000 Hz; bands 4-99: 1/24 octave 1000-16000 Hz (edge `1000*2^(k/24)`) |
| beams | 0..8 | 0..8 |
| coarse groups for gating | 19 (band 0, bands 1-9 each, bands 10-45 in fours) | 16 (bands 0-3 each, bands 4-99 in eights) |
| dashboard ring | 60 s (600 rows x 9 beams) | 20 s (1000 rows x 9 beams) |

## Row boundaries

* Window ends are cut on the unwrapped 64-bit BMAA block index, starting at the first complete block after process start; every beam of a tier uses the same boundaries.
* `row` counts windows since process start; a restart resets it to 0 (a consumer detects it by `row` != previous + 1 together with `t_us` not advancing by `hop_ms`, and starts a new run).
* 1 s gating blocks align to `row // 10` (standard) and `row // 50` (detail) inside one run.

## Speech

* The producer does no speech detection: rows are sent when their window completes, never gated.
* The dashboard sets `gated` and coarsens a row (`coarsen_block`) when the analyzer tagger reports `speech_active` (speech probability >= 0.30 over 5 s; arrives 2-52 s late), then bumps `rev`. Rows already drawn are not recalled.

## Loss and time

* UDP: no order, delivery or duplicate guarantee.
* The receiver treats a `row` that is not the next of its (tier, beam) stream as a gap in display time (no interpolation) and drops a duplicate or lower-numbered row of the same run.
* Rate cap: token bucket per (tier, beam), 10 rows/s (standard) / 50 rows/s (detail) sustained, burst 1 s of rows; excess is dropped and counted.
* A row number equal to one in the ring at a different time (> 1 s apart), or a lower row number with a later time than the newest row, is a producer restart and continues the dashboard's `idx` in a new run.

## Versioning

* New flag bits, trailing fields (larger `n_bins`) or a new tier value are minor only where an old receiver rejects them cleanly; a new tier or layout is a new `version` byte (major).
* Changing a band edge, the quantization or the coarse groups is major.

## Conformance

`ref_binary.parse_spectrum` is the reference; `ref_binary.build_spectrum` must reproduce every valid example byte for byte and the production producer must agree. Examples (valid, invalid, one `coarsen_block`) in `fixtures/spectrum_examples.json`.
