from __future__ import annotations

import pytest

from conftest import FAKE_DB
from lego_one_row import (Anchor, Config, ExecutionFill, PASS_THRESHOLD,
                          READY_BUY, compute_recurrence, compute_row,
                          finalize_recurrence)
from lego_orders import apply_fill
from lego_outbox import list_actionable, put_intent, update_intent
from lego_state import apply_realized_fill, commit_final_row, read_anchor


@pytest.fixture(autouse=True)
def reset_db(monkeypatch):
    FAKE_DB.store.clear()
    monkeypatch.setenv("LEGO_SLOT_SECONDS", "600")


def test_critical_1_pass_threshold_signal_one_freezes_model_ledger():
    cfg = Config(symbol="AAPL", fix_c=3000.0, diff=5.0,
                 dna_code="bypass:10", decimal_precision=2)
    anchor = Anchor(version=1, dna_step=0, p0=320.64,
                    prev_price=320.64, prev_actual=0.0)
    snap = {"captured_at": "2026-07-23T18:10:05Z",
            "price": 320.87, "holdings": 9.34492}
    row = compute_row(cfg, snap, anchor)
    assert row["DNA signal"] == 1
    assert row["สถานะ"] == PASS_THRESHOLD
    assert row["จำนวนสั่ง (หุ้น)"] == 0
    assert row["ΔAₙ ต่อสเต็ป (USD)"] == 0
    assert row["Aₙ สะสม (USD)"] == 0
    assert row["_meta"]["acted"] is False
    assert row["_meta"]["acted_price_next"] == pytest.approx(320.64)


def test_ready_decision_alone_never_moves_the_model_ledger():
    """A READY_* is an intent. Only a confirmed fill may book ΔAₙ."""
    cfg = Config(symbol="AAPL", fix_c=3000.0, diff=5.0,
                 dna_code="bypass:10", decimal_precision=2)
    anchor = Anchor(version=2, dna_step=1, p0=320.64,
                    prev_price=320.64, prev_actual=0.0)
    snap = {"captured_at": "2026-07-23T18:20:14Z",
            "price": 320.32, "holdings": 9.34492}
    row = compute_row(cfg, snap, anchor)
    assert row["สถานะ"] == READY_BUY
    assert row["ΔAₙ ต่อสเต็ป (USD)"] == 0.0
    assert row["Aₙ สะสม (USD)"] == pytest.approx(anchor.prev_actual)
    assert row["_meta"]["execution_pending"] is True

    # ...and the same arithmetic, once the broker fills it at that price.
    final = finalize_recurrence(
        cfg,
        ExecutionFill(filled_price=320.32, filled_quantity=row["จำนวนสั่ง (หุ้น)"],
                      holdings_after=9.4),
        last_action_price=anchor.prev_price,
        actual_cumulative=anchor.prev_actual,
        reference_R=row["Rₙ อ้างอิง (USD)"])
    assert final.dA == pytest.approx(3000.0 * (320.32 / 320.64 - 1.0))


def test_compute_recurrence_requires_boolean_acted():
    cfg = Config("AAPL", 3000.0)
    anchor = Anchor(1, 0, 100.0, 100.0, 0.0)
    with pytest.raises(ValueError):
        compute_recurrence(cfg, 101.0, anchor, acted=1)


def test_critical_2_realized_occurs_only_when_fill_legs_close():
    legs, realized1 = apply_fill(None, "BUY", 2.0, 100.0, fee=1.0)
    assert realized1 == 0.0
    legs, realized2 = apply_fill(legs, "SELL", 1.5, 110.0, fee=0.75)
    assert realized2 == pytest.approx(13.5)
    assert legs["buys"][0][0] == pytest.approx(0.5)


def test_realized_fill_is_idempotent_and_partial_fill_uses_incremental_price():
    ck = "AAPL_test"
    first = apply_realized_fill(ck, "buy-1", "BUY", 1.0, 100.0, 0.10)
    assert first["realized_cumulative"] == 0.0
    again = apply_realized_fill(ck, "buy-1", "BUY", 1.0, 100.0, 0.10)
    assert again["realized_cumulative"] == 0.0
    partial = apply_realized_fill(ck, "buy-1", "BUY", 2.0, 105.0, 0.20)
    assert partial["open_legs"]["buys"][0][:2] == pytest.approx([1.0, 100.0])
    assert partial["open_legs"]["buys"][1][:2] == pytest.approx([1.0, 110.0])
    closed = apply_realized_fill(ck, "sell-1", "SELL", 2.0, 120.0, 0.20)
    assert closed["realized_delta"] == pytest.approx(29.6)
    assert closed["realized_cumulative"] == pytest.approx(29.6)


def test_critical_3_old_pending_does_not_block_next_dna_row():
    cfg = Config("AAPL", 3000.0, diff=5.0, dna_code="bypass:20", decimal_precision=2)
    snap0 = {"captured_at": "2026-07-23T18:00:05Z", "price": 320.0, "holdings": 9.0}
    row0 = compute_row(cfg, snap0, None, dna_step=0)
    commit_final_row(cfg, snap0, None, row0)
    anchor = read_anchor(cfg)
    snap2 = {"captured_at": "2026-07-23T18:20:05Z", "price": 321.0, "holdings": 9.0}
    row2 = compute_row(cfg, snap2, anchor, dna_step=2)
    result = commit_final_row(cfg, snap2, anchor, row2)
    assert result["committed"] is True
    assert read_anchor(cfg).dna_step == 2


def test_critical_3_outbox_keeps_multiple_slot_intents():
    ck = "AAPL_test"
    put_intent(ck, "r1", {"status": "PENDING_DISPATCH", "created_at": "2026-07-23T18:00:00Z"})
    put_intent(ck, "r2", {"status": "PENDING_DISPATCH", "created_at": "2026-07-23T18:10:00Z"})
    assert [x["run_id"] for x in list_actionable(ck)] == ["r1", "r2"]
    update_intent(ck, "r1", {"status": "EXPIRED_UNSENT"})
    assert [x["run_id"] for x in list_actionable(ck)] == ["r2"]


def test_17_column_contract_is_preserved():
    cfg = Config("AAPL", 3000.0, diff=5.0, dna_code="bypass:10", decimal_precision=2)
    snap = {"captured_at": "2026-07-23T18:00:05Z", "price": 320.0, "holdings": 9.0}
    row = compute_row(cfg, snap, None, dna_step=0)
    assert len([k for k in row if k != "_meta"]) == 18
