"""SEC EDGAR Form 4 insider buying/selling activity.

No API key required. Uses two free EDGAR endpoints:
  1. https://www.sec.gov/files/company_tickers.json  — ticker -> CIK map (once per process)
  2. https://data.sec.gov/submissions/CIK{cik}.json  — filing history (24h cache per symbol)
  3. Primary Form 4 XML files (up to 3 per symbol per 24h) — parsed for buy/sell direction

Rate limit: EDGAR enforces 10 req/sec. Enforce >= 0.15s between calls.
Required: User-Agent header (SEC blocks anonymous requests).

Returns per symbol:
  {
    "buy_count":     int,        # insider stock purchases in last 30 days
    "sell_count":    int,        # insider stock sales
    "net_shares":    int,        # shares bought minus shares sold
    "csuite_bought": bool,       # CEO / CFO / President / Director bought
    "last_buy_date": str | None, # ISO date of most recent purchase
  }
"""
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Optional

import requests

log = logging.getLogger(__name__)

_EDGAR_BASE = "https://data.sec.gov"
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_CACHE_TTL = 86400      # 24h per symbol
_MIN_CALL_INTERVAL = 0.25  # 4 req/sec per container; two containers = 8 req/sec < EDGAR's 10 limit
_MAX_FORM4_PER_SYMBOL = 3   # parse at most 3 recent Form 4 XMLs per symbol
_LOOKBACK_DAYS = 30

_USER_AGENT = "JatinTradingBot darthmelonder@gmail.com"

# Module-level caches
_tickers_map: dict[str, int] = {}          # ticker -> CIK (loaded once)
_cache: dict[str, dict] = {}               # symbol -> {data, "cached_at"}
_last_call_at: float = 0.0


def get_insider_activity(symbol: str) -> dict:
    """Return insider buy/sell summary for *symbol* over the last 30 days.

    Cached 24h. Returns {} on any fetch failure — non-blocking.
    """
    cached = _cache.get(symbol)
    if cached and (time.time() - cached["cached_at"]) < _CACHE_TTL:
        log.debug("%s: using cached insider data", symbol)
        return {k: v for k, v in cached.items() if k != "cached_at"}

    result = _fetch_insider_activity(symbol)
    _cache[symbol] = {**result, "cached_at": time.time()}
    if result:
        log.debug(
            "%s: insider activity -> buys=%d sells=%d csuite=%s",
            symbol, result.get("buy_count", 0), result.get("sell_count", 0),
            result.get("csuite_bought", False),
        )
    return result


# ── internal ─────────────────────────────────────────────────────────────────

def _rate_limit() -> None:
    global _last_call_at
    elapsed = time.time() - _last_call_at
    if elapsed < _MIN_CALL_INTERVAL:
        time.sleep(_MIN_CALL_INTERVAL - elapsed)
    _last_call_at = time.time()


def _get(url: str) -> Optional[requests.Response]:
    _rate_limit()
    try:
        resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=15)
        resp.raise_for_status()
        return resp
    except Exception as exc:
        log.warning("EDGAR fetch failed %s: %s", url, exc)
        return None


def _load_cik(symbol: str) -> Optional[int]:
    """Return the CIK for *symbol*, loading the ticker map if not yet cached."""
    global _tickers_map
    if not _tickers_map:
        resp = _get(_TICKERS_URL)
        if not resp:
            return None
        data = resp.json()
        # Format: {"0": {"cik_str": 320193, "ticker": "AAPL", ...}, ...}
        _tickers_map = {
            v["ticker"].upper(): int(v["cik_str"])
            for v in data.values()
            if "ticker" in v and "cik_str" in v
        }
        log.debug("Loaded EDGAR ticker map: %d entries", len(_tickers_map))
    return _tickers_map.get(symbol.upper())


def _fetch_insider_activity(symbol: str) -> dict:
    cik = _load_cik(symbol)
    if not cik:
        log.debug("%s: CIK not found in EDGAR ticker map", symbol)
        return {}

    resp = _get(f"{_EDGAR_BASE}/submissions/CIK{cik:010d}.json")
    if not resp:
        return {}

    data = resp.json()
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    cutoff = datetime.now() - timedelta(days=_LOOKBACK_DAYS)
    form4_entries = [
        (date_str, acc, doc)
        for form, date_str, acc, doc in zip(forms, dates, accessions, primary_docs)
        if form == "4" and _parse_date(date_str) >= cutoff
    ]

    if not form4_entries:
        return {"buy_count": 0, "sell_count": 0, "net_shares": 0,
                "csuite_bought": False, "last_buy_date": None}

    buy_count = 0
    sell_count = 0
    net_shares = 0
    csuite_bought = False
    last_buy_date = None

    for date_str, accession, primary_doc in form4_entries[:_MAX_FORM4_PER_SYMBOL]:
        txns = _parse_form4(cik, accession, primary_doc)
        for t in txns:
            if t["code"] == "A":
                buy_count += 1
                net_shares += t["shares"]
                if last_buy_date is None:
                    last_buy_date = date_str
                if t["csuite"]:
                    csuite_bought = True
            elif t["code"] == "D":
                sell_count += 1
                net_shares -= t["shares"]

    return {
        "buy_count": buy_count,
        "sell_count": sell_count,
        "net_shares": net_shares,
        "csuite_bought": csuite_bought,
        "last_buy_date": last_buy_date,
    }


def _parse_form4(cik: int, accession: str, primary_doc: str) -> list[dict]:
    """Fetch and parse a Form 4 XML. Returns list of {code, shares, csuite} dicts."""
    accession_clean = accession.replace("-", "")
    # primaryDocument may include a stylesheet prefix (e.g. "xslF345X06/wk-form4.xml")
    # Strip any leading path components — the file lives directly in the accession directory.
    filename = primary_doc.split("/")[-1]
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_clean}/{filename}"
    resp = _get(url)
    if not resp or not resp.text.strip().startswith("<"):
        return []
    try:
        return _extract_transactions(resp.text)
    except ET.ParseError as exc:
        log.debug("Form 4 XML parse error (%s): %s", accession, exc)
        return []


def _extract_transactions(xml_text: str) -> list[dict]:
    """Extract buy/sell transactions and officer info from a Form 4 XML string."""
    root = ET.fromstring(xml_text)

    # Determine if any reporting owner is C-suite
    csuite = False
    for owner in root.findall(".//reportingOwner"):
        rel = owner.find("reportingOwnerRelationship")
        if rel is not None:
            is_dir = (rel.findtext("isDirector") or "0").strip() == "1"
            title = (rel.findtext("officerTitle") or "").upper()
            is_exec = any(k in title for k in ("CHIEF", "PRESIDENT", "CFO", "CEO", "COO", "CTO"))
            if is_dir or is_exec:
                csuite = True
                break

    results = []
    for txn in root.findall(".//nonDerivativeTransaction"):
        amounts = txn.find("transactionAmounts")
        if amounts is None:
            continue
        code_el = amounts.find("transactionAcquiredDisposedCode/value")
        shares_el = amounts.find("transactionShares/value")
        if code_el is None or shares_el is None:
            continue
        try:
            shares = abs(float(shares_el.text or "0"))
        except ValueError:
            shares = 0.0
        results.append({
            "code": (code_el.text or "").strip().upper(),
            "shares": int(shares),
            "csuite": csuite,
        })
    return results


def _parse_date(date_str: str) -> datetime:
    try:
        return datetime.fromisoformat(date_str)
    except ValueError:
        return datetime.min
