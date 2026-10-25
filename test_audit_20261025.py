"""Audit 2026-10-25 (docs/AUDIT_20261025_TH.md).

Revision lego-tick-uat-00055-dej (main@9baf9fc, candidate 57ab596c...), UAT sessions 2026-10-22 and
2026-10-23 (UBER), log 2026-10-22T13:30Z..2026-10-25T06:35Z. The numbers below are the real ones.

F1  Tick fc098796 (2026-10-22 14:15:04Z, slot 3, row v4, READY_SELL 0.5). The decision snapshot timed
    out once and was retried, so dispatch started with 12.095 s of the 35 s budget. After the second
    snapshot 8.101 s were left, yet the worker still ran Preview (1.196 s), the post-Preview open orders
    (4.050 s) and the post-Preview positions read (timed out after 1.573 s), although the fence before
    Place needs 12 s *after* those reads and was certain to refuse. The tick ended with 0.575 s left:
    the flight recorder (1 s floor for evidence, locked by test_audit_20261020) dropped the trace, so v4
    has no recorded D08 equation (trace_audit TRACE_GAP_ROWS). The deadline was also swallowed by the
    post-Preview refetch's ``except Exception`` and persisted as PENDING_DISPATCH with
    last_error=TickDeadlineExceeded / dispatch_check_phase=post_preview_refetch, so the tick was an ERROR
    (an alert would have paged someone for a routine deferral). The next tick placed and filled it.
    Fix: check the fence's budget before the first gate read, and let the refetch pass the deadline on.
F2  Three BUY orders of 2026-10-23 stayed PENDING with the cancel refused and were released at +30 min
    (order_recovery.UAT_STUCK_PENDING_SECONDS); each cost two decision slots. Not changed: locked here as
    documentation of the trade-off.

Nothing here touches a broker or a database: the RTDB is the in-memory fake of conftest.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import execution_service as execution
import flight_recorder as fr
import lego_outbox as outbox
import main
import observability
import order_recovery
import tick_runtime
import webull_io
from conftest import FAKE_DB
from test_dispatch_overshoot import CFG, NOW, dispatch_fixture, snapshot

# lego_operation.remaining_budget_ms / duration_ms of tick fc098796 (Cloud Run log).
START_OF_DISPATCH = 12.095        # before the pre-gate open orders (0.517 s)
LEFT_AT_GATE = 8.101              # after positions 1.351 s + snapshot 1.874 s
PREVIEW_COST = 1.307              # 8.101 -> 6.794 (Preview 1.196 s and the writes before it)
POST_PREVIEW_OPEN_ORDERS_COST = 4.195   # 6.794 -> 2.599 (open orders 4.050 s and the PREVIEWED write)
POST_PREVIEW_POSITIONS_COST = 1.632     # 2.599 -> 0.967 (positions timed out after 1.573 s)
TAIL_COST = 0.392                 # 0.967 -> 0.575: exception path, tick log line, alert check
LEFT_WHEN_LOGGED = 0.575          # 35 s - 34.425 s

# Budget at the start of each of the 25 Previews in the log, and whether that tick placed the order.
PREVIEW_STARTS = [
    ("2026-10-22T13:45:06Z", 10.52, False), ("2026-10-22T13:46:05Z", 30.91, True),
    ("2026-10-22T14:00:07Z", 21.45, True), ("2026-10-22T14:15:06Z", 7.99, False),
    ("2026-10-22T14:16:06Z", 30.44, True), ("2026-10-22T14:30:09Z", 22.06, False),
    ("2026-10-22T14:31:06Z", 32.60, True), ("2026-10-22T14:45:07Z", 25.13, True),
    ("2026-10-22T15:00:07Z", 24.72, True), ("2026-10-22T15:45:06Z", 22.71, True),
    ("2026-10-22T16:15:07Z", 26.48, True), ("2026-10-22T16:30:06Z", 26.58, True),
    ("2026-10-22T16:45:06Z", 25.38, False), ("2026-10-22T17:00:07Z", 27.31, True),
    ("2026-10-22T17:15:06Z", 28.24, True), ("2026-10-22T19:15:06Z", 26.56, True),
    ("2026-10-23T14:00:06Z", 24.80, True), ("2026-10-23T14:30:08Z", 24.20, True),
    ("2026-10-23T14:45:06Z", 16.93, False), ("2026-10-23T14:46:14Z", 23.15, True),
    ("2026-10-23T15:45:07Z", 20.63, True), ("2026-10-23T17:30:06Z", 28.41, True),
    ("2026-10-23T17:45:07Z", 27.94, True), ("2026-10-23T18:30:06Z", 26.17, True),
    ("2026-10-23T19:15:06Z", 27.47, True),
]


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    FAKE_DB.store.clear()
    fr.reset_state()
    webull_io.reset_clients()
    execution.configure(main)
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("WEBULL_ENV", "UAT")
    for name in ("LEGO_TRACE_LEVEL", "LEGO_TRACE_BODIES", "LEGO_TRACE_RETENTION_DAYS"):
        monkeypatch.delenv(name, raising=False)

    class FixedNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(execution, "datetime", FixedNow)
    monkeypatch.setattr(outbox, "datetime", FixedNow)
    yield
    FAKE_DB.store.clear()
    fr.reset_state()
    webull_io.reset_clients()


# ------------------------------------------------------------------------------ helpers

class Budget:
    """tick_runtime.remaining() for a test: seconds left, spent by the doubled broker reads."""

    def __init__(self, left):
        self.left = left

    def spend(self, seconds):
        self.left = max(0.0, self.left - seconds)


def use_budget(monkeypatch, left):
    budget = Budget(left)
    monkeypatch.setattr(tick_runtime, "remaining", lambda: budget.left)
    return budget


def counting(monkeypatch, name, budget=None, costs=()):
    """Wrap execution.<name>: count calls and spend the measured seconds of each one."""
    original = getattr(execution, name)
    calls = []
    costs = list(costs)

    def wrapped(*args, **kwargs):
        calls.append(args)
        if budget is not None and costs:
            budget.spend(costs.pop(0))
        return original(*args, **kwargs)

    monkeypatch.setattr(execution, name, wrapped)
    return calls


def attempt(client, intent, claim, runtime):
    return execution._dispatch_or_reconcile_one(client, object(), CFG, intent, claim, runtime)


def stored(intent):
    return outbox.read_intent(intent["chain_key"], intent["run_id"])


def deadline(*_args, **_kwargs):
    raise tick_runtime.TickDeadlineExceeded("tick time budget exhausted; resume recovery next tick")


def w23_tags():
    return [e.get("tag") for e in fr._CURRENT.get().events if e.get("n") == "W23"]


def all_traces():
    found = []
    for chain in (FAKE_DB.reference(fr.TRACE_PATH).get() or {}).values():
        for day in chain.values():
            found.extend(day.values())
    return found


# ------------------------------------------------------------------------------ F1: the guard

def test_the_guard_asks_for_exactly_what_the_fence_asks_for():
    assert execution.DISPATCH_FENCE_MIN_BUDGET_SECONDS == 12.0
    # What the guard buys on the evidence: the tick that lost its trace ends with seconds to spare.
    assert LEFT_AT_GATE - TAIL_COST >= fr.FLUSH_MIN_BUDGET_MUTATION_SECONDS
    assert LEFT_WHEN_LOGGED < fr.FLUSH_MIN_BUDGET_MUTATION_SECONDS


@pytest.mark.parametrize("left", [LEFT_AT_GATE, 7.99, 10.52, 11.99])
def test_a_dispatch_that_cannot_reach_the_fence_defers_before_any_gate_read(monkeypatch, left):
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch)
    use_budget(monkeypatch, left)
    previews = counting(monkeypatch, "preview_market_order_result")
    balances = counting(monkeypatch, "fetch_buying_power")
    open_orders = counting(monkeypatch, "fetch_open_orders")
    snapshots = counting(monkeypatch, "fetch_snapshot")

    with tick_runtime.tick_scope("f1-guard"):
        with pytest.raises(tick_runtime.TickDeadlineExceeded):
            attempt(client, intent, claim, runtime)
        assert w23_tags() == ["deferred"]

    assert previews == [] and balances == [] and client.order_v3.preview_order.calls == []
    assert len(open_orders) == 1 and len(snapshots) == 1          # the pre-gate reads only
    after = stored(intent)
    assert after["status"] == "PENDING_DISPATCH" and not after.get("place_attempted")
    assert not after.get("last_error") and not after.get("dispatch_check_phase")
    assert client.order_v3.place_order.calls == []


@pytest.mark.parametrize("left", [12.0, 20.63, 32.60])
def test_with_the_fence_budget_the_order_is_placed_exactly_as_before(monkeypatch, left):
    # 20.63 s is the least budget any of the 20 placed orders of 22-23 Oct started its Preview with.
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch)
    use_budget(monkeypatch, left)
    result = attempt(client, intent, claim, runtime)
    assert result["status"] == "SUBMITTED", result
    assert len(client.order_v3.preview_order.calls) == 1
    assert len(client.order_v3.place_order.calls) == 1


def test_the_fence_and_the_guard_read_the_same_constant(monkeypatch):
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch)
    monkeypatch.setattr(execution, "DISPATCH_FENCE_MIN_BUDGET_SECONDS", 5.0)
    use_budget(monkeypatch, 10.52)          # under the literal 12 s, over the patched 5 s and 10 s later on
    result = attempt(client, intent, claim, runtime)
    assert result["status"] == "SUBMITTED", result
    assert len(client.order_v3.place_order.calls) == 1


def test_replay_of_the_tick_that_lost_its_trace(monkeypatch):
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch)
    budget = use_budget(monkeypatch, START_OF_DISPATCH)
    first, second = snapshot(), snapshot()
    reads = iter([first, second])
    open_order_costs = [START_OF_DISPATCH - 11.578, POST_PREVIEW_OPEN_ORDERS_COST]
    snapshot_costs = [11.578 - LEFT_AT_GATE]

    def open_orders(*_):
        budget.spend(open_order_costs.pop(0))
        return []

    def fetch(*_):
        if snapshot_costs:
            budget.spend(snapshot_costs.pop(0))
            return next(reads)
        budget.spend(POST_PREVIEW_POSITIONS_COST)       # positions: connect timeout, then the deadline
        deadline()

    monkeypatch.setattr(execution, "fetch_open_orders", open_orders)
    monkeypatch.setattr(execution, "fetch_snapshot", fetch)
    monkeypatch.setattr(execution, "fetch_buying_power", lambda *_: execution.Decimal(10000))  # UBER was a SELL
    counting(monkeypatch, "preview_market_order_result", budget, [PREVIEW_COST])

    with tick_runtime.tick_scope("fc098796732842e2acfd43d1fe4a2e0d"):
        fr.bind(chain_key=intent["chain_key"], symbol=CFG.symbol, env="UAT", mode="trade")
        fr.node("D08", "READY_SELL")                # the committed row's equation, earlier in this tick
        fr.node("D10", "committed")
        with pytest.raises(tick_runtime.TickDeadlineExceeded):
            attempt(client, intent, claim, runtime)
        assert budget.left == pytest.approx(LEFT_AT_GATE)
        budget.spend(TAIL_COST)
        written = fr.finish({"pipeline_status": "TICK_DEFERRED", "business_status": "TICK_DEFERRED",
                             "dispatch": {"pipeline_status": "TICK_DEFERRED",
                                          "deferred_reason": "tick_deadline"}}, 200)

    assert written["written"] is True and "deferred" in written["reasons"]
    (trace,) = all_traces()
    assert "D08" in trace["path"] and "W23" in trace["path"]
    assert client.order_v3.preview_order.calls == [] and client.order_v3.place_order.calls == []
    assert stored(intent)["status"] == "PENDING_DISPATCH" and not stored(intent).get("last_error")


# ------------------------------------------------------------------------------ F1: the refetch

def test_a_deadline_inside_the_post_preview_refetch_is_passed_on(monkeypatch):
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch)
    use_budget(monkeypatch, 20.0)
    reads = iter([snapshot()])
    monkeypatch.setattr(execution, "fetch_snapshot", lambda *_: next(reads, None) or deadline())

    with pytest.raises(tick_runtime.TickDeadlineExceeded):
        attempt(client, intent, claim, runtime)

    after = stored(intent)
    assert after["status"] == "PENDING_DISPATCH" and not after.get("place_attempted")
    assert not after.get("last_error") and after.get("dispatch_check_phase") is None
    assert len(client.order_v3.preview_order.calls) == 1 and client.order_v3.place_order.calls == []


def test_any_other_refetch_failure_still_waits_as_post_preview_refetch(monkeypatch):
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch)
    use_budget(monkeypatch, 20.0)
    reads = iter([snapshot()])

    def fetch(*_):
        value = next(reads, None)
        if value is None:
            raise RuntimeError("positions read failed")
        return value

    monkeypatch.setattr(execution, "fetch_snapshot", fetch)
    result = attempt(client, intent, claim, runtime)
    assert result["status"] == "PENDING_DISPATCH" and result["error_type"] == "RuntimeError"
    assert stored(intent)["dispatch_check_phase"] == "post_preview_refetch"
    assert client.order_v3.place_order.calls == []


def test_the_deferral_is_a_warning_tick_and_the_old_swallow_was_an_error():
    deferred = {"pipeline_status": "TICK_DEFERRED", "decision": {},
                "dispatch": {"pipeline_status": "TICK_DEFERRED", "deferred_reason": "tick_deadline"}}
    swallowed = {"pipeline_status": "TICK_OK", "decision": {},
                 "dispatch": {"results": [{"run_id": "b1d691bb", "status": "PENDING_DISPATCH",
                                           "error": "TickDeadlineExceeded: tick time budget exhausted",
                                           "error_type": "TickDeadlineExceeded"}]}}
    assert observability.business_status(deferred, 200) == "TICK_DEFERRED"
    assert observability.severity_for("TICK_DEFERRED") == "WARNING"
    assert observability.business_status(swallowed, 200) == "ERROR"      # what 14:15:39Z logged
    assert observability.severity_for("ERROR") == "ERROR"


# ------------------------------------------------------------------------------ F1: the evidence

def test_no_placed_order_of_the_two_sessions_started_its_preview_under_the_fence_budget():
    floor = execution.DISPATCH_FENCE_MIN_BUDGET_SECONDS
    placed = [left for _, left, did in PREVIEW_STARTS if did]
    assert len(PREVIEW_STARTS) == 25 and len(placed) == 20
    assert min(placed) == 20.63 and min(placed) >= floor
    assert [at for at, left, _ in PREVIEW_STARTS if left < floor] == [
        "2026-10-22T13:45:06Z", "2026-10-22T14:15:06Z"]           # both deferred on the old code too


def test_the_budget_arithmetic_of_tick_fc098796():
    left = START_OF_DISPATCH - (START_OF_DISPATCH - 11.578) - (11.578 - LEFT_AT_GATE)
    assert left == pytest.approx(LEFT_AT_GATE)
    left -= PREVIEW_COST + POST_PREVIEW_OPEN_ORDERS_COST + POST_PREVIEW_POSITIONS_COST
    assert left == pytest.approx(0.967, abs=1e-6)
    assert left - TAIL_COST == pytest.approx(LEFT_WHEN_LOGGED, abs=1e-6)


# ------------------------------------------------------------------------------ F2 (unchanged)

STUCK = [   # run, Place, release (expiry_released_at)
    ("ca84090ddf0b0c327e29a11e8088a3fe", "2026-10-23T14:46:22Z", "2026-10-23T15:17:10.207081Z"),
    ("df945469b5f4c238aa730ece13659ad2", "2026-10-23T18:30:19Z", "2026-10-23T19:01:07.857575Z"),
    ("7945264992174aa7ef145d593751a428", "2026-10-23T19:15:17Z", "2026-10-23T19:46:08.756325Z"),
]


def _at(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _slot_starts_between(start, end, slot=timedelta(minutes=15)):
    first = start.replace(minute=start.minute - start.minute % 15, second=0, microsecond=0) + slot
    count, cursor = 0, first
    while cursor <= end:
        count, cursor = count + 1, cursor + slot
    return count


@pytest.mark.parametrize("run_id,placed,released", STUCK)
def test_each_stuck_pending_order_was_released_at_thirty_minutes_and_held_two_slots(run_id, placed, released):
    placed_at, released_at = _at(placed), _at(released)
    intent = {"run_id": run_id, "placed_at": placed}
    assert order_recovery.UAT_STUCK_PENDING_SECONDS == 1800
    assert not order_recovery._stuck_pending_age_reached(intent, placed_at + timedelta(seconds=1799))
    assert order_recovery._stuck_pending_age_reached(intent, released_at)
    assert timedelta(seconds=1800) <= released_at - placed_at < timedelta(seconds=1800 + 90)
    assert _slot_starts_between(placed_at, released_at) == 2
    # The owner's option recorded in the audit: 1,200 s would hold one slot, with 5 minutes of margin
    # over the slowest genuine PENDING fill on record (< 15 minutes, 2026-10-05).
    assert _slot_starts_between(placed_at, placed_at + timedelta(seconds=1200 + 60)) == 1
