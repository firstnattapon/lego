from decimal import Decimal

import pytest

from config import load_runtime_config
from conftest import FAKE_DB
from execution_service import _apply_realized_if_available
from lego_orders import summarize_order_result
from lego_state import (BROKER_CASHFLOW_PATH, REALIZED_LOT_PAGE_PATH,
                        REALIZED_PATH, apply_broker_cashflow,
                        apply_realized_fill, migrate_realized_open_legs,
                        rollback_realized_open_legs_migration)
import main


@pytest.fixture(autouse=True)
def clean_db():
    FAKE_DB.store.clear()


def test_unknown_fee_becoming_known_zero_is_persisted_once():
    first = apply_broker_cashflow("ck", "run", "BUY", "2", "100", None)
    known = apply_broker_cashflow("ck", "run", "BUY", "2", "100", "0")
    replay = apply_broker_cashflow("ck", "run", "BUY", "2", "100", "0")

    assert first["broker_fee_status"] == "PENDING"
    assert known["broker_fee_status"] == "KNOWN"
    assert known["broker_cashflow_applied_now"] is True
    assert known["broker_cash_cumulative"] == "-200"
    assert replay["broker_cashflow_applied_now"] is False
    stored = FAKE_DB.reference(
        f"{BROKER_CASHFLOW_PATH}/ck/events/run").get()
    assert stored["actual_fees"] == "0"


def test_known_fee_is_not_downgraded_by_missing_and_malformed_is_rejected():
    known = apply_broker_cashflow("fees", "run", "SELL", "2", "100", "0.5")
    missing = apply_broker_cashflow("fees", "run", "SELL", "2", "100", None)
    assert known["broker_fee_status"] == "KNOWN"
    assert missing["broker_fee_status"] == "KNOWN"
    assert missing["broker_cashflow_applied_now"] is False
    stored = FAKE_DB.reference(
        f"{BROKER_CASHFLOW_PATH}/fees/events/run").get()
    assert stored["actual_fees"] == "0.5"
    with pytest.raises(ValueError, match="actual fees"):
        apply_broker_cashflow("fees", "bad", "BUY", "1", "100", "oops")
    malformed_summary = summarize_order_result({}, {
        "order_status": "FILLED", "filled_quantity": "1",
        "filled_price": "100", "filled_fee": "oops",
    })
    assert "filled_fee" not in malformed_summary


def test_broker_decimal_strings_survive_summary_and_cashflow_exactly():
    raw = {
        "order_status": "FILLED",
        "filled_quantity": "2",
        "filled_price": "100.1234567890123456789",
        "filled_fee": "0.1234567890123456789",
    }
    summary = summarize_order_result({}, raw)
    actual = _apply_realized_if_available(
        {"chain_key": "precision", "run_id": "order", "side": "BUY"},
        summary,
    )
    expected = (
        -Decimal(raw["filled_quantity"]) * Decimal(raw["filled_price"])
        - Decimal(raw["filled_fee"])
    )

    assert summary["filled_quantity"] == raw["filled_quantity"]
    assert summary["filled_price"] == raw["filled_price"]
    assert summary["filled_fee"] == raw["filled_fee"]
    assert Decimal(actual["broker_cash_cumulative"]) == expected


def test_broker_decimal_summary_accepts_scientific_notation_and_rejects_nonfinite():
    valid = summarize_order_result({}, {
        "order_status": "FILLED", "filled_quantity": "2E+1",
        "filled_price": "1.001234567890123456789E+2",
        "filled_fee": "1.25E-7",
    })
    assert valid["filled_quantity"] == "2E+1"
    assert valid["filled_price"] == "100.1234567890123456789"
    assert valid["filled_fee"] == "1.25E-7"
    invalid = summarize_order_result({}, {
        "order_status": "FILLED", "filled_quantity": "NaN",
        "filled_price": "Infinity", "filled_fee": "-0.1",
    })
    assert "filled_quantity" not in invalid
    assert "filled_price" not in invalid
    assert "filled_fee" not in invalid


