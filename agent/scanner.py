import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

from client import AI4TradeClient
from notifier import Notifier
from strategies.base import Strategy

log = logging.getLogger(__name__)


@dataclass
class _ScanStats:
    symbols_checked: int = 0
    symbols_with_analysis: int = 0
    exits_triggered: int = 0
    entries_opened: int = 0
    api_errors: list = field(default_factory=list)

    def log_summary(self, elapsed: float) -> None:
        log.info(
            "Scan summary: %ds elapsed | %d symbols iterated | %d had platform analysis "
            "| %d entries opened | %d exits triggered",
            int(elapsed), self.symbols_checked, self.symbols_with_analysis,
            self.entries_opened, self.exits_triggered,
        )
        if self.symbols_with_analysis > 0 and self.entries_opened == 0:
            log.info(
                "No entries: %d symbols had data but none passed all strategy gates "
                "(normal in trending/non-oversold markets — use --verbose to see per-symbol decisions)",
                self.symbols_with_analysis,
            )
        if self.api_errors:
            log.warning("API errors during scan: %s", ", ".join(self.api_errors))

ET = ZoneInfo("America/New_York")
_MARKET_OPEN = dtime(9, 30)
_MARKET_CLOSE = dtime(16, 0)

NASDAQ_TOP_50 = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "TSLA",
    "AVGO", "COST", "NFLX", "ASML", "AMD", "QCOM", "INTC", "INTU",
    "AMAT", "LRCX", "MU", "ADI", "KLAC", "MRVL", "SNPS", "CDNS",
    "PANW", "CRWD", "FTNT", "ZS", "OKTA", "DDOG", "SNOW", "NET",
    "TEAM", "WDAY", "NOW", "CRM", "ADBE", "ORCL", "CSCO", "TXN",
    "HON", "ISRG", "REGN", "VRTX", "GILD", "AMGN", "BIIB", "MRNA",
    "BKNG", "ABNB", "SNDK", "LITE", "WDC", "CIEN",
]


def is_market_open() -> bool:
    now_et = datetime.now(ET)
    if now_et.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    t = now_et.time()
    return _MARKET_OPEN <= t < _MARKET_CLOSE


