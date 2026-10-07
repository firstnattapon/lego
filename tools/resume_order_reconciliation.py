"""Explicitly re-arm accounting reconciliation after fresh broker proof.

Two kinds of proof, both dry run by default:
  * terminal: the broker reports a terminal order (default mode);
  * --expiry-proof: the order is still non-terminal but provably dead -- a refused
    cancel, a DAY/CORE market order whose session ended, no fill, not listed as
    open, holdings unchanged (order_recovery.expiry_proof_blockers). 2026-10-06 a
    UAT order stayed PENDING for 8+ hours and no terminal proof ever came.

Never Place, cancel, clear a fence, or reset an operator halt. The worker re-reads
the broker and runs the existing idempotent ledger finalizers (for --expiry-proof it
repeats the proof itself before releasing anything).
"""
import argparse
import hashlib
import json
import uuid
from datetime import datetime, timezone

from firebase_admin import db
import lego_outbox as outbox
import order_recovery
import transition_audit
import webull_io
from lego_orders import TERMINAL_STATUSES, summarize_order_result


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()


def plan(intent, detail, identity):
    if (intent.get("place_attempted") is not True or not intent.get("needs_manual_check")
            or intent.get("runtime_identity_fingerprint") != identity
            or intent.get("cashflow_abandoned") or intent.get("admin_reconciliation_pending")):
        raise ValueError("intent is not eligible for broker-proven accounting recovery")
    summary = summarize_order_result({}, detail)
    evidence = order_recovery.validate_evidence(intent, detail, summary)
    if evidence.status not in TERMINAL_STATUSES or not evidence.broker_order_id:
        raise ValueError("terminal broker order identity required")
    if evidence.filled_quantity > 0 and evidence.filled_price is None:
        raise ValueError("actual fill price required")
    # Proof is fresh from the configured account's endpoint. Positions alone or
    # user-supplied JSON cannot authorize this transition.
    result = {"schema": "resume_reconciliation_v1", "intent_sha256": fingerprint(intent),
              "broker_evidence_sha256": fingerprint(summary), "broker_status": evidence.status,
              "run_id": intent["run_id"], "chain_key": intent["chain_key"],
              "broker_order_id": evidence.broker_order_id}
    return {**result, "confirmation": "RESUME " + fingerprint(result)}


EXPIRY_SCHEMA = "resume_expiry_proof_v1"


def expiry_plan(intent, detail, open_orders, holdings, identity, now, tolerance):
    """Dry-run plan for a non-terminal order that the expiry proof shows is dead."""
    if (intent.get("place_attempted") is not True or not intent.get("needs_manual_check")
            or intent.get("runtime_identity_fingerprint") != identity
            or intent.get("cashflow_abandoned") or intent.get("admin_reconciliation_pending")):
        raise ValueError("intent is not eligible for broker-proven accounting recovery")
    summary = summarize_order_result({}, detail)
    blockers = order_recovery.expiry_proof_blockers(
        intent, detail, summary, open_orders=open_orders, holdings=holdings, now=now,
        tolerance=tolerance)
    if blockers:
        raise ValueError("expiry proof not met: " + ", ".join(blockers))
    evidence = order_recovery.validate_evidence(intent, detail, summary)
    result = {"schema": EXPIRY_SCHEMA, "intent_sha256": fingerprint(intent),
              "broker_evidence_sha256": fingerprint(summary), "broker_status": evidence.status,
              "run_id": intent["run_id"], "chain_key": intent["chain_key"],
              "broker_order_id": evidence.broker_order_id, "holdings": float(holdings),
              "open_order_count": len(open_orders)}
    return {**result, "confirmation": "RELEASE-EXPIRED " + fingerprint(result)}


