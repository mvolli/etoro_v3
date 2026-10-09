"""feat/core-sweep (2026-07-22): liquides Cash-Deployment.

Reiner, testbarer Planer: entkoppelt das Deployment ueberschuessigen Cashs vom
illiquiden Small-Cap-Signalfluss. Wenn Cash ueber dem Reserve-Ziel liegt, wird
der Ueberschuss in einen kuratierten Korb hochliquider Large-Caps/ETFs
("Core") gesweept — grosse, diversifizierte, stop-losste Positionen in Titeln,
die grosse Groessen sicher aufnehmen.

Die Funktion plant nur (keine Seiteneffekte, keine Order): der signal_worker
setzt den Plan ueber dieselbe create->APPROVED->execution-Bahn wie normale
Signale um und erbt damit SL-Clamp, Market-Open-Guard und Ghost-Order-Pipeline.

fix/core-sweep-portfolio-gates (2026-08-12): Der Satz oben galt fuer den
EXECUTION-Pfad — nicht fuer die Risiko-Gates. Core-Sweep rief NIE
check_buy_gate und pruefte damit ausschliesslich Cash- und Einzeltitel-
Grenzen; keine einzige Pruefung betrachtete das Portfolio als Ganzes.
Belegt am 2026-08-12 05:19:29: derselbe signal_worker-Lauf blockte ROVI.MC
mit "Exposure-Gate: 79.5% > 75% Max" und eroeffnete zeitgleich 2883.HK
($345.90) via Core-Sweep. 41 der 57 offenen Trades stammten aus diesem Pfad.

Verschaerfend: amount_usd ist EINGESETZTES KAPITAL, nicht Marktwert —
Exposure-% steigt also, wenn die Equity FAELLT. Core-Sweep sah nur Cash
gegen den Reserve-Floor, und Cash bleibt im Drawdown konstant (geschlossene
Positionen spuelen es zurueck). Das Ergebnis war ein Verlust-Verstaerker:
$10.000 -> $8.668 Equity bei ~konstant $7.100 investiert = 71% -> 81.9%.

Zwei Portfolio-Grenzen sind jetzt verdrahtet:
  1. exposure_headroom deckelt `deployable` (SIZING statt Binaer-Block —
     der Bot regelt die Groesse selbst herunter, statt stumm zu stoppen).
  2. correlation_gate (injiziert, damit die Funktion pur/testbar bleibt)
     filtert Kandidaten, die zu einer bestehenden Position r >= 0.80 laufen.

hybrid-whitelist (fix/core-sweep-auto-discovery 2026-07-22):
Core-Sweep liest Kandidaten aus zwei Quellen zusammen:
  1. Config-Whitelist (statisch, pinned, expires=NULL)
  2. DB-Tabelle core_sweep_whitelist (dynamisch, discovery-geschrieben,
     expires_at TTL 24h)

Discovery-Worker schreibt FRESH-Signale (conviction HIGH/MEDIUM, score >= 35,
rsi < 75) automatisch in die DB-Whitelist — Core-Sweep findet so Instrumente,
die nicht in der statischen Whitelist stehen, aber gerade ein starkes Signal
liefern.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SweepOrder:
    symbol: str
    instrument_id: int
    amount_usd: float
    atr_pct: float | None = None


def _cfg_block(cfg: dict) -> dict:
    return ((cfg or {}).get("trading", {}) or {}).get("core_sweep", {}) or {}


def is_enabled(cfg: dict) -> bool:
    return bool(_cfg_block(cfg).get("enabled", False))


def _ensure_core_sweep_whitelist_table(db: Any) -> None:
    """Lazy migration: core_sweep_whitelist-Tabelle anlegen (idempotent)."""
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS core_sweep_whitelist (
                instrument_id INTEGER PRIMARY KEY,
                symbol TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'config',
                score REAL,
                conviction TEXT,
                added_at TEXT NOT NULL DEFAULT (datetime('now')),
                expires_at TEXT,
                UNIQUE(instrument_id, source)
            )
        """)
    except Exception:
        pass  # table already exists or similar


