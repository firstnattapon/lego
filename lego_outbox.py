"""Multi-intent RTDB order outbox, independent from the DNA state pointer."""
from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone
from firebase_admin import db

from lego_orders import normalize_status as _normalize_status

OUTBOX_PATH = "webull_lego_order_outbox"
ROWS_PATH = "webull_lego_rows"
DISPATCH_LOCK_PATH = "webull_lego_order_dispatch_locks"
# Terminal for the *outbox*: nothing more to dispatch. Wider than
# lego_orders.TERMINAL_STATUSES, which only means the broker is done.
TERMINAL = {
    "FILLED", "CANCELLED", "FAILED", "REJECTED", "EXPIRED_UNSENT",
    # The broker's own EXPIRED — a DAY order the session ended on — as opposed to
    # EXPIRED_UNSENT, which is ours for an intent that never left. It was missing
    # here while lego_orders.TERMINAL_STATUSES had it, so such an intent stayed
    # actionable forever: no branch in the dispatcher handles it, yet
    # list_actionable kept serving it oldest-first. Three of them fill
    # LEGO_ORDER_WORKER_LIMIT and no later decision is ever dispatched again —
    # the same starvation RECONCILE_ABANDONED was added to prevent.
    "EXPIRED",
    "SUPPRESSED_ACTIVE_ORDER", "SUPPRESSED_STATE_CHANGED", "NOT_PLACED",
    "UNSENT_ABORTED",
    # The broker never resolved this order and the reconcile budget ran out.
    # Terminal for dispatch only — the order audit keeps needs_manual_check so
    # a human still answers whether the order exists.
    "RECONCILE_ABANDONED",
    # The broker confirmed the fill; only the 17-column model ledger could not
    # book it. Terminal for the same reason as REALIZED_MATH_ERROR below —
    # re-sending would duplicate a filled order — and needs_manual_check keeps
    # the ΔAₙ/Aₙ/Eₙ gap visible instead of retrying arithmetic forever.
    "CASHFLOW_FINALIZE_ERROR",
    # The broker confirmed the fill; only our realized math could not use it.
    # Nothing is left to dispatch — re-sending would duplicate a filled order —
    # so it must leave the queue, and needs_manual_check keeps the ledger gap
    # visible. Without this it would sit actionable forever and starve later
    # decisions exactly the way RECONCILE_ABANDONED was introduced to prevent.
    "REALIZED_MATH_ERROR",
}

# These outcomes are valid only while the broker has never been called.  A
# delayed pre-Place worker must not turn an attempted order into an unsent one.
UNSENT_TERMINAL = {
    "EXPIRED_UNSENT", "SUPPRESSED_ACTIVE_ORDER", "SUPPRESSED_STATE_CHANGED",
    "NOT_PLACED", "UNSENT_ABORTED",
}


class StaleIntentClaim(RuntimeError):
    """A worker lost the intent generation before its result could commit."""


def normalize_status(value) -> str:
    """Same normalization as lego_orders; a missing status reads as UNKNOWN."""
    return _normalize_status(value or "UNKNOWN")


def account_symbol_fence_key(runtime_identity: str, symbol: str) -> str:
    """Stable money fence across strategy/DNA/config chain changes."""
    identity = str(runtime_identity or "").strip()
    normalized_symbol = str(symbol or "").strip().upper()
    if not identity or not normalized_symbol:
        raise ValueError("account-symbol fence ต้องมี runtime identity และ symbol")
    digest = hashlib.sha256(
        f"lego-account-symbol-fence-v2\0{identity}\0{normalized_symbol}".encode()
    ).hexdigest()[:24]
    return f"{normalized_symbol}_{digest}"


def _dispatch_lease_seconds() -> int:
    """Lease for the one worker allowed to cross a chain's money boundary."""
    return max(5, int(os.environ.get(
        "LEGO_CHAIN_DISPATCH_LEASE_SECONDS", "120")))


