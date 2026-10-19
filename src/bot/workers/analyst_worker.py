#!/usr/bin/env python3
"""eToro Trading Bot V3 — Analysten-Strategie (Q2-Screen + Earnings-Katalysator S1).

VoLLi 2026-10-17. Belege: docs/e8-, e6-, e9-ergebnis-2026-10-17.md. Modul-Regeln:
bot/core/analyst_strategy.py, quality_screen.py, earnings_catalyst.py.

Laeuft einmal je US-Handelstag kurz nach der Eroeffnung (Hermes-Cron
v3_analyst_worker.sh). Reihenfolge:
  1. Ausstiege der eigenen Positionen: 126 Handelstage erreicht, News-Flag AVOID
     (Headlines) oder 8-K 1.03/3.01/4.02 seit Einstieg -> Vollverkauf.
     Der Stop -15 % liegt als Broker-SL auf der Position.
  2. Q2: am ersten Lauf nach dem Monatsstichtag M den Screen rechnen
     (SEC companyfacts + 12-1-Momentum), Picks in data/analyst_q2_picks.json.
  3. S1: Berichte mit day0 = T-2 pruefen (Nasdaq-Kalender + yfinance).
  4. Kaeufe in freie Plaetze (max_open), Tranche ~6,5 % Equity, Cash-Reserve
     18 % + Index-Kern-Luecke. Trade APPROVED -> Pre-Trade-Veto -> execution_worker.

Nur bei offener US-Boerse (sonst veraltete APPROVED-Trades, vgl. Core-Index #2932).
mode shadow = nur loggen.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("analyst_worker")

WORKER_NAME = "analyst_worker"
PICKS_PATH = PROJECT_ROOT / "data" / "analyst_q2_picks.json"
EVENT_PICKS_PATH = PROJECT_ROOT / "data" / "analyst_a_picks.json"   # Variante A (E11)


def _ny_today() -> date:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York")).date()


def _download_closes(symbols: list[str], start: str, end: str | None = None) -> dict:
    """{yf_symbol: (dates, closes)} — Tagesschluss, auto_adjust wie im Backtest."""
    import yfinance as yf
    out: dict = {}
    for k in range(0, len(symbols), 100):
        chunk = symbols[k:k + 100]
        try:
            df = yf.download(chunk, start=start, end=end, auto_adjust=True, progress=False,
                             group_by="ticker", threads=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Kursdownload fehlgeschlagen (%d Titel): %s", len(chunk), exc)
            continue
        for s in chunk:
            try:
                cols = df.columns
                multi = hasattr(cols, "levels") and s in cols.get_level_values(0)
                sub = (df[s] if multi else df)["Close"]
                if hasattr(sub, "columns"):              # yfinance: (Feld, Ticker)-Spalten
                    sub = sub.iloc[:, 0]
                sub = sub.dropna()
                sub = sub[sub > 0]
                if len(sub):
                    out[s] = ([d.strftime("%Y-%m-%d") for d in sub.index], [float(x) for x in sub])
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.5)
    return out


def _last_month_end(today: date) -> str | None:
    """Letzter Handelstag des Vormonats (aus SPY-Kursen)."""
    spy = _download_closes(["SPY"], (today - timedelta(days=70)).isoformat()).get("SPY")
    if not spy:
        return None
    first = today.replace(day=1).isoformat()
    prev = [d for d in spy[0] if d < first]
    return prev[-1] if prev else None


def _load_picks() -> dict:
    try:
        return json.loads(PICKS_PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}


def compute_q2(M: str, universe: dict) -> dict:
    """Screen zum Stichtag M rechnen (E8-Regeln)."""
    from bot.core import quality_screen as qs
    nonfin = {t: v for t, v in universe.items() if v.get("cik") and not qs.is_fin(v.get("sic"))}
    start = (date.fromisoformat(M) - timedelta(days=400)).isoformat()
    end = (date.fromisoformat(M) + timedelta(days=1)).isoformat()
    closes = _download_closes(sorted({v.get("yf") or t for t, v in nonfin.items()}), start, end)
    filings, mom = {}, {}
    fresh_after = datetime.fromisoformat(M).replace(tzinfo=timezone.utc).timestamp() + 86400
    for t, v in nonfin.items():
        c = closes.get(v.get("yf") or t)
        if not c or c[0][-1] != M:
            continue                                # E8: Titel muss am Stichtag handeln
        fl = qs.company_filings(int(v["cik"]), fresh_after=fresh_after)
        if fl:
            filings[t] = fl
        m = qs.momentum_12_1(c[1])
        if m is not None:
            mom[t] = m
    snap = qs.snapshot(filings, M)
    buyable = {t for t, v in universe.items() if v.get("seg") == "large_mid"}
    picks = qs.rank_picks(snap, mom, buyable)
    res = {"M": M, "generated_at": datetime.now(timezone.utc).isoformat()[:19],
           "n_rank": len(snap), "n_universe": len(nonfin),
           "picks": [[round(s, 4), t] for s, t in picks], "bought": []}
    PICKS_PATH.write_text(json.dumps(res, indent=1))
    logger.info("Q2 %s: Rangmenge %d von %d, %d Picks", M, len(snap), len(nonfin), len(picks))
    return res


def _load_event_picks() -> dict:
    try:
        return json.loads(EVENT_PICKS_PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}


def compute_event(today: date, universe: dict) -> dict:
    """Variante A (E11): Einreichungen, die an d1 (letzter Handelstag vor heute) den
    Eintritt ins Top-Dezil ausloesen. Nur die Einreicher werden bei der SEC neu
    geladen (EDGAR-Tagesindex), alle anderen kommen aus dem companyfacts-Cache.
    """
    from bot.core import quality_screen as qs
    nonfin = {t: v for t, v in universe.items() if v.get("cik") and not qs.is_fin(v.get("sic"))}
    buyable = {t for t, v in universe.items() if v.get("seg") == "large_mid"}
    start = (today - timedelta(days=560)).isoformat()
    spy = _download_closes(["SPY"], start).get("SPY")
    cal = [d for d in (spy[0] if spy else []) if d < today.isoformat()]
    if len(cal) < 2:
        raise RuntimeError("SPY-Kalender fehlt")
    d1, prev = cal[-1], cal[-2]
    filers: set[int] = set()
    incomplete = []
    day = date.fromisoformat(prev)
    while day < date.fromisoformat(d1):                     # Einreichungen mit d1 als erstem Handelstag danach
        got = qs.daily_filers(day)
        if got is None:
            incomplete.append(day.isoformat())
        else:
            filers |= got
        day += timedelta(days=1)
    raw = _download_closes(sorted({v.get("yf") or t for t, v in nonfin.items()}), start)
    closes = {}
    for t, v in nonfin.items():
        c = raw.get(v.get("yf") or t)
        if c:
            n = sum(1 for d in c[0] if d < today.isoformat())
            closes[t] = (c[0][:n], c[1][:n])
    filings = {}
    for t, v in nonfin.items():
        cik = int(v["cik"])
        fl = qs.company_filings(cik, refresh=cik in filers, max_age_days=3650)
        if fl:
            filings[t] = fl
    ranker = qs.Ranker(filings, closes, [t for t in nonfin if t in closes])
    picks = qs.event_candidates(ranker, cal, d1, buyable)
    score, _, _, _ = ranker.at(d1)
    res = {"date": today.isoformat(), "d1": d1, "generated_at": datetime.now(timezone.utc).isoformat()[:19],
           "filers": len(filers), "n_rank": len(score), "index_missing": incomplete,
           "picks": [[round(s, 4), t] for s, t in picks], "bought": []}
    EVENT_PICKS_PATH.write_text(json.dumps(res, indent=1))
    logger.info("Q2-A %s (d1 %s): %d Einreicher, Rangmenge %d, %d Picks%s", today, d1, len(filers),
                len(score), len(picks), f", Index fehlt: {incomplete}" if incomplete else "")
    return res


def _instrument(db, ticker: str) -> dict | None:
    row = db.fetchone(
        "SELECT instrument_id, symbol, yfinance_symbol FROM instruments "
        "WHERE asset_class='stock' AND is_tradable=1 AND symbol NOT LIKE '%.%' "
        "AND UPPER(REPLACE(symbol, '.', '-')) = ? LIMIT 1", (ticker.upper(),))
    return dict(row) if row else None


def _interleave(a: list, b: list) -> list:
    out = []
    for i in range(max(len(a), len(b))):
        if i < len(a):
            out.append(a[i])
        if i < len(b):
            out.append(b[i])
    return out


def main() -> int:
    from bot.core.worker_lock import worker_lock
    screen_only = "--screen" in sys.argv

    with worker_lock(WORKER_NAME) as acquired:
        if not acquired:
            print(f"{WORKER_NAME}: SKIPPED (already running)")
            return 0
        t0 = time.monotonic()
        from bot.config import load_hermes_env
        load_hermes_env(logger)
        import os
        import yaml
        from bot.api.client import ClientConfig, EToroClient
        from bot.core import analyst_strategy as an
        from bot.core import buy_veto as bv
        from bot.core import core_index as ci
        from bot.core import earnings_catalyst as ec
        from bot.core import quality_screen as qs
        from bot.core import signal_trades as st
        from bot.core.heartbeat import record_heartbeat
        from bot.core.market_hours import is_market_open
        from bot.db.connection import DB
        from bot.db.repo import LogRepo, PortfolioRepo, SignalRepo, StateRepo, TradeRepo

        cfg = yaml.safe_load((PROJECT_ROOT / "config" / "config.yaml").read_text()) or {}
        acfg = an.load_config(cfg)
        db = DB(db_path=PROJECT_ROOT / cfg["db"]["path"],
                busy_timeout_ms=cfg["db"].get("busy_timeout_ms", 5000))
        state_repo, log_repo = StateRepo(db), LogRepo(db)
        trade_repo, signal_repo = TradeRepo(db), SignalRepo(db)
        try:
            record_heartbeat(state_repo, WORKER_NAME)
        except Exception:  # noqa: BLE001
            pass
        if acfg["mode"] == "off":
            print(f"{WORKER_NAME}: mode=off")
            return 0
        live = acfg["mode"] == "live"
        today = _ny_today()
        if screen_only and acfg.get("q2_mode") == "event":
            if acfg["q2"] and _load_event_picks().get("date") != today.isoformat():
                res = compute_event(today, qs.load_universe())
                print(f"{WORKER_NAME}: Q2-A {today} (d1 {res['d1']}): {res['filers']} Einreicher, "
                      f"{len(res['picks'])} Picks")
            else:
                print(f"{WORKER_NAME}: Q2-A {today} schon gerechnet")
            return 0
        if screen_only:
            M = _last_month_end(today)
            in_window = bool(M) and (date.fromisoformat(M) + timedelta(
                days=int(acfg["q2_buy_window_days"]))) >= today
            if acfg["q2"] and in_window and _load_picks().get("M") != M:
                res = compute_q2(M, qs.load_universe())
                print(f"{WORKER_NAME}: Q2-Screen {M}: {len(res['picks'])} Picks")
            else:
                print(f"{WORKER_NAME}: Q2-Screen {M} vorhanden oder Kauffenster vorbei")
            return 0
        from bot.core.market_hours import resolve_market_fields
        _row = db.fetchone("SELECT instrument_id FROM instruments WHERE symbol='AAPL' LIMIT 1")
        _mf = resolve_market_fields(db, _row["instrument_id"]) if _row else None
        if not is_market_open(*(_mf or ("AAPL", "AAPL", "")), fail_open=False):
            print(f"{WORKER_NAME}: US-Boerse geschlossen — nichts zu tun")
            return 0

        client = EToroClient(api_key=os.environ.get("ETORO_BOT_API_KEY", ""),
                             user_key=os.environ.get("ETORO_BOT_USER_KEY", ""),
                             config=ClientConfig.from_dict(cfg.get("api", {})))
        summary: list[str] = []

        # ── 1. Ausstiege ─────────────────────────────────────────────────────
        try:
            portfolio = client.get_portfolio()
            cp = portfolio.get("clientPortfolio", {})
            positions = {str(p.get("positionID") or p.get("positionId")): p
                         for p in (cp.get("positions") or cp.get("openPositions") or [])}
            flags = bv.load_news_flags()
            from bot.core.sell_exits import SellExitAction, execute_sell_exits
            actions = []
            for row in an.open_rows(db):
                pid = str(row.get("api_position_id") or "")
                p = positions.get(pid)
                if not p:
                    continue
                cik = bv.sec_cik(row["symbol"])
                fl = bv.sec_recent_filings(cik) if cik else None
                amount = float(p.get("amount") or row.get("amount_usd") or 0.0)
                pnl = float((p.get("unrealizedPnL") or {}).get("pnL") or 0.0)
                reason = an.exit_reason(row, acfg, today, flags, fl,
                                        pnl_pct=(pnl / amount * 100.0) if amount else None)
                if not reason:
                    continue
                actions.append(SellExitAction(
                    signal_id=0, symbol=row["symbol"], position_id=pid,
                    instrument_id=int(row["instrument_id"]), amount_usd=amount,
                    open_rate=float(p.get("openRate") or 0.0),
                    pnl_pct=(pnl / amount * 100.0) if amount else 0.0,
                    close_pct=100.0, reason=f"Analysten-Strategie: {reason}"))
                summary.append(f"🔚 {row['symbol']}: {reason}")
            if actions and live:
                res = execute_sell_exits(client, signal_repo, actions, db=db,
                                         allow_core=True, source="analyst_exit")
                log_repo.write("INFO", WORKER_NAME, f"Ausstiege: {len(actions)} Aktion(en), "
                               f"{res.get('closed', 0)} ausgefuehrt", {"errors": res.get("errors")})
            elif actions:
                log_repo.write("INFO", WORKER_NAME, f"SHADOW Ausstiege: {[a.symbol for a in actions]}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ausstiegs-Schritt fehlgeschlagen: %s", exc)
            log_repo.write("WARN", WORKER_NAME, f"Ausstiegs-Schritt fehlgeschlagen: {exc}")

        # ── 2./3. Kandidaten ────────────────────────────────────────────────
        universe = qs.load_universe()
        q2_list: list[tuple[str, str]] = []
        picks = _load_picks()
        picks_path = PICKS_PATH
        q2_why = None
        if acfg["q2"] and acfg.get("q2_mode") == "event":
            # Variante A (E11, VoLLi 19.10.: A + S1): Kaufliste vom Morgenlauf.
            try:
                picks_path = EVENT_PICKS_PATH
                picks = _load_event_picks()
                if picks.get("date") != today.isoformat():
                    picks = compute_event(today, universe)
                q2_why = f"Q2 bei Einreichung (Top-Dezil-Eintritt, d1 {picks.get('d1')})"
                if picks.get("picks"):
                    summary.append(f"📊 Q2-A: {len(picks['picks'])} Picks aus {picks.get('filers')} Einreichungen "
                                   f"(d1 {picks.get('d1')})")
                bought = set(picks.get("bought") or [])
                q2_list = [("Q2", t) for _, t in picks.get("picks", []) if t not in bought]
            except Exception as exc:  # noqa: BLE001
                logger.exception("Q2-A-Schritt fehlgeschlagen: %s", exc)
                log_repo.write("WARN", WORKER_NAME, f"Q2-A fehlgeschlagen: {exc}")
        elif acfg["q2"]:
            try:
                M = _last_month_end(today)
                if M and (date.fromisoformat(M) + timedelta(days=int(acfg["q2_buy_window_days"]))
                          >= today):
                    if picks.get("M") != M:
                        picks = compute_q2(M, universe)
                        summary.append(f"📊 Q2-Screen {M}: {len(picks['picks'])} Picks "
                                       f"(Rangmenge {picks['n_rank']})")
                    bought = set(picks.get("bought") or [])
                    q2_list = [("Q2", t) for _, t in picks.get("picks", []) if t not in bought]
            except Exception as exc:  # noqa: BLE001
                logger.exception("Q2-Schritt fehlgeschlagen: %s", exc)
                log_repo.write("WARN", WORKER_NAME, f"Q2-Screen fehlgeschlagen: {exc}")
        s1_list: list[tuple[str, str]] = []
        s1_info: dict = {}
        if acfg["s1"]:
            try:
                start = (today - timedelta(days=420)).isoformat()
                res = ec.scan(universe, today, lambda syms: _download_closes(syms, start))
                s1_list = [("S1", r["ticker"]) for r in res]
                s1_info = {r["ticker"]: r for r in res}
                if res:
                    summary.append("📣 S1: " + ", ".join(
                        f"{r['ticker']} (+{r['surprise']:.0f} % Überraschung, +{r['ear']:.1f} % Reaktion)"
                        for r in res[:8]))
            except Exception as exc:  # noqa: BLE001
                logger.exception("S1-Schritt fehlgeschlagen: %s", exc)
                log_repo.write("WARN", WORKER_NAME, f"S1-Scan fehlgeschlagen: {exc}")

        # ── 4. Kaeufe ───────────────────────────────────────────────────────
        rows = an.open_rows(db)
        held = {str(r["symbol"]).upper().replace(".", "-") for r in rows}
        slots = int(acfg["max_open"]) - len(rows)
        equity = state_repo.get_equity()
        cash = float(state_repo.get_float("AVAILABLE_CASH", 0.0))
        from bot.core.regime import get_regime_params
        regime = state_repo.get_regime()
        rp = get_regime_params(regime) or {}
        amt = an.tranche_usd(acfg, equity, float(rp.get("max_trade_pct", 0.0)))
        try:
            core_ids = ci.core_position_ids(db)
            gap = st.core_gap_usd(cfg, equity, PortfolioRepo(db).get_all(), core_ids)
        except Exception:  # noqa: BLE001
            gap = 0.0
        reserve = st.reserve_usd(equity, gap, acfg)
        bought_now = []
        if regime == "CRITICAL":
            summary.append("⛔ Regime CRITICAL — keine Käufe")
        elif equity <= 0 or amt < float(acfg["min_usd"]):
            summary.append(f"⛔ Tranche ${amt:.0f} unter Minimum — keine Käufe")
        else:
            for kind, t in _interleave(q2_list, s1_list):
                if slots <= 0:
                    break
                if t in held:
                    continue
                if cash - amt < reserve:
                    summary.append(f"💰 Cash-Reserve erreicht (${cash:.0f} − ${amt:.0f} < ${reserve:.0f})")
                    break
                inst = _instrument(db, t)
                if not inst:
                    continue
                sig_type = an.SIGNAL_Q2 if kind == "Q2" else an.SIGNAL_S1
                why = ((q2_why or f"Q2-Screen {picks.get('M')}") if kind == "Q2" else
                       f"S1 Earnings {s1_info.get(t, {}).get('ed')}: +{s1_info.get(t, {}).get('surprise', 0):.0f} % "
                       f"Überraschung, +{s1_info.get(t, {}).get('ear', 0):.1f} % Reaktion")
                if not live:
                    log_repo.write("INFO", WORKER_NAME, f"SHADOW Kauf {t} ${amt:.2f} — {why}")
                    summary.append(f"👁 SHADOW {t} ${amt:.0f} — {why}")
                    slots -= 1; cash -= amt; held.add(t)
                    continue
                sig_id = signal_repo.create(instrument_id=int(inst["instrument_id"]), signal_type=sig_type,
                                            conviction="MEDIUM", score=0.0, rsi=None, ttl_minutes=5)
                signal_repo.update_signal_status(sig_id, "CONSUMED")
                tid = trade_repo.create(instrument_id=int(inst["instrument_id"]), symbol=inst["symbol"],
                                        direction="BUY", amount_usd=amt,
                                        stop_loss_pct=float(acfg["stop_pct"]),
                                        signal_id=sig_id, signal_price=None)
                trade_repo.update_status(tid, "APPROVED",
                                         approved_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
                log_repo.write("INFO", WORKER_NAME, f"APPROVED #{tid} {inst['symbol']} ${amt:.2f} "
                               f"({sig_type}, SL {acfg['stop_pct']}%) — {why}")
                summary.append(f"🟢 {inst['symbol']} ${amt:.0f} — {why}")
                bought_now.append(t)
                slots -= 1; cash -= amt; held.add(t)
                if kind == "Q2":
                    picks.setdefault("bought", []).append(t)
            if bought_now and (picks.get("M") or picks.get("date")):
                picks_path.write_text(json.dumps(picks, indent=1))

        elapsed = time.monotonic() - t0
        print(f"{WORKER_NAME}: {len(bought_now)} Kauf/Käufe, {elapsed:.0f}s")
        if summary:
            try:
                sys.path.insert(0, str(SRC_DIR / "bot"))
                import discord_embeds as _DE
                _DE.post_alert_embed(
                    title=f"🧭 Analysten-Strategie [{acfg['mode']}] — {today:%d.%m.%Y}",
                    description=("\n".join(summary)
                                 + f"\nPlätze: {len(an.open_rows(db))}/{acfg['max_open']} · "
                                 f"Tranche ${amt:.0f} · Regime {regime}")[:1900],
                    severity="INFO", channel="trades")
            except Exception:  # noqa: BLE001
                pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
