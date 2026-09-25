"""Durable operator stop for new orders on one account and symbol.

The flag lives inside the same RTDB document as the money fence. A halt
transaction therefore orders itself against the transaction that admits a new
dispatch. It does not cancel an order already submitted or resolve an inflight
broker outcome; the worker must continue read-only reconciliation.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from firebase_admin import db

from lego_outbox import DISPATCH_LOCK_PATH, account_symbol_fence_key
from security_text import redact_sensitive_text

KEY = "operator_halt"
AUDIT_PATH = "webull_lego_operator_halt_audit"


def _scope(identity: str, symbol: str) -> str:
    return account_symbol_fence_key(identity, symbol)


def _ref(scope: str):
    return db.reference(f"{DISPATCH_LOCK_PATH}/{scope}")


def _text(value: str, name: str, maximum: int) -> str:
    text = redact_sensitive_text(str(value or "").strip())[:maximum]
    if not text:
        raise ValueError(f"{name} is required")
    return text


def status(identity: str, symbol: str) -> dict:
    doc = _ref(_scope(identity, symbol)).get() or {}
    if not isinstance(doc, dict):
        raise ValueError("dispatch fence document is malformed")
    return dict(doc.get(KEY) or {})


def repair_audit(identity: str, symbol: str) -> bool:
    """Replay the append-only audit mirror after an interrupted RTDB write."""
    scope = _scope(identity, symbol)
    ref = _ref(scope)
    doc = ref.get() or {}
    pending = (doc.get(KEY) or {}).get("audit_pending_event")
    if not isinstance(pending, dict):
        return False
    event_id = str(pending.get("event_id") or "")
    if not event_id or pending.get("scope") != scope:
        raise ValueError("operator halt audit marker is malformed")
    audit_ref = db.reference(f"{AUDIT_PATH}/{scope}/{event_id}")

    def write_once(current):
        if current is None:
            return pending
        if current != pending:
            raise ValueError("operator halt audit event changed")
        return current

    audit_ref.transaction(write_once)

    def acknowledge(current):
        if not isinstance(current, dict):
            return current
        updated = dict(current)
        halt = dict(updated.get(KEY) or {})
        marker = halt.get("audit_pending_event")
        if isinstance(marker, dict) and marker.get("event_id") == event_id:
            halt.pop("audit_pending_event", None)
            halt["last_audit_event_id"] = event_id
            updated[KEY] = halt
        return updated

    ref.transaction(acknowledge)
    return True


def set_halt(identity: str, symbol: str, *, operator: str, reason: str,
             apply: bool = False) -> dict:
    """Stop new dispatches immediately; preserve any already inflight order."""
    scope = _scope(identity, symbol)
    operator = _text(operator, "operator", 128)
    reason = _text(reason, "reason", 500)
    current = _ref(scope).get() or {}
    existing = dict(current.get(KEY) or {})
    if not apply:
        return {"dry_run": True, "scope": scope,
                "halted": existing.get("halted") is True,
                "inflight": bool(current.get("inflight_run_id"))}

    now = datetime.now(timezone.utc).isoformat()
    halt_id, event_id = uuid.uuid4().hex, uuid.uuid4().hex

    def txn(old):
        doc = dict(old or {})
        halt = dict(doc.get(KEY) or {})
        if halt.get("halted") is True:
            return doc
        if halt.get("audit_pending_event"):
            raise ValueError("repair prior operator halt audit before a new transition")
        event = {"event_id": event_id, "scope": scope, "action": "HALT",
                 "halt_id": halt_id, "operator": operator, "reason": reason, "at": now}
        halt.update(halted=True, halt_id=halt_id, set_at=now, set_by=operator,
                    reason=reason, audit_pending_event=event)
        doc[KEY] = halt
        return doc

    written = _ref(scope).transaction(txn) or {}
    halt = dict(written.get(KEY) or {})
    repair_audit(identity, symbol)
    return {"dry_run": False, "scope": scope,
            "halt_id": halt.get("halt_id"),
            "inflight": bool(written.get("inflight_run_id"))}


def clear_halt(identity: str, symbol: str, *, expected_halt_id: str,
               operator: str, reason: str, apply: bool = False) -> dict:
    """Resume only after the exact halt is reviewed and the fence is idle."""
    scope = _scope(identity, symbol)
    operator = _text(operator, "operator", 128)
    reason = _text(reason, "reason", 500)
    now = datetime.now(timezone.utc)

    def validate(doc):
        halt = dict(doc.get(KEY) or {})
        if halt.get("halted") is not True or halt.get("halt_id") != expected_halt_id:
            raise ValueError("operator halt changed or absent")
        if halt.get("audit_pending_event"):
            raise ValueError("operator halt audit is pending")
        if halt.get("set_by") == operator:
            raise ValueError("a second operator must review the halt")
        if doc.get("inflight_run_id"):
            raise ValueError("unresolved order fence; reconcile before clearing halt")
        if doc.get("owner"):
            try:
                until = datetime.fromisoformat(
                    str(doc.get("lease_until")).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                raise ValueError("dispatch lease unreadable") from None
            if until.tzinfo is None or until > now:
                raise ValueError("dispatch worker active")
        return halt

    current = _ref(scope).get() or {}
    validate(current)
    if not apply:
        return {"dry_run": True, "scope": scope, "halt_id": expected_halt_id}

    at = now.isoformat()
    event_id = uuid.uuid4().hex

    def txn(old):
        doc = dict(old or {})
        halt = validate(doc)
        event = {"event_id": event_id, "scope": scope, "action": "CLEAR",
                 "halt_id": expected_halt_id, "operator": operator,
                 "reason": reason, "at": at}
        halt.update(halted=False, clear_at=at, clear_by=operator,
                    clear_reason=reason, audit_pending_event=event)
        doc[KEY] = halt
        return doc

    _ref(scope).transaction(txn)
    repair_audit(identity, symbol)
    return {"dry_run": False, "scope": scope, "halt_id": expected_halt_id,
            "cleared": True}
