#!/usr/bin/env python3
"""Kill Switch — Trading Bible V5.

Creating the file data/kill_switch.flag immediately forces CRITICAL regime.
Removing the file allows normal regime detection to resume on next Risk Worker run.

Usage:
    # Activate:
    echo 'Emergency stop — manual' > /path/to/etoro_v3/data/kill_switch.flag

    # Deactivate:
    rm /path/to/etoro_v3/data/kill_switch.flag

    # Programmatic:
    from bot.core.kill_switch import activate, deactivate, is_kill_switch_active
"""
from pathlib import Path

# Persistent path — survives WSL reboot (unlike /tmp which can be cleared)
_KILL_SWITCH_FILE = Path(__file__).resolve().parent.parent.parent.parent / "data" / "kill_switch.flag"


def is_kill_switch_active() -> bool:
    """Return True when the kill switch file exists."""
    return _KILL_SWITCH_FILE.exists()


def get_reason() -> str:
    """Return the reason text written in the kill switch file (or empty string)."""
    if _KILL_SWITCH_FILE.exists():
        try:
            return _KILL_SWITCH_FILE.read_text().strip()
        except Exception:
            return 'Manual kill switch'
    return ''


def activate(reason: str = 'Manual kill switch') -> None:
    """Create the kill switch file with the given reason."""
    _KILL_SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    _KILL_SWITCH_FILE.write_text(reason)


def deactivate() -> None:
    """Remove the kill switch file to resume normal operation."""
    if _KILL_SWITCH_FILE.exists():
        _KILL_SWITCH_FILE.unlink()


# ── Backward compatibility alias (risk_worker imports KILL_SWITCH_FILE) ────────
KILL_SWITCH_FILE = _KILL_SWITCH_FILE
