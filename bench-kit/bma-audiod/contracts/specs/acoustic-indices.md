contract: acoustic-indices
contract_version: 1.0.0
status: stable
producer: analysis/host.py plugin `indices` (analysis/indices.py); flags `--indices-interval-s` (300), `--indices-hop-s` (30), `--indices-without-speech-gate`; `--no indices` disables it
consumers: analysis/indices_report.py
schema: schemas/acoustic_indices.schema.json; example: fixtures/acoustic_indices.jsonl (regenerate with tools/make_indices_fixtures.py)

# acoustic_indices

One JSONL record per closed interval (default 300 s, epoch-aligned) appended to `<--out>/indices.jsonl` via `hist.RotatingJsonl` (64 MB x 2). Metadata only: scalar statistics, no audio stored.

## Signal and spectrogram

* Mean of the 8 beam outputs (float64); one 5 s window every `--indices-hop-s` (30 s).
* Hann (periodic) frames of 480 samples (10 ms), no overlap: bin width exactly 100 Hz; band [lo, hi) = bins lo/100 ... hi/100 - 1. 5 s = 500 frames.

## Indices of one window

| name | definition |
|---|---|
| `aci` | per bin: sum over t of abs(I(t+1) - I(t)) / sum over t of I(t); summed over 1-11 kHz bins; I = amplitude spectrogram; a bin with mean amplitude < 1e-9 adds 0 (Pieretti 2011) |
| `hf` | Shannon entropy of the time-mean AMPLITUDE spectrum over 1-11 kHz (100 bins) / ln 100 (Sueur 2008) |
| `ht` | Shannon entropy of the Hilbert amplitude envelope of the 1-11 kHz-limited signal / ln n (Sueur 2008) |
| `h` | `ht` * `hf` |
| `bi` | area under the mean amplitude spectrum in dB (20 log10, floor -180 dBFS) above its minimum in 2-8 kHz: sum of (L - Lmin) * 0.1 kHz, dB * kHz (Boelman 2007) |
| `ndsi` | (B - A) / (B + A), A = Welch power 1-2 kHz, B = 2-8 kHz (Kasten 2012) |
| `level_dbfs` | 10 log10 of the mean square of the mean signal |
| `chorus_dbfs` | mean-square power 4-12 kHz in dBFS (full-scale sine = -3.01) |

* Interval record: p10 / p50 / p90 of each index over the interval's used windows (linear interpolation).
* scikit-maad 1.5.2 reference (analysis/compare_maad.py): `aci`, `bi`, `ndsi` equal to 1e-4 relative on the same spectrogram; tests assert 1e-4.
* `bi` = maad `bioacoustics_index` * df^2 / 1000 (df in Hz).
* White-noise closed forms used by tests: ACI per bin = (2 - sqrt 2) (n - 1) / n (n = 500: 58.47 for 100 bins); 1 - Ht = 0.1390 / ln M (M = 50000: 0.98715); 1 - Hf = (4/pi - 1) / (2 K ln N) (K = 500, N = 100: 5.9e-5).

## Which windows count

Per window, in order:

1. Non-finite sample: `win_nonfinite`.
2. Level < `params.dead_dbfs` (-100 dBFS): `win_silent`.
3. Overlapping (strictly) a tagger (PANNs, window 5 s, hop 2 s, best beam) window flagged `speech_active`: `win_speech`.
4. Not covered end to end by the union of tagger windows: `win_untagged` (fail closed).
5. Else used.

* `win_total` = used + speech + silent + nonfinite + untagged. With `speech_gate` false nothing is excluded for the tagger and `win_untagged` is 0.
* An interval is closed 30 s after its end (or by shutdown).
* Values of a speech-flagged window are never stored; an interval whose windows are all excluded writes NO record.
* Human-voice labels are never read or reported.
* `speech_gate: false` = plugin ran without the tagger (`--indices-without-speech-gate`); by default the host refuses to start `indices` without the `tags` plugin.

## Fields

* `k` = "acoustic_indices", `v` = 1, `t` (interval end, Unix s), `t0` (start, multiple of `span_s`), `span_s`, optional `partial: true` (flushed at shutdown, or analyzer started late in the interval).
* `win_s`, `hop_s`, the six window counts, `speech_gate`, `params` (sample rate, mix, frame, df, every band in Hz), `indices` (eight `{p10, p50, p90}`).
* Ranges: hf, ht, h 0..1; ndsi -1..1; aci, bi >= 0; level and chorus -200..0 dBFS.

## Usage rules

* Within-site trends only; never a richness or species-count proxy.
* Compare each index with `chorus_dbfs`; |r| >= 0.8 = "chorus meter" (`analysis/indices_report.py` prints it).
* ACI depends on clip length (5 s) and frame size; do not compare across settings.
* Electronic lines at 11.51 and 12.0 kHz are outside the 1-11 kHz bands but inside `chorus_dbfs`.
* Levels are dBFS only, no SPL. Acoustic evenness and diversity indices are not computed.

## Compatibility

* Additive within `v` 1 (readers ignore new optional fields).
* Changing the frame, window length, a band or an index's meaning changes `v` (1.0.0 schema pins `fs`, `mix`, `frame`, `df_hz`).
* Cost budget: indices adds 1 ms per second of audio at hop 30 s (analysis/bench_plugins.py).
