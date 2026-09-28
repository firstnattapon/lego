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


def validate_counter(counter):
    if not isinstance(counter, dict):
        raise ExecutionLimitError("malformed migration counter")
    if not counter:
        return
    if (not isinstance(counter.get("key"), str) or not counter["key"].strip()
            or type(counter.get("count")) is not int or counter["count"] < 0):
        raise ExecutionLimitError("malformed execution counter")
    if counter["key"].startswith("XNYS:"):
        from datetime import date
        try:
            canonical = "XNYS:" + date.fromisoformat(counter["key"][5:]).isoformat()
        except ValueError:
            raise ExecutionLimitError("malformed market-day key") from None
        if counter["key"] != canonical:
            raise ExecutionLimitError("noncanonical market-day key")
    else:
        positive(counter["key"])


def session_key_for(mode, limits, *, now):
    if mode == "release_window":
        return str(limits.end.timestamp())
    if mode != "market_day":
        raise ExecutionLimitError("unknown session key mode")
    from market_clock import NY, session_bounds
    day = now.astimezone(NY).date()
    bounds = session_bounds(day)
    if not bounds or not bounds[0] <= now < bounds[1]:
        raise ExecutionLimitError("new reservation outside regular market session")
    return f"XNYS:{day.isoformat()}"


def migrate_market_day(scope, *, now, apply=False):
    """Conservative idle cutover. Caller must stop new-order deployments first."""
    from firebase_admin import db
    from lego_outbox import DISPATCH_LOCK_PATH
    from market_clock import NY
    ref = db.reference(f"{DISPATCH_LOCK_PATH}/{scope}")
    key = f"XNYS:{now.astimezone(NY).date().isoformat()}"
    def plan(old):
        if old is not None and not isinstance(old, dict):
            raise ExecutionLimitError("malformed dispatch state")
        doc = dict(old or {})
        if doc.get("inflight_run_id") or doc.get("owner"):
            raise ExecutionLimitError("migration requires idle dispatch fence")
        session = doc.get("execution_session", {})
        daily = doc.get("market_day_migration", {})
        for counter in (session, daily):
            validate_counter(counter)
            if counter and counter["key"].startswith("XNYS:") and counter["key"] > key:
                raise ExecutionLimitError("market day regression")
        counts = [session.get("count", 0), daily.get("count", 0)]
        if any(type(n) is not int or n < 0 for n in counts):
            raise ExecutionLimitError("invalid migration count")
        if str(session.get("key", "")).startswith("XNYS:") and session["key"] > key:
            raise ExecutionLimitError("market day regression")
        count = max(counts)
        doc["execution_session"] = {"key": key, "count": count,
                                    "last_run_id": session.get("last_run_id", "")}
        doc["market_day_migration"] = {"key": key, "count": count,
                                       "at": now.isoformat()}
        doc["worker_schema_version"] = 4
        return doc
    result = ref.transaction(plan) if apply else plan(ref.get())
    return {"dry_run": not apply, "execution_session": result["execution_session"]}


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

    Market-day keys are independent of release expiry and chain identity.
    Legacy window counters require an explicit idle migration to market-day.
    One bounded counter per account-symbol; keys cannot move backwards.
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
        session = doc.get("execution_session", {})
        validate_counter(session)
        schema = doc.get("worker_schema_version", 1)
        if type(schema) is not int or not 1 <= schema <= 4:
            raise ExecutionLimitError("unsupported worker schema")
        if session.get("key") != session_key:
            if session_key.startswith("XNYS:"):
                if session and (not str(session.get("key", "")).startswith("XNYS:")
                                or session["key"] >= session_key):
                    return old  # explicit legacy migration; never roll backwards
            elif session and (str(session.get("key", "")).startswith("XNYS:")
                              or float(session.get("end_epoch", float("inf"))) >= limits.end.timestamp()):
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
        doc["worker_schema_version"] = 4
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
