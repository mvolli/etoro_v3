"""Re-Buy-Cooldown-Lieferung — welche Instrumente sind gerade gesperrt?

fix/rebuy-cooldown-closed (2026-09-17): Der Signal-Worker sperrt Instrumente
ein Zeitfenster nach einem Kauf (config ``trading.rebuy_cooldown_hours``,
Default 6.0h), damit ein Signal nicht im Minutentakt denselben Namen
nachkaeuft (LEG.DE, IBE.MC, MAU.PA, 6753.T).

Das Ursprungs-SQL deckte nur ``status IN ('APPROVED','SUBMITTING','ACTIVE')``
ab. Ein Exposure-Auto-Trim, der die Position GANZ schliesst
(concentration_monitor ``min_remaining_pct: 50``), setzt den Trade aber auf
CLOSED — und fiel damit exakt in die Luecke: das Symbol war in dem Moment der
Sperre nicht mehr "aktiv", der naechste FRESH-Signal-Zyklus kaufte es sofort
wieder. Evidenz (2026-07-26..): 9531.T dreimal in 3 Tagen, GFRD.L nach
8 Min, 5101.T nach 77 Min — der Churn lief ausschliesslich ueber diesen Pfad.

Die Sperrzeit wird an dem Zeitstempel gemessen, zu dem die Position zuletzt
gekauft / zurueck zur Verfuegung stand: ``created_at`` fuer offene,
``closed_at`` fuer geschlossene Trades. Ein Trade, der vor dem Fenster
gekauft UND vor dem Fenster geschlossen wurde, blockiert NICHT mehr.
"""
from __future__ import annotations


def recent_buy_instrument_ids(
    db,
    cooldown_hours: float,
    now_iso: str | None = None,
) -> set[int]:
    """Instrument-IDs, die innerhalb der letzten *cooldown_hours* gekauft
    wurden und daher fuer Re-Buys gesperrt sind.

    Pure-ish: *db* muss ``fetchall(sql, params) -> list[dict-like]`` mit
    einem ``instrument_id``-Key liefern. *now_iso* wird nur als SQL-Parameter
    weitergereicht (fail-open auf Default ``datetime('now')``), damit die
    Funktion ohne DB-Mocks testbar bleibt.
    """
    if cooldown_hours is None or cooldown_hours <= 0:
        return set()
    window = f"-{cooldown_hours} hours"
    if now_iso is None:
        now_sql = "'now'"
        now_params: tuple = ()
    else:
        now_sql = "?"
        now_params = (now_iso,)
    # Ein Zeitstempel-Parameter pro OR-Zweig: created_at bzw. closed_at
    # werden gegen "jetzt minus Cooldown" gemessen.
    z1 = (now_params + (window,))
    z2 = (now_params + (window,))
    sql = f"""
        SELECT DISTINCT instrument_id
        FROM trades
        WHERE (
                status IN ('APPROVED', 'SUBMITTING', 'ACTIVE')
                AND created_at > datetime({now_sql}, ?)
              )
           OR (
                status = 'CLOSED'
                AND closed_at IS NOT NULL
                AND closed_at > datetime({now_sql}, ?)
              )
    """
    params: tuple = z1 + z2
    try:
        rows = db.fetchall(sql, params)
    except Exception:
        return set()  # fail-open: ohne Sperre laueft der Filter wie vorher
    ids: set[int] = set()
    for row in rows:
        try:
            ids.add(int(row["instrument_id"]))
        except (KeyError, TypeError, ValueError):
            continue
    return ids
