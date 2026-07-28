#!/usr/bin/env python3
"""Unit tests — feat/pnl-nachreport: Message-ID-Capture, _edit_embed, P/L-Fixes.

- _post_embed gibt bei Erfolg die Discord-Message-ID zurueck (truthy, damit
  alte `if ok:`-Call-Sites weiterlaufen) und fuellt get_last_post().
- _edit_embed PATCHt das Original ohne 'attachments'-Key (Chart bleibt).
- post_position_closed_embed: pnl_usd=None → grau + "P/L folgt (Nachreport)"
  (vorher: teal "$+0.00 Gewinn"); pnl_pct=0.0 wird angezeigt (vorher durch
  Truthiness-Check verschluckt).
"""
from __future__ import annotations

import io
import json

import pytest

import bot.discord_embeds as DE


class FakeResponse:
    def __init__(self, status: int, body: dict):
        self.status = status
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body


class FakeConnection:
    """Zeichnet Requests auf; liefert vorgegebene Antworten in Reihenfolge."""

    requests: list[dict] = []
    responses: list[FakeResponse] = []

    def __init__(self, host, timeout=None):
        pass

    def request(self, method, path, body=None, headers=None):
        FakeConnection.requests.append(
            {"method": method, "path": path, "body": body, "headers": headers}
        )

    def getresponse(self):
        return FakeConnection.responses.pop(0)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def fake_discord(monkeypatch):
    FakeConnection.requests = []
    FakeConnection.responses = []
    monkeypatch.setattr(DE.http.client, "HTTPSConnection", FakeConnection)
    monkeypatch.setattr(DE, "_read_token", lambda: "test-token")
    monkeypatch.setattr(DE, "insert_system_log", lambda *a, **k: None)
    DE._LAST_POST["message_id"] = None
    DE._LAST_POST["channel_id"] = None
    DE._PENDING_CHART["png"] = None
    yield


def test_post_embed_returns_message_id():
    FakeConnection.responses = [FakeResponse(200, {"id": "111222333"})]
    ok = DE._post_embed({"title": "t"}, "chan1")
    assert ok == "111222333"
    assert bool(ok) is True                      # truthy-Kontrakt bleibt
    last = DE.get_last_post()
    assert last["message_id"] == "111222333"
    assert last["channel_id"] == "chan1"


def test_post_embed_failure_returns_false():
    FakeConnection.responses = [FakeResponse(400, {"message": "bad"})]
    assert DE._post_embed({"title": "t"}, "chan1") is False


def test_post_embed_retries_on_429():
    FakeConnection.responses = [
        FakeResponse(429, {"retry_after": 0.01}),
        FakeResponse(200, {"id": "999"}),
    ]
    assert DE._post_embed({"title": "t"}, "chan1") == "999"
    assert len(FakeConnection.requests) == 2


def test_edit_embed_patches_correct_path_without_attachments():
    FakeConnection.responses = [FakeResponse(200, {"id": "42"})]
    ok = DE._edit_embed("chanX", "msgY", {"title": "neu"})
    assert ok is True
    req = FakeConnection.requests[0]
    assert req["method"] == "PATCH"
    assert req["path"] == "/api/v10/channels/chanX/messages/msgY"
    payload = json.loads(req["body"])
    assert "attachments" not in payload          # Chart-Anhang bleibt erhalten
    assert payload["embeds"][0]["title"] == "neu"


def test_edit_embed_requires_ids():
    assert DE._edit_embed("", "msg", {"title": "x"}) is False
    assert DE._edit_embed("chan", "", {"title": "x"}) is False


# ── P/L-Rendering ─────────────────────────────────────────────────────────────

def _pnl_field(embed: dict) -> str:
    return next(f["value"] for f in embed["fields"] if f["name"] == "📊 PnL")


def test_unknown_pnl_renders_neutral_not_zero_gain():
    embed = DE._build_position_closed_embed(symbol="XYZ", amount_usd=100.0)
    assert embed["color"] == DE.COLOR_GREY
    assert "folgt (Nachreport)" in _pnl_field(embed)
    assert "Gewinn" not in embed["description"]
    assert "$+0.00" not in embed["description"]


def test_flat_close_shows_zero_percent():
    # pnl_pct == 0.0 wurde frueher durch `if pnl_pct` verschluckt
    embed = DE._build_position_closed_embed(
        symbol="XYZ", amount_usd=100.0, pnl_usd=0.0, pnl_pct=0.0)
    assert "(+0.0%)" in _pnl_field(embed)
    assert embed["color"] == DE.COLOR_TEAL


def test_loss_renders_red():
    embed = DE._build_position_closed_embed(
        symbol="XYZ", amount_usd=100.0, pnl_usd=-3.5, pnl_pct=-2.1)
    assert embed["color"] == DE.COLOR_RED
    assert "Verlust" in embed["description"]


def test_partial_close_title_and_keep_chart():
    embed = DE._build_position_closed_embed(
        symbol="XYZ", amount_usd=25.0, pnl_usd=1.2, pnl_pct=5.0,
        close_pct=25.0, keep_chart_image=True)
    assert embed["title"].startswith("✂️ TEILVERKAUF 25%")
    assert embed["image"] == {"url": "attachment://chart.png"}


def test_post_position_closed_embed_returns_message_id():
    FakeConnection.responses = [FakeResponse(200, {"id": "777"})]
    ok = DE.post_position_closed_embed(symbol="ABC", amount_usd=50.0,
                                       pnl_usd=2.0, pnl_pct=4.0)
    assert ok == "777"


# ── Daily Report ──────────────────────────────────────────────────────────────

def test_daily_report_embed_chunks_and_routes_to_reports():
    FakeConnection.responses = [FakeResponse(200, {"id": "1"})]
    lines = [f"SYM{i} $+1.00 (+1.0%)" for i in range(80)]  # > 1024 Zeichen
    ok = DE.post_daily_report_embed(
        report_date="2026-07-28", realized_pnl_usd=12.5, wins=3, losses=1,
        unconfirmed=1, open_count=32, open_exposure_usd=5000.0,
        unrealized_pnl_usd=-42.0,
        sections=[("🏁 Closes (80)", lines), ("🟢 Eröffnungen (0)", [])])
    assert ok == "1"
    req = FakeConnection.requests[0]
    assert "/channels/1513401408643141642/messages" in req["path"]
    payload = json.loads(req["body"])
    embed = payload["embeds"][0]
    close_fields = [f for f in embed["fields"] if f["name"].startswith("🏁")]
    assert len(close_fields) >= 2                # gechunkt
    assert all(len(f["value"]) <= 1024 for f in embed["fields"])
    assert "Win-Rate: 3/4 (75%)" in embed["description"]
    assert "1 ohne bestätigtes P/L" in embed["description"]
