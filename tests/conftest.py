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
