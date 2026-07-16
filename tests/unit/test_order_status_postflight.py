"""fix/ghost-defer-idempotent (2026-07-16): Post-flight-Status-Auswertung.

Testet die reine Entscheidungslogik resolve_deferred_action sowie
get_order_status-Klassifikation (Transportfehler vs. echter Order-Status)
mit einem Fake-Client — KEINE Live-DB, KEINE Netzwerk-Calls.
"""
import types

from bot.api.client import APIError, EToroClient
from bot.workers.execution_worker import DEFER_CAP, resolve_deferred_action


# ── get_order_status via Fake-Client ─────────────────────────────────────────

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _api_error(status_code):
    exc = APIError.__new__(APIError)
    exc.status_code = status_code
    return exc


def _get_status(payload=None, exc=None):
    fake = types.SimpleNamespace()
    fake.config = types.SimpleNamespace(base_url="https://api.example.invalid/")

    def _get_raw(url):
        if exc is not None:
            raise exc
        return _FakeResp(payload)

    fake._get_raw = _get_raw
    return EToroClient.get_order_status(fake, 42)


def test_executed_with_positions():
    r = _get_status({"statusID": "Executed", "positions": [{"positionID": 7}],
                     "instrumentID": 22})
    assert r["status"] == "executed"
    assert r["positions"] == [{"positionID": 7}]
    assert r["transport_error"] is False


def test_rejected_carries_reason():
    r = _get_status({"statusID": "Rejected", "rejectionReason": "Insufficient funds"})
    assert r["status"] == "rejected"
    assert r["rejection_reason"] == "Insufficient funds"


def test_unknown_status_id_defaults_to_pending():
    # fix/order-status-numeric: unbekannte statusID OHNE Position -> pending
    # (gecappter Defer loest idempotent auf); 'failed' erzeugte Orphans (#442).
    r = _get_status({"statusID": "TotallyNewStatus"})
    assert r["status"] == "pending"
    assert r["transport_error"] is False


def test_numeric_status_3_executed_with_positions():
    # Live verifiziert 2026-07-16: Order 1531989003 (VIS.MC)
    r = _get_status({"statusID": 3, "positions": [{"positionID": 3516306523,
                                                   "rate": 55.2}],
                     "instrumentID": 5544, "errorCode": 0})
    assert r["status"] == "executed"


def test_numeric_status_4_failed_without_positions():
    # Live verifiziert 2026-07-16: NATGAS/TEK.L (Ghost-Pattern)
    r = _get_status({"statusID": 4, "positions": [], "errorCode": 0})
    assert r["status"] == "failed"


def test_numeric_status_1_pending():
    r = _get_status({"statusID": 1, "positions": []})
    assert r["status"] == "pending"


def test_positions_override_any_status_id():
    # positions[] gefuellt schlaegt JEDE statusID-Interpretation
    r = _get_status({"statusID": 999, "positions": [{"positionID": 1}]})
    assert r["status"] == "executed"
    r2 = _get_status({"statusID": 4, "positions": [{"positionID": 2}]})
    assert r2["status"] == "executed"


def test_404_is_timing_issue_pending():
    r = _get_status(exc=_api_error(404))
    assert r["status"] == "pending"
    assert r["is_timing_issue"] is True


def test_http_5xx_is_transport_error_not_failed():
    r = _get_status(exc=_api_error(503))
    assert r["status"] == "unknown"
    assert r["transport_error"] is True


def test_network_error_is_transport_error():
    r = _get_status(exc=RuntimeError("connection reset"))
    assert r["status"] == "unknown"
    assert r["transport_error"] is True


# ── resolve_deferred_action Matrix ───────────────────────────────────────────

def test_action_executed_with_position_is_active():
    pf = {"status": "executed", "positions": [{"positionID": 1}]}
    assert resolve_deferred_action(pf, 1) == "ACTIVE"


def test_action_executed_without_position_defers_then_ghosts():
    pf = {"status": "executed", "positions": None}
    assert resolve_deferred_action(pf, 1) == "DEFER"
    assert resolve_deferred_action(pf, DEFER_CAP) == "GHOST_FAILED"


def test_action_pending_defers_then_ghosts():
    pf = {"status": "pending", "is_timing_issue": False}
    assert resolve_deferred_action(pf, 0) == "DEFER"
    assert resolve_deferred_action(pf, DEFER_CAP) == "GHOST_FAILED"


def test_action_404_means_repost():
    pf = {"status": "pending", "is_timing_issue": True}
    # 404 = eToro kennt die Order nicht -> neuer POST ist sicher, kein Defer
    assert resolve_deferred_action(pf, 1) == "REPOST"
    assert resolve_deferred_action(pf, DEFER_CAP + 5) == "REPOST"


def test_action_rejected_and_failed():
    assert resolve_deferred_action({"status": "rejected"}, 1) == "FAILED_REJECTED"
    assert resolve_deferred_action({"status": "failed"}, 1) == "FAILED"


def test_action_transport_error_never_marks_failed():
    pf = {"status": "unknown", "transport_error": True}
    assert resolve_deferred_action(pf, 1) == "DEFER"
    assert resolve_deferred_action(pf, DEFER_CAP) == "GHOST_FAILED"
    # Belt & braces: selbst ein "failed" MIT transport_error darf nie FAILED werden
    pf2 = {"status": "failed", "transport_error": True}
    assert resolve_deferred_action(pf2, 1) == "DEFER"


# ── fix/order-error-learning (2026-07-16) ────────────────────────────────────

from bot.workers.execution_worker import (
    is_internal_only_error,
    parse_min_position_amount,
)


def test_error_message_becomes_rejection_reason():
    # Live-Befund: statusID=4 traegt errorCode/errorMessage, rejectionReason leer
    r = _get_status({
        "statusID": 4, "errorCode": 720,
        "errorMessage": "Error opening position - ... MinimumPositionAmount: 1000 (Dollars)",
    })
    assert r["status"] == "rejected"
    assert r["rejection_reason"].startswith("eToro 720:")
    assert r["error_code"] == 720


def test_parse_min_position_amount():
    msg = ("eToro 720: Error opening position - Initial Leveraged Position "
           "Amount is under the minimum defined Leveraged Amount in the system. "
           "leveraged InitialPositionAmount: 50.00 MinimumPositionAmount: 1000 (Dollars)")
    assert parse_min_position_amount(msg) == 1000.0
    assert parse_min_position_amount("kein Treffer") is None
    assert parse_min_position_amount(None) is None


def test_internal_only_detection():
    assert is_internal_only_error("eToro 814: instrument is visible internal only")
    assert not is_internal_only_error("eToro 604: insufficient funds for the order")
    assert not is_internal_only_error(None)
