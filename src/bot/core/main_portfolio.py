#!/usr/bin/env python3
"""Haupt-Konto-Portfolio: Snapshot, Diff und Kennzahlen.

feat/main-portfolio-report (2026-08-20): Der Report fuer das HAUPTKONTO des
Users — bewusst getrennt vom Bot-Konto, das der Rest dieses Repos handelt.

ZWEI HARTE ABGRENZUNGEN (beide schon einmal teuer gewesen):

1. KEYS: Dieses Modul und main_report_worker.py lesen AUSSCHLIESSLICH
   ETORO_MAIN_*. Der API-Key ist bei beiden Konten identisch, nur der
   USER-Key unterscheidet sie — ein vertauschter User-Key liest also
   klaglos das falsche Konto. ETORO_BOT_* darf hier nie auftauchen.

2. DB: Alles landet in `data/main_portfolio.db`, NICHT in `trading.db`.
   AGENTS.md erklaert trading.db zur "Einzigen DB" des Bots; Haupt-Konto-
   Zeilen dort wuerden frueher oder spaeter von einer Bot-Query erfasst,
   die annimmt, alles darin gehoere dem Bot.

Die Funktionen hier sind pur (kein Netz, keine DB) — der Worker holt die
Daten und reicht sie herein.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Positionen unterhalb dieses Betrags gelten als Staub und tauchen in
# Bewegungs-Listen nicht auf (eToro laesst Bruchteile ab ~$1 zu; eine
# 0.3%-Bewegung auf $2 ist keine Nachricht wert).
DUST_USD = 5.0


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def build_snapshot(
    client_portfolio: dict,
    symbol_by_id: dict[int, str] | None = None,
    now: datetime | None = None,
) -> tuple[dict, list[dict]]:
    """(account, positions) aus der eToro-clientPortfolio-Antwort.

    `unrealizedPnL` auf Portfolio-Ebene ist die AUTORITATIVE Gesamtzahl und
    weicht bewusst von der Summe der Positionen ab: Positionen innerhalb
    kopierter Portfolios (Mirrors) tauchen in `positions` nicht auf. Beide
    Werte werden getrennt gefuehrt, damit der Report die Differenz benennen
    kann statt sie zu verstecken.
    """
    now = now or datetime.now(timezone.utc)
    symbol_by_id = symbol_by_id or {}
    raw = client_portfolio.get("positions") or []

    positions: list[dict] = []
    for p in raw:
        upnl = p.get("unrealizedPnL") or {}
        iid = int(_f(p.get("instrumentID")))
        positions.append({
            "position_id": str(p.get("positionID") or ""),
            "instrument_id": iid,
            "symbol": symbol_by_id.get(iid) or f"ID{iid}",
            "amount": _f(p.get("amount")),
            "units": _f(p.get("units")),
            "open_rate": _f(p.get("openRate")),
            "close_rate": _f(upnl.get("closeRate")),
            "pnl_usd": _f(upnl.get("pnL")),
            "is_buy": 1 if p.get("isBuy", True) else 0,
            "leverage": int(_f(p.get("leverage"), 1)),
            "opened_at": str(p.get("openDateTime") or ""),
        })

    mirrors = client_portfolio.get("mirrors") or []
    invested = sum(p["amount"] for p in positions)
    account = {
        "snapshot_date": now.strftime("%Y-%m-%d"),
        "taken_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "invested": round(invested, 2),
        "credit": round(_f(client_portfolio.get("credit")), 2),
        # Autoritativ (inkl. Mirror-Innenleben):
        "unrealized_pnl": round(_f(client_portfolio.get("unrealizedPnL")), 2),
        # Nur die gelisteten Positionen — Differenz = Mirror-Anteil:
        "positions_pnl": round(sum(p["pnl_usd"] for p in positions), 2),
        "position_count": len(positions),
        "mirror_count": len(mirrors),
        "mirror_invested": round(sum(_f(m.get("initialInvestment")) for m in mirrors), 2),
        "mirror_net_profit": round(sum(_f(m.get("closedPositionsNetProfit")) for m in mirrors), 2),
        "mirror_available": round(sum(_f(m.get("availableAmount")) for m in mirrors), 2),
    }
    account["equity"] = round(account["invested"] + account["credit"]
                              + account["unrealized_pnl"], 2)
    account["mirror_pnl"] = round(account["unrealized_pnl"] - account["positions_pnl"], 2)
    return account, positions


def pct_change(new: float, old: float) -> float | None:
    """Prozentuale Veraenderung; None wenn keine sinnvolle Basis existiert."""
    if old is None or new is None or old == 0:
        return None
    return (new - old) / abs(old) * 100.0


def diff_snapshots(
    prev_account: dict | None,
    prev_positions: list[dict] | None,
    curr_account: dict,
    curr_positions: list[dict],
) -> dict:
    """Vergleicht zwei Snapshots.

    ERSTER LAUF (prev is None) ist ein eigener Zustand, kein Nullvergleich:
    `is_baseline=True`, alle Bewegungslisten leer. Sonst wuerde der erste
    Report "+71 Positionen, +$7.080" melden — was wie ein dramatischer Tag
    aussieht, obwohl schlicht die Vergleichsbasis fehlt.
    """
    if not prev_account or prev_positions is None:
        return {
            "is_baseline": True,
            "opened": [], "closed": [], "movers": [],
            "equity_delta": None, "equity_delta_pct": None,
            "pnl_delta": None, "invested_delta": None,
            "prev_date": None,
        }

    prev_by_id = {p["position_id"]: p for p in prev_positions}
    curr_by_id = {p["position_id"]: p for p in curr_positions}

    opened = [p for pid, p in curr_by_id.items() if pid not in prev_by_id]
    closed = [p for pid, p in prev_by_id.items() if pid not in curr_by_id]

    # Bewegungen nur fuer Positionen, die BEIDE Tage existierten — sonst
    # waere die "Veraenderung" in Wahrheit ein Ein- oder Ausstieg.
    movers = []
    for pid, cur in curr_by_id.items():
        old = prev_by_id.get(pid)
        if not old or cur["amount"] < DUST_USD:
            continue
        chg = pct_change(cur["close_rate"], old["close_rate"])
        if chg is None:
            continue
        movers.append({
            **cur,
            "prev_close_rate": old["close_rate"],
            "change_pct": chg,
            "pnl_delta": round(cur["pnl_usd"] - old["pnl_usd"], 2),
        })
    movers.sort(key=lambda m: -abs(m["change_pct"]))

    return {
        "is_baseline": False,
        "prev_date": prev_account.get("snapshot_date"),
        "opened": sorted(opened, key=lambda p: -p["amount"]),
        "closed": sorted(closed, key=lambda p: -p["amount"]),
        "movers": movers,
        "equity_delta": round(curr_account["equity"] - prev_account.get("equity", 0.0), 2),
        "equity_delta_pct": pct_change(curr_account["equity"], prev_account.get("equity")),
        "pnl_delta": round(curr_account["unrealized_pnl"]
                           - prev_account.get("unrealized_pnl", 0.0), 2),
        "invested_delta": round(curr_account["invested"]
                                - prev_account.get("invested", 0.0), 2),
    }


def aggregate_by(positions: list[dict], key_by_symbol: dict[str, str],
                 label: str = "?") -> list[tuple[str, float, float]]:
    """[(Gruppe, investiert, P/L)] absteigend nach investiert.

    Fuer Sektor-/Regionsverteilung. Symbole ohne Zuordnung landen unter
    *label* statt zu verschwinden.
    """
    buckets: dict[str, list[float]] = {}
    for p in positions:
        g = key_by_symbol.get(p["symbol"]) or label
        b = buckets.setdefault(g, [0.0, 0.0])
        b[0] += p["amount"]
        b[1] += p["pnl_usd"]
    return sorted(((g, round(v[0], 2), round(v[1], 2)) for g, v in buckets.items()),
                  key=lambda t: -t[1])


def top_positions(positions: list[dict], n: int = 10) -> list[dict]:
    """Groesste Titel nach investiertem Betrag — je SYMBOL aggregiert.

    eToro fuehrt mehrere Kaeufe desselben Titels als getrennte Positionen
    (gleicher Kurs, eigene positionID). Ungruppiert stuende derselbe Titel
    mehrfach in der Liste und saehe kleiner aus, als er ist — genau das
    Gegenteil dessen, was eine Klumpen-Uebersicht leisten soll.
    """
    agg: dict[str, dict] = {}
    for p in positions:
        a = agg.setdefault(p["symbol"], {
            "symbol": p["symbol"], "amount": 0.0, "pnl_usd": 0.0, "parts": 0})
        a["amount"] += p["amount"]
        a["pnl_usd"] += p["pnl_usd"]
        a["parts"] += 1
    for a in agg.values():
        a["amount"] = round(a["amount"], 2)
        a["pnl_usd"] = round(a["pnl_usd"], 2)
    return sorted(agg.values(), key=lambda a: -a["amount"])[:n]
