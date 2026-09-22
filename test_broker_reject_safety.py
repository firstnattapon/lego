"""Crash/replay, dispatch, privacy and operator regressions; no live broker I/O."""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import alerting
import broker_circuit as circuit
import decision_service
import execution_service as execution
import lego_outbox as outbox
import main
import webull_io
from config import load_runtime_config
from conftest import FAKE_DB, FakeReference
from lego_one_row import Config, PASS_MIN_ORDER
from lego_orders import summarize_order_result
from observability import business_status
from security_text import broker_diagnostic_json

UTC = timezone.utc
NOW = datetime(2026, 9, 21, 15, 0, tzinfo=UTC)
IDENTITY = "test-runtime"
SCOPE = outbox.account_symbol_fence_key(IDENTITY, "TSLA")


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    FAKE_DB.store.clear()
    webull_io.reset_clients()
    execution.configure(main)
    decision_service.configure(main)
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    yield
    FAKE_DB.store.clear()
    webull_io.reset_clients()


def outcome(run_id, status="FAILED", quantity="0", *, chain="chain"):
    intent = {"run_id": run_id, "chain_key": chain, "symbol": "TSLA",
              "runtime_identity_fingerprint": IDENTITY}
    ref = FAKE_DB.reference(f"{outbox.DISPATCH_LOCK_PATH}/{SCOPE}")
    ref.update({"inflight_run_id": run_id})
    summary = {"status": status, "filled_quantity": quantity}
    return intent, summary


def test_three_distinct_rejects_halt_and_replay_or_chain_change_cannot_bypass():
    for i in range(3):
        intent, summary = outcome(str(i), chain=f"dna-{i}")
        for _ in range(4):
            state = circuit.record_outcome(intent, summary)
            assert state["consecutive_broker_rejects"] == i + 1
    assert state["halted"] is True
    assert circuit.status(IDENTITY, "TSLA")["status"] == circuit.HALT
    assert circuit.status("other-account", "TSLA") == {}
    assert circuit.status(IDENTITY, "AAPL") == {}
    # A stale callback cannot reset or count after a successor owns the fence.
    circuit.record_outcome({**intent, "run_id": "0"}, {"status": "FILLED", "filled_quantity": "1"})
    assert circuit.status(IDENTITY, "TSLA")["consecutive_broker_rejects"] == 3


def test_confirmed_terminal_fill_resets_streak_but_partial_and_cancel_zero_do_not():
    intent, summary = outcome("reject")
    circuit.record_outcome(intent, summary)
    for status, qty in (("PARTIAL_FILLED", "1"), ("CANCELLED", "0")):
        intent, summary = outcome(status, status, qty)
        circuit.record_outcome(intent, summary)
        assert circuit.status(IDENTITY, "TSLA")["consecutive_broker_rejects"] == 1
    intent, summary = outcome("filled", "FILLED", "1")
    assert circuit.record_outcome(intent, summary)["consecutive_broker_rejects"] == 0


def test_sticky_halt_and_dry_run_reset_require_exact_halt_and_idle_fence():
    for i in range(3):
        circuit.record_outcome(*outcome(str(i)))
    intent, summary = outcome("late-fill", "FILLED", "1")
    assert circuit.record_outcome(intent, summary)["halted"] is True
    with pytest.raises(ValueError, match="unresolved"):
        circuit.reset(IDENTITY, "TSLA", expected_halt_id="2", reason="reviewed")
    ref = FAKE_DB.reference(f"{outbox.DISPATCH_LOCK_PATH}/{SCOPE}")
    state = ref.get()
    state.pop("inflight_run_id")
    ref.set(state)
    with pytest.raises(ValueError, match="changed"):
        circuit.reset(IDENTITY, "TSLA", expected_halt_id="wrong", reason="reviewed", apply=True)
    assert circuit.reset(IDENTITY, "TSLA", expected_halt_id="2", reason="reviewed")["dry_run"]
    assert circuit.status(IDENTITY, "TSLA")["halted"]
    circuit.reset(IDENTITY, "TSLA", expected_halt_id="2", reason="reviewed", apply=True)
    assert not circuit.status(IDENTITY, "TSLA")["halted"]
    assert circuit.status(IDENTITY, "TSLA")["last_outcome_run_id"] == "late-fill"


