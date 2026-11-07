"""LEGO_MAX_SESSION_BUY_ORDERS: at most N BUY orders per session (owner, 2026-11-07).

LEGO_MAX_SESSION_ORDERS counted BUY and SELL together, so every one of its 10 orders could
be a BUY. The BUY cap is a second bound inside the same transactional session counter
(execution_session.buy_count): a BUY spends one total and one BUY reservation, a SELL only
a total one and never refunds a BUY. A counter written before buy_count existed is read as
count, and the dispatch-lock document is schema 5 so a revision that does not advance
buy_count refuses it instead of reserving BUYs it does not count.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

import alerting
import observability
from conftest import FAKE_DB
from execution_limits import (ENV_KEYS, WORKER_SCHEMA_VERSION, ExecutionLimitError,
                              ExecutionLimits, SessionBuyCapReached, buy_count_of,
                              buy_quota_spent, migrate_market_day, reserve_attempt,
                              validate_counter)
from lego_outbox import DISPATCH_LOCK_PATH
from test_dispatch_overshoot import NOW, isolate  # noqa: F401  (autouse)

WINDOW = "2030-01-01T00:00:00Z"
LIMITS = ExecutionLimits.parse(("20", "150", "10", WINDOW, "3"))
KEY = "XNYS:2026-09-21"
CLAIM = {"owner": "worker", "claim_token": "token"}


def lock(**extra):
    ref = FAKE_DB.reference(f"{DISPATCH_LOCK_PATH}/scope")
    ref.set({**CLAIM, "inflight_run_id": "r0",
             "lease_until": (NOW + timedelta(seconds=120)).isoformat(), **extra})
    return ref


def reserve(ref, run_id, side, limits=LIMITS):
    ref.update({"inflight_run_id": run_id})
    return reserve_attempt("scope", CLAIM, run_id, limits, KEY, side=side, now=NOW)


def test_the_buy_cap_is_the_fifth_limit_appended_after_the_four_old_ones():
    assert ENV_KEYS[:4] == ("LEGO_MAX_ORDER_QUANTITY", "LEGO_MAX_ORDER_NOTIONAL_USD",
                            "LEGO_MAX_SESSION_ORDERS", "LEGO_TRADING_WINDOW_END")
    assert ENV_KEYS[4] == "LEGO_MAX_SESSION_BUY_ORDERS"
    assert (LIMITS.orders, LIMITS.buy_orders) == (10, 3)


def test_three_buys_then_the_fourth_is_refused_and_a_sell_still_goes():
    ref = lock()
    for n in range(1, 4):
        result = reserve(ref, f"buy{n}", "BUY")
        assert (result["reservation_count"], result["buy_reservation_count"]) == (n, n)
    with pytest.raises(SessionBuyCapReached, match="3 BUY orders per session"):
        reserve(ref, "buy4", "BUY")
    session = ref.get()["execution_session"]
    assert (session["count"], session["buy_count"]) == (3, 3)      # the refused BUY spent nothing
    sell = reserve(ref, "sell1", "sell")
    assert (sell["reservation_count"], sell["buy_reservation_count"]) == (4, 3)
    with pytest.raises(SessionBuyCapReached):
        reserve(ref, "buy5", "BUY")                                # a SELL never refunds a BUY
    assert ref.get()["worker_schema_version"] == WORKER_SCHEMA_VERSION == 5


def test_the_total_cap_still_binds_and_is_reported_as_the_old_limit():
    ref = lock()
    total = ExecutionLimits.parse(("20", "150", "2", WINDOW, "2"))
    reserve(ref, "a", "SELL", total), reserve(ref, "b", "BUY", total)
    with pytest.raises(ExecutionLimitError) as refused:
        reserve(ref, "c", "BUY", total)
    assert not isinstance(refused.value, SessionBuyCapReached)     # EXECUTION_LIMIT_BLOCKED, as before


def test_the_same_run_reserving_again_is_idempotent():
    ref = lock()
    first = reserve(ref, "one", "BUY")
    assert reserve(ref, "one", "BUY") == first
    assert ref.get()["execution_session"]["buy_count"] == 1


def test_a_new_market_day_starts_a_fresh_buy_quota():
    ref = lock()
    for n in range(3):
        reserve(ref, f"d1-{n}", "BUY")
    tomorrow = NOW + timedelta(days=1)
    ref.update({"inflight_run_id": "d2", "lease_until": (tomorrow + timedelta(seconds=120)).isoformat()})
    next_day = reserve_attempt("scope", CLAIM, "d2", LIMITS, "XNYS:2026-09-22", side="BUY",
                               now=tomorrow)
    assert (next_day["reservation_count"], next_day["buy_reservation_count"]) == (1, 1)


def test_a_counter_from_before_the_buy_cap_reads_every_attempt_as_a_buy():
    ref = lock(execution_session={"key": KEY, "count": 3, "last_run_id": "old"},
               worker_schema_version=4)
    assert buy_count_of(ref.get()["execution_session"]) == 3
    with pytest.raises(SessionBuyCapReached):
        reserve(ref, "after-upgrade", "BUY")
    assert reserve(ref, "sell", "SELL")["buy_reservation_count"] == 3


def test_a_lost_lease_is_not_reported_as_the_buy_cap():
    ref = lock(execution_session={"key": KEY, "count": 3, "buy_count": 3, "last_run_id": "x"})
    ref.update({"lease_until": (NOW - timedelta(seconds=1)).isoformat()})
    with pytest.raises(ExecutionLimitError) as refused:
        reserve_attempt("scope", CLAIM, "r0", LIMITS, KEY, side="BUY", now=NOW)
    assert not isinstance(refused.value, SessionBuyCapReached)


@pytest.mark.parametrize("side", ["", None, "HOLD"])
def test_a_reservation_needs_the_order_side(side):
    ref = lock()
    ref.update({"inflight_run_id": "r"})
    with pytest.raises(ExecutionLimitError, match="side"):
        reserve_attempt("scope", CLAIM, "r", LIMITS, KEY, side=side, now=NOW)


@pytest.mark.parametrize("counter", [
    {"key": KEY, "count": 2, "buy_count": 3}, {"key": KEY, "count": 2, "buy_count": -1},
    {"key": KEY, "count": 2, "buy_count": "1"}, {"key": KEY, "count": 2, "buy_count": True}])
def test_a_malformed_buy_counter_is_refused(counter):
    with pytest.raises(ExecutionLimitError):
        validate_counter(counter)


def test_a_schema_the_worker_does_not_know_fails_closed():
    lock(worker_schema_version=WORKER_SCHEMA_VERSION + 1)
    with pytest.raises(ExecutionLimitError, match="unsupported worker schema"):
        reserve_attempt("scope", CLAIM, "r0", LIMITS, KEY, side="SELL", now=NOW)


def test_the_advisory_read_says_when_the_buy_quota_is_spent():
    ref = lock()
    assert not buy_quota_spent("scope", LIMITS, KEY)
    for n in range(3):
        reserve(ref, f"b{n}", "BUY")
    assert buy_quota_spent("scope", LIMITS, KEY)
    assert not buy_quota_spent("scope", LIMITS, "XNYS:2026-09-22")    # another day
    assert not buy_quota_spent("missing", LIMITS, KEY)
    ref.update({"execution_session": {"key": KEY, "count": "x"}})
    assert not buy_quota_spent("scope", LIMITS, KEY)                   # reserve_attempt decides


def test_the_market_day_migration_carries_the_buy_count():
    ref = lock(execution_session={"key": "1890000000.0", "count": 2, "end_epoch": 1890000000.0},
               worker_schema_version=4)
    ref.update({"inflight_run_id": None, "owner": None})
    result = migrate_market_day("scope", now=NOW, apply=True)
    session = result["execution_session"]
    assert (session["key"], session["count"], session["buy_count"]) == (KEY, 2, 2)
    assert ref.get()["worker_schema_version"] == WORKER_SCHEMA_VERSION


def test_the_new_statuses_page_only_for_the_large_clipped_gap():
    assert observability.business_status(
        {"dispatch": {"results": [{"status": "NOT_PLACED", "session_buy_cap_reached": True}]}},
        200) == "SESSION_BUY_CAP_REACHED"
    assert observability.severity_for("SESSION_BUY_CAP_REACHED") == "WARNING"
    assert "SESSION_BUY_CAP_REACHED" not in alerting.ACTION_EVENTS
    assert observability.business_status({"decision": {"order_clipped_large_gap": True}}, 200) == (
        "ORDER_CLIPPED_LARGE_GAP")
    assert observability.severity_for("ORDER_CLIPPED_LARGE_GAP") == "ERROR"
    assert "ORDER_CLIPPED_LARGE_GAP" in alerting.ACTION_EVENTS
    # A real fault in the same tick still outranks both.
    assert observability.business_status({"dispatch": {"results": [
        {"status": "NOT_PLACED", "session_buy_cap_reached": True},
        {"status": "NOT_PLACED", "execution_limit_blocked": True}]}}, 200) == "EXECUTION_LIMIT_BLOCKED"
