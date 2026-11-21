"""Deployment-bound limits. Missing limits block new orders, never recovery.

Notional is an estimate at the last verified quote, not a guaranteed MARKET fill
price. Session reservations count possible attempts (including crashes) and are
never refunded automatically.

LEGO_MAX_SESSION_BUY_ORDERS bounds the *net* BUYs of a session (owner, 2026-11-21):
net = ``execution_session.buy_count`` (BUY reservations, counted before Place and never
refunded) − ``execution_session.sell_count`` (SELLs the broker reported filled,
credited by broker_circuit.record_outcome inside the same lock-document transaction).
A BUY is reserved only while net < LEGO_MAX_SESSION_BUY_ORDERS; net may go below zero
(SELL, SELL, then up to N + 2 BUYs). A SELL spends one total reservation, is never
blocked by the BUY cap, and frees a BUY only once it fills. A counter written before
buy_count existed is read as count (every earlier attempt may have been a BUY); one
written before sell_count existed has no filled SELL.

The dispatch-lock document stays schema 5: a revision that does not know sell_count
reads only buy_count, i.e. the stricter gross rule, so a rollback fails closed without a
schema bump and still carries sell_count over in ``{**session}``.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json


# Appended, never reordered: index 0..3 are read positionally (ops.py, release_horizon).
ENV_KEYS = ("LEGO_MAX_ORDER_QUANTITY", "LEGO_MAX_ORDER_NOTIONAL_USD",
            "LEGO_MAX_SESSION_ORDERS", "LEGO_TRADING_WINDOW_END",
            "LEGO_MAX_SESSION_BUY_ORDERS")
# The dispatch-lock document this module writes. 5 = execution_session.buy_count. A
# revision that only knows 1..4 refuses it, so rolling back to it fails closed instead
# of reserving BUYs against a counter it does not advance.
WORKER_SCHEMA_VERSION = 5


class ExecutionLimitError(ValueError):
    pass


class SessionBuyCapReached(ExecutionLimitError):
    """The net BUY quota of this session is spent. Expected in a long fall, not a fault."""


def buy_count_of(session) -> int:
    """BUY reservations of a counter; one written before buy_count is read as count."""
    if not session:
        return 0
    if "buy_count" in session:
        return session["buy_count"]
    return session.get("count", 0)


def sell_count_of(session) -> int:
    """Filled SELLs credited to a counter; one written before sell_count has none."""
    if not session:
        return 0
    return session.get("sell_count", 0)


def net_buy_count_of(session) -> int:
    """BUY reservations minus filled SELLs; below zero after a session that sold first."""
    return buy_count_of(session) - sell_count_of(session)


def buy_cap_reason(limits, session) -> str:
    return (f"session net BUY limit reached (net {net_buy_count_of(session)} = "
            f"{buy_count_of(session)} BUY - {sell_count_of(session)} filled SELL; "
            f"max {limits.buy_orders} net BUY per session)")


def validate_counter(counter):
    if not isinstance(counter, dict):
        raise ExecutionLimitError("malformed migration counter")
    if not counter:
        return
    if (not isinstance(counter.get("key"), str) or not counter["key"].strip()
            or type(counter.get("count")) is not int or counter["count"] < 0):
        raise ExecutionLimitError("malformed execution counter")
    if "buy_count" in counter and (type(counter["buy_count"]) is not int
                                   or not 0 <= counter["buy_count"] <= counter["count"]):
        raise ExecutionLimitError("malformed BUY counter")
    # Each reservation is one BUY or one SELL, and only a reserved SELL can be credited.
    if "sell_count" in counter and (type(counter["sell_count"]) is not int
                                    or counter["sell_count"] < 0
                                    or buy_count_of(counter) + counter["sell_count"]
                                    > counter["count"]):
        raise ExecutionLimitError("malformed SELL counter")
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
        # Every attempt before buy_count existed may have been a BUY.
        buys = max(buy_count_of(session), buy_count_of(daily))
        doc["execution_session"] = {"key": key, "count": count, "buy_count": min(buys, count),
                                    "last_run_id": session.get("last_run_id", "")}
        doc["market_day_migration"] = {"key": key, "count": count,
                                       "at": now.isoformat()}
        doc["worker_schema_version"] = WORKER_SCHEMA_VERSION
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
    buy_orders: int = 0

    @classmethod
    def parse(cls, values):
        if len(values) != len(ENV_KEYS) or not all(values):
            raise ExecutionLimitError("all five execution limits must be explicitly configured")
        quantity, notional = positive(values[0]), positive(values[1])
        try:
            orders = int(values[2])
            end = datetime.fromisoformat(values[3].replace("Z", "+00:00"))
            buy_orders = int(values[4])
        except (TypeError, ValueError, AttributeError):
            raise ExecutionLimitError("invalid session order count or trading window") from None
        if orders <= 0 or str(orders) != str(values[2]) or end.tzinfo is None:
            raise ExecutionLimitError("session count must be positive integer; window must include timezone")
        if buy_orders <= 0 or str(buy_orders) != str(values[4]) or buy_orders > orders:
            raise ExecutionLimitError(
                "session BUY count must be a positive integer no larger than the session count")
        return cls(quantity, notional, orders, end, policy_hash(values), buy_orders)

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


def buy_quota_spent(scope, limits, session_key):
    """Advisory, read-only: the session counter when its net BUY quota is spent, else None.

    Lets the worker stop a BUY before it spends a broker Preview. Never the
    authority: reserve_attempt decides inside its transaction.
    """
    from firebase_admin import db
    from lego_outbox import DISPATCH_LOCK_PATH
    doc = db.reference(f"{DISPATCH_LOCK_PATH}/{scope}").get()
    if not isinstance(doc, dict):
        return None
    session = doc.get("execution_session") or {}
    try:
        validate_counter(session)
    except ExecutionLimitError:
        return None                      # malformed: reserve_attempt refuses it with its reason
    if (session.get("key") == session_key
            and net_buy_count_of(session) >= limits.buy_orders):
        return dict(session)
    return None


def credit_filled_sell(doc, run_id, now) -> bool:
    """Credit one filled SELL to the counter this run reserved in; pure, never raises.

    Called by broker_circuit.record_outcome inside its lock-document transaction, so the
    credit and the outcome receipt are one write. ``doc`` is that transaction's working
    copy: the counter is replaced by a new dict, never edited in place, because a
    transaction callback may run again on the same input. No credit unless the counter's
    latest reservation is this run (same session), this run was not credited before, the
    counter is well formed and still has an uncredited SELL reservation.
    """
    session = doc.get("execution_session")
    if not isinstance(session, dict) or not session or not run_id:
        return False
    try:
        validate_counter(session)
    except ExecutionLimitError:
        return False
    if (session.get("last_run_id") != run_id
            or session.get("last_sell_credit_run_id") == run_id):
        return False
    sells = sell_count_of(session)
    if sells >= session["count"] - buy_count_of(session):
        return False
    doc["execution_session"] = {**session, "sell_count": sells + 1,
                                "last_sell_credit_run_id": run_id,
                                "sell_credited_at": str(now)}
    return True


def reserve_attempt(scope, claim, run_id, limits, session_key, *, side, now=None):
    """Reserve under the *same* account-symbol lease as Place, transactionally.

    Market-day keys are independent of release expiry and chain identity.
    Legacy window counters require an explicit idle migration to market-day.
    One bounded counter per account-symbol; keys cannot move backwards.
    A BUY also needs net BUYs (BUY reservations − filled SELLs) below limits.buy_orders.
    """
    side = str(side or "").strip().upper()
    if side not in ("BUY", "SELL"):
        raise ExecutionLimitError("reservation needs the order side")
    buy = side == "BUY"
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
        if type(schema) is not int or not 1 <= schema <= WORKER_SCHEMA_VERSION:
            raise ExecutionLimitError("unsupported worker schema")
        if session.get("key") != session_key:
            if session_key.startswith("XNYS:"):
                if session and (not str(session.get("key", "")).startswith("XNYS:")
                                or session["key"] >= session_key):
                    return old  # explicit legacy migration; never roll backwards
            elif session and (str(session.get("key", "")).startswith("XNYS:")
                              or float(session.get("end_epoch", float("inf"))) >= limits.end.timestamp()):
                return old
            session = {"key": session_key, "end_epoch": limits.end.timestamp(), "count": 0,
                       "buy_count": 0, "sell_count": 0}
        count = session.get("count")
        if type(count) is not int or count < 0:
            return old
        if session.get("last_run_id") == run_id:
            return old
        if count >= limits.orders:
            return old
        buys = buy_count_of(session)
        if buy and net_buy_count_of(session) >= limits.buy_orders:
            return old
        doc["execution_session"] = {**session, "count": count + 1,
                                    "buy_count": min(buys + (1 if buy else 0), count + 1),
                                    "last_run_id": run_id, "reserved_at": now.isoformat()}
        doc["worker_schema_version"] = WORKER_SCHEMA_VERSION
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
        if (buy and result.get("owner") == claim.get("owner")
                and result.get("claim_token") == claim.get("claim_token")
                and result.get("inflight_run_id") == run_id
                and lease.tzinfo is not None and lease > now
                and session.get("key") == session_key
                and type(session.get("count")) is int and session["count"] < limits.orders
                and net_buy_count_of(session) >= limits.buy_orders):
            raise SessionBuyCapReached(buy_cap_reason(limits, session))
        raise ExecutionLimitError("session attempt limit reached or dispatch lease lost")
    return {"session_key": session_key, "reservation_count": session["count"],
            "buy_reservation_count": buy_count_of(session),
            "sell_fill_count": sell_count_of(session),
            "net_buy_count": net_buy_count_of(session)}
