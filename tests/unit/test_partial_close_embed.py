"""feat/partial-close-embed: Teilverkauf visuell unterscheidbar."""
import bot.discord_embeds as DE


def _cap(monkeypatch):
    seen = {}
    monkeypatch.setattr(DE, "_post_embed", lambda e, ch, dry: seen.update(embed=e, channel=ch) or True)
    monkeypatch.setattr(DE, "insert_system_log", lambda *a, **k: None)
    return seen


def test_partial_close_titled_teilverkauf(monkeypatch):
    seen = _cap(monkeypatch)
    DE.post_position_closed_embed(symbol="LUS1.DE", amount_usd=86.1, pnl_usd=15.5,
                                  pnl_pct=18.0, reason="Profit-Taking: rung", close_pct=25.0)
    e = seen["embed"]
    assert "TEILVERKAUF 25%" in e["title"] and "✂️" in e["title"]
    assert "Rest der Position bleibt offen" in e["description"]
    assert seen["channel"] == DE.DISCORD_TRADE_CHANNEL


def test_full_close_unchanged(monkeypatch):
    seen = _cap(monkeypatch)
    DE.post_position_closed_embed(symbol="MSI", amount_usd=145.0, pnl_usd=-4.79,
                                  pnl_pct=-3.3, reason="SL", close_pct=100.0)
    assert "POSITION CLOSED" in seen["embed"]["title"]
    assert "TEILVERKAUF" not in seen["embed"]["title"]


def test_default_close_pct_is_full(monkeypatch):
    seen = _cap(monkeypatch)
    DE.post_position_closed_embed(symbol="X", amount_usd=100.0, pnl_usd=1.0, pnl_pct=1.0)
    assert "POSITION CLOSED" in seen["embed"]["title"]
