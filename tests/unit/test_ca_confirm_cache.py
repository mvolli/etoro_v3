"""Tests perf/ca-confirm-cache (2026-08-17).

Der Corporate-Action-Guard laesst einen Sprung CA_SCAN_BARS (50) Bars in der
Reihe stehen. Ohne Cache fragte der data_worker deshalb im 5-min-Takt fuer
dieselben Symbole (KRS.L, ADME.L) dieselbe Yahoo-Action-Historie ab und bekam
dieselbe negative Antwort — gemessen +4,6 s je Lauf, dauerhaft.

Jeder Lauf ist ein eigener Prozess ⇒ der Cache muss persistent sein. Alle
Tests arbeiten auf einer tmp-DB, NIE auf data/trading.db (AGENTS.md).
"""

import sys

import pandas as pd
import pytest

from bot.core import ca_confirm_cache as cc
from bot.core import corporate_actions as ca
from bot.core.corporate_actions import scan_price_gaps


@pytest.fixture
def db(tmp_path):
    """Pfad auf eine frische tmp-DB — niemals die Live-DB."""
    return str(tmp_path / "cache_test.db")


# Der Anlassfall JMAT.L als Indikator-Dict, wie es aus scan_price_gaps kommt.
def _ind(**over):
    base = {
        "price": 2282.0,
        "ca_gap_pct": -21.94,
        "ca_gap_ratio": 0.7806,
        "ca_gap_date": "2026-08-17",
    }
    base.update(over)
    return base


# ── ca_gap_date: die stabile Identitaet des Sprungs ──────────────────────────

def _dated_df(closes, start="2026-06-01"):
    idx = pd.bdate_range(start=start, periods=len(closes))
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c * 1.01 for c in closes],
            "Low": [c * 0.99 for c in closes],
            "Close": closes,
            "Volume": [1_000_000] * len(closes),
        },
        index=idx,
    )


def test_scan_reports_the_date_of_the_jump_bar():
    closes = [100.0] * 60 + [133.333, 133.5, 133.2]
    df = _dated_df(closes)
    gaps = scan_price_gaps(df)
    # Sprung-Bar ist Index 60 der Reihe (erste Bar auf neuem Niveau)
    assert gaps["ca_gap_date"] == df.index[60].date().isoformat()


def test_scan_date_is_none_without_datetime_index():
    # Synthetischer Frame mit RangeIndex — kein Datum ableitbar, kein Cache.
    closes = [100.0] * 60 + [200.0] * 3
    gaps = scan_price_gaps(pd.DataFrame({"Close": closes, "Volume": [1] * len(closes)}))
    assert gaps["ca_gap_date"] is None


def test_gap_date_survives_a_new_bar_while_bars_ago_does_not():
    """Genau der Grund, warum bars_ago als Schluessel untauglich ist."""
    closes = [100.0] * 60 + [133.333, 133.5]
    day1 = scan_price_gaps(_dated_df(closes))
    day2 = scan_price_gaps(_dated_df(closes + [133.4]))
    assert day2["ca_gap_bars_ago"] == day1["ca_gap_bars_ago"] + 1
    assert day2["ca_gap_date"] == day1["ca_gap_date"]


# ── cache_key ────────────────────────────────────────────────────────────────

def test_key_contains_symbol_date_and_ratio():
    assert cc.cache_key("JMAT.L", _ind()) == "JMAT.L|2026-08-17|0.78"


def test_key_is_none_without_gap_date():
    # Ohne Datum ist der Sprung nicht zuordenbar → gar nicht cachen.
    assert cc.cache_key("JMAT.L", _ind(ca_gap_date=None)) is None


def test_key_is_none_without_ratio():
    assert cc.cache_key("JMAT.L", _ind(ca_gap_ratio=None)) is None


def test_key_is_none_without_symbol():
    assert cc.cache_key("", _ind()) is None


def test_key_ignores_intraday_ratio_noise():
    # 2 Dezimalen: das intraday wandernde Close der letzten Bar soll den
    # Schluessel nicht bei jedem 5-min-Lauf umwerfen.
    assert cc.cache_key("X", _ind(ca_gap_ratio=0.7806)) == cc.cache_key(
        "X", _ind(ca_gap_ratio=0.7811)
    )


# ── Runde durch die DB ───────────────────────────────────────────────────────

def test_lookup_on_empty_db_is_a_miss(db):
    assert cc.lookup("JMAT.L|2026-08-17|0.78", db) == (False, None)


def test_positive_result_round_trip(db):
    cc.store("K", "JMAT.L", "Split 0.75x am 2026-08-17", db)
    assert cc.lookup("K", db) == (True, "Split 0.75x am 2026-08-17")


def test_negative_result_is_cached_too(db):
    """Der haeufige Fall — ohne ihn bringt der Cache fuer KRS.L/ADME.L nichts."""
    cc.store("K", "KRS.L", None, db)
    hit, result = cc.lookup("K", db)
    assert hit is True and result is None


