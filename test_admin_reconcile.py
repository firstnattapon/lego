"""Operator reconciliation is evidence-only, fenced, and crash-replayable."""
from __future__ import annotations

import math
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

import lego_admin_reconcile as admin
from main import _chain_fence_can_clear
from conftest import FAKE_DB, FakeReference, fake_trade_client
from lego_one_row import (ACTUAL_COLUMN, COLUMN_ORDER, DELTA_COLUMN,
                          EXCESS_COLUMN, REFERENCE_COLUMN, Config)
from lego_outbox import DISPATCH_LOCK_PATH, OUTBOX_PATH, ROWS_PATH
from lego_state import (BROKER_CASHFLOW_PATH, CASHFLOW_FINALIZED, CASHFLOW_SEMANTICS,
                        EXECUTION_STATE_KEY, REALIZED_PATH, STATE_PATH,
                        chain_key as strategy_chain_key, config_hash,
                        realized_open_legs_hash)
from webull_io import runtime_identity_fingerprint


UTC = timezone.utc
NOW = datetime(2026, 8, 2, 9, 0, 0, tzinfo=UTC)
CFG = Config(symbol="AAPL", fix_c=1000.0, diff=0.0,
             dna_code="bypass:100", strategy_id="shannon_demon_lego",
             decimal_precision=5)
CHAIN = strategy_chain_key(CFG)
RUN = "a" * 32
ACCOUNT = "admin-test-account"
ROW_PRICE_COLUMN = COLUMN_ORDER[5]


@pytest.fixture(autouse=True)
def env(monkeypatch):
    FAKE_DB.store.clear()
    monkeypatch.setenv("WEBULL_ENV", "UAT")
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", ACCOUNT)
    monkeypatch.setenv("FIREBASE_DB_URL", "https://test.firebaseio.com")
    monkeypatch.setenv("LEGO_ADMIN_RECONCILE_LEASE_SECONDS", "300")
    monkeypatch.setenv("LEGO_SYMBOL", CFG.symbol)
    monkeypatch.setenv("LEGO_FIX_C", str(CFG.fix_c))
    monkeypatch.setenv("LEGO_DIFF", str(CFG.diff))
    monkeypatch.setenv("LEGO_DNA_CODE", CFG.dna_code)
    monkeypatch.setenv("LEGO_STRATEGY_ID", CFG.strategy_id)
    monkeypatch.setenv("LEGO_DECIMAL_PRECISION", str(CFG.decimal_precision))


def _detail(*, status="REJECTED", qty=0, price=None, account=ACCOUNT,
            fee=None, run=RUN, broker_id="wb-123"):
    out = {
        "order_id": broker_id,
        "client_order_id": run,
        "account_id": account,
        "order_status": status,
        "filled_quantity": qty,
        "side": "BUY",
        "symbol": "AAPL",
    }
    if price is not None:
        out["average_filled_price"] = price
    if fee is not None:
        out["filled_fee"] = fee
    return out


def _seed_zero_fill(*, lock_overrides=None, intent_overrides=None,
                    state_overrides=None, row_overrides=None):
    identity = runtime_identity_fingerprint()
    lock = {"generation": 7, "inflight_run_id": RUN, "owner": "",
            "claim_token": "", "lease_until": ""}
    lock.update(lock_overrides or {})
    intent = {
        "chain_key": CHAIN,
        "run_id": RUN,
        "client_order_id": RUN,
        "status": "RECONCILE_ABANDONED",
        "needs_manual_check": True,
        "runtime_identity_fingerprint": identity,
        "side": "BUY",
        "symbol": "AAPL",
    }
    intent.update(intent_overrides or {})
    state = {
        "runtime_identity_fingerprint": identity,
        "config_hash": config_hash(CFG),
        "symbol": CFG.symbol,
        "cashflow_semantics": CASHFLOW_SEMANTICS,
        "p0": 100.0,
        "prev_price": 100.0,
        "prev_actual": 0.0,
        "prev_holdings": 10.0,
        EXECUTION_STATE_KEY: {
            "last_action_price": 100.0,
            "actual_cumulative": 0.0,
            "finalized_seq": 0,
            "finalized_runs": {},
        },
    }
    state.update(state_overrides or {})
    row = {
        "run_id": RUN,
        "chain_key": CHAIN,
        "committed": True,
        "semantics": CASHFLOW_SEMANTICS,
        "สินทรัพย์": CFG.symbol,
        ROW_PRICE_COLUMN: 100.0,
        REFERENCE_COLUMN: 0.0,
        DELTA_COLUMN: 0.0,
        ACTUAL_COLUMN: 0.0,
        EXCESS_COLUMN: 0.0,
        "cashflow_status": "PENDING_EXECUTION",
    }
    row.update(row_overrides or {})
    FAKE_DB.reference(f"{DISPATCH_LOCK_PATH}/{CHAIN}").set(lock)
    FAKE_DB.reference(f"{OUTBOX_PATH}/{CHAIN}/{RUN}").set(intent)
    FAKE_DB.reference(f"{STATE_PATH}/{CHAIN}").set(state)
    FAKE_DB.reference(f"{ROWS_PATH}/{RUN}").set(row)
    FAKE_DB.reference(f"{REALIZED_PATH}/{CHAIN}").set({})


def _client(detail):
    return fake_trade_client(order_detail=detail, preview={"ok": True},
                             place={"order_status": "FILLED"})


