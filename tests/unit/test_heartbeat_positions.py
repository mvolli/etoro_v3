"""feat/heartbeat-positions: offene Positionen dreispaltig im Heartbeat-Embed."""
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


def _pos_fields(embed):
    # Positions-Spalten sind die letzten Felder (phase_durations fehlt im Test).
    flds = embed["fields"]
    idx = next((i for i, f in enumerate(flds) if "Offene Positionen" in f["name"]), None)
    return flds[idx:] if idx is not None else []


def test_three_inline_columns(monkeypatch):
    seen = _capture(monkeypatch)
    pos = [{"symbol": f"S{i}", "unrealized_pnl_pct": float(i - 3)} for i in range(9)]
    DE.post_heartbeat_embed(**_base(positions_summary=pos))
    cols = _pos_fields(seen["embed"])
    assert len(cols) == 3
    assert all(c["inline"] for c in cols)
    assert "(9)" in cols[0]["name"]
    # schlechteste (S0 = -3%) in der ersten Spalte oben
    assert cols[0]["value"].startswith("🔴 S0")


def test_all_positions_present(monkeypatch):
    seen = _capture(monkeypatch)
    pos = [{"symbol": f"X{i}", "unrealized_pnl_pct": 1.0} for i in range(7)]
    DE.post_heartbeat_embed(**_base(positions_summary=pos))
    joined = " ".join(c["value"] for c in _pos_fields(seen["embed"]))
    for i in range(7):
        assert f"X{i}" in joined


def test_no_field_when_empty(monkeypatch):
    seen = _capture(monkeypatch)
    DE.post_heartbeat_embed(**_base(positions_summary=None))
    assert not any("Offene Positionen" in f["name"] for f in seen["embed"]["fields"])


def test_overflow_note(monkeypatch):
    seen = _capture(monkeypatch)
    many = [{"symbol": f"S{i}", "unrealized_pnl_pct": float(i)} for i in range(40)]
    DE.post_heartbeat_embed(**_base(positions_summary=many))
    cols = _pos_fields(seen["embed"])
    assert "(40)" in cols[0]["name"]
    assert any("weitere" in c["value"] for c in cols)


def test_no_stop_loss_marker(monkeypatch):
    seen = _capture(monkeypatch)
    DE.post_heartbeat_embed(**_base(positions_summary=[
        {"symbol": "NG.L", "unrealized_pnl_pct": 1.2, "is_no_stop_loss": 1},
    ]))
    joined = " ".join(c["value"] for c in _pos_fields(seen["embed"]))
    assert "⚠️" in joined and "NG.L" in joined