def test_table_creation_is_idempotent(db):
    cc.store("K", "KRS.L", None, db)
    cc.store("K2", "ADME.L", None, db)     # zweiter Lauf, Tabelle existiert
    assert cc.lookup("K", db)[0] is True
    assert cc.lookup("K2", db)[0] is True


def test_restore_overwrites_a_stale_entry(db):
    cc.store("K", "JMAT.L", None, db)
    cc.store("K", "JMAT.L", "Split 0.75x am 2026-08-17", db)
    assert cc.lookup("K", db) == (True, "Split 0.75x am 2026-08-17")


# ── Ein anderer Sprung darf NICHT vom alten Eintrag maskiert werden ──────────

def test_a_jump_on_another_date_is_a_miss(db):
    cc.store(cc.cache_key("KRS.L", _ind()), "KRS.L", None, db)
    other = cc.cache_key("KRS.L", _ind(ca_gap_date="2026-09-30"))
    assert cc.lookup(other, db) == (False, None)


def test_a_different_ratio_on_the_same_date_is_a_miss(db):
    """Yahoo passt nur EINEN Effekt an → gleiche Sprung-Bar, andere Hoehe."""
    cc.store(cc.cache_key("JMAT.L", _ind()), "JMAT.L", None, db)
    partial = cc.cache_key("JMAT.L", _ind(ca_gap_ratio=0.75))
    assert cc.lookup(partial, db) == (False, None)


def test_another_symbol_is_a_miss(db):
    cc.store(cc.cache_key("KRS.L", _ind()), "KRS.L", None, db)
    assert cc.lookup(cc.cache_key("ADME.L", _ind()), db) == (False, None)


# ── TTL ──────────────────────────────────────────────────────────────────────

def _freeze(monkeypatch, t):
    monkeypatch.setattr(cc, "_now", lambda: t)


def test_negative_expires_after_six_hours(monkeypatch, db):
    _freeze(monkeypatch, 1_000_000.0)
    cc.store("K", "KRS.L", None, db)

    _freeze(monkeypatch, 1_000_000.0 + cc.CA_CACHE_TTL_NEGATIVE_S - 60)
    assert cc.lookup("K", db)[0] is True

    _freeze(monkeypatch, 1_000_000.0 + cc.CA_CACHE_TTL_NEGATIVE_S + 60)
    assert cc.lookup("K", db)[0] is False


def test_positive_outlives_the_negative_ttl(monkeypatch, db):
    # Eine bestaetigte Aktion verschwindet nicht aus Yahoos Historie; die
    # Sperre endet ohnehin, sobald der Sprung aus der Reihe faellt.
    _freeze(monkeypatch, 1_000_000.0)
    cc.store("K", "JMAT.L", "Split 0.75x am 2026-08-17", db)

    _freeze(monkeypatch, 1_000_000.0 + cc.CA_CACHE_TTL_NEGATIVE_S + 60)
    assert cc.lookup("K", db)[0] is True

    _freeze(monkeypatch, 1_000_000.0 + cc.CA_CACHE_TTL_POSITIVE_S + 60)
    assert cc.lookup("K", db)[0] is False


def test_negative_ttl_stays_well_inside_the_lag_window():
    # Das Guard-Fenster IST das Lag zwischen Ex-Tag und Yahoos Anpassung
    # (Stunden bis Tage). Ein negativer Tages-TTL verschluckte es.
    assert cc.CA_CACHE_TTL_NEGATIVE_S <= 6 * 3600
    assert cc.CA_CACHE_TTL_NEGATIVE_S < cc.CA_CACHE_TTL_POSITIVE_S
    # Mindestens 4 Pruefungen pro Kalendertag — eine je Handelssession.
    assert 86400 / cc.CA_CACHE_TTL_NEGATIVE_S >= 4


def test_prune_drops_ancient_rows(monkeypatch, db):
    _freeze(monkeypatch, 1_000_000.0)
    cc.store("ALT", "KRS.L", None, db)

    _freeze(monkeypatch, 1_000_000.0 + (cc.CA_CACHE_PRUNE_DAYS + 1) * 86400)
    cc.store("NEU", "ADME.L", None, db)     # Schreibpfad raeumt auf

    conn = cc._get_conn(db)
    keys = {r[0] for r in conn.execute("SELECT cache_key FROM ca_confirm_cache")}
    conn.close()
    assert keys == {"NEU"}


# ── Fail-open ────────────────────────────────────────────────────────────────

def test_lookup_fails_open_on_broken_db(tmp_path):
    broken = tmp_path / "notadb.db"
    broken.write_bytes(b"das ist keine sqlite-datei" * 100)
    assert cc.lookup("K", str(broken)) == (False, None)


def test_store_fails_open_on_broken_db(tmp_path):
    broken = tmp_path / "notadb.db"
    broken.write_bytes(b"das ist keine sqlite-datei" * 100)
    cc.store("K", "X", None, str(broken))    # darf nicht werfen


def test_empty_key_touches_nothing(db):
    cc.store("", "X", "Split 2x", db)
    assert cc.lookup("", db) == (False, None)


