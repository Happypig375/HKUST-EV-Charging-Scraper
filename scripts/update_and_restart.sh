#!/usr/bin/env bash
# Run on the server to pull the latest commit and restart the service.
# Normally the post-receive hook does this automatically on git push.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../" && pwd)"
cd "$PROJECT_DIR"

git pull --ff-only
source .venv/bin/activate
pip install -r requirements.txt -q
systemctl --user daemon-reload
systemctl --user restart ev-collector
systemctl --user status ev-collector --no-pager
