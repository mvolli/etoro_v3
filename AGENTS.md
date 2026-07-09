# AGENTS.md — etoro_v3 (DOX-Child von ~/.hermes/workspace/AGENTS.md)

## Purpose

LIVE-Trading-Bot auf eToro (ECHTES GELD). 7 Worker auf Hermes-Cron
(5–120 min Takt), SQLite-WAL-DB, Trading Bible V5 (4-Regime, risk_scalar,
ATR-Profit-Leiter, Momentum-Fade). Der Bot läuft während JEDER Änderung weiter.

## Ownership

- Code/Tests/Docs in diesem Repo: der Agent (Commits auf `main`, Push nach Test-Grün).
- Alle Trading-Parameter (SL, Sizing, Regime-Thresholds, Loss-Limits) werden autonom vom
  LLM Review Worker optimiert (taeglich 20:00 UTC, src/bot/workers/llm_review_worker.py).
  Unveraenderliche Grenzen: BIBLE_HARD_LIMITS im Quellcode — diese ueberschreitet keine LLM.
- Cron-Zeitpläne: `~/.hermes/cron/jobs.json` (Root-Scope), gespiegelt in `crontab.txt` hier.

## Local Contracts (Invarianten)

- Einzige DB: `data/trading.db`. Kill-Switch: `data/kill_switch.flag`
  (JSON scope: daily=Auto-Clear nächster UTC-Tag, sonst manuell).
- State-Machine: PENDING_APPROVAL→APPROVED→SUBMITTING→ACTIVE→CLOSING→
  CLOSED/FAILED/REJECTED. Neue Übergänge brauchen Reconciler-Recovery-Pfad.
- eToro-API: 200/statusID=1 = Order ANGENOMMEN, nicht ausgeführt →
  Portfolio-Polling-Verifikation Pflicht. Kein SL-Update-Endpoint.
- Einmal-Aktionen pro Position IMMER persistieren (position_state) —
  sonst feuert der 5-min-Zyklus endlos.
- DB-Migrationen idempotent (ALTER TABLE in try/except, je Spalte einzeln).
- Worker-Wrapper liegen in `~/.hermes/scripts/v3_*.sh` — Script-Änderungen
  in `scripts/` müssen dorthin kopiert werden, sonst läuft der Cron alt.

## Work Guidance

Workflow: Skill `finance/etoro-v3-safe-change` befolgen (lesen → pure
Function → Tests → volle Suite → Commit/Push → Live-Verifikation).
Debugging: Skills `finance/trading-system-debugging`, `ghost-order-debugging`.
Regeln: Skill `finance/trading-bible-v5`. NIEMALS-Liste der Root-AGENTS.md
gilt uneingeschränkt.

## Verification

```bash
PYTHONPATH=src python3 -m pytest                  # volle Suite, alles grün
sqlite3 -readonly data/trading.db "SELECT key,value FROM system_state WHERE key LIKE 'LAST_RUN_%'"
bash scripts/etoro_kill_switch_watchdog.sh        # leer = gesund
```

## Child DOX Index

Keine Children — dieses Repo ist eine Einheit.
