"""Sticky broker-rejection halt, serialized with the account/symbol money fence.

Record BEFORE making an outbox result terminal. A crash then retries the same
run; the retained receipt makes it a no-op. Old callbacks cannot count after
the fence moves to a newer run. No unbounded list of processed orders is needed.

On UAT the halt is a pause, not a verdict: after a cooldown try_half_open lets ONE order
through. If the broker rejects it the halt is back at once with a longer cooldown; if it
fills, the streak is gone. PROD never probes by itself -- a person clears it with reset().
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import uuid

from firebase_admin import db
from lego_orders import TERMINAL_STATUSES, normalize_status
from lego_outbox import DISPATCH_LOCK_PATH, account_symbol_fence_key

MAX_CONSECUTIVE_BROKER_REJECTS = 3
HALT = "BROKER_REJECT_HALT"
HALF_OPEN = "HALF_OPEN"
KEY = "broker_reject_circuit"

# 2026-10-16: three UAT SELLs in a row came back FAILED with no reason (the broker's own
# `positions` call was failing in waves around the same time) and the halt then stood for the
# rest of the session. Every earlier single FAILED (6, 9 and 13 October) was followed by a fill
# on the very next order, 6-16 minutes later, so on UAT the halt is a pause. Decided here and
# not by an environment variable, like cancel_expire: no configuration edit can switch an
# unattended probe on for a real-money account.
AUTO_PROBE_ENVIRONMENTS = frozenset({"UAT"})
PROBE_BASE_SECONDS = 900        # one DNA slot
PROBE_MAX_SECONDS = 14400       # four hours


def _ref(scope):
    return db.reference(f"{DISPATCH_LOCK_PATH}/{scope}")


def status(identity, symbol):
    return (_ref(account_symbol_fence_key(identity, symbol)).get() or {}).get(KEY) or {}


def _count(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _utc(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def probe_cooldown_seconds(streak):
    """900 s after a halt, then 1,800, 3,600, 7,200 and 14,400 after each failed probe."""
    return min(PROBE_MAX_SECONDS, PROBE_BASE_SECONDS * 2 ** min(_count(streak), 8))


def _may_probe(doc, now):
    """Every condition for opening the circuit, read from one fence document. Pure."""
    if not isinstance(doc, dict):
        return False
    circuit = doc.get(KEY)
    if not isinstance(circuit, dict) or circuit.get("halted") is not True or doc.get("inflight_run_id"):
        return False
    stop = doc.get("operator_halt")
    if isinstance(stop, dict) and (stop.get("halted") or stop.get("audit_pending_event")):
        return False
    if doc.get("owner"):
        until = _utc(doc.get("lease_until"))
        if until is None or until > now:
            return False
    halted_at = _utc(circuit.get("halted_at"))
    if halted_at is None:
        return False
    # A halted_at in the future has simply not cooled down yet.
    return now >= halted_at + timedelta(seconds=probe_cooldown_seconds(circuit.get("probe_streak")))


def try_half_open(identity, symbol, *, environment, now):
    """UAT only: after the cooldown, let one order through a standing halt.

    Returns the circuit document when THIS call opened it, else {} (not UAT, not halted,
    still cooling down, or something else owns the fence). The transaction re-reads every
    precondition on the fence document, so it orders itself against a dispatch claim, an
    operator halt and a reset; the plain read before it only spares a write while cooling down.
    An unreadable halted_at keeps the halt.

    Opening sets the counter one short of the limit: the next FAILED/REJECTED brings the
    halt back with a new halt_id (record_outcome counts it as before), a fill clears it. The
    circuit is then armed, not on a clock: if that order is never sent (a preflight block, a
    409), the next order is the probe, and a zero-fill CANCELLED/EXPIRED neither counts nor
    clears -- the same rule the circuit has always had.
    """
    if environment not in AUTO_PROBE_ENVIRONMENTS:       # exactly what config.py stores: "UAT"
        return {}
    if (not identity or not symbol or not isinstance(now, datetime)
            or now.tzinfo is None):
        return {}
    now = now.astimezone(timezone.utc)
    scope = account_symbol_fence_key(identity, symbol)
    if not _may_probe(_ref(scope).get(), now):
        return {}
    token = uuid.uuid4().hex

    def probe(current):
        if not _may_probe(current, now):
            return current
        doc = dict(current)
        circuit = dict(doc[KEY])
        circuit.update(halted=False, status=HALF_OPEN,
                       consecutive_broker_rejects=MAX_CONSECUTIVE_BROKER_REJECTS - 1,
                       probe_id=token, last_probe_at=now.isoformat(),
                       probe_of_halt_id=circuit.get("halt_id"),
                       probe_total=_count(circuit.get("probe_total")) + 1)
        doc[KEY] = circuit
        return doc

    written = _ref(scope).transaction(probe)
    circuit = (written.get(KEY) or {}) if isinstance(written, dict) else {}
    return circuit if circuit.get("probe_id") == token else {}


def record_outcome(intent, summary):
    broker_status = normalize_status(summary.get("broker_status") or summary.get("status"))
    if broker_status not in TERMINAL_STATUSES:
        return {}
    identity, symbol = intent.get("runtime_identity_fingerprint"), intent.get("symbol")
    if not identity or not symbol:
        return {}  # Unbound legacy records cannot alter another account's halt.
    scope = account_symbol_fence_key(identity, symbol)
    run_id = intent["run_id"]
    try:
        qty = Decimal(str(summary.get("filled_quantity", "0")))
        positive_fill = qty.is_finite() and qty > 0
    except (ValueError, InvalidOperation):
        positive_fill = False
    now = datetime.now(timezone.utc).isoformat()

    def record(current):
        doc = dict(current or {})
        circuit = dict(doc.get(KEY) or {})
        if (doc.get("inflight_run_id") != run_id
                or circuit.get("last_outcome_run_id") == run_id):
            return current
        count = int(circuit.get("consecutive_broker_rejects", 0))
        # The order a half-open circuit let through: its outcome decides what comes next.
        probing = circuit.get("status") == HALF_OPEN and not circuit.get("halted")
        cleared = False
        if broker_status in {"FAILED", "REJECTED"}:
            count = min(count + 1, MAX_CONSECUTIVE_BROKER_REJECTS)
        elif positive_fill:
            count = 0
            cleared = True
        circuit.update(last_outcome_run_id=run_id, last_broker_status=broker_status,
                       consecutive_broker_rejects=count, updated_at=now)
        if count >= MAX_CONSECUTIVE_BROKER_REJECTS and not circuit.get("halted"):
            circuit.update(halted=True, status=HALT, halted_at=now,
                           halt_id=run_id)
            if probing:
                circuit["probe_streak"] = _count(circuit.get("probe_streak")) + 1
        elif cleared and probing:
            circuit.update(status="READY", probe_streak=0)
        doc[KEY] = circuit
        return doc

    return (_ref(scope).transaction(record) or {}).get(KEY) or {}


def reset(identity, symbol, *, expected_halt_id, reason, apply=False):
    """Compare-and-reset while no dispatch owner or unresolved order exists."""
    if not str(reason).strip():
        raise ValueError("operator reason is required")
    scope = account_symbol_fence_key(identity, symbol)
    now = datetime.now(timezone.utc)
    reset_id = uuid.uuid4().hex

    def validate(doc):
        circuit = doc.get(KEY) or {}
        if circuit.get("status") == HALF_OPEN and not circuit.get("halted"):
            raise ValueError("circuit is half-open, not halted: the next order is the probe; nothing to reset")
        if not circuit.get("halted") or circuit.get("halt_id") != expected_halt_id:
            raise ValueError("halt changed or absent; inspect status again")
        if doc.get("inflight_run_id"):
            raise ValueError("unresolved order fence; reconcile before reset")
        if doc.get("owner"):
            try:
                until = datetime.fromisoformat(str(doc.get("lease_until")).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                raise ValueError("dispatch lease is unreadable; reset refused")
            if until > now:
                raise ValueError("dispatch worker active; pause and wait before reset")
        return circuit

    current = _ref(scope).get() or {}
    validate(current)
    if not apply:
        return {"dry_run": True, "scope": scope, "halt_id": expected_halt_id}

    def clear(old):
        doc = dict(old or {})
        circuit = dict(validate(doc))
        from security_text import redact_sensitive_text
        circuit.update(halted=False, status="READY", consecutive_broker_rejects=0,
                       probe_streak=0, reset_at=now.isoformat(), reset_id=reset_id,
                       reset_reason=redact_sensitive_text(reason)[:500])
        doc[KEY] = circuit
        return doc

    written = _ref(scope).transaction(clear)
    return {"dry_run": False, "scope": scope, "reset_id": written[KEY]["reset_id"]}
