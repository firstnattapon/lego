"""Read-only blocker witnesses; they never grant Place or Cancel permission."""
from datetime import datetime, timezone
import hashlib

from firebase_admin import db
from lego_outbox import DISPATCH_LOCK_PATH, StaleIntentClaim
from lego_orders import normalize_status


def describe(orders, *, now=None):
    now = now or datetime.now(timezone.utc)
    return {"observed_at": now.isoformat(), "count": len(orders),
            "orders": [{"client_order_id": order["client_order_id"],
                        "status": normalize_status(order.get("status")),
                        "symbol": order["symbol"]} for order in orders]}


def public(witness):
    if not witness or not witness.get("count"):
        return {}
    return {"open_order_blocked": True,
            "open_order_count": witness["count"],
            "open_order_observed_at": witness["observed_at"],
            "open_order_fingerprints": [hashlib.sha256(
                item["client_order_id"].encode()).hexdigest()[:16]
                for item in witness["orders"]]}


def record(scope, claim, orders, *, now=None):
    """Store the last complete read under the existing account-symbol lease.

    Idle ticks report that observation without querying the broker again. Only
    a later complete open-order read may clear it. This is monitoring state,
    independent of the durable inflight fence and mutation authorization.
    """
    now = now or datetime.now(timezone.utc)
    witness = describe(orders, now=now) if orders else None
    ref = db.reference(f"{DISPATCH_LOCK_PATH}/{scope}")

    def txn(old):
        if not isinstance(old, dict):
            raise StaleIntentClaim("open-order observation lease missing")
        try:
            end = datetime.fromisoformat(old.get("lease_until", "").replace("Z", "+00:00"))
        except (ValueError, TypeError):
            raise StaleIntentClaim("open-order observation lease invalid") from None
        if (not claim.get("owner") or not claim.get("claim_token")
                or old.get("owner") != claim.get("owner")
                or old.get("claim_token") != claim.get("claim_token")
                or end.tzinfo is None or end <= now):
            raise StaleIntentClaim("open-order observation lease changed")
        return {**old, "broker_open_order_blocker": witness}

    ref.transaction(txn)
    claim["broker_open_order_blocker"] = witness
    return witness
