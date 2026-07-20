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

    def get_trade_history(
        self,
        min_date: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> list[dict]:
        """Return closed trades from eToro history.

        Endpoint: GET /trading/info/trade/history

        Parameters
        ----------
        min_date : str | None
            Start date (``YYYY-MM-DD``). Trades from this date onwards are
            returned. If *None*, defaults to 90 days ago.
        page : int
            Page number for pagination (default: 1).
        page_size : int
            Number of trades per page (default: 50, max: 100).

        Returns
        -------
        list[dict]
            Each dict contains: ``orderId``, ``positionId``, ``instrumentId``,
            ``isBuy``, ``openRate``, ``closeRate``, ``openTimestamp``,
            ``closeTimestamp``, ``units``, ``initialInvestment``, ``investment``,
            ``netProfit``, ``fees``, ``leverage``, ``stopLossRate``,
            ``takeProfitRate``, ``trailingStopLoss``.

        Raises
        ------
        APIError
            On non-2xx responses.
        """
        from datetime import date, timedelta
        if min_date is None:
            min_date = (date.today() - timedelta(days=90)).isoformat()

        resp = self.get(
            "/trading/info/trade/history",
            params={
                "minDate": min_date,
                "page": page,
                "pageSize": min(page_size, 100),
            },
        )
        # API returns a list directly
        if isinstance(resp, list):
            return resp
        # Some responses wrap in a dict — try common wrapper keys
        if isinstance(resp, dict):
            for key in ("trades", "data", "items"):
                if key in resp and isinstance(resp[key], list):
                    return resp[key]
        return []

    def get_instrument_metadata(self, instrument_id: int) -> dict:
        """Fetch live instrument metadata (symbol/name/exchange) by ID.

        Endpoint: GET /market-data/instruments?instrumentIds={id}

        Used as a pre-flight identity check before opening a position, to
        catch a stale or wrong local instrument_id → symbol mapping BEFORE
        an order is built for it (see: the DOT-USD incident, where the
        watchlist held a Futures contract ID instead of the Spot ID for
        the same nominal symbol — same symbol string, different real
        instrument, silently accepted orders that never became positions).

        Returns {} on any lookup failure (fail-open at the lookup level —
        see verify_instrument_identity() for how that's handled).

        Note: field names in the response (``internalSymbolFull`` vs
        ``symbol`` vs ``ticker``) are based on public eToro API docs, not
        a live-verified response shape. verify_instrument_identity() checks
        multiple candidate field names defensively for this reason.
        """
        try:
            resp = self.get(
                "/market-data/instruments",
                params={"instrumentIds": str(instrument_id)},
            )
        except APIError as exc:
            logger.warning(
                "get_instrument_metadata failed for %s: %s", instrument_id, exc
            )
            return {}
        except Exception as exc:
            logger.warning(
                "get_instrument_metadata unexpected error for %s: %s",
                instrument_id, exc,
            )
            return {}

        items = resp
        if isinstance(resp, dict):
            # Live API returns 'instrumentDisplayDatas' (verified 2026-07-01)
            items = resp.get("instrumentDisplayDatas") or resp.get("instruments") or resp.get("data") or []
        if not isinstance(items, list):
            return {}

        for item in items:
            iid = (
                item.get("instrumentID")
                or item.get("instrumentId")
            )
            try:
                if iid is not None and int(iid) == int(instrument_id):
                    return item
            except (TypeError, ValueError):
                continue
        return items[0] if items else {}

    def get_instruments_metadata_batch(
        self, instrument_ids: list[int], chunk_size: int = 50
    ) -> dict[int, dict]:
        """Fetch live metadata for MANY instrument IDs in as few requests as
        possible, instead of one call per ID.

        The eToro API accepts a comma-separated list for ``instrumentIds``
        (documented at builders.etoro.com/blog/developers-guide-to-instrument-
        discovery and multiple third-party API references — e.g.
        ``instrumentIds=1001,1002,1003``). This is a huge win for bulk
        auditing: checking ~14,000 instruments one-by-one at ~1 req/s would
        take hours; batched at *chunk_size* IDs per request, it's minutes.

        Parameters
        ----------
        instrument_ids : list[int]
            IDs to look up. Duplicates are fine (deduplicated internally).
        chunk_size : int
            Max IDs per request. 50 is a conservative default — no
            documented hard limit was found, but very long comma-separated
            query strings risk URL-length issues on some server/proxy
            configs, so this stays deliberately modest rather than
            maximal. Tune down if requests start failing with 4xx for
            reasons other than 429.

        Returns
        -------
        dict[int, dict]
            ``{instrument_id: metadata_dict}`` for every ID that could be
            resolved. IDs that failed or weren't found are simply absent
            from the result — callers should treat a missing key the same
            way get_instrument_metadata() treats an empty dict (fail-open
            at the lookup level, never raises).

        Raises
        ------
        APIError
            Propagated as-is (including status_code=429) so callers can
            implement their own backoff/retry strategy per chunk — unlike
            get_instrument_metadata(), which swallows errors internally,
            this method intentionally lets 429s surface so bulk callers
            can back off deliberately instead of silently losing data.
        """
        unique_ids = sorted(set(int(i) for i in instrument_ids))
        result: dict[int, dict] = {}

        for start in range(0, len(unique_ids), chunk_size):
            chunk = unique_ids[start : start + chunk_size]
            ids_param = ",".join(str(i) for i in chunk)

            resp = self.get(
                "/market-data/instruments",
                params={"instrumentIds": ids_param},
            )

            items = resp
            if isinstance(resp, dict):
                items = resp.get("instrumentDisplayDatas") or resp.get("instruments") or resp.get("data") or []
            if not isinstance(items, list):
                continue

            for item in items:
                iid = item.get("instrumentID") or item.get("instrumentId")
                try:
                    if iid is not None:
                        result[int(iid)] = item
                except (TypeError, ValueError):
                    continue

        return result

    @staticmethod
    def _normalize_symbol_for_comparison(sym: str) -> str:
        """Loosely normalize a ticker for identity comparison.

        Strips common quote-currency suffixes (BTC-USD ↔ BTC) and
        upper-cases, so e.g. a local 'DOT-USD' and a live API 'DOT'
        response are recognised as the same underlying instrument.
        """
        if not sym:
            return ""
        s = sym.upper().strip()
        for suffix in ("-USD", "/USD", "USD"):
            if s.endswith(suffix) and len(s) > len(suffix):
                s = s[: -len(suffix)]
                break
        return s

    def verify_instrument_identity(
        self, instrument_id: int, expected_symbol: str
    ) -> tuple[bool, str]:
        """Verify *instrument_id* actually resolves to *expected_symbol* on
        the live eToro API before an order is built for it.

        Fail-open (ok=True) on metadata-lookup failure — a live endpoint
        outage shouldn't block trading entirely, matching the eligibility
        gate's existing fail-open philosophy elsewhere in this file.

        A genuine MISMATCH is NEVER fail-open — it is always blocked. This
        is deliberately the opposite failure mode: an ID that resolves to
        the wrong instrument is exactly the bug class that caused the
        DOT-USD ghost-order incident (Futures ID silently substituted for
        Spot ID under the same watchlist symbol).
        """
        meta = self.get_instrument_metadata(instrument_id)
        if not meta:
            return True, (
                f"Identity check skipped for {instrument_id} — metadata "
                f"endpoint returned nothing (fail-open)"
            )

        # Live API uses 'symbolFull' (verified 2026-07-01 via /market-data/instruments)
        live_symbol = (
            meta.get("symbolFull")
            or meta.get("internalSymbolFull")
            or meta.get("symbol")
            or meta.get("ticker")
            or meta.get("displayName")
            or meta.get("displayname")
            or ""
        )
        if not live_symbol:
            return True, (
                f"Identity check skipped for {instrument_id} — metadata "
                f"had no recognisable symbol field (fail-open)"
            )

        expected_norm = self._normalize_symbol_for_comparison(expected_symbol)
        live_norm = self._normalize_symbol_for_comparison(str(live_symbol))

        if expected_norm != live_norm:
            return False, (
                f"ID/Symbol MISMATCH: instrument_id={instrument_id} "
                f"resolves to '{live_symbol}' on eToro, but local data "
                f"expected '{expected_symbol}' — refusing to trade "
                f"(stale/wrong instrument mapping)"
            )
        return True, f"Identity OK: instrument_id={instrument_id} == {live_symbol}"

    def get_current_price(self, instrument_id: int) -> float | None:
        """Fetch the live price for *instrument_id* from the market-data
        rates endpoint. Returns None when unavailable (caller decides
        fail-open vs. fail-closed).

        fix/autonomy-hardening: extracted as a public method so the
        execution worker's slippage gate can reuse the exact same price
        source as open_position() itself.
        """
        try:
            rates_resp = self.get(
                "/market-data/instruments/rates",
                params={"instrumentIds": str(instrument_id)},
            )
            rates_list = rates_resp.get("rates", []) if isinstance(rates_resp, dict) else []
            if rates_list:
                rate_data = rates_list[0]
                price = (
                    rate_data.get("lastExecution")
                    or rate_data.get("bid")
                    or rate_data.get("ask")
                )
                if price:
                    return float(price)
        except (APIError, TypeError, ValueError) as exc:
            logger.warning(
                "get_current_price failed for %s: %s", instrument_id, exc
            )
        return None

    def open_position(
        self,
        instrument_id: int,
        amount_usd: float,
        stop_loss_pct: float = 3.0,
        symbol: str = "",
        take_profit_pct: float | None = None,
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
            blocks the trade **or** if the instrument eligibility check
            returns ``allowOpenPosition: false``.

        Raises
        ------
        APIError
            On non-2xx responses or if the current price cannot be fetched.
        """
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
        from bot.core.risk import calculate_sl_price, check_sl_quality_gate
        from bot.core.market_hours import is_market_open

        _symbol_for_preflight = symbol or str(instrument_id)

        # 0a) Identity gate — does instrument_id actually resolve to the
        #     expected ticker on the live API? Catches a stale/wrong local
        #     ID→symbol mapping BEFORE any order is built (root cause of
        #     the DOT-USD Futures-vs-Spot-ID ghost-order incident).
        id_ok, id_reason = self.verify_instrument_identity(
            instrument_id, _symbol_for_preflight
        )
        if not id_ok:
            logger.error("open_position BLOCKED: %s", id_reason)
            return {"success": False, "error": id_reason}
        logger.debug(id_reason)

        # 0b) Eligibility gate — POST /api/v2/trading/info/eligibility
        #     Prüft live ob eToro aktuell Trades erlaubt:
        #       allowOpenPosition = Instrument generell handelbar
        #       allowEntryOrders  = Markt gerade offen (ersetzt statische market_hours)
        #     Vorteil gegenüber is_market_open(): eToro entscheidet selbst —
        #     deckt Pre-Market, After-Hours, Maintenance und 24/5-Fenster ab.
        #
        #    POST /api/v2/trading/info/eligibility
        #    Returns: allowOpenPosition, leverageConfigs (SL/TP bounds), minPositionExposure, etc.
        eligibility_resp = None
        elig_data = None  # The eligibility dict for this instrument
        current_price = None  # Initialize early — may be set by eligibility or fallback
        try:
            eligibility_resp = self.post(
                "/trading/info/eligibility",
                {"instrumentIds": [instrument_id], "currency": "USD"},
                v2=True,
            )
        except APIError as exc:
            logger.warning(
                "Eligibility v2 endpoint failed for %s (%s) — fail-open, proceeding with trade",
                instrument_id, exc
            )
            eligibility_resp = None

        if eligibility_resp is not None:
            elig_list = eligibility_resp.get("eligibilities", [])
            for e in elig_list:
                if e.get("instrumentId") == instrument_id:
                    elig_data = e
                    # 1) allowOpenPosition — Instrument generell handelbar?
                    if not e.get("allowOpenPosition", True):
                        logger.warning(
                            "open_position BLOCKED: instrument %s allowOpenPosition=false",
                            instrument_id,
                        )
                        return {
                            "success": False,
                            "error": (
                                f"Instrument {instrument_id} not eligible for real trading "
                                f"(allowOpenPosition={e.get('allowOpenPosition')})"
                            ),
                        }
                    # 1b) minPositionExposure — Broker-Minimum (GEHEBELTER
                    #     Betrag; OSS-Fund 2026-07-16: stand schon immer in
                    #     der Eligibility-Antwort). Proaktiver Block VOR dem
                    #     POST; Fehlertext kompatibel zum 720-Lerner des
                    #     execution_workers (MinimumPositionAmount-Regex).
                    _min_exp = e.get("minPositionExposure")
                    if _min_exp and float(amount_usd) < float(_min_exp):
                        logger.warning(
                            "open_position BLOCKED: %s Order $%.2f < minPositionExposure $%.0f",
                            _symbol_for_preflight, amount_usd, float(_min_exp),
                        )
                        return {
                            "success": False,
                            "error": (
                                f"MinimumPositionAmount: {float(_min_exp):.0f} (Dollars) "
                                f"> Order ${float(amount_usd):.2f} (minPositionExposure, Eligibility)"
                            ),
                        }

                    # 2) allowEntryOrders — Markt gerade offen? (ersetzt statische market_hours)
                    #    False = Markt geschlossen / Maintenance / eToro-Restriction
                    #    Fail-open: wenn Feld fehlt, annehmen dass offen
                    if not e.get("allowEntryOrders", True):
                        msg = (
                            f"Markt geschlossen für {_symbol_for_preflight} "
                            f"(allowEntryOrders=false laut eToro Eligibility-API)"
                        )
                        logger.warning("open_position BLOCKED: %s", msg)
                        return {"success": False, "error": msg}
                    # 3) Validate SL is within allowed bounds from leverageConfigs
                    #    We'll validate the computed SL rate after we have current_price
                    break
            else:
                # Instrument not in eligibilities — check notFoundInstrumentIds
                not_found = eligibility_resp.get("notFoundInstrumentIds", [])
                not_found_sym = eligibility_resp.get("notFoundSymbols", [])
                if instrument_id in not_found:
                    logger.warning(
                        "open_position BLOCKED: instrument %s not found by eToro API",
                        instrument_id,
                    )
                    return {
                        "success": False,
                        "error": f"Instrument {instrument_id} not found (not tradable)",
                    }
                # current_price stays None — will use fallback below

        # Fetch current price from market-data/rates endpoint (reliable source)
        if current_price is None:
            try:
                rates_resp = self.get(
                    "/market-data/instruments/rates",
                    params={"instrumentIds": str(instrument_id)},
                )
                rates_list = rates_resp.get("rates", [])
                if rates_list:
                    rate_data = rates_list[0]
                    # Use bid price for long positions (conservative)
                    current_price = (
                        rate_data.get("lastExecution")
                        or rate_data.get("bid")
                        or rate_data.get("ask")
                    )
                    if current_price:
                        logger.info(
                            "Market-data price for %s: %.6f",
                            instrument_id, current_price
                        )
            except APIError as exc:
                logger.warning(
                    "Market-data rates failed for %s (%s) — trying DB fallback",
                    instrument_id, exc
                )
        
        # If no price from market-data, try to get it from portfolio_snapshot or signals
        if current_price is None:
            import sqlite3
            from pathlib import Path
            
            # Resolve DB path: client.py is at src/bot/api/client.py, DB is at project_root/data/
            project_root = Path(__file__).resolve().parent.parent.parent.parent  # → etoro_v3/
            db_path = project_root / "data" / "trading.db"
            
            # fix/conn-leak: close() must run even if a query raises — the
            # old code called conn.close() only on the success path, leaking
            # the handle on every failed DB fallback.
            conn = None
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
            except Exception as db_exc:
                logger.warning("DB price lookup failed for %s: %s", instrument_id, db_exc)
            finally:
                if conn is not None:
                    conn.close()
        
        if current_price is None:
            raise APIError(
                message=(
                    f"Cannot determine current price for instrument {instrument_id} "
                    f"— eligibility endpoint failed and no DB fallback available"
                ),
                status_code=0,
                endpoint="/trading/info/real/eligibility",
            )

        # ─── GHOST ORDER GUARD: Price must be > 0 ──────────────────────────
        # Instruments with price = 0 are inactive (delisted futures, closed markets).
        # Without this guard, SL = $0.00 * 0.97 = $0.00 → meaningless order → ghost.
        if current_price <= 0:
            logger.warning(
                "open_position BLOCKED: instrument %s has price %.6f (inactive/delisted)",
                instrument_id, current_price,
            )
            return {
                "success": False,
                "error": f"Ghost guard: instrument {instrument_id} price={current_price:.6f} (market inactive)",
            }

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

        # SL bounds validation against eligibility leverageConfigs
        if elig_data is not None:
            sl_distance_pct = ((current_price - stop_loss_rate) / current_price) * 100
            leverage_configs = elig_data.get("leverageConfigs", [])
            # Find the config for leverage=1, long direction (our standard)
            matching_config = None
            for lc in leverage_configs:
                if (lc.get("direction") == "long" and
                    1 in lc.get("leverageValues", [])):
                    matching_config = lc
                    break
            # If no exact match, use the first long config
            if matching_config is None:
                for lc in leverage_configs:
                    if lc.get("direction") == "long":
                        matching_config = lc
                        break
            if matching_config is not None:
                min_sl_pct = matching_config.get("minStopLossPercentage")
                max_sl_pct = matching_config.get("maxStopLossPercentage")
                allow_edit_sl = matching_config.get("allowEditStopLoss", True)
                if allow_edit_sl and min_sl_pct is not None and max_sl_pct is not None:
                    # Clamp SL to allowed range
                    clamped_sl_pct = max(min_sl_pct, min(sl_distance_pct, max_sl_pct))
                    if abs(clamped_sl_pct - sl_distance_pct) > 0.5:  # More than 0.5% difference
                        clamped_sl_rate = current_price * (1 - clamped_sl_pct / 100)
                        logger.info(
                            "open_position SL clamped for %s: %.1f%% → %.1f%% (bounds: %.1f-%.1f%%), "
                            "sl_rate %.6f → %.6f",
                            instrument_id, sl_distance_pct, clamped_sl_pct,
                            min_sl_pct, max_sl_pct, stop_loss_rate, clamped_sl_rate
                        )
                        stop_loss_rate = clamped_sl_rate
                        # Update body will use the new stop_loss_rate
                    logger.info(
                        "open_position SL validated for %s: distance=%.1f%% within bounds %.1f-%.1f%%",
                        instrument_id, sl_distance_pct, min_sl_pct, max_sl_pct
                    )

        # feat/tp-safety-net (OSS-Fund 2026-07-16): weit gesetztes Broker-TP
        # als Crash-Sicherheitsnetz — faellt der Bot aus, realisiert eToro
        # den Gewinn. Primaerer Mechanismus bleibt Trailing/Ladder; Default
        # +25% liegt bewusst darueber. Bounds aus leverageConfigs geclampt.
        take_profit_rate = None
        if take_profit_pct and current_price:
            _tp_pct = float(take_profit_pct)
            try:
                for _lc in (elig_data or {}).get("leverageConfigs", []) or []:
                    if _lc.get("direction") == "long" and 1 in (_lc.get("leverageValues") or []):
                        _tp_max = float(_lc.get("maxTakeProfitPercentage") or 0) or None
                        if _tp_max:
                            _tp_pct = min(_tp_pct, _tp_max)
                        break
            except Exception:
                pass
            take_profit_rate = round(float(current_price) * (1.0 + _tp_pct / 100.0), 6)

        body = {
            "transaction": "Buy",
            "instrumentId": instrument_id,
            "amount": amount_usd,
            "leverage": 1,
            "isNoStopLoss": False,
            "stopLossRate": stop_loss_rate,
        }
        if take_profit_rate:
            body["isNoTakeProfit"] = False
            body["takeProfitRate"] = take_profit_rate

        logger.debug(
            "open_position instrument=%s symbol=%s amount=%.2f sl_pct=%.1f sl_rate=%.6f",
            instrument_id,
            _symbol,
            amount_usd,
            stop_loss_pct,
            stop_loss_rate,
        )
        try:
            return self.post("/trading/execution/orders", body, v2=True)
        except APIError as exc:
            # Retry-ohne-TP (OSS-Muster): manche Instrumente lehnen TP im
            # POST ab — daran darf die Order nicht scheitern.
            if take_profit_rate and getattr(exc, "status_code", 0) == 400:
                logger.warning(
                    "open_position: TakeProfit abgelehnt (%s) — Retry ohne TP", exc
                )
                body.pop("takeProfitRate", None)
                body.pop("isNoTakeProfit", None)
                return self.post("/trading/execution/orders", body, v2=True)
            raise

    def get_position_units(self, position_id: str | int) -> float | None:
        """Echte Unit-Anzahl einer offenen Position aus dem Live-Portfolio.

        fix/partial-close-units (2026-07-14, HLAG.DE): alle Partial-Close-
        Pfade berechneten units als amount_usd/open_rate — das ignoriert die
        Waehrungsumrechnung (openConversionRate) und lag bei EUR-Titeln ~14%
        daneben (HLAG: Formel 5.562 vs. real 4.87677 units). Das Live-
        Portfolio (clientPortfolio.positions[].units) liefert die Wahrheit.

        Returns None wenn Position nicht gefunden oder API-Fehler (Aufrufer
        entscheidet ueber Fallback — NIEMALS None an close_position
        weiterreichen, wenn ein Teilverkauf gemeint war: None = Vollverkauf!).
        """
        try:
            payload = self.get_portfolio()
            positions = (payload.get("clientPortfolio") or {}).get("positions") or []
            for p in positions:
                if str(p.get("positionID")) == str(position_id):
                    units = float(p.get("units") or 0)
                    return units if units > 0 else None
        except Exception as exc:
            logger.warning("get_position_units(%s) fehlgeschlagen: %s", position_id, exc)
        return None

    def close_position(
        self,
        position_id: str | int,
        instrument_id: int,
        units_to_deduct: float | None = None,
    ) -> dict:
        """Close an existing open position, fully or partially.

        Endpoint:
            POST /trading/execution/market-close-orders/positions/{position_id}

        Per eToro API docs (api-portal.etoro.com/guides/market-orders):
          - Full close:    omit ``UnitsToDeduct`` (or leave it ``None``).
          - Partial close: provide ``UnitsToDeduct`` as an absolute unit
            count (NOT a percentage) representing how much of the position
            to liquidate. Callers must convert a target percentage into
            units themselves (units = amount_usd / open_rate * pct/100).

        Parameters
        ----------
        position_id : str | int
            The eToro position identifier.
        instrument_id : int
            The instrument ID (included in the body for audit purposes).
        units_to_deduct : float | None
            Absolute number of units to close. ``None`` (default) closes
            the entire position. Must be > 0 and less than the position's
            total units for a genuine partial close.

        Returns
        -------
        dict
            Close-order confirmation payload.

        Note
        ----
        The exact partial-close semantics (unit vs. lot conventions per
        asset class) have not been verified against eToro's Demo API by
        this patch. Before relying on this for real partial closes, run
        one small test against the Demo trading environment.
        """
        endpoint = (
            f"/trading/execution/market-close-orders/positions/{position_id}"
        )
        body: dict = {"instrumentId": instrument_id}
        if units_to_deduct is not None and units_to_deduct > 0:
            body["UnitsToDeduct"] = units_to_deduct
        logger.debug(
            "close_position position_id=%s instrument_id=%s units_to_deduct=%s",
            position_id,
            instrument_id,
            units_to_deduct,
        )
        return self.post(endpoint, body)

    def get_all_closing_prices(self) -> list:
        """GET /market-data/instruments/history/closing-price — Schlusskurse
        ALLER Instrumente (daily/weekly/monthly + isMarketOpen) in EINEM Call.

        feat/market-movers (2026-07-20): Basis fuer den marktweiten
        Mover-Scan im discovery_worker. Achtung: price=-1.0 ist das
        eToro-Sentinel fuer "kein Wert".
        """
        resp = self.get("/market-data/instruments/history/closing-price")
        if isinstance(resp, list):
            return resp
        if isinstance(resp, dict):
            for key in ("closingPrices", "instruments", "data"):
                val = resp.get(key)
                if isinstance(val, list):
                    return val
        return []

    def get_rates_batch(self, instrument_ids: list, chunk_size: int = 100) -> dict:
        """Live-Rates fuer viele IDs -> {instrument_id: rate_dict}.

        WICHTIG (feat/market-movers 2026-07-20): instrumentIds MUSS als
        rohes Komma in der URL stehen — ueber params= encodiert requests
        das Komma zu %2C und die API antwortet 400 ("not a valid
        integer"). DAS war der "Rates kann nur 1 ID"-Mythos; Batch
        funktioniert (verifiziert: 20 IDs in 0.1s). Best-effort:
        fehlgeschlagene Chunks werden geloggt und uebersprungen.
        """
        out: dict = {}
        ids = [int(i) for i in instrument_ids]
        for i in range(0, len(ids), chunk_size):
            chunk = ids[i : i + chunk_size]
            try:
                resp = self.get(
                    "/market-data/instruments/rates?instrumentIds="
                    + ",".join(str(x) for x in chunk)
                )
                rates = resp.get("rates") if isinstance(resp, dict) else resp
                for r in rates or []:
                    iid = r.get("instrumentID") or r.get("instrumentId")
                    if iid is not None:
                        out[int(iid)] = r
            except Exception as exc:
                logger.warning(
                    "get_rates_batch: Chunk %d-%d fehlgeschlagen: %s",
                    i, i + len(chunk), exc,
                )
        return out

    def get_instrument_eligibility(self, instrument_id: int) -> dict:
        """Return eligibility and pricing info for a single instrument.

        Uses the official v2 POST endpoint:
            POST /api/v2/trading/info/eligibility with {"instrumentIds": [...], "currency": "USD"}

        Falls back to market-data rate lookup on failure.

        Returns
        -------
        dict
            Eligibility data for the instrument (single item from eligibilities list).
        """
        try:
            resp = self.post(
                "/trading/info/eligibility",
                {"instrumentIds": [instrument_id], "currency": "USD"},
                v2=True,
            )
            elig_list = resp.get("eligibilities", [])
            if elig_list:
                return elig_list[0]
            # Check notFoundInstrumentIds
            not_found = resp.get("notFoundInstrumentIds", [])
            if instrument_id in not_found:
                raise APIError(
                    message=f"Instrument {instrument_id} not found by eToro API",
                    status_code=404,
                    endpoint="/trading/info/eligibility (v2)",
                )
            # No eligibility data — fall through to market-data fallback
        except APIError as exc:
            logger.warning(
                "Eligibility v2 failed for %d (%s) — trying market-data rate fallback",
                instrument_id, exc
            )
        # Fallback: get current rate from market-data endpoint
        try:
            rates = self.get(
                "/market-data/instruments/rates",
                params={"instrumentIds": str(instrument_id)},
            )
            if rates and isinstance(rates, list) and len(rates) > 0:
                return {"rate": rates[0].get("rate"), "instrumentId": instrument_id}
        except APIError as rate_exc:
            logger.warning(
                "Market-data rate fallback also failed for %d: %s",
                instrument_id, rate_exc
            )
        # All fallbacks exhausted — re-raise original eligibility error
        raise APIError(
            message=f"Could not get eligibility or rate for instrument {instrument_id}",
            status_code=0,
            endpoint="/trading/info/eligibility (v2) + /market-data/instruments/rates",
        )

    # ------------------------------------------------------------------
    # Order status check (post-flight verification)
    # ------------------------------------------------------------------

    def get_order_status(self, order_id: int, env: str = "real") -> dict:
        """Check order status via eToro API.

        Endpoint: GET /api/v1/trading/info/{env}/orders/{order_id}

        Used as a post-flight verification after POST /orders to distinguish
        between Rejected (with rejectionReason), Deferred (Pending/market closed),
        and True Ghost Orders (Executed but no position).

        Returns dict with:
          - status: "executed" | "pending" | "rejected" | "failed" | "cancelled" | "unknown"
          - order_id: int
          - instrument_id: int | None
          - positions: list[dict] | None  # list of position dicts or None
          - rejection_reason: str | None  # only if status="rejected"
          - raw: dict  # raw API response for audit trail
          - is_timing_issue: bool  # True if 404 (orderId not yet available)

        Raises no exceptions — always returns a status dict for safe handling.
        """
        env_segment = "/demo" if env == "demo" else "/real"
        url = f"{self.config.base_url.rstrip('/')}/trading/info{env_segment}/orders/{order_id}"

        try:
            resp = self._get_raw(url)
            resp.raise_for_status()
            raw = resp.json()
        except APIError as exc:
            if exc.status_code == 404:
                # orderId noch nicht verfügbar (timing issue) — DEFER statt FAIL
                return {
                    "status": "pending",
                    "order_id": order_id,
                    "instrument_id": None,
                    "positions": None,
                    "rejection_reason": None,
                    "raw": {"error": "404 - orderId not found yet"},
                    "is_timing_issue": True,
                }
            return {
                "status": "unknown",  # Transportfehler != Order-Failure — Worker faellt auf Portfolio-Polling zurueck
                "order_id": order_id,
                "instrument_id": None,
                "positions": None,
                "rejection_reason": None,
                "raw": {"error": f"HTTP {exc.status_code}", "body": str(exc)},
                "transport_error": True,
            }
        except requests.HTTPError as exc:
            # raise_for_status() wirft HTTPError (z.B. 404/500/503)
            status_code = exc.response.status_code if exc.response else 0
            if status_code == 404:
                return {
                    "status": "pending",
                    "order_id": order_id,
                    "instrument_id": None,
                    "positions": None,
                    "rejection_reason": None,
                    "raw": {"error": "404 - orderId not found yet"},
                    "is_timing_issue": True,
                }
            return {
                "status": "unknown",  # Transportfehler != Order-Failure — Worker faellt auf Portfolio-Polling zurueck
                "order_id": order_id,
                "instrument_id": None,
                "positions": None,
                "rejection_reason": None,
                "raw": {"error": f"HTTP {status_code}", "body": str(exc)},
                "transport_error": True,
            }
        except Exception as exc:
            return {
                "status": "unknown",  # Netzfehler: Order-Zustand unbekannt, NIE als failed buchen
                "order_id": order_id,
                "instrument_id": None,
                "positions": None,
                "rejection_reason": None,
                "raw": {"error": f"Network error: {exc}"},
                "transport_error": True,
            }

        # Parse response — eBull status mapping (verified via app/providers/implementations/etoro_broker.py)
        status_id = raw.get("statusID", "Unknown")
        positions = raw.get("positions")
        instrument_id = raw.get("instrumentID")
        rejection_reason = raw.get("rejectionReason")
        error_code = raw.get("errorCode")
        error_message = raw.get("errorMessage")
        if not rejection_reason and error_message:
            # fix/order-error-learning (Live-Befund 2026-07-16): eToro liefert
            # Ablehnungsgruende als errorCode/errorMessage (720=unter
            # MinimumPositionAmount, 814=instrument internal only,
            # 604=insufficient funds) — rejectionReason bleibt dabei leer.
            rejection_reason = f"eToro {error_code}: {error_message}"

        # fix/order-status-numeric (Live-Befund 2026-07-16, Trades #438/439/442):
        # der v1-Endpoint liefert statusID NUMERISCH — die eBull-String-Map
        # griff nie, default 'failed' verbuchte real gefuellte Orders als
        # FAILED (verwaiste Positionen). Verifizierte Semantik:
        #   3 = Executed (positions[] gefuellt)   4 = nicht ausgefuehrt
        #   1 = angenommen/queued (POST-Antwort)
        _STATUS_MAP = {
            # numerisch (v1, live verifiziert 2026-07-16)
            1: "pending",
            3: "executed",
            4: "failed",
            # String-Varianten (eBull) — falls andere API-Versionen sie liefern
            "Executed": "executed",
            "Filled": "executed",
            "Pending": "pending",
            "Rejected": "rejected",
            "Failed": "failed",
            "Cancelled": "rejected",
            "PartiallyFilled": "executed",
            "Expired": "failed",
            "Triggered": "pending",
        }
        if positions:
            # Staerkstes Signal: eine gefuellte Position IST die Ausfuehrung,
            # egal was statusID behauptet.
            status = "executed"
        elif rejection_reason:
            status = "rejected"
        else:
            status = _STATUS_MAP.get(status_id)
            if status is None:
                # Unbekannte statusID OHNE Position: pending statt failed —
                # der Worker deferred gecappt (DEFER_CAP) und loest die Order
                # idempotent erneut auf. Ein falsches 'failed' dagegen erzeugt
                # verwaiste Positionen (Vorfall Trade #442).
                status = "pending"
                logger.warning(
                    "get_order_status: unbekannte statusID %r fuer orderId=%s "
                    "(errorCode=%r) — behandle als 'pending' (gecappter Defer)",
                    status_id, order_id, raw.get("errorCode"),
                )

        return {
            "status": status,
            "order_id": order_id,
            "instrument_id": instrument_id,
            "positions": positions if positions else None,
            "rejection_reason": rejection_reason,
            "error_code": error_code,
            "raw": raw,
            "is_timing_issue": False,
            "transport_error": False,
        }

    # ------------------------------------------------------------------
    # Market data: Echtzeit-Rate + native Candles (OSS-Fund 2026-07-16)
    # ------------------------------------------------------------------

    def get_rate(self, instrument_id: int) -> dict | None:
        """Echtzeit-Rate (bid/ask/lastExecution) fuer EIN Instrument.

        Dokumentierter eToro-Bug: der Endpoint akzeptiert nur EINE ID pro
        Call — Komma-Listen liefern HTTP 500 (trading-Repo README).
        """
        try:
            resp = self.get(
                "/market-data/instruments/rates",
                params={"instrumentIds": str(instrument_id)},
            )
            rates = resp.get("rates", []) if isinstance(resp, dict) else []
            return rates[0] if rates else None
        except (APIError, TypeError, ValueError) as exc:
            logger.warning("get_rate(%s) fehlgeschlagen: %s", instrument_id, exc)
            return None

    def get_candles(
        self, instrument_id: int, interval: str = "OneHour", count: int = 100
    ) -> list[dict]:
        """eToro-eigene OHLCV-Kerzen — direkt per instrument_id, KEIN
        yfinance-Symbol-Mapping noetig (OSS-Fund 2026-07-16).

        GET /market-data/instruments/{id}/history/candles/asc/{interval}/{count}
        Intervalle: OneMinute..OneWeek, max 1000 Bars.
        Response-Shape: candles[0].candles[] mit fromDate/open/high/low/close/volume.
        """
        try:
            resp = self.get(
                f"/market-data/instruments/{int(instrument_id)}/history/"
                f"candles/asc/{interval}/{int(count)}"
            )
            outer = (resp or {}).get("candles") or []
            inner = (outer[0] or {}).get("candles") if outer else []
            return inner or []
        except (APIError, TypeError, ValueError, IndexError) as exc:
            logger.warning(
                "get_candles(%s, %s) fehlgeschlagen: %s", instrument_id, interval, exc
            )
            return []

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
