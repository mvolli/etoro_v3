#!/usr/bin/env python3
"""Validate that SL rates are meaningful (not eToro placeholder 0.01).

Called by risk_worker after position fetch.
"""


def is_sl_meaningful(
    stop_loss_rate: float,
    open_rate: float,
    is_no_stop_loss: bool,
    max_sl_distance_pct: float = 50.0,
) -> bool:
    """Return True if the stop-loss is a real, meaningful value.

    eToro sometimes stores SL=$0.01 for crypto (or other near-zero values)
    which acts as no stop-loss in practice.

    Args:
        stop_loss_rate: The broker-side SL rate
        open_rate: The position entry rate
        is_no_stop_loss: The isNoStopLoss flag from API
        max_sl_distance_pct: Max allowed distance from entry (default 50%)
    """
    if is_no_stop_loss:
        return False
    if stop_loss_rate <= 0:
        return False
    if open_rate <= 0:
        return True  # Can't calculate, assume ok
    distance_pct = abs(open_rate - stop_loss_rate) / open_rate * 100
    return distance_pct <= max_sl_distance_pct


def has_effective_sl(pos: dict) -> bool:
    """Check if a position dict (from eToro API) has an effective stop-loss."""
    return is_sl_meaningful(
        stop_loss_rate=float(pos.get("stopLossRate", 0) or 0),
        open_rate=float(pos.get("openRate", 0) or 0),
        is_no_stop_loss=bool(pos.get("isNoStopLoss", True)),
    )
