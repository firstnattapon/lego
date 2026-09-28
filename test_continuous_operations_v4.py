"""Release failures must remain visible and incomplete evidence never earns PASS."""
import copy
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import pytest

import main
import tick_runtime
import execution_limits as limits
import operational_health
from conftest import FAKE_DB
from config import load_runtime_config
from test_continuous_runtime import setup_tick
from tools.daily_execution_report import build
from tools.monitoring_config import build as monitoring


@pytest.fixture(autouse=True)
def isolate():
    FAKE_DB.store.clear()
    yield
    FAKE_DB.store.clear()


@pytest.mark.parametrize("boundary", ["recovery", "dispatch", "outer"])
def test_deadline_is_safe_deferral_at_each_boundary(monkeypatch, boundary):
    setup_tick(monkeypatch)
    calls = []
    def worker(*a, **kw):
        calls.append(1)
        if boundary == "recovery" or len(calls) == 2:
            raise tick_runtime.TickDeadlineExceeded()
        return {"results": []}
    if boundary == "outer":
        monkeypatch.setattr(main, "_run_tick", lambda *a: (_ for _ in ()).throw(tick_runtime.TickDeadlineExceeded()))
    else:
        monkeypatch.setattr(main.execution_service, "_run_order_worker", worker)
    body, code = main.lego_tick(None)
    assert code == 200 and body["pipeline_status"] == "TICK_DEFERRED"
    assert body["business_status"] == "TICK_DEFERRED"


def test_auth_backoff_never_masks_persistence_failure(monkeypatch):
    monkeypatch.setattr(main, "_run_tick", lambda *a: ({"pipeline_status": "RECOVERY_ERROR", "error": "persistence failed"}, 503))
    monkeypatch.setattr(main.auth_circuit, "status", lambda *a: {"active": True})
    body, code = main.lego_tick(None)
    assert code == 503 and body["business_status"] == "ERROR"


@pytest.mark.parametrize("moment,expected", [
    ("2026-03-06T15:00:00+00:00", "XNYS:2026-03-06"),
    ("2026-03-09T14:00:00+00:00", "XNYS:2026-03-09"),
    ("2026-11-27T17:59:00+00:00", "XNYS:2026-11-27"),
    ("2026-11-27T18:00:00+00:00", None),
    ("2026-12-25T15:00:00+00:00", None),
    ("2026-09-26T15:00:00+00:00", None),
])
def test_market_daily_key_obeys_dst_holidays_and_early_close(moment, expected):
    now = datetime.fromisoformat(moment)
    if expected:
        assert limits.session_key_for("market_day", None, now=now) == expected
    else:
        with pytest.raises(limits.ExecutionLimitError): limits.session_key_for("market_day", None, now=now)


def test_dna_horizon_skips_holiday_and_honors_early_close():
    end = operational_health.dna_end("2026-11-25T14:30:00Z", 41, 900, "test")
    assert datetime.fromisoformat(end) == datetime(2026, 11, 30, 14, 45, tzinfo=timezone.utc)
    runtime = load_runtime_config({"WEBULL_ACCOUNT_ID": "test", "LEGO_SYMBOL": "UBER", "LEGO_FIX_C": "10000", "LEGO_DIFF": "25"})
    report = operational_health.report(runtime, {"dna_steps_remaining": 39}, {}, now=datetime(2026, 11, 25, 15, tzinfo=timezone.utc))
    assert report["two_session_slots"] == 40 and report["dna_low"]


def test_phase_trace_inherits_witness_without_payload(capsys):
    with tick_runtime.tick_scope("trace", seconds=10):
        with tick_runtime.phase("place", witness="PLACING_UNKNOWN"):
            with tick_runtime.phase("sdk_place"): pass
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(events) == 2 and all(e["witness"] == "PLACING_UNKNOWN" for e in events)
    assert all(e["remaining_budget_ms"] > 0 and e["correlation_id"] == "trace" for e in events)


def test_monitoring_covers_business_errors_and_missing_ticks():
    resources = monitoring("lego-tick-uat", "projects/demo/notificationChannels/123")
    assert "severity>=ERROR" in resources["health-policy"]["conditions"][0]["conditionMatchedLog"]["filter"]
    assert resources["absence-policy"]["conditions"][0]["conditionAbsent"]["duration"] == "180s"
    with pytest.raises(ValueError): monitoring("invalid service", "unverified")


