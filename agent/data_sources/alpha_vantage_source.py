"""Alpha Vantage technical indicator fetches (RSI-14, MACD 12/26/9).

Free tier: 5 req/min, 500 req/day.
Rate limiter: enforces >= 12s between any two AV calls (module-level, shared
across all symbols) to stay under the 5 req/min ceiling.
Per-symbol cache (1h TTL) means each symbol costs 2 AV calls at most once per hour.
"""
import logging
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

_AV_BASE = "https://www.alphavantage.co/query"
_CACHE_TTL = 3600       # 1 hour per symbol
# 5 req/min -> one call every 12s minimum (shared across all symbols + all indicators)
_MIN_CALL_INTERVAL = 12.0

# module-level: {symbol: {"rsi_14": float, "macd": dict, "cached_at": float}}
_cache: dict[str, dict] = {}
_last_call_at: float = 0.0


def get_technicals(symbol: str, api_key: str) -> dict:
    """Return RSI-14 and MACD for *symbol*. Cached 1h; rate-limited to 5 req/min."""
    if not api_key:
        log.debug("%s: no AV API key — technicals skipped", symbol)
        return {}

    cached = _cache.get(symbol)
    if cached and (time.time() - cached["cached_at"]) < _CACHE_TTL:
        log.debug("%s: using cached AV technicals", symbol)
        return {k: v for k, v in cached.items() if k != "cached_at"}

    result: dict = {}
    rsi = _fetch_rsi(symbol, api_key)
    if rsi is not None:
        result["rsi_14"] = rsi

    macd = _fetch_macd(symbol, api_key)
    if macd:
        result["macd"] = macd

    _cache[symbol] = {**result, "cached_at": time.time()}
    log.debug("%s: AV technicals -> %s", symbol, result)
    return result


def _rate_limit() -> None:
    """Block until the minimum inter-call interval has elapsed."""
    global _last_call_at
    elapsed = time.time() - _last_call_at
    if elapsed < _MIN_CALL_INTERVAL:
        wait = _MIN_CALL_INTERVAL - elapsed
        log.debug("AV rate-limit: sleeping %.1fs", wait)
        time.sleep(wait)
    _last_call_at = time.time()


def _fetch_rsi(symbol: str, api_key: str) -> Optional[float]:
    _rate_limit()
    try:
        resp = requests.get(_AV_BASE, params={
            "function": "RSI",
            "symbol": symbol,
            "interval": "daily",
            "time_period": "14",
            "series_type": "close",
            "apikey": api_key,
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        # AV returns {"Information": "..."} when rate-limited or key is invalid
        if "Information" in data or "Note" in data:
            msg = data.get("Information") or data.get("Note", "")
            log.warning("%s: AV RSI quota message: %s", symbol, msg[:120])
            return None
        series = data.get("Technical Analysis: RSI", {})
        if not series:
            return None
        latest = sorted(series.keys(), reverse=True)[0]
        return round(float(series[latest]["RSI"]), 2)
    except Exception as exc:
        log.warning("%s: AV RSI fetch failed: %s", symbol, exc)
        return None


def _fetch_macd(symbol: str, api_key: str) -> Optional[dict]:
    _rate_limit()
    try:
        resp = requests.get(_AV_BASE, params={
            "function": "MACD",
            "symbol": symbol,
            "interval": "daily",
            "series_type": "close",
            "fastperiod": "12",
            "slowperiod": "26",
            "signalperiod": "9",
            "apikey": api_key,
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if "Information" in data or "Note" in data:
            msg = data.get("Information") or data.get("Note", "")
            log.warning("%s: AV MACD quota message: %s", symbol, msg[:120])
            return None
        series = data.get("Technical Analysis: MACD", {})
        if not series:
            return None
        latest = sorted(series.keys(), reverse=True)[0]
        row = series[latest]
        return {
            "macd": round(float(row["MACD"]), 4),
            "signal": round(float(row["MACD_Signal"]), 4),
            "histogram": round(float(row["MACD_Hist"]), 4),
        }
    except Exception as exc:
        log.warning("%s: AV MACD fetch failed: %s", symbol, exc)
        return None
