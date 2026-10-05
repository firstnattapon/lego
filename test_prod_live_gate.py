"""Real-money gate: a production release must echo the ack derived from itself."""
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
        "LEGO_MAX_SESSION_ORDERS": "10", "LEGO_TRADING_WINDOW_END": WINDOW,
        "LEGO_SESSION_KEY_MODE": "market_day"}
    base.update(overrides)
    return base


def authorized(values):
    return {**values, "LEGO_RELEASE_AUTHORIZATION": release_binding_for(values)}


def acked(values, funding="prefunded"):
    runtime = load_runtime_config(values)
    ack = rh.prod_live_acks(rh.inputs_from_runtime(runtime),
                            runtime.deployment.expected_release_binding)[funding]
    return {**values, "LEGO_PROD_LIVE_ACK": ack}


def test_production_trade_needs_the_acknowledgement(tmp_path):
    runtime = load_runtime_config(authorized(env(tmp_path)))
    assert runtime.deployment.release_is_authorized          # the binding alone was enough before
    assert runtime.prod_live_gate_open is False
    assert runtime.allows_new_broker_mutation is False


def test_matching_acknowledgement_opens_the_gate(tmp_path):
    runtime = load_runtime_config(acked(authorized(env(tmp_path))))
    assert runtime.deployment.prod_live_ack.startswith("LIVE-PROD-UBER-q20-n1000-o10-20261009T2000Z-prefunded-")
    assert runtime.prod_live_gate_open and runtime.allows_new_broker_mutation


def test_ack_cannot_be_reused_for_a_different_release(tmp_path):
    first = acked(authorized(env(tmp_path)))
    other = authorized(env(tmp_path, LEGO_MAX_ORDER_NOTIONAL_USD="900"))
    reused = {**other, "LEGO_PROD_LIVE_ACK": first["LEGO_PROD_LIVE_ACK"]}
    assert load_runtime_config(reused).allows_new_broker_mutation is False
    assert load_runtime_config(acked(other)).allows_new_broker_mutation is True


@pytest.mark.parametrize("tamper", [lambda s: s[:-1] + ("0" if s[-1] != "0" else "1"),
                                    lambda s: s.lower(), lambda s: s.replace("PROD", "UAT"),
                                    lambda s: s[:-1]])
def test_a_tampered_acknowledgement_is_refused(tmp_path, tamper):
    values = acked(authorized(env(tmp_path)))
    values["LEGO_PROD_LIVE_ACK"] = tamper(values["LEGO_PROD_LIVE_ACK"])
    assert load_runtime_config(values).allows_new_broker_mutation is False


def test_acknowledgement_does_not_replace_the_release_authorization(tmp_path):
    values = acked(env(tmp_path))                              # ack but no LEGO_RELEASE_AUTHORIZATION
    runtime = load_runtime_config(values)
    assert runtime.prod_live_gate_open and not runtime.deployment.release_is_authorized
    assert runtime.allows_new_broker_mutation is False


def test_an_unfit_production_release_cannot_be_acknowledged(tmp_path):
    values = authorized(env(tmp_path, LEGO_MAX_ORDER_NOTIONAL_USD="3000"))   # 30% of principal
    runtime = load_runtime_config(values)
    assert set(rh.prod_live_acks(rh.inputs_from_runtime(runtime),
                                 runtime.deployment.expected_release_binding).values()) == {None}
    forged = "LIVE-PROD-UBER-q20-n3000-o10-20261009T2000Z-prefunded-" + (
        runtime.deployment.expected_release_binding[:16])
    assert load_runtime_config({**values, "LEGO_PROD_LIVE_ACK": forged}).allows_new_broker_mutation is False


def test_dna_ending_before_the_window_cannot_be_acknowledged(tmp_path):
    values = authorized(env(tmp_path, LEGO_DNA_BUNDLE=bundle(tmp_path, "bypass:500")))
    runtime = load_runtime_config(values)
    assert set(rh.prod_live_acks(rh.inputs_from_runtime(runtime),
                                 runtime.deployment.expected_release_binding).values()) == {None}


