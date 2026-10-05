"""ops.py release-plan: one command that plans, judges and hands off a release."""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import ops
from config import release_binding_for

ROOT = Path(__file__).parent
UAT_POLICY = ROOT / "deploy" / "uat-continuous.env.example"
NOW = datetime(2026, 10, 5, 7, 0, tzinfo=timezone.utc)           # Monday, before the open
ACCOUNT = "private-account-id-123"
CANDIDATE = "c" * 64
PRICE = "67.4"


@pytest.fixture(autouse=True)
def plan_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("LEGO_", "WEBULL_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", ACCOUNT)
    monkeypatch.setattr(ops, "_candidate", lambda: (CANDIDATE, "backend-only"))


def args(**overrides):
    base = dict(window_sessions=None, window_end=None, env_file=str(UAT_POLICY),
                reference_price=None, recommended_limits=False, initial_funding=False,
                candidate_hash=None, enforce=False, account_id_stdin=False)
    base.update(overrides)
    return SimpleNamespace(**base)


def plan(**overrides):
    return ops.release_plan_command(args(**overrides), now=NOW)


def prod_policy(tmp_path, mode="trade", active="true"):
    path = tmp_path / "prod.env"
    path.write_text("\n".join([
        "WEBULL_ENV=PROD", f"LEGO_MODE={mode}", f"LEGO_ACTIVE={active}", "LEGO_SYMBOL=UBER",
        "LEGO_FIX_C=10000", "LEGO_DIFF=25", "LEGO_ALLOW_FRACTIONAL=false",
        f"LEGO_DNA_BUNDLE={ROOT / 'strategy.uat-continuous.json'}",
        "LEGO_SESSION_KEY_MODE=market_day", "LEGO_STALE_ORDER_ACTION=hold", ""]))
    return str(path)


# -------------------------------------------------------------------- UAT profile
def test_uat_policy_file_plans_a_ten_session_window_without_findings():
    result = plan(window_sessions=10)
    assert result["environment"] == "UAT" and result["read_only"] is True
    assert result["release"]["LEGO_TRADING_WINDOW_END"] == "2026-10-16T20:00:00Z"
    assert result["assessment"]["ok"] and not result["assessment"]["warnings"]
    assert result["assessment"]["limits"]["complete_sessions_in_window"] == 10
    assert result["candidate_hash"] == CANDIDATE and result["candidate_scope"] == "backend-only"
    assert len(result["release"]["LEGO_RELEASE_AUTHORIZATION"]) == 64


def test_the_policy_file_caps_equal_the_recommendation():
    result = plan(window_sessions=10, reference_price=PRICE)
    release = result["release"]
    recommended = result["recommended_limits"]
    assert (release["LEGO_MAX_ORDER_QUANTITY"], release["LEGO_MAX_ORDER_NOTIONAL_USD"],
            release["LEGO_MAX_SESSION_ORDERS"]) == ("30", "1500", "26")
    assert (recommended["LEGO_MAX_ORDER_QUANTITY"], recommended["LEGO_MAX_ORDER_NOTIONAL_USD"],
            recommended["LEGO_MAX_SESSION_ORDERS"]) == ("30", "1500", "26")


SCRIPT_BINDING_NAMES = (
    "WEBULL_ENV", "LEGO_CANDIDATE_HASH", "LEGO_SYMBOL", "LEGO_MAX_ORDER_QUANTITY",
    "LEGO_MAX_ORDER_NOTIONAL_USD", "LEGO_MAX_SESSION_ORDERS", "LEGO_TRADING_WINDOW_END",
    "LEGO_FIX_C", "LEGO_DIFF", "LEGO_DNA_BUNDLE", "LEGO_MODE", "LEGO_ACTIVE",
    "LEGO_ALLOW_FRACTIONAL", "LEGO_STALE_ORDER_ACTION", "LEGO_STALE_ORDER_SECONDS",
    "LEGO_CANCEL_CONFIRM_GRACE_SECONDS", "LEGO_SESSION_KEY_MODE")


