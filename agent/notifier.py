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
        decision_data: dict = None,
    ) -> None:
        title = f"TRADE SIGNAL — {action.upper()} {symbol}"
        body = _format_entry_body(action, symbol, quantity, price, strategy_name,
                                  thesis, decision_data or {})
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
        body = _format_exit_body(symbol, quantity, price, entry_price, pnl_str, reason, thesis)
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


# ── notification body formatters ──────────────────────────────────────────────

def _format_entry_body(
    action: str,
    symbol: str,
    quantity: float,
    price: float,
    strategy_name: str,
    thesis: str,
    decision_data: dict,
) -> str:
    lines = [
        f"{action.upper()} {quantity:.0f} shares @ ~${price:.2f}",
        f"Strategy: {strategy_name}",
        "",
    ]

    if decision_data:
        # LLM-backed strategy — show full structured reasoning
        raw_thesis = decision_data.get("thesis", thesis)
        lines += [
            "Thesis:",
            f"  {raw_thesis}",
            "",
            "Decision metrics:",
            f"  Confidence: {decision_data.get('confidence', '?')}  |  "
            f"Hold: {decision_data.get('holding_horizon_days', '?')} days",
            f"  Target: +{decision_data.get('target_return_pct', '?')}%  |  "
            f"Stop: -{decision_data.get('stop_loss_pct', '?')}%",
        ]
        risks = decision_data.get("key_risks") or []
        if risks:
            lines += ["", "Key risks:"]
            lines += [f"  • {r}" for r in risks]
    else:
        # Rule-based strategy — show thesis as-is
        lines += ["Rationale:", f"  {thesis}"]

    lines += ["", "→ Execute manually on your broker."]
    return "\n".join(lines)


def _format_exit_body(
    symbol: str,
    quantity: float,
    price: float,
    entry_price: float,
    pnl_str: str,
    reason: str,
    thesis: str,
) -> str:
    lines = [
        f"SELL {quantity:.0f} shares @ ~${price:.2f}",
        f"Entry: ${entry_price:.2f}  |  Est. P&L: {pnl_str}",
        f"Reason: {reason.replace('_', ' ')}",
        "",
        "Thesis:",
        f"  {thesis}",
        "",
        "→ Execute manually on your broker.",
    ]
    return "\n".join(lines)
