"""Request-local time budget and correlation, shared by adapters and services."""
from contextlib import contextmanager
from contextvars import ContextVar
import time
import json
import os

_deadline = ContextVar("lego_tick_deadline", default=None)
_correlation = ContextVar("lego_tick_correlation", default=None)
_witness = ContextVar("lego_tick_witness", default="none")


class TickDeadlineExceeded(TimeoutError):
    """Stop starting work; an already attempted order still needs recovery."""


def remaining() -> float | None:
    end = _deadline.get()
    return None if end is None else max(0.0, end - time.monotonic())


def require_budget(minimum: float = 2.0) -> None:
    left = remaining()
    if left is not None and left < minimum:
        raise TickDeadlineExceeded("tick time budget exhausted; resume recovery next tick")


def correlation_id() -> str | None:
    return _correlation.get()


@contextmanager
def phase(operation: str, *, witness: str | None = None):
    """No payloads or error messages: only timing and durable-boundary state."""
    start = time.monotonic()
    witness_token = _witness.set(witness if witness is not None else _witness.get())
    outcome = "ok"
    try:
        yield
    except Exception:
        outcome = "error"
        raise
    finally:
        left = remaining()
        print(json.dumps({
            "event": "lego_operation", "phase": operation, "operation": operation,
            "witness": _witness.get(), "outcome": outcome, "severity": "INFO",
            "candidate_hash": os.environ.get("LEGO_CANDIDATE_HASH"),
            "revision": os.environ.get("K_REVISION"),
            "correlation_id": correlation_id(),
            "duration_ms": round((time.monotonic() - start) * 1000, 3),
            "remaining_budget_ms": None if left is None else round(left * 1000, 3),
        }), flush=True)
        _witness.reset(witness_token)


@contextmanager
def tick_scope(identifier: str, seconds: float = 35.0):
    deadline_token = _deadline.set(time.monotonic() + seconds)
    correlation_token = _correlation.set(identifier)
    try:
        yield
    finally:
        _correlation.reset(correlation_token)
        _deadline.reset(deadline_token)
