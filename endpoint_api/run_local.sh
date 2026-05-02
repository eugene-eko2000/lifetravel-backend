#!/usr/bin/env bash
set -euo pipefail

export PORT="${PORT:-8080}"

python3 "$(dirname "$0")/src/main.py"
