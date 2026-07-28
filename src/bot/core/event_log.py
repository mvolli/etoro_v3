"""Zentrales Trade-Event-Logging (feat/pnl-nachreport, 2026-07-28).

Ein Aufruf pro geposteter Open/Partial/Close-Notification: persistiert das
Event in trade_events und haengt die Discord-Message-Koordinaten aus
de.get_last_post() an, damit der Reconciler das Embed spaeter mit dem
finalen PnL editieren kann.

WICHTIG: `de` muss dieselbe discord_embeds-Modulinstanz sein, ueber die
gepostet wurde (trailing_stop laedt das Modul per importlib als eigene
Instanz — get_last_post() einer anderen Instanz waere leer).

Fail-open: darf nie einen Live-Trade-Pfad brechen.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def record_posted_event(
    db: Any,
    de: Any,
    *,
    symbol: str,
    event_type: str,
    source: str,
    post_result: Any = None,
    **fields: Any,
) -> int | None:
    """Event persistieren; bei erfolgreichem Post (post_result truthy)
    zusaetzlich channel_id/message_id aus de.get_last_post() speichern.

    Returns event id oder None (fail-open).
    """
    try:
        from bot.db.repo import TradeEventRepo

        repo = TradeEventRepo(db)
        eid = repo.record(symbol=symbol, event_type=event_type,
                          source=source, **fields)
        if eid and post_result and de is not None and hasattr(de, "get_last_post"):
            lp = de.get_last_post()
            if lp.get("message_id"):
                repo.set_discord_message(eid, lp.get("channel_id") or "",
                                         lp["message_id"])
        return eid
    except Exception as exc:
        logger.debug("record_posted_event fehlgeschlagen: %s", exc)
        return None
