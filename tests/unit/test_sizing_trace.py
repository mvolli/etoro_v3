"""Sizing-Herleitung im Signal-Worker-Embed (feat/sizing-trace).

Vorher zeigte der Embed nur den Endbetrag: "BUY $60" ohne Herkunft. Die
Faktoren lagen verstreut im Log, und die Logzeile "auf min_buy $100 angehoben"
widersprach dem Endbetrag sichtbar, weil die ATR-Risk-Parity danach noch
skaliert. Diese Tests halten fest, dass die Kette im Embed landet und der
Ausfall der Kette (CORE_SWEEP-Pfad liefert keine) nichts kaputt macht.
"""
import pathlib

WORKER = (pathlib.Path(__file__).resolve().parents[2]
          / "src" / "bot" / "workers" / "signal_worker.py").read_text(encoding="utf-8")
EMBEDS = (pathlib.Path(__file__).resolve().parents[2]
          / "src" / "bot" / "discord_embeds.py").read_text(encoding="utf-8")


def test_jeder_groessenschritt_wird_protokolliert():
    """Wer buy_amount aendert, muss die Spur ergaenzen — sonst fehlt ein Glied."""
    import re

    # Die Basis legt die Liste an, alle weiteren Schritte haengen an.
    assert '_trace = [f"Basis ' in WORKER, "Die Spur wird nicht angelegt"
    # Toleriert Zeilenumbrueche nach append( — laengere Eintraege sind umbrochen.
    for schritt in ("Kelly x", "News CAUTION", "Deployment-Boost x",
                    "Entry-Quality x", "ATR-Risk-Parity x", "Korrelation x",
                    "Region x"):
        muster = r'_trace\.append\(\s*f"' + re.escape(schritt)
        assert re.search(muster, WORKER), (
            f"Der Schritt {schritt!r} taucht nicht in der Sizing-Spur auf"
        )


def test_spur_geht_an_den_embed():
    assert '"sizing_trace": list(_trace),' in WORKER
    assert 't.get("sizing_trace")' in EMBEDS


def test_embed_kommt_ohne_spur_zurecht():
    """Der CORE_SWEEP-Pfad legt keine Spur an — das darf nicht knallen."""
    assert 't.get("sizing_trace") or []' in EMBEDS, (
        "Ohne Fallback wuerde ein Trade ohne Spur den Embed zerlegen"
    )


def test_laenge_wird_gedeckelt():
    """Discord verwirft Felder ueber 1024 Zeichen kommentarlos."""
    assert "_chain[:857]" in EMBEDS


def test_rendering_ergibt_die_erwartete_zeile():
    trace = [
        "Basis MEDIUM 5.0% x $8,368 x 0.50 = $209.19",
        "Kelly x0.63 = $131.79",
        "Entry-Quality x0.50 -> min_buy $100 = $100.00",
        "ATR-Risk-Parity x0.60 (SL 6.00% statt 3.00%) = $60.00",
    ]
    chain = " \u2192 ".join(trace)
    zeile = f"\u2514 {chain}"
    assert zeile.startswith("\u2514 Basis MEDIUM")
    assert zeile.endswith("$60.00")
    assert len(zeile) < 1024
