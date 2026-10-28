# Artifact-gate UI: tests to register in scripts/ci_manifest.toml (PR #77)

Branch artifact-gate-ui. `.github/workflows/ci.yml` is not edited. Until #77 lands, `scripts/test_ci_workflow.py` is red on exactly these two files:
`ui/test_artifact_ui.py`, `ui/test_dom_artifact.py` ("run or named in a skip comment"). Paths relative to bench-kit/bma-audiod. Seconds measured on a loaded Windows box.

| path | area | tier | os | timing_sensitive | needs | timeout | seconds | note |
|---|---|---|---|---|---|---|---|---|
| ui/test_artifact_ui.py | ui | fast | linux | no | none | 120 | 1 | stdlib, hermetic, real server on an ephemeral port, 53 checks |
| ui/test_dom_artifact.py | ui | full | windows | yes | browser | 600 | 58 | Edge DOM, 9 checks; fixed sleeps wait for the server's 1 s poll (2.5 s after start, 1.5 s after a toggle click) |
| ui/mutation_artifact.py | ui | full | linux | no | node | 300 | 20 | not a test file; 13 mutants must all be KILLED, exit 1 otherwise. Register as a test (needs node for the JS mutants) or leave it to the by-hand list |

Already registered, unchanged: `analysis/test_artifact_gate.py` (fast, linux), `ui/test_app_js.js` (gains 29 artifact checks, no new file).

Motion: `ui/test_dom_artifact.py` asserts no animation, transition, scroll or rAF behaviour (it reads DOM state, the toggle's `checked`, row counts and the tooltip text), so it does not depend on `prefers-reduced-motion`. A runner with OS animations off needs no emulation for it. If a motion check is ever added, set `Emulation.setEmulatedMedia` prefers-reduced-motion explicitly in both branches, as `ui/test_dom_spectrum.py` does.

Rules: a change under `ui/static/` or `ui/bma_ui.py` selects area ui; `contracts/specs/` selects ui as well (existing rules).
