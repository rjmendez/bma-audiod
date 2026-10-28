# Dashboard design: stack, tooltips, tokens, live spectrum

Contracts: `contracts/specs/ui-http.md` 1.7.0 (`GET /api/spectrum`), `contracts/specs/spectrum-packet.md` 1.0.0 (loopback row packet); fixtures in `contracts/fixtures/`.

## 1. Constraints

- Stdlib-only Python server, GET/HEAD only, Host allowlist, no CDN, no build step, phone-friendly, accessible.
- XSS-safe: `textContent` / `el()` only.
- CSP, constant `CSP` in `bma_ui.py`, unchanged: `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`.
- Canvas 2D needs no CSP change; WASM would need `'wasm-unsafe-eval'` (not used).

## 2. Stack

| Need | Decision |
|---|---|
| Tooltips | Hand-rolled on the native Popover API, `popover="manual"`, `tip.js` (~5 KB) + ~30 lines CSS. Fallback without Popover: toggle `hidden`. Positioning: `getBoundingClientRect` flip/clamp setting `style.left/top` through the CSSOM (allowed by `style-src 'self'`) |
| CSS / layout | Hand-written tokens + components in `app.css` |
| Charts | Hand-written SVG helpers (`barChart`, `heatmap`, `compass`, `stackedShare`, `scatter`) |
| Reactivity | `el()` |
| Spectrum / waterfall | Hand-written Canvas 2D (`spectrum.js`), no copied GPL code |

Vendored files: none.
- A vendored file MUST add in the same commit: file under `ui/static/vendor/`, its LICENSE text, a `VENDOR.md` row (name, pinned version, SPDX licence, sha256, upstream URL, where used), an explicit `ASSETS` entry, the asset-enumeration test update.
- Licences: MIT/BSD/Apache/ISC/OFL only.
Colour tables in `spectrum.js` (viridis, magma: CC0; turbo: Apache-2.0, Google) are generated data (`make_colormaps.py`); the file header names source, licence, command.

Assets served (ui-http 1.5.0+): `/`, `/index.html`, `/static/app.js`, `/static/app.css`, `/static/tip.js`, `/static/spectrum.js`. `index.html` loads `tip.js` then `app.js` (global `Tip`); `app.js` loads `spectrum.js` (global `Spectrum`, ~44 KB) when the System tab is first shown.

## 3. Tooltip contract

```
<button type="button" class="tip-btn" aria-label="About <topic>" aria-describedby="tip-7" aria-expanded="false">i</button>
<span id="tip-7" class="tip-bubble" role="tooltip" popover="manual">The long text, set with textContent.</span>
```

- `Tip.tip(text, {label})` -> button + bubble DocumentFragment (`label` default "More information").
- `Tip.badge(text, kind, tipText)` -> terse badge, `kind` = `warn|bad|info`, optional info button.
- `Tip.label(textNode, tipText)` -> label followed by its info button.
- Ids `tip-<counter>`. Text only via `textContent`.
- Trigger: real `<button>`, "i" glyph in a bordered circle, >= 24x24 CSS px, 44 px tap target on coarse pointers.
- Open: keyboard `focus` at once; `pointerenter` (mouse/pen) after 300 ms; click/tap toggles and pins. Close: `pointerleave` (when not over the bubble), `blur`, `Escape` (focus stays), scroll, tap elsewhere. One bubble open at a time. Bubble never takes focus.
- `popover="manual"` (never `auto`). Placement: below the button, flip above if it does not fit, clamp to viewport with 8 px margin, `max-width: min(36ch, calc(100vw - 16px))`.
- Explanations with links or focusable content, and "how to read this chart" text over ~400 characters: native `<details><summary>`.
- `prefers-reduced-motion: reduce`: no transitions. `forced-colors`: circle uses `CanvasText`/`ButtonText` borders. Light/dark via `prefers-color-scheme`.
- `[hidden] { display: none !important }` is required (component classes set `display`).
- Tests read `[role=tooltip]` / `.tip-bubble` `textContent` and MUST assert the button's `aria-describedby` resolves to that bubble.

## 4. Visible vs behind the info button

