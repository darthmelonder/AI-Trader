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

# ── Strategy 3 setup (.env.mean_reversion / credentials.mean_reversion.json) ──
echo ""
echo "═══ Strategy 3: LLM-Guided Mean Reversion (optional) ═════════════════════"
read -rp "Set up the Mean Reversion agent as a separate leaderboard entry? [y/N] " setup_mr
RUN_MR=false

if [[ "$setup_mr" =~ ^[yY] ]]; then
  RUN_MR=true

  if [ ! -f .env.mean_reversion ]; then
    if [ -f .env.mean_reversion.example ]; then
      cp .env.mean_reversion.example .env.mean_reversion
      echo "Created .env.mean_reversion from template."
    else
      echo "ERROR: .env.mean_reversion.example not found."
      exit 1
    fi
    _edit_prompt ".env.mean_reversion" \
      "AGENT_NAME      — different from S1 and S2 (e.g. JatinMeanRevBot)" \
      "AGENT_EMAIL     — can use +tag trick: you+meanrev@gmail.com" \
      "AGENT_PASSWORD  — choose a strong password" \
      "GEMINI_API_KEY  — same key as .env.swing is fine" \
      "FRED_API_KEY    — same key as .env.swing is fine" \
      "DRY_RUN=false   — flip when ready to go live"
  else
    echo ".env.mean_reversion already exists — skipping."
  fi

  if [ ! -f credentials.mean_reversion.json ]; then
    echo '{"token":null,"agent_id":null}' > credentials.mean_reversion.json
    echo "Created empty credentials.mean_reversion.json."
  fi
fi

# ── Build and start ───────────────────────────────────────────────────────────
echo ""
echo "Building image and starting agent(s)..."

PROFILES=""
[ "$RUN_SWING" = true ] && PROFILES="$PROFILES --profile swing"
[ "$RUN_MR"    = true ] && PROFILES="$PROFILES --profile mean-reversion"

# shellcheck disable=SC2086
docker compose $PROFILES up -d --build

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Agent(s) running. Useful commands:                         ║"
echo "║                                                              ║"
echo "║  docker compose logs -f                       # all logs    ║"
echo "║  docker compose logs -f agent                 # S1 only    ║"
if [ "$RUN_SWING" = true ]; then
echo "║  docker compose logs -f agent-swing           # S2 only    ║"
fi
if [ "$RUN_MR" = true ]; then
echo "║  docker compose logs -f agent-mean-reversion  # S3 only    ║"
fi
echo "║                                                              ║"
echo "║  docker compose restart agent                 # restart S1 ║"
if [ "$RUN_SWING" = true ]; then
echo "║  docker compose restart agent-swing           # restart S2 ║"
fi
if [ "$RUN_MR" = true ]; then
echo "║  docker compose restart agent-mean-reversion  # restart S3 ║"
fi
echo "║                                                              ║"
STOP_CMD="docker compose"
[ "$RUN_SWING" = true ] && STOP_CMD="$STOP_CMD --profile swing"
[ "$RUN_MR"    = true ] && STOP_CMD="$STOP_CMD --profile mean-reversion"
echo "║  $STOP_CMD down"
echo "╚══════════════════════════════════════════════════════════════╝"
