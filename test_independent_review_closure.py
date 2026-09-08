from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from config import ConfigurationError, load_runtime_config
from conftest import FAKE_DB
from lego_one_row import Config
from lego_outbox import (OUTBOX_PATH, account_symbol_fence_key,
                         list_actionable, list_audit_pending, put_intent,
                         update_intent)
from lego_state import (BROKER_CASHFLOW_PATH, REALIZED_EVENT_ARCHIVE_PATH,
                        REALIZED_HOT_EVENT_LIMIT, REALIZED_PATH,
                        STATE_PATH, apply_broker_cashflow, apply_realized_fill,
                        chain_key)
import lego_outbox
import main


@pytest.fixture(autouse=True)
def clean_db():
    FAKE_DB.store.clear()


def _v2_cfg(**changes):
    cfg = Config("AAPL", 1500.0, 25.0, "bypass:100",
                 "shannon_demon_lego_v2", 5, 0.00001)
    return replace(cfg, **changes)


def _runtime_env(**changes):
    env = {
        "LEGO_SYMBOL": "AAPL", "LEGO_FIX_C": "1500", "LEGO_DIFF": "25",
        "WEBULL_ENV": "UAT", "WEBULL_ACCOUNT_ID": "test-account",
    }
    env.update(changes)
    return env


def test_broker_lot_capability_does_not_fork_v2_strategy_chain():
    base = _v2_cfg()
    derived = _v2_cfg(decimal_precision=0, quantity_increment=1.0)
    assert chain_key(base) == chain_key(derived)
    legacy = replace(base, strategy_id="shannon_demon_lego")
    assert chain_key(legacy) != chain_key(replace(legacy, decimal_precision=0))


def test_legacy_auto_submit_cannot_enable_v2_and_diff_is_required():
    runtime = load_runtime_config(_runtime_env(AUTO_SUBMIT="true"))
    assert runtime.operator.mode == "observe"
    assert runtime.operator.active is False
    missing = _runtime_env()
    missing.pop("LEGO_DIFF")
    with pytest.raises(ConfigurationError):
        load_runtime_config(missing)


def test_account_symbol_fence_is_stable_across_strategy_config_changes():
    identity = "uat-account-fingerprint"
    assert account_symbol_fence_key(identity, "aapl") == \
        account_symbol_fence_key(identity, "AAPL")
    assert chain_key(_v2_cfg(diff=25.0)) != chain_key(_v2_cfg(diff=30.0))


def test_new_config_worker_reconciles_old_chain_inflight_before_new_work(monkeypatch):
    old_cfg = _v2_cfg(diff=25.0)
    new_cfg = _v2_cfg(diff=30.0)
    old_chain, new_chain = chain_key(old_cfg), chain_key(new_cfg)
    identity = "runtime-identity"
    FAKE_DB.reference(f"{STATE_PATH}/{new_chain}").set({
        "version": 1, "runtime_identity_fingerprint": identity,
    })
    run_id = "a" * 32
    FAKE_DB.reference(f"webull_lego_rows/{run_id}").set({
        "run_id": run_id, "chain_key": old_chain, "committed": True,
    })
    put_intent(old_chain, run_id, {
        "status": "SUBMITTED", "created_at": "2026-09-06T00:00:00Z",
        "runtime_identity_fingerprint": identity,
        "strategy_config": {
            "symbol": old_cfg.symbol, "fix_c": old_cfg.fix_c,
            "diff": old_cfg.diff, "dna_code": old_cfg.dna_code,
            "strategy_id": old_cfg.strategy_id,
            "decimal_precision": old_cfg.decimal_precision,
            "quantity_increment": old_cfg.quantity_increment,
        },
    })
    scope = account_symbol_fence_key(identity, "AAPL")
    old_time = datetime.now(timezone.utc) - timedelta(seconds=10)
    first = lego_outbox.claim_chain_dispatch(
        scope, "old-worker", now_utc=old_time, lease_seconds=1)
    assert first is not None
    assert lego_outbox.fence_chain_dispatch(
        scope, run_id, "old-worker", first["claim_token"],
        intent_chain_key=old_chain, now_utc=old_time,
        lease_seconds=1) is not None
    lego_outbox.release_chain_dispatch(
        scope, "old-worker", first["claim_token"])

    monkeypatch.setattr(main, "build_clients", lambda: (object(), object()))
    monkeypatch.setattr(main, "fetch_order_detail", lambda *_args: {
        "order_status": "SUBMITTED"})
    result = main._run_order_worker(
        new_cfg, limit=1, runtime_identity=identity)
    assert result["processed"] == 1
    assert result["results"][0]["run_id"] == run_id
    assert result["results"][0]["status"] == "SUBMITTED"


def test_broker_cashflow_keeps_unknown_fee_pending_then_applies_only_late_fee():
    partial = apply_broker_cashflow(
        "ck", "run", "BUY", "2", "100.125", None)
    assert partial["broker_fee_status"] == "PENDING"
    assert partial["broker_cash_cumulative"] == "-200.250"
    terminal = apply_broker_cashflow(
        "ck", "run", "BUY", "4", "101.125", None)
    assert terminal["broker_fee_status"] == "PENDING"
    assert terminal["broker_cash_cumulative"] == "-404.500"
    corrected = apply_broker_cashflow(
        "ck", "run", "BUY", "4", "101.125", "0.375")
    assert corrected["broker_fee_status"] == "KNOWN"
    assert corrected["broker_cash_cumulative"] == "-404.875"
    replay = apply_broker_cashflow(
        "ck", "run", "BUY", "4", "101.125", "0.375")
    assert replay["broker_cashflow_applied_now"] is False
    stored = FAKE_DB.reference(
        f"{BROKER_CASHFLOW_PATH}/ck/events/run").get()
    assert stored["actual_fees"] == "0.375"


def test_realized_hot_idempotency_set_is_bounded_and_old_replay_is_noop():
    total = REALIZED_HOT_EVENT_LIMIT + 8
    for index in range(total):
        apply_realized_fill("ck", f"event-{index:03d}", "BUY", 1, 100, 0)
    head = FAKE_DB.reference(f"{REALIZED_PATH}/ck").get()
    assert len(head["applied_fills"]) == REALIZED_HOT_EVENT_LIMIT
    assert FAKE_DB.reference(
        f"{REALIZED_EVENT_ARCHIVE_PATH}/ck/event-000").get() is not None
    before_seq = head["applied_seq"]
    replay = apply_realized_fill("ck", "event-000", "BUY", 1, 100, 0)
    after = FAKE_DB.reference(f"{REALIZED_PATH}/ck").get()
    assert replay["realized_delta"] == 0
    assert after["applied_seq"] == before_seq


def test_outbox_hot_reads_use_query_keys_and_return_only_bounded_work():
    for index in range(200):
        put_intent("ck", f"done-{index:03d}", {
            "created_at": f"2026-01-01T00:{index % 60:02d}:00Z"})
        update_intent("ck", f"done-{index:03d}", {
            "status": "REJECTED", "audit_pending": False})
    for index in range(7):
        put_intent("ck", f"live-{index}", {
            "created_at": f"2026-09-06T00:0{index}:00Z"})
    update_intent("ck", "live-5", {"audit_pending": True})
    update_intent("ck", "live-6", {"audit_pending": True})
    assert len(list_actionable("ck", limit=3)) == 3
    assert {item["run_id"] for item in list_audit_pending("ck", limit=1)} \
        <= {"live-5", "live-6"}