def _load_db_whitelist(db: Any) -> dict[str, int]:
    """Lade dynamische Core-Sweep-Whitelist aus DB.

    Filtert abgelaufene Einträge (expires_at < now) und gibt
    {symbol: instrument_id} fuer noch gueltige Einzurueck.
    """
    _ensure_core_sweep_whitelist_table(db)
    try:
        rows = db.fetchall("""
            SELECT symbol, instrument_id
            FROM core_sweep_whitelist
            WHERE source = 'discovery'
              AND (expires_at IS NULL OR expires_at > datetime('now'))
        """)
        return {row["symbol"]: int(row["instrument_id"]) for row in rows}
    except Exception:
        return {}


def _recent_core_sweep_rejects(db: Any, instrument_id: int, hours: float) -> int:
    """Anzahl REJECTED/FAILED CORE_SWEEP-Trades fuer *instrument_id* in den
    letzten *hours* Stunden.

    Deterministische Alternative zum LLM-gefuehrten Ghost-Blacklist: ein
    Titel, der wiederholt am Veto-Gate oder an der Execution scheitert,
    ohne je eine Order auszufuehren, soll nicht bei jedem 15-Minuten-Zyklus
    erneut versucht werden (siehe reject_cooldown_after in plan_core_sweep).

    Zeitbasis: trades.created_at wird per DEFAULT datetime('now') befuellt
    (scripts/init_db.py), was in SQLite bereits echtes UTC liefert. Der
    Cutoff hier verwendet dieselbe datetime('now', ?)-Form -- beide Seiten
    sind also echtes UTC (siehe systemischer Fix des datetime('now','utc')-
    Stolpersteins, der frueher hier wie ueberall im Code den lokalen
    TZ-Offset ein zweites Mal abgezogen hat).
    """
    try:
        row = db.fetchone(
            """
            SELECT COUNT(*) AS n
            FROM trades t JOIN signals s ON s.id = t.signal_id
            WHERE t.instrument_id = ?
              AND s.signal_type = 'CORE_SWEEP'
              AND t.status IN ('REJECTED', 'FAILED')
              AND t.created_at >= datetime('now', ?)
            """,
            (instrument_id, f"-{hours} hours"),
        )
        return int(row["n"]) if row and row["n"] is not None else 0
    except Exception:
        return 0


def _prune_expired_db_whitelist(db: Any) -> int:
    """Loesche abgelaufene Eintraege aus core_sweep_whitelist.

    Gibt Anzahl der geloeschten Eintraege zurueck.
    """
    _ensure_core_sweep_whitelist_table(db)
    try:
        cur = db.execute("""
            DELETE FROM core_sweep_whitelist
            WHERE source = 'discovery'
              AND expires_at IS NOT NULL
              AND expires_at <= datetime('now')
        """)
        return cur.rowcount if hasattr(cur, "rowcount") else 0
    except Exception:
        return 0


