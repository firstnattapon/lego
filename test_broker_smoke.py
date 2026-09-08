"""Broker smoke is redacted, read-mostly, and structurally non-submitting."""
from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import lego_broker_smoke as smoke
from lego_one_row import Config
from lego_orders import PROD, UAT
from webull_io import PROD_ENDPOINT, UAT_ENDPOINT


CFG = Config(symbol="AAPL", fix_c=1000.0, diff=0.0,
             dna_code="bypass:100", strategy_id="shannon_demon_lego",
             decimal_precision=5)


class Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


@pytest.fixture
def broker(monkeypatch):
    monkeypatch.setenv("AUTO_SUBMIT", "false")
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "account-test-123")
    monkeypatch.setattr(smoke, "environment_label", lambda: UAT)
    monkeypatch.setattr(smoke, "load_config", lambda: CFG)
    trade = SimpleNamespace(account_v2=SimpleNamespace(
        get_account_list=lambda: Response({
            "data": [{"account_id": "account-test-123"}]
        })))
    data = object()
    monkeypatch.setattr(smoke, "build_clients", lambda: (trade, data))
    monkeypatch.setattr(smoke, "clients_endpoint",
                        lambda *_a: UAT_ENDPOINT)
    monkeypatch.setattr(smoke, "fetch_holdings", lambda *_a: 2.0)
    monkeypatch.setattr(smoke, "fetch_open_orders", lambda *_a: [])
    monkeypatch.setattr(smoke, "fetch_snapshot", lambda *_a: {
        "price": 100.0,
        "quote_time": "2026-08-02T09:00:00.000Z",
    })
    return trade, data


def test_connectivity_mode_never_previews(broker, monkeypatch):
    called = []
    monkeypatch.setattr(smoke, "preview_market_order",
                        lambda *_a: called.append(True))

    report = smoke.run_smoke()

    assert report["ok"] is True
    assert report["broker_mutations"] is False
    assert report["checks"]["preview_requested"] is False
    assert called == []


def test_explicit_uat_preview_uses_safe_payload(broker, monkeypatch):
    payloads = []
    monkeypatch.setattr(smoke, "preview_market_order",
                        lambda _client, payload: payloads.append(payload) or True)

    report = smoke.run_smoke(preview_side="BUY", preview_quantity="1")

    assert report["ok"] is True
    assert report["checks"]["preview_accepted"] is True
    order = payloads[0][0]
    assert order["side"] == "BUY"
    assert order["quantity"] == "1"
    assert len(order["client_order_id"]) == 32


def test_preview_rejection_is_a_failed_smoke_not_a_submission(broker, monkeypatch):
    monkeypatch.setattr(smoke, "preview_market_order", lambda *_a: False)
    report = smoke.run_smoke(preview_side="BUY", preview_quantity="1")
    assert report["ok"] is False
    assert report["checks"]["preview_accepted"] is False
    assert report["broker_mutations"] is False


def test_production_refuses_preview_before_building_clients(broker, monkeypatch):
    monkeypatch.setattr(smoke, "environment_label", lambda: PROD)
    monkeypatch.setattr(
        smoke, "build_clients",
        lambda: pytest.fail("clients must not be built for refused PROD Preview"))
    with pytest.raises(smoke.SmokeRefusal, match="only in Test"):
        smoke.run_smoke(preview_side="BUY", preview_quantity="1")


def test_production_connectivity_mode_remains_read_only(broker, monkeypatch):
    monkeypatch.setattr(smoke, "environment_label", lambda: PROD)
    monkeypatch.setattr(smoke, "clients_endpoint",
                        lambda *_a: PROD_ENDPOINT)
    report = smoke.run_smoke()
    assert report["ok"] is True
    assert report["environment"] == PROD
    assert report["broker_mutations"] is False


def test_auto_submit_must_be_off(broker, monkeypatch):
    monkeypatch.setenv("AUTO_SUBMIT", "true")
    with pytest.raises(smoke.SmokeRefusal, match="explicitly false"):
        smoke.run_smoke()


@pytest.mark.parametrize("value", ["1", "yes", "on", "typo", ""])
def test_every_non_false_auto_submit_spelling_is_refused(
        broker, monkeypatch, value):
    monkeypatch.setenv("AUTO_SUBMIT", value)
    with pytest.raises(smoke.SmokeRefusal, match="explicitly false"):
        smoke.run_smoke()


def test_missing_auto_submit_is_refused(broker, monkeypatch):
    monkeypatch.delenv("AUTO_SUBMIT")
    with pytest.raises(smoke.SmokeRefusal, match="explicitly false"):
        smoke.run_smoke()


def test_sell_preview_cannot_exceed_holdings(broker):
    with pytest.raises(smoke.SmokeRefusal, match="exceeds"):
        smoke.run_smoke(preview_side="SELL", preview_quantity="2.1")


@pytest.mark.parametrize(
    "quantity", ["0", "-1", "NaN", "Infinity", "bad", "1e-10000"])
def test_preview_quantity_must_be_finite_and_positive(broker, quantity):
    with pytest.raises(smoke.SmokeRefusal, match="quantity"):
        smoke.run_smoke(preview_side="BUY", preview_quantity=quantity)


def test_configured_account_must_be_in_authenticated_list(broker, monkeypatch):
    trade = SimpleNamespace(account_v2=SimpleNamespace(
        get_account_list=lambda: Response({"data": []})))
    monkeypatch.setattr(smoke, "build_clients", lambda: (trade, object()))
    with pytest.raises(smoke.SmokeRefusal, match="not present"):
        smoke.run_smoke()


def test_account_id_in_unrelated_field_does_not_pass(broker, monkeypatch):
    trade = SimpleNamespace(account_v2=SimpleNamespace(
        get_account_list=lambda: Response({
            "data": [{"nickname": "account-test-123"}]
        })))
    monkeypatch.setattr(smoke, "build_clients", lambda: (trade, object()))
    with pytest.raises(smoke.SmokeRefusal, match="not present"):
        smoke.run_smoke()


def test_endpoint_attestation_must_match_environment(broker, monkeypatch):
    monkeypatch.setattr(smoke, "clients_endpoint",
                        lambda *_a: PROD_ENDPOINT)
    with pytest.raises(smoke.SmokeRefusal, match="endpoint"):
        smoke.run_smoke()


def test_cli_error_is_json_and_redacts_credentials(monkeypatch, capsys):
    monkeypatch.setenv("WEBULL_APP_SECRET", "super-secret-value")
    monkeypatch.setattr(
        smoke, "run_smoke",
        lambda **_kw: (_ for _ in ()).throw(
            RuntimeError("bad super-secret-value")))

    assert smoke.main([]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert report["broker_mutations"] is False
    assert "super-secret-value" not in json.dumps(report)


def test_invalid_argv_is_generic_redacted_json(capsys):
    assert smoke.main(["--unknown", "raw-secret-like-value"]) == 2
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert captured.err == ""
    assert report["error"] == "invalid command-line arguments"
    assert "raw-secret-like-value" not in json.dumps(report)


def test_module_imports_no_broker_mutation_helper():
    tree = ast.parse(Path(smoke.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "webull_io"
        for alias in node.names
    }
    assert imported.isdisjoint({
        "place_market_order", "replace_market_order", "cancel_market_order"
    })
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert called_attributes.isdisjoint({
        "place_order", "replace_order", "cancel_order"
    })

