#!/usr/bin/env bash
# Run this once on a fresh VM after cloning the repo.
# It installs Docker (if missing), creates .env from the example,
# and starts the agent in the background.
set -e

AGENT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$AGENT_DIR"

# ── Docker check ──────────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
  echo "Installing Docker..."
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "$USER"
  echo "Docker installed. You may need to log out and back in for group changes."
fi

# ── .env setup ────────────────────────────────────────────────────────────────
if [ ! -f .env ]; then
  cp .env.example .env
  echo ""
  echo "╔══════════════════════════════════════════════════════╗"
  echo "║  .env created from template.                        ║"
  echo "║  Edit it now before starting:                       ║"
  echo "║                                                      ║"
  echo "║    nano .env                                         ║"
  echo "║                                                      ║"
  echo "║  Required fields:                                    ║"
  echo "║    AGENT_PASSWORD   — choose a strong password      ║"
  echo "║    NTFY_TOPIC       — your ntfy.sh topic name       ║"
  echo "║    DRY_RUN=false    — flip when ready to go live    ║"
  echo "╚══════════════════════════════════════════════════════╝"
  echo ""
  read -rp "Press Enter after editing .env to continue, or Ctrl-C to stop..."
fi

# ── credentials.json ─────────────────────────────────────────────────────────
# Docker requires this file to exist on the host for the volume mount to work.
# If you are migrating an existing agent account from another machine, copy your
# credentials.json here BEFORE running this script (scp or paste the contents).
# Otherwise a fresh agent account will be registered on first start.
if [ ! -f credentials.json ]; then
  echo '{"token":null,"agent_id":null}' > credentials.json
  echo "Created empty credentials.json — a new agent account will be registered on first start."
fi

# ── Start ─────────────────────────────────────────────────────────────────────
echo "Building and starting agent..."
docker compose up -d --build

echo ""
echo "Agent is running. Useful commands:"
echo "  docker compose logs -f          # live logs"
echo "  docker compose restart agent    # restart after config change"
echo "  docker compose down             # stop"
