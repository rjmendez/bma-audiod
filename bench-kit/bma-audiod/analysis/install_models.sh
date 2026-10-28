#!/bin/bash
# One-time install of the analysis model stacks into an overlay under $BMA_MODEL_DIR (nothing goes into the venv or the
# home directory). Idempotent. Log: $BMA_MODEL_DIR/install.log.
# Usage: BMA_MODEL_DIR=<model-dir> install_models.sh [python]      python defaults to python3 on PATH
#   PANNs CNN14 (AudioSet tagger + 2048-d embeddings), CLAP htsat-unfused (zero-shot, 48 kHz native).
R="${BMA_MODEL_DIR:?set BMA_MODEL_DIR to the directory that holds the model files}"
PY="${1:-python3}"
mkdir -p "$R/overlay" "$R/panns_data" "$R/hf" "$R/tmp" "$R/pipcache" "$R/out"
R="$(cd "$R" && pwd)"
export PIP_CACHE_DIR="$R/pipcache" TMPDIR="$R/tmp" HF_HOME="$R/hf"
exec > "$R/install.log" 2>&1
set -x
date
# no-deps: torch/numpy/librosa already come from the venv; pull only what is missing
$PY -m pip install --target "$R/overlay" --no-deps --upgrade panns-inference torchlibrosa transformers huggingface_hub tokenizers safetensors regex
cd "$R/panns_data"
[ -s "Cnn14_mAP=0.431.pth" ] || curl -L --fail -o "Cnn14_mAP=0.431.pth" "https://zenodo.org/records/3987831/files/Cnn14_mAP%3D0.431.pth?download=1"
[ -s class_labels_indices.csv ] || curl -L --fail -o class_labels_indices.csv "https://raw.githubusercontent.com/qiuqiangkong/audioset_tagging_cnn/master/metadata/class_labels_indices.csv"
ls -la "$R/panns_data"
PYTHONPATH="$R/overlay" $PY - <<'PY'
from huggingface_hub import snapshot_download
p = snapshot_download("laion/clap-htsat-unfused", allow_patterns=["*.json", "*.safetensors", "*.txt"])
print("CLAP at", p)
PY
du -sh "$R"/*
date
echo INSTALL-DONE
