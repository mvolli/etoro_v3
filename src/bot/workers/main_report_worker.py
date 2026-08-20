#!/usr/bin/env python3
"""Haupt-Konto-Tagesreport -> #reports (feat/main-portfolio-report 2026-08-20).

Zusaetzlich zum Bot-Tagesreport (daily_report_worker, 23:15). Dieser hier
berichtet ueber das HAUPTKONTO des Users: Kennzahlen, Tagesveraenderungen,
Grafik der staerksten Bewegungen, KI-Einschaetzung und die Nachrichten, die
hinter den groessten Ausschlaegen stehen.

ZWEI ABGRENZUNGEN, die nicht aufgeweicht werden duerfen:

1. KEYS — hier ausschliesslich ETORO_MAIN_*. Der API-Key ist bei Bot- und
   Hauptkonto IDENTISCH, nur der USER-Key trennt sie. Ein vertauschter
   User-Key liest also klaglos das falsche Konto und der Report waere still
   falsch. ETORO_BOT_* darf in dieser Datei nicht vorkommen.

2. DB — data/main_portfolio.db, NICHT trading.db. Letztere ist laut
   AGENTS.md die "Einzige DB" des Bots; Hauptkonto-Zeilen darin wuerden
   frueher oder spaeter von einer Bot-Query erfasst, die annimmt, alles
   darin gehoere dem Bot. Nur LESEND greift der Worker auf trading.db zu,
   um instrument_id -> Symbol/Sektor/yfinance_symbol aufzuloesen.

Ausfuehrung: bash ~/.hermes/scripts/v3_main_report.sh
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("main_report_worker")

WORKER_NAME = "main_report_worker"
MAIN_DB = PROJECT_ROOT / "data" / "main_portfolio.db"
BOT_DB = PROJECT_ROOT / "data" / "trading.db"

NEWS_TOP_N = 6          # Nachrichten nur fuer die staerksten Bewegungen
NEWS_MAX_AGE_H = 48
CHART_TOP_N = 12


def _load_env() -> None:
    env_path = Path.home() / ".hermes" / ".env"
    if not env_path.exists():
        return
    with open(env_path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


# ── Persistenz ────────────────────────────────────────────────────────────────

def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS account_snapshot (
            snapshot_date     TEXT PRIMARY KEY,
            taken_at          TEXT,
            equity            REAL,
            invested          REAL,
            credit            REAL,
            unrealized_pnl    REAL,
            positions_pnl     REAL,
            mirror_pnl        REAL,
            position_count    INTEGER,
            mirror_count      INTEGER,
            mirror_invested   REAL,
            mirror_net_profit REAL,
            mirror_available  REAL
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS position_snapshot (
            snapshot_date TEXT NOT NULL,
            position_id   TEXT NOT NULL,
            instrument_id INTEGER,
            symbol        TEXT,
            amount        REAL,
            units         REAL,
            open_rate     REAL,
            close_rate    REAL,
            pnl_usd       REAL,
            is_buy        INTEGER,
            leverage      INTEGER,
            opened_at     TEXT,
            PRIMARY KEY (snapshot_date, position_id)
        )""")
    conn.commit()


def _save_snapshot(conn, account: dict, positions: list[dict]) -> None:
    cols = ("snapshot_date taken_at equity invested credit unrealized_pnl positions_pnl "
            "mirror_pnl position_count mirror_count mirror_invested mirror_net_profit "
            "mirror_available").split()
    conn.execute(
        f"INSERT OR REPLACE INTO account_snapshot ({','.join(cols)}) "
        f"VALUES ({','.join('?' * len(cols))})",
        tuple(account.get(c) for c in cols))
    pcols = ("snapshot_date position_id instrument_id symbol amount units open_rate "
             "close_rate pnl_usd is_buy leverage opened_at").split()
    conn.executemany(
        f"INSERT OR REPLACE INTO position_snapshot ({','.join(pcols)}) "
        f"VALUES ({','.join('?' * len(pcols))})",
        [tuple(p.get(c) if c != "snapshot_date" else account["snapshot_date"]
               for c in pcols) for p in positions])
    conn.commit()


