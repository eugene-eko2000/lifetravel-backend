#!/usr/bin/env bash
set -euo pipefail

export PORT="${PORT:-8081}"
export HEADLESS="${HEADLESS:-false}"

python3 "$(dirname "$0")/src/main.py"
