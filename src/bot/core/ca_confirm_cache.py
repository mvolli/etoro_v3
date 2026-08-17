#!/usr/bin/env python3
"""Persistenter TTL-Cache fuer ``confirm_corporate_action`` (Netz-Abruf).

Warum ueberhaupt ein Cache: der Corporate-Action-Guard laesst einen Sprung
CA_SCAN_BARS (50) Bars lang in der Reihe stehen — solange verzerrt er die
Indikatoren, solange muss das Gate greifen. ``ConfirmBudget.annotate()`` fragt
fuer JEDES Symbol mit Sprung Yahoos Action-Historie ab, und der data_worker
laeuft im 5-min-Takt. Ergebnis am 2026-08-17: fuer KRS.L und ADME.L (GBp/GBP-
Umstellung, Yahoo meldet dazu gar keine Aktion) lief 288-mal am Tag dieselbe
Abfrage mit derselben Antwort — gemessen +4,6 s je Lauf (18,5–20,7 s vorher,
23,1–26,2 s nachher, Cron-Budget 120 s). Nicht kritisch, aber dauerhaft.

Jeder Worker-Lauf ist ein eigener Prozess ⇒ ein prozess-lokaler Cache bringt
nichts. Der Cache liegt deshalb als Tabelle in ``trading.db`` (WAL,
busy_timeout — data_worker und discovery_worker halten VERSCHIEDENE
worker_locks und koennen gleichzeitig laufen; eine JSON-Datei unter ``data/``
haette dabei ein Last-Writer-Wins-Problem beim Vollschreiben).

Vorbild ist ``correlation.py`` (``correlation_cache``): rohes sqlite3,
injizierbarer ``db_path``, ``CREATE TABLE IF NOT EXISTS`` inline.

── Der Schluessel enthaelt den ORT des Sprungs ──────────────────────────────

``{yf_symbol}|{gap_date}|{ratio:.2f}``

``ca_gap_bars_ago`` ist als Schluessel untauglich: der Wert verschiebt sich mit
jeder neuen Bar, derselbe Sprung haette jeden Handelstag eine neue Identitaet.
Das Datum der Sprung-Bar ist stabil. Ohne Datum (nicht-datumsartiger Index)
gibt ``cache_key()`` ``None`` zurueck und der Cache wird umgangen — lieber der
alte Preis als ein Eintrag, der nicht zuordenbar ist.

Das Verhaeltnis gehoert mit in den Schluessel: passt Yahoo nur EINEN von
mehreren Effekten an (JMAT.L: Zusammenlegung ja, Sonderdividende noch nicht),
aendert sich die Sprunghoehe am selben Datum — eine sachlich andere Frage, die
eine frische Antwort verdient. Rundung auf 2 Dezimalen, damit das intraday
wandernde Close der LETZTEN Bar den Schluessel nicht bei jedem Lauf umwirft.

Nicht im Schluessel: der Kurs, gegen den ``CA_MATERIAL_DIV_PCT`` die
Dividenden-Materialitaet prueft. Er aendert sich in jedem Lauf, waere also das
Ende jeder Trefferquote; ein Kurs knapp an der 5-%-Grenze kann dadurch bis zu
einem negativen TTL lang das alte Urteil behalten.

── TTL: negativ kurz, positiv lang ─────────────────────────────────────────

Das NEGATIVE Ergebnis ist der haeufige Fall und muss mitgecacht werden, sonst
bringt der Cache genau fuer KRS.L/ADME.L nichts. Es ist aber auch das
gefaehrlichere: das Guard-Fenster IST das Lag zwischen Ex-Tag und Yahoos
rueckwirkender Anpassung (Stunden bis Tage), und Yahoo kann die Aktion in der
Action-Historie fuehren, waehrend die Kursreihe noch unbereinigt ist. Jede
Stunde negativer TTL ist eine Stunde, in der Pfad C blind ist — und Pfad C
existiert, weil die Heuristik (A/B) den Anlassfall JMAT.L durchgelassen hat.
Ein negativer TTL von einem Tag verschluckte damit genau das Fenster, das der
Guard abdecken soll.

6 Stunden ist der Kompromiss: klar im unteren (Stunden-)Ende der Lag-Spanne,
und es bleiben 4 Pruefungen je Kalendertag — mindestens eine pro Handels-
session jeder Boerse, die der Bot anfaesst (US, EU, HK/ASX). Gleichzeitig
fallen 98,6 % der Wiederholungen weg (4 statt 288 Abrufe pro Symbol und Tag).

Der POSITIVE TTL darf lang sein (24 h): eine bestaetigte Kapitalmassnahme
verschwindet nicht wieder aus Yahoos Historie, und die Sperre endet ohnehin
von selbst — sobald Yahoo die Reihe anpasst, ist der Sprung weg,
``needs_action_confirmation()`` sagt False, es wird gar nicht mehr
nachgeschlagen und das Gate oeffnet. Die 24 h sind reine Hygiene gegen einen
faelschlich geschriebenen Eintrag, Kosten: 1 Abruf pro Tag.

Alle Fehler sind fail-open: eine gesperrte oder kaputte Cache-Tabelle fuehrt
zum normalen Netz-Abruf, nie zu einer verschluckten Pruefung.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ─── Parameter ───────────────────────────────────────────────────────────────

CA_CACHE_TTL_NEGATIVE_S = 6 * 3600     # "keine Aktion gefunden" — s. Modul-Doc
CA_CACHE_TTL_POSITIVE_S = 24 * 3600    # bestaetigte Aktion, verfaellt kaum
CA_CACHE_PRUNE_DAYS = 7                # Aufraeumhorizont (Sprung-Datum bleibt 50 Bars)


def _now() -> float:
    """Zeitquelle als Funktion — Test-Seam fuer die TTL-Tests, damit die nicht
    die globale ``time.time`` umbiegen muessen."""
    return time.time()


def _default_db_path() -> str:
    """``data/trading.db`` relativ zu diesem Modul (src/bot/core/…)."""
    project_root = Path(__file__).resolve().parent.parent.parent.parent
    return str(project_root / 'data' / 'trading.db')


def _get_conn(db_path: str | None = None) -> sqlite3.Connection:
    """Connection auf die Cache-DB (= trading.db), WAL + busy_timeout."""
    path = db_path or _default_db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(f"file:{path}", uri=True, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Idempotente Migration (AGENTS.md): CREATE TABLE IF NOT EXISTS.

    ``result IS NULL`` ist das NEGATIVE Ergebnis — "geprueft, nichts
    Materielles gefunden". Es vom Cache-Miss zu unterscheiden ist der ganze
    Zweck dieser Tabelle, deshalb steht die Unterscheidung in der Zeile selbst
    (Zeile vorhanden = geprueft) und nicht im Wert.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ca_confirm_cache (
            cache_key   TEXT PRIMARY KEY,
            yf_symbol   TEXT NOT NULL,
            result      TEXT,
            checked_at  REAL NOT NULL
        )
    """)
    conn.commit()


