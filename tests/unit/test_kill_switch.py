#!/usr/bin/env python3
"""Unit tests — kill_switch (previously zero coverage; safety-critical).

Covers activate/deactivate/is_active/get_reason and the TOCTOU-safe
deactivate (unlink(missing_ok=True)).
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def ks(tmp_path, monkeypatch):
    """kill_switch module with its flag file redirected into tmp_path."""
    import bot.core.kill_switch as ks_mod
    importlib.reload(ks_mod)
    flag = tmp_path / "kill_switch.flag"
    monkeypatch.setattr(ks_mod, "_KILL_SWITCH_FILE", flag)
    monkeypatch.setattr(ks_mod, "KILL_SWITCH_FILE", flag)
    return ks_mod


def test_inactive_by_default(ks):
    assert ks.is_kill_switch_active() is False
    assert ks.get_reason() == ""


def test_activate_creates_flag_with_reason(ks):
    ks.activate("daily loss -6%")
    assert ks.is_kill_switch_active() is True
    assert ks.get_reason() == "daily loss -6%"


def test_activate_default_reason(ks):
    ks.activate()
    assert ks.is_kill_switch_active() is True
    assert ks.get_reason() == "Manual kill switch"


def test_deactivate_removes_flag(ks):
    ks.activate("x")
    ks.deactivate()
    assert ks.is_kill_switch_active() is False


def test_deactivate_is_idempotent_no_race(ks):
    # TOCTOU guard: deactivating when already gone must not raise.
    ks.activate("x")
    ks.deactivate()
    ks.deactivate()  # second call — file already absent
    assert ks.is_kill_switch_active() is False


def test_deactivate_when_never_active(ks):
    # Never activated → deactivate must be a silent no-op, not an error.
    ks.deactivate()
    assert ks.is_kill_switch_active() is False


def test_activate_creates_parent_dir(ks, tmp_path, monkeypatch):
    nested = tmp_path / "sub" / "dir" / "kill_switch.flag"
    monkeypatch.setattr(ks, "_KILL_SWITCH_FILE", nested)
    ks.activate("boom")
    assert nested.exists()
