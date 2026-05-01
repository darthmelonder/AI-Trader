import time
import logging
from typing import Optional

import requests

log = logging.getLogger(__name__)

_RETRY_DELAYS = (2, 4, 8)


class ClientError(Exception):
    pass


class AI4TradeClient:
    def __init__(self, base_url: str, token: Optional[str] = None, dry_run: bool = False):
        self.base_url = base_url.rstrip("/")
        self.dry_run = dry_run
        self._session = requests.Session()
        if token:
            self.set_token(token)

    def set_token(self, token: str) -> None:
        self._session.headers.update({"Authorization": f"Bearer {token}"})

    # ── internal ───────────────────────────────────────────────────────────

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        url = f"{self.base_url}{path}"
        for attempt, delay in enumerate((*_RETRY_DELAYS, None), 1):
            try:
                resp = self._session.get(url, params=params, timeout=15)
                if resp.status_code < 500:
                    resp.raise_for_status()  # 4xx → HTTPError, not retried
                    return resp.json()
                log.warning("GET %s → %d (attempt %d)", path, resp.status_code, attempt)
            except requests.HTTPError as exc:
                # 4xx errors are client errors — retrying won't help
                raise ClientError(f"GET {path} → HTTP {exc.response.status_code}") from exc
            except requests.RequestException as exc:
                log.warning("GET %s failed (attempt %d): %s", path, attempt, exc)
            if delay is None:
                raise ClientError(f"GET {path} failed after {len(_RETRY_DELAYS)+1} attempts")
            time.sleep(delay)

    def _post(self, path: str, payload: dict) -> dict:
        url = f"{self.base_url}{path}"
        if self.dry_run:
            log.info("[DRY RUN] Would POST %s: %s", path, payload)
            return {"dry_run": True}
        for attempt, delay in enumerate((*_RETRY_DELAYS, None), 1):
            try:
                resp = self._session.post(url, json=payload, timeout=15)
                if resp.status_code < 500:
                    resp.raise_for_status()
                    return resp.json()
                log.warning("POST %s → %d (attempt %d)", path, resp.status_code, attempt)
            except requests.HTTPError as exc:
                raise ClientError(f"POST {path} → HTTP {exc.response.status_code}") from exc
            except requests.RequestException as exc:
                log.warning("POST %s failed (attempt %d): %s", path, attempt, exc)
            if delay is None:
                raise ClientError(f"POST {path} failed after {len(_RETRY_DELAYS)+1} attempts")
            time.sleep(delay)

    # ── auth ──────────────────────────────────────────────────────────────

    def register(self, name: str, email: str, password: str) -> dict:
        if self.dry_run:
            log.info("[DRY RUN] Would register agent '%s'", name)
            return {"dry_run": True, "token": "dry-run-token", "agent_id": 0}
        return self._post("/claw/agents/selfRegister", {
            "name": name, "email": email, "password": password,
        })

    def login(self, email: str, password: str) -> dict:
        return self._post("/claw/agents/login", {"email": email, "password": password})

    def me(self) -> dict:
        return self._get("/claw/agents/me")

    def heartbeat(self, agent_id: int) -> dict:
        return self._post("/claw/agents/heartbeat", {"agent_id": agent_id, "status": "alive"})

    # ── market intel ──────────────────────────────────────────────────────

    def macro_signals(self) -> dict:
        return self._get("/market-intel/macro-signals")

    def stock_latest(self, symbol: str) -> dict:
        return self._get(f"/market-intel/stocks/{symbol}/latest")

    def featured_stocks(self, limit: int = 12) -> dict:
        return self._get("/market-intel/stocks/featured", {"limit": limit})

    def news(self, category: str = "equities", limit: int = 5) -> dict:
        return self._get("/market-intel/news", {"category": category, "limit": limit})

    def price(self, symbol: str) -> dict:
        return self._get("/price", {"symbol": symbol, "market": "us-stock"})

    # ── positions ─────────────────────────────────────────────────────────

    def positions(self) -> dict:
        return self._get("/positions")

    # ── signals ───────────────────────────────────────────────────────────

    def publish_realtime(
        self,
        action: str,
        symbol: str,
        quantity: float,
        content: str = "",
    ) -> dict:
        return self._post("/signals/realtime", {
            "market": "us-stock",
            "action": action,
            "symbol": symbol,
            "price": 0,
            "quantity": quantity,
            "content": content,
            "executed_at": "now",
        })

    def publish_strategy(self, title: str, content: str, symbols: list) -> dict:
        return self._post("/signals/strategy", {
            "market": "us-stock",
            "title": title,
            "content": content,
            "symbols": ",".join(symbols),
            "tags": "momentum,macro-alignment,nasdaq",
        })