def test_funding_release_is_acknowledged_only_in_its_own_mode(tmp_path):
    values = authorized(env(tmp_path, LEGO_MAX_ORDER_QUANTITY="200",
                            LEGO_MAX_ORDER_NOTIONAL_USD="12500", LEGO_MAX_SESSION_ORDERS="2"))
    runtime = load_runtime_config(values)
    acks = rh.prod_live_acks(rh.inputs_from_runtime(runtime), runtime.deployment.expected_release_binding)
    assert acks["prefunded"] is None and "-initial-funding-" in acks["initial-funding"]
    assert load_runtime_config(acked(values, "initial-funding")).allows_new_broker_mutation
    steady_form = acks["initial-funding"].replace("initial-funding", "prefunded")
    assert load_runtime_config({**values, "LEGO_PROD_LIVE_ACK": steady_form}).allows_new_broker_mutation is False


def test_uat_is_not_affected(tmp_path):
    values = authorized(env(tmp_path, WEBULL_ENV="UAT", LEGO_MAX_ORDER_NOTIONAL_USD="36000",
                            LEGO_ALLOW_FRACTIONAL="true"))
    runtime = load_runtime_config(values)
    assert runtime.prod_live_gate_open and runtime.allows_new_broker_mutation


def test_production_observe_or_inactive_never_trades_even_with_an_ack(tmp_path):
    base = acked(authorized(env(tmp_path)))
    assert load_runtime_config({**base, "LEGO_MODE": "observe"}).allows_new_broker_mutation is False
    assert load_runtime_config({**base, "LEGO_ACTIVE": "false"}).allows_new_broker_mutation is False


def test_the_acknowledgement_is_not_part_of_the_release_binding(tmp_path):
    values = env(tmp_path)
    assert release_binding_for(values) == release_binding_for({**values, "LEGO_PROD_LIVE_ACK": "x"})


def test_release_binding_recipe_is_unchanged():
    """Golden value computed with config.py at the commit before the PROD gate."""
    golden_env = {
        "WEBULL_ENV": "UAT", "WEBULL_ACCOUNT_ID": "test-account", "LEGO_SYMBOL": "UBER",
        "LEGO_FIX_C": "10000", "LEGO_DIFF": "25", "LEGO_MODE": "trade", "LEGO_ACTIVE": "true",
        "LEGO_CANDIDATE_HASH": "c" * 64, "LEGO_MAX_ORDER_QUANTITY": "30",
        "LEGO_MAX_ORDER_NOTIONAL_USD": "1500", "LEGO_MAX_SESSION_ORDERS": "26",
        "LEGO_TRADING_WINDOW_END": "2026-10-16T20:00:00Z", "LEGO_ALLOW_FRACTIONAL": "true",
        "LEGO_SESSION_KEY_MODE": "market_day", "LEGO_STALE_ORDER_ACTION": "cancel"}
    assert release_binding_for(golden_env) == (
        "15a17bf09119632fb2f746a5213aae22d398f2246cecb0b4218037160ef5fb07")


def test_the_gate_never_raises_and_fails_closed():
    assert rh.prod_live_gate_open(SimpleNamespace()) is False
    broken = SimpleNamespace(operator=None, deployment=SimpleNamespace(prod_live_ack="LIVE-x"))
    assert rh.prod_live_gate_open(broken) is False


def test_dispatch_aborts_an_unsent_production_intent_without_the_ack(monkeypatch):
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch, environment="PROD")
    assert runtime.allows_new_broker_mutation                       # the fixture carries a valid ack
    unacked = replace(runtime, deployment=replace(runtime.deployment, prod_live_ack=""))
    assert not unacked.allows_new_broker_mutation
    result = execution._dispatch_or_reconcile_one(client, object(), CFG, intent, claim, unacked)
    assert result["status"] == "UNSENT_ABORTED"
    assert not client.order_v3.preview_order.calls and not client.order_v3.place_order.calls


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
    no_ack = authorized(env(tmp_path))
    assert blocked_flag(no_ack) is True                               # PROD: authorized binding, no ack
    assert blocked_flag(acked(no_ack)) is False
    assert blocked_flag({**acked(no_ack), "LEGO_MAX_ORDER_NOTIONAL_USD": "900"}) is True   # drifted caps
    uat = env(tmp_path, WEBULL_ENV="UAT", LEGO_ALLOW_FRACTIONAL="true")
    assert blocked_flag(authorized(uat)) is False
    assert blocked_flag({**uat, "LEGO_RELEASE_AUTHORIZATION": "0" * 64}) is True          # stale binding


def test_deployments_that_never_intended_to_trade_are_not_flagged(tmp_path, slot_clock):
    base = authorized(env(tmp_path))                                   # PROD, no ack: gate closed
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
        deployment=SimpleNamespace(execution_limits=("", "", "", "")))
    assert operational_health.report(runtime, {}, {}, now=NOW)["orders_blocked_by_release"] is False
