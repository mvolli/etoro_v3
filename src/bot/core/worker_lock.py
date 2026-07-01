#!/usr/bin/env python3
"""Simple non-blocking file lock to prevent overlapping cron invocations
of the same worker. If the lock is already held, the caller should skip
this run entirely (not wait) — the next cron tick will retry.

Usage:
    from bot.core.worker_lock import worker_lock

    def main() -> None:
        with worker_lock("risk_worker") as acquired:
            if not acquired:
                logger.warning("RiskWorker: previous run still active — skipping this cycle")
                print("RiskWorker: SKIPPED (already running)")
                return
            # ... rest of worker logic ...
"""
from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path

LOCK_DIR = Path(__file__).resolve().parent.parent.parent.parent / "locks"


@contextmanager
def worker_lock(worker_name: str):
    """Non-blocking file lock per worker name.

    Yields True if the lock was acquired, False if another instance is
    already running. The caller should skip this cycle on False.
    """
    LOCK_DIR.mkdir(exist_ok=True)
    lock_path = LOCK_DIR / f"{worker_name}.lock"
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield True
    except BlockingIOError:
        yield False
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        except Exception:
            pass
        fh.close()
