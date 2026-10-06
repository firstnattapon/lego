"""2026-10-05 17:06Z: a refused cancel stopped UAT for 3.5 hours.

Run 9f8ec809... (SELL 0.47 UBER, MARKET/DAY) stayed PENDING at the broker. At 300 s
order_recovery cancelled it once and the SDK raised ServerException(error_code=
OPENAPI_ORDER_CANNOT_OPERATE, http_status=417, "The current status cannot be
modified."). The handler read every exception as CANCEL_OUTCOME_UNKNOWN, the 120 s
grace ran out, mark_manual moved the intent to MANUAL_RECONCILIATION_REQUIRED (terminal
in the outbox, so never polled again) and set an operator halt. Slots 15-25 were
PASS_OPERATOR_HALT and every tick, including the first one, was logged at INFO.

These pin what the fix keeps true together: a refusal is recorded and held (the order
keeps being reconciled, the fence stays shut, nothing halts) for a bounded time; any
other cancel failure is still an unknown outcome that halts after the grace; and a halt
is no longer silent. The values are the incident's own.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from webull.core.exception.exceptions import ServerException

import execution_service as execution
import lego_outbox as outbox
import observability
import operator_halt
import order_recovery as recovery
import tick_runtime
import transition_audit as audit
import webull_io
from conftest import FAKE_DB
from test_continuous_recovery_v4 import NOW, existing_order, isolated  # noqa: F401

REQUEST_ID = "ce116722-99a6-4a9e-a49f-c47b2c7ec961"
HALT_AT = datetime(2026, 10, 5, 17, 9, 6, tzinfo=timezone.utc)


def refusal() -> ServerException:
    return ServerException("OPENAPI_ORDER_CANNOT_OPERATE",
                           "The current status cannot be modified.", 417, REQUEST_ID)


def raising(error):
    def cancel(run_id):
        raise error
    return cancel


def refused_order():
    intent, claim, detail, summary = existing_order()
    result = recovery.handle(intent, detail, summary, claim, raising(refusal()))
    return intent, claim, detail, summary, result


# ------------------------------------------------------------- classification
@pytest.mark.parametrize("code,expected", [
    ("OPENAPI_ORDER_CANNOT_OPERATE", True), (" openapi_order_cannot_operate ", True),
    ("OPENAPI_SYSTEM_ERROR", False), ("OPENAPI_PARAM_ERR", False),
    ("GATEWAY_TIMEOUT", False), ("", False), (None, False), (417, False)])
def test_only_the_cannot_operate_code_is_a_refusal(code, expected):
    assert recovery.cancel_refused(SimpleNamespace(error_code=code)) is expected


def test_exceptions_without_an_error_code_are_not_refusals():
    assert not recovery.cancel_refused(TimeoutError())
    assert not recovery.cancel_refused(webull_io.WebullConfigError("cancel response non-success"))


# ---------------------------------------------------------------- the refusal
def test_a_refused_cancel_is_recorded_held_and_never_retried():
    intent, claim, detail, summary = existing_order()
    calls = []

    def refuse(run_id):
        calls.append(run_id)
        raise refusal()

    result = recovery.handle(intent, detail, summary, claim, refuse)

    assert calls == [intent["run_id"]]
    assert result["status"] == "CANCEL_UNKNOWN" and result["cancel_attempt_count"] == 1
    assert result["cancel_last_error_code"] == recovery.CANCEL_REFUSED
    assert result["cancel_refused_at"] == NOW.isoformat()
    assert result["cancel_confirmation_deadline"] == (
        NOW + timedelta(seconds=recovery.REFUSED_HOLD_SECONDS)).isoformat()
    assert not result.get("needs_manual_check")
    assert not operator_halt.status("identity", "UBER").get("halted")
    scope = outbox.account_symbol_fence_key("identity", "UBER")
    assert FAKE_DB.reference(f"{outbox.DISPATCH_LOCK_PATH}/{scope}").get()["inflight_run_id"] == intent["run_id"]
    # The single mutation is spent: later ticks read, they never cancel again.
    again = recovery.handle(result, detail, summary, claim, lambda _: pytest.fail("second cancel"))
    assert not again.get("needs_manual_check") and len(calls) == 1
    # The refusal is part of the immutable audit trail.
    audit.replay("chain", intent["run_id"])
    events = FAKE_DB.reference(f"{audit.PATH}/chain/{intent['run_id']}").get().values()
    assert any(event["state"].get("cancel_refused_at") == NOW.isoformat() for event in events)


@pytest.mark.parametrize("age", [121, 900, recovery.REFUSED_HOLD_SECONDS - 1])
def test_a_refused_order_outlives_the_grace_and_stays_actionable(age):
    intent, *_, refused = refused_order()
    held = recovery.read_failed(refused, now=NOW + timedelta(seconds=age))
    assert not held.get("needs_manual_check") and held["status"] == "CANCEL_UNKNOWN"
    assert [item["run_id"] for item in outbox.list_actionable("chain")] == [intent["run_id"]]
    assert not operator_halt.status("identity", "UBER").get("halted")


def test_the_hold_is_bounded_and_ends_in_a_halt_with_its_own_reason():
    intent, *_, refused = refused_order()
    expired = recovery.read_failed(
        refused, now=NOW + timedelta(seconds=recovery.REFUSED_HOLD_SECONDS))
    assert expired["needs_manual_check"]
    assert expired["status"] == "MANUAL_RECONCILIATION_REQUIRED"
    assert expired["cancel_last_error_code"] == "CANCEL_REFUSED_HOLD_EXPIRED"
    assert operator_halt.status("identity", "UBER")["halted"]
    assert not execution._chain_fence_can_clear(expired)
    assert outbox.list_actionable("chain") == []


def test_a_terminal_answer_during_the_hold_needs_no_human():
    intent, claim, detail, summary, refused = refused_order()
    detail.update(status="FILLED", filled_quantity="2")
    result = recovery.handle(refused, detail, {"status": "FILLED", "filled_quantity": "2"},
                             claim, lambda _: pytest.fail("terminal order cancelled"))
    assert result["cancel_confirmed_at"] and not result.get("needs_manual_check")
    assert not operator_halt.status("identity", "UBER").get("halted")


def test_the_hold_survives_an_unreadable_order_detail(monkeypatch):
    """execution_service rewrites cancel_last_error_code when the detail read fails."""
    intent, claim, detail, summary, refused = refused_order()
    monkeypatch.setattr(execution, "fetch_order_detail",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("HTTP 504")))
    monkeypatch.setattr(webull_io, "find_recent_order_by_client_id", lambda *a, **kw: None)

    result = execution._dispatch_or_reconcile_one(
        None, None, SimpleNamespace(symbol="UBER"), refused, claim)

    stored = outbox.read_intent("chain", intent["run_id"])
    assert result["status"] == "CANCEL_UNKNOWN" and not result.get("needs_manual_check")
    assert stored["cancel_last_error_code"] == "CANCEL_DETAIL_UNAVAILABLE"
    assert stored["cancel_refused_at"] == NOW.isoformat()
    assert not recovery.read_failed(stored, now=NOW + timedelta(seconds=121)).get("needs_manual_check")
    expired = recovery.read_failed(
        stored, now=NOW + timedelta(seconds=recovery.REFUSED_HOLD_SECONDS))
    assert expired["cancel_last_error_code"] == "CANCEL_REFUSED_HOLD_EXPIRED"


# ------------------------------------------------- everything else still halts
@pytest.mark.parametrize("error,code", [
    (ServerException("OPENAPI_SYSTEM_ERROR", "System error.", 417), "CANCEL_OUTCOME_UNKNOWN"),
    (ServerException("OPENAPI_PARAM_ERR", "bad request", 417), "CANCEL_OUTCOME_UNKNOWN"),
    (ServerException("GATEWAY_TIMEOUT", "", 504), "CANCEL_OUTCOME_UNKNOWN"),
    (ServerException(None, "no code", 417), "CANCEL_OUTCOME_UNKNOWN"),
    (TimeoutError(), "CANCEL_OUTCOME_UNKNOWN"),
    (webull_io.WebullConfigError("cancel response non-success; reconcile required"),
     "CANCEL_OUTCOME_UNKNOWN"),
    (tick_runtime.TickDeadlineExceeded(), "TICK_DEFERRED"),
])
def test_every_other_cancel_failure_is_still_an_unknown_outcome(error, code):
    intent, claim, detail, summary = existing_order()
    unknown = recovery.handle(intent, detail, summary, claim, raising(error))
    assert unknown["status"] == "CANCEL_UNKNOWN" and unknown["cancel_last_error_code"] == code
    assert "cancel_refused_at" not in unknown
    halted = recovery.read_failed(unknown, now=NOW + timedelta(seconds=121))
    assert halted["needs_manual_check"]
    assert halted["cancel_last_error_code"] == "CANCEL_CONFIRMATION_OVERDUE"
    assert operator_halt.status("identity", "UBER")["halted"]


def test_the_cancel_still_spends_its_one_mutation_before_the_call():
    intent, claim, detail, summary = existing_order()
    started = recovery.begin_cancel(intent, claim, recovery.RecoveryPolicy("cancel"), NOW)
    assert started and started["cancel_attempt_count"] == 1
    # A crash after the witness leaves the original grace: no refusal was ever seen.
    assert "cancel_refused_at" not in started
    assert recovery.read_failed(started, now=NOW + timedelta(seconds=121))["needs_manual_check"]


# -------------------------------------------------------------- the halt pages
def _paused_tick(capsys, offset, halt_since=HALT_AT.isoformat()):
    body = {"recovery": {"dispatch_blocked": True, "reconciliation_paused": True,
                         "halt_since": halt_since, "results": []}}
    observability.emit_tick(body, 200, now=HALT_AT + timedelta(seconds=offset))
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("offset,severity", [
    (1, "ERROR"), (60, "ERROR"), (179, "ERROR"), (180, "INFO"), (1000, "INFO"),
    (3599, "INFO"), (3600, "ERROR"), (3779, "ERROR"), (3780, "INFO"),
    (7230, "ERROR"), (86400 + 30, "ERROR"), (86400 + 600, "INFO")])
def test_a_paused_halt_pages_when_it_begins_and_every_hour_after(capsys, offset, severity):
    event = _paused_tick(capsys, offset)
    assert event["business_status"] == "MANUAL_RECONCILIATION_REQUIRED"
    assert event["reconciliation_paused"] and event["severity"] == severity


@pytest.mark.parametrize("halt_since", [None, "", "garbage", "2026-10-05T17:09:06", 17.5,
                                        "2026-10-06T17:09:06+00:00"])
def test_an_unreadable_or_future_halt_time_pages_instead_of_going_quiet(capsys, halt_since):
    event = _paused_tick(capsys, 900, halt_since=halt_since)
    assert event["severity"] == "ERROR" and event["business_status"] == "MANUAL_RECONCILIATION_REQUIRED"


def test_the_transition_tick_of_the_incident_would_have_paged(capsys):
    """17:09:07 was INFO: the dispatch phase of the same tick already reported paused."""
    body = {"recovery": {"results": [{"run_id": "9f8ec809", "needs_manual_check": True,
                                      "status": "MANUAL_RECONCILIATION_REQUIRED"}]},
            "dispatch": {"dispatch_blocked": True, "reconciliation_paused": True,
                         "halt_since": HALT_AT.isoformat(), "results": []}}
    observability.emit_tick(body, 200, now=HALT_AT + timedelta(seconds=1.3))
    event = json.loads(capsys.readouterr().out)
    assert event["severity"] == "ERROR" and event["halt_since"] == HALT_AT.isoformat()


def test_a_held_order_shows_why_in_the_tick_log(capsys):
    body = {"recovery": {"results": [{
        "run_id": "9f8ec809", "status": "CANCEL_UNKNOWN", "broker_status": "PENDING",
        "cancel_attempt_count": 1, "cancel_last_error_code": recovery.CANCEL_REFUSED,
        "cancel_refused_at": "2026-10-05T17:06:07+00:00"}]}}
    observability.emit_tick(body, 200, now=HALT_AT)
    event = json.loads(capsys.readouterr().out)
    assert event["business_status"] == "WAITING_RECONCILIATION" and event["severity"] == "WARNING"
    held = event["execution"][0]
    assert held["cancel_last_error_code"] == "CANCEL_REFUSED_NOT_OPERABLE"
    assert held["cancel_refused_at"] == "2026-10-05T17:06:07+00:00"
