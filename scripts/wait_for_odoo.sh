#!/usr/bin/env bash
# Block until Odoo answers its health check, or fail after TIMEOUT seconds.
set -euo pipefail

URL="${ODOO_URL:-http://localhost:8069}/web/health"
TIMEOUT="${TIMEOUT:-90}"

for ((i = 0; i < TIMEOUT; i++)); do
  if curl -fs "$URL" >/dev/null 2>&1; then
    exit 0
  fi
  sleep 1
done
echo "Odoo did not become healthy at $URL within ${TIMEOUT}s" >&2
exit 1
