"""Sticky broker-rejection halt, serialized with the account/symbol money fence.

Record BEFORE making an outbox result terminal. A crash then retries the same
run; the retained receipt makes it a no-op. Old callbacks cannot count after
the fence moves to a newer run. No unbounded list of processed orders is needed.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import uuid

from firebase_admin import db
from lego_orders import TERMINAL_STATUSES, normalize_status
from lego_outbox import DISPATCH_LOCK_PATH, account_symbol_fence_key

MAX_CONSECUTIVE_BROKER_REJECTS = 3
HALT = "BROKER_REJECT_HALT"
KEY = "broker_reject_circuit"


def _ref(scope):
    return db.reference(f"{DISPATCH_LOCK_PATH}/{scope}")


def status(identity, symbol):
    return (_ref(account_symbol_fence_key(identity, symbol)).get() or {}).get(KEY) or {}


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
        if broker_status in {"FAILED", "REJECTED"}:
            count = min(count + 1, MAX_CONSECUTIVE_BROKER_REJECTS)
        elif positive_fill:
            count = 0
        circuit.update(last_outcome_run_id=run_id, last_broker_status=broker_status,
                       consecutive_broker_rejects=count, updated_at=now)
        if count >= MAX_CONSECUTIVE_BROKER_REJECTS and not circuit.get("halted"):
            circuit.update(halted=True, status=HALT, halted_at=now,
                           halt_id=run_id)
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
                       reset_at=now.isoformat(), reset_id=reset_id,
                       reset_reason=redact_sensitive_text(reason)[:500])
        doc[KEY] = circuit
        return doc

    written = _ref(scope).transaction(clear)
    return {"dry_run": False, "scope": scope, "reset_id": written[KEY]["reset_id"]}
