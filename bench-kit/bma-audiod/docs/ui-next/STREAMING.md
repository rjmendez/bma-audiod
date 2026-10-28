# Streaming and rendering

Question: how should live spectrum data reach the browser and be drawn, for a scrolling waterfall of the 8 beams plus the mean (9 tiles, 60 s of history, 600 columns each) with a level meter bank, on desktop and phone browsers. Everything below was measured with `ui-next/tools/bench_transport.py` and `bench_render.py`; the real data is the `std` load (spectrum-packet standard tier) or `detail` (detail tier).

Caveat that applies to every timing: the machine ran at 97 to 100 % CPU from other jobs and headless `requestAnimationFrame` was throttled, so single runs are noisy (the frame rate column is omitted for that reason). Orders of magnitude and the repeated-run table are the evidence; repeat on an idle machine with the commands in the README before quoting a number. KB = 1,024 bytes.

## 1. Transport

Same content in every row: 9 beams x 46 uint8 bands at 10 rows/s. Bytes are measured on raw sockets (HTTP headers included for polling, WebSocket frame headers included).

| Transport | Bytes/s | Messages | What it carries |
|---|---|---|---|
| Polling JSON (current `/api/spectrum`, 2 requests/s) | 3716 | 24 | one beam (the mean) only; 2 requests/s; levels[8] for the octave-band compare strip not requested |
| SSE, JSON per row | 13367 | 120 | 9 beams, 10 events/s; handshake headers 148 B once |
| SSE, base64 of the binary frame | 6019 | 120 | 9 beams, 10 events/s; handshake headers 148 B once |
| WebSocket, binary frame | 4579 | 126 | 9 beams, 10 frames/s, 442 B per frame (438 payload + 4 WebSocket header); handshake 181 B once; backlog 264758 B once (599 frames, uncompressed) |

| Option | Verdict |
|---|---|
| Polling | covers one beam per request. Eight waterfalls would be nine requests per tick. Each poll pays headers and a layout pass |
| SSE | works over plain HTTP/1.1 and survives proxies, text only: binary needs base64 (+33 %), JSON is 2.9x the WebSocket bytes. One direction, which is enough for spectrum |
| WebSocket | binary frames, 4.6 KB/s for all nine beams, bidirectional (listen control, backpressure). Needs a hand-written RFC 6455 server on the stdlib (about 150 lines, see MIGRATION phase 2) |
| WebTransport | needs an HTTP/3 (QUIC) server, an explicit port and a browser-trusted certificate; no stdlib server exists, so it adds a dependency (aioquic or similar). Baseline since March 2026 per MDN, Safari 26.4. Rejected: nothing in these loads needs unreliable datagrams |

