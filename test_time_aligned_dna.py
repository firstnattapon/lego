from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

import market_clock
from conftest import FAKE_DB
from lego_one_row import Config, compute_row
from lego_orders import normalize_status as broker_status
from lego_outbox import list_actionable, put_intent, row_is_committed, update_intent
from lego_outbox import normalize_status as outbox_status
from lego_state import (STATE_PATH, CalendarDriftError, OrdinalRegression,
                        SlotAlreadyConsumed, chain_key, commit_final_row,
                        read_anchor, verify_calendar_continuity)
from market_clock import (MarketClockError, calendar_fingerprint, clock_mode,
                          fallback_slot_id, is_regular_session,
                          market_ordinal_for_slot_id, resolve_dna_step,
                          resolve_market_slot, session_bounds, session_slot_count,
                          slot_seconds, us_market_holidays)

UTC = timezone.utc
NORMAL_SESSION = date(2026, 7, 23)        # Thursday, 09:30-16:00 ET
EARLY_CLOSE = date(2026, 11, 27)          # day after Thanksgiving, 09:30-13:00 ET
CFG = Config("AAPL", 3000.0, diff=5.0, dna_code="bypass:100", decimal_precision=2)


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    FAKE_DB.store.clear()
    monkeypatch.setenv("LEGO_SLOT_SECONDS", "1800")          # 30m, a trained timeframe
    monkeypatch.setenv("LEGO_DNA_ORIGIN_UTC", "2026-07-23T18:00:00Z")
    monkeypatch.setenv("LEGO_DNA_CLOCK_MODE", "market")
    monkeypatch.delenv("LEGO_MARKET_HOLIDAYS", raising=False)
    monkeypatch.delenv("LEGO_MARKET_EARLY_CLOSES", raising=False)


def _commit(snapshot, anchor, step, slot_id, ordinal):
    row = compute_row(CFG, snapshot, anchor, dna_step=step)
    return commit_final_row(CFG, snapshot, anchor, row,
                            slot_id=slot_id, market_ordinal=ordinal, clock_mode="market")


# --- grid must reproduce the yfinance bar count the DNA was trained on --------

@pytest.mark.parametrize("sec,normal,early", [
    (900, 26, 14),      # 15m
    (1800, 13, 7),      # 30m
    (3600, 7, 4),       # 1h  - trailing half bar counts
    (14400, 2, 1),      # 4h
    (86400, 1, 1),      # 1d
])
def test_session_slot_count_matches_trained_bars(sec, normal, early):
    assert session_slot_count(NORMAL_SESSION, sec) == normal
    assert session_slot_count(EARLY_CLOSE, sec) == early


def test_untrained_slot_size_is_rejected(monkeypatch):
    monkeypatch.setenv("LEGO_SLOT_SECONDS", "600")
    with pytest.raises(MarketClockError):
        slot_seconds()


def test_missing_slot_size_is_rejected(monkeypatch):
    monkeypatch.delenv("LEGO_SLOT_SECONDS", raising=False)
    with pytest.raises(MarketClockError):
        slot_seconds()


# --- clock semantics ---------------------------------------------------------

def test_market_clock_skips_missed_scheduler_slots():
    s0 = resolve_market_slot(datetime(2026, 7, 23, 18, 0, 5, tzinfo=UTC))
    s2 = resolve_market_slot(datetime(2026, 7, 23, 19, 0, 5, tzinfo=UTC))
    assert s0 is not None and s2 is not None
    assert (s0.market_ordinal, s2.market_ordinal) == (0, 2)
    effective, error = resolve_dna_step(legacy_step=1, slot=s2)
    assert effective == 2
    assert error == -1


def test_market_clock_does_not_count_overnight():
    friday_last = resolve_market_slot(datetime(2026, 7, 24, 19, 30, 5, tzinfo=UTC))
    monday_first = resolve_market_slot(datetime(2026, 7, 27, 13, 30, 5, tzinfo=UTC))
    assert friday_last is not None and monday_first is not None
    assert monday_first.market_ordinal == friday_last.market_ordinal + 1


def test_slot_id_round_trips_to_the_same_ordinal():
    slot = resolve_market_slot(datetime(2026, 7, 24, 15, 0, 5, tzinfo=UTC))
    assert market_ordinal_for_slot_id(slot.slot_id) == slot.market_ordinal


def test_fallback_slot_id_is_namespaced():
    assert fallback_slot_id("2026-07-23T18:05:00Z").startswith("epoch:")


