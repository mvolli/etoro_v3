"""Pytest configuration and shared fixtures."""
import builtins
import sqlite3
import sys
import tempfile
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

_PROD = (Path(__file__).parent.parent / "data").resolve()
_ORIG: dict = {}


# ─── Schranke gegen Schreibzugriffe auf data/ ────────────────────────────────
#
# fix/tests-schreiben-in-produktion (2026-09-10/11). Zwei Lecks liefen
# wochenlang unbemerkt, beide nach demselben Muster: eine Fixture schaltete
# nach NAMEN stumm und griff daneben.
#
#   test_llm_tighten_remaining  patchte LE._post_closed_embed — ein Name, den
#       llm_execution gar nicht hat (er heisst _discord). Das `if hasattr(...)`
#       drumherum verschluckte den Fehlgriff. Folge: 540 Zeilen in der
#       Produktions-system_log und ebenso viele ECHTE Posts nach
#       #etoro-trades, ueber fuenf Tage.
#
#   test_llm_advisors           patchte CONFIG_YAML_PATH und
#       _load_decision_log, aber nicht DECISION_LOG_PATH. Weil
#       _load_decision_log auf [] gepatcht war, wurde
#       data/llm_decision_log.json nicht ergaenzt sondern UEBERSCHRIEBEN.
#       110 echte Eintraege mussten aus der Git-Historie zurueck.
#
# Gegen den naechsten uebersehenen Namen hilft keine dritte Stummschaltung:
# allein unter src/bot/ zeigen 40 Modulkonstanten direkt auf data/, alle zur
# Importzeit gesetzt, keine injiziert. Deshalb eine Schranke am Systemaufruf.
#
# Installiert wird in pytest_configure, NICHT als Fixture. Eine
# funktionsweite Fixture greift erst, wenn ein Test laeuft — gemessen am
# 2026-09-11 kamen an ihr vorbei:
#   * Schreibzugriffe auf Modulebene einer Testdatei (Sammelphase)
#   * Fixtures mit scope="session"/"module" (laufen vor den funktionsweiten)
# Beide Proben legten ihre Datei unter data/ an, ohne dass etwas ansprang.


def _ist_produktion(ziel) -> bool:
    try:
        return Path(str(ziel)).resolve().is_relative_to(_PROD)
    except Exception:
        return False


def _stop(ziel):
    import os
    test = os.environ.get("PYTEST_CURRENT_TEST", "(Sammelphase / Modulebene)")
    raise AssertionError(
        f"Test schreibt in die Produktionsdaten: {Path(str(ziel)).name}\n"
        f"  Stelle: {test}\n"
        f"  Die betroffene Modulkonstante auf tmp_path patchen, z. B.\n"
        f"    monkeypatch.setattr(modul, 'DECISION_LOG_PATH', tmp_path / 'x.json')"
    )


def pytest_configure(config):
    _ORIG.update(connect=sqlite3.connect, open=builtins.open,
                 write_text=Path.write_text, write_bytes=Path.write_bytes)

    def connect(*a, **k):
        # Lesen bleibt erlaubt (mode=ro) — sonst waeren Fixtures blockiert,
        # die legitim aus der Produktions-DB lesen.
        if a and "mode=ro" not in str(a[0]) and _ist_produktion(a[0]):
            _stop(a[0])
        return _ORIG["connect"](*a, **k)

    def open_(file, mode="r", *a, **k):
        if any(c in str(mode) for c in "wax+") and _ist_produktion(file):
            _stop(file)
        return _ORIG["open"](file, mode, *a, **k)

    def write_text(self, *a, **k):
        if _ist_produktion(self):
            _stop(self)
        return _ORIG["write_text"](self, *a, **k)

    def write_bytes(self, *a, **k):
        if _ist_produktion(self):
            _stop(self)
        return _ORIG["write_bytes"](self, *a, **k)

    sqlite3.connect, builtins.open = connect, open_
    Path.write_text, Path.write_bytes = write_text, write_bytes

    # Zweite Ebene: der Netzpfad. Ohne Token oeffnet _request_discord() gar
    # keine Verbindung (return 0, "kein DISCORD_BOT_TOKEN"), _post_embed()
    # gibt False zurueck und ruft insert_system_log() nie auf. Die gesamte
    # Logik darueber laeuft unveraendert — anders als bei einem Stub von
    # _post_embed, der 17 Tests genau das wegnahm, was sie pruefen.
    # Tests, die einen erfolgreichen Post BRAUCHEN, setzen _read_token per
    # monkeypatch selbst und gewinnen damit.
    try:
        from bot import discord_embeds as de
        de._read_token = lambda: None
        de._TRADING_DB_PATH = Path(tempfile.gettempdir()) / "pytest_kein_prod.db"
    except Exception:
        pass


def pytest_unconfigure(config):
    if _ORIG:
        sqlite3.connect = _ORIG["connect"]
        builtins.open = _ORIG["open"]
        Path.write_text = _ORIG["write_text"]
        Path.write_bytes = _ORIG["write_bytes"]
