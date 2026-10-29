"""LEGO_MODE=preview: the order path of a real release up to Preview, and never Place.

The worker runs every read, the Preview and every check a real order passes, then ends
the intent UNSENT_ABORTED before the chain fence. Three locks keep Place out of reach:
that cut, the preview_only stamp an intent is born with, and webull_io refusing Place
under LEGO_MODE=preview. A position or order that changes right after a Preview halts
new orders (docs/PROD_OBSERVE_TH.md). Only broker I/O is a double.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import decision_service
import execution_limits
import execution_service as execution
import lego_outbox as outbox
import main
import observability
import operator_halt
import release_horizon as rh
import webull_io
from config import load_runtime_config
from conftest import FAKE_DB, fake_trade_client
from lego_one_row import READY_SELL, Config
from lego_outbox import DISPATCH_LOCK_PATH, account_symbol_fence_key, read_intent
from test_dispatch_overshoot import CFG, dispatch_fixture, isolate  # noqa: F401  (isolate: autouse)
from test_fractional_quantity_incident import (DECISION_TIME, DISPATCH_PRICE, DISPATCH_TIME,
                                               _frozen, _profile)
from test_prod_live_gate import authorized, env
from tools.trace_audit import TERMINAL_INTENT
from webull_io import parse_instrument_capability

UTC = timezone.utc


def refuse(*_args, **_kwargs):
    raise AssertionError("preview reached the money boundary")


def forbid_the_money_boundary(monkeypatch):
    monkeypatch.setattr(execution, "fence_chain_dispatch", refuse)
    monkeypatch.setattr(execution, "begin_place_attempt", refuse)
    monkeypatch.setattr(execution_limits, "reserve_attempt", refuse)


def halt_of(symbol):
    return operator_halt.status(webull_io.runtime_identity_fingerprint(), symbol)


# ------------------------------------------------------------------------ config
def test_preview_needs_the_release_but_never_the_real_money_acknowledgement(tmp_path):
    values = env(tmp_path, LEGO_MODE="preview")
    unauthorized = load_runtime_config(values)
    assert unauthorized.operator.allows_new_intents and unauthorized.operator.preview_only
    assert unauthorized.allows_new_broker_mutation is False          # no release, no intent
    runtime = load_runtime_config(authorized(values))
    assert runtime.allows_new_broker_mutation is True
    assert runtime.prod_live_gate_open is False                       # the live gate stays shut
    # The binding includes the mode: a preview release never authorizes trade.
    trade = load_runtime_config({**authorized(values), "LEGO_MODE": "trade"})
    assert trade.deployment.release_is_authorized is False


def test_a_preview_release_has_no_acknowledgement_to_echo(tmp_path):
    runtime = load_runtime_config(authorized(env(tmp_path, LEGO_MODE="preview")))
    inputs = rh.inputs_from_runtime(runtime)
    assert inputs.trading and not inputs.live                  # judged like trade, never live
    assert set(rh.prod_live_acks(inputs, runtime.deployment.expected_release_binding).values()) == {None}


def test_inactive_preview_creates_nothing(tmp_path):
    runtime = load_runtime_config(authorized(env(tmp_path, LEGO_MODE="preview", LEGO_ACTIVE="false")))
    assert not runtime.operator.allows_new_intents and not runtime.allows_new_broker_mutation


# ------------------------------------------------------------------------ worker
def test_preview_runs_the_order_path_to_its_preview_and_never_places(monkeypatch):
    runtime, intent, claim, client, _ = dispatch_fixture(
        monkeypatch, environment="PROD", mode="preview")
    forbid_the_money_boundary(monkeypatch)
    result = execution._dispatch_or_reconcile_one(client, object(), CFG, intent, claim, runtime)
    assert result == {"run_id": intent["run_id"], "status": "UNSENT_ABORTED",
                      "preview_only": True, "preview_fee_pct": "0.0200"}   # 1 / 4990.48
    assert len(client.order_v3.preview_order.calls) == 1
    assert client.order_v3.place_order.calls == []
    stored = read_intent(intent["chain_key"], intent["run_id"])
    assert stored["status"] == "UNSENT_ABORTED" and stored["preview_only"] is True
    assert stored["dispatch_check_phase"] == "preview_cut"
    assert stored["preview_result"]["estimated_transaction_fee"] == "1"
    assert stored["funding_check"]["required_cash"] == "4991.48"
    assert not stored.get("place_attempted")
    assert halt_of(CFG.symbol).get("halted") is not True


def test_an_intent_born_in_preview_is_never_placed_by_a_trade_deployment(monkeypatch):
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch, environment="PROD")
    assert runtime.allows_new_broker_mutation and runtime.prod_live_gate_open   # live, with ack
    forbid_the_money_boundary(monkeypatch)
    result = execution._dispatch_or_reconcile_one(
        client, object(), CFG, {**intent, "preview_only": True}, claim, runtime)
    assert result["status"] == "UNSENT_ABORTED" and result["preview_only"] is True
    assert client.order_v3.place_order.calls == []


def test_the_caps_still_bind_at_the_preview_cut(monkeypatch):
    runtime, intent, claim, client, _ = dispatch_fixture(
        monkeypatch, environment="PROD", mode="preview")
    forbid_the_money_boundary(monkeypatch)
    calls = []
    real_check = execution_limits.ExecutionLimits.check

    def expire_at_the_cut(self, *args, **kwargs):           # the pre-Preview check passes
        calls.append(args)
        if len(calls) > 1:
            raise execution_limits.ExecutionLimitError("trading window expired")
        return real_check(self, *args, **kwargs)

    monkeypatch.setattr(execution_limits.ExecutionLimits, "check", expire_at_the_cut)
    result = execution._dispatch_or_reconcile_one(client, object(), CFG, intent, claim, runtime)
    assert result["status"] == "NOT_PLACED" and result["execution_limit_blocked"] is True
    assert len(client.order_v3.preview_order.calls) == 1 and not client.order_v3.place_order.calls


def test_an_order_right_after_a_preview_halts_new_orders(monkeypatch):
    runtime, intent, _, client, _ = dispatch_fixture(
        monkeypatch, environment="PROD", mode="preview")
    forbid_the_money_boundary(monkeypatch)
    # The worker holds the account+symbol lease, where the open-order blocker is recorded.
    scope = account_symbol_fence_key(execution.runtime_identity_fingerprint(), CFG.symbol)
    claim = {**outbox.claim_chain_dispatch(scope, "test-worker", now_utc=datetime.now(UTC)),
             "dispatch_scope_key": scope}
    order = {"client_order_id": "x" * 32, "symbol": "PFE", "side": "BUY",
             "status": "SUBMITTED", "total_quantity": "1", "filled_quantity": "0"}
    reads = iter([[], [order]])                  # none before the Preview, one right after it
    monkeypatch.setattr(execution, "fetch_open_orders", lambda *_: next(reads))
    result = execution._dispatch_or_reconcile_one(client, object(), CFG, intent, claim, runtime)
    assert result["status"] == "SUPPRESSED_ACTIVE_ORDER" and result["preview_tripwire"] is True
    halt = halt_of(CFG.symbol)
    assert halt["halted"] is True and halt["set_by"] == execution.PREVIEW_TRIPWIRE_OPERATOR
    assert client.order_v3.place_order.calls == []


def test_a_position_that_moves_across_a_preview_halts_new_orders(monkeypatch):
    runtime, intent, claim, client, _ = dispatch_fixture(
        monkeypatch, environment="PROD", mode="preview", final_holdings=182)
    forbid_the_money_boundary(monkeypatch)
    result = execution._dispatch_or_reconcile_one(client, object(), CFG, intent, claim, runtime)
    assert result["status"] == "SUPPRESSED_STATE_CHANGED" and result["preview_tripwire"] is True
    assert halt_of(CFG.symbol)["halted"] is True


def test_trade_keeps_its_behaviour_when_the_position_moves(monkeypatch):
    runtime, intent, claim, client, _ = dispatch_fixture(
        monkeypatch, environment="PROD", final_holdings=182)
    result = execution._dispatch_or_reconcile_one(client, object(), CFG, intent, claim, runtime)
    assert result["status"] == "SUPPRESSED_STATE_CHANGED" and "preview_tripwire" not in result
    assert halt_of(CFG.symbol).get("halted") is not True


def test_place_refuses_under_preview_before_any_broker_call(monkeypatch):
    monkeypatch.setenv("LEGO_MODE", "preview")
    client = fake_trade_client(place={"order_id": "never"})
    with pytest.raises(webull_io.WebullConfigError, match="never places"):
        webull_io.place_market_order(client, [{"client_order_id": "a" * 32}])
    assert client.order_v3.place_order.calls == []


# ------------------------------------------------------------- one tick, end to end
def preview_tick(monkeypatch):
    """Decide a PROD slot under LEGO_MODE=preview, then run the worker on it."""
    webull_io.reset_clients()
    execution.configure(main)
    decision_service.configure(main)
    values = {
        "LEGO_SYMBOL": "UBER", "LEGO_FIX_C": "10000", "LEGO_DIFF": "25",
        "WEBULL_ENV": "PROD", "WEBULL_ACCOUNT_ID": "test-account",
        "LEGO_DNA_CODE": "bypass:1000", "LEGO_DNA_CLOCK_MODE": "market",
        "LEGO_SLOT_SECONDS": "900", "LEGO_DNA_ORIGIN_UTC": "2026-10-05T13:30:00Z",
        "LEGO_MODE": "preview", "LEGO_ACTIVE": "true", "LEGO_CANDIDATE_HASH": "candidate",
        "LEGO_ALLOW_FRACTIONAL": "true", "FIREBASE_DB_URL": "https://test.firebaseio.com",
        "LEGO_MAX_ORDER_QUANTITY": "1000", "LEGO_MAX_ORDER_NOTIONAL_USD": "1000",
        "LEGO_MAX_SESSION_ORDERS": "10", "LEGO_TRADING_WINDOW_END": "2030-01-01T00:00:00Z",
    }
    values["LEGO_RELEASE_AUTHORIZATION"] = load_runtime_config(
        values).deployment.expected_release_binding
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    runtime = load_runtime_config(values)
    cfg = Config("UBER", 10000.0, 25.0, "bypass:1000", "shannon_demon_lego_v2")
    token = {"ok": True, "ready": True, "status": "NORMAL", "secret_configured": True,
             "token_storage": "SECRET_MANAGER",
             "expires_at": (datetime.now(UTC) + timedelta(days=14)).isoformat()}

    monkeypatch.setattr(decision_service, "datetime", _frozen(DECISION_TIME))
    monkeypatch.setattr(decision_service, "build_clients", lambda: (object(), object()))
    monkeypatch.setattr(decision_service, "fetch_instrument_capability",
                        lambda *_a: parse_instrument_capability(_profile(), "UBER"))
    monkeypatch.setattr(decision_service, "token_health", lambda: token)
    monkeypatch.setattr(decision_service, "fetch_snapshot", lambda *_a: {
        "price": 68.68, "holdings": 147.13455,
        "captured_at": DECISION_TIME.isoformat(), "quote_time": DECISION_TIME.isoformat()})
    body, code = decision_service.run_decision(None, runtime, cfg)
    assert code == 200 and body["status"] == READY_SELL, body
    born = read_intent(main.chain_key(cfg), body["run_id"])

    client = fake_trade_client(
        preview={"estimated_cost": "106.15", "estimated_transaction_fee": "1.14"},
        place={"order_id": "never"})
    monkeypatch.setattr(execution, "datetime", _frozen(DISPATCH_TIME))
    monkeypatch.setattr(outbox, "datetime", _frozen(DISPATCH_TIME))
    monkeypatch.setattr(execution, "build_clients", lambda: (client, object()))
    monkeypatch.setattr(execution, "fetch_open_orders", lambda *_a: [])
    monkeypatch.setattr(execution, "fetch_snapshot", lambda *_a: {
        "price": DISPATCH_PRICE, "holdings": 147.13455,
        "quote_time": DISPATCH_TIME.isoformat(), "captured_at": DISPATCH_TIME.isoformat()})
    monkeypatch.setattr(execution, "fetch_buying_power", lambda *_a: Decimal("100000"))
    monkeypatch.setattr(execution, "token_health", lambda: token)
    worker = execution._run_order_worker(cfg, limit=1, runtime=runtime)
    return body, born, worker, client, cfg


def test_one_preview_tick_end_to_end_leaves_no_fence_quota_or_order(monkeypatch):
    body, born, worker, client, cfg = preview_tick(monkeypatch)
    assert born["status"] == "PENDING_DISPATCH" and born["preview_only"] is True   # the decision stamps it
    stored = read_intent(main.chain_key(cfg), body["run_id"])
    assert stored["status"] == "UNSENT_ABORTED"
    assert worker["results"] == [{"run_id": body["run_id"], "status": "UNSENT_ABORTED",
                                  "preview_only": True, "preview_fee_pct": "1.0740"}]
    assert len(client.order_v3.preview_order.calls) == 1
    assert client.order_v3.place_order.calls == []
    scope = account_symbol_fence_key(webull_io.runtime_identity_fingerprint(), "UBER")
    fence = FAKE_DB.reference(f"{DISPATCH_LOCK_PATH}/{scope}").get() or {}
    assert not fence.get("inflight_run_id") and "execution_session" not in fence


# ------------------------------------------------------------------ logs and audit
def test_the_tick_log_carries_the_preview_result(capsys):
    body = {"pipeline_status": "TICK_OK", "decision": {}, "recovery": {"results": []},
            "dispatch": {"results": [{"run_id": "r", "status": "UNSENT_ABORTED",
                                      "preview_only": True, "preview_fee_pct": "1.0740"}]}}
    observability.emit_tick(body, 200)
    event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert event["execution"][0]["preview_only"] is True
    assert event["execution"][0]["preview_fee_pct"] == "1.0740"
    assert event["severity"] not in {"ERROR", "WARNING"}


def test_a_preview_intent_is_terminal_for_the_trace_audit():
    assert "UNSENT_ABORTED" in TERMINAL_INTENT