def claim_chain_dispatch(chain_key: str, worker_id: str, *,
                         now_utc: datetime | None = None,
                         lease_seconds: int | None = None) -> dict | None:
    """Lease the irreversible order path for one strategy chain.

    Per-intent claims stop two workers sending the *same* client_order_id.  They
    do not stop those workers claiming two different run_ids, both observing an
    empty open-order list, and placing concurrently.  This lease serializes that
    cross-intent window while leaving all chains independent.

    An expired owner is replaceable.  ``claim_token`` is unique per acquisition,
    so an old worker can neither renew nor release a successor's lease even when
    a process id is accidentally reused.
    """
    now_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    lease_seconds = _dispatch_lease_seconds() if lease_seconds is None else max(
        1, int(lease_seconds))
    lease_until = now_utc + timedelta(seconds=lease_seconds)
    lease_text = lease_until.strftime("%Y-%m-%dT%H:%M:%SZ")
    token = uuid.uuid4().hex
    ref = db.reference(f"{DISPATCH_LOCK_PATH}/{chain_key}")

    def txn(current):
        doc = dict(current or {})
        owner = str(doc.get("owner") or "")
        active_until = _parse_utc(doc.get("lease_until"))
        if owner and owner != worker_id and active_until and active_until > now_utc:
            return doc
        doc.update({
            "owner": worker_id,
            "claim_token": token,
            "generation": int(doc.get("generation", 0) or 0) + 1,
            "claimed_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "lease_until": lease_text,
        })
        # An unresolved broker call survives owner expiry.  The successor may
        # reconcile that run_id, but may not forget it and dispatch another one.
        if not doc.get("inflight_run_id"):
            doc.pop("place_fence", None)
            doc.pop("fenced_run_id", None)
        return doc

    result = ref.transaction(txn)
    if not isinstance(result, dict) or result.get("claim_token") != token:
        return None
    return result


