"""Incident regressions: documented fees, terminal audit, deadline and health."""
import copy
import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

import main
import execution_service
import lego_outbox
import tick_runtime
import webull_io
from conftest import FAKE_DB
from config import load_runtime_config
from lego_orders import summarize_order_result, UAT, PROD
from lego_preflight import auto_submit_preflight
from observability import business_status


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    FAKE_DB.store.clear()
    webull_io.reset_clients()
    execution_service.configure(main)
    monkeypatch.setenv("WEBULL_ENV", "UAT")
    yield
    webull_io.reset_clients()


def detail(commission="1.00", fee="0.20"):
    return {"orders": [{"status": "FILLED", "filled_quantity": "7.000000",
                        "filled_price": "332.470000",
                        "commission": {"actual_commission": commission, "receivable_commission": "99"},
                        "fees": [{"type": "GST", "actual_value": fee, "receivable_value": "99"}]}]}


@pytest.mark.parametrize("commission,fee,expected", [("1.00", "0.20", Decimal("1.20")),
                                                     ("0", "0", Decimal("0")),
                                                     ("0.00000001", "0.00000002", Decimal("0.00000003"))])
def test_nested_actual_fees_ignore_receivables(commission, fee, expected):
    parsed = summarize_order_result({}, detail(commission, fee))
    assert Decimal(parsed["filled_fee"]) == expected
    assert parsed["filled_quantity"] == "7.000000"


@pytest.mark.parametrize("value", [None, "", "NaN", "Infinity", "-1", True, {}, []])
def test_incomplete_or_invalid_breakdown_is_unknown_not_zero(value):
    assert "filled_fee" not in summarize_order_result({}, detail(fee=value))


def test_missing_fee_array_and_duplicate_fee_types_are_not_known_totals():
    payload = detail()
    payload["orders"][0].pop("fees")
    assert "filled_fee" not in summarize_order_result({}, payload)
    payload = detail()
    payload["orders"][0]["fees"] *= 2
    assert "filled_fee" not in summarize_order_result({}, payload)


def test_late_nested_fee_closes_fence_without_double_cashflow_or_quantity():
    intent = {"run_id": "filled-run", "chain_key": "chain", "side": "BUY",
              "status": "AWAITING_BROKER_FEE", "cashflow_finalized": True,
              "created_at": "2026-09-11T14:00:00Z"}
    lego_outbox.put_intent("chain", "filled-run", intent)
    pending = execution_service._finish_with_realized(
        None, None, intent, summarize_order_result({}, detail(fee=None)))
    assert pending["status"] == "AWAITING_BROKER_FEE"
    assert pending["fee_overdue"] is True
    assert not execution_service._chain_fence_can_clear(pending)
    latest = lego_outbox.read_intent("chain", "filled-run")
    complete = execution_service._finish_with_realized(
        None, None, latest, summarize_order_result({}, detail()))
    assert complete["status"] == "FILLED"
    latest = lego_outbox.read_intent("chain", "filled-run")
    assert execution_service._chain_fence_can_clear(latest)
    assert latest["fee_overdue"] is False
    root = "webull_lego_broker_cashflow/chain/events/filled-run"
    first = FAKE_DB.reference(root).get()
    assert Decimal(first["cash_cumulative"]) == Decimal("-2328.49")
    for _ in range(3):
        execution_service._finish_with_realized(
            None, None, latest, summarize_order_result({}, detail()))
    after = FAKE_DB.reference(root).get()
    assert after["cumulative_quantity"] == first["cumulative_quantity"]
    assert after["cash_cumulative"] == first["cash_cumulative"]
    assert FAKE_DB.reference("webull_lego_warnings/broker_fee_overdue").get()


def test_suppressed_and_expired_intents_have_repairable_final_audit():
    for name in ("suppressed", "expired"):
        lego_outbox.put_intent("chain", name, {"created_at": "2026-01-01T00:00:00Z",
                                              "expires_at": "2026-01-01T00:15:00Z"})
    execution_service._stop("chain", "suppressed", "SUPPRESSED_STATE_CHANGED")
    lego_outbox.expire_unsent_before("chain", datetime.now(timezone.utc))
    execution_service._repair_pending_audits("chain")
    for name, status in (("suppressed", "SUPPRESSED_STATE_CHANGED"), ("expired", "EXPIRED_UNSENT")):
        assert FAKE_DB.reference("webull_lego_order_audit/" + name).get()["status"] == status
        assert lego_outbox.read_intent("chain", name)["audit_pending"] is False


def test_audit_write_failure_retains_repair_marker(monkeypatch):
    lego_outbox.put_intent("chain", "r", {})
    original = execution_service.update_order_audit
    monkeypatch.setattr(execution_service, "update_order_audit", lambda *a: (_ for _ in ()).throw(RuntimeError("offline")))
    execution_service._stop("chain", "r", "NOT_PLACED")
    assert lego_outbox.read_intent("chain", "r")["audit_pending"] is True
    monkeypatch.setattr(execution_service, "update_order_audit", original)
    assert execution_service._repair_pending_audits("chain") == 1


