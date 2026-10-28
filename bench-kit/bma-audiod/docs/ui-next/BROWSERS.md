# Browsers

## 1. Measured: Edge, Chrome, Firefox (headless, Windows)

Tool: `ui-next/tools/cross_browser.py` runs `ui-next/bench/static/features.html` in each browser with a fresh profile; the page reports by HTTP POST. Each row exercises the code the prototype uses (the real renderers draw a known level and the pixel is read back).

| Browser | User agent |
|---|---|
| edge | `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0` |
| chrome | `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/155.0.0.0 Safari/537.36` |
| firefox | `Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:159.0) Gecko/20100101 Firefox/159.0` |

| Feature | edge | chrome | firefox |
|---|---|---|---|
| ecmascript: BigInt DataView (frame timestamps) | pass | pass | pass |
| ecmascript: dynamic import, module scripts | pass | pass | pass |
| webgl2: context | pass | pass | pass |
| webgl2: waterfall renderer draws the right pixel (R8 texture, LUT, ring) | pass | pass | pass |
| canvas2d: waterfall renderer draws the right pixel | pass | pass | pass |
| webgl: float textures (informational, not used by the probe renderer: it is R8) | info | info | info |
| webgpu: adapter (optional renderer) | pass | pass | FAIL |
| webgpu: waterfall renderer creates and draws (no pixel check) | pass | pass | FAIL |
| offscreencanvas: constructor, 2d and webgl2 contexts on the main thread | pass | pass | pass |
| worker: module worker + OffscreenCanvas waterfall (canvas2d) | pass | pass | pass |
| worker: module worker + OffscreenCanvas waterfall (webgl) | pass | pass | pass |
| worker: requestAnimationFrame and webgl2 inside a classic worker | pass | pass | pass |
| audio: AudioContext at 32 kHz + AudioWorklet module + node | pass | pass | pass |
| audio: HTMLAudioElement WAV / FLAC / Ogg | pass | pass | pass |
| image: createImageBitmap(ImageData) | pass | pass | pass |
| stream: DecompressionStream('deflate') (backlog) | pass | pass | pass |
| net: WebSocket binary frame from the mock (438 bytes, BMW1) | pass | pass | pass |
| dom: dialog.showModal | pass | pass | pass |
| css: 100dvh and env(safe-area-inset-top) | pass | pass | pass |
| css: prefers-color-scheme, prefers-reduced-motion | pass | pass | pass |
| pointer: pointer events and setPointerCapture | pass | pass | pass |
| net: navigator.connection.saveData (informational) | info | info | info |

Details of the rows that are not a plain pass:

- edge, webgl: float textures (informational, not used by the probe renderer: it is R8): webgl2 EXT_color_buffer_float true, OES_texture_float_linear true, webgl1 OES_texture_float true
- edge, net: navigator.connection.saveData (informational): connection true, saveData false
- chrome, webgl: float textures (informational, not used by the probe renderer: it is R8): webgl2 EXT_color_buffer_float true, OES_texture_float_linear true, webgl1 OES_texture_float true
- chrome, net: navigator.connection.saveData (informational): connection true, saveData false
- firefox, webgl: float textures (informational, not used by the probe renderer: it is R8): webgl2 EXT_color_buffer_float true, OES_texture_float_linear true, webgl1 OES_texture_float true
- firefox, webgpu: adapter (optional renderer): no adapter
- firefox, webgpu: waterfall renderer creates and draws (no pixel check): no webgpu adapter
- firefox, net: navigator.connection.saveData (informational): connection false

Reading the matrix:

- The waterfall needs no float texture: levels are uint8 (0.5 dB steps) in R8 textures and the colour table is a 256 x 1 RGBA texture, so `EXT_color_buffer_float` and `OES_texture_float` are informational only. WebGL2 with R8 is core.
- Everything the design depends on passes in all three engines: WebGL2 renderer, Canvas2D renderer, module worker with OffscreenCanvas (both 2D and WebGL), `requestAnimationFrame` in a worker, AudioWorklet at 32 kHz, WebSocket binary frames, `BigInt` timestamps, `DecompressionStream`, `dialog.showModal`, `dvh` and safe-area units.
- WebGPU is available in Edge and Chrome and absent in Firefox 152 on this machine (no adapter). The design does not need it; it stays an optional renderer behind the same interface.
- `navigator.connection` (Save-Data) exists in Edge and Chrome and not in Firefox: the phone client needs a visible "low data" toggle instead of relying on it.
- Headless runs use the machine's GPU through ANGLE (the renderer strings are in the details). A phone GPU, a software renderer or a remote desktop will behave differently: this matrix says the features exist, not how fast they are.

## 2. Not testable here: WebKit (Safari on iOS and macOS) and Android Chrome. UNVERIFIED

No WebKit engine is installed. Every row below is a risk taken from documentation or from general knowledge, and is marked by source. Treat all of them as UNVERIFIED until the checklist in section 3 has been run on a phone.

