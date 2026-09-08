"""Adversarial round-three regressions, reusable against FakeDB or local RTDB."""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def growing_fill(db, *, quantity=12, price=201, fee=0.4, terminal=True):
    from decimal import Decimal
    import execution_service as execution
    import lego_state as state
    from lego_one_row import Config
    from lego_orders import summarize_order_result

    chain = "r3-grow-" + uuid.uuid4().hex
    ref = db.reference(f"{state.REALIZED_PATH}/{chain}")
    for index in range(25):
        state.apply_realized_fill(chain, f"buy-{index}", "BUY", 1, 100 + index, 0)
    intent = {"chain_key": chain, "run_id": "sell", "side": "SELL",
              "status": "SUBMITTED", "place_attempted": True}
    cfg = Config("AAPL", 1500, 25, strategy_id="shannon_demon_lego_v2")

    def summary(qty, avg, fees, status):
        return summarize_order_result({}, {
            "order_status": status, "filled_quantity": str(qty),
            "filled_price": str(avg), "filled_fee": str(fees)})

    finalized = {"applied": True, "delta_actual": 0, "actual_cumulative": 0,
                 "excess": 0, "holdings_after": 25 - quantity}
    with patch.object(execution, "_finalize_model_ledger", return_value=finalized) as model:
        first = execution._finish_with_realized(
            None, cfg, intent, summary(10, 200, 0, "PARTIALLY_FILLED"))
        assert first["status"] == "REALIZED_MATCHING_PENDING", first
        assert ref.get()["fifo_read_cursor"]["buys"] == 8
        latest = summary(quantity, price, fee, "FILLED" if terminal else "PARTIALLY_FILLED")
        second = execution._finish_with_realized(None, cfg, {**intent, **first}, latest)
        assert second["status"] == "REALIZED_MATCHING_PENDING", second
        assert not second.get("needs_manual_check")
        assert model.call_count == 0
        checkpoint_witness = ref.get()["applied_fills"]["sell"]
        assert checkpoint_witness["quantity"] == 10
        assert checkpoint_witness["fee"] == 0
        assert checkpoint_witness["average_price"] == 200
        result = second
        advances = [8, 2]
        for _ in range(8):
            before = ref.get()["fifo_read_cursor"]["buys"]
            result = execution._finish_with_realized(None, cfg, {**intent, **result}, latest)
            advances.append(ref.get()["fifo_read_cursor"]["buys"] - before)
            if result["status"] != "REALIZED_MATCHING_PENDING":
                break
        assert result["status"] != "REALIZED_MATCHING_PENDING", result
        assert not result.get("needs_manual_check")
        assert max(advances) <= 8
        assert model.call_count == int(terminal)
        head = ref.get()
        expected = quantity * price - sum(100 + i for i in range(quantity)) - fee
        assert abs(head["cumulative_realized"] - expected) < 1e-8, head
        assert head["applied_fills"]["sell"]["quantity"] == quantity
        assert head["applied_fills"]["sell"]["fee"] == fee
        cash = db.reference(f"{state.BROKER_CASHFLOW_PATH}/{chain}/events/sell").get()
        assert Decimal(cash["cumulative_notional"]) == Decimal(str(quantity)) * Decimal(str(price))
        execution._finish_with_realized(None, cfg, {**intent, **result}, latest)
        assert ref.get()["applied_seq"] == head["applied_seq"]
        assert ref.get()["cumulative_realized"] == head["cumulative_realized"]
        assert model.call_count == int(terminal)
    return {"quantity": quantity, "fee": fee, "terminal": terminal,
            "max_cursor_advance": max(advances), "realized": expected,
            "model_finalize_calls": int(terminal), "replay": "PASS"}


