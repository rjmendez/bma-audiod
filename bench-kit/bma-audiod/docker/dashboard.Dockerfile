# syntax=docker/dockerfile:1
# BMA dashboard (ui/bma_ui.py): standard library only, a read-only view of the data directory. Build context: bench-kit/bma-audiod.
#   docker build -f docker/dashboard.Dockerfile -t bma-dashboard .
# The dashboard has a login (ui/AUTH.md): put dashboard_token.txt in the data dir (the supervisor-made file works), or it refuses to start on
# 0.0.0.0. Optional HTTPS: <data>/tls/dashboard_cert.pem + dashboard_key.pem. Keep the published port on loopback (docker-compose.yml)
# unless the network is trusted.
FROM python:3.12.15-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BMA_DATA_DIR=/data
WORKDIR /app/ui
COPY ui/bma_ui.py ui/auth.py ui/ws.py ui/passkeys.py ui/review.py ui/taxa.json analysis/labels.py analysis/events.py analysis/review_queue.py /app/ui/
COPY analysis/artifact_gate.py /app/ui/
COPY ui/static/ /app/ui/static/
COPY ui-next/static/ /app/ui/next/
COPY docker/entrypoint.sh docker/healthcheck.py /usr/local/lib/bma/
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin bma \
 && mkdir -p /data \
 && chmod 0555 /usr/local/lib/bma/entrypoint.sh
USER bma
VOLUME ["/data"]
EXPOSE 48500/tcp
ENTRYPOINT ["/usr/local/lib/bma/entrypoint.sh"]
CMD ["dashboard"]