def test_the_binding_equals_what_the_deploy_script_will_compute():
    """deploy/cloudshell-all-in-one.sh recomputes the binding from exactly these names."""
    result = plan(window_sessions=10)
    reverse = {override: name for name, override in ops.DEPLOY_OVERRIDES.items()}
    deploy = result["deploy_env"]
    env = {reverse[key]: value for key, value in deploy.items() if key in reverse}
    env["LEGO_CANDIDATE_HASH"] = deploy["EXPECTED_CANDIDATE_HASH"]
    script_env = {name: env[name] for name in SCRIPT_BINDING_NAMES}
    script_env["WEBULL_ACCOUNT_ID"] = ACCOUNT
    assert release_binding_for(script_env) == result["release"]["LEGO_RELEASE_AUTHORIZATION"]
    assert deploy["LEGO_RELEASE_AUTHORIZATION_OVERRIDE"] == result["release"]["LEGO_RELEASE_AUTHORIZATION"]


def test_the_incident_release_is_blocked_by_the_plan(monkeypatch):
    for key, value in {"LEGO_TRADING_WINDOW_END": "2026-10-01T06:19:26Z",
                       "LEGO_MAX_ORDER_QUANTITY": "1000", "LEGO_MAX_ORDER_NOTIONAL_USD": "36000",
                       "LEGO_MAX_SESSION_ORDERS": "30",
                       "LEGO_DNA_BUNDLE": str(ROOT / "strategy.example.json")}.items():
        monkeypatch.setenv(key, value)
    result = plan(enforce=True)
    assessment = result["assessment"]
    assert result["blocked"] is True and assessment["blocking"] == ["window_open"]
    assert {"notional_cap_ratio", "session_orders_cap"} <= set(assessment["warnings"])


def test_a_new_window_longer_than_the_old_dna_is_blocked(monkeypatch):
    monkeypatch.setenv("LEGO_DNA_BUNDLE", str(ROOT / "strategy.example.json"))
    result = plan(window_sessions=10, enforce=True)
    assert result["blocked"] and "dna_covers_window" in result["assessment"]["blocking"]


def test_enforce_decides_the_exit_code(monkeypatch, capsys):
    argv = ["ops.py", "release-plan", "--env-file", str(UAT_POLICY),
            "--window-end", "2000-01-01T00:00:00Z"]
    monkeypatch.setattr(sys, "argv", argv + ["--enforce"])
    assert ops.main_cli() == 1
    assert json.loads(capsys.readouterr().out)["blocked"] is True
    monkeypatch.setattr(sys, "argv", argv)
    assert ops.main_cli() == 0                                    # advisory without --enforce
    assert "blocked" not in json.loads(capsys.readouterr().out)


def test_recommended_limits_replace_loose_configured_caps(monkeypatch):
    monkeypatch.setenv("LEGO_MAX_ORDER_NOTIONAL_USD", "36000")
    monkeypatch.setenv("LEGO_MAX_ORDER_QUANTITY", "1000")
    monkeypatch.setenv("LEGO_MAX_SESSION_ORDERS", "30")
    loose = plan(window_sessions=10)
    assert "notional_cap_ratio" in loose["assessment"]["warnings"]
    fixed = plan(window_sessions=10, recommended_limits=True, reference_price=PRICE)
    assert fixed["release"]["LEGO_MAX_ORDER_NOTIONAL_USD"] == "1500"
    assert not fixed["assessment"]["warnings"]


def test_recommended_limits_need_a_reference_price():
    with pytest.raises(ValueError, match="reference-price"):
        plan(window_sessions=10, recommended_limits=True)


