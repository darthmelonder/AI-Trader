"""FRED macroeconomic data: Fed Funds Rate, 10Y Treasury, CPI YoY.

Data is cached for 24 hours — macro indicators change slowly and
FRED enforces rate limits on free-tier keys.
"""
import logging
import time

log = logging.getLogger(__name__)

_CACHE_TTL = 86400  # 24 hours
_cache: dict = {}
_cached_at: float = 0.0


def get_macro_snapshot(api_key: str) -> dict:
    """Return the latest FRED macro snapshot. Cached 24h.

    Returns a dict with keys: fed_funds_rate, treasury_10y, cpi_yoy_pct.
    Returns an empty dict if fredapi is not installed or the key is missing.
    """
    global _cached_at

    if not api_key:
        log.debug("FRED: no API key — macro snapshot skipped")
        return {}

    if _cache and (time.time() - _cached_at) < _CACHE_TTL:
        log.debug("FRED: using cached macro snapshot")
        return dict(_cache)

    result: dict = {}
    try:
        from fredapi import Fred
        fred = Fred(api_key=api_key)

        fedfunds = fred.get_series("FEDFUNDS")
        if not fedfunds.empty:
            result["fed_funds_rate"] = round(float(fedfunds.iloc[-1]), 2)

        dgs10 = fred.get_series("DGS10")
        if not dgs10.empty:
            result["treasury_10y"] = round(float(dgs10.dropna().iloc[-1]), 2)

        cpi = fred.get_series("CPIAUCSL")
        if len(cpi) >= 13:
            latest = float(cpi.iloc[-1])
            year_ago = float(cpi.iloc[-13])
            result["cpi_yoy_pct"] = round((latest - year_ago) / year_ago * 100, 2)

    except ImportError:
        log.warning("fredapi not installed — FRED macro data unavailable")
    except Exception as exc:
        log.warning("FRED fetch failed: %s", exc)

    _cache.clear()
    _cache.update(result)
    _cached_at = time.time()
    if result:
        log.info("FRED macro snapshot: %s", result)
    return dict(result)
