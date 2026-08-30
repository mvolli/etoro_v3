"""feat/deploy-idle-cash (2026-08-29) — Idle-Cash-Deployment via Floor-Bump.

VoLLi-Direktive 2026-08-29: Wenn der Bot viel Free Cash haelt (>= Schwellwert
des Equity) und eine BUY-Signal nur wegen des Floors (Signal-Floor oder
Dust-Floor) zu klein waere, BUMPEN wir die Order auf den Floor und lassen den
Trade genehmigt werden — statt sie zu REJECT. Damit wird der Idle-Cash-Pool
aktiv deployed, bis Free Cash unter die Schwellwerte faellt; danach greift
wieder das normale Reject-Verhalten.

Semantik (bewusst eng):
  - Floor bleibt der Regime-Wert (DEFENSIVE=100, CRITICAL=150, …) — es wird
    NICHT ueber den Floor aufgehoert.
  - Die max_trade_pct-Klammer (execution_worker Guard, DEFENSIVE) wird
    gespiegelt: der bumped Betrag wird auf den Cap geklammert, wenn der Floor
    ihn uebertreffen wuerde (AGENTS.md: Klammer + Guard IMMER gemeinsam).
  - Broker-Minimum (eToro 720, instruments.min_position_amount) bleibt voll
    in Kraft — der Bump laeuft VOR dem Gate, nicht ueber ihm.
  - Config ``trading.deploy_idle_cash_min_free_cash_pct`` = 0 deaktiviert.

Pure Functions, ohne DB/API — testbar ohne Mocks.
"""

from __future__ import annotations


def deploy_bump_active(
    cash: float, equity: float, min_free_cash_pct: float
) -> bool:
    """True wenn der Idle-Cash-Deployment-Modus fuer diesen Zyklus gilt.

    ``min_free_cash_pct <= 0`` deaktiviert den Modus (Config-Default bei
    fehlendem Key). Fehlende/ungueltige Equity-Werte fail-closed (kein Bump):
    ohne belastbare Equity-Basis darf Cash nicht gegen Free-Cash-Quote
    geprueft werden.
    """
    if min_free_cash_pct <= 0 or equity <= 0 or cash < 0:
        return False
    return cash >= equity * min_free_cash_pct / 100.0


def bumped_amount(
    buy_amount: float, floor: float, cap: float | None = None
) -> float:
    """Sub-Floor-Betrag auf den Floor anheben, kappbar durch ``cap``.

    ``cap`` ist die max_trade_pct-Klammer (DEFENSIVE) in USD; ``None`` =
    keine Klammer. Invariant: das Ergebnis ist immer >= ``buy_amount``,
    solange ``buy_amount <= floor`` und (``cap`` None oder ``cap >=
    buy_amount``) — die Aufrufer pruefen genau diese Voraussetzung.
    """
    if cap is not None:
        return round(min(floor, cap), 2)
    return round(floor, 2)
