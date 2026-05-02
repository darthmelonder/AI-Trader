"""Gemini-powered market analyst for the LLM Swing strategy.

Model: gemini-3.1-flash-lite (free tier).

Rate strategy:
  - Enforce >= 10s between calls (~6 RPM) to stay within the token-per-minute
    ceiling (large JSON contexts spike TPM quickly).
  - On 429, retry up to 3 times honouring the server-suggested retry delay.
  - 24h per-symbol cache keeps daily call count well under 500.

Single public class: LLMAnalyst.analyze(symbol, context) -> decision dict.
"""
import json
import logging
import re
import time

from google import genai
from google.genai import types

log = logging.getLogger(__name__)

_MODEL = "gemini-3.1-flash-lite-preview"
_MAX_OUTPUT_TOKENS = 512
_MIN_CALL_INTERVAL = 10.0  # >= 10s between calls -> ~6 RPM
_MAX_RETRIES = 3

_SYSTEM = """\
You are a disciplined swing-trading analyst. Evaluate whether a stock is worth entering as a swing trade targeting a 1-4 week hold (up to 30 days).

You will receive structured market data. Respond ONLY with valid JSON - no commentary, no markdown fences:

{
  "decision": "buy | skip | sell",
  "confidence": <float 0.0-1.0>,
  "holding_horizon_days": <int 7-30>,
  "thesis": "<2-3 sentence rationale>",
  "key_risks": ["<risk1>", "<risk2>"],
  "target_return_pct": <float>,
  "stop_loss_pct": <float>
}

Guidelines:
- Output "buy" only if confidence >= 0.70 and the risk/reward is clearly favourable.
- Output "sell" if the stock looks positioned to decline over the next 1-4 weeks.
- Output "skip" when data is thin or the setup is ambiguous.
- Set target_return_pct to a realistic 1-4 week gain (typically 5-20%).
- Set stop_loss_pct to a tight but sane level (typically 6-12%).
- Weight evidence: technicals 40%, fundamentals 30%, macro 30%.
- Never invent data not present in the input.
"""


class LLMAnalyst:
    def __init__(self, api_key: str, cache_ttl_hours: float = 24.0):
        self._client = genai.Client(api_key=api_key)
        self._cache_ttl = cache_ttl_hours * 3600
        # {symbol: {"decision": dict, "cached_at": float}}
        self._cache: dict[str, dict] = {}
        self._last_call_at: float = 0.0

    def analyze(self, symbol: str, context: dict) -> dict:
        """Return LLM decision for *symbol*. Uses in-memory cache if fresh."""
        entry = self._cache.get(symbol)
        if entry:
            age = time.time() - entry["cached_at"]
            if age < self._cache_ttl:
                log.debug("%s: LLM cache hit (age=%.0fs)", symbol, age)
                return entry["decision"]

        log.info("%s: calling Gemini for swing analysis", symbol)
        decision = self._call_llm(symbol, context)
        self._cache[symbol] = {"decision": decision, "cached_at": time.time()}
        return decision

    def invalidate(self, symbol: str) -> None:
        """Force a fresh LLM call for *symbol* on the next analyze() call."""
        self._cache.pop(symbol, None)

    def _call_llm(self, symbol: str, context: dict) -> dict:
        # Enforce minimum gap between calls
        elapsed = time.time() - self._last_call_at
        if elapsed < _MIN_CALL_INTERVAL:
            time.sleep(_MIN_CALL_INTERVAL - elapsed)

        user_msg = (
            f"Analyze this stock for a swing trade entry decision:\n\n"
            f"{json.dumps(_compact_context(context), default=str)}"
        )

        raw = ""
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            self._last_call_at = time.time()
            try:
                response = self._client.models.generate_content(
                    model=_MODEL,
                    config=types.GenerateContentConfig(
                        system_instruction=_SYSTEM,
                        max_output_tokens=_MAX_OUTPUT_TOKENS,
                        temperature=0.1,
                    ),
                    contents=user_msg,
                )
                raw = response.text.strip()
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                err_str = str(exc)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    m = re.search(r"retryDelay[^0-9]*(\d+)s", err_str)
                    wait = float(m.group(1)) + 2 if m else 15.0
                    log.warning(
                        "%s: Gemini 429 (attempt %d/%d) — waiting %.0fs",
                        symbol, attempt, _MAX_RETRIES, wait,
                    )
                    time.sleep(wait)
                    self._last_call_at = time.time()
                else:
                    raise

        if last_exc is not None:
            raise ValueError(
                f"Gemini rate-limited after {_MAX_RETRIES} retries for {symbol}"
            ) from last_exc

        # Strip accidental markdown code fences
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw

        try:
            decision = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Gemini returned non-JSON for {symbol}: {exc}\nRaw: {raw[:200]}"
            ) from exc

        required = {"decision", "confidence", "thesis", "target_return_pct", "stop_loss_pct"}
        missing = required - set(decision.keys())
        if missing:
            raise ValueError(f"Gemini response missing fields {missing} for {symbol}")

        log.info(
            "%s: Gemini -> decision=%s confidence=%.2f horizon=%sd target=+%.1f%% stop=-%.1f%%",
            symbol,
            decision["decision"],
            float(decision.get("confidence", 0)),
            decision.get("holding_horizon_days", "?"),
            float(decision.get("target_return_pct", 0)),
            float(decision.get("stop_loss_pct", 0)),
        )
        return decision


def _compact_context(ctx: dict) -> dict:
    """Trim verbose fields to reduce token count sent to Gemini."""
    pa = ctx.get("platform_analysis", {})
    tech = ctx.get("technicals", {})
    fund = ctx.get("fundamentals", {})
    macro = ctx.get("macro", {})
    return {
        "symbol": ctx.get("symbol"),
        "signal": pa.get("signal"),
        "trend": pa.get("trend_status"),
        "score": pa.get("signal_score"),
        "ret_5d": pa.get("return_5d_pct"),
        "ret_20d": pa.get("return_20d_pct"),
        "price": pa.get("current_price"),
        "bullish": (pa.get("bullish_factors") or [])[:3],
        "risks_platform": (pa.get("risk_factors") or [])[:2],
        "summary": (pa.get("summary") or "")[:200],
        "rsi": tech.get("rsi_14"),
        "macd_hist": (tech.get("macd") or {}).get("histogram"),
        "above_50ma": tech.get("above_50d_ma"),
        "above_200ma": tech.get("above_200d_ma"),
        "pe": fund.get("pe_ratio"),
        "analyst": fund.get("analyst_recommendation"),
        "days_to_earnings": fund.get("days_to_earnings"),
        "macro_verdict": macro.get("platform_verdict"),
        "fed_rate": macro.get("fed_funds_rate"),
        "treasury_10y": macro.get("treasury_10y"),
        "cpi_yoy": macro.get("cpi_yoy_pct"),
        "news": ctx.get("news_sentiment"),
    }