def test_v2_tick_ignores_legacy_decimal_precision_before_recovery(monkeypatch):
    runtime = load_runtime_config({
        "LEGO_SYMBOL": "AAPL", "LEGO_FIX_C": "1500", "LEGO_DIFF": "25",
        "WEBULL_ACCOUNT_ID": "audit-fake", "WEBULL_ENV": "UAT",
    })
    monkeypatch.setattr(main, "load_runtime_config", lambda: runtime)
    monkeypatch.setattr(main.execution_service, "_init_firebase", lambda: None)
    monkeypatch.setenv("LEGO_DECIMAL_PRECISION", "invalid-legacy-value")
    recovery = []
    monkeypatch.setattr(
        main.execution_service, "_run_order_worker",
        lambda *args, **kwargs: recovery.append(True) or {"processed": 0},
    )
    monkeypatch.setattr(
        main.decision_service, "run_decision",
        lambda *args, **kwargs: ({"pipeline_status": "PASS"}, 200),
    )
    monkeypatch.setattr(main, "archive_terminal_records", lambda **kwargs: {})

    body, code = main.lego_tick(None)

    assert code == 200
    assert body["pipeline_status"] == "TICK_OK"
    assert len(recovery) == 1


def test_single_fill_resumes_across_fifo_pages_and_matches_reference_oracle():
    prices = [100.0 + index for index in range(25)]
    for index, buy_price in enumerate(prices):
        result = apply_realized_fill(
            "fifo", f"buy-{index:02d}", "BUY", 1, buy_price, 0.1)
        assert result["matching_pending"] is False

    calls = 0
    result = {"matching_pending": True}
    while result["matching_pending"]:
        result = apply_realized_fill("fifo", "sell", "SELL", 25, 200, 2.5)
        calls += 1
        assert calls < 10

    oracle = sum((200.0 - buy_price) - 0.1 - 0.1 for buy_price in prices)
    assert calls == 4
    assert result["realized_delta"] == pytest.approx(oracle)
    assert result["realized_cumulative"] == pytest.approx(oracle)
    head = FAKE_DB.reference(f"{REALIZED_PATH}/fifo").get()
    assert head["active_matching_event_id"] is None
    assert len(head["open_legs"]["buys"]) == 0
    assert head["fifo_read_cursor"]["buys"] == 25
    pages = FAKE_DB.reference(f"{REALIZED_LOT_PAGE_PATH}/fifo/buys").get()
    assert len(pages) == 25


def test_fifo_head_is_constant_size_while_immutable_history_grows():
    sizes = []
    for index in range(400):
        apply_realized_fill(
            "bounded", f"buy-{index:04d}", "BUY", 1,
            100 + index / 1000, 0)
        if index + 1 in {100, 200, 400}:
            import json
            head = FAKE_DB.reference(f"{REALIZED_PATH}/bounded").get()
            sizes.append(len(json.dumps(
                head, separators=(",", ":")).encode("utf-8")))
            assert len(head["open_legs"]["buys"]) <= 16
            assert len(head["applied_fills"]) <= 64
    assert max(sizes) - min(sizes) < 1024
    assert max(sizes) < 65536


@pytest.mark.parametrize("point", [
    "after_event_checkpoint",
    "after_page_write_before_head",
    "after_page_head_commit",
    "before_event_finalize",
    "after_event_finalize",
])
def test_fifo_append_crash_windows_resume_without_lost_or_duplicate_lot(point):
    fired = []

    def crash(name):
        if name == point and not fired:
            fired.append(name)
            raise RuntimeError(f"injected:{name}")

    with pytest.raises(RuntimeError, match="injected"):
        apply_realized_fill(
            f"crash-{point}", "buy", "BUY", 1, 100, 0.1,
            _fault_hook=crash)
    resumed = apply_realized_fill(
        f"crash-{point}", "buy", "BUY", 1, 100, 0.1)
    replay = apply_realized_fill(
        f"crash-{point}", "buy", "BUY", 1, 100, 0.1)
    pages = FAKE_DB.reference(
        f"{REALIZED_LOT_PAGE_PATH}/crash-{point}/buys").get()
    assert resumed["matching_pending"] is False
    assert replay["realized_delta"] == 0
    assert len(pages) == 1


