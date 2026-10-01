"""Durable snapshot backoff, with a fenced probe across Cloud Run instances.

This gate only protects quote reads. Order detail, fills, fees and reconciliation
remain available. Never cache a quote or replace broker time with response time.
"""
import hashlib
import time
import uuid
from datetime import datetime, timezone

from firebase_admin import db

import tick_runtime

BACKOFF = "MARKET_DATA_BACKOFF"
PROBE_SECONDS = 45


class MarketDataCircuitOpen(RuntimeError):
    def __init__(self, state):
        self.state = state
        super().__init__("snapshot unavailable; wait for retry_after")

    def response(self):
        return {"pipeline_status": BACKOFF, "committed": False,
                "retry_after": self.state.get("retry_after", 0),
                "probe_until": self.state.get("probe_until", 0),
                "broker_error": self.state.get("broker_error", {})}


def scope(identity, symbol, category):
    return hashlib.sha256(f"{identity}\0{symbol.upper()}\0{category}".encode()).hexdigest()


def _reference(key):
    return db.reference(f"webull_lego_market_data_circuit/{key}")


def guard(key):
    """Skip account/capability reads when no snapshot probe can run yet."""
    state = _reference(key).get() or {}
    now = time.time()
    if now < max(state.get("retry_after", 0), state.get("probe_until", 0)):
        raise MarketDataCircuitOpen(state)


def run(key, fn, *, is_transient, error_details):
    """One snapshot attempt; elapsed cooldown permits one atomic probe owner."""
    tick_runtime.require_budget()
    now, owner = time.time(), uuid.uuid4().hex
    ref = _reference(key)

    def claim(old):
        old = old or {}
        if now < max(old.get("retry_after", 0), old.get("probe_until", 0)):
            return old
        return {**old, "probe_owner": owner, "probe_until": now + PROBE_SECONDS}

    state = ref.transaction(claim)
    if state.get("probe_owner") != owner:
        raise MarketDataCircuitOpen(state)

    try:
        result = fn()
    except Exception as exc:
        transient = (not isinstance(exc, tick_runtime.TickDeadlineExceeded)
                     and is_transient(exc))
        stamp = datetime.now(timezone.utc).isoformat()

        def failed(old):
            if (not old or old.get("probe_owner") != owner
                    or time.time() >= old.get("probe_until", 0)):
                return old
            released = {**old, "probe_owner": "", "probe_until": 0}
            if not transient:
                return released
            count = min(int(old.get("consecutive_failures", 0)) + 1, 1000000)
            delay = min(1800, 60 * 2 ** min(count - 1, 5))
            return {**released, "active": True, "kind": BACKOFF,
                    "consecutive_failures": count,
                    "first_at": old.get("first_at", stamp), "last_at": stamp,
                    "retry_after": time.time() + delay,
                    "broker_error": error_details(exc)}

        state = ref.transaction(failed) or {}
        if transient:
            raise MarketDataCircuitOpen(state) from exc
        raise

    def succeeded(old):
        # An expired probe's late success cannot clear a successor's failure.
        if (not old or old.get("probe_owner") != owner
                or time.time() >= old.get("probe_until", 0)):
            return old
        return {**old, "active": False, "consecutive_failures": 0,
                "retry_after": 0, "probe_owner": "", "probe_until": 0,
                "resolved_at": datetime.now(timezone.utc).isoformat()}

    state = ref.transaction(succeeded) or {}
    # A result whose lease expired is not evidence for the current owner.
    if state.get("active") or state.get("probe_owner") or time.time() >= now + PROBE_SECONDS:
        raise MarketDataCircuitOpen(state)
    return result
