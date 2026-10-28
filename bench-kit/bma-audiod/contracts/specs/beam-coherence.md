contract: beam-coherence
contract_version: 1.0.0
status: stable
producer: analysis/host.py plugin `coherence` (analysis/coherence.py); flag `--coherence-interval-s` (default 60); `--no coherence` disables it
consumers: analysis/coherence_report.py (verdict table), offline analysis
schema: schemas/beam_coherence.schema.json; example: fixtures/beam_coherence.jsonl (regenerate with tools/make_coherence_fixtures.py)

# beam_coherence - phase coherence of the 8 beam outputs

One JSONL record per closed interval (default 60 s) appended to `<--out>/coherence.jsonl` via `hist.RotatingJsonl` (64 MB x 2). Write errors and rotate fallbacks are counted in status.json `meta`. Metadata only: no audio stored.

## What is measured

Per 1 s window (plugin runs every 2 s, `hop_s` 2) of the 8 ring-aligned beams:

* Welch cross-spectra of the 8 ring-adjacent pairs p = (beam p, beam (p+1) mod 8), beams from 0: Hann segments `nfft` 4096 (11.72 Hz), hop `seg_hop` 2048, `nseg_per_win` 22. Bins below 50 Hz and above 16 kHz unused.
* Per octave band (`edges_hz`, the beam-bands edges) and pair: MSC (mean of per-bin MSC over unmasked bins), cross-phase at the band's centre bin `centre_hz`, lag in samples (phase slope across the band's bins), level difference dB (beam p minus beam p+1).
* `xc_lag`: per pair, lag of the largest-magnitude cross-correlation peak of the 125-4000 Hz averaged cross-spectrum (search +-256 samples, parabolic sub-sample interpolation), median over used windows. Polarity not reported: an anti-phase pair shows `ph_deg` near +-180.
* `full`: every 5th used window, MSC of all 28 beam pairs in three bands (125-1000, 1000-4000, 4000-12000 Hz), median per condition.
* Convention: a delay of d samples of beam p+1 relative to beam p gives cross-phase +360 f d / 48000 degrees and lag +d.

## Window classes

Precedence dead > gated > classified. Time-domain test on four 250 ms sub-blocks:

* `win_dead`: some sub-block's loudest beam < `dead_dbfs` (-100).
* `win_gated`: in some sub-block a beam is > `gate_db` (40) below the loudest. Gated and dead windows are excluded from every coherence statistic.
* `win_nonfinite`: NaN/Inf samples (counted, never analysed). `win_total` = used + gated + dead + nonfinite.
* Otherwise (`win_used`) each band is classified against a running noise floor = 10th percentile of the last 900 used-window levels of that band (recomputed every 10 windows).
* `quiet`: level <= floor + `quiet_margin_db` (3). `active`: >= floor + `active_margin_db` (6). `mid`: in between (counted, not in statistics). `warmup`: first 60 used windows after plugin start.
* Band level = mean over beams of the band power in dB. Per band: n_quiet + n_active + mid + warmup = win_used.
* A (band, condition) cell with < 5 windows in the interval is `null` (count still in `n_quiet` / `n_active`).

## MSC bias and correction

* Hann 50 % overlap: K_eff = K / (1 + 2 rho^2) = 22 / (1 + 2/36) = 20.84 (rho = 1/6). `msc_floor` = 1/K_eff = 0.048.
* E[MSC_hat] = MSC + (1 - MSC)^2 / K_eff. Records carry the RAW MSC.
* Correction: msc_c = 1 - (K_eff / 2) * (1 - sqrt(1 - 4 (1 - msc) / K_eff)), clipped to [0, 1]; `coherence_report.correct()` does this. K_eff = 1 / msc_floor.
* The linear form (K_eff * msc - 1) / (K_eff - 1) is NOT the inverse.
* Band MSC = mean of per-bin MSC. For a common source plus independent noise of power ratio r per channel, true MSC = r^2 / (1 + r)^2.

## Verdict rules (analysis/coherence_report.py)

Pools per-interval medians per cell (weighted by the cell's window count `n`), corrects raw MSC with the last record's `msc_floor`, judges each (band, condition) over the 8 pairs:

* `coherent`: corrected MSC >= 0.8 for ALL 8 pairs (`msc_min`) AND median circular phase spread <= 30 deg.
* `incoherent`: median corrected MSC over the pairs < 0.3.
* `partially coherent`: everything between.
* `insufficient data`: < `--min-windows` (10) windows in the cell.
* `dominated by gated windows`: > 50 % of ALL windows gated; every row says so.
* The beamspace line says `undetermined` when no row reached coherent / partial / incoherent.
* Cut-offs are printed in the report header and pinned at their boundaries in analysis/test_coherence.py. Inclusive on the coherent side (0.8 and 30 deg coherent; exactly 50 % gated not dominated; exactly 0.3 partial).

## Tonal lines

* With `--tonal-mask` (same file and `--tonal-mask-tol` as the dsp plugin) every bin within max(tol, 2 bins = 23.4 Hz) of a line gets weight 0 in every band statistic (`tonal_bins` per band).
* A band with masked bins reports `msc_unmasked_p50` next to the masked median. No mask file: nothing masked (`mask_hz` []).

## Fields

| field | meaning |
|---|---|
| `k`, `v` | `beam_coherence`, 1 |
| `t`, `t0`, `span_s` | interval end, start (multiple of `span_s`, epoch-aligned), length; no record for an interval without windows; a late window is folded into the open interval |
| `partial` | optional, only `true`: interval flushed at shutdown, or analyzer started > 1 hop into it. `span_s` stays nominal |
| `win_total`, `win_used`, `win_gated`, `win_dead`, `win_nonfinite` | window counts |
| `level_dbfs` | mean over used windows of the mean-square level over the beams (dB re full scale); null without a used window |
| `nfft`, `seg_hop`, `nseg_per_win`, `msc_floor` | estimator constants (4096, 2048, 22, 0.048) |
| `gate_db`, `dead_dbfs`, `quiet_margin_db`, `active_margin_db` | classification constants the interval ran with |
| `edges_hz`, `centre_hz` | the 8 octave bands and their centre-bin frequencies |
| `mask_hz`, `mask_tol_hz`, `tonal_bins` | tonal mask in force and masked bins per band |
| `bands[b]` | `n_quiet`, `n_active`, `mid`, `warmup`, and `quiet` / `active` cells (null or `n`, `msc_p10/p50/p90`, `ph_deg`, `ph_spread_deg`, `lag_p50`, `lag_spread` (p90-p10), `dl_db_p50`, optional `msc_unmasked_p50`), each an array of 8 values indexed by pair |
| `xc_lag` | broadband cross-correlation lag per pair or null (< 5 used windows) |
| `full` | `{edges_hz, pairs, cond:{quiet,active}}`; each condition null or `{n:[3], msc:[3][28]}` |

`ph_deg` = circular mean over windows; `ph_spread_deg` = circular std sqrt(-2 ln R) of per-window phases.

## Status

* status.json `meta.coherence_errors` counts windows refused (non-finite input, wrong shape) or failed; `plugins.coherence.errors` counts exceptions caught by the host.
* At shutdown the host calls `flush()` and the open interval is written `partial: true`.
* After a restart the noise floor and the 60-window warmup restart from zero.

## Compatibility

* Additive within `v` 1: new optional fields allowed (readers ignore them).
* Changing nfft, seg_hop, band edges or a field's meaning changes `v` (the 1.0.0 schema pins `nfft`, `seg_hop`, `edges_hz`).
* Classification constants are visible in the record; changing them is not a contract change.
