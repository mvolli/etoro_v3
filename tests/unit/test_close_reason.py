"""feat/close-reason-inference: Schliessungsgrund aus History-API-Daten."""
from bot.workers.reconciler import infer_close_reason


MSI = {  # realer Fall Trade #464 (2026-07-20)
    "closeRate": 403.7, "stopLossRate": 406.39, "takeProfitRate": 521.01,
    "isBuy": True,
    "openTimestamp": "2026-07-17T16:06:37.813Z",
    "closeTimestamp": "2026-07-20T11:00:12.517Z",
}


def test_sl_hit_with_gap():
    r = infer_close_reason(MSI, 464)
    assert "Broker-Stop-Loss" in r and "406.39" in r
    assert "Gap/Slippage" in r          # Fill -0.66% unter dem Stop
    assert "Haltedauer 2.8d" in r
    assert "#464" in r


def test_tp_hit():
    m = dict(MSI, closeRate=521.5)
    r = infer_close_reason(m, 7)
    assert "Broker-Take-Profit" in r and "521.01" in r


def test_external_close_between_levels():
    m = dict(MSI, closeRate=450.0)
    r = infer_close_reason(m, 8)
    assert "Extern geschlossen" in r


def test_sell_direction_sl():
    m = {"closeRate": 106.0, "stopLossRate": 105.0, "takeProfitRate": 90.0,
         "isBuy": False}
    r = infer_close_reason(m, 9)
    assert "Broker-Stop-Loss" in r


def test_missing_data_falls_back():
    r = infer_close_reason({}, 10)
    assert r.startswith("✅ Finalisiert")
