#!/usr/bin/env python3
"""discord_embeds.py — Zentrales Discord Embed-Modul für alle Pipeline-Phasen.

Bietet strukturierte Embeds für Heartbeat, Worker-Summaries (data/risk/
signal/discovery/reconciler), Alerts, Trade-Fills/-Closes, Regime-Wechsel,
Kill-Switch sowie Tages- und Hauptkonto-Report.

refactor/embeds-dead-v2 (2026-09-25): die V2-Embeds P2–P5 (Reconciliation,
SL-Watchdog, Trading Decisions, Consolidation), beide Dashboards (Portfolio
Health, Pipeline Performance), Data Ingestion, API Metrics und Cash
Discrepancy wurden entfernt — nie aufgerufen, weder im Repo (gesamte
Git-Historie) noch in ~/.hermes/scripts oder ~/.hermes/cron (geprueft von VoLLi).

Alle Embeds gehen via _post_embed() → Discord Bot API v10.
Channel-Routing:
  #etoro-trading  (MAIN)   — Portfolio, Heartbeat, Monitoring, Candidates
  #etoro-trades   (TRADES) — BUY/SELL Executions, SL-Events, Consolidation
"""

from __future__ import annotations

import http.client
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import logging

# V3: infrastructure_module removed — stub out to avoid 120s SQLite timeout
logger = logging.getLogger("discord_embeds")