Rule: an explanation-only sentence goes behind the info button. A fact that changes how the data is read stays visible as a terse badge; its long text goes in the badge tooltip. Data lines (counts, pager, "no data", values, timestamps) stay.

Allowed data lines: strip card value + one short sub line (stale sources: at most 3 names and "and N more", full list in tooltip); "Unclassified labels: N event(s)"; spectrum state summary ("Live|Stale|No fresh data, <beams>. Loudest band ... at N dB, ..."); pager and range lines.
`test_dom_edge.py` enforces: no own text of 80 or more characters outside tooltips, tables, buttons, headings, empty-state boxes (the live spectrum summary is not exercised).

Badges: `Unreliable region` (hatched slot-mapping data), `Not steerable` (bands 0-1), frame badge (`Uncalibrated frame` / `Calibrated panel frame (provisional)`), `STALE` (monitor log),
`speech suppressed`, `live`/`stale`/`no data` spectrum state, `unknown unit` (raw monitor cells), `aggregated` (aggregated events), `continuous caller`.

Info buttons: header (disclaimer, angle frames, time zones; text in `HELP_TEXT`; footer removed; `<noscript>` text stays); compass legend/frame notes; Overview, Detections, Beams, System panel titles. `title=` attributes stay as fallback, never the only carrier.
Helpers in `app.js`: `ptitle(title, text)` (panel title + info button, static explanations), `tnote(text, label)` (info-button row under the data, explanations with live numbers), `Tip.badge(...)`.

## 5. Tokens (`app.css`, `:root`)

Names kept: `--bg --panel --panel2 --fg --muted --line --accent --warn --bad --good --grid`. `color-scheme: light dark`, no JS theme switch.

| Group | Tokens |
|---|---|
| Space (4 px base) | `--s-1: 4px; --s-2: 8px; --s-3: 12px; --s-4: 16px; --s-5: 24px; --s-6: 32px` |
| Type | `--fs-xs: 11px; --fs-sm: 12px; --fs-md: 14px (body); --fs-lg: 15px; --fs-xl: 17px`; weights 400/600/650; `--lh: 1.4`; system font stack; `font-variant-numeric: tabular-nums` on numbers |
| Radius / lines | `--r-1: 4px; --r-2: 8px; --r-pill: 999px; --bw: 1px` |
| Colour roles | surface `--bg --panel --panel2`; text `--fg --muted`; border `--line --grid`; interactive `--accent`, `--focus: var(--accent)`; status `--good --warn --bad --info`, each with a `--*-bg` tint (12 % mix) |
| Data colours | 8 colour-blind-safe hues `--c1..--c8` (Okabe-Ito based); sequential ramps only in `spectrum.js` |
| Elevation | none; 1 px borders; only the tooltip bubble has a shadow |
| Motion | `--dur: 120ms`; transitions inside `@media (prefers-reduced-motion: no-preference)` |
| Focus | `:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px }`, never removed |

SVG charts take `fill`/`stroke` from CSS classes and `var()`, never `style=`. Colour is never the only encoding.

## 6. Components

`header` (title, tabs, status chips, help); `tabs` (`role=tablist/tab/tabpanel`, roving tabindex, arrows, Home/End, keeps `data-tab`); `status strip`; `card` (label, value, sub; tone via left accent bar);
- `panel` (title row + info button + body); `chip` (`aria-pressed`); `button` (default/primary/quiet/icon); `field` (16 px font on touch); `table` (`.tablewrap`, `.num` right-aligned); `badge`/`pill` (ok/warn/bad/info); `tip`; `legend`;
- `empty state`; `figure`.
Layout: CSS grid `repeat(auto-fit, minmax(min(100%, 340px), 1fr))`; breakpoints 560 px and 900 px; minimum width 320 px; tap targets >= 44 px under `(pointer: coarse)`.
Status strip: card markup (`.card .k .v .s`) unchanged; compact flexible chips; only `warn`/`bad` cards get a coloured left bar and tint; `#refresh` is quiet right-aligned text.

## 7. Spectrum display (System tab)

