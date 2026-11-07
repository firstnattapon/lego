"""Real-money gate: since 2026-11-07 the release binding alone, as in UAT (no PROD ack)."""
import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import execution_service as execution
import observability
import operational_health
import release_horizon as rh
from config import load_runtime_config, release_binding_for
from dna_engine import dna_fingerprint
from test_dispatch_overshoot import CFG, dispatch_fixture, isolate  # noqa: F401

ORIGIN = "2026-09-08T13:30:00Z"
WINDOW = "2026-10-09T20:00:00Z"        # static checks never read the clock
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def bundle(tmp_path, code="bypass:6500"):
    # One file per code: env() builds its default bundle even when a test passes
    # another one, and a shared name made the default overwrite the override.
    path = tmp_path / (code.replace(":", "-") + ".json")
    path.write_text(json.dumps({
        "dna_code": code, "interval_seconds": 900, "origin_utc": ORIGIN,
        "calendar_id": "XNYS-regular", "calendar_fingerprint": "0287d84627be239d",
        "decoder_version": "legacy-v1-test", "decoded_array_sha256": dna_fingerprint(code)}))
    return str(path)


def env(tmp_path, **overrides):
    base = {
        "WEBULL_ENV": "PROD", "WEBULL_ACCOUNT_ID": "test-account",
        "LEGO_SYMBOL": "UBER", "LEGO_FIX_C": "10000", "LEGO_DIFF": "25",
        "LEGO_MODE": "trade", "LEGO_ACTIVE": "true", "LEGO_ALLOW_FRACTIONAL": "false",
        "LEGO_DNA_BUNDLE": bundle(tmp_path), "LEGO_CANDIDATE_HASH": "c" * 64,
        "LEGO_MAX_ORDER_QUANTITY": "20", "LEGO_MAX_ORDER_NOTIONAL_USD": "1000",
        "LEGO_MAX_SESSION_ORDERS": "10", "LEGO_MAX_SESSION_BUY_ORDERS": "10", "LEGO_TRADING_WINDOW_END": WINDOW,
        "LEGO_SESSION_KEY_MODE": "market_day"}
    base.update(overrides)
    return base


def authorized(values):
    return {**values, "LEGO_RELEASE_AUTHORIZATION": release_binding_for(values)}


def test_production_trade_is_gated_by_the_release_binding_alone(tmp_path):
    """LEGO_PROD_LIVE_ACK was removed (2026-11-07): the binding is the one runtime gate."""
    unauthorized = load_runtime_config(env(tmp_path))
    assert not unauthorized.deployment.release_is_authorized
    assert unauthorized.allows_new_broker_mutation is False
    runtime = load_runtime_config(authorized(env(tmp_path)))
    assert runtime.deployment.release_is_authorized
    assert runtime.allows_new_broker_mutation is True
    assert not hasattr(runtime, "prod_live_gate_open")
    assert not hasattr(rh, "prod_live_ack") and not hasattr(rh, "prod_live_gate_open")


def test_a_leftover_acknowledgement_is_ignored(tmp_path):
    values = authorized(env(tmp_path))
    with_ack = {**values, "LEGO_PROD_LIVE_ACK": "LIVE-PROD-UBER-anything"}
    assert release_binding_for(with_ack) == release_binding_for(values)
    assert load_runtime_config(with_ack).allows_new_broker_mutation is True
    assert not hasattr(load_runtime_config(with_ack).deployment, "prod_live_ack")


@pytest.mark.parametrize("key,value", [
    ("LEGO_MAX_ORDER_NOTIONAL_USD", "900"), ("LEGO_MAX_SESSION_ORDERS", "9"),
    ("LEGO_MAX_SESSION_BUY_ORDERS", "3"), ("LEGO_TRADING_WINDOW_END", "2026-10-10T20:00:00Z"),
    ("LEGO_SYMBOL", "AAPL")])
def test_any_drifted_release_value_closes_the_gate(tmp_path, key, value):
    values = authorized(env(tmp_path))
    assert load_runtime_config({**values, key: value}).allows_new_broker_mutation is False


def test_uat_and_production_share_one_gate(tmp_path):
    uat = authorized(env(tmp_path, WEBULL_ENV="UAT", LEGO_ALLOW_FRACTIONAL="true"))
    assert load_runtime_config(uat).allows_new_broker_mutation is True
    assert load_runtime_config({**uat, "LEGO_RELEASE_AUTHORIZATION": "0" * 64}
                               ).allows_new_broker_mutation is False


def test_production_observe_or_inactive_never_trades(tmp_path):
    values = authorized(env(tmp_path))
    for override in ({"LEGO_MODE": "observe"}, {"LEGO_ACTIVE": "false"}):
        assert load_runtime_config(authorized({**values, **override})
                                   ).allows_new_broker_mutation is False