def _load_previous(conn, before_date: str) -> tuple[dict | None, list[dict] | None]:
    """Juengster Snapshot VOR *before_date* — nicht einfach 'gestern'.

    Faellt ein Lauf aus (Neustart, Netzfehler), waere ein starrer
    Gestern-Bezug leer und der Report meldete faelschlich 'Baseline'. So
    vergleicht er gegen den letzten vorhandenen Stand und nennt dessen Datum.
    """
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM account_snapshot WHERE snapshot_date < ? "
        "ORDER BY snapshot_date DESC LIMIT 1", (before_date,)).fetchone()
    if not row:
        return None, None
    acc = dict(row)
    pos = [dict(r) for r in conn.execute(
        "SELECT * FROM position_snapshot WHERE snapshot_date = ?",
        (acc["snapshot_date"],))]
    return acc, pos


def _instrument_meta(instrument_ids: list[int]) -> dict[int, dict]:
    """{id: {symbol, yfinance_symbol, sector}} aus der Bot-DB (nur lesend)."""
    ids = [i for i in instrument_ids if i]
    if not ids:
        return {}
    try:
        conn = sqlite3.connect(f"file:{BOT_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        ph = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT instrument_id, symbol, yfinance_symbol, sector "
            f"FROM instruments WHERE instrument_id IN ({ph})", tuple(ids)).fetchall()
        conn.close()
        return {int(r["instrument_id"]): dict(r) for r in rows}
    except Exception as exc:
        logger.warning("[%s] Instrument-Aufloesung fehlgeschlagen: %s", WORKER_NAME, exc)
        return {}


# ── Nachrichten ───────────────────────────────────────────────────────────────

def _fetch_news(symbols_yf: list[tuple[str, str]]) -> list[tuple[str, list[str]]]:
    """[(Anzeigesymbol, [Schlagzeilen])] fuer die staerksten Bewegungen.

    Bewusst NUR fuer die angezeigten Titel: yfinance drosselt, und 40
    Instrumente wuerden den Lauf minutenlang blockieren. Yahoo-Namespace
    (yfinance_symbol) verwenden — nicht das eToro-Symbol.
    """
    import time as _t
    out: list[tuple[str, list[str]]] = []
    try:
        import yfinance as yf
        from bot.workers.news_flags_worker import _extract_headline
    except Exception as exc:
        logger.warning("[%s] News-Modul nicht verfuegbar: %s", WORKER_NAME, exc)
        return out
    cutoff = _t.time() - NEWS_MAX_AGE_H * 3600
    for display, yf_sym in symbols_yf:
        if not yf_sym:
            continue
        try:
            items = yf.Ticker(yf_sym).news or []
            heads = []
            for it in items[:8]:
                title, ts = _extract_headline(it)
                if title and (ts == 0 or ts >= cutoff):
                    heads.append(title[:150])
            if heads:
                out.append((display, heads[:3]))
        except Exception:
            continue
    return out


# ── KI-Einschaetzung ──────────────────────────────────────────────────────────