class Scanner:
    def __init__(
        self,
        client: AI4TradeClient,
        strategies: list,
        notifier: Notifier,
        config: dict,
    ):
        self.client = client
        self.strategies = strategies
        self.notifier = notifier
        self.config = config

    def run_scan(self) -> None:
        t0 = time.time()
        stats = _ScanStats()
        log.info("── scan start ─────────────────────────────────")
        market_open = is_market_open()
        log.info("Market open: %s", market_open)

        # Fetch shared data once for the whole scan
        macro = self._safe_fetch("macro_signals", self.client.macro_signals)
        news_resp = self._safe_fetch("news", self.client.news, "equities", 5)
        news_items = (news_resp.get("categories") or [{}])[0].get("items") or [] if news_resp else []

        if not macro:
            log.warning("Could not fetch macro signals — skipping scan")
            return

        macro_count = f"{macro.get('bullish_count','?')}/{macro.get('total_count','?')}"
        log.info("Macro: verdict=%s (%s signals bullish)", macro.get("verdict"), macro_count)
        log.info("News: %d equities items loaded", len(news_items))

        # Current open positions (self-owned only)
        positions_resp = self._safe_fetch("positions", self.client.positions) or {}
        open_positions = [
            p for p in (positions_resp.get("positions") or [])
            if (p.get("source") or "self") == "self" and p.get("side") == "long"
        ]
        held_symbols = {p["symbol"] for p in open_positions}
        # Fall back to starting balance when positions endpoint is unavailable (e.g. dry-run, no token yet)
        cash = float(positions_resp.get("cash") or 100_000.0)

        log.info("Open positions: %d  |  Cash: $%.2f", len(open_positions), cash)
        for pos in open_positions:
            log.info(
                "  Holding %s: qty=%.2f entry=$%.2f pnl=$%.2f",
                pos.get("symbol"), float(pos.get("quantity") or 0),
                float(pos.get("entry_price") or 0), float(pos.get("pnl") or 0),
            )

        # ── Phase 1: Exit checks ──────────────────────────────────────────
        for pos in open_positions:
            self._check_exit(pos, macro, market_open, stats)

        # Refresh after exits
        positions_resp = self._safe_fetch("positions", self.client.positions) or {}
        open_positions = [
            p for p in (positions_resp.get("positions") or [])
            if (p.get("source") or "self") == "self" and p.get("side") == "long"
        ]
        held_symbols = {p["symbol"] for p in open_positions}
        cash = float(positions_resp.get("cash") or 100_000.0)
        max_positions = int(self.config.get("max_positions", 6))

        # ── Phase 2: Entry scan ───────────────────────────────────────────
        force_scan = self.config.get("force_scan", False)
        if len(open_positions) >= max_positions:
            log.info("At max positions (%d/%d) — skipping entry scan", len(open_positions), max_positions)
        elif not market_open and not force_scan:
            log.info("Market closed — skipping entry scan (use --force-scan to override)")
        else:
            if force_scan and not market_open:
                log.info("Market closed but --force-scan active — running entry scan for testing")
            self._scan_entries(macro, news_items, held_symbols, cash, max_positions, len(open_positions), stats)

        stats.log_summary(time.time() - t0)
        log.info("── scan end ───────────────────────────────────")

    # ── exit logic ────────────────────────────────────────────────────────

    def _check_exit(self, position: dict, macro: dict, market_open: bool, stats: "_ScanStats") -> None:
        symbol = position["symbol"]
        price_resp = self._safe_fetch(f"price:{symbol}", self.client.price, symbol)
        if not price_resp:
            return
        current_price = float(price_resp.get("price") or 0)
        if current_price <= 0:
            log.warning("%s: could not get valid price (%s)", symbol, price_resp)
            return

        stock = self._safe_fetch(f"stock:{symbol}", self.client.stock_latest, symbol) or {}

        for strategy in self.strategies:
            exit_sig = strategy.evaluate_exit(
                symbol=symbol,
                position=position,
                macro=macro,
                stock=stock,
                current_price=current_price,
                config=self.config,
            )
            if exit_sig:
                stats.exits_triggered += 1
                if market_open:
                    try:
                        self._execute_exit(position, current_price, exit_sig)
                    except Exception as exc:
                        log.error("Failed to publish exit for %s: %s — scan continues", symbol, exc)
                else:
                    log.info("EXIT flagged (market closed) %s | reason=%s — will publish when market opens",
                             symbol, exit_sig.reason)
                break

    def _execute_exit(self, position: dict, current_price: float, exit_sig) -> None:
        symbol = exit_sig.symbol
        quantity = float(position.get("quantity") or 0)
        entry_price = float(position.get("entry_price") or 0)
        pnl = (current_price - entry_price) * quantity

        _log_exit(symbol, quantity, current_price, entry_price, pnl, exit_sig)
        platform_ok = False

        # 1. Publish realtime sell to platform (best-effort)
        try:
            result = self.client.publish_realtime(
                action="sell",
                symbol=symbol,
                quantity=quantity,
                content=exit_sig.thesis,
            )
            if result.get("dry_run"):
                platform_ok = True
            elif not result.get("success", True):
                log.warning("%s: sell signal rejected by platform: %s", symbol, result)
            else:
                platform_ok = True
        except Exception as exc:
            log.error("%s: failed to publish realtime sell to platform: %s", symbol, exc)

        # 2. Publish strategy post (best-effort)
        if platform_ok:
            try:
                self.client.publish_strategy(
                    title=f"{symbol} — exit ({exit_sig.reason.replace('_', ' ')})",
                    content=exit_sig.thesis,
                    symbols=[symbol],
                )
            except Exception as exc:
                log.warning("%s: failed to publish strategy exit post: %s", symbol, exc)

        # 3. Always notify
        self.notifier.send_exit_alert(
            symbol=symbol,
            quantity=quantity,
            price=current_price,
            entry_price=entry_price,
            reason=exit_sig.reason,
            thesis=exit_sig.thesis,
        )
        if not platform_ok:
            log.warning(
                "%s: ntfy notification sent but platform exit post FAILED — "
                "virtual position may still show as open on the leaderboard",
                symbol,
            )

    # ── entry logic ───────────────────────────────────────────────────────

    def _scan_entries(
        self,
        macro: dict,
        news_items: list,
        held_symbols: set,
        cash: float,
        max_positions: int,
        current_count: int,
        stats: "_ScanStats",
    ) -> None:
        # Featured stocks first (platform's hot picks, have freshest analysis)
        featured_resp = self._safe_fetch("featured", self.client.featured_stocks, 12) or {}
        featured_symbols = [
            item["symbol"]
            for item in (featured_resp.get("items") or [])
            if item.get("symbol")
        ]
        # Remaining universe in order, deduped
        rest = [s for s in NASDAQ_TOP_50 if s not in featured_symbols]
        ordered = featured_symbols + rest

        slots_available = max_positions - current_count
        filled = 0

        for symbol in ordered:
            if filled >= slots_available:
                break
            if symbol in held_symbols:
                continue

            # Rate-limit: 1 req/sec on /price
            time.sleep(1)

            stats.symbols_checked += 1
            stock = self._safe_fetch(f"stock:{symbol}", self.client.stock_latest, symbol)
            if not stock or not stock.get("available"):
                log.debug("%s: no platform analysis available — skipping", symbol)
                continue

            stats.symbols_with_analysis += 1
            for strategy in self.strategies:
                entry_sig = strategy.evaluate_entry(
                    symbol=symbol,
                    macro=macro,
                    stock=stock,
                    news_items=news_items,
                    universe=NASDAQ_TOP_50,
                )
                if not entry_sig:
                    continue

                # Fetch price for quantity calc
                price_resp = self._safe_fetch(f"price:{symbol}", self.client.price, symbol)
                if not price_resp:
                    continue
                current_price = float(price_resp.get("price") or 0)
                if current_price <= 0:
                    continue

                quantity = self._compute_quantity(current_price, cash)
                if quantity < 1:
                    log.info("%s: insufficient cash for entry (cash=$%.2f, price=$%.2f)", symbol, cash, current_price)
                    continue

                entry_sig.quantity = quantity
                _log_entry(symbol, strategy.name, quantity, current_price, entry_sig)
                try:
                    self._execute_entry(entry_sig, current_price, strategy.name)
                except Exception as exc:
                    log.error("Unexpected error executing entry for %s: %s", symbol, exc, exc_info=True)
                    continue

                stats.entries_opened += 1
                # Update cash estimate locally (avoid extra API call)
                cash -= current_price * quantity
                held_symbols.add(symbol)
                filled += 1
                break  # only one strategy can enter per symbol per scan

    def _execute_entry(self, entry_sig, current_price: float, strategy_name: str) -> None:
        symbol = entry_sig.symbol
        platform_ok = False

        # 1. Publish realtime trade to platform (best-effort)
        try:
            result = self.client.publish_realtime(
                action="buy",
                symbol=symbol,
                quantity=entry_sig.quantity,
                content=entry_sig.thesis,
            )
            if result.get("dry_run"):
                platform_ok = True
            elif not result.get("success", True):
                log.warning("%s: buy signal rejected by platform: %s", symbol, result)
            else:
                platform_ok = True
        except Exception as exc:
            log.error("%s: failed to publish realtime signal to platform: %s", symbol, exc)

        # 2. Publish strategy post (only worth doing if the trade was accepted)
        if platform_ok:
            try:
                self.client.publish_strategy(
                    title=f"{symbol} — {strategy_name} entry signal",
                    content=self._build_strategy_post(entry_sig, current_price, strategy_name),
                    symbols=[symbol],
                )
            except Exception as exc:
                log.warning("%s: failed to publish strategy post: %s", symbol, exc)

        # 3. Always notify — this is the user's primary alert channel for manual execution
        self.notifier.send_entry_alert(
            symbol=symbol,
            action="buy",
            quantity=entry_sig.quantity,
            price=current_price,
            thesis=entry_sig.thesis,
            strategy_name=strategy_name,
            decision_data=entry_sig.decision_data,
        )
        if not platform_ok:
            log.warning(
                "%s: ntfy notification sent but platform post FAILED — "
                "virtual position is NOT tracked on the leaderboard",
                symbol,
            )

    # ── helpers ───────────────────────────────────────────────────────────

    def _compute_quantity(self, price: float, cash: float) -> float:
        position_size_pct = float(self.config.get("position_size_pct", 20.0))
        allocation = cash * (position_size_pct / 100.0)
        return max(0, math.floor(allocation / price))

    def _build_strategy_post(self, entry_sig, price: float, strategy_name: str = "") -> str:
        factors = "\n".join(f"• {f}" for f in entry_sig.confidence_factors[:5]) or "• n/a"
        d = entry_sig.decision_data
        if d:
            metrics = (
                f"Confidence: {d.get('confidence', '?')}  |  "
                f"Horizon: {d.get('holding_horizon_days', '?')} days  |  "
                f"Target: +{d.get('target_return_pct', '?')}%  |  "
                f"Stop: -{d.get('stop_loss_pct', '?')}%"
            )
            risks = "\n".join(f"• {r}" for r in (d.get("key_risks") or [])) or "• n/a"
            thesis_text = d.get("thesis", entry_sig.thesis)
            return (
                f"Strategy: {strategy_name}\n"
                f"Symbol: {entry_sig.symbol}\n"
                f"Virtual entry: {entry_sig.quantity:.0f} shares @ ~${price:.2f}\n\n"
                f"Thesis:\n{thesis_text}\n\n"
                f"Decision metrics:\n{metrics}\n\n"
                f"Key risks:\n{risks}\n\n"
                f"Confidence factors:\n{factors}\n\n"
                f"This is a simulated trade for strategy validation only."
            )
        return (
            f"Strategy: {strategy_name}\n"
            f"Symbol: {entry_sig.symbol}\n"
            f"Virtual entry: {entry_sig.quantity:.0f} shares @ ~${price:.2f}\n\n"
            f"Signal rationale:\n{entry_sig.thesis}\n\n"
            f"Confidence factors:\n{factors}\n\n"
            f"This is a simulated trade for strategy validation only."
        )

    def _safe_fetch(self, label: str, fn, *args):
        try:
            return fn(*args)
        except Exception as exc:
            log.warning("fetch '%s' failed: %s", label, exc)
            return None