### Data path
Dante bridge `--all-to 127.0.0.1:48410` --BMAA--> `pc/bma_spectrum.py` --BMAS rows--> `bma_ui.py` ring --`GET /api/spectrum`--> browser polling.
- Separate process: own GIL and crash domain, loopback-only both sides. The spectrum path carries band levels only, never audio; clip audio (opt-in by marker file, off by default) is served only by `/api/review/audio`, login only.
- Polling, not SSE: each SSE stream pins one of `MAX_CONNECTIONS=64` slots. Polling 2 requests/s, token-bucket capped.
- Flags: `bma_spectrum.py --listen 127.0.0.1:48410 --to 127.0.0.1:48520 [--detail] [--no-gate]` (non-loopback refused; `--no-gate` not offered); `bma_ui.py --spectrum-port 48520 [--spectrum-detail]`. Ports are derived defaults in `run_pipeline.ps1`.

### Client (`spectrum.js`, global `Spectrum`)
- Layout: state line (`live` pill, `stale`, `no data`, `not enabled`, `speech suppressed` when the newest row is gated), toolbar, persistence spectrum (frequency x level), waterfall (frequency x time),
  - 8-beam compare strip (8 rows x time, colour = level of chosen octave band from `levels[]`), legend (colour bar with dB ticks), text summary.
  - Each canvas: `role="img"`, `aria-label`, `tabindex="0"`.
- Controls: Beam `Mean of 8` (default) / `Loudest` / `1`..`8`; axis `Log` (default) / `Linear`; range `Auto` / presets (-100..-20, -90..-40, -80..-30 dB) / custom; colour map `Viridis` (default) / `Magma` / `Turbo` / `Grey`; `Freeze`;
  - `Peak hold` + `Reset peak`; persistence tau `0.5 / 1 / 2 (default) / 4 s`; waterfall span `15 / 30 (default) / 60 s`; compare band (octave select, default 2-4 kHz). Detail tier offered only when `GET /api/spectrum?tier=detail` is allowed;
  - on 403 the option stays hidden.
- `Auto` range: 5th to 99th percentile of the last 10 s, rounded outward to 5 dB, at least 20 dB wide.
- `Loudest`: beam with highest mean compare-band power over the last 5 s, re-evaluated at most every 5 s.
- Polling: `since=` + `rev=` cursor every 500 ms while the System tab is shown and `document` visible; 429 backs off to `Retry-After`;
  - freeze stops polling, resume asks `limit=1`. Rows replaced by `idx`.
  - Playout buffer releases rows by `t_s` against server `now_s` with 0.3 s slack. Display lag 2 s (1.5 s speech lookahead + polling).
- Persistence histogram: `H[bin][256]` Float32, one cell per uint8 row value (independent of viewed dB range). Per frame: `H *= exp(-dt/tau)`;
  - per row and bin: `H[b][l] += 1` (weighted `exp(-age/tau)` for replayed history), `H[b][l±1] += 0.25`.
  - Intensity `I = min(1, log1p(8*H) / log1p(8*Hnorm))`, `Hnorm = tau * rows_per_s`, via a 1024-entry table into the colour LUT.
  - One `putImageData` per frame at device-pixel-ratio 1.
  - Peak hold: per-band max until `Reset peak` (no decay), 1 px dashed `--fg` line;
  - newest row a thin second line. Bands drawn as steps. x map: log 50 Hz..16 kHz or linear Hz, precomputed pixel-to-band table.
- Waterfall: newest row at top, scroll by `drawImage(self)` shifted one row; `rowPx = max(1, floor(canvasHeight / rows))`. Gated rows: same coarse values + 45° hatch + 4 px marker strip at left margin. Gap/zero rows: background with dotted hatch.
- Waterfall and compare strip are redrawn each frame from the row ring by time, so latch revisions and range/colour-map/axis changes redraw everything.
- Frame loop: capped 30 fps, drops to 15 fps when a frame takes > 25 ms.
- Readout: `pointermove`, one-finger drag, arrow keys (left/right one band, up/down one level step or row); `role="status"` line below the canvas, e.g. `4.2-4.5 kHz, -52.5 dB`; waterfall adds "12.4 s ago"; compare shows beam and level.
- Accessible summary: updated every 2 s, `aria-live="off"`, "Announce" button; collapsed `<details>` with newest row as 46-row table and per-beam compare levels. State text is never colour-only.
- Reduced motion: no animation frames; still snapshot of the last span rebuilt every 2 s; no pulsing dot; `Freeze` available.
- Theme: canvas colours from CSS tokens via `getComputedStyle`, re-read on `prefers-color-scheme` change.
- Colour maps: 256-entry RGB tables (viridis, magma, turbo, Grey); legend colour bar always shown.

### Canvas 2D, not WebGL
640x160 px, 46 bins: 2.0 ms/frame; 1280x320 px, 100 bins (detail): 7.8 ms/frame. Data: 46 bins x 10 rows/s (standard), 100 bins x 50 rows/s (detail).

## 8. Tests

Follow `AGENTS.md` test-writing rules; each new test needs a RUN mutation (`ui/mutation_check.py`, `ui/mutation_js.py`; a fifth tuple element names the mutated file).
- `test_spectrum_js.js`: colour maps, decay, axis, loudest beam, ranges, store, text.
- `test_dom_tips.py`: tooltip behaviour with real CDP key/mouse/touch input.
- `test_dom_spectrum.py`: panel against `spectrum_fixture.py`, real pixels.
- `test_dom_edge.py`: prose-count and `aria-describedby` checks for every tab.
- Prose tests read the exact strings from the tooltip bubble and assert the `aria-describedby` link.
- `test_bma_ui.py` enumerates asset paths (includes `tip.js`, `spectrum.js`).
- Spectrum: packet parser against `spectrum_examples.json`; endpoint against `ui_spectrum.json`; auth gate with positive twin; rate cap; token bucket; gate and latch (exact values from `coarsen_block`); CSP header unchanged;
  - no PCM in any fixture or log; `test_bma_ui_spectrum.py` asserts the dashboard process writes no audio-like file (extension or magic bytes) in its data tree or cwd; `test_auth_spectrum.py`, `test_dom_auth_spectrum.py`.

## 9. Access, gate, residual risk

### Who may see the spectrum (ui-http 1.7.0)
- Authentication, not source network. `GET /api/spectrum` serves both tiers only to an authenticated client; otherwise state `auth_required` (HTTP 200, no rows) and the UI shows a "sign in required" badge.
- Detail tier also needs `--spectrum-detail` (else 403 `spectrum_detail_disabled`).
- Peer address and Host header play no part.
- One function `spectrum_client_allowed(authenticated, tier, detail_flag) -> (allowed, reason)`, fed by `request_authenticated(handler)`: true only when `server.auth` exists and the auth layer set `handler.authenticated = True`.
- Login: `ui/auth.py` (ui/AUTH.md); unauthenticated requests get 401 before routing.
- A server without a login (no token file, loopback) authenticates nobody: everyone sees `auth_required`.
- Live audio to the browser is not part of this feature.

### Gate
`bma_spectrum.py` never gates; only the analyzer's speech-tag latch rewrites rows. Gated rows: one-third-octave averages over 1 s (19 values/s), both tiers, all beams.
Detail tier (50 ms windows every 20 ms, 60 % overlap, 100 bands, 50 rows/s) stays off (`run_pipeline.ps1`), authentication-gated, flag-gated.
Per-IP 429 cap.

### Owner decisions
1. Bridge cost of the extra `--all-to` target (~+2.3 points of a core, `pc/SPECTRUM.md`) accepted; MUST be re-measured at deploy on the real host.
2. No voice recordings kept: rows are magnitudes in a 60 s in-memory ring, nothing on disk.
3. Human voices hidden from every detection view and rollup (`HUMAN_LABELS`); only trace is `status.human_hidden_24h`.

### Standard tier content
46 band powers/row, 10 rows/s, 100 ms non-overlapping windows, 0.5 dB steps, magnitudes only, mean of 8 beams or one beam.
- No phase, no per-bin magnitude, no window overlap: audio not recoverable. Bands below 2 kHz are 1/3 octave wide: no pitch contour.
- Coarse envelope (syllable rhythm) remains; the speech gate is the protection.
- Persisted data: none. Dashboard does not log row values.
