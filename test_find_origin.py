"""find_origin must produce an origin the engine agrees with.

The tool is only useful if `market_ordinal(now)` equals the step it promised, so
every case here sets the printed origin and asks market_clock to confirm it.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from find_origin import current_slot, last_slot_index, walk_back
from market_clock import MarketClockError, resolve_market_slot, session_slot_count

UTC = timezone.utc
THURSDAY_1545_ET = datetime(2026, 7, 23, 19, 45, tzinfo=UTC)     # slot 12 of 13
FRIDAY_AFTER_THANKSGIVING = datetime(2026, 11, 27, 15, 0, tzinfo=UTC)   # early close


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("LEGO_SLOT_SECONDS", "1800")
    monkeypatch.delenv("LEGO_MARKET_HOLIDAYS", raising=False)
    monkeypatch.delenv("LEGO_MARKET_EARLY_CLOSES", raising=False)


def _ordinal_at(monkeypatch, origin: datetime, at: datetime) -> int:
    monkeypatch.setenv("LEGO_DNA_ORIGIN_UTC", origin.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return resolve_market_slot(at).market_ordinal


@pytest.mark.parametrize("n", [0, 1, 12, 13, 14, 26, 40, 130])
def test_walk_back_round_trips_to_the_requested_step(monkeypatch, n):
    """The whole point: origin = walk_back(n) makes the next row DNA step n."""
    origin = walk_back(n, THURSDAY_1545_ET)
    assert _ordinal_at(monkeypatch, origin, THURSDAY_1545_ET) == n


def test_walk_back_zero_is_the_current_slot_start():
    assert walk_back(0, THURSDAY_1545_ET) == datetime(2026, 7, 23, 19, 30, tzinfo=UTC)


def test_walk_back_skips_weekends_and_never_lands_off_session(monkeypatch):
    # 13 slots per regular 30m session, so 13 steps back is the previous session.
    assert walk_back(13, THURSDAY_1545_ET) == datetime(2026, 7, 22, 19, 30, tzinfo=UTC)
    monday = walk_back(40, THURSDAY_1545_ET)
    assert monday.date() == datetime(2026, 7, 20).date()        # Monday, not Sunday
    assert current_slot(monday)[2] == monday                    # lands on a slot start
    assert _ordinal_at(monkeypatch, monday, THURSDAY_1545_ET) == 40


def test_walk_back_counts_an_early_close_session_correctly(monkeypatch):
    """The day after Thanksgiving is 7 slots, not 13 — the ordinal must agree."""
    assert last_slot_index(FRIDAY_AFTER_THANKSGIVING.date()) == 6
    for n in (0, 3, 7, 20):
        origin = walk_back(n, FRIDAY_AFTER_THANKSGIVING)
        assert _ordinal_at(monkeypatch, origin, FRIDAY_AFTER_THANKSGIVING) == n


@pytest.mark.parametrize("sec", ["900", "3600", "14400", "86400"])
def test_walk_back_holds_on_every_trained_timeframe(monkeypatch, sec):
    monkeypatch.setenv("LEGO_SLOT_SECONDS", sec)
    for n in (0, 1, 5, 30):
        origin = walk_back(n, THURSDAY_1545_ET)
        assert _ordinal_at(monkeypatch, origin, THURSDAY_1545_ET) == n


def test_last_slot_index_delegates_to_the_clocks_bar_count(monkeypatch):
    """One owner for the trailing-partial-bar rule; no second copy to drift."""
    for sec in (900, 1800, 3600, 14400, 86400):
        monkeypatch.setenv("LEGO_SLOT_SECONDS", str(sec))
        for day in (THURSDAY_1545_ET.date(), FRIDAY_AFTER_THANKSGIVING.date()):
            assert last_slot_index(day) == session_slot_count(day, sec) - 1


def test_outside_a_session_fails_closed():
    with pytest.raises(MarketClockError):
        walk_back(1, datetime(2026, 7, 25, 18, 0, tzinfo=UTC))    # Saturday
    with pytest.raises(MarketClockError):
        walk_back(1, datetime(2026, 7, 23, 12, 0, tzinfo=UTC))    # pre-open


def test_negative_step_is_rejected():
    with pytest.raises(ValueError):
        walk_back(-1, THURSDAY_1545_ET)


def test_current_slot_matches_the_engine_grid(monkeypatch):
    session_date, index, start = current_slot(THURSDAY_1545_ET)
    monkeypatch.setenv("LEGO_DNA_ORIGIN_UTC", "2026-07-23T13:30:00Z")
    slot = resolve_market_slot(THURSDAY_1545_ET)
    assert slot.slot_id == f"{session_date.isoformat()}:{index}"
    assert slot.slot_start_utc == start
