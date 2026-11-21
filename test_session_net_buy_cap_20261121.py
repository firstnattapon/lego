"""LEGO_MAX_SESSION_BUY_ORDERS counts net BUYs (owner, 2026-11-21).

PROD RGTI on 2026-11-20 placed BUY, SELL, BUY, BUY (all FILLED) and the next BUY was refused
with "session BUY limit reached (3 BUY orders per session)", though the session had bought
only +2 net. Now net = BUY reservations (counted before Place, never refunded) − SELLs the
broker reported filled (credited by broker_circuit.record_outcome in the same lock-document
transaction as the outcome receipt). A BUY is reserved while net < the cap; net may go
below zero. A SELL is never blocked by the BUY cap, frees a BUY only once it fills, and the
total cap LEGO_MAX_SESSION_ORDERS binds as before.
"""
from __future__ import annotations

import copy
from datetime import timedelta

import pytest

import broker_circuit
import ops
from conftest import FAKE_DB
from execution_limits import (WORKER_SCHEMA_VERSION, ExecutionLimitError, ExecutionLimits,
                              SessionBuyCapReached, buy_quota_spent, credit_filled_sell,
                              migrate_market_day, net_buy_count_of, reserve_attempt,
                              sell_count_of, validate_counter)
from lego_outbox import DISPATCH_LOCK_PATH, account_symbol_fence_key
from test_dispatch_overshoot import NOW, isolate  # noqa: F401  (autouse)

WINDOW = "2030-01-01T00:00:00Z"
LIMITS = ExecutionLimits.parse(("20", "150", "10", WINDOW, "3"))
KEY = "XNYS:2026-09-21"
CLAIM = {"owner": "worker", "claim_token": "token"}
IDENTITY = "net-cap-runtime"
SCOPE = account_symbol_fence_key(IDENTITY, "RGTI")
# The four PROD runs of RGTI on 2026-11-20, in order.
RGTI_20261120 = (("2fd69c04bd3b72da95bc11e1cfd589c8", "BUY"),
                 ("bc2939892d957461548e3fc945311b27", "SELL"),
                 ("ba4d8146b1643a631781640fa33a0637", "BUY"),
                 ("bad1bbfa5d556f967026eb86f3828fec", "BUY"))


def ref():
    return FAKE_DB.reference(f"{DISPATCH_LOCK_PATH}/{SCOPE}")


@pytest.fixture(autouse=True)
def lock():
    ref().set({**CLAIM, "inflight_run_id": "r0",
               "lease_until": (NOW + timedelta(seconds=120)).isoformat()})


def reserve(run_id, side, limits=LIMITS, *, key=KEY, now=NOW):
    ref().update({"inflight_run_id": run_id,
                  "lease_until": (now + timedelta(seconds=120)).isoformat()})
    return reserve_attempt(SCOPE, CLAIM, run_id, limits, key, side=side, now=now)


def outcome(run_id, side, status="FILLED", filled="1"):
    intent = {"run_id": run_id, "side": side, "symbol": "RGTI", "chain_key": "RGTI_chain",
              "runtime_identity_fingerprint": IDENTITY}
    return broker_circuit.record_outcome(intent, {"status": status, "filled_quantity": filled})


def order(run_id, side, **kwargs):
    reservation = reserve(run_id, side)
    outcome(run_id, side, **kwargs)
    return reservation


def session():
    return ref().get()["execution_session"]


def test_the_rgti_session_of_2026_11_20_may_buy_again_at_net_two():
    for run_id, side in RGTI_20261120:
        order(run_id, side)
    counter = session()
    assert (counter["count"], counter["buy_count"], counter["sell_count"]) == (4, 3, 1)
    assert net_buy_count_of(counter) == 2
    assert not buy_quota_spent(SCOPE, LIMITS, KEY)                    # the old rule said spent
    fourth = reserve("buy-4", "BUY")
    assert (fourth["reservation_count"], fourth["buy_reservation_count"],
            fourth["sell_fill_count"], fourth["net_buy_count"]) == (5, 4, 1, 3)
    with pytest.raises(SessionBuyCapReached, match=r"net 3 = 4 BUY - 1 filled SELL; max 3 net BUY"):
        reserve("buy-5", "BUY")
    assert buy_quota_spent(SCOPE, LIMITS, KEY)["buy_count"] == 4
    assert reserve("sell-2", "SELL")["reservation_count"] == 6         # SELL is never BUY-capped


@pytest.mark.parametrize("status, filled", [
    ("FAILED", "0"), ("REJECTED", "0"), ("CANCELLED", "0"), ("EXPIRED", "0"),
    ("SUBMITTED", "0"), ("PARTIAL_FILLED", "1")])          # the last two are not terminal yet
def test_a_sell_that_did_not_fill_frees_no_buy(status, filled):
    order("buy-1", "BUY"), order("buy-2", "BUY")
    order("sell-1", "SELL", status=status, filled=filled)
    order("buy-3", "BUY")
    assert sell_count_of(session()) == 0
    with pytest.raises(SessionBuyCapReached):
        reserve("buy-4", "BUY")


def test_a_sell_reservation_alone_frees_no_buy_until_its_fill_arrives():
    for n in range(3):
        order(f"buy-{n}", "BUY")
    reserve("sell-1", "SELL")
    assert buy_quota_spent(SCOPE, LIMITS, KEY)                         # SUBMITTED, not filled
    outcome("sell-1", "SELL")
    assert not buy_quota_spent(SCOPE, LIMITS, KEY)
    assert reserve("buy-4", "BUY")["net_buy_count"] == 3


def test_a_partly_filled_then_cancelled_sell_counts_as_one():
    order("buy-1", "BUY")
    order("sell-1", "SELL", status="CANCELLED", filled="2.5")
    assert sell_count_of(session()) == 1


