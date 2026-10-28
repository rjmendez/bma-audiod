# Architecture

One shell, four views, one renderer, one DSP module, one mock server. No framework, no build step, no vendored file. Everything is served same-origin under the current CSP (`script-src 'self'; style-src 'self'`; no inline style attributes).

## Tree

```
bench-kit/bma-audiod/ui-next/
  mock_server.py          stdlib + numpy backend: static, /api/v2/*, WebSockets. Loopback only unless --allow-lan
  synth.py                deterministic synthetic scenes (tones, sweeps, noise); no recordings
  static/
    index.html            shell: header, nav, v-live, v-review, v-det, v-sys, consent dialog
    app.css               tokens and layout (type 12/15/19/24, tinted neutrals, --hit 36/44)
    app.js                shell: live renderer, WebSocket, chips, selection, listening, Freeze, detections, system, navigation, theme
    review.js             Review viewer: mountViewer(root, hooks); transforms, box select, audition, A/B, blind verdict flow
    selftest.html, .js    cross-engine probe (renderer parity, context restore, audio, stream)
    engine/               classic scripts (browser, worker, node): dsp.js, render.js, colormaps.js, audition.js, worklets.js
  test/                   test_mock_server.py, test_browser.py, test_ui_quality.py, test_dsp.js (+refs.json, gen_refs.py, mutation_dsp.py), test_verdict_contract.py
  tools/                  cdp.py (DevTools driver), measure_proto.py, first_paint.py, bench_*.py, cross_browser.py, shots.py, audit_*.py, run_selftests.ps1
  bench/                  rejected renderers and feature probes, kept as evidence for STREAMING.md
docs/ui-next/             this folder
```

## Views

| View | Contents | Data |
|---|---|---|
| Live | 9-ring waterfall (mean + 8 beams) on one canvas, beam chips with level meter, inspector, Listen / Freeze, event ticker | `/api/v2/live` WebSocket (`BMW1` frames, events as text frames) |
| Review | clip queue (tabs, list), lab viewer: transform, scale, box select, band-pass and loop audition, beam solo/mix, A/B, verdict bar, record preview | `/api/v2/clips`, `beams.wav`, `/api/v2/review/*` |
| Detections | events table, species chips with counts, filters; Open goes to Review at that clip | `/api/v2/clips`, event frames |
| System | strip values, health, plugin and board state | `/api/v2/status` |

Phone: the same four views as a bottom bar over one column. 30 panels of the current dashboard become 12.

## Modules

- `LabRender.Waterfall`: rings (one per beam) and layers (rects) on one canvas. WebGL2 R32F circular textures; Canvas2D fallback with one ring canvas per layer. A CPU copy of every ring restores the picture after context loss (`_glSetup`, `_texels`); `replayInto` moves the history to another instance. Shared view: range, colormap, scale, time window. Row maps for lin / log / mel / erb / bark.
- `LabDSP`: STFT, multitaper, reassigned, constant-Q, mel / ERB / Bark, PCEN, background normalisation, HPSS, flux, cepstrum, coherence; streaming forms. 63 checks against numpy / scipy / librosa (`test_dsp.js`), 32 mutations all red (`mutation_dsp.py`).
- `LabAudition`: see AUDITION.md.
- `review.js` and `app.js` are ES modules and talk to the engine through globals only.

## Redundancy removed (same fact, more than one place)

| Fact | Was shown in | Kept |
|---|---|---|
| Latest detections | strip, Overview table, Detections table | Live ticker (5 rows) + Detections table |
| Species ranking | Overview, Detections, Review | Detections chips with counts |
| Live beam levels | Overview compass, Beams, System compare strip | Live beam chips with meter and peak |
| Stream loss, board temperature | strip and System, twice each | System panel; the strip shows the current value |
| Events per hour, time x direction | 3 and 2 panels | one density strip, one scatter in Detections |

## One flow

```
Live: pick beam (chip or 0-8) -> drag a time range -> inspector (duration, peak dB, band)
      clip overlaps -> Play clip opens Review    none -> "no audio is kept for this time" (policy)
      Listen -> consent dialog -> PCM -> LISTENING pill with countdown;  Freeze = last 6 s of what was heard, in tab memory, opened in the viewer
Review: Space play, R replay, 0.5/1/2, loop, Y correct / N wrong / U unsure / S skip -> verdict record (DATA-MODEL.md)
```

Past time plays only as a stored clip: no raw audio history, by policy (clips only, speech guard, 24 h).

## Policy constraints on the design

| Policy | Consequence |
|---|---|
| Audio only as clips, speech guard, human-voice purge, 24 h | Waterfall carries levels only. No "play back this minute" |
| Spectrum and listening behind authentication | production routes sit behind `ui/auth.py`; the mock refuses a non-loopback bind without `--allow-lan` |
| Human-voice labels hidden | frames and events carry species names only |
| Nothing leaves the machine | no CDN, fonts or analytics |
| Live listening is an interception question | off by default, per-session consent, see OPEN-CHOICES.md |
