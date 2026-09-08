"""Canonical regular-session market clock for time-trained DNA.

The clock is deterministic and independent from broker/order state. It supports
NYSE regular sessions, observed holidays, common early closes, and an explicit
origin. Production must use the same calendar/session rules as DNA training.
"""
from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
UTC = timezone.utc

# DNA is trained on yfinance bars (index-to-index), so production slots must use
# a timeframe the trainer can emit. 10m has no counterpart and is rejected.
ALLOWED_SLOT_SECONDS = {900: "15m", 1800: "30m", 3600: "1h",
                        14400: "4h", 86400: "1d"}
# Bump when the built-in holiday/early-close rules change; it re-keys the
# calendar fingerprint so an existing chain fails closed instead of re-phasing.
CALENDAR_RULES_VERSION = "2026-07-nyse-v2"
CLOCK_MODES = ("shadow", "market", "legacy")


class MarketClockError(RuntimeError):
    pass


@dataclass(frozen=True)
class MarketSlot:
    slot_id: str
    session_date: date
    session_open_utc: datetime
    session_close_utc: datetime
    slot_start_utc: datetime
    slot_end_utc: datetime
    slot_in_session: int
    market_ordinal: int


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        d = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _easter_sunday(year: int) -> date:
    # Anonymous Gregorian computus.
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _new_year_observed(d: date) -> date | None:
    """Jan 1 อาทิตย์ -> จันทร์ 2; Jan 1 เสาร์ -> ไม่ observe เลย

    NYSE Rule 7.2 ยกเว้นกรณี 'unusual business conditions such as the ending of a
    monthly or yearly accounting period' — 31 ธ.ค. เป็นวันสิ้นเดือน/ไตรมาส/ปี จึง
    เป็นวันเทรดเต็มวัน คืน Dec 31 เป็นวันหยุดจะทำให้ N_j = 0 และ ordinal เลื่อน
    ถาวรเทียบ bar index ที่เทรนมา โดยที่ fingerprint guard จับไม่ได้
    """
    if d.weekday() == 5:
        return None
    return d + timedelta(days=1) if d.weekday() == 6 else d


@lru_cache(maxsize=64)
def _holiday_set(year: int, declared: str) -> frozenset[date]:
    """Cached per (year, declared holidays) so the cache can never go stale.

    Keying on the year alone would keep serving the old calendar after
    LEGO_MARKET_HOLIDAYS changes, which is exactly the silent re-phasing the
    fingerprint guard exists to prevent.
    """
    holidays = {
        # New Year ใช้กฎเฉพาะของมัน (ดู _new_year_observed) ไม่ใช้ _observed
        _nth_weekday(year, 1, 0, 3),       # MLK
        _nth_weekday(year, 2, 0, 3),       # Presidents Day
        _easter_sunday(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),         # Memorial Day
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),       # Labor Day
        _nth_weekday(year, 11, 3, 4),      # Thanksgiving
        _observed(date(year, 12, 25)),
    }
    if year >= 2022:
        holidays.add(_observed(date(year, 6, 19)))  # Juneteenth
    ny = _new_year_observed(date(year, 1, 1))
    if ny is not None:
        holidays.add(ny)
    for raw in declared.split(","):
        raw = raw.strip()
        if raw:
            holidays.add(date.fromisoformat(raw))
    return frozenset(holidays)


def us_market_holidays(year: int) -> frozenset[date]:
    return _holiday_set(year, os.environ.get("LEGO_MARKET_HOLIDAYS", ""))


def _early_close(d: date) -> bool:
    # Day after Thanksgiving.
    thanksgiving = _nth_weekday(d.year, 11, 3, 4)
    if d == thanksgiving + timedelta(days=1):
        return True
    # Christmas Eve when it is a weekday and not itself an observed holiday.
    if d.month == 12 and d.day == 24 and d.weekday() < 5:
        return True
    # July 3 เมื่อเป็นวันทำการ. ไม่มีเคส July 2: ถ้า July 4 ตกวันอาทิตย์ holiday
    # จริงคือจันทร์ 5 ก.ค. และปฏิทินทางการไม่มี early close เดือน ก.ค. ปีนั้น
    if d.month == 7 and d.day == 3 and d.weekday() < 5:
        return True
    extra = os.environ.get("LEGO_MARKET_EARLY_CLOSES", "")
    return d.isoformat() in {x.strip() for x in extra.split(",") if x.strip()}


