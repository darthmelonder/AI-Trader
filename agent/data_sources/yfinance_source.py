"""Yahoo Finance fundamentals and technical indicators via yfinance + pandas.

RSI-14 and MACD (12/26/9) are computed locally from 90-day price history —
no Alpha Vantage or other external API required for technicals.

Rate limiter: >= 2s between yfinance calls (module-level). Retries up to 3
times with exponential backoff on connection/throttle errors. Results cached
1h per symbol so repeated scans within an hour are free.
"""
import logging
import time
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)

_CACHE_TTL = 3600        # 1 hour per symbol
_MIN_CALL_INTERVAL = 2.0
_MAX_RETRIES = 3

# {symbol: {data..., "cached_at": float}}
_cache: dict[str, dict] = {}
_last_call_at: float = 0.0


def get_fundamentals(symbol: str) -> dict:
    """Return fundamentals + technical indicators for *symbol*.

    Keys returned: pe_ratio, ma_50d, ma_200d, current_price_yf,
    analyst_recommendation, next_earnings_date, days_to_earnings,
    rsi_14, macd (dict with macd/signal/histogram keys).
    Missing fields are None rather than omitted.
    """
    cached = _cache.get(symbol)
    if cached and (time.time() - cached["cached_at"]) < _CACHE_TTL:
        log.debug("%s: using cached yfinance data", symbol)
        return {k: v for k, v in cached.items() if k != "cached_at"}

    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance not installed — data unavailable for %s", symbol)
        return {}

    result = _fetch_with_retry(symbol, yf)
    _cache[symbol] = {**result, "cached_at": time.time()}
    return result


def _rate_limit() -> None:
    global _last_call_at
    elapsed = time.time() - _last_call_at
    if elapsed < _MIN_CALL_INTERVAL:
        time.sleep(_MIN_CALL_INTERVAL - elapsed)
    _last_call_at = time.time()


def _fetch_with_retry(symbol: str, yf) -> dict:
    for attempt in range(1, _MAX_RETRIES + 1):
        _rate_limit()
        try:
            result = _fetch(symbol, yf)
            log.debug("%s: yfinance -> %s", symbol, result)
            return result
        except Exception as exc:
            err = str(exc).lower()
            if attempt < _MAX_RETRIES and any(
                k in err for k in ("429", "too many", "connection", "timeout", "reset")
            ):
                wait = 2 ** attempt  # 2s, 4s, 8s
                log.warning(
                    "%s: yfinance error (attempt %d/%d), retrying in %ds: %s",
                    symbol, attempt, _MAX_RETRIES, wait, exc,
                )
                time.sleep(wait)
            else:
                log.warning("%s: yfinance fetch failed: %s", symbol, exc)
                return {}
    return {}


