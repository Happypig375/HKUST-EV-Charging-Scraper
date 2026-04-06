#!/usr/bin/env bash
# Runs ONCE on CEZ083 to set up the bare git repo, post-receive hook,
# working directory, and systemd user service.
# Pipe it from Windows: ssh -p 830 user@143.89.22.207 "bash -s" < scripts/server_init.sh
set -euo pipefail

WORKDIR="$HOME/hkust-ev-collector"
BAREREPO="$HOME/hkust-ev-collector.git"
SERVICE_DIR="$HOME/.config/systemd/user"

echo "==> CEZ083 server init starting at $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
echo "    HOME=$HOME  WORKDIR=$WORKDIR  BAREREPO=$BAREREPO"

echo "==> Creating working directory"
mkdir -p "$WORKDIR"

echo "==> Creating bare git repository"
if [[ -d "$BAREREPO/HEAD" || -f "$BAREREPO/HEAD" ]]; then
  echo "    Bare repo already exists, skipping git init"
else
  git init --bare "$BAREREPO"
fi

echo "==> Installing post-receive hook"
cat > "$BAREREPO/hooks/post-receive" << 'HOOK'
#!/usr/bin/env bash
set -u
WORK_TREE="$HOME/hkust-ev-collector"
BARE_REPO="$HOME/hkust-ev-collector.git"
SERVICE_DIR="$HOME/.config/systemd/user"

echo "--- Checking out to $WORK_TREE"
git --work-tree="$WORK_TREE" --git-dir="$BARE_REPO" checkout -f

cd "$WORK_TREE"

echo "--- Installing Python dependencies"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q

echo "--- Installing Playwright Chromium"
python -m playwright install chromium 2>&1 | tail -5

echo "--- Creating .env if missing"
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "    Created .env from .env.example"
  echo "    IMPORTANT: edit $WORK_TREE/.env with real API_KEY and portal credentials"
fi

mkdir -p logs

echo "--- Installing systemd user service"
mkdir -p "$SERVICE_DIR"
cp ev-collector.service "$SERVICE_DIR/ev-collector.service"
systemctl --user daemon-reload
systemctl --user enable ev-collector 2>/dev/null || true
systemctl --user restart ev-collector 2>/dev/null \
  || echo "  >> Start manually: systemctl --user start ev-collector"

echo "Deployment complete at $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
HOOK
chmod +x "$BAREREPO/hooks/post-receive"

echo "==> Enabling systemd user linger (keeps service alive after logout)"
loginctl enable-linger "$USER" 2>/dev/null \
  || echo "    Linger may already be enabled or require a privilege; check with: loginctl show-user $USER"

mkdir -p "$SERVICE_DIR"

echo ""
echo "Server initialised successfully."
echo "  Bare repo : $BAREREPO"
echo "  Work dir  : $WORKDIR"
echo ""
echo "Next steps (back on your Windows machine):"
echo "  1. scripts/setup_git_remote.ps1 will set git remote and push"
echo "  2. Edit /home/user/hkust-ev-collector/.env on the server with real credentials"
echo "  3. systemctl --user start ev-collector   (or it starts automatically on push)"