def test_fifo_match_crash_after_atomic_head_commit_resumes_exactly_once():
    chain = "crash-match"
    for index in range(12):
        apply_realized_fill(chain, f"buy-{index}", "BUY", 1, 100 + index, 0)
    fired = []

    def crash(name):
        if name == "after_match_head_commit" and not fired:
            fired.append(name)
            raise RuntimeError("injected match crash")

    with pytest.raises(RuntimeError, match="injected match crash"):
        apply_realized_fill(
            chain, "sell", "SELL", 12, 200, 0,
            _fault_hook=crash)
    result = {"matching_pending": True}
    while result["matching_pending"]:
        result = apply_realized_fill(chain, "sell", "SELL", 12, 200, 0)
    oracle = sum(200 - (100 + index) for index in range(12))
    assert result["realized_cumulative"] == pytest.approx(oracle)
    assert apply_realized_fill(
        chain, "sell", "SELL", 12, 200, 0)["realized_delta"] == 0


def test_archive_before_evict_crash_and_old_replay_preserve_idempotency():
    chain = "crash-archive"
    for index in range(64):
        apply_realized_fill(chain, f"buy-{index:03d}", "BUY", 1, 100, 0)
    fired = []

    def crash(name):
        if name == "after_archive_before_eviction" and not fired:
            fired.append(name)
            raise RuntimeError("injected archive crash")

    with pytest.raises(RuntimeError, match="archive crash"):
        apply_realized_fill(
            chain, "buy-064", "BUY", 1, 100, 0, _fault_hook=crash)
    apply_realized_fill(chain, "buy-064", "BUY", 1, 100, 0)
    before = FAKE_DB.reference(f"{REALIZED_PATH}/{chain}").get()
    replay = apply_realized_fill(chain, "buy-000", "BUY", 1, 100, 0)
    after = FAKE_DB.reference(f"{REALIZED_PATH}/{chain}").get()
    assert replay["realized_delta"] == 0
    assert after["applied_seq"] == before["applied_seq"]


def test_legacy_fifo_migration_dry_run_checkpoint_resume_and_preservation():
    chain = "migration"
    legs = [[1 + index / 100, 100 + index / 10, index / 1000]
            for index in range(130)]
    FAKE_DB.reference(f"{REALIZED_PATH}/{chain}").set({
        "schema_version": 2,
        "open_legs": {"buys": legs, "sells": []},
        "cumulative_realized": "123.5",
        "applied_fills": {},
    })
    dry = migrate_realized_open_legs(chain, dry_run=True, max_pages=64)
    assert dry["total_lots"] == 130
    assert dry["remaining"] == 130
    assert dry["cumulative_realized"] == 123.5
    first = migrate_realized_open_legs(chain, max_pages=64)
    second = migrate_realized_open_legs(chain, max_pages=64)
    assert first["next_index"] == 64 and first["complete"] is False
    assert second["next_index"] == 128 and second["complete"] is False
    checkpoint = FAKE_DB.reference(
        f"{REALIZED_PATH}/{chain}/fifo_migration").get()
    assert checkpoint["next_index"] == 128
    final = migrate_realized_open_legs(chain, max_pages=64)
    assert final["complete"] is True
    head = FAKE_DB.reference(f"{REALIZED_PATH}/{chain}").get()
    pages = FAKE_DB.reference(
        f"{REALIZED_LOT_PAGE_PATH}/{chain}/generations/"
        f"{head['fifo_page_generation']}/buys").get()
    assert head["schema_version"] == 3
    assert head["cumulative_realized"] == "123.5"
    assert head["fifo_write_cursor"]["buys"] == 130
    assert len(head["open_legs"]["buys"]) == 16
    assert len(pages) == 130
    assert sum(float(page["quantity"]) for page in pages.values()) \
        == pytest.approx(sum(leg[0] for leg in legs))
    assert sum(float(page["quantity"]) * float(page["fee_per_share"])
               for page in pages.values()) \
        == pytest.approx(sum(leg[0] * leg[2] for leg in legs))


