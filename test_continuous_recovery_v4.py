"""Recovery contract, crash boundaries, durable audit and daily quota regressions."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from types import SimpleNamespace
import json
import pytest

from conftest import FAKE_DB
import lego_outbox as outbox
import order_recovery as recovery
import transition_audit as audit
import execution_service as execution
import execution_limits as limits
import webull_io
import tick_runtime
from recovery_policy import RecoveryPolicy
from config import load_runtime_config, ConfigurationError

NOW = datetime(2026, 9, 25, 15, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    FAKE_DB.store.clear()
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return NOW
    for module in (outbox, recovery, webull_io):
        monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "test")
    yield
    FAKE_DB.store.clear()


def existing_order(policy=None):
    policy = policy or RecoveryPolicy("cancel")
    scope = outbox.account_symbol_fence_key("identity", "UBER")
    intent = outbox.put_intent("chain", "a" * 32, {
        "status": "SUBMITTED", "symbol": "UBER", "side": "BUY", "quantity": 2,
        "runtime_identity_fingerprint": "identity", "place_attempted": True,
        "placed_at": (NOW - timedelta(seconds=301)).isoformat(),
        "cancel_policy": policy.snapshot(), "cancel_policy_hash": policy.fingerprint,
    })
    claim = outbox.claim_chain_dispatch(scope, "worker", now_utc=NOW)
    outbox.fence_chain_dispatch(scope, intent["run_id"], "worker", claim["claim_token"],
                                intent_chain_key="chain", now_utc=NOW)
    intent = outbox.claim_intent("chain", intent["run_id"], "worker", now_utc=NOW)
    detail = {"client_order_id": intent["run_id"], "symbol": "UBER", "side": "BUY",
              "total_quantity": "2", "filled_quantity": "0", "status": "SUBMITTED"}
    summary = {"status": "SUBMITTED", "filled_quantity": "0"}
    return intent, claim, detail, summary


def test_cancel_once_survives_nonterminal_reads_and_restart():
    intent, claim, detail, summary = existing_order()
    calls = []
    intent = recovery.handle(intent, detail, summary, claim, calls.append)
    assert intent["status"] == "CANCEL_REQUESTED" and intent["cancel_attempt_count"] == 1
    intent = recovery.handle(intent, detail, summary, claim, calls.append)
    assert calls == [intent["run_id"]]
    assert not execution._chain_fence_can_clear(intent)


def test_lost_cancel_response_never_retries():
    intent, claim, detail, summary = existing_order()
    calls = []
    def lost(rid):
        calls.append(rid)
        raise TimeoutError()
    intent = recovery.handle(intent, detail, summary, claim, lost)
    assert intent["status"] == "CANCEL_UNKNOWN"
    recovery.handle(intent, detail, summary, claim, lost)
    assert len(calls) == 1


def test_crash_after_cancel_witness_does_not_retry():
    intent, claim, detail, summary = existing_order()
    started = recovery.begin_cancel(intent, claim, RecoveryPolicy("cancel"), NOW)
    assert started
    recovery.handle(started, detail, summary, claim,
                    lambda _: pytest.fail("crashed witness must not be retried"))


def test_missing_broker_proof_after_grace_halts_and_preserves_fence():
    intent, claim, detail, summary = existing_order()
    intent = recovery.handle(intent, detail, summary, claim, lambda _: None)
    # A later worker has a fresh intent lease; order uncertainty outlives leases.
    intent = outbox.claim_intent("chain", intent["run_id"], "next", now_utc=NOW + timedelta(seconds=121))
    intent = recovery.read_failed(intent, now=NOW + timedelta(seconds=121))
    assert intent["needs_manual_check"]
    assert not execution._chain_fence_can_clear(intent)
    import operator_halt
    assert operator_halt.status("identity", "UBER")["halted"]


@pytest.mark.parametrize("field,value", [("symbol", "OTHER"), ("side", "SELL"),
                                        ("total_quantity", "3"), ("client_order_id", "other")])
def test_cancel_identity_anomalies_keep_fence(field, value):
    intent, claim, detail, summary = existing_order()
    detail[field] = value
    result = recovery.handle(intent, detail, summary, claim,
                             lambda _: pytest.fail("bad identity cancelled"))
    assert result["needs_manual_check"]


@pytest.mark.parametrize("policy", [None, RecoveryPolicy()])
def test_legacy_or_hold_never_gains_cancel_right(policy):
    intent, claim, detail, summary = existing_order(policy or RecoveryPolicy())
    if policy is None:
        intent.pop("cancel_policy")
    recovery.handle(intent, detail, summary, claim, lambda _: pytest.fail("hold cancelled"))


def test_cancel_ack_is_not_terminal_and_terminal_does_not_erase_partial_fill():
    intent, claim, detail, summary = existing_order()
    intent = recovery.handle(intent, detail, summary, claim, lambda _: {"status": "CANCELLED"})
    assert not execution._chain_fence_can_clear(intent)
    detail.update(status="CANCELLED", filled_quantity="1")
    result = recovery.handle(intent, detail, {"status": "CANCELLED", "filled_quantity": "1"}, claim,
                             lambda _: pytest.fail("terminal cancelled again"))
    assert result["cancel_confirmed_at"]
    assert not execution._chain_fence_can_clear({**result, "status": "CANCELLED", "filled_quantity": "1"})


def test_expired_cancel_lease_cannot_mutate():
    intent, claim, detail, summary = existing_order()
    ref = FAKE_DB.reference(f"{outbox.DISPATCH_LOCK_PATH}/{outbox.account_symbol_fence_key('identity', 'UBER')}")
    ref.update({"lease_until": (NOW - timedelta(seconds=1)).isoformat()})
    with pytest.raises(outbox.StaleIntentClaim):
        recovery.handle(intent, detail, summary, claim, lambda _: pytest.fail("expired worker cancelled"))


def test_transition_replay_is_immutable_idempotent_and_preserves_pending_on_failure(monkeypatch):
    from conftest import FakeReference
    intent, *_ = existing_order()
    original = FakeReference.transaction
    def fail(self, fn):
        if self.parts[0] == audit.PATH:
            raise RuntimeError("offline")
        return original(self, fn)
    monkeypatch.setattr(FakeReference, "transaction", fail)
    with pytest.raises(RuntimeError): audit.replay("chain", intent["run_id"])
    assert outbox.read_intent("chain", intent["run_id"])["transition_pending"]
    monkeypatch.setattr(FakeReference, "transaction", original)
    audit.replay("chain", intent["run_id"])
    saved = FAKE_DB.reference(audit.PATH).get()
    audit.replay("chain", intent["run_id"])
    assert saved == FAKE_DB.reference(audit.PATH).get()


def test_daily_quota_does_not_reset_for_release_renewal_and_rolls_next_session():
    policy = limits.ExecutionLimits.parse(("100", "10000", "1", "2030-01-01T00:00:00Z"))
    claim = {"owner": "worker", "claim_token": "token"}
    ref = FAKE_DB.reference(f"{outbox.DISPATCH_LOCK_PATH}/scope")
    ref.set({**claim, "lease_until": (NOW + timedelta(days=5)).isoformat(), "inflight_run_id": "one"})
    key = limits.session_key_for("market_day", policy, now=NOW)
    assert limits.reserve_attempt("scope", claim, "one", policy, key, now=NOW)["reservation_count"] == 1
    ref.update({"inflight_run_id": "two"})
    renewed = replace(policy, end=policy.end + timedelta(days=1))
    with pytest.raises(limits.ExecutionLimitError):
        limits.reserve_attempt("scope", claim, "two", renewed, key, now=NOW)
    monday = NOW + timedelta(days=3)
    next_key = limits.session_key_for("market_day", policy, now=monday)
    assert limits.reserve_attempt("scope", claim, "two", renewed, next_key, now=monday)["reservation_count"] == 1


def test_migration_requires_idle_and_never_decreases_count():
    ref = FAKE_DB.reference(f"{outbox.DISPATCH_LOCK_PATH}/scope")
    ref.set({"execution_session": {"key": "123", "count": 3}})
    report = limits.migrate_market_day("scope", now=NOW)
    assert report["dry_run"] and ref.get()["execution_session"]["key"] == "123"
    assert limits.migrate_market_day("scope", now=NOW, apply=True)["execution_session"]["count"] == 3
    ref.update({"owner": "worker"})
    with pytest.raises(limits.ExecutionLimitError): limits.migrate_market_day("scope", now=NOW, apply=True)


@pytest.mark.parametrize("counter", [[], {"count": 0}, {"key": "XNYS:20260925", "count": 1},
                                     {"key": "Infinity", "count": 1}, {"key": "XNYS:2026-09-24", "count": -1}])
def test_malformed_counter_cannot_be_migrated_or_reset_on_rollover(counter):
    ref = FAKE_DB.reference(f"{outbox.DISPATCH_LOCK_PATH}/scope")
    ref.set({"execution_session": counter})
    with pytest.raises(limits.ExecutionLimitError): limits.migrate_market_day("scope", now=NOW, apply=True)
    claim = {"owner": "worker", "claim_token": "token"}
    ref.update({**claim, "lease_until": (NOW + timedelta(minutes=2)).isoformat(), "inflight_run_id": "new"})
    policy = limits.ExecutionLimits.parse(("100", "10000", "30", "2030-01-01T00:00:00Z"))
    with pytest.raises(limits.ExecutionLimitError):
        limits.reserve_attempt("scope", claim, "new", policy, "XNYS:2026-09-25", now=NOW)


def test_release_binding_covers_recovery_strategy_and_fractional():
    env = {"LEGO_SYMBOL": "UBER", "LEGO_FIX_C": "10000", "LEGO_DIFF": "25", "WEBULL_ACCOUNT_ID": "test", "LEGO_CANDIDATE_HASH": "hash"}
    base = load_runtime_config(env).deployment.expected_release_binding
    for key, value in (("LEGO_FIX_C", "9000"), ("LEGO_DIFF", "30"), ("LEGO_ALLOW_FRACTIONAL", "false"),
                       ("LEGO_STALE_ORDER_ACTION", "cancel"), ("LEGO_SESSION_KEY_MODE", "market_day")):
        assert load_runtime_config({**env, key: value}).deployment.expected_release_binding != base
    with pytest.raises(ConfigurationError):
        load_runtime_config({**env, "LEGO_ORDER_TIMEOUT_SECONDS": "300"})


def test_poll_budget_defers_without_broker_call(monkeypatch):
    monkeypatch.setattr(execution, "fetch_order_detail", lambda *_: pytest.fail("no time to read"))
    with tick_runtime.tick_scope("test", seconds=0):
        result = execution._poll_order_status(None, "test", {"status": "SUBMITTED"})
    assert result["status"] == "SUBMITTED"


def test_poll_reads_at_most_once(monkeypatch):
    calls = []
    monkeypatch.setattr(execution, "fetch_order_detail", lambda *_: calls.append(1) or {"status": "SUBMITTED"})
    execution._poll_order_status(None, "test", {})
    assert calls == [1]


@pytest.mark.parametrize("status", [401, 417, 429, 500])
def test_cancel_error_response_is_not_retried(status):
    calls = []
    def cancel(*args):
        calls.append(args)
        return SimpleNamespace(status_code=status, json=lambda: {})
    with pytest.raises(webull_io.WebullConfigError):
        webull_io.cancel_order(SimpleNamespace(order_v3=SimpleNamespace(cancel_order=cancel)), "abc")
    assert len(calls) == 1


def test_cancel_response_requires_matching_identity():
    client = SimpleNamespace(order_v3=SimpleNamespace(cancel_order=lambda *_:
        SimpleNamespace(status_code=200, json=lambda: {"client_order_id": "other", "order_id": "broker"})))
    with pytest.raises(webull_io.WebullConfigError): webull_io.cancel_order(client, "abc")


def test_history_scan_reads_all_pages_and_rejects_repeated_cursor():
    pages = [{"data": [{"orders": [{"client_order_id": "abc", "symbol": "UBER"}]}], "pagination_key": "next"}, {"data": []}]
    def history(*args, **kw): return SimpleNamespace(json=lambda: pages.pop(0))
    client = SimpleNamespace(order_v3=SimpleNamespace(list_order_history=history))
    assert webull_io.find_recent_order_by_client_id(client, "abc", placed_at=NOW.isoformat())["symbol"] == "UBER"
    repeated = {"data": [{"client_order_id": "abc", "symbol": "UBER"}], "pagination_key": "same"}
    pages.extend([repeated, repeated])
    with pytest.raises(webull_io.WebullConfigError):
        webull_io.find_recent_order_by_client_id(client, "abc", placed_at=NOW.isoformat())


def test_production_requires_secret_even_if_token_check_disabled():
    assert webull_io.new_order_token_block({"token_check_enabled": False}, "PROD")
    assert webull_io.new_order_token_block({}, "UAT") is None


@pytest.mark.parametrize("missing", ["side", "total_quantity", "client_order_id", "filled_quantity"])
def test_incomplete_detail_uses_complete_history_before_cancel(monkeypatch, missing):
    intent, claim, detail, summary = existing_order()
    incomplete = {k: v for k, v in detail.items() if k != missing}
    calls = []
    monkeypatch.setattr(execution, "fetch_order_detail", lambda *a: incomplete)
    monkeypatch.setattr(webull_io, "find_recent_order_by_client_id", lambda *a, **kw: calls.append("history") or detail)
    monkeypatch.setattr(webull_io, "cancel_order", lambda *a: calls.append("cancel"))
    monkeypatch.setattr(execution, "_finish_with_realized", lambda tc, cfg, i, s: i)
    result = execution._dispatch_or_reconcile_one(None, None, SimpleNamespace(symbol="UBER"), intent, claim)
    assert calls == ["history", "cancel"] and result["cancel_attempt_count"] == 1


def test_conflict_halt_write_failure_is_not_swallowed_as_broker_failure(monkeypatch):
    intent, claim, detail, summary = existing_order()
    detail["side"] = "SELL"
    monkeypatch.setattr(execution, "fetch_order_detail", lambda *a: detail)
    monkeypatch.setattr(recovery, "mark_manual", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("persistence failed")))
    with pytest.raises(RuntimeError, match="persistence failed"):
        execution._dispatch_or_reconcile_one(None, None, SimpleNamespace(symbol="UBER"), intent, claim)


def test_terminal_before_cancel_never_mutates():
    intent, claim, detail, summary = existing_order()
    summary.update(status="CANCELLED")
    result = recovery.handle(intent, detail, summary, claim, lambda _: pytest.fail("terminal order cancelled"))
    assert not result.get("cancel_attempt_count")


def test_deadline_before_cancel_witness_consumes_no_right():
    intent, claim, detail, summary = existing_order()
    with tick_runtime.tick_scope("short", seconds=0):
        with pytest.raises(tick_runtime.TickDeadlineExceeded): recovery.begin_cancel(intent, claim, RecoveryPolicy("cancel"), NOW)
    assert not outbox.read_intent("chain", intent["run_id"]).get("cancel_attempt_count")


def test_deadline_after_cancel_witness_never_retries():
    intent, claim, detail, summary = existing_order()
    result = recovery.handle(intent, detail, summary, claim,
                             lambda _: (_ for _ in ()).throw(tick_runtime.TickDeadlineExceeded()))
    assert result["status"] == "CANCEL_UNKNOWN" and result["cancel_attempt_count"] == 1
    recovery.handle(result, detail, summary, claim, lambda _: pytest.fail("second mutation"))


def test_history_older_than_seven_days_never_calls_network():
    with pytest.raises(webull_io.WebullConfigError, match="seven-day"):
        webull_io.find_recent_order_by_client_id(None, "abc", placed_at=(NOW - timedelta(days=8)).isoformat())
