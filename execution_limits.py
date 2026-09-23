"""Deployment-bound limits. Missing limits block new orders, never recovery.

Notional is an estimate at the last verified quote, not a guaranteed MARKET fill
price. Session reservations count possible attempts (including crashes) and are
never refunded automatically.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json


ENV_KEYS = ("LEGO_MAX_ORDER_QUANTITY", "LEGO_MAX_ORDER_NOTIONAL_USD",
            "LEGO_MAX_SESSION_ORDERS", "LEGO_TRADING_WINDOW_END")


class ExecutionLimitError(ValueError):
    pass


def policy_hash(values) -> str:
    return hashlib.sha256(json.dumps(list(values), separators=(",", ":")).encode()).hexdigest()


def positive(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ExecutionLimitError("invalid execution limit or order value") from None
    if not result.is_finite() or result <= 0:
        raise ExecutionLimitError("execution limits and order values must be finite and positive")
    return result


@dataclass(frozen=True)
class ExecutionLimits:
    quantity: Decimal
    notional: Decimal
    orders: int
    end: datetime
    fingerprint: str

    @classmethod
    def parse(cls, values):
        if len(values) != 4 or not all(values):
            raise ExecutionLimitError("all four execution limits must be explicitly configured")
        quantity, notional = positive(values[0]), positive(values[1])
        try:
            orders = int(values[2])
            end = datetime.fromisoformat(values[3].replace("Z", "+00:00"))
        except (TypeError, ValueError, AttributeError):
            raise ExecutionLimitError("invalid session order count or trading window") from None
        if orders <= 0 or str(orders) != str(values[2]) or end.tzinfo is None:
            raise ExecutionLimitError("session count must be positive integer; window must include timezone")
        return cls(quantity, notional, orders, end, policy_hash(values))

    def check(self, quantity, price, *, now=None) -> dict:
        now = now or datetime.now(timezone.utc)
        if now >= self.end:
            raise ExecutionLimitError("trading window expired")
        qty, quote = positive(quantity), positive(price)
        if qty > self.quantity:
            raise ExecutionLimitError("order quantity exceeds deployment limit")
        if qty * quote > self.notional:
            raise ExecutionLimitError("estimated order notional exceeds deployment limit")
        return {"policy_hash": self.fingerprint, "estimated_notional_usd": str(qty * quote),
                "quantity": str(qty), "checked_at": now.isoformat()}


def reserve_attempt(scope, claim, run_id, limits, session_key, *, now=None):
    """Reserve under the *same* account-symbol lease as Place, transactionally.

    A session key is derived from the immutable window end, so changing code,
    limits, or the spelling of the same timestamp cannot reset consumed slots.
    One bounded counter per account-symbol; an older window cannot replace it.
    """
    from firebase_admin import db
    from lego_outbox import DISPATCH_LOCK_PATH
    now = now or datetime.now(timezone.utc)
    if now >= limits.end:
        raise ExecutionLimitError("trading window expired")
    ref = db.reference(f"{DISPATCH_LOCK_PATH}/{scope}")

    def txn(old):
        if not isinstance(old, dict):
            return old
        doc = dict(old)
        try:
            lease = datetime.fromisoformat(doc.get("lease_until", "").replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return old
        if (doc.get("owner") != claim.get("owner")
                or doc.get("claim_token") != claim.get("claim_token")
                or doc.get("inflight_run_id") != run_id
                or lease.tzinfo is None or lease <= now):
            return old
        session = doc.get("execution_session") or {}
        if session.get("key") != session_key:
            if session and float(session.get("end_epoch", float("inf"))) >= limits.end.timestamp():
                return old
            session = {"key": session_key, "end_epoch": limits.end.timestamp(), "count": 0}
        count = session.get("count")
        if type(count) is not int or count < 0:
            return old
        if session.get("last_run_id") == run_id:
            return old
        if count >= limits.orders:
            return old
        doc["execution_session"] = {**session, "count": count + 1,
                                    "last_run_id": run_id, "reserved_at": now.isoformat()}
        return doc

    result = ref.transaction(txn) or {}
    session = result.get("execution_session") or {}
    try:
        lease = datetime.fromisoformat(result.get("lease_until", "").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise ExecutionLimitError("dispatch lease missing") from None
    if (result.get("owner") != claim.get("owner")
            or result.get("claim_token") != claim.get("claim_token")
            or result.get("inflight_run_id") != run_id
            or lease.tzinfo is None or lease <= now
            or type(session.get("count")) is not int or not 0 < session["count"] <= limits.orders
            or session.get("key") != session_key or session.get("last_run_id") != run_id):
        raise ExecutionLimitError("session attempt limit reached or dispatch lease lost")
    return {"session_key": session_key, "reservation_count": session["count"]}
