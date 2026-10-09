#!/usr/bin/env python3
"""Unit tests — fix/risk-worker-import-order (2026-10-09).

Cron startet die Worker als nacktes Skript (`python3 src/bot/workers/x.py`),
ohne PYTHONPATH=src. Ein Import aus dem `bot`-Paket VOR dem Path-Bootstrap
laesst den Worker dann mit ModuleNotFoundError sterben. Gemessen: risk_worker
ist so 198 Laeufe in Folge (~16,5 h) ausgefallen, waehrend Tests und Import-
Pruefungen mit PYTHONPATH=src gruen blieben.

Dieser Test laedt jede Worker-Datei in einem Subprozess OHNE PYTHONPATH und
ohne __main__, damit nur die Modul-Ebene laeuft (kein Worker-Lauf, kein
Broker-Zugriff) und der Import-Fehler sichtbar wird.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKERS = sorted((PROJECT_ROOT / "src" / "bot" / "workers").glob("*.py"))


def _clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    return env


@pytest.mark.parametrize("worker", WORKERS, ids=lambda p: p.name)
def test_worker_module_imports_without_pythonpath(worker: Path, tmp_path: Path) -> None:
    code = (
        "import runpy, sys\n"
        f"runpy.run_path({str(worker)!r}, run_name='worker_import_probe')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=_clean_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"{worker.name} laesst sich ohne PYTHONPATH nicht laden:\n{result.stderr[-800:]}"
    )
