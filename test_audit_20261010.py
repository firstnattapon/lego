"""Audit of revision lego-tick-uat-00043-tux (2026-10-09T15:24Z..20:34Z), docs/AUDIT_20261010_TH.md.

Each group pins one finding of that audit; the evidence is quoted where it is not obvious.
Every broker and database call is a test double.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

import flight_recorder as fr
import ops
import release_horizon as rh
import tick_runtime
import webull_io
from config import ConfigurationError, load_runtime_config
from conftest import FAKE_DB
from test_deploy_scripts import log, observe_env, release_env, run, sandbox  # noqa: F401
from test_deploy_scripts import authorized as deploy_authorized
from test_flight_recorder import body, tick, traces
from test_prod_live_gate import authorized
from test_prod_live_gate import env as prod_env
from test_release_plan import ACCOUNT, CANDIDATE, NOW as PLAN_NOW, PRICE, UAT_POLICY, prod_policy
from test_release_plan import args as plan_args
from test_trace_audit import CHAIN, FIX_C, at, make_world, run_id
from test_webull_io import _install_fake_sdk
from tools import trace_audit as ta

ROOT = Path(__file__).parent


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    FAKE_DB.store.clear()
    monkeypatch.delenv("WEBULL_API_DEBUG", raising=False)
    monkeypatch.setenv("WEBULL_APP_KEY", "test-key")
    monkeypatch.setenv("WEBULL_APP_SECRET", "test-secret")
    webull_io.reset_clients()
    yield
    webull_io.reset_clients()
    FAKE_DB.store.clear()


# --------------------------------------------------------------------------------------------
# F2: one transient error while the SDK clients are built must not fail the whole tick.
# 2026-10-09 18:45:05Z: GET /openapi/config (the second of the two SDK constructor calls) hit a
# read timeout; build_clients() raised, the tick answered HTTP 503, and the slot row was
# committed a minute late. Every other read in webull_io already survives one such error.
# --------------------------------------------------------------------------------------------

class SdkHttpError(Exception):
    """What the SDK raises for a socket timeout: ClientException('SDK.HttpError', ...)."""
    error_code = "SDK.HttpError"


class BadParameter(Exception):
    http_status = 417
    error_code = "OPENAPI_PARAM_ERR"


def flaky_data_client(monkeypatch, failures):
    """DataClient(api) raises the queued exceptions first, then succeeds."""
    calls = {"trade": 0, "data": 0}
    trade_module = sys.modules["webull.trade.trade_client"]
    data_module = sys.modules["webull.data.data_client"]
    real_trade = trade_module.TradeClient

    def trade(api):
        calls["trade"] += 1
        return real_trade(api)

    def data(api):
        calls["data"] += 1
        if failures:
            raise failures.pop(0)
        return ("data", api)

    monkeypatch.setattr(trade_module, "TradeClient", trade)
    monkeypatch.setattr(data_module, "DataClient", data)
    return calls


@pytest.fixture
def sleeps(monkeypatch):
    waited = []
    monkeypatch.setattr(webull_io.time, "sleep", waited.append)
    return waited


def test_a_transient_error_while_building_the_clients_is_retried_once(monkeypatch, sleeps):
    _install_fake_sdk(monkeypatch)
    calls = flaky_data_client(monkeypatch, [SdkHttpError("Read timed out. (read timeout=5.0)")])
    trade, data = webull_io.build_clients()
    assert calls == {"trade": 2, "data": 2}            # the pair is rebuilt as a unit
    assert sleeps == [1.0] and data[0] == "data"
    assert webull_io._CLIENTS is not None


def test_a_non_transient_error_is_never_retried(monkeypatch, sleeps):
    _install_fake_sdk(monkeypatch)
    calls = flaky_data_client(monkeypatch, [BadParameter("bad parameter")])
    with pytest.raises(BadParameter):
        webull_io.build_clients()
    assert calls["data"] == 1 and sleeps == []
    assert webull_io._CLIENTS is None


def test_two_transient_errors_surface_and_nothing_is_cached(monkeypatch, sleeps):
    _install_fake_sdk(monkeypatch)
    second = SdkHttpError("second")
    calls = flaky_data_client(monkeypatch, [SdkHttpError("first"), second])
    with pytest.raises(SdkHttpError) as raised:
        webull_io.build_clients()
    assert raised.value is second and calls["data"] == 2 and sleeps == [1.0]
    assert webull_io._CLIENTS is None                    # a half-built pair is never reused
    assert webull_io.broker_error_details(second)["retryable_read"] is True


def test_no_retry_when_the_tick_has_no_time_for_one(monkeypatch, sleeps):
    _install_fake_sdk(monkeypatch)
    calls = flaky_data_client(monkeypatch, [SdkHttpError("slow")])
    monkeypatch.setattr(tick_runtime, "remaining", lambda: 2.5)    # < 1.0 s delay + 2.0 s floor
    with pytest.raises(tick_runtime.TickDeadlineExceeded):
        webull_io.build_clients()
    assert calls["data"] == 1 and sleeps == []
    assert webull_io._CLIENTS is None


def test_a_warm_cache_is_untouched_by_the_retry(monkeypatch, sleeps):
    built = _install_fake_sdk(monkeypatch)
    first = webull_io.build_clients()
    assert webull_io.build_clients() is not None and built["clients"] == 1
    assert webull_io._CLIENTS[2] is first[0] and sleeps == []


# --------------------------------------------------------------------------------------------
# F1: the tail of a long tick that moved money must reach the trace.
# 2026-10-09 15:24:04Z: the first tick after deploy overran (30.7 s of 35). Its trace stops at
# Place ("partial": true, heartbeat skipped_budget 1); the order-detail error that followed and
# the TICK_DEFERRED were not recorded, and that is the order that ended FAILED without a reason.
# --------------------------------------------------------------------------------------------

def test_a_tick_that_moved_money_keeps_its_tail_with_three_seconds_left(monkeypatch):
    monkeypatch.setattr(fr, "_remaining", lambda: 3.0)
    with tick():
        fr.flag("mutation")
        fr.node("W25", "placed")
        result = fr.finish(body(), 200)
    assert result["written"] is True and len(traces()) == 1
    assert fr._STATS["skipped_budget"] == 0


def test_a_routine_tick_with_the_same_budget_is_still_skipped(monkeypatch):
    monkeypatch.setattr(fr, "_remaining", lambda: 3.0)
    with tick():
        fr.node("D10", "committed")
        result = fr.finish(body(), 200)
    assert result["skipped"] == "budget" and traces() == {}
    assert fr._STATS["skipped_budget"] == 1


def test_even_a_tick_that_moved_money_is_not_written_below_one_second(monkeypatch):
    monkeypatch.setattr(fr, "_remaining", lambda: 0.5)
    with tick():
        fr.flag("mutation")
        fr.node("W25", "placed")
        result = fr.finish(body(), 200)
    assert result["skipped"] == "budget" and traces() == {}


def test_the_mutation_floor_leaves_room_inside_the_cloud_run_limit():
    # deadline 35 s (tick_runtime.tick_scope) + one time-boxed write must stay under the 45 s
    # request timeout deploy/cloudshell-all-in-one.sh gives the function.
    assert fr.FLUSH_MIN_BUDGET_MUTATION_SECONDS < fr.FLUSH_MIN_BUDGET_SECONDS
    assert 35.0 - fr.FLUSH_MIN_BUDGET_MUTATION_SECONDS + fr.FLUSH_TIMEBOX_SECONDS < 45.0


# --------------------------------------------------------------------------------------------
# F7: the SDK debug switch must not run against a real account (open follow-up 4 of
# docs/AUDIT_20261006_TH.md). With WEBULL_API_DEBUG=sdk the SDK logs every request and response.
# --------------------------------------------------------------------------------------------

def test_production_refuses_the_sdk_debug_switch(tmp_path):
    values = authorized(prod_env(tmp_path))
    assert load_runtime_config(values).deployment.environment == "PROD"
    with pytest.raises(ConfigurationError, match="WEBULL_API_DEBUG"):
        load_runtime_config({**values, "WEBULL_API_DEBUG": "sdk"})
    assert load_runtime_config({**values, "WEBULL_API_DEBUG": "  "})        # blank is unset


@pytest.mark.parametrize("name", ["PROD", "PRODUCTION"])
def test_the_debug_guard_follows_the_normalised_environment(tmp_path, name):
    values = prod_env(tmp_path, WEBULL_ENV=name, WEBULL_API_DEBUG="sdk")
    with pytest.raises(ConfigurationError, match="WEBULL_API_DEBUG"):
        load_runtime_config(values)


def test_uat_may_still_use_the_debug_switch(tmp_path):
    values = prod_env(tmp_path, WEBULL_ENV="UAT", WEBULL_API_DEBUG="sdk")
    assert load_runtime_config(values).deployment.environment == "UAT"


def test_a_tick_with_the_debug_switch_on_production_answers_config_error(tmp_path, monkeypatch):
    import main
    for key, value in authorized(prod_env(tmp_path)).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("WEBULL_API_DEBUG", "sdk")
    monkeypatch.setattr(main.execution_service, "_init_firebase", lambda: None)
    with tick_runtime.tick_scope("debug-switch"):
        payload, status = main._run_tick(None)
    assert status == 500 and payload["pipeline_status"] == "CONFIG_ERROR"
    assert "WEBULL_API_DEBUG" in payload["error"]


# --------------------------------------------------------------------------------------------
# F6: real positions and orders must not land, by default, in the database UAT uses, whose rules
# let anyone read rows, state and order audit (open follow-up 4 of docs/AUDIT_20261006_TH.md).
# deploy/cloudshell-all-in-one.sh auto-discovered "<project>-default-rtdb" when no override was
# given, and the runbook keeps UAT and PROD in the same project (lego-firebase).
# --------------------------------------------------------------------------------------------

def production_observe_env():
    env = release_env(LEGO_MODE_OVERRIDE="observe", LEGO_ACTIVE_OVERRIDE="false")
    for name in ("LEGO_MAX_ORDER_QUANTITY_OVERRIDE", "LEGO_MAX_ORDER_NOTIONAL_USD_OVERRIDE",
                 "LEGO_MAX_SESSION_ORDERS_OVERRIDE", "LEGO_TRADING_WINDOW_END_OVERRIDE"):
        env.pop(name)
    return env


def test_production_observe_without_a_named_database_is_refused_before_any_cloud_call(sandbox):
    result = run(sandbox, production_observe_env(), DATABASE_URL_OVERRIDE="")
    assert result.returncode == 1, result.stderr
    assert "explicit DATABASE_URL_OVERRIDE" in result.stderr
    assert log(sandbox) == ""


def test_production_live_without_a_named_database_is_refused_before_any_cloud_call(sandbox):
    result = run(sandbox, deploy_authorized(sandbox, release_env()), DATABASE_URL_OVERRIDE="")
    assert result.returncode == 1, result.stderr
    assert "explicit DATABASE_URL_OVERRIDE" in result.stderr
    assert log(sandbox) == ""


def test_production_with_a_named_database_goes_on_to_the_cloud_steps(sandbox):
    result = run(sandbox, production_observe_env(),
                 DATABASE_URL_OVERRIDE="https://prod-only-default-rtdb.firebaseio.com")
    assert result.returncode == 78, result.stderr          # the stub gcloud stops at `functions deploy`
    assert "Using DATABASE_URL_OVERRIDE: https://prod-only-default-rtdb.firebaseio.com" in result.stdout


def test_uat_keeps_discovering_its_database(sandbox):
    result = run(sandbox, observe_env(), DATABASE_URL_OVERRIDE="")
    assert "explicit DATABASE_URL_OVERRIDE" not in result.stderr
    assert "6/10 RESOLVE FIREBASE REALTIME DATABASE" in result.stdout


def read_rules(node):
    for key, value in node.items():
        if key == ".read":
            yield value
        elif isinstance(value, dict):
            yield from read_rules(value)


def without_reads(node):
    return {key: without_reads(value) if isinstance(value, dict) else value
            for key, value in node.items() if key != ".read"}


def test_the_production_rules_differ_from_the_standard_rules_only_in_read_access():
    standard = json.loads((ROOT / "database.rules.json").read_text())
    private = json.loads((ROOT / "database.rules.prod.json").read_text())
    assert any(value is True for value in read_rules(standard))     # UAT's dashboards read these
    reads = list(read_rules(private))
    assert reads and all(value is False for value in reads)         # nothing is public
    assert without_reads(private) == without_reads(standard)        # same nodes, indexes and write rules


# --------------------------------------------------------------------------------------------
# F3/F5: a real-money release is planned with the facts only the operator has: the measured fee
# and whether alerts have a channel. They warn and inform; they never block (nothing here can
# prove a profit or a loss), and they never change the release or its acknowledgement.
# UAT charged 1.07% of notional per order (median of 5 fills, 1.054%..1.103%).
# --------------------------------------------------------------------------------------------

@pytest.fixture
def plan_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("LEGO_", "WEBULL_", "ALERT_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", ACCOUNT)
    monkeypatch.setattr(ops, "_candidate", lambda: (CANDIDATE, "backend-only"))


def prod_plan(tmp_path, **overrides):
    base = dict(env_file=prod_policy(tmp_path), window_sessions=5, recommended_limits=True,
                reference_price=PRICE)
    base.update(overrides)
    return ops.release_plan_command(plan_args(**base), now=PLAN_NOW)


def found(result):
    return {finding["id"]: finding for finding in result["assessment"]["findings"]}


def test_a_production_plan_without_a_measured_fee_warns_and_is_not_blocked(plan_env, tmp_path):
    result = prod_plan(tmp_path, enforce=True)
    findings = found(result)
    assert findings["fee_unmeasured"]["severity"] == "WARN" and "--fee-pct" in findings["fee_unmeasured"]["message"]
    assert findings["alert_unset"]["severity"] == "WARN"
    assert result["blocked"] is False and result["assessment"]["ok"]
    assert result["prod_live_acks"]["prefunded"] and "fee_check" not in result


def test_a_measured_fee_is_reported_as_a_cost_and_still_does_not_gate(plan_env, tmp_path):
    result = prod_plan(tmp_path, fee_pct="1.07", enforce=True)
    findings = found(result)
    assert "fee_unmeasured" not in findings
    assert findings["fee_measured"]["severity"] == "INFO"
    assert "0.2675 USD at DIFF 25" in findings["fee_measured"]["message"]
    assert "2.14% round trip" in findings["fee_measured"]["message"]
    assert result["fee_check"]["fee_pct"] == "1.07" and result["fee_check"]["min_cost_per_order_usd"] == "0.2675"
    assert result["blocked"] is False                         # even at UAT's fee, which is far above any edge


def test_the_fee_never_changes_the_release_or_its_acknowledgement(plan_env, tmp_path):
    bare, measured = prod_plan(tmp_path), prod_plan(tmp_path, fee_pct="0.15")
    for key in ("release", "prod_live_acks", "deploy_env", "candidate_hash"):
        assert bare[key] == measured[key]


def test_a_webhook_secret_in_the_environment_silences_only_the_alert_warning(plan_env, tmp_path, monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_SECRET_OVERRIDE", "lego-alert-webhook-prod")
    findings = found(prod_plan(tmp_path))
    assert "alert_unset" not in findings and "fee_unmeasured" in findings


def test_uat_and_production_observe_get_no_operator_findings(plan_env, tmp_path):
    operator_ids = {"fee_unmeasured", "fee_measured", "alert_unset"}
    uat = ops.release_plan_command(plan_args(window_sessions=10, fee_pct="1.07"), now=PLAN_NOW)
    assert not operator_ids & set(found(uat)) and uat["fee_check"]["fee_pct"] == "1.07"
    observe = ops.release_plan_command(
        plan_args(env_file=prod_policy(tmp_path, "observe", "false"), window_sessions=5), now=PLAN_NOW)
    assert not operator_ids & set(found(observe))


@pytest.mark.parametrize("good", ["0", "0.15", "1.07", "99.99", " 0.5 "])
def test_parse_fee_pct_accepts_a_percentage(good):
    assert rh.parse_fee_pct(good) == rh.Decimal(good.strip())


@pytest.mark.parametrize("bad", ["", "abc", "-0.1", "100", "nan", "inf", "1e400"])
def test_parse_fee_pct_rejects_what_is_not_a_percentage(bad):
    with pytest.raises(ValueError, match="fee-pct"):
        rh.parse_fee_pct(bad)


def test_a_mistyped_fee_stops_the_plan(plan_env, tmp_path):
    with pytest.raises(ValueError, match="fee-pct"):
        prod_plan(tmp_path, fee_pct="1,07")


def test_the_command_line_accepts_fee_pct(plan_env, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["ops.py", "release-plan", "--env-file", str(UAT_POLICY),
                                      "--window-sessions", "10", "--fee-pct", "1.07"])
    assert ops.main_cli() == 0
    assert json.loads(capsys.readouterr().out)["fee_check"]["fee_pct"] == "1.07"


def test_the_deploy_script_shows_the_operator_warnings_for_a_real_money_release(sandbox):
    result = run(sandbox, deploy_authorized(sandbox, release_env()))
    assert result.returncode == 3, result.stderr              # stops for the acknowledgement, as before
    assert "[WARN] fee_unmeasured" in result.stdout and "[WARN] alert_unset" in result.stdout


def test_the_deploy_script_hands_the_fee_and_the_webhook_secret_to_the_plan(sandbox):
    result = run(sandbox, deploy_authorized(sandbox, release_env()),
                 LEGO_FEE_PCT_OVERRIDE="0.15", ALERT_WEBHOOK_SECRET_OVERRIDE="lego-alert-webhook-prod")
    assert result.returncode == 3, result.stderr
    assert "fee_unmeasured" not in result.stdout and "alert_unset" not in result.stdout


def test_the_deploy_script_refuses_a_mistyped_fee_before_deploying(sandbox):
    result = run(sandbox, deploy_authorized(sandbox, release_env()), LEGO_FEE_PCT_OVERRIDE="1,07")
    assert result.returncode == 1
    assert "FIREBASE_DEPLOY_REACHED" not in log(sandbox)


# --------------------------------------------------------------------------------------------
# F3/F4: the digest must say what the fee costs against the size of the orders, and must name an
# order the broker failed without a reason (tools.readiness_audit fails snapshot_integrity on it;
# before this the digest listed neither). Expected values are computed here, not by the tool.
# --------------------------------------------------------------------------------------------

WORLD_FILLS = [(2.02, 49.52, 1.07), (5.94, 51.02, 3.05), (4.32, 49.93, 2.31)]     # make_world's three fills


def economics_of(world):
    return ta.build_report(rtdb=world)["rtdb"]["chains"][0]["economics"]


def test_the_digest_names_the_order_size_that_could_earn_back_the_fee():
    eco = economics_of(make_world(stuck=False))
    pct = sorted(100.0 * fee / (qty * price) for qty, price, fee in WORLD_FILLS)[1]
    sizes = [qty * price for qty, price, _ in WORLD_FILLS]
    breakeven = 2.0 * pct / 100.0 * FIX_C
    assert eco["order_size"]["breakeven_notional"] == pytest.approx(breakeven, abs=0.01)
    assert eco["order_size"]["median_notional"] == pytest.approx(sorted(sizes)[1], abs=0.01)
    assert eco["order_size"]["below_breakeven"] == sum(size < breakeven for size in sizes)
    assert "pays back its own fee" in eco["order_size"]["note"]


def test_the_run_rate_is_extrapolated_only_from_the_sessions_that_were_seen():
    world = make_world(stuck=False)
    assert "extrapolation" not in economics_of(world)                      # no market_slot_id: no sessions
    for row in world["webull_lego_rows"].values():
        row["market_slot_id"] = ("2026-10-08" if row["version"] <= 5 else "2026-10-09") + f":{row['version']}"
    eco = economics_of(world)["extrapolation"]
    median_fee = sorted(fee for *_, fee in WORLD_FILLS)[1]
    assert eco["sessions"] == 2 and eco["orders_per_session"] == 1.5
    assert eco["fee_per_session"] == pytest.approx(sum(fee for *_, fee in WORLD_FILLS) / 2, abs=1e-4)
    assert eco["annual_fee_pct_of_fix_c"] == pytest.approx(100.0 * median_fee * 1.5 * 252 / FIX_C, abs=0.01)
    assert "2 session(s)" in eco["note"] and "more than a few days" in eco["note"]


def test_economics_without_a_principal_keep_their_old_shape():
    realized = make_world(stuck=False)["webull_lego_realized"][CHAIN]
    eco = ta.economics(realized)
    assert eco["n"] == 3 and "order_size" not in eco and "extrapolation" not in eco


def test_the_fee_finding_quotes_the_order_size_and_the_text_report_shows_it():
    report = ta.build_report(rtdb=make_world(stuck=False))
    drag = next(f for f in report["findings"] if f["code"] == "FEE_DRAG")
    assert "best-case break-even" in drag["text"] and "2 x fee x FIX_C" in drag["text"]
    assert "order size: median" in ta.render(report)


def failed_world(*, reason_missing):
    world = make_world(stuck=False)
    world["webull_lego_order_outbox"][CHAIN][run_id(50)] = {
        "run_id": run_id(50), "client_order_id": run_id(50), "chain_key": CHAIN, "symbol": "TEST",
        "side": "SELL", "quantity": 5.08, "created_at": at(190), "placed_at": at(191),
        "status": "FAILED", "broker_status": "FAILED", "broker_order_id": "BRK-SYNTHETIC-FAILED-000000",
        "filled_quantity": "0.000000", "broker_reason_missing": reason_missing,
        "terminal_reason": "broker order failed; broker rejection reason unavailable"}
    return world


def test_a_failed_order_with_no_reason_is_a_finding_that_names_it():
    report = ta.build_report(rtdb=failed_world(reason_missing=True))
    named = [f for f in report["findings"] if f["code"] == "ORDER_FAILED_REASON_UNKNOWN"]
    assert len(named) == 1 and named[0]["level"] == "P1"
    assert run_id(50)[:8] in named[0]["text"] and "snapshot_integrity" in named[0]["text"]
    assert named[0]["evidence"]["broker_id"] == "BRK-SYNT"      # ids are shortened to 8 characters
    assert "FAILED WITHOUT REASON" in ta.render(report)


def test_a_failed_order_that_carries_its_reason_is_not_flagged():
    report = ta.build_report(rtdb=failed_world(reason_missing=False))
    assert not [f for f in report["findings"] if f["code"] == "ORDER_FAILED_REASON_UNKNOWN"]


def test_a_healthy_export_still_has_nothing_worse_than_info_after_the_additions():
    report = ta.build_report(rtdb=make_world(stuck=False))
    assert report["summary"]["P0"] == 0 and "ORDER_FAILED_REASON_UNKNOWN" not in {
        f["code"] for f in report["findings"]}