def test_dry_run_is_read_only_and_prints_exact_confirmation():
    _seed_zero_fill()
    client = _client(_detail())
    before = FAKE_DB.reference().get()

    plan = admin.inspect_reconciliation(CHAIN, RUN, client, now_utc=NOW)

    assert plan["allowed"] is True, plan["blockers"]
    assert plan["dry_run"] is True
    assert plan["confirmation_phrase"] == (
        f"ACK RECONCILED {CHAIN} {RUN} wb-123 REJECTED 0")
    assert FAKE_DB.reference().get() == before
    assert len(client.order_v3.get_order_detail.calls) == 1
    assert client.order_v3.preview_order.calls == []
    assert client.order_v3.place_order.calls == []


@pytest.mark.parametrize("status", [
    "UNKNOWN", "OPEN", "SUBMITTED", "PARTIAL_FILLED", "PARTIALLY_FILLED",
])
def test_unknown_open_submitted_and_partial_broker_statuses_are_refused(status):
    _seed_zero_fill()
    plan = admin.inspect_reconciliation(
        CHAIN, RUN, _client(_detail(status=status)), now_utc=NOW)
    assert plan["allowed"] is False
    assert f"broker_status_not_terminal:{status}" in plan["blockers"]


def test_terminal_zero_fill_requires_explicit_quantity_and_broker_id():
    _seed_zero_fill()
    no_qty = _detail()
    no_qty.pop("filled_quantity")
    plan = admin.inspect_reconciliation(CHAIN, RUN, _client(no_qty), now_utc=NOW)
    assert "broker_filled_quantity_missing" in plan["blockers"]

    no_id = _detail(broker_id="")
    plan = admin.inspect_reconciliation(CHAIN, RUN, _client(no_id), now_utc=NOW)
    assert "broker_order_id_missing" in plan["blockers"]


@pytest.mark.parametrize(("field", "blocker"), [
    ("client_order_id", "broker_client_order_id_missing"),
    ("account_id", "broker_account_id_missing"),
    ("side", "broker_side_missing"),
    ("symbol", "broker_symbol_missing"),
])
def test_admin_refuses_terminal_detail_without_broker_identity(field, blocker):
    _seed_zero_fill()
    detail = _detail()
    detail.pop(field)
    plan = admin.inspect_reconciliation(CHAIN, RUN, _client(detail), now_utc=NOW)
    assert plan["allowed"] is False
    assert blocker in plan["blockers"]


@pytest.mark.parametrize("field", ["client_order_id", "account_id", "side", "symbol"])
@pytest.mark.parametrize("container", ["orders", "data"])
def test_admin_refuses_conflicting_group_and_leg_identity(field, container):
    _seed_zero_fill()
    detail = {field: "different", container: [_detail()] if container == "orders"
              else _detail()}
    with pytest.raises(admin.ReconcileRefusal, match="conflicting"):
        admin.inspect_reconciliation(CHAIN, RUN, _client(detail), now_utc=NOW)


@pytest.mark.parametrize(("canonical", "alias"), [
    ("client_order_id", "clientOrderId"),
    ("account_id", "accountId"),
    ("side", "order_side"),
    ("symbol", "ticker"),
])
def test_admin_refuses_conflicting_broker_identity_aliases(canonical, alias):
    _seed_zero_fill()
    detail = _detail()
    detail[alias] = "different"
    with pytest.raises(admin.ReconcileRefusal, match="conflicting"):
        admin.inspect_reconciliation(CHAIN, RUN, _client(detail), now_utc=NOW)

    wrapped = {alias: "different", "orders": [_detail()]}
    with pytest.raises(admin.ReconcileRefusal, match="conflicting"):
        admin.inspect_reconciliation(CHAIN, RUN, _client(wrapped), now_utc=NOW)


@pytest.mark.parametrize(("row_overrides", "intent_overrides", "blocker"), [
    ({"cashflow_status": "NO_ACTION"}, {}, "zero_fill_row_not_pending"),
    ({"execution_quantity": 0.1}, {}, "zero_fill_row_has_execution_evidence"),
    ({}, {"cashflow_finalized": True},
     "zero_fill_intent_cashflow_is_finalized"),
    ({}, {"realized": True}, "zero_fill_intent_realized_is_finalized"),
])
def test_zero_fill_requires_an_unfinalized_pending_model_row(
        row_overrides, intent_overrides, blocker):
    _seed_zero_fill(row_overrides=row_overrides,
                    intent_overrides=intent_overrides)
    plan = admin.inspect_reconciliation(
        CHAIN, RUN, _client(_detail()), now_utc=NOW)
    assert plan["allowed"] is False
    assert blocker in plan["blockers"]


@pytest.mark.parametrize(("row_overrides", "state_overrides", "blocker"), [
    ({DELTA_COLUMN: 999.0}, {}, "zero_fill_delta_not_frozen"),
    ({ACTUAL_COLUMN: 999.0}, {}, "zero_fill_actual_not_frozen"),
    ({EXCESS_COLUMN: 999.0}, {}, "zero_fill_excess_not_frozen"),
    ({REFERENCE_COLUMN: 999.0}, {}, "row_reference_equation_mismatch"),
    ({ROW_PRICE_COLUMN: "bad"}, {}, "row_quote_price_invalid"),
    ({}, {"config_hash": "bad"}, "state_config_hash_mismatch"),
    ({}, {"cashflow_semantics": "gated_theoretical_v2"},
     "state_cashflow_semantics_mismatch"),
])
def test_zero_fill_requires_frozen_recurrence_and_current_config_provenance(
        row_overrides, state_overrides, blocker):
    _seed_zero_fill(row_overrides=row_overrides,
                    state_overrides=state_overrides)
    plan = admin.inspect_reconciliation(
        CHAIN, RUN, _client(_detail()), now_utc=NOW)
    assert plan["allowed"] is False
    assert blocker in plan["blockers"]


