#!/bin/bash
# Runs the BMA analysis host. Started by run_pipeline.ps1 (WSL) or by the container entrypoint.
# Usage: run_analyzer.sh [port] [python] [beam-shift] [clip-audio] [host.py args...]      python defaults to python3 on PATH; beam-shift (optional, 8
#   comma-separated integers, host.py --beam-shift) is passed on only when non-empty; clip-audio: "1" passes --clip-audio, anything else nothing; every further argument is appended to the host.py command line as is
# Environment:
#   BMA_DATA_DIR   root of analysis/, history/, logs/ (default: the parent of this script's directory when that
#                  directory is named "app" (the pinned live copy), else ./data)
#   BMA_MODEL_DIR  model caches and weights (default: <data-dir>/models)
# Outputs: <data-dir>/analysis/*.jsonl (metadata, embeddings; audio only with the 4th argument 1 (--clip-audio, wildlife clips, 24 h, confirmed-voice clips purged; see clips.py), else visual-only clips under <data-dir>/analysis/clips), <data-dir>/logs/analyzer.log.
PORT=${1:-48300}
PY=${2:-python3}
APP="$(cd "$(dirname "$0")" && pwd)"
if [ -z "$BMA_DATA_DIR" ]; then
  if [ "$(basename "$APP")" = app ]; then BMA_DATA_DIR="$(dirname "$APP")"; else BMA_DATA_DIR=./data; fi
fi
mkdir -p "$BMA_DATA_DIR/analysis" "$BMA_DATA_DIR/logs" || exit 2
BMA_DATA_DIR="$(cd "$BMA_DATA_DIR" && pwd)"
MODELS="${BMA_MODEL_DIR:-$BMA_DATA_DIR/models}"
mkdir -p "$MODELS/panns_data" "$MODELS/hf" "$MODELS/tmp" || exit 2
MODELS="$(cd "$MODELS" && pwd)"
export BMA_DATA_DIR BMA_MODEL_DIR="$MODELS"
# panns_inference reads its labels and weights from <home>/panns_data (Path.home(), its own fixed location): make the
# model dir the home of this process, so <model-dir>/panns_data is found there and no user's real home is touched
export HOME="$MODELS"
[ -d "$MODELS/overlay" ] && export PYTHONPATH="$MODELS/overlay${PYTHONPATH:+:$PYTHONPATH}"
export HF_HOME="$MODELS/hf" HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TMPDIR="$MODELS/tmp"
# an analyzer orphaned by a previous supervisor would still hold the UDP port
pkill -f "host.py --listen 0.0.0.0:$PORT" 2>/dev/null
sleep 1
# optional: mask of the unit's always-on electronic tonal lines (analysis/learn_tonal_mask.py) and the county-level
# location/date species filter for the birds plugin (classify/README.md); each is off when its file is absent.
# mixscore.json (any content, or {"every": N}) switches on the opt-in mixscore plugin (host.py --mixscore; analysis/mixscore.py):
# BirdNET on the 8-beam mean next to the per-beam birds plugin, one more BirdNET model in memory (about 750 MB). "every" is
# --mixscore-every (default 4); delete the file to turn the plugin off.
EXTRA=()
[ -f "$APP/tonal_mask.json" ] && EXTRA+=(--tonal-mask "$APP/tonal_mask.json")
[ -f "$APP/geo_county.json" ] && EXTRA+=(--geo-config "$APP/geo_county.json")
[ -n "$3" ] && EXTRA+=(--beam-shift "$3")
[ "$4" = 1 ] && EXTRA+=(--clip-audio)
[ $# -gt 4 ] && EXTRA+=("${@:5}")
if [ -f "$APP/mixscore.json" ]; then
  EXTRA+=(--mixscore)
  MIX_EVERY="$(tr -d ' \r\n' < "$APP/mixscore.json" | sed -n 's/.*"every":\([0-9][0-9]*\).*/\1/p')"
  [ -n "$MIX_EVERY" ] && EXTRA+=(--mixscore-every "$MIX_EVERY")
fi
cd "$APP" || exit 2
exec "$PY" host.py --listen "0.0.0.0:$PORT" --out "$BMA_DATA_DIR/analysis" --panns-dir "$MODELS/panns_data" "${EXTRA[@]}" \
  >> "$BMA_DATA_DIR/logs/analyzer.log" 2>&1
