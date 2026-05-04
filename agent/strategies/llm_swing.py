"""LLM-Guided Multi-Factor Swing Strategy (Strategy 2).

Entry pipeline:
  1. Hard pre-gates (RSI range, above 50d MA, earnings blackout, platform signal)
  2. Compile context: platform analysis + yfinance fundamentals+technicals + FRED macro
  3. Gemini analysis (cached 24h per symbol)
  4. Confidence gate (default >= 0.70)

Exit pipeline (evaluated every scan):
  1. Price stop-loss (instant)
  2. Profit target hit (instant)
  3. Max hold age (instant)
  4. LLM re-evaluation flips to sell or drops below confidence floor (cache-aware)
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from .base import EntrySignal, ExitSignal, Strategy

log = logging.getLogger(__name__)

_RSI_MIN = 35.0
_RSI_MAX = 65.0
_EXIT_CONFIDENCE_FLOOR = 0.40
_DEFAULT_MIN_CONFIDENCE = 0.70
_DEFAULT_EARNINGS_BLACKOUT = 5
_DEFAULT_MAX_HOLD_DAYS = 30
_DEFAULT_STOP_LOSS_PCT = 9.0
_DEFAULT_TARGET_RETURN_PCT = 12.0


class LLMSwingStrategy(Strategy):
    # Tells _build_scanner in main.py to pass config dict to the constructor.
    NEEDS_CONFIG = True

    name = "llm_swing"
    description = (
        "Swing trades targeting 1–4 week holds. Combines AI4Trade platform signals, "
        "yfinance fundamentals + RSI/MACD (computed locally, no AV needed), and FRED macro. "
        "Gemini synthesises all data into a buy/skip/sell decision before any trade fires."
    )

    def __init__(self, config: dict):
        from llm_analyst import LLMAnalyst
        from data_sources import fred_source, sec_insider_source, yfinance_source

        self._yf = yfinance_source
        self._fred = fred_source
        self._insider = sec_insider_source

        self._fred_key: str = config.get("fred_api_key", "")

        self._min_confidence: float = float(config.get("s2_min_llm_confidence", _DEFAULT_MIN_CONFIDENCE))
        self._max_hold_days: int = int(config.get("s2_max_hold_days", _DEFAULT_MAX_HOLD_DAYS))
        self._earnings_blackout: int = int(config.get("s2_earnings_blackout_days", _DEFAULT_EARNINGS_BLACKOUT))
        self._stop_loss_pct: float = float(config.get("s2_stop_loss_pct", _DEFAULT_STOP_LOSS_PCT))
        self._target_return_pct: float = float(config.get("s2_target_return_pct", _DEFAULT_TARGET_RETURN_PCT))

        self._analyst = LLMAnalyst(
            api_key=config["gemini_api_key"],
            cache_ttl_hours=float(config.get("s2_llm_cache_ttl_hours", 24.0)),
        )
        log.info(
            "LLMSwingStrategy init: confidence_min=%.2f max_hold=%dd earnings_blackout=%dd",
            self._min_confidence, self._max_hold_days, self._earnings_blackout,
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
            log.debug("  %s SKIP [llm_swing] gate=%s %s", symbol, gate, detail)
            return None

        if symbol not in universe:
            return _skip("universe")

        # Hard platform gate — don't enter against an active sell signal
        if stock.get("signal") == "sell" or stock.get("trend_status") == "defensive":
            return _skip("platform-signal", f"signal={stock.get('signal')} trend={stock.get('trend_status')}")

        # Fetch fundamentals + locally-computed RSI/MACD (no external API needed)
        fundamentals = self._yf.get_fundamentals(symbol)
        current_price = float(stock.get("current_price") or fundamentals.get("current_price_yf") or 0)

        # RSI gate
        rsi = fundamentals.get("rsi_14")
        if rsi is not None:
            if rsi < _RSI_MIN:
                return _skip("rsi-oversold", f"RSI={rsi:.1f}<{_RSI_MIN}")
            if rsi > _RSI_MAX:
                return _skip("rsi-overbought", f"RSI={rsi:.1f}>{_RSI_MAX}")

        # 50d MA gate
        ma_50d = fundamentals.get("ma_50d")
        if ma_50d and current_price > 0 and current_price < ma_50d:
            return _skip("below-50d-ma", f"price={current_price:.2f}<MA50={ma_50d:.2f}")

        # Earnings blackout gate
        days_to_earnings = fundamentals.get("days_to_earnings")
        if days_to_earnings is not None and 0 <= days_to_earnings <= self._earnings_blackout:
            return _skip("earnings-blackout", f"earnings in {days_to_earnings}d")

        # All pre-gates passed — log at INFO so production logs show the funnel
        log.info("%s [llm_swing] pre-gates passed: RSI=%.1f MA50=%s analyst=%s — calling Gemini",
                 symbol, rsi if rsi is not None else -1,
                 "above" if ma_50d and current_price >= ma_50d else "below/n/a",
                 fundamentals.get("analyst_recommendation") or "n/a")

        # Build LLM context and call analyst
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
                "  %s SKIP [llm_swing] LLM decision=%s confidence=%.2f",
                symbol, decision.get("decision"), float(decision.get("confidence", 0)),
            )
            return None

        confidence = float(decision.get("confidence", 0))
        if confidence < self._min_confidence:
            return _skip("llm-confidence", f"confidence={confidence:.2f}<{self._min_confidence}")

        risks = "; ".join(decision.get("key_risks") or [])
        thesis = (
            f"[llm_swing] {symbol}: {decision['thesis']} "
            f"| confidence={confidence:.2f} horizon={decision.get('holding_horizon_days', '?')}d "
            f"| target=+{decision.get('target_return_pct', '?')}% "
            f"| stop=-{decision.get('stop_loss_pct', '?')}% "
            f"| risks: {risks or 'n/a'}"
        )
        confidence_factors = [
            f"RSI={rsi:.1f}" if rsi is not None else "RSI=n/a",
            f"MA50={'above' if ma_50d and current_price >= ma_50d else 'n/a'}",
            f"analyst={fundamentals.get('analyst_recommendation') or 'n/a'}",
            f"LLM-confidence={confidence:.2f}",
        ]
        return EntrySignal(symbol=symbol, quantity=0, thesis=thesis,
                           confidence_factors=confidence_factors, decision_data=decision)

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

        # 1. Stop loss
        drawdown_pct = (entry_price - current_price) / entry_price * 100
        stop_loss = float(config.get("s2_stop_loss_pct", self._stop_loss_pct))
        if drawdown_pct >= stop_loss:
            self._analyst.invalidate(symbol)
            return ExitSignal(
                symbol=symbol,
                reason="stop_loss",
                thesis=(
                    f"[llm_swing] {symbol}: stop loss — {drawdown_pct:.1f}% drawdown "
                    f"from entry ${entry_price:.2f} → ${current_price:.2f}"
                ),
            )

        # 2. Profit target — use LLM-suggested target if cached, else config default
        cached_decision = (self._analyst._cache.get(symbol) or {}).get("decision", {})
        target = float(cached_decision.get("target_return_pct") or 0) or float(
            config.get("s2_target_return_pct", self._target_return_pct)
        )
        gain_pct = (current_price - entry_price) / entry_price * 100
        if gain_pct >= target:
            self._analyst.invalidate(symbol)
            return ExitSignal(
                symbol=symbol,
                reason="profit_target",
                thesis=(
                    f"[llm_swing] {symbol}: profit target — +{gain_pct:.1f}% "
                    f"(target +{target:.1f}%) from entry ${entry_price:.2f}"
                ),
            )

        # 3. Max hold age
        max_hold = int(config.get("s2_max_hold_days", self._max_hold_days))
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
                        thesis=f"[llm_swing] {symbol}: held {age_days}d ≥ {max_hold}d limit",
                    )
            except ValueError:
                pass

        # 4. LLM re-evaluation (respects cache TTL — at most once per day)
        try:
            context = _build_context(
                symbol, macro, stock, [],
                self._yf.get_fundamentals(symbol),
                self._fred_key, self._fred, self._insider,
            )
            decision = self._analyst.analyze(symbol, context)
            confidence = float(decision.get("confidence", 1.0))
            if decision.get("decision") == "sell" or confidence < _EXIT_CONFIDENCE_FLOOR:
                self._analyst.invalidate(symbol)
                return ExitSignal(
                    symbol=symbol,
                    reason="llm_signal_flip",
                    thesis=(
                        f"[llm_swing] {symbol}: LLM → decision={decision.get('decision')} "
                        f"confidence={confidence:.2f}"
                    ),
                )
        except Exception as exc:
            log.warning("%s: LLM exit check failed: %s", symbol, exc)

        return None


# ── context builder ───────────────────────────────────────────────────────

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
    """Compile all data sources into the context dict sent to the LLM."""
    analysis = stock.get("analysis") or {}
    current_price = float(stock.get("current_price") or 0)
    ma_50d = fundamentals.get("ma_50d")

    # Distil ticker-specific news sentiment
    ticker_sentiments = []
    for item in news_items:
        for ts in item.get("ticker_sentiment") or []:
            if ts.get("ticker", "").upper() == symbol:
                ticker_sentiments.append({
                    "label": ts.get("sentiment_label"),
                    "relevance": ts.get("relevance_score"),
                })

    fred_data = fred_source.get_macro_snapshot(fred_key) if fred_key else {}

    # Use yfinance returns when platform analysis is unavailable (synthetic stock)
    ret_5d  = analysis.get("return_5d_pct")  or fundamentals.get("return_5d_pct")
    ret_20d = analysis.get("return_20d_pct") or fundamentals.get("return_20d_pct")
    eff_price = current_price or fundamentals.get("current_price_yf")

    return {
        "symbol": symbol,
        "platform_analysis": {
            "signal": stock.get("signal"),
            "trend_status": stock.get("trend_status"),
            "signal_score": stock.get("signal_score"),
            "return_5d_pct": ret_5d,
            "return_20d_pct": ret_20d,
            "current_price": eff_price,
            "bullish_factors": (stock.get("bullish_factors") or [])[:5],
            "risk_factors": (stock.get("risk_factors") or [])[:3],
            "summary": stock.get("summary") or "",
            "data_source": "yfinance_only" if stock.get("synthetic") else "platform",
        },
        "technicals": {
            "rsi_14": fundamentals.get("rsi_14"),
            "macd": fundamentals.get("macd"),
            "ma_50d": ma_50d,
            "ma_200d": fundamentals.get("ma_200d"),
            "above_50d_ma": bool(ma_50d and current_price >= ma_50d),
            "above_200d_ma": bool(fundamentals.get("ma_200d") and current_price >= float(fundamentals.get("ma_200d", 0))),
        },
        "fundamentals": {
            "pe_ratio": fundamentals.get("pe_ratio"),
            "analyst_recommendation": fundamentals.get("analyst_recommendation"),
            "next_earnings_date": fundamentals.get("next_earnings_date"),
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
        "insider_activity": insider_source.get_insider_activity(symbol) if insider_source else {},
    }