@pytest.mark.parametrize(("seed_kwargs", "detail_kwargs", "blocker"), [
    ({"lock_overrides": {"inflight_run_id": "b" * 32}}, {},
     "wrong_inflight_run"),
    ({"lock_overrides": {"owner": "worker", "lease_until":
                          "2026-08-02T09:05:00Z"}}, {},
     "dispatch_owner_lease_active"),
    ({"intent_overrides": {"claim_owner": "worker", "claim_until":
                            "2026-08-02T09:05:00Z"}}, {},
     "intent_owner_lease_active"),
    ({"intent_overrides": {"runtime_identity_fingerprint": "x" * 64}}, {},
     "intent_runtime_identity_mismatch"),
    ({"state_overrides": {"runtime_identity_fingerprint": "x" * 64}}, {},
     "state_runtime_identity_mismatch"),
    ({}, {"account": "another-account"}, "broker_account_mismatch"),
    ({}, {"run": "b" * 32}, "broker_client_order_id_mismatch"),
])
def test_exact_chain_run_runtime_account_and_idle_owners_are_mandatory(
        seed_kwargs, detail_kwargs, blocker):
    _seed_zero_fill(**seed_kwargs)
    plan = admin.inspect_reconciliation(
        CHAIN, RUN, _client(_detail(**detail_kwargs)), now_utc=NOW)
    assert plan["allowed"] is False
    assert blocker in plan["blockers"]


def _seed_positive_fill(*, break_witness: str | None = None):
    qty, price, holdings_after = 0.5, 101.25, 10.5
    previous_price, previous_actual, quote_price = 100.0, 0.0, 101.0
    reference = CFG.fix_c * math.log(quote_price / 100.0)
    delta = CFG.fix_c * (price / previous_price - 1.0)
    actual = previous_actual + delta
    excess = actual - reference
    finalized_at = "2026-08-02T08:59:30Z"
    identity = runtime_identity_fingerprint()
    _seed_zero_fill(
        intent_overrides={
            "status": "CASHFLOW_FINALIZE_ERROR",
            "cashflow_abandoned": True,
            "place_attempted": True,
            "quantity": qty,
            "cashflow_finalized": True,
            "realized": True,
            "filled_quantity": qty,
            "filled_price": price,
            "filled_fee": 0.1,
            "broker_fee_status": "KNOWN",
            "broker_cashflow_recorded": True,
            "broker_cash_cumulative": "-50.725",
        },
        row_overrides={
            "cashflow_status": CASHFLOW_FINALIZED,
            "execution_quantity": qty,
            "execution_price": price,
            "post_execution_holdings": holdings_after,
            "cashflow_finalized_at": finalized_at,
            ROW_PRICE_COLUMN: quote_price,
            REFERENCE_COLUMN: reference,
            DELTA_COLUMN: delta,
            ACTUAL_COLUMN: actual,
            EXCESS_COLUMN: excess,
        },
        state_overrides={
            "runtime_identity_fingerprint": identity,
            "prev_price": price,
            "prev_actual": actual,
            "prev_holdings": holdings_after,
            EXECUTION_STATE_KEY: {
                "last_action_price": price,
                "actual_cumulative": actual,
                "finalized_seq": 1,
                "last_finalized_run_id": RUN,
                "finalized_runs": {RUN: {
                    "filled_quantity": qty,
                    "filled_price": price,
                    "holdings_after": holdings_after,
                    "delta_actual": delta,
                    "actual_cumulative": actual,
                    "excess": excess,
                    "reference": reference,
                    "previous_action_price": previous_price,
                    "previous_actual_cumulative": previous_actual,
                    "seq": 1,
                    "at": finalized_at,
                }},
            },
        })
    open_legs = {"buys": [[qty, price, 0.2]], "sells": []}
    FAKE_DB.reference(f"{REALIZED_PATH}/{CHAIN}").set({
        "open_legs": open_legs,
        "cumulative_realized": 0.0,
        "applied_seq": 1,
        "last_event_id": RUN,
        "applied_fills": {RUN: {
            "quantity": qty, "average_price": price,
            "fee": 0.1, "side": "BUY", "realized_delta": 0.0,
            "cumulative_realized_after": 0.0,
            "open_legs_after_hash": realized_open_legs_hash(open_legs),
            "seq": 1,
        }},
    })
    FAKE_DB.reference(f"{BROKER_CASHFLOW_PATH}/{CHAIN}/events/{RUN}").set({
        "event_id": RUN, "chain_key": CHAIN, "side": "BUY",
        "cumulative_quantity": "0.5", "cumulative_notional": "50.625",
        "actual_fees": "0.1", "fee_status": "KNOWN",
        "cash_cumulative": "-50.725",
    })
    if break_witness == "row":
        FAKE_DB.reference(f"{ROWS_PATH}/{RUN}").update({
            "execution_quantity": 0.4})
    elif break_witness == "model":
        FAKE_DB.reference(
            f"{STATE_PATH}/{CHAIN}/{EXECUTION_STATE_KEY}/finalized_runs/{RUN}").delete()
    elif break_witness == "realized":
        FAKE_DB.reference(f"{REALIZED_PATH}/{CHAIN}/applied_fills/{RUN}").delete()


