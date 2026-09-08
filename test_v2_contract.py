import base64
import hashlib
import hmac
import json
from decimal import Decimal
from datetime import datetime, timezone
from urllib.parse import quote

import pytest

from config import DNABundle, OperatorSettings, load_runtime_config, release_binding_for
from ledger_v2 import BrokerCashflow, FrozenLedger, r_market
import main
import lego_archive
from lego_one_row import Config, build_decision, PASS_MIN_ORDER
from webull_io import (canonical_payload_hash, hydrate_token_from_secret,
                       parse_buying_power, parse_instrument_capability,
                       preview_market_order_result, validate_preview_funding,
                       validate_order_detail_identity, validate_place_response)
from conftest import fake_trade_client


def _env(**overrides):
    data = {
        "LEGO_SYMBOL": "AAPL",
        "LEGO_FIX_C": "1500",
        "LEGO_DIFF": "25",
        "WEBULL_ENV": "UAT",
        "WEBULL_ACCOUNT_ID": "test-account",
    }
    data.update(overrides)
    return data


def test_six_operator_settings_default_fail_safe():
    runtime = load_runtime_config(_env())
    assert set(runtime.operator.canonical()) == {
        "symbol", "principal_usd", "diff_usd", "dna_fingerprint", "mode", "active"
    }
    assert runtime.operator.mode == "observe"
    assert runtime.operator.active is False
    assert runtime.allows_new_broker_mutation is False


def test_environment_host_is_allowlisted_and_not_request_controlled():
    runtime = load_runtime_config(_env(WEBULL_ENV="PROD"))
    assert runtime.deployment.endpoint == "api.webull.co.th"
    with pytest.raises(ValueError):
        load_runtime_config(_env(WEBULL_ENV="https://attacker.invalid"))


def test_release_binding_is_candidate_account_and_environment_specific():
    base = _env(
        LEGO_MODE="trade", LEGO_ACTIVE="true", LEGO_CANDIDATE_HASH="abc123"
    )
    first = load_runtime_config(base)
    assert not first.allows_new_broker_mutation
    base["LEGO_RELEASE_AUTHORIZATION"] = first.deployment.expected_release_binding
    authorized = load_runtime_config(base)
    assert authorized.allows_new_broker_mutation
    assert load_runtime_config({**base, "WEBULL_ACCOUNT_ID": "other"}).allows_new_broker_mutation is False


def test_operator_can_generate_exact_release_binding_without_authorizing_a_trade():
    env = _env(LEGO_CANDIDATE_HASH="candidate")
    assert release_binding_for(env) == load_runtime_config(env).deployment.expected_release_binding
    assert callable(main.lego_tick)


def test_trained_legacy_dna_without_bundle_is_blocked():
    with pytest.raises(ValueError, match="DNA bundle"):
        load_runtime_config(_env(LEGO_DNA_CODE="111050"))


def test_pass_freezes_e_while_e_mark_moves():
    ledger = FrozenLedger(
        A=Decimal("75"), p_acted=Decimal("105"), E=Decimal("2"),
        r_basis=Decimal("73"), finalized_seq=1,
    )
    observed = ledger.observe(1500, 110, 100)
    assert observed["delta_A"] == 0
    assert observed["A"] == 75
    assert observed["p_acted"] == 105
    assert observed["E"] == 2
    assert abs(observed["R_market"] - Decimal("142.9652697065")) < Decimal("1e-10")
    assert abs(observed["E_mark"] - Decimal("-67.9652697065")) < Decimal("1e-10")


def test_slippage_terminal_fill_then_pass_keeps_exact_e():
    ledger = FrozenLedger.genesis(100)
    basis = r_market(1500, 110, 100)
    finalized, event = ledger.finalize_terminal_fill(
        principal=1500, fill_price=111, decision_r_basis=basis
    )
    assert event["delta_A"] == Decimal("165.00")
    frozen = finalized.E
    assert abs(frozen - Decimal("22.0347302935")) < Decimal("1e-10")
    assert finalized.observe(1500, 112, 100)["E"] == frozen


def test_partial_cashflow_is_incremental_and_repeated_cumulative_is_noop():
    cash = BrokerCashflow()
    cash, first = cash.apply_cumulative(side="BUY", quantity="2", average_price="100")
    assert first["broker_cash_delta"] == Decimal("-200")
    cash, repeated = cash.apply_cumulative(side="BUY", quantity="2", average_price="100")
    assert repeated["broker_cash_delta"] == 0
    cash, terminal = cash.apply_cumulative(side="BUY", quantity="4", average_price="101")
    assert terminal["broker_cash_delta"] == Decimal("-204")
    assert cash.cash_delta == Decimal("-404")


def test_fee_only_correction_does_not_repeat_notional():
    cash, _ = BrokerCashflow().apply_cumulative(
        side="SELL", quantity="4", average_price="101", actual_fees="0.50"
    )
    cash, correction = cash.apply_cumulative(
        side="SELL", quantity="4", average_price="101", actual_fees="0.75"
    )
    assert correction["delta_quantity"] == 0
    assert correction["delta_notional"] == 0
    assert correction["delta_fee"] == Decimal("0.25")
    assert correction["broker_cash_delta"] == Decimal("-0.25")


