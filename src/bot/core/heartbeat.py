#!/usr/bin/env python3
"""Worker heartbeat — dead-man's switch support (fix/autonomy-hardening).

Every worker records a LAST_RUN_<NAME> timestamp in system_state at the
start of each successful (lock-acquired) cycle. The monitor worker calls
get_stale_workers() and raises a CRITICAL Discord alert when any worker
has been silent for longer than STALE_FACTOR × its expected interval.

Pure logic + thin state_repo wrapper — unit-testable without DB.
"""
from __future__ import annotations

from datetime import datetime, timezone

# Expected cron interval per worker (minutes). Keep in sync with crontab.txt.
EXPECTED_INTERVALS_MIN: dict[str, int] = {
    "data_worker": 5,
    "risk_worker": 5,
    "reconciler": 5,
    "signal_worker": 15,
    "execution_worker": 15,
}

# A worker is considered stale after this many missed intervals.
STALE_FACTOR = 3

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def record_heartbeat(state_repo, worker_name: str) -> None:
    """Persist LAST_RUN_<NAME> = now (UTC). Never raises."""
    try:
        state_repo.set(
            f"LAST_RUN_{worker_name.upper()}",
            _utcnow().strftime(_TS_FORMAT),
        )
    except Exception:
        # Heartbeat must never break a worker cycle.
        pass


def is_stale(last_run: str | None, interval_min: int, now: datetime | None = None) -> bool:
    """Pure check: True when last_run is missing/unparseable or older than
    STALE_FACTOR × interval_min.

    A missing heartbeat is treated as stale so a worker that has *never*
    run (broken cron entry) is detected too — but see get_stale_workers()
    for the grace handling on first deploy.
    """
    if now is None:
        now = _utcnow()
    if not last_run:
        return True
    try:
        ts = datetime.strptime(last_run.strip(), _TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    age_min = (now - ts).total_seconds() / 60.0
    return age_min > STALE_FACTOR * interval_min


def get_stale_workers(state_repo, now: datetime | None = None) -> list[str]:
    """Return human-readable descriptions of all stale workers.

    Workers that have never written a heartbeat are only reported once the
    deploy marker HEARTBEAT_DEPLOYED_AT exists and is itself older than the
    stale window — avoids a false-alarm storm right after rolling out this
    feature.
    """
    if now is None:
        now = _utcnow()

    deployed_at = state_repo.get("HEARTBEAT_DEPLOYED_AT")
    if not deployed_at:
        # First monitor run after deploy: set marker, report nothing yet.
        try:
            state_repo.set("HEARTBEAT_DEPLOYED_AT", now.strftime(_TS_FORMAT))
        except Exception:
            pass
        return []

    stale: list[str] = []
    for worker, interval in EXPECTED_INTERVALS_MIN.items():
        last_run = state_repo.get(f"LAST_RUN_{worker.upper()}")
        if last_run is None and is_stale(deployed_at, interval, now):
            stale.append(f"{worker}: noch nie gelaufen seit Deploy ({deployed_at} UTC)")
        elif last_run is not None and is_stale(last_run, interval, now):
            stale.append(f"{worker}: letzter Lauf {last_run} UTC (Intervall {interval}min)")
    return stale
