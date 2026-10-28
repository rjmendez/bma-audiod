contract: beam-bands
contract_version: 1.0.0
status: stable
producer: pc/bma_cable_sink.py (`BandMeter`, `feed_bands`, `JsonlLog`)
consumers: offline analysis and dashboards (log); cable-stream data track kind 4
schema: schemas/beam_bands.schema.json; example: fixtures/beam_bands.jsonl

# beam_bands - per-beam octave-band levels, one record per second

One JSON object per second of CONTIGUOUS aligned audio, metered by the sink on the 8 beams it plays (keeps running when the 16-channel endpoint is not ready). No audio.

```
{"k":"beam_bands","v":1,"idx":284267690,"t_us":1792886495588944,"n":48000,"zero_frac":0.0,
 "edges_hz":[[50,125],[125,250],[250,500],[500,1000],[1000,2000],[2000,4000],[4000,8000],[8000,16000]],
 "db":[[...8 values...], ... 8 beams ...]}
```

## Carriers

* Log: one line per record in `beam_bands.jsonl` (default `<data-dir>/reports/beam_bands.jsonl`, rotated to `.1` at 256 MB). `--bands-log ''` disables it.
* Cable data track: the identical compact JSON bytes (separators `,` and `:`) are the payload of BMD1 frame kind 4 (cable-stream.md).

## Fields

| field | meaning |
|---|---|
| `k`, `v` | `beam_bands`, 1. A reader skips records whose `v` it does not know. |
| `idx` | card sample index (u32, the BMAA `block` space) of the window's first sample |
| `t_us` | PC UTC microseconds when the record was PRODUCED, after the window's last sample arrived; place on the audio axis with the cable anchors |
| `n` | window length, 48000 samples |
| `zero_frac` | share of the window's rows where all 8 beams are exactly zero |
| `edges_hz` | the 8 octave bands [lo, hi) Hz: 50-125, 125-250, 250-500, 500-1k, 1-2k, 2-4k, 4-8k, 8-16k |
| `db[beam][band]` | mean-square level in dB re full scale, one decimal, beam 0-7 = beams 1-8, range -200.0 to 0.0 |

## Level definition

* Hann-windowed FFT of the window, Parseval-scaled: the band value is the mean-square power in the band.
* A sine of amplitude A reads `20*log10(A/sqrt(2))` dB in its band (A = 0.1 -> -23.0 dB, full scale -> -3.0 dB).
* Silence reads exactly -200.0. The DC bin is in no band.
* A tone within 2 Hz of a band edge splits between the two bands (Hann main lobe): 1000 Hz at A = 0.1 reads -30.8 dB in 500-1000 and -23.8 dB in 1000-2000.

## Windowing

* Windows are 1 s long and non-overlapping.
* A hole in the card's index larger than the sink's gap fill (0.5 s) restarts the window, so a record always describes contiguous audio.
* Short holes inside the gap fill are zero-filled and show in `zero_frac`.

## Compatibility

* Additive within `v` 1: new optional fields allowed (readers ignore them).
* Changing the bands, the window length or the level definition changes `v`. The 1.0.0 schema pins `n` and `edges_hz`.