def test_terminal_positive_fill_requires_both_ledgers_and_matching_row():
    _seed_positive_fill()
    detail = _detail(status="CANCELLED", qty=0.5, price=101.25, fee=0.1)
    plan = admin.inspect_reconciliation(CHAIN, RUN, _client(detail), now_utc=NOW)
    assert plan["allowed"] is True, plan["blockers"]

    expected = {
        "row": "row_execution_quantity_mismatch",
        "model": "model_cashflow_finalization_missing",
        "realized": "realized_ledger_fill_missing",
    }
    for broken, blocker in expected.items():
        FAKE_DB.store.clear()
        _seed_positive_fill(break_witness=broken)
        plan = admin.inspect_reconciliation(
            CHAIN, RUN, _client(detail), now_utc=NOW)
        assert plan["allowed"] is False
        assert blocker in plan["blockers"]


@pytest.mark.parametrize(("path", "value", "blocker"), [
    (f"{OUTBOX_PATH}/{CHAIN}/{RUN}/quantity", 0.31721,
     "submitted_order_anomaly:fill_exceeds_submitted_quantity"),
    (f"{OUTBOX_PATH}/{CHAIN}/{RUN}/place_attempted", None,
     "durable_place_marker_missing"),
    (f"{OUTBOX_PATH}/{CHAIN}/{RUN}/order_payload", [{
        "client_order_id": RUN, "symbol": "AAPL", "side": "BUY",
        "quantity": "0.31721"}],
     "submitted_order_anomaly:intent_payload_quantity_mismatch"),
    (f"{OUTBOX_PATH}/{CHAIN}/{RUN}/order_contract_anomaly",
     "fill_exceeds_submitted_quantity", "order_contract_anomaly_unresolved"),
    (f"{BROKER_CASHFLOW_PATH}/{CHAIN}/events/{RUN}", None,
     "broker_cashflow_event_missing"),
    (f"{BROKER_CASHFLOW_PATH}/{CHAIN}/events/{RUN}/cumulative_quantity",
     "0.4", "broker_cashflow_quantity_mismatch"),
    (f"{BROKER_CASHFLOW_PATH}/{CHAIN}/events/{RUN}/actual_fees",
     "0.2", "broker_cashflow_fee_mismatch"),
    (f"{BROKER_CASHFLOW_PATH}/{CHAIN}/events/{RUN}/cash_cumulative",
     "-50.625", "broker_cashflow_cash_mismatch"),
    (f"{OUTBOX_PATH}/{CHAIN}/{RUN}/broker_cashflow_recorded", False,
     "intent_broker_cashflow_not_recorded"),
    (f"{OUTBOX_PATH}/{CHAIN}/{RUN}/broker_cash_cumulative", "-50.7249",
     "intent_broker_cash_mismatch"),
])
def test_admin_positive_fill_refuses_unproven_submitted_or_cash_witness(
        path, value, blocker):
    _seed_positive_fill()
    ref = FAKE_DB.reference(path)
    if value is None:
        ref.delete()
    else:
        ref.set(value)
    plan = admin.inspect_reconciliation(
        CHAIN, RUN,
        _client(_detail(status="CANCELLED", qty=0.5, price=101.25, fee=0.1)),
        now_utc=NOW)
    assert plan["allowed"] is False
    assert blocker in plan["blockers"]


def test_positive_fill_cash_witness_change_after_dry_run_refuses_apply(monkeypatch):
    _seed_positive_fill()
    client = _client(_detail(status="CANCELLED", qty=0.5,
                             price=101.25, fee=0.1))
    plan = admin.inspect_reconciliation(CHAIN, RUN, client, now_utc=NOW)
    assert plan["allowed"] is True
    original_reserve = admin._reserve

    def reserve_then_change(*args, **kwargs):
        result = original_reserve(*args, **kwargs)
        FAKE_DB.reference(
            f"{BROKER_CASHFLOW_PATH}/{CHAIN}/events/{RUN}/updated_at").set(
                "2026-08-02T09:00:01Z")
        return result

    monkeypatch.setattr(admin, "_reserve", reserve_then_change)
    with pytest.raises(admin.ReconcileRefusal, match="evidence changed"):
        admin.acknowledge_reconciliation(
            plan, plan["confirmation_phrase"], client,
            operator="alice", now_utc=NOW)
    assert FAKE_DB.reference(f"{DISPATCH_LOCK_PATH}/{CHAIN}").get()[
        "inflight_run_id"] == RUN
    assert FAKE_DB.reference(f"{OUTBOX_PATH}/{CHAIN}/{RUN}").get().get(
        "admin_reconciled") is not True


def test_admin_accepts_bounded_fifo_v3_head_and_rejects_bad_or_pending_cursor():
    _seed_positive_fill()
    head_ref = FAKE_DB.reference(f"{REALIZED_PATH}/{CHAIN}")
    head_ref.update({
        "schema_version": 3,
        "fifo_read_cursor": {"buys": 0, "sells": 0},
        "fifo_write_cursor": {"buys": 1, "sells": 0},
        "active_matching_event_id": None,
    })
    detail = _detail(status="CANCELLED", qty=0.5, price=101.25, fee=0.1)
    valid = admin.inspect_reconciliation(CHAIN, RUN, _client(detail), now_utc=NOW)
    assert valid["allowed"] is True, valid["blockers"]

    head_ref.update({
        "fifo_read_cursor": {"buys": 2, "sells": 0},
        "fifo_write_cursor": {"buys": 1, "sells": 0},
    })
    invalid = admin.inspect_reconciliation(
        CHAIN, RUN, _client(detail), now_utc=NOW)
    assert "realized_fifo_cursor_invalid" in invalid["blockers"]

    head_ref.update({
        "fifo_read_cursor": {"buys": 0, "sells": 0},
        "active_matching_event_id": "pending-event",
    })
    pending = admin.inspect_reconciliation(
        CHAIN, RUN, _client(detail), now_utc=NOW)
    assert "realized_fifo_matching_pending" in pending["blockers"]