# ---------------------------------------------------------------------- production
def test_production_plan_prints_the_acknowledgement_the_runtime_will_require(tmp_path):
    result = plan(env_file=prod_policy(tmp_path), window_sessions=5, recommended_limits=True,
                  reference_price=PRICE, enforce=True)
    assert result["environment"] == "PROD" and result["blocked"] is False
    ack = result["prod_live_acks"]["prefunded"]
    assert ack.startswith("LIVE-PROD-UBER-q20-n1000-o10-20261009T2000Z-prefunded-")
    assert result["prod_live_acks"]["initial-funding"] is None
    assert "LEGO_FUNDING_MODE_OVERRIDE" not in result["deploy_env"]
    assert result["deploy_env"]["LEGO_PROD_LIVE_ACK"] == ack
    assert any("WEBULL_TOKEN_SECRET_OVERRIDE" in step for step in result["next_steps"])
    assert result["deploy_env"]["LEGO_RELEASE_AUTHORIZATION_OVERRIDE"] == (
        result["release"]["LEGO_RELEASE_AUTHORIZATION"])


def test_the_acknowledgement_opens_the_runtime_gate_for_exactly_this_plan(tmp_path):
    from config import load_runtime_config
    result = plan(env_file=prod_policy(tmp_path), window_sessions=5, recommended_limits=True,
                  reference_price=PRICE)
    release, deploy = result["release"], result["deploy_env"]
    env = {"WEBULL_ENV": "PROD", "WEBULL_ACCOUNT_ID": ACCOUNT, "LEGO_MODE": "trade",
           "LEGO_ACTIVE": "true", "LEGO_SYMBOL": "UBER", "LEGO_FIX_C": "10000", "LEGO_DIFF": "25",
           "LEGO_ALLOW_FRACTIONAL": "false", "LEGO_SESSION_KEY_MODE": "market_day",
           "LEGO_STALE_ORDER_ACTION": "hold",
           "LEGO_DNA_BUNDLE": str(ROOT / "strategy.uat-continuous.json"), **release,
           "LEGO_PROD_LIVE_ACK": deploy["LEGO_PROD_LIVE_ACK"]}
    assert load_runtime_config(env).allows_new_broker_mutation is True
    assert load_runtime_config({**env, "LEGO_PROD_LIVE_ACK": "LIVE-PROD"}).allows_new_broker_mutation is False


def test_funding_plan_uses_loose_caps_a_separate_ack_and_reminds_to_redeploy(tmp_path):
    result = plan(env_file=prod_policy(tmp_path), window_sessions=2, recommended_limits=True,
                  reference_price=PRICE, initial_funding=True, enforce=True)
    release = result["release"]
    assert (release["LEGO_MAX_ORDER_QUANTITY"], release["LEGO_MAX_ORDER_NOTIONAL_USD"],
            release["LEGO_MAX_SESSION_ORDERS"]) == ("200", "12500", "2")
    assert result["blocked"] is False and "funding_release_temporary" in result["assessment"]["warnings"]
    assert result["prod_live_acks"]["prefunded"] is None
    assert "-initial-funding-" in result["deploy_env"]["LEGO_PROD_LIVE_ACK"]
    assert result["deploy_env"]["LEGO_FUNDING_MODE_OVERRIDE"] == "initial-funding"     # the script must judge it as such
    assert any("FUNDING release" in step and "LEGO_FUNDING_MODE_OVERRIDE" in step for step in result["next_steps"])


def test_steady_caps_cannot_fund_a_flat_account_and_the_plan_says_so(tmp_path, monkeypatch):
    steady = plan(env_file=prod_policy(tmp_path), window_sessions=2, recommended_limits=True,
                  reference_price=PRICE, enforce=True)
    assert steady["blocked"] is False
    release = steady["release"]
    for name in ("LEGO_MAX_ORDER_QUANTITY", "LEGO_MAX_ORDER_NOTIONAL_USD", "LEGO_MAX_SESSION_ORDERS"):
        monkeypatch.setenv(name, release[name])                   # 20 / 1000 / 10
    # The first order of a flat account is ~FIX_C (10,000): these caps would block it
    # every slot, so a plan for the t0 release must not pass with them.
    flat = plan(env_file=prod_policy(tmp_path), window_sessions=2, initial_funding=True,
                reference_price=PRICE, enforce=True)
    assert flat["blocked"] is True
    assert {"funding_notional_sufficient", "funding_quantity_sufficient"} <= set(
        flat["assessment"]["blocking"])
    # Those caps are fine for a pre-funded account (its ack exists) but cannot fund one,
    # so the funding plan hands out no acknowledgement to deploy with.
    assert flat["prod_live_acks"]["prefunded"] is not None
    assert flat["prod_live_acks"]["initial-funding"] is None
    assert "LEGO_PROD_LIVE_ACK" not in flat["deploy_env"]


