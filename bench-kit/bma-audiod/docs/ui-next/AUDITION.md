# Audition

`LabAudition` (static/engine/audition.js) judges a signal by ear from a clip, a review item or the live stream through one chain.

```
8 beam buffers --(beam weights: solo or mix)--> box band-pass (HP+LP from the selected box) --> heterodyne | divide | none
   --> level match --> master gain --> soft-clip WaveShaper limiter --> destination
```

| Stage | Behaviour |
|---|---|
| Beam solo / mix | per-beam gain; A/B toggles two settings at equal loudness |
| Box band-pass | the box selected on the spectrogram sets low and high edges; loop plays the box's time span |
| Heterodyne / divide | shifts ultrasonic content into hearing range (mix with a local oscillator / integer frequency division) |
| Limiter | soft clip, so a wrong gain cannot hurt |
| Live | `pcmring` AudioWorklet (`engine/worklets.js`) fed by a WebSocket: `UILAB1` header frame, then interleaved PCM16, 8 beams, 48 kHz, 20 ms chunks (about 768 kB/s); 200 ms jitter buffer in the browser |

## Live listening policy (enforced by the mock server and the page)

| Rule | Where |
|---|---|
| Off by default; per-session consent dialog; `AudioContext.resume()` runs inside the click that closes it | page |
| `POST /api/v2/listen/consent` returns a 10-minute token; `GET /api/v2/listen` needs it (403 without, 409 while another listener is connected, slot freed when the client closes) | server (`test_mock_server.py`) |
| LISTENING pill with countdown and tab-title prefix | page |
| Stops after 30 s in a hidden tab | page |
| Nothing is recorded server-side; Freeze keeps the last 6 s in tab memory only; there is no download or recorder button | both |
| Human voices are not exposed; production adds mute-on-speech (OPEN-CHOICES.md 3) | production phase |

## Verification

`test_dsp.js` (filter responses, heterodyne, limiter numerics), `test_browser.py` (live ring fills, consent flow, Freeze opens the viewer, one listener), `selftest.html` (AudioWorklet, unlock, ring in Edge, Chrome, Firefox).
