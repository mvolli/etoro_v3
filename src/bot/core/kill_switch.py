#!/usr/bin/env python3
"""Kill Switch — Trading Bible V5.

Creating the file data/kill_switch.flag immediately forces CRITICAL regime.
Removing the file allows normal regime detection to resume on next Risk Worker run.

fix/kill-switch-daily-auto-reset: Die Flag-Datei traegt jetzt strukturierten
JSON-State {reason, tripped_at, scope}. Ein DAILY-Loss-Trip (Intraday-Limit)
cleared automatisch am naechsten UTC-Handelstag — vorher blockierte EIN
schlechter Tag alle BUYs unbegrenzt, bis ein Mensch `rm` ausfuehrte.
Weekly/Monthly/Manual-Trips bleiben bewusst manuell (harte Breaches ->
menschliche Pruefung). Alte Plaintext-Flags bleiben lesbar und werden als
scope='manual' behandelt (nie auto-clear).

Usage:
    # Activate (manual):
    echo 'Emergency stop — manual' > /path/to/etoro_v3/data/kill_switch.flag

    # Deactivate:
    rm /path/to/etoro_v3/data/kill_switch.flag

    # Programmatic:
    from bot.core.kill_switch import activate, deactivate, is_kill_switch_active
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

# Persistent path — survives WSL reboot (unlike /tmp which can be cleared)
_KILL_SWITCH_FILE = Path(__file__).resolve().parent.parent.parent.parent / "data" / "kill_switch.flag"

# Scopes: only 'daily' ever auto-clears (on the next UTC day).
VALID_SCOPES = ("daily", "weekly", "monthly", "manual")

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_kill_switch_active() -> bool:
    """Return True when the kill switch file exists."""
    return _KILL_SWITCH_FILE.exists()


def get_state() -> dict:
    """Return structured kill-switch state.

    {'active': bool, 'reason': str, 'scope': str, 'tripped_at': str|None}

    Plaintext flags (legacy or hand-written via `echo`) parse as
    scope='manual' with tripped_at=None — they never auto-clear.
    """
    if not _KILL_SWITCH_FILE.exists():
        return {"active": False, "reason": "", "scope": "manual", "tripped_at": None}
    try:
        raw = _KILL_SWITCH_FILE.read_text().strip()
    except Exception:
        return {"active": True, "reason": "Manual kill switch", "scope": "manual", "tripped_at": None}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not a dict")
        scope = data.get("scope")
        if scope not in VALID_SCOPES:
            scope = "manual"
        return {
            "active": True,
            "reason": str(data.get("reason") or "Kill switch"),
            "scope": scope,
            "tripped_at": data.get("tripped_at"),
        }
    except Exception:
        # Legacy plaintext flag — reason is the raw file content.
        return {"active": True, "reason": raw or "Manual kill switch", "scope": "manual", "tripped_at": None}


def get_reason() -> str:
    """Return the reason text of the active kill switch (or empty string)."""
    state = get_state()
    return state["reason"] if state["active"] else ""


def activate(reason: str = "Manual kill switch", scope: str = "manual") -> None:
    """Create the kill switch file with structured state.

    scope='daily' auto-clears on the next UTC day (see auto_clear_if_new_day);
    all other scopes require manual removal after human review.
    """
    if scope not in VALID_SCOPES:
        scope = "manual"
    _KILL_SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    _KILL_SWITCH_FILE.write_text(json.dumps({
        "reason": reason,
        "scope": scope,
        "tripped_at": _utcnow().strftime(_TS_FORMAT),
    }))


def deactivate() -> None:
    """Remove the kill switch file to resume normal operation."""
    # missing_ok=True avoids a TOCTOU race: a concurrent deactivation between
    # an exists() check and unlink() would otherwise raise FileNotFoundError.
    _KILL_SWITCH_FILE.unlink(missing_ok=True)


def auto_clear_if_new_day(now: datetime | None = None) -> tuple[bool, str]:
    """Clear a DAILY-scope kill switch once a new UTC day has begun.

    Returns (cleared, detail). Only scope='daily' flags with a parseable
    tripped_at from a PREVIOUS UTC date are cleared — weekly/monthly/manual
    and same-day daily trips stay active. Fail-safe: any parse problem
    leaves the flag in place.
    """
    if now is None:
        now = _utcnow()
    state = get_state()
    if not state["active"] or state["scope"] != "daily":
        return False, ""
    tripped_at = state.get("tripped_at")
    if not tripped_at:
        return False, ""
    try:
        tripped = datetime.strptime(str(tripped_at).strip(), _TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return False, ""
    if now.date() <= tripped.date():
        return False, ""
    deactivate()
    detail = (
        f"Daily-Loss Kill-Switch auto-cleared: getriggert {tripped_at} UTC "
        f"({state['reason'][:150]}), neuer UTC-Handelstag {now.date().isoformat()}"
    )
    return True, detail


# ── Backward compatibility alias (risk_worker imports KILL_SWITCH_FILE) ────────
KILL_SWITCH_FILE = _KILL_SWITCH_FILE