def apply_plan(expected, *, identity, symbol, operator, read_detail, replan=None):
    if not operator.strip():
        raise ValueError("operator is required")
    scope = outbox.account_symbol_fence_key(identity, symbol)
    owner = "admin-resume-" + uuid.uuid4().hex
    claim = outbox.claim_chain_dispatch(scope, owner, lease_seconds=120)
    if claim is None:
        raise ValueError("worker holds the money fence; pause scheduler and wait for lease expiry")
    try:
        run, chain = expected["run_id"], expected["chain_key"]
        if claim.get("inflight_run_id") != run or claim.get("inflight_chain_key", chain) != chain:
            raise ValueError("money fence no longer belongs to this order")
        intent = outbox.read_intent(chain, run)
        if not intent or intent.get("symbol") != symbol:
            raise ValueError("intent missing or symbol changed")
        fresh = replan(intent) if replan else plan(intent, read_detail(run), identity)
        if fresh != expected:
            raise ValueError("evidence changed; obtain a new dry-run confirmation")
        now = datetime.now(timezone.utc)
        event = {"operator": webull_io.redact_sensitive_text(operator)[:128],
                 "at": now.isoformat(), "confirmation": expected["confirmation"],
                 "intent_sha256": expected["intent_sha256"],
                 "broker_evidence_sha256": expected["broker_evidence_sha256"]}
        if expected.get("schema") == EXPIRY_SCHEMA:
            # The worker honours this only together with its own fresh proof.
            event["expiry_proof_authorized"] = True
        # Recheck the fence lease after broker I/O; mutations stay under one owner.
        if not order_recovery.fence_owned(intent, claim, now):
            raise ValueError("money fence lease expired")

        def transition(old):
            if not old or fingerprint(old) != expected["intent_sha256"]:
                raise ValueError("intent changed during reviewed recovery")
            until = outbox._parse_utc(old.get("claim_until"))
            if old.get("claim_owner") and until and until > datetime.now(timezone.utc):
                raise ValueError("intent still has an active worker")
            doc = dict(old)
            doc.update(status="PLACING_UNKNOWN", needs_manual_check=False,
                       broker_order_id=expected["broker_order_id"],
                       reconcile_attempts=0, reconcile_resume=event,
                       claim_owner=None, claim_until=None,
                       claim_generation=int(old.get("claim_generation", 0)) + 1,
                       audit_pending=True, audit_revision=int(old.get("audit_revision", 0)) + 1,
                       actionable_sort=str(old.get("slot_start_utc") or old.get("created_at") or run))
            return transition_audit.enqueue(doc, old)

        db.reference(f"{outbox.OUTBOX_PATH}/{chain}/{run}").transaction(transition)
        transition_audit.replay(chain, run)
        return {"reconciliation_resumed": True, "operator_halt_retained": True,
                "money_fence_retained": True}
    finally:
        outbox.release_chain_dispatch(scope, owner, claim["claim_token"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chain", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--confirm")
    parser.add_argument("--expiry-proof", action="store_true",
                        help="non-terminal order: prove it is dead instead of terminal")
    args = parser.parse_args()
    # Shared initialization uses the runtime's configured account/environment.
    import execution_service
    execution_service._init_firebase()
    identity = webull_io.runtime_identity_fingerprint()
    intent = outbox.read_intent(args.chain, args.run)
    if not intent or intent.get("symbol") != webull_io.load_config().symbol:
        raise ValueError("intent missing or symbol differs from configured account/symbol")
    trade, _ = webull_io.build_clients()
    read = lambda run: webull_io.fetch_order_detail(trade, run)
    replan = None
    if args.expiry_proof:
        cfg = webull_io.load_config()
        tolerance = execution_service._holdings_drift_tolerance(
            typed_v2=execution_service._intent_is_v2(intent))

        def replan(current):
            return expiry_plan(
                current, read(args.run), webull_io.fetch_open_orders(trade, current["symbol"]),
                webull_io.fetch_holdings(trade, cfg), identity, datetime.now(timezone.utc),
                tolerance)
        proposed = replan(intent)
    else:
        proposed = plan(intent, read(args.run), identity)
    if args.confirm:
        if args.confirm != proposed["confirmation"]:
            raise ValueError("confirmation differs from fresh evidence")
        result = apply_plan(proposed, identity=identity, symbol=intent["symbol"],
                            operator=args.operator, read_detail=read, replan=replan)
    else:
        result = {"dry_run": True, **proposed}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