def _llm_review(account: dict, diff: dict, sectors: list, top: list[dict]) -> dict | None:
    """Bewertung der aktuellen Entwicklung. Fail-open: None -> Report ohne KI."""
    try:
        from bot.core.llm_client import call_llm_json
    except Exception:
        return None

    mov = "\n".join(
        f"  {m['symbol']}: {m['change_pct']:+.1f}% (P/L {m['pnl_delta']:+.0f}$)"
        for m in diff.get("movers", [])[:10]) or "  (keine)"
    sec = "\n".join(f"  {g}: ${inv:,.0f} (P/L {pnl:+.0f}$)" for g, inv, pnl in sectors[:8])
    pos = "\n".join(f"  {p['symbol']}: ${p['amount']:,.0f} (P/L {p['pnl_usd']:+.0f}$)"
                    for p in top[:10])
    basis = ("ERSTER LAUF - keine Vergleichsbasis, bewerte nur den Ist-Zustand."
             if diff.get("is_baseline") else
             f"Seit {diff.get('prev_date')}: Depot {diff.get('equity_delta'):+.2f}$, "
             f"{len(diff.get('opened', []))} neue / {len(diff.get('closed', []))} "
             f"geschlossene Positionen.")

    prompt = f"""Bewerte dieses Privatanleger-Depot sachlich und knapp.

DEPOT
  Wert ${account['equity']:,.2f} | investiert ${account['invested']:,.2f} | Cash ${account['credit']:,.2f}
  Unrealisiert ${account['unrealized_pnl']:+,.2f} (davon Copy-Trading ${account['mirror_pnl']:+,.2f})
  {account['position_count']} Positionen, {account['mirror_count']} Copy-Trader
{basis}

GROESSTE POSITIONEN
{pos}

SEKTOREN
{sec}

STAERKSTE TAGESBEWEGUNGEN
{mov}

Antworte NUR mit JSON:
{{"verdict": "<max 4 Woerter, z.B. 'Solide, aber konzentriert'>",
  "summary": "<2-3 Saetze zur aktuellen Entwicklung>",
  "strengths": ["<max 3 Punkte>"],
  "risks": ["<max 3 Punkte, konkret mit Zahlen>"],
  "watch": ["<max 3 konkrete Beobachtungspunkte>"]}}"""

    return call_llm_json(
        prompt,
        system=("Du bist ein nuechterner Portfolio-Analyst. Keine Kaufempfehlungen, "
                "keine Floskeln. Nenne Zahlen. Antworte AUSSCHLIESSLICH mit JSON."),
        max_tokens=900, label="main_report")


# ── Report ────────────────────────────────────────────────────────────────────

def _fmt_pos(p: dict) -> str:
    pnl = p.get("pnl_usd", 0.0)
    pct = (pnl / p["amount"] * 100) if p.get("amount") else 0.0
    em = "🟢" if pnl >= 0 else "🔴"
    return f"{em} `{p['symbol']:<10}` ${p['amount']:>7,.0f}  {pnl:+7.2f}$ ({pct:+.1f}%)"


def _fmt_mover(m: dict) -> str:
    em = "🟢" if m["change_pct"] >= 0 else "🔴"
    return (f"{em} `{m['symbol']:<10}` {m['change_pct']:+6.2f}%  "
            f"({m['pnl_delta']:+,.2f}$)")