def test_late_audit_mirror_cannot_overwrite_or_acknowledge_newer_terminal():
    from lego_state import update_order_audit, write_order_audit
    old = lego_outbox.put_intent("c", "r", {})
    new = lego_outbox.update_intent("c", "r", {"status": "NOT_PLACED"})
    update_order_audit("r", new)
    update_order_audit("r", old)
    write_order_audit("r", {"status": "PENDING_DISPATCH", "audit_revision": old["audit_revision"]})
    assert not lego_outbox.acknowledge_audit("c", "r", old["audit_revision"])
    assert lego_outbox.read_intent("c", "r")["audit_pending"] is True
    assert FAKE_DB.reference("webull_lego_order_audit/r").get()["status"] == "NOT_PLACED"
    assert lego_outbox.acknowledge_audit("c", "r", new["audit_revision"])


def test_warning_log_is_deduplicated_but_durable_count_keeps_increasing(caplog):
    for _ in range(3):
        execution_service._record_warning("same", "same warning", {"run_id": "r"})
    assert len([r for r in caplog.records if "same warning" in r.message]) == 1
    assert FAKE_DB.reference("webull_lego_warnings/same").get()["count"] == 3
    execution_service._record_warning("same", "same warning", {"run_id": "new"})
    assert len([r for r in caplog.records if "same warning" in r.message]) == 2


