"""Request-local time budget and correlation, shared by adapters and services."""
from contextlib import contextmanager
from contextvars import ContextVar
import time
import json
import os
import re

_deadline = ContextVar("lego_tick_deadline", default=None)
_correlation = ContextVar("lego_tick_correlation", default=None)
_witness = ContextVar("lego_tick_witness", default="none")
_annotations = ContextVar("lego_phase_annotations", default=None)

_REQUEST_ID = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{5,63}$")
_ERROR_CODE = re.compile(r"^[A-Z0-9_]{1,64}$")


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


def annotate(**fields) -> None:
    """Attach request id / HTTP status / error code to the phase that is open right now.

    Metadata only, and validated: a phase event must stay free of payloads and messages.
    Outside a phase this does nothing.
    """
    current = _annotations.get()
    if current is None:
        return
    request_id = fields.get("request_id")
    if isinstance(request_id, str) and _REQUEST_ID.match(request_id):
        current["request_id"] = request_id
    status = fields.get("http_status")
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        current["http_status"] = status
    code = fields.get("error_code")
    if isinstance(code, str) and _ERROR_CODE.match(code.strip().upper()):
        current["error_code"] = code.strip().upper()


@contextmanager
def phase(operation: str, *, witness: str | None = None):
    """No payloads or error messages: only timing and durable-boundary state."""
    start = time.monotonic()
    witness_token = _witness.set(witness if witness is not None else _witness.get())
    annotations: dict = {}
    annotation_token = _annotations.set(annotations)
    outcome = "ok"
    try:
        yield
    except Exception:
        outcome = "error"
        raise
    finally:
        left = remaining()
        duration_ms = round((time.monotonic() - start) * 1000, 3)
        remaining_ms = None if left is None else round(left * 1000, 3)
        print(json.dumps({
            "event": "lego_operation", "phase": operation, "operation": operation,
            "witness": _witness.get(), "outcome": outcome, "severity": "INFO",
            "candidate_hash": os.environ.get("LEGO_CANDIDATE_HASH"),
            "revision": os.environ.get("K_REVISION"),
            "correlation_id": correlation_id(),
            "duration_ms": duration_ms,
            "remaining_budget_ms": remaining_ms,
            **annotations,
        }), flush=True)
        try:  # the flight recorder is best effort and must never alter a phase
            import flight_recorder
            flight_recorder.phase(operation, _witness.get(), outcome, duration_ms,
                                  remaining_ms, annotations)
        except Exception:
            pass
        _annotations.reset(annotation_token)
        _witness.reset(witness_token)


@contextmanager
def tick_scope(identifier: str, seconds: float = 35.0):
    deadline_token = _deadline.set(time.monotonic() + seconds)
    correlation_token = _correlation.set(identifier)
    trace_token = None
    try:
        try:  # one forensic trace per tick; failure to start it changes nothing
            import flight_recorder
            trace_token = flight_recorder.begin(identifier, budget=remaining)
        except Exception:
            trace_token = None
        yield
    finally:
        if trace_token is not None:
            try:
                import flight_recorder
                flight_recorder.reset(trace_token)
            except Exception:
                pass
        _correlation.reset(correlation_token)
        _deadline.reset(deadline_token)
