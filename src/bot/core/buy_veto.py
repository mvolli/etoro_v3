"""Pre-Trade-Veto auf ALLEN Kaufwegen (VoLLi 2026-10-17).

Es gibt genau einen Ort, an dem gekauft wird: den execution_worker. Drei Pfade
legen Kauf-Trades an (Signale inkl. signal_trades, CORE_SWEEP, CORE_INDEX).
Bis heute pruefte der trade_veto_worker nur den ersten, und auch den nur, wenn
er vor dem Execution-Slot fertig war; sonst lief der Kauf ungeprueft. Lehre
NVDA (24.08.): ein AVOID muss in jedem Kaufpfad wirken.

Zwei Schichten:

1. **Regel-Sperren (hart, fail-closed, ohne LLM):** News-Flag AVOID,
   Earnings binnen 2 Tagen (Aktien), SEC-8-K der letzten 10 Tage mit Item
   1.03 (Insolvenz), 3.01 (Delisting-Warnung) oder 4.02 (Bilanzkorrektur,
   US-Aktien). Wirken auch, wenn das LLM ausfaellt.
2. **LLM-Urteil (trade_veto_worker):** APPROVE / REDUCE / VETO mit frischen
   Headlines, SEC-Meldungen und Truth-Social-Posts (trumpstruth.org-RSS).
   Index-Kern und Core-Sweep: nur VETO bei konkretem System-/Firmenereignis,
   kein REDUCE.

Veto-Pflicht im execution_worker (`execution_gate`): Ein Kauf laeuft nur mit
gueltigem Urteil (APPROVE/REDUCE, hoechstens VERDICT_TTL_MIN alt). Ohne Urteil
wartet er einen Zyklus; danach (LLM aus/zu langsam) greifen nur die Regeln und
der Kauf laeuft mit Urteil NO_LLM (VoLLi: „Regeln hart, LLM weich“).
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DATA = PROJECT_ROOT / "data"
FLAGS_PATH = DATA / "llm_news_flags.json"
CIK_MAP_PATH = DATA / "sec_company_tickers.json"
TRUTH_CACHE_PATH = DATA / "truth_social_cache.json"

CORE_TYPES = frozenset({"CORE_INDEX", "CORE_SWEEP"})
VERDICT_TTL_MIN = 60          # Urteil aelter -> neu pruefen
WAIT_MAX_MIN = 14             # ein Execution-Zyklus ohne Urteil, dann Regel-Fallback
PASS_DECISIONS = frozenset({"APPROVE", "REDUCE", "NO_LLM"})

SEC_UA = "VoLLi etoro-bot volli1979@gmail.com"   # VoLLi-Freigabe 17.10. (live)
SEC_LOOKBACK_DAYS = 10
SEC_BLOCK_ITEMS = {
    "1.03": "Insolvenz/Zwangsverwaltung (8-K 1.03)",
    "3.01": "Delisting-Warnung (8-K 3.01)",
    "4.02": "Bilanzkorrektur, Abschluesse nicht verlaesslich (8-K 4.02)",
}
CIK_MAP_MAX_AGE_DAYS = 7

TRUTH_FEED_URL = "https://www.trumpstruth.org/feed"
TRUTH_REFRESH_MIN = 10
TRUTH_MAX_AGE_H = 48
MACRO_KEYWORDS = (
    "tariff", "tariffs", "sanction", "sanctions", "china", "chinese", "federal reserve", "the fed",
    "interest rate", "interest rates", "stock market", "dow", "nasdaq", "s&p", "oil", "opec",
    "trade deal", "export", "exports", "import", "imports", "ban", "bans", "chip", "chips",
    "semiconductor", "semiconductors", "pharma", "drug prices", "european union", "europe", "japan",
    "canada", "mexico", "india", "crypto", "bitcoin", "war", "strike", "strikes", "shutdown",
    "default", "recession",
)
_NAME_STOP = {"inc", "corp", "corporation", "holdings", "holding", "group", "the", "plc",
              "company", "co", "ltd", "limited", "sa", "ag", "se", "nv", "class", "trust",
              "ishares", "etf", "ucits", "international", "global"}


# ── DB ───────────────────────────────────────────────────────────────────────

def ensure_columns(db) -> None:
    """Idempotente Migration: Urteils-Spalten an trades."""
    for col in ("veto_decision TEXT", "veto_reason TEXT", "veto_at TEXT", "veto_wait_since TEXT"):
        try:
            db.execute(f"ALTER TABLE trades ADD COLUMN {col}")
        except Exception:  # noqa: BLE001 — Spalte existiert schon
            pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse(ts) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def set_verdict(db, trade_id: int, decision: str, reason: str, now: datetime | None = None) -> bool:
    """Urteil race-safe schreiben (nur APPROVED ohne Order). Setzt die Warte-Marke zurueck."""
    cur = db.execute(
        "UPDATE trades SET veto_decision=?, veto_reason=?, veto_at=?, veto_wait_since=NULL "
        "WHERE id=? AND status='APPROVED' AND (order_id IS NULL OR order_id='')",
        (decision, str(reason or "")[:200], _iso(now or _now()), trade_id),
    )
    return bool(getattr(cur, "rowcount", 0))


def verdict_valid(decision, veto_at, now: datetime | None = None) -> bool:
    if str(decision or "") not in PASS_DECISIONS:
        return False
    at = _parse(veto_at)
    return at is not None and (now or _now()) - at <= timedelta(minutes=VERDICT_TTL_MIN)


def needs_verdict(row: dict, now: datetime | None = None) -> bool:
    return not verdict_valid(row.get("veto_decision"), row.get("veto_at"), now)


# ── Quellen ──────────────────────────────────────────────────────────────────

def load_news_flags(path: Path = FLAGS_PATH) -> dict:
    try:
        data = json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return {}
    exp = _parse(data.get("auto_expires_at"))
    if exp is not None and exp < _now():
        return {}
    return data.get("flags", {}) or {}


def _http_get(url: str, ua: str, timeout: float = 15.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def is_us_stock(symbol: str, asset_class: str | None) -> bool:
    return str(asset_class or "") == "stock" and "." not in str(symbol or "")


def yahoo_symbol(trade: dict) -> str:
    """Yahoo-Symbol fuer Headlines/Earnings.

    US-Aktien: der eToro-Ticker selbst. `instruments.yfinance_symbol` ist dort
    systematisch falsch befuellt (gemessen 17.10.: 5 704 von 6 266, z. B.
    DPZ -> 'DPI', FAST -> 'FC_4212', MRK -> 'M&C'). Sonst yfinance_symbol.
    """
    sym = str(trade.get("symbol") or "")
    if is_us_stock(sym, trade.get("asset_class")):
        return sym.replace(".", "-")
    return str(trade.get("yfinance_symbol") or sym)


def sec_cik(symbol: str) -> int | None:
    """Ticker -> CIK aus SEC company_tickers.json (woechentlich aufgefrischt)."""
    try:
        fresh = (CIK_MAP_PATH.exists()
                 and time.time() - CIK_MAP_PATH.stat().st_mtime < CIK_MAP_MAX_AGE_DAYS * 86400)
        if not fresh:
            CIK_MAP_PATH.write_bytes(_http_get("https://www.sec.gov/files/company_tickers.json", SEC_UA))
        data = json.loads(CIK_MAP_PATH.read_text())
    except Exception as exc:  # noqa: BLE001
        logger.warning("SEC-CIK-Map nicht verfuegbar: %s", exc)
        return None
    t = str(symbol).upper().replace(".", "-")
    for v in data.values():
        if str(v.get("ticker", "")).upper() == t:
            return int(v["cik_str"])
    return None


_SUB_CACHE: dict[int, tuple[float, list[dict]]] = {}


def sec_recent_filings(cik: int, days: int = SEC_LOOKBACK_DAYS) -> list[dict] | None:
    """Meldungen der letzten `days` Tage: [{form, date, items}] — None bei Fehler."""
    hit = _SUB_CACHE.get(cik)
    if hit and time.time() - hit[0] < 600:
        rows = hit[1]
    else:
        try:
            d = json.loads(_http_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", SEC_UA))
            r = d["filings"]["recent"]
            rows = [{"form": r["form"][i], "date": r["filingDate"][i], "items": r.get("items", [""] * 999)[i] or ""}
                    for i in range(min(len(r["form"]), 60))]
        except Exception as exc:  # noqa: BLE001
            logger.warning("SEC-Submissions CIK %s nicht verfuegbar: %s", cik, exc)
            return None
        _SUB_CACHE[cik] = (time.time(), rows)
    lo = (_now() - timedelta(days=days)).date().isoformat()
    return [x for x in rows if x["date"] >= lo]


def sec_block_reason(filings: list[dict] | None) -> str | None:
    for f in filings or []:
        if not str(f.get("form", "")).startswith("8-K"):
            continue
        items = {i.strip() for i in str(f.get("items", "")).split(",")}
        for code, label in SEC_BLOCK_ITEMS.items():
            if code in items:
                return f"{label} vom {f['date']}"
    return None


YAHOO_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US"
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={q}+when:2d&hl=en-US&gl=US&ceid=US:en"


def parse_news_rss(xml: str, max_age_h: int = 48, now: datetime | None = None) -> list[str]:
    """RSS (Yahoo/Google News) -> Headlines juenger als max_age_h, neueste zuerst."""
    now = now or _now()
    out = []
    for item in re.findall(r"<item>(.*?)</item>", xml, re.S):
        t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item, re.S)
        pub = re.search(r"<pubDate>(.*?)</pubDate>", item)
        if not t:
            continue
        try:
            ts = parsedate_to_datetime(pub.group(1).strip()) if pub else None
        except Exception:  # noqa: BLE001
            ts = None
        if ts is not None and now - ts > timedelta(hours=max_age_h):
            continue
        out.append((ts or now, unescape(t.group(1)).strip()[:160]))
    out.sort(key=lambda x: x[0], reverse=True)
    return [h for _, h in out if h]


def fresh_headlines(yf_symbol: str, name: str | None = None, max_age_h: int = 48, n: int = 6) -> list[str]:
    """Frische Headlines je Titel.

    yfinance `Ticker.news` liefert seit Oktober 2026 leere Listen (gemessen
    17.10.: NVDA 0 Items). Quelle daher der Yahoo-Finance-RSS-Feed je Symbol,
    Rueckfall Google-News-RSS mit dem Firmennamen. Beide kostenlos, ohne Konto.
    """
    from urllib.parse import quote_plus
    keys = trade_keywords({"symbol": yf_symbol, "name": name})
    pats = [re.compile(r"\b" + re.escape(k) + r"\b", re.I) for k in keys]
    heads: list[str] = []
    try:
        # Der Yahoo-Feed mischt allgemeine Marktmeldungen bei -> nur Titel mit
        # Ticker oder Firmenname.
        heads = [h for h in parse_news_rss(
            _http_get(YAHOO_RSS.format(sym=quote_plus(yf_symbol)), "Mozilla/5.0 etoro-bot")
            .decode("utf-8", "ignore"), max_age_h) if any(rx.search(h) for rx in pats)]
    except Exception as exc:  # noqa: BLE001
        logger.debug("Yahoo-RSS %s nicht verfuegbar: %s", yf_symbol, exc)
    if len(heads) < n and name:
        try:
            short = " ".join(t for t in re.split(r"\s+", name) if t.lower().strip(".,") not in _NAME_STOP)
            q = quote_plus(f'"{short or name}"')
            for h in parse_news_rss(_http_get(GOOGLE_NEWS_RSS.format(q=q), "Mozilla/5.0 etoro-bot")
                                    .decode("utf-8", "ignore"), max_age_h):
                if h not in heads:
                    heads.append(h)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Google-News-RSS %s nicht verfuegbar: %s", name, exc)
    return heads[:n]


def earnings_block_reason(yf_symbol: str) -> str | None:
    try:
        from bot.workers.news_flags_worker import _earnings_flag_for
        f = _earnings_flag_for(yf_symbol)
        return f"Earnings binnen 2 Tagen ({f['reason']})" if f else None
    except Exception:  # noqa: BLE001
        return None


def _strip_html(s: str) -> str:
    s = re.sub(r"<a [^>]*>View image</a>|<a [^>]*>View video</a>", " ", s or "")
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def parse_truth_rss(xml: str) -> list[dict]:
    out = []
    for item in re.findall(r"<item>(.*?)</item>", xml, re.S):
        desc = re.search(r"<description><!\[CDATA\[(.*?)\]\]></description>", item, re.S)
        pub = re.search(r"<pubDate>(.*?)</pubDate>", item)
        link = re.search(r"<link>(.*?)</link>", item)
        text = _strip_html(desc.group(1) if desc else "")
        if not text or not pub:
            continue
        try:
            ts = parsedate_to_datetime(pub.group(1).strip()).astimezone(timezone.utc)
        except Exception:  # noqa: BLE001
            continue
        out.append({"ts": _iso(ts), "text": text[:600], "url": link.group(1) if link else ""})
    return out


def truth_posts(now: datetime | None = None) -> list[dict]:
    """Truth-Social-Posts der letzten 48 h aus dem RSS-Cache (Refresh alle 10 min)."""
    now = now or _now()
    cache = {}
    try:
        cache = json.loads(TRUTH_CACHE_PATH.read_text())
    except Exception:  # noqa: BLE001
        pass
    fetched = _parse(cache.get("fetched_at"))
    if fetched is None or now - fetched > timedelta(minutes=TRUTH_REFRESH_MIN):
        try:
            posts = parse_truth_rss(_http_get(TRUTH_FEED_URL, "Mozilla/5.0 etoro-bot").decode("utf-8", "ignore"))
            known = {p["url"] or p["ts"] + p["text"][:40]: p for p in cache.get("posts", [])}
            for p in posts:
                known[p["url"] or p["ts"] + p["text"][:40]] = p
            cache = {"fetched_at": _iso(now), "posts": sorted(known.values(), key=lambda p: p["ts"])}
            TRUTH_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Truth-Social-Feed nicht verfuegbar: %s", exc)
    lo = _iso(now - timedelta(hours=TRUTH_MAX_AGE_H))
    posts = [p for p in cache.get("posts", []) if p.get("ts", "") >= lo]
    if len(posts) != len(cache.get("posts", [])):
        try:
            cache["posts"] = posts
            TRUTH_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass
    return posts


def trade_keywords(trade: dict) -> list[str]:
    """Begriffe, unter denen ein Post diesen Trade betrifft."""
    kw = []
    sym = str(trade.get("symbol") or "").split(".")[0]
    if len(sym) >= 2:
        kw.append(sym)
    for tok in re.split(r"[\s,.\-&]+", str(trade.get("name") or "")):
        if len(tok) >= 4 and tok.lower() not in _NAME_STOP:
            kw.append(tok)
            break
    for f in ("sector", "industry"):
        v = str(trade.get(f) or "").split(" - ")[0].strip()
        if len(v) >= 4:
            kw.append(v)
    return kw


def relevant_posts(posts: list[dict], keywords: list[str], limit: int = 8) -> list[dict]:
    pats = [re.compile(r"\b" + re.escape(k) + r"\b", re.I) for k in keywords if k]
    pats += [re.compile(r"(?<![\w&])" + re.escape(k) + r"(?![\w&])", re.I) for k in MACRO_KEYWORDS]
    hits = [p for p in posts if any(rx.search(p["text"]) for rx in pats)]
    return hits[-limit:]


# ── Regeln ───────────────────────────────────────────────────────────────────

def rule_block_reason(trade: dict, flags: dict | None = None) -> str | None:
    """Harte Regel-Sperre fuer einen Kauf, sonst None. Netzwerkfehler -> keine Sperre."""
    sym = str(trade.get("symbol") or "")
    flags = load_news_flags() if flags is None else flags
    f = flags.get(sym)
    if f and str(f.get("flag", "")).upper() == "AVOID":
        return f"News-Flag AVOID: {str(f.get('reason', ''))[:120]}"
    yf_sym = yahoo_symbol(trade)
    if str(trade.get("asset_class") or "") == "stock":
        r = earnings_block_reason(yf_sym)
        if r:
            return r
    if is_us_stock(sym, trade.get("asset_class")):
        cik = sec_cik(sym)
        if cik:
            r = sec_block_reason(sec_recent_filings(cik))
            if r:
                return r
    return None


# ── Veto-Pflicht im execution_worker ─────────────────────────────────────────

TRADE_CTX_SQL = """
    SELECT t.id, t.symbol, t.instrument_id, t.amount_usd, t.signal_price, t.signal_id,
           t.status, t.order_id, t.veto_decision, t.veto_reason, t.veto_at, t.veto_wait_since,
           s.signal_type, s.conviction, s.score, s.rsi, s.bb_pct,
           i.yfinance_symbol, i.asset_class, i.name, i.sector, i.industry, i.market_region
    FROM trades t
    LEFT JOIN signals s ON s.id = t.signal_id
    LEFT JOIN instruments i ON i.instrument_id = t.instrument_id
