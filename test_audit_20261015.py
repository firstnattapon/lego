"""Audit of revision lego-tick-uat-00045-luv (2026-10-13T13:29Z..20:34Z), docs/AUDIT_20261015_TH.md.

Each group pins one finding of that audit. Every broker, database and webhook call is a
test double; nothing here carries a real account, URL or order.
"""
from __future__ import annotations

import ast
import json
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import alerting
import broker_circuit
import execution_service
import observability
import ops
from conftest import FAKE_DB
from lego_one_row import Config
from lego_orders import PROD, UAT
from tools import measure_fee as mf
from tools import trace_audit as ta
from tools.monitoring_config import build as monitoring
from webull_io import PROD_ENDPOINT, UAT_ENDPOINT, InstrumentCapability

ROOT = Path(__file__).parent
SECRET_URL = "https://hooks.example.invalid/services/T000/B000/very-secret-token"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    FAKE_DB.store.clear()
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    yield
    FAKE_DB.store.clear()


# ======================================================================== C1: alert coverage
class Response:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


@pytest.fixture
def webhook(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", SECRET_URL)
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "account-test-123")
    monkeypatch.setenv("WEBULL_ENV", "UAT")
    monkeypatch.setenv("LEGO_SYMBOL", "UBER")
    posts = []

    def post(url, **kwargs):
        posts.append({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr(alerting.requests, "post", post)
    return posts


NEW_ACTION_EVENTS = ("BROKER_ORDER_FAILED", "OPERATOR_HALT", "OPEN_ORDER_BLOCKED",
                     "RELEASE_UNAUTHORIZED", "TOKEN_PREFLIGHT_BLOCKED")


def test_every_status_the_health_policy_pages_on_can_also_reach_the_webhook():
    """2026-10-13: a broker FAILED order paged Cloud Monitoring only; no webhook event existed."""
    filter_ = monitoring("lego-tick-uat", "projects/demo/notificationChannels/1")[
        "health-policy"]["conditions"][0]["conditionMatchedLog"]["filter"]
    named = {"AUTH_BACKOFF", "OPERATOR_HALT", "OPEN_ORDER_BLOCKED"}
    assert all(status in filter_ for status in named) and "severity>=ERROR" in filter_
    paged = (set(observability.ERROR_STATUSES) | named) - {"ERROR"}   # "ERROR" names no cause
    assert paged <= alerting.EVENTS, sorted(paged - alerting.EVENTS)
    # BROKER_REJECT_HALT and AUTH_BACKOFF are posted by their own call sites, not by notify_tick.
    own_call_sites = {broker_circuit.HALT, "AUTH_BACKOFF"}
    assert paged - own_call_sites <= alerting.ACTION_EVENTS, sorted(paged - own_call_sites - alerting.ACTION_EVENTS)
    assert own_call_sites.isdisjoint(alerting.ACTION_EVENTS)


@pytest.mark.parametrize("status", NEW_ACTION_EVENTS)
def test_a_new_action_status_sends_one_webhook_then_waits_out_the_cooldown(webhook, status):
    body = {"business_status": status}
    assert alerting.notify_tick(body) is True
    assert [p["json"]["event"] for p in webhook] == [status]
    assert alerting.notify_tick(body) is False and len(webhook) == 1       # 24 h cooldown, not one per tick


def test_the_generic_error_status_is_still_not_a_webhook_event(webhook):
    assert "ERROR" not in alerting.EVENTS
    assert alerting.notify_tick({"business_status": "ERROR"}) is False and webhook == []


def test_the_payload_keeps_its_old_keys_and_adds_text_for_stock_receivers(webhook):
    alerting.notify_tick({"business_status": "OPERATOR_HALT"})
    post = webhook[0]
    payload = post["json"]
    assert payload["event"] == "OPERATOR_HALT" and payload["symbol"] == "UBER"
    assert payload["consecutive_count"] is None and payload["expires_at"] is None
    assert payload["environment"] == "UAT"
    assert payload["text"] == payload["content"] == "[LEGO UAT] OPERATOR_HALT UBER"
    assert (post["timeout"], post["allow_redirects"]) == ((1, 2), False)
    blob = json.dumps(payload)
    assert "account-test-123" not in blob and "very-secret-token" not in blob


def test_the_environment_follows_webull_env(webhook, monkeypatch):
    monkeypatch.setenv("WEBULL_ENV", "prod")
    alerting.notify_tick({"business_status": "OPERATOR_HALT"})
    assert webhook[0]["json"]["environment"] == "PROD" and webhook[0]["json"]["text"].startswith("[LEGO PROD]")


def test_a_payload_that_cannot_be_built_never_breaks_the_tick(webhook, monkeypatch):
    """main.lego_tick calls notify_tick outside any try: the payload is built inside notify's."""
    def boom(*args, **kwargs):
        raise RuntimeError("payload failure " + SECRET_URL)
    monkeypatch.setattr(alerting, "build_payload", boom)
    assert alerting.notify_tick({"business_status": "OPERATOR_HALT"}) is False
    assert webhook == []


def test_a_rejected_delivery_is_retried_and_not_cooled_down(webhook, monkeypatch):
    monkeypatch.setattr(alerting.requests, "post", lambda *a, **k: Response(500))
    assert alerting.notify_tick({"business_status": "OPERATOR_HALT"}) is False
    (record,) = [v for v in FAKE_DB.store["webull_lego_alert_delivery"].values()]
    assert "delivered_at" not in record


# ------------------------------------------------------------------------------ the drill
def test_the_drill_is_a_dry_run_unless_told_to_send(webhook):
    result = alerting.drill(SECRET_URL)
    assert result["status"] == "DRY_RUN" and result["sent"] is False and webhook == []
    assert result["payload"]["drill"] is True and result["payload"]["event"] == "ALERT_DRILL"
    assert "drill" in result["payload"]["text"]
    assert SECRET_URL not in json.dumps(result)


def test_a_sent_drill_posts_once_and_still_is_not_verified_delivery(webhook):
    result = alerting.drill(SECRET_URL, send=True)
    assert result["status"] == "SENT" and result["sent"] is True and result["http_status"] == 200
    assert result["delivery_verified"] is False
    assert len(webhook) == 1 and webhook[0]["json"]["event"] == "ALERT_DRILL"
    assert (webhook[0]["timeout"], webhook[0]["allow_redirects"]) == ((1, 2), False)
    assert FAKE_DB.store == {}                                         # no cooldown spent or created


def test_a_drill_never_leaks_the_url_or_the_error_text(webhook, monkeypatch):
    def down(*args, **kwargs):
        raise OSError("connect failed to " + SECRET_URL)
    monkeypatch.setattr(alerting.requests, "post", down)
    failed = alerting.drill(SECRET_URL, send=True)
    assert failed["status"] == "SEND_FAILED" and failed["sent"] is False
    monkeypatch.setattr(alerting.requests, "post", lambda *a, **k: Response(403))
    rejected = alerting.drill(SECRET_URL, send=True)
    assert rejected["status"] == "SEND_REJECTED" and rejected["sent"] is False
    assert SECRET_URL not in json.dumps([failed, rejected])


@pytest.mark.parametrize("url", ["http://hooks.example.invalid/x", "https://user:pw@hooks.example.invalid/x",
                                 "hooks.example.invalid/x", ""])
def test_the_drill_applies_the_same_https_rule_as_delivery(webhook, url):
    with pytest.raises(ValueError, match="HTTPS") as error:
        alerting.drill(url, send=True)
    assert "pw" not in str(error.value) and webhook == []


def test_the_ops_command_reads_the_environment_and_does_not_send_by_default(webhook, monkeypatch):
    args = ops.parser().parse_args(["alert-drill"])
    assert args.send is False and args.secret is None
    result = ops.alert_drill_command(args)
    assert result["status"] == "DRY_RUN" and result["url_source"] == "environment" and webhook == []
    assert SECRET_URL not in json.dumps(result)
    sent = ops.alert_drill_command(ops.parser().parse_args(["alert-drill", "--send"]))
    assert sent["status"] == "SENT" and "confirm" in sent["next"] and len(webhook) == 1


def test_the_ops_command_reads_the_url_from_secret_manager(webhook, monkeypatch):
    monkeypatch.delenv("ALERT_WEBHOOK_URL")
    asked = []

    class Client:
        def access_secret_version(self, request):
            asked.append(request["name"])
            return SimpleNamespace(payload=SimpleNamespace(data=(SECRET_URL + "\n").encode()))

    monkeypatch.setattr("google.cloud.secretmanager.SecretManagerServiceClient", Client)
    result = ops.alert_drill_command(ops.parser().parse_args(
        ["alert-drill", "--secret", "projects/demo/secrets/lego-alert", "--send"]))
    assert asked == ["projects/demo/secrets/lego-alert/versions/latest"]
    assert result["status"] == "SENT" and result["url_source"] == "secret_manager"
    assert webhook[0]["url"] == SECRET_URL and SECRET_URL not in json.dumps(result)


@pytest.mark.parametrize("secret", ["lego-alert", "projects/demo/secrets/x/versions/3", "projects/demo"])
def test_the_ops_command_refuses_a_malformed_secret_name(webhook, secret):
    with pytest.raises(ValueError, match="projects/PROJECT/secrets/NAME"):
        ops.alert_drill_command(ops.parser().parse_args(["alert-drill", "--secret", secret]))


def test_the_ops_command_needs_a_url_and_exits_nonzero_on_a_bad_one(monkeypatch, capsys):
    with pytest.raises(ValueError, match="no webhook URL"):
        ops.alert_drill_command(ops.parser().parse_args(["alert-drill"]))
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://hooks.example.invalid/x")
    monkeypatch.setattr(sys, "argv", ["ops.py", "alert-drill", "--send"])
    assert ops.main_cli() == 1
    assert json.loads(capsys.readouterr().out)["sent"] is False


# ================================================================================ C2: fee tool
CFG = Config(symbol="UBER", fix_c=10000.0, diff=25.0, dna_code="bypass:100",
             strategy_id="shannon_demon_lego", decimal_precision=5)
PRICE = 80.0


class FeeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def fee_for(cost: Decimal) -> Decimal:
    """A per-order minimum of 0.50 plus 0.05% -- the smallest order is the costliest."""
    return max(Decimal("0.50"), cost * Decimal("0.0005"))


@pytest.fixture
def feeworld(monkeypatch):
    monkeypatch.setenv("AUTO_SUBMIT", "false")
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "account-test-123")
    monkeypatch.setattr(mf, "environment_label", lambda: UAT)
    monkeypatch.setattr(mf, "load_config", lambda: CFG)
    forbidden = SimpleNamespace(
        place_order=lambda *a, **k: pytest.fail("a Place was attempted"),
        cancel_order=lambda *a, **k: pytest.fail("a Cancel was attempted"))
    trade = SimpleNamespace(order_v3=forbidden, account_v2=SimpleNamespace(
        get_account_list=lambda: FeeResponse({"data": [{"account_id": "account-test-123"}]})))
    monkeypatch.setattr(mf, "build_clients", lambda: (trade, object()))
    monkeypatch.setattr(mf, "clients_endpoint", lambda *_a: UAT_ENDPOINT)
    monkeypatch.setattr(mf, "fetch_instrument_capability", lambda *_a: InstrumentCapability(
        symbol="UBER", status="OC", category="US_STOCK", currency="USD", lot_size=Decimal("1"),
        fractionable=True, quantity_increment=Decimal("0.01"), decimal_precision=2))
    monkeypatch.setattr(mf, "fetch_snapshot", lambda *_a: {
        "price": PRICE, "quote_time": "2026-10-13T13:30:00.000Z", "holdings": 5.0})
    monkeypatch.setattr(mf, "fetch_holdings", lambda *_a: 5.0)
    previews = []

    def preview(_client, payload, *, strict=True):
        assert strict is True
        previews.append(payload)
        cost = Decimal(payload[0]["quantity"]) * Decimal(str(PRICE))
        return {"estimated_cost": str(cost), "estimated_transaction_fee": str(fee_for(cost))}

    monkeypatch.setattr(mf, "preview_market_order_result", preview)
    return previews


def test_the_fee_is_estimated_fee_over_estimated_cost_at_each_notional(feeworld):
    report = mf.measure(notionals=mf.parse_notionals("25,100,1000"))
    by_target = {s["notional_target"]: s for s in report["samples"]}
    assert by_target["25"]["quantity"] == "0.31"                      # floor(25 / 80 / 0.01) * 0.01
    assert Decimal(by_target["25"]["estimated_cost"]) == Decimal("24.80")
    assert Decimal(by_target["25"]["fee_pct"]) == Decimal("2.017")    # 0.50 / 24.80 = 2.01612..., rounded up
    assert Decimal(by_target["1000"]["fee_pct"]) == Decimal("0.050")
    assert report["environment"] == UAT and report["side"] == "BUY" and report["ok"] is True
    assert report["broker_mutations"] is False


def test_the_suggestion_is_the_largest_percent_rounded_up_not_a_median(feeworld):
    report = mf.measure(notionals=mf.parse_notionals("25,50,100,250,1000"))
    percents = [Decimal(s["fee_pct"]) for s in report["samples"]]
    assert report["suggested_fee_pct"] == "2.017"                     # 2.01612... rounded UP
    assert Decimal(report["suggested_fee_pct"]) >= max(percents)
    assert Decimal(report["suggested_fee_pct"]) > sorted(percents)[len(percents) // 2]
    assert report["use"] == "python ops.py release-plan --fee-pct 2.017"


def test_it_proves_the_payload_shape_production_sends(feeworld):
    report = mf.measure(notionals=mf.parse_notionals("100"))
    assert report["payload_shape"] == {"order_type": "MARKET", "time_in_force": "DAY",
                                       "entrust_type": "QTY", "support_trading_session": "CORE"}
    leg = feeworld[0][0]
    assert leg["symbol"] == "UBER" and leg["side"] == "BUY" and leg["client_order_id"].startswith("legofee")
    assert len(leg["client_order_id"]) <= 32


def test_a_refused_preview_is_reported_and_does_not_hide_the_others(feeworld, monkeypatch):
    real = mf.preview_market_order_result

    def picky(client, payload, *, strict=True):
        if payload[0]["quantity"] == "0.31":
            raise RuntimeError("refused for key=abc123")
        return real(client, payload, strict=strict)

    monkeypatch.setattr(mf, "preview_market_order_result", picky)
    report = mf.measure(notionals=mf.parse_notionals("25,100"))
    small, large = report["samples"]
    assert "error" in small and "fee_pct" not in small and "fee_pct" in large
    assert report["ok"] is True and report["suggested_fee_pct"] == large["fee_pct"]


def test_all_previews_refused_is_a_failed_measurement(feeworld, monkeypatch, capsys):
    monkeypatch.setattr(mf, "preview_market_order_result",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no")))
    report = mf.measure(notionals=mf.parse_notionals("100"))
    assert report["ok"] is False and report["suggested_fee_pct"] is None and report["use"] is None
    assert mf.main(["--notionals", "100"]) == 2
    assert json.loads(capsys.readouterr().out)["broker_mutations"] is False


def test_a_notional_below_one_increment_is_skipped_not_rounded_up(feeworld):
    report = mf.measure(notionals=mf.parse_notionals("0.5,100"))
    assert "below one quantity increment" in report["samples"][0]["error"] and len(feeworld) == 1


def test_a_sell_preview_cannot_exceed_holdings(feeworld):
    report = mf.measure(notionals=mf.parse_notionals("100,1000"), side="SELL")
    assert report["samples"][0]["quantity"] == "1.25" and "fee_pct" in report["samples"][0]
    assert report["samples"][1]["error"] == "SELL quantity exceeds broker holdings"


@pytest.mark.parametrize("value", ["true", "1", "", "typo"])
def test_auto_submit_must_be_explicitly_false(feeworld, monkeypatch, value):
    monkeypatch.setenv("AUTO_SUBMIT", value)
    with pytest.raises(mf.SmokeRefusal, match="explicitly false"):
        mf.measure(notionals=mf.parse_notionals("100"))


def test_production_needs_allow_prod_and_builds_no_client_without_it(feeworld, monkeypatch):
    monkeypatch.setattr(mf, "environment_label", lambda: PROD)
    monkeypatch.setattr(mf, "build_clients",
                        lambda: pytest.fail("clients must not be built without --allow-prod"))
    with pytest.raises(mf.SmokeRefusal, match="--allow-prod"):
        mf.measure(notionals=mf.parse_notionals("100"))
    assert mf.main(["--notionals", "100"]) == 2


def test_production_with_allow_prod_previews_and_reports_the_quote_age(feeworld, monkeypatch):
    monkeypatch.setattr(mf, "environment_label", lambda: PROD)
    monkeypatch.setattr(mf, "clients_endpoint", lambda *_a: PROD_ENDPOINT)
    report = mf.measure(notionals=mf.parse_notionals("100"), allow_prod=True)
    assert report["environment"] == PROD and report["ok"] is True
    assert report["quote_age_seconds"] > 0 and report["quote_time"] == "2026-10-13T13:30:00.000Z"


def test_the_endpoint_must_match_the_environment(feeworld, monkeypatch):
    monkeypatch.setattr(mf, "clients_endpoint", lambda *_a: PROD_ENDPOINT)
    with pytest.raises(mf.SmokeRefusal, match="endpoint"):
        mf.measure(notionals=mf.parse_notionals("100"))


@pytest.mark.parametrize("text", ["", "abc", "0", "-5", "NaN", "Infinity", "5,5", ",".join(map(str, range(1, 12)))])
def test_notionals_must_be_a_short_list_of_distinct_positive_numbers(text):
    with pytest.raises(mf.SmokeRefusal):
        mf.parse_notionals(text)


def test_cli_errors_are_json_and_redacted(monkeypatch, capsys):
    monkeypatch.setenv("WEBULL_APP_SECRET", "super-secret-value")
    monkeypatch.setattr(mf, "measure", lambda **_kw: (_ for _ in ()).throw(RuntimeError("bad super-secret-value")))
    assert mf.main([]) == 2
    out = capsys.readouterr().out
    assert json.loads(out)["ok"] is False and "super-secret-value" not in out
    assert mf.main(["--unknown", "raw-secret-like-value"]) == 2
    assert "raw-secret-like-value" not in capsys.readouterr().out


def test_the_fee_tool_can_neither_place_nor_cancel_nor_write_the_database():
    tree = ast.parse((ROOT / "tools" / "measure_fee.py").read_text(encoding="utf-8"))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
                for alias in node.names}
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    modules |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert imported.isdisjoint({"place_market_order", "replace_market_order", "cancel_market_order",
                                "cancel_order", "find_recent_order_by_client_id"})
    assert not {m for m in modules if m.split(".")[0] in {"firebase_admin", "lego_outbox", "order_recovery",
                                                           "execution_service", "main"}}
    called = {node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert called.isdisjoint({"place_order", "replace_order", "cancel_order", "reference", "transaction"})


# =========================================================================== C3: quote age
def trace_world(ages):
    """A flight-recorder export whose ticks each priced on a quote `age` seconds old."""
    items = {}
    for n, age in enumerate(ages):
        captured = f"2026-10-13T15:{n % 60:02d}:05Z"
        quoted = ta.iso(ta.parse_ts(captured) - timedelta(seconds=age))
        items[f"k{n:03d}"] = {"at": captured, "tick": f"tick{n:04d}", "http": 200, "pipe": "TICK_OK", "biz": "TICK_OK",
                              "events": [{"t": 5, "k": "n", "n": "D06", "ok": True,
                                          "x": {"price": 80.0, "captured_at": captured, "quote_time": quoted}}]}
    return {"webull_lego_trace": {"CHAIN": {"2026-10-13": items}}}


def quote_findings(ages, env):
    rtdb = ta.analyze_rtdb(trace_world(ages))
    report = {"rtdb": rtdb, "deploy": {"env": {"WEBULL_ENV": env}}, "logs": {}}
    return {f["code"]: f for f in ta.build_findings(report)}


def test_the_age_of_each_quote_is_read_from_the_decision_snapshot_event():
    summary = ta.analyze_rtdb(trace_world([10, 20, 30, 40, 900]))["traces"]["quotes"]
    assert summary["n"] == 5 and summary["median_s"] == 30.0 and summary["max_s"] == 900.0
    assert summary["over_limit"] == 1 and summary["over_limit_share"] == 0.2
    assert ta.analyze_rtdb(trace_world([]))["traces"]["quotes"] == {"n": 0}


def test_a_prod_export_on_delayed_quotes_is_a_p1():
    found = quote_findings([900] * 10, "PROD")["QUOTE_NOT_REALTIME"]
    assert found["level"] == "P1" and "not real-time" in found["text"]


def test_a_prod_export_with_an_occasional_stale_quote_is_only_a_p2():
    codes = quote_findings([5] * 90 + [120] * 10, "PROD")
    assert codes["QUOTE_OFTEN_STALE"]["level"] == "P2" and "QUOTE_NOT_REALTIME" not in codes


def test_a_prod_export_with_fresh_quotes_has_no_quote_finding():
    codes = quote_findings([5] * 99 + [120], "PROD")                 # 1 % over the limit, below the 5 % bar
    assert not {"QUOTE_NOT_REALTIME", "QUOTE_OFTEN_STALE", "QUOTE_AGE"} & set(codes)


def test_a_uat_export_reports_the_age_as_information_only():
    found = quote_findings([900] * 10, "UAT")["QUOTE_AGE"]
    assert found["level"] == "INFO" and "QUOTE_NOT_REALTIME" not in quote_findings([900] * 10, "UAT")


def test_the_age_limit_is_the_one_production_enforces():
    assert ta.PROD_QUOTE_AGE_LIMIT_SECONDS == execution_service.PROD_MAX_DISPATCH_QUOTE_AGE_SECONDS


def test_the_digest_prints_the_quote_age_line():
    report = ta.build_report(rtdb=trace_world([10, 20, 900]))
    assert "quote age at decision: median 20.0 s" in ta.render(report)
