"""Request-local time budget and correlation, shared by adapters and services."""
from contextlib import contextmanager
from contextvars import ContextVar
import time

_deadline = ContextVar("lego_tick_deadline", default=None)
_correlation = ContextVar("lego_tick_correlation", default=None)


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
def tick_scope(identifier: str, seconds: float = 35.0):
    deadline_token = _deadline.set(time.monotonic() + seconds)
    correlation_token = _correlation.set(identifier)
    try:
        yield
    finally:
        _correlation.reset(correlation_token)
        _deadline.reset(deadline_token)
