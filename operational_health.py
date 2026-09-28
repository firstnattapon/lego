"""Pure horizon reporting; no calendar changes, token rotation or renewal."""
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from dna_engine import decode_dna
from market_clock import NY, session_bounds, session_slot_count, calendar_fingerprint


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
    raw = runtime.deployment.execution_limits[3]
    if raw:
        end = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        result.update(release_expiry_utc=end.isoformat(),
                      release_expiring=(end - now).total_seconds() <= 86400)
    return result