def test_unfinalized_fifo_migration_can_rollback_without_touching_legacy_head():
    chain = "migration-rollback"
    legs = [[1, 100 + index, 0.01] for index in range(70)]
    original = {"buys": legs, "sells": []}
    FAKE_DB.reference(f"{REALIZED_PATH}/{chain}").set({
        "schema_version": 2, "open_legs": original,
        "cumulative_realized": 42.0,
    })
    partial = migrate_realized_open_legs(chain, max_pages=20)
    assert partial["complete"] is False
    generation = FAKE_DB.reference(
        f"{REALIZED_PATH}/{chain}/fifo_migration/generation").get()
    rolled = rollback_realized_open_legs_migration(chain)
    assert rolled == {"rolled_back": True, "pages_deleted": 0}
    head = FAKE_DB.reference(f"{REALIZED_PATH}/{chain}").get()
    assert head["open_legs"] == original
    assert head["cumulative_realized"] == 42.0
    assert "fifo_migration" not in head
    assert head["fifo_migration_epoch"] == 1
    assert len(FAKE_DB.reference(
        f"{REALIZED_LOT_PAGE_PATH}/{chain}/generations/{generation}/buys").get()) == 20
    migrate_realized_open_legs(chain, max_pages=64)
    assert migrate_realized_open_legs(chain, max_pages=64)["complete"]
    head = FAKE_DB.reference(f"{REALIZED_PATH}/{chain}").get()
    assert head["fifo_page_generation"] != generation
    result = apply_realized_fill(chain, "after-rollback", "SELL", 1, 200, 0)
    assert result["realized_cumulative"] == pytest.approx(141.99)


def test_long_alternating_fifo_stream_matches_independent_reference_oracle():
    chain = "long-oracle"
    oracle = {"buys": [], "sells": []}
    oracle_realized = 0.0

    for index in range(300):
        side = "BUY" if index % 5 in {0, 1, 3} else "SELL"
        qty = 0.25 + (index % 7) * 0.125
        price = 80.0 + ((index * 37) % 211) / 10.0
        fee = qty * (0.005 + (index % 3) * 0.0025)
        own = "buys" if side == "BUY" else "sells"
        opposite = "sells" if side == "BUY" else "buys"
        remaining = qty
        fee_ps = fee / qty
        while remaining > 1e-9 and oracle[opposite]:
            oq, op, ofps = oracle[opposite][0]
            matched = min(remaining, oq)
            oracle_realized += (
                (op - price) * matched if side == "BUY"
                else (price - op) * matched)
            oracle_realized -= (ofps + fee_ps) * matched
            remaining -= matched
            if oq - matched <= 1e-9:
                oracle[opposite].pop(0)
            else:
                oracle[opposite][0][0] = oq - matched
        if remaining > 1e-9:
            oracle[own].append([remaining, price, fee_ps])

        result = {"matching_pending": True}
        while result["matching_pending"]:
            result = apply_realized_fill(
                chain, f"event-{index:03d}", side, qty, price, fee)
        assert result["realized_cumulative"] == pytest.approx(
            oracle_realized, abs=1e-8)

    head = FAKE_DB.reference(f"{REALIZED_PATH}/{chain}").get()
    assert head["cumulative_realized"] == pytest.approx(oracle_realized)
    assert not (head["open_legs"]["buys"] and head["open_legs"]["sells"])


def test_conflicting_fifo_event_defers_then_resumes_without_permanent_error():
    fired = []

    def crash(name):
        if name == "after_event_checkpoint" and not fired:
            fired.append(name)
            raise RuntimeError("leave active checkpoint")

    with pytest.raises(RuntimeError, match="active checkpoint"):
        apply_realized_fill(
            "conflict", "event-a", "BUY", 1, 100, 0,
            _fault_hook=crash)
    deferred = apply_realized_fill(
        "conflict", "event-b", "BUY", 1, 101, 0)
    assert deferred["matching_pending"] is True
    assert deferred["matching_reason"] == "FIFO_OTHER_EVENT_ACTIVE"
    assert apply_realized_fill(
        "conflict", "event-a", "BUY", 1, 100, 0)["matching_pending"] is False
    assert apply_realized_fill(
        "conflict", "event-b", "BUY", 1, 101, 0)["matching_pending"] is False
