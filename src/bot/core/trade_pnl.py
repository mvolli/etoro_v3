"""Autoritatives realisiertes Ergebnis je Trade — ueber ALLE Tranchen.

WARUM (Bestandsaufnahme 2026-09-05):
`trades.pnl_usd` enthaelt nur die LETZTE Tranche. Die Profit-Leiter und die
LLM-Reviews schliessen Positionen aber gestaffelt, und von 910
Teilschliessungen tragen 12 einen Dollar-Betrag. Folge:

    Konto            -$1.830   (Equity 10.000 -> 8.169)
    trades.pnl_usd     -$454
    ------------------------------------------------
    ohne Datensatz   ~$1.377

Zusaetzlich liest `kelly_size_factor()` `trades.pnl_pct` — bei einer
gestaffelt geschlossenen Position ist das die VOLLE Kursbewegung, nicht das
Vereinnahmte. Beispiel 1929.HK (trade 293): pnl_pct 17,42 bei pnl_usd 2,28
auf einer 662,90-$-Position. Der Lernkreis, der die Positionsgroessen setzt,
sieht ein System das +0,6 %/Trade verdient, waehrend das Konto blutet.

WAEHRUNG: `trade_events.price` steht in LANDESWAEHRUNG (HKD, GBp, AUD),
`amount_usd` in Dollar. `units * (price - entry)` ist deshalb FALSCH fuer
alles ausserhalb der USA — gemessen Faktor 8,3 bei .HK und 83,6 bei .L
(Pence). Waehrungssicher ist:

    pnl_usd = amount_usd * pnl_pct / 100

`amount_usd` ist die Kostenbasis der Tranche in USD, `pnl_pct` die Bewegung
gegen den Einstieg. Gegen 400 Ereignisse geprueft: stimmt in 99 % mit
`amount_usd * (price - entry) / entry` ueberein.

DEDUPLIZIERUNG: `trade_events` enthaelt Duplikate aus Bulk-Batch-Laeufen
(gemessen 141 Events gegen 35 geschlossene Trades in einem Fenster). Jede
Auswertung MUSS ueber (trade_id, event_at, close_pct) deduplizieren.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Ereignisse, die realisierte Dollar erzeugen. OPEN nicht — das ist Einsatz.
REALIZING_EVENTS = ("PARTIAL_CLOSE", "CLOSE")


def event_pnl_usd(amount_usd: float | None, pnl_pct: float | None,
                  stored_usd: float | None = None) -> float | None:
    """Realisierte Dollar EINES Ereignisses.

    `stored_usd` gewinnt, wenn vorhanden — ein vom Broker gemeldeter Wert
    ist besser als jede Herleitung. Sonst waehrungssicher aus Kostenbasis
    und Prozentbewegung.
    """
    if stored_usd is not None:
        try:
            return float(stored_usd)
        except (TypeError, ValueError):
            pass
    if amount_usd is None or pnl_pct is None:
        return None
    try:
        return float(amount_usd) * float(pnl_pct) / 100.0
    except (TypeError, ValueError):
        return None


def realized_by_trade(db: Any) -> dict[int, dict]:
    """{trade_id: {realized_usd, tranchen, basis_usd}} ueber alle Tranchen.

    Fail-open: bei Fehler ein leeres Dict — Aufrufer fallen dann auf
    `trades.pnl_usd` zurueck und verhalten sich wie bisher.
    """
    try:
        rows = db.fetchall(
            """
            SELECT trade_id, event_type, amount_usd, pnl_pct, pnl_usd
            FROM (
                SELECT DISTINCT trade_id, event_at, close_pct, event_type,
                       amount_usd, pnl_pct, pnl_usd
                FROM trade_events
                WHERE event_type IN ('PARTIAL_CLOSE', 'CLOSE')
                  AND trade_id IS NOT NULL
            )
            """
        )
    except Exception as exc:
        logger.debug("realized_by_trade: %s", exc)
        return {}

    out: dict[int, dict] = {}
    for r in rows or []:
        try:
            tid = int(r["trade_id"])
        except (TypeError, ValueError, KeyError):
            continue
        val = event_pnl_usd(r["amount_usd"], r["pnl_pct"], r["pnl_usd"])
        if val is None:
            continue
        slot = out.setdefault(tid, {"realized_usd": 0.0, "tranchen": 0,
                                    "basis_usd": 0.0})
        slot["realized_usd"] += val
        slot["tranchen"] += 1
        try:
            slot["basis_usd"] += float(r["amount_usd"] or 0.0)
        except (TypeError, ValueError):
            pass
    return out


def reconcile(db: Any, start_equity: float = 10_000.0) -> dict:
    """Stellt die Summe der Trade-Ergebnisse der Kontoentwicklung gegenueber.

    Der Rest (`residual_usd`) ist alles, was kein Trade-Datensatz erklaert:
    Spread, Gebuehren, Slippage, nicht gebuchte Schliessungen. Heute wird
    davon NICHTS erfasst — die Zahl sichtbar zu machen ist der ganze Zweck.
    """
    res = {"start_equity": start_equity, "equity": None, "realized_usd": 0.0,
           "unrealized_usd": 0.0, "trades": 0, "tranchen": 0,
           "residual_usd": None}
    try:
        row = db.fetchone(
            "SELECT value FROM system_state WHERE key = 'CURRENT_EQUITY'")
        if row:
            res["equity"] = float(row["value"])
    except Exception:
        pass

    per_trade = realized_by_trade(db)
    res["realized_usd"] = round(sum(v["realized_usd"] for v in per_trade.values()), 2)
    res["trades"] = len(per_trade)
    res["tranchen"] = sum(v["tranchen"] for v in per_trade.values())

    try:
        row = db.fetchone("SELECT SUM(unrealized_pnl) AS u FROM portfolio_snapshot")
        if row and row["u"] is not None:
            res["unrealized_usd"] = round(float(row["u"]), 2)
    except Exception:
        pass

    if res["equity"] is not None:
        erwartet = start_equity + res["realized_usd"] + res["unrealized_usd"]
        res["residual_usd"] = round(res["equity"] - erwartet, 2)
    return res


# ── feat/fill-costs (2026-09-05) ─────────────────────────────────────────────

def estimate_fill_cost(amount_usd: float | None,
                       spread_pct: float | None) -> float | None:
    """Geschaetzte Reibung EINES Fills aus dem gemessenen Spread.

    Modell: wer sofort ausgefuehrt werden will, zahlt gegen die Mitte den
    halben Spread — beim Einstieg wie beim Ausstieg. Ein vollstaendiger
    Rundlauf kostet damit rund einen ganzen Spread, dieser Wert ist die
    HAELFTE davon (ein Fill).

    Das ist eine SCHAETZUNG, keine Abrechnung: eToro weist die Kosten nicht
    einzeln aus. Sie ist bewusst konservativ und ersetzt keine echten
    Gebuehrendaten — sie macht nur die Groessenordnung sichtbar, die bisher
    komplett fehlte. Uebernachtungs- und Umrechnungsgebuehren sind NICHT
    enthalten.

    Gibt None zurueck, wenn der Spread nicht gemessen werden konnte
    (check_spread_gate liefert dort fail-open None) — dann bleibt die
    Kostenspalte leer statt eine Null vorzutaeuschen.
    """
    if amount_usd is None or spread_pct is None:
        return None
    try:
        return abs(float(amount_usd)) * float(spread_pct) / 200.0
    except (TypeError, ValueError):
        return None


def recorded_costs(db: Any) -> dict:
    """{cost_usd, fills_mit_kosten, fills_gesamt} aus trade_events."""
    out = {"cost_usd": 0.0, "fills_mit_kosten": 0, "fills_gesamt": 0}
    try:
        row = db.fetchone(
            "SELECT COALESCE(SUM(cost_usd), 0) AS c, "
            "       SUM(cost_usd IS NOT NULL) AS m, COUNT(*) AS n "
            "FROM (SELECT DISTINCT trade_id, event_at, close_pct, cost_usd "
            "      FROM trade_events)"
        )
    except Exception:
        return out
    if row:
        out["cost_usd"] = round(float(row["c"] or 0.0), 2)
        out["fills_mit_kosten"] = int(row["m"] or 0)
        out["fills_gesamt"] = int(row["n"] or 0)
    return out