def migration_race(db, mode):
    import lego_state as state
    chain = "r3-migrate-" + uuid.uuid4().hex
    ref = db.reference(f"{state.REALIZED_PATH}/{chain}")
    ref.set({"schema_version": 2, "open_legs": {
        "buys": [[1, 100, 0], [1, 101, 0]], "sells": []},
        "cumulative_realized": 0})
    state.migrate_realized_open_legs(chain, max_pages=1)
    old_generation = ref.get()["fifo_migration"]["generation"]
    if mode == "finalize_first":
        # Another worker wins the head immediately before rollback's transaction.
        original = type(ref).transaction
        fired = []

        def transaction(reference, callback):
            if not fired:
                fired.append(True)
                state.migrate_realized_open_legs(chain, max_pages=64)
            return original(reference, callback)

        with patch.object(type(ref), "transaction", transaction):
            try:
                state.rollback_realized_open_legs_migration(chain)
            except ValueError as exc:
                assert "already finalized" in str(exc)
            else:
                raise AssertionError("finalized rollback must be rejected")
    elif mode == "cancel_writer":
        original = state._write_immutable_fifo_page
        fired = []

        def write(*args, **kwargs):
            page = original(*args, **kwargs)
            if not fired:
                fired.append(True)
                assert state.rollback_realized_open_legs_migration(chain)["rolled_back"]
            return page

        with patch.object(state, "_write_immutable_fifo_page", write):
            try:
                state.migrate_realized_open_legs(chain, max_pages=64)
            except ValueError as exc:
                assert "cancelled" in str(exc)
            else:
                raise AssertionError("cancelled writer must not link pages")
        assert ref.get()["schema_version"] == 2
        # A legitimate changed legacy source must not collide with orphan pages.
        db.reference(f"{state.REALIZED_PATH}/{chain}/open_legs/buys").set(
            [[1, 90, 0], [1, 91, 0]])
        state.migrate_realized_open_legs(chain, max_pages=64)
        assert ref.get()["fifo_page_generation"] != old_generation
    elif mode == "stale_checkpoint":
        original = state._write_immutable_fifo_page
        fired = []

        def write(*args, **kwargs):
            page = original(*args, **kwargs)
            if not fired:
                fired.append(True)
                state.migrate_realized_open_legs(chain, max_pages=64)
            return page

        with patch.object(state, "_write_immutable_fifo_page", write):
            state.migrate_realized_open_legs(chain, max_pages=1)
    else:
        raise AssertionError(mode)
    head = ref.get()
    assert head["schema_version"] == 3
    assert head["fifo_write_cursor"]["buys"] == 2
    assert head["fifo_migration"]["complete"]
    assert head["fifo_migration"]["next_index"] == 2
    result = state.apply_realized_fill(chain, "sell", "SELL", 2, 200, 0)
    assert not result["matching_pending"]
    assert result["realized_cumulative"] == (219 if mode == "cancel_writer" else 199)
    # Rollback never physically removes a page even when it loses the race.
    assert db.reference(f"{state.REALIZED_LOT_PAGE_PATH}/{chain}/generations/"
                        f"{old_generation}/buys/{0:020d}").get()
    return {"mode": mode, "linked_pages_valid": True, "subsequent_match": "PASS"}


def main():
    host = os.environ.get("FIREBASE_DATABASE_EMULATOR_HOST", "")
    project = os.environ.get("GCLOUD_PROJECT", "demo-lego-firebase")
    if not host.startswith(("127.0.0.1:", "localhost:")) or not project.startswith("demo-"):
        raise SystemExit("local RTDB emulator and demo project required")
    import firebase_admin
    from firebase_admin import db
    firebase_admin.initialize_app(options={
        "databaseURL": f"https://{project}-default-rtdb.firebaseio.com",
        "projectId": project})
    results = [growing_fill(db, **args) for args in (
        {}, {"quantity": 10, "price": 200, "fee": 0.5},
        {"quantity": 20, "price": 201, "fee": 1.2}, {"terminal": False})]
    results += [migration_race(db, mode) for mode in
                ("finalize_first", "cancel_writer", "stale_checkpoint")]
    print(json.dumps({"round3_adversarial_regressions": "PASS",
                      "real_rtdb_emulator": True, "results": results}, indent=2))


if __name__ == "__main__":
    main()
