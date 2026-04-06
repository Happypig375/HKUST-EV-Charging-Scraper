#!/usr/bin/env bash
set -u

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

if [[ -f .venv/bin/activate ]]; then
  source .venv/bin/activate
fi

mkdir -p logs

while true; do
  timestamp="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  echo "$timestamp starting collector" >> logs/restarter.log
  python3 collector.py
  exit_code=$?
  timestamp="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  echo "$timestamp collector exited with code $exit_code" >> logs/restarter.log
  sleep 5
done