# --- DNA time alignment, output contract -------------------------------------

def test_explicit_market_step_preserves_17_columns_and_can_jump():
    snap0 = {"captured_at": "2026-07-23T18:00:05Z", "price": 320.0, "holdings": 9.0}
    res0 = _commit(snap0, None, 0, "2026-07-23:9", 0)
    anchor = read_anchor(CFG)
    snap3 = {"captured_at": "2026-07-23T19:30:05Z", "price": 321.0, "holdings": 9.0}
    row3 = compute_row(CFG, snap3, anchor, dna_step=3)
    res3 = commit_final_row(CFG, snap3, anchor, row3,
                            slot_id="2026-07-23:12", market_ordinal=3, clock_mode="market")
    assert res0["version"] == 1 and res3["version"] == 2
    assert row3["DNA step"] == 3
    assert len([k for k in row3 if k != "_meta"]) == 17


def test_committed_row_carries_slot_provenance_outside_the_17_columns():
    snap = {"captured_at": "2026-07-23T18:00:05Z", "price": 320.0, "holdings": 9.0}
    res = _commit(snap, None, 0, "2026-07-23:9", 0)
    doc = FAKE_DB.reference(f"webull_lego_rows/{res['run_id']}").get()
    assert doc["market_slot_id"] == "2026-07-23:9"
    assert doc["market_ordinal"] == 0
    assert doc["clock_mode"] == "market"
    columns = [k for k in compute_row(CFG, snap, None, dna_step=0) if k != "_meta"]
    assert [k for k in doc if k in columns] == columns


def test_legacy_pending_order_in_state_does_not_block_new_row():
    snap0 = {"captured_at": "2026-07-23T18:00:05Z", "price": 320.0, "holdings": 9.0}
    _commit(snap0, None, 0, "2026-07-23:9", 0)
    ck = chain_key(CFG)
    state = FAKE_DB.reference(f"{STATE_PATH}/{ck}").get()
    state["pending_order"] = {"run_id": "old", "status": "SUBMITTED"}
    FAKE_DB.reference(f"{STATE_PATH}/{ck}").set(state)

    anchor = read_anchor(CFG)
    snap1 = {"captured_at": "2026-07-23T18:30:05Z", "price": 321.0, "holdings": 9.0}
    result = _commit(snap1, anchor, 1, "2026-07-23:10", 1)
    assert result["committed"] is True
    assert read_anchor(CFG).dna_step == 1


# --- one commit per slot -----------------------------------------------------

def test_scheduler_retry_cannot_consume_the_same_slot_twice():
    snap0 = {"captured_at": "2026-07-23T18:00:05Z", "price": 320.0, "holdings": 9.0}
    _commit(snap0, None, 0, "2026-07-23:9", 0)
    anchor = read_anchor(CFG)
    # Same slot, later retry: different captured_at/price gives a different run_id.
    snap_retry = {"captured_at": "2026-07-23T18:12:41Z", "price": 320.4, "holdings": 9.0}
    with pytest.raises(SlotAlreadyConsumed):
        _commit(snap_retry, anchor, 0, "2026-07-23:9", 0)
    assert read_anchor(CFG).version == 1


def test_degraded_clock_still_guards_duplicate_slots():
    snap0 = {"captured_at": "2026-07-23T18:00:05Z", "price": 320.0, "holdings": 9.0}
    slot_id = fallback_slot_id(snap0["captured_at"])
    row0 = compute_row(CFG, snap0, None, dna_step=0)
    commit_final_row(CFG, snap0, None, row0, slot_id=slot_id, clock_mode="shadow:degraded")
    anchor = read_anchor(CFG)
    snap_retry = {"captured_at": "2026-07-23T18:20:00Z", "price": 320.4, "holdings": 9.0}
    assert fallback_slot_id(snap_retry["captured_at"]) == slot_id
    row_retry = compute_row(CFG, snap_retry, anchor, dna_step=1)
    with pytest.raises(SlotAlreadyConsumed):
        commit_final_row(CFG, snap_retry, anchor, row_retry,
                         slot_id=slot_id, clock_mode="shadow:degraded")