def insert_system_log(level: str, category: str, message: str, details: str = "") -> None:
    """Persistiert ein Embed-Ereignis in trading.db.system_log (fail-open).

    feat/close-history-observability (2026-07-22): war ein No-op-Stub — dadurch
    fehlte jede persistente Close-Historie (Cron-Outputs rotieren nach ~4h, und
    das per importlib geladene Modul nutzt keinen LogRepo). Schreibt jetzt
    leichtgewichtig via eigener Connection (busy_timeout gegen Worker-Locks),
    damit Partial-/Full-Closes + FILLED-Events einen durablen DB-Record haben.
    Darf NIE werfen (wird aus dem Embed-Post-Pfad gerufen).
    """
    logger.debug("[discord_embeds] log(%s, %s): %s", level, category, message)
    try:
        import sqlite3
        conn = sqlite3.connect(str(_TRADING_DB_PATH), timeout=3)
        try:
            conn.execute("PRAGMA busy_timeout=3000")
            conn.execute(
                "INSERT INTO system_log (ts, level, worker, message, details) "
                "VALUES (datetime('now'), ?, ?, ?, ?)",
                (level, category, message, (details or None)),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass

# ─── Channels ────────────────────────────────────────────────────────────────
DISCORD_MAIN_CHANNEL    = "1513971015108263957"   # #etoro-trading
DISCORD_TRADE_CHANNEL   = "1514786489110630600"   # #trades
DISCORD_REPORTS_CHANNEL = "1513401408643141642"   # #reports (Tagesreport)

# ─── Embed-Farben ─────────────────────────────────────────────────────────────
COLOR_GREEN   = 0x2ECC71
COLOR_RED     = 0xE74C3C
COLOR_ORANGE  = 0xE67E22
COLOR_BLUE    = 0x3498DB
COLOR_YELLOW  = 0xF1C40F
COLOR_GREY    = 0x95A5A6
COLOR_PURPLE  = 0x9B59B6
COLOR_TEAL    = 0x1ABC9C


# ─── Interne Helpers ─────────────────────────────────────────────────────────

def _read_token() -> Optional[str]:
    env = os.path.expanduser("~/.hermes/.env")
    if os.path.exists(env):
        with open(env) as f:
            for line in f:
                if "DISCORD_BOT_TOKEN" in line and not line.startswith("#"):
                    return line.split("=", 1)[1].strip()
    return None


# ── Discord-Hard-Limits (API v10) ────────────────────────────────────────────
MAX_FIELDS_PER_EMBED = 25
MAX_FIELD_VALUE = 1024
MAX_FIELD_NAME = 256
MAX_DESCRIPTION = 4096
MAX_TITLE = 256
MAX_EMBEDS_PER_MESSAGE = 10
# Discord zaehlt title+description+alle Feldnamen+alle Feldwerte gegen 6000.
# Etwas Luft lassen, damit Footer/Author nicht ueber die Kante kippen.
MAX_EMBED_TOTAL = 5800


def _embed_char_total(embed: dict) -> int:
    """Zeichen, die Discord gegen das 6000er-Gesamtlimit zaehlt."""
    n = len(str(embed.get("title") or "")) + len(str(embed.get("description") or ""))
    for f in embed.get("fields") or []:
        n += len(str(f.get("name") or "")) + len(str(f.get("value") or ""))
    return n


def pack_lines_into_fields(
    name: str,
    lines: list[str],
    inline: bool = False,
    max_value: int = MAX_FIELD_VALUE,
) -> list[dict]:
    """Zeilen VOLLSTAENDIG auf so viele Felder verteilen, wie noetig.

    fix/embeds-no-hidden-data (2026-08-12): ersetzt das Muster
    `"\\n".join(lines)[:1024]` bzw. `… +N weitere`, das Zeilen einfach
    verschwinden liess. Hier geht NICHTS verloren — laeuft ein Feld voll,
    entsteht ein Folgefeld mit „…"-Suffix im Namen (Muster aus
    post_daily_report_embed, hier verallgemeinert).

    Eine einzelne Zeile, die laenger als *max_value* ist, wird als einzige
    Ausnahme hart geschnitten — sonst gaebe es kein gueltiges Feld. Das ist
    ein Datenfehler weiter oben (z.B. 2000-Zeichen-Fehlermeldung), kein
    Mengenproblem, und wird geloggt.
    """
    if not lines:
        return []
    out: list[dict] = []
    chunk: list[str] = []
    size = 0
    for raw in lines:
        line = str(raw)
        if len(line) > max_value:
            logger.warning(
                "[discord_embeds] Einzelzeile laenger als Feldlimit "
                "(%d > %d) — gekuerzt: %.60s", len(line), max_value, line,
            )
            line = line[: max_value - 1] + "…"
        if chunk and size + len(line) + 1 > max_value:
            out.append({"name": name if not out else f"{name} …",
                        "value": "\n".join(chunk), "inline": inline})
            chunk, size = [], 0
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        out.append({"name": name if not out else f"{name} …",
                    "value": "\n".join(chunk), "inline": inline})
    return out


def _clip_embed_limits(embed: dict) -> dict:
    """Discord-API-Limits durchsetzen — Titel/Beschreibung/Feldnamen.

    Ohne Clipping lehnt Discord das GANZE Embed mit 400 ab (Notification
    verloren) — z.B. wenn resolve_instrument_display() lange Namen liefert
    und ein Feld über 1024 Zeichen wächst.

    fix/embeds-no-hidden-data (2026-08-12): das frühere `fields[:25]` hat
    Felder STILL weggeworfen — dieselbe Fehlerklasse wie „+21 weitere", nur
    unsichtbar, weil Discord danach brav 200 zurückgibt. Feldanzahl und
    Gesamtlaenge werden jetzt NICHT mehr hier gekappt, sondern von
    _split_embed() auf Folge-Embeds verteilt.
    """
    if embed.get("title"):
        embed["title"] = str(embed["title"])[:MAX_TITLE]
    if embed.get("description"):
        embed["description"] = str(embed["description"])[:MAX_DESCRIPTION]
    fields = embed.get("fields") or []
    embed["fields"] = [
        {**f,
         "name":  str(f.get("name", ""))[:MAX_FIELD_NAME],
         "value": str(f.get("value", ""))[:MAX_FIELD_VALUE]}
        for f in fields
    ]
    return embed


def _split_embed(embed: dict) -> list[dict]:
    """Ein Embed auf so viele Embeds verteilen, wie die Limits erfordern.

    Discord erlaubt 10 Embeds je Nachricht — das ist die eigentliche
    Reserve, statt Felder wegzuwerfen. Folge-Embeds erben die Farbe, tragen
    aber weder Titel noch Beschreibung doppelt (nur einen „(2/3)"-Zaehler),
    damit die Nachricht als EIN Bericht lesbar bleibt.

    Reissen die Daten selbst 10 Embeds (≈250 Felder), wird der Rest als
    Fehler geloggt statt still verworfen — dann stimmt weiter oben etwas
    nicht, und das soll auffallen.
    """
    # fix/embeds-no-hidden-data (2026-08-12): ZENTRALES Sicherheitsnetz.
    # Vor dem Clippen jedes ueberlange Feld in Folgefelder aufteilen, statt
    # es in _clip_embed_limits bei 1024 Zeichen hart abzuschneiden. Damit ist
    # jede Aufrufstelle abgesichert — auch kuenftige, die den Packer nicht
    # selbst benutzen. Ein Feld, dessen Wert eine einzige ueberlange Zeile
    # ist, bleibt der dokumentierte Ausnahmefall (Datenfehler, wird geloggt).
    _expanded: list[dict] = []
    for _f in (embed.get("fields") or []):
        _val = str(_f.get("value") or "")
        if len(_val) <= MAX_FIELD_VALUE:
            _expanded.append(_f)
            continue
        _parts = pack_lines_into_fields(
            str(_f.get("name") or "​"), _val.split("\n"),
            inline=bool(_f.get("inline")),
        )
        logger.info(
            "[discord_embeds] Feld '%.40s' (%d Zeichen) auf %d Felder verteilt "
            "statt gekuerzt", _f.get("name", "?"), len(_val), len(_parts),
        )
        _expanded.extend(_parts)
    embed = {**embed, "fields": _expanded}

    embed = _clip_embed_limits(embed)
    fields = embed.get("fields") or []
    base_total = (len(str(embed.get("title") or ""))
                  + len(str(embed.get("description") or "")))

    if len(fields) <= MAX_FIELDS_PER_EMBED and _embed_char_total(embed) <= MAX_EMBED_TOTAL:
        return [embed]

    pages: list[list[dict]] = []
    cur: list[dict] = []
    cur_len = base_total
    for f in fields:
        flen = len(str(f.get("name") or "")) + len(str(f.get("value") or ""))
        if cur and (len(cur) >= MAX_FIELDS_PER_EMBED or cur_len + flen > MAX_EMBED_TOTAL):
            pages.append(cur)
            cur, cur_len = [], 0          # Folgeseiten ohne Titel/Description
        cur.append(f)
        cur_len += flen
    if cur:
        pages.append(cur)

    dropped = 0
    if len(pages) > MAX_EMBEDS_PER_MESSAGE:
        dropped = sum(len(p) for p in pages[MAX_EMBEDS_PER_MESSAGE:])
        logger.error(
            "[discord_embeds] '%s': %d Felder passen auch in %d Embeds nicht — "
            "%d Felder gehen verloren. Datenmenge weiter oben begrenzen!",
            embed.get("title", "?"), len(fields), MAX_EMBEDS_PER_MESSAGE, dropped,
        )
        pages = pages[:MAX_EMBEDS_PER_MESSAGE]

    out: list[dict] = []
    total_pages = len(pages)
    for idx, page in enumerate(pages):
        if idx == 0:
            e = {**embed, "fields": page}
            if total_pages > 1:
                e["title"] = f"{embed.get('title','')} (1/{total_pages})"[:MAX_TITLE]
        else:
            e = {"color": embed.get("color"), "fields": page,
                 "title": f"… ({idx + 1}/{total_pages})"}
            if idx == total_pages - 1 and embed.get("footer"):
                e["footer"] = embed["footer"]
        out.append(e)
    return out


# feat/candle-charts (2026-07-16): Ein-Schuss-Slot fuer ein Embed-Bild.
# attach_chart(png) vor einem post_*-Aufruf setzen — _post_embed konsumiert
# es genau einmal (Single-Thread-Worker, kein Locking noetig) und haengt es
# als Multipart-Attachment (attachment://chart.png) an. Kein Hosting noetig.
_PENDING_CHART: dict = {"png": None}


def attach_chart(png_bytes: bytes | None) -> None:
    """Bild fuer das NAECHSTE Embed vormerken (einmalig konsumiert)."""
    _PENDING_CHART["png"] = png_bytes


# feat/pnl-nachreport: Koordinaten des zuletzt erfolgreich geposteten Embeds.
# ACHTUNG Dual-Instanz: trailing_stop.py laedt dieses Modul per importlib als
# EIGENE Instanz — attach_chart()/post_*()/get_last_post() muessen immer am
# selben Modul-Objekt aufgerufen werden, sonst liest man den Slot der falschen
# Instanz.
_LAST_POST: dict = {"message_id": None, "channel_id": None}


def get_last_post() -> dict:
    """{'message_id', 'channel_id'} des letzten erfolgreichen _post_embed()."""
    return dict(_LAST_POST)


def _request_discord(method: str, path: str, payload: bytes,
                     content_type: str) -> tuple[int, str]:
    """Ein Discord-API-Request mit einmaligem 429-Retry. Returns (status, body)."""
    token = _read_token()
    if not token:
        return 0, "kein DISCORD_BOT_TOKEN"
    for attempt in (1, 2):
        conn = http.client.HTTPSConnection("discord.com", timeout=15)
        conn.request(method, path, body=payload, headers={
            "Authorization":  f"Bot {token}",
            "Content-Type":   content_type,
            "Content-Length": str(len(payload)),
        })
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        if resp.status == 429 and attempt == 1:
            try:
                wait = float(json.loads(body).get("retry_after", 1.0))
            except Exception:
                wait = 1.0
            import time as _time
            _time.sleep(min(wait, 5.0))
            continue
        return resp.status, body
    return 0, "unreachable"  # pragma: no cover


def _post_embed(embed: dict, channel_id: str, dry_run: bool = False) -> "str | bool":
    """Sende ein Discord Embed.

    Rueckgabe truthy bei Erfolg — konkret die Message-ID (String) fuer
    spaetere Edits via _edit_embed(); alte `if ok:`-Call-Sites bleiben
    unveraendert gueltig. False bei Fehler, True im Dry-Run.
    """
    # fix/embeds-no-hidden-data (2026-08-12): passt der Inhalt nicht in EIN
    # Embed, wird er auf mehrere verteilt (Discord: 10 je Nachricht) statt
    # Felder stillschweigend abzuschneiden. Es bleibt EINE Nachricht — also
    # eine Message-ID fuer _LAST_POST/_edit_embed und ein Chart-Anhang.
    embeds = _split_embed(embed)
    embed = embeds[0]
    png = _PENDING_CHART.get("png")
    _PENDING_CHART["png"] = None  # immer konsumieren — nie ans falsche Embed
    # Slot VOR dem Versuch leeren — sonst erbt ein fehlgeschlagener Post die
    # Message-ID des vorherigen und das Event-Log verlinkt das falsche Embed.
    _LAST_POST["message_id"] = None
    _LAST_POST["channel_id"] = None
    if dry_run:
        _extra = f" (+{len(embeds) - 1} Folge-Embed(s))" if len(embeds) > 1 else ""
        logger.info(f"[discord_embeds DRY-RUN] '{embed.get('title', '?')}'{_extra} → channel {channel_id}")
        return True

    try:
        if png:
            # Chart haengt am ERSTEN Embed — Folge-Embeds tragen nur Felder.
            embed["image"] = {"url": "attachment://chart.png"}
            embeds[0] = embed
            import uuid as _uuid
            boundary = "----v3chart" + _uuid.uuid4().hex
            payload_json = json.dumps({
                "embeds": embeds,
                "attachments": [{"id": 0, "filename": "chart.png"}],
            })
            payload = b"".join([
                (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f"name=\"payload_json\"\r\nContent-Type: application/json"
                 f"\r\n\r\n{payload_json}\r\n").encode("utf-8"),
                (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f"name=\"files[0]\"; filename=\"chart.png\"\r\n"
                 f"Content-Type: image/png\r\n\r\n").encode("utf-8"),
                png,
                f"\r\n--{boundary}--\r\n".encode("utf-8"),
            ])
            content_type = f"multipart/form-data; boundary={boundary}"
        else:
            payload = json.dumps({"embeds": embeds}).encode("utf-8")
            content_type = "application/json"

        status, body = _request_discord(
            "POST", f"/api/v10/channels/{channel_id}/messages",
            payload, content_type,
        )

        if status in (200, 201):
            _n = f" ({len(embeds)} Embeds)" if len(embeds) > 1 else ""
            logger.info(f"[discord_embeds] Embed gepostet{_n}: '{embed.get('title','?')}' → {channel_id}")
            msg_id = None
            try:
                msg_id = json.loads(body).get("id")
            except Exception:
                pass
            _LAST_POST["message_id"] = msg_id
            _LAST_POST["channel_id"] = channel_id
            return msg_id or True
        else:
            logger.error(f"[discord_embeds] Discord API {status}: {body[:200]}")
            return False
    except Exception as exc:
        logger.error(f"[discord_embeds] Post-Fehler: {exc}")
        return False


def _edit_embed(channel_id: str, message_id: str, embed: dict,
                dry_run: bool = False) -> bool:
    """Editiere ein bereits gepostetes Embed (PATCH).

    feat/pnl-nachreport: wird vom Reconciler genutzt, um Close-Embeds mit dem
    finalen PnL aus der eToro-History zu aktualisieren. Bewusst KEIN
    'attachments'-Key im Payload — Discord behaelt dann den vorhandenen
    Chart-Anhang; das Embed muss dafuer seine image-Referenz
    (attachment://chart.png) weitertragen.
    """
    if not channel_id or not message_id:
        return False
    # fix/embeds-no-hidden-data: PATCH ersetzt ALLE Embeds der Nachricht —
    # deshalb dieselbe Aufteilung wie beim Posten. Ohne sie wuerde ein Embed
    # mit >25 Feldern hier einen 400er ausloesen (seit _clip_embed_limits
    # nicht mehr still kappt), und der Nachreport bliebe stumm haengen.
    embeds = _split_embed(embed)
    embed = embeds[0]
    if dry_run:
        logger.info(f"[discord_embeds DRY-RUN] EDIT '{embed.get('title', '?')}' → {channel_id}/{message_id}")
        return True
    try:
        payload = json.dumps({"embeds": embeds}).encode("utf-8")
        status, body = _request_discord(
            "PATCH", f"/api/v10/channels/{channel_id}/messages/{message_id}",
            payload, "application/json",
        )
        if status == 200:
            logger.info(f"[discord_embeds] Embed editiert: '{embed.get('title','?')}' → {channel_id}/{message_id}")
            return True
        logger.error(f"[discord_embeds] Edit-Fehler {status}: {body[:200]}")
        return False
    except Exception as exc:
        logger.error(f"[discord_embeds] Edit-Fehler: {exc}")
        return False


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pnl_emoji(pct: float) -> str:
    if pct >= 2:    return "🟢"
    if pct >= 0:    return "🔵"
    if pct >= -2:   return "🟡"
    if pct >= -5:   return "🟠"
    return "🔴"


def _severity_color(severity: str) -> int:
    return {
        "NORMAL":          COLOR_GREEN,
        "CAUTION":         COLOR_YELLOW,
        "WARNING":         COLOR_ORANGE,
        "CRITICAL":        COLOR_RED,
        "CIRCUIT_BREAKER": COLOR_PURPLE,
    }.get(severity, COLOR_GREY)


# ─── Instrument Display Resolution ───────────────────────────────────────────
# Discord-Embeds sollen nie rohe instrument_ids ohne Kontext zeigen.
# resolve_instrument_display() löst eine ID oder ein Symbol gegen die
# instruments-Tabelle auf und liefert "Symbol — Name (Market)".
# Fail-open: wenn DB/Zeile fehlt, kommt die Eingabe unverändert zurück
# (numerische IDs werden als "Instrument #<id>" gekennzeichnet).

_PROJECT_ROOT_DE = Path(__file__).resolve().parent.parent.parent   # src/bot → src → etoro_v3
_TRADING_DB_PATH = _PROJECT_ROOT_DE / "data" / "trading.db"
_INSTRUMENT_LOOKUP_CACHE: dict[str, Optional[dict]] = {}


def _lookup_instrument(instrument_ref) -> Optional[dict]:
    """Instrumenten-Zeile per instrument_id ODER Symbol aus data/trading.db.

    Ergebnis wird pro Prozess gecacht. Fail-open: None bei Fehlern/kein Match.
    """
    ref = str(instrument_ref).strip()
    if not ref:
        return None
    if ref in _INSTRUMENT_LOOKUP_CACHE:
        return _INSTRUMENT_LOOKUP_CACHE[ref]

    row_dict: Optional[dict] = None
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{_TRADING_DB_PATH}?mode=ro", uri=True, timeout=3)
        try:
            conn.row_factory = sqlite3.Row
            if ref.isdigit():
                row = conn.execute(
                    "SELECT instrument_id, symbol, name, market_region, asset_class "
                    "FROM instruments WHERE instrument_id = ?", (int(ref),)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT instrument_id, symbol, name, market_region, asset_class "
                    "FROM instruments WHERE symbol = ? COLLATE NOCASE", (ref,)
                ).fetchone()
            if row:
                row_dict = dict(row)
        finally:
            conn.close()
    except Exception as exc:
        logger.debug(f"[discord_embeds] Instrument-Lookup '{ref}' fehlgeschlagen: {exc}")
        return None  # nicht cachen — DB könnte gleich wieder da sein

    # Nur Treffer cachen: ein Miss kann durch spätere Discovery-Inserts zum
    # Treffer werden — Negativ-Cache würde dauerhaft "Instrument #id" zeigen.
    if row_dict is not None:
        _INSTRUMENT_LOOKUP_CACHE[ref] = row_dict
    return row_dict


def resolve_instrument_display(instrument_ref) -> str:
    """'Symbol — Name (Market)' für eine instrument_id oder ein Symbol.

    Beispiele:
        2358      → "00027.HK — China Telecom (HK)"
        "CVX.US"  → "CVX.US — Chevron (US)"
        "AAPL"    → "AAPL — Apple Inc (US)"

    Fail-open: unbekanntes Symbol kommt unverändert zurück, eine unbekannte
    numerische ID als "Instrument #<id>" (nie eine nackte Zahl ohne Kontext).
    """
    ref = str(instrument_ref).strip()
    if not ref or ref == "?":
        return "?"
    return _format_instrument_display(ref, _lookup_instrument(ref))


def _format_instrument_display(ref: str, row: Optional[dict]) -> str:
    """Display-String aus einer (evtl. fehlenden) instruments-Zeile bauen."""
    if row is None:
        return f"Instrument #{ref}" if ref.isdigit() else ref

    symbol = row.get("symbol") or ref
    name   = (row.get("name") or "").strip()
    region = (row.get("market_region") or "").strip()

    display = symbol
    if name and name.upper() != symbol.upper():
        display += f" — {name}"
    if region:
        display += f" ({region})"
    return display


# ═══════════════════════════════════════════════════════════════════════════════
# P1 — HEARTBEAT / TICK-STATUS
# ═══════════════════════════════════════════════════════════════════════════════

def post_heartbeat_embed(
    tick: int,
    equity: float,
    cash: float,
    position_count: int,
    drawdown_pct: float,
    severity: str,
    cb_active: bool,
    elapsed_s: float,
    cb_status: dict = None,
    phase_durations: dict = None,
    positions_summary: list = None,
    dry_run: bool = False,
) -> bool:
    """Pipeline-Heartbeat — alle 30min (TAKT_MONITORING) in #etoro-trading.

    Args:
        tick: Pipeline tick number
        equity: Total portfolio equity
        cash: Available cash balance
        position_count: Number of open positions
        drawdown_pct: Current drawdown percentage
        severity: Drawdown severity level (NORMAL/CAUTION/WARNING/CRITICAL/CIRCUIT_BREAKER)
        cb_active: Whether circuit breaker is active
        elapsed_s: Pipeline runtime in seconds
        cb_status: CircuitBreaker.get_status() dict (state, failure_count, error_counts, etc.)
        phase_durations: Dict of {phase_name: elapsed_seconds} for this pipeline run
    """
    cash_pct  = (cash / equity * 100) if equity else 0
    dd_emoji  = _pnl_emoji(-drawdown_pct)

    # T10.1: Circuit Breaker Status Details
    if cb_status:
        cb_state = cb_status.get("state", "UNKNOWN")
        cb_failures = cb_status.get("failure_count", 0)
        cb_errors = cb_status.get("error_counts", {})
        cb_uptime = cb_status.get("uptime_seconds", 0)

        if cb_state == "OPEN":
            cb_str = f"🔴 OPEN (failures: {cb_failures})"
        elif cb_state == "HALF_OPEN":
            cb_str = f"🟡 HALF_OPEN (test mode)"
        elif cb_active:
            cb_str = f"🔴 AKTIV (failures: {cb_failures})"
        else:
            cb_str = "✅ CLOSED"

        # Add error breakdown if there are errors
        if cb_errors and any(v > 0 for v in cb_errors.values()):
            error_parts = [f"{k}×{v}" for k, v in sorted(cb_errors.items(), key=lambda x: -x[1]) if v > 0]
            # fix/embeds-no-hidden-data: frueher [:4] — bei einem Ausfall
            # mit vielen Fehlerarten fehlten genau die selteneren, die den
            # Hinweis auf die Ursache tragen.
            cb_str += f"\nErrors: {', '.join(error_parts)}"
    else:
        cb_str = "🔴 AKTIV" if cb_active else "✅ Inaktiv"

    pnl_total = equity - 10_000
    pnl_pct   = (pnl_total / 10_000 * 100)

    color = COLOR_PURPLE if cb_active else _severity_color(severity)

    fields = [
        {
            "name":   "💰 Portfolio",
            "value":  (
                f"Equity:  **${equity:,.2f}**\n"
                f"Cash:    **${cash:,.2f}** ({cash_pct:.1f}%)\n"
                f"Pos:     **{position_count}**"
            ),
            "inline": True,
        },
        {
            "name":   "📉 Drawdown",
            "value":  (
                f"{dd_emoji} **{drawdown_pct:.2f}%**\n"
                f"Severity: `{severity}`\n"
                f"CB: {cb_str}"
            ),
            "inline": True,
        },
        {
            "name":   "📊 Total PnL (seit $10k)",
            "value":  f"{_pnl_emoji(pnl_pct)} **${pnl_total:+,.2f}** ({pnl_pct:+.2f}%)",
            "inline": True,
        },
    ]

    # feat/heartbeat-positions (2026-07-22): offene Positionen direkt im
    # Heartbeat — bisher nur im post_reconciler_embed (feuert nur bei
    # Close/Orphan, also selten). Sortiert nach PnL (schlechteste zuerst,
    # damit Problem-Positionen sofort ins Auge fallen).
    if positions_summary:
        _ps = sorted(positions_summary, key=lambda x: (x.get("unrealized_pnl_pct") if x.get("unrealized_pnl_pct") is not None else 0.0))
        # fix/embeds-no-hidden-data (2026-08-12): frueher _CAP = 30 mit
        # "\u2026 +N weitere". Das war KEIN Discord-Limit, sondern eine
        # gegriffene Zahl — bei 58 offenen Positionen blieben 28 unsichtbar,
        # und zwar ausgerechnet die mit dem besten PnL (sortiert ist
        # schlechteste zuerst). Ein 1024-Zeichen-Feld fasst ~55 dieser
        # Zeilen, drei Spalten also ~165 Positionen. Der Cap hat nie etwas
        # geschuetzt, nur Information unterschlagen.
        _entries = []
        for _p in _ps:
            _sym = str(_p.get("symbol") or "?")
            _pnl = _p.get("unrealized_pnl_pct")
            _nosl = " ⚠️" if _p.get("is_no_stop_loss") else ""
            if _pnl is None:
                _entries.append(f"⚪ {_sym}{_nosl}")
            else:
                _em = "🟢" if _pnl >= 0 else "🔴"
                _entries.append(f"{_em} {_sym} {_pnl:+.1f}%{_nosl}")
        _n = len(positions_summary)
        # Dreispaltig: inline-Felder rendern nebeneinander. In bis zu 3
        # moeglichst gleich grosse Spalten aufteilen (Reihenfolge nach PnL,
        # schlechteste zuerst -> obere linke Spalte).
        _per = -(-len(_entries) // 3) if _entries else 0   # ceil(n/3)
        _cols = [_entries[i:i + _per] for i in range(0, len(_entries), _per)] if _per else [[]]
        # Laeuft eine Spalte ueber 1024 Zeichen, erzeugt pack_lines_into_fields
        # eine Fortsetzungsspalte statt zu kuerzen — es geht nichts verloren.
        for _ci, _col in enumerate(_cols):
            _packed = pack_lines_into_fields(
                f"\U0001f4cb Offene Positionen ({_n})", _col, inline=True)
            for _pi, _pf in enumerate(_packed):
                if _ci > 0 or _pi > 0:
                    _pf["name"] = "\u200b"   # Ueberschrift nur ueber Spalte 1
            fields.extend(_packed)

    # T10.2: Pipeline Duration per Phase (if available)
    if phase_durations:
        sorted_phases = sorted(phase_durations.items(), key=lambda x: -x[1])
        duration_lines = []
        for name, dur in sorted_phases:
            bar_len = min(int(dur / 5), 20)  # 1 block per 5 seconds, max 20
            bar = "🟦" * bar_len if dur < 60 else ("🟧" * bar_len if dur < 120 else "🟥" * bar_len)
            duration_lines.append(f"{bar} **{name}**: {dur:.0f}s")
        total_pipeline = sum(phase_durations.values())
        duration_lines.insert(0, f"**Total Pipeline**: {total_pipeline:.0f}s")
        fields.append({
            "name":   "⏱️ Phase Duration",
            "value":  "\n".join(duration_lines),
            "inline": False,
        })

    embed = {
        "title":       f"💓 Pipeline Heartbeat — Tick #{tick}",
        "description": f"Laufzeit letzter Tick: `{elapsed_s:.0f}s`",
        "color":       color,
        "fields":      fields,
        "footer":    {"text": f"eToro RoBoCop · Tick #{tick} · alle 30min"},
        "timestamp": _ts(),
    }
    ok = _post_embed(embed, DISCORD_MAIN_CHANNEL, dry_run)
    if ok:
        insert_system_log("INFO", "discord_embeds", f"P1 Heartbeat Tick#{tick} gepostet")
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
# P7 — DATA INGESTION (yfinance)
# ═══════════════════════════════════════════════════════════════════════════════

def post_data_worker_embed(
    tier1_count: int,
    tier2_open: int,
    tier2_closed: int,
    tier2_total: int,
    total_symbols: int,
    symbols_fetched: int,
    signals_generated: int,
    signals_expired: int,
    failed_cache_size: int,
    elapsed_s: float,
    new_signals: list = None,
    market_status: str = "",
    top_candidate: dict = None,
    dry_run: bool = False,
) -> bool:
    """Data Worker Summary — data-rich Embed mit Fetch-Stats, Signalen und Märkten → #etoro-trading.

    Wird am Ende jedes Data-Worker-Runs aufgerufen (alle 5min).
    """
    if new_signals is None:
        new_signals = []

    # ── Color based on activity ─────────────────────────────────────────────
    if signals_generated > 0:
        color = COLOR_GREEN
    elif symbols_fetched < total_symbols * 0.5:
        color = COLOR_ORANGE
    else:
        color = COLOR_BLUE

    # ── Description: Fetch summary + timing ─────────────────────────────────
    fetch_pct = (symbols_fetched / total_symbols * 100) if total_symbols else 0
    desc_parts = [
        f"OHLCV geladen: **{symbols_fetched}/{total_symbols}** ({fetch_pct:.0f}%)",
        f"Dauer: **{elapsed_s:.1f}s**",
    ]
    if market_status:
        desc_parts.append(f"Märkte: {market_status}")
    desc = " · ".join(desc_parts)

    # ── Fields ──────────────────────────────────────────────────────────────
    fields = []

    # 1) Data Pipeline Stats
    pipeline_lines = [
        f"Tier 1 (Portfolio): **{tier1_count}** Symbole",
        f"Tier 2 (Watchlist): **{tier2_open}** offen / {tier2_closed} closed ({tier2_total} total)",
        f"Failed-Cache: **{failed_cache_size}** Symbole (cooldown 2d)",
    ]
    fields.append({
        "name": "📡 Data Pipeline",
        "value": "\n".join(pipeline_lines),
        "inline": True,
    })

    # 2) Signals
    signal_lines = [
        f"Neue Signale: **{signals_generated}**",
        f"Expired: **{signals_expired}**",
    ]
    if signals_generated == 0 and top_candidate:
        # feat/heartbeat-top-candidate: warum ist nichts los? Der beste
        # BUY unterhalb der Schwelle zeigt es auf einen Blick.
        signal_lines.append(
            f"Bester Kandidat: **{top_candidate.get('symbol', '?')}** "
            f"{top_candidate.get('score', 0):.0f}/{top_candidate.get('min_score', 30):.0f}"
            + (f" ({top_candidate.get('types', '')})" if top_candidate.get('types') else "")
        )
    fields.append({
        "name": "📊 Signale",
        "value": "\n".join(signal_lines),
        "inline": True,
    })

    # 3) New Signals Detail (if any)
    if new_signals:
        sig_lines = []
        for s in new_signals:
            sym = s.get("symbol", "?")
            direction = s.get("direction", "?").upper()
            score = s.get("score", 0)
            conviction = s.get("conviction", "?")
            rsi = s.get("rsi")
            dir_emoji = "🟢" if direction == "BUY" else "🔴"
            line = f"{dir_emoji} **{sym}** {direction} (Score: {score:.0f}, {conviction})"
            if rsi is not None:
                line += f" | RSI: {rsi:.1f}"
            sig_lines.append(line)
        fields.append({
            "name": "🎯 Neue Signale",
            "value": "\n".join(sig_lines),
            "inline": False,
        })

    embed = {
        "title":       f"📡 Data Worker — {symbols_fetched} Symbole geladen" + (f" ({signals_generated} Signale)" if signals_generated > 0 else ""),
        "description": desc,
        "color":       color,
        "fields":      fields,
        "footer":      {"text": "eToro RoBoCop · Data Worker · alle 5min"},
        "timestamp":   _ts(),
    }
    ok = _post_embed(embed, DISCORD_MAIN_CHANNEL, dry_run)
    if ok:
        insert_system_log("INFO", "discord_embeds", f"P8 Data Worker gepostet fetched={symbols_fetched} signals={signals_generated}")
    return ok


def post_reconciler_embed(
    equity: float,
    peak_equity: float,
    position_count: int,
    synced_count: int,
    orphan_count: int,
    trades_closed: int,
    regime: str,
    drawdown_pct: float = 0.0,
    available_cash: float = 0.0,
    positions_summary: list = None,
    dry_run: bool = False,
) -> bool:
    """Reconciler Summary — data-rich Embed mit Equity, Positionen und Regime → #etoro-trading.

    Wird am Ende jedes Reconciler-Runs aufgerufen (alle 5min).
    """
    if positions_summary is None:
        positions_summary = []

    # ── Color based on regime + drawdown ──────────────────────────────────────
    if regime in ("CRITICAL",):
        color = COLOR_RED
    elif regime in ("DRAWDOWN", "CAUTION"):
        color = COLOR_ORANGE
    else:
        color = COLOR_GREEN

    # ── Description: Portfolio overview ───────────────────────────────────────
    desc_parts = [
        f"Equity: **${equity:,.2f}**",
        f"Peak: **${peak_equity:,.2f}**",
        f"Cash: **${available_cash:,.2f}**",
    ]
    if drawdown_pct > 0.5:
        desc_parts.append(f"Drawdown: **-{drawdown_pct:.1f}%**")
    desc = " · ".join(desc_parts)

    # ── Fields ────────────────────────────────────────────────────────────────
    fields = []

    # 1) Sync Stats
    sync_lines = [
        f"Positionen synchronisiert: **{synced_count}**",
        f"Orphans entfernt: **{orphan_count}**",
        f"Trades geschlossen: **{trades_closed}**",
    ]
    fields.append({
        "name": "🔄 Sync",
        "value": "\n".join(sync_lines),
        "inline": True,
    })

    # 2) Regime & Risk
    regime_emoji = {"NORMAL": "🟢", "CAUTION": "🟡", "DRAWDOWN": "🟠", "CRITICAL": "🔴"}.get(regime, "⚪")
    risk_lines = [
        f"Regime: {regime_emoji} **{regime}**",
        f"Aktive Positionen: **{position_count}**",
    ]
    if drawdown_pct > 0.5:
        risk_lines.append(f"Drawdown seit Peak: **-{drawdown_pct:.1f}%**")
    fields.append({
        "name": "⚠️ Regime & Risiko",
        "value": "\n".join(risk_lines),
        "inline": True,
    })

    # 3) Positions detail — all positions up to Discord 1024-char field limit
    if positions_summary:
        pos_lines = []
        for p in positions_summary:
            sym = p.get("symbol", "?")
            amount = p.get("amount_usd", 0) or 0
            pnl_pct = p.get("unrealized_pnl_pct")
            sl_rate = p.get("stop_loss_rate")
            no_sl = p.get("is_no_stop_loss", 0)

            emoji = "🟢" if (pnl_pct is not None and pnl_pct >= 0) else "🔴"
            line = f"{emoji} **{sym}** ${amount:,.2f}"
            if pnl_pct is not None:
                line += f" ({pnl_pct:+.1f}%)"
            if no_sl:
                line += " ⚠️ No SL"
            elif sl_rate:
                line += f" | SL: ${sl_rate:,.2f}"
            pos_lines.append(line)

        # fix/embeds-no-hidden-data (2026-08-12): frueher wurde hier bei
        # 1020 Zeichen abgeschnitten und der Rest zu "_+N weitere..._"
        # zusammengefasst — bei 58 Positionen blieb rund die Haelfte
        # unsichtbar. pack_lines_into_fields legt stattdessen so viele
        # Folgefelder an, wie die Zeilen brauchen.
        fields.extend(pack_lines_into_fields(
            "\U0001f4bc Positionen", pos_lines, inline=False))

    embed = {
        "title":       f"🔄 Reconciler — ${equity:,.2f}",
        "description": desc,
        "color":       color,
        "fields":      fields,
        "footer":      {"text": "eToro RoBoCop · Reconciler · alle 5min"},
        "timestamp":   _ts(),
    }
    ok = _post_embed(embed, DISCORD_MAIN_CHANNEL, dry_run)
    if ok:
        insert_system_log("INFO", "discord_embeds", f"P2 Reconciler gepostet equity={equity:.2f} regime={regime}")
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
# P10 — SYSTEM ALERT (Pipeline Watchdog, Regime-Wechsel, kritische Fehler)
# ═══════════════════════════════════════════════════════════════════════════════

def post_alert_embed(
    title: str,
    description: str,
    severity: str = "WARNING",   # "INFO" | "WARNING" | "CRITICAL"
    fields: list = None,
    channel: str = "main",       # "main" | "trades"
    dry_run: bool = False,
) -> bool:
    """Generisches System-Alert Embed — #etoro-trading (default) oder #etoro-trades.

    Wird genutzt von:
      - pipeline_watchdog.py  (Pipeline stalled / Mutex stale)
      - unified_pipeline.py   (Regime-Transition, CB-Aktivierung)
      - reconciler_service.py (Ghost / Orphan Orders)
      - Beliebige kritische Ereignisse
    """
    color_map = {
        "INFO":     COLOR_BLUE,
        "WARNING":  COLOR_ORANGE,
        "CRITICAL": COLOR_RED,
    }
    emoji_map = {
        "INFO":     "ℹ️",
        "WARNING":  "⚠️",
        "CRITICAL": "🚨",
    }
    sev = severity.upper()
    color  = color_map.get(sev, COLOR_ORANGE)
    emoji  = emoji_map.get(sev, "⚠️")
    ch_id  = DISCORD_TRADE_CHANNEL if channel == "trades" else DISCORD_MAIN_CHANNEL

    embed = {
        "title":       f"{emoji} {title}",
        "description": description,
        "color":       color,
        "fields":      fields or [],
        "footer":      {"text": f"eToro RoBoCop · System Alert · {sev}"},
        "timestamp":   _ts(),
    }

    ok = _post_embed(embed, ch_id, dry_run)
    if ok:
        log_level = "WARN" if sev == "WARNING" else ("CRITICAL" if sev == "CRITICAL" else "INFO")
        insert_system_log(log_level, "discord_embeds", f"P10 Alert: {title[:120]}")
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
# P11 — TRADE EXECUTION (BUY / SELL FILLED oder FAILED)
# ═══════════════════════════════════════════════════════════════════════════════

def post_trade_filled_embed(
    symbol: str,
    direction: str,          # "BUY" | "SELL" | "CLOSE"
    amount_usd: float,
    position_id: str = "",
    entry_price: float = 0.0,
    sl_price: float = 0.0,
    sl_pct: float = 0.0,
    reason: str = "",
    equity: float = 0.0,
    dry_run: bool = False,
) -> bool:
    """Trade FILLED Embed → #etoro-trades.

    Wird gepostet wenn ein BUY/SELL/CLOSE von eToro bestätigt wurde.
    """
    if direction.upper() == "BUY":
        color  = COLOR_GREEN
        emoji  = "✅"
        action = "BUY FILLED"
    elif direction.upper() in ("SELL", "CLOSE"):
        color  = COLOR_TEAL
        emoji  = "💰"
        action = f"{direction.upper()} FILLED"
    else:
        color  = COLOR_BLUE
        emoji  = "✅"
        action = f"{direction.upper()} FILLED"

    fields = [
        {"name": "💵 Betrag",    "value": f"`${amount_usd:,.2f}`",          "inline": True},
        # fix/filled-embed-fields (2026-07-20, PARRO.PA): im Pickup-Pfad
        # (deferte Order aufgeloest) ist der Fill-Kurs noch unbekannt —
        # der Reconciler traegt ihn nach. Ehrlich sagen statt "-", und den
        # Stop wenigstens als Prozent zeigen (der ist immer bekannt).
        {"name": "📈 Kurs",      "value": f"`${entry_price:,.4f}`" if entry_price else "`folgt (Backfill)`", "inline": True},
        {"name": "🛡️ Stop-Loss", "value": (
            f"`${sl_price:,.4f}` ({sl_pct:.1f}%)" if sl_price
            else (f"`-{sl_pct:.1f}%`" if sl_pct else "`–`")
        ), "inline": True},
    ]
    if position_id:
        fields.append({"name": "🆔 Position-ID", "value": f"`{position_id}`", "inline": True})
    if equity:
        fields.append({"name": "💼 Equity", "value": f"`${equity:,.2f}`", "inline": True})
    if reason:
        fields.append({"name": "📋 Grund", "value": f"`{reason[:100]}`", "inline": False})

    embed = {
        "title":       f"{emoji} {action} — {resolve_instrument_display(symbol)}",
        "description": f"Order erfolgreich ausgeführt",
        "color":       color,
        "fields":      fields,
        "footer":      {"text": "eToro RoBoCop · Trade Execution"},
        "timestamp":   _ts(),
    }

    ok = _post_embed(embed, DISCORD_TRADE_CHANNEL, dry_run)
    if ok:
        insert_system_log("INFO", "discord_embeds",
                          f"P11 Trade FILLED: {direction} {symbol} ${amount_usd:.2f}")
    return ok


def post_trade_failed_embed(
    symbol: str,
    direction: str,
    amount_usd: float,
    error: str = "",
    reason: str = "",
    is_ghost: bool = False,
    blocked: bool = False,
    dry_run: bool = False,
) -> bool:
    """Trade FAILED / GHOST / BLOCKIERT Embed → #etoro-trades.

    Wird gepostet wenn ein BUY/SELL von eToro abgelehnt, als Ghost erkannt
    oder von einem deterministischen Risiko-Gate gestoppt wurde.

    fix/risk-block-framing (2026-07-20): ein Spread-/Slippage-Gate-Block
    ist KEIN Fehler — das System hat korrekt Kapital geschuetzt (HAYD.L
    3.77% Spread). Rotes "TRADE FAILED / Fehler" war irrefuehrend; solche
    Blocks sind jetzt orange "BLOCKIERT — Risiko-Schutz".
    """
    display = resolve_instrument_display(symbol)
    if is_ghost:
        color  = COLOR_PURPLE
        emoji  = "👻"
        title  = f"GHOST ORDER — {display}"
        desc   = "Order von eToro akzeptiert, aber keine Position erstellt"
    elif blocked:
        color  = COLOR_ORANGE
        emoji  = "🛡️"
        title  = f"TRADE BLOCKIERT — {display}"
        desc   = "Order vom Risiko-Gate gestoppt — kein Fehler, Kapital geschützt"
    else:
        color  = COLOR_RED
        emoji  = "❌"
        title  = f"TRADE FAILED — {display}"
        desc   = "Order konnte nicht ausgeführt werden"

    fields = [
        {"name": "📋 Richtung", "value": f"`{direction.upper()}`",    "inline": True},
        {"name": "💵 Betrag",   "value": f"`${amount_usd:,.2f}`",     "inline": True},
    ]
    if error:
        _err_label = "🛡️ Risiko-Gate" if blocked else "🔴 Fehler"
        fields.append({"name": _err_label, "value": f"```{error[:200]}```", "inline": False})
    if reason:
        fields.append({"name": "📝 Grund",  "value": f"`{reason[:100]}`",    "inline": False})

    embed = {
        "title":       f"{emoji} {title}",
        "description": desc,
        "color":       color,
        "fields":      fields,
        "footer":      {"text": "eToro RoBoCop · " + ("Risiko-Schutz" if blocked else "Trade Failure")},
        "timestamp":   _ts(),
    }

    ok = _post_embed(embed, DISCORD_TRADE_CHANNEL, dry_run)
    if ok:
        level = "INFO" if blocked else ("WARN" if is_ghost else "ERROR")
        _kind = "BLOCKIERT" if blocked else ("GHOST" if is_ghost else "FAILED")
        insert_system_log(level, "discord_embeds",
                          f"P11 Trade {_kind}: {direction} {symbol} ${amount_usd:.2f}")
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
# P12 — REGIME-WECHSEL (DRAWDOWN / RECOVERY / NORMAL)
# ═══════════════════════════════════════════════════════════════════════════════

def post_regime_change_embed(
    old_regime: str,
    new_regime: str,
    drawdown_pct: float,
    equity: float,
    peak_equity: float,
    reason: str = "",
    dry_run: bool = False,
) -> bool:
    """Regime-Transition Embed → #etoro-trading.

    Wird gepostet wenn das System zwischen NORMAL / DRAWDOWN / RECOVERY wechselt.
    """
    if new_regime == "DRAWDOWN":
        color  = COLOR_RED
        emoji  = "🔴"
        action = "DRAWDOWN-Regime aktiviert — BUYs blockiert"
    elif new_regime == "RECOVERY":
        color  = COLOR_YELLOW
        emoji  = "🟡"
        action = "RECOVERY-Regime — defensiver Betrieb"
    elif new_regime == "NORMAL":
        color  = COLOR_GREEN
        emoji  = "🟢"
        action = "NORMAL-Regime — Trading freigegeben"
    else:
        color  = COLOR_GREY
        emoji  = "⚪"
        action = f"Regime: {new_regime}"

    pnl_total = equity - peak_equity
    pnl_pct   = (pnl_total / peak_equity * 100) if peak_equity > 0 else 0.0

    fields = [
        {"name": "📊 Drawdown",    "value": f"`{drawdown_pct:.2f}%`",         "inline": True},
        {"name": "💼 Equity",      "value": f"`${equity:,.2f}`",               "inline": True},
        {"name": "🏔️ Peak",        "value": f"`${peak_equity:,.2f}`",          "inline": True},
        {"name": "📉 PnL vs Peak", "value": f"`${pnl_total:+,.2f}` ({pnl_pct:+.1f}%)", "inline": True},
        {"name": "↩️ Vorher",      "value": f"`{old_regime}`",                 "inline": True},
        {"name": "➡️ Jetzt",       "value": f"`{new_regime}`",                 "inline": True},
    ]
    if reason:
        fields.append({"name": "📋 Grund", "value": f"`{reason}`", "inline": False})

    embed = {
        "title":       f"{emoji} Regime-Wechsel: {old_regime} → {new_regime}",
        "description": action,
        "color":       color,
        "fields":      fields,
        "footer":      {"text": "eToro RoBoCop · Adaptive Regime System"},
        "timestamp":   _ts(),
    }

    ok = _post_embed(embed, DISCORD_MAIN_CHANNEL, dry_run)
    if ok:
        insert_system_log("WARNING", "discord_embeds",
                          f"P12 Regime-Wechsel: {old_regime}→{new_regime} (DD={drawdown_pct:.2f}%)")
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
# P13 — PIPELINE WATCHDOG STATUS
# ═══════════════════════════════════════════════════════════════════════════════

def post_watchdog_alert_embed(
    status: str = "",             # "STALLED" | "MUTEX_STALE" | "HEALTHY" | "MISSING" | "GHOST_ORDER_ESCALATION"
    last_tick_age_s: float = 0.0,
    last_tick: int = 0,
    details: str = "",
    dry_run: bool = False,
    # c) Ghost-order escalation params (optional — used when status="GHOST_ORDER_ESCALATION")
    alert_type: str | None = None,
    symbol: str | None = None,
    message: str | None = None,
    severity: str | None = None,
) -> bool:
    """Pipeline Watchdog Alert → #etoro-trading.

    Nur bei PROBLEMen posten (STALLED, MUTEX_STALE, MISSING, GHOST_ORDER_ESCALATION).
    Bei HEALTHY → kein Post (oder nur auf explizite Anfrage).
    """
    # c) Ghost-order escalation path
    if status == "GHOST_ORDER_ESCALATION" or alert_type == "GHOST_ORDER_ESCALATION":
        st = severity or "high"
        color  = COLOR_RED if st == "critical" else COLOR_ORANGE
        emoji  = "🚨" if st == "critical" else "⚠️"
        sym_display = resolve_instrument_display(symbol) if symbol else "?"
        title  = f"{emoji} Ghost-Order Eskalation — {sym_display}"
        desc   = message or f"{sym_display}: Multiple consecutive ghost failures detected"

        fields = []
        if symbol:
            fields.append({"name": "📌 Instrument", "value": f"`{sym_display}`", "inline": True})
        if severity:
            sev_emoji = "💀" if severity == "critical" else "⚠️"
            fields.append({"name": "🔥 Severity", "value": f"{sev_emoji} `{severity.upper()}`", "inline": True})
        if details:
            fields.append({"name": "📋 Details", "value": f"`{details[:300]}`", "inline": False})

        action_text = (
            "⚠️ Manuelle Prüfung empfohlen: Check eToro-Instrument-Status, API-Limits, Account-Restriktionen."
            if severity != "critical"
            else "💀 BLACKLIST (9+ Fails) — rollierende 7-Tage-Sperre, danach automatisch 1 neuer Versuch (Auto-Expiry)."
        )
        fields.append({"name": "🔧 Aktion", "value": action_text, "inline": False})

        embed = {
            "title":       title,
            "description": desc,
            "color":       color,
            "fields":      fields,
            "footer":      {"text": "eToro RoBoCop · Ghost-Order Watchdog"},
            "timestamp":   _ts(),
        }

        ok = _post_embed(embed, DISCORD_MAIN_CHANNEL, dry_run)
        if ok:
            insert_system_log("WARNING", "discord_embeds", f"P13 Watchdog Alert: GHOST_ORDER_ESCALATION {symbol}")
        return ok

    if status == "STALLED":
        color  = COLOR_RED
        emoji  = "🛑"
        title  = "Pipeline STALLED"
        desc   = f"Pipeline hat seit `{last_tick_age_s:.0f}s` keinen neuen Tick produziert"
    elif status == "MUTEX_STALE":
        color  = COLOR_ORANGE
        emoji  = "🔒"
        title  = "Pipeline Mutex STALE"
        desc   = "Abgestürzter Pipeline-Lauf — Mutex automatisch bereinigt"
    elif status == "MISSING":
        color  = COLOR_RED
        emoji  = "❓"
        title  = "Pipeline MISSING"
        desc   = "Kein Heartbeat gefunden — Pipeline möglicherweise nie gestartet"
    else:  # HEALTHY
        return True  # Kein Post bei gesunden Status

    fields = []
    if last_tick > 0:
        fields.append({"name": "🔢 Letzter Tick", "value": f"`#{last_tick}`", "inline": True})
    if last_tick_age_s > 0:
        fields.append({"name": "⏱️ Alter", "value": f"`{last_tick_age_s:.0f}s`", "inline": True})
    if details:
        fields.append({"name": "📋 Details", "value": f"```{details[:300]}```", "inline": False})

    fields.append({
        "name":  "🔧 Aktion",
        "value": "Watchdog prüft automatisch. Bei wiederholtem Stall → Pipeline-Neustart.",
        "inline": False,
    })

    embed = {
        "title":       f"{emoji} {title}",
        "description": desc,
        "color":       color,
        "fields":      fields,
        "footer":      {"text": "eToro RoBoCop · Pipeline Watchdog"},
        "timestamp":   _ts(),
    }

    ok = _post_embed(embed, DISCORD_MAIN_CHANNEL, dry_run)
    if ok:
        insert_system_log("WARNING", "discord_embeds", f"P13 Watchdog Alert: {status}")
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
# P14 — POSITION CLOSED (SL-Trigger, Konzentrations-Bereinigung, manuell)
# ═══════════════════════════════════════════════════════════════════════════════

def _build_position_closed_embed(
    symbol: str,
    amount_usd: float,
    position_id: str = "",
    entry_price: float = 0.0,
    close_price: float = 0.0,
    pnl_usd: "float | None" = None,
    pnl_pct: "float | None" = None,
    reason: str = "",
    close_pct: float = 100.0,
    keep_chart_image: bool = False,
) -> dict:
    """Baut das CLOSED/TEILVERKAUF-Embed-Dict (fuer Post UND Edit).

    feat/pnl-nachreport (2026-07-28): pnl_usd/pnl_pct sind jetzt Optional.
    None = PnL (noch) unbekannt → neutrales Grau + "P/L folgt (Nachreport)".
    Vorher defaulteten beide auf 0.0 und ein Close ohne API-Daten wurde als
    teal "$+0.00 Gewinn" gepostet. Ausserdem: pnl_pct==0.0 wird jetzt
    angezeigt (vorher Truthiness-Check → glatte Closes verloren die Prozente).

    keep_chart_image=True traegt die attachment-Referenz des Original-Posts
    weiter — noetig beim Edit, damit Discord den Chart-Anhang behaelt.
    """
    if pnl_usd is None:
        color = COLOR_GREY
        emoji = "⏳"
        result = "P/L folgt (Nachreport)"
        pnl_value = "`folgt (Nachreport)`"
    else:
        if pnl_usd >= 0:
            color = COLOR_TEAL
            emoji = "💰"
            result = f"Gewinn: **${pnl_usd:+.2f}**"
        else:
            color = COLOR_RED
            emoji = "🔴"
            result = f"Verlust: **${pnl_usd:+.2f}**"
        pnl_value = (f"`${pnl_usd:+.2f}` ({pnl_pct:+.1f}%)"
                     if pnl_pct is not None else f"`${pnl_usd:+.2f}`")

    fields = [
        {"name": ("💵 Teil-Betrag" if (close_pct and close_pct < 99.5) else "💵 Betrag"),
         "value": f"`${amount_usd:,.2f}`" + (f" ({close_pct:.0f}%)" if (close_pct and close_pct < 99.5) else ""), "inline": True},
        {"name": "📊 PnL",      "value": pnl_value, "inline": True},
        {"name": "📋 Grund",    "value": f"`{reason[:80]}`" if reason else "`–`", "inline": False},
    ]
    if entry_price:
        fields.append({"name": "📈 Entry", "value": f"`${entry_price:,.4f}`", "inline": True})
    if close_price:
        fields.append({"name": "📉 Close", "value": f"`${close_price:,.4f}`", "inline": True})
    if position_id:
        fields.append({"name": "🆔 Position", "value": f"`{position_id}`", "inline": True})

    # feat/partial-close-embed (2026-07-22): Teilverkaeufe (close_pct < 100)
    # klar als solche kennzeichnen — vorher titelten sie "POSITION CLOSED"
    # wie ein Full-Close und waren im Channel nicht unterscheidbar.
    _partial = bool(close_pct) and close_pct < 99.5
    if _partial:
        _title = f"✂️ TEILVERKAUF {close_pct:.0f}% — {resolve_instrument_display(symbol)}"
        _desc = f"{result} · Rest der Position bleibt offen"
        _foot = "eToro RoBoCop · Partial Close (Profit-Taking)"
    else:
        _title = f"{emoji} POSITION CLOSED — {resolve_instrument_display(symbol)}"
        _desc = result
        _foot = "eToro RoBoCop · Position Close"
    embed = {
        "title":       _title,
        "description": _desc,
        "color":       color,
        "fields":      fields,
        "footer":      {"text": _foot},
        "timestamp":   _ts(),
    }
    if keep_chart_image:
        embed["image"] = {"url": "attachment://chart.png"}
    return embed


def post_position_closed_embed(
    symbol: str,
    amount_usd: float,
    position_id: str = "",
    entry_price: float = 0.0,
    close_price: float = 0.0,
    pnl_usd: "float | None" = None,
    pnl_pct: "float | None" = None,
    reason: str = "",
    close_pct: float = 100.0,
    dry_run: bool = False,
) -> "str | bool":
    """Position CLOSED / TEILVERKAUF Embed → #trades.

    Wird gepostet wenn eine Position geschlossen wird:
    - SL-Trigger (Rule 1: -3% Hard Close, -4% Emergency)
    - Konzentrationslimit-Verletzung (Rule 2)
    - Reconciler-Close (Position nicht mehr in API)
    - LLM EXIT/TIGHTEN Teilverkauf
    - Manuelle Schließung

    pnl_usd=None ⇒ neutrales "P/L folgt (Nachreport)"-Embed; der Reconciler
    editiert es spaeter mit den finalen Zahlen (siehe _edit_embed).
    Rueckgabe: Message-ID (String) bei Erfolg, sonst False (True im Dry-Run).
    """
    embed = _build_position_closed_embed(
        symbol=symbol, amount_usd=amount_usd, position_id=position_id,
        entry_price=entry_price, close_price=close_price,
        pnl_usd=pnl_usd, pnl_pct=pnl_pct, reason=reason, close_pct=close_pct,
    )
    ok = _post_embed(embed, DISCORD_TRADE_CHANNEL, dry_run)
    if ok:
        _pnl_txt = f"PnL=${pnl_usd:+.2f}" if pnl_usd is not None else "PnL=folgt"
        level = "INFO" if (pnl_usd is None or pnl_usd >= 0) else "WARN"
        # diag/close-embed-caller (2026-09-10): Zwischen dem 2026-08-28
        # 02:31:45 — der Sekunde, in der Trade 682 (CAR.AX) finalisiert
        # wurde — und dem 2026-09-10 21:33 gingen 511 Close-Embeds fuer
        # CAR.AX nach #trades, immer exakt 15 am Stueck, alle mit
        # "$0.00 PnL=folgt", waehrend CAR.AX genau EIN CLOSE-Event vom
        # 28.08. hat. Der Verursacher liess sich statisch nicht bestimmen:
        # alle 12 Aufrufstellen rufen record_posted_event(), das aber
        # dedupliziert, sobald die Position schon ein CLOSE-Event hat —
        # in trade_events steht deshalb keine Spur.
        #
        # amount_usd == 0 ist die Signatur der Anomalie (ein echter Close
        # hat immer einen Betrag). Nur dann den Aufrufer mitschreiben:
        # kein Rauschen im Normalbetrieb, aber beim naechsten Auftreten
        # steht die Zeile im Log statt einer weiteren Ratesitzung.
        _caller = ""
        if not amount_usd:
            try:
                import traceback
                _fr = [f for f in traceback.extract_stack()[:-1]
                       if "discord_embeds" not in f.filename]
                if _fr:
                    _last = _fr[-1]
                    _caller = (f"  [Aufrufer: {Path(_last.filename).name}"
                               f":{_last.lineno} {_last.name}]")
            except Exception:
                pass
        insert_system_log(level, "discord_embeds",
                          f"P14 Position Closed: {symbol} ${amount_usd:.2f} "
                          f"{_pnl_txt}{_caller}")
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
# P14b — DAILY REPORT (feat/daily-report → #reports)
# ═══════════════════════════════════════════════════════════════════════════════

def post_daily_report_embed(
    report_date: str,
    realized_pnl_usd: "float | None",
    wins: int,
    losses: int,
    unconfirmed: int,
    open_count: int,
    open_exposure_usd: float,
    unrealized_pnl_usd: float,
    sections: "list[tuple[str, list[str]]]",
    dry_run: bool = False,
) -> "str | bool":
    """Tagesreport-Embed → #reports.

    sections: Liste (Feld-Name, Zeilen) — z.B. ("🟢 Eröffnungen (2)", [...]).
    Zeilen werden zu 1024-Zeichen-Chunks gebündelt (Discord-Feld-Limit);
    laeuft ein Abschnitt ueber, entstehen Folgefelder mit "…"-Suffix im Namen.
    Chart (daily_grid_png) vorher via attach_chart() anhaengen — optional.
    """
    if realized_pnl_usd is None:
        color = COLOR_GREY
        realized_txt = "–"
    else:
        color = COLOR_TEAL if realized_pnl_usd >= 0 else COLOR_RED
        realized_txt = f"**${realized_pnl_usd:+.2f}**"

    total = wins + losses
    win_txt = f"{wins}/{total} ({wins / total * 100:.0f}%)" if total else "–"
    desc = (f"Realisiert: {realized_txt} · Win-Rate: {win_txt}"
            + (f" · {unconfirmed} ohne bestätigtes P/L" if unconfirmed else "")
            + f"\nOffen: {open_count} Positionen · Exposure `${open_exposure_usd:,.0f}`"
              f" · unrealisiert `${unrealized_pnl_usd:+,.2f}`")

    fields = []
    for name, lines in sections:
        if not lines:
            continue
        chunk: list[str] = []
        size = 0
        part = 0
        for line in lines:
            if size + len(line) + 1 > 1000 and chunk:
                fields.append({"name": name if part == 0 else f"{name} …",
                               "value": "\n".join(chunk), "inline": False})
                chunk, size, part = [], 0, part + 1
            chunk.append(line)
            size += len(line) + 1
        if chunk:
            fields.append({"name": name if part == 0 else f"{name} …",
                           "value": "\n".join(chunk), "inline": False})

    embed = {
        "title":       f"📊 Tagesreport — {report_date}",
        "description": desc,
        "color":       color,
        "fields":      fields,
        "footer":      {"text": "eToro RoBoCop · Daily Report"},
        "timestamp":   _ts(),
    }
    ok = _post_embed(embed, DISCORD_REPORTS_CHANNEL, dry_run)
    if ok:
        insert_system_log("INFO", "discord_embeds",
                          f"P14b Daily Report gepostet: {report_date}")
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
# P15 — KILL SWITCH ALERT
# ═══════════════════════════════════════════════════════════════════════════════

def post_main_portfolio_embeds(
    account: dict,
    diff: dict,
    sections: "list[tuple[str, list[str], bool]]",
    llm_review: "dict | None" = None,
    news: "list[tuple[str, list[str]]] | None" = None,
    dry_run: bool = False,
) -> "str | bool":
    """Haupt-Konto-Report -> #reports (feat/main-portfolio-report 2026-08-20).

    BEWUSST mehrteilig: Kopf-Embed mit Kennzahlen + Chart, danach die
    Detail-Abschnitte. _post_embed verteilt automatisch auf Folge-Embeds,
    wenn 25 Felder oder 6000 Zeichen ueberschritten werden — hier wird
    nichts gekuerzt (Invariante fix/embeds-no-hidden-data).

    sections: (Titel, Zeilen, inline) — Zeilen gehen durch
    pack_lines_into_fields, laufen also in Folgefelder statt abgeschnitten
    zu werden.

    Chart vorher via attach_chart() anhaengen.
    """
    eq = account.get("equity", 0.0)
    pnl = account.get("unrealized_pnl", 0.0)
    d_eq = diff.get("equity_delta")
    d_pct = diff.get("equity_delta_pct")

    if diff.get("is_baseline"):
        color = COLOR_BLUE
        delta_txt = "_Erster Lauf — dies ist die Vergleichsbasis fuer morgen._"
    else:
        color = COLOR_GREEN if (d_eq or 0) >= 0 else COLOR_RED
        pct_txt = f" ({d_pct:+.2f}%)" if d_pct is not None else ""
        seit = (diff.get("prev_taken_at") or diff.get("prev_date") or "?")[:16]
        delta_txt = f"Seit letztem Report ({seit}): **${d_eq:+,.2f}**{pct_txt}"

    # Kennzahlen-Kopf. Die Mirror-Differenz wird ausgewiesen, nicht
    # weggerechnet: Positions-P/L und Portfolio-P/L weichen um das
    # Innenleben kopierter Portfolios ab.
    desc = (
        f"**Depotwert: ${eq:,.2f}**\n{delta_txt}\n\n"
        f"Investiert `${account.get('invested', 0):,.2f}` · "
        f"Cash `${account.get('credit', 0):,.2f}` · "
        f"{account.get('position_count', 0)} Positionen"
    )

    fields: list[dict] = [
        {"name": "💰 Unrealisiert (gesamt)",
         "value": f"**${pnl:+,.2f}**", "inline": True},
        {"name": "📊 davon Positionen",
         "value": f"${account.get('positions_pnl', 0):+,.2f}", "inline": True},
        {"name": "👥 davon Copy-Trading",
         "value": f"${account.get('mirror_pnl', 0):+,.2f}", "inline": True},
    ]

    if account.get("mirror_count"):
        fields.append({
            "name": f"👥 Copy-Trading ({account['mirror_count']})",
            "value": (f"Eingesetzt `${account.get('mirror_invested', 0):,.2f}` · "
                      f"frei `${account.get('mirror_available', 0):,.2f}` · "
                      f"realisiert `${account.get('mirror_net_profit', 0):+,.2f}`"),
            "inline": False,
        })

    for title, lines, inline in sections:
        if lines:
            fields.extend(pack_lines_into_fields(title, lines, inline=inline))

    if llm_review:
        verdict = str(llm_review.get("verdict") or "—")
        fields.append({
            "name": f"🤖 KI-Einschätzung — {verdict}",
            "value": str(llm_review.get("summary") or "_keine_")[:1020],
            "inline": False,
        })
        for label, key in (("✅ Stärken", "strengths"), ("⚠️ Risiken", "risks"),
                           ("🎯 Beobachten", "watch")):
            items = llm_review.get(key) or []
            if items:
                fields.extend(pack_lines_into_fields(
                    label, [f"• {str(i)}" for i in items], inline=False))

    if news:
        for sym, heads in news:
            if heads:
                fields.extend(pack_lines_into_fields(
                    f"📰 {sym}", [f"• {h}" for h in heads], inline=False))

    embed = {
        "title":       f"📈 Hauptkonto — Tagesreport {account.get('snapshot_date', '')}",
        "description": desc,
        "color":       color,
        "fields":      fields,
        "footer":      {"text": "eToro RoBoCop · Hauptkonto · täglich"},
        "timestamp":   _ts(),
    }
    ok = _post_embed(embed, DISCORD_REPORTS_CHANNEL, dry_run)
    if ok and not dry_run:
        insert_system_log("INFO", "discord_embeds",
                          f"P16 Hauptkonto-Report {account.get('snapshot_date','')} gepostet")
    return ok


def post_kill_switch_embed(
    reason: str = 'Manual kill switch',
    dry_run: bool = False,
) -> bool:
    """Kill Switch Aktivierungs-Alert → #etoro-trading (MAIN).

    Wird gepostet wenn data/kill_switch.flag erstellt wird (Source of Truth:
    bot.core.kill_switch — NICHT /tmp, das WSL beim Reboot leert).
    Blockiert alle neuen BUYs durch Erzwingen von CRITICAL-Regime.
    """
    embed = {
        "title":       "🔴 KILL SWITCH AKTIVIERT — eToro Bot gestoppt",
        "description": (
            "Der Kill Switch ist aktiv. **Alle neuen BUYs sind blockiert.**\n"
            "Bestehende Positionen werden weiterhin per SL überwacht.\n\n"
            f"**Deaktivieren:** `rm data/kill_switch.flag` (im Projekt-Root)"
        ),
        "color":       COLOR_RED,
        "fields": [
            {
                "name":   "📋 Grund",
                "value":  f"`{reason[:200]}`",
                "inline": False,
            },
            {
                "name":   "⚙️ Forced Regime",
                "value":  "`CRITICAL` — Nur VERY_HIGH Conviction BUYs erlaubt",
                "inline": True,
            },
            {
                "name":   "📉 Risk Scalar",
                "value":  "`0.25` (25% der normalen Positionsgrösse)",
                "inline": True,
            },
        ],
        "footer":    {"text": "eToro RoBoCop · Kill Switch V5 · data/kill_switch.flag"},
        "timestamp": _ts(),
    }
    ok = _post_embed(embed, DISCORD_MAIN_CHANNEL, dry_run)
    if ok:
        insert_system_log("WARNING", "discord_embeds", f"P15 Kill Switch alert gepostet: {reason[:80]}")
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
# P16 — DISCOVERY CANDIDATES (Data-Rich Ranking)
# ═══════════════════════════════════════════════════════════════════════════════

_RANK_EMOJI = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]


def _rsi_emoji(rsi: Optional[float]) -> str:
    if rsi is None:
        return "⚪"
    if rsi <= 30:
        return "🟢"   # oversold — Kaufzone
    if rsi >= 70:
        return "🔴"   # overbought
    return "⚪"


def _trend_str(macd_hist: Optional[float], bb_pct: Optional[float]) -> str:
    """Menschenlesbare Trend-Einschätzung aus MACD-Histogramm + Bollinger %B."""
    if macd_hist is None:
        trend = "— unklar"
    elif macd_hist > 0:
        trend = "↗️ Aufwärts (MACD+)"
    else:
        trend = "↘️ Abwärts (MACD−)"
    if bb_pct is not None:
        if bb_pct <= 0.2:
            trend += " · nahe unterem BB-Band"
        elif bb_pct >= 0.8:
            trend += " · nahe oberem BB-Band"
    return trend


def _portfolio_fit(symbol: str) -> str:
    """Portfolio-Fit: ist das Symbol bereits im Portfolio? Fail-open bei DB-Fehler."""
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{_TRADING_DB_PATH}?mode=ro", uri=True, timeout=3)
        try:
            row = conn.execute(
                "SELECT SUM(amount_usd) FROM portfolio_snapshot WHERE symbol = ? COLLATE NOCASE",
                (symbol,),
            ).fetchone()
        finally:
            conn.close()
        if row and row[0]:
            return f"⚠️ Bereits im Portfolio (${row[0]:,.0f} Exposure)"
        return "✅ Neu — keine Überschneidung"
    except Exception:
        return "—"


def post_risk_worker_embed(
    checked: int,
    closed: int,
    regime: str,
    equity: float,
    trailing_break_evens: int = 0,
    trailing_partials: int = 0,
    trailing_errors: list = None,
    sl_warnings: int = 0,
    sell_exits_closed: int = 0,
    concentration_closed: int = 0,
    concentration_warned: int = 0,
    kill_switch_active: bool = False,
    positions_summary: list = None,
    dry_run: bool = False,
) -> bool:
    """Risk Worker Summary — data-rich Embed mit allen Risikometriken → #etoro-trading.

    Wird am Ende jedes Risk-Worker-Runs aufgerufen (alle 5min).
    Nur posten bei Events (closed > 0, trailing actions, regime change, kill switch)
    ODER als periodischer Status alle ~6 Ticks (~30min).
    """
    if positions_summary is None:
        positions_summary = []
    if trailing_errors is None:
        trailing_errors = []

    # ── Color based on regime / events ──────────────────────────────────────
    if kill_switch_active:
        color = COLOR_RED
    elif closed > 0:
        color = COLOR_ORANGE
    elif regime in ("CRITICAL", "CIRCUIT_BREAKER"):
        color = COLOR_RED
    elif regime == "CAUTION":
        color = COLOR_YELLOW
    else:
        color = COLOR_TEAL

    # ── Description: Equity + Regime ────────────────────────────────────────
    ks_badge = "🛑 KILL SWITCH AKTIV" if kill_switch_active else ""
    desc = f"Equity: **${equity:,.2f}** · Regime: **{regime}** {ks_badge}"

    # ── Fields ──────────────────────────────────────────────────────────────
    fields = []

    # 1) Risk Summary
    risk_lines = [
        f"Positionen geprüft: **{checked}**",
        f"SL geschlossen:     **{closed}**",
        f"SL Warnungen:       **{sl_warnings}**",
    ]
    if sell_exits_closed > 0:
        risk_lines.append(f"SELL-Exits:         **{sell_exits_closed}**")
    if concentration_closed > 0:
        risk_lines.append(f"Konzentration Fix:  **{concentration_closed}**")
    if concentration_warned > 0:
        risk_lines.append(f"Konzentr. Warnung:  **{concentration_warned}**")
    fields.append({
        "name": "🛡️ Risiko-Status",
        "value": "\n".join(risk_lines),
        "inline": True,
    })

    # 2) Trailing Stop / Profit-Taking
    trailing_lines = [
        f"Break-Evens armed: **{trailing_break_evens}**",
        f"Partial Closes:    **{trailing_partials}**",
    ]
    if trailing_errors:
        trailing_lines.append(f"⚠️ Fehler:         **{len(trailing_errors)}**")
        for err in trailing_errors:
            trailing_lines.append(f"  • {str(err)[:100]}")
    fields.append({
        "name": "📈 Trailing Stop",
        "value": "\n".join(trailing_lines),
        "inline": True,
    })

    # 3) Positionen Overview — all positions up to Discord 1024-char limit
    if positions_summary:
        pos_lines = []
        for p in positions_summary:
            sym = p.get("symbol", "?")
            pnl = p.get("pnl_pct", 0.0)
            amt = p.get("amount_usd", 0.0)
            emoji = _pnl_emoji(pnl)
            trailing = p.get("trailing_status", "")
            line = f"{emoji} **{sym}** {pnl:+.1f}% (${amt:,.0f})"
            if trailing:
                line += f" — {trailing}"
            pos_lines.append(line)
        # fix/embeds-no-hidden-data (2026-08-12): frueher wurde hier bei
        # 1020 Zeichen abgeschnitten und der Rest zu "_+N weitere..._"
        # zusammengefasst — bei 58 Positionen blieb rund die Haelfte
        # unsichtbar. pack_lines_into_fields legt stattdessen so viele
        # Folgefelder an, wie die Zeilen brauchen.
        fields.extend(pack_lines_into_fields(
            "\U0001f4bc Positionen", pos_lines, inline=False))

    embed = {
        "title":       f"🛡️ Risk Worker — {regime}" + (f" ({closed} geschlossen)" if closed > 0 else ""),
        "description": desc,
        "color":       color,
        "fields":      fields,
        "footer":      {"text": "eToro RoBoCop · Risk Worker · alle 5min"},
        "timestamp":   _ts(),
    }
    ok = _post_embed(embed, DISCORD_MAIN_CHANNEL, dry_run)
    if ok:
        insert_system_log("INFO", "discord_embeds", f"P7 Risk Worker gepostet regime={regime} closed={closed}")
    return ok


def post_discovery_embed(
    candidates: list,
    scanned: int = 0,
    stored: int = 0,
    unverified: int = 0,
    elapsed_s: float = 0.0,
    dry_run: bool = False,
    evicted: int = 0,
    slots_used: int = 0,
) -> bool:
    """Discovery-Ranking — Top-Kandidaten mit vollem Kontext → #etoro-trading.

    `candidates` — sortierte Liste von Dicts (Score absteigend):
        {symbol, score, conviction, rsi, macd_hist, bb_pct, price,
         signal_types, instrument_id?}
    Pro Kandidat: Ranking, Score, Asset-Klasse, Preis, RSI, Trend,
    Begründung (Signal-Typen) und Portfolio-Fit.
    """
    if not candidates:
        logger.debug("[discord_embeds] Discovery: keine Kandidaten — kein Embed")
        return True

    top = candidates
    fields = []
    for i, c in enumerate(top):
        symbol  = c.get("symbol", "?")
        ref     = str(c.get("instrument_id") or symbol)
        row     = _lookup_instrument(ref)
        display = _format_instrument_display(ref, row)
        asset   = ((row or {}).get("asset_class") or "").capitalize() or "—"

        score = c.get("score", 0)
        conv  = c.get("conviction", "?")
        rsi   = c.get("rsi")
        price = c.get("price")

        rsi_str   = f"{rsi:.1f} {_rsi_emoji(rsi)}" if rsi is not None else "N/A"
        price_str = f"${price:,.2f}" if price else "N/A"
        types     = c.get("signal_types") or []
        reason    = ", ".join(types) if isinstance(types, (list, tuple)) else str(types)

        value = (
            f"📊 Score: **{score:.0f}** ({conv}) · 🏷️ {asset}\n"
            f"💵 Preis: {price_str} · RSI: {rsi_str}\n"
            f"📈 Trend: {_trend_str(c.get('macd_hist'), c.get('bb_pct'))}\n"
            f"📋 Begründung: {reason[:120] or '—'}\n"
            f"💼 Portfolio-Fit: {_portfolio_fit(symbol)}"
        )
        rank = _RANK_EMOJI[i] if i < len(_RANK_EMOJI) else f"#{i + 1}"
        fields.append({"name": f"{rank} {display}", "value": value, "inline": False})

    desc_parts = []
    if scanned:
        desc_parts.append(f"Gescannt: **{scanned}** Symbole")
    desc_parts.append(f"Kandidaten: **{len(candidates)}**")
    if stored:
        desc_parts.append(f"Gespeichert: **{stored}** Signale")
    if unverified:
        desc_parts.append(f"⚠️ Unverifiziert: **{unverified}**")
    if elapsed_s:
        desc_parts.append(f"Dauer: {elapsed_s:.0f}s")

    # feat/rotation-embed (2026-08-28): Die Raeumung veralteter Plaetze lief
    # bisher unsichtbar — der Embed meldete nur Funde. Ohne diese Zeile laesst
    # sich nicht beurteilen, ob die Rotation ueberhaupt arbeitet (sie tat es
    # monatelang nicht, siehe fix/rotation-usefulness).
    if evicted or slots_used:
        _rot = []
        if evicted:
            _rot.append(f"**{evicted}** Plätze geräumt")
        if slots_used:
            _rot.append(f"**{slots_used}** belegt")
        fields.append({
            "name": "🔄 Rotation",
            "value": " · ".join(_rot),
            "inline": False,
        })

    embed = {
        "title":       f"🔍 Discovery — Top {len(top)} Kandidaten",
        "description": " · ".join(desc_parts),
        "color":       COLOR_BLUE,
        "fields":      fields,
        "footer":      {"text": "eToro RoBoCop · Discovery · alle 2h"},
        "timestamp":   _ts(),
    }
    ok = _post_embed(embed, DISCORD_MAIN_CHANNEL, dry_run)
    if ok:
        insert_system_log("INFO", "discord_embeds",
                          f"P16 Discovery gepostet candidates={len(candidates)} stored={stored}")
    return ok



# ═══════════════════════════════════════════════════════════════════════════════
# P17 — SIGNAL WORKER (Trade-Approval Summary)
# ═══════════════════════════════════════════════════════════════════════════════

def post_signal_worker_embed(
    approved_trades: list,
    regime: str,
    risk_scalar: float,
    evaluated_count: int,
    equity: float,
    cash: float,
    total_exposure: float,
    position_count: int,
    signal_report: list | None = None,
    dry_run: bool = False,
) -> bool:
    """Signal Worker Summary -> #etoro-trades. Wird IMMER gepostet.

    approved_trades: dicts mit symbol, amount_usd, signal_type, conviction,
                     score, signal_price, sizing_trace
    signal_report:   dicts mit symbol, signal_type, conviction, score,
                     direction ("BUY"/"SELL"), outcome (Klartext)

    Bis 2026-08-29 wurde nur bei mindestens einem genehmigten Trade gepostet;
    sonst gab es gedrosselte Alert-Embeds ("All markets closed", "All signals
    blocked") oder gar nichts. Damit war die haeufigste Frage nicht zu
    beantworten: WELCHE Signale hat der Lauf gesehen und was ist daraus
    geworden? Jetzt steht das in jedem Post — Kaufsignale gruen,
    Verkaufssignale rot.
    """
    n = len(approved_trades)
    report = list(signal_report or [])
    buys = [r for r in report if r.get("direction") != "SELL"]
    sells = [r for r in report if r.get("direction") == "SELL"]

    fields = []

    # ── Genehmigte Trades zuerst, mit voller Sizing-Herleitung ───────────────
    for t in approved_trades:
        sig   = t.get("signal_type") or "?"
        conv  = t.get("conviction") or "?"
        amt   = float(t.get("amount_usd") or 0)
        score = float(t.get("score") or 0)
        sp    = t.get("signal_price")
        price_str = f" @ ${sp:.4f}" if sp else ""
        # fix/embeds-no-hidden-data (2026-08-12): frueher [:2]. Die
        # Kombo-Zusammensetzung ist die aussagekraeftigste Groesse ueberhaupt
        # — "RSI_EXTREME_OVERSOLD,MACD_TURN_BELOW_SMA20" war profitabel,
        # dieselben Regeln OHNE MACD-Bestaetigung verloren 36 von 37 Trades.
        sig_parts = [p.strip().replace("_", " ").title() for p in sig.split(",")]
        sig_short = " + ".join(sig_parts)
        _value = f"`BUY ${amt:,.0f}`{price_str} | {sig_short} | {conv} | Score `{score:.2f}`"
        # feat/sizing-trace (2026-08-28): Herleitung der Groesse mit anzeigen.
        _trace = t.get("sizing_trace") or []
        if _trace:
            _chain = " \u2192 ".join(str(s) for s in _trace)
            if len(_chain) > 860:
                _chain = _chain[:857] + "..."
            _value += f"\n\u2514 {_chain}"
        fields.append({
            "name":   f"\u2705 {t['symbol']} \u2014 genehmigt",
            "value":  _value,
            "inline": False,
        })

    # ── Signalliste, farbig ──────────────────────────────────────────────────
    # Discord faerbt nur den Embed-Rand, nicht einzelne Felder. Fuer "auf einen
    # Blick" braucht es daher ANSI in einem Codeblock (Desktop faerbt, Mobil
    # zeigt denselben Text ungefaerbt — degradiert also sauber) PLUS ein Emoji
    # in der Feldueberschrift, das ueberall traegt.
    # Discord bricht im Codeblock bei rund 50 Zeichen um — jede Spalte
    # kostet also unmittelbar Lesbarkeit. Darum: Typ auf den fuehrenden
    # Bestandteil gekuerzt (+n fuer weitere Komponenten), Conviction auf drei
    # Buchstaben, Ergebnis auf ein Stichwort. Bei SELL entfaellt die
    # Ergebnisspalte ganz — die rote Sektion sagt bereits alles.
    _OUTCOME_KURZ = {
        "genehmigt":                    "OK",
        "markt_geschlossen":            "Markt zu",
        "diversity_kappe":              "Diversity",
        "news_avoid":                   "News",
        "slippage_blacklist":           "Slippage",
        "llm_exchange_blacklist":       "Exchange",
        "nicht bewertet (Slots belegt)": "kein Slot",
        "bewertet, kein Trade":         "kein Trade",
    }

    def _kurz_typ(st: str) -> str:
        parts = [p.strip() for p in str(st or "?").split(",") if p.strip()]
        if not parts:
            return "?"
        toks = parts[0].split("_")
        lead = "_".join(toks[:2]) if len(toks) > 1 else parts[0]
        lead = lead[:18]
        return lead + (f"+{len(parts) - 1}" if len(parts) > 1 else "")

    def _kurz_erg(o: str) -> str:
        o = str(o or "")
        if o in _OUTCOME_KURZ:
            return _OUTCOME_KURZ[o]
        if "FLOOR" in o.upper():
            return "< Floor"
        if "Min-Buy" in o or "DUST" in o.upper():
            return "< Dust"
        if "Korrelation" in o:
            return "Korrel."
        if "Region" in o:
            return "Region"
        if "Cooldown" in o:
            return "Cooldown"
        return o[:11]

    def _lines(rows, ansi_code=None, mit_ergebnis=True, limit=10):
        """Eine Zeile je Signal in normaler Embed-Schrift.

        Kein Codeblock: der rendert in grosser Monospace und war der Grund,
        warum drei Signale einen halben Bildschirm belegten. Ausrichtung per
        Trennzeichen statt Padding, weil Proportionalschrift nicht spaltet.
        """
        out = []
        for r in rows[:limit]:
            sym = str(r.get("symbol") or "?")
            typ = _kurz_typ(r.get("signal_type"))
            conv = str(r.get("conviction") or "?")[:3]
            try:
                score = f"{float(r.get('score') or 0):.0f}"
            except (TypeError, ValueError):
                score = "?"
            zeile = f"`{sym}` {typ} \u00b7 {conv} {score}"
            if mit_ergebnis:
                erg = _kurz_erg(r.get("outcome"))
                if erg:
                    zeile += f" \u00b7 {erg}"
            out.append(zeile)
        if len(rows) > limit:
            out.append(f"_+{len(rows) - limit} weitere_")
        body = "\n".join(out)
        while len(body) > 1000 and out:
            out.pop()
            body = "\n".join(out) + "\n_... gekuerzt_"
        return body

    if buys:
        fields.append({
            "name":   f"\U0001f7e2 Kaufsignale ({len(buys)})",
            "value":  _lines(buys, "0;32"),
            "inline": False,
        })
    if sells:
        fields.append({
            "name":   f"\U0001f534 Verkaufssignale ({len(sells)})",
            "value":  _lines(sells, "0;31", mit_ergebnis=False),
            "inline": False,
        })
    if not report:
        fields.append({
            "name":   "\U0001f4ed Keine Signale",
            "value":  "Der Pool war leer \u2014 kein frisches Signal mit "
                      f"`{regime}`-Mindest-Conviction.",
            "inline": False,
        })

    cash_pct = (cash / equity * 100) if equity > 0 else 0.0
    exp_pct  = (total_exposure / equity * 100) if equity > 0 else 0.0
    fields.append({
        "name":   "\U0001f4bc Portfolio",
        "value":  (
            f"{position_count} Positionen | "
            f"Exposure `${total_exposure:,.0f}` ({exp_pct:.1f}%) | "
            f"Cash `${cash:,.0f}` ({cash_pct:.1f}%)"
        ),
        "inline": False,
    })

    # Farbe des Rands: gruen bei Trades, gelb wenn Kaufsignale da waren aber
    # keines durchkam, sonst grau — der Rand allein sagt schon, ob es was zu
    # sehen gibt.
    if n > 0:
        color = COLOR_GREEN
    elif buys:
        color = globals().get("COLOR_YELLOW", 0xF1C40F)
    else:
        color = globals().get("COLOR_GREY", 0x95A5A6)

    embed = {
        "title": (
            f"\U0001f4c8 Signal Worker \u2014 {len(buys)} Kauf / {len(sells)} Verkauf"
            f" \u00b7 {n} genehmigt"
        ),
        "description": (
            f"Regime: **{regime}** \u00b7 Scalar: `{risk_scalar:.2f}` \u00b7 "
            f"{evaluated_count} evaluiert \u2192 **{n} approved**"
        ),
        "color":       color,
        "fields":      fields,
        "footer":      {"text": "eToro RoBoCop \u00b7 Signal Worker"},
        "timestamp":   _ts(),
    }
    # Zielkanal MAIN, nicht TRADES (VoLLi 2026-08-29): der Embed ist seit
    # feat/signal-report ein Lagebericht ueber ALLE gesehenen Signale, kein
    # Trade-Ereignis. In #trades stand er zwischen den Ausfuehrungsmeldungen
    # und verdraengte sie.
    ok = _post_embed(embed, DISCORD_MAIN_CHANNEL, dry_run)
    if ok:
        syms = ", ".join(t["symbol"] for t in approved_trades) or "-"
        insert_system_log(
            "INFO", "discord_embeds",
            f"P17 Signal Worker: {n} approved ({syms}), "
            f"{len(buys)} BUY / {len(sells)} SELL im Bericht",
        )
    return ok