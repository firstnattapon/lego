"""2026-10-06: a refused UAT order stayed PENDING past the close and ended in a halt.
(The last section, F6, is the PROD half of the same promise: reads and reconcile keep
working when only the token's refresh margin is short.)

Run 56340902... (BUY 0.57 UBER, MARKET/DAY/CORE) was placed 15:45:27Z and went stale. The
one cancel (15:51:10Z) was refused with 417 OPENAPI_ORDER_CANNOT_OPERATE. The order was
read 298 times and was still PENDING 3h51m after the 20:00Z close; the 8 hour hold then
expired into MANUAL_RECONCILIATION_REQUIRED plus an operator halt that carried into the next
session. Neither resume_order_reconciliation nor lego_admin_reconcile could release it:
both need a terminal broker status.

These pin the release that closes that dead end -- and everything that must keep it shut:
only a refused, fill-less, unlisted DAY/CORE market order whose session is over plus a margin,
with holdings unchanged, is released as EXPIRED; PROD cannot opt in; a person can confirm the
same proof for an order that is already halted. The values are the incident's own.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from webull.core.exception.exceptions import ServerException

import main
import operator_halt
import order_recovery as recovery
import webull_io
from config import ConfigurationError, load_runtime_config
from conftest import FAKE_DB
from lego_outbox import OUTBOX_PATH, list_actionable
from lego_state import chain_key
from recovery_policy import RecoveryPolicy
from test_execution_confirmed_cashflow import (  # noqa: F401  (env is an autouse fixture)
    SLOT_0, SLOT_1, _cashflow, _cfg, _fenced_runs, _fixed_now, _intent, _run, _stub_broker,
    _work, env)
from test_webull_io import (  # noqa: F401  (account / fresh_client_cache are autouse fixtures)
    _install_fake_sdk, _write_token, account, fresh_client_cache)
from tools import resume_order_reconciliation as resume

UTC = timezone.utc
RUN = "56340902044c15bea43c31bb4b6b46b4"
BROKER_ID = "0388HSGMH480O0KF4Q5C000000"
PLACED = datetime(2026, 10, 6, 15, 45, 27, tzinfo=UTC)
REFUSED = datetime(2026, 10, 6, 15, 51, 6, tzinfo=UTC)
CLOSE = datetime(2026, 10, 6, 20, 0, tzinfo=UTC)
PROVEN = CLOSE + timedelta(seconds=recovery.EXPIRY_MARGIN_SECONDS)  # 21:00Z
TOLERANCE = 0.000001


def intent(**changes):
    policy = RecoveryPolicy("cancel_expire")
    doc = {
        "run_id": RUN, "chain_key": "chain", "symbol": "UBER", "side": "BUY", "quantity": 0.57,
        "broker_order_id": BROKER_ID, "place_attempted": True,
        "placed_at": PLACED.isoformat(), "decision_holdings": 143.9528,
        "cancel_attempt_count": 1, "cancel_refused_at": REFUSED.isoformat(),
        "cancel_last_error_code": recovery.CANCEL_REFUSED, "status": "CANCEL_UNKNOWN",
        "cancel_policy": policy.snapshot(), "cancel_policy_hash": policy.fingerprint,
        "order_payload": [{
            "client_order_id": RUN, "combo_type": "NORMAL", "entrust_type": "QTY",
            "instrument_type": "EQUITY", "market": "US", "order_type": "MARKET",
            "quantity": "0.57", "side": "BUY", "support_trading_session": "CORE",
            "symbol": "UBER", "time_in_force": "DAY"}],
    }
    doc.update(changes)
    return doc


def detail(**changes):
    doc = {"client_order_id": RUN, "order_id": BROKER_ID, "symbol": "UBER", "side": "BUY",
           "total_quantity": "0.57", "filled_quantity": "0", "status": "PENDING"}
    doc.update(changes)
    return doc


def summary_of(broker):
    return {"status": broker.get("status"), "filled_quantity": broker.get("filled_quantity")}


def blockers(*, doc=None, broker=None, open_orders=(), holdings=143.9528, now=PROVEN):
    doc = doc or intent()
    broker = broker or detail()
    return recovery.expiry_proof_blockers(
        doc, broker, summary_of(broker),
        open_orders=None if open_orders is None else list(open_orders),
        holdings=holdings, now=now, tolerance=TOLERANCE)


# ------------------------------------------------------------------ the proof
def test_the_incidents_own_values_are_proven_at_close_plus_margin():
    assert blockers() == []
    assert blockers(now=PROVEN - timedelta(seconds=1)) == ["session_not_over_plus_margin"]
    assert blockers(now=datetime(2026, 10, 6, 23, 51, 7, tzinfo=UTC)) == []


def test_one_missing_condition_is_enough_to_refuse():
    listed = {"client_order_id": RUN, "symbol": "UBER", "status": "PENDING"}
    cases = [
        (dict(now=PROVEN - timedelta(minutes=1)), "session_not_over_plus_margin"),
        (dict(now=CLOSE - timedelta(hours=1)), "session_not_over_plus_margin"),
        (dict(broker=detail(filled_quantity="0.1", status="PARTIAL_FILLED")), "filled_quantity_not_zero"),
        (dict(broker=detail(status="CANCELLED")), "broker_status_terminal"),
        (dict(broker=detail(status="EXPIRED")), "broker_status_terminal"),
        (dict(broker=detail(status="UNKNOWN")), "broker_status_unrecognized"),
        (dict(broker=detail(status="SUBMITTED")), None),                   # also still live
        (dict(broker=detail(filled_quantity=None)), "broker_evidence_invalid"),
        (dict(broker=detail(symbol="OTHER")), "broker_evidence_invalid"),
        (dict(broker=detail(side="SELL")), "broker_evidence_invalid"),
        (dict(broker=detail(total_quantity="0.58")), "broker_evidence_invalid"),
        (dict(open_orders=[listed]), "order_still_listed_open"),
        (dict(open_orders=None), "open_orders_unread"),
        (dict(holdings=144.5), "holdings_changed"),
        (dict(holdings=0.0), "holdings_changed"),
        (dict(holdings=float("nan")), "holdings_unverifiable"),
        (dict(doc=intent(decision_holdings=None)), "holdings_unverifiable"),
        (dict(doc=intent(cancel_refused_at=None)), "cancel_not_refused"),
        (dict(doc=intent(cancel_attempt_count=0, cancel_refused_at=None)),
         "cancel_not_attempted"),
        (dict(doc=intent(place_attempted=False)), "place_not_attempted"),
        (dict(doc=intent(order_payload=None)), "payload_not_single_leg"),
    ]
    for kwargs, expected in cases:
        found = blockers(**kwargs)
        if expected is None:
            assert found == [], (kwargs, found)
        else:
            assert expected in found, (kwargs, found)


@pytest.mark.parametrize("key,value,expected", [
    ("order_type", "LIMIT", "payload_order_type_not_market"),
    ("time_in_force", "GTC", "payload_time_in_force_not_day"),
    ("support_trading_session", "ALL", "payload_support_trading_session_not_core")])
def test_only_a_market_day_core_order_can_be_released(key, value, expected):
    doc = intent()
    doc["order_payload"] = [{**doc["order_payload"][0], key: value}]
    assert blockers(doc=doc) == [expected]


def test_the_session_is_the_one_the_order_was_placed_in():
    saturday = intent(placed_at=datetime(2026, 10, 10, 15, tzinfo=UTC).isoformat())
    assert blockers(doc=saturday, now=datetime(2026, 10, 12, tzinfo=UTC)) \
        == ["placed_outside_regular_session"]
    after_close = intent(placed_at=datetime(2026, 10, 6, 20, 5, tzinfo=UTC).isoformat())
    assert blockers(doc=after_close, now=datetime(2026, 10, 7, tzinfo=UTC)) \
        == ["placed_outside_regular_session"]


def test_an_early_close_session_ends_three_hours_sooner():
    # The day after Thanksgiving closes at 13:00 New York = 18:00Z.
    placed = intent(placed_at=datetime(2026, 11, 27, 15, tzinfo=UTC).isoformat())
    close = datetime(2026, 11, 27, 18, tzinfo=UTC)
    assert blockers(doc=placed, now=close + timedelta(minutes=59)) == ["session_not_over_plus_margin"]
    assert blockers(doc=placed, now=close + timedelta(hours=1)) == []


# ------------------------------------------------- who may ask, and what they get
def test_the_policy_and_a_person_are_the_only_ways_to_ask():
    assert recovery.expiry_candidate(intent(), now=PROVEN)
    assert not recovery.expiry_candidate(intent(), now=PROVEN - timedelta(seconds=1))
    cancel = RecoveryPolicy("cancel")
    plain = intent(cancel_policy=cancel.snapshot(), cancel_policy_hash=cancel.fingerprint)
    assert not recovery.expiry_candidate(plain, now=PROVEN)
    hold = intent(cancel_policy=RecoveryPolicy().snapshot(),
                  cancel_policy_hash=RecoveryPolicy().fingerprint)
    assert not recovery.expiry_candidate(hold, now=PROVEN)
    assert not recovery.expiry_candidate(intent(cancel_policy_hash="0" * 64), now=PROVEN)
    assert not recovery.expiry_candidate(intent(cancel_policy=None), now=PROVEN)
    assert not recovery.expiry_candidate(intent(needs_manual_check=True), now=PROVEN)
    approved = {"expiry_proof_authorized": True, "operator": "reviewer"}
    assert recovery.expiry_candidate({**plain, "reconcile_resume": approved}, now=PROVEN)
    for weak in ({"expiry_proof_authorized": True, "operator": " "},
                 {"expiry_proof_authorized": "yes", "operator": "reviewer"},
                 {"operator": "reviewer"}):
        assert not recovery.expiry_candidate({**plain, "reconcile_resume": weak}, now=PROVEN)


def test_a_release_is_an_expired_zero_fill_that_still_says_pending():
    broker = detail()
    released, found = recovery.expiry_release(
        intent(), broker, summary_of(broker), open_orders=[], holdings=143.9528,
        tolerance=TOLERANCE, now=PROVEN)
    assert found == [] and released["status"] == "EXPIRED"
    assert released["broker_status"] == "PENDING"        # never reads as a broker expiry
    assert released["filled_quantity"] == "0"
    assert released["terminal_reason"] == recovery.EXPIRY_TERMINAL_REASON
    assert released["expiry_released"] is True and released["expiry_released_by"] == "policy:cancel_expire"
    again, _ = recovery.expiry_release(
        intent(), broker, summary_of(broker), open_orders=[], holdings=143.9528,
        tolerance=TOLERANCE, now=PROVEN + timedelta(hours=2))
    assert again["expiry_proof_sha256"] == released["expiry_proof_sha256"]  # same facts, same hash
    refused, why = recovery.expiry_release(
        intent(), broker, summary_of(broker), open_orders=[], holdings=1.0,
        tolerance=TOLERANCE, now=PROVEN)
    assert refused is None and why == ["holdings_changed"]


def test_existing_policies_keep_their_fingerprint_and_cancel_still_refuses_nothing_new():
    # The hash the deployed 00040 intents carry (action=cancel, 300 s, 120 s, 1 mutation).
    assert RecoveryPolicy("cancel").fingerprint == (
        "e583ee789d8132fcaf778f38efbbf079e817b242e5aec48793717e84b9b10890")
    assert RecoveryPolicy("cancel_expire").fingerprint != RecoveryPolicy("cancel").fingerprint
    with pytest.raises(ValueError):
        RecoveryPolicy("expire")


def test_prod_cannot_opt_in_but_uat_can():
    base = {"LEGO_SYMBOL": "UBER", "LEGO_FIX_C": "10000", "LEGO_DIFF": "25",
            "WEBULL_ACCOUNT_ID": "test", "LEGO_CANDIDATE_HASH": "hash"}
    uat = load_runtime_config({**base, "WEBULL_ENV": "UAT", "LEGO_STALE_ORDER_ACTION": "cancel_expire"})
    assert uat.deployment.recovery_policy.action == "cancel_expire"
    with pytest.raises(ConfigurationError, match="UAT only"):
        load_runtime_config({**base, "WEBULL_ENV": "PROD", "LEGO_STALE_ORDER_ACTION": "cancel_expire"})
    for action in ("hold", "cancel"):
        assert load_runtime_config({**base, "WEBULL_ENV": "PROD", "LEGO_STALE_ORDER_ACTION": action}) \
            .deployment.recovery_policy.action == action


# ---------------------------------------------------- through the real order worker
# The refused order of the incident, driven through _run_order_worker: stale -> the one
# cancel is refused by an SDK ServerException -> the session ends -> what happens next.
PLACED_E2E = datetime(2026, 7, 23, 18, 31, tzinfo=UTC)       # inside the 13:30-20:00Z session
REFUSED_E2E = datetime(2026, 7, 23, 18, 40, tzinfo=UTC)
PAST_MARGIN = datetime(2026, 7, 23, 21, 30, tzinfo=UTC)      # close 20:00Z + 1h30
HALT_TIME = datetime(2026, 7, 24, 3, 0, tzinfo=UTC)          # past the 8 hour hold
NEXT_OPEN = datetime(2026, 7, 24, 14, 0, 5, tzinfo=UTC)


def _clock(monkeypatch, moment):
    monkeypatch.setattr(recovery, "datetime", _fixed_now(moment))


def _zombie(monkeypatch, action):
    """A placed AAPL order that went stale and whose one cancel was refused."""
    FAKE_DB.store.clear()
    _run(monkeypatch, SLOT_0, 320.0, holdings=0.0)
    body, _ = _run(monkeypatch, SLOT_1, 330.0, holdings=8.0)
    run_id = body["run_id"]
    _stub_broker(monkeypatch, holdings_after=8.0, detail={"order_status": "SUBMITTED", "filled_quantity": 0})
    _work()
    row = _intent(run_id)
    ordered = float(row["quantity"])
    policy = RecoveryPolicy(action)
    FAKE_DB.reference(f"{OUTBOX_PATH}/{chain_key(_cfg())}/{run_id}").update({
        "cancel_policy": policy.snapshot(), "cancel_policy_hash": policy.fingerprint,
        "placed_at": PLACED_E2E.isoformat(),
        "order_payload": [{
            "client_order_id": run_id, "combo_type": "NORMAL", "entrust_type": "QTY",
            "instrument_type": "EQUITY", "market": "US", "order_type": "MARKET",
            "quantity": str(row["quantity"]), "side": row["side"],
            "support_trading_session": "CORE", "symbol": "AAPL", "time_in_force": "DAY"}]})
    pending = {"client_order_id": run_id, "symbol": "AAPL", "side": row["side"],
               "order_id": "BRK-1", "total_quantity": ordered, "filled_quantity": 0,
               "order_status": "PENDING"}
    attempts = []

    def refuse(trade_client, client_order_id):
        attempts.append(client_order_id)
        raise ServerException("OPENAPI_ORDER_CANNOT_OPERATE", "The current status cannot be modified.", 417)

    monkeypatch.setattr(webull_io, "cancel_order", refuse)
    _clock(monkeypatch, REFUSED_E2E)
    _stub_broker(monkeypatch, holdings_after=8.0, detail=pending)
    first = _work()[0]
    assert attempts == [run_id] and first["status"] == "CANCEL_UNKNOWN"
    assert first["cancel_last_error_code"] == recovery.CANCEL_REFUSED
    return run_id, pending, attempts


def _halted():
    locks = FAKE_DB.reference("webull_lego_order_dispatch_locks").get() or {}
    return [doc["operator_halt"] for doc in locks.values()
            if isinstance(doc, dict) and (doc.get("operator_halt") or {}).get("halted")]


def test_a_refused_day_order_is_released_after_the_session_and_trading_resumes(monkeypatch):
    run_id, pending, attempts = _zombie(monkeypatch, "cancel_expire")
    seconds = (PAST_MARGIN - REFUSED_E2E).total_seconds()
    assert seconds < recovery.REFUSED_HOLD_SECONDS        # the hold is still running
    _clock(monkeypatch, PAST_MARGIN)
    _stub_broker(monkeypatch, holdings_after=8.0, detail=pending)

    released = _work()[0]

    assert released["status"] == "EXPIRED" and not released.get("needs_manual_check")
    row = _intent(run_id)
    assert row["status"] == "EXPIRED" and row["broker_status"] == "PENDING"
    assert row["terminal_reason"] == recovery.EXPIRY_TERMINAL_REASON
    assert row["expiry_released"] is True and row["expiry_proof_sha256"]
    assert not row.get("cancel_confirmed_at")             # the cancel was refused, never confirmed
    assert float(row["filled_quantity"]) == 0 and not row.get("cashflow_finalized")
    assert _cashflow()["finalized_seq"] == 0              # nothing was booked
    assert _fenced_runs() == set() and _halted() == []    # no halt, no fence
    assert attempts == [run_id]                           # the one cancel was never repeated
    assert FAKE_DB.reference("webull_lego_warnings/day_expiry_released").get()
    # The account is free again: the next session decides and nothing blocks it.
    _stub_broker(monkeypatch, holdings_after=8.0, detail=pending)
    body, code = _run(monkeypatch, NEXT_OPEN, 331.0, holdings=8.0)
    assert code == 200 and body["status"] != "PASS_RECOVERY_BLOCKED"
    assert run_id not in [i["run_id"] for i in list_actionable(chain_key(_cfg()))]


def test_cancel_without_the_expiry_action_ends_in_the_old_halt(monkeypatch):
    run_id, pending, _ = _zombie(monkeypatch, "cancel")
    _clock(monkeypatch, PAST_MARGIN)
    _stub_broker(monkeypatch, holdings_after=8.0, detail=pending)
    held = _work()[0]
    assert held["status"] == "CANCEL_UNKNOWN" and not held.get("needs_manual_check")
    assert _fenced_runs() == {run_id}
    _clock(monkeypatch, HALT_TIME)
    stopped = _work()[0]
    assert stopped["status"] == "MANUAL_RECONCILIATION_REQUIRED"
    assert _intent(run_id)["cancel_last_error_code"] == "CANCEL_REFUSED_HOLD_EXPIRED"
    assert _fenced_runs() == {run_id} and len(_halted()) == 1


@pytest.mark.parametrize("why,holdings,listed,broker_up", [
    ("holdings moved", 9.0, False, True),
    ("still listed as open", 8.0, True, True),
    ("open orders unreadable", 8.0, False, False)])
def test_a_missing_proof_keeps_the_hold_and_the_fence(monkeypatch, why, holdings, listed, broker_up):
    run_id, pending, _ = _zombie(monkeypatch, "cancel_expire")
    _clock(monkeypatch, PAST_MARGIN)
    _stub_broker(monkeypatch, holdings_after=holdings, detail=pending)
    if listed:
        monkeypatch.setattr(main, "fetch_open_orders", lambda tc, s: [
            {"client_order_id": run_id, "symbol": "AAPL", "status": "PENDING"}])
    if not broker_up:
        def down(tc, symbol):
            raise TimeoutError("open orders unavailable")
        monkeypatch.setattr(main, "fetch_open_orders", down)

    held = _work()[0]

    assert held["status"] == "CANCEL_UNKNOWN", why
    assert not held.get("needs_manual_check") and _fenced_runs() == {run_id}
    assert _intent(run_id)["status"] == "CANCEL_UNKNOWN" and _halted() == []


def test_before_the_margin_nothing_is_even_read(monkeypatch):
    run_id, pending, _ = _zombie(monkeypatch, "cancel_expire")
    _clock(monkeypatch, datetime(2026, 7, 23, 20, 59, tzinfo=UTC))
    _stub_broker(monkeypatch, holdings_after=8.0, detail=pending)
    monkeypatch.setattr(main, "fetch_open_orders", lambda *_: pytest.fail("read before the margin"))
    held = _work()[0]
    assert held["status"] == "CANCEL_UNKNOWN" and _fenced_runs() == {run_id}


def test_a_person_can_confirm_the_same_proof_for_an_order_that_is_already_halted(monkeypatch):
    # The order stuck in production-of-record: policy cancel (no auto release), the hold
    # expired and the system halted. Nothing but this path can free it.
    run_id, pending, _ = _zombie(monkeypatch, "cancel")
    _clock(monkeypatch, HALT_TIME)
    _stub_broker(monkeypatch, holdings_after=8.0, detail=pending)
    assert _work()[0]["status"] == "MANUAL_RECONCILIATION_REQUIRED"
    assert len(_halted()) == 1 and _fenced_runs() == {run_id}

    row = _intent(run_id)
    identity = row["runtime_identity_fingerprint"]
    _clock(monkeypatch, NEXT_OPEN)
    tolerance = main._holdings_drift_tolerance()

    def replan(current, holdings=8.0):
        return resume.expiry_plan(current, pending, [], holdings, identity, NEXT_OPEN, tolerance)

    proposed = replan(row)
    assert proposed["confirmation"].startswith("RELEASE-EXPIRED ") and proposed["broker_status"] == "PENDING"
    with pytest.raises(ValueError, match="holdings_changed"):
        replan(row, holdings=9.0)
    with pytest.raises(ValueError, match="evidence changed"):    # a stale confirmation is refused
        resume.apply_plan({**proposed, "holdings": 7.0}, identity=identity, symbol="AAPL",
                          operator="reviewer", read_detail=lambda _: pending, replan=replan)
    assert _intent(run_id)["needs_manual_check"]

    result = resume.apply_plan(proposed, identity=identity, symbol="AAPL", operator="reviewer",
                               read_detail=lambda _: pending, replan=replan)
    assert result["money_fence_retained"] and result["operator_halt_retained"]
    armed = _intent(run_id)
    assert armed["status"] == "PLACING_UNKNOWN" and not armed["needs_manual_check"]
    assert armed["reconcile_resume"]["expiry_proof_authorized"] is True
    assert _fenced_runs() == {run_id}                    # the tool released nothing itself

    _stub_broker(monkeypatch, holdings_after=8.0, detail=pending)
    released = _work()[0]
    assert released["status"] == "EXPIRED" and _fenced_runs() == set()
    assert _intent(run_id)["expiry_released_by"] == "reviewer"
    assert _cashflow()["finalized_seq"] == 0
    assert len(_halted()) == 1                           # the halt still needs its second person
    assert operator_halt.status(identity, "AAPL")["halted"]
    blocked, _ = _run(monkeypatch, NEXT_OPEN, 331.0, holdings=8.0)
    assert blocked["status"] == "PASS_OPERATOR_HALT"


def test_the_tool_refuses_an_order_that_is_not_halted_or_not_dead(monkeypatch):
    run_id, pending, _ = _zombie(monkeypatch, "cancel")
    row = _intent(run_id)
    tolerance = main._holdings_drift_tolerance()
    _clock(monkeypatch, NEXT_OPEN)
    with pytest.raises(ValueError, match="not eligible"):        # still held, not manual
        resume.expiry_plan(row, pending, [], 8.0, row["runtime_identity_fingerprint"], NEXT_OPEN, tolerance)
    with pytest.raises(ValueError, match="not eligible"):        # another account
        resume.expiry_plan({**row, "needs_manual_check": True}, pending, [], 8.0, "other", NEXT_OPEN, tolerance)
    with pytest.raises(ValueError, match="session_not_over_plus_margin"):
        resume.expiry_plan({**row, "needs_manual_check": True}, pending, [], 8.0,
                           row["runtime_identity_fingerprint"], REFUSED_E2E + timedelta(minutes=5), tolerance)


# ---------------------------------------------------------------- F6: PROD token margin
# new_order_token_block promises it "never prevents order reconciliation", yet build_clients
# refused to build on `ready`, which goes false three days before expiry. From then on an open
# PROD order could not even be read. The token still signs; only new orders should stop.
def _durable(**changes):
    health = {"ready": False, "margin_only": True, "secret_configured": True,
              "token_storage": "SECRET_MANAGER", "status": "NORMAL", "found": True,
              "days_left": 2, "ephemeral_token_dir": False,
              "expires_at": (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    health.update(changes)
    return health


def test_a_token_inside_the_margin_still_signs_but_opens_no_new_order():
    health = _durable()
    assert webull_io.token_can_sign_now(health)
    block = webull_io.new_order_token_block(health, "PROD")
    assert block and "refresh margin" in block
    assert webull_io.new_order_token_block(health, "UAT") is None
    assert webull_io.token_can_sign_now({"ready": True})


@pytest.mark.parametrize("changes", [
    {"margin_only": False},                                   # some other reason as well
    {"secret_configured": False},
    {"token_storage": "EPHEMERAL"},
    {"status": "EXPIRED"},
    {"expires_at": None},
    {"expires_at": "2026-10-06T10:00:00"},                    # no timezone
    {"expires_at": (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")},
    {"expires_at": (datetime.now(UTC) + timedelta(minutes=4)).strftime("%Y-%m-%dT%H:%M:%SZ")}])
def test_anything_else_keeps_the_client_closed(changes):
    health = _durable(**changes)
    assert not webull_io.token_can_sign_now(health)
    assert webull_io.new_order_token_block(health, "PROD")


def test_token_health_names_a_margin_only_block(tmp_path, monkeypatch):
    _write_token(tmp_path, monkeypatch, "tok", datetime.now(UTC) + timedelta(days=2))
    assert webull_io.token_health()["margin_only"] is True
    _write_token(tmp_path, monkeypatch, "tok", datetime.now(UTC) + timedelta(days=9))
    assert webull_io.token_health()["margin_only"] is False and webull_io.token_health()["ready"]
    _write_token(tmp_path, monkeypatch, "tok", datetime.now(UTC) + timedelta(days=9), "EXPIRED")
    assert webull_io.token_health()["margin_only"] is False      # the status is the reason
    _write_token(tmp_path, monkeypatch, "tok", datetime.now(UTC) - timedelta(days=1))
    expired = webull_io.token_health()
    assert expired["margin_only"] is True and not webull_io.token_can_sign_now(
        {**expired, "secret_configured": True, "token_storage": "SECRET_MANAGER"})
    monkeypatch.undo()
    monkeypatch.setenv("WEBULL_TOKEN_DIR", "/tmp/webull_token")   # ephemeral dir + short token
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "acc-1")
    monkeypatch.setattr(webull_io, "read_local_token", lambda: {
        "token": "tok", "expires": "0", "status": "NORMAL",
        "expires_at": datetime.now(UTC) + timedelta(days=2)})
    assert webull_io.token_health()["margin_only"] is False       # two independent reasons


def _prod_build(monkeypatch, health):
    built = _install_fake_sdk(monkeypatch)
    verified = []
    monkeypatch.setattr(webull_io, "verify_production_token", lambda api: verified.append(api))
    monkeypatch.setattr(webull_io, "token_health", lambda: health)
    monkeypatch.setenv("WEBULL_APP_KEY", "key")
    monkeypatch.setenv("WEBULL_APP_SECRET", "secret")
    monkeypatch.setenv("WEBULL_ENV", "PROD")
    # PROD tokens come from the durable secret, which ensure_token_fresh never rotates.
    monkeypatch.setenv("WEBULL_TOKEN_SECRET", "projects/p/secrets/webull-token-prod")
    monkeypatch.setattr(webull_io, "hydrate_token_from_secret",
                        lambda *a, **k: {"configured": True, "hydrated": True})
    return built, verified


def test_prod_builds_clients_to_reconcile_inside_the_margin(monkeypatch):
    built, verified = _prod_build(monkeypatch, _durable())
    trade, data = webull_io.build_clients()
    assert built["endpoint"] == ("th", webull_io.PROD_ENDPOINT) and len(verified) == 1
    assert webull_io.clients_endpoint(trade, data) == webull_io.PROD_ENDPOINT


def test_prod_still_refuses_to_build_when_the_token_cannot_sign(monkeypatch):
    for bad in (_durable(status="EXPIRED"), _durable(token_storage="EPHEMERAL"),
                _durable(margin_only=False), _durable(secret_configured=False)):
        webull_io.reset_clients()
        _prod_build(monkeypatch, bad)
        with pytest.raises(webull_io.WebullConfigError, match="hydrated token"):
            webull_io.build_clients()