def test_ordinal_must_move_forward():
    """A later commit may skip ahead but may never replay an older gate."""
    snap0 = {"captured_at": "2026-07-23T18:00:05Z", "price": 320.0, "holdings": 9.0}
    _commit(snap0, None, 2, "2026-07-23:11", 2)
    anchor = read_anchor(CFG)
    snap1 = {"captured_at": "2026-07-23T18:30:05Z", "price": 321.0, "holdings": 9.0}
    with pytest.raises(OrdinalRegression):
        _commit(snap1, anchor, 1, "2026-07-23:10", 1)     # backwards
    with pytest.raises(OrdinalRegression):
        _commit(snap1, anchor, 2, "2026-07-23:12", 2)     # sideways
    state = FAKE_DB.reference(f"{STATE_PATH}/{chain_key(CFG)}").get()
    assert (state["version"], state["market_ordinal"]) == (1, 2)
    assert len(FAKE_DB.reference("webull_lego_rows").get()) == 1   # no orphan row


def test_ordinal_guard_allows_forward_jumps():
    snap0 = {"captured_at": "2026-07-23T18:00:05Z", "price": 320.0, "holdings": 9.0}
    _commit(snap0, None, 0, "2026-07-23:9", 0)
    anchor = read_anchor(CFG)
    snap1 = {"captured_at": "2026-07-23T19:30:05Z", "price": 321.0, "holdings": 9.0}
    assert _commit(snap1, anchor, 3, "2026-07-23:12", 3)["committed"] is True


def test_degraded_commit_does_not_disarm_the_ordinal_guard():
    """A clock-less commit resolves no ordinal, but must not erase the chain's.

    Dropping it would leave the guard off for every later commit — precisely
    when the clock has just proven unreliable.
    """
    snap0 = {"captured_at": "2026-07-23T20:30:05Z", "price": 320.0, "holdings": 9.0}
    _commit(snap0, None, 5, "2026-07-23:14", 5)
    anchor = read_anchor(CFG)
    snap1 = {"captured_at": "2026-07-23T21:00:05Z", "price": 321.0, "holdings": 9.0}
    row = compute_row(CFG, snap1, anchor, dna_step=6)
    result = commit_final_row(CFG, snap1, anchor, row,
                              slot_id=fallback_slot_id(snap1["captured_at"]),
                              clock_mode="shadow:degraded")
    assert result["committed"] is True
    state = FAKE_DB.reference(f"{STATE_PATH}/{chain_key(CFG)}").get()
    assert state["market_ordinal"] == 5          # carried, not dropped

    anchor = read_anchor(CFG)
    snap2 = {"captured_at": "2026-07-23T18:30:05Z", "price": 322.0, "holdings": 9.0}
    with pytest.raises(OrdinalRegression):
        _commit(snap2, anchor, 2, "2026-07-23:11", 2)
    assert read_anchor(CFG).dna_step == 6        # pointer never walked backwards


# --- one calendar for every path ---------------------------------------------

def test_regular_session_predicate_knows_holidays_and_early_closes(monkeypatch):
    assert is_regular_session(datetime(2026, 7, 23, 18, 0, tzinfo=UTC)) is True
    assert is_regular_session(datetime(2026, 7, 23, 12, 0, tzinfo=UTC)) is False   # pre-open
    assert is_regular_session(datetime(2026, 7, 25, 18, 0, tzinfo=UTC)) is False   # Saturday
    # 13:00 ET close on the day after Thanksgiving: 18:30 UTC is already shut.
    assert is_regular_session(datetime(2026, 11, 27, 18, 30, tzinfo=UTC)) is False
    monkeypatch.setenv("LEGO_MARKET_HOLIDAYS", "2026-07-23")
    assert is_regular_session(datetime(2026, 7, 23, 18, 0, tzinfo=UTC)) is False


# ตารางทางการจาก NYSE Group / ICE press release (ปฏิทิน 2026-2027-2028)
# ใส่เป็น literal โดยตั้งใจ: ปฏิทินคือ input ที่กำหนด phase ของ DNA จึงต้องถูก pin
# ไว้ใน repo ไม่ใช่คำนวณซ้ำด้วยกฎชุดเดียวกับที่กำลังตรวจ (จะเห็นด้วยกับตัวเองเสมอ)
OFFICIAL_HOLIDAYS = {
    2026: ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
           "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25"],
    2027: ["2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
           "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"],
    # 1 ม.ค. 2028 ตกวันเสาร์: "no New Year's Day holiday is observed"
    2028: ["2028-01-17", "2028-02-21", "2028-04-14", "2028-05-29", "2028-06-19",
           "2028-07-04", "2028-09-04", "2028-11-23", "2028-12-25"],
}
OFFICIAL_EARLY_CLOSES = {
    2026: ["2026-11-27", "2026-12-24"],
    2027: ["2027-11-26"],
}
# excerpt ทางการที่ยืนยันแล้วไม่ครอบ Dec 2028 จึงเช็ค 2028 แบบ superset
OFFICIAL_EARLY_CLOSES_2028_AT_LEAST = ["2028-07-03", "2028-11-24"]


