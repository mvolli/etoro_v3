"""Pytest configuration and shared fixtures."""
import sys
from pathlib import Path

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def _kein_zugriff_auf_produktion(monkeypatch, tmp_path_factory):
    """Testlaeufe duerfen weder nach Discord posten noch in trading.db schreiben.

    fix/tests-schreiben-in-produktion (2026-09-10): Zwischen dem 2026-09-05
    und heute sind 526 Close-Embeds fuer CAR.AX nach #etoro-trades gegangen
    und ebenso viele Zeilen in die Produktions-`system_log` — nicht vom Bot,
    sondern von `tests/unit/test_llm_tighten_remaining.py`. Ein Lauf dieser
    Datei erzeugt reproduzierbar exakt 15 davon.

    Ursache war eine Isolations-Fixture, die den falschen Namen stumm
    schaltete:

        for name in ("_post_closed_embed", "_append_outcome_entry"):
            if hasattr(LE, name):
                monkeypatch.setattr(LE, name, lambda *a, **k: None)

    `llm_execution` hat keine Funktion `_post_closed_embed` — sie heisst dort
    `_discord`. `hasattr` war False, der Patch griff nie, und weil das `if`
    den Fehler verschluckte, sah die Fixture aus wie eine Absicherung.
    `_post_embed()` hat dann mit dry_run=False echt gepostet und
    `insert_system_log()` in `data/trading.db` geschrieben — beide Pfade
    haengen an Modulkonstanten, nicht an einer injizierten Verbindung.

    Diese Fixture ist die Absicherung, die nicht davon abhaengt, dass jeder
    kuenftige Test an den richtigen Namen denkt: sie greift automatisch in
    JEDEM Test. Wer Discord-Verhalten pruefen will, patcht `_post_embed`
    bzw. `insert_system_log` im eigenen Test selbst — das gewinnt, weil es
    spaeter gesetzt wird.
    """
    try:
        from bot import discord_embeds as de
    except Exception:
        return

    # Angesetzt wird an den ZWEI Modulkonstanten, ueber die der Zugriff
    # ueberhaupt erst moeglich ist — nicht an der Logik darueber.
    #
    # Eine erste Fassung ersetzte _post_embed komplett und nahm damit 17
    # Tests genau das weg, was sie pruefen (Embed-Splitting, Chart-Konsum,
    # Message-ID). Eine zweite ersetzte _request_discord und ueberfuhr die
    # FakeConnection, die test_embed_message_id.py selbst installiert.
    #
    # _read_token() ist der richtige Punkt: ohne Token oeffnet
    # _request_discord() gar keine Verbindung (return 0, "kein
    # DISCORD_BOT_TOKEN"), _post_embed() gibt False zurueck und ruft
    # insert_system_log() nie auf. Die gesamte Logik darueber laeuft
    # unveraendert. Tests, die einen erfolgreichen Post BRAUCHEN, setzen
    # _read_token spaeter selbst — und gewinnen damit.
    monkeypatch.setattr(de, "_read_token", lambda: None, raising=False)

    # Zweiter Leck-Pfad: insert_system_log() haengt an einer Modulkonstante
    # statt an einer injizierten Verbindung. Umbiegen statt stummschalten,
    # damit test_close_history_log.py sein eigenes Ziel weiter setzen kann.
    monkeypatch.setattr(de, "_TRADING_DB_PATH",
                        tmp_path_factory.mktemp("kein_prod") / "trading.db",
                        raising=False)


@pytest.fixture(autouse=True)
def _kein_schreiben_in_produktion(request):
    """Bricht ab, sobald ein Test in data/ schreibt.

    fix/tests-schreiben-in-produktion (2026-09-10): Die Fixture darueber
    deckt die zwei bekannten Pfade in discord_embeds ab. Sie hilft nichts
    gegen den naechsten Test, der eine ANDERE Modulkonstante uebersieht —
    und davon gibt es viele: allein unter src/bot/ zeigen 40 Konstanten
    direkt auf data/ (RECS_PATH, DECISION_LOG_PATH, GHOST_BLACKLIST_PATH,
    CACHE_FILE, _TRADING_DB_PATH …), alle zur Importzeit gesetzt, keine
    injiziert.

    Zwei Lecks haben genau so funktioniert und beide fielen erst nach
    Wochen auf:

      test_llm_tighten_remaining  -> 540 Zeilen in system_log + ebenso
                                     viele echte Posts nach #etoro-trades
      test_llm_advisors           -> UEBERSCHRIEB llm_decision_log.json
                                     bei jedem Lauf; 110 echte Eintraege
                                     mussten aus der Git-Historie zurueck

    Deshalb hier keine weitere Stummschaltung nach Namen, sondern eine
    Schranke am Systemaufruf: wer waehrend eines Tests unter data/
    schreibend oeffnet, bekommt einen Fehler mit dem Dateinamen. Lesen
    bleibt erlaubt (mode=ro / 'r'), sonst waeren Fixtures blockiert, die
    legitim aus der Produktions-DB lesen.
    """
    import builtins
    import sqlite3

    prod = (Path(__file__).parent.parent / "data").resolve()

    def _ist_produktion(ziel) -> bool:
        try:
            return Path(str(ziel)).resolve().is_relative_to(prod)
        except Exception:
            return False

    def _stop(ziel):
        raise AssertionError(
            f"Test schreibt in die Produktionsdaten: {Path(str(ziel)).name}\n"
            f"  Test: {request.node.nodeid}\n"
            f"  Die betroffene Modulkonstante im Test auf tmp_path patchen "
            f"(z. B. monkeypatch.setattr(modul, 'DECISION_LOG_PATH', "
            f"tmp_path / 'x.json'))."
        )

    _connect, _open, _wt, _wb = (sqlite3.connect, builtins.open,
                                 Path.write_text, Path.write_bytes)

    def connect(*a, **k):
        if a and "mode=ro" not in str(a[0]) and _ist_produktion(a[0]):
            _stop(a[0])
        return _connect(*a, **k)

    def open_(file, mode="r", *a, **k):
        if any(c in str(mode) for c in "wax+") and _ist_produktion(file):
            _stop(file)
        return _open(file, mode, *a, **k)

    def write_text(self, *a, **k):
        if _ist_produktion(self):
            _stop(self)
        return _wt(self, *a, **k)

    def write_bytes(self, *a, **k):
        if _ist_produktion(self):
            _stop(self)
        return _wb(self, *a, **k)

    sqlite3.connect, builtins.open = connect, open_
    Path.write_text, Path.write_bytes = write_text, write_bytes
    try:
        yield
    finally:
        sqlite3.connect, builtins.open = _connect, _open
        Path.write_text, Path.write_bytes = _wt, _wb