# ─── Schluessel ──────────────────────────────────────────────────────────────

def cache_key(yf_symbol: str, indicators: dict) -> str | None:
    """Cache-Schluessel aus Symbol + Ort/Hoehe des Sprungs, sonst ``None``.

    ``None`` heisst "nicht cachebar" — der Aufrufer fragt dann wie bisher das
    Netz. Tritt nur auf, wenn ``scan_price_gaps()`` kein ``ca_gap_date``
    liefern konnte (Index ohne Datumssemantik).
    """
    gap_date = indicators.get("ca_gap_date")
    ratio = indicators.get("ca_gap_ratio")
    if not yf_symbol or not gap_date or ratio is None:
        return None
    try:
        return f"{yf_symbol}|{gap_date}|{float(ratio):.2f}"
    except (TypeError, ValueError):
        return None


# ─── Lesen / Schreiben ───────────────────────────────────────────────────────

def lookup(key: str, db_path: str | None = None) -> tuple[bool, str | None]:
    """``(hit, result)`` — ``hit=False`` heisst "nicht geprueft, frag Yahoo".

    Bei ``hit=True`` ist ``result`` die Beschreibung der Kapitalmassnahme oder
    ``None`` fuer das gecachte Negativergebnis. Abgelaufene Eintraege gelten
    als Miss (und werden beim naechsten ``store()`` ueberschrieben).
    """
    if not key:
        return False, None
    try:
        conn = _get_conn(db_path)
        try:
            _ensure_table(conn)
            row = conn.execute(
                "SELECT result, checked_at FROM ca_confirm_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("[ca_confirm_cache] Lesen fehlgeschlagen (%s) — Netz-Abruf", exc)
        return False, None

    if row is None:
        return False, None

    result, checked_at = row[0], float(row[1])
    ttl = CA_CACHE_TTL_POSITIVE_S if result else CA_CACHE_TTL_NEGATIVE_S
    if _now() - checked_at >= ttl:
        return False, None
    return True, result


def store(key: str, yf_symbol: str, result: str | None, db_path: str | None = None) -> None:
    """Ergebnis ablegen — auch (und vor allem) das negative ``None``."""
    if not key:
        return
    try:
        conn = _get_conn(db_path)
        try:
            _ensure_table(conn)
            conn.execute(
                "INSERT OR REPLACE INTO ca_confirm_cache"
                " (cache_key, yf_symbol, result, checked_at) VALUES (?, ?, ?, ?)",
                (key, yf_symbol, result, _now()),
            )
            # Aufraeumen im seltenen Pfad: nur echte Netz-Abrufe schreiben,
            # das sind eine Handvoll Zeilen pro Lauf.
            conn.execute(
                "DELETE FROM ca_confirm_cache WHERE checked_at < ?",
                (_now() - CA_CACHE_PRUNE_DAYS * 86400,),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("[ca_confirm_cache] Schreiben fehlgeschlagen (%s) — ignoriert", exc)