def test_reset_rechecks_concurrent_dispatch_owner(monkeypatch):
    for i in range(3):
        circuit.record_outcome(*outcome(str(i)))
    ref = FAKE_DB.reference(f"{outbox.DISPATCH_LOCK_PATH}/{SCOPE}")
    doc = ref.get()
    doc.pop("inflight_run_id")
    ref.set(doc)
    original = FakeReference.transaction

    def raced(self, fn):
        self.update({"owner": "new-worker", "lease_until":
                     (datetime.now(UTC) + timedelta(minutes=2)).isoformat()})
        return original(self, fn)

    monkeypatch.setattr(FakeReference, "transaction", raced)
    with pytest.raises(ValueError, match="worker active"):
        circuit.reset(IDENTITY, "TSLA", expected_halt_id="2", reason="reviewed", apply=True)
    assert circuit.status(IDENTITY, "TSLA")["halted"]


def test_atomic_place_fence_refuses_halted_scope():
    ref = FAKE_DB.reference(f"{outbox.DISPATCH_LOCK_PATH}/{SCOPE}")
    ref.set({circuit.KEY: {"halted": True}})
    claim = outbox.claim_chain_dispatch(SCOPE, "worker")
    assert outbox.fence_chain_dispatch(SCOPE, "new", "worker", claim["claim_token"]) is None
    assert not ref.get().get("inflight_run_id")


def test_crash_after_circuit_update_before_terminal_write_counts_once(monkeypatch):
    intent, summary = outcome("r")
    intent["side"] = "BUY"
    outbox.put_intent("chain", "r", {**intent, "status": "PLACING_UNKNOWN"})
    original = execution._persist_summary
    monkeypatch.setattr(execution, "_persist_summary", lambda *a: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError, match="crash"):
        execution._finish_with_realized(None, Config("TSLA", 100), intent, summary)
    assert outbox.read_intent("chain", "r")["status"] == "PLACING_UNKNOWN"
    monkeypatch.setattr(execution, "_persist_summary", original)
    execution._finish_with_realized(None, Config("TSLA", 100), intent, summary)
    assert outbox.read_intent("chain", "r")["status"] == "FAILED"
    assert circuit.status(IDENTITY, "TSLA")["consecutive_broker_rejects"] == 1


def test_failed_diagnostics_private_bounded_redacted_and_public_health_explicit():
    intent, _ = outcome("r")
    intent["side"] = "BUY"
    outbox.put_intent("chain", "r", {**intent, "status": "PLACING_UNKNOWN"})
    detail = {"orders": [{"status": "FAILED", "filled_quantity": "0",
                          "order_id": "broker-order", "error_code": "E42",
                          "accountId": "private-account", "accessToken": "private-token",
                          "custom.debug": {"authorization": "Bearer private-bearer"}}]}
    summary = summarize_order_result({}, detail)
    assert summary["broker_reason_missing"]
    result = execution._finish_with_realized(None, Config("TSLA", 100), intent, summary)
    stored = outbox.read_intent("chain", "r")
    audit = FAKE_DB.reference("webull_lego_order_audit/r").get()
    assert "broker_raw_detail" in stored
    assert "broker_raw_detail" not in result and "broker_raw_detail" not in audit
    assert "private-" not in stored["broker_raw_detail"]
    assert json.loads(stored["broker_raw_detail"])["detail"]["orders"][0]["custom.debug"]
    assert audit["broker_reject_code"] == "E42"
    assert audit["broker_order_id"] == "broker-order"
    assert business_status({"dispatch": {"results": [result]}}, 200) == "BROKER_ORDER_FAILED"
    huge = broker_diagnostic_json({"x": ["\u0e01" * 500] * 10000})
    assert len(huge) <= 16384
    json.loads(huge)


