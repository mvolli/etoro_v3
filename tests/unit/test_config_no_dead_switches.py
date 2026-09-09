#!/usr/bin/env python3
"""Kein toter Config-Schalter (2026-09-09).

ANLASS: `kelly_asset_class_split: true` stand zwei Commits lang in
config.yaml und hat NICHTS getan — `_get_sizing_cfg()` baut ein explizites
Dict und der Schluessel fehlte darin. Aufgefallen ist es nur zufaellig, weil
der Vol-Guard eine Zahl meldete, die nicht zur Rechnung passte.

Ein Schalter, der aussieht als steuere er etwas, es aber nicht tut, ist
schlimmer als gar keiner: man dreht daran und glaubt, es sei passiert.

Dieser Test verlangt fuer JEDES Feld einer *Config-Dataclass entweder
  (a) eine Referenz im Code ausserhalb von config.py, oder
  (b) einen Eintrag in NUR_DOKUMENTATION mit Begruendung.

Damit wird jede Attrappe zu einer bewussten Entscheidung.
"""
from __future__ import annotations

import pathlib
import re
import sys
from dataclasses import fields, is_dataclass

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

import bot.config as C

ROOT = pathlib.Path(__file__).resolve().parents[2]

# Felder, die BEWUSST nichts steuern. Jeder Eintrag braucht einen Grund —
# wer hier etwas eintraegt, sagt damit: "Attrappe, absichtlich."
NUR_DOKUMENTATION: dict[tuple[str, str], str] = {
    ("DiscordConfig", "main_channel"):
        "Channel-IDs stehen hartkodiert in discord_embeds.py:62 — hier nur "
        "als Doku, welche Kanaele der Bot bedient. Aendern wirkt NICHT.",
    ("DiscordConfig", "trades_channel"):
        "wie main_channel — discord_embeds.py:63.",
    ("Config", "discord"):
        "Container fuer DiscordConfig; wird als Objekt gehalten, nicht als Feld gelesen.",
    ("Config", "discord_token"):
        "Token kommt aus der Umgebung (.env), nie aus config.yaml — "
        "Klartext-Secrets in Dateien sind per Sicherheitsregel verboten.",
    ("DBConfig", "wal_mode"):
        "WAL wird in db/connection.py UNBEDINGT gesetzt (live verifiziert: "
        "PRAGMA journal_mode = wal). Der Schalter kann es nicht abstellen.",
    ("CacheConfig", "instrument_map_ttl_hours"):
        "instrument_map.json wird ohne TTL-Pruefung geschrieben; der Wert "
        "steuert nichts.",
    ("MarketHoursConfig", "timezone"):
        "market_hours rechnet je Boerse mit eigener zoneinfo; der Schluessel "
        "ist in config.yaml auskommentiert.",
}


def _quelltext() -> str:
    teile = []
    for unter in ("src", "scripts", "backtest"):
        d = ROOT / unter
        if not d.exists():
            continue
        for p in d.rglob("*.py"):
            if ".bak" in p.name or p.name == "config.py":
                continue
            teile.append(p.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(teile)


def _wird_gelesen(feld: str, text: str) -> bool:
    return bool(
        re.search(r"\." + re.escape(feld) + r"\b", text)
        or re.search(r"['\"]" + re.escape(feld) + r"['\"]", text)
    )


def _config_klassen():
    for name in dir(C):
        cls = getattr(C, name)
        if is_dataclass(cls) and name.endswith("Config"):
            yield name, cls


def test_kein_unbemerkt_toter_schalter():
    text = _quelltext()
    tot = []
    for name, cls in _config_klassen():
        for f in fields(cls):
            if (name, f.name) in NUR_DOKUMENTATION:
                continue
            if not _wird_gelesen(f.name, text):
                tot.append(f"{name}.{f.name}")
    assert not tot, (
        "Diese Config-Felder werden nirgends gelesen — der Wert aus "
        "config.yaml wirkt NICHT:\n  " + "\n  ".join(sorted(tot)) +
        "\n\nEntweder verdrahten, entfernen, oder mit Begruendung in "
        "NUR_DOKUMENTATION eintragen."
    )


def test_ausnahmeliste_bleibt_ehrlich():
    """Wird eine Attrappe doch verdrahtet, muss sie aus der Liste raus."""
    text = _quelltext()
    ueberholt = [f"{k[0]}.{k[1]}" for k in NUR_DOKUMENTATION
                 if _wird_gelesen(k[1], text)]
    assert not ueberholt, (
        "Diese Felder stehen als 'Attrappe' in NUR_DOKUMENTATION, werden aber "
        "inzwischen gelesen — Eintrag entfernen:\n  " + "\n  ".join(ueberholt)
    )


def test_jede_ausnahme_hat_eine_begruendung():
    for schluessel, grund in NUR_DOKUMENTATION.items():
        assert grund and len(grund) > 20, f"{schluessel}: Begruendung fehlt"


@pytest.mark.parametrize("klasse,feld", [
    ("SizingConfig", "kelly_asset_class_split"),
    ("SizingConfig", "kelly_target_mean"),
    ("SizingConfig", "kelly_drift_band_pct"),
])
def test_die_drei_aus_dem_anlassfall_sind_verdrahtet(klasse, feld):
    """Regressionsschutz fuer genau die Schalter, die tot waren."""
    import bot.core.sizing as _sz
    assert feld in _sz._get_sizing_cfg(), f"{klasse}.{feld} erreicht den Code nicht"