def test_net_may_go_below_zero_after_a_session_that_sold_first():
    order("sell-1", "SELL"), order("sell-2", "SELL")
    assert net_buy_count_of(session()) == -2
    for n in range(5):
        assert reserve(f"buy-{n}", "BUY")["net_buy_count"] == n - 1
    with pytest.raises(SessionBuyCapReached, match="net 3 = 5 BUY - 2 filled SELL"):
        reserve("buy-6", "BUY")


def test_the_total_cap_still_binds_whatever_the_net():
    total = ExecutionLimits.parse(("20", "150", "4", WINDOW, "3"))
    reserve("sell-1", "SELL", total), outcome("sell-1", "SELL")
    for n in range(3):
        reserve(f"buy-{n}", "BUY", total)
    assert net_buy_count_of(session()) == 2
    with pytest.raises(ExecutionLimitError) as refused:
        reserve("buy-4", "BUY", total)
    assert not isinstance(refused.value, SessionBuyCapReached)        # EXECUTION_LIMIT_BLOCKED


def test_one_fill_is_credited_once():
    order("buy-1", "BUY"), order("sell-1", "SELL")
    outcome("sell-1", "SELL")                                          # the same outcome again
    assert sell_count_of(session()) == 1
    doc = ref().get()
    assert credit_filled_sell(doc, "sell-1", "now") is False           # its own receipt too
    assert doc["execution_session"]["last_sell_credit_run_id"] == "sell-1"


def test_only_a_sell_this_run_reserved_is_credited():
    order("buy-1", "BUY")
    ref().update({"inflight_run_id": "never-reserved"})                # e.g. no runtime, no reservation
    outcome("never-reserved", "SELL")
    order("buy-2", "BUY", status="FILLED")                             # a BUY fill credits nothing
    assert sell_count_of(session()) == 0


def test_a_counter_without_an_uncredited_sell_reservation_takes_no_credit():
    # Read as every attempt a BUY (written before buy_count): no SELL reservation to credit.
    ref().update({"execution_session": {"key": KEY, "count": 2, "last_run_id": "sell-x"},
                  "inflight_run_id": "sell-x"})
    outcome("sell-x", "SELL")
    assert "sell_count" not in session()


def test_the_credit_replaces_the_counter_and_never_edits_the_transaction_input():
    order("buy-1", "BUY"), reserve("sell-1", "SELL")
    current = ref().get()
    before = copy.deepcopy(current)
    doc = dict(current)
    assert credit_filled_sell(doc, "sell-1", "now") is True
    assert current == before and doc["execution_session"]["sell_count"] == 1


def test_a_new_market_day_starts_with_no_filled_sell():
    order("buy-1", "BUY"), order("sell-1", "SELL")
    tomorrow = NOW + timedelta(days=1)
    result = reserve("d2", "BUY", key="XNYS:2026-09-22", now=tomorrow)
    assert (result["reservation_count"], result["sell_fill_count"], result["net_buy_count"]) == (1, 0, 1)


def test_the_counter_keeps_sell_count_and_schema_5():
    order("buy-1", "BUY"), order("sell-1", "SELL"), order("buy-2", "BUY")
    doc = ref().get()
    assert doc["execution_session"]["sell_count"] == 1
    assert doc["worker_schema_version"] == WORKER_SCHEMA_VERSION == 5


def test_the_market_day_migration_drops_filled_sells_conservatively():
    ref().update({"execution_session": {"key": "1890000000.0", "count": 3, "buy_count": 1,
                                        "sell_count": 2, "end_epoch": 1890000000.0},
                  "inflight_run_id": None, "owner": None})
    migrated = migrate_market_day(SCOPE, now=NOW, apply=True)["execution_session"]
    assert sell_count_of(migrated) == 0 and net_buy_count_of(migrated) == 1


@pytest.mark.parametrize("counter", [
    {"key": KEY, "count": 2, "buy_count": 1, "sell_count": -1},
    {"key": KEY, "count": 2, "buy_count": 1, "sell_count": "1"},
    {"key": KEY, "count": 2, "buy_count": 1, "sell_count": True},
    {"key": KEY, "count": 2, "buy_count": 1, "sell_count": 2},          # 1 BUY + 2 SELL > 2
    {"key": KEY, "count": 2, "sell_count": 1}])                         # legacy: all BUY
def test_a_malformed_sell_counter_is_refused(counter):
    with pytest.raises(ExecutionLimitError, match="SELL counter"):
        validate_counter(counter)


def test_the_prod_counters_of_2026_11_20_read_under_the_new_rule():
    rgti = {"key": KEY, "count": 4, "buy_count": 3, "end_epoch": 1797886800.0,
            "last_run_id": "bad1bbfa5d556f967026eb86f3828fec"}
    qbts = {**rgti, "count": 3, "buy_count": 3}
    for counter in (rgti, qbts, {**rgti, "sell_count": 1}):
        validate_counter(counter)
    ref().update({"execution_session": {**rgti, "sell_count": 1}})
    assert not buy_quota_spent(SCOPE, LIMITS, KEY)                     # net 2 < 3
    ref().update({"execution_session": qbts})
    assert buy_quota_spent(SCOPE, LIMITS, KEY)                         # 3 BUY, no SELL: net 3


def test_ops_status_shows_the_net():
    assert ops._execution_session_status({"key": KEY, "count": 4, "buy_count": 3,
                                          "sell_count": 1})["net_buy_count"] == 2
    assert ops._execution_session_status(None) == {}
    assert "malformed" in ops._execution_session_status({"key": KEY, "count": 1, "sell_count": 2})
