"""fix/risk-block-framing: Risiko-Gate-Block ist orange 'BLOCKIERT', kein rotes FAILED."""
import bot.discord_embeds as DE


def _capture(monkeypatch):
    seen = {}
    def fake_post(embed, channel, dry_run):
        seen["embed"] = embed
        return True
    monkeypatch.setattr(DE, "_post_embed", fake_post)
    monkeypatch.setattr(DE, "insert_system_log", lambda *a, **k: None)
    return seen


def test_blocked_is_orange_shield(monkeypatch):
    seen = _capture(monkeypatch)
    DE.post_trade_failed_embed("HAYD.L", "BUY", 102.9,
                               error="Spread-Gate: 3.77% > 1.50%", blocked=True)
    e = seen["embed"]
    assert e["color"] == DE.COLOR_ORANGE
    assert "BLOCKIERT" in e["title"]
    assert "geschützt" in e["description"]
    assert any("Risiko-Gate" in f["name"] for f in e["fields"])


def test_real_failure_stays_red(monkeypatch):
    seen = _capture(monkeypatch)
    DE.post_trade_failed_embed("XYZ", "BUY", 100.0, error="eToro 604: insufficient funds")
    e = seen["embed"]
    assert e["color"] == DE.COLOR_RED
    assert "FAILED" in e["title"]


def test_ghost_stays_purple(monkeypatch):
    seen = _capture(monkeypatch)
    DE.post_trade_failed_embed("XYZ", "BUY", 100.0, is_ghost=True)
    assert seen["embed"]["color"] == DE.COLOR_PURPLE
