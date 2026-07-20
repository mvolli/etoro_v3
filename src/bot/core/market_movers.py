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
    max_pct: float = 25.0,
    min_price: float = 0.5,
) -> list:
    """Top-N Mover nach |Tagesmove| -> [(instrument_id, move_pct)].

    closings: Liste aus EToroClient.get_all_closing_prices().
    rates:    {instrument_id: rate_dict} aus get_rates_batch().
    Nur offene Maerkte (isMarketOpen) und plausible Preise; -1.0 ist
    das eToro-Sentinel fuer fehlende Werte.

    fix/movers-sanity (2026-07-20, erster Live-Lauf): max_pct + min_price
    filtern Pennystock-Artefakte — VXT.DE "+170%"/PLAZ.L "+107%" waren
    Mini-Kurse, bei denen ein Tick riesige Prozente macht. Solche Werte
    sind Datenrauschen oder untradebare Illiquiditaet, keine Mover —
    sie verdraengten im ersten Lauf alle 10 Slots.
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
        if last <= 0 or daily < min_price:
            continue
        move = (last / daily - 1.0) * 100.0
        if abs(move) > max_pct:
            continue
        thresh = (
            min_pct_crypto
            if asset_class_by_id.get(iid) == "crypto"
            else min_pct
        )
        if abs(move) >= thresh:
            out.append((iid, move))
    out.sort(key=lambda m: -abs(m[1]))
    return out[:top_n]
