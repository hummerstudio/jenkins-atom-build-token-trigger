#!/usr/bin/env bash
# Build the atom, then hand the actual testing to tools/smoke.py: a real HTTP server plus assertions on the request
# the atom sends and on what it reports for every answer (201/200/302/303/403/404/500, timeouts, bad input).
set -euo pipefail
cd "$(dirname "$0")/.."
./build.sh >/dev/null
python3 tools/smoke.py