@pytest.mark.parametrize(("path", "value", "blocker"), [
    (f"{ROWS_PATH}/{RUN}/post_execution_holdings", 11.0,
     "row_post_execution_holdings_mismatch"),
    (f"{STATE_PATH}/{CHAIN}/{EXECUTION_STATE_KEY}/finalized_runs/{RUN}/holdings_after",
     -1.0, "model_cashflow_holdings_invalid"),
    (f"{STATE_PATH}/{CHAIN}/prev_holdings", 11.0,
     "state_prev_holdings_mismatch"),
    (f"{STATE_PATH}/{CHAIN}/{EXECUTION_STATE_KEY}/actual_cumulative", 2.6,
     "model_cashflow_actual_cumulative_mismatch"),
    (f"{STATE_PATH}/{CHAIN}/{EXECUTION_STATE_KEY}/last_action_price", 101.5,
     "model_cashflow_last_action_price_mismatch"),
    (f"{STATE_PATH}/{CHAIN}/{EXECUTION_STATE_KEY}/last_finalized_run_id", "b" * 32,
     "model_cashflow_latest_run_mismatch"),
    (f"{STATE_PATH}/{CHAIN}/{EXECUTION_STATE_KEY}/finalized_seq", 2,
     "model_cashflow_finalized_seq_mismatch"),
    (f"{STATE_PATH}/{CHAIN}/{EXECUTION_STATE_KEY}/finalized_runs/{RUN}/seq", 1.5,
     "model_cashflow_finalized_run_seq_invalid"),
    (f"{STATE_PATH}/{CHAIN}/prev_actual", 2.6,
     "state_prev_actual_mismatch"),
    (f"{STATE_PATH}/{CHAIN}/prev_price", 101.5,
     "state_prev_price_mismatch"),
    (f"{STATE_PATH}/{CHAIN}/{EXECUTION_STATE_KEY}/finalized_runs/{RUN}/at", "",
     "model_cashflow_finalized_at_missing"),
    (f"{ROWS_PATH}/{RUN}/cashflow_finalized_at", "2026-08-02T08:58:00Z",
     "row_cashflow_finalized_at_mismatch"),
    (f"{ROWS_PATH}/{RUN}/{REFERENCE_COLUMN}", 999.0,
     "row_reference_equation_mismatch"),
    (f"{STATE_PATH}/{CHAIN}/{EXECUTION_STATE_KEY}/finalized_runs/{RUN}/reference",
     999.0, "model_cashflow_reference_mismatch"),
    (f"{STATE_PATH}/{CHAIN}/{EXECUTION_STATE_KEY}/finalized_runs/{RUN}/previous_action_price",
     -1.0, "model_cashflow_previous_action_price_invalid"),
    (f"{STATE_PATH}/{CHAIN}/{EXECUTION_STATE_KEY}/finalized_runs/{RUN}/previous_actual_cumulative",
     "bad", "model_cashflow_previous_actual_invalid"),
])
def test_positive_fill_requires_one_consistent_durable_ledger_head(
        path, value, blocker):
    _seed_positive_fill()
    FAKE_DB.reference(path).set(value)
    plan = admin.inspect_reconciliation(
        CHAIN, RUN, _client(_detail(status="CANCELLED", qty=0.5, price=101.25, fee=0.1)),
        now_utc=NOW)
    assert plan["allowed"] is False
    assert blocker in plan["blockers"]


def test_positive_fill_rejects_consistently_corrupted_mirrors_by_equation():
    _seed_positive_fill()
    row_ref = FAKE_DB.reference(f"{ROWS_PATH}/{RUN}")
    row_ref.update({DELTA_COLUMN: 777.0, ACTUAL_COLUMN: 888.0,
                    EXCESS_COLUMN: 999.0})
    final_ref = FAKE_DB.reference(
        f"{STATE_PATH}/{CHAIN}/{EXECUTION_STATE_KEY}/finalized_runs/{RUN}")
    final_ref.update({"delta_actual": 777.0, "actual_cumulative": 888.0,
                      "excess": 999.0})
    FAKE_DB.reference(
        f"{STATE_PATH}/{CHAIN}/{EXECUTION_STATE_KEY}/actual_cumulative").set(888.0)
    FAKE_DB.reference(f"{STATE_PATH}/{CHAIN}/prev_actual").set(888.0)

    plan = admin.inspect_reconciliation(
        CHAIN, RUN,
        _client(_detail(status="CANCELLED", qty=0.5, price=101.25, fee=0.1)),
        now_utc=NOW)
    assert plan["allowed"] is False
    assert "model_cashflow_delta_equation_mismatch" in plan["blockers"]
    assert "model_cashflow_actual_equation_mismatch" in plan["blockers"]
    assert "model_cashflow_excess_equation_mismatch" in plan["blockers"]


