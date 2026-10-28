# CI registration (for scripts/ci_manifest.toml)

Not registered in `ci.yml` on this branch: the CI manifest replaces the workflow. `scripts/test_ci_workflow.py` on this branch does not scan `ui-next/test`, so it passes as on master. Run from `bench-kit/bma-audiod`; measured on a loaded Windows host.

| File | tier | os | timing_sensitive | needs | timeout s | Measured |
|---|---|---|---|---|---|---|
| `ui-next/test/test_dsp.js` | fast | linux, windows | no | node | 60 | 63 checks, under 1 s |
| `ui-next/test/test_mock_server.py` | fast | linux, windows | no | none (numpy) | 60 | 37 checks, 6 s |
| `ui-next/test/test_verdict_contract.py` | fast | linux, windows | no | none (imports `analysis/labels.py`) | 30 | 6 checks, under 1 s |
| `ui-next/test/test_ui_quality.py` | fast (static half) and full (browser half) | windows for the browser half; linux runs the static half and prints SKIP for the browser half, exit 0 | yes (headless rAF is throttled under load; the checks need only progress, never a rate) | browser | 240 | 67 checks, 52 to 66 s |
| `ui-next/test/test_browser.py --browser edge` | full | windows | yes (the audio ring and the 599-frame backlog wait on a paced server) | browser | 300 | 51 checks, 94 s |
| `ui-next/test/test_adapter.js` | fast | linux, windows | no | node (and python for the label pin) | 60 | 34 checks, under 2 s |
| `ui-next/test/test_spectrum_relay.py` | fast | linux, windows | no (real UDP on loopback, 8 s) | none | 60 | 7 checks, 10 s |
| `pc/test_ui_next_install.py` | fast | windows | no | powershell | 180 | 7 checks, about 25 s (runs the installer's ui block, starts the installed `bma_ui.py --ui-next`) |
| `ui/test_ui_ws.py` | fast | linux, windows | no (real sockets; waits on state) | none | 120 | 72 checks, 7 s |
| `ui/test_bma_ui_next.py` | fast | linux | no | none | 60 | 27 checks, under 10 s |
| `ui-next/test/test_next_browser.py --browser edge` | full | windows | yes (real server, real UDP feed at 10 rows/s, polled waterfall) | browser | 300 | 34 checks, about 70 s |

Not tests, not registered: `ui-next/test/mutation_dsp.py` (32 mutations, about 100 s, run on demand), `ui-next/test/gen_refs.py` (needs numpy, scipy, librosa), `ui-next/tools/run_selftests.ps1` (Chrome, Edge, Firefox self-test page; needs the three browsers).

`test_browser.py` exits 77 and prints SKIP when no Chrome or Edge is installed: the manifest should treat 77 as a failure on the windows runner, where Edge is installed. The linux job has no torch; none of these files import it.