@pytest.mark.parametrize("hours,blocked", [(12, True), (23.999, True), (24, False), (120, False)])
def test_exact_production_token_expiry_floor(hours, blocked):
    health = {"status": "NORMAL", "expires_at": (NOW + timedelta(hours=hours)).isoformat()}
    assert bool(webull_io.new_order_token_block(health, "PROD", NOW)) == blocked
    assert webull_io.new_order_token_block(health, "UAT", NOW) is None


def test_unknown_expiry_fails_closed_but_verified_tokenless_auth_is_allowed():
    assert webull_io.new_order_token_block({}, "PROD", NOW)
    assert webull_io.new_order_token_block({"token_check_enabled": False}, "PROD", NOW) is None


def test_seven_day_token_warning_does_not_block_five_day_valid_token(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBULL_TOKEN_DIR", str(tmp_path))
    monkeypatch.setenv("LEGO_ALLOW_EPHEMERAL_TOKEN_DIR", "true")
    expiry = int((NOW + timedelta(days=5)).timestamp() * 1000)
    (tmp_path / "token.txt").write_text(f"test-token\n{expiry}\nNORMAL\n", encoding="utf8")
    health = webull_io.token_health(NOW)
    assert health["expiry_warning"] is True
    assert health["ready"] is True


def decision_setup(monkeypatch, *, allow_fractional, holdings=0, principal=5000):
    env = {"LEGO_SYMBOL": "TSLA", "LEGO_FIX_C": str(principal), "LEGO_DIFF": "0",
           "WEBULL_ENV": "UAT", "WEBULL_ACCOUNT_ID": "test-account",
           "LEGO_DNA_CODE": "bypass:1000", "LEGO_DNA_CLOCK_MODE": "market",
           "LEGO_SLOT_SECONDS": "900", "LEGO_DNA_ORIGIN_UTC": "2026-09-21T13:30:00Z",
           "LEGO_MODE": "trade", "LEGO_ACTIVE": "true", "LEGO_CANDIDATE_HASH": "candidate",
           "LEGO_ALLOW_FRACTIONAL": str(allow_fractional).lower()}
    env["FIREBASE_DB_URL"] = "https://test.firebaseio.com"
    env["LEGO_RELEASE_AUTHORIZATION"] = load_runtime_config(env).deployment.expected_release_binding
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    runtime = load_runtime_config(env)
    cfg = Config("TSLA", principal, dna_code="bypass:1000", strategy_id="shannon_demon_lego_v2")
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(decision_service, "datetime", Clock)
    monkeypatch.setattr(decision_service, "build_clients", lambda: (object(), object()))
    monkeypatch.setattr(decision_service, "fetch_instrument_capability", lambda *a:
                        webull_io.InstrumentCapability("TSLA", "OC", "US_STOCK", "USD", 1, True))
    monkeypatch.setattr(decision_service, "token_health", lambda: {"ok": True, "ready": True})
    monkeypatch.setattr(decision_service, "fetch_snapshot", lambda *a: {
        "price": 375.65, "holdings": holdings, "captured_at": NOW.isoformat(),
        "quote_time": NOW.isoformat()})
    return runtime, cfg


@pytest.mark.parametrize("holdings,expected", [(0, 13), (30.5, 17), (13, 0)])
def test_whole_share_policy_quantizes_before_commit_and_keeps_fractional_holdings(monkeypatch, holdings, expected):
    runtime, cfg = decision_setup(monkeypatch, allow_fractional=False, holdings=holdings)
    body, code = decision_service.run_decision(None, runtime, cfg)
    assert code == 200, body
    row = FAKE_DB.reference(f"webull_lego_rows/{body['run_id']}").get()
    assert row["จำนวนสั่ง (หุ้น)"] == expected
    assert row["จำนวนถือครอง (หุ้น)"] == holdings
    intent = outbox.read_intent(main.chain_key(cfg), body["run_id"])
    if expected:
        assert intent["quantity"] == expected
        assert intent["strategy_config"]["quantity_increment"] == 1
        assert intent["allow_fractional"] is False
    else:
        assert body["status"] == PASS_MIN_ORDER
        assert intent is None


def test_halt_blocks_new_intent_but_keeps_committing_dna_slots(monkeypatch):
    runtime, cfg = decision_setup(monkeypatch, allow_fractional=True)
    identity = webull_io.runtime_identity_fingerprint()
    scope = outbox.account_symbol_fence_key(identity, "TSLA")
    FAKE_DB.reference(f"{outbox.DISPATCH_LOCK_PATH}/{scope}").set({circuit.KEY: {"halted": True}})
    body, code = decision_service.run_decision(None, runtime, cfg)
    assert code == 200 and body["committed"], body
    assert body["outbox_blocked"] == circuit.HALT
    assert outbox.list_actionable(main.chain_key(cfg)) == []


@pytest.mark.parametrize("status", ["PLACING_UNKNOWN", "PENDING", "PARTIAL_FILLED"])
def test_halt_still_reconciles_attempted_order(monkeypatch, status):
    intent, summary = outcome("r")
    intent.update(status=status, side="BUY")
    outbox.put_intent("chain", "r", intent)
    ref = FAKE_DB.reference(f"{outbox.DISPATCH_LOCK_PATH}/{SCOPE}")
    ref.update({circuit.KEY: {"halted": True, "consecutive_broker_rejects": 3}})
    monkeypatch.setattr(execution, "read_committed_row", lambda _: {"committed": True})
    calls = []
    monkeypatch.setattr(execution, "fetch_order_detail", lambda *a: calls.append(a) or summary)
    monkeypatch.setattr(execution, "place_market_order", lambda *a: pytest.fail("duplicate Place"))
    result = execution._dispatch_or_reconcile_one(None, None, Config("TSLA", 100), intent)
    assert calls and result["status"] == "FAILED"


@pytest.mark.parametrize("failure", ["timeout", "http500", "redirect"])
def test_webhook_failures_are_bounded_private_and_nonfatal(monkeypatch, caplog, failure):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://example.invalid/private-token")
    class Response:
        status_code = 500 if failure == "http500" else 302
        def __enter__(self): return self
        def __exit__(self, *args): pass
    def post(url, **kwargs):
        assert kwargs["timeout"] == (1, 2) and not kwargs["allow_redirects"]
        if failure == "timeout":
            raise RuntimeError(url)
        return Response()
    monkeypatch.setattr(alerting.requests, "post", post)
    assert alerting.notify("AUTH_BACKOFF", IDENTITY) is False
    assert "private-token" not in caplog.text


def test_webhook_delivery_deduplicates_across_instances(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://example.invalid/hook")
    calls = []
    class Response:
        status_code = 204
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(alerting.requests, "post", lambda *a, **k: calls.append(k) or Response())
    assert alerting.notify(circuit.HALT, SCOPE, count=3)
    assert not alerting.notify(circuit.HALT, SCOPE, count=3)
    assert len(calls) == 1


def test_three_real_worker_rejects_prevent_fourth_preview_and_place(monkeypatch):
    from conftest import fake_trade_client
    runtime, cfg = decision_setup(monkeypatch, allow_fractional=False)
    client = fake_trade_client(preview={"estimated_cost": "4883.45", "estimated_transaction_fee": "1"})
    places = []
    clock = [NOW]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return clock[0]
    snapshot = lambda *a: {"price": 375.65, "holdings": 0,
                          "captured_at": clock[0].isoformat(), "quote_time": clock[0].isoformat()}
    monkeypatch.setattr(main, "datetime", Clock)
    monkeypatch.setattr(decision_service, "datetime", Clock)
    monkeypatch.setattr(outbox, "datetime", Clock)
    monkeypatch.setattr(main, "build_clients", lambda: (client, object()))
    monkeypatch.setattr(decision_service, "build_clients", lambda: (client, object()))
    monkeypatch.setattr(main, "fetch_snapshot", snapshot)
    monkeypatch.setattr(decision_service, "fetch_snapshot", snapshot)
    monkeypatch.setattr(main, "fetch_open_orders", lambda *a: [])
    monkeypatch.setattr(main, "place_market_order", lambda tc, order:
                        places.append(order) or {"client_order_id": order[0]["client_order_id"], "order_id": "broker-id"})
    monkeypatch.setattr(main, "fetch_order_detail", lambda tc, rid:
                        {"client_order_id": rid, "symbol": "TSLA", "status": "FAILED", "filled_quantity": "0"})
    monkeypatch.setattr(execution, "fetch_buying_power", lambda *a: Decimal("10000"))
    for i in range(4):
        clock[0] = NOW + timedelta(minutes=15 * i)
        body, code = decision_service.run_decision(None, runtime, cfg)
        assert code == 200, body
        execution.configure(main)
        result = execution._run_order_worker(cfg, limit=3, runtime=runtime)
        if i < 3:
            assert result["results"][0]["status"] == "FAILED", result
        else:
            assert body["outbox_blocked"] == circuit.HALT
            assert result["processed"] == 0
    assert len(places) == len(client.order_v3.preview_order.calls) == 3


def test_dispatch_blocks_old_fractional_intent_without_resizing(monkeypatch):
    from test_dispatch_overshoot import dispatch_fixture, CFG
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch)
    runtime = replace(runtime, deployment=replace(runtime.deployment, allow_fractional=False))
    result = execution._dispatch_or_reconcile_one(client, None, CFG, intent, claim, runtime)
    assert result["status"] == "NOT_PLACED"
    assert not client.order_v3.preview_order.calls and not client.order_v3.place_order.calls
    assert outbox.read_intent(intent["chain_key"], intent["run_id"])["quantity"] == intent["quantity"]


def test_post_place_poll_rejects_wrong_identity_before_accounting(monkeypatch):
    monkeypatch.setattr(execution, "fetch_order_detail", lambda *a:
                        {"client_order_id": "different", "symbol": "TSLA", "status": "FILLED"})
    with pytest.raises(ValueError):
        execution._poll_order_status(None, "expected", {}, expected_symbol="TSLA")


def test_unknown_broker_status_remains_reconcilable_instead_of_stranding_queue():
    assert execution._execution_summary({}, {"status": "FUTURE_BROKER_STATUS"})["status"] == "UNKNOWN"


@pytest.mark.parametrize("phase", ["before_preview", "before_place"])
def test_production_token_floor_is_enforced_at_dispatch_boundaries(monkeypatch, phase):
    from test_dispatch_overshoot import dispatch_fixture, CFG, NOW as DISPATCH_NOW
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch, environment="PROD")
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return DISPATCH_NOW
    monkeypatch.setattr(execution, "datetime", Clock)
    monkeypatch.setattr(outbox, "datetime", Clock)
    valid = {"status": "NORMAL", "expires_at": (datetime.now(UTC) + timedelta(days=14)).isoformat()}
    short = {"status": "NORMAL", "expires_at": (datetime.now(UTC) + timedelta(hours=12)).isoformat()}
    health = iter([short] if phase == "before_preview" else [valid, short])
    monkeypatch.setattr(execution, "token_health", lambda: next(health))
    result = execution._dispatch_or_reconcile_one(client, None, CFG, intent, claim, runtime)
    assert result["token_preflight_blocked"]
    assert not client.order_v3.place_order.calls
    assert len(client.order_v3.preview_order.calls) == (phase == "before_place")
    assert not outbox.read_intent(intent["chain_key"], intent["run_id"]).get("place_attempted")


def test_public_audit_boundary_also_filters_raw_detail_from_other_callers():
    from lego_state import update_order_audit
    update_order_audit("r", {"status": "FAILED", "broker_raw_detail": "private"})
    assert FAKE_DB.reference("webull_lego_order_audit/r").get() == {"status": "FAILED"}