def test_reconciliation_refuses_runtime_config_for_another_chain(monkeypatch):
    _seed_zero_fill()
    monkeypatch.setenv("LEGO_FIX_C", "2000")
    plan = admin.inspect_reconciliation(
        CHAIN, RUN, _client(_detail()), now_utc=NOW)
    assert plan["allowed"] is False
    assert "current_config_chain_mismatch" in plan["blockers"]


@pytest.mark.parametrize(("path", "value", "blocker"), [
    (f"{REALIZED_PATH}/{CHAIN}/open_legs", None,
     "realized_ledger_open_legs_missing"),
    (f"{REALIZED_PATH}/{CHAIN}/open_legs",
     {"buys": [[0.4, 101.25, 0.2]], "sells": []},
     "realized_ledger_open_legs_mismatch"),
    (f"{REALIZED_PATH}/{CHAIN}/cumulative_realized", 1.0,
     "realized_ledger_cumulative_mismatch"),
    (f"{REALIZED_PATH}/{CHAIN}/last_event_id", "b" * 32,
     "realized_ledger_latest_event_mismatch"),
    (f"{REALIZED_PATH}/{CHAIN}/applied_seq", 2,
     "realized_ledger_applied_seq_mismatch"),
    (f"{REALIZED_PATH}/{CHAIN}/applied_fills/{RUN}/seq", 1.5,
     "realized_ledger_fill_seq_invalid"),
    (f"{REALIZED_PATH}/{CHAIN}/applied_fills/{RUN}/realized_delta", "NaN",
     "realized_ledger_event_delta_invalid"),
    (f"{REALIZED_PATH}/{CHAIN}/applied_fills/{RUN}/open_legs_after_hash", "0" * 64,
     "realized_ledger_open_legs_mismatch"),
])
def test_positive_fill_requires_transactional_realized_ledger_witnesses(
        path, value, blocker):
    _seed_positive_fill()
    if value is None:
        FAKE_DB.reference(path).delete()
    else:
        FAKE_DB.reference(path).set(value)
    plan = admin.inspect_reconciliation(
        CHAIN, RUN, _client(_detail(status="CANCELLED", qty=0.5, price=101.25, fee=0.1)),
        now_utc=NOW)
    assert plan["allowed"] is False
    assert blocker in plan["blockers"]


def test_positive_fill_fee_requires_broker_and_persisted_evidence():
    _seed_positive_fill()
    missing = admin.inspect_reconciliation(
        CHAIN, RUN, _client(_detail(status="CANCELLED", qty=0.5, price=101.25)),
        now_utc=NOW)
    assert missing["allowed"] is False
    assert "broker_filled_fee_missing" in missing["blockers"]

    broker = admin.inspect_reconciliation(
        CHAIN, RUN,
        _client(_detail(status="CANCELLED", qty=0.5, price=101.25, fee=0.1)),
        now_utc=NOW)
    assert broker["allowed"] is True, broker["blockers"]

    FAKE_DB.reference(f"{OUTBOX_PATH}/{CHAIN}/{RUN}/filled_fee").delete()
    missing_intent = admin.inspect_reconciliation(
        CHAIN, RUN,
        _client(_detail(status="CANCELLED", qty=0.5, price=101.25, fee=0.1)),
        now_utc=NOW)
    assert missing_intent["allowed"] is False
    assert "intent_filled_fee_missing" in missing_intent["blockers"]

    invalid = admin.inspect_reconciliation(
        CHAIN, RUN,
        _client(_detail(status="CANCELLED", qty=0.5, price=101.25, fee="NaN")),
        now_utc=NOW)
    assert invalid["allowed"] is False
    assert "broker_filled_fee_invalid" in invalid["blockers"]


def test_positive_fill_requires_known_fee_state_and_matching_actual_fee():
    _seed_positive_fill()
    detail = _detail(status="CANCELLED", qty=0.5, price=101.25, fee=0.1)
    FAKE_DB.reference(f"{OUTBOX_PATH}/{CHAIN}/{RUN}/broker_fee_status").delete()
    unknown = admin.inspect_reconciliation(CHAIN, RUN, _client(detail), now_utc=NOW)
    assert "intent_broker_fee_not_known" in unknown["blockers"]

    FAKE_DB.reference(f"{OUTBOX_PATH}/{CHAIN}/{RUN}/broker_fee_status").set("KNOWN")
    wrong = admin.inspect_reconciliation(
        CHAIN, RUN,
        _client(_detail(status="CANCELLED", qty=0.5, price=101.25, fee=0.2)),
        now_utc=NOW)
    assert "intent_broker_fee_mismatch" in wrong["blockers"]
    assert "realized_ledger_fee_mismatch" in wrong["blockers"]


def test_positive_fill_accepts_complete_actual_fee_breakdown():
    _seed_positive_fill()
    detail = _detail(status="CANCELLED", qty=0.5, price=101.25)
    detail["commission"] = {"actual_commission": "0.07"}
    detail["fees"] = [{"type": "regulatory", "actual_value": "0.03"}]
    plan = admin.inspect_reconciliation(CHAIN, RUN, _client(detail), now_utc=NOW)
    assert plan["allowed"] is True, plan["blockers"]


def test_wrong_confirmation_changes_nothing():
    _seed_zero_fill()
    client = _client(_detail())
    plan = admin.inspect_reconciliation(CHAIN, RUN, client, now_utc=NOW)
    before = FAKE_DB.reference().get()
    with pytest.raises(admin.ReconcileRefusal, match="exactly"):
        admin.acknowledge_reconciliation(
            plan, plan["confirmation_phrase"] + " ", client,
            operator="alice", now_utc=NOW)
    assert FAKE_DB.reference().get() == before


