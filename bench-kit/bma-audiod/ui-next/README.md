# ui-next

One dashboard shell (Live, Review, Detections, System) with one waterfall renderer, one DSP module and one audition engine, served by a stdlib mock backend with synthetic data. Served by `ui/bma_ui.py` only with `--ui-next`, under `/next/` (phase 1; `adapter.js` picks the mock's v2 surface or the existing endpoints). Design, numbers and plan: `docs/ui-next/` (ARCHITECTURE, STREAMING, BROWSERS, AUDITION, DATA-MODEL, MIGRATION, OPEN-CHOICES, UI-QUALITY, CI-REGISTRATION).

All commands from `bench-kit/bma-audiod`.

| Step | Command |
|---|---|
| Mock backend + dashboard (loopback; `--allow-lan` to expose) | `python ui-next/mock_server.py --port 48600`, open `http://127.0.0.1:48600/` (`?dev` hot reload, `?r=canvas2d` renderer override) |
| DSP tests / mutations | `node ui-next/test/test_dsp.js`, `python ui-next/test/mutation_dsp.py` |
| Mock server, verdict contract, adapter | `python ui-next/test/test_mock_server.py`, `python ui-next/test/test_verdict_contract.py`, `node ui-next/test/test_adapter.js` |
| Live WebSocket (`/api/v2/live`): sockets, frames, rules; mutations | `python ui/test_ui_ws.py`, `python ui/mutation_ws.py` |
| Real server (`--ui-next`) assets and CSP; browser suite against it | `python ui/test_bma_ui_next.py`; from `ui-next/test`: `python test_next_browser.py [--browser edge] [--shots-dir DIR]` |
| Real rows into a test dashboard | `python ui-next/tools/spectrum_relay.py --source <dashboard URL> --token-file <its token file> --to 127.0.0.1:48521 [--insecure]` (GET only on the source, rows only, loopback UDP out); then the line below with `--spectrum-port 48521` |
| Run it against a real feed | `python ui/bma_ui.py --ui-next --port 48501 --pipeline-root <test data> --spectrum-port <udp>`, open `http://127.0.0.1:48501/next/` (never the live 48500 or the live data dir) |
| Browser suite, UI quality gates | `python ui-next/test/test_browser.py [--browser chrome\|edge]`, `python ui-next/test/test_ui_quality.py` |
| Measurements | `python ui-next/tools/measure_proto.py`, `first_paint.py`, `bench_transport.py`, `bench_render.py`, `cross_browser.py` |
| Screenshots | `python ui-next/tools/shots.py OUT_DIR --widths 390,1280` |
| Engine self-test in Chrome, Edge, Firefox | `ui-next/tools/run_selftests.ps1` |

Rules kept: synthetic data only in git; no audio stored by the page or the server; live listening is off until a per-session consent; the colour tables come from `tools/make_colormaps.py`; `bench/` holds the rejected renderers and feature probes behind STREAMING.md and BROWSERS.md.