def test_instrument_lot_is_broker_derived_and_v2_quantity_rounds_down():
    capability = parse_instrument_capability({"data": [{
        "symbol": "AAPL", "status": "OC", "category": "US_STOCK",
        "currency": "USD", "lot_size": "1", "fractionable": False,
    }]}, "AAPL")
    cfg = Config(
        "AAPL", 1500, 0, strategy_id="shannon_demon_lego_v2",
        quantity_increment=float(capability.quantity_increment),
    )
    decision = build_decision(cfg, price=111, holdings=0, signal=1)
    assert decision.quantity == 13  # 13.513... is never rounded up


def test_v2_below_minimum_quantity_is_pass():
    cfg = Config(
        "AAPL", 10, 0, strategy_id="shannon_demon_lego_v2",
        quantity_increment=1,
    )
    assert build_decision(cfg, price=111, holdings=0, signal=1).status == PASS_MIN_ORDER


def test_token_secret_hydrates_exact_sdk_format(monkeypatch, tmp_path):
    expires = "1893456000000"  # 2030-01-01 UTC, milliseconds
    class _Client:
        def access_secret_version(self, request):
            assert request["name"].endswith("/versions/latest")
            payload = type("Payload", (), {
                "data": f"secret-token\n{expires}\nNORMAL\n".encode()
            })()
            return type("Response", (), {"payload": payload})()
    monkeypatch.setenv("WEBULL_TOKEN_SECRET", "projects/p/secrets/webull-token")
    monkeypatch.setenv("WEBULL_TOKEN_DIR", str(tmp_path))
    result = hydrate_token_from_secret(_Client())
    assert result["hydrated"] is True
    assert (tmp_path / "token.txt").read_text().splitlines() == [
        "secret-token", expires, "NORMAL"
    ]


def test_strict_preview_contract_and_payload_hash(monkeypatch):
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "acct")
    order = [{"client_order_id": "run", "quantity": "1"}]
    result = preview_market_order_result(fake_trade_client(preview={
        "estimated_cost": "101.25", "estimated_transaction_fee": "1.25"
    }), order)
    assert result["estimated_cost"] == "101.25"
    assert canonical_payload_hash(order) == canonical_payload_hash([dict(order[0])])
    with pytest.raises(ValueError, match="estimated_cost"):
        preview_market_order_result(fake_trade_client(preview={"ok": True}), order)


def test_buying_power_and_sellable_guards_are_decimal_and_fail_closed():
    buying_power = parse_buying_power({"account_currency_assets": [{
        "currency": "USD", "buying_power": "102.50",
    }]})
    assert validate_preview_funding(
        side="BUY", quantity="1", holdings="0",
        preview={"estimated_cost": "100", "estimated_transaction_fee": "2.50"},
        buying_power=buying_power,
    )["required_cash"] == "102.50"
    with pytest.raises(ValueError, match="เกิน buying power"):
        validate_preview_funding(
            side="BUY", quantity="1", holdings="0",
            preview={"estimated_cost": "101", "estimated_transaction_fee": "2"},
            buying_power=buying_power,
        )
    with pytest.raises(ValueError, match="เกิน position"):
        validate_preview_funding(
            side="SELL", quantity="2", holdings="1",
            preview={}, buying_power=None,
        )


def test_place_and_detail_identity_are_bound_to_intent():
    assert validate_place_response(
        {"client_order_id": "run", "order_id": "broker-1"}, "run"
    )["order_id"] == "broker-1"
    with pytest.raises(ValueError, match="unknown"):
        validate_place_response({"client_order_id": "other"}, "run")
    detail = {"data": {"client_order_id": "run", "symbol": "AAPL"}}
    assert validate_order_detail_identity(
        detail, client_order_id="run", symbol="AAPL") == detail


def test_pinned_sdk_signature_matches_documented_hmac_sha256(monkeypatch):
    from webull.core.auth.composer import default_signature_composer as composer
    import webull.core.headers as headers_module

    monkeypatch.setattr(
        composer.common, "get_iso_8601_date", lambda: "2026-09-05T12:00:00Z")
    monkeypatch.setattr(
        composer.common, "get_uuid", lambda: "0123456789abcdef0123456789abcdef")
    headers = {}
    signature = composer.calc_signature(
        headers, "th-api.uat.webullbroker.com", "/trading/accounts/list",
        {"z": "last", "a": "first"}, {"probe": "value"},
        "app-key", "app-secret", None)

    sign_params = {str(k).lower(): str(v) for k, v in headers.items()
                   if k != headers_module.SIGNATURE}
    sign_params[headers_module.NATIVE_HOST.lower()] = "th-api.uat.webullbroker.com"
    sign_params.update({"z": "last", "a": "first"})
    body_hash = hashlib.sha256(json.dumps(
        {"probe": "value"}, separators=(",", ":")).encode()).hexdigest().upper()
    raw = "/trading/accounts/list&" + "&".join(
        f"{key}={value}" for key, value in sorted(sign_params.items()))
    raw += "&" + body_hash
    expected = base64.b64encode(hmac.new(
        b"app-secret&", quote(raw, safe="").encode(), hashlib.sha256
    ).digest()).decode()
    assert headers[headers_module.SIGN_ALGORITHM] == "HMAC-SHA256"
    assert headers[headers_module.SIGN_VERSION] == "1.0"
    assert signature == expected == headers[headers_module.SIGNATURE]