def _fetch(symbol: str, yf) -> dict:
    import pandas as pd

    ticker = yf.Ticker(symbol)
    info = ticker.info or {}

    pe_ratio = info.get("trailingPE") or info.get("forwardPE")
    ma_50d = info.get("fiftyDayAverage")
    ma_200d = info.get("twoHundredDayAverage")
    current_price = info.get("currentPrice") or info.get("regularMarketPrice")
    recommendation = (info.get("recommendationKey") or "").lower() or None

    # Earnings date
    next_earnings_date = None
    days_to_earnings = None
    try:
        cal = ticker.calendar
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date")
            if ed is not None:
                ed_list = list(ed) if hasattr(ed, "__iter__") and not isinstance(ed, pd.Timestamp) else [ed]
                if ed_list:
                    first = ed_list[0]
                    date_str = (
                        first.to_pydatetime().date().isoformat()
                        if hasattr(first, "to_pydatetime")
                        else str(first)[:10]
                    )
                    next_earnings_date = date_str
                    days_to_earnings = (datetime.fromisoformat(date_str) - datetime.now()).days
    except Exception as exc:
        log.debug("%s: earnings date parse failed: %s", symbol, exc)

    # Technical indicators from 90-day price history
    rsi_14 = None
    macd_data = None
    bb_lower = None
    bb_upper = None
    bb_pct = None          # 0.0 = price at lower band, 1.0 = at upper band, <0 = below lower
    at_lower_bb = False
    drop_from_20d_high_pct = None
    return_5d_pct = None
    return_20d_pct = None
    try:
        hist = ticker.history(period="90d")
        if len(hist) >= 30:
            closes = hist["Close"]
            rsi_14 = _compute_rsi(closes, period=14)
            macd_data = _compute_macd(closes)
        if len(hist) >= 20:
            closes = hist["Close"]
            price_now = float(closes.iloc[-1])
            sma_20 = closes.rolling(20).mean()
            std_20 = closes.rolling(20).std()
            bb_upper_val = float(sma_20.iloc[-1] + 2 * std_20.iloc[-1])
            bb_lower_val = float(sma_20.iloc[-1] - 2 * std_20.iloc[-1])
            bb_range = bb_upper_val - bb_lower_val
            bb_lower = round(bb_lower_val, 2)
            bb_upper = round(bb_upper_val, 2)
            bb_pct = round((price_now - bb_lower_val) / bb_range, 3) if bb_range > 0 else None
            at_lower_bb = price_now <= bb_lower_val
            high_20d = float(closes.rolling(20).max().iloc[-1])
            drop_from_20d_high_pct = round((high_20d - price_now) / high_20d * 100, 2)
        if len(hist) >= 6:
            closes = hist["Close"]
            p_now = float(closes.iloc[-1])
            p_5d  = float(closes.iloc[-6])
            p_20d = float(closes.iloc[-21]) if len(closes) >= 21 else float(closes.iloc[0])
            return_5d_pct  = round((p_now - p_5d)  / p_5d  * 100, 2) if p_5d  > 0 else None
            return_20d_pct = round((p_now - p_20d) / p_20d * 100, 2) if p_20d > 0 else None
    except Exception as exc:
        log.debug("%s: technicals computation failed: %s", symbol, exc)

    return {
        "pe_ratio": round(pe_ratio, 2) if pe_ratio else None,
        "ma_50d": round(ma_50d, 2) if ma_50d else None,
        "ma_200d": round(ma_200d, 2) if ma_200d else None,
        "current_price_yf": round(current_price, 2) if current_price else None,
        "analyst_recommendation": recommendation,
        "next_earnings_date": next_earnings_date,
        "days_to_earnings": days_to_earnings,
        "rsi_14": rsi_14,
        "macd": macd_data,
        "bb_lower": bb_lower,
        "bb_upper": bb_upper,
        "bb_pct": bb_pct,
        "at_lower_bb": at_lower_bb,
        "drop_from_20d_high_pct": drop_from_20d_high_pct,
        "return_5d_pct": return_5d_pct,
        "return_20d_pct": return_20d_pct,
    }


def _compute_rsi(closes, period: int = 14) -> Optional[float]:
    """Wilder's RSI from a pandas Series of closing prices."""
    try:
        delta = closes.diff().dropna()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, float("inf"))
        rsi = 100 - (100 / (1 + rs))
        val = float(rsi.iloc[-1])
        return round(val, 2) if not (val != val) else None  # guard NaN
    except Exception:
        return None


def _compute_macd(closes, fast: int = 12, slow: int = 26, signal: int = 9) -> Optional[dict]:
    """MACD (fast EMA - slow EMA), signal line, histogram, and previous histogram for slope."""
    try:
        ema_fast = closes.ewm(span=fast, adjust=False).mean()
        ema_slow = closes.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        histogram = macd_line - signal_line
        hist_now = round(float(histogram.iloc[-1]), 4)
        hist_prev = round(float(histogram.iloc[-2]), 4) if len(histogram) >= 2 else hist_now
        return {
            "macd": round(float(macd_line.iloc[-1]), 4),
            "signal": round(float(signal_line.iloc[-1]), 4),
            "histogram": hist_now,
            "histogram_prev": hist_prev,   # yesterday's histogram — used to detect slope reversal
        }
    except Exception:
        return None
