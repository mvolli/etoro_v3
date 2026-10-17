#!/usr/bin/env python3
"""config.yaml darf keinen Schluessel doppelt haben (2026-10-17).

PyYAML nimmt bei doppelten Schluesseln still den letzten. So lag seit 16.10.
ein zweiter `partial_close:`-Block am Dateiende, der den ersten (mode, ledger,
partial_threshold_pct) komplett ueberschrieb — folgenlos nur, weil die Werte
den Code-Defaults glichen.
"""
from __future__ import annotations

from pathlib import Path

import yaml

CFG = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


def test_keine_doppelten_schluessel():
    dups: list[str] = []

    class _Loader(yaml.SafeLoader):
        pass

    def _mapping(loader, node, deep=False):
        keys = [k.value for k, _ in node.value]
        dups.extend(f"{k} (Zeile {node.start_mark.line + 1})" for k in set(keys) if keys.count(k) > 1)
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    _Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
    yaml.load(CFG.read_text(encoding="utf-8"), Loader=_Loader)
    assert dups == []