def daily_evidence():
    order = {"client_order_id": "r", "symbol": "UBER", "side": "BUY", "status": "CANCELLED",
             "filled_quantity": "1", "filled_price": "70", "filled_fee": "0.1"}
    broker = {"captured_at": "2026-09-25T21:00:00Z", "environment": "UAT", "account_fingerprint": "id",
              "orders_complete": True, "positions_complete": True, "positions": {"UBER": "1"},
              "cash": "929.9", "orders": [order], "external_cash_delta": "0", "external_position_delta": {}}
    broker["opening"] = {**broker, "captured_at": "2026-09-25T13:00:00Z", "positions": {}, "cash": "1000", "orders": []}
    intent = {**order, "run_id": "r", "place_attempted": True, "runtime_identity_fingerprint": "id", "cashflow_finalized": True}
    export = {"webull_lego_order_outbox": {"c": {"r": intent}},
              "webull_lego_broker_cashflow": {"c": {"events": {"r": {"side": "BUY", "cumulative_quantity": "1",
                  "cumulative_notional": "70", "actual_fees": "0.1", "cash_cumulative": "-70.1"}}}},
              "webull_lego_state": {"c": {"execution_cashflow": {"finalized_runs": {"r": {"filled_quantity": "1", "filled_price": "70"}}}}},
              "webull_lego_realized": {"c": {"applied_fills": {"r": {"side": "BUY", "quantity": "1", "average_price": "70", "fee": "0.1"}}}}}
    return export, broker


def test_daily_reconciliation_includes_cancelled_partial_fill_and_all_ledgers():
    export, broker = daily_evidence()
    report = build(export, broker)
    assert report["status"] == "PASS" and report["real_money_ready"] is False
    assert build(export)["status"] == "BLOCKED"
    broker.pop("opening")
    assert build(export, broker)["status"] == "BLOCKED"


@pytest.mark.parametrize("failure", ["missing_order", "missing_ledger", "cash", "quantity", "nonfinite", "wrong_account", "incomplete_scan"])
def test_daily_reconciliation_fails_closed(failure):
    export, broker = daily_evidence()
    if failure == "missing_order": broker["orders"] = []
    if failure == "missing_ledger": export.pop("webull_lego_realized")
    if failure == "cash": broker["cash"] = "999"
    if failure == "quantity": broker["positions"]["UBER"] = "2"
    if failure == "nonfinite": broker["orders"][0]["filled_fee"] = "NaN"
    if failure == "wrong_account": broker["account_fingerprint"] = "other"
    if failure == "incomplete_scan": broker["orders_complete"] = False
    assert build(export, broker)["status"] == "FAIL"


@pytest.mark.parametrize("fault", [None, "traffic", "retry", "retry_duration", "timeout", "candidate", "image", "prod_active"])
def test_captured_deployment_must_match_release(fault):
    from tools.verify_deployment import verify
    image, candidate = "registry/lego@sha256:" + "a" * 64, "b" * 64
    service = {"metadata": {"name": "lego-tick-uat"}, "spec": {"template": {
        "metadata": {"annotations": {"autoscaling.knative.dev/maxScale": "1"}},
        "spec": {"containerConcurrency": 1, "timeoutSeconds": 45, "containers": [
            {"image": image, "env": [{"name": "LEGO_CANDIDATE_HASH", "value": candidate}, {"name": "WEBULL_ENV", "value": "UAT"}]}]}}},
        "status": {"latestReadyRevisionName": "v4", "traffic": [{"revisionName": "v4", "percent": 100}], "url": "https://example.run.app"}}
    scheduler = {"schedule": "* * * * *", "retryConfig": {"retryCount": 0}, "httpTarget": {"uri": "https://example.run.app"}}
    spec = service["spec"]["template"]["spec"]
    if fault == "traffic": service["status"]["traffic"].append({"revisionName": "old", "percent": 0, "tag": "old"})
    if fault == "retry": scheduler["retryConfig"]["retryCount"] = 1
    if fault == "retry_duration": scheduler["retryConfig"]["maxRetryDuration"] = "60s"
    if fault == "timeout": spec["timeoutSeconds"] = 60
    if fault == "candidate": spec["containers"][0]["env"][0]["value"] = "other"
    if fault == "image": spec["containers"][0]["image"] = "registry/lego:latest"
    if fault == "prod_active": spec["containers"][0]["env"] += [{"name": "WEBULL_ENV", "value": "PROD"}, {"name": "LEGO_MODE", "value": "trade"}, {"name": "LEGO_ACTIVE", "value": "true"}]
    report = verify(service, scheduler, candidate=candidate, revision="v4", image=image)
    assert report["status"] == ("PASS" if fault is None else "FAIL")
