"""Signal-Worker-Embed: Ablehnungsgruende werden lesbar gekuerzt, nicht abgeschnitten.

17.10.2026: "markt_gebuehr" erschien als "markt_gebue" (Fallback o[:11]).
"""
from bot import discord_embeds as de


def _post(monkeypatch, report):
    sent = {}
    monkeypatch.setattr(de, "_post_embed", lambda e, ch, dry_run=False: sent.setdefault("e", e) or True)
    monkeypatch.setattr(de, "insert_system_log", lambda *a, **k: None, raising=False)
    de.post_signal_worker_embed([], "NORMAL", 1.0, 0, 8800.0, 7600.0, 1150.0, 11,
                                signal_report=report)
    return "\n".join(f["value"] for f in sent["e"]["fields"])


def _row(sym, outcome):
    return {"symbol": sym, "signal_type": "MACD_TURN_BELOW_SMA20,X", "conviction": "MEDIUM",
            "score": 35, "direction": "BUY", "outcome": outcome}


def test_neue_skip_gruende_haben_kurzform(monkeypatch):
    txt = _post(monkeypatch, [_row("HNT", "markt_gebuehr"), _row("NVDA", "signal_max_open"),
                              _row("AAPL", "signal_reserve")])
    assert "Gebühr (Krypto/$2-Markt)" in txt
    assert "Signal-Trades voll" in txt
    assert "Cash-Reserve" in txt
    assert "markt_gebue" not in txt.replace("markt_gebuehr", "")


def test_unbekannter_grund_bleibt_bis_24_zeichen_ganz(monkeypatch):
    txt = _post(monkeypatch, [_row("A", "ein_neuer_grund_xyz"),
                              _row("B", "ein sehr langer Grund, der weit ueber das Limit geht")])
    assert "ein_neuer_grund_xyz" in txt
    assert "ein sehr langer Grund, …" in txt
