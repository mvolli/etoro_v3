"""
src/bot/api/instruments.py
─────────────────────────────────────────────────────────────────────────────
Instrument map — resolves eToro integer instrument IDs ↔ ticker symbols.

Cache strategy (layered, cheapest-first):
  1. File cache  → data/instrument_map.json  (TTL: 24 h)
  2. Legacy SQLite DB fallback
     → /home/mvolli/.hermes/workspace/etoro/db/etoro_trading.db
       table: instrument_metadata  (symbol, etoro_id columns)
  3. If both fail: returns empty dict and logs a warning.

The cache JSON format is::

    {
        "_meta": {"saved_at": "<ISO-8601 UTC>"},
        "map": {"<instrument_id_int>": "<SYMBOL>", ...}
    }

Keys in ``map`` are stored as strings (JSON limitation) but
``get_instrument_map()`` returns them as integers.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.api.client import EToroClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CACHE_FILE: Path = Path("data/instrument_map.json")
CACHE_TTL_HOURS: int = 24

_LEGACY_DB_PATH: Path = Path(
    "/home/mvolli/.hermes/workspace/etoro/db/etoro_trading.db"
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _cache_is_fresh(cache_file: Path, ttl_hours: int = CACHE_TTL_HOURS) -> bool:
    """Return *True* if *cache_file* exists and is younger than *ttl_hours*."""
    if not cache_file.is_file():
        return False
    try:
        raw = json.loads(cache_file.read_text(encoding="utf-8"))
        saved_at_str: str = raw.get("_meta", {}).get("saved_at", "")
        if not saved_at_str:
            return False
        saved_at = datetime.fromisoformat(saved_at_str)
        # Ensure timezone-aware comparison
        if saved_at.tzinfo is None:
            saved_at = saved_at.replace(tzinfo=timezone.utc)
        age_hours = (
            datetime.now(tz=timezone.utc) - saved_at
        ).total_seconds() / 3600.0
        return age_hours < ttl_hours
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not parse cache timestamp: %s", exc)
        return False


def _load_cache(cache_file: Path) -> dict[int, str]:
    """Load and return the instrument map from *cache_file*."""
    raw = json.loads(cache_file.read_text(encoding="utf-8"))
    return {int(k): v for k, v in raw.get("map", {}).items()}


def _save_cache(cache_file: Path, instrument_map: dict[int, str]) -> None:
    """Persist *instrument_map* to *cache_file* with a UTC timestamp."""
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_meta": {"saved_at": datetime.now(tz=timezone.utc).isoformat()},
        "map": {str(k): v for k, v in instrument_map.items()},
    }
    cache_file.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logger.debug("Instrument map cached → %s (%d entries)", cache_file, len(instrument_map))


def _fetch_from_legacy_db(db_path: Path) -> dict[int, str]:
    """Read instrument_metadata from the legacy eToro SQLite database.

    Only rows where ``etoro_id`` is a non-empty, integer-convertible string
    are included.

    Returns
    -------
    dict[int, str]
        ``{etoro_id_int: symbol}``
    """
    if not db_path.is_file():
        logger.warning("Legacy DB not found at %s", db_path)
        return {}

    result: dict[int, str] = {}
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT symbol, etoro_id FROM instrument_metadata "
                "WHERE etoro_id IS NOT NULL AND etoro_id != ''"
            ).fetchall()
        for row in rows:
            try:
                eid = int(row["etoro_id"])
                sym = row["symbol"].strip()
                if sym:
                    result[eid] = sym
            except (ValueError, TypeError):
                continue
        logger.info(
            "Loaded %d instruments from legacy DB %s", len(result), db_path
        )
    except sqlite3.Error as exc:
        logger.error("Failed to read legacy DB %s: %s", db_path, exc)

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_instrument_map(
    client: "EToroClient | None" = None,
    force_refresh: bool = False,
    cache_file: Path = CACHE_FILE,
    ttl_hours: int = CACHE_TTL_HOURS,
    legacy_db_path: Path = _LEGACY_DB_PATH,
) -> dict[int, str]:
    """Return a ``{instrument_id: symbol}`` mapping.

    Resolution order
    ----------------
    1. **File cache** — ``data/instrument_map.json`` if age < *ttl_hours*
       (skipped when *force_refresh* is True).
    2. **Legacy SQLite DB** — reads ``instrument_metadata`` from the old
       etoro v2 database as a fallback.
    3. **Empty dict** — logged as a warning; callers must handle gracefully.

    The result is always written back to the cache file so subsequent calls
    are fast.

    Parameters
    ----------
    client : EToroClient | None
        Currently unused (reserved for a future live-API fetch path).
        Pass *None* safely.
    force_refresh : bool
        Skip the cache and re-fetch from the source regardless of age.
    cache_file : Path
        Override the default cache path (mainly for testing).
    ttl_hours : int
        Override the default TTL (mainly for testing).
    legacy_db_path : Path
        Override the legacy DB path (mainly for testing).

    Returns
    -------
    dict[int, str]
        ``{instrument_id (int): symbol (str)}``
    """
    # --- Step 1: file cache ---
    if not force_refresh and _cache_is_fresh(cache_file, ttl_hours):
        try:
            instrument_map = _load_cache(cache_file)
            logger.debug(
                "Instrument map loaded from cache (%d entries)", len(instrument_map)
            )
            return instrument_map
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cache read failed, falling through to DB: %s", exc)

    # --- Step 2: legacy SQLite DB ---
    instrument_map = _fetch_from_legacy_db(legacy_db_path)

    if not instrument_map:
        logger.warning(
            "Instrument map is EMPTY — no data from cache or legacy DB. "
            "Subsequent symbol lookups will fail until the map is populated."
        )
        return {}

    # Persist result so the next call hits the cache
    try:
        _save_cache(cache_file, instrument_map)
    except OSError as exc:
        logger.warning("Could not write instrument map cache: %s", exc)

    return instrument_map


def symbol_to_id(symbol: str, instrument_map: dict[int, str]) -> int | None:
    """Return the eToro instrument ID for *symbol*, or *None* if not found.

    The lookup is case-insensitive.

    Parameters
    ----------
    symbol : str
        Ticker symbol, e.g. ``"NVDA"`` or ``"BTC/USD"``.
    instrument_map : dict[int, str]
        Mapping returned by :func:`get_instrument_map`.

    Returns
    -------
    int | None
    """
    symbol_upper = symbol.strip().upper()
    for iid, sym in instrument_map.items():
        if sym.strip().upper() == symbol_upper:
            return iid
    return None


def id_to_symbol(instrument_id: int, instrument_map: dict[int, str]) -> str | None:
    """Return the ticker symbol for *instrument_id*, or *None* if not found.

    Parameters
    ----------
    instrument_id : int
        eToro instrument identifier.
    instrument_map : dict[int, str]
        Mapping returned by :func:`get_instrument_map`.

    Returns
    -------
    str | None
    """
    return instrument_map.get(instrument_id)
