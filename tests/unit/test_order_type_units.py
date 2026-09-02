"""fix/order-type-units (2026-09-02): BY_AMOUNT (17) vs BY_UNITS (18).

eToro klassifiziert die Order nach dem SIZE-FELD: "amount" -> BY_AMOUNT
(internal 17), "units" -> BY_UNITS (internal 18). Real-Share-Instrumente
mit allowedOrderQuantityType="unitsOnly" (live: MAD.ASX / IFT.ASX) lehnen
17 mit "eToro 2021 ... ORDER_FOR_EXECUTION_BY_UNITS (18)" ab.

Diese Tests pinnen das Body-Verhalten von EToroClient.open_position:
  1. unitsOnly  -> Body hat "units" (Whole), KEIN stopLossRate/takeProfitRate
  2. unitsOnly mit < 1 share -> blockiert (UnitsOnlyMinShare)
  3. 2021-Rejection auf Fail-Open-Pfad (eligibility down) -> Retry as BY_UNITS
  4. Normal (all) -> Body hat "amount" + stopLossRate wie bisher
"""
from __future__ import annotations

import pytest

from bot.api.client import APIError, ClientConfig, EToroClient


class _CaptureClient:
    """EToroClient-Doppel: deterministischer Eligibility-/Price-/Order-Pfad.

    EToroClient.open_position wird als UNBOUND-Methode auf dieser Instanz
    aufgerufen — der Client selbst liefert nur die Abhaengigkeiten.
    """

    def __init__(self, eligibility: dict | None, price: float,
                 order_result: object = None,
                 order_error: Exception | None = None):
        self._elig = eligibility
        self._price = price
        self._order_result = (
            {"success": True, "orderId": 123} if order_result is None else order_result
        )
        self._order_error = order_error
        self.order_bodies: list[dict] = []
        self.config = ClientConfig()

    def verify_instrument_identity(self, instrument_id, expected_symbol):
        return True, f"Identity OK: instrument_id={instrument_id} == {expected_symbol}"

    def post(self, endpoint, body, v2=False):
        if "eligibility" in endpoint:
            iid = (body.get("instrumentIds") or [None])[0]
            return {
                "eligibilities": [
                    {"instrumentId": iid, **(self._elig or {})}
                ]
            }
        if "execution/orders" in endpoint:
            self.order_bodies.append(body)
            if self._order_error is not None:
                raise self._order_error
            return self._order_result
        return {}

    def get(self, endpoint, params=None, timeout=None):
        if "rates" in endpoint:
            return {"rates": [{"lastExecution": self._price}]}
        return {}


def _open(client: _CaptureClient, instrument_id: int, amount_usd: float,
          symbol: str) -> dict:
    # unbound call: self = unser Capture-Client
    return EToroClient.open_position(client, instrument_id, amount_usd,
                                     symbol=symbol, is_crypto=False)


_UNITS_ONLY_ELIG = {
    "allowOpenPosition": True, "allowEntryOrders": False,
    "allowedOrderQuantityType": "unitsOnly",
    "unitsQuantityType": "whole",
    "leverageConfigs": [{"direction": "long", "leverageValues": [1],
                         "allowEditStopLoss": False}],
}


def test_units_only_sends_units_not_amount():
    client = _CaptureClient(_UNITS_ONLY_ELIG, price=6.19)
    res = _open(client, 1000989, 112.31, "MAD.ASX")
    assert res.get("success") is not False
    assert len(client.order_bodies) == 1
    body = client.order_bodies[0]
    assert body["units"] == 18.0            # floor(112.31 / 6.19) = 18 Whole-Shares
    assert "amount" not in body             # NICHT BY_AMOUNT (17)
    assert "stopLossRate" not in body       # broker SL/TP nicht editierbar
    assert "takeProfitRate" not in body
    assert body["isNoStopLoss"] is True
    assert body["leverage"] == 1


def test_units_only_sub_one_share_blocked():
    client = _CaptureClient(_UNITS_ONLY_ELIG, price=250.0)
    res = _open(client, 1, 100.0, "EXP.DE")
    assert res.get("success") is False
    assert "UnitsOnlyMinShare" in res.get("error", "")
    assert client.order_bodies == []        # KEIN Order-POST


def test_2021_fallback_retries_as_by_units():
    # Fail-Open: eligibility komplett down (None) -> _units_only=False.
    # Erster POST: BY_AMOUNT, wird mit 2021/400 abgelehnt -> Retry as BY_UNITS.
    err = APIError(
        message=(
            "HTTP 400 from /trading/execution/orders: {'code': 2021, "
            "'error': 'Order type validation failure, requested order type: 17, "
            "allowed OrderType ORDER_FOR_EXECUTION_BY_UNITS (18)'}"
        ),
        status_code=400,
        endpoint="/trading/execution/orders",
    )
    client = _CaptureClient(None, price=6.19)
    first = {"done": False}

    def once_post(endpoint, body, v2=False):
        if "execution/orders" in endpoint:
            client.order_bodies.append(body)
            if not first["done"]:
                first["done"] = True
                raise err                     # 1. POST (BY_AMOUNT) rejected
            return {"success": True, "orderId": 999}   # 2. POST (BY_UNITS) ok
        return {}

    client.post = once_post
    res = _open(client, 1000989, 112.31, "MAD.ASX")
    assert res.get("success") is True
    assert len(client.order_bodies) == 2
    # 1. Versuch: BY_AMOUNT (default) ...
    assert client.order_bodies[0]["amount"] == 112.31
    assert "units" not in client.order_bodies[0]
    # ... 2. Versuch: BY_UNITS (18) mit Whole-Shares:
    assert client.order_bodies[1]["units"] == 18.0
    assert "amount" not in client.order_bodies[1]
    assert client.order_bodies[1]["isNoStopLoss"] is True


def test_normal_instrument_keeps_amount_sizing():
    elig = {
        "allowOpenPosition": True, "allowEntryOrders": True,
        "allowedOrderQuantityType": "all",
        "unitsQuantityType": "fractional",
        "leverageConfigs": [{"direction": "long", "leverageValues": [1, 2, 3],
                             "allowEditStopLoss": True,
                             "minStopLossPercentage": 0.5,
                             "maxStopLossPercentage": 50.0}],
    }
    client = _CaptureClient(elig, price=100.0)
    res = _open(client, 13684, 1000.0, "AAPL")
    assert res.get("success") is not False
    body = client.order_bodies[0]
    assert body["amount"] == 1000.0
    assert "units" not in body
    assert "stopLossRate" in body
    assert body["isNoStopLoss"] is False
