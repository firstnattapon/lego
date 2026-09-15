"""September 15 incident regressions; broker and RTDB are local doubles."""
import copy
import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

import decision_service
import main
import observability
import webull_io
from conftest import FAKE_DB, fake_trade_client
from config import load_runtime_config
from lego_one_row import Config
from lego_state import STATE_PATH, chain_key
from test_continuous_runtime import setup_tick
from test_main_pipeline import _fixed_now


@pytest.fixture
def typed_decision(monkeypatch):
    FAKE_DB.store.clear()
    env = {
        "LEGO_SYMBOL": "AAPL", "LEGO_FIX_C": "3000", "LEGO_DIFF": "25",
        "LEGO_DNA_CODE": "bypass:100", "LEGO_SLOT_SECONDS": "900",
        "LEGO_DNA_ORIGIN_UTC": "2026-09-14T13:30:00Z",
        "LEGO_DNA_CLOCK_MODE": "market", "WEBULL_ENV": "UAT",
        "WEBULL_ACCOUNT_ID": "local-test", "FIREBASE_DB_URL": "https://test.invalid",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    runtime = load_runtime_config(env)
    cfg = Config("AAPL", 3000, 25, dna_code="bypass:100", strategy_id="shannon_demon_lego_v2")
    moment = datetime(2026, 9, 14, 13, 30, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(main, "datetime", _fixed_now(moment))
    calls = []
    def clients():
        calls.append("clients")
        return object(), object()
    monkeypatch.setattr(main, "build_clients", clients)
    monkeypatch.setattr(main, "token_health", lambda: {"ok": True, "reasons": []})
    monkeypatch.setattr(main, "fetch_snapshot", lambda *a: {
        "price": 333.4, "holdings": 9,
        "captured_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "quote_time": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    monkeypatch.setattr(decision_service, "fetch_instrument_capability",
                        lambda *a: SimpleNamespace(quantity_increment=Decimal("1")))
    decision_service.configure(main)
    def run():
        return decision_service.run_decision(None, runtime=runtime, cfg_override=cfg)
    first, code = run()
    assert code == 200 and first["committed"]
    return run, cfg, first, calls


def test_consumed_slot_survives_broker_outage_without_broker_calls(typed_decision, monkeypatch):
    run, cfg, first, calls = typed_decision
    before = copy.deepcopy(FAKE_DB.store)
    monkeypatch.setattr(decision_service, "build_clients",
                        lambda: pytest.fail("consumed slot must not contact broker"))
    body, code = run()
    assert (code, body["pipeline_status"]) == (200, "SLOT_CONSUMED")
    assert body["run_id"] == first["run_id"]
    assert body["dna_steps_remaining"] == 99
    assert FAKE_DB.store == before
    assert calls == ["clients"]


@pytest.mark.parametrize("field,value,status", [
    ("dna_fingerprint", "changed", "DNA_DRIFT"),
    ("calendar_fingerprint", "changed", "CALENDAR_DRIFT"),
    ("runtime_identity_fingerprint", "changed", "CONFIG_ERROR"),
])
def test_duplicate_fast_path_retains_drift_guards(typed_decision, field, value, status):
    run, cfg, first, calls = typed_decision
    FAKE_DB.reference(f"{STATE_PATH}/{chain_key(cfg)}").update({field: value})
    body, code = run()
    assert code >= 400 and body["pipeline_status"] == status
    assert len(calls) == 1


def test_duplicate_fast_path_repairs_crash_after_state_commit(typed_decision):
    run, cfg, first, calls = typed_decision
    row = FAKE_DB.reference(f"webull_lego_rows/{first['run_id']}")
    row.update({"committed": False})
    body, code = run()
    assert code == 200 and body["pipeline_status"] == "SLOT_CONSUMED"
    assert row.get()["committed"] is True
    assert FAKE_DB.reference(f"{STATE_PATH}/{chain_key(cfg)}").get()["version"] == 1


def test_new_slot_still_fetches_fresh_broker_inputs(typed_decision, monkeypatch):
    run, cfg, first, calls = typed_decision
    monkeypatch.setattr(decision_service, "datetime", _fixed_now(
        datetime(2026, 9, 14, 13, 45, 5, tzinfo=timezone.utc)))
    body, code = run()
    assert code == 200 and body["committed"] is True
    assert len(calls) == 2 and body["step"] == 1


def test_consumed_decision_does_not_skip_execution_recovery(monkeypatch, capsys):
    calls = setup_tick(monkeypatch, {"results": [{"status": "AWAITING_BROKER_FEE"}]})
    monkeypatch.setattr(decision_service, "run_decision", lambda *a, **k: (
        {"pipeline_status": "SLOT_CONSUMED", "dna_steps_remaining": 9}, 200))
    body, code = main.lego_tick(None)
    assert calls == [3] and code == 200
    assert body["business_status"] == "WAITING_BROKER_FEE"


@pytest.mark.parametrize("remaining,status,severity", [(370, "ROW_COMMITTED", "INFO"),
                                                       (10, "DNA_LOW", "WARNING"),
                                                       (0, "DNA_LOW", "WARNING")])
def test_dna_headroom_is_visible_in_scheduler_logs(remaining, status, severity, capsys):
    body = {"decision": {"pipeline_status": "ROW_COMMITTED", "dna_steps_remaining": remaining}}
    observability.emit_tick(body, 200)
    event = json.loads(capsys.readouterr().out)
    assert event["decision"]["dna_steps_remaining"] == remaining
    assert event["business_status"] == status and event["severity"] == severity


def test_dispatch_exception_log_identifies_class_without_secret_message(monkeypatch, capsys):
    setup_tick(monkeypatch, dispatch_error=True)
    body, code = main.lego_tick(None)
    event = json.loads(capsys.readouterr().out)
    assert code == 503
    assert event["errors"] == [{"phase": "dispatch", "type": "RuntimeError"}]
    assert "dispatch unavailable" not in json.dumps(event)


def test_shell_deployment_script_is_covered_by_candidate_identity(tmp_path):
    from tools.candidate_manifest import source_files
    shell = tmp_path / "deploy.sh"
    shell.write_text("echo deploy", encoding="utf-8")
    assert shell in list(source_files(tmp_path))


@pytest.mark.parametrize("payload", [
    {"orders": [], "pagination_key": "another-page"},
    {"data": [{"orders": [{"symbol": "AAPL"}]}]},
    {"orders": ["unreadable"]},
    {"orders": [None]},
    {"orders": [{}]},
    {"orders": [{"items": None}]},
    {"orders": [{"items": []}]},
    {"orders": None},
    {"orders": False},
])
def test_unproven_open_order_page_cannot_be_treated_as_empty(payload, monkeypatch):
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "local-test")
    with pytest.raises((ValueError, webull_io.IncompleteOpenOrdersError)):
        webull_io.fetch_open_orders(fake_trade_client(open_orders=payload), "AAPL")
