"""fix/cfg-load-order: llm_review_worker.CFG liest die config.yaml.

CONFIG_YAML_PATH war erst NACH dem CFG-Load definiert; der NameError wurde
still geschluckt und CFG blieb {} — Ratsche- und QMD-Config wirkten nie.
"""
import yaml

from bot.workers import llm_review_worker as lrw


def test_cfg_ist_nicht_leer_und_entspricht_der_config():
    erwartet = yaml.safe_load(lrw.CONFIG_YAML_PATH.read_text(encoding="utf-8"))
    assert lrw.CFG, "CFG ist leer — Config wird nicht geladen"
    assert lrw.CFG.get("trading", {}).get("signal_weight_ratchet") == \
        erwartet["trading"]["signal_weight_ratchet"]
    assert lrw.CFG.get("memory", {}).get("qmd") == erwartet["memory"]["qmd"]