def test_apply_writes_immutable_audit_clears_manual_fence_and_never_mutates_broker():
    _seed_zero_fill()
    client = _client(_detail())
    plan = admin.inspect_reconciliation(CHAIN, RUN, client, now_utc=NOW)

    result = admin.acknowledge_reconciliation(
        plan, plan["confirmation_phrase"], client,
        operator="alice@example.com", now_utc=NOW)

    assert result["applied"] is True and result["fence_cleared"] is True
    intent = FAKE_DB.reference(f"{OUTBOX_PATH}/{CHAIN}/{RUN}").get()
    assert intent["status"] == "REJECTED"
    assert intent["admin_reconciliation"]["event_id"] == plan["event_id"]
    assert "needs_manual_check" not in intent
    assert "cashflow_abandoned" not in intent
    lock = FAKE_DB.reference(f"{DISPATCH_LOCK_PATH}/{CHAIN}").get()
    assert "inflight_run_id" not in lock
    assert lock["last_admin_reconciliation_id"] == plan["event_id"]
    assert lock["admin_reconciliation_events"][plan["event_id"]] == {
        "schema": "admin_fence_clear_v1",
        "event_id": plan["event_id"],
        "run_id": RUN,
        "cleared_at": "2026-08-02T09:00:00Z",
    }
    audit = FAKE_DB.reference(
        f"{admin.ADMIN_AUDIT_PATH}/{CHAIN}/{RUN}/{plan['event_id']}").get()
    assert set(audit) == {"authorized", "applied", "completed"}
    assert all(item["event_id"] == plan["event_id"] for item in audit.values())
    # inspect + post-reservation recheck; no preview/place/cancel/replace surface
    assert len(client.order_v3.get_order_detail.calls) == 2
    assert client.order_v3.preview_order.calls == []
    assert client.order_v3.place_order.calls == []


def test_successful_replay_is_a_noop_and_does_not_append_a_second_audit():
    _seed_zero_fill()
    client = _client(_detail())
    first = admin.inspect_reconciliation(CHAIN, RUN, client, now_utc=NOW)
    admin.acknowledge_reconciliation(
        first, first["confirmation_phrase"], client,
        operator="alice", now_utc=NOW)
    before = FAKE_DB.reference().get()

    replay = admin.inspect_reconciliation(
        CHAIN, RUN, client, now_utc=NOW + timedelta(minutes=1))
    assert replay["allowed"] is True and replay["replay"] is True
    result = admin.acknowledge_reconciliation(
        replay, replay["confirmation_phrase"], client,
        operator="bob", now_utc=NOW + timedelta(minutes=1))
    assert result["applied"] is False and result["replay_noop"] is True
    assert FAKE_DB.reference().get() == before


def test_first_event_map_clear_migrates_the_legacy_scalar_marker():
    legacy_event = "e" * 64
    next_event = "d" * 64
    next_run = "b" * 32
    token = "admin-token"
    FAKE_DB.reference(f"{DISPATCH_LOCK_PATH}/{CHAIN}").set({
        "generation": 9,
        "inflight_run_id": next_run,
        "owner": "admin-reconcile:test",
        "claim_token": token,
        "lease_until": "2026-08-02T09:05:00Z",
        "admin_reconciliation_id": next_event,
        "last_admin_reconciliation_id": legacy_event,
        "last_cleared_run_id": "legacy-run",
        "cleared_at": "2026-08-02T08:00:00Z",
    })

    assert admin._clear_fence({
        "chain_key": CHAIN,
        "run_id": next_run,
        "event_id": next_event,
    }, token, NOW) is True

    lock = FAKE_DB.reference(f"{DISPATCH_LOCK_PATH}/{CHAIN}").get()
    assert lock["last_admin_reconciliation_id"] == next_event
    assert admin._admin_clear_recorded(lock, legacy_event, "legacy-run") is True
    assert admin._admin_clear_recorded(lock, next_event, next_run) is True
    assert lock["admin_reconciliation_events"][legacy_event] == {
        "schema": "admin_fence_clear_legacy_v1",
        "event_id": legacy_event,
        "migrated_at": "2026-08-02T09:00:00Z",
    }


def test_two_concurrent_admins_have_exactly_one_winner(monkeypatch):
    _seed_zero_fill()
    client = _client(_detail())
    plan = admin.inspect_reconciliation(CHAIN, RUN, client, now_utc=NOW)
    transaction_lock = threading.RLock()
    original = FakeReference.transaction

    def atomic(self, callback):
        with transaction_lock:
            return original(self, callback)

    monkeypatch.setattr(FakeReference, "transaction", atomic)

    def attempt(name):
        try:
            result = admin.acknowledge_reconciliation(
                plan, plan["confirmation_phrase"], client,
                operator=name, now_utc=NOW)
            return ("won", result)
        except admin.ReconcileRefusal as exc:
            return ("lost", str(exc))

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, ("alice", "bob")))
    assert [kind for kind, _ in outcomes].count("won") == 1
    assert [kind for kind, _ in outcomes].count("lost") == 1
    assert FAKE_DB.reference(
        f"{OUTBOX_PATH}/{CHAIN}/{RUN}").get()["admin_reconciled"] is True


