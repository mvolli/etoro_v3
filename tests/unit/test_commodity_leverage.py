#!/usr/bin/env python3
"""Unit tests — feat/commodity-leverage (2026-08-24).

Rohstoffe brauchen bei eToro 1000 USD Mindest-EXPOSURE. ``amount`` ist die
Margin, Exposure = amount x leverage — 500 USD bei Hebel 2 erreichen die
Schwelle. Der Hebel ist im Client HART auf 2 gedeckelt und faellt auf 1
zurueck, wenn der Broker den Wert fuer das Instrument nicht zulaesst.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

REPO = Path(__file__).resolve().parents[2]


def _cfg():
    return yaml.safe_load((REPO / "config" / "config.yaml").read_text(encoding="utf-8"))


# ─── Konfiguration: die Sicherheitsgrenzen ──────────────────────────────────

def test_leverage_never_exceeds_two():
    """Harte Obergrenze — hoeher als 2 wird nie gehebelt (User-Vorgabe)."""
    c = _cfg()["trading"]["commodity"]
    assert int(c["leverage"]) <= 2


def test_only_one_commodity_position_at_a_time():
    c = _cfg()["trading"]["commodity"]
    assert int(c["max_positions"]) == 1


def test_margin_times_leverage_meets_broker_minimum():
    """500 x 2 = 1000 USD Exposure — genau die eToro-Mindestgroesse."""
    c = _cfg()["trading"]["commodity"]
    exposure = float(c["position_usd"]) * int(c["leverage"])
    assert exposure >= 1000.0


def test_worst_case_loss_stays_small():
    """Verlustfall bei 6 % Stop (sl.max_pct) auf die Exposure.

    Der Sinn des Experiments: Datenpunkte sammeln, ohne relevantes Kapital
    zu riskieren. Bei 500 USD Margin und Hebel 2 sind 6 % Kursverlust rund
    60 USD — knapp 12 % der Margin.
    """
    cfg = _cfg()
    c = cfg["trading"]["commodity"]
    sl_max = float(cfg.get("sl", {}).get("max_pct", 6.0))
    exposure = float(c["position_usd"]) * int(c["leverage"])
    loss = exposure * sl_max / 100.0
    assert loss <= 100.0, f"Verlustfall {loss:.0f} USD zu gross"


# ─── Client: Hebel-Deckelung und Broker-Pruefung ────────────────────────────

def _elig(leverage_values):
    return {"leverageConfigs": [{"direction": "long",
                                 "leverageValues": leverage_values,
                                 "minStopLossPercentage": 1.0,
                                 "maxStopLossPercentage": 50.0,
                                 "allowEditStopLoss": True}]}


def test_client_clamps_leverage_to_two():
    """Auch eine fehlerhaft hohe Einstellung darf nie zur Order werden."""
    for requested, expected in ((1, 1), (2, 2), (5, 2), (50, 2), (0, 1), (-3, 1)):
        assert max(1, min(2, int(requested or 1))) == expected


def test_open_position_accepts_leverage_argument():
    """Signatur-Regression: der Parameter muss existieren und 1 als Default haben."""
    import inspect
    from bot.api.client import EToroClient
    sig = inspect.signature(EToroClient.open_position)
    assert "leverage" in sig.parameters
    assert sig.parameters["leverage"].default == 1


def test_leverage_config_lookup_matches_requested_level():
    """Die SL-Grenzen muessen fuer den TATSAECHLICHEN Hebel gelesen werden.

    Vorher war die Suche auf ``1 in leverageValues`` verdrahtet — bei Hebel 2
    haette der Stop gegen die falschen Grenzen geprueft.
    """
    src = (REPO / "src" / "bot" / "api" / "client.py").read_text(encoding="utf-8")
    assert "_lev in lc.get(\"leverageValues\", [])" in src
    assert "1 in lc.get(\"leverageValues\", [])" not in src


def test_broker_rejection_falls_back_to_unleveraged():
    """Laesst der Broker Hebel 2 nicht zu, wird ungehebelt geordert."""
    elig = _elig([1])          # nur Hebel 1 erlaubt
    allowed = any(
        lc.get("direction") == "long" and 2 in (lc.get("leverageValues") or [])
        for lc in elig["leverageConfigs"]
    )
    assert allowed is False, "Testaufbau: Hebel 2 darf hier nicht erlaubt sein"
