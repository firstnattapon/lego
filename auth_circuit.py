"""Durable authentication cooldown; no credentials or broker payloads stored."""
from datetime import datetime, timezone
import time

from firebase_admin import db
import tick_runtime


class AuthCircuitOpen(RuntimeError):
    def __init__(self, state):
        self.state = state
        super().__init__("broker authentication paused; check credentials and retry_after")


def _reference(key):
    return db.reference(f"webull_lego_warnings/auth_failure_{key}")


def status(key):
    return _reference(key).get() or {}


def guard(key):
    """Permit one request owner to probe after cooldown, including cold starts."""
    current = status(key)
    if not current.get("active"):
        return
    now = time.time()
    owner = tick_runtime.correlation_id()
    if now < current.get("retry_after", 0):
        raise AuthCircuitOpen(current)

    def claim(old):
        old = old or {}
        if not old.get("active"):
            return old
        if (now < old.get("retry_after", 0)
                or (now < old.get("probe_until", 0) and old.get("probe_owner") != owner)):
            return old
        return {**old, "probe_owner": owner, "probe_until": now + 45}

    current = _reference(key).transaction(claim)
    if current.get("active") and (not owner or current.get("probe_owner") != owner
                                  or now < current.get("retry_after", 0)):
        raise AuthCircuitOpen(current)


def failed(key):
    now = time.time()
    stamp = datetime.fromtimestamp(now, timezone.utc).isoformat()

    def record(old):
        old = old or {}
        count = min(int(old.get("consecutive_failures", 0)) + 1, 1000000)
        delay = min(1800, 60 * 2 ** min(count - 1, 5))
        return {"active": True, "kind": "AUTH_BACKOFF",
                "message": "Broker authentication failed; verify environment, credentials and token",
                "count": min(int(old.get("count", 0)) + 1, 1000000000),
                "consecutive_failures": count, "first_at": old.get("first_at", stamp),
                "last_at": stamp, "retry_after": now + delay,
                "probe_until": 0, "probe_owner": ""}

    return _reference(key).transaction(record)


def succeeded(key):
    """Only the current probe may clear a newer failure; preserve history."""
    current = status(key)
    if not current.get("active"):
        return
    owner = tick_runtime.correlation_id()
    def clear(old):
        if not old or old.get("probe_owner") != owner or not owner:
            return old
        return {**old, "active": False, "consecutive_failures": 0,
                "retry_after": 0, "probe_until": 0, "probe_owner": "",
                "resolved_at": datetime.now(timezone.utc).isoformat()}
    _reference(key).transaction(clear)
