#!/usr/bin/env bash
# Checks the host tools the README lists, and names anything missing (ADR 0010).
# Usage: doctor.sh [floci|aws]. Docker runs Floci only, so AWS targets don't need it.
set -u
target=${1:-floci}
status=0

check() {
  local name=$1 min=$2 version=$3
  if [[ -z "$version" ]]; then
    echo "✗ $name: not found (need >= $min)"; status=1; return
  fi
  if [[ "$(printf '%s\n%s\n' "$min" "$version" | sort -V | head -1)" != "$min" ]]; then
    echo "✗ $name: $version found, need >= $min"; status=1; return
  fi
  echo "✓ $name $version"
}

ver() { "$@" 2>/dev/null | grep -Eo '[0-9]+(\.[0-9]+)+' | head -1; }

if [[ "$target" == floci ]]; then
  check docker 24.0 "$(ver docker version --format '{{.Client.Version}}')"
  check "docker compose" 2.20 "$(ver docker compose version --short)"
fi
check make 3.81 "$(ver make --version)"
check uv 0.8 "$(ver uv --version)"
check node 20.0 "$(ver node --version)"
check npm 10.0 "$(ver npm --version)"

if [[ "$target" == floci ]]; then
  if docker info >/dev/null 2>&1; then echo "✓ docker daemon reachable"; else
    echo "✗ docker daemon not reachable (is Docker running?)"; status=1; fi
fi

exit $status
