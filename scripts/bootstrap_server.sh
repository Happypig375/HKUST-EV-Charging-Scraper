#!/usr/bin/env bash
# Manual bootstrap fallback (if git-push deploy isn't being used).
# The preferred path is: Windows -> setup_git_remote.ps1 -> git push.
# Run on the server from the cloned/checked-out project directory.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../" && pwd)"
SERVICE_DIR="$HOME/.config/systemd/user"

cd "$PROJECT_DIR"
echo "==> Bootstrapping from $PROJECT_DIR"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q

mkdir -p logs

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example. Edit .env with real credentials before starting."
fi

loginctl enable-linger "$USER" 2>/dev/null || true

mkdir -p "$SERVICE_DIR"
cp ev-collector.service "$SERVICE_DIR/ev-collector.service"
systemctl --user daemon-reload
systemctl --user enable ev-collector
systemctl --user restart ev-collector
systemctl --user status ev-collector --no-pager