class _Request:
    def __init__(self, payload=None):
        self.payload = payload

    def get_json(self, silent=True):
        return self.payload


def test_lego_tick_rejects_request_identity_overrides_before_any_work(monkeypatch):
    touched = []
    monkeypatch.setattr(main, "load_runtime_config", lambda: touched.append("config"))
    body, code = main.lego_tick(_Request({"environment": "PROD", "symbol": "TSLA"}))
    assert code == 400
    assert body["pipeline_status"] == "UNTRUSTED_REQUEST_OVERRIDE"
    assert body["rejected_fields"] == ["environment", "symbol"]
    assert touched == []


def test_lego_tick_recovers_while_observe_but_never_dispatches(monkeypatch):
    runtime = load_runtime_config(_env())
    worker_calls = []
    monkeypatch.setattr(main, "load_runtime_config", lambda: runtime)
    monkeypatch.setattr(main.execution_service, "_init_firebase", lambda: None)
    monkeypatch.setattr(main, "runtime_identity_fingerprint", lambda: "identity")
    monkeypatch.setattr(
        main.execution_service, "_run_order_worker",
        lambda *args, **kwargs: worker_calls.append(kwargs["limit"]) or {
            "processed": 0, "actionable": 0, "results": []})
    monkeypatch.setattr(
        main.decision_service, "run_decision",
        lambda *args, **kwargs: ({"pipeline_status": "MARKET_CLOSED"}, 200))
    monkeypatch.setattr(main, "archive_terminal_records", lambda **kwargs: {"archived": 0})
    body, code = main.lego_tick(_Request())
    assert code == 200
    assert body["pipeline_status"] == "TICK_OK"
    assert body["new_mutations_authorized"] is False
    assert body["dispatch"] is None
    assert worker_calls == [3]


def test_lego_tick_dispatches_only_after_release_bound_trade_gate(monkeypatch):
    env = _env(LEGO_MODE="trade", LEGO_ACTIVE="true", LEGO_CANDIDATE_HASH="candidate")
    staged = load_runtime_config(env)
    env["LEGO_RELEASE_AUTHORIZATION"] = staged.deployment.expected_release_binding
    runtime = load_runtime_config(env)
    worker_calls = []
    monkeypatch.setattr(main, "load_runtime_config", lambda: runtime)
    monkeypatch.setattr(main.execution_service, "_init_firebase", lambda: None)
    monkeypatch.setattr(main, "runtime_identity_fingerprint", lambda: "identity")
    monkeypatch.setattr(
        main.execution_service, "_run_order_worker",
        lambda *args, **kwargs: worker_calls.append(kwargs["limit"]) or {
            "processed": 0, "actionable": 0, "results": []})
    monkeypatch.setattr(
        main.decision_service, "run_decision",
        lambda *args, **kwargs: ({"pipeline_status": "ROW_COMMITTED"}, 200))
    monkeypatch.setattr(main, "archive_terminal_records", lambda **kwargs: {"archived": 0})
    body, code = main.lego_tick(_Request())
    assert code == 200
    assert body["new_mutations_authorized"] is True
    assert body["dispatch"] is not None
    assert worker_calls == [3, 1]


def test_v2_archive_tail_uses_only_bounded_queries(monkeypatch):
    calls = []

    class _Query:
        def __init__(self, path):
            self.path = path

        def order_by_child(self, child):
            calls.append((self.path, "order_by_child", child))
            return self

        def end_at(self, value):
            calls.append((self.path, "end_at", value))
            return self

        def limit_to_first(self, value):
            calls.append((self.path, "limit_to_first", value))
            return self

        def get(self):
            calls.append((self.path, "get", None))
            return {}

    class _DB:
        @staticmethod
        def reference(path):
            return _Query(path)

    monkeypatch.setattr(lego_archive, "db", _DB)
    result = lego_archive.archive_terminal_records(
        datetime(2026, 9, 5, tzinfo=timezone.utc),
        days=30, limit=2, chain_key_="chain-a")
    assert result["bounded"] is True
    assert ("webull_lego_order_outbox/chain-a", "limit_to_first", 8) in calls
    assert ("webull_lego_order_audit", "limit_to_first", 8) in calls
    assert not any(path == "webull_lego_order_outbox" and op == "get"
                   for path, op, _ in calls)

