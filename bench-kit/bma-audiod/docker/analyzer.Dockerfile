# syntax=docker/dockerfile:1
# BMA analyzer + classifier image (analysis/host.py and classify/bma_classify.py). Build context: bench-kit/bma-audiod.
#   docker build -f docker/analyzer.Dockerfile --build-arg VARIANT=cpu -t bma-analyzer:cpu .
#   docker build -f docker/analyzer.Dockerfile --build-arg VARIANT=gpu -t bma-analyzer:gpu .
# gpu: nvidia/cuda "base" image (driver hooks only; torch's CUDA 13 wheels carry cuDNN/cuBLAS themselves, so no
#      second copy of the CUDA libraries is baked) + torch from the cu130 index. Needs the NVIDIA container toolkit.
# cpu: python slim + torch from the CPU index. analysis/host.py falls back to the CPU when torch.cuda is unavailable.
ARG VARIANT=cpu

FROM python:3.12.15-slim-bookworm AS base-cpu
ENV TORCH_INDEX=https://download.pytorch.org/whl/cpu
RUN apt-get update \
 && apt-get install -y --no-install-recommends libsndfile1 \
 && rm -rf /var/lib/apt/lists/*

FROM nvidia/cuda:13.0.2-base-ubuntu24.04 AS base-gpu
ENV TORCH_INDEX=https://download.pytorch.org/whl/cu130
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv libsndfile1 \
 && rm -rf /var/lib/apt/lists/*

FROM base-${VARIANT} AS runtime
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PATH=/opt/venv/bin:$PATH
RUN python3 -m venv /opt/venv
# torch first, from the variant's index; the rest resolves from PyPI (the +cpu / +cu130 torch satisfies its pins)
RUN pip install torch==2.14.1 --index-url "$TORCH_INDEX" --extra-index-url https://pypi.org/simple
COPY docker/requirements-analyzer.txt /tmp/requirements-analyzer.txt
RUN pip install -r /tmp/requirements-analyzer.txt && rm /tmp/requirements-analyzer.txt

# the same file set pc/install_task.ps1 pins into the live app directory, flat in /app, except the Perch plugin module: it
# needs onnxruntime and --perch-* flags, which this image does not carry (contracts/specs/perch.md, Deploy). The analysis files are
# listed literally (no build-time step that nothing here can run): docker/test_install_lists.py fails when this COPY differs from the
# image set of analysis/plugins.manifest.json (support_files + the files of every plugin whose deploy includes "image").
WORKDIR /app
COPY analysis/host.py analysis/plugins.py analysis/core_make.py analysis/wiring.py analysis/manifest.py analysis/plugins.manifest.json \
     analysis/hist.py analysis/taxa.py analysis/runmeta.py analysis/rollup.py analysis/coherence.py analysis/indices.py analysis/bursts.py analysis/mixscore.py analysis/pad_lane.py \
     analysis/daily.py analysis/analyzer_history.py analysis/tonal_mask.json \
     analysis/clips.py analysis/clip_features.py analysis/clip_render.py analysis/pcen.py analysis/clip_exclude.json ui/taxa.json /app/
COPY classify/bma_classify.py classify/birdnet_coverage.json pc/history_log.py /app/
COPY docker/entrypoint.sh docker/healthcheck.py docker/fetch_models.py /usr/local/lib/bma/

# non-root; /data and /models are the two mounted volumes; panns_inference reads ~/panns_data, so link it to the volume
RUN useradd --uid 10001 --create-home --home-dir /var/lib/bma --shell /usr/sbin/nologin bma \
 && mkdir -p /data /models \
 && chown bma /data /models \
 && ln -s /models/panns_data /var/lib/bma/panns_data \
 && chmod 0555 /usr/local/lib/bma/entrypoint.sh
ENV BMA_DATA_DIR=/data BMA_MODEL_DIR=/models HOME=/var/lib/bma \
    HF_HOME=/models/hf HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    TMPDIR=/tmp NUMBA_CACHE_DIR=/tmp/numba MPLCONFIGDIR=/tmp/mpl \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER bma
VOLUME ["/data", "/models"]
EXPOSE 48300/udp 48200/udp
ENTRYPOINT ["/usr/local/lib/bma/entrypoint.sh"]
CMD ["analyzer"]
