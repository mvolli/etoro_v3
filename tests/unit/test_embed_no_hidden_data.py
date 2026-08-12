"""Unit tests fuer fix/embeds-no-hidden-data (2026-08-12).

Zwei Fehlerklassen, die BEIDE Daten verschwinden liessen:

1. sichtbar:   `… +21 weitere` / `_+N weitere..._` in den Embed-Werten
2. unsichtbar: `fields[:25]` in _clip_embed_limits — Discord antwortet danach
               brav mit 200, die Felder sind trotzdem weg

Diese Datei testet die Infrastruktur (pack_lines_into_fields, _split_embed).
Kein Test darf posten — geprueft wird ausschliesslich der gebaute Payload.
"""
from __future__ import annotations

import pytest

from bot import discord_embeds as de


# ── pack_lines_into_fields ────────────────────────────────────────────────────

def test_leere_liste_ergibt_keine_felder():
    assert de.pack_lines_into_fields("X", []) == []


def test_kurze_liste_bleibt_ein_feld():
    fields = de.pack_lines_into_fields("📋 Positionen", ["a", "b", "c"])
    assert len(fields) == 1
    assert fields[0]["name"] == "📋 Positionen"
    assert fields[0]["value"] == "a\nb\nc"


def test_ueberlauf_erzeugt_folgefeld_statt_zu_kuerzen():
    lines = [f"Zeile {i:03d} mit etwas Text" for i in range(200)]
    fields = de.pack_lines_into_fields("📋 Positionen", lines)
    assert len(fields) > 1
    # Der Kern: JEDE Zeile ist irgendwo enthalten
    joined = "\n".join(f["value"] for f in fields)
    for line in lines:
        assert line in joined


def test_folgefelder_sind_als_fortsetzung_erkennbar():
    lines = [f"x{i:03d}" * 10 for i in range(100)]
    fields = de.pack_lines_into_fields("📋 Positionen", lines)
    assert fields[0]["name"] == "📋 Positionen"
    assert all(f["name"].endswith("…") for f in fields[1:])


def test_kein_feld_verletzt_das_1024_limit():
    lines = [f"Zeile {i} — etwas laengerer Text zum Fuellen" for i in range(300)]
    for f in de.pack_lines_into_fields("X", lines):
        assert len(f["value"]) <= de.MAX_FIELD_VALUE


def test_uebergrosse_einzelzeile_wird_als_einzige_ausnahme_geschnitten():
    """Eine Zeile > 1024 kann nicht aufgeteilt werden — das ist ein
    Datenfehler weiter oben und wird geloggt, nicht still geschluckt."""
    fields = de.pack_lines_into_fields("X", ["A" * 3000])
    assert len(fields) == 1
    assert len(fields[0]["value"]) <= de.MAX_FIELD_VALUE
    assert fields[0]["value"].endswith("…")


def test_inline_flag_wird_durchgereicht():
    fields = de.pack_lines_into_fields("X", ["a"], inline=True)
    assert fields[0]["inline"] is True


# ── _split_embed ──────────────────────────────────────────────────────────────

def _embed(n_fields: int, value: str = "kurz") -> dict:
    return {
        "title": "Test",
        "description": "desc",
        "color": 123,
        "fields": [{"name": f"F{i}", "value": value, "inline": False}
                   for i in range(n_fields)],
    }


def test_kleines_embed_bleibt_eines():
    out = de._split_embed(_embed(5))
    assert len(out) == 1
    assert len(out[0]["fields"]) == 5


def test_mehr_als_25_felder_werden_verteilt_nicht_verworfen():
    """Der unsichtbare Datenverlust: frueher fields[:25], Rest weg."""
    out = de._split_embed(_embed(60))
    assert len(out) == 3
    assert sum(len(e["fields"]) for e in out) == 60
    assert all(len(e["fields"]) <= de.MAX_FIELDS_PER_EMBED for e in out)


def test_gesamtlaengenlimit_erzwingt_ebenfalls_eine_teilung():
    out = de._split_embed(_embed(20, value="Y" * 900))
    assert len(out) > 1
    assert sum(len(e["fields"]) for e in out) == 20
    for e in out:
        assert de._embed_char_total(e) <= de.MAX_EMBED_TOTAL


def test_erstes_embed_behaelt_titel_und_beschreibung():
    out = de._split_embed(_embed(60))
    assert out[0]["description"] == "desc"
    assert out[0]["title"].startswith("Test")
    # Folge-Embeds wiederholen die Beschreibung nicht
    assert "description" not in out[1]


def test_seitenzaehler_im_titel():
    out = de._split_embed(_embed(60))
    assert "(1/3)" in out[0]["title"]
    assert "(2/3)" in out[1]["title"]


def test_farbe_wird_vererbt():
    for e in de._split_embed(_embed(60)):
        assert e["color"] == 123


def test_absurde_menge_wird_laut_geloggt(caplog):
    """Auch die 10-Embed-Grenze darf nicht STILL verschlucken."""
    with caplog.at_level("ERROR"):
        out = de._split_embed(_embed(400))
    assert len(out) == de.MAX_EMBEDS_PER_MESSAGE
    assert any("verloren" in r.message or "verloren" in str(r) for r in caplog.records)


def test_clip_kuerzt_keine_felder_mehr():
    """_clip_embed_limits darf die Anzahl nicht mehr anfassen."""
    e = de._clip_embed_limits(_embed(60))
    assert len(e["fields"]) == 60


def test_clip_erzwingt_weiterhin_zeichenlimits():
    e = de._clip_embed_limits({
        "title": "T" * 500, "description": "D" * 9000,
        "fields": [{"name": "N" * 500, "value": "V" * 5000}],
    })
    assert len(e["title"]) == de.MAX_TITLE
    assert len(e["description"]) == de.MAX_DESCRIPTION
    assert len(e["fields"][0]["name"]) == de.MAX_FIELD_NAME
    assert len(e["fields"][0]["value"]) == de.MAX_FIELD_VALUE


# ── _post_embed: Payload statt Netzwerk ───────────────────────────────────────

def test_post_embed_sendet_alle_embeds(monkeypatch):
    """Regressionsschutz: der Payload muss die Folge-Embeds enthalten."""
    import json
    captured = {}

    def fake_request(method, path, payload, content_type):
        captured["body"] = json.loads(payload.decode("utf-8"))
        return 200, '{"id": "999"}'

    monkeypatch.setattr(de, "_request_discord", fake_request)
    de._post_embed(_embed(60), "chan123")
    assert len(captured["body"]["embeds"]) == 3
    assert sum(len(e["fields"]) for e in captured["body"]["embeds"]) == 60


def test_post_embed_dry_run_sendet_nichts(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("dry_run darf nie senden")
    monkeypatch.setattr(de, "_request_discord", boom)
    assert de._post_embed(_embed(60), "chan123", dry_run=True) is True
