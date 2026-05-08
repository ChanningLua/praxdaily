#!/bin/sh
# Trigger praxdaily's native pipeline once via the running container's
# HTTP API. Used by the docker-compose scheduler sidecar — see
# docker-compose.yml.
#
# We hit the running container instead of `docker exec`-ing because:
#   1. The scheduler sidecar doesn't have docker socket access by
#      design (smaller blast radius if compromised).
#   2. The dashboard exposes /api/cron/run-once for exactly this case.

set -e

URL="${PRAXDAILY_RUN_URL:-http://praxdaily:7878/api/cron/run-once}"

curl -fsS -X POST "$URL" \
  -H 'Content-Type: application/json' \
  -d '{}' \
  --max-time 300 \
  || echo "[$(date -Iseconds)] run-now request failed"
