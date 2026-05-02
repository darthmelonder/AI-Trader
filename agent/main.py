"""
AI-Trader personal agent — entry point.

Usage:
  python main.py                        # momentum_macro agent (reads .env)
  python main.py --env .env.swing       # llm_swing agent (separate identity + credentials)
  python main.py --dry-run              # force dry-run regardless of .env
  python main.py --scan-now             # run one scan immediately then exit
  python main.py --verbose              # show per-symbol gate debug logs

Running two strategies as separate leaderboard agents:
  Terminal 1:  python main.py                   # JatinMomentumBot
  Terminal 2:  python main.py --env .env.swing  # JatinSwingBot
Each agent registers independently, holds its own virtual cash, and appears
separately on the platform leaderboard so P&L is fully attributable.
"""
import argparse
import json
import logging
import logging.handlers
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# Parsed early so --env takes effect before any os.environ reads.
_pre = argparse.ArgumentParser(add_help=False)
_pre.add_argument("--env", default=".env")
_pre_args, _ = _pre.parse_known_args()
load_dotenv(_pre_args.env)

LOG_FMT = "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _setup_logging(level: str, log_file: str) -> None:
    """Configure root logger with stdout handler and optional rotating file handler."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    fmt = logging.Formatter(LOG_FMT, datefmt=LOG_DATEFMT)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(fmt)
    root.addHandler(stdout_handler)

    if log_file:
        # 5 MB per file, keep last 5 files (~25 MB total)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
        logging.getLogger(__name__).info("Logging to file: %s", log_file)


log = logging.getLogger(__name__)

# Credentials file is derived from the env file so each agent instance
# maintains its own auth token.
#   .env          → credentials.json
#   .env.swing    → credentials.swing.json
_env_name = Path(_pre_args.env).name
if _env_name == ".env":
    _creds_name = "credentials.json"
else:
    _tag = _env_name.removeprefix(".env.")  # ".env.swing" → "swing"
    _creds_name = f"credentials.{_tag}.json"
CREDENTIALS_PATH = Path(__file__).parent / _creds_name


def _load_config(force_dry_run: bool) -> dict:
    return {
        "agent_name": os.environ["AGENT_NAME"],
        "agent_email": os.environ["AGENT_EMAIL"],
        "agent_password": os.environ["AGENT_PASSWORD"],
        "base_url": os.environ.get("BASE_URL", "https://ai4trade.ai/api"),
        "ntfy_topic": os.environ.get("NTFY_TOPIC", ""),
        "discord_webhook_url": os.environ.get("DISCORD_WEBHOOK_URL", ""),
        "stop_loss_pct": float(os.environ.get("STOP_LOSS_PCT", 8.0)),
        "max_positions": int(os.environ.get("MAX_POSITIONS", 6)),
        "position_size_pct": float(os.environ.get("POSITION_SIZE_PCT", 20.0)),
        "max_position_age_days": int(os.environ.get("MAX_POSITION_AGE_DAYS", 90)),
        "scan_interval_seconds": int(os.environ.get("SCAN_INTERVAL_SECONDS", 3600)),
        "heartbeat_interval_seconds": int(os.environ.get("HEARTBEAT_INTERVAL_SECONDS", 60)),
        "dry_run": force_dry_run or os.environ.get("DRY_RUN", "true").lower() == "true",
        "active_strategies": [
            s.strip()
            for s in os.environ.get("ACTIVE_STRATEGIES", "momentum_macro").split(",")
            if s.strip()
        ],
        "log_level": os.environ.get("LOG_LEVEL", "INFO").upper(),
        "log_file": os.environ.get("LOG_FILE", ""),
        # ── external data API keys ─────────────────────────────────────────
        "alpha_vantage_api_key": os.environ.get("ALPHA_VANTAGE_API_KEY", ""),
        "gemini_api_key": os.environ.get("GEMINI_API_KEY", ""),
        "fred_api_key": os.environ.get("FRED_API_KEY", ""),
        # ── Strategy 2 (llm_swing) tuning ─────────────────────────────────
        "s2_min_llm_confidence": float(os.environ.get("S2_MIN_LLM_CONFIDENCE", 0.70)),
        "s2_max_positions": int(os.environ.get("S2_MAX_POSITIONS", 3)),
        "s2_position_size_pct": float(os.environ.get("S2_POSITION_SIZE_PCT", 15.0)),
        "s2_max_hold_days": int(os.environ.get("S2_MAX_HOLD_DAYS", 30)),
        "s2_llm_cache_ttl_hours": float(os.environ.get("S2_LLM_CACHE_TTL_HOURS", 24.0)),
        "s2_earnings_blackout_days": int(os.environ.get("S2_EARNINGS_BLACKOUT_DAYS", 5)),
        "s2_stop_loss_pct": float(os.environ.get("S2_STOP_LOSS_PCT", 9.0)),
        "s2_target_return_pct": float(os.environ.get("S2_TARGET_RETURN_PCT", 12.0)),
    }


def _load_credentials() -> tuple[str | None, int | None]:
    """Return (token, agent_id) from credentials.json, or (None, None) if absent/incomplete."""
    if not CREDENTIALS_PATH.exists():
        return None, None
    try:
        creds = json.loads(CREDENTIALS_PATH.read_text())
        token = creds.get("token")
        agent_id = creds.get("agent_id")
        if token and agent_id:
            return token, int(agent_id)
    except (json.JSONDecodeError, ValueError):
        pass
    return None, None


def _bootstrap_client(config: dict):
    from client import AI4TradeClient

    client = AI4TradeClient(config["base_url"], dry_run=config["dry_run"])
    token, agent_id = _load_credentials()

    if token and agent_id:
        client.set_token(token)
        config["agent_id"] = agent_id

        if config["dry_run"]:
            log.info("DRY RUN — loaded credentials (agent_id=%d), skipping verification", agent_id)
        else:
            try:
                me = client.me()
                log.info("Authenticated as '%s' (id=%d)", me.get("name"), me.get("id"))
            except Exception:
                log.info("Token invalid — re-logging in")
                _reauth(client, config)
    else:
        if config["dry_run"]:
            log.info("DRY RUN — no credentials found, skipping registration")
            config["agent_id"] = 0
        else:
            log.info("No credentials found — registering new agent '%s'", config["agent_name"])
            resp = client.register(
                config["agent_name"], config["agent_email"], config["agent_password"]
            )
            _save_credentials(resp["agent_id"], resp["token"])
            client.set_token(resp["token"])
            config["agent_id"] = resp["agent_id"]
            log.info(
                "Registered! agent_id=%d — credentials saved to %s",
                resp["agent_id"],
                CREDENTIALS_PATH,
            )

    return client


def _reauth(client, config: dict) -> None:
    """Login to refresh a stale/rotated token. Updates client session and credentials in-place."""
    resp = client.login(config["agent_name"], config["agent_password"])
    _save_credentials(resp["agent_id"], resp["token"])
    client.set_token(resp["token"])
    config["agent_id"] = resp["agent_id"]
    log.info("Re-authenticated (agent_id=%d)", resp["agent_id"])


def _save_credentials(agent_id: int, token: str) -> None:
    CREDENTIALS_PATH.write_text(json.dumps({
        "agent_id": agent_id,
        "token": token,
        "registered_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2))


def _process_heartbeat(hb: dict) -> None:
    messages = hb.get("messages") or []
    if messages:
        log.info("Heartbeat: %d message(s)", len(messages))
        for msg in messages:
            log.info("  [%s] %s", msg.get("type"), msg.get("content"))
    tasks = hb.get("tasks") or []
    if tasks:
        log.info("Heartbeat: %d task(s) pending", len(tasks))


def _build_scanner(client, config, notifier):
    from scanner import Scanner
    from strategies import STRATEGY_REGISTRY

    active = []
    for name in config["active_strategies"]:
        cls = STRATEGY_REGISTRY.get(name)
        if cls:
            try:
                instance = cls(config) if getattr(cls, "NEEDS_CONFIG", False) else cls()
                active.append(instance)
                log.info("Strategy loaded: %s", name)
            except Exception as exc:
                log.error("Failed to instantiate strategy '%s': %s", name, exc)
        else:
            log.warning("Unknown strategy '%s' — skipping", name)

    if not active:
        log.error("No valid strategies configured. Check ACTIVE_STRATEGIES in .env")
        sys.exit(1)

    return Scanner(client, active, notifier, config)


def main() -> None:
    parser = argparse.ArgumentParser(description="AI-Trader personal agent")
    parser.add_argument("--env", default=".env", help="Env file to load (default: .env)")
    parser.add_argument("--dry-run", action="store_true", help="Suppress all write API calls")
    parser.add_argument("--scan-now", action="store_true", help="Run one scan then exit")
    parser.add_argument("--force-scan", action="store_true", help="Run entry scan even when market is closed (testing)")
    parser.add_argument("--verbose", action="store_true", help="Show per-symbol gate debug logs")
    args = parser.parse_args()

    config = _load_config(force_dry_run=args.dry_run)
    config["force_scan"] = args.force_scan

    level = "DEBUG" if args.verbose else config["log_level"]
    _setup_logging(level, config["log_file"])

    if config["dry_run"]:
        log.info("═══ DRY RUN MODE — no trades will be published ═══")

    client = _bootstrap_client(config)

    from notifier import Notifier
    notifier = Notifier(
        ntfy_topic=config["ntfy_topic"],
        discord_webhook=config["discord_webhook_url"],
        dry_run=config["dry_run"],
    )

    scanner = _build_scanner(client, config, notifier)

    if args.scan_now:
        scanner.run_scan()
        return

    # ── main loop ─────────────────────────────────────────────────────────
    log.info("Agent started. Heartbeat every %ds, scan every %ds.",
             config["heartbeat_interval_seconds"], config["scan_interval_seconds"])

    last_heartbeat = 0.0
    last_scan = 0.0

    while True:
        now = time.time()

        if now - last_heartbeat >= config["heartbeat_interval_seconds"]:
            try:
                if config["dry_run"]:
                    log.debug("DRY RUN — heartbeat skipped")
                else:
                    hb = client.heartbeat(config["agent_id"])
                    _process_heartbeat(hb)
            except Exception as exc:
                if "401" in str(exc):
                    log.warning("Heartbeat 401 — token stale, re-authenticating")
                    try:
                        _reauth(client, config)
                    except Exception as auth_exc:
                        log.error("Re-auth failed: %s", auth_exc)
                else:
                    log.warning("Heartbeat error: %s", exc)
            last_heartbeat = now

        if now - last_scan >= config["scan_interval_seconds"]:
            try:
                scanner.run_scan()
            except Exception as exc:
                log.error("Scan error: %s", exc, exc_info=True)
            last_scan = now

        time.sleep(5)


if __name__ == "__main__":
    main()
