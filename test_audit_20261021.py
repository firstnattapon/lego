"""Audit 2026-10-21 (docs/AUDIT_20261021_TH.md).

Revision lego-tick-uat-00049-duk (main@befa78c, candidate d06333fe...), UAT session 2026-10-20
13:29Z-20:45Z, RTDB heartbeat 2026-10-21T03:42:05Z. The numbers below are the real ones of that session.

C1a The pre-place gate of execution_service._dispatch_or_reconcile_one caught Exception, and
    tick_runtime.TickDeadlineExceeded is a TimeoutError: running out of tick time while the gate was
    still reading ended the intent as terminal NOT_PLACED. The same wrapper sat around
    fetch_instrument_capability (it re-raised everything as FractionalGateError).
C1b GET /trading/assets/balances/get answered 417 OPENAPI_SYSTEM_ERROR three times in a row for both
    BUY intents of the session (14:00:24-33Z BUY 0.74 with 8.45 s of tick budget left, 16:45:25-33Z
    BUY 0.56 with 7.28 s left); both ended as NOT_PLACED with 14 minutes of their slot to go. A
    transient read failure before the place fence now defers the intent at most MAX_GATE_DEFERRALS
    times; the next tick runs the whole gate again.
C2  Row v2 (step 781, run 50c6a237) came out of its snapshot at 13:45:38.159Z with 1.715 s of the
    35 s budget left (a 15.35 s read timeout, a 2 s back-off, a 10.24 s retry), was committed, and
    its tick ended with 0.878 s left: the recorder (1 s floor) dropped it, skipped_budget = 1 and no
    D08 equation for v2. Over the 26 committed rows the budget left after the decision-stage snapshot was
    1.72 s for v2 and at least 17.06 s for every other row.
C3  _AUTH_PROFILE is stamped before ensure_token_fresh() / verify_account_access(), _CLIENTS 1.536 s
    later (15:45:06.935Z / 15:45:08.471Z). At 16:45:07.124Z build_clients() had been a cache hit, the
    1.159 s instrument call followed, and token_health() found the profile 0.189 s past its TTL
    while the pair was still served: a false webull_token warning on UAT.

Nothing here touches a broker or a database: the RTDB is the in-memory fake of conftest.py.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal

import pytest

import broker_circuit as circuit
import decision_service
import execution_service as execution
import flight_recorder as fr
import lego_outbox as outbox
import main
import tick_runtime
import webull_io
from conftest import FAKE_DB, fake_trade_client
from lego_one_row import Anchor, Config, compute_row
from test_audit_20261016 import decision_circuit, seed_decision_halt
from test_broker_reject_safety import decision_setup
from test_dispatch_overshoot import CFG, NOW, dispatch_fixture, snapshot


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

    # Lease claims, the dispatch gate and the quote-age check must share one clock.
    monkeypatch.setattr(execution, "datetime", FixedNow)
    monkeypatch.setattr(outbox, "datetime", FixedNow)
    yield
    FAKE_DB.store.clear()
    webull_io.reset_clients()


# ------------------------------------------------------------------------------ helpers

def broker_error(status=417, code="OPENAPI_SYSTEM_ERROR",
                 request_id="dc38ceba-8ffd-4d50-8535-70dbb8c48639"):
    """The real SDK exception, so is_transient_exception / broker_error_details see what they see live."""
    from webull.core.exception.exceptions import ServerException
    return ServerException(code, "System error.", http_status=status, request_id=request_id)


def raises(error):
    def fail(*_args, **_kwargs):
        raise error
    return fail


def deadline(*_args, **_kwargs):
    raise tick_runtime.TickDeadlineExceeded("tick time budget exhausted; resume recovery next tick")


def attempt(client, intent, claim, runtime):
    return execution._dispatch_or_reconcile_one(client, object(), CFG, intent, claim, runtime)


def stored(intent):
    return outbox.read_intent(intent["chain_key"], intent["run_id"])


def always_the_same_quote(monkeypatch):
    monkeypatch.setattr(execution, "fetch_snapshot", lambda *_: snapshot())


# ------------------------------------------------------------------------------ C1a

def test_deadline_while_reading_buying_power_is_not_a_verdict(monkeypatch):
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch)
    monkeypatch.setattr(execution, "fetch_buying_power", deadline)
    with tick_runtime.tick_scope("c1a-balance"):
        with pytest.raises(tick_runtime.TickDeadlineExceeded):
            attempt(client, intent, claim, runtime)
        w23 = [e for e in fr._CURRENT.get().events if e.get("n") == "W23"]
    assert [e.get("tag") for e in w23] == ["deferred"]
    assert w23[0]["ok"] is False and w23[0]["x"]["reason"] == "tick_deadline"
    assert stored(intent)["status"] == "PENDING_DISPATCH"
    assert not stored(intent).get("place_attempted")
    assert client.order_v3.place_order.calls == []


def test_deadline_while_previewing_is_not_a_verdict(monkeypatch):
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch)
    monkeypatch.setattr(execution, "preview_market_order_result", deadline)
    with pytest.raises(tick_runtime.TickDeadlineExceeded):
        attempt(client, intent, claim, runtime)
    assert stored(intent)["status"] == "PENDING_DISPATCH"
    assert client.order_v3.place_order.calls == []


def test_deadline_while_reading_the_instrument_capability_is_not_wrapped(monkeypatch):
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch)
    intent.pop("instrument_capability")             # an intent without provenance asks the broker
    monkeypatch.setattr(execution, "fetch_instrument_capability", deadline)
    with pytest.raises(tick_runtime.TickDeadlineExceeded):
        attempt(client, intent, claim, runtime)
    assert stored(intent)["status"] == "PENDING_DISPATCH"
    assert client.order_v3.place_order.calls == []


def test_any_other_capability_failure_still_fails_closed(monkeypatch):
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch)
    intent.pop("instrument_capability")
    monkeypatch.setattr(execution, "fetch_instrument_capability", raises(broker_error(502, "SDK.UNKNOWNSERVERERROR")))
    result = attempt(client, intent, claim, runtime)
    assert result["status"] == "NOT_PLACED" and "unable to verify instrument capability" in result["error"]
    assert client.order_v3.place_order.calls == []


def test_the_order_worker_gives_back_its_claims_when_the_gate_hits_the_deadline(monkeypatch):
    runtime, cfg = decision_setup(monkeypatch, allow_fractional=False)      # TSLA, flat: READY_BUY 13
    body, code = decision_service.run_decision(None, runtime, cfg)
    assert code == 200 and body["committed"], body
    ck, run_id = main.chain_key(cfg), body["run_id"]
    client = fake_trade_client(preview={"estimated_cost": "4883.45", "estimated_transaction_fee": "1"})

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    # The worker takes its clock and broker reads from main (execution.configure), as in the
    # three-rejects test of test_broker_reject_safety.py.
    monkeypatch.setattr(main, "datetime", Clock)
    monkeypatch.setattr(main, "build_clients", lambda: (client, object()))
    monkeypatch.setattr(main, "fetch_open_orders", lambda *_: [])
    monkeypatch.setattr(main, "fetch_snapshot", lambda *_: {
        "price": 375.65, "holdings": 0, "captured_at": NOW.isoformat(), "quote_time": NOW.isoformat()})
    execution.configure(main)
    monkeypatch.setattr(execution, "fetch_buying_power", deadline)

    with pytest.raises(tick_runtime.TickDeadlineExceeded):
        execution._run_order_worker(cfg, limit=1, runtime=runtime)

    intent = outbox.read_intent(ck, run_id)
    assert intent["status"] == "PENDING_DISPATCH" and not intent.get("claim_owner")
    scope = outbox.account_symbol_fence_key(webull_io.runtime_identity_fingerprint(), cfg.symbol)
    fence = FAKE_DB.reference(f"{outbox.DISPATCH_LOCK_PATH}/{scope}").get() or {}
    assert not fence.get("owner") and not fence.get("inflight_run_id")
    assert client.order_v3.place_order.calls == []


# ------------------------------------------------------------------------------ C1b

@pytest.mark.parametrize("request_id,status,code", [
    ("aba7c0f0-7fd6-4955-bfd5-c83b7ebc5ec0", 417, "OPENAPI_SYSTEM_ERROR"),      # 14:00:33Z, BUY 0.74
    ("dc38ceba-8ffd-4d50-8535-70dbb8c48639", 417, "OPENAPI_SYSTEM_ERROR"),      # 16:45:33Z, BUY 0.56
    ("ad211fa6-416d-49e3-a2dc-422ed32ba62f", 504, "GATEWAY_TIMEOUT"),           # 18:00:15Z, positions
])
def test_a_transient_balance_error_defers_twice_then_ends_not_placed(monkeypatch, request_id, status, code):
    assert execution.MAX_GATE_DEFERRALS == 2
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch)
    always_the_same_quote(monkeypatch)
    monkeypatch.setattr(execution, "fetch_buying_power", raises(broker_error(status, code, request_id)))

    first = attempt(client, intent, claim, runtime)
    s1 = stored(intent)
    assert first["status"] == s1["status"] == "PENDING_DISPATCH"
    assert s1["gate_defer_count"] == 1 and s1["dispatch_check_phase"] == "pre_place_read"
    assert s1["broker_error"]["http_status"] == status and s1["broker_error"]["code"] == code
    assert s1["broker_error"]["request_id"] == request_id

    second = attempt(client, s1, claim, runtime)
    s2 = stored(intent)
    assert second["status"] == s2["status"] == "PENDING_DISPATCH" and s2["gate_defer_count"] == 2

    third = attempt(client, s2, claim, runtime)
    s3 = stored(intent)
    assert third["status"] == s3["status"] == "NOT_PLACED" and s3["gate_defer_count"] == 2
    assert not s3.get("place_attempted")
    assert client.order_v3.place_order.calls == []


def test_a_transient_preview_error_defers_the_same_way(monkeypatch):
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch)
    always_the_same_quote(monkeypatch)
    monkeypatch.setattr(execution, "preview_market_order_result", raises(broker_error(417)))
    assert attempt(client, intent, claim, runtime)["status"] == "PENDING_DISPATCH"
    assert stored(intent)["gate_defer_count"] == 1
    assert client.order_v3.place_order.calls == []


def test_a_buy_that_recovers_after_one_deferral_places_exactly_once(monkeypatch):
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch)
    always_the_same_quote(monkeypatch)
    calls = []

    def flaky(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise broker_error(417, request_id="96902146-e314-4a32-b3f3-ad3a037e6448")
        return Decimal("10000")

    monkeypatch.setattr(execution, "fetch_buying_power", flaky)
    assert attempt(client, intent, claim, runtime)["status"] == "PENDING_DISPATCH"
    s1 = stored(intent)
    assert s1["gate_defer_count"] == 1 and client.order_v3.place_order.calls == []

    result = attempt(client, s1, claim, runtime)
    assert result["status"] == "SUBMITTED"
    assert stored(intent)["place_attempted"] is True
    assert len(client.order_v3.place_order.calls) == 1


@pytest.mark.parametrize("error", [
    RuntimeError("buying power could not be established"),
    ValueError("order payload identity/quantity differs from committed intent"),
    pytest.param("client-error", id="http-400-invalid-parameter"),
])
def test_a_failure_that_is_not_transient_ends_not_placed_at_once(monkeypatch, error):
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch)
    if error == "client-error":
        error = broker_error(400, "INVALID_PARAMETER")
    monkeypatch.setattr(execution, "fetch_buying_power", raises(error))
    result = attempt(client, intent, claim, runtime)
    assert result["status"] == stored(intent)["status"] == "NOT_PLACED"
    assert "gate_defer_count" not in stored(intent)
    assert client.order_v3.place_order.calls == []


def test_too_little_buying_power_is_still_a_verdict_not_a_retry(monkeypatch):
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch, buying_power=1)
    result = attempt(client, intent, claim, runtime)
    assert result["status"] == "NOT_PLACED" and "gate_defer_count" not in stored(intent)
    assert client.order_v3.place_order.calls == []


@pytest.mark.parametrize("count", [-1, -7, "garbage", "2", 2, 3])
def test_an_unreadable_negative_or_spent_defer_count_is_not_deferred_again(monkeypatch, count):
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch)
    always_the_same_quote(monkeypatch)
    intent["gate_defer_count"] = count
    monkeypatch.setattr(execution, "fetch_buying_power", raises(broker_error(417)))
    result = attempt(client, intent, claim, runtime)
    assert result["status"] == stored(intent)["status"] == "NOT_PLACED"


@pytest.mark.parametrize("count", [None, 0, "0", ""])
def test_an_absent_or_empty_count_means_no_deferral_has_been_used(monkeypatch, count):
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch)
    always_the_same_quote(monkeypatch)
    intent["gate_defer_count"] = count
    monkeypatch.setattr(execution, "fetch_buying_power", raises(broker_error(417)))
    assert attempt(client, intent, claim, runtime)["status"] == "PENDING_DISPATCH"
    assert stored(intent)["gate_defer_count"] == 1


def test_a_count_stored_as_a_numeral_still_counts(monkeypatch):
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch)
    always_the_same_quote(monkeypatch)
    intent["gate_defer_count"] = "1"
    monkeypatch.setattr(execution, "fetch_buying_power", raises(broker_error(417)))
    assert attempt(client, intent, claim, runtime)["status"] == "PENDING_DISPATCH"
    assert stored(intent)["gate_defer_count"] == 2


def test_a_deferred_prod_intent_is_suppressed_once_its_decision_is_older_than_120_seconds(monkeypatch):
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch, environment="PROD")
    always_the_same_quote(monkeypatch)
    monkeypatch.setattr(execution, "fetch_buying_power", raises(broker_error(417)))
    assert attempt(client, intent, claim, runtime)["status"] == "PENDING_DISPATCH"
    s1 = stored(intent)
    monkeypatch.setattr(execution, "fetch_buying_power", lambda *_: Decimal("10000"))

    late = {**s1, "decision_time": (NOW - timedelta(seconds=121)).isoformat()}
    result = attempt(client, late, claim, runtime)

    assert result["status"] == stored(intent)["status"] == "SUPPRESSED_STATE_CHANGED"
    assert "decision_age_limit" in stored(intent)["reasons"]
    assert client.order_v3.place_order.calls == []


# ------------------------------------------------------------------------------ C2

def after_snapshot(monkeypatch, left):
    """The tick has `left` seconds of its 35 s budget from the moment the decision snapshot returns."""
    inner = decision_service.fetch_snapshot

    def snap(*args, **kwargs):
        value = inner(*args, **kwargs)
        monkeypatch.setattr(tick_runtime, "remaining", lambda: left)
        monkeypatch.setattr(fr, "_remaining", lambda: left)
        return value

    monkeypatch.setattr(decision_service, "fetch_snapshot", snap)


@pytest.mark.parametrize("left", [1.715, 0.878, 2.5, 2.99])
def test_a_snapshot_that_leaves_under_three_seconds_defers_before_anything_is_written(monkeypatch, left):
    runtime, cfg = decision_setup(monkeypatch, allow_fractional=True)
    seed_decision_halt(20)                       # cooldown over: a READY row would open the UAT probe
    after_snapshot(monkeypatch, left)

    with tick_runtime.tick_scope("c2-defer"):
        body, code = decision_service.run_decision(None, runtime, cfg)
        nodes = [e.get("n") for e in fr._CURRENT.get().events]
        flushed = fr.finish({"pipeline_status": "TICK_DEFERRED", "business_status": "TICK_DEFERRED",
                             "decision": body}, code)

    assert code == 200 and body["pipeline_status"] == "TICK_DEFERRED"
    assert body["deferred_reason"] == "tick_deadline"
    assert "D08" in nodes and "T00" in nodes and "D10" not in nodes
    assert FAKE_DB.reference("webull_lego_rows").get() is None
    assert outbox.list_actionable(main.chain_key(cfg)) == []
    assert decision_circuit()["halted"] is True and "probe_total" not in decision_circuit()
    assert FAKE_DB.reference("webull_lego_warnings/BROKER_REJECT_AUTO_PROBE").get() is None
    # What the guard buys: the deferred tick is still written when the recorder's 1 s floor is met.
    assert flushed["written"] is (left >= fr.FLUSH_MIN_BUDGET_MUTATION_SECONDS)


@pytest.mark.parametrize("left", [3.0, 3.5, 17.06])
def test_a_snapshot_that_leaves_three_seconds_or_more_commits_and_keeps_the_equation(monkeypatch, left):
    runtime, cfg = decision_setup(monkeypatch, allow_fractional=True)
    seed_decision_halt(20)
    after_snapshot(monkeypatch, left)

    with tick_runtime.tick_scope("c2-commit"):
        body, code = decision_service.run_decision(None, runtime, cfg)
        nodes = [e.get("n") for e in fr._CURRENT.get().events]
        flushed = fr.finish({"pipeline_status": "TICK_OK", "business_status": "ROW_COMMITTED",
                             "decision": body}, code)

    assert code == 200 and body["pipeline_status"] == "ROW_COMMITTED" and body["committed"], body
    assert "D08" in nodes and "D10" in nodes
    assert decision_circuit()["status"] == circuit.HALF_OPEN and decision_circuit()["probe_total"] == 1
    assert flushed["written"] is True and "row_committed" in flushed["reasons"]


def test_a_tick_without_a_deadline_is_not_constrained_by_the_guard(monkeypatch):
    runtime, cfg = decision_setup(monkeypatch, allow_fractional=True)
    body, code = decision_service.run_decision(None, runtime, cfg)        # no tick_scope: remaining() is None
    assert code == 200 and body["committed"], body


def test_the_guard_trips_only_for_the_row_that_lost_its_equation():
    # Budget left right after the decision-stage snapshot, the five lowest of the 26 committed rows
    # of 2026-10-20 (step -> seconds); the other 21 rows had more than 22 s.
    lowest = {781: 1.72, 796: 17.06, 798: 20.19, 803: 20.80, 780: 22.20}
    guard = decision_service.DECISION_COMMIT_MIN_BUDGET_SECONDS
    assert fr.FLUSH_MIN_BUDGET_MUTATION_SECONDS < guard < min(v for k, v in lowest.items() if k != 781)
    assert {step for step, left in lowest.items() if left < guard} == {781}


# ------------------------------------------------------------------------------ C3

def profile(monkeypatch, *, enabled=False, profile_at=1000.0, pair_at=None, pair_key=None):
    key = webull_io._client_identity()
    monkeypatch.setattr(webull_io, "_AUTH_PROFILE", (key, profile_at, enabled))
    monkeypatch.setattr(webull_io, "_CLIENTS", None if pair_at is None else
                        (pair_key or key, pair_at, object(), object()))


def clock(monkeypatch, now):
    monkeypatch.setattr(webull_io.time, "monotonic", lambda: now)
    monkeypatch.setattr(webull_io, "read_local_token", lambda: None)    # UAT never writes token.txt


def test_a_profile_a_moment_older_than_its_client_pair_stays_valid_while_the_pair_is_served(monkeypatch):
    ttl = webull_io._client_cache_ttl()
    # 15:45:06.935Z profile, 15:45:08.471Z pair; token_health() at 16:45:07.124Z.
    profile(monkeypatch, profile_at=1000.0, pair_at=1001.536)
    clock(monkeypatch, 1000.0 + ttl + 0.189)
    assert webull_io._token_check_disabled() is True
    health = webull_io.token_health()
    assert health["ok"] is True and health["ready"] is True and health["reasons"] == []
    assert health["authentication_mode"] == "SIGNED_WITHOUT_TOKEN"


def test_once_the_pair_has_expired_too_the_profile_is_no_longer_trusted(monkeypatch):
    ttl = webull_io._client_cache_ttl()
    profile(monkeypatch, profile_at=1000.0, pair_at=1001.536)
    clock(monkeypatch, 1001.536 + ttl + 0.01)
    assert webull_io._token_check_disabled() is False
    assert webull_io.token_health()["ok"] is False


def test_without_a_pair_the_profile_keeps_its_own_ttl(monkeypatch):
    ttl = webull_io._client_cache_ttl()
    profile(monkeypatch, profile_at=1000.0)
    clock(monkeypatch, 1000.0 + ttl - 0.1)
    assert webull_io._token_check_disabled() is True
    clock(monkeypatch, 1000.0 + ttl + 0.189)
    assert webull_io._token_check_disabled() is False


def test_a_pair_built_for_another_identity_does_not_rescue_an_expired_profile(monkeypatch):
    ttl = webull_io._client_cache_ttl()
    other = ("another-endpoint",) + webull_io._client_identity()[1:]
    profile(monkeypatch, profile_at=1000.0, pair_at=1001.536, pair_key=other)
    clock(monkeypatch, 1000.0 + ttl + 0.189)
    assert webull_io._token_check_disabled() is False


def test_rotating_the_app_secret_drops_the_profile_even_with_a_live_pair(monkeypatch):
    profile(monkeypatch, profile_at=1000.0, pair_at=1000.5)
    clock(monkeypatch, 1001.0)
    assert webull_io._token_check_disabled() is True
    monkeypatch.setenv("WEBULL_APP_SECRET", "rotated-app-secret")
    assert webull_io._token_check_disabled() is False


@pytest.mark.parametrize("flag", [True, None, 0, "False"])
def test_only_an_explicit_false_means_that_token_checking_is_off(monkeypatch, flag):
    profile(monkeypatch, enabled=flag, profile_at=1000.0, pair_at=1000.5)
    clock(monkeypatch, 1001.0)
    assert webull_io._token_check_disabled() is False


def test_no_profile_means_token_checking_is_assumed_on(monkeypatch):
    monkeypatch.setattr(webull_io, "_AUTH_PROFILE", None)
    monkeypatch.setattr(webull_io, "_CLIENTS", (webull_io._client_identity(), 1000.0, object(), object()))
    clock(monkeypatch, 1001.0)
    assert webull_io._token_check_disabled() is False


# ------------------------------------------------------------------------------ evidence of 2026-10-20

# (version, step, slot, P_n, committed status, committed quantity, R_n) of the 26 rows in the RTDB export.
P0, HOLDINGS, FIX_C, DIFF = 69.96, 145.1328, 10000.0, 25.0
ROWS_20261020 = [
    (1, 780, "2026-10-20:0", 69.96, "READY_SELL", 2.19, 0),
    (2, 781, "2026-10-20:1", 69.3, "READY_SELL", 0.83, -94.7874395454377),
    (3, 782, "2026-10-20:2", 68.55, "READY_BUY", 0.74, -203.60244142077914),
    (4, 783, "2026-10-20:3", 69.47, "READY_SELL", 1.18, -70.28645445611649),
    (5, 784, "2026-10-20:4", 69.16, "READY_SELL", 0.54, -115.00989335311546),
    (6, 785, "2026-10-20:5", 69.37, "READY_SELL", 0.97, -84.69152753191234),
    (7, 786, "2026-10-20:6", 69.46, "PASS_BROKER_REJECT_HALT", 0.0, -71.72602834473317),
    (8, 787, "2026-10-20:7", 69.595, "READY_SELL", 1.44, -52.30924471037425),
    (9, 788, "2026-10-20:8", 69.31, "PASS_BROKER_REJECT_HALT", 0.0, -93.34454220507907),
    (10, 789, "2026-10-20:9", 69.135, "PASS_BROKER_REJECT_HALT", 0.0, -118.62535309819832),
    (11, 790, "2026-10-20:10", 68.83, "PASS_BROKER_REJECT_HALT", 0.0, -162.83953740128413),
    (12, 791, "2026-10-20:11", 68.825, "PASS_BROKER_REJECT_HALT", 0.0, -163.56599121730216),
    (13, 792, "2026-10-20:12", 68.835, "PASS_BROKER_REJECT_HALT", 0.0, -162.1131363549485),
    (14, 793, "2026-10-20:13", 68.635, "READY_BUY", 0.56, -191.21041446778284),
    (15, 794, "2026-10-20:14", 68.77, "PASS_THRESHOLD", 0.0, -171.5604681865656),
    (16, 795, "2026-10-20:15", 68.77, "PASS_THRESHOLD", 0.0, -171.5604681865656),
    (17, 796, "2026-10-20:16", 68.755, "PASS_THRESHOLD", 0.0, -173.74188975493206),
    (18, 797, "2026-10-20:17", 68.985, "PASS_THRESHOLD", 0.0, -140.34560490404357),
    (19, 798, "2026-10-20:18", 69.06, "PASS_THRESHOLD", 0.0, -129.47958188554878),
    (20, 799, "2026-10-20:19", 69.08, "READY_SELL", 0.37, -126.58396871923354),
    (21, 800, "2026-10-20:20", 69.0318, "PASS_BROKER_REJECT_HALT", 0.0, -133.56382155684085),
    (22, 801, "2026-10-20:21", 68.845, "PASS_BROKER_REJECT_HALT", 0.0, -160.6604925406558),
    (23, 802, "2026-10-20:22", 68.93, "PASS_BROKER_REJECT_HALT", 0.0, -148.3215325284071),
    (24, 803, "2026-10-20:23", 68.955, "PASS_BROKER_REJECT_HALT", 0.0, -144.69532224100217),
    (25, 804, "2026-10-20:24", 68.915, "PASS_BROKER_REJECT_HALT", 0.0, -150.49789003998953),
    (26, 805, "2026-10-20:25", 68.848, "PASS_BROKER_REJECT_HALT", 0.0, -160.22474054157465),
]


def floor_quantity(gap, price):
    return float(Decimal(str(abs(gap) / price)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def test_the_26_rows_of_20261020_follow_from_the_first_principles():
    """V = H*P, gap = V - FIX_C, R = FIX_C*ln(P/P0), E_mark = A - R, qty = floor(|gap|/P, 2 places)."""
    assert [row[0] for row in ROWS_20261020] == list(range(1, 27))
    assert [row[1] for row in ROWS_20261020] == list(range(780, 806))
    for version, _step, _slot, price, status, quantity, r_n in ROWS_20261020:
        gap = HOLDINGS * price - FIX_C
        assert r_n == pytest.approx(FIX_C * math.log(price / P0), abs=1e-9), version
        if status.startswith("READY"):
            assert abs(gap) >= DIFF and quantity == floor_quantity(gap, price), version
            assert status == ("READY_SELL" if gap > 0 else "READY_BUY"), version
        elif status == "PASS_THRESHOLD":
            assert abs(gap) < DIFF and quantity == 0, version
        else:                                   # the broker-reject halt overwrites the row, whatever the gap
            assert status == "PASS_BROKER_REJECT_HALT" and quantity == 0, version


def test_the_decision_engine_reproduces_the_rows_it_did_not_have_to_overwrite():
    cfg = Config("UBER", FIX_C, diff=DIFF, dna_code="bypass:6500", strategy_id="shannon_demon_lego_v2",
                 decimal_precision=2, quantity_increment=0.01)
    for version, step, _slot, price, status, quantity, r_n in ROWS_20261020:
        anchor = Anchor(version=version, dna_step=step, p0=P0, prev_price=P0, prev_actual=0.0,
                        prev_holdings=HOLDINGS, prev_excess=0.0)
        row = compute_row(cfg, {"price": price, "holdings": HOLDINGS,
                                "captured_at": "2026-10-20T14:00:00Z"}, anchor, dna_step=step)
        assert row["Rₙ อ้างอิง (USD)"] == pytest.approx(r_n, abs=1e-6), version
        assert row["ΔAₙ ต่อสเต็ป (USD)"] == 0 and row["Aₙ สะสม (USD)"] == 0, version
        if status != "PASS_BROKER_REJECT_HALT":
            assert row["สถานะ"] == status and row["จำนวนสั่ง (หุ้น)"] == quantity, version
        elif row["สถานะ"].startswith("READY"):
            # v7, v9, v10: the gate, not the equation, turned these into PASS_BROKER_REJECT_HALT.
            assert version in (7, 9, 10)