| Area | Risk | Design answer in the prototype | Source |
|---|---|---|---|
| WebGL contexts | WebKit limits active contexts and loses the oldest when a page creates more (16 in the commit; one bug report mentions 8 in a video-texture case) | one context; `webglcontextlost` handler switches to Canvas2D and replays the CPU history ring | [WebKit commit](https://webkit.googlesource.com/WebKit/+/70e10861eb2fbd9d0862fd8702874c25ae5d8bfd), [bug 200031](https://bugs.webkit.org/show_bug.cgi?id=200031) (search result titles only; not read in full) |
| Float textures | The probe renderer in `bench/` is R8 + LUT. The shipped renderer (`engine/render.js`) uses R32F textures read with `texelFetch`: no float-linear filtering and no float render target, so `EXT_color_buffer_float` is not required. Checked by `test_browser.py` (Edge, Chrome) and `selftest.html` | none needed | `test_browser.py` parity and context-restore checks |
| WebGPU | Shipped in Safari 26 / iOS 26; the spec is still a candidate draft | not used by default | [WebGPU in iOS 26](https://appdevelopermagazine.com/webgpu-in-ios-26/), [WebGPU](https://en.wikipedia.org/wiki/WebGPU) (search results, not read in full) |
| Audio unlock | Audible playback starts only after a user gesture; `AudioContext` may start suspended | `resume()` runs inside the click that closes the consent dialog; the context is created there | [MDN autoplay guide](https://developer.mozilla.org/en-US/docs/Web/Media/Guides/Autoplay) (read) |
| Audio and the ringer switch | iOS may mute Web Audio when the ringer switch is on while `<audio>` elements still play | none yet. If confirmed, play a looping silent `<audio>` element during a listen session | general knowledge, UNVERIFIED |
| `AudioContext({sampleRate})` | Safari honoured a fixed context rate only in later versions | the worklet resamples from 32 kHz to whatever rate the context reports | general knowledge, UNVERIFIED |
| Secure context | AudioWorklet, WebGPU and `crypto` need HTTPS or localhost. A phone opening `http://<tailnet ip>` is NOT a secure context, so the probe would fail for the wrong reason | the checklist puts Tailscale HTTPS in front of the mock, as the real dashboard has | [MDN WebTransport (secure context)](https://developer.mozilla.org/en-US/docs/Web/API/WebTransport_API) for the general rule; AudioWorklet rule from general knowledge |
| Background tabs | Hidden pages get timers throttled to once per second, and once per minute after 5 minutes hidden unless they played sound in the last 30 s; WebSocket is not listed as an exemption. iOS may suspend sockets entirely when the page is hidden or the phone locks | drawing pauses while hidden; listening stops after 30 s hidden; the socket reconnects with a 60 s backlog on return | [Chrome timer throttling](https://developer.chrome.com/blog/timer-throttling-in-chrome-88) (read); iOS behaviour UNVERIFIED |
| Viewport height | `100vh` is taller than the visible area while the iOS toolbar is shown | `min-height: 100dvh` and `env(safe-area-inset-*)`; `viewport-fit=cover` | `CSS.supports` passes in the three engines; Safari support (15.4+) from general knowledge, UNVERIFIED |
| Touch | A horizontal drag on the waterfall selects a time range while a vertical drag must still scroll the page | `touch-action: pan-y` on the canvas | UNVERIFIED on a device |
| OffscreenCanvas worker | Safari supports it from 16.4 | not used by default (measured: no need at these loads) | general knowledge, UNVERIFIED |
| `<dialog>` modal | Safari 15.4+ | consent dialog | general knowledge, UNVERIFIED |
| Audio formats | Safari plays WAV and FLAC in recent versions; older ones lack FLAC | clips served as stored (FLAC or WAV); the probe prints `canPlayType` for each | measured per desktop engine above; Safari UNVERIFIED |

## 3. Five steps for a phone over the tailnet

The mock serves synthetic data only and has no login, so it must never share a port or a process with the real dashboard. Default bind is loopback; the steps below keep it that way and put Tailscale's HTTPS in front, like the real dashboard. Replace `HOST` with the PC's tailnet name.

1. On the PC, from `bench-kit/bma-audiod`: `python ui-next/mock_server.py --port 48600` (loopback only), then `tailscale serve --bg --https=8443 http://127.0.0.1:48600`. This exposes it to your tailnet only (not Funnel) on `https://HOST:8443/`.
2. On the phone (Safari on iOS, then Chrome on Android), open `https://HOST:8443/bench/features.html?id=phone`. Wait for the list to finish. Screenshot it. Rows marked FAIL are the findings; compare with the matrix above. (Fallback without Tailscale HTTPS: `--bind <tailnet ip> --allow-lan` on `http://`; AudioWorklet will report a failure only because the page is not a secure context.)
3. Open `https://HOST:8443/`. Check: the waterfall draws and scrolls; tapping a beam chip moves the large view; a horizontal drag selects a time range and a vertical drag still scrolls the page; rotate the phone; the bottom bar reaches the screen edge and nothing hides behind the toolbar.
4. Tap Review, then Play on a clip (synthetic tones): sound plays; with the ringer switch ON and OFF; speed 0.5 and 2; Y N U S change the queue count. Tap Live, Listen, tick the box, Start: a 880 Hz tone pulses; the red LISTENING pill counts down; press Stop. Start again, switch to another app for 40 s, return: listening must have stopped and the waterfall must have caught up.
5. Stop: `tailscale serve --https=8443 off`, then end the mock with Ctrl+C. Send back the step 2 screenshot and which of steps 3 and 4 failed.

## 4. Repo DOM tests and Chrome

The nine DOM suites (`ui/test_dom_*.py`) look for `msedge` and print SKIP when it is absent; they are unchanged. A Chrome fallback is cheap in principle: Chrome speaks the same DevTools protocol and takes the same flags, and `cdp.browser_exe('chrome')` here already drives it (smoke test and screenshots pass in Chrome 155). In the suites it would be one `find_edge()` change per file (about 10 lines, nine files) plus a profile directory per run. It was not done here: `ui/` is being edited on other branches and the owner asked to keep those tests as they are.
