#!/bin/bash
# Runs the streaming classifier against the bridge's BMAA datagrams. Started by run_pipeline.ps1 (WSL).
# Usage: run_classifier.sh [port] [python]      python defaults to python3 on PATH
# Environment: BMA_DATA_DIR (default: the parent of this script's directory when that directory is named "app",
# else ./data).
# Detections: one JSON object per line on stdout -> <data-dir>/logs/detections.jsonl; log lines -> logs/classifier.log.
PORT=${1:-48200}
PY=${2:-python3}
APP="$(cd "$(dirname "$0")" && pwd)"
if [ -z "$BMA_DATA_DIR" ]; then
  if [ "$(basename "$APP")" = app ]; then BMA_DATA_DIR="$(dirname "$APP")"; else BMA_DATA_DIR=./data; fi
fi
mkdir -p "$BMA_DATA_DIR/logs" || exit 2
LOGS="$(cd "$BMA_DATA_DIR/logs" && pwd)"
EXTRA=()
# optional county-level location/date species filter for BirdNET (see classify/README.md); off when the file is absent
[ -f "$APP/geo_county.json" ] && EXTRA+=(--geo-config "$APP/geo_county.json")
cd "$APP" || exit 2
# a classifier orphaned by a previous supervisor (logoff, crash) would still hold the UDP port
pkill -f "bma_classify.py --udp 0.0.0.0:$PORT" 2>/dev/null
sleep 1
exec "$PY" bma_classify.py --udp "0.0.0.0:$PORT" --slot 0 --min-conf 0.25 "${EXTRA[@]}" \
  >> "$LOGS/detections.jsonl" 2>> "$LOGS/classifier.log"
