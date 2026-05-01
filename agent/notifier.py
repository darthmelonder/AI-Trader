import logging
from typing import Optional

import requests

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, ntfy_topic: str, discord_webhook: Optional[str] = None, dry_run: bool = False):
        self.ntfy_topic = ntfy_topic
        self.discord_webhook = discord_webhook or ""
        self.dry_run = dry_run

    # ── public ────────────────────────────────────────────────────────────

    def send_entry_alert(
        self,
        symbol: str,
        action: str,
        quantity: float,
        price: float,
        thesis: str,
        strategy_name: str,
    ) -> None:
        title = f"TRADE SIGNAL — {action.upper()} {symbol}"
        body = (
            f"Action: {action.upper()} {quantity:.0f} shares @ ~${price:.2f}\n"
            f"Strategy: {strategy_name}\n"
            f"\n{thesis}\n"
            f"\n→ Execute manually on your broker."
        )
        self._send(title, body, priority="high", tags=["chart_increasing", symbol])

    def send_exit_alert(
        self,
        symbol: str,
        quantity: float,
        price: float,
        entry_price: float,
        reason: str,
        thesis: str,
    ) -> None:
        pnl = (price - entry_price) * quantity
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        priority = "urgent" if reason == "stop_loss" else "high"
        title = f"EXIT SIGNAL — SELL {symbol} ({reason.replace('_', ' ')})"
        body = (
            f"Action: SELL {quantity:.0f} shares @ ~${price:.2f}\n"
            f"Entry: ${entry_price:.2f}  |  Est. P&L: {pnl_str}\n"
            f"Reason: {reason.replace('_', ' ')}\n"
            f"\n{thesis}\n"
            f"\n→ Execute manually on your broker."
        )
        self._send(title, body, priority=priority, tags=["rotating_light", symbol])

    # ── internal ──────────────────────────────────────────────────────────

    def _send(self, title: str, body: str, priority: str = "default", tags: list = None) -> None:
        if self.dry_run:
            log.info("[DRY RUN] Notification — %s\n%s", title, body)
            return
        self._ntfy(title, body, priority, tags or [])
        if self.discord_webhook:
            self._discord(title, body, priority)

    def _ntfy(self, title: str, body: str, priority: str, tags: list) -> None:
        if not self.ntfy_topic:
            return
        try:
            requests.post(
                f"https://ntfy.sh/{self.ntfy_topic}",
                data=body.encode("utf-8"),
                headers={
                    "Title": title,
                    "Priority": priority,
                    "Tags": ",".join(tags),
                },
                timeout=10,
            )
            log.info("ntfy alert sent: %s", title)
        except Exception as exc:
            log.warning("ntfy send failed: %s", exc)

    def _discord(self, title: str, body: str, priority: str) -> None:
        color = 0xFF0000 if priority == "urgent" else (0xFFAA00 if priority == "high" else 0x00AA00)
        try:
            requests.post(
                self.discord_webhook,
                json={"embeds": [{"title": title, "description": body, "color": color}]},
                timeout=10,
            )
        except Exception as exc:
            log.warning("Discord send failed: %s", exc)
