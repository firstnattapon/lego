"""Synthetic contract fixtures: the historical raw Order Detail was not captured."""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
import json
import pytest

import execution_service as execution
import lego_orders as orders
import lego_outbox as outbox
import order_recovery as recovery
import webull_io
import observability
from conftest import FAKE_DB
from test_continuous_recovery_v4 import existing_order, isolated, NOW


def fractional():
    intent, claim, detail, _ = existing_order()
    payload = [{"client_order_id": intent["run_id"], "symbol": "UBER",
                "side": "BUY", "quantity": "147.13455"}]
    intent = outbox.update_intent("chain", intent["run_id"], {
        "quantity": "147.13455", "order_payload": payload})
    detail.update(total_quantity="147.13455", filled_quantity="147.13455",
                  status="FILLED", filled_price="67.97", filled_fee="0")
    return intent, claim, detail


@pytest.mark.parametrize("qty_alias", orders.TOTAL_QUANTITY_FIELDS)
@pytest.mark.parametrize("fill_alias", orders.FILLED_QUANTITY_FIELDS)
@pytest.mark.parametrize("wrapper", ["flat", "data", "items", "orders", "nested"])
def test_fractional_aliases_preserve_exact_quantity(qty_alias, fill_alias, wrapper):
    intent, _, detail = fractional()
    detail[qty_alias] = detail.pop("total_quantity")
    detail[fill_alias] = detail.pop("filled_quantity")
    if wrapper == "nested":
        detail = {"data": {"client_order_id": intent["run_id"], "orders": [detail]}}
    elif wrapper != "flat":
        detail = {wrapper: [detail]}
    summary = orders.summarize_order_result({}, detail)
    evidence = recovery.validate_evidence(intent, detail, summary)
    assert evidence.total_quantity == evidence.filled_quantity == Decimal("147.13455")


@pytest.mark.parametrize("status,filled", [("SUBMITTED", "0"), ("PARTIAL_FILLED", "70"),
    ("CANCELLED", "70"), ("REJECTED", "0"), ("EXPIRED", "0"), ("FILLED", "147.13455")])
def test_terminal_and_partial_contract(status, filled):
    intent, _, detail = fractional()
    detail.update(status=status, filled_quantity=filled)
    summary = orders.summarize_order_result({}, detail)
    recovery.validate_evidence(intent, detail, summary)
    assert summary["realized"] == (Decimal(filled) > 0)


@pytest.mark.parametrize("change", [{"qty": "147.13"}, {"filled_qty": "147.13"},
    {"total_quantity": "NaN"}, {"total_quantity": True}])
def test_contradictory_or_malformed_quantity_cannot_fall_through(change):
    _, _, detail = fractional()
    with pytest.raises(ValueError):
        orders.canonical_order_evidence({**detail, **change})


def test_nested_quantity_conflict_is_not_overwritten():
    _, _, detail = fractional()
    with pytest.raises(orders.ConflictingOrderEvidence):
        orders.canonical_order_evidence({"total_quantity": "147.13", "orders": [detail]})


@pytest.mark.parametrize("field", ["client_order_id", "total_quantity", "filled_quantity"])
def test_missing_evidence_is_incomplete(field):
    intent, _, detail = fractional()
    detail.pop(field)
    with pytest.raises(orders.IncompleteOrderEvidence):
        recovery.validate_evidence(intent, detail, orders.summarize_order_result({}, detail))


def test_quantity_mismatch_halts_once_and_keeps_private_diagnostic(monkeypatch):
    intent, claim, detail = fractional()
    detail.update(total_quantity="147.13", access_token="sensitive-fixture")
    reads = []
    monkeypatch.setattr(execution, "fetch_order_detail", lambda *_: reads.append(1) or detail)
    monkeypatch.setattr(webull_io, "cancel_order", lambda *_: pytest.fail("unverified cancel"))
    monkeypatch.setattr(webull_io, "find_recent_order_by_client_id", lambda *a, **k: pytest.fail("conflict masked"))
    result = execution._dispatch_or_reconcile_one(None, None, SimpleNamespace(symbol="UBER"), intent, claim)
    assert result["needs_manual_check"] and result["status"] == "MANUAL_RECONCILIATION_REQUIRED"
    persisted = outbox.read_intent("chain", intent["run_id"])
    assert "147.13" in persisted["reconcile_evidence"]
    assert "sensitive-fixture" not in persisted["reconcile_evidence"]
    assert persisted["actionable_sort"] is None
    # Simulate successive restarted workers receiving the durable manual state.
    for _ in range(3):
        result = execution._dispatch_or_reconcile_one(None, None, None, persisted, claim)
        assert result["reconciliation_paused"]
    assert reads == [1]
    assert not execution._chain_fence_can_clear(persisted)
    scope = outbox.account_symbol_fence_key("identity", "UBER")
    assert FAKE_DB.reference(f"{outbox.DISPATCH_LOCK_PATH}/{scope}").get()["inflight_run_id"] == intent["run_id"]


