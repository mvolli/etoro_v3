"""feat/heartbeat-positions: offene Positionen im Heartbeat-Embed."""
from unittest import mock

import bot.discord_embeds as DE


def _capture(monkeypatch):
    seen = {}
    monkeypatch.setattr(DE, "_post_embed", lambda embed, ch, dry: seen.update(embed=embed) or True)
    monkeypatch.setattr(DE, "insert_system_log", lambda *a, **k: None)
    return seen


def _base(**extra):
    d = dict(tick=1, equity=9155.0, cash=5767.0, position_count=3, drawdown_pct=4.1,
             severity="WARNING", cb_active=False, elapsed_s=0.0)
    d.update(extra)
    return d


def test_positions_field_present_and_sorted(monkeypatch):
    seen = _capture(monkeypatch)
    DE.post_heartbeat_embed(**_base(positions_summary=[
        {"symbol": "BTC-USD", "unrealized_pnl_pct": 0.5},
        {"symbol": "MSI", "unrealized_pnl_pct": -3.5},
        {"symbol": "NG.L", "unrealized_pnl_pct": 1.2, "is_no_stop_loss": 1},
    ]))
    fld = next(f for f in seen["embed"]["fields"] if "Offene Positionen" in f["name"])
    assert "(3)" in fld["name"]
    # schlechteste zuerst
    assert fld["value"].index("MSI") < fld["value"].index("BTC-USD")
    assert "🔴 MSI -3.5%" in fld["value"]
    assert "⚠️" in fld["value"]  # NG.L ohne SL


def test_no_positions_field_when_empty(monkeypatch):
    seen = _capture(monkeypatch)
    DE.post_heartbeat_embed(**_base(positions_summary=None))
    assert not any("Offene Positionen" in f["name"] for f in seen["embed"]["fields"])


def test_overflow_capped(monkeypatch):
    seen = _capture(monkeypatch)
    many = [{"symbol": f"S{i}", "unrealized_pnl_pct": float(i)} for i in range(40)]
    DE.post_heartbeat_embed(**_base(positions_summary=many))
    fld = next(f for f in seen["embed"]["fields"] if "Offene Positionen" in f["name"])
    assert "weitere" in fld["value"] and "(40)" in fld["name"]
