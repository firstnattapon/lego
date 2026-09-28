"""Bounded cancellation under the existing money fence. No replacement orders."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import uuid
import os

from firebase_admin import db
import lego_outbox as outbox
import operator_halt
import tick_runtime
import transition_audit
from recovery_policy import RecoveryPolicy
from lego_orders import TERMINAL_STATUSES, normalize_status, _order_fields

CANCEL_STATES = {"CANCEL_REQUESTED", "CANCEL_UNKNOWN"}
CANCELLABLE = {"PENDING", "SUBMITTED", "PARTIAL_FILLED", "PARTIALLY_FILLED"}


class IncompleteOrderEvidence(ValueError):
    pass


def validate_evidence(intent, detail, summary):
    """Separate missing evidence (history may help) from conflicting identity."""
    fields = _order_fields(detail)
    for key, expected in (("client_order_id", intent["run_id"]),
                          ("symbol", intent["symbol"]), ("side", intent["side"]),
                          ("order_id", intent.get("broker_order_id"))):
        actual = fields.get(key)
        if expected and actual is not None and str(actual) != str(expected):
            raise ValueError("order identity changed")
    if fields.get("account_id") is not None and str(fields["account_id"]) != os.environ.get("WEBULL_ACCOUNT_ID"):
        raise ValueError("order account changed")
    required = ["client_order_id", "symbol", "side"]
    if intent.get("broker_order_id"):
        required.append("order_id")
    total_raw = fields.get("total_quantity", fields.get("quantity"))
    if (any(not fields.get(key) for key in required) or total_raw is None
            or fields.get("filled_quantity") is None):
        raise IncompleteOrderEvidence("order identity/quantity evidence incomplete")
    total = Decimal(str(total_raw))
    filled = Decimal(str(summary.get("filled_quantity")))
    if (not total.is_finite() or not filled.is_finite()
            or total != Decimal(str(intent["quantity"])) or not 0 <= filled <= total):
        raise ValueError("order quantity changed")


def utc(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    return parsed


def fence_owned(intent, claim, now):
    if not claim:
        return False
    scope = outbox.account_symbol_fence_key(intent["runtime_identity_fingerprint"], intent["symbol"])
    doc = db.reference(f"{outbox.DISPATCH_LOCK_PATH}/{scope}").get() or {}
    try:
        return (doc.get("owner") == claim.get("owner")
                and doc.get("claim_token") == claim.get("claim_token")
                and doc.get("inflight_run_id") == intent["run_id"]
                and doc.get("inflight_chain_key", intent["chain_key"]) == intent["chain_key"]
                and utc(doc.get("lease_until")) > now)
    except (ValueError, TypeError):
        return False


def mark_manual(intent, reason):
    updated = outbox.update_intent(intent["chain_key"], intent["run_id"], {
        "needs_manual_check": True, "cancel_last_error_code": reason,
        "audit_pending": True,
    }, expected_claim_owner=intent["claim_owner"],
       expected_claim_generation=intent["claim_generation"])
    operator_halt.set_halt(intent["runtime_identity_fingerprint"], intent["symbol"],
                           operator="system:order-recovery", reason=reason, apply=True)
    return updated


def begin_cancel(intent, claim, policy, now):
    """Consume the only mutation permission BEFORE calling the broker."""
    tick_runtime.require_budget(10)
    transition_audit.replay(intent["chain_key"], intent["run_id"])
    if not fence_owned(intent, claim, now):
        raise outbox.StaleIntentClaim("cancel money fence changed")
    token = uuid.uuid4().hex
    ref = db.reference(f"{outbox.OUTBOX_PATH}/{intent['chain_key']}/{intent['run_id']}")
    def txn(old):
        if not old:
            return old
        doc = dict(old)
        if (doc.get("cancel_attempt_count", 0) != 0
                or not doc.get("place_attempted") or doc.get("needs_manual_check")
                or doc.get("status") not in outbox_statuses()
                or doc.get("claim_owner") != intent.get("claim_owner")
                or doc.get("claim_generation") != intent.get("claim_generation")
                or utc(doc.get("claim_until")) <= datetime.now(timezone.utc)
                or doc.get("cancel_policy_hash") != policy.fingerprint):
            return doc
        doc.update(status="CANCEL_REQUESTED", cancel_attempt_count=1,
                   cancel_token=token, cancel_requested_at=now.isoformat(),
                   cancel_confirmation_deadline=(now + timedelta(seconds=policy.grace_seconds)).isoformat(),
                   audit_pending=True, audit_revision=int(doc.get("audit_revision", 0)) + 1)
        return transition_audit.enqueue(doc, old)
    result = ref.transaction(txn)
    return result if result and result.get("cancel_token") == token else None


def outbox_statuses():
    return CANCELLABLE | {"PLACING_UNKNOWN", "UNKNOWN", "PLACING"}


def handle(intent, detail, summary, claim, cancel, *, now=None):
    """Return refreshed intent; caller always settles the authoritative summary."""
    now = now or datetime.now(timezone.utc)
    status = normalize_status(summary.get("status"))
    # Never book a terminal result whose side/quantity/broker identity changed.
    if intent.get("cancel_policy") is not None:
        try:
            validate_evidence(intent, detail, summary)
        except (ValueError, TypeError, InvalidOperation):
            return mark_manual(intent, "CANCEL_EVIDENCE_INVALID")
    if intent.get("cancel_attempt_count"):
        if status in TERMINAL_STATUSES:
            return outbox.update_intent(intent["chain_key"], intent["run_id"], {
                "cancel_confirmed_at": now.isoformat(), "audit_pending": True,
            }, expected_claim_owner=intent["claim_owner"],
               expected_claim_generation=intent["claim_generation"])
        return read_failed(intent, now=now)
    raw = intent.get("cancel_policy")
    if raw is None:  # Never grant new recovery rights to legacy orders.
        return intent
    try:
        policy = RecoveryPolicy(**raw)
    except (TypeError, ValueError):
        return mark_manual(intent, "CANCEL_POLICY_MISMATCH")
    if policy.fingerprint != intent.get("cancel_policy_hash"):
        return mark_manual(intent, "CANCEL_POLICY_MISMATCH")
    if policy.action != "cancel" or status not in CANCELLABLE or intent.get("needs_manual_check"):
        return intent
    try:
        age = (now - utc(intent.get("placed_at"))).total_seconds()
        if age < 0 or not intent.get("place_attempted"):
            raise ValueError("invalid place witness")
        fields = _order_fields(detail)
        quantity = Decimal(str(fields.get("total_quantity", fields.get("quantity"))))
        filled = Decimal(str(summary.get("filled_quantity")))
        if (fields.get("client_order_id") != intent["run_id"]
                or str(fields.get("symbol", "")).upper() != intent["symbol"]
                or fields.get("side") != intent["side"]
                or not quantity.is_finite() or not filled.is_finite()
                or quantity != Decimal(str(intent["quantity"])) or not 0 <= filled < quantity
                or (intent.get("broker_order_id") and fields.get("order_id") != intent["broker_order_id"])):
            raise ValueError("unverified cancellation identity/quantity")
    except (ValueError, TypeError, InvalidOperation):
        return mark_manual(intent, "CANCEL_EVIDENCE_INVALID")
    if age < policy.stale_seconds:
        return intent
    started = begin_cancel(intent, claim, policy, now)
    if started is None:
        return outbox.read_intent(intent["chain_key"], intent["run_id"]) or intent
    # A crash after the witness consumes the right even if no request is sent.
    # Recheck both leases immediately before the external mutation.
    transition_audit.replay(started["chain_key"], started["run_id"])
    current = outbox.read_intent(started["chain_key"], started["run_id"])
    if (not current or current.get("claim_owner") != started.get("claim_owner")
            or current.get("claim_generation") != started.get("claim_generation")
            or utc(current.get("claim_until")) <= datetime.now(timezone.utc)
            or not fence_owned(started, claim, datetime.now(timezone.utc))):
        raise outbox.StaleIntentClaim("cancel lease lost after witness")
    try:
        cancel(started["run_id"])
    except Exception as exc:
        code = "TICK_DEFERRED" if isinstance(exc, tick_runtime.TickDeadlineExceeded) else "CANCEL_OUTCOME_UNKNOWN"
        return outbox.update_intent(started["chain_key"], started["run_id"], {
            "status": "CANCEL_UNKNOWN", "cancel_last_error_code": code,
            "audit_pending": True,
        }, expected_claim_owner=started["claim_owner"],
           expected_claim_generation=started["claim_generation"])
    return started


def read_failed(intent, *, now=None):
    """Cancel grace still expires when the broker cannot be read at all."""
    now = now or datetime.now(timezone.utc)
    if intent.get("cancel_attempt_count"):
        try:
            deadline = utc(intent.get("cancel_confirmation_deadline"))
        except (TypeError, ValueError):
            return mark_manual(intent, "CANCEL_DEADLINE_INVALID")
        if now >= deadline:
            return mark_manual(intent, "CANCEL_CONFIRMATION_OVERDUE")
    return intent
