"""Regression evidence for durable auth backoff and rejected order diagnostics."""
import json
from types import SimpleNamespace

import pytest

import auth_circuit
import execution_service
import main
import tick_runtime
import webull_io
from conftest import FAKE_DB
from lego_orders import summarize_order_result
from lego_outbox import put_intent, read_intent
from test_continuous_runtime import Request, Response, setup_tick


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    FAKE_DB.store.clear()
    webull_io.reset_clients()
    monkeypatch.setenv("WEBULL_ENV", "UAT")
    monkeypatch.setattr(webull_io, "_HYDRATED_TOKEN", None)
    yield
    FAKE_DB.store.clear()
    webull_io.reset_clients()


class Unauthorized(RuntimeError):
    http_status = 401
    error_code = "UNAUTHORIZED"


def test_330_ticks_back_off_across_client_resets_and_resume(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(auth_circuit.time, "time", lambda: clock[0])
    calls = []
    failing = [True]
    class Base:
        def get_response(self, request):
            calls.append(clock[0])
            if failing[0]:
                raise Unauthorized("do not persist raw credentials")
            return Response({})
    for n in range(330):
        clock[0] = 1000 + n * 60
        webull_io.reset_clients()  # cold starts cannot erase the pause
        with tick_runtime.tick_scope(str(n)):
            try:
                webull_io._bounded_api_class(Base)().get_response(Request("/trading/accounts/list"))
            except (Unauthorized, auth_circuit.AuthCircuitOpen):
                pass
    assert len(calls) < 20
    warnings = FAKE_DB.reference("webull_lego_warnings").get()
    assert len(warnings) == 1
    state = auth_circuit.status(webull_io.auth_circuit_key())
    assert state["count"] == len(calls) and state["active"]
    assert not FAKE_DB.reference("webull_lego_errors").get()
    clock[0] = state["retry_after"]
    failing[0] = False
    with tick_runtime.tick_scope("recovered"):
        webull_io._bounded_api_class(Base)().get_response(Request("/trading/accounts/list"))
    assert not auth_circuit.status(webull_io.auth_circuit_key())["active"]


def test_probe_is_exclusive_and_old_success_cannot_clear_new_failure(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(auth_circuit.time, "time", lambda: clock[0])
    state = auth_circuit.failed("k")
    clock[0] = state["retry_after"]
    with tick_runtime.tick_scope("first"):
        auth_circuit.guard("k")
    with tick_runtime.tick_scope("other"):
        with pytest.raises(auth_circuit.AuthCircuitOpen):
            auth_circuit.guard("k")
        auth_circuit.succeeded("k")
    assert auth_circuit.status("k")["active"]
    auth_circuit.failed("k")
    with tick_runtime.tick_scope("first"):
        auth_circuit.succeeded("k")
    assert auth_circuit.status("k")["active"]


def test_auth_does_not_spend_order_reconciliation_budget_or_release_fence():
    intent = put_intent("chain", "run", {
        "status": "AWAITING_BROKER_FEE", "reconcile_attempts": 19,
        "place_attempted": True, "cashflow_finalized": True})
    for exc in (Unauthorized(), auth_circuit.AuthCircuitOpen({})):
        result = execution_service._persist_reconcile_failure(intent, exc)
        assert result["status"] == "AWAITING_BROKER_FEE"
        assert result["deferred_reason"] == "auth_backoff"
    assert read_intent("chain", "run")["reconcile_attempts"] == 19


def test_tick_acknowledges_pause_without_recovery_or_place(monkeypatch):
    setup_tick(monkeypatch)
    auth_circuit.failed(webull_io.auth_circuit_key())
    monkeypatch.setattr(execution_service, "_run_order_worker",
                        lambda *a, **k: pytest.fail("no broker work while paused"))
    body, code = main.lego_tick(None)
    assert code == 200
    assert body["business_status"] == body["pipeline_status"] == "AUTH_BACKOFF"


def test_market_data_403_is_not_an_auth_outage():
    assert not webull_io.is_auth_failure(SimpleNamespace(http_status=403, error_code="FORBIDDEN"))
    assert not webull_io.is_auth_failure(RuntimeError("401 shares"))


def test_timeout_limits_use_available_budget():
    class Base:
        def get_response(self, request): return Response({})
    req = Request("/trading/accounts/list")
    with tick_runtime.tick_scope("timeout", seconds=35):
        webull_io._bounded_api_class(Base)().get_response(req)
    assert req.connect == 5 and req.read == 10


@pytest.mark.parametrize("reason_key", ["fail_reason", "failed_reason", "reject_reason",
                                       "rejected_reason", "third_error_msg", "error_message", "remark"])
def test_deep_rejects_preserve_parent_identity_and_redact(reason_key):
    detail = {"client_order_id": "run", "symbol": "AAPL", "data": {
        "orders": [{"items": [{"sub_orders": [{"status": "FAILED", "filled_quantity": "0",
                                                reason_key: "denied token=secret-value"}]}]}]}}
    assert webull_io.validate_order_detail_identity(detail, client_order_id="run", symbol="AAPL") == detail
    result = summarize_order_result({}, detail)
    assert result["status"] == "FAILED" and not result["realized"]
    assert result["filled_quantity"] == "0" and "secret-value" not in result["reject_reason"]
    intent = put_intent("chain", "run", {})
    execution_service._persist_summary(intent, result)
    assert "denied" in read_intent("chain", "run")["terminal_reason"]


@pytest.mark.parametrize("payload", [
    {"orders": [{"status": "FILLED"}, {"status": "FAILED"}]},
    {"client_order_id": "run", "data": {"client_order_id": "other"}},
    {"orders": [{"status": "FILLED"}], "data": {"status": "FAILED"}},
])
def test_ambiguous_order_data_cannot_be_booked(payload):
    with pytest.raises(ValueError):
        summarize_order_result({}, payload)


def test_nested_place_reject_is_diagnostic_not_fill_evidence():
    result = summarize_order_result({"data": {"status": "FAILED", "filled_quantity": "10",
                                             "error": {"error_message": "buying power"}}})
    assert result["reject_reason"] == "buying power"
    assert not result["realized"] and "filled_quantity" not in result


def test_token_secret_rotation_replaces_stale_local_cache_and_is_durable(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBULL_TOKEN_DIR", str(tmp_path))
    monkeypatch.setenv("WEBULL_TOKEN_SECRET", "projects/example/secrets/token")
    monkeypatch.setattr(webull_io, "token_dir_is_ephemeral", lambda: True)
    payload = [b"old-secret\n1893456000000\nNORMAL\n"]
    client = SimpleNamespace(access_secret_version=lambda **k: SimpleNamespace(
        payload=SimpleNamespace(data=payload[0])))
    webull_io.hydrate_token_from_secret(client)
    assert webull_io.token_health()["ok"]
    assert webull_io.token_health()["token_storage"] == "SECRET_MANAGER"
    payload[0] = b"rotated-secret\n1893456000000\nNORMAL\n"
    webull_io.hydrate_token_from_secret(client)
    assert webull_io.read_local_token()["token"] == "rotated-secret"
    assert "rotated-secret" not in json.dumps(webull_io.token_health())
    monkeypatch.setenv("WEBULL_TOKEN_SECRET", "projects/other/secrets/token")
    assert not webull_io.token_health()["ok"]


def test_expired_secret_cannot_overwrite_existing_token(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBULL_TOKEN_DIR", str(tmp_path))
    monkeypatch.setenv("WEBULL_TOKEN_SECRET", "projects/example/secrets/token")
    before = "valid-local\n1893456000000\nNORMAL\n"
    (tmp_path / "token.txt").write_text(before)
    client = SimpleNamespace(access_secret_version=lambda **k: SimpleNamespace(
        payload=SimpleNamespace(data=b"expired\n1000000000000\nNORMAL\n")))
    with pytest.raises(webull_io.WebullConfigError):
        webull_io.hydrate_token_from_secret(client)
    assert (tmp_path / "token.txt").read_text() == before


def test_auth_pause_does_not_override_invalid_http_request(monkeypatch):
    setup_tick(monkeypatch)
    auth_circuit.failed(webull_io.auth_circuit_key())
    request = SimpleNamespace(get_json=lambda **k: {"account_id": "other"})
    body, code = main.lego_tick(request)
    assert code == 400 and body["pipeline_status"] == "UNTRUSTED_REQUEST_OVERRIDE"


def test_unavailable_circuit_storage_blocks_broker_call(monkeypatch):
    class Base:
        def get_response(self, request): pytest.fail("storage failure must fail closed")
    monkeypatch.setattr(auth_circuit, "status", lambda key: (_ for _ in ()).throw(RuntimeError("database down")))
    with tick_runtime.tick_scope("storage"):
        with pytest.raises(RuntimeError, match="database down"):
            webull_io._bounded_api_class(Base)().get_response(Request())


def test_rotated_credentials_keep_account_cooldown(monkeypatch):
    key = webull_io.auth_circuit_key()
    auth_circuit.failed(key)
    monkeypatch.setenv("WEBULL_APP_SECRET", "replacement-credential")
    assert webull_io.auth_circuit_key() == key
    with tick_runtime.tick_scope("new-credential"):
        with pytest.raises(auth_circuit.AuthCircuitOpen):
            auth_circuit.guard(key)


def test_unusable_durable_token_opens_circuit_before_sdk_construction(monkeypatch):
    from test_webull_io import _install_fake_sdk
    _install_fake_sdk(monkeypatch)
    monkeypatch.setattr(webull_io, "hydrate_token_from_secret",
                        lambda: (_ for _ in ()).throw(webull_io.TokenUnavailableError("expired")))
    with tick_runtime.tick_scope("expired-token"):
        with pytest.raises(webull_io.TokenUnavailableError):
            webull_io.build_clients()
    assert auth_circuit.status(webull_io.auth_circuit_key())["active"]
    with tick_runtime.tick_scope("next-tick"):
        with pytest.raises(auth_circuit.AuthCircuitOpen):
            webull_io.build_clients()
