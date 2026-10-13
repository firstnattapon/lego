"""2026-10-12: a UAT order stayed PENDING -- and listed as open -- until the halt (4th time in 6 sessions).

Run 2a614784... (BUY 1.37 UBER, MARKET/DAY/CORE, 14:01:14Z) never filled. Its one cancel (14:07:07Z)
was refused with 417. Order detail said PENDING / filled 0 for 7+ hours and open-orders/list still
returned it at 21:00Z and 22:07Z -- one and two hours after the close -- so the DAY-expiry proof of
2026-10-06 never passed (`order_still_listed_open`), the 8 hour hold ended in
MANUAL_RECONCILIATION_REQUIRED plus an operator halt, and 23 of 26 slots traded nothing.

These pin the UAT-only rule that ends it, and everything that must keep it shut: an order that is
still PENDING with nothing filled 30 minutes after Place, whose one cancel was refused, that the
broker lists as exactly that and whose holdings never moved, is released as a zero-fill EXPIRED
during the session. UAT only; PROD and every person-confirmed PROD release keep the original proof.
(The dispatch guards that must then ignore the still-listed order are in
test_incident_20261012_dispatch.py.) The values are the incident's own.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import lego_orders
import main
import order_recovery as recovery
from conftest import FAKE_DB
from recovery_policy import RecoveryPolicy
from test_execution_confirmed_cashflow import (  # noqa: F401  (env is an autouse fixture)
    SLOT_1, _cashflow, _fenced_runs, _intent, _run, _stub_broker, _work, env)
from test_incident_20261006 import (HALT_TIME, NEXT_OPEN, PAST_MARGIN, PLACED_E2E, REFUSED_E2E,
                                    _clock, _halted, _zombie)
from test_trace_audit import CHAIN, make_world, run_id as world_run_id
from test_webull_io import (  # noqa: F401  (account / fresh_client_cache are autouse fixtures)
    _install_fake_sdk, _write_token, account, fresh_client_cache)
from tools import resume_order_reconciliation as resume
from tools import trace_audit as ta

UTC = timezone.utc
RUN = "2a614784f17f8c9bf408fb7f14bb99c7"
BROKER_ID = "0389GDBNC080O0KF41U8000000"
PLACED = datetime(2026, 10, 12, 14, 1, 14, tzinfo=UTC)
REFUSED = datetime(2026, 10, 12, 14, 7, 7, tzinfo=UTC)
CLOSE = datetime(2026, 10, 12, 20, 0, tzinfo=UTC)
STUCK_AT = PLACED + timedelta(seconds=recovery.UAT_STUCK_PENDING_SECONDS)       # 14:31:14Z
HOLDINGS = 140.4528                                                              # never moved
TOLERANCE = 0.000001

# The row open-orders/list returned at 21:00Z and 22:07Z (flight recorder, W11 / wb events).
LISTED = {"client_order_id": RUN, "filled_price": "0.000000", "filled_quantity": "0",
          "instrument_type": "EQUITY", "order_id": BROKER_ID, "order_type": "MARKET",
          "place_time": "2026-10-12T14:01:16.736Z", "side": "BUY", "status": "PENDING",
          "support_trading_session": "CORE", "symbol": "UBER", "time_in_force": "DAY",
          "total_quantity": "1.37"}


def intent(**changes):
    policy = RecoveryPolicy("cancel_expire")
    doc = {
        "run_id": RUN, "chain_key": "chain", "symbol": "UBER", "side": "BUY", "quantity": 1.37,
        "broker_order_id": BROKER_ID, "place_attempted": True,
        "placed_at": PLACED.isoformat(), "decision_holdings": HOLDINGS,
        "cancel_attempt_count": 1, "cancel_refused_at": REFUSED.isoformat(),
        "cancel_last_error_code": recovery.CANCEL_REFUSED, "status": "CANCEL_UNKNOWN",
        "cancel_policy": policy.snapshot(), "cancel_policy_hash": policy.fingerprint,
        "order_payload": [{
            "client_order_id": RUN, "combo_type": "NORMAL", "entrust_type": "QTY",
            "instrument_type": "EQUITY", "market": "US", "order_type": "MARKET",
            "quantity": "1.37", "side": "BUY", "support_trading_session": "CORE",
            "symbol": "UBER", "time_in_force": "DAY"}],
    }
    doc.update(changes)
    return doc


def detail(**changes):
    doc = {"client_order_id": RUN, "order_id": BROKER_ID, "symbol": "UBER", "side": "BUY",
           "total_quantity": "1.370000", "filled_quantity": "0.000000", "status": "PENDING"}
    doc.update(changes)
    return doc


def summary_of(broker):
    return {"status": broker.get("status"), "filled_quantity": broker.get("filled_quantity")}


def blockers(*, doc=None, broker=None, open_orders=(LISTED,), holdings=HOLDINGS, now=STUCK_AT,
             uat=True):
    doc = doc or intent()
    broker = broker or detail()
    return recovery.expiry_proof_blockers(
        doc, broker, summary_of(broker),
        open_orders=None if open_orders is None else list(open_orders),
        holdings=holdings, now=now, tolerance=TOLERANCE, uat_stuck_pending=uat)


def release(*, now=STUCK_AT, uat=True, open_orders=(LISTED,), doc=None, broker=None):
    broker = broker or detail()
    return recovery.expiry_release(
        doc or intent(), broker, summary_of(broker), open_orders=list(open_orders),
        holdings=HOLDINGS, tolerance=TOLERANCE, now=now, uat_stuck_pending=uat)


# ---------------------------------------------------- the incident, step by step
def test_the_incident_was_blocked_only_by_the_listing_and_stays_so_without_the_uat_rule():
    # 21:00:05Z and 22:07:07Z, flight recorder W11: the only blocker was the listing.
    for moment in (datetime(2026, 10, 12, 21, 0, 5, tzinfo=UTC),
                   datetime(2026, 10, 12, 22, 7, 7, tzinfo=UTC)):
        assert blockers(now=moment, uat=False) == ["order_still_listed_open"]
        assert blockers(now=moment, uat=False, open_orders=[]) == []           # the 10-06 proof
        assert blockers(now=moment, uat=True) == []                            # the UAT rule


def test_the_order_is_released_thirty_minutes_after_place_not_before():
    assert STUCK_AT == datetime(2026, 10, 12, 14, 31, 14, tzinfo=UTC)
    assert blockers(now=STUCK_AT) == []
    assert blockers(now=STUCK_AT - timedelta(seconds=1)) == ["session_not_over_plus_margin"]
    assert blockers(now=REFUSED + timedelta(seconds=1)) == ["session_not_over_plus_margin"]
    assert blockers(now=STUCK_AT + timedelta(hours=3)) == []                    # still in session
    # Without the UAT rule the session must be over plus the margin, as before.
    assert sorted(blockers(now=STUCK_AT, uat=False)) == [
        "order_still_listed_open", "session_not_over_plus_margin"]


def test_a_cheap_gate_decides_whether_to_read_the_broker_at_all():
    assert not recovery.expiry_candidate(intent(), now=STUCK_AT, uat_stuck_pending=False)
    assert not recovery.expiry_candidate(intent(), now=STUCK_AT - timedelta(seconds=1),
                                         uat_stuck_pending=True)
    assert recovery.expiry_candidate(intent(), now=STUCK_AT, uat_stuck_pending=True)
    # After the close plus the margin the original gate opens on its own.
    assert recovery.expiry_candidate(intent(), now=CLOSE + timedelta(seconds=recovery.EXPIRY_MARGIN_SECONDS))
    # The flag is not a policy: every other requirement of the gate still applies.
    cancel = RecoveryPolicy("cancel")
    for doc in (intent(cancel_policy=cancel.snapshot(), cancel_policy_hash=cancel.fingerprint),
                intent(cancel_policy_hash="0" * 64), intent(needs_manual_check=True),
                intent(cancel_refused_at=None), intent(cancel_attempt_count=0),
                intent(place_attempted=False)):
        assert not recovery.expiry_candidate(doc, now=STUCK_AT, uat_stuck_pending=True), doc
    # A person's confirmation opens the gate for any policy -- for the proof, not around it.
    approved = {"expiry_proof_authorized": True, "operator": "reviewer"}
    plain = intent(cancel_policy=cancel.snapshot(), cancel_policy_hash=cancel.fingerprint)
    assert recovery.expiry_candidate({**plain, "reconcile_resume": approved}, now=STUCK_AT,
                                     uat_stuck_pending=True)


@pytest.mark.parametrize("what,kwargs,expected", [
    ("listed SUBMITTED", dict(open_orders=[{**LISTED, "status": "SUBMITTED"}]), "order_still_listed_open"),
    ("listed WORKING", dict(open_orders=[{**LISTED, "status": "WORKING"}]), "order_still_listed_open"),
    ("listed partly filled", dict(open_orders=[{**LISTED, "status": "PARTIAL_FILLED",
                                                "filled_quantity": "0.5"}]), "order_still_listed_open"),
    ("listed PENDING but filled", dict(open_orders=[{**LISTED, "filled_quantity": "0.01"}]),
     "order_still_listed_open"),
    ("listed filled missing", dict(open_orders=[{k: v for k, v in LISTED.items()
                                                if k != "filled_quantity"}]), "order_still_listed_open"),
    ("listed filled malformed", dict(open_orders=[{**LISTED, "filled_quantity": "n/a"}]),
     "order_still_listed_open"),
    ("listed filled NaN", dict(open_orders=[{**LISTED, "filled_quantity": "NaN"}]),
     "order_still_listed_open"),
    ("listed size differs", dict(open_orders=[{**LISTED, "total_quantity": "1.38"}]),
     "order_still_listed_open"),
    ("listed size missing", dict(open_orders=[{k: v for k, v in LISTED.items()
                                              if k != "total_quantity"}]), "order_still_listed_open"),
    ("listed other symbol", dict(open_orders=[{**LISTED, "symbol": "AAPL"}]), "order_still_listed_open"),
    ("listed other side", dict(open_orders=[{**LISTED, "side": "SELL"}]), "order_still_listed_open"),
    ("listed twice", dict(open_orders=[LISTED, dict(LISTED)]), "order_still_listed_open"),
    ("order detail SUBMITTED", dict(broker=detail(status="SUBMITTED")), "stuck_release_requires_pending"),
    ("order detail part filled", dict(broker=detail(status="PARTIAL_FILLED", filled_quantity="0.5")),
     "filled_quantity_not_zero"),
    ("order detail FILLED", dict(broker=detail(status="FILLED", filled_quantity="1.370000")),
     "broker_status_terminal"),
    ("order detail CANCELLED", dict(broker=detail(status="CANCELLED")), "broker_status_terminal"),
    ("order detail UNKNOWN", dict(broker=detail(status="UNKNOWN")), "broker_status_unrecognized"),
    ("order detail other size", dict(broker=detail(total_quantity="1.38")), "broker_evidence_invalid"),
    ("holdings moved", dict(holdings=HOLDINGS + 1.37), "holdings_changed"),
    ("holdings unreadable", dict(holdings=float("nan")), "holdings_unverifiable"),
    ("open orders unread", dict(open_orders=None), "open_orders_unread"),
    ("cancel never refused", dict(doc=intent(cancel_refused_at=None)), "cancel_not_refused"),
    ("cancel never sent", dict(doc=intent(cancel_attempt_count=0, cancel_refused_at=None)),
     "cancel_not_attempted"),
    ("not placed", dict(doc=intent(place_attempted=False)), "place_not_attempted"),
    ("no payload", dict(doc=intent(order_payload=None)), "payload_not_single_leg"),
    ("placed before the open", dict(doc=intent(placed_at=datetime(2026, 10, 12, 13, 0, tzinfo=UTC).isoformat())),
     "placed_outside_regular_session"),
])
def test_every_condition_of_the_uat_rule_is_required(what, kwargs, expected):
    found = blockers(**kwargs)
    assert expected in found, (what, found)


@pytest.mark.parametrize("key,value,expected", [
    ("order_type", "LIMIT", "payload_order_type_not_market"),
    ("time_in_force", "GTC", "payload_time_in_force_not_day"),
    ("support_trading_session", "ALL", "payload_support_trading_session_not_core")])
def test_only_a_market_day_core_order_can_be_released_early(key, value, expected):
    doc = intent()
    doc["order_payload"] = [{**doc["order_payload"][0], key: value}]
    assert blockers(doc=doc) == [expected]


def test_the_listed_row_must_be_this_order_not_just_any_pending_one():
    other = {**LISTED, "client_order_id": "f" * 32}
    # Someone else's PENDING row is not listing for this run: it is simply not this order's.
    assert blockers(open_orders=[other]) == []
    assert blockers(open_orders=[other, LISTED]) == []
    assert blockers(open_orders=[other, {**LISTED, "status": "WORKING"}]) == ["order_still_listed_open"]


# ------------------------------------------------------------ what a release is
def test_the_release_is_an_expired_zero_fill_that_still_says_pending_and_names_its_basis():
    released, found = release()
    assert found == [] and released["status"] == "EXPIRED"
    assert released["broker_status"] == "PENDING"                 # never reads as a broker expiry
    assert released["filled_quantity"] == "0.000000"
    assert released["expiry_released"] is True
    assert released["expiry_basis"] == recovery.BASIS_UAT_STUCK_PENDING
    assert released["terminal_reason"] == recovery.UAT_STUCK_PENDING_REASON
    assert released["expiry_released_by"] == "policy:cancel_expire"
    again, _ = release(now=STUCK_AT + timedelta(hours=2))
    assert again["expiry_proof_sha256"] == released["expiry_proof_sha256"]   # same facts, same hash
    refused, why = release(open_orders=[{**LISTED, "filled_quantity": "0.2"}])
    assert refused is None and why == ["order_still_listed_open"]


def test_an_order_the_original_proof_would_release_is_not_recorded_as_a_uat_exception():
    after = CLOSE + timedelta(hours=2)
    for uat in (False, True):
        released, found = release(now=after, uat=uat, open_orders=[])
        assert found == []
        assert released["expiry_basis"] == recovery.BASIS_SESSION_OVER
        assert released["terminal_reason"] == recovery.EXPIRY_TERMINAL_REASON
    plain, _ = release(now=after, uat=False, open_orders=[])
    flagged, _ = release(now=after, uat=True, open_orders=[])
    assert plain["expiry_proof_sha256"] == flagged["expiry_proof_sha256"]    # the original hash, unchanged
    refused, why = release(now=after, uat=False)                              # listed, no UAT rule
    assert refused is None and why == ["order_still_listed_open"]


def test_the_proof_reports_which_rule_it_used():
    facts = dict(open_orders=[LISTED], holdings=HOLDINGS, tolerance=TOLERANCE)
    broker = detail()
    assert recovery.expiry_proof(intent(), broker, summary_of(broker), now=STUCK_AT,
                                 uat_stuck_pending=True, **facts) == ([], recovery.BASIS_UAT_STUCK_PENDING)
    assert recovery.expiry_proof(intent(), broker, summary_of(broker), now=STUCK_AT,
                                 uat_stuck_pending=False, **facts)[1] is None
    facts["open_orders"] = []
    assert recovery.expiry_proof(intent(), broker, summary_of(broker),
                                 now=CLOSE + timedelta(hours=1), uat_stuck_pending=True,
                                 **facts) == ([], recovery.BASIS_SESSION_OVER)


# ----------------------------------------- what the dispatch guards rely on to ignore it
RELEASED = {"run_id": RUN, "symbol": "UBER", "status": "EXPIRED", "broker_status": "PENDING",
            "expiry_released": True, "expiry_basis": recovery.BASIS_UAT_STUCK_PENDING}


def test_a_released_order_is_ignored_only_while_the_broker_still_says_pending_and_unfilled():
    assert recovery.is_released_uat_stuck(LISTED, RELEASED)
    for row in ({**LISTED, "status": "WORKING"}, {**LISTED, "status": "SUBMITTED"},
                {**LISTED, "status": "PARTIAL_FILLED", "filled_quantity": "0.4"},
                {**LISTED, "filled_quantity": "0.01"}, {**LISTED, "filled_quantity": None},
                {k: v for k, v in LISTED.items() if k != "filled_quantity"},
                {**LISTED, "filled_quantity": "NaN"}, {**LISTED, "symbol": "AAPL"},
                {**LISTED, "symbol": ""}, {**LISTED, "client_order_id": "f" * 32}, None, "row"):
        assert not recovery.is_released_uat_stuck(row, RELEASED), row


def test_only_an_order_this_rule_released_is_ever_ignored():
    for changes in ({"status": "CANCEL_UNKNOWN"}, {"status": "FILLED"}, {"status": "MANUAL_RECONCILIATION_REQUIRED"},
                    {"expiry_released": False}, {"expiry_released": None},
                    {"expiry_basis": recovery.BASIS_SESSION_OVER}, {"expiry_basis": None},
                    {"broker_status": "FILLED"}, {"broker_status": "EXPIRED"}, {"symbol": "AAPL"},
                    {"run_id": "f" * 32}, {"run_id": ""}):
        assert not recovery.is_released_uat_stuck(LISTED, {**RELEASED, **changes}), changes
    assert not recovery.is_released_uat_stuck(LISTED, None)
    assert not recovery.is_released_uat_stuck(LISTED, "intent")


# --------------------------------------------------- through the real order worker
def _listed(run_id, pending, **changes):
    row = {"client_order_id": run_id, "symbol": "AAPL", "side": pending["side"], "status": "PENDING",
           "filled_quantity": "0", "total_quantity": str(pending["total_quantity"]),
           "order_type": "MARKET", "time_in_force": "DAY"}
    row.update(changes)
    return row


def _list_open(monkeypatch, *rows):
    monkeypatch.setattr(main, "fetch_open_orders", lambda tc, symbol: list(rows))


def test_uat_releases_a_stuck_pending_order_during_the_session_and_the_next_slot_can_trade(monkeypatch):
    run_id, pending, attempts = _zombie(monkeypatch, "cancel_expire")
    assert (REFUSED_E2E - PLACED_E2E).total_seconds() < recovery.UAT_STUCK_PENDING_SECONDS
    stuck_at = PLACED_E2E + timedelta(seconds=recovery.UAT_STUCK_PENDING_SECONDS)
    session_end = datetime(2026, 7, 23, 20, 0, tzinfo=UTC)
    assert stuck_at < session_end                                  # the session is still running

    # 29 minutes: nothing is even read (the broker keeps listing the order meanwhile).
    _clock(monkeypatch, stuck_at - timedelta(minutes=1))
    _stub_broker(monkeypatch, holdings_after=8.0, detail=pending)
    monkeypatch.setattr(main, "fetch_open_orders", lambda *_: pytest.fail("read before the age"))
    assert _work()[0]["status"] == "CANCEL_UNKNOWN" and _fenced_runs() == {run_id}

    # 30 minutes: PENDING, nothing filled, still listed as exactly that -> released.
    _clock(monkeypatch, stuck_at)
    _stub_broker(monkeypatch, holdings_after=8.0, detail=pending)
    _list_open(monkeypatch, _listed(run_id, pending))
    released = _work()[0]

    assert released["status"] == "EXPIRED" and not released.get("needs_manual_check")
    row = _intent(run_id)
    assert row["status"] == "EXPIRED" and row["broker_status"] == "PENDING"
    assert row["expiry_basis"] == recovery.BASIS_UAT_STUCK_PENDING
    assert row["terminal_reason"] == recovery.UAT_STUCK_PENDING_REASON
    assert row["expiry_released"] is True and row["expiry_proof_sha256"]
    assert not row.get("cancel_confirmed_at")
    assert float(row["filled_quantity"]) == 0 and not row.get("cashflow_finalized")
    assert _cashflow()["finalized_seq"] == 0                       # nothing was booked
    assert _fenced_runs() == set() and _halted() == []             # no fence, no halt
    assert attempts == [run_id]                                    # the one cancel never repeated
    warning = FAKE_DB.reference("webull_lego_warnings/day_expiry_released").get()
    assert warning and warning["basis"] == recovery.BASIS_UAT_STUCK_PENDING

    # The next slot decides and places even though the broker still lists the released order.
    placed = []
    body, code = _run(monkeypatch, datetime(2026, 7, 23, 19, 30, 5, tzinfo=UTC), 331.0, holdings=8.0)
    assert code == 200 and body["status"] in {"READY_BUY", "READY_SELL"}
    _stub_broker(monkeypatch, holdings_after=8.0, detail=pending,
                 place=lambda tc, order: placed.append(order) or {"order_status": "SUBMITTED"})
    _list_open(monkeypatch, _listed(run_id, pending))
    _work()
    assert len(placed) == 1                                        # a new order, not blocked
    assert FAKE_DB.reference("webull_lego_warnings/uat_stuck_pending_ignored").get()["ignored_released"] == 1


def test_a_listing_that_is_not_pending_and_unfilled_keeps_the_hold_and_the_fence(monkeypatch):
    run_id, pending, _ = _zombie(monkeypatch, "cancel_expire")
    at = PLACED_E2E + timedelta(minutes=45)
    for why, row in (("working", dict(status="WORKING")), ("part filled", dict(filled_quantity="0.1")),
                     ("another size", dict(total_quantity="99"))):
        _clock(monkeypatch, at)
        _stub_broker(monkeypatch, holdings_after=8.0, detail=pending)
        _list_open(monkeypatch, _listed(run_id, pending, **row))
        held = _work()[0]
        assert held["status"] == "CANCEL_UNKNOWN" and not held.get("needs_manual_check"), why
        assert _fenced_runs() == {run_id} and _halted() == [], why
        assert _intent(run_id)["expiry_proof_blockers"] == ["order_still_listed_open"], why


def test_the_broker_moving_holdings_or_being_unreadable_keeps_the_hold(monkeypatch):
    run_id, pending, _ = _zombie(monkeypatch, "cancel_expire")
    at = PLACED_E2E + timedelta(minutes=45)
    _clock(monkeypatch, at)
    _stub_broker(monkeypatch, holdings_after=9.37, detail=pending)         # the position moved
    _list_open(monkeypatch, _listed(run_id, pending))
    assert _work()[0]["status"] == "CANCEL_UNKNOWN" and _fenced_runs() == {run_id}
    assert _intent(run_id)["expiry_proof_blockers"] == ["holdings_changed"]

    def down(tc, symbol):
        raise TimeoutError("open orders unavailable")

    _stub_broker(monkeypatch, holdings_after=8.0, detail=pending)
    monkeypatch.setattr(main, "fetch_open_orders", down)
    assert _work()[0]["status"] == "CANCEL_UNKNOWN" and _fenced_runs() == {run_id}
    assert _halted() == []


def test_on_prod_nothing_is_read_before_the_margin_and_a_listed_order_is_never_released(monkeypatch):
    run_id, pending, _ = _zombie(monkeypatch, "cancel_expire")
    monkeypatch.setattr(main, "environment_label", lambda: lego_orders.PROD)   # real label
    _clock(monkeypatch, PLACED_E2E + timedelta(minutes=45))
    _stub_broker(monkeypatch, holdings_after=8.0, detail=pending)
    monkeypatch.setattr(main, "fetch_open_orders", lambda *_: pytest.fail("PROD read before the margin"))
    assert _work()[0]["status"] == "CANCEL_UNKNOWN" and _fenced_runs() == {run_id}
    _clock(monkeypatch, datetime(2026, 7, 23, 20, 59, tzinfo=UTC))
    assert _work()[0]["status"] == "CANCEL_UNKNOWN"                        # still before close + 1h
    _clock(monkeypatch, PAST_MARGIN)
    _list_open(monkeypatch, _listed(run_id, pending))
    held = _work()[0]
    assert held["status"] == "CANCEL_UNKNOWN" and _fenced_runs() == {run_id}
    assert _intent(run_id)["expiry_proof_blockers"] == ["order_still_listed_open"]
    _list_open(monkeypatch)                                                # only a real removal counts
    assert _work()[0]["status"] == "EXPIRED"
    assert _intent(run_id)["expiry_basis"] == recovery.BASIS_SESSION_OVER


def test_after_the_close_a_listed_pending_order_is_still_released_on_uat(monkeypatch):
    run_id, pending, _ = _zombie(monkeypatch, "cancel_expire")
    _clock(monkeypatch, PAST_MARGIN)
    _stub_broker(monkeypatch, holdings_after=8.0, detail=pending)
    _list_open(monkeypatch, _listed(run_id, pending))
    assert _work()[0]["status"] == "EXPIRED"
    assert _intent(run_id)["expiry_basis"] == recovery.BASIS_UAT_STUCK_PENDING
    assert _fenced_runs() == set() and _halted() == []


# ---------------------------------------------- a person confirms an order that is already halted
def _halted_incident(monkeypatch):
    """What happened 2026-10-12: the hold ran out, the system halted, the broker still lists it."""
    run_id, pending, _ = _zombie(monkeypatch, "cancel")          # no auto release: the old halt
    _clock(monkeypatch, HALT_TIME)
    _stub_broker(monkeypatch, holdings_after=8.0, detail=pending)
    assert _work()[0]["status"] == "MANUAL_RECONCILIATION_REQUIRED"
    assert len(_halted()) == 1 and _fenced_runs() == {run_id}
    return run_id, pending, _intent(run_id)


def test_a_person_can_release_the_halted_incident_order_on_uat_but_not_on_prod(monkeypatch):
    run_id, pending, row = _halted_incident(monkeypatch)
    identity = row["runtime_identity_fingerprint"]
    listed = [_listed(run_id, pending)]
    tolerance = main._holdings_drift_tolerance()
    _clock(monkeypatch, NEXT_OPEN)

    with pytest.raises(ValueError, match="order_still_listed_open"):          # PROD / the old tool
        resume.expiry_plan(row, pending, listed, 8.0, identity, NEXT_OPEN, tolerance)
    plan = resume.expiry_plan(row, pending, listed, 8.0, identity, NEXT_OPEN, tolerance,
                              uat_stuck_pending=True)
    assert plan["confirmation"].startswith("RELEASE-EXPIRED ")
    assert plan["basis"] == recovery.BASIS_UAT_STUCK_PENDING and plan["broker_status"] == "PENDING"
    unlisted = resume.expiry_plan(row, pending, [], 8.0, identity, NEXT_OPEN, tolerance,
                                  uat_stuck_pending=True)
    assert unlisted["basis"] == recovery.BASIS_SESSION_OVER                   # nothing relaxed
    assert plan["confirmation"] != unlisted["confirmation"]                   # the basis is confirmed
    for bad in (dict(holdings=9.37), dict(listed=[{**listed[0], "status": "WORKING"}]),
                dict(listed=[{**listed[0], "filled_quantity": "0.3"}])):
        with pytest.raises(ValueError, match="holdings_changed|order_still_listed_open"):
            resume.expiry_plan(row, pending, bad.get("listed", listed), bad.get("holdings", 8.0),
                               identity, NEXT_OPEN, tolerance, uat_stuck_pending=True)

    def replan(current):
        return resume.expiry_plan(current, pending, listed, 8.0, identity, NEXT_OPEN, tolerance,
                                  uat_stuck_pending=True)

    result = resume.apply_plan(plan, identity=identity, symbol="AAPL", operator="reviewer",
                               read_detail=lambda _: pending, replan=replan)
    assert result["money_fence_retained"] and result["operator_halt_retained"]
    assert _intent(run_id)["status"] == "PLACING_UNKNOWN" and _fenced_runs() == {run_id}

    # The worker proves it again itself -- under UAT it accepts the same listing ...
    _stub_broker(monkeypatch, holdings_after=8.0, detail=pending)
    _list_open(monkeypatch, _listed(run_id, pending))
    assert _work()[0]["status"] == "EXPIRED" and _fenced_runs() == set()
    released = _intent(run_id)
    assert released["expiry_released_by"] == "reviewer"
    assert released["expiry_basis"] == recovery.BASIS_UAT_STUCK_PENDING
    assert len(_halted()) == 1                                    # the halt still needs a second person
    assert _cashflow()["finalized_seq"] == 0


def test_the_worker_reproves_a_confirmed_order_and_prod_never_accepts_the_uat_basis(monkeypatch):
    run_id, pending, row = _halted_incident(monkeypatch)
    identity = row["runtime_identity_fingerprint"]
    listed = [_listed(run_id, pending)]
    tolerance = main._holdings_drift_tolerance()
    _clock(monkeypatch, NEXT_OPEN)
    plan = resume.expiry_plan(row, pending, listed, 8.0, identity, NEXT_OPEN, tolerance,
                              uat_stuck_pending=True)
    resume.apply_plan(plan, identity=identity, symbol="AAPL", operator="reviewer",
                      read_detail=lambda _: pending,
                      replan=lambda cur: resume.expiry_plan(cur, pending, listed, 8.0, identity,
                                                            NEXT_OPEN, tolerance,
                                                            uat_stuck_pending=True))
    # ... but the same re-armed order on a PROD environment is judged by the original proof.
    monkeypatch.setattr(main, "environment_label", lambda: lego_orders.PROD)   # real label
    _stub_broker(monkeypatch, holdings_after=8.0, detail=pending)
    _list_open(monkeypatch, _listed(run_id, pending))
    stopped = _work()[0]
    assert stopped["status"] != "EXPIRED" and stopped.get("needs_manual_check")
    assert _fenced_runs() == {run_id} and not _intent(run_id).get("expiry_released")


def _run_tool(monkeypatch, capsys, *, environment, row, pending, listed, holdings=8.0, confirm=None):
    import sys
    import execution_service
    import webull_io
    from types import SimpleNamespace
    monkeypatch.setattr(sys, "argv", ["resume", "--chain", row["chain_key"], "--run", row["run_id"],
                                      "--operator", "reviewer", "--expiry-proof"]
                        + (["--confirm", confirm] if confirm else []))
    monkeypatch.setattr(execution_service, "_init_firebase", lambda: None)
    monkeypatch.setattr(webull_io, "runtime_identity_fingerprint",
                        lambda: row["runtime_identity_fingerprint"])
    monkeypatch.setattr(resume.outbox, "read_intent", lambda chain, run: row)
    monkeypatch.setattr(webull_io, "load_config", lambda: SimpleNamespace(symbol="AAPL"))
    monkeypatch.setattr(webull_io, "build_clients", lambda: (object(), object()))
    monkeypatch.setattr(webull_io, "fetch_order_detail", lambda tc, run: pending)
    monkeypatch.setattr(webull_io, "fetch_open_orders", lambda tc, symbol: listed)
    monkeypatch.setattr(webull_io, "fetch_holdings", lambda tc, cfg: holdings)
    # The real label, not a stub: environment_label() returns "Test (UAT)" / "Production",
    # and a stub returning "UAT" once hid a comparison that never matched on real UAT.
    monkeypatch.setenv("WEBULL_ENV", environment)
    capsys.readouterr()                       # drop the worker's own log lines from the setup
    resume.main()
    return capsys.readouterr().out


def test_the_resume_tool_applies_the_uat_rule_only_on_uat(monkeypatch, capsys):
    import json
    run_id, pending, row = _halted_incident(monkeypatch)
    listed = [_listed(run_id, pending)]
    _clock(monkeypatch, NEXT_OPEN)
    shown = json.loads(_run_tool(monkeypatch, capsys, environment="UAT", row=row, pending=pending,
                                 listed=listed))
    assert shown["dry_run"] is True and shown["basis"] == recovery.BASIS_UAT_STUCK_PENDING
    assert shown["confirmation"].startswith("RELEASE-EXPIRED ")
    with pytest.raises(ValueError, match="expiry proof not met: order_still_listed_open"):
        _run_tool(monkeypatch, capsys, environment="PROD", row=row, pending=pending, listed=listed)
    prod_unlisted = json.loads(_run_tool(monkeypatch, capsys, environment="PROD", row=row,
                                         pending=pending, listed=[]))
    assert prod_unlisted["basis"] == recovery.BASIS_SESSION_OVER


# ----------------------------------------------------------------- the audit tool
def test_a_released_stuck_order_is_a_finding_for_webull_not_an_unresolved_order():
    world = make_world()
    stuck = world["webull_lego_order_outbox"][CHAIN][world_run_id(10)]
    stuck.update({"status": "EXPIRED", "needs_manual_check": False, "broker_status": "PENDING",
                  "expiry_released": True, "expiry_basis": recovery.BASIS_UAT_STUCK_PENDING,
                  "expiry_released_at": "2026-10-12T14:31:20+00:00",
                  "terminal_reason": recovery.UAT_STUCK_PENDING_REASON})
    report = ta.build_report(rtdb=world)
    by = {f["code"]: f for f in report["findings"]}
    assert "UAT_STUCK_PENDING_RELEASED" in by and by["UAT_STUCK_PENDING_RELEASED"]["level"] == "P2"
    assert world_run_id(10)[:8] in by["UAT_STUCK_PENDING_RELEASED"]["text"]
    assert "Webull" in by["UAT_STUCK_PENDING_RELEASED"]["text"]
    assert "ORDER_UNRESOLVED" not in by and "EXPIRY_PROOF_BLOCKED" not in by
    assert "RELEASED (UAT stuck PENDING)" in ta.render(report)


def test_the_documented_recovery_of_the_halted_order_works_end_to_end_on_uat(monkeypatch, capsys):
    """docs/AUDIT_20261013_TH.md steps 2-4, in order, with the real CLI and the real label.

    The broker keeps listing the order the whole time. Pass 3 of the audit found that the
    tool compared the label with "UAT" while environment_label() says "Test (UAT)", so on
    real UAT step 2 would have failed with order_still_listed_open; this walks the whole path.
    """
    import json
    import operator_halt
    run_id, pending, row = _halted_incident(monkeypatch)
    identity = row["runtime_identity_fingerprint"]
    listed = [_listed(run_id, pending)]
    _clock(monkeypatch, NEXT_OPEN)

    # Step 2a: dry run of the CLI on UAT.
    shown = json.loads(_run_tool(monkeypatch, capsys, environment="UAT", row=row, pending=pending,
                                 listed=listed))
    assert shown["dry_run"] is True and shown["basis"] == recovery.BASIS_UAT_STUCK_PENDING
    # Step 2b: the same command with the printed confirmation re-arms the order.
    applied = json.loads(_run_tool(monkeypatch, capsys, environment="UAT", row=row, pending=pending,
                                   listed=listed, confirm=shown["confirmation"]))
    assert applied["reconciliation_resumed"] and applied["operator_halt_retained"]
    assert _intent(run_id)["status"] == "PLACING_UNKNOWN" and _fenced_runs() == {run_id}

    # Step 2c: the worker proves it again itself and releases it; the broker still lists it.
    _stub_broker(monkeypatch, holdings_after=8.0, detail=pending)
    _list_open(monkeypatch, *listed)
    assert _work()[0]["status"] == "EXPIRED" and _fenced_runs() == set()
    assert _intent(run_id)["expiry_basis"] == recovery.BASIS_UAT_STUCK_PENDING

    # Step 4: a second person clears the halt the system set.
    (halt,) = _halted()
    with pytest.raises(ValueError, match="second operator"):
        operator_halt.clear_halt(identity, "AAPL", expected_halt_id=halt["halt_id"],
                                 operator="system:order-recovery", reason="x", apply=True)
    operator_halt.clear_halt(identity, "AAPL", expected_halt_id=halt["halt_id"],
                             operator="second-reviewer", reason="UAT stuck order released",
                             apply=True)
    assert _halted() == []

    # Trading resumes although the broker still lists the released order.
    body, code = _run(monkeypatch, NEXT_OPEN, 331.0, holdings=8.0)
    assert code == 200 and body["status"] in {"READY_BUY", "READY_SELL"}
    placed = []
    _stub_broker(monkeypatch, holdings_after=8.0, detail=pending,
                 place=lambda tc, order: placed.append(order) or {"order_status": "SUBMITTED"})
    _list_open(monkeypatch, *listed)
    _work()
    assert len(placed) == 1
