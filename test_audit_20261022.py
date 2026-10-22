"""Audit 2026-10-22 (docs/AUDIT_20261022_TH.md).

Revisions lego-tick-uat-00050 … 00054 (main@99000da, candidate a465c2d8… on 00054), UAT session
2026-10-21 13:23Z → 2026-10-22 04:22Z. The 16 intents below are the real ones of chain
AAPL_5cac601a0865 (webull_lego_order_audit); holdings 9.89001 AAPL throughout.

Q1  Every READY_BUY of the session (6 UBER on 00051, 16 AAPL on 00054) ended SUPPRESSED_STATE_CHANGED
    with reasons ["quote_age_limit"]: the sandbox snapshot (last_trade_time and quote_time alike) said
    13:40:31Z at 16:10Z and 13:52:38Z at 19:45Z, so the feed fell 8,987 → 21,154 s behind the clock
    against the UAT allowance of 1,260 s. No order reached Preview. On UAT a quote now has to come
    from the open session (the previous close still fails); PROD keeps its 60 s.
Q2  The tick log said SUPPRESSED_STATE_CHANGED and nothing else; it now carries the reasons.
Q3  59 ticks logged only "WebullConfigError" (NFLX and MSFT are not OC on UAT; 00050 also failed at
    the instrument profile for 15 minutes and its trace was deleted with the RTDB). The profile
    refusals now carry an enum reason, which the tick log and webull_lego_errors keep.
Q4  tools.trace_audit had no finding for a day on which no order reached the broker.

Nothing here touches a broker or a database: the RTDB is the in-memory fake of conftest.py.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import decision_service
import execution_service as execution
import lego_outbox as outbox
import main
import observability
import webull_io
from conftest import FAKE_DB
from lego_one_row import Config
from lego_state import AUDIT_PATH
from test_broker_reject_safety import decision_setup
from test_dispatch_overshoot import CFG, NOW, dispatch_fixture, snapshot
from tools import trace_audit as ta

UTC = timezone.utc
AAPL = Config("AAPL", 10000, diff=25, strategy_id="shannon_demon_lego_v2",
              decimal_precision=2, quantity_increment=0.01)
HOLDINGS = 9.89001
UAT_LIMIT = execution.DEFAULT_MAX_DISPATCH_QUOTE_AGE_SECONDS + execution.UAT_QUOTE_DELAY_SECONDS

# slot, run, decision_time, decision_price, quantity, dispatch_price, quote_time,
# decision_age_seconds, quote_age_seconds (all as stored in webull_lego_order_audit)
SESSION = [
    ("2026-10-21:10", "76cc71ea", "2026-10-21T16:10:05Z", 340.375, 19.48, 340.375, "2026-10-21T13:40:31.282Z", 13.078858, 8986.796858),
    ("2026-10-21:11", "4fd18990", "2026-10-21T16:15:05Z", 340.205, 19.5, 340.2, "2026-10-21T13:40:41.482Z", 19.481263, 9282.999263),
    ("2026-10-21:12", "75ff98d4", "2026-10-21T16:30:05Z", 340.6, 19.46, 340.6, "2026-10-21T13:41:24.446Z", 9.042759, 10129.596759),
    ("2026-10-21:13", "d7f69bea", "2026-10-21T16:45:05Z", 340.75, 19.45, 340.74, "2026-10-21T13:41:59.046Z", 7.164982, 10993.118982),
    ("2026-10-21:14", "bf2c1932", "2026-10-21T17:00:05Z", 340.87, 19.44, 340.87, "2026-10-21T13:42:27.850Z", 8.5694, 11865.7194),
    ("2026-10-21:15", "99d2762e", "2026-10-21T17:15:05Z", 340.87, 19.44, 340.87, "2026-10-21T13:42:33.392Z", 10.650877, 12762.258877),
    ("2026-10-21:16", "a5471a3b", "2026-10-21T17:30:05Z", 340.34, 19.49, 340.34, "2026-10-21T13:45:35.985Z", 7.065805, 13476.080805),
    ("2026-10-21:17", "c834171d", "2026-10-21T17:45:05Z", 340.34, 19.49, 340.34, "2026-10-21T13:46:15.192Z", 7.438318, 14337.246318),
    ("2026-10-21:18", "68c20f3c", "2026-10-21T18:00:05Z", 340.57, 19.47, 340.57, "2026-10-21T13:47:03.593Z", 6.858473, 15188.265473),
    ("2026-10-21:19", "a9fdf96d", "2026-10-21T18:15:05Z", 340.29, 19.49, 340.24, "2026-10-21T13:47:52.657Z", 10.074776, 16042.417776),
    ("2026-10-21:20", "55bfb715", "2026-10-21T18:30:05Z", 340.29, 19.49, 340.29, "2026-10-21T13:48:29.541Z", 8.145743, 16903.604743),
    ("2026-10-21:21", "a696f068", "2026-10-21T18:45:05Z", 340.18, 19.5, 340.18, "2026-10-21T13:49:20.001Z", 7.243715, 17752.242715),
    ("2026-10-21:22", "d20c0289", "2026-10-21T19:00:05Z", 340.335, 19.49, 340.335, "2026-10-21T13:50:14.229Z", 8.347579, 18599.118579),
    ("2026-10-21:23", "c59ce049", "2026-10-21T19:15:05Z", 340.445, 19.48, 340.445, "2026-10-21T13:51:10.520Z", 7.24974, 19441.72974),
    ("2026-10-21:24", "5bdb1870", "2026-10-21T19:30:05Z", 340.06, 19.51, 340.066, "2026-10-21T13:52:09.839Z", 9.670528, 20284.831528),
    ("2026-10-21:25", "88d2fae0", "2026-10-21T19:45:05Z", 340.28, 19.49, 340.28, "2026-10-21T13:52:38.852Z", 8.245801, 21154.393801),
]


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    FAKE_DB.store.clear()
    webull_io.reset_clients()
    execution.configure(main)
    decision_service.configure(main)
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("WEBULL_ENV", "UAT")

    class FixedNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(execution, "datetime", FixedNow)
    monkeypatch.setattr(outbox, "datetime", FixedNow)
    yield
    FAKE_DB.store.clear()
    webull_io.reset_clients()


def utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def intent_at(decision_time, price, quantity):
    return {"decision_price": price, "decision_holdings": HOLDINGS, "quantity": quantity,
            "signal": 1, "side": "BUY", "decision_time": decision_time, "created_at": decision_time}


def verdict(intent, quote_time, price, now, **options):
    fresh = {"price": price, "holdings": HOLDINGS, "quote_time": quote_time}
    return execution._dispatch_quote_safety(
        AAPL, intent, fresh, now_utc=now, max_price_drift_bps=100,
        max_quote_age_seconds=UAT_LIMIT, max_decision_age_seconds=360, **options)


# ------------------------------------------------------------------ Q1: the real session

@pytest.mark.parametrize("slot,run,decided,price,qty,dispatch_price,quoted,decision_age,quote_age", SESSION,
                         ids=[row[0] for row in SESSION])
def test_every_intent_of_the_session_was_stopped_only_by_the_wall_clock_age(
        slot, run, decided, price, qty, dispatch_price, quoted, decision_age, quote_age):
    now = utc(decided) + timedelta(seconds=decision_age)
    old = verdict(intent_at(decided, price, qty), quoted, dispatch_price, now)
    assert old["reasons"] == ["quote_age_limit"]                 # what the outbox recorded
    assert old["quote_age_seconds"] == pytest.approx(quote_age, abs=1e-6)
    assert old["decision_age_seconds"] == pytest.approx(decision_age, abs=1e-6)
    assert old.get("quote_not_before") is None


@pytest.mark.parametrize("slot,run,decided,price,qty,dispatch_price,quoted,decision_age,quote_age", SESSION,
                         ids=[row[0] for row in SESSION])
def test_on_uat_a_quote_of_the_open_session_passes_whatever_the_feed_lag(
        slot, run, decided, price, qty, dispatch_price, quoted, decision_age, quote_age):
    now = utc(decided) + timedelta(seconds=decision_age)
    new = verdict(intent_at(decided, price, qty), quoted, dispatch_price, now, session_quote_floor=True)
    assert new["ok"] is True and new["reasons"] == []
    assert new["quote_not_before"] == "2026-10-21T13:30:00Z"
    assert new["quote_age_seconds"] == pytest.approx(quote_age, abs=1e-6)   # still recorded


def test_the_previous_close_still_fails_at_the_open():
    # Slot 0 of every UAT day: the snapshot still shows the previous session's last trade.
    now = utc("2026-10-21T13:30:06Z")
    stale = verdict(intent_at("2026-10-21T13:30:05Z", 340.375, 19.48),
                    "2026-10-20T19:59:59.000Z", 340.375, now, session_quote_floor=True)
    assert stale["reasons"] == ["quote_age_limit"]
    assert stale["quote_not_before"] == "2026-10-21T13:30:00Z"


@pytest.mark.parametrize("now,quote_age,ok", [
    ("2026-10-24T16:00:00Z", 1200, True),       # Saturday: no session, the 1,260 s rule decides
    ("2026-10-24T16:00:00Z", 1300, False),
    ("2026-10-22T13:00:00Z", 1300, False),      # before the open of a session day
    ("2026-10-21T20:00:30Z", 1300, False),      # after the close
])
def test_outside_a_session_the_wall_clock_allowance_still_decides(now, quote_age, ok):
    moment = utc(now)
    decided = (moment - timedelta(seconds=10)).isoformat()
    quoted = (moment - timedelta(seconds=quote_age)).isoformat()
    result = verdict(intent_at(decided, 340.205, 19.5), quoted, 340.205, moment, session_quote_floor=True)
    assert result["ok"] is ok
    assert result["quote_not_before"] is None


def test_the_floor_never_admits_a_quote_from_the_future_or_an_old_decision():
    now = utc("2026-10-21T16:15:24Z")
    future = verdict(intent_at("2026-10-21T16:15:05Z", 340.205, 19.5),
                     "2026-10-21T16:15:40Z", 340.205, now, session_quote_floor=True)
    assert future["reasons"] == ["quote_time_in_future"]
    old_decision = verdict(intent_at("2026-10-21T16:05:00Z", 340.205, 19.5),
                           "2026-10-21T13:40:41.482Z", 340.205, now, session_quote_floor=True)
    assert old_decision["reasons"] == ["decision_age_limit"]


# ------------------------------------------------------- Q1: every dispatch check uses it

def lagged_snapshot(quote_time):
    return lambda *_: {**snapshot(), "quote_time": quote_time}


def test_a_lagged_uat_quote_of_this_session_passes_all_five_checks_and_places_once(monkeypatch):
    # NOW is 2026-09-21T15:00Z; a quote of 13:35Z is 85 minutes old, four times the old allowance.
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch, environment="UAT")
    monkeypatch.setattr(execution, "fetch_snapshot", lagged_snapshot("2026-09-21T13:35:00Z"))
    result = execution._dispatch_or_reconcile_one(client, object(), CFG, intent, claim, runtime)
    assert result["status"] == "SUBMITTED", result
    assert len(client.order_v3.preview_order.calls) == len(client.order_v3.place_order.calls) == 1
    evidence = FAKE_DB.reference(f"{AUDIT_PATH}/{intent['run_id']}").get()["dispatch_quote_check"]
    assert evidence["ok"] is True and evidence["quote_not_before"] == "2026-09-21T13:30:00Z"
    assert evidence["quote_age_seconds"] == 5100.0


def test_prod_keeps_its_sixty_seconds_for_the_same_quote(monkeypatch):
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch, environment="PROD")
    monkeypatch.setattr(execution, "fetch_snapshot", lagged_snapshot("2026-09-21T13:35:00Z"))
    result = execution._dispatch_or_reconcile_one(client, object(), CFG, intent, claim, runtime)
    assert result["status"] == "SUPPRESSED_STATE_CHANGED"
    assert result["state_change_reasons"] == ["quote_age_limit"]
    assert client.order_v3.preview_order.calls == client.order_v3.place_order.calls == []
    stored = outbox.read_intent(intent["chain_key"], intent["run_id"])
    assert stored["max_quote_age_seconds"] == execution.PROD_MAX_DISPATCH_QUOTE_AGE_SECONDS
    assert stored.get("quote_not_before") is None


def test_a_uat_quote_from_the_previous_session_is_still_refused(monkeypatch):
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch, environment="UAT")
    monkeypatch.setattr(execution, "fetch_snapshot", lagged_snapshot("2026-09-18T19:59:59Z"))
    result = execution._dispatch_or_reconcile_one(client, object(), CFG, intent, claim, runtime)
    assert result["status"] == "SUPPRESSED_STATE_CHANGED"
    assert result["state_change_reasons"] == ["quote_age_limit"]
    assert client.order_v3.preview_order.calls == client.order_v3.place_order.calls == []


# ------------------------------------------------------------- Q2/Q3: what the log says

def emitted(capsys, body, code):
    observability.emit_tick(body, code)
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_the_tick_log_names_why_dispatch_was_suppressed(capsys):
    event = emitted(capsys, {
        "pipeline_status": "TICK_OK",
        "decision": {"status": "READY_BUY", "pipeline_status": "ROW_COMMITTED", "committed": True},
        "dispatch": {"results": [{"run_id": "4fd189904201cd120180ad809bcf3025",
                                  "status": "SUPPRESSED_STATE_CHANGED",
                                  "state_change_reasons": ["quote_age_limit"],
                                  "price_drift_bps": 0.1469702091383418}]}}, 200)
    item = event["execution"][0]
    assert item["status"] == "SUPPRESSED_STATE_CHANGED"
    assert item["state_change_reasons"] == ["quote_age_limit"]


def test_the_tick_log_names_which_instrument_check_refused_but_never_the_message(capsys):
    message = "NFLX ไม่อยู่สถานะ tradable (OC); status='SUSPENDED' account_id=123456"
    event = emitted(capsys, {
        "pipeline_status": "TICK_DECISION_ERROR",
        "decision": {"status": "ERROR", "pipeline_status": "SNAPSHOT_OR_ENGINE_ERROR",
                     "error": message, "type": "WebullConfigError",
                     "reason": "INSTRUMENT_NOT_TRADABLE"}}, 500)
    assert event["errors"] == [{"phase": "decision", "type": "WebullConfigError",
                                "reason": "INSTRUMENT_NOT_TRADABLE"}]
    assert "SUSPENDED" not in json.dumps(event) and "123456" not in json.dumps(event)


@pytest.mark.parametrize("reason", ["not an enum", "x", "A" * 41, 7, ["INSTRUMENT_NOT_TRADABLE"]])
def test_anything_but_an_enum_reason_stays_out_of_the_log(capsys, reason):
    event = emitted(capsys, {"pipeline_status": "TICK_DECISION_ERROR",
                             "decision": {"status": "ERROR", "error": "e", "type": "WebullConfigError",
                                          "reason": reason}}, 500)
    assert event["errors"] == [{"phase": "decision", "type": "WebullConfigError"}]


def profile(**fields):
    item = {"symbol": "NFLX", "status": "OC", "currency": "USD", "lot_size": "1.0000000000",
            "category": "US_STOCK", "fractionable": True}
    item.update(fields)
    return {"data": [{k: v for k, v in item.items() if v is not None}]}


@pytest.mark.parametrize("payload,reason", [
    (profile(status="SUSPENDED"), "INSTRUMENT_NOT_TRADABLE"),
    (profile(status=""), "INSTRUMENT_NOT_TRADABLE"),
    ({"data": []}, "INSTRUMENT_NOT_FOUND"),
    ({}, "INSTRUMENT_NOT_FOUND"),
    (profile(currency="THB"), "INSTRUMENT_NOT_USD"),
    (profile(lot_size="0"), "INSTRUMENT_BAD_LOT"),
    (profile(category="US_OPTION"), "INSTRUMENT_CATEGORY"),
    (profile(fractionable=None), "INSTRUMENT_FRACTIONABLE_UNKNOWN"),
    (profile(fractionable="true"), "INSTRUMENT_FRACTIONABLE_UNKNOWN"),
])
def test_each_instrument_profile_refusal_carries_its_reason_and_keeps_its_type(payload, reason):
    with pytest.raises(webull_io.WebullConfigError) as caught:
        webull_io.parse_instrument_capability(payload, "NFLX")
    assert type(caught.value) is webull_io.WebullConfigError
    assert webull_io.error_reason(caught.value) == reason


def test_a_tradable_profile_still_parses_and_other_errors_have_no_reason():
    assert webull_io.parse_instrument_capability(profile(), "NFLX").status == "OC"
    assert webull_io.error_reason(ValueError("x")) is None
    odd = RuntimeError("urllib3-style")
    odd.reason, odd.lego_reason = ValueError("object"), object()
    assert webull_io.error_reason(odd) is None


def test_a_refused_instrument_reaches_the_decision_body_and_the_error_record(monkeypatch):
    runtime, cfg = decision_setup(monkeypatch, allow_fractional=True)
    monkeypatch.setattr(decision_service, "fetch_instrument_capability", lambda *_a:
                        webull_io.parse_instrument_capability(profile(symbol="TSLA", status="SUSPENDED"), "TSLA"))
    body, code = decision_service.run_decision(None, runtime, cfg)
    assert code == 500 and body["pipeline_status"] == "SNAPSHOT_OR_ENGINE_ERROR"
    assert body["type"] == "WebullConfigError" and body["reason"] == "INSTRUMENT_NOT_TRADABLE"
    records = list((FAKE_DB.reference(decision_service.ERRORS_PATH).get() or {}).values())
    assert [r["reason"] for r in records] == ["INSTRUMENT_NOT_TRADABLE"]


# --------------------------------------------------------------------- Q4: the digest

def _world(statuses):
    from test_trace_audit import make_world
    world = make_world(stuck=False)
    intents = world["webull_lego_order_outbox"]["TESTCO_a1b2c3d4e5f6"]
    for run, status in zip(sorted(intents), statuses):
        if status == "SUPPRESSED_STATE_CHANGED":
            intents[run].update(status=status, reasons=["quote_age_limit"],
                                terminal_reason="dispatch blocked: quote_age_limit")
    return world


def test_the_digest_pages_when_no_intent_reached_the_broker():
    findings = {f["code"]: f for f in ta.build_report(rtdb=_world(["SUPPRESSED_STATE_CHANGED"] * 3))["findings"]}
    found = findings["DISPATCH_SUPPRESSED"]
    assert found["level"] == "P1"
    assert "3 of 3 intents" in found["text"] and "quote_age_limit×3" in found["text"]
    assert "no order of this export reached the broker" in found["text"]


def test_the_digest_names_a_single_suppressed_intent_without_paging():
    findings = {f["code"]: f for f in ta.build_report(
        rtdb=_world(["SUPPRESSED_STATE_CHANGED", "FILLED", "FILLED"]))["findings"]}
    assert findings["DISPATCH_SUPPRESSED"]["level"] == "P2"
    assert "1 of 3 intents" in findings["DISPATCH_SUPPRESSED"]["text"]


def test_a_session_without_suppressed_intents_has_no_such_finding():
    codes = {f["code"] for f in ta.build_report(rtdb=_world(["FILLED"] * 3))["findings"]}
    assert "DISPATCH_SUPPRESSED" not in codes


# -------------------------------------------------------------- the user's UAT policy

def test_the_continuous_uat_policy_is_the_one_the_user_set():
    from pathlib import Path
    policy = dict(line.split("=", 1) for line in Path("deploy/uat-continuous.env.example").read_text().splitlines()
                  if line and not line.startswith("#"))
    assert {k: policy[k] for k in ("WEBULL_ENV", "LEGO_MODE", "LEGO_ACTIVE", "LEGO_SYMBOL",
                                   "LEGO_FIX_C", "LEGO_ALLOW_FRACTIONAL")} == {
        "WEBULL_ENV": "UAT", "LEGO_MODE": "trade", "LEGO_ACTIVE": "true", "LEGO_SYMBOL": "UBER",
        "LEGO_FIX_C": "10000", "LEGO_ALLOW_FRACTIONAL": "true"}
    assert "LEGO_SYMBOL_OVERRIDE=UBER " in Path("deploy/continuous-uat.sh").read_text()