def test_release_binding_recipe_is_unchanged():
    """The recipe is the one before the PROD gate; only the fifth limit is new.

    Golden values computed with config.py: with the four limits of the commit before
    the PROD gate the recipe still gives its old value; LEGO_MAX_SESSION_BUY_ORDERS
    (2026-11-07) is part of policy_hash(execution_limits), so every release binding
    changed with it and every release must be planned again.
    """
    golden_env = {
        "WEBULL_ENV": "UAT", "WEBULL_ACCOUNT_ID": "test-account", "LEGO_SYMBOL": "UBER",
        "LEGO_FIX_C": "10000", "LEGO_DIFF": "25", "LEGO_MODE": "trade", "LEGO_ACTIVE": "true",
        "LEGO_CANDIDATE_HASH": "c" * 64, "LEGO_MAX_ORDER_QUANTITY": "30",
        "LEGO_MAX_ORDER_NOTIONAL_USD": "1500", "LEGO_MAX_SESSION_ORDERS": "26", "LEGO_MAX_SESSION_BUY_ORDERS": "26",
        "LEGO_TRADING_WINDOW_END": "2026-10-16T20:00:00Z", "LEGO_ALLOW_FRACTIONAL": "true",
        "LEGO_SESSION_KEY_MODE": "market_day", "LEGO_STALE_ORDER_ACTION": "cancel"}
    assert release_binding_for(golden_env) == (
        "f88e613ee3f65f6122f19fc39cfd99068a3694a4f3b2f6f9ca635e21a29b6be2")
    deployment = load_runtime_config(golden_env).deployment
    four = replace(deployment, execution_limits=deployment.execution_limits[:4])
    assert four.expected_release_binding == (
        "15a17bf09119632fb2f746a5213aae22d398f2246cecb0b4218037160ef5fb07")


def test_dispatch_sends_an_authorized_production_intent(monkeypatch):
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch, environment="PROD")
    assert runtime.allows_new_broker_mutation
    result = execution._dispatch_or_reconcile_one(client, object(), CFG, intent, claim, runtime)
    assert result["status"] != "UNSENT_ABORTED"
    assert client.order_v3.place_order.calls
    drifted = replace(runtime, deployment=replace(runtime.deployment, release_authorization="0" * 64))
    assert not drifted.allows_new_broker_mutation


# ------------------------------------------- a closed gate must not be silent
@pytest.fixture
def slot_clock(monkeypatch):
    # main._run_tick exports these from the bundle before any health report.
    monkeypatch.setenv("LEGO_SLOT_SECONDS", "900")
    monkeypatch.setenv("LEGO_DNA_ORIGIN_UTC", ORIGIN)


def blocked_flag(values):
    runtime = load_runtime_config(values)
    return operational_health.report(runtime, {}, {}, now=NOW)["orders_blocked_by_release"]


def test_a_live_deployment_that_cannot_send_orders_says_so(tmp_path, slot_clock):
    live = authorized(env(tmp_path))
    assert blocked_flag(live) is False                                # PROD: the binding is enough
    assert blocked_flag({**live, "LEGO_MAX_ORDER_NOTIONAL_USD": "900"}) is True   # drifted caps
    uat = env(tmp_path, WEBULL_ENV="UAT", LEGO_ALLOW_FRACTIONAL="true")
    assert blocked_flag(authorized(uat)) is False
    assert blocked_flag({**uat, "LEGO_RELEASE_AUTHORIZATION": "0" * 64}) is True          # stale binding


def test_deployments_that_never_intended_to_trade_are_not_flagged(tmp_path, slot_clock):
    base = env(tmp_path)                                               # PROD, no binding: gate closed
    assert blocked_flag(base) is True
    assert blocked_flag({**base, "LEGO_MODE": "observe"}) is False
    assert blocked_flag({**base, "LEGO_ACTIVE": "false"}) is False


def test_the_flag_is_its_own_error_status():
    assert observability.business_status(
        {"operational_health": {"orders_blocked_by_release": True}}, 200) == "RELEASE_UNAUTHORIZED"
    assert observability.severity_for("RELEASE_UNAUTHORIZED") == "ERROR"


def test_the_status_outranks_the_soft_warnings_but_not_a_finished_horizon():
    def status(**flags):
        return observability.business_status(
            {"operational_health": {"orders_blocked_by_release": True, **flags}}, 200)

    assert status(release_expiring=True, token_warning=True, dna_low=True) == "RELEASE_UNAUTHORIZED"
    assert status(release_expired=True) == "RELEASE_EXPIRED"
    assert status(dna_exhausted=True) == "DNA_EXHAUSTED"


def test_minimal_runtimes_without_a_gate_report_not_blocked():
    runtime = SimpleNamespace(
        operator=SimpleNamespace(dna_bundle=SimpleNamespace(
            interval_seconds=900, origin_utc=None, dna_code="bypass:50")),
        deployment=SimpleNamespace(execution_limits=("", "", "", "", "")))
    assert operational_health.report(runtime, {}, {}, now=NOW)["orders_blocked_by_release"] is False