# ── ConfirmBudget mit Cache ──────────────────────────────────────────────────

def _stub_yf(monkeypatch, splits=None, dividends=None, date="2026-08-17"):
    """Wie in test_corporate_action_guard, zusaetzlich mit Aufruf-Zaehler."""
    calls: list[str] = []
    idx = pd.to_datetime([date], utc=True)
    empty = pd.Series(dtype=float)

    class _Ticker:
        def __init__(self, sym):
            calls.append(sym)
            self.splits = pd.Series([splits], index=idx) if splits is not None else empty
            self.dividends = pd.Series([dividends], index=idx) if dividends is not None else empty

    monkeypatch.setitem(sys.modules, "yfinance", type("M", (), {"Ticker": _Ticker}))
    return calls


def test_second_run_hits_the_cache_instead_of_yahoo(monkeypatch, db):
    calls = _stub_yf(monkeypatch, splits=0.75)

    first = ca.ConfirmBudget(db_path=db)
    ind1 = _ind()
    assert first.annotate("JMAT.L", ind1) is True
    assert ind1["ca_confirmed"].startswith("Split")
    assert (first.used, first.cached) == (1, 0)

    # Neuer Prozess = neue Instanz, derselbe Sprung.
    second = ca.ConfirmBudget(db_path=db)
    ind2 = _ind()
    assert second.annotate("JMAT.L", ind2) is True
    assert ind2["ca_confirmed"].startswith("Split")
    assert (second.used, second.cached, second.hits) == (0, 1, 1)
    assert len(calls) == 1        # genau EIN Netz-Call fuer zwei Laeufe


def test_negative_result_stops_the_repeated_query(monkeypatch, db):
    """Der KRS.L/ADME.L-Fall: Yahoo meldet dauerhaft nichts."""
    calls = _stub_yf(monkeypatch)          # keine Aktionen

    for _ in range(5):                     # fuenf 5-min-Laeufe
        budget = ca.ConfirmBudget(db_path=db)
        ind = _ind(price=1.85)
        assert budget.annotate("KRS.L", ind) is False
        assert "ca_confirmed" not in ind

    assert len(calls) == 1


def test_cache_hit_does_not_consume_the_budget(monkeypatch, db):
    # Ein Treffer kostet keinen Netz-Call und darf den Deckel nicht anfassen,
    # sonst verhungern frische Symbole hinter lange bekannten.
    _stub_yf(monkeypatch)
    cc.store(cc.cache_key("KRS.L", _ind()), "KRS.L", None, db)

    budget = ca.ConfirmBudget(limit=1, db_path=db)
    budget.annotate("KRS.L", _ind())
    assert (budget.used, budget.cached) == (0, 1)
    # Budget noch frei fuer das unbekannte Symbol
    assert budget.annotate("NEU.L", _ind(ca_gap_date="2026-08-16")) is False
    assert budget.used == 1


def test_cached_positive_works_with_an_exhausted_budget(monkeypatch, db):
    calls = _stub_yf(monkeypatch)
    cc.store(cc.cache_key("JMAT.L", _ind()), "JMAT.L", "Split 0.75x am 2026-08-17", db)

    budget = ca.ConfirmBudget(limit=0, db_path=db)
    ind = _ind()
    assert budget.annotate("JMAT.L", ind) is True
    assert ind["ca_confirmed"] == "Split 0.75x am 2026-08-17"
    assert calls == []


def test_symbol_without_gap_touches_neither_cache_nor_net(monkeypatch, db):
    calls = _stub_yf(monkeypatch, splits=0.75)
    budget = ca.ConfirmBudget(db_path=db)
    assert budget.annotate("AAPL", {"price": 100.0}) is False
    assert (budget.used, budget.cached) == (0, 0)
    assert calls == []
    conn = cc._get_conn(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "ca_confirm_cache" not in tables


def test_uncacheable_gap_still_queries_every_run(monkeypatch, db):
    # Ohne ca_gap_date bleibt es beim alten Verhalten — lieber der alte Preis
    # als ein Eintrag, der sich keinem Sprung zuordnen laesst.
    calls = _stub_yf(monkeypatch, splits=0.75)
    for _ in range(3):
        budget = ca.ConfirmBudget(db_path=db)
        assert budget.annotate("X", _ind(ca_gap_date=None)) is True
        assert budget.used == 1
    assert len(calls) == 3


def test_expired_negative_is_re_queried(monkeypatch, db):
    calls = _stub_yf(monkeypatch)
    _freeze(monkeypatch, 2_000_000.0)
    ca.ConfirmBudget(db_path=db).annotate("KRS.L", _ind())

    _freeze(monkeypatch, 2_000_000.0 + cc.CA_CACHE_TTL_NEGATIVE_S + 60)
    budget = ca.ConfirmBudget(db_path=db)
    budget.annotate("KRS.L", _ind())
    assert (budget.used, budget.cached) == (1, 0)
    assert len(calls) == 2
