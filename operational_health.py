"""Pure horizon reporting; no calendar changes, token rotation or renewal."""
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from dna_engine import decode_dna
from market_clock import NY, session_bounds, session_slot_count, calendar_fingerprint

# Warn two days ahead. A window that ends over a weekend has to page before the
# operator leaves on Friday; the previous 24 hours gave a Sunday-night expiry no
# working-hours warning at all.
RELEASE_WARNING_SECONDS = 172800
_MAX_CALENDAR_DAYS = 4000


def regular_sessions(start):
    """(open, close) of each regular session, from *start*'s New York date onward."""
    day = start.astimezone(NY).date()
    for _ in range(_MAX_CALENDAR_DAYS):
        bounds = session_bounds(day)
        if bounds:
            yield bounds
        day += timedelta(days=1)


def complete_sessions_between(start, end):
    """Regular sessions that open at/after *start* and close at/before *end*."""
    count = 0
    for opened, closed in regular_sessions(start):
        if opened >= end:
            break
        if opened >= start and closed <= end:
            count += 1
    return count


@lru_cache(maxsize=32)
def dna_end(origin_text, length, interval, calendar_hash):
    origin = datetime.fromisoformat(origin_text.replace("Z", "+00:00"))
    if origin.tzinfo is None:
        raise ValueError("DNA origin requires timezone")
    day, remaining = origin.astimezone(NY).date(), length
    for _ in range(40000):
        bounds = session_bounds(day)
        if bounds:
            start, close = max(origin, bounds[0]), bounds[1]
            slots = max(0, int((close - start).total_seconds() + interval - 1) // interval)
            if remaining <= slots:
                return min(close, start + timedelta(seconds=remaining * interval)).isoformat()
            remaining -= slots
        day += timedelta(days=1)
    raise ValueError("DNA horizon outside supported range")


def report(runtime, decision, token, *, now=None):
    now = now or datetime.now(timezone.utc)
    bundle = runtime.operator.dna_bundle
    day, needed, sessions = now.astimezone(NY).date(), 0, 0
    while sessions < 2:
        day += timedelta(days=1)
        count = session_slot_count(day, bundle.interval_seconds)
        if count:
            needed += count
            sessions += 1
    remaining = decision.get("dna_steps_remaining")
    result = {"two_session_slots": needed, "dna_slots_remaining": remaining,
              "dna_low": type(remaining) is int and remaining < needed,
              "token_warning": bool(token.get("expiry_warning"))}
    if bundle.origin_utc:
        result["dna_end_utc"] = dna_end(bundle.origin_utc, len(decode_dna(bundle.dna_code)),
                                        bundle.interval_seconds, calendar_fingerprint())
        result["dna_exhausted"] = now >= datetime.fromisoformat(result["dna_end_utc"])
    raw = runtime.deployment.execution_limits[3]
    if raw:
        end = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        seconds = (end - now).total_seconds()
        result.update(release_expiry_utc=end.isoformat(),
                      release_seconds_remaining=seconds,
                      release_expired=seconds <= 0,
                      release_expiring=seconds <= RELEASE_WARNING_SECONDS,
                      release_sessions_remaining=(
                          complete_sessions_between(now, end) if seconds > 0 else 0))
    # trade+active that still cannot send an order because the release binding or
    # the PROD acknowledgement does not match: the deployment looks live and is not.
    # getattr: report() is also driven by minimal duck-typed runtimes that carry no gate.
    intends_orders = getattr(getattr(runtime, "operator", None), "allows_new_intents", False)
    result["orders_blocked_by_release"] = bool(
        intends_orders and getattr(runtime, "allows_new_broker_mutation", None) is False)
    return result
