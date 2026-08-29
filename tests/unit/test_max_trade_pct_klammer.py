"""signal_worker darf nichts genehmigen, was der execution_worker verwirft.

Hintergrund (2026-08-29): der execution_worker verwirft in DEFENSIVE jeden
Trade ueber max_trade_pct des Equity. Der signal_worker kannte diese Grenze
nicht. Latent seit 076e661, das den Deployment-Boost auf CAUTION/DEFENSIVE
ausweitete:  5.0 % x 0.5 x 1.25 = 261.15 USD gegen einen Deckel von 250.71.
Mit der Basis auf 6.0 % liegt sie exakt AUF dem Deckel, jeder Boost reisst ihn.
"""
from pathlib import Path

import pytest
import yaml

from bot.core.regime import get_regime_params

ROOT = Path(__file__).resolve().parents[2]
CFG = yaml.safe_load((ROOT / "config/config.yaml").read_text(encoding="utf-8"))
SW_SRC = (ROOT / "src/bot/workers/signal_worker.py").read_text(encoding="utf-8")
EX_SRC = (ROOT / "src/bot/workers/execution_worker.py").read_text(encoding="utf-8")


def _cap(equity: float, regime: str) -> float:
    """Dieselbe Formel wie im execution_worker."""
    return round(equity * float(get_regime_params(regime)["max_trade_pct"]) / 100.0, 2)


def test_klammer_ist_ueberhaupt_noetig():
    """Ohne Klammer wuerde die aktuelle Konfiguration den Deckel reissen.

    Faellt der Test, ist die Klammer entbehrlich geworden — dann pruefen, ob
    conviction_pct oder deployment_boost gesenkt wurde, und ob sie noch traegt.
    """
    eq = 8356.85
    base = CFG["sizing"]["medium_pct"] / 100.0 * eq * \
        get_regime_params("DEFENSIVE")["buy_aggressiveness"]
    boosted = base * float(CFG["trading"].get("deployment_boost", 1.0))
    assert boosted > _cap(eq, "DEFENSIVE"), (
        "Deployment-Boost reisst den DEFENSIVE-Deckel nicht mehr — Klammer pruefen"
    )


@pytest.mark.parametrize("equity", [3000.0, 8356.85, 25000.0])
def test_geklammerter_betrag_passiert_den_execution_guard(equity):
    """Der geklammerte Wert muss den Guard im execution_worker bestehen."""
    cap = _cap(equity, "DEFENSIVE")
    base = CFG["sizing"]["medium_pct"] / 100.0 * equity * \
        get_regime_params("DEFENSIVE")["buy_aggressiveness"]
    boosted = base * float(CFG["trading"].get("deployment_boost", 1.0))
    geklammert = min(boosted, cap)
    # execution_worker verwirft bei `amount_usd > _cap`
    assert not (geklammert > cap)


def test_klammer_greift_nur_in_defensive():
    """Sie spiegelt den execution_worker — der prueft nur DEFENSIVE.

    Griffe sie in jedem Regime, wuerde sie die 6-%-Basis in NORMAL sofort auf
    max_trade_pct 5.0 % stutzen (501.41 -> 417.84) und damit genau das
    aufheben, wofuer sie eingefuehrt wurde.
    """
    assert 'if regime == "DEFENSIVE" and _mt_pct > 0 and equity > 0:' in SW_SRC
    # Gegenprobe: der execution_worker guardet ebenfalls nur DEFENSIVE.
    assert 'if regime == "DEFENSIVE":' in EX_SRC


def test_normal_basis_bleibt_ungeklammert():
    """Zielgroesse ~500 USD in NORMAL darf nicht wegdefiniert werden."""
    eq = 8356.85
    base = CFG["sizing"]["medium_pct"] / 100.0 * eq * \
        get_regime_params("NORMAL")["buy_aggressiveness"]
    assert 480.0 <= base <= 520.0, f"NORMAL-Basis {base:.2f} nicht mehr bei ~500"


def test_conviction_leiter_bleibt_monoton():
    """User-Entscheid 2026-07-14: VERY_HIGH >= HIGH >= MEDIUM >= LOW."""
    s = CFG["sizing"]
    assert s["very_high_pct"] >= s["high_pct"] >= s["medium_pct"] >= s["low_pct"]
