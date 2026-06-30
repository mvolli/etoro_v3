"""
src/bot/api/client.py
─────────────────────────────────────────────────────────────────────────────
eToro Public API v1 client — production-ready with retry, logging, and typed
error handling.

All public methods raise APIError on non-2xx HTTP responses.
Transient network errors (Timeout, ConnectionError) are retried up to 3 times
with exponential back-off (2–10 s).
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import requests
from requests.exceptions import ConnectionError, Timeout
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL_V1 = "https://public-api.etoro.com/api/v1"
BASE_URL_V2 = "https://public-api.etoro.com/api/v2"

_TRANSIENT_ERRORS = (Timeout, ConnectionError)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class APIError(Exception):
    """Raised when the eToro API returns a non-2xx HTTP status code.

    Attributes
    ----------
    status_code : int
        HTTP status code returned by the server.
    endpoint : str
        The endpoint path that triggered the error (for diagnostics).
    """

    def __init__(self, message: str, status_code: int, endpoint: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.endpoint = endpoint

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"APIError(status_code={self.status_code!r}, "
            f"endpoint={self.endpoint!r}, message={str(self)!r})"
        )


# ---------------------------------------------------------------------------
# Config helper (simple dataclass so we don't import YAML in this module)
# ---------------------------------------------------------------------------


@dataclass
class ClientConfig:
    """Minimal config consumed by EToroClient.

    Can be constructed directly from a dict loaded from config.yaml:

        cfg = ClientConfig(**yaml_data["api"])
    """

    base_url: str = BASE_URL_V1
    timeout_connect: float = 5.0
    timeout_read: float = 10.0
    retry_attempts: int = 3
    retry_wait_min: float = 2.0
    retry_wait_max: float = 10.0
    # Any extra keys from YAML are silently ignored via __post_init__
    _extra: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        # Allow construction with **yaml["api"] without blowing up on extras
        pass

    @classmethod
    def from_dict(cls, data: dict) -> "ClientConfig":
        """Build from a raw dict, ignoring unknown keys."""
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


# ---------------------------------------------------------------------------
# Retry decorator factory
# ---------------------------------------------------------------------------


def _make_retry(config: ClientConfig):
    """Return a tenacity retry decorator parameterised by *config*."""
    return retry(
        stop=stop_after_attempt(config.retry_attempts),
        wait=wait_exponential(
            multiplier=1,
            min=config.retry_wait_min,
            max=config.retry_wait_max,
        ),
        retry=retry_if_exception_type(_TRANSIENT_ERRORS),
        reraise=True,
    )


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------


class EToroClient:
    """eToro Public API v1/v2 client.

    Parameters
    ----------
    api_key : str
        eToro API key (``x-api-key`` header).
    user_key : str
        eToro user key (``x-user-key`` header).
    config : ClientConfig | None
        Client configuration.  If *None*, defaults are used.

    Examples
    --------
    >>> from bot.api.client import EToroClient, ClientConfig
    >>> client = EToroClient(api_key="...", user_key="...", config=ClientConfig())
    >>> portfolio = client.get_portfolio()
    """

    def __init__(
        self,
        api_key: str,
        user_key: str,
        config: ClientConfig | None = None,
    ) -> None:
        self.config = config or ClientConfig()
        self._timeout = (self.config.timeout_connect, self.config.timeout_read)

        self._session = requests.Session()
        self._session.headers.update(
            {
                "x-api-key": api_key,
                "x-user-key": user_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

        # Build retry-decorated low-level methods at construction time so the
        # decorator picks up the live config values.
        _retry = _make_retry(self.config)
        self._get_raw = _retry(self.__get_raw)
        self._post_raw = _retry(self.__post_raw)
        self._delete_raw = _retry(self.__delete_raw)

        logger.debug(
            "EToroClient initialised — base_url=%s timeout=%s",
            self.config.base_url,
            self._timeout,
        )

    # ------------------------------------------------------------------
    # Private raw HTTP methods (wrapped by retry above)
    # ------------------------------------------------------------------

    def __get_raw(
        self, url: str, params: dict | None = None
    ) -> requests.Response:
        t0 = time.perf_counter()
        # eToro requires a unique x-request-id per call
        import uuid, hashlib
        req_id = hashlib.md5(f"{url}{time.time()}".encode()).hexdigest()
        resp = self._session.get(
            url, params=params, timeout=self._timeout,
            headers={"x-request-id": req_id}
        )
        logger.debug(
            "GET %s params=%s → %s (%.3fs)",
            url,
            params,
            resp.status_code,
            time.perf_counter() - t0,
        )
        return resp

    def __post_raw(self, url: str, body: Any) -> requests.Response:
        t0 = time.perf_counter()
        import hashlib
        req_id = hashlib.md5(f"{url}{body}{time.time()}".encode()).hexdigest()
        resp = self._session.post(
            url, json=body, timeout=self._timeout,
            headers={"x-request-id": req_id}
        )
        logger.debug(
            "POST %s → %s (%.3fs)",
            url,
            resp.status_code,
            time.perf_counter() - t0,
        )
        return resp

    def __delete_raw(self, url: str) -> requests.Response:
        t0 = time.perf_counter()
        resp = self._session.delete(url, timeout=self._timeout)
        logger.debug(
            "DELETE %s → %s (%.3fs)",
            url,
            resp.status_code,
            time.perf_counter() - t0,
        )
        return resp

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _raise_for_status(resp: requests.Response, endpoint: str) -> None:
        """Raise *APIError* if *resp* is not 2xx."""
        if not resp.ok:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text[:200]
            raise APIError(
                message=f"HTTP {resp.status_code} from {endpoint}: {detail}",
                status_code=resp.status_code,
                endpoint=endpoint,
            )

    def _v1_url(self, endpoint: str) -> str:
        base = self.config.base_url.rstrip("/")
        return f"{base}/{endpoint.lstrip('/')}"

    @staticmethod
    def _v2_url(endpoint: str) -> str:
        return f"{BASE_URL_V2}/{endpoint.lstrip('/')}"

    # ------------------------------------------------------------------
    # Public low-level HTTP interface
    # ------------------------------------------------------------------

    def get(self, endpoint: str, params: dict | None = None) -> Any:
        """Issue a GET against a v1 endpoint and return parsed JSON.

        Parameters
        ----------
        endpoint : str
            Path relative to ``BASE_URL_V1`` (leading slash optional).
        params : dict | None
            Query-string parameters.

        Returns
        -------
        Any
            Parsed JSON response body.

        Raises
        ------
        APIError
            On non-2xx responses.
        """
        url = self._v1_url(endpoint)
        resp = self._get_raw(url, params=params)
        self._raise_for_status(resp, endpoint)
        return resp.json()

    def post(self, endpoint: str, body: Any, *, v2: bool = False) -> Any:
        """Issue a POST and return parsed JSON.

        Parameters
        ----------
        endpoint : str
            Path relative to the appropriate base URL.
        body : Any
            JSON-serialisable request body.
        v2 : bool
            If *True*, route to ``BASE_URL_V2`` instead of v1.
        """
        url = self._v2_url(endpoint) if v2 else self._v1_url(endpoint)
        resp = self._post_raw(url, body)
        self._raise_for_status(resp, endpoint)
        return resp.json()

    def delete(self, endpoint: str) -> Any:
        """Issue a DELETE against a v1 endpoint and return parsed JSON."""
        url = self._v1_url(endpoint)
        resp = self._delete_raw(url)
        self._raise_for_status(resp, endpoint)
        # Some DELETE responses return 204 No Content
        if resp.status_code == 204 or not resp.text:
            return {}
        return resp.json()

    # ------------------------------------------------------------------
    # High-level business methods
    # ------------------------------------------------------------------

    def get_portfolio(self) -> dict:
        """Return the full real-money portfolio PnL snapshot.

        Endpoint: GET /trading/info/real/pnl

        Returns
        -------
        dict
            Raw JSON payload from eToro.
        """
        return self.get("/trading/info/real/pnl")

    def get_positions(self) -> list[dict]:
        """Return the list of open positions from the portfolio snapshot.

        Parses ``portfolio["positions"]`` (falls back to empty list if the
        key is absent so callers never need to guard against None).

        Returns
        -------
        list[dict]
            Each dict is one open position as returned by eToro.
        """
        portfolio = self.get_portfolio()
        # eToro nests positions under different keys depending on API version
        positions = (
            portfolio.get("positions")
            or portfolio.get("openPositions")
            or []
        )
        return positions

    def open_position(
        self,
        instrument_id: int,
        amount_usd: float,
        stop_loss_pct: float = 3.0,
        symbol: str = "",
    ) -> dict:
        """Open a new long position on *instrument_id*.

        The stop-loss rate is computed via ``calculate_sl_price()`` from
        ``bot.core.risk``, which always uses a relative percentage and
        enforces crypto-safe SL calculation (prevents near-zero absolute SL).

        Endpoint: POST /api/v2/trading/execution/orders (v2)

        Parameters
        ----------
        instrument_id : int
            eToro instrument identifier.
        amount_usd : float
            Order size in USD.
        stop_loss_pct : float
            Stop-loss percentage below entry price (default 3.0 %).
        symbol : str
            Ticker symbol (e.g. ``"XRP-USD"``).  Used for crypto-safe SL
            calculation and quality-gate checks.  Falls back to
            ``str(instrument_id)`` when empty.

        Returns
        -------
        dict
            Order confirmation payload from eToro, or a dict with
            ``{"success": False, "error": ...}`` if the SL quality gate
            blocks the order.

        Raises
        ------
        APIError
            On non-2xx responses or if the current price cannot be fetched.
        """
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
        from bot.core.risk import calculate_sl_price, check_sl_quality_gate

        # Fetch current price from eligibility info (with fallback to DB)
        try:
            eligibility = self.get_instrument_eligibility(instrument_id)
            current_price: float | None = (
                eligibility.get("lastPrice")
                or eligibility.get("currentPrice")
                or eligibility.get("rate")
            )
        except APIError as exc:
            # Eligibility endpoint may return 404 — fall back to DB price
            logger.warning(
                "get_instrument_eligibility failed for %s (%s) — falling back to DB price",
                instrument_id, exc
            )
            current_price = None
        
        # If no price from API, try to get it from portfolio_snapshot or signals
        if current_price is None:
            import sqlite3
            from pathlib import Path
            
            # Resolve DB path: client.py is at src/bot/api/client.py, DB is at project_root/data/
            project_root = Path(__file__).resolve().parent.parent.parent.parent  # → etoro_v3/
            db_path = project_root / "data" / "trading.db"
            
            try:
                conn = sqlite3.connect(str(db_path))
                conn.row_factory = sqlite3.Row
                
                # Try portfolio_snapshot first (has current_price) — only works for existing positions
                snapshot_rows = conn.execute(
                    "SELECT current_price FROM portfolio_snapshot WHERE instrument_id=? AND current_price > 0 ORDER BY last_synced DESC LIMIT 1",
                    (instrument_id,),
                ).fetchall()
                if snapshot_rows:
                    price = float(snapshot_rows[0]["current_price"])
                    if price > 0:
                        current_price = price
                        logger.info(
                            "Using portfolio_snapshot price for %s: %.6f",
                            instrument_id, current_price
                        )
                
                # Fallback to latest signal price
                if current_price is None:
                    signal_rows = conn.execute(
                        "SELECT price FROM signals WHERE instrument_id=? AND price > 0 ORDER BY generated_at DESC LIMIT 1",
                        (instrument_id,),
                    ).fetchall()
                    if signal_rows:
                        current_price = float(signal_rows[0]["price"])
                        logger.info(
                            "Using signal price for %s: %.6f",
                            instrument_id, current_price
                        )
                
                conn.close()
            except Exception as db_exc:
                logger.warning("DB price lookup failed for %s: %s", instrument_id, db_exc)
        
        if current_price is None:
            raise APIError(
                message=(
                    f"Cannot determine current price for instrument {instrument_id} "
                    f"— eligibility endpoint failed and no DB fallback available"
                ),
                status_code=0,
                endpoint="/trading/info/real/eligibility",
            )

        # Resolve symbol: use provided value or fall back to instrument_id string
        _symbol = symbol or str(instrument_id)

        # Calculate SL price via risk module — always relative for crypto
        stop_loss_rate = calculate_sl_price(current_price, _symbol, sl_pct=stop_loss_pct)

        # SL quality gate: block orders where SL distance > 50% (meaningless SL)
        sl_check = check_sl_quality_gate(current_price, stop_loss_rate, _symbol)
        if not sl_check.allowed:
            logger.warning(
                "open_position BLOCKED by SL quality gate: instrument=%s symbol=%s "
                "price=%.6f sl=%.6f reason=%s",
                instrument_id,
                _symbol,
                current_price,
                stop_loss_rate,
                sl_check.summary(),
            )
            return {"success": False, "error": f"SL Quality Gate: {sl_check.summary()}"}

        body = {
            "transaction": "Buy",
            "instrumentId": instrument_id,
            "amount": amount_usd,
            "leverage": 1,
            "isNoStopLoss": False,
            "stopLossRate": stop_loss_rate,
        }

        logger.debug(
            "open_position instrument=%s symbol=%s amount=%.2f sl_pct=%.1f sl_rate=%.6f",
            instrument_id,
            _symbol,
            amount_usd,
            stop_loss_pct,
            stop_loss_rate,
        )
        return self.post("/trading/execution/orders", body, v2=True)

    def close_position(self, position_id: str | int, instrument_id: int) -> dict:
        """Close an existing open position.

        Endpoint:
            POST /trading/execution/market-close-orders/positions/{position_id}

        Parameters
        ----------
        position_id : str | int
            The eToro position identifier.
        instrument_id : int
            The instrument ID (included in the body for audit purposes).

        Returns
        -------
        dict
            Close-order confirmation payload.
        """
        endpoint = (
            f"/trading/execution/market-close-orders/positions/{position_id}"
        )
        body = {"instrumentId": instrument_id}
        logger.debug(
            "close_position position_id=%s instrument_id=%s",
            position_id,
            instrument_id,
        )
        return self.post(endpoint, body)

    def get_instrument_eligibility(self, instrument_id: int) -> dict:
        """Return eligibility and pricing info for a single instrument.

        Tries v2 endpoint first (primary), falls back to v1 on 404.

        Endpoints:
            GET /api/v2/trading/info/real/eligibility?instrumentId={id}
            GET /trading/info/real/eligibility?instrumentId={id}  (v1 fallback)
        """
        try:
            return self.get(
                "/trading/info/real/eligibility",
                params={"instrumentId": instrument_id},
            )
        except APIError as exc:
            if exc.status_code == 404:
                logger.warning(
                    "v1 eligibility 404 for %d — trying v2 endpoint",
                    instrument_id,
                )
                try:
                    return self.post(
                        "/trading/info/real/eligibility",
                        {"instrumentId": instrument_id},
                        v2=True,
                    )
                except APIError:
                    pass
                # Both failed — re-raise original
                raise exc
            raise

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Release the underlying ``requests.Session``."""
        self._session.close()

    def __enter__(self) -> "EToroClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