def test_missing_detail_uses_verified_history_without_new_place(monkeypatch):
    intent, claim, detail = fractional()
    monkeypatch.setattr(execution, "fetch_order_detail", lambda *_: {"status": "UNKNOWN"})
    reads = []
    monkeypatch.setattr(webull_io, "find_recent_order_by_client_id", lambda *a, **k: reads.append(1) or detail)
    monkeypatch.setattr(execution, "_finish_with_realized", lambda tc, cfg, i, s: s)
    result = execution._dispatch_or_reconcile_one(None, None, SimpleNamespace(symbol="UBER"), intent, claim)
    assert result["status"] == "FILLED" and result["filled_quantity"] == "147.13455"
    assert reads == [1]


def test_quiet_heartbeat_retains_unhealthy_business_status(capsys):
    body = {"recovery": {"dispatch_blocked": True, "reconciliation_paused": True,
                         "halt_since": NOW.isoformat(), "results": []}}
    observability.emit_tick(body, 200)
    event = json.loads(capsys.readouterr().out)
    assert event["business_status"] == "MANUAL_RECONCILIATION_REQUIRED"
    assert event["severity"] == "INFO" and event["reconciliation_paused"]


@pytest.mark.parametrize("status", ["PENDING", "INVALID", "EXPIRED", None, "NORMAL"])
def test_production_live_token_status_is_required(monkeypatch, status):
    local = {"token": "test-token", "status": "NORMAL"}
    monkeypatch.setattr(webull_io, "read_local_token", lambda: local)
    written = []
    monkeypatch.setattr(webull_io, "_write_local_token", lambda *a: written.append(a))
    payload = {"token": "test-token", "status": status,
               "expires": int((NOW + timedelta(days=7)).timestamp() * 1000)}
    calls = []
    operation = SimpleNamespace(check_token=lambda token: calls.append(token) or
        SimpleNamespace(status_code=200, json=lambda: payload))
    api = SimpleNamespace(set_token=lambda _: None)
    if status == "NORMAL":
        webull_io.verify_production_token(api, operation)
    else:
        with pytest.raises(webull_io.TokenUnavailableError):
            webull_io.verify_production_token(api, operation)
    assert calls == ["test-token"]
    assert bool(written) == (status == "NORMAL")


def test_production_missing_token_never_calls_broker(monkeypatch):
    monkeypatch.setattr(webull_io, "read_local_token", lambda: None)
    with pytest.raises(webull_io.TokenUnavailableError):
        webull_io.verify_production_token(None, SimpleNamespace())


def test_first_post_place_anomaly_retains_detail_and_stops_retries(monkeypatch):
    intent, _, detail = fractional()
    detail["total_quantity"] = "147.13"
    monkeypatch.setattr(execution, "fetch_order_detail", lambda *_: detail)
    with pytest.raises(orders.BrokerContractAnomaly) as caught:
        execution._poll_order_status(None, intent["run_id"], {}, intent=intent)
    result = execution._persist_reconcile_failure(intent, caught.value)
    assert result["status"] == "MANUAL_RECONCILIATION_REQUIRED"
    assert "147.13" in outbox.read_intent("chain", intent["run_id"])["reconcile_evidence"]


@pytest.mark.parametrize("broker_id,error", [(None, orders.IncompleteOrderEvidence),
                                           ("another-order", orders.ConflictingOrderEvidence)])
def test_detail_must_prove_the_persisted_place_ack_broker_id(broker_id, error):
    intent, _, detail = fractional()
    intent["broker_order_id"] = "ack-order"
    if broker_id is not None:
        detail["order_id"] = broker_id
    with pytest.raises(error):
        recovery.validate_evidence(intent, detail, orders.summarize_order_result({}, detail))