def session_bounds(session_date: date) -> tuple[datetime, datetime] | None:
    if session_date.weekday() >= 5 or session_date in us_market_holidays(session_date.year):
        return None
    open_ny = datetime.combine(session_date, time(9, 30), tzinfo=NY)
    close_ny = datetime.combine(session_date, time(13 if _early_close(session_date) else 16, 0), tzinfo=NY)
    return open_ny.astimezone(UTC), close_ny.astimezone(UTC)


def is_regular_session(at: datetime | None = None) -> bool:
    """One calendar for the whole pipeline: is *at* inside a regular session?

    Same predicate resolve_market_slot applies, minus the slot/origin lookup, so
    it never raises. Holidays and early closes therefore also hold on the
    degraded path where no ordinal can be resolved.
    """
    at = (at or datetime.now(UTC)).astimezone(UTC)
    bounds = session_bounds(at.astimezone(NY).date())
    return bounds is not None and bounds[0] <= at < bounds[1]


def clock_mode() -> str:
    """Single reader for LEGO_DNA_CLOCK_MODE — every caller must go through it."""
    mode = os.environ.get("LEGO_DNA_CLOCK_MODE", "shadow").strip().lower()
    if mode not in CLOCK_MODES:
        raise MarketClockError("LEGO_DNA_CLOCK_MODE ต้องเป็น shadow|market|legacy")
    return mode


def _parse_origin() -> datetime:
    raw = os.environ.get("LEGO_DNA_ORIGIN_UTC", "").strip()
    if not raw:
        raise MarketClockError("ต้องตั้ง LEGO_DNA_ORIGIN_UTC เช่น 2026-07-23T18:00:00Z")
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)


def slot_seconds() -> int:
    """Mandatory and validated: the grid must match the trained yfinance bars."""
    raw = os.environ.get("LEGO_SLOT_SECONDS", "").strip()
    if not raw:
        allowed = ", ".join(f"{s} ({n})" for s, n in sorted(ALLOWED_SLOT_SECONDS.items()))
        raise MarketClockError(f"ต้องตั้ง LEGO_SLOT_SECONDS ให้ตรง timeframe ที่เทรน DNA: {allowed}")
    try:
        sec = int(raw)
    except ValueError as exc:
        raise MarketClockError(f"LEGO_SLOT_SECONDS ไม่ใช่จำนวนเต็ม: {raw!r}") from exc
    if sec not in ALLOWED_SLOT_SECONDS:
        allowed = ", ".join(f"{s} ({n})" for s, n in sorted(ALLOWED_SLOT_SECONDS.items()))
        raise MarketClockError(f"LEGO_SLOT_SECONDS={sec} ไม่รองรับ ต้องเป็น {allowed}")
    return sec


def session_slot_count(d: date, sec: int) -> int:
    """Count bars the way yfinance emits them: a partial trailing bar counts.

    Floor division silently dropped the 15:30-16:00 half bar on 1h and made 1d
    zero, which drifts the ordinal against the trained bar index every session.
    Public because find_origin needs the same count; this is the only place the
    rule may be written.
    """
    bounds = session_bounds(d)
    if not bounds:
        return 0
    start, end = bounds
    return math.ceil((end - start).total_seconds() / sec)


def _ordinal_from_origin(origin: datetime, slot_start: datetime, sec: int) -> int:
    if slot_start < origin:
        raise MarketClockError("เวลาปัจจุบันอยู่ก่อน LEGO_DNA_ORIGIN_UTC")
    origin_date = origin.astimezone(NY).date()
    slot_date = slot_start.astimezone(NY).date()
    origin_bounds = session_bounds(origin_date)
    slot_bounds = session_bounds(slot_date)
    if not origin_bounds or not slot_bounds:
        raise MarketClockError("origin หรือ slot ไม่อยู่ใน regular market session")
    origin_open, _ = origin_bounds
    slot_open, _ = slot_bounds
    origin_index = int((origin - origin_open).total_seconds()) // sec
    slot_index = int((slot_start - slot_open).total_seconds()) // sec
    total = -origin_index + slot_index
    d = origin_date
    while d < slot_date:
        total += session_slot_count(d, sec)
        d += timedelta(days=1)
    return total