# ── structured log helpers ────────────────────────────────────────────────────

def _log_entry(symbol: str, strategy: str, quantity: float, price: float, entry_sig) -> None:
    d = entry_sig.decision_data
    log.info("ENTRY %s | strategy=%s | BUY %.0f shares @ ~$%.2f", symbol, strategy, quantity, price)
    if d:
        log.info(
            "ENTRY %s | confidence=%.2f | horizon=%sd | target=+%.1f%% | stop=-%.1f%%",
            symbol,
            float(d.get("confidence", 0)),
            d.get("holding_horizon_days", "?"),
            float(d.get("target_return_pct", 0)),
            float(d.get("stop_loss_pct", 0)),
        )
        log.info("ENTRY %s | thesis: %s", symbol, d.get("thesis", ""))
        risks = d.get("key_risks") or []
        if risks:
            log.info("ENTRY %s | risks: %s", symbol, " | ".join(risks))
    else:
        log.info("ENTRY %s | thesis: %s", symbol, entry_sig.thesis)


def _log_exit(
    symbol: str, quantity: float, price: float,
    entry_price: float, pnl: float, exit_sig,
) -> None:
    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
    log.info(
        "EXIT %s | reason=%s | SELL %.0f shares @ ~$%.2f | entry=$%.2f | est_pnl=%s",
        symbol, exit_sig.reason, quantity, price, entry_price, pnl_str,
    )
    log.info("EXIT %s | thesis: %s", symbol, exit_sig.thesis)
