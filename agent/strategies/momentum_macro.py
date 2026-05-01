import logging
from datetime import datetime, timezone
from typing import Optional

from .base import EntrySignal, ExitSignal, Strategy

log = logging.getLogger(__name__)

# Gate thresholds
_MIN_5D_RETURN = 0.0
_MIN_20D_RETURN = 0.0
# Distance from current price to nearest support, as % of support price.
# 30% means "price is no more than 30% above its 20-day support floor."
# Too tight (e.g. 5%) misses strong uptrend entries; too loose loses the risk/reward filter.
_MAX_DISTANCE_TO_SUPPORT_PCT = 30.0
_NEGATIVE_SENTIMENT_RELEVANCE_THRESHOLD = 0.3


class MomentumMacroStrategy(Strategy):
    name = "momentum_macro"
    description = (
        "Buys NASDAQ top-50 stocks when macro is bullish, the platform signal is buy/bullish, "
        "both 5d and 20d returns are positive, price is near support, and no bearish news."
    )

    def evaluate_entry(
        self,
        symbol: str,
        macro: dict,
        stock: dict,
        news_items: list,
        universe: list,
    ) -> Optional[EntrySignal]:
        def _skip(gate: str, detail: str):
            log.debug("  %s SKIP gate=%s %s", symbol, gate, detail)
            return None

        # Gate 1: symbol in universe
        if symbol not in universe:
            return _skip("1-universe", "not in NASDAQ top-50")

        # Gate 2: macro must be bullish
        if macro.get("verdict") != "bullish":
            return _skip("2-macro", macro.get("verdict"))

        # Gate 3: platform signal and trend
        sig = stock.get("signal")
        trend = stock.get("trend_status")
        if sig != "buy":
            return _skip("3-signal", f"signal={sig}")
        if trend not in ("bullish", "constructive"):
            return _skip("3-trend", f"trend={trend}")

        # Gate 4: both momentum timeframes positive
        analysis = stock.get("analysis") or {}
        return_5d = float(analysis.get("return_5d_pct") or 0)
        return_20d = float(analysis.get("return_20d_pct") or 0)
        if return_5d <= _MIN_5D_RETURN:
            return _skip("4-momentum", f"5d={return_5d:+.1f}%")
        if return_20d <= _MIN_20D_RETURN:
            return _skip("4-momentum", f"20d={return_20d:+.1f}%")

        # Gate 5: not too far above support (distance_to_support_pct lives in analysis)
        distance_to_support = float(analysis.get("distance_to_support_pct") or 999)
        if distance_to_support >= _MAX_DISTANCE_TO_SUPPORT_PCT:
            return _skip("5-support", f"dist={distance_to_support:.1f}% >= {_MAX_DISTANCE_TO_SUPPORT_PCT}%")

        # Gate 6: no bearish news for this ticker
        for item in news_items:
            for ts in item.get("ticker_sentiment") or []:
                if ts.get("ticker", "").upper() != symbol:
                    continue
                label = (ts.get("sentiment_label") or "").lower()
                relevance = float(ts.get("relevance_score") or 0)
                if "bearish" in label and relevance > _NEGATIVE_SENTIMENT_RELEVANCE_THRESHOLD:
                    return _skip("6-news", f"bearish sentiment relevance={relevance:.2f}")

        bullish_factors = stock.get("bullish_factors") or []
        macro_count = f"{macro.get('bullish_count', '?')}/{macro.get('total_count', '?')}"
        thesis = (
            f"[momentum_macro] {symbol}: macro={macro['verdict']} ({macro_count} signals), "
            f"signal={stock['signal']}/{stock['trend_status']}, "
            f"5d={return_5d:+.1f}%, 20d={return_20d:+.1f}%, "
            f"dist_support={distance_to_support:.1f}%. "
            f"Bullish factors: {'; '.join(bullish_factors[:3]) or 'n/a'}"
        )
        return EntrySignal(
            symbol=symbol,
            quantity=0,  # set by scanner after price fetch
            thesis=thesis,
            confidence_factors=bullish_factors,
        )

    def evaluate_exit(
        self,
        symbol: str,
        position: dict,
        macro: dict,
        stock: dict,
        current_price: float,
        config: dict,
    ) -> Optional[ExitSignal]:
        entry_price = float(position.get("entry_price") or 0)
        stop_loss_pct = float(config.get("stop_loss_pct", 8.0))
        max_age_days = int(config.get("max_position_age_days", 90))

        # Exit 1: stop loss
        if entry_price > 0:
            drawdown_pct = ((entry_price - current_price) / entry_price) * 100
            if drawdown_pct >= stop_loss_pct:
                return ExitSignal(
                    symbol=symbol,
                    reason="stop_loss",
                    thesis=(
                        f"[momentum_macro] {symbol} stop loss: {drawdown_pct:.1f}% drawdown "
                        f"from entry ${entry_price:.2f} → current ${current_price:.2f}"
                    ),
                )

        # Exit 2: signal flip
        signal = stock.get("signal", "")
        trend = stock.get("trend_status", "")
        if signal == "sell" or trend == "defensive":
            return ExitSignal(
                symbol=symbol,
                reason="signal_flip",
                thesis=(
                    f"[momentum_macro] {symbol} signal flipped: signal={signal}, trend={trend}"
                ),
            )

        # Exit 3: macro flip
        if macro.get("verdict") == "defensive":
            summary = (macro.get("meta") or {}).get("summary", "")
            return ExitSignal(
                symbol=symbol,
                reason="macro_flip",
                thesis=f"[momentum_macro] {symbol} macro turned defensive. {summary}",
            )

        # Exit 4: position age exceeded
        opened_raw = position.get("opened_at") or position.get("created_at") or ""
        if opened_raw:
            try:
                opened_at = datetime.fromisoformat(opened_raw.replace("Z", "+00:00"))
                age_days = (datetime.now(timezone.utc) - opened_at).days
                if age_days >= max_age_days:
                    return ExitSignal(
                        symbol=symbol,
                        reason="age_limit",
                        thesis=(
                            f"[momentum_macro] {symbol} held {age_days}d, "
                            f"exceeded {max_age_days}d limit"
                        ),
                    )
            except ValueError:
                pass

        return None
