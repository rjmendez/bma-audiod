# Spectrum rows for the dashboard (backend)

Contracts: `contracts/specs/spectrum-packet.md` (BMAS 1.0.0), `contracts/specs/ui-http.md` `GET /api/spectrum` (1.7.0). Design: `ui/DESIGN.md`.

```
dante_bridge.py --all-to 127.0.0.1:48410          (8 beams, BMAA, loopback)
        |
bma_spectrum.py --listen 127.0.0.1:48410 --to 127.0.0.1:48520 [--detail]      (own process: numpy)
        |  BMAS datagrams (magnitudes, uint8 dB, no phase, no sample), loopback only
bma_ui.py --spectrum-port 48520 [--spectrum-detail]     (binds 127.0.0.1 only; bounded in-memory ring; nothing persisted)
        |  GET /api/spectrum  (polling, since= cursor, rev= for rewritten rows)
browser
```

## Flags (derived defaults, no environment variables)

| program | flag | default | meaning |
|---|---|---|---|
| `bma_spectrum.py` | `--listen HOST:PORT` | `127.0.0.1:48410` | receives the bridge's BMAA; must be loopback (exit 2 otherwise) |
| | `--to HOST:PORT` | `127.0.0.1:48520` | the dashboard's `--spectrum-port`; must be loopback (exit 2 otherwise) |
| | `--detail` | off | also compute the detail tier (50 rows/s, 100 bands) |
| | `--stats-s` | 30 | status line interval |
| `bma_ui.py` | `--spectrum-port PORT` | absent = off, nothing listens | loopback UDP port for the rows |
| | `--spectrum-detail` | off | allow the detail tier for authenticated clients (needs `--spectrum-port`) |

No flag accepts a non-loopback address.
- `run_pipeline.ps1` starts `bma_spectrum.py` (standard tier only), adds `--all-to 127.0.0.1:48410` to the bridge (only when the spectrum process is running at bridge start) and `--spectrum-port 48520` to the dashboard.
- `install_task.ps1` pins `bma_spectrum.py`, `tip.js`, `spectrum.js`. Not deployed.

## Access
- `GET /api/spectrum` is gated by authentication, not source network: both tiers only to an authenticated client from any address; else `state: auth_required` (HTTP 200, no rows), dashboard shows "sign in required". Detail tier also needs `--spectrum-detail`.
- Rule: `spectrum_client_allowed(authenticated, tier, detail_flag)` in `ui/bma_ui.py`, fed by `request_authenticated(handler)` (`server.auth`, `handler.authenticated` of `ui/auth.py`).
- With a token file (supervisor creates one, `Initialize-DashboardToken`) an unauthenticated request gets 401 before routing;
  - a server without a login authenticates nobody and shows `auth_required` to everyone.
- Tests: `ui/test_auth_spectrum.py`, `ui/test_dom_auth_spectrum.py`.
- Speech: this process never gates; only the analyzer tag latch rewrites rows, 2-52 s late; drawn rows are not recalled. No audio stored.

## Implementation
- Gate `low` is power-weighted: the 300-1500 Hz share of the window's 300-3400 Hz energy.
- Beam 0 = mean over beams whose window has no gap; a gap beam is not silence.
- Rows leave when their window completes; `state` uses arrival time at the dashboard (stale = nothing for 3 s).
- Row-rate cap = token bucket per (tier, beam), burst of one second of rows (10 standard, 50 detail).
- Audio clock paces output; a bridge stall stalls rows; a gap of more than 30 s resynchronises the row counter (rows skipped, never invented).
- `Chan` keeps at most 65,536 float32 samples per beam in memory (1.4 s); the process logs only counters.

## Tests
- `pc/test_spectrum.py` (producer; 82 checks); 29 mutations in `pc/mutation_spectrum.py` (each red by a failing check; a crash or hang alone = not caught).
- `ui/test_bma_ui_spectrum.py` (dashboard; 138 checks); mutations in `ui/mutation_check.py` (`spectrum:` entries).
- `pc/bench_spectrum.py` (CPU and loss at 3000 pkt/s x 8 channels).

## CPU cost (3000 pkt/s x 8 channels, 16 frames per packet)
`python -I bench_spectrum.py --secs 20 --reps 3`: synthetic 8-channel flow through `dante_bridge.py --bind`, null UDP counters; process CPU time over 20 s, mean of 3.

| configuration | bridge, % of one core | spectrum process, % of one core | bridge loss / gaps |
|---|---|---|---|
| baseline (analysis host + cable sink targets) | 70.1 | - | 0.00 % / 0 |
| + one more `--all-to` loopback target (null counter) | 72.9 | - | 0.00 % / 0 |
| + `bma_spectrum.py`, standard tier | 72.4 | 13.4 | 0.00 % / 0 |
| + `bma_spectrum.py --detail` (both tiers) | 73.2 | 22.7 | 0.00 % / 0 |

Live audit 2026-10-27 (real host, all consumers attached): bridge 79.9 %, spectrum process 15.9 % of one core. The table rows are the synthetic bench, not the live host.

The extra `--all-to` target costs the bridge 2.3 points of a core (one more `sendto` per 256-sample block, 1500/s). Re-measure at deploy on the real host (`bench_spectrum.py`; compare `loss_pct` and `gaps` in `bridge_status.json` with and without the spectrum target).