def fence_chain_dispatch(chain_key: str, run_id: str, worker_id: str,
                         claim_token: str, *,
                         intent_chain_key: str | None = None,
                         now_utc: datetime | None = None,
                         lease_seconds: int | None = None) -> dict | None:
    """Revalidate and renew the chain lease immediately before ``place_order``.

    A worker whose lease expired loses this transaction after a successor claims
    the chain.  Its stale result therefore cannot cross the irreversible call.
    """
    now_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    lease_seconds = _dispatch_lease_seconds() if lease_seconds is None else max(
        1, int(lease_seconds))
    lease_text = (now_utc + timedelta(seconds=lease_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    fence = f"{claim_token}:{run_id}"
    ref = db.reference(f"{DISPATCH_LOCK_PATH}/{chain_key}")

    def txn(current):
        if not isinstance(current, dict):
            return current
        doc = dict(current)
        active_until = _parse_utc(doc.get("lease_until"))
        inflight = str(doc.get("inflight_run_id") or "")
        if (doc.get("owner") != worker_id
                or doc.get("claim_token") != claim_token
                or active_until is None or active_until <= now_utc
                # Once a broker call may have started, no later run may replace
                # that uncertainty merely because it acquired the owner lease.
                or (inflight and inflight != str(run_id))):
            return doc
        if not inflight and (doc.get("broker_reject_circuit") or {}).get("halted"):
            return doc
        operator_stop = doc.get("operator_halt") or {}
        if not inflight and (operator_stop.get("halted")
                             or operator_stop.get("audit_pending_event")):
            return doc
        doc.update({
            "lease_until": lease_text,
            "place_fence": fence,
            "fenced_run_id": str(run_id),
            # Durable across owner release/expiry.  A successor must reconcile
            # this run before any different intent can cross place_order.
            "inflight_run_id": str(run_id),
            "inflight_chain_key": str(intent_chain_key or chain_key),
            "fenced_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        return doc

    result = ref.transaction(txn)
    if not isinstance(result, dict) or result.get("place_fence") != fence:
        return None
    return result


def release_chain_dispatch(chain_key: str, worker_id: str,
                           claim_token: str) -> None:
    """Release owner lease only; unresolved ``inflight_run_id`` stays durable."""
    ref = db.reference(f"{DISPATCH_LOCK_PATH}/{chain_key}")

    def txn(current):
        if not isinstance(current, dict):
            return current
        doc = dict(current)
        if (doc.get("owner") != worker_id
                or doc.get("claim_token") != claim_token):
            return doc
        doc.update({"owner": "", "claim_token": "", "lease_until": ""})
        return doc

    ref.transaction(txn)


def clear_chain_dispatch_inflight(chain_key: str, run_id: str, worker_id: str,
                                  claim_token: str) -> bool:
    """Clear a resolved broker run, but only for the current chain owner."""
    ref = db.reference(f"{DISPATCH_LOCK_PATH}/{chain_key}")
    clear_token = uuid.uuid4().hex

    def txn(current):
        if not isinstance(current, dict):
            return current
        doc = dict(current)
        if (doc.get("owner") != worker_id
                or doc.get("claim_token") != claim_token
                or str(doc.get("inflight_run_id") or "") != str(run_id)):
            return doc
        doc.pop("inflight_run_id", None)
        doc.pop("place_fence", None)
        doc.pop("fenced_run_id", None)
        doc.pop("fenced_at", None)
        doc.pop("inflight_chain_key", None)
        # A transaction callback may be retried.  A durable token in its result,
        # unlike a closure side effect, proves that *this* attempt cleared it.
        doc.update({
            "last_clear_token": clear_token,
            "last_cleared_run_id": str(run_id),
            "cleared_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
        })
        return doc

    result = ref.transaction(txn)
    return (isinstance(result, dict)
            and result.get("last_clear_token") == clear_token
            and not result.get("inflight_run_id"))


def read_intent(chain_key: str, run_id: str) -> dict | None:
    payload = db.reference(f"{OUTBOX_PATH}/{chain_key}/{run_id}").get()
    if not isinstance(payload, dict):
        return None
    doc = dict(payload)
    doc.setdefault("run_id", str(run_id))
    return doc


def put_intent(chain_key: str, run_id: str, payload: dict) -> dict:
    """Idempotently create one intent per committed decision candidate."""
    ref = db.reference(f"{OUTBOX_PATH}/{chain_key}/{run_id}")
    doc = dict(payload)
    doc.update({
        "run_id": run_id,
        "client_order_id": run_id,
        "chain_key": chain_key,
        "status": normalize_status(doc.get("status") or "PENDING_DISPATCH"),
    })
    doc["actionable_sort"] = str(
        doc.get("slot_start_utc") or doc.get("created_at") or run_id)
    doc["audit_pending"] = True
    doc["audit_revision"] = 1

    def txn(current):
        if current:
            return current
        return doc

    return ref.transaction(txn) or doc


def update_intent(chain_key: str, run_id: str, fields: dict, *,
                  expected_claim_owner: str | None = None,
                  expected_claim_generation: int | None = None) -> dict:
    if (expected_claim_owner is None) != (expected_claim_generation is None):
        raise ValueError("claim owner and generation must be supplied together")
    ref = db.reference(f"{OUTBOX_PATH}/{chain_key}/{run_id}")

    def txn(current):
        current = dict(current or {})
        if expected_claim_owner is not None:
            active_until = _parse_utc(current.get("claim_until"))
            if (current.get("claim_owner") != expected_claim_owner
                    or int(current.get("claim_generation", 0) or 0)
                    != int(expected_claim_generation)
                    or active_until is None
                    or active_until <= datetime.now(timezone.utc)):
                raise StaleIntentClaim("intent claim changed or expired")
        incoming = dict(fields)
        old_status = normalize_status(current.get("status"))
        new_status = normalize_status(incoming.get("status")) if "status" in incoming else old_status
        # A terminal broker/ledger outcome is an accounting snapshot, not just
        # a status label.  A stale lease holder may return after a successor has
        # finalized the order; allowing its non-status fields through can turn
        # FILLED/qty=1 into FILLED/qty=0 and incorrectly affect the money fence.
        # Audit repair is the only ordinary post-terminal update.  Operator
        # reconciliation uses its own explicit transaction and evidence checks.
        if old_status in TERMINAL:
            if expected_claim_owner is not None:
                raise StaleIntentClaim("intent became terminal before result write")
            if set(incoming) == {"audit_pending"}:
                current["audit_pending"] = incoming["audit_pending"]
                if incoming["audit_pending"] is True:
                    current["audit_revision"] = int(current.get("audit_revision", 0)) + 1
            return current
        # An attempted order cannot become an unsent outcome, even if an old
        # worker made that decision using a snapshot from before Place started.
        if current.get("place_attempted") is True and new_status in UNSENT_TERMINAL:
            return current
        if new_status == "PENDING_DISPATCH" and old_status not in {
                "", "UNKNOWN", "PENDING_DISPATCH"}:
            return current
        current.update(incoming)
        current["status"] = normalize_status(current.get("status"))
        if current["status"] != old_status or fields.get("audit_pending") is True:
            current["audit_pending"] = True
            current["audit_revision"] = int(current.get("audit_revision", 0)) + 1
        current["actionable_sort"] = (
            None if current["status"] in TERMINAL
            else str(current.get("slot_start_utc")
                     or current.get("created_at") or run_id))
        if set(fields) != {"audit_pending"}:
            current["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return current

    return ref.transaction(txn) or {}


def acknowledge_audit(chain_key: str, run_id: str, revision: int) -> bool:
    """A late mirror may never clear the marker for a newer transition."""
    ref = db.reference(f"{OUTBOX_PATH}/{chain_key}/{run_id}")
    def txn(current):
        if not isinstance(current, dict) or int(current.get("audit_revision", 0)) != revision:
            return current
        return {**current, "audit_pending": False}
    result = ref.transaction(txn)
    return bool(isinstance(result, dict) and not result.get("audit_pending")
                and int(result.get("audit_revision", 0)) == revision)


def _claim_lease_seconds() -> int:
    return max(5, int(os.environ.get("LEGO_ORDER_CLAIM_LEASE_SECONDS", "120")))


def _parse_utc(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def claim_intent(chain_key: str, run_id: str, worker_id: str, *,
                 now_utc: datetime | None = None,
                 lease_seconds: int | None = None) -> dict | None:
    """Atomically lease one non-terminal intent without changing its status."""
    now_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    lease_seconds = _claim_lease_seconds() if lease_seconds is None else max(
        1, int(lease_seconds))
    lease_until = now_utc + timedelta(seconds=lease_seconds)
    lease_text = lease_until.strftime("%Y-%m-%dT%H:%M:%SZ")
    ref = db.reference(f"{OUTBOX_PATH}/{chain_key}/{run_id}")

    def txn(current):
        if not isinstance(current, dict):
            return current
        doc = dict(current)
        if normalize_status(doc.get("status")) in TERMINAL:
            return doc
        owner = str(doc.get("claim_owner") or "")
        active_until = _parse_utc(doc.get("claim_until"))
        if owner and owner != worker_id and active_until and active_until > now_utc:
            return doc
        doc.update({
            "claim_owner": worker_id,
            "claim_until": lease_text,
            "claim_generation": int(doc.get("claim_generation", 0) or 0) + 1,
        })
        return doc

    result = ref.transaction(txn)
    if not isinstance(result, dict):
        return None
    if result.get("claim_owner") != worker_id or result.get("claim_until") != lease_text:
        return None
    return result


def renew_intent_claim(chain_key: str, run_id: str, worker_id: str,
                       claim_generation: int, *,
                       now_utc: datetime | None = None,
                       lease_seconds: int | None = None) -> dict | None:
    """Validate the current generation and extend its lease before accounting.

    This prevents a worker that already lost the claim from applying a broker
    snapshot. It cannot make the separate RTDB ledger paths atomic with outbox.
    """
    now_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    lease_seconds = _claim_lease_seconds() if lease_seconds is None else max(
        1, int(lease_seconds))
    lease_text = (now_utc + timedelta(seconds=lease_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    ref = db.reference(f"{OUTBOX_PATH}/{chain_key}/{run_id}")

    def txn(current):
        if not isinstance(current, dict):
            return current
        doc = dict(current)
        active_until = _parse_utc(doc.get("claim_until"))
        if (normalize_status(doc.get("status")) in TERMINAL
                or doc.get("claim_owner") != worker_id
                or int(doc.get("claim_generation", 0) or 0) != int(claim_generation)
                or active_until is None or active_until <= now_utc):
            return doc
        if active_until < _parse_utc(lease_text):
            doc["claim_until"] = lease_text
        return doc

    result = ref.transaction(txn)
    if not isinstance(result, dict):
        return None
    active_until = _parse_utc(result.get("claim_until"))
    if (normalize_status(result.get("status")) in TERMINAL
            or result.get("claim_owner") != worker_id
            or int(result.get("claim_generation", 0) or 0) != int(claim_generation)
            or active_until is None or active_until <= now_utc):
        return None
    return result


def recover_fenced_intent(chain_key: str, run_id: str) -> dict | None:
    """Mark a chain-fenced run uncertain only while it is still undispatched.

    The dispatch owner calls this after finding a durable inflight run with an
    outbox status that predates the Place marker. A newer broker observation may
    commit between its read and this transaction, so the transition must be
    conditional on the current status.
    """
    ref = db.reference(f"{OUTBOX_PATH}/{chain_key}/{run_id}")

    def txn(current):
        if not isinstance(current, dict):
            return current
        doc = dict(current)
        if normalize_status(doc.get("status")) != "PENDING_DISPATCH":
            return doc
        doc.update({
            "status": "PLACING_UNKNOWN",
            "place_attempted": True,
            "recovered_chain_fence": True,
            "audit_pending": True,
            "audit_revision": int(doc.get("audit_revision", 0)) + 1,
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        return doc

    return ref.transaction(txn)


def begin_place_attempt(chain_key: str, run_id: str, worker_id: str,
                        claim_generation: int) -> dict | None:
    """Fence the irreversible broker call behind the current claim generation."""
    ref = db.reference(f"{OUTBOX_PATH}/{chain_key}/{run_id}")
    # Unique per invocation, not merely per lease generation. Firebase may run
    # two callbacks concurrently with the same owner/generation; only the
    # invocation whose random token survives the transaction may place.
    fence = f"{worker_id}:{int(claim_generation)}:{uuid.uuid4().hex}"

    def txn(current):
        if not isinstance(current, dict):
            return current
        doc = dict(current)
        if normalize_status(doc.get("status")) != "PENDING_DISPATCH":
            return doc
        if doc.get("claim_owner") != worker_id:
            return doc
        if int(doc.get("claim_generation", 0) or 0) != int(claim_generation):
            return doc
        doc.update({
            "status": "PLACING_UNKNOWN",
            "place_attempted": True,
            "place_fence": fence,
            "audit_pending": True,
            "audit_revision": int(doc.get("audit_revision", 0)) + 1,
            "placed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "updated_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
        })
        return doc

    result = ref.transaction(txn)
    if not isinstance(result, dict) or result.get("place_fence") != fence:
        return None
    return result


def release_intent_claim(chain_key: str, run_id: str, worker_id: str) -> None:
    """Release only the lease owned by this worker; a newer owner is untouched."""
    ref = db.reference(f"{OUTBOX_PATH}/{chain_key}/{run_id}")

    def txn(current):
        if not isinstance(current, dict):
            return current
        doc = dict(current)
        if doc.get("claim_owner") == worker_id:
            doc.pop("claim_owner", None)
            doc.pop("claim_until", None)
        return doc

    ref.transaction(txn)


def list_actionable(chain_key: str, limit: int = 20) -> list[dict]:
    raw = (db.reference(f"{OUTBOX_PATH}/{chain_key}")
           .order_by_child("actionable_sort")
           .start_at("")
           .limit_to_first(max(1, int(limit)))
           .get() or {})
    rows = []
    for run_id, payload in raw.items():
        if not isinstance(payload, dict):
            continue
        status = normalize_status(payload.get("status"))
        if status in TERMINAL:
            continue
        doc = dict(payload)
        doc.setdefault("run_id", run_id)
        rows.append(doc)
    rows.sort(key=lambda x: (str(x.get("slot_start_utc") or x.get("created_at") or ""),
                             str(x.get("run_id") or "")))
    return rows[:limit]


def list_audit_pending(chain_key: str, limit: int = 100) -> list[dict]:
    """Intents whose outbox update committed but audit mirror still needs repair."""
    raw = (db.reference(f"{OUTBOX_PATH}/{chain_key}")
           .order_by_child("audit_pending")
           .equal_to(True)
           .limit_to_first(max(1, int(limit)))
           .get() or {})
    rows = []
    for run_id, payload in raw.items():
        if not isinstance(payload, dict) or not payload.get("audit_pending"):
            continue
        doc = dict(payload)
        doc.setdefault("run_id", run_id)
        rows.append(doc)
    rows.sort(key=lambda x: (
        str(x.get("updated_at") or x.get("created_at") or ""),
        str(x.get("run_id") or ""),
    ))
    return rows[:limit]


def read_committed_row(run_id: str) -> dict | None:
    """The committed row behind an intent, or None when it is not committed.

    The dispatcher already had to read this row to answer 'was it committed?';
    returning the document itself lets the submit gate compare the intent against
    what the engine actually persisted, at no extra read.
    """
    row = db.reference(f"{ROWS_PATH}/{run_id}").get()
    return row if isinstance(row, dict) and row.get("committed") is True else None


def row_is_committed(run_id: str) -> bool:
    return read_committed_row(run_id) is not None


def expire_unsent_before(chain_key: str, now_utc: datetime) -> int:
    count = 0
    for intent in list_actionable(chain_key, limit=100):
        if normalize_status(intent.get("status")) != "PENDING_DISPATCH":
            continue
        raw = intent.get("expires_at")
        if not raw:
            continue
        expiry = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if now_utc < expiry:
            continue
        ref = db.reference(f"{OUTBOX_PATH}/{chain_key}/{intent['run_id']}")
        expired_token = uuid.uuid4().hex

        def txn(current):
            if not isinstance(current, dict):
                return current
            doc = dict(current)
            # Recheck inside the transaction.  The candidate list can be stale
            # while another worker writes the durable Place marker.
            if (normalize_status(doc.get("status")) != "PENDING_DISPATCH"
                    or doc.get("place_attempted") is True
                    or doc.get("place_fence")):
                return doc
            active_until = _parse_utc(doc.get("claim_until"))
            if doc.get("claim_owner") and active_until and active_until > now_utc:
                return doc
            current_expiry = _parse_utc(doc.get("expires_at"))
            if current_expiry is None or now_utc < current_expiry:
                return doc
            doc.update({
                "status": "EXPIRED_UNSENT",
                "terminal_reason": "slot execution window expired before place",
                "audit_pending": True,
                "audit_revision": int(doc.get("audit_revision", 0)) + 1,
                "actionable_sort": None,
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "expired_token": expired_token,
            })
            return doc

        written = ref.transaction(txn)
        if isinstance(written, dict) and written.get("expired_token") == expired_token:
            count += 1
    return count
