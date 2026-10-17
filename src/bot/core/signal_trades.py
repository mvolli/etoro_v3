#!/usr/bin/env python3
"""eToro Trading Bot V3 — Aktien-Signal-Trades mit fester Groesse.

src/bot/core/signal_trades.py — feat/signal-trades (VoLLi 2026-10-17).

VoLLi will Aktien weiter handeln, mit dem echten Gebuehrenschema ($1/$2 je
Order beim Oeffnen UND Schliessen). E5 (docs/e5-ergebnis-2026-10-17.md):
Bei ~$880 und "laufen lassen" sind die Familien A/C/D/E absolut leicht positiv
(+0,1 bis +0,3 % je Trade, E +1,9 % von Ausreissern getragen), gegenueber dem
Index aber ~0,6-1 Pp schlechter. VoLLi hat sich bewusst dafuer entschieden.

Regeln (Config trading.signal_trades):
  - nur die zugelassenen Familien (exakte Komponenten-Menge des Signaltyps)
  - nur $1-Boersen, kein Krypto (die Provision von $2 bzw. 1 %/Seite frisst die Kante)
  - feste Tranche = max_trade_pct des Regimes x 0,98 (nie unter $200) statt
    der Kette Conviction x Aggressivitaet x Kelly x Paritaet (~$121)
  - hoechstens max_open Positionen dieser Familien gleichzeitig
  - Index-Kern hat Vorrang: Cash nach dem Kauf >= Reserve + offene Kern-Luecke
  - vom Edge-Gate ausgenommen (die Live-Statistik Jul-Okt ist negativ; der Gate
    wuerde sonst weiter blocken). Keine Abbruchregel (VoLLi), Wochenbericht.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

logger = logging.getLogger("signal_trades")

DEFAULTS: dict = {
    "enabled": False,
    "families": [],                 # Signaltyp-Strings, z. B. "TREND_PULLBACK,GOLDEN_CROSS"
    "max_open": 0,
    "cap_headroom": 0.98,           # Tranche <= 98 % des Regime-Caps (Execution prueft mit Live-Equity)
    "min_usd": 200.0,               # Eroeffnungs-Mindestgroesse (VoLLi 16.10.)
    "reserve_pct": 18.0,            # Cash-Reserve nach dem Kauf (wie core_index.min_cash_pct)
    "exclude_suffixes": [],
    "exclude_asset_classes": [],
    "since": None,                  # nur Trades ab diesem Datum zaehlen gegen max_open (Altbestand nicht)
}
OPEN_STATUSES = ("APPROVED", "SUBMITTING", "PENDING", "ACTIVE")


def components(signal_type: Optional[str]) -> frozenset:
    return frozenset(p.strip().upper() for p in (signal_type or "").split(",") if p.strip())


def load_config(cfg: dict | None) -> dict:
    out = dict(DEFAULTS)
    st = (((cfg or {}).get("trading") or {}).get("signal_trades") or {})
    for k in DEFAULTS:
        if k in st and st[k] is not None:
            out[k] = st[k]
    out["enabled"] = bool(out["enabled"])
    out["family_sets"] = {components(f) for f in (out.get("families") or []) if components(f)}
    return out


def is_family(signal_type: Optional[str], st_cfg: dict) -> bool:
    return bool(st_cfg.get("enabled")) and components(signal_type) in st_cfg.get("family_sets", set())


def excluded_market(symbol: Optional[str], asset_class: Optional[str], st_cfg: dict) -> bool:
    s = (symbol or "").upper()
    ac = (asset_class or "").lower()
    if ac and ac in {str(x).lower() for x in st_cfg.get("exclude_asset_classes") or []}:
        return True
    if "crypto" in {str(x).lower() for x in st_cfg.get("exclude_asset_classes") or []} and s.endswith("-USD"):
        return True
    suf = tuple(str(x).upper() for x in st_cfg.get("exclude_suffixes") or [])
    return bool(suf) and s.endswith(suf)


def fixed_amount(equity: float, regime_max_trade_pct: float, st_cfg: dict) -> float:
    """Feste Tranche: max_trade_pct x headroom, nie unter min_usd."""
    # Nie ueber 10 % des Portfolios (VoLLi 16.10.), auch wenn ein Regime keinen
    # max_trade_pct traegt (der Code-Default waere 100 %).
    pct = min(max(float(regime_max_trade_pct or 0.0), 0.0), 10.0)
    cap = equity * pct / 100.0 * float(st_cfg.get("cap_headroom", 0.98))
    return round(max(cap, float(st_cfg.get("min_usd", 200.0))), 2)


def reserve_usd(equity: float, core_gap_usd: float, st_cfg: dict) -> float:
    return equity * float(st_cfg.get("reserve_pct", 18.0)) / 100.0 + max(0.0, float(core_gap_usd or 0.0))


def open_count(db: Any, st_cfg: dict) -> int:
    """Offene Positionen/Freigaben der zugelassenen Familien. Fail-closed (gross)."""
    fams = st_cfg.get("family_sets") or set()
    if not fams:
        return 0
    try:
        ph = ",".join("?" * len(OPEN_STATUSES))
        since = st_cfg.get("since")
        q = (f"SELECT s.signal_type FROM trades t JOIN signals s ON s.id = t.signal_id "
             f"WHERE t.status IN ({ph})")
        args = list(OPEN_STATUSES)
        if since:
            q += " AND t.created_at >= ?"
            args.append(str(since))
        rows = db.fetchall(q, tuple(args))
        return sum(1 for r in rows if components(r["signal_type"]) in fams)
    except Exception as exc:  # noqa: BLE001
        logger.warning("signal_trades: open_count fehlgeschlagen (blockiert): %s", exc)
        return 10 ** 6


def core_gap_usd(cfg: dict, equity: float, portfolio_rows: Iterable[dict], core_ids: set) -> float:
    """Offene Luecke des Index-Kerns (Ziel - Bestand), 0 wenn aus. Fail-open 0."""
    try:
        from bot.core import core_index as _ci
        ci = _ci.load_config(cfg)
        if ci.get("mode") != "live":
            return 0.0
        core_usd, _ = _ci.core_exposure(list(portfolio_rows), core_ids)
        return max(0.0, equity * float(ci.get("target_pct") or 0.0) / 100.0 - core_usd)
    except Exception:  # noqa: BLE001
        return 0.0