def test_production_observe_has_no_acknowledgement(tmp_path):
    result = plan(env_file=prod_policy(tmp_path, "observe", "false"), window_sessions=5)
    assert "prod_live_acks" not in result and "LEGO_PROD_LIVE_ACK" not in result["deploy_env"]


# -------------------------------------------------------------------------- hygiene
def test_the_account_id_is_never_printed():
    text = json.dumps(plan(window_sessions=10), ensure_ascii=False)
    assert ACCOUNT not in text


def test_the_deploy_script_can_pipe_the_account_id_in(monkeypatch):
    import io
    monkeypatch.delenv("WEBULL_ACCOUNT_ID")
    monkeypatch.setattr(sys, "stdin", io.StringIO("piped-account-id\n"))
    result = plan(window_sessions=10, account_id_stdin=True)
    assert "piped-account-id" not in json.dumps(result)
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "piped-account-id")
    assert plan(window_sessions=10)["account_fingerprint"] == result["account_fingerprint"]


def test_process_environment_overrides_the_policy_file(monkeypatch):
    monkeypatch.setenv("LEGO_FIX_C", "5000")
    assert plan(window_sessions=10)["principal_usd"] == 5000.0


def test_stale_authorization_and_ack_in_the_environment_are_never_reused(monkeypatch):
    monkeypatch.setenv("LEGO_RELEASE_AUTHORIZATION", "f" * 64)
    monkeypatch.setenv("LEGO_PROD_LIVE_ACK", "LIVE-old")
    result = plan(window_sessions=10)
    assert result["release"]["LEGO_RELEASE_AUTHORIZATION"] != "f" * 64


def test_candidate_hash_override_must_be_a_sha256():
    with pytest.raises(ValueError):
        plan(window_sessions=10, candidate_hash="abc")
    other = plan(window_sessions=10, candidate_hash="d" * 64)
    assert other["candidate_scope"] == "supplied"
    assert other["release"]["LEGO_RELEASE_AUTHORIZATION"] != plan(
        window_sessions=10)["release"]["LEGO_RELEASE_AUTHORIZATION"]


def test_window_options_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        ops.parser().parse_args(["release-plan", "--window-sessions", "3", "--window-end", "x"])


def test_an_unparseable_window_end_blocks_instead_of_crashing():
    result = plan(window_end="tomorrow", enforce=True)
    assert result["blocked"] and result["assessment"]["blocking"] == ["limits_configured"]


def test_env_file_format(tmp_path):
    path = tmp_path / "policy.env"
    path.write_text("# comment\n\nA=1\nB = two words \nC=\nD=x=y\n")
    assert ops._read_env_file(path) == {"A": "1", "B": "two words", "D": "x=y"}
    path.write_text("A=1\nnot a pair\n")
    with pytest.raises(ValueError, match=":2:"):
        ops._read_env_file(path)


def test_check_reports_the_horizon_too(monkeypatch):
    monkeypatch.setattr(ops, "candidate_hash", lambda: CANDIDATE)
    for key, value in {"LEGO_SYMBOL": "UBER", "LEGO_FIX_C": "10000", "LEGO_DIFF": "25"}.items():
        monkeypatch.setenv(key, value)
    report = ops.check_command(None)
    assert report["horizon"]["ok"] is True and report["horizon"]["trading"] is False
    assert report["new_orders_authorized"] is False       # observe/inactive never sends orders
    assert report["prod_live_gate_open"] is True          # UAT is not subject to the production gate