def _dates(iso_list) -> set[date]:
    return {date.fromisoformat(x) for x in iso_list}


def _early_close_sessions(year: int) -> set[date]:
    """วันที่เปิดเทรดแต่ปิด 13:00 ET — วันหยุดเต็มวันไม่นับ (holiday ชนะ early close)."""
    found, d = set(), date(year, 1, 1)
    while d.year == year:
        bounds = session_bounds(d)
        if bounds and (bounds[1] - bounds[0]).total_seconds() == 3.5 * 3600:
            found.add(d)
        d += timedelta(days=1)
    return found


def test_builtin_calendar_matches_official_nyse_table():
    """ปฏิทิน built-in ต้องตรงตารางทางการ ไม่ใช่แค่ตรงกับกฎของตัวเอง

    guard ที่มีอยู่มองไม่เห็นบั๊กชนิดนี้: fingerprint ไม่เปลี่ยนเพราะกฎไม่ได้ถูกแก้
    และ verify_calendar_continuity คำนวณ ordinal ใหม่ด้วยกฎผิดชุดเดียวกัน
    """
    for year in (2026, 2027, 2028):
        builtin = set(us_market_holidays(year))
        official = _dates(OFFICIAL_HOLIDAYS[year])
        assert builtin - official == set(), f"{year}: มีวันหยุดเกินจากตารางทางการ"
        assert official - builtin == set(), f"{year}: ขาดวันหยุดจากตารางทางการ"

    for year in (2026, 2027):
        builtin = _early_close_sessions(year)
        official = _dates(OFFICIAL_EARLY_CLOSES[year])
        assert builtin - official == set(), f"{year}: มี early close เกินจากตารางทางการ"
        assert official - builtin == set(), f"{year}: ขาด early close จากตารางทางการ"
    assert _dates(OFFICIAL_EARLY_CLOSES_2028_AT_LEAST) <= _early_close_sessions(2028)

    # 1 ม.ค. 2028 ตกวันเสาร์: ไม่มีวันหยุดปีใหม่ และห้ามดัน 31 ธ.ค. 2027 เป็นวันหยุด
    assert not [d for d in us_market_holidays(2028) if d.month == 1 and d.day <= 3]
    assert date(2027, 12, 31) not in us_market_holidays(2027)
    assert session_bounds(date(2027, 12, 31)) is not None
    assert session_slot_count(date(2027, 12, 31), 3600) == 7
    # 4 ก.ค. 2027 ตกวันอาทิตย์: holiday คือจันทร์ 5 ก.ค. และศุกร์ 2 ก.ค. เปิดเต็มวัน
    assert session_slot_count(date(2027, 7, 2), 3600) == 7

    # _observed เดิมต้องยังทำงานกับวันหยุดที่เหลือทุกตัว
    assert date(2027, 6, 18) in us_market_holidays(2027)    # Juneteenth เสาร์ -> ศุกร์
    assert date(2027, 7, 5) in us_market_holidays(2027)     # July 4 อาทิตย์ -> จันทร์
    assert session_bounds(date(2027, 12, 24)) is None       # Christmas observed = ปิดเต็มวัน
    # holiday ชนะ early close: 3 ก.ค. 2026 เข้าเงื่อนไข early close แต่เป็นวันหยุด
    assert session_bounds(date(2026, 7, 3)) is None
    # 1 ม.ค. 2027 เป็นวันศุกร์ 31 ธ.ค. 2026 จึงเปิดปกติอยู่แล้ว — ห้ามพังเคสนี้
    assert session_slot_count(date(2026, 12, 31), 3600) == 7


def test_clock_mode_is_validated_once(monkeypatch):
    monkeypatch.setenv("LEGO_DNA_CLOCK_MODE", "  MARKET ")
    assert clock_mode() == "market"
    monkeypatch.delenv("LEGO_DNA_CLOCK_MODE")
    assert clock_mode() == "shadow"
    monkeypatch.setenv("LEGO_DNA_CLOCK_MODE", "turbo")
    with pytest.raises(MarketClockError):
        clock_mode()


