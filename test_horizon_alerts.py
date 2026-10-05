"""Release/DNA horizon alerts: thresholds, log severity, webhook events, policies."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import alerting
import observability
import operational_health
from conftest import FAKE_DB
from tools.monitoring_config import build as monitoring

NOW = datetime(2026, 10, 5, 7, 0, tzinfo=timezone.utc)
ORIGIN = "2026-09-08T13:30:00Z"


@pytest.fixture(autouse=True)
def clean_alert_state():
    # The 24h delivery cooldown lives in the (shared) fake RTDB; without this a
    # delivery in one test suppresses the same event in the next.
    FAKE_DB.store.clear()
    yield
    FAKE_DB.store.clear()


def runtime(end, code="bypass:6500"):
    return SimpleNamespace(
        operator=SimpleNamespace(dna_bundle=SimpleNamespace(
            dna_code=code, interval_seconds=900, origin_utc=ORIGIN)),
        deployment=SimpleNamespace(execution_limits=("30", "1500", "26", end)))


@pytest.fixture
def clock_env(monkeypatch):
    monkeypatch.setenv("LEGO_SLOT_SECONDS", "900")
    monkeypatch.setenv("LEGO_DNA_ORIGIN_UTC", ORIGIN)


# --------------------------------------------------------------------- thresholds
@pytest.mark.parametrize("hours,expiring,expired", [(49, False, False), (47, True, False),
                                                    (48, True, False), (0, True, True),
                                                    (-100, True, True)])
def test_release_warning_starts_forty_eight_hours_ahead(clock_env, hours, expiring, expired):
    end = (NOW + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    health = operational_health.report(runtime(end), {}, {}, now=NOW)
    assert health["release_expiring"] is expiring and health["release_expired"] is expired


def test_health_reports_how_many_complete_sessions_are_left(clock_env):
    health = operational_health.report(runtime("2026-10-16T20:00:00Z"), {}, {}, now=NOW)
    assert health["release_sessions_remaining"] == 10
    expired = operational_health.report(runtime("2026-10-01T06:19:26Z"), {}, {}, now=NOW)
    assert expired["release_sessions_remaining"] == 0 and expired["release_expired"]


# ---------------------------------------------------------------- log severity
@pytest.mark.parametrize("health,expected", [
    ("RELEASE_EXPIRED", "NOTICE"), ("DNA_EXHAUSTED", "NOTICE"),
    ("RELEASE_EXPIRING", "WARNING"), ("DNA_LOW", "WARNING"), ("TOKEN_EXPIRY_WARNING", "WARNING"),
    ("OPEN_ORDER_BLOCKED", "WARNING"), ("AUTH_BACKOFF", "WARNING"),
    ("EXECUTION_LIMIT_BLOCKED", "ERROR"), ("ERROR", "ERROR"), ("FEE_OVERDUE", "ERROR"),
    ("TICK_OK", "INFO"), ("PASS_MARKET_CLOSED", "INFO")])
def test_severity_by_business_status(health, expected):
    assert observability.severity_for(health) == expected


def test_a_paused_manual_reconciliation_stays_an_info_heartbeat():
    assert observability.severity_for("MANUAL_RECONCILIATION_REQUIRED", paused=True) == "INFO"
    assert observability.severity_for("MANUAL_RECONCILIATION_REQUIRED") == "ERROR"


@pytest.mark.parametrize("flags,status,severity", [
    ({"release_expired": True, "release_expiring": True}, "RELEASE_EXPIRED", "NOTICE"),
    ({"dna_exhausted": True}, "DNA_EXHAUSTED", "NOTICE"),
    ({"release_expiring": True}, "RELEASE_EXPIRING", "WARNING")])
def test_an_expired_horizon_is_not_a_warning_on_every_tick(flags, status, severity, capsys):
    observability.emit_tick({"pipeline_status": "TICK_OK", "operational_health": flags}, 200)
    event = json.loads(capsys.readouterr().out)
    assert event["business_status"] == status and event["severity"] == severity


# ----------------------------------------------------------------- webhook events
class Response:
    status_code = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


@pytest.fixture
def webhook(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "test-account")
    monkeypatch.setenv("LEGO_SYMBOL", "UBER")
    posts = []
    monkeypatch.setattr(alerting.requests, "post",
                        lambda *a, **k: posts.append(k["json"]) or Response())
    return posts


def horizon_body(**health):
    return {"business_status": "TICK_OK", "operational_health": health}


def test_expired_release_and_low_dna_each_alert_once_with_their_end(webhook):
    body = horizon_body(release_expired=True, release_expiring=True,
                        release_expiry_utc="2026-10-01T06:19:26+00:00",
                        dna_low=True, dna_end_utc="2026-10-05T15:00:00+00:00")
    assert alerting.notify_tick(body)
    assert {(p["event"], p["expires_at"]) for p in webhook} == {
        ("RELEASE_EXPIRED", "2026-10-01T06:19:26+00:00"),
        ("DNA_LOW", "2026-10-05T15:00:00+00:00")}
    assert all(p["symbol"] == "UBER" for p in webhook)
    assert not alerting.notify_tick(body)                          # 24h cooldown, not one per tick
    assert len(webhook) == 2


def test_exhausted_dna_supersedes_low_dna(webhook):
    alerting.notify_tick(horizon_body(dna_low=True, dna_exhausted=True,
                                      dna_end_utc="2026-10-05T15:00:00+00:00"))
    assert [p["event"] for p in webhook] == ["DNA_EXHAUSTED"]


def test_the_expiry_that_follows_a_warning_is_not_suppressed_by_its_cooldown(webhook):
    assert alerting.notify_tick(horizon_body(release_expiring=True,
                                             release_expiry_utc="2026-10-07T20:00:00+00:00"))
    assert alerting.notify_tick(horizon_body(release_expired=True, release_expiring=True,
                                             release_expiry_utc="2026-10-07T20:00:00+00:00"))
    assert [p["event"] for p in webhook] == ["RELEASE_EXPIRING", "RELEASE_EXPIRED"]


def test_action_and_horizon_events_are_sent_together(webhook):
    body = {"business_status": "EXECUTION_LIMIT_BLOCKED",
            "operational_health": {"release_expiring": True,
                                   "release_expiry_utc": "2026-10-07T20:00:00+00:00"}}
    assert alerting.notify_tick(body)
    assert [p["event"] for p in webhook] == ["EXECUTION_LIMIT_BLOCKED", "RELEASE_EXPIRING"]


@pytest.mark.parametrize("body", [
    {}, {"business_status": "TICK_OK"}, horizon_body(release_expiring=False, dna_low=False),
    {"business_status": "INTENT_BLOCKED", "operational_health": None}])
def test_a_healthy_tick_sends_nothing(webhook, body):
    assert alerting.notify_tick(body) is False and webhook == []


def test_nothing_is_sent_without_a_webhook(monkeypatch):
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    assert alerting.notify_tick(horizon_body(release_expired=True)) is False


def test_a_failing_webhook_never_breaks_the_tick(monkeypatch, webhook):
    def boom(*args, **kwargs):
        raise OSError("receiver down")
    monkeypatch.setattr(alerting.requests, "post", boom)
    assert alerting.notify_tick(horizon_body(release_expired=True)) is False


# ----------------------------------------------------------------------- policies
RESOURCES = monitoring("lego-tick-uat", "projects/demo/notificationChannels/1")
HORIZON_STATUSES = ("DNA_LOW", "DNA_EXHAUSTED", "RELEASE_EXPIRING", "RELEASE_EXPIRED",
                    "TOKEN_EXPIRY_WARNING")


def condition_filter(name):
    return RESOURCES[name]["conditions"][0]["conditionMatchedLog"]["filter"]


def test_action_policy_no_longer_pages_for_steady_horizon_states():
    filter_ = condition_filter("health-policy")
    for needed in ("severity>=ERROR", "AUTH_BACKOFF", "OPERATOR_HALT", "OPEN_ORDER_BLOCKED"):
        assert needed in filter_
    for horizon in HORIZON_STATUSES:
        assert horizon not in filter_
    assert RESOURCES["health-policy"]["alertStrategy"]["notificationRateLimit"]["period"] == "300s"


def test_horizon_policy_covers_release_dna_and_token_at_a_slow_cadence():
    filter_ = condition_filter("horizon-policy")
    for status in HORIZON_STATUSES:
        assert status in filter_
    for flag in ("dna_low", "dna_exhausted", "release_expiring", "release_expired", "token_warning"):
        assert f"jsonPayload.operational_health.{flag}=true" in filter_
    assert "severity>=ERROR" not in filter_
    strategy = RESOURCES["horizon-policy"]["alertStrategy"]
    assert strategy["notificationRateLimit"]["period"] == "21600s" and strategy["autoClose"] == "86400s"
    assert "release-plan" in RESOURCES["horizon-policy"]["documentation"]["content"]
    assert RESOURCES["horizon-policy"]["notificationChannels"] == ["projects/demo/notificationChannels/1"]


def test_every_policy_is_serializable_and_the_tick_metric_is_unchanged():
    assert set(RESOURCES) == {"tick-metric", "health-policy", "horizon-policy", "absence-policy"}
    json.dumps(RESOURCES)
    assert RESOURCES["tick-metric"]["name"] == "lego_tick_completed_lego_tick_uat"