Frame sizes (computed from the mock server's frames): standard row 438 B (24 header + 9 x 46); detail tier 924 B at 50 rows/s = 46,200 B/s. Over a metered connection that is 15.8 MB/h (standard) or 166.3 MB/h (detail): the phone client should default to the standard tier and drop to the mean beam only (about 0.5 KB/s) when `navigator.connection.saveData` is set. The 60 s backlog sent on connect is 262,362 B raw and 142,093 B deflated (synthetic noise compresses worse than real levels); compress it with `DecompressionStream("deflate")` only if the connect cost matters. ADPCM-style row compression is not justified: a 438 B frame deflates to about 292 B and the steady rate is already 4.6 KB/s.

Clip tiles (3 s spectrogram, 150 columns x 64 bands, uint8): 9,604 B raw, 3,958 B deflated, against the stored PNG sidecars (7.5 KB average in the live clips directory per the audit). A raw tile needs no decoder in the page and is the same level scale as the waterfall.

## 2. Rendering, one run per cell

Columns: JS time per animation frame for push + draw (main-thread variants), main-thread CPU share (CPU time of all page tasks over wall time), worker busy share, bytes uploaded to the renderer per second. Worker rows count only work inside the worker; on the phone profile a worker above 100 % means it could not keep up and queued.

### Desktop (1280 px, no throttle)

| Load | Renderer | Where | Render ms/frame | p95 ms | Main CPU % | Worker busy % | Upload KB/s |
|---|---|---|---|---|---|---|---|
| std: 46 bins x 10 columns/s | canvas2d | main | 1.09 | 2.3 | 2.5 | 0.0 | 4.1 |
| std: 46 bins x 10 columns/s | canvas2d | worker | 0.00 | 0.0 | 2.0 | 0.2 | 4.0 |
| std: 46 bins x 10 columns/s | tiled | main | 0.59 | 3.5 | 2.9 | 0.0 | 4.2 |
| std: 46 bins x 10 columns/s | webgl | main | 0.38 | 1.1 | 2.1 | 0.0 | 4.4 |
| std: 46 bins x 10 columns/s | webgl | worker | 0.00 | 0.0 | 1.1 | 0.1 | 4.2 |
| std: 46 bins x 10 columns/s | webgpu | main | 0.94 | 4.0 | 0.6 | 0.0 | 4.5 |
| detail: 100 bins x 50 columns/s | canvas2d | main | 1.48 | 10.9 | 1.5 | 0.0 | 49.2 |
| detail: 100 bins x 50 columns/s | canvas2d | worker | 0.00 | 0.0 | 1.7 | 0.7 | 44.7 |
| detail: 100 bins x 50 columns/s | tiled | main | 12.25 | 30.2 | 6.7 | 0.0 | 52.8 |
| detail: 100 bins x 50 columns/s | webgl | main | 0.76 | 3.5 | 34.7 | 0.0 | 46.3 |
| detail: 100 bins x 50 columns/s | webgl | worker | 0.00 | 0.0 | 0.8 | 0.3 | 44.4 |
| detail: 100 bins x 50 columns/s | webgpu | main | 2.42 | 11.1 | 22.8 | 0.0 | 70.5 |
| heavy: 1024 bins x 50 columns/s | canvas2d | main | 24.83 | 27.2 | 75.7 | 0.0 | 482.6 |
| heavy: 1024 bins x 50 columns/s | canvas2d | worker | 0.00 | 0.0 | 2.8 | 1.1 | 490.5 |
| heavy: 1024 bins x 50 columns/s | tiled | main | 1063.87 | 3148.6 | 46.4 | 0.0 | 452.2 |
| heavy: 1024 bins x 50 columns/s | webgl | main | 28.87 | 67.8 | 69.1 | 0.0 | 1089.0 |
| heavy: 1024 bins x 50 columns/s | webgl | worker | 0.00 | 0.0 | 2.6 | 0.4 | 474.8 |
| heavy: 1024 bins x 50 columns/s | webgpu | main | 5.31 | 19.7 | 1.0 | 0.0 | 508.5 |

### Phone profile (390 px, touch, 4x CPU slowdown)

| Load | Renderer | Where | Render ms/frame | p95 ms | Main CPU % | Worker busy % | Upload KB/s |
|---|---|---|---|---|---|---|---|
| std: 46 bins x 10 columns/s | canvas2d | main | 6.18 | 29.8 | 6.2 | 0.0 | 4.8 |
| std: 46 bins x 10 columns/s | canvas2d | worker | 0.00 | 0.0 | 8.6 | 0.2 | 4.0 |
| std: 46 bins x 10 columns/s | tiled | main | 270.77 | 1012.9 | 89.1 | 0.0 | 4.9 |
| std: 46 bins x 10 columns/s | webgl | main | 2.98 | 7.5 | 4.5 | 0.0 | 4.3 |
| std: 46 bins x 10 columns/s | webgl | worker | 0.00 | 0.0 | 6.0 | 0.2 | 4.4 |
| std: 46 bins x 10 columns/s | webgpu | main | 3.33 | 9.6 | 13.0 | 0.0 | 4.2 |
| heavy: 1024 bins x 50 columns/s | canvas2d | main | 123.83 | 270.2 | 96.1 | 0.0 | 532.1 |
| heavy: 1024 bins x 50 columns/s | canvas2d | worker | 0.00 | 0.0 | 8.5 | 172.2 | 1194.8 |
| heavy: 1024 bins x 50 columns/s | tiled | main | 619.55 | 770.4 | 99.8 | 0.0 | 838.1 |
| heavy: 1024 bins x 50 columns/s | webgl | main | 5.43 | 23.0 | 30.1 | 0.0 | 561.4 |
| heavy: 1024 bins x 50 columns/s | webgl | worker | 0.00 | 0.0 | 14.7 | 0.4 | 459.0 |
| heavy: 1024 bins x 50 columns/s | webgpu | main | 17.38 | 62.5 | 42.0 | 0.0 | 473.6 |

## 3. Repeated runs: phone profile, main thread, Canvas2D vs WebGL2 (3 runs each)

| Load | Renderer | Render ms/frame (min / median / max) | Main CPU % (min / median / max) |
|---|---|---|---|
| heavy: 1024 bins x 50 columns/s | canvas2d | 6.95 / 8.49 / 28.40 | 20.4 / 21.5 / 24.8 |
| heavy: 1024 bins x 50 columns/s | webgl | 3.90 / 7.88 / 23.11 | 10.6 / 18.5 / 32.1 |
| std: 46 bins x 10 columns/s | canvas2d | 2.98 / 5.43 / 10.27 | 12.8 / 41.3 / 49.5 |
| std: 46 bins x 10 columns/s | webgl | 1.79 / 1.81 / 3.14 | 9.0 / 12.0 / 56.3 |

## 4. The two techniques the owner asked about

| Technique | Where it is used | Implemented here as | Result |
|---|---|---|---|
| Circular-buffer luminance texture, colour table in the shader, range by uniform (ShinySDR, GPL-3, `webstatic/client/widgets/spectrum-*.glsl`) | WebGL | `bench/static/lib/wf-webgl.js`: one R8 texture per tile written a column at a time, 256 x 1 colour table, scroll offset as a uniform | JS time per frame about 3x lower than Canvas2D at the real load in the repeated runs; range and colormap changes touch no pixel data |
| One `putImageData` row into stacked fixed-size canvases scrolled by a CSS transform (OpenWebRX, AGPL-3, `htdocs/openwebrx.js`) | Canvas2D + DOM | `bench/static/lib/wf-tiled.js` | Cheapest possible per column for ONE waterfall; with 9 tiles it updates 9 transforms per column and was the slowest option in 4 of the 5 cells it was measured in (single runs; 271 ms/frame on the phone profile at the real load; only desktop at the real load was on par) |

Licence rule kept: ShinySDR, OpenWebRX, BrowSDR and gqrx are GPL or AGPL and were read for technique only; no code was copied. spectrogram-js (MIT) and the WebGPU compute-FFT package were noted and not needed: the FFT already happens in `pc/bma_spectrum.py`, the browser draws levels.

## 5. Browser constraints

Feature matrix across Edge, Chrome and Firefox, the WebKit risks (WebGL context limits, audio unlock, background tabs, viewport units) and the phone checklist are in BROWSERS.md. Documented limits that shaped this design: audible Web Audio needs a user gesture ([MDN autoplay](https://developer.mozilla.org/en-US/docs/Web/Media/Guides/Autoplay)); hidden pages get timers throttled and WebSockets are not exempt ([Chrome](https://developer.chrome.com/blog/timer-throttling-in-chrome-88)); WebTransport needs HTTP/3, an explicit port and a browser-trusted certificate ([MDN](https://developer.mozilla.org/en-US/docs/Web/API/WebTransport_API)).

## 6. Recommendation

| Layer | Choice | Because |
|---|---|---|
| Transport | one WebSocket, binary `BMW1` frames (9 beams x 46 bands per frame, 438 B, 10/s), 60 s backlog on connect, text frames for events | 4.6 KB/s for all nine beams against 3.7 KB/s for one beam polled; bidirectional for listen control; stdlib-implementable (about 150 lines) |
| Fallback transport | the existing `/api/spectrum` polling, kept | phase 1 of MIGRATION ships before the socket exists |
| Render | WebGL2 circular R8 texture on the main thread, Canvas2D renderer behind the same interface as fallback and for context loss | lowest JS time at the real load in the repeats; no float textures needed; one context; every engine tested passes |
| Not chosen | OffscreenCanvas worker, WebGPU, tiled DOM canvases, SSE, WebTransport | the worker and WebGPU show no benefit at 46 to 100 bins and add engine constraints; tiled DOM is the slowest with 9 tiles; SSE is text-only; WebTransport needs a QUIC server |
| Build | none: same-origin ES modules, no framework | the whole front end is the size listed in README.md of ui-next; CSP unchanged for scripts |

Code cost: server side about 150 lines (RFC 6455 handshake, binary frames, ping/pong, close, slow-client drop) plus a frame builder; client side two renderers of about 60 and 70 lines behind one interface, a 40-line socket wrapper with backoff, the rest is UI.

## 7. Unified shell, before and after (Edge, headless, same host, `tools/measure_proto.py`; data `data/measure_edge.json`)

The renderer shipped here is `engine/render.js` (R32F textures, several rings and layers on one canvas), not the R8 probe renderers measured above.

| | ui-next (before) | ui-lab (before) | Combined |
|---|---|---|---|
| First load, requests | 8 | 7 (static files) | 10 |
| First load, bytes | 54.2 KB wire | 103.5 KB static, not measured on the wire | 159.6 KB wire (116.9 KB of JavaScript: app 23.5, review 32.7, DSP 22.4, renderer 18.0, audition 12.2, colormaps 8.0) |
| Live, desktop CPU / phone 4x CPU | 1.4 % / 11.7 % | not measured | 1.7 % / 8.7 % |
| Live, DOM nodes / heap | 315 / 1.2 MB | not measured | 751 / 1.2 MB |
| Review, desktop / phone 4x CPU | 1.2 % / 5.2 % | not measured | 5.8 % / 23.3 % (measured before the idle-loop fix; Review shows 4 canvases and the live socket keeps running) |
| Detections / System, phone 4x CPU | 7.1 % / 13.1 % | n/a | 6.6 % / 6.6 % |
| Network in every view | 4.4 KB/s | n/a | 4.5 KB/s (WebSocket 10.2 frames/s) |
| Horizontal overflow 360, 390, 768, 1280, 1920 px | none | not measured | none in 4 views at 5 widths |

The combined page is larger than either source because it carries both feature sets (the viewer and the engine). The Review figures above predate the frame-loop fix (the loop ran in every view); not re-measured after it.
