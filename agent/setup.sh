#!/usr/bin/env bash
# Run this once on a fresh VM after cloning the repo.
# Installs Docker (if missing), walks through env configuration,
# and starts one or both agents as background containers.
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

# ── Helper: prompt user to edit a file and wait ───────────────────────────────
_edit_prompt() {
  local file="$1"; shift
  echo ""
  echo "  Required fields in $file:"
  for field in "$@"; do
    echo "    $field"
  done
  echo ""
  read -rp "  Open $file now? [Y/n] " yn
  case "$yn" in
    [nN]*) ;;
    *) "${EDITOR:-nano}" "$file" ;;
  esac
  read -rp "  Press Enter when $file is ready to continue..."
}

# ── Strategy 1 setup (.env / credentials.json) ───────────────────────────────
echo ""
echo "═══ Strategy 1: Momentum + Macro Alignment ═══════════════════════════════"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from template."
  _edit_prompt ".env" \
    "AGENT_PASSWORD  — choose a strong password" \
    "NTFY_TOPIC      — your ntfy.sh topic name (e.g. jatin-momentum-bot)" \
    "DRY_RUN=false   — flip when ready to go live"
else
  echo ".env already exists — skipping."
fi

if [ ! -f credentials.json ]; then
  echo '{"token":null,"agent_id":null}' > credentials.json
  echo "Created empty credentials.json — a new agent account registers on first start."
fi

# ── Strategy 2 setup (.env.swing / credentials.swing.json) ───────────────────
echo ""
echo "═══ Strategy 2: LLM-Guided Swing (optional) ══════════════════════════════"
read -rp "Set up the LLM Swing agent as a separate leaderboard entry? [y/N] " setup_swing
RUN_SWING=false

if [[ "$setup_swing" =~ ^[yY] ]]; then
  RUN_SWING=true

  if [ ! -f .env.swing ]; then
    if [ -f .env.swing.example ]; then
      cp .env.swing.example .env.swing
      echo "Created .env.swing from template."
    else
      echo "ERROR: .env.swing.example not found. Cannot create .env.swing."
      exit 1
    fi
    _edit_prompt ".env.swing" \
      "AGENT_NAME      — different from Strategy 1 (e.g. JatinSwingBot)" \
      "AGENT_EMAIL     — can use +tag trick: you+swing@gmail.com" \
      "AGENT_PASSWORD  — choose a strong password (can differ from S1)" \
      "GEMINI_API_KEY  — free at https://aistudio.google.com/apikey" \
      "FRED_API_KEY    — free at https://fredaccount.stlouisfed.org/apikeys" \
      "DRY_RUN=false   — flip when ready to go live"
  else
    echo ".env.swing already exists — skipping."
  fi

  if [ ! -f credentials.swing.json ]; then
    echo '{"token":null,"agent_id":null}' > credentials.swing.json
    echo "Created empty credentials.swing.json — a new agent account registers on first start."
  fi
fi

# ── Build and start ───────────────────────────────────────────────────────────
echo ""
echo "Building image and starting agent(s)..."

if [ "$RUN_SWING" = true ]; then
  docker compose --profile swing up -d --build
else
  docker compose up -d --build
fi

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Agent(s) running. Useful commands:                         ║"
echo "║                                                              ║"
echo "║  docker compose logs -f                 # all live logs     ║"
echo "║  docker compose logs -f agent           # Strategy 1 only  ║"
if [ "$RUN_SWING" = true ]; then
echo "║  docker compose logs -f agent-swing     # Strategy 2 only  ║"
echo "║                                                              ║"
echo "║  docker compose restart agent           # restart S1       ║"
echo "║  docker compose restart agent-swing     # restart S2       ║"
echo "║  docker compose --profile swing down    # stop both        ║"
else
echo "║                                                              ║"
echo "║  docker compose restart agent           # restart          ║"
echo "║  docker compose down                    # stop             ║"
fi
echo "╚══════════════════════════════════════════════════════════════╝"
