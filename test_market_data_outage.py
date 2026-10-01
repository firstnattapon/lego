"""Snapshot outage regressions; no broker or webhook requests."""
import importlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import main
import market_data_circuit as circuit
import observability
import tick_runtime
import webull_io
from conftest import FAKE_DB, fake_data_client, fake_trade_client
from lego_one_row import Config

CFG = Config(symbol="UBER", fix_c=10000, decimal_precision=5)
REQUEST_ID = "861ff886-05d7-4f1a-969d-a3ab21e8fb3e"


class SnapshotFailure(Exception):
    http_status = 500
    error_code = "INTERNAL_ERROR"
    request_id = REQUEST_ID
    _lego_operation = "snapshot"


def fail():
    raise SnapshotFailure("private broker response")


def run(key, fn):
    return circuit.run(key, fn, is_transient=webull_io.is_transient_exception,
                       error_details=webull_io.broker_error_details)


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    FAKE_DB.store.clear()
    monkeypatch.setenv("WEBULL_ENV", "UAT")
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "test-account")
    clock = [10000.0]
    monkeypatch.setattr(circuit.time, "time", lambda: clock[0])
    return clock


def pause(key):
    with pytest.raises(circuit.MarketDataCircuitOpen) as error:
        run(key, fail)
    return error.value


def test_one_snapshot_failure_then_cold_start_skips_all_broker_reads():
    trade = fake_trade_client(positions={"positions": []})
    data = fake_data_client(snapshot=SnapshotFailure())
    with tick_runtime.tick_scope("first"):
        with pytest.raises(circuit.MarketDataCircuitOpen) as error:
            webull_io.fetch_snapshot(trade, data, CFG)
    assert len(data.market_data.get_snapshot.calls) == 1
    assert error.value.response()["broker_error"]["code"] == "INTERNAL_ERROR"
    importlib.reload(circuit)
    before = len(trade.account_v2.get_account_position.calls)
    with tick_runtime.tick_scope("cold-start"):
        with pytest.raises(circuit.MarketDataCircuitOpen):
            webull_io.fetch_snapshot(trade, data, CFG)
    assert len(trade.account_v2.get_account_position.calls) == before
    assert len(data.market_data.get_snapshot.calls) == 1
    assert trade.order_v3.place_order.calls == []


def test_cooldown_grows_to_cap_and_valid_quote_resets_it(isolate):
    key = webull_io.market_data_scope(CFG)
    for delay in (60, 120, 240, 480, 960, 1800, 1800):
        state = pause(key).state
        assert state["retry_after"] == isolate[0] + delay
        with pytest.raises(circuit.MarketDataCircuitOpen):
            run(key, lambda: pytest.fail("cooldown sent a request"))
        isolate[0] = state["retry_after"]
    trade = fake_trade_client(positions={"positions": []})
    data = fake_data_client(snapshot=[{"symbol": "UBER", "last": "80.50",
                                      "last_trade_time": 1790856600000}])
    with tick_runtime.tick_scope("recovered"):
        snapshot = webull_io.fetch_snapshot(trade, data, CFG)
    assert snapshot["price"] == 80.5
    assert snapshot["quote_time"] == "2026-10-01T12:10:00.000Z"
    state = circuit._reference(key).get()
    assert not state["active"] and state["consecutive_failures"] == 0
    assert len(data.market_data.get_snapshot.calls) == 1


def test_scope_is_separate_for_environment_account_symbol_and_category(monkeypatch):
    original = webull_io.market_data_scope(CFG)
    pause(original)
    monkeypatch.setenv("WEBULL_ENV", "PROD")
    prod = webull_io.market_data_scope(CFG)
    assert prod != original
    assert run(prod, lambda: 1) == 1
    monkeypatch.setenv("WEBULL_ENV", "UAT")
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "another-account")
    assert webull_io.market_data_scope(CFG) != original
    assert circuit.scope("id", "UBER", "US_STOCK") != circuit.scope("id", "AAPL", "US_STOCK")
    assert circuit.scope("id", "UBER", "US_STOCK") != circuit.scope("id", "UBER", "US_ETF")


def test_only_one_owner_can_probe_after_cooldown(isolate):
    key = "scope"
    isolate[0] = pause(key).state["retry_after"]

    def first_probe():
        with tick_runtime.tick_scope("other"):
            with pytest.raises(circuit.MarketDataCircuitOpen):
                run(key, lambda: pytest.fail("second concurrent request"))
        return "quote"

    assert run(key, first_probe) == "quote"


def test_crashed_probe_resumes_only_after_lease_expiry(isolate):
    circuit._reference("scope").set({"active": True, "retry_after": isolate[0],
                                     "probe_owner": "crashed", "probe_until": isolate[0] + 45})
    with pytest.raises(circuit.MarketDataCircuitOpen):
        run("scope", lambda: pytest.fail("lease still active"))
    isolate[0] += 45
    assert run("scope", lambda: "fresh") == "fresh"


@pytest.mark.parametrize("late_failure", [False, True])
def test_stale_probe_cannot_clear_or_extend_successor_failure(isolate, late_failure):
    def delayed():
        isolate[0] += 46
        pause("scope")
        if late_failure:
            raise SnapshotFailure()
        return "stale quote"
    with pytest.raises(circuit.MarketDataCircuitOpen):
        run("scope", delayed)
    state = circuit._reference("scope").get()
    assert state["active"] and state["consecutive_failures"] == 1
    assert state["retry_after"] == isolate[0] + 60


