# bma-audiod

PC side: receive, analyse, show Dante audio (`pc/`, `analysis/`, `classify/`, `ui/`, `tools/`). Interfaces: `contracts/`. Feature registry (state, cost, owner approval of every opt-in or default-live feature): `docs/FEATURES.md`. Current state: `STATUS.md`.

## Configuration

No tracked file names a machine (no drive letters, user names, LAN addresses, MACs, host names, tool locations). Paths derive from the program's location or working directory; the rest is one of these optional environment variables:

| Variable | Meaning | Read by |
|---|---|---|
| `BMA_DATA_DIR` | root of `analysis/`, `history/`, `logs/`, `reports/` (default `./data`; live supervisor sets it) | `analysis/host.py`, `analysis/run_analyzer.sh`, `analysis/{beam_bands_report,cluster_embeddings,learn_tonal_mask,replay_tonal_mask,summarize}.py` |
| | | `pc/history_log.py`, `pc/run_classifier.sh`, `ui/bma_ui.py`, `ui/smoke_real.py`, `contracts/{check_contracts,tools/make_fixtures}.py` |
| `BMA_MODEL_DIR` | model caches and weights (default `<data-dir>/models`) | `analysis/host.py`, `analysis/run_analyzer.sh`, `analysis/install_models.sh` (required there) |
| `BMA_BOARD` | board IP or MAC address; MAC auto-discovery stays | `tools/bma_debug_rx.py` (`auto`), `ui/bma_ui.py`, `ui/smoke_real.py`, `pc/run_pipeline.ps1` |
| `BMA_DSPD_TOKEN_FILE` | file holding the dspd bearer token (no default) | `ui/bma_ui.py`, `ui/smoke_real.py` |
| `BMA_PYTHON`, `BMA_SINK_PYTHON` | interpreters for the Windows supervisor (default `python` on PATH; the cable sink starts only when `BMA_SINK_PYTHON` is set) | `pc/run_pipeline.ps1` |
| `BMA_WSL_PYTHON` | python inside WSL for analyzer and classifier (default `python3`); torch and onnxruntime live in a venv whose location the supervisor cannot derive | `pc/run_pipeline.ps1` |

- Supervisor reports directory fixed at `<data-dir>\reports`; dashboard host names = this computer's name + (when `tailscale` is on PATH) this node's tailnet name; neither configurable. A data dir with reports elsewhere needs a junction: `mklink /J <data-dir>\reports <old reports dir>`.
- Flags with a relative default (formerly environment): `--geo-config` (on `classify/bma_classify.py` and `analysis/host.py`), `--taxa`, `--calibration`.
- Board paths `/fwlog`, `/fwdata`, `/fwsys`, `/usr/local/bin` belong to the firmware, not settings.

### Windows live deployment
`pc/install_task.ps1 -Root <data-dir>` pins a copy of the PC side into `<data-dir>\app` and registers the logon task. `pc/run_pipeline.ps1` sits in `<data-dir>` and derives everything from its location.
- Machine-specific values go in the untracked `bma.local.ps1` next to it (template `pc/bma.local.example.ps1`; installer copies once, never overwrites).
- County-level BirdNET location: untracked `classify/geo_county.local.json` (template `classify/geo_county.example.json`).

## Feature registry

`docs/FEATURES.md` declares every opt-in, off-by-default or removable feature (flag, marker file, cost, approval). `python tools/feature_state.py --root <data root>` prints the live state, OFF rows first. `python scripts/test_features_registry.py` fails on any undeclared feature.

## CI

Tiered: guards and the changed areas' fast tests on every PR push; the full suite on master, on a manual dispatch, or on a PR labelled `ci:full`. Tests are registered in `scripts/ci_manifest.toml` (one line each), not in the workflow. See `docs/CI.md`.

## Private-path guard

`scripts/check_private_paths.py` exits 1 on any private-looking value in a file git would commit (tracked + untracked-not-ignored):
- drive paths, WSL mounts, home paths, private / CGNAT / link-local addresses, MACs outside RFC 7042.
- tailnet and lab DNS names, the token file name, the WSL distro name, non-zero JSON coordinates, job folders.
- Exceptions: `scripts/private_paths_allowlist.toml` (reason + exact count each; an entry matching nothing fails; can only shrink). Optional owner-specific names: one regex per line in untracked `scripts/private_names.txt`.

    python -I scripts/check_private_paths.py          # exit 0 = clean
    python -I scripts/test_check_private_paths.py

Example values: `<repo>`, `<data-dir>`, `<board-ip>`, `192.0.2.x`, `00-00-5E-00-53-xx`.

## Verdict labels
`tools/bma_labels.py`: owner verdicts (`<data-dir>/labels/labels.jsonl`, append-only, contract `verdict-labels`) and review `queue` (near-threshold, disagreement, species-stratified random audit slice; speech excluded by construction).
- Metadata only, run by hand. Spec `contracts/specs/verdict-labels.md`, design `docs/design/label-store.md`. Tests: `analysis/test_labels.py`, `analysis/test_review_queue.py`;
- mutations `analysis/run_label_mutations.py --work <scratch dir>`.

## Merging

- Commits and PR descriptions carry no AI attribution trailer (`Co-authored-by` or `Generated with` naming an AI).
- Merge with `gh pr merge --squash --body ""`: a squash merge otherwise appends a `Co-authored-by` line per PR commit author.
- `python -I scripts/check_commit_trailers.py [--base REV]` fails on such trailers in `BASE..HEAD` (default base: the master tip when the script was written; history is not rewritten). CI: the `guards` job runs it.
