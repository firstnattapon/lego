"""Economic dispatch budget regressions; all broker calls are test doubles."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import execution_service as execution
import main
from config import load_runtime_config
from conftest import FAKE_DB, fake_trade_client
from lego_one_row import Config, build_decision, compute_row
from lego_outbox import (claim_chain_dispatch, claim_intent, put_intent,
                         read_intent)
from lego_state import AUDIT_PATH, chain_key


NOW = datetime(2026, 9, 21, 15, 0, tzinfo=timezone.utc)
STAMP = NOW.isoformat()
CFG = Config("PFE", 10000, diff=25, strategy_id="shannon_demon_lego_v2",
             decimal_precision=5, quantity_increment=0.00001)


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    FAKE_DB.store.clear()
    execution.configure(main)
    monkeypatch.setenv("WEBULL_ENV", "UAT")

    class FixedNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(execution, "datetime", FixedNow)


def intent_for(cfg=CFG, price=27.68, holdings=181):
    decision = build_decision(cfg, price, holdings, 1)
    return {
        "run_id": "a" * 32, "chain_key": chain_key(cfg),
        "status": "PENDING_DISPATCH", "row_status": decision.status,
        "symbol": cfg.symbol, "side": decision.side, "quantity": decision.quantity,
        "step": 0, "signal": 1, "decision_price": price,
        "decision_holdings": holdings, "decision_time": STAMP,
        "created_at": STAMP, "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
    }


def snapshot(price=27.6843, holdings=181):
    return {"price": price, "holdings": holdings,
            "quote_time": STAMP, "captured_at": STAMP}


def check(cfg=CFG, intent=None, fresh=None, **limits):
    return execution._dispatch_quote_safety(
        cfg, intent if intent is not None else intent_for(cfg),
        fresh if fresh is not None else snapshot(), now_utc=NOW,
        max_price_drift_bps=limits.get("max_price_drift_bps", 100),
        max_quote_age_seconds=360, max_decision_age_seconds=360)


@pytest.mark.parametrize("symbol", ["PFE", "AAPL"])
def test_tiny_adverse_buy_move_inside_diff(symbol):
    cfg = replace(CFG, symbol=symbol)
    intent = intent_for(cfg)
    verdict = check(cfg, intent)
    assert verdict["ok"] is True
    assert verdict["reasons"] == []
    assert intent["quantity"] == verdict["intent_quantity"] == 180.27167
    assert verdict["dispatch_safe_quantity"] == 180.21556
    assert verdict["overshoot_quantity"] == 0.05611
    assert verdict["overshoot_notional_usd"] == pytest.approx(1.553366073)
    assert verdict["quantity_tolerance"] == 0.000005
    assert verdict["rounding_notional_usd"] == pytest.approx(0.0001384215)
    assert verdict["max_overshoot_notional_usd"] == pytest.approx(25.0001384215)


@pytest.mark.parametrize("holdings,fresh_price,allowed", [
    (181, 27.7658, False),  # BUY excess about $31, with drift below 100 bps.
    (500, 27.6757, True),   # SELL adverse move, excess about $1.55.
    (500, 27.5942, False),  # SELL excess about $31.
    (181, 27.6757, True),   # Smaller BUY merely under-rebalances.
    (500, 27.6843, True),   # Smaller SELL merely under-rebalances.
])
def test_buy_and_sell_economic_budget(holdings, fresh_price, allowed):
    verdict = check(intent=intent_for(holdings=holdings),
                    fresh=snapshot(fresh_price, holdings))
    assert verdict["ok"] is allowed
    assert ("quantity_would_overshoot" in verdict["reasons"]) is (not allowed)
    assert verdict["price_drift_bps"] < 100
    if not allowed:
        assert verdict["overshoot_notional_usd"] == pytest.approx(31, abs=0.01)


@pytest.mark.parametrize("diff,allowed", [
    (250, True),               # Exactly diff.
    (249.9995, True),          # Exactly diff + half a quantity quantum * $100.
    (249.9995000001, True),    # Just inside the complete rounding budget.
    (249.9994999999, False),   # Just outside; no arbitrary float epsilon.
    (0, False),               # Zero diff retains the original strict budget.
])
def test_exact_economic_boundary(diff, allowed):
    cfg = replace(CFG, fix_c=1000, diff=diff)
    verdict = check(cfg, intent_for(cfg, price=80, holdings=1), snapshot(100, 1),
                    max_price_drift_bps=3000)
    assert verdict["overshoot_notional_usd"] == 250
    assert verdict["ok"] is allowed
    assert verdict["reasons"] == ([] if allowed else ["quantity_would_overshoot"])


@pytest.mark.parametrize("fresh_price", [55.25, 56])
def test_side_changes_or_passes_remain_blocked(fresh_price):
    verdict = check(fresh=snapshot(fresh_price), max_price_drift_bps=20000)
    assert "side_changed_or_pass" in verdict["reasons"]


def test_small_overshoot_cannot_override_other_guards():
    assert check(max_price_drift_bps=1)["reasons"] == ["price_drift_limit"]
    stale = (NOW - timedelta(seconds=361)).isoformat()
    assert check(intent={**intent_for(), "decision_time": stale})["reasons"] == [
        "decision_age_limit"]
    assert check(fresh={**snapshot(), "quote_time": stale})["reasons"] == [
        "quote_age_limit"]
    assert check(intent={**intent_for(), "quantity": 180.28})["reasons"] == [
        "intent_decision_mismatch"]


def dispatch_fixture(monkeypatch, *, environment="UAT", final_price=27.6843,
                     final_holdings=181, fractionable=True, buying_power=10000):
    env = {"LEGO_SYMBOL": "PFE", "LEGO_FIX_C": "10000", "LEGO_DIFF": "25",
           "WEBULL_ENV": environment, "WEBULL_ACCOUNT_ID": "dispatch-test",
           "LEGO_MODE": "trade", "LEGO_ACTIVE": "true",
           "LEGO_CANDIDATE_HASH": "test-candidate"}
    env["LEGO_RELEASE_AUTHORIZATION"] = load_runtime_config(
        env).deployment.expected_release_binding
    runtime = load_runtime_config(env)
    monkeypatch.setenv("WEBULL_ENV", environment)
    intent = intent_for()
    ck, run_id = intent["chain_key"], intent["run_id"]
    row = compute_row(CFG, snapshot(27.68), None, dna_step=0)
    committed = {k: v for k, v in row.items() if k != "_meta"}
    FAKE_DB.reference(f"webull_lego_rows/{run_id}").set({**committed, "committed": True})
    intent["instrument_capability"] = {
        "symbol": "PFE", "fractionable": fractionable,
        "quantity_increment": "0.00001", "decimal_precision": 5,
    }
    put_intent(ck, run_id, intent)
    intent = claim_intent(ck, run_id, "test-worker", now_utc=NOW)
    claim = claim_chain_dispatch(ck, "test-worker", now_utc=NOW)
    snapshots = iter([snapshot(), snapshot(final_price, final_holdings)])
    monkeypatch.setattr(execution, "fetch_open_orders", lambda *_: [])
    monkeypatch.setattr(execution, "fetch_snapshot", lambda *_: next(snapshots))
    monkeypatch.setattr(execution, "fetch_buying_power", lambda *_: Decimal(buying_power))
    # Real fractional gate, payload builder, Preview parser, funding validator,
    # submit gate, and database fences; only broker I/O/polling are doubled.
    client = fake_trade_client(
        preview={"estimated_cost": "4990.48", "estimated_transaction_fee": "1"},
        place={"client_order_id": run_id, "order_id": "test-broker-order"})
    monkeypatch.setattr(execution, "_poll_order_status",
                        lambda *_: {"status": "SUBMITTED", "filled_quantity": 0})
    return runtime, intent, claim, client, committed


@pytest.mark.parametrize("environment", ["UAT", "PROD"])
def test_original_quantity_reaches_preview_and_place_once(monkeypatch, environment):
    runtime, intent, claim, client, committed = dispatch_fixture(
        monkeypatch, environment=environment)
    result = execution._dispatch_or_reconcile_one(
        client, object(), CFG, intent, claim, runtime)
    assert result["status"] == "SUBMITTED"
    previewed = client.order_v3.preview_order.calls
    placed = client.order_v3.place_order.calls
    assert len(previewed) == len(placed) == 1
    assert previewed[0] == placed[0]
    stored = read_intent(intent["chain_key"], intent["run_id"])
    assert stored["order_payload"][0]["quantity"] == "180.27167"
    assert stored["quantity"] == 180.27167
    assert stored["place_attempted"] is True
    row = FAKE_DB.reference(f"webull_lego_rows/{intent['run_id']}").get()
    assert row["จำนวนสั่ง (หุ้น)"] == committed["จำนวนสั่ง (หุ้น)"] == 180.27167
    audit = FAKE_DB.reference(f"{AUDIT_PATH}/{intent['run_id']}").get()
    evidence = audit["dispatch_quote_check"]
    assert evidence["dispatch_check_phase"] == "pre_preview"
    assert evidence["ok"] is True
    assert evidence["overshoot_notional_usd"] == pytest.approx(1.553366073)
    # A repeat with the same claimed intent cannot pass the durable Place fence.
    monkeypatch.setattr(execution, "fetch_snapshot", lambda *_: snapshot())
    execution._dispatch_or_reconcile_one(client, object(), CFG, intent, claim, runtime)
    assert len(client.order_v3.place_order.calls) == 1


@pytest.mark.parametrize("options,status,preview_count,reason", [
    ({"final_price": 27.7658}, "SUPPRESSED_STATE_CHANGED", 1, "quantity_would_overshoot"),
    ({"final_holdings": 182}, "SUPPRESSED_STATE_CHANGED", 1, None),
    ({"fractionable": False}, "NOT_PLACED", 0, None),
    ({"buying_power": 1}, "NOT_PLACED", 1, None),
])
def test_small_move_preserves_downstream_safety(monkeypatch, options, status,
                                              preview_count, reason):
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch, **options)
    result = execution._dispatch_or_reconcile_one(
        client, object(), CFG, intent, claim, runtime)
    assert result["status"] == status
    assert len(client.order_v3.preview_order.calls) == preview_count
    assert client.order_v3.place_order.calls == []
    stored = read_intent(intent["chain_key"], intent["run_id"])
    assert stored["quantity"] == 180.27167
    if reason:
        assert stored["reasons"] == [reason]
        assert stored["dispatch_check_phase"] == "post_preview"
        assert stored["overshoot_notional_usd"] > stored["max_overshoot_notional_usd"]