def test_expired_probe_cannot_clear_backoff_even_without_a_successor(isolate):
    key = "scope"
    isolate[0] = pause(key).state["retry_after"]
    def expired_quote():
        isolate[0] += circuit.PROBE_SECONDS
        return "late quote"
    with pytest.raises(circuit.MarketDataCircuitOpen):
        run(key, expired_quote)
    state = circuit._reference(key).get()
    assert state["active"] and state["consecutive_failures"] == 1
    assert run(key, lambda: "fresh quote") == "fresh quote"


@pytest.mark.parametrize("failure", [ValueError("malformed quote"),
                                      SimpleNamespace(http_status=401)])
def test_nontransient_failure_does_not_create_market_data_backoff(failure):
    if not isinstance(failure, Exception):
        failure = type("AuthFailure", (Exception,), {"http_status": 401})()
    def request():
        raise failure
    with pytest.raises(type(failure)):
        run("scope", request)
    assert not circuit._reference("scope").get().get("active")


@pytest.mark.parametrize("price", ["inf", "nan", "-1", "0"])
def test_invalid_quote_never_clears_existing_outage(isolate, price):
    key = webull_io.market_data_scope(CFG)
    isolate[0] = pause(key).state["retry_after"]
    trade = fake_trade_client(positions={"positions": []})
    data = fake_data_client(snapshot=[{"symbol": "UBER", "last": price,
                                      "last_trade_time": 1790856600000}])
    with tick_runtime.tick_scope("invalid"):
        with pytest.raises(ValueError):
            webull_io.fetch_snapshot(trade, data, CFG)
    assert circuit._reference(key).get()["active"]
    assert trade.order_v3.place_order.calls == []


def test_decision_pause_skips_client_build_and_creates_no_row(monkeypatch):
    for key, value in {"LEGO_SYMBOL": "UBER", "LEGO_FIX_C": "10000", "LEGO_DIFF": "25",
                       "LEGO_DNA_CODE": "bypass:100", "LEGO_DECIMAL_PRECISION": "5",
                       "LEGO_SLOT_SECONDS": "900", "LEGO_DNA_CLOCK_MODE": "shadow",
                       "FIREBASE_DB_URL": "https://test.firebaseio.com"}.items():
        monkeypatch.setenv(key, value)
    class Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(main, "datetime", Now)
    monkeypatch.setattr(main, "build_clients", lambda: pytest.fail("paused decision built broker clients"))
    pause(webull_io.market_data_scope(CFG))
    with tick_runtime.tick_scope("decision"):
        body, code = main.lego_one_row(object())
    assert code == 200 and body["pipeline_status"] == circuit.BACKOFF
    assert body["committed"] is False
    assert FAKE_DB.reference("webull_lego_rows").get() is None
    assert FAKE_DB.reference("webull_lego_order_outbox").get() is None


def test_tick_keeps_recovery_and_skips_new_dispatch_during_pause(monkeypatch):
    bundle = SimpleNamespace(interval_seconds=900, origin_utc=None,
                             calendar_fingerprint=None, dna_code="bypass:100")
    runtime = SimpleNamespace(
        operator=SimpleNamespace(symbol="UBER", principal_usd=10000, diff_usd=25,
                                 dna_bundle=bundle, mode="trade", active=True),
        deployment=SimpleNamespace(environment="UAT"), allows_new_broker_mutation=True)
    monkeypatch.setattr(main, "load_runtime_config", lambda: runtime)
    monkeypatch.setattr(main.execution_service, "_init_firebase", lambda: None)
    calls = []
    def recover(*args, **kwargs):
        calls.append(kwargs["limit"])
        return {"processed": 1, "results": [{"status": "FILLED", "cashflow_finalized": True}]}
    monkeypatch.setattr(main.execution_service, "_run_order_worker", recover)
    response = pause("scope").response()
    monkeypatch.setattr(main.decision_service, "run_decision", lambda *a, **k: (response, 200))
    monkeypatch.setattr(main, "archive_terminal_records", lambda **k: {})
    import operational_health
    monkeypatch.setattr(operational_health, "report", lambda *a: {})
    monkeypatch.setattr(main, "token_health", lambda: {})
    body, code = main.lego_tick(object())
    assert code == 200 and body["pipeline_status"] == circuit.BACKOFF
    assert body["business_status"] == circuit.BACKOFF
    assert body["recovery"]["results"][0]["cashflow_finalized"] is True
    assert calls == [3] and body["dispatch"] is None


def test_backoff_log_has_code_request_id_and_private_state_is_not_logged(capsys):
    error = pause("scope")
    body = error.response()
    observability.emit_tick(body, 200)
    event = json.loads(capsys.readouterr().out)
    assert event["severity"] == "WARNING" and event["business_status"] == circuit.BACKOFF
    assert event["errors"][0]["broker_error"]["request_id"] == REQUEST_ID
    assert event["errors"][0]["broker_error"]["code"] == "INTERNAL_ERROR"
    assert "private broker response" not in json.dumps(event)
    assert "probe_owner" not in json.dumps(event)


def test_manual_reconciliation_keeps_priority_over_quote_backoff():
    body = {"pipeline_status": circuit.BACKOFF,
            "recovery": {"dispatch_blocked": True, "results": []}}
    assert observability.business_status(body, 200) == "MANUAL_RECONCILIATION_REQUIRED"


def test_unrelated_recovery_error_is_not_hidden_by_quote_backoff():
    body = {"pipeline_status": circuit.BACKOFF,
            "recovery": {"results": [{"error": "ledger failed", "error_type": "RuntimeError"}]}}
    assert observability.business_status(body, 200) == "ERROR"


def test_monitoring_includes_http_200_market_data_backoff():
    from tools.monitoring_config import build
    policy = build("lego-tick-uat", "projects/demo/notificationChannels/1")
    assert circuit.BACKOFF in policy["health-policy"]["conditions"][0]["conditionMatchedLog"]["filter"]