"""


def execution_gate(db, trade_id: int, now: datetime | None = None,
                   rule_check=rule_block_reason) -> tuple[str, dict]:
    """Entscheidung fuer einen Kauf im execution_worker (Trade steht auf SUBMITTING).

    Rueckgabe (action, info):
      ("PASS",  {"amount_usd", "decision"})   — Urteil gueltig oder Fallback ohne Sperre
      ("WAIT",  {"waited_min"})               — noch kein Urteil, einen Zyklus warten
      ("BLOCK", {"reason"})                    — Regel-Sperre im Fallback
    Liest das Urteil frisch aus der DB (der Veto-Worker kann es eben geschrieben haben).
    """
    now = now or _now()
    row = db.fetchone(TRADE_CTX_SQL + " WHERE t.id = ?", (trade_id,))
    row = dict(row) if row else {}
    if verdict_valid(row.get("veto_decision"), row.get("veto_at"), now):
        return "PASS", {"amount_usd": float(row.get("amount_usd") or 0.0),
                        "decision": row.get("veto_decision")}
    since = _parse(row.get("veto_wait_since"))
    if since is None:
        db.execute("UPDATE trades SET veto_wait_since=? WHERE id=?", (_iso(now), trade_id))
        return "WAIT", {"waited_min": 0.0}
    waited = (now - since).total_seconds() / 60.0
    if waited < WAIT_MAX_MIN:
        return "WAIT", {"waited_min": waited}
    reason = rule_check(row)
    if reason:
        db.execute("UPDATE trades SET veto_decision='RULE_VETO', veto_reason=?, veto_at=? WHERE id=?",
                   (reason[:200], _iso(now), trade_id))
        return "BLOCK", {"reason": reason}
    db.execute("UPDATE trades SET veto_decision='NO_LLM', veto_reason=?, veto_at=?, veto_wait_since=NULL "
               "WHERE id=?", (f"kein LLM-Urteil nach {waited:.0f} min — nur Regeln geprueft",
                              _iso(now), trade_id))
    return "PASS", {"amount_usd": float(row.get("amount_usd") or 0.0), "decision": "NO_LLM",
                    "waited_min": waited}
