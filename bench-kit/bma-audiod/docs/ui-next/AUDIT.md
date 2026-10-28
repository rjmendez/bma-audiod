# Audit of the current dashboard

Measured with `ui-next/tools/audit_current.py` and `audit_spectrum.py`: the real `ui/bma_ui.py` and `ui/static` served over a synthetic pipeline tree (7,200 one-second beam windows, 802 events) and the repo's own spectrum fixture server, in headless Edge over the DevTools protocol. Never the live dashboard. Steady-state windows are 21 s; the phone profile is a 390 px viewport with touch and a 4x CPU slowdown. The machine was at 97 to 100 % CPU from other jobs during the runs, so absolute CPU is inflated; read the ratios. KB = 1,024 bytes throughout.

## 1. First load (1280 px, desktop)

| Item | Value |
|---|---|
| Requests | 15 |
| Bytes on the wire | 356.7 KB |
| First paint | 128 ms |
| DOMContentLoaded / load | 69 / 80 ms |
| Largest responses | /api/detections/events 184.2 KB, /static/app.js 116.8 KB, /static/app.css 13.3 KB, /static/review.js 11.7 KB |
| JavaScript | `app.js` 116.8 KB, `review.js` 11.7 KB, `tip.js` 6.4 KB, `session.js` 3.3 KB |

The first paint is fast; the weight is the first `/api/detections/events` response (500 events, `min_conf` 0).

## 2. Every pane: source, rate, cost

Refresh: `app.js` polls its tab every 7 s (`setInterval(refresh, 7000)`); the System tab also runs the spectrum poll (500 ms). Bytes are the steady-state wire bytes per second over 21 s.

| Tab | Panels | DOM nodes | Endpoints (requests in 21 s, KB per request) | KB/s | CPU desktop | CPU phone 4x | Layouts/s |
|---|---|---|---|---|---|---|---|
| Overview | 6 | 2971 | `beams/live` 3 x 2.1; `calibration` 3 x 0.9; `detections/events` 3 x 184.2; `detections/species` 6 x 4.2; `status` 3 x 3.7 | 28.2 | 0.3 % | 2.5 % | 0.57 |
| Detections | 8 | 21938 | `beams/live` 4 x 2.1; `calibration` 4 x 0.9; `detections/density` 12 x 77.9; `detections/events` 8 x 101.7; `detections/species` 8 x 2.9; `status` 4 x 3.7 | 81.7 | 1.5 % | 10.0 % | 1.0 |
| Beams | 7 | 2387 | `beams/heatmap` 3 x 12.3; `beams/live` 3 x 2.1; `beams/stats` 3 x 2.8; `calibration` 3 x 0.9; `status` 3 x 3.7 | 3.1 | 0.3 % | 3.8 % | 0.71 |
| System | 7 | 1832 | `beams/live` 3 x 2.1; `calibration` 3 x 0.9; `spectrum` 42 x 0.9; `status` 3 x 3.7; `system` 3 x 1.2 | 2.8 | 2.5 % | 55.5 % | 7.06 |
| Review | 2 | 1059 | `beams/live` 3 x 2.1; `calibration` 3 x 0.9; `review/next` 3 x 0.6; `review/stats` 3 x 0.6; `status` 3 x 3.7 | 1.1 | 0.1 % | 1.1 % | 0.29 |

Findings:

- `/api/detections/events` is refetched in full every 7 s: 184.2 KB per fetch, 26.3 KB/s on Overview and 81.7 KB/s on Detections with `/api/detections/density` (77.9 KB per fetch). The rows change by a handful per minute.
- `/api/status` is fetched by every tab, `/api/calibration` every 7 s although it changes rarely (0.9 KB per fetch).
- The Detections tab holds 21938 DOM nodes at 1280 px (Overview 2971, Review 1059).
- System with no spectrum source: 55.5 % CPU and 26.05 layouts/s on the phone profile (desktop 2.5 %): the panel polls at 2 requests/s even when the state is "no data".

## 3. The live spectrum panel with data (System tab)

| Profile | CPU | Script | Layouts/s | Requests/s | Bytes per request | Total KB/s |
|---|---|---|---|---|---|---|
| desktop | 3.0 % | 1.8 % | 4.05 | 1.93 | 1901 | 3.7 |
| phone_4x | 21.1 % | 8.8 % | 6.49 | 1.28 | 2419 | 3.1 |

One beam (the power mean) per request; `compare=true` adds the 8 octave-band levels. The 8 per-beam waterfalls the owner would expect are not in this panel: they would need 8 more polled requests per tick.

## 4. Phone readability (current UI)

| Width | Tab | Horizontal overflow | Elements past the right edge | Targets under 44 px | Page height px |
|---|---|---|---|---|---|
| 360 | Overview | no | 47 | 24 | 2825 |
| 360 | Detections | no | 381 | 34 | 4359 |
| 360 | Beams | no | 21 | 27 | 3191 |
| 360 | System | no | 0 | 23 | 2839 |
| 360 | Review | no | 0 | 10 | 1041 |
| 390 | Overview | no | 36 | 24 | 2791 |
| 390 | Detections | no | 324 | 34 | 4305 |
| 390 | Beams | no | 0 | 26 | 2860 |
| 390 | System | no | 0 | 23 | 2758 |
| 390 | Review | no | 0 | 10 | 1007 |
| 768 | Overview | no | 0 | 23 | 1412 |
| 768 | Detections | no | 0 | 34 | 3183 |
| 768 | Beams | no | 39 | 26 | 1813 |
| 768 | System | no | 0 | 23 | 1863 |
| 768 | Review | no | 0 | 10 | 1024 |

No tab scrolls the page sideways at any width; wide tables overflow inside their boxes. Detections at 360 px has 381 elements past the edge. Targets under 44 px: 10 to 34 per tab at 390 px.

## 5. Panels by tab (1280 px)

- **Overview** (6 panels): Last 6 h: most recent detectionsiOne row per; Last 6 h: top species and where fromiBars = ; Live beam compass (last 60 s); Events per hour, last 24 h; Activity by group, last 24 hiEvents per hour; Background sound
- **Detections** (8 panels): EventsiOne event = one call reported on one ; Detections: time × direction; Species summary; Hour of day (local), events; Species × hour of day × direction; Species timeline
- **Beams** (7 panels): Live beams, last 60 s; Bias: each beam relative to the ring meaniLo; Beam × time heatmapiPower-mean level per bea; Direction strip: time × directioniThe same b; Leader share per hour (2-8 kHz)iShare of win; Contrast statistics
- **System** (7 panels): Live spectrumiA live spectrum of the array: ; Stream loss, last 24 hi30 s samples from the; Loss per hour; Board temperatures; Board, memory and versions; Analysis plugins; DSP monitor (raw)
- **Review** (2 panels): ReviewiKeys: space play, r replay, 1-4 verdi; Labels per speciesiBar fills at 20 labels

Status strip cards: Streamno statusbridge_st; BoarddisabledBoard polli; Last detection2 min agoA; Beam patternnot readable; Sources10 stalebeam_band; Backfilldone7200 beam wi; Loss, 24 hn/ano stream s; Board temp, 24 hn/aBoard; Stream state, 24 hno dat.

## 6. Redundant, diagnostic and unreadable on a phone

Redundancy table: ARCHITECTURE.md (redundancy removed). There is no usage telemetry in the dashboard, so no pane is marked unused; panes are marked low-value by role only:

| Pane | Why |
|---|---|
| Strip cards Sources, Backfill, Stream state 24 h | operator diagnostics, repeated in System |
| System: DSP monitor (raw), Board memory and versions | operator only, rarely actionable |
| Beams: Contrast statistics, Leader share | four analytic panels over the same `/api/beams/*` data; keep as one drill-down |
| Detections: Species x hour x direction | third view of the density data |
