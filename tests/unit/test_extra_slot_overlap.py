"""fix/extra-slot-overlap: Extra-Slots duerfen keine Basis-Kandidaten doppeln."""
from bot.workers.signal_worker import _select_candidates


def _elig(n, conviction="HIGH"):
    return [({"instrument_id": i, "conviction": conviction}, f"S{i}") for i in range(n)]


def _cfg(slots):
    return {"trading": {"candidate_slots": slots, "cash_target_max_pct": 30.0}}


def test_extra_slots_start_after_base_slots():
    c = _select_candidates(_cfg(5), _elig(9), cash_estimate=6000, equity=10000)
    ids = [s["instrument_id"] for s, _ in c]
    assert ids == [0, 1, 2, 3, 4, 5, 6]
    assert len(ids) == len(set(ids)), "Instrument doppelt in den Kandidaten"


def test_no_duplicates_when_pool_only_fills_base():
    c = _select_candidates(_cfg(5), _elig(5), cash_estimate=6000, equity=10000)
    assert [s["instrument_id"] for s, _ in c] == [0, 1, 2, 3, 4]


def test_three_base_slots_unchanged():
    c = _select_candidates(_cfg(3), _elig(9), cash_estimate=6000, equity=10000)
    assert [s["instrument_id"] for s, _ in c] == [0, 1, 2, 3, 4]


def test_no_extra_slots_without_cash_surplus():
    c = _select_candidates(_cfg(5), _elig(9), cash_estimate=1000, equity=10000)
    assert [s["instrument_id"] for s, _ in c] == [0, 1, 2, 3, 4]


def test_extra_slots_only_high_plus():
    elig = _elig(5) + [({"instrument_id": 5, "conviction": "MEDIUM"}, "S5"),
                       ({"instrument_id": 6, "conviction": "VERY_HIGH"}, "S6")]
    c = _select_candidates(_cfg(5), elig, cash_estimate=6000, equity=10000)
    assert [s["instrument_id"] for s, _ in c] == [0, 1, 2, 3, 4, 6]
