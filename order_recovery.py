"""Bounded cancellation under the existing money fence. No replacement orders."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import uuid
import os

from firebase_admin import db
import flight_recorder as fr
import lego_outbox as outbox
import operator_halt
import tick_runtime
import transition_audit
import market_clock
from recovery_policy import CANCEL_ACTIONS, RecoveryPolicy
from lego_orders import (TERMINAL_STATUSES, normalize_status, canonical_order_evidence,
                         IncompleteOrderEvidence, ConflictingOrderEvidence,
                         BrokerContractAnomaly)

CANCEL_STATES = {"CANCEL_REQUESTED", "CANCEL_UNKNOWN"}
CANCELLABLE = {"PENDING", "SUBMITTED", "PARTIAL_FILLED", "PARTIALLY_FILLED"}
# The broker answered the one cancel with "this status cannot be operated on"
# (HTTP 417 OPENAPI_ORDER_CANNOT_OPERATE). That is a refusal, not an unknown
# outcome: the order is still live and ends by itself (a fill or its DAY expiry),
# so it stays in ordinary reconciliation instead of halting after the grace.
# 2026-10-05: a 0.47-share UAT market order sat PENDING, the refused cancel was
# read as unknown and trading stopped for 3.5 hours. A DAY order cannot outlive
# its session, so the hold is bounded; after it a person decides (mark_manual).
CANCEL_REFUSED = "CANCEL_REFUSED_NOT_OPERABLE"
CANCEL_REFUSAL_CODES = frozenset({"OPENAPI_ORDER_CANNOT_OPERATE"})
REFUSED_HOLD_SECONDS = 8 * 3600
# 2026-10-06: the refused order (BUY 0.57, MARKET/DAY/CORE) was still PENDING 3h51m
# after the 20:00Z close and the 8 hour hold ended in a halt that carried into the
# next session. A DAY order cannot trade once its session is over, so past the close
# plus this margin a fill-less, unlisted order with unchanged holdings is released as
# EXPIRED instead. The margin outlasts UAT's 15 minute quote delay with room to spare.
EXPIRY_MARGIN_SECONDS = 3600
EXPIRY_TERMINAL_REASON = "DAY_EXPIRY_PROOF"
LIVE_STATUSES = frozenset({"PENDING", "SUBMITTED"})
# 2026-10-12 (4th time in 6 UAT sessions): a MARKET/DAY/CORE fractional order stayed
# PENDING *and listed in open orders* for 7+ hours -- 21:00Z and 22:07Z, one and two hours
# after the close, order detail PENDING / filled 0 and open-orders/list still returned it --
# so the proof above could never pass (`order_still_listed_open`), the hold ended in a halt
# and 23 of 26 slots traded nothing. UAT does not retire such an order; it also never filled
# one (three earlier ones left the position exactly where it was for days), while a slow but
# genuine fill takes seconds to minutes (longest seen: under 15 minutes, 2026-10-05).
# UAT ONLY and only ever by the caller passing uat_stuck_pending=True (execution_service does
# so for WEBULL_ENV=UAT, never for PROD): an order that is still PENDING with nothing filled
# this long after Place, whose one cancel was refused, and that the broker lists as exactly
# that, is released as a zero-fill EXPIRED during the session instead of at the close.
UAT_STUCK_PENDING_SECONDS = 1800
UAT_STUCK_PENDING_REASON = "UAT_STUCK_PENDING_RELEASE"
BASIS_SESSION_OVER = "session_over"
BASIS_UAT_STUCK_PENDING = "uat_stuck_pending"


def validate_evidence(intent, detail, summary):
    """Separate missing evidence (history may help) from conflicting identity."""
    evidence = canonical_order_evidence(detail)
    fields = {"client_order_id": evidence.client_order_id, "symbol": evidence.symbol,
              "side": evidence.side, "order_id": evidence.broker_order_id,
              "account_id": evidence.account_id}
    for key, expected in (("client_order_id", intent["run_id"]),
                          ("symbol", intent["symbol"]), ("side", intent["side"]),
                          ("order_id", intent.get("broker_order_id"))):
        actual = fields.get(key)
        if expected and actual is not None and str(actual) != str(expected):
            raise ConflictingOrderEvidence("order identity changed: " + key)
    if fields.get("account_id") is not None and str(fields["account_id"]) != os.environ.get("WEBULL_ACCOUNT_ID"):
        raise ConflictingOrderEvidence("order account changed")
    required = ["client_order_id", "symbol", "side"]
    if intent.get("broker_order_id"):
        required.append("order_id")
    total = evidence.total_quantity
    filled = evidence.filled_quantity
    if (any(not fields.get(key) for key in required) or total is None or filled is None):
        raise IncompleteOrderEvidence("order identity/quantity evidence incomplete")
    submitted = Decimal(str(intent["quantity"]))
    payload = intent.get("order_payload")
    if payload is not None:
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise IncompleteOrderEvidence("submitted payload missing or ambiguous")
        order = payload[0]
        if any(order.get(key) != intent.get(expected) for key, expected in
               (("client_order_id", "run_id"), ("symbol", "symbol"), ("side", "side"))):
            raise ConflictingOrderEvidence("submitted order identity changed")
        if Decimal(str(order.get("quantity"))) != submitted:
            raise ConflictingOrderEvidence("intent/payload quantity changed")
        submitted = Decimal(str(order["quantity"]))
    if (not submitted.is_finite() or submitted <= 0 or total != submitted
            or not 0 <= filled <= total
            or (evidence.status == "FILLED" and filled != total)):
        raise BrokerContractAnomaly(
            f"order quantity changed: broker_total={total}, submitted={submitted}, filled={filled}")
    if Decimal(str(summary.get("filled_quantity"))) != filled:
        raise ConflictingOrderEvidence("summary filled quantity changed")
    return evidence


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


def mark_manual(intent, reason, *, detail=None):
    import hashlib
    from security_text import broker_diagnostic_json
    fr.node("H00", "MANUAL", ok=False, run_id=intent.get("run_id"), reason=reason,
            **fr.pick(intent, "status", "broker_status", "cancel_attempt_count",
                      "cancel_refused_at", "cancel_confirmation_deadline"))
    diagnostic = broker_diagnostic_json(detail) if detail is not None else None
    updated = outbox.update_intent(intent["chain_key"], intent["run_id"], {
        "status": "MANUAL_RECONCILIATION_REQUIRED",
        "needs_manual_check": True, "cancel_last_error_code": reason,
        "manual_since": intent.get("manual_since") or datetime.now(timezone.utc).isoformat(),
        "next_auto_reconcile_at": None,
        **({"reconcile_evidence": diagnostic,
            "reconcile_evidence_sha256": hashlib.sha256(diagnostic.encode()).hexdigest()}
           if diagnostic is not None else {}),
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


def cancel_refused(exc):
    """True only for the broker's definitive 'cannot operate' answer to a cancel.

    Duck-typed on the SDK ServerException (error_code); a timeout, a 5xx or any
    other code stays an unknown outcome, because the cancel may have been taken.
    """
    return str(getattr(exc, "error_code", "") or "").strip().upper() in CANCEL_REFUSAL_CODES


def handle(intent, detail, summary, claim, cancel, *, now=None):
    """Return refreshed intent; caller always settles the authoritative summary."""
    now = now or datetime.now(timezone.utc)
    if intent.get("needs_manual_check"):
        return intent
    status = normalize_status(summary.get("status"))
    # Never book a terminal result whose side/quantity/broker identity changed.
    if intent.get("cancel_policy") is not None:
        try:
            validate_evidence(intent, detail, summary)
        except (ValueError, TypeError, InvalidOperation):
            return mark_manual(intent, "CANCEL_EVIDENCE_INVALID", detail=detail)
    if intent.get("cancel_attempt_count"):
        if status in TERMINAL_STATUSES:
            if summary.get("expiry_released"):
                return intent  # released by proof, so no cancel was ever confirmed
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
    if policy.action not in CANCEL_ACTIONS or status not in CANCELLABLE or intent.get("needs_manual_check"):
        return intent
    try:
        age = (now - utc(intent.get("placed_at"))).total_seconds()
        if age < 0 or not intent.get("place_attempted"):
            raise ValueError("invalid place witness")
        evidence = validate_evidence(intent, detail, summary)
        if not 0 <= evidence.filled_quantity < evidence.total_quantity:
            raise ValueError("unverified cancellation identity/quantity")
    except (ValueError, TypeError, InvalidOperation):
        return mark_manual(intent, "CANCEL_EVIDENCE_INVALID", detail=detail)
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
        fields = {"status": "CANCEL_UNKNOWN", "audit_pending": True}
        if isinstance(exc, tick_runtime.TickDeadlineExceeded):
            fields["cancel_last_error_code"] = "TICK_DEFERRED"
        elif cancel_refused(exc):
            # Move the deadline rather than add a branch: read_failed and any older
            # binary read the same field, so a rollback still honours the hold.
            fields.update(
                cancel_last_error_code=CANCEL_REFUSED, cancel_refused_at=now.isoformat(),
                cancel_confirmation_deadline=(
                    now + timedelta(seconds=REFUSED_HOLD_SECONDS)).isoformat())
        else:
            fields["cancel_last_error_code"] = "CANCEL_OUTCOME_UNKNOWN"
        return outbox.update_intent(started["chain_key"], started["run_id"], fields,
                                    expected_claim_owner=started["claim_owner"],
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
            # After a refused cancel the deadline is the end of the bounded hold
            # (handle), and the order was never terminal in all that time.
            return mark_manual(intent, "CANCEL_REFUSED_HOLD_EXPIRED"
                               if intent.get("cancel_refused_at")
                               else "CANCEL_CONFIRMATION_OVERDUE")
    return intent


def _session_close(placed_at):
    """UTC close of the regular session an order was placed in, else None."""
    placed = utc(placed_at)
    bounds = market_clock.session_bounds(placed.astimezone(market_clock.NY).date())
    if bounds is None or not bounds[0] <= placed < bounds[1]:
        return None
    return bounds[1]


def expiry_authorized(intent):
    """A person confirmed the proof with tools/resume_order_reconciliation --expiry-proof."""
    resume = intent.get("reconcile_resume")
    return (isinstance(resume, dict) and resume.get("expiry_proof_authorized") is True
            and bool(str(resume.get("operator") or "").strip()))


def _expiry_policy_allows(intent):
    if expiry_authorized(intent):
        return True
    raw = intent.get("cancel_policy")
    if not isinstance(raw, dict):
        return False
    try:
        policy = RecoveryPolicy(**raw)
    except (TypeError, ValueError):
        return False
    return (policy.action == "cancel_expire"
            and policy.fingerprint == intent.get("cancel_policy_hash"))


def _stuck_pending_age_reached(intent, now):
    """True once the order is UAT_STUCK_PENDING_SECONDS old, counted from our own Place time."""
    try:
        return (now - utc(intent.get("placed_at"))).total_seconds() >= UAT_STUCK_PENDING_SECONDS
    except (TypeError, ValueError):
        return False


def _decimal(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _listed_rows(run_id, orders):
    return [o for o in orders
            if isinstance(o, dict) and str(o.get("client_order_id") or "") == str(run_id)]


def _same_order(row, *, symbol, side, total):
    """A listed row that names a different symbol, side or size is not the order we sent."""
    for field, wanted in (("symbol", symbol), ("side", side)):
        seen = row.get(field)
        if seen is not None and wanted and str(seen).strip().upper() != str(wanted).strip().upper():
            return False
    quantity = _decimal(row.get("total_quantity"))
    return quantity is not None and total is not None and quantity == total


def _listed_is_stuck_pending(intent, evidence, open_orders):
    """The broker lists exactly this order once, PENDING, nothing filled, same size.

    Anything else -- a second row, another status (SUBMITTED / PARTIAL_FILLED / FILLED), a
    filled quantity that is missing, malformed or positive -- is not the signature the
    UAT rule was written for and stays a blocker.
    """
    if evidence is None or evidence.status != "PENDING":
        return False
    rows = _listed_rows(intent.get("run_id"), open_orders)
    if len(rows) != 1:
        return False
    row = rows[0]
    if normalize_status(row.get("status")) != "PENDING":
        return False
    filled = _decimal(row.get("filled_quantity"))
    if filled is None or filled != 0:
        return False
    return _same_order(row, symbol=intent.get("symbol"), side=intent.get("side"),
                       total=evidence.total_quantity)


def is_released_uat_stuck(row, intent):
    """Is this open-order row an order we released as a UAT stuck-PENDING zombie, unchanged?

    The dispatch guards use it so a released order that the broker still lists does not
    block every later order. It stays true only while the broker's row still says PENDING
    with nothing filled; the moment it says anything else the row blocks again.
    """
    if not isinstance(row, dict) or not isinstance(intent, dict):
        return False
    run_id = str(intent.get("run_id") or "")
    if not run_id or str(row.get("client_order_id") or "") != run_id:
        return False
    if (normalize_status(intent.get("status")) != "EXPIRED"
            or intent.get("expiry_released") is not True
            or intent.get("expiry_basis") != BASIS_UAT_STUCK_PENDING
            or normalize_status(intent.get("broker_status")) != "PENDING"):
        return False
    if normalize_status(row.get("status")) != "PENDING":
        return False
    filled = _decimal(row.get("filled_quantity"))
    if filled is None or filled != 0:
        return False
    symbol = str(row.get("symbol") or "").strip().upper()
    return bool(symbol) and symbol == str(intent.get("symbol") or "").strip().upper()


def expiry_candidate(intent, *, now=None, uat_stuck_pending=False):
    """Cheap, read-free gate: is it worth reading the broker for the proof?"""
    now = now or datetime.now(timezone.utc)
    if (intent.get("needs_manual_check") or intent.get("place_attempted") is not True
            or not intent.get("cancel_attempt_count") or not intent.get("cancel_refused_at")
            or not _expiry_policy_allows(intent)):
        return False
    try:
        close = _session_close(intent.get("placed_at"))
    except (TypeError, ValueError):
        return False
    if close is None:
        return False
    if now >= close + timedelta(seconds=EXPIRY_MARGIN_SECONDS):
        return True
    return bool(uat_stuck_pending) and _stuck_pending_age_reached(intent, now)


def expiry_proof_blockers(intent, detail, summary, *, open_orders, holdings, now, tolerance,
                          uat_stuck_pending=False):
    """Why a refused DAY order is not yet provably dead. Empty list = proven.

    Pure: every broker read is an argument. Every condition must hold together;
    each one alone is explained by something other than "the order is gone".

    uat_stuck_pending (UAT only, default off) replaces two of those conditions with the
    stricter signature of UAT's never-retired order: age since Place instead of the session
    close, and exactly one PENDING / nothing-filled open-orders row instead of no row.
    Everything else -- cancel refused, single MARKET/DAY/CORE leg, identity and quantity,
    holdings unchanged, open orders readable -- is required exactly as before.
    """
    blockers = []
    relaxed_age = False
    if intent.get("place_attempted") is not True:
        blockers.append("place_not_attempted")
    if not intent.get("cancel_attempt_count"):
        blockers.append("cancel_not_attempted")
    if not intent.get("cancel_refused_at"):
        blockers.append("cancel_not_refused")
    payload = intent.get("order_payload")
    leg = payload[0] if (isinstance(payload, list) and len(payload) == 1
                         and isinstance(payload[0], dict)) else None
    if leg is None:
        blockers.append("payload_not_single_leg")
    else:
        for key, wanted in (("order_type", "MARKET"), ("time_in_force", "DAY"),
                            ("support_trading_session", "CORE")):
            if str(leg.get(key) or "").strip().upper() != wanted:
                blockers.append(f"payload_{key}_not_{wanted.lower()}")
    try:
        close = _session_close(intent.get("placed_at"))
    except (TypeError, ValueError):
        close = None
    if close is None:
        blockers.append("placed_outside_regular_session")
    elif now < close + timedelta(seconds=EXPIRY_MARGIN_SECONDS):
        if uat_stuck_pending and _stuck_pending_age_reached(intent, now):
            relaxed_age = True
        else:
            blockers.append("session_not_over_plus_margin")
    evidence = None
    try:
        evidence = validate_evidence(intent, detail, summary)
    except (ValueError, TypeError, ArithmeticError):
        blockers.append("broker_evidence_invalid")
    if evidence is not None:
        if evidence.status in TERMINAL_STATUSES:
            blockers.append("broker_status_terminal")  # ordinary reconciliation owns it
        elif evidence.status not in LIVE_STATUSES:
            blockers.append("broker_status_unrecognized")  # UNKNOWN is not "still pending"
        elif relaxed_age and evidence.status != "PENDING":
            # A SUBMITTED order may be working at the exchange; only PENDING has ever been stuck.
            blockers.append("stuck_release_requires_pending")
        if evidence.filled_quantity != 0:
            blockers.append("filled_quantity_not_zero")
    if open_orders is None:
        blockers.append("open_orders_unread")
    elif any(str(order.get("client_order_id") or "") == str(intent.get("run_id"))
             for order in open_orders):
        # Same code in both modes: "the broker still lists it as open". The UAT rule only
        # lifts it for the one signature above; the unmet row is on the W11 trace.
        if not (uat_stuck_pending and _listed_is_stuck_pending(intent, evidence, open_orders)):
            blockers.append("order_still_listed_open")
    try:
        before, after = float(intent["decision_holdings"]), float(holdings)
        if not (math.isfinite(before) and math.isfinite(after) and before >= 0 and after >= 0):
            raise ValueError("holdings must be finite and non-negative")
        if abs(after - before) > tolerance:
            blockers.append("holdings_changed")
    except (KeyError, TypeError, ValueError):
        blockers.append("holdings_unverifiable")
    return blockers


def expiry_proof(intent, detail, summary, *, open_orders, holdings, now, tolerance,
                 uat_stuck_pending=False):
    """(blockers, basis): the strict proof first; the UAT rule only when that is not enough.

    basis says which rule released the order -- session_over (the original proof, nothing
    relaxed) or uat_stuck_pending -- so an order that would have passed anyway is never
    recorded as a UAT exception. basis is None while blockers remain.
    """
    facts = dict(open_orders=open_orders, holdings=holdings, now=now, tolerance=tolerance)
    strict = expiry_proof_blockers(intent, detail, summary, **facts)
    if not strict:
        return [], BASIS_SESSION_OVER
    if not uat_stuck_pending:
        return strict, None
    relaxed = expiry_proof_blockers(intent, detail, summary, uat_stuck_pending=True, **facts)
    return relaxed, (None if relaxed else BASIS_UAT_STUCK_PENDING)


def expiry_release(intent, detail, summary, *, open_orders, holdings, tolerance, now=None,
                   uat_stuck_pending=False):
    """(summary, blockers): an EXPIRED zero-fill summary only when the proof holds.

    The caller feeds it to the ordinary terminal path, so ledgers, circuit and
    fence behave exactly as for a broker EXPIRED with no fill. broker_status keeps
    what the broker really said (PENDING) so the release can never read as a
    broker-confirmed expiry.
    """
    now = now or datetime.now(timezone.utc)
    blockers, basis = expiry_proof(intent, detail, summary, open_orders=open_orders,
                                   holdings=holdings, now=now, tolerance=tolerance,
                                   uat_stuck_pending=uat_stuck_pending)
    if blockers:
        return None, blockers
    broker_status = normalize_status(summary.get("status"))
    proof = {"run_id": intent["run_id"], "broker_order_id": intent.get("broker_order_id"),
             "broker_status": broker_status, "placed_at": intent.get("placed_at"),
             "session_close": _session_close(intent["placed_at"]).isoformat(),
             "margin_seconds": EXPIRY_MARGIN_SECONDS, "holdings": float(holdings),
             "decision_holdings": float(intent["decision_holdings"]),
             "open_order_count": len(open_orders)}
    if basis == BASIS_UAT_STUCK_PENDING:
        # Only the UAT rule adds to the facts, so a session_over proof hashes exactly as before.
        proof.update(basis=basis, threshold_seconds=UAT_STUCK_PENDING_SECONDS,
                     listed=bool(_listed_rows(intent["run_id"], open_orders)))
    digest = hashlib.sha256(json.dumps(proof, sort_keys=True, separators=(",", ":"),
                                       default=str).encode()).hexdigest()
    resume = intent.get("reconcile_resume") if expiry_authorized(intent) else None
    return {**summary, "status": "EXPIRED", "broker_status": broker_status,
            "terminal_reason": (EXPIRY_TERMINAL_REASON if basis == BASIS_SESSION_OVER
                                else UAT_STUCK_PENDING_REASON),
            "expiry_released": True, "expiry_basis": basis,
            "expiry_released_at": now.isoformat(), "expiry_proof_sha256": digest,
            "expiry_released_by": (str(resume["operator"])[:128] if resume
                                   else "policy:cancel_expire")}, []
