#!/usr/bin/env python3
"""Unit tests — worker_lock (previously zero coverage; concurrency-critical).

The non-blocking flock guard is what stops two overlapping cron invocations
of the same worker from double-trading. flock is tied to the open file
description, so two separate worker_lock() contexts on the same name within
one process genuinely contend — which is exactly how we test it.
"""
from __future__ import annotations

import bot.core.worker_lock as wl


import pytest


@pytest.fixture(autouse=True)
def _tmp_lock_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(wl, "LOCK_DIR", tmp_path / "locks")


def test_acquires_when_free():
    with wl.worker_lock("risk_worker") as acquired:
        assert acquired is True


def test_second_holder_is_blocked():
    with wl.worker_lock("data_worker") as first:
        assert first is True
        with wl.worker_lock("data_worker") as second:
            assert second is False  # already held → skip this cycle


def test_lock_released_after_context():
    with wl.worker_lock("reconciler") as a:
        assert a is True
    # released — a fresh acquire must succeed
    with wl.worker_lock("reconciler") as b:
        assert b is True


def test_different_workers_do_not_contend():
    with wl.worker_lock("signal_worker") as a:
        with wl.worker_lock("execution_worker") as b:
            assert a is True and b is True


def test_lock_file_created_in_lock_dir(tmp_path):
    with wl.worker_lock("monitor_worker"):
        assert (tmp_path / "locks" / "monitor_worker.lock").exists()


def test_lock_dir_created_if_missing(tmp_path):
    # LOCK_DIR (tmp_path/locks) does not exist yet — worker_lock must mkdir it.
    assert not (tmp_path / "locks").exists()
    with wl.worker_lock("x") as acquired:
        assert acquired is True
    assert (tmp_path / "locks").exists()
