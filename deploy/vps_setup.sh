#!/usr/bin/env bash
# One-shot VPS setup for my_poly_bots.
#
# Usage on a fresh Ubuntu 22.04 / 24.04 VPS:
#   bash deploy/vps_setup.sh
#
# This is idempotent — safe to re-run after code updates.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Shmuel18/my_poly_bots.git}"
REPO_DIR="${REPO_DIR:-$HOME/my_poly_bots}"
BRANCH="${BRANCH:-main}"
VENV_DIR="$REPO_DIR/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"

log() { printf '\033[36m[setup]\033[0m %s\n' "$*"; }
err() { printf '\033[31m[error]\033[0m %s\n' "$*" >&2; }

# 1. OS sanity
if ! command -v apt >/dev/null 2>&1; then
  err "This script expects a Debian/Ubuntu VPS (apt not found)."
  exit 1
fi

# 2. System deps
log "Installing system packages (python3-venv, git, tmux)…"
sudo apt update -y
sudo apt install -y python3 python3-venv python3-pip git tmux

# 3. Clone or update repo
if [ -d "$REPO_DIR/.git" ]; then
  log "Repo exists at $REPO_DIR — pulling latest on branch $BRANCH…"
  cd "$REPO_DIR"
  git fetch --all --prune
  git checkout "$BRANCH"
  git pull --ff-only origin "$BRANCH"
else
  log "Cloning $REPO_URL → $REPO_DIR"
  git clone "$REPO_URL" "$REPO_DIR"
  cd "$REPO_DIR"
  git checkout "$BRANCH"
fi

# 4. venv + deps
if [ ! -d "$VENV_DIR" ]; then
  log "Creating virtualenv at $VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
log "Upgrading pip…"
pip install --upgrade pip >/dev/null
log "Installing Python requirements (this can take a minute)…"
pip install -r requirements.txt

# 5. .env sanity
if [ ! -f "config/.env" ]; then
  err "config/.env missing — copy your keys to $REPO_DIR/config/.env before running the bot."
  err "Template: cp config/.env.example config/.env && nano config/.env"
  exit 2
fi
log "config/.env present. Required keys check:"
for k in POLYMARKET_API_KEY POLYMARKET_API_SECRET POLYMARKET_API_PASSPHRASE \
         POLYMARKET_PRIVATE_KEY POLYMARKET_FUNDER_ADDRESS GEMINI_API_KEY; do
  if grep -q "^${k}=..*" config/.env 2>/dev/null; then
    printf '  \033[32m✓\033[0m %s set\n' "$k"
  else
    printf '  \033[33m⚠\033[0m %s missing or empty — bot may not work as expected\n' "$k"
  fi
done

log "Setup complete."
cat <<EOF

Next steps:
  1. (Optional) Install systemd service so the bot auto-starts on reboot:
       sudo bash deploy/install_service.sh

  2. Or run interactively in tmux (Ctrl+B then D to detach):
       tmux new -s polybot
       source .venv/bin/activate
       python run_calendar_bot.py --env config/.env --live --use-llm

  3. Watch the log lines in order:
       💰 Balance: \$XX.XX USDC    → wallet connected
       🤖 LLM Agent enabled       → Gemini key valid
       📦 Discovery: Markets 0-100 → scanning started
EOF
