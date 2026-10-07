"""Private immutable events, with an outbox-local transactional replay marker."""
from datetime import datetime, timezone
import os
from firebase_admin import db

PATH = "webull_lego_execution_transitions"
FIELDS = ("status", "broker_status", "place_attempted", "placed_at", "cancel_attempt_count",
          "cancel_requested_at", "cancel_confirmed_at", "cancel_last_error_code",
          "cancel_confirmation_deadline", "cancel_refused_at", "cancel_policy",
          "needs_manual_check", "filled_quantity", "filled_price", "filled_fee",
          "cashflow_finalized", "realized", "cancel_policy_hash",
          "manual_since", "reconcile_evidence_sha256", "reconcile_resume",
          "expiry_released_at", "expiry_proof_sha256")


def enqueue(doc, old=None):
    """Called INSIDE the intent transaction; never performs I/O here."""
    old = old or {}
    # RTDB omits null children. Normalize before hashing/comparing/replaying.
    current = {k: doc[k] for k in FIELDS if doc.get(k) is not None}
    if old and current == {k: old[k] for k in FIELDS if old.get(k) is not None}:
        return doc
    pending = dict(doc.get("transition_pending") or {})
    if len(pending) >= 32:
        raise RuntimeError("transition audit backlog full; repair before mutation")
    revision = int(doc.get("transition_revision", 0)) + 1
    event = {"revision": revision, "at": datetime.now(timezone.utc).isoformat(),
             "candidate_hash": doc.get("candidate_hash") or os.environ.get("LEGO_CANDIDATE_HASH", ""),
             "deployment_revision": os.environ.get("K_REVISION", ""),
             "intent_revision": int(doc.get("audit_revision", 0)),
             "release_binding": doc.get("release_binding") or "",
             "state": current}
    pending[f"r_{revision:012d}"] = event
    doc.update(transition_revision=revision, transition_pending=pending)
    return doc


def replay(chain_key, run_id):
    OUTBOX_PATH = "webull_lego_order_outbox"
    ref = db.reference(f"{OUTBOX_PATH}/{chain_key}/{run_id}")
    for revision, event in ((ref.get() or {}).get("transition_pending") or {}).items():
        target = db.reference(f"{PATH}/{chain_key}/{run_id}/{revision}")
        def append(old):
            if old is not None and old != event:
                raise RuntimeError("immutable transition audit conflict")
            return event
        target.transaction(append)
        def ack(old):
            if not old:
                return old
            doc = dict(old)
            pending = dict(doc.get("transition_pending") or {})
            if pending.get(revision) == event:
                pending.pop(revision)
            doc["transition_pending"] = pending or None
            return doc
        ref.transaction(ack)
