"""
AI-Trader personal agent — entry point.

Usage:
  python main.py               # live mode (reads DRY_RUN from .env)
  python main.py --dry-run     # force dry-run regardless of .env
  python main.py --scan-now    # run one scan immediately then exit
  python main.py --verbose     # show per-symbol gate debug logs
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

load_dotenv()

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

CREDENTIALS_PATH = Path(__file__).parent / "credentials.json"

CREDENTIALS_PATH = Path(__file__).parent / "credentials.json"


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
            active.append(cls())
            log.info("Strategy loaded: %s", name)
        else:
            log.warning("Unknown strategy '%s' — skipping", name)

    if not active:
        log.error("No valid strategies configured. Check ACTIVE_STRATEGIES in .env")
        sys.exit(1)

    return Scanner(client, active, notifier, config)


def main() -> None:
    parser = argparse.ArgumentParser(description="AI-Trader personal agent")
    parser.add_argument("--dry-run", action="store_true", help="Suppress all write API calls")
    parser.add_argument("--scan-now", action="store_true", help="Run one scan then exit")
    parser.add_argument("--verbose", action="store_true", help="Show per-symbol gate debug logs")
    args = parser.parse_args()

    config = _load_config(force_dry_run=args.dry_run)

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
