"""feat/market-movers: pure Mover-Berechnung aus Bulk-Closing + Rates."""
import pytest

from bot.core.market_movers import compute_movers


def _closing(iid, daily, open_=True):
    return {"instrumentId": iid, "isMarketOpen": open_,
            "closingPrices": {"daily": {"price": daily}}}


def _rate(last):
    return {"lastExecution": last}


def test_threshold_and_sorting():
    closings = [_closing(1, 100.0), _closing(2, 100.0), _closing(3, 100.0)]
    rates = {1: _rate(106.0), 2: _rate(93.0), 3: _rate(102.0)}  # +6, -7, +2
    out = compute_movers(closings, rates, {}, min_pct=5.0, top_n=10)
    assert [(i, pytest.approx(m)) for i, m in out] == [(2, -7.0), (1, 6.0)]  # nach |move| sortiert, +2% raus


def test_crypto_threshold_higher():
    closings = [_closing(100000, 100.0)]
    rates = {100000: _rate(106.0)}  # +6% — unter Krypto-Schwelle 8
    assert compute_movers(closings, rates, {100000: "crypto"}) == []
    rates = {100000: _rate(109.0)}  # +9%
    out = compute_movers(closings, rates, {100000: "crypto"})
    assert len(out) == 1 and out[0][0] == 100000 and out[0][1] == pytest.approx(9.0)


def test_closed_market_and_sentinel_filtered():
    closings = [
        _closing(1, 100.0, open_=False),   # Markt zu
        _closing(2, -1.0),                 # eToro-Sentinel "kein Wert"
        _closing(3, 100.0),
    ]
    rates = {1: _rate(110.0), 2: _rate(110.0), 3: _rate(0)}  # 3: kein Preis
    assert compute_movers(closings, rates, {}) == []


def test_top_n_cap():
    closings = [_closing(i, 100.0) for i in range(20)]
    rates = {i: _rate(100.0 + 5 + i) for i in range(20)}
    out = compute_movers(closings, rates, {}, top_n=5)
    assert len(out) == 5
    assert out[0][0] == 19  # groesster Move zuerst


def test_pennystock_artifacts_filtered():
    """fix/movers-sanity: >max_pct oder Mini-Kurse fliegen raus (VXT.DE +170%)."""
    closings = [_closing(1, 0.02), _closing(2, 100.0), _closing(3, 100.0)]
    rates = {1: _rate(0.05), 2: _rate(270.0), 3: _rate(110.0)}  # +150%, +170%, +10%
    out = compute_movers(closings, rates, {}, max_pct=25.0, min_price=0.5)
    assert [i for i, _ in out] == [3]