def plan_core_sweep(
    cfg: dict,
    equity: float,
    cash: float,
    regime: str,
    held_instrument_ids: set[int] | None = None,
    atr_by_id: dict[int, float] | None = None,
    rsi_by_id: dict[int, float] | None = None,
    db: Any | None = None,
    total_exposed: float | None = None,
    max_exposure_pct: float | None = None,
    open_positions: list[dict] | None = None,
    correlation_gate: Any | None = None,
    market_open: Any | None = None,
) -> tuple[list[SweepOrder], list[str]]:
    """Plane Core-Sweep-Orders fuer ueberschuessiges Cash.

    Rein & seiteneffektfrei — eignet sich fuer Dry-Log UND Live. Gibt
    (orders, reasons) zurueck; leere orders + genau ein reason erklaeren,
    warum nicht gesweept wurde.

    Sizing: per_position_pct*equity je Titel, geclampt auf max_position_pct
    und auf den deploybaren Rest. Nie unter reserve_floor_pct Cash. Bis
    max_sweeps_per_run Titel pro Lauf ("zuegig"). Kandidaten = Whitelist-Titel,
    die noch nicht gehalten werden (kein Core-Pyramiding — Diversifikation),
    optional RSI-gefiltert (nicht in einen extended Titel kaufen). Sortiert
    nach ATR aufsteigend: die stabilsten Anker (SPY) zuerst.

    Hybrid-Whitelist (fix/core-sweep-auto-discovery 2026-07-22):
    Kombiniert config.yaml-Whitelist mit DB-Tabelle core_sweep_whitelist
    (Discovery-geschrieben, FRESH-Signale). Config-Werte haben Vorrang
    (kein Duplikat bei gleicher instrument_id).

    Portfolio-Grenzen (fix/core-sweep-portfolio-gates 2026-08-12), beide
    optional und fail-open, damit ein Aufrufer ohne die neuen Argumente
    exakt das alte Verhalten behaelt:

    total_exposed / max_exposure_pct
        Deckelt `deployable` zusaetzlich auf den Abstand zum Exposure-Cap.
        BEWUSST als Sizing-Input, nicht als Binaer-Gate: liegt das Portfolio
        knapp unter dem Cap, sweept der Bot eine KLEINERE Tranche statt gar
        nichts — er regelt sich selbst herunter und bleibt handlungsfaehig.
        Erst bei erschoepftem Headroom faellt die Planung auf 0 Orders.

    open_positions / correlation_gate
        `correlation_gate(symbol, open_positions) -> (allowed, reason)` wird
        pro Kandidat aufgerufen. Injiziert statt importiert, damit diese
        Funktion pur und ohne yfinance/DB-Mocks testbar bleibt. Fehlt der
        Gate, verhaelt sich die Planung wie bisher (fail-open); wirft er,
        wird der Kandidat DURCHGELASSEN — eine Korrelations-Stoerung darf
        das Cash-Deployment nicht lahmlegen.

    market_open
        `market_open(instrument_id) -> bool | None` — injiziert (wie
        correlation_gate) statt importiert, damit der Planer pur bleibt.
        True/None = Kandidat bleibt; False = SKIP (Markt aktuell zu).
        fix/core-sweep-market-open (2026-09-23): planen NUR in offene
        Maerkte. Ohne diesen Filter plant der Bot ueber Nacht US-Titel,
        die im execution_worker auf DEFER -> market-closed-TTL ->
        REJECTED landen (273 'Markt >4h geschlossen — Signal veraltet'
        Rejections in 30d, fast alle US-listed, in den US-closed
        Stunden). SKIP statt Order ist kostenlos (not buying costs
        nothing) und der Titel wird im naechsten Lauf wieder geplant,
        sobald sein Markt offen ist. `market_open` None/abwesend =
        fail-open (altes Verhalten); wirft er, wird der Kandidat
        DURCHGELASSEN — ein Kalender-Fehler darf das Cash-Deployment
        nicht lahmlegen.
    """
    cs = _cfg_block(cfg)
    reasons: list[str] = []
    held = set(held_instrument_ids or set())
    atr_by_id = atr_by_id or {}
    rsi_by_id = rsi_by_id or {}

    if equity <= 0:
        return [], ["Core-Sweep: equity <= 0 (fail-closed)"]

    # ── Regime-Gate ──────────────────────────────────────────────────────────
    allowed_regimes = [str(r).upper() for r in cs.get("regimes", ["NORMAL", "CAUTION"])]
    if str(regime).upper() not in allowed_regimes:
        return [], [f"Core-Sweep: Regime {regime} nicht in {allowed_regimes} — pausiert"]

    # ── D4 (2026-10-09): harter Cap max. 3 offene CORE_SWEEP-Positionen ─────
    # Ohne Cap wirkt der 0.25x-DEFENSIVE-Faktor nicht — der $200-Floor im
    # execution_worker hebt jede kleiner dimensionierte Sweep-Tranche auf
    # $200, dadurch laeuft Core-Sweep in DEFENSIVE mit voller Groesse.
    # Der Cap zaehlt offene CORE_SWEEP-Positionen (DB) und pausiert den
    # Sweep, wenn er erreicht ist. Fail-open: DB-Fehler = Cap ignoriert.
    _d4_max_open = int(cs.get("max_open_positions", 3))
    if _d4_max_open > 0 and db is not None:
        try:
            _d4_row = db.fetchone("""
                SELECT COUNT(*) AS n
                FROM trades t
                JOIN signals s ON s.id = t.signal_id
                WHERE t.status IN ('APPROVED', 'SUBMITTING', 'ACTIVE', 'VERIFIED', 'OPEN')
                  AND s.signal_type = 'CORE_SWEEP'
            """)
            _d4_open = int(_d4_row["n"]) if _d4_row else 0
            if _d4_open >= _d4_max_open:
                return [], [
                    f"Core-Sweep (D4): {_d4_open} offene CORE_SWEEP-Positionen "
                    f">= Cap {_d4_max_open} — pausiert"
                ]
            reasons.append(
                f"Core-Sweep (D4): {_d4_open}/{_d4_max_open} offene "
                f"CORE_SWEEP-Positionen"
            )
        except Exception:  # noqa: BLE001 — DB-Fehler -> Cap fail-open
            pass

    # ── D4 (2026-10-09): Vorregistrierte Abbruchregel ────────────────────────
    # VoLLi-Entscheid (Report D4): CORE_SWEEP in DEFENSIVE laeuft mit
    # vorregistrierter Abbruchregel — CORE_SWEEP-Drawdown > $150 ODER
    # letzte 10 CORE_SWEEP-Trades mit WR < 30 % -> Sweep wird pausiert.
    # Der Drawdown ist CORE_SWEEP-spezifisch (Summe negativer PnL der
    # letzten 10 geschlossenen Sweep-Trades), NICHT der Portfolio-DD.
    # Fail-open: DB-Fehler = Regel ignoriert.
    if db is not None and str(regime).upper() == "DEFENSIVE":
        try:
            # Letzte 10 geschlossene CORE_SWEEP-Trades (neu zuerst).
            _d4_recent = db.fetchall("""
                SELECT t.pnl_usd
                FROM trades t
                JOIN signals s ON s.id = t.signal_id
                WHERE s.signal_type = 'CORE_SWEEP'
                  AND t.status = 'CLOSED'
                  AND t.pnl_usd IS NOT NULL
                ORDER BY t.closed_at DESC
                LIMIT 10
            """)
            if len(_d4_recent) >= 5:  # min. 5 Trades fuer aussagekraeftige Regel
                # (a) Drawdown > $150: Summe negativer PnL der letzten 10.
                _d4_dd = sum(-r["pnl_usd"] for r in _d4_recent if r["pnl_usd"] < 0)
                if _d4_dd > 150.0:
                    return [], [
                        f"Core-Sweep (D4-Abort): Sweep-DD ${_d4_dd:.0f} "
                        f"> $150 (letzte {len(_d4_recent)} Trades) — pausiert"
                    ]
                # (b) WR < 30 %: Gewinnquote der letzten 10.
                _d4_wins = sum(1 for r in _d4_recent if r["pnl_usd"] > 0)
                _d4_wr = _d4_wins / len(_d4_recent)
                if _d4_wr < 0.30:
                    return [], [
                        f"Core-Sweep (D4-Abort): WR letzter {len(_d4_recent)} "
                        f"Sweep-Trades {_d4_wr:.0%} < 30% — pausiert"
                    ]
                reasons.append(
                    f"Core-Sweep (D4-Abort): DD ${_d4_dd:.0f}<=150, "
                    f"WR {_d4_wr:.0%}>=30% OK"
                )
            else:
                reasons.append(
                    f"Core-Sweep (D4-Abort): nur {len(_d4_recent)} "
                    f"geschlossene Sweep-Trades (min. 5 fuer Regel) — skip"
                )
        except Exception as _d4_abort_exc:  # noqa: BLE001 — fail-open
            reasons.append(
                f"Core-Sweep (D4-Abort): Regel-Check fehlgeschlagen "
                f"({_d4_abort_exc}) — fail-open"
            )

    reserve_target_pct = float(cs.get("reserve_target_pct", 15.0))
    reserve_floor_pct = float(cs.get("reserve_floor_pct", 10.0))
    per_position_pct = float(cs.get("per_position_pct", 4.0))
    max_position_pct = float(cs.get("max_position_pct", 6.0))
    max_sweeps = int(cs.get("max_sweeps_per_run", 4))
    rsi_overbought = float(cs.get("rsi_overbought", 75.0))
    config_whitelist: dict = cs.get("whitelist", {}) or {}

    # ── Hybrid-Whitelist: Config + DB zusammenfuegen ─────────────────────────
    whitelist: dict[str, int] = dict(config_whitelist)  # copy
    if db is not None:
        # Prune expired entries (silent)
        _pruned = _prune_expired_db_whitelist(db)
        if _pruned:
            reasons.append(f"Core-Sweep: {_pruned} abgelaufene DB-Einträge entfernt")

        # Load DB whitelist
        db_whitelist = _load_db_whitelist(db)
        for sym, iid in db_whitelist.items():
            if sym not in whitelist:  # config hat Vorrang
                whitelist[sym] = iid

    reserve_target = equity * reserve_target_pct / 100.0
    reserve_floor = equity * reserve_floor_pct / 100.0
    target_size = round(equity * per_position_pct / 100.0, 2)
    max_size = equity * max_position_pct / 100.0

    excess = cash - reserve_target
    above_floor = cash - reserve_floor
    deployable = min(excess, above_floor)

    # ── Exposure-Headroom (fix/core-sweep-portfolio-gates) ───────────────────
    # Der Abstand zum Gesamt-Exposure-Cap deckelt das deploybare Cash. Das ist
    # dieselbe Grenze, die check_exposure_gate dem regulaeren Signalpfad
    # auferlegt — Core-Sweep hat sie bisher schlicht nicht gesehen.
    exposure_headroom: float | None = None
    if total_exposed is not None and max_exposure_pct is not None and equity > 0:
        exposure_cap_usd = equity * float(max_exposure_pct) / 100.0
        exposure_headroom = exposure_cap_usd - float(total_exposed)
        current_pct = float(total_exposed) / equity * 100.0
        if exposure_headroom < deployable:
            reasons.append(
                f"Core-Sweep: Exposure {current_pct:.1f}% (Cap {float(max_exposure_pct):.0f}%) "
                f"deckelt deploybar ${deployable:.0f} → ${max(exposure_headroom, 0.0):.0f}"
            )
        deployable = min(deployable, exposure_headroom)

    if target_size <= 0:
        return [], ["Core-Sweep: per_position_pct=0 — nichts zu tun"]
    if deployable < target_size:
        if exposure_headroom is not None and exposure_headroom < target_size:
            # Eigene Meldung: NICHT "kein Ueberschuss" — Cash ist da, aber das
            # Portfolio ist voll. Sonst sucht der Operator den Fehler beim Cash.
            return [], [
                f"Core-Sweep: Exposure {float(total_exposed) / equity * 100.0:.1f}% am Cap "
                f"{float(max_exposure_pct):.0f}% — Headroom ${max(exposure_headroom, 0.0):.0f} "
                f"< Tranche ${target_size:.0f}, kein Sweep (Cash ${cash:.0f} bleibt liegen)"
            ]
        return [], [
            f"Core-Sweep: kein Ueberschuss (Cash ${cash:.0f}, Ziel-Reserve "
            f"${reserve_target:.0f}, deploybar ${deployable:.0f} < Tranche ${target_size:.0f})"
        ]

    # ── Kandidaten: Whitelist-Titel, die noch NICHT gehalten werden ──────────
    candidates: list[tuple[str, int, float | None]] = []
    for sym, iid in whitelist.items():
        try:
            iid = int(iid)
        except (TypeError, ValueError):
            continue
        if iid in held:
            continue  # kein Core-Pyramiding
        rsi = rsi_by_id.get(iid)
        if rsi is not None and rsi > rsi_overbought:
            reasons.append(f"{sym}: RSI {rsi:.0f} > {rsi_overbought:.0f} — nicht extended kaufen")
            continue
        # fix/eligibility-tradability-learning (2026-09-18): der Planer war
        # die EINZIGE Stelle ohne is_tradable-Filter — Discovery/Signal
        # pruefen ihn, Core-Sweep nicht. Ein Broker-nicht-handelbarer Titel
        # (allowOpenPosition=false) wanderte so ueber die Auto-Whitelist
        # (24h-TTL) alle 24h zurueck und jeder Versuch endete FAILED:
        # NSDQ100.FUT 22x + JPN225.FUT 15x + HKG50.FUT 3x, 0 Orders.
        # is_tradable=NULL heisst "noch nie geprueft" -> fail-open (1).
        if db is not None:
            try:
                _trow = db.fetchone(
                    "SELECT is_tradable FROM instruments WHERE instrument_id = ?",
                    (iid,),
                )
                if _trow is not None and _trow["is_tradable"] == 0:
                    reasons.append(f"{sym}: is_tradable=0 (Broker) — SKIP")
                    continue
            except Exception:
                pass  # Spalte fehlt / DB-Fehler -> fail-open

        # Delisted-Gate (fix/core-sweep-delisted 2026-08-07):
        # AI_2878 (instrument_id=2878) war 'delisted' auf Yahoo,
        # aber Core-Sweep hat weiter darauf geplant → 16x FAILED.
        # "ID/Symbol MISMATCH". instruments.yahoo_status='delisted'
        # sofort überspringen.
        if db is not None:
            try:
                _del_row = db.fetchone(
                    "SELECT yahoo_status FROM instruments WHERE instrument_id = ?",
                    (iid,),
                )
                if _del_row and str(_del_row.get("yahoo_status") or "").strip().lower() == "delisted":
                    reasons.append(f"{sym}: yahoo_status=delisted — SKIP")
                    continue
            except Exception:
                pass  # fail-open

        # Broker-Minimum-Check (fix/core-sweep-min-exposure 2026-07-27):
        # Orders unter minPositionExposure werden von eToro mit 720 abgelehnt.
        if db is not None:
            try:
                _min_row = db.fetchone(
                    "SELECT min_position_amount FROM instruments WHERE instrument_id = ?",
                    (iid,),
                )
                if _min_row and _min_row["min_position_amount"]:
                    _broker_min = float(_min_row["min_position_amount"])
                    _per_pos = equity * per_position_pct / 100.0
                    if _per_pos < _broker_min:
                        reasons.append(f"{sym}: ${_per_pos:.2f} < Broker-Min ${_broker_min:.0f} — SKIP")
                        continue
            except Exception:
                pass  # Spalte fehlt -> fail-open
        # Reject-Cooldown (fix/core-sweep-reject-cooldown 2026-07-28):
        # ohne diesen Check plant der Worker jeden Zyklus (alle 15min) einen
        # neuen Sweep fuer denselben Titel, selbst wenn Veto-Worker oder
        # Execution ihn gerade erst zurueckgewiesen haben -- ein Titel, der
        # nie eine einzige Aktie kauft, blieb sonst bis zum Whitelist-Expiry
        # (bis zu 24h) "frei" und wurde stur neu versucht (UBER-Incident
        # 2026-07-28: 19 Core-Sweep-Versuche in 8h, 0 Erfolge, jeder davon
        # ein eigener LLM-Veto-Call).
        cooldown_after = int(cs.get("reject_cooldown_after", 3))
        if db is not None and cooldown_after > 0:
            cooldown_hours = float(cs.get("reject_cooldown_hours", 3.0))
            n_rejects = _recent_core_sweep_rejects(db, iid, cooldown_hours)
            if n_rejects >= cooldown_after:
                reasons.append(
                    f"{sym}: {n_rejects} CORE_SWEEP-Rejections in "
                    f"{cooldown_hours:.0f}h — Cooldown, SKIP"
                )
                continue

        # Korrelations-Gate (fix/core-sweep-portfolio-gates): derselbe Schutz,
        # den der regulaere Signalpfad ueber check_buy_gate bekommt. Bisher
        # konnte Core-Sweep beliebig viele gleichlaufende Titel aufsatteln —
        # genau das, wogegen Diversifikation schuetzen soll.
        if correlation_gate is not None:
            try:
                _allowed, _corr_reason = correlation_gate(sym, open_positions or [])
                if not _allowed:
                    reasons.append(f"{sym}: {_corr_reason} — SKIP")
                    continue
            except Exception as exc:
                # Fail-open: eine Korrelations-Stoerung (yfinance-Ausfall,
                # Cache-Fehler) darf das Cash-Deployment nie blockieren.
                reasons.append(f"{sym}: Korrelations-Check uebersprungen ({exc})")

        # fix/core-sweep-market-open (2026-09-23): planen NUR in offene
        # Maerkte. Injiziert statt importiert (wie correlation_gate); die
        # eigentliche Boersenauflösung (resolve_market_fields +
        # is_market_open) steht im signal_worker. Die execution_worker-
        # BUY-Gate bleibt die fail-closed Letztlinie — hier ist es reine
        # Effizienz, damit ueber Nacht keine US-Titel geplant werden, die
        # nur auf DEFER -> market-closed-TTL -> REJECTED laufen.
        if market_open is not None:
            try:
                if market_open(iid) is False:
                    reasons.append(f"{sym}: market closed — SKIP (next run)")
                    continue
            except Exception as exc:
                # Fail-open: ein Kalender-/Auflösungs-Fehler darf das
                # Cash-Deployment nicht blockieren (BUY-Gate bleibt letzte
                # Linie).
                reasons.append(f"{sym}: market-open-Check uebersprungen ({exc})")

        candidates.append((sym, iid, atr_by_id.get(iid)))

    if not candidates:
        reasons.insert(0, "Core-Sweep: keine freien Core-Titel (alle gehalten/gefiltert)")
        return [], reasons

    # Stabilste Anker zuerst (ATR aufsteigend; None ans Ende)
    candidates.sort(key=lambda c: (c[2] is None, c[2] if c[2] is not None else 0.0))

    orders: list[SweepOrder] = []
    remaining = deployable
    for sym, iid, atr in candidates:
        if len(orders) >= max_sweeps:
            break
        if remaining < target_size:
            break
        size = round(min(target_size, max_size, remaining), 2)
        orders.append(SweepOrder(symbol=sym, instrument_id=iid, amount_usd=size, atr_pct=atr))
        remaining -= size

    reasons.insert(
        0,
        f"Core-Sweep: Cash ${cash:.0f} > Reserve ${reserve_target:.0f} → "
        f"{len(orders)} Sweep(s) á ~${target_size:.0f} geplant (deploybar ${deployable:.0f})",
    )
    return orders, reasons
