contract: burst-stats
contract_version: 1.0.0
status: stable
producer: analysis/host.py plugin `bursts` (analysis/bursts.py); flag `--bursts-interval-s` (default 60); `--no bursts` disables it
consumers: analysis/bursts_report.py, offline analysis
schema: schemas/burst_stats.schema.json; example: fixtures/burst_stats.jsonl (regenerate with tools/make_burst_fixtures.py)

# burst_stats - impulsive 6-16 kHz bursts

Metadata only: no audio, no coefficient that could rebuild audio.

* One JSONL record per closed interval (default 60 s, epoch-aligned) appended to `<--out>/bursts.jsonl` via `hist.RotatingJsonl` (64 MB x 2).
* A window belongs to the interval holding its midpoint.
* The interval open at shutdown, and one begun late, carry `"partial": true`; other records have no `partial` key.
* A record is written even when nothing happened (impulses.n 0, sub-band levels present).

## Measured (per used 1 s window, 8 beams, 48 kHz)

* Band: Butterworth band-pass 6-16 kHz (3 poles per edge, second-order sections, state carried across windows) on every beam; per-beam mean square in 1 ms frames; TOTAL = mean over beams of per-beam frame energy, dB re full scale.
* Impulse: a frame of the total that is a local maximum, >= 5 ms from the previous impulse, >= `thresh_db` (10) above the running median of the last `median_s` (3 s) of frames, and above `abs_floor_db` (-80 dBFS).
* The median ignores impulses while they occupy < half of the frames. The first and last frame of a window cannot be a peak; after a start or gap the filter start-up is masked (6 ms). Impulses closer than 5 ms count once.
* `impulses`: `n`, `rate_per_s` (n / analysed_s), p10/p50/p90 of impulse level (total, dB), of its excess over the median, and of beam spread (loudest minus quietest beam; per-beam level = max of the beam's frames within +-1 ms of the peak).
* `within3_hist[i]`: impulses with i+1 beams within 3 dB of the loudest. `loudest_beam_hist[i]`: loudest beam is i+1.
* `beams_hit_hist[i]`: impulses for which exactly i of the 8 beams are themselves an impulse within +-1 ms (beam level in [peak - 1 ms, peak + 1 ms] >= 10 dB over that beam's own median). `coincident_all_n` = `beams_hit_hist[8]`.
* `bands_db`: for each 1 kHz band 6-7 ... 15-16 kHz (10 values) p10/p50/p90 over used windows of the band power of the beam MEAN (11 non-overlapping Hann segments of 4096 points per window), dBFS. `ref_2_6k_db`: p50 of the 2-6 kHz band.
* `gaps`: inter-impulse gaps inside the interval (n = impulses - number of runs of contiguous used windows that hold impulses; never across excluded or lost windows).
  * `hist` 30 log-spaced bins, edge i = 0.005 * 6000^(i/30) s (5 ms .. 30 s), `under_n` / `over_n` outside that range.
  * `mode_s`: the gap whose +-15 % window (|g - c| <= 0.15 c) holds the most gaps, refined to the MEDIAN of the gaps in that window.
  * `regularity` = share of ALL gaps within +-15 % of `mode_s` (1.0 = metronome). `jitter_cv` = std / mean of the gaps in that window.
  * `mode_s`, `mode_n`, `regularity`, `jitter_cv` are null below 5 gaps.
* `mod`: envelope modulation. Total frame energy averaged to 200 Hz, Welch spectrum (Hann, 20 s segments, df 0.05 Hz) per run of >= 10 s of contiguous windows.
  * `peak_hz` = largest bin between 0.3 and 30 Hz (for a pulse train may be a HARMONIC); `fund_hz` = fundamental by weighted harmonic sum (+-0.05 Hz).
  * `prom_db` = peak over the median of that range; `rms_rel` = RMS / mean of the envelope; `s` = seconds used.
* Windows: `win_total` = `win_used` + `win_speech` + `win_unvetted` + `win_dead`. `analysed_s` = `win_used` x window length.

## Speech and privacy

* Windows overlapping a tagger window flagged speech are excluded and counted (`win_speech`).
* With the tagger running, a window the verdicts do not cover in full (0.25 s slack) is excluded (`win_unvetted`).
* A record is held back 15 s after its interval ends; windows younger than that at shutdown are dropped, never written unvetted.
* `tagger: false` = no tagger ran.
* Content is counts, levels and intervals above 6 kHz; audio only in RAM for one window; no coordinates.

## Failure behaviour

* `process()` never raises: a wrong-shape or non-finite window, or an exception, counts in `meta.bursts_errors` and `plugins.bursts.errors`; other plugins are unaffected.
* A start failure (module missing, scipy too old) shows as `plugins.bursts.enabled: false` with the reason.

## Versioning

Receivers MUST ignore unknown fields and skip a record whose `v` they do not know. Optional field = minor; changed meaning or removed field = major.
