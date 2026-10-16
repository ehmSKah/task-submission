#!/usr/bin/env bash
# Waits until Floci answers its health endpoint.
set -euo pipefail
url="${1:-http://localhost:4566}/_floci/health"
for _ in $(seq 1 60); do
  if curl -fsS "$url" >/dev/null 2>&1; then echo "Floci is up at ${url%/_floci/health}"; exit 0; fi
  sleep 1
done
echo "Floci did not become healthy at $url" >&2
exit 1
