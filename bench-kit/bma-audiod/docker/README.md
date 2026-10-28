# Docker setup for the BMA pipeline

Run every command from `bench-kit/bma-audiod` (build context). Images and compose file name no machine, user, drive or address: paths come from two mounted volumes, the rest from four variables in `.env.example`.

## What runs where

| Part | Where | How |
|---|---|---|
| Analyzer (`analysis/host.py`; JSONL + status under `<data>/analysis`) | container `analyzer-cpu` or `analyzer-gpu` | UDP `127.0.0.1:48300` |
| Streaming classifier (`classify/bma_classify.py`, BirdNET on CPU) | container `classifier` | UDP `127.0.0.1:48200`, detections in `<data>/logs/detections.jsonl` |
| Dashboard (`ui/bma_ui.py`, stdlib only) | container `dashboard` | TCP `127.0.0.1:48500` |
| Board binaries (`dspd`, `daemon`) | built in a container, run on the unit | `docker build --output` |
| Tests and contracts | containers `tests`, `dspd-test` | below |
| Dante multicast bridge (`pc/dante_bridge.py`) | host | needs the Dante card's NIC and host multicast delivery |
| Windows cable sink (`pc/bma_cable_sink.py`) | host | needs WASAPI and the virtual cable driver |

The containers are a parallel path (dev, fleet). The Windows supervisor (`pc/run_pipeline.ps1`) keeps starting bridge and sink; analyzer and classifier run in WSL or in containers at the operator's choice.

## Volumes and variables

| Variable | Used for | Default |
|---|---|---|
| `BMA_DATA_DIR` | mounted at `/data`: `analysis/`, `history/`, `logs/`, `reports/`, optional `config/` | `./data` |
| `BMA_MODEL_DIR` | mounted at `/models`: `panns_data/`, `hf/` | `./models` |
| `BMA_BOARD` | dashboard board panel (IPv4 of the unit); unset = panel off | unset |
| `BMA_DSPD_TOKEN_FILE` | bearer token file, mounted read-only; unset = no token | unset |

- Put them in a gitignored `.env` next to `docker-compose.yml` (copy `.env.example`).
- Ports fixed, published on loopback only. To publish elsewhere use `docker-compose.override.yml`; the dashboard's Host allow-list covers `localhost`, `127.0.0.1` and IP literals on its own port, so the published port must stay 48500.
- Optional `<data>/config/`: `geo_county.json` (county-level BirdNET location filter; present = on) and `tonal_mask.json` (replaces the image's mask).
- Containers run as uid 10001, read-only root FS (`/tmp` tmpfs), all capabilities dropped. Linux host: the two mounted directories must be writable by uid 10001 (`chown 10001 <data-dir> <model-dir>` or `user:` in an override). Docker Desktop and WSL mounts need nothing.
- Login: the dashboard requires `<data-dir>/dashboard_token.txt` (24+ printable characters, no trailing newline, readable by uid 10001) and refuses to start without it. HTTPS: `dashboard_cert.pem` + `dashboard_key.pem` in `<data-dir>/tls/` (`pc/setup_tls.ps1` makes and renews them on the host);
  - restart the container after renewal.
- dspd token: bind-mounted read-only at `/run/secrets/dspd_token`, read as uid 10001. A 0600 host file owned by you is NOT readable: the board panel runs without a token and the entrypoint says so on stderr (`docker compose logs dashboard`).
  - Fix: `install -m 0400 -o 10001 <token-file> <data-dir>/dspd_token` and point `BMA_DSPD_TOKEN_FILE` at the copy, or `chown 10001` the original.

## Analyzer, classifier, dashboard

```
cp .env.example .env
docker compose --profile setup run --rm models      # once: PANNs + CLAP weights into BMA_MODEL_DIR (needs network)
docker compose --profile cpu up -d --build          # analyzer-cpu + classifier + dashboard
docker compose ps                                   # STATUS shows the healthchecks
```

- Perch shadow plugin (`--perch-model`) is not in the image (no onnxruntime, `perch.py` not copied, no `--perch-*` flags): use the PC install (`contracts/specs/perch.md`).
- Healthchecks: analyzer = `analysis/status.json` rewritten within 60 s (300 s start period); classifier = UDP 48200 bound; dashboard = `GET /` answers 200.

### Feeding from the host bridge
```
python pc/dante_bridge.py --to 127.0.0.1:48200 --all-to 127.0.0.1:48300 ...
```
`--to` feeds the single-channel classifier, `--all-to` the 8-beam analyzer (default target `wsl:48200` is the WSL path). Docker Desktop WSL2: published UDP ports on `127.0.0.1` reachable from Windows. Datagrams arrive from the Docker gateway address; neither service checks the source.

### WSL2
- All WSL2 distros share ONE network namespace: a container publishing `127.0.0.1:48300/udp` collides with any other listener on that port ("address already in use"). Stop the other one or publish other host ports (`ports: !override ["127.0.0.1:48310:48300/udp"]`; dashboard host port stays 48500).
- Large UDP receive buffer: `net.core.rmem_max` is not settable in a container (`sysctls:` fails "permission denied"); on the Docker host: `sysctl -w net.core.rmem_max=8388608`.

### GPU
```
docker compose --profile gpu up -d --build        # analyzer-gpu (nvidia/cuda base + torch cu130 wheels) + classifier + dashboard
```
Needs NVIDIA driver and container toolkit (`nvidia-ctk runtime configure --runtime=docker`, restart Docker); compose reserves `count: all`. WSL2: install the toolkit inside the distro running Docker;
- the Windows driver provides `/usr/lib/wsl`. `analysis/host.py` picks the CUDA card with the most free memory, falls back to CPU: check `device` in `<data>/analysis/status.json`. The classifier always runs on the CPU image (BirdNET is TensorFlow-CPU).

## Board binaries (cross-build)

```
docker build -f docker/dspd-build.Dockerfile --output type=local,dest=out .
#   -> out/bma-dspd   stripped ARMv5TE static ELF
docker build -f docker/dspd-build.Dockerfile --build-arg CRATE=daemon --build-arg BIN=bma-audiod --output type=local,dest=out .
docker build -f docker/dspd-build.Dockerfile --target test .       # cargo test (unit, API, process, TCP)
```

## Tests

```
docker compose --profile cpu build analyzer-cpu      # test image sits on top
docker compose --profile test build tests dspd-test  # building dspd-test runs `cargo test`
docker compose --profile test run --rm tests            # all suites
docker compose --profile test run --rm tests contracts  # one suite: analysis classify pc ui tools contracts docker registry
```
`docker/run-tests.sh` runs each `test_*.py` as `python file`, prints `RUN` / `PASS` / `FAIL` / `SKIP` per file and a count;
- exit 1 if any failed. SKIP (not a pass): `pc/test_run_ledger.py` (needs `powershell.exe`) and any test exiting 77 (e.g. `ui/test_dom_edge.py`, needs a browser). Not run: `ui/test_app_js.js` (needs node). Contract tests that read live data have nothing to read in the container.

## Pins
Base images by exact tag: `python:3.12.15-slim-bookworm`, `nvidia/cuda:13.0.2-base-ubuntu24.04`, `rust:1.99.1-slim-bookworm`. Python packages by exact version in `requirements-analyzer.txt` and `requirements-test.txt` (live audioeval venv and contracts venv versions);
- torch `2.14.1` from the CPU or cu130 index. Transitive packages resolve at build time.
