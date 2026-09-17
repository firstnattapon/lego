"""Account identity and incident diagnostics; all broker calls are test doubles."""
import io
import json
import logging
import sys
from types import SimpleNamespace

import pytest
from webull.core.exception.exceptions import ServerException

import observability
import webull_io
from test_webull_io import _install_fake_sdk


@pytest.mark.parametrize("payload", [
    [{"account_id": "actual-id", "account_number": "configured-id"}],
    [{"account_number": "configured-id"}],
    [{"account_id": 123}],
    {"error_code": "OPENAPI_SYSTEM_ERROR", "data": [{"account_id": "configured-id"}]},
    {"unrelated": [{"account_id": "configured-id"}]},
    [{"account_id": "configured-id"}, {"account_id": "configured-id"}],
    [{"account_id": "configured-id"}, None],
    [], None, True,
])
def test_unproven_account_identity_is_rejected(payload):
    assert not webull_io.account_id_is_listed(payload, "configured-id")


@pytest.mark.parametrize("envelope", [False, True])
def test_only_exact_account_id_is_accepted(envelope):
    payload = [{"account_id": "configured-id", "account_number": "other"}]
    assert webull_io.account_id_is_listed({"data": payload} if envelope else payload, "configured-id")


def test_account_check_precedes_cache_and_repeats_on_identity_change(monkeypatch):
    webull_io.reset_clients()
    built = _install_fake_sdk(monkeypatch)
    monkeypatch.setenv("WEBULL_APP_KEY", "test-key")
    monkeypatch.setenv("WEBULL_APP_SECRET", "test-secret")
    try:
        webull_io.build_clients()
        webull_io.build_clients()
        assert built["account_reads"] == 1
        monkeypatch.setenv("WEBULL_ACCOUNT_ID", "second-account")
        webull_io.build_clients()
        assert built["account_reads"] == 2
    finally:
        webull_io.reset_clients()


def test_invalid_account_never_populates_client_cache(monkeypatch):
    webull_io.reset_clients()
    _install_fake_sdk(monkeypatch)
    monkeypatch.setenv("WEBULL_APP_KEY", "test-key")
    monkeypatch.setenv("WEBULL_APP_SECRET", "test-secret")
    module = sys.modules["webull.trade.trade_client"]
    monkeypatch.setattr(module, "TradeClient", lambda api: SimpleNamespace(
        account_v2=SimpleNamespace(get_account_list=lambda: SimpleNamespace(
            status_code=200, json=lambda: [{"account_id": "different-account"}]))))
    try:
        with pytest.raises(webull_io.WebullConfigError, match="not present"):
            webull_io.build_clients()
        assert webull_io._CLIENTS is None
    finally:
        webull_io.reset_clients()


@pytest.mark.parametrize("source", [
    'Authorization: Bearer test-credential-value',
    '{"authorization": "Basic test-credential-value"}',
    'authorization%3DBearer%20test-credential-value&other=ok',
    '{"token": "test-credential-value"}',
    '{"refresh_token": "test-credential-value"}',
    '{"x-app-secret": "test-credential-value"}',
    '{"account_number": "test-credential-value"}',
])
def test_credentials_are_removed_in_full(source):
    assert "test-credential-value" not in webull_io.redact_sensitive_text(source)


def test_sdk_exception_trace_is_redacted_and_log_is_not_duplicated():
    sdk = logging.getLogger("webull.core")
    root = logging.getLogger()
    before, propagated, level = list(sdk.handlers), sdk.propagate, sdk.level
    output, duplicate = io.StringIO(), io.StringIO()
    root_handler = logging.StreamHandler(duplicate)
    root.addHandler(root_handler)
    class Api:
        def set_stream_logger(self, **kwargs):
            sdk.addHandler(logging.StreamHandler(output))
            sdk.setLevel(logging.INFO)
    try:
        sdk.handlers = []
        webull_io._configure_sdk_stream_logger(Api())
        try:
            raise RuntimeError("Authorization: Bearer test-credential-value")
        except RuntimeError:
            logging.getLogger("webull.core.client").exception("request failed")
        assert output.getvalue().count("request failed") == 1
        assert "test-credential-value" not in output.getvalue()
        assert duplicate.getvalue() == ""
    finally:
        sdk.handlers, sdk.propagate, sdk.level = before, propagated, level
        root.removeHandler(root_handler)


def test_broker_metadata_survives_sdk_boundary_and_tick_event(capsys):
    error = ServerException("OPENAPI_SYSTEM_ERROR", "private account message", http_status=417,
                            request_id="11111111-2222-3333-4444-555555555555")
    class Base:
        def get_response(self, request):
            raise error
    request = SimpleNamespace(get_action_name=lambda: "/trading/assets/positions/list")
    with pytest.raises(ServerException):
        webull_io._bounded_api_class(Base)().get_response(request)
    details = webull_io.broker_error_details(error)
    assert details["operation"] == "positions"
    assert details["http_status"] == 417
    assert details["code"] == "OPENAPI_SYSTEM_ERROR"
    body = {"decision": {"error": str(error), "type": "ServerException", "broker_error": details}}
    observability.emit_tick(body, 503)
    event = json.loads(capsys.readouterr().out)
    assert event["errors"][0]["broker_error"] == details
    assert "private account message" not in json.dumps(event)


def test_execution_item_errors_are_in_completion_event(capsys):
    observability.emit_tick({"dispatch": {"results": [{
        "status": "PLACING_UNKNOWN", "error": "sensitive message", "error_type": "ServerException",
        "broker_error": {"code": "OPENAPI_SYSTEM_ERROR", "http_status": 417,
                         "request_id": "not-a-request-id", "operation": {"unsafe": "value"},
                         "account_id": "private-id"},
    }]}}, 200)
    event = json.loads(capsys.readouterr().out)
    assert event["business_status"] == "ERROR"
    assert event["errors"][0]["broker_error"] == {
        "code": "OPENAPI_SYSTEM_ERROR", "http_status": 417, "retryable_read": True}
    assert "private-id" not in json.dumps(event)


def test_smoke_rejects_account_number_alias():
    from lego_broker_smoke import _contains_account_id
    assert not _contains_account_id([{"account_id": "real-id", "account_number": "wrong-id"}], "wrong-id")


@pytest.mark.parametrize("method", ["fetch_instrument_capability", "fetch_buying_power"])
def test_smoke_cannot_pass_when_a_required_money_path_read_fails(monkeypatch, method):
    import lego_broker_smoke as smoke
    monkeypatch.setenv("AUTO_SUBMIT", "false")
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "test-account")
    monkeypatch.setattr(smoke, "load_config", lambda: SimpleNamespace(symbol="AAPL"))
    trade = SimpleNamespace(account_v2=SimpleNamespace(get_account_list=lambda: SimpleNamespace(
        status_code=200, json=lambda: [{"account_id": "test-account"}])))
    monkeypatch.setattr(smoke, "build_clients", lambda: (trade, object()))
    monkeypatch.setattr(smoke, "clients_endpoint", lambda *_: webull_io.UAT_ENDPOINT)
    monkeypatch.setattr(smoke, "environment_label", lambda: smoke.UAT)
    monkeypatch.setattr(smoke, "fetch_instrument_capability", lambda *_: object())
    def fail(*_):
        raise ServerException("OPENAPI_SYSTEM_ERROR", http_status=417)
    monkeypatch.setattr(smoke, method, fail)
    with pytest.raises(ServerException):
        smoke.run_smoke()
