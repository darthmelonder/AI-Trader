"""LLM-Guided Mean Reversion Strategy (Strategy 3).

Philosophy: the opposite of S1/S2. Buys temporarily beaten-down quality
stocks that are oversold but structurally healthy, targeting a bounce back
toward fair value within 7-14 days.

Entry pipeline:
  1. Hard pre-gates (RSI <= 35 oversold, above 200d MA, earnings blackout 3d,
     platform signal not "sell")
  2. Compile context: platform analysis + yfinance fundamentals/technicals +
     FRED macro + SEC insider buying (contrarian confirmation)
  3. Gemini analysis with mean-reversion-tuned system prompt (cached 24h)
  4. Confidence gate (default >= 0.65, lower than S2 — oversold setups riskier)

Exit pipeline:
  1. Stop-loss 7% (tighter — mean-reversion failures are fast)
  2. Profit target 10% (or Gemini-suggested)
  3. Max hold 14 days (bounce window, not a long-term hold)
  4. LLM re-evaluation (cache-aware)
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from .base import EntrySignal, ExitSignal, Strategy

log = logging.getLogger(__name__)

_RSI_OVERSOLD = 35.0
_EXIT_CONFIDENCE_FLOOR = 0.40
_DEFAULT_MIN_CONFIDENCE = 0.65
_DEFAULT_EARNINGS_BLACKOUT = 3
_DEFAULT_MAX_HOLD_DAYS = 14
_DEFAULT_STOP_LOSS_PCT = 7.0
_DEFAULT_TARGET_RETURN_PCT = 10.0


class MeanReversionStrategy(Strategy):
    NEEDS_CONFIG = True

    name = "mean_reversion"
    description = (
        "Buys oversold NASDAQ stocks (RSI <= 35) that remain above the 200d MA, "
        "confirmed by Gemini mean-reversion analysis and optional SEC insider buying. "
        "Targets a 7-14 day bounce. Stop-loss 7%, profit target ~10%."
    )

    def __init__(self, config: dict):
        from llm_analyst import LLMAnalyst, MEAN_REVERSION_SYSTEM
        from data_sources import fred_source, sec_insider_source, yfinance_source

        self._yf = yfinance_source
        self._fred = fred_source
        self._insider = sec_insider_source

        self._fred_key: str = config.get("fred_api_key", "")
        self._min_confidence: float = float(config.get("s3_min_llm_confidence", _DEFAULT_MIN_CONFIDENCE))
        self._max_hold_days: int = int(config.get("s3_max_hold_days", _DEFAULT_MAX_HOLD_DAYS))
        self._earnings_blackout: int = int(config.get("s3_earnings_blackout_days", _DEFAULT_EARNINGS_BLACKOUT))
        self._stop_loss_pct: float = float(config.get("s3_stop_loss_pct", _DEFAULT_STOP_LOSS_PCT))
        self._target_return_pct: float = float(config.get("s3_target_return_pct", _DEFAULT_TARGET_RETURN_PCT))

        self._analyst = LLMAnalyst(
            api_key=config["gemini_api_key"],
            cache_ttl_hours=float(config.get("s3_llm_cache_ttl_hours", 24.0)),
            system_prompt=MEAN_REVERSION_SYSTEM,
        )
        log.info(
            "MeanReversionStrategy init: confidence_min=%.2f max_hold=%dd stop=%.1f%% target=%.1f%%",
            self._min_confidence, self._max_hold_days,
            self._stop_loss_pct, self._target_return_pct,
        )

    # ── entry ─────────────────────────────────────────────────────────────

    def evaluate_entry(
        self,
        symbol: str,
        macro: dict,
        stock: dict,
        news_items: list,
        universe: list,
    ) -> Optional[EntrySignal]:
        def _skip(gate: str, detail: str = ""):
            log.debug("  %s SKIP [mean_reversion] gate=%s %s", symbol, gate, detail)
            return None

        if symbol not in universe:
            return _skip("universe")

        # Don't fight an active sell signal — we want temporary oversold, not breakdown
        if stock.get("signal") == "sell" or stock.get("trend_status") == "defensive":
            return _skip("platform-signal",
                         f"signal={stock.get('signal')} trend={stock.get('trend_status')}")

        # Fetch fundamentals + locally-computed RSI/MACD
        fundamentals = self._yf.get_fundamentals(symbol)
        current_price = float(stock.get("current_price") or 0)

        # RSI must be oversold (opposite of S2's overbought filter)
        rsi = fundamentals.get("rsi_14")
        if rsi is None:
            return _skip("rsi-unavailable", "no RSI data")
        if rsi > _RSI_OVERSOLD:
            return _skip("rsi-not-oversold", f"RSI={rsi:.1f}>{_RSI_OVERSOLD}")

        # Must be above 200d MA — structurally healthy, just temporarily beaten down
        ma_200d = fundamentals.get("ma_200d")
        if ma_200d and current_price > 0 and current_price < ma_200d:
            return _skip("below-200d-ma", f"price={current_price:.2f}<MA200={ma_200d:.2f}")

        # Tight earnings blackout — bounce trades can't hold through binary events
        days_to_earnings = fundamentals.get("days_to_earnings")
        if days_to_earnings is not None and 0 <= days_to_earnings <= self._earnings_blackout:
            return _skip("earnings-blackout", f"earnings in {days_to_earnings}d")

        # Build context and call Gemini
        context = _build_context(
            symbol, macro, stock, news_items, fundamentals,
            self._fred_key, self._fred, self._insider,
        )
        try:
            decision = self._analyst.analyze(symbol, context)
        except Exception as exc:
            log.warning("%s: LLM analysis failed: %s", symbol, exc)
            return None

        if decision.get("decision") != "buy":
            log.debug(
                "  %s SKIP [mean_reversion] LLM decision=%s confidence=%.2f",
                symbol, decision.get("decision"), float(decision.get("confidence", 0)),
            )
            return None

        confidence = float(decision.get("confidence", 0))
        if confidence < self._min_confidence:
            return _skip("llm-confidence",
                         f"confidence={confidence:.2f}<{self._min_confidence}")

        insider = context.get("insider_activity", {})
        risks = "; ".join(decision.get("key_risks") or [])
        thesis = (
            f"[mean_reversion] {symbol}: {decision['thesis']} "
            f"| RSI={rsi:.1f} confidence={confidence:.2f} horizon={decision.get('holding_horizon_days', '?')}d "
            f"| target=+{decision.get('target_return_pct', '?')}% "
            f"| stop=-{decision.get('stop_loss_pct', '?')}% "
            f"| insider_buys={insider.get('buy_count', 0)} csuite={insider.get('csuite_bought', False)} "
            f"| risks: {risks or 'n/a'}"
        )
        confidence_factors = [
            f"RSI={rsi:.1f} (oversold)",
            f"MA200={'above' if ma_200d and current_price >= ma_200d else 'n/a'}",
            f"insider_buys={insider.get('buy_count', 0)}",
            f"LLM-confidence={confidence:.2f}",
        ]
        return EntrySignal(symbol=symbol, quantity=0, thesis=thesis,
                           confidence_factors=confidence_factors)

    # ── probe (gate-free inspection) ──────────────────────────────────────

    def probe(self, symbol: str, macro: dict, stock: dict, news_items: list) -> Optional[dict]:
        """Return raw Gemini analysis for *symbol* without applying any pre-gates."""
        self._analyst.invalidate(symbol)
        fundamentals = self._yf.get_fundamentals(symbol)
        context = _build_context(
            symbol, macro, stock, news_items, fundamentals,
            self._fred_key, self._fred, self._insider,
        )
        decision = self._analyst.analyze(symbol, context)
        return {"strategy": self.name, "context": context, "decision": decision}

    # ── exit ──────────────────────────────────────────────────────────────

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
        if entry_price <= 0 or current_price <= 0:
            return None

        # 1. Stop-loss (tight — mean-reversion failures are fast)
        drawdown_pct = (entry_price - current_price) / entry_price * 100
        stop_loss = float(config.get("s3_stop_loss_pct", self._stop_loss_pct))
        if drawdown_pct >= stop_loss:
            self._analyst.invalidate(symbol)
            return ExitSignal(
                symbol=symbol,
                reason="stop_loss",
                thesis=(
                    f"[mean_reversion] {symbol}: stop loss — {drawdown_pct:.1f}% drawdown "
                    f"from entry ${entry_price:.2f} -> ${current_price:.2f}"
                ),
            )

        # 2. Profit target
        cached_decision = (self._analyst._cache.get(symbol) or {}).get("decision", {})
        target = float(cached_decision.get("target_return_pct") or 0) or float(
            config.get("s3_target_return_pct", self._target_return_pct)
        )
        gain_pct = (current_price - entry_price) / entry_price * 100
        if gain_pct >= target:
            self._analyst.invalidate(symbol)
            return ExitSignal(
                symbol=symbol,
                reason="profit_target",
                thesis=(
                    f"[mean_reversion] {symbol}: profit target — +{gain_pct:.1f}% "
                    f"(target +{target:.1f}%) from entry ${entry_price:.2f}"
                ),
            )

        # 3. Max hold age (shorter than S2 — bounce window)
        max_hold = int(config.get("s3_max_hold_days", self._max_hold_days))
        opened_raw = position.get("opened_at") or position.get("created_at") or ""
        if opened_raw:
            try:
                opened_at = datetime.fromisoformat(opened_raw.replace("Z", "+00:00"))
                age_days = (datetime.now(timezone.utc) - opened_at).days
                if age_days >= max_hold:
                    self._analyst.invalidate(symbol)
                    return ExitSignal(
                        symbol=symbol,
                        reason="age_limit",
                        thesis=f"[mean_reversion] {symbol}: held {age_days}d >= {max_hold}d limit",
                    )
            except ValueError:
                pass

        # 4. LLM re-evaluation (cache-aware — at most once per day)
        try:
            fundamentals = self._yf.get_fundamentals(symbol)
            context = _build_context(
                symbol, macro, stock, [],
                fundamentals, self._fred_key, self._fred, self._insider,
            )
            decision = self._analyst.analyze(symbol, context)
            confidence = float(decision.get("confidence", 1.0))
            if decision.get("decision") == "sell" or confidence < _EXIT_CONFIDENCE_FLOOR:
                self._analyst.invalidate(symbol)
                return ExitSignal(
                    symbol=symbol,
                    reason="llm_signal_flip",
                    thesis=(
                        f"[mean_reversion] {symbol}: LLM -> decision={decision.get('decision')} "
                        f"confidence={confidence:.2f}"
                    ),
                )
        except Exception as exc:
            log.warning("%s: LLM exit check failed: %s", symbol, exc)

        return None


# ── context builder ───────────────────────────────────────────────────────────

def _build_context(
    symbol: str,
    macro: dict,
    stock: dict,
    news_items: list,
    fundamentals: dict,
    fred_key: str,
    fred_source,
    insider_source=None,
) -> dict:
    """Compile all data sources into the context dict for Gemini."""
    analysis = stock.get("analysis") or {}
    current_price = float(stock.get("current_price") or 0)
    ma_200d = fundamentals.get("ma_200d")

    ticker_sentiments = []
    for item in news_items:
        for ts in item.get("ticker_sentiment") or []:
            if ts.get("ticker", "").upper() == symbol:
                ticker_sentiments.append({
                    "label": ts.get("sentiment_label"),
                    "relevance": ts.get("relevance_score"),
                })

    fred_data = fred_source.get_macro_snapshot(fred_key) if fred_key else {}
    insider = insider_source.get_insider_activity(symbol) if insider_source else {}

    return {
        "symbol": symbol,
        "platform_analysis": {
            "signal": stock.get("signal"),
            "trend_status": stock.get("trend_status"),
            "signal_score": stock.get("signal_score"),
            "return_5d_pct": analysis.get("return_5d_pct"),
            "return_20d_pct": analysis.get("return_20d_pct"),
            "current_price": current_price,
            "bullish_factors": (stock.get("bullish_factors") or [])[:3],
            "risk_factors": (stock.get("risk_factors") or [])[:3],
            "summary": (stock.get("summary") or "")[:200],
        },
        "technicals": {
            "rsi_14": fundamentals.get("rsi_14"),
            "macd": fundamentals.get("macd"),
            "ma_50d": fundamentals.get("ma_50d"),
            "ma_200d": ma_200d,
            "above_200d_ma": bool(ma_200d and current_price >= float(ma_200d or 0)),
        },
        "fundamentals": {
            "pe_ratio": fundamentals.get("pe_ratio"),
            "analyst_recommendation": fundamentals.get("analyst_recommendation"),
            "days_to_earnings": fundamentals.get("days_to_earnings"),
        },
        "macro": {
            "platform_verdict": macro.get("verdict"),
            "platform_signals": f"{macro.get('bullish_count', '?')}/{macro.get('total_count', '?')} bullish",
            "fed_funds_rate": fred_data.get("fed_funds_rate"),
            "treasury_10y": fred_data.get("treasury_10y"),
            "cpi_yoy_pct": fred_data.get("cpi_yoy_pct"),
        },
        "news_sentiment": ticker_sentiments[:3] if ticker_sentiments else "no_ticker_specific_news",
        "insider_activity": {
            "buy_count": insider.get("buy_count", 0),
            "sell_count": insider.get("sell_count", 0),
            "csuite_bought": insider.get("csuite_bought", False),
            "last_buy_date": insider.get("last_buy_date"),
        },
    }