# --- calendar drift ----------------------------------------------------------

def test_declaring_a_new_holiday_fails_closed_instead_of_rephasing(monkeypatch):
    snap0 = {"captured_at": "2026-07-23T18:00:05Z", "price": 320.0, "holdings": 9.0}
    _commit(snap0, None, 0, "2026-07-23:9", 0)
    anchor = read_anchor(CFG)
    monkeypatch.setenv("LEGO_MARKET_HOLIDAYS", "2026-07-22")
    snap1 = {"captured_at": "2026-07-23T18:30:05Z", "price": 321.0, "holdings": 9.0}
    with pytest.raises(CalendarDriftError):
        _commit(snap1, anchor, 1, "2026-07-23:10", 1)


def test_slot_that_no_longer_recomputes_fails_closed():
    snap0 = {"captured_at": "2026-07-23T18:00:05Z", "price": 320.0, "holdings": 9.0}
    _commit(snap0, None, 0, "2026-07-23:9", 0)
    ref = FAKE_DB.reference(f"{STATE_PATH}/{chain_key(CFG)}")
    state = ref.get()
    state["market_ordinal"] = 999          # as if the calendar had shifted the chain
    ref.set(state)
    anchor = read_anchor(CFG)
    snap1 = {"captured_at": "2026-07-23T18:30:05Z", "price": 321.0, "holdings": 9.0}
    with pytest.raises(CalendarDriftError):
        _commit(snap1, anchor, 1, "2026-07-23:10", 1)


def test_engine_only_commit_skips_calendar_guard():
    """Pure-engine callers (no clock) keep working exactly as before."""
    snap0 = {"captured_at": "2026-07-23T18:00:05Z", "price": 320.0, "holdings": 9.0}
    row0 = compute_row(CFG, snap0, None, dna_step=0)
    assert commit_final_row(CFG, snap0, None, row0)["committed"] is True
    assert "calendar_fingerprint" not in FAKE_DB.reference(f"{STATE_PATH}/{chain_key(CFG)}").get()


def test_calendar_fingerprint_tracks_slot_size(monkeypatch):
    before = calendar_fingerprint()
    monkeypatch.setenv("LEGO_SLOT_SECONDS", "3600")
    assert calendar_fingerprint() != before


def test_calendar_fingerprint_ignores_origin_spelling(monkeypatch):
    before = calendar_fingerprint()
    monkeypatch.setenv("LEGO_DNA_ORIGIN_UTC", "2026-07-23T18:00:00+00:00")
    assert calendar_fingerprint() == before


def test_degraded_commit_does_not_pin_a_calendar():
    snap0 = {"captured_at": "2026-07-23T18:00:05Z", "price": 320.0, "holdings": 9.0}
    row0 = compute_row(CFG, snap0, None, dna_step=0)
    commit_final_row(CFG, snap0, None, row0,
                     slot_id=fallback_slot_id(snap0["captured_at"]),
                     clock_mode="shadow:degraded")
    state = FAKE_DB.reference(f"{STATE_PATH}/{chain_key(CFG)}").get()
    assert state["slot_id"].startswith("epoch:")
    assert "calendar_fingerprint" not in state


