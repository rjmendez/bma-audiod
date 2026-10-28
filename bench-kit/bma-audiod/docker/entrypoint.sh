#!/bin/sh
# Container entrypoint. Roles: analyzer | classifier | dashboard | models. Every path comes from BMA_DATA_DIR / BMA_MODEL_DIR
# (set by the images to the two mounted volumes); extra arguments after the role go to the program.
set -eu
role="${1:-analyzer}"
[ "$#" -gt 0 ] && shift
data="${BMA_DATA_DIR:?BMA_DATA_DIR is not set}"

case "$role" in
analyzer)
    mkdir -p "$data/analysis" "$data/history" "$data/logs"
    # ~/panns_data in the image links here; panns_inference makedirs() it, which fails on a dangling link
    mkdir -p "${BMA_MODEL_DIR:?BMA_MODEL_DIR is not set}/panns_data"
    # optional tonal mask of the unit's own electronic lines: a mask under <data>/config wins over the one in the image
    mask=/app/tonal_mask.json
    [ -f "$data/config/tonal_mask.json" ] && mask="$data/config/tonal_mask.json"
    exec python /app/host.py --listen 0.0.0.0:48300 --out "$data/analysis" --history-dir "$data/history" \
        --panns-dir "${BMA_MODEL_DIR:?BMA_MODEL_DIR is not set}/panns_data" --tonal-mask "$mask" "$@"
    ;;
classifier)
    mkdir -p "$data/logs"
    # optional county-level BirdNET location filter: file present = on, absent = off (classify/README.md)
    if [ -f "$data/config/geo_county.json" ]; then
        set -- --geo-config "$data/config/geo_county.json" "$@"
    fi
    # detections: JSON lines on stdout -> logs/detections.jsonl; log lines -> logs/classifier.log
    exec python /app/bma_classify.py --udp 0.0.0.0:48200 --slot 0 --min-conf 0.25 "$@" \
        >> "$data/logs/detections.jsonl" 2>> "$data/logs/classifier.log"
    ;;
dashboard)
    # read-only view of <data>; the board panel needs BMA_BOARD as an IPv4 address and the token file mounted at
    # /run/secrets/dspd_token, otherwise the panel is off (--no-board)
    board=""
    case "${BMA_BOARD:-}" in
        "") echo "BMA_BOARD not set: board panel off" >&2 ;;
        *[!0-9.]*) echo "BMA_BOARD is not an IPv4 address: board panel off" >&2 ;;
        *) board="${BMA_BOARD}" ;;
    esac
    if [ -n "$board" ]; then
        # the compose file mounts BMA_DSPD_TOKEN_FILE here (BMA_TOKEN_MOUNT only moves it for the entrypoint test)
        tok="${BMA_TOKEN_MOUNT:-/run/secrets/dspd_token}"
        if [ ! -r "$tok" ]; then
            echo "dspd token file $tok is not readable by uid $(id -u): make the host file readable by that uid (chown/chmod); the board panel runs without a token" >&2
        elif [ ! -s "$tok" ]; then
            echo "dspd token file $tok is empty (BMA_DSPD_TOKEN_FILE unset?): the board panel runs without a token" >&2
        fi
        set -- --board-url "http://$board:8470" --token-file "$tok" "$@"
    else
        set -- --no-board "$@"
    fi
    # HTTPS: <data>/tls/dashboard_cert.pem + dashboard_key.pem (the same layout pc/setup_tls.ps1 makes from a Tailscale certificate; made and renewed on the
    # host, the container never runs tailscale) turn it on. Only a complete pair does; a lone file is named on stderr. The login token is read by the
    # dashboard itself from <data>/dashboard_token.txt (ui/AUTH.md).
    tlsd="$data/tls"
    if [ -s "$tlsd/dashboard_cert.pem" ] && [ -s "$tlsd/dashboard_key.pem" ]; then
        set -- --tls-cert "$tlsd/dashboard_cert.pem" --tls-key "$tlsd/dashboard_key.pem" "$@"
    elif [ -e "$tlsd/dashboard_cert.pem" ] || [ -e "$tlsd/dashboard_key.pem" ]; then
        echo "TLS needs both $tlsd/dashboard_cert.pem and $tlsd/dashboard_key.pem (readable and non-empty): serving plain HTTP" >&2
    fi
    exec python /app/ui/bma_ui.py --bind 0.0.0.0 --port 48500 --allow-ip-hosts \
        --pipeline-root "$data" --reports-dir "$data/reports" --rollup-dir "$data/reports/ui" "$@"
    ;;
models)
    HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 exec python /usr/local/lib/bma/fetch_models.py "${BMA_MODEL_DIR:?BMA_MODEL_DIR is not set}"
    ;;
*)
    exec "$role" "$@"
    ;;
esac
