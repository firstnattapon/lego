"""Real dispatch gates with broker doubles; crash/lease/session regressions."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json

import pytest

from conftest import FAKE_DB
import execution_service as execution
from execution_limits import ExecutionLimits, ExecutionLimitError, reserve_attempt
from lego_outbox import DISPATCH_LOCK_PATH
from observability import request_trace, emit_tick
from test_dispatch_overshoot import dispatch_fixture, CFG, NOW, isolate


VALUES = ("200", "5000", "2", "2030-01-01T00:00:00Z")


@pytest.mark.parametrize("values", [("", "", "", ""), ("NaN", *VALUES[1:]),
    ("Infinity", *VALUES[1:]), ("0", *VALUES[1:]),
    ("200", "5000", "1.5", VALUES[3]), ("200", "5000", "0", VALUES[3]),
    ("200", "5000", "2", "2030-01-01")])
def test_invalid_limits_fail_closed(values):
    with pytest.raises(ExecutionLimitError):
        ExecutionLimits.parse(values)


@pytest.mark.parametrize("values,price,preview_count", [
    (("", "", "", ""), 27.6843, 0),
    (("180", "10000", "2", VALUES[3]), 27.6843, 0),
    (("200", "4989", "2", VALUES[3]), 27.6843, 0),
    (("200", "4991", "2", VALUES[3]), 27.69, 1),
    (("200", "5000", "2", "2026-09-21T14:59:59Z"), 27.6843, 0),
])
def test_limits_block_before_place_and_recheck_post_preview(monkeypatch, values, price, preview_count):
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch, final_price=price)
    deployment = replace(runtime.deployment, execution_limits=values)
    deployment = replace(deployment, release_authorization=deployment.expected_release_binding)
    runtime = replace(runtime, deployment=deployment)
    result = execution._dispatch_or_reconcile_one(client, None, CFG, intent, claim, runtime)
    assert result["execution_limit_blocked"] is True
    assert len(client.order_v3.preview_order.calls) == preview_count
    assert not client.order_v3.place_order.calls


def test_changed_limits_or_symbol_invalidates_authorization(monkeypatch):
    runtime, *_ = dispatch_fixture(monkeypatch)
    assert runtime.deployment.release_is_authorized
    assert not replace(runtime.deployment, execution_limits=VALUES).release_is_authorized
    assert not replace(runtime.deployment, trading_symbol="OTHER").release_is_authorized


def test_exact_decimal_boundary_and_window_expiry():
    limits = ExecutionLimits.parse(VALUES)
    limits.check("200", "25", now=NOW)
    with pytest.raises(ExecutionLimitError):
        limits.check("200", "25.0000000000001", now=NOW)
    with pytest.raises(ExecutionLimitError):
        limits.check("1", "1", now=limits.end)


def test_reservations_survive_restart_and_new_limits_do_not_reset_count():
    limits = ExecutionLimits.parse(VALUES)
    claim = {"owner": "worker", "claim_token": "token"}
    ref = FAKE_DB.reference(f"{DISPATCH_LOCK_PATH}/scope")
    ref.set({**claim, "inflight_run_id": "one", "lease_until": (NOW + timedelta(seconds=120)).isoformat()})
    key = str(limits.end.timestamp())
    assert reserve_attempt("scope", claim, "one", limits, key, now=NOW)["reservation_count"] == 1
    assert reserve_attempt("scope", claim, "one", limits, key, now=NOW)["reservation_count"] == 1
    ref.update({"inflight_run_id": "two", "owner": "restarted"})
    claim["owner"] = "restarted"
    assert reserve_attempt("scope", claim, "two", limits, key, now=NOW)["reservation_count"] == 2
    ref.update({"inflight_run_id": "three"})
    with pytest.raises(ExecutionLimitError):
        reserve_attempt("scope", claim, "three", replace(limits, notional=limits.notional * 2), key, now=NOW)
    assert ref.get()["execution_session"]["count"] == 2


def test_expired_owner_cannot_reuse_existing_reservation():
    limits = ExecutionLimits.parse(VALUES)
    claim = {"owner": "worker", "claim_token": "token"}
    ref = FAKE_DB.reference(f"{DISPATCH_LOCK_PATH}/scope")
    ref.set({**claim, "inflight_run_id": "one", "lease_until": (NOW + timedelta(seconds=1)).isoformat()})
    key = str(limits.end.timestamp())
    reserve_attempt("scope", claim, "one", limits, key, now=NOW)
    with pytest.raises(ExecutionLimitError):
        reserve_attempt("scope", claim, "one", limits, key, now=NOW + timedelta(seconds=2))


def test_missing_source_row_and_missing_limits_cannot_disable_recovery(monkeypatch):
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch)
    runtime = replace(runtime, deployment=replace(runtime.deployment, execution_limits=("",) * 4))
    intent.update(status="PLACING_UNKNOWN", place_attempted=True)
    FAKE_DB.reference(f"webull_lego_rows/{intent['run_id']}").delete()
    monkeypatch.setattr(execution, "fetch_order_detail", lambda *_: {
        "client_order_id": intent["run_id"], "symbol": CFG.symbol, "status": "SUBMITTED"})
    seen = []
    monkeypatch.setattr(execution, "_finish_with_realized", lambda *args: seen.append(args) or {"status": "SUBMITTED"})
    assert execution._dispatch_or_reconcile_one(client, None, CFG, intent, claim, runtime)["status"] == "SUBMITTED"
    assert seen and not client.order_v3.place_order.calls


def test_window_expiring_during_attempt_marker_cannot_place(monkeypatch):
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch)
    values = ("200", "10000", "2", (NOW + timedelta(seconds=1)).isoformat())
    profile = replace(runtime.deployment, execution_limits=values)
    runtime = replace(runtime, deployment=replace(profile, release_authorization=profile.expected_release_binding))
    begin = execution.begin_place_attempt
    class Later(datetime):
        @classmethod
        def now(cls, tz=None): return NOW + timedelta(seconds=2)
    def delayed(*args):
        result = begin(*args)
        monkeypatch.setattr(execution, "datetime", Later)
        return result
    monkeypatch.setattr(execution, "begin_place_attempt", delayed)
    result = execution._dispatch_or_reconcile_one(client, None, CFG, intent, claim, runtime)
    assert result["status"] == "RECONCILE_ABANDONED" and result["needs_manual_check"]
    assert not client.order_v3.place_order.calls


def test_trace_maps_cloud_request_and_rejects_untrusted_text(monkeypatch, capsys):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "demo-lego-firebase")
    request = SimpleNamespace(headers={"X-Cloud-Trace-Context": "a" * 32 + "/15;o=1"})
    emit_tick({"pipeline_status": "TICK_OK"}, 200, request=request)
    event = json.loads(capsys.readouterr().out)
    assert event["logging.googleapis.com/trace"].endswith("a" * 32)
    assert event["logging.googleapis.com/spanId"] == "000000000000000f"
    for value in ("secret", "a" * 32 + "/15;o=1\nsecret", "a" * 32 + "/18446744073709551616"):
        assert request_trace(SimpleNamespace(headers={"X-Cloud-Trace-Context": value})) == {}


@pytest.mark.parametrize("kind", ["FEE_OVERDUE", "MANUAL_RECONCILIATION_REQUIRED", "EXECUTION_LIMIT_BLOCKED", "RECONCILIATION_OVERDUE"])
def test_new_actionable_alert_routes_are_bounded(monkeypatch, kind):
    import alerting
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "test-account")
    monkeypatch.setenv("LEGO_SYMBOL", "PFE")
    calls = []
    class Response:
        status_code = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(alerting.requests, "post", lambda *a, **k: calls.append(k) or Response())
    assert alerting.notify_tick({"business_status": kind})
    assert not alerting.notify_tick({"business_status": kind})
    assert len(calls) == 1 and calls[0]["timeout"] == (1, 2)
    assert calls[0]["json"]["event"] == kind


def test_successful_but_stuck_submitted_order_alerts_without_releasing_fence(monkeypatch):
    from observability import business_status
    from lego_outbox import put_intent, read_intent
    intent = {"run_id": "stuck", "chain_key": "chain", "side": "BUY",
              "status": "SUBMITTED", "place_attempted": True,
              "placed_at": (NOW - timedelta(minutes=16)).isoformat()}
    put_intent("chain", "stuck", intent)
    result = execution._finish_with_realized(None, CFG, intent, {"status": "SUBMITTED", "filled_quantity": 0})
    assert result["status"] == "SUBMITTED" and result["reconciliation_overdue"]
    assert result["reconciliation_age_seconds"] == 960
    assert not execution._chain_fence_can_clear(read_intent("chain", "stuck"))
    assert business_status({"recovery": {"results": [result]}}, 200) == "RECONCILIATION_OVERDUE"