def calendar_fingerprint() -> str:
    """Digest of every input that can shift a market ordinal.

    Stored on the chain at genesis. A changed slot size, a newly declared
    holiday, or a rules bump changes this digest, so the chain fails closed
    instead of silently trading a re-phased gate array.
    """
    raw_origin = os.environ.get("LEGO_DNA_ORIGIN_UTC", "").strip()
    try:
        # Compare the instant, not the spelling: '...T18:00:00Z' and
        # '...T18:00:00+00:00' are the same origin and must not re-key a chain.
        origin_key = _parse_origin().strftime("%Y-%m-%dT%H:%M:%SZ")
    except (MarketClockError, ValueError):
        origin_key = raw_origin
    payload = "|".join([
        CALENDAR_RULES_VERSION,
        str(slot_seconds()),
        origin_key,
        ",".join(sorted(x.strip() for x in os.environ.get("LEGO_MARKET_HOLIDAYS", "").split(",") if x.strip())),
        ",".join(sorted(x.strip() for x in os.environ.get("LEGO_MARKET_EARLY_CLOSES", "").split(",") if x.strip())),
    ])
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def market_ordinal_for_slot_id(slot_id: str) -> int:
    """Recompute the ordinal of an already-committed slot id.

    Used to detect calendar drift: if a past slot no longer resolves to the
    ordinal it was committed with, the calendar changed underneath the chain.
    """
    raw = str(slot_id)
    session_raw, _, index_raw = raw.rpartition(":")
    if not session_raw or not index_raw.lstrip("-").isdigit():
        raise MarketClockError(f"slot_id ไม่ถูกรูปแบบ: {slot_id!r}")
    sec = slot_seconds()
    try:
        session_date = date.fromisoformat(session_raw)
    except ValueError as exc:
        raise MarketClockError(f"slot_id ไม่ถูกรูปแบบ: {slot_id!r}") from exc
    bounds = session_bounds(session_date)
    if not bounds:
        raise MarketClockError(f"slot_id {slot_id!r} ไม่ตรงกับวันทำการของปฏิทินปัจจุบัน")
    session_open, _ = bounds
    slot_start = session_open + timedelta(seconds=int(index_raw) * sec)
    return _ordinal_from_origin(_parse_origin(), slot_start, sec)


def fallback_slot_id(captured_at: str) -> str:
    """Degraded slot key used only when the market clock is unavailable.

    Namespaced with 'epoch:' so it can never collide with a canonical
    '<session-date>:<index>' id, while still keeping one-commit-per-slot.
    """
    dt = datetime.strptime(str(captured_at), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return f"epoch:{int(dt.timestamp()) // slot_seconds()}"


def resolve_market_slot(at: datetime | None = None) -> MarketSlot | None:
    """Return the canonical slot containing *at*, or None outside regular hours."""
    at = (at or datetime.now(UTC)).astimezone(UTC)
    sec = slot_seconds()
    session_date = at.astimezone(NY).date()
    bounds = session_bounds(session_date)
    if not bounds:
        return None
    session_open, session_close = bounds
    if not (session_open <= at < session_close):
        return None
    slot_index = int((at - session_open).total_seconds()) // sec
    slot_start = session_open + timedelta(seconds=slot_index * sec)
    slot_end = min(slot_start + timedelta(seconds=sec), session_close)
    origin = _parse_origin()
    ordinal = _ordinal_from_origin(origin, slot_start, sec)
    return MarketSlot(
        slot_id=f"{session_date.isoformat()}:{slot_index}",
        session_date=session_date,
        session_open_utc=session_open,
        session_close_utc=session_close,
        slot_start_utc=slot_start,
        slot_end_utc=slot_end,
        slot_in_session=slot_index,
        market_ordinal=ordinal,
    )


def resolve_dna_step(legacy_step: int, slot: MarketSlot) -> tuple[int, int]:
    """Return (effective_step, alignment_error).

    shadow: preserve legacy output and only report alignment.
    market: market ordinal is authoritative.
    legacy: old behavior, provided only for rollback.
    """
    mode = clock_mode()
    error = legacy_step - slot.market_ordinal
    return (slot.market_ordinal if mode == "market" else legacy_step), error

