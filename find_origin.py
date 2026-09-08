"""Find LEGO_DNA_ORIGIN_UTC before switching LEGO_DNA_CLOCK_MODE to 'market'.

The DNA index is a bar index from training, so production must satisfy

    market_ordinal(t)  ==  bar_index ที่ DNA เทรนมา

market_ordinal counts slots forward from the origin, so the origin is simply the
start of the slot N market slots before the slot you are about to commit.
Walking back must follow the same calendar the engine uses — overnight gaps,
weekends, holidays, and early closes are worth zero slots — so this module reuses
market_clock's session rules instead of doing its own date math.

Deliberately origin-free: it only needs LEGO_SLOT_SECONDS, because it is the tool
you run when LEGO_DNA_ORIGIN_UTC does not exist yet.

    export LEGO_SLOT_SECONDS=1800
    python find_origin.py <dna_step ที่อยากให้แถวถัดไปเป็น>

ตัวอย่าง: chain ที่ anchor อยู่ dna_step 41 ต้องการให้แถวถัดไปเป็น 42 -> รัน
`python find_origin.py 42` แล้ว export ค่าที่พิมพ์ออกมา
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta

from market_clock import (NY, UTC, MarketClockError, session_bounds,
                          session_slot_count, slot_seconds)


def current_slot(at: datetime | None = None) -> tuple[date, int, datetime]:
    """(session_date, slot_in_session, slot_start_utc) of the slot holding *at*.

    Same grid resolve_market_slot uses, without the ordinal — and therefore
    without needing an origin.
    """
    at = (at or datetime.now(UTC)).astimezone(UTC)
    session_date = at.astimezone(NY).date()
    bounds = session_bounds(session_date)
    if bounds is None or not (bounds[0] <= at < bounds[1]):
        raise MarketClockError("เวลาที่ให้มาไม่อยู่ในเวลาเทรดปกติ — เลือกเวลาในเซสชัน")
    sec = slot_seconds()
    index = int((at - bounds[0]).total_seconds()) // sec
    return session_date, index, bounds[0] + timedelta(seconds=index * sec)


def last_slot_index(session_date: date) -> int:
    """Index of the final bar of a session.

    The trailing-partial-bar rule lives in market_clock and nowhere else; a
    second copy of it here is how the ordinal drifts away from the trained bars.
    """
    slots = session_slot_count(session_date, slot_seconds())
    if not slots:
        raise MarketClockError(f"{session_date} ไม่ใช่วันทำการ")
    return slots - 1


def walk_back(n: int, at: datetime | None = None) -> datetime:
    """UTC start of the slot *n* market slots before the slot holding *at*.

    n = 0 returns the current slot's own start, so setting the result as
    LEGO_DNA_ORIGIN_UTC makes the next commit land on DNA step n.
    """
    if type(n) is not int or n < 0:
        raise ValueError("n ต้องเป็นจำนวนเต็ม >= 0")
    session_date, index, _ = current_slot(at)
    remaining = n
    # A 20-year bound only stops a malformed calendar from looping forever; no
    # DNA is anywhere near that long.
    for _ in range(20 * 365):
        if remaining <= index:
            open_utc, _close = session_bounds(session_date)
            return open_utc + timedelta(seconds=(index - remaining) * slot_seconds())
        remaining -= index + 1
        session_date = _previous_session(session_date)
        index = last_slot_index(session_date)
    raise MarketClockError("ถอยหลังเกินปฏิทินที่รองรับ")


def _previous_session(session_date: date) -> date:
    day = session_date - timedelta(days=1)
    while session_bounds(day) is None:
        day -= timedelta(days=1)
        if day.year < 1990:
            raise MarketClockError("ถอยหลังเกินปฏิทินที่รองรับ")
    return day


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].isdigit():
        print("usage: python find_origin.py <dna_step ที่อยากให้แถวถัดไปเป็น>")
        print("ต้องตั้ง LEGO_SLOT_SECONDS ให้ตรง timeframe ที่เทรน DNA ก่อน")
        return 2
    try:
        target_step = int(argv[1])
        session_date, index, slot_start = current_slot()
        origin = walk_back(target_step)
    except (MarketClockError, ValueError) as exc:
        print(f"หาไม่ได้: {exc}")
        return 1

    stamp = origin.strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"LEGO_SLOT_SECONDS = {slot_seconds()}")
    print(f"slot ปัจจุบัน      = {session_date.isoformat()}:{index} "
          f"(เริ่ม {slot_start:%Y-%m-%dT%H:%M:%SZ})")
    print("ตั้งค่าเป็น:")
    print(f"  LEGO_DNA_ORIGIN_UTC={stamp}")
    print("  LEGO_DNA_CLOCK_MODE=market")
    print(f"-> แถวถัดไปจะได้ DNA step = {target_step} "
          f"(slot {origin.astimezone(NY):%Y-%m-%d %H:%M} ET คือ step 0)")
    print("เตือน: แก้ origin / slot size / วันหยุด หลัง commit แรกแล้ว = CALENDAR_DRIFT "
          "ต้องเริ่ม chain ใหม่")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