def test_retry_does_not_sleep_past_remaining_budget(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(tick_runtime.time, "monotonic", lambda: clock[0])
    sleeps = []
    monkeypatch.setattr(webull_io.time, "sleep", sleeps.append)
    calls = []
    class Unavailable(RuntimeError):
        http_status = 503
    def slow_read():
        calls.append(True)
        clock[0] += 4
        raise Unavailable()
    with tick_runtime.tick_scope("retry", seconds=7):
        with pytest.raises(tick_runtime.TickDeadlineExceeded):
            webull_io._retry_transient(slow_read)
    assert len(calls) == 1 and sleeps == []
    assert tick_runtime.remaining() is None and tick_runtime.correlation_id() is None


def test_deadline_deferral_preserves_filled_status_and_reconcile_attempts():
    intent = lego_outbox.put_intent("c", "r", {"status": "AWAITING_BROKER_FEE", "reconcile_attempts": 4})
    result = execution_service._persist_reconcile_failure(intent, tick_runtime.TickDeadlineExceeded())
    assert result["status"] == "AWAITING_BROKER_FEE"
    assert lego_outbox.read_intent("c", "r")["reconcile_attempts"] == 4


class Response:
    status_code = 200
    def __init__(self, payload): self.payload = payload
    def json(self): return self.payload


class Request:
    def __init__(self, action="/openapi/config"): self.action = action
    def get_action_name(self): return self.action
    def set_read_timeout(self, value): self.read = value
    def set_connect_timeout(self, value): self.connect = value


def test_sdk_boundary_bounds_io_and_records_explicit_disabled_mode():
    class Base:
        def get_response(self, request): return Response({"token_check_enabled": False})
    api = webull_io._bounded_api_class(Base)()
    request = Request()
    with tick_runtime.tick_scope("io", seconds=5):
        api.get_response(request)
    assert 0 < request.read <= 2 and 0 < request.connect <= 2
    assert api._lego_token_check_enabled is False


def test_unknown_auth_flag_cannot_disable_authentication():
    class Base:
        def get_response(self, request): return Response({"unrelated": True})
    with pytest.raises(webull_io.WebullConfigError):
        webull_io._bounded_api_class(Base)().get_response(Request())


def test_disabled_token_health_is_scoped_to_verified_profile_and_ttl(monkeypatch):
    monkeypatch.setattr(webull_io, "read_local_token", lambda: None)
    webull_io._AUTH_PROFILE = (webull_io._client_identity(), webull_io.time.monotonic(), False)
    assert webull_io.token_health()["ok"] is True
    verified_at = webull_io._AUTH_PROFILE[1]
    with monkeypatch.context() as clock_patch:
        clock_patch.setattr(webull_io.time, "monotonic", lambda: verified_at + webull_io._client_cache_ttl() + 1)
        assert webull_io.token_health()["ok"] is False
    monkeypatch.setenv("WEBULL_APP_SECRET", "rotated-app-secret")
    assert webull_io.token_health()["ok"] is False


def setup_tick(monkeypatch, recovery=None, dispatch_error=False):
    env = {"LEGO_SYMBOL": "AAPL", "LEGO_FIX_C": "3000", "LEGO_DIFF": "25", "WEBULL_ENV": "UAT",
           "WEBULL_ACCOUNT_ID": "test", "LEGO_MODE": "trade", "LEGO_ACTIVE": "true",
           "LEGO_CANDIDATE_HASH": "candidate"}
    env["LEGO_RELEASE_AUTHORIZATION"] = load_runtime_config(env).deployment.expected_release_binding
    runtime = load_runtime_config(env)
    monkeypatch.setattr(main, "load_runtime_config", lambda: runtime)
    monkeypatch.setattr(execution_service, "_init_firebase", lambda: None)
    monkeypatch.setattr(main, "runtime_identity_fingerprint", lambda: "identity")
    calls = []
    def worker(*args, **kwargs):
        calls.append(kwargs["limit"])
        if len(calls) == 2 and dispatch_error: raise RuntimeError("dispatch unavailable")
        return recovery or {"results": [], "processed": 0}
    monkeypatch.setattr(execution_service, "_run_order_worker", worker)
    monkeypatch.setattr(main.decision_service, "run_decision", lambda *a, **k: ({"pipeline_status": "ROW_COMMITTED", "run_id": "new"}, 200))
    monkeypatch.setattr(main, "archive_terminal_records", lambda **k: {})
    return calls


def test_dispatch_error_is_not_http_or_business_success(monkeypatch, capsys):
    setup_tick(monkeypatch, dispatch_error=True)
    body, code = main.lego_tick(None)
    assert code == 503 and body["business_status"] == "ERROR"
    event = json.loads(capsys.readouterr().out)
    assert event["event"] == "lego_tick_completed"
    assert event["correlation_id"] == body["correlation_id"]
    assert event["severity"] == "ERROR"


def test_one_fee_query_per_tick_with_visible_business_wait(monkeypatch, capsys):
    calls = setup_tick(monkeypatch, {"results": [{"run_id": "old", "status": "AWAITING_BROKER_FEE"}]})
    body, code = main.lego_tick(None)
    assert calls == [3] and code == 200
    assert body["business_status"] == "WAITING_BROKER_FEE"
    assert json.loads(capsys.readouterr().out)["execution"][0]["run_id"] == "old"


def test_config_error_also_has_a_correlated_result_log(monkeypatch, capsys):
    monkeypatch.setattr(main, "load_runtime_config", lambda: (_ for _ in ()).throw(ValueError("invalid config")))
    body, code = main.lego_tick(None)
    assert code == 500
    assert json.loads(capsys.readouterr().out)["correlation_id"] == body["correlation_id"]


def test_operator_audit_repair_is_dry_run_and_identity_bound(monkeypatch):
    import ops
    setup_tick(monkeypatch)
    runtime = main.load_runtime_config()
    monkeypatch.setattr(ops, "load_runtime_config", lambda: runtime)
    cfg = main.Config(runtime.operator.symbol, runtime.operator.principal_usd,
                      runtime.operator.diff_usd, runtime.operator.dna_bundle.dna_code,
                      "shannon_demon_lego_v2")
    ck = main.chain_key(cfg)
    lego_outbox.put_intent(ck, "old", {"status": "SUPPRESSED_STATE_CHANGED",
                                       "runtime_identity_fingerprint": "identity"})
    audit_ref = FAKE_DB.reference("webull_lego_order_audit/old")
    audit_ref.set({"status": "PENDING_DISPATCH", "placed_at": "2026-01-01T00:00:00Z"})
    before = copy.deepcopy(FAKE_DB.store)
    args = SimpleNamespace(run_id="old", apply=False)
    assert ops.repair_audit_command(args)["dry_run"] is True
    assert FAKE_DB.store == before
    args.apply = True
    assert ops.repair_audit_command(args)["audit_pending"] is False
    assert audit_ref.get()["status"] == "SUPPRESSED_STATE_CHANGED"
    assert audit_ref.get().get("placed_at") is None
    assert lego_outbox.read_intent(ck, "old")["status"] == "SUPPRESSED_STATE_CHANGED"
    monkeypatch.setattr(main, "runtime_identity_fingerprint", lambda: "different")
    with pytest.raises(ValueError, match="identity differs"):
        ops.repair_audit_command(args)


def test_operator_status_reads_actual_account_symbol_fence(monkeypatch):
    import ops
    setup_tick(monkeypatch)
    monkeypatch.setattr(ops, "load_runtime_config", main.load_runtime_config)
    scope = main.account_symbol_fence_key("identity", "AAPL")
    FAKE_DB.reference(f"{lego_outbox.DISPATCH_LOCK_PATH}/{scope}").set({
        "inflight_run_id": "pending", "inflight_chain_key": "old-chain"})
    lego_outbox.put_intent("old-chain", "pending", {"status": "AWAITING_BROKER_FEE",
                                                   "broker_fee_status": "PENDING"})
    status = ops.status_command(None)
    assert status["active_intent_id"] == "pending"
    assert status["execution_status"] == "AWAITING_BROKER_FEE"
    assert status["broker_fee_status"] == "PENDING"


@pytest.mark.parametrize("environment,binding,ok", [(PROD, None, False), (PROD, False, False),
                                                    (PROD, True, True), (UAT, False, False), (UAT, None, True)])
def test_preflight_requires_explicit_production_release_binding(environment, binding, ok):
    row = {"สถานะ": "READY_BUY", "DNA step": 1, "_meta": {"quantity": 1, "step": 1}}
    report = auto_submit_preflight(auto_submit=True, environment=environment, row=row, row_durable=True,
                                   slot=SimpleNamespace(market_ordinal=1), token={"ok": True}, dna_remaining=10,
                                   release_authorized=binding)
    check = next(c for c in report["checks"] if c["id"] == "environment_uat")
    assert check["ok"] is ok
    assert report["ok"] is ok
