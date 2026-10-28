# CI

Workflow `.github/workflows/ci.yml`. Tests are listed in `scripts/ci_manifest.toml`; `scripts/run_ci.py` runs them. The workflow has no per-test steps.

## Tiers

| Tier | When | What |
|---|---|---|
| guards | every PR push, every run | private paths, commit trailers, manifest completeness, `check_contracts.py`, ruff (`ruff.toml`, `scripts/check_ruff.py`), runner tests |
| fast | every PR push (not draft) | deterministic tests of the changed areas, `--tier fast` |
| full | push to master; dispatch `tier=full`; PR labelled `ci:full` | every test incl. timing-sensitive e2e (one re-run, reported FLAKY-PASS), long contract suite, Edge DOM suites; `rmem_max` raised on linux |

The full tier replaces the fast jobs (it contains them). Draft PRs run nothing. A label other than `ci:full` runs nothing and cancels nothing. A new push cancels the stale PR run, never a master run.

## Areas

An area is a directory: analysis ui pc tools contracts scripts classify control docker (+ `guards`, named explicitly). `run_ci.py --areas auto --base REF` maps changed files to areas with the `rules` in the manifest:

| Change | Areas run (linux fast) | Windows fast |
|---|---|---|
| docs only (`*.md`, except `contracts/*.md` and `docs/FEATURES.md`) | none (guards only) | none |
| `docs/FEATURES.md` | scripts, tools | none |
| `ui/` | ui, pc, docker, scripts | ui |
| `pc/` | pc, docker, scripts | pc |
| `contracts/` | contracts, analysis, tools, ui, scripts | contracts (none) |
| `analysis/` | analysis, tools, ui, pc, contracts, classify, docker, scripts | none |
| `.github/` | scripts | none |
| a file no rule matches | every area | every area |

Windows fast runs only when a changed file is under `pc/` or `ui/` (reverse dependencies are `linux_only`); the `plan` job counts the tests per OS and the windows job is skipped at 0.

## Run locally

    python scripts/run_ci.py --tier fast --areas auto --base master --list     what would run
    python scripts/run_ci.py --tier fast --areas auto --base master            run it
    python scripts/run_ci.py --tier full --areas all --retry-timing 1          everything for this OS
    python scripts/run_ci.py --tier fast --areas guards                        the guards
    python scripts/run_ci.py --tier fast --areas auto --changed ui/bma_ui.py --list

Statuses: PASS, FAIL, SKIP (exit 77 or an unavailable `needs`, with the reason), FLAKY-PASS. Exit 1 on any FAIL. `$GITHUB_STEP_SUMMARY` gets a table; `--report FILE` writes JSON. Tests needing torch, onnx, a browser, node, pwsh or powershell.exe SKIP with the reason where the tool is absent. CI linux has no torch: `analysis/test_analysis.py` and `test_provenance.py` SKIP there; run them in the audioeval venv.

## Run the full tier on a PR

    gh pr edit N --add-label ci:full                      (or, on the PR head:)
    gh workflow run ci --ref BRANCH -f tier=full
    gh workflow run ci --ref BRANCH -f tier=fast -f areas=ui,pc

Merge rule (no required checks or branch protection on this plan, so by process): before every merge, add `ci:full` or dispatch the full tier on the PR head, wait for green, then merge. Agents and the orchestrator do this.

## Add a test

Add one line to `tests` in `scripts/ci_manifest.toml` (`path`, `area` = its directory, `tier`, `os`; `timing_sensitive = true` for real-time paced e2e, which must be tier full; `needs`, `timeout`, `args` optional). Do not edit `ci.yml`. `scripts/test_ci_workflow.py` fails on a `test_*.py|js|ps1` with no entry or skip, a missing path, an invalid value. `ui-next/` tests use area `ui`.

## Numbers

Measured on 18 finished runs (2026-10-27/28, before): linux 18.4 min per run, windows 1.5 min per run (billed x2), 21.4 effective minutes per run, 20 runs in 24 h (428 effective min), 11 of 20 failed, mostly timing tests.

| Test (CI linux, min per run) | Tier now |
|---|---|
| analysis/test_indices 3.3, test_bursts 2.3, test_clips 2.0, test_coherence 1.6, test_mixscore 1.6, test_beam_shift 1.2, test_analyzer_history 0.7 | full, timing_sensitive |
| contracts/test_contracts 1.4 | full |
| pc/test_cable_sink 0.9 | full, timing_sensitive |
| analysis/test_beam_response_map 0.8 | full |
| pip install 0.56 linux, 0.39 windows | cached by `setup-python cache: pip` |

Wall seconds of every test on the loaded dev machine (Windows Python 3.13 and WSL; about 2x the CI linux time) are in the table below; fast = under about 25 s here.

| Seconds | Test |
|---|---|
| 295 / 267 / 265 / 265 / 204 / 150 | indices / clips / bursts / mixscore / coherence / beam_shift (full) |
| 261 | contracts/test_contracts (full) |
| 47 / 25 / 13 / 7 | analysis: manifest / labels / meta_history / pcen (fast) |
| 23 / 23 / 22 / 20 / 15 / 13 | pc cable_sink (full) / pc setup_tls / pc protect_root / ui test_auth / ui bma_ui_more / pc backup (fast) |
| under 10 | every other fast test |

Fast tier per push (windows 9 tests: 119 s on the loaded box; linux ui+pc+docker+scripts: about 60 s; linux, every area: about 4 min WSL under load). Plus per job about 40 s of checkout and cached install. Full tier: same tests as before plus classify, docker, the Edge DOM suites; run on demand and on master only.

| | Before | After (estimate) |
|---|---|---|
| every PR push | 21.4 effective min | guards 2 + plan 1 + fast-linux 3-5 (+ windows 2-3 x2 when pc/ui changed) = 6-12 |
| full run | 21.4 | about 25 (plus the DOM suites), only on master, dispatch or label |
| 20 runs/day, 3 of them full | 428 | about 200 |

## Copilot code review

Separate from this workflow: GitHub's "Running Copilot Code Review" dynamic workflow ran 75 times (564 min, 7.5 min each) on PR pushes. Switch off automatic review on push: repo Settings > Copilot > Code review (disable "Review new pushes"), and github.com/settings/copilot (automatic review). Recommended: request a review by hand on the final head.