def test_degraded_commit_keeps_the_calendar_guard_armed(monkeypatch):
    """degraded commit ไม่ปักปฏิทินใหม่ แต่ก็ห้ามลบของเดิมทิ้ง

    ลบทิ้ง = ปลด calendar guard ถาวร: รอบถัดไป stored เป็น None แล้ว skip
    ทั้งที่ slot_id เป็น epoch:* ทำให้ check ตัวที่สอง skip อยู่แล้ว
    """
    snap0 = {"captured_at": "2026-07-23T18:00:05Z", "price": 320.0, "holdings": 9.0}
    _commit(snap0, None, 0, "2026-07-23:9", 0)
    pinned = calendar_fingerprint()
    ref = FAKE_DB.reference(f"{STATE_PATH}/{chain_key(CFG)}")
    assert ref.get()["calendar_fingerprint"] == pinned

    anchor = read_anchor(CFG)
    snap1 = {"captured_at": "2026-07-23T18:30:05Z", "price": 321.0, "holdings": 9.0}
    row1 = compute_row(CFG, snap1, anchor, dna_step=1)
    result = commit_final_row(CFG, snap1, anchor, row1,
                              slot_id=fallback_slot_id(snap1["captured_at"]),
                              clock_mode="shadow:degraded")
    assert result["committed"] is True
    state = ref.get()
    assert state["slot_id"].startswith("epoch:")
    assert state["calendar_fingerprint"] == pinned      # carried forward, ไม่ใช่ None

    monkeypatch.setenv("LEGO_SLOT_SECONDS", "900")
    with pytest.raises(CalendarDriftError):
        verify_calendar_continuity(state)

    # chain ที่เขียนก่อนมี field นี้ต้องยัง commit ได้ ไม่มีอะไรให้เทียบก็ไม่ raise
    monkeypatch.setenv("LEGO_SLOT_SECONDS", "1800")
    FAKE_DB.store.clear()
    snap_legacy = {"captured_at": "2026-07-24T18:00:05Z", "price": 330.0, "holdings": 9.0}
    row_legacy = compute_row(CFG, snap_legacy, None, dna_step=0)
    assert commit_final_row(CFG, snap_legacy, None, row_legacy)["committed"] is True
    legacy_anchor = read_anchor(CFG)
    snap_next = {"captured_at": "2026-07-24T18:30:05Z", "price": 331.0, "holdings": 9.0}
    row_next = compute_row(CFG, snap_next, legacy_anchor, dna_step=1)
    assert commit_final_row(CFG, snap_next, legacy_anchor, row_next,
                            slot_id=fallback_slot_id(snap_next["captured_at"]),
                            clock_mode="shadow:degraded")["committed"] is True
    assert "calendar_fingerprint" not in ref.get()


def _fingerprint_under_rules(version: str) -> str:
    original = market_clock.CALENDAR_RULES_VERSION
    market_clock.CALENDAR_RULES_VERSION = version
    try:
        return calendar_fingerprint()
    finally:
        market_clock.CALENDAR_RULES_VERSION = original


def test_calendar_rules_version_is_part_of_the_fingerprint():
    """bump CALENDAR_RULES_VERSION ต้องทำให้ chain เดิม fail closed ไม่ใช่ผ่านเงียบ ๆ"""
    snap0 = {"captured_at": "2026-07-23T18:00:05Z", "price": 320.0, "holdings": 9.0}
    _commit(snap0, None, 0, "2026-07-23:9", 0)
    ref = FAKE_DB.reference(f"{STATE_PATH}/{chain_key(CFG)}")
    state = ref.get()
    assert state["calendar_fingerprint"] == calendar_fingerprint()

    pinned_under_v1 = _fingerprint_under_rules("2026-07-nyse-v1")
    assert pinned_under_v1 != calendar_fingerprint()   # กฎเปลี่ยนจริง digest ต้องเปลี่ยน
    state["calendar_fingerprint"] = pinned_under_v1    # chain ที่ commit ก่อน bump
    ref.set(state)
    with pytest.raises(CalendarDriftError):
        verify_calendar_continuity(ref.get())


# --- outbox stays independent from the DNA pointer ---------------------------

def test_outbox_supports_multiple_slots_without_overwrite():
    ck = "AAPL_test"
    put_intent(ck, "r1", {"status": "PENDING_DISPATCH", "created_at": "2026-07-23T18:00:00Z"})
    put_intent(ck, "r2", {"status": "PENDING_DISPATCH", "created_at": "2026-07-23T18:30:00Z"})
    assert [x["run_id"] for x in list_actionable(ck)] == ["r1", "r2"]
    update_intent(ck, "r1", {"status": "EXPIRED_UNSENT"})
    assert [x["run_id"] for x in list_actionable(ck)] == ["r2"]


def test_outbox_worker_must_require_committed_source_row():
    assert row_is_committed("missing") is False


def test_outbox_status_normalization_matches_the_broker_normalizer():
    """One normalizer, two defaults: outbox reads a missing status as UNKNOWN.

    A blank-but-present status must stay blank — 'UNKNOWN' routes an intent into
    the broker-reconcile branch, which a whitespace value should never trigger.
    """
    assert outbox_status(None) == "UNKNOWN"
    assert outbox_status("") == "UNKNOWN"
    assert outbox_status(0) == "UNKNOWN"
    assert outbox_status("   ") == ""
    assert outbox_status("partial filled") == broker_status("partial filled")
    assert outbox_status("Submitted") == "SUBMITTED"