def run(dry_run: bool = False) -> int:
    _load_env()
    from bot.api.client import ClientConfig, EToroClient
    from bot.core.main_portfolio import (aggregate_by, build_snapshot,
                                         diff_snapshots, top_positions)

    api_key = os.environ.get("ETORO_MAIN_API_KEY", "")
    user_key = os.environ.get("ETORO_MAIN_USER_KEY", "")
    if not api_key or not user_key:
        logger.error("[%s] ETORO_MAIN_API_KEY / ETORO_MAIN_USER_KEY fehlen", WORKER_NAME)
        return 1

    client = EToroClient(api_key=api_key, user_key=user_key,
                         config=ClientConfig.from_dict({}))
    try:
        cp = client.get_portfolio().get("clientPortfolio", {}) or {}
    except Exception as exc:
        logger.error("[%s] Portfolio-Abruf fehlgeschlagen: %s", WORKER_NAME, exc)
        client.close()
        return 1
    finally:
        try:
            client.close()
        except Exception:
            pass

    raw_ids = [int(p.get("instrumentID") or 0) for p in (cp.get("positions") or [])]
    meta = _instrument_meta(raw_ids)
    symbol_by_id = {i: m["symbol"] for i, m in meta.items() if m.get("symbol")}

    account, positions = build_snapshot(cp, symbol_by_id)

    MAIN_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(MAIN_DB), timeout=10)
    _ensure_schema(conn)
    prev_acc, prev_pos = _load_previous(conn, account["snapshot_date"])
    diff = diff_snapshots(prev_acc, prev_pos, account, positions)

    sector_by_symbol = {m["symbol"]: (m.get("sector") or "")
                        for m in meta.values()
                        if m.get("sector") and m["sector"] != "unknown"}
    sectors = aggregate_by(positions, sector_by_symbol, label="Sektor noch nicht abgerufen")
    top = top_positions(positions, 10)

    # ── Abschnitte ────────────────────────────────────────────────────────────
    sections: list[tuple[str, list[str], bool]] = []
    if diff["movers"]:
        gain = [m for m in diff["movers"] if m["change_pct"] > 0][:8]
        loss = [m for m in diff["movers"] if m["change_pct"] < 0][:8]
        if gain:
            sections.append((f"📈 Tagesgewinner ({len(gain)})",
                             [_fmt_mover(m) for m in gain], False))
        if loss:
            sections.append((f"📉 Tagesverlierer ({len(loss)})",
                             [_fmt_mover(m) for m in loss], False))
    if diff["opened"]:
        sections.append((f"🟢 Neue Positionen ({len(diff['opened'])})",
                         [f"`{p['symbol']:<10}` ${p['amount']:,.0f}"
                          for p in diff["opened"]], False))
    if diff["closed"]:
        sections.append((f"⚪ Geschlossen ({len(diff['closed'])})",
                         [f"`{p['symbol']:<10}` ${p['amount']:,.0f} "
                          f"(zuletzt {p['pnl_usd']:+,.2f}$)"
                          for p in diff["closed"]], False))
    sections.append((f"🏆 Groesste Positionen ({len(top)})",
                     [_fmt_pos(p) for p in top], False))
    if sectors:
        tot = sum(i for _, i, _ in sectors) or 1.0
        sections.append(("🧭 Sektoren", [
            f"`{g:<22}` ${inv:>7,.0f}  {inv / tot * 100:4.1f}%  ({pnl:+,.0f}$)"
            for g, inv, pnl in sectors], False))

    # ── Grafik ────────────────────────────────────────────────────────────────
    chart = None
    if diff["movers"]:
        try:
            from bot.core.candle_chart import movers_bar_png
            chart = movers_bar_png(diff["movers"], top_n=CHART_TOP_N)
        except Exception as exc:
            logger.warning("[%s] Chart fehlgeschlagen: %s", WORKER_NAME, exc)

    # ── Nachrichten zu den staerksten Bewegungen ──────────────────────────────
    yf_by_symbol = {m["symbol"]: (m.get("yfinance_symbol") or m.get("symbol"))
                    for m in meta.values() if m.get("symbol")}
    news = _fetch_news([(m["symbol"], yf_by_symbol.get(m["symbol"], ""))
                        for m in diff["movers"][:NEWS_TOP_N]]) if diff["movers"] else []

    review = _llm_review(account, diff, sectors, top)

    # ── Posten ────────────────────────────────────────────────────────────────
    ok = False
    try:
        import bot.discord_embeds as DE
        if chart and hasattr(DE, "attach_chart"):
            DE.attach_chart(chart)
        ok = DE.post_main_portfolio_embeds(
            account=account, diff=diff, sections=sections,
            llm_review=review, news=news, dry_run=dry_run)
    except Exception as exc:
        logger.error("[%s] Discord-Post fehlgeschlagen: %s", WORKER_NAME, exc)

    # Snapshot ERST nach dem Report speichern — sonst vergleicht ein
    # Wiederholungslauf gegen sich selbst und meldet "keine Veraenderung".
    if not dry_run:
        _save_snapshot(conn, account, positions)
    conn.close()

    logger.info("[%s] Report %s: Depot $%.2f, %d Positionen, %d Bewegungen, "
                "Chart=%s KI=%s News=%d, gepostet=%s",
                WORKER_NAME, account["snapshot_date"], account["equity"],
                account["position_count"], len(diff["movers"]),
                bool(chart), bool(review), len(news), bool(ok))
    return 0 if ok or dry_run else 1


def main() -> int:
    return run(dry_run="--dry-run" in sys.argv)


if __name__ == "__main__":
    sys.exit(main())
