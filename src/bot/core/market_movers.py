"""Marktweiter Daily-Mover-Scan aus eToro-Bulk-Daten.

feat/market-movers (2026-07-20): eToro hat KEIN dediziertes
Movers-Endpoint — aber /market-data/instruments/history/closing-price
(alle Instrumente, 1 Call) + Batch-Rates ergeben zusammen den
Tagesmove des gesamten tradebaren Universums:
    move = lastExecution / daily_close - 1
Pure Funktionen, keine I/O — der discovery_worker orchestriert.
"""
from __future__ import annotations


def compute_movers(
    closings: list,
    rates: dict,
    asset_class_by_id: dict,
    min_pct: float = 5.0,
    min_pct_crypto: float = 8.0,
    top_n: int = 10,
) -> list:
    """Top-N Mover nach |Tagesmove| -> [(instrument_id, move_pct)].

    closings: Liste aus EToroClient.get_all_closing_prices().
    rates:    {instrument_id: rate_dict} aus get_rates_batch().
    Nur offene Maerkte (isMarketOpen) und plausible Preise; -1.0 ist
    das eToro-Sentinel fuer fehlende Werte.
    """
    out: list = []
    for c in closings or []:
        iid = c.get("instrumentId")
        if iid is None or not c.get("isMarketOpen"):
            continue
        iid = int(iid)
        rate = rates.get(iid)
        if not rate:
            continue
        last = rate.get("lastExecution") or 0
        daily = ((c.get("closingPrices") or {}).get("daily") or {}).get("price") or 0
        try:
            last, daily = float(last), float(daily)
        except (TypeError, ValueError):
            continue
        if last <= 0 or daily <= 0:
            continue
        move = (last / daily - 1.0) * 100.0
        thresh = (
            min_pct_crypto
            if asset_class_by_id.get(iid) == "crypto"
            else min_pct
        )
        if abs(move) >= thresh:
            out.append((iid, move))
    out.sort(key=lambda m: -abs(m[1]))
    return out[:top_n]