def test_crash_after_intent_ack_keeps_fence_and_replay_finishes(monkeypatch):
    _seed_zero_fill()
    client = _client(_detail())
    plan = admin.inspect_reconciliation(CHAIN, RUN, client, now_utc=NOW)
    original_clear = admin._clear_fence
    monkeypatch.setattr(admin, "_clear_fence", lambda *_a, **_kw: False)

    with pytest.raises(admin.ReconcileIncomplete, match="replay"):
        admin.acknowledge_reconciliation(
            plan, plan["confirmation_phrase"], client,
            operator="alice", now_utc=NOW)
    lock = FAKE_DB.reference(f"{DISPATCH_LOCK_PATH}/{CHAIN}").get()
    assert lock["inflight_run_id"] == RUN
    assert lock["owner"].startswith("admin-reconcile:")
    pending = FAKE_DB.reference(f"{OUTBOX_PATH}/{CHAIN}/{RUN}").get()
    assert pending["admin_reconciliation_pending"] is True
    assert pending["needs_manual_check"] is True
    assert pending.get("admin_reconciled") is not True

    monkeypatch.setattr(admin, "_clear_fence", original_clear)
    later = NOW + timedelta(minutes=6)
    recovery = admin.inspect_reconciliation(CHAIN, RUN, client, now_utc=later)
    assert recovery["allowed"] is True
    result = admin.acknowledge_reconciliation(
        recovery, recovery["confirmation_phrase"], client,
        operator="bob", now_utc=later)
    assert result["fence_cleared"] is True
    assert "inflight_run_id" not in FAKE_DB.reference(
        f"{DISPATCH_LOCK_PATH}/{CHAIN}").get()


def test_submitted_intent_admin_ack_forces_manual_guard_until_fence_clear(
        monkeypatch):
    _seed_zero_fill(intent_overrides={
        "status": "SUBMITTED",
        "needs_manual_check": False,
    })
    client = _client(_detail(status="REJECTED"))
    plan = admin.inspect_reconciliation(CHAIN, RUN, client, now_utc=NOW)
    assert plan["allowed"] is True, plan["blockers"]
    monkeypatch.setattr(admin, "_clear_fence", lambda *_a, **_kw: False)

    with pytest.raises(admin.ReconcileIncomplete, match="replay"):
        admin.acknowledge_reconciliation(
            plan, plan["confirmation_phrase"], client,
            operator="alice", now_utc=NOW)

    pending = FAKE_DB.reference(f"{OUTBOX_PATH}/{CHAIN}/{RUN}").get()
    assert pending["admin_reconciliation_pending"] is True
    assert pending["needs_manual_check"] is True
    assert _chain_fence_can_clear(pending) is False
    pending.pop("needs_manual_check")
    assert _chain_fence_can_clear(pending) is False


def test_crash_after_fence_clear_replay_finalizes_intent_and_completed_audit(
        monkeypatch):
    _seed_zero_fill()
    client = _client(_detail())
    plan = admin.inspect_reconciliation(CHAIN, RUN, client, now_utc=NOW)
    original_finalize = admin._finalize_intent_ack
    calls = {"n": 0}

    def crash_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise admin.ReconcileIncomplete("simulated post-clear crash; replay")
        return original_finalize(*args, **kwargs)

    monkeypatch.setattr(admin, "_finalize_intent_ack", crash_once)
    with pytest.raises(admin.ReconcileIncomplete, match="post-clear"):
        admin.acknowledge_reconciliation(
            plan, plan["confirmation_phrase"], client,
            operator="alice", now_utc=NOW)

    lock = FAKE_DB.reference(f"{DISPATCH_LOCK_PATH}/{CHAIN}").get()
    assert "inflight_run_id" not in lock
    pending = FAKE_DB.reference(f"{OUTBOX_PATH}/{CHAIN}/{RUN}").get()
    assert pending["admin_reconciliation_pending"] is True
    assert pending["needs_manual_check"] is True
    event_path = (f"{admin.ADMIN_AUDIT_PATH}/{CHAIN}/{RUN}/"
                  f"{plan['event_id']}")
    assert set(FAKE_DB.reference(event_path).get()) == {"authorized", "applied"}

    # A later chain repair may replace the scalar status marker.  The older
    # event's durable map entry must still make its post-clear crash replayable.
    later_event = "f" * 64
    markers = dict(lock["admin_reconciliation_events"])
    markers[later_event] = {
        "schema": "admin_fence_clear_v1",
        "event_id": later_event,
        "run_id": "later-run",
        "cleared_at": "2026-08-02T09:00:30Z",
    }
    FAKE_DB.reference(f"{DISPATCH_LOCK_PATH}/{CHAIN}").update({
        "last_admin_reconciliation_id": later_event,
        "last_cleared_run_id": "later-run",
        "admin_reconciliation_events": markers,
    })

    replay = admin.inspect_reconciliation(
        CHAIN, RUN, client, now_utc=NOW + timedelta(minutes=1))
    assert replay["allowed"] is True and replay["replay"] is True
    result = admin.acknowledge_reconciliation(
        replay, replay["confirmation_phrase"], client,
        operator="bob", now_utc=NOW + timedelta(minutes=1))
    assert result["replay_noop"] is True
    final = FAKE_DB.reference(f"{OUTBOX_PATH}/{CHAIN}/{RUN}").get()
    assert final["admin_reconciled"] is True
    assert "admin_reconciliation_pending" not in final
    assert "needs_manual_check" not in final
    assert set(FAKE_DB.reference(event_path).get()) == {
        "authorized", "applied", "completed"}
