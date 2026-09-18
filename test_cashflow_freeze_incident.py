"""The 2026-07-29 AAPL chain: a PASS row that moved the ledger, and the fence.

The incident, from the CSV export of chain AAPL_b33f0e5116a7:

    v  status           Pₙ       Rₙ      ΔAₙ      Aₙ      Eₙ
    1  READY_SELL   340.08     0.00     0.00    0.00    0.00
    2  READY_SELL   341.54    12.85    12.88   12.88    0.03
    3  PASS_THRESH  341.23    10.13    -2.72   10.16    0.03   <-- run a65b2583...
    4  READY_SELL   342.68    22.89    12.79   22.95    0.06

Row 3 is a PASS. It ordered nothing, filled nothing, and still booked
ΔAₙ = −2.72. The arithmetic identifies its author exactly: −2.72 is
3000 × (341.23/341.54 − 1), the *act* branch, taken with P_acted = 341.54 —
the price of the row before it. Only a revision that advances the ledger on
every row produces that, and this repository stopped doing so at 23a0cbc
(decision-gated) and again at 0a6513c (execution-confirmed). The rows also
carry no `semantics`, `market_slot_id` or `clock_mode`, fields main has written
for months. The code in this repository did not write these rows; an older
deployment did, against the same database.

So these tests do two things. They pin the numbers current main produces for
that exact slot — the freeze the CSV violated — and they pin the fence added
for the root cause, which is that nothing stopped an out-of-date revision from
writing a chain that had already moved on.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

import main
from conftest import FAKE_DB
from lego_cashflow_audit import (ACTUAL_COLUMN, DELTA_COLUMN, DISPLAY_PRICE_QUANTUM,
                                 EXCESS_COLUMN, REFERENCE_COLUMN, audit_rows,
                                 frozen_row_violations)
from lego_one_row import Anchor, Config, ExecutionFill, compute_row
from lego_state import (CASHFLOW_NO_ACTION, CASHFLOW_SEMANTICS,
                        CASHFLOW_SEMANTICS_HISTORY, EXECUTION_STATE_KEY,
                        STATE_PATH, CashflowSemanticsDowngrade, chain_key,
                        commit_final_row, finalize_execution_fill,
                        verify_cashflow_semantics)

UTC = timezone.utc
FIX_C = 3000.0
DIFF = 5.0

# The chain as exported, to the cent.
GENESIS_PRICE = 340.08
FILLED_PRICE = 341.54          # row 2's sell; holdings fell 9.14492 -> 8.78392
PASS_PRICE = 341.23            # row 3, the row under audit
PASS_HOLDINGS = 8.78392
CUMULATIVE_AFTER_FILL = 12.88
EXCESS_AFTER_FILL = 0.03
TARGET_RUN_ID = "a65b25831b8a49db85c980b828367816"

SLOT = datetime(2026, 7, 23, 18, 0, 5, tzinfo=UTC)


def _cfg(fix_c: float = FIX_C, diff: float = DIFF) -> Config:
    return Config(symbol="AAPL", fix_c=fix_c, diff=diff, decimal_precision=5)


def _snapshot(price: float, holdings: float) -> dict:
    return {"captured_at": "2026-07-29T14:05:06Z",
            "quote_time": "2026-07-29T14:05:06Z", "price": price,
            "holdings": holdings}


@pytest.fixture(autouse=True)
def _clean_db():
    FAKE_DB.store.clear()
    yield
    FAKE_DB.store.clear()


# --- 1. The mandatory regression case, verbatim ------------------------------

def test_pass_threshold_after_a_fill_freezes_every_cashflow_column():
    """fix_c 3000 · P_acted 340.08 · Aₙ₋₁ 12.88 · Eₙ₋₁ 0.03 · Pₙ 341.23 · no fill
    -> ΔAₙ 0.00 · Aₙ 12.88 · Eₙ 0.03 · P_acted 340.08 · NO_ACTION.

    P₀ is the one input the case leaves out, and it is not free: Eₙ₋₁ = Aₙ₋₁ −
    fix_c·ln(P_acted/P₀) pins it at 338.6264, and any other value would make the
    stated setup contradict itself before a row is computed. Derived here rather
    than hardcoded so the dependency is visible.
    """
    p_acted, prev_actual, prev_excess = 340.08, 12.88, 0.03
    p0 = p_acted * math.exp(-(prev_actual - prev_excess) / FIX_C)
    assert round(p0, 4) == 338.6264
    assert prev_actual - FIX_C * math.log(p_acted / p0) == pytest.approx(prev_excess)

    cfg = _cfg()
    anchor = Anchor(version=2, dna_step=1, p0=p0, prev_price=p_acted,
                    prev_actual=prev_actual, prev_holdings=8.7915)
    FAKE_DB.reference(f"{STATE_PATH}/{chain_key(cfg)}").set({
        "version": 2, "dna_step": 1, "p0": p0, "prev_price": p_acted,
        "prev_actual": prev_actual, "prev_holdings": 8.7915,
        "cashflow_semantics": CASHFLOW_SEMANTICS,
        "updated_at": "2026-07-29T13:50:06Z",
        EXECUTION_STATE_KEY: {"last_action_price": p_acted,
                              "actual_cumulative": prev_actual,
                              "finalized_seq": 1},
    })
    # holdings chosen so |gap| <= DIFF, which is what makes this PASS_THRESHOLD
    # rather than a READY_*: 8.7915 x 341.23 = 2999.996, gap = 0.0045.
    row = compute_row(cfg, _snapshot(PASS_PRICE, 8.7915), anchor, dna_step=2)

    assert row["สถานะ"] == "PASS_THRESHOLD"
    assert row["จำนวนสั่ง (หุ้น)"] == 0
    assert row[DELTA_COLUMN] == 0.0
    assert row[ACTUAL_COLUMN] == pytest.approx(12.88)
    assert row[EXCESS_COLUMN] == pytest.approx(0.03)
    assert row["_meta"]["acted_price_next"] == pytest.approx(340.08)
    assert row["_meta"]["execution_pending"] is False

    result = commit_final_row(cfg, _snapshot(PASS_PRICE, 8.7915), anchor, row)
    assert result["committed"] is True
    doc = FAKE_DB.reference(f"webull_lego_rows/{result['run_id']}").get()
    assert doc["cashflow_status"] == CASHFLOW_NO_ACTION
    assert doc["semantics"] == "execution_confirmed_v1"
    cashflow = FAKE_DB.reference(f"{STATE_PATH}/{chain_key(cfg)}").get()[EXECUTION_STATE_KEY]
    assert cashflow["last_action_price"] == pytest.approx(340.08)
    assert cashflow["actual_cumulative"] == pytest.approx(12.88)


# --- 2. The same slot, on the chain's own numbers -----------------------------

def test_the_target_run_id_slot_recomputed_on_the_real_chain():
    """Row 3 of the export, rebuilt by current main from rows 1-2 as recorded.

    Here P_acted is 341.54 — the price row 2's sell actually filled at, which is
    where the split puts it — not 340.08. Every cashflow column still freezes,
    and Rₙ still tracks the live price, which is the whole distinction the CSV
    lost.
    """
    cfg = _cfg()
    anchor = Anchor(version=2, dna_step=1, p0=GENESIS_PRICE,
                    prev_price=FILLED_PRICE, prev_actual=CUMULATIVE_AFTER_FILL,
                    prev_holdings=PASS_HOLDINGS)
    row = compute_row(cfg, _snapshot(PASS_PRICE, PASS_HOLDINGS), anchor, dna_step=2)

    assert row["สถานะ"] == "PASS_THRESHOLD"
    assert round(row["มูลค่าพอร์ต (USD)"], 2) == 2997.34      # as exported
    assert round(row["ส่วนต่างเป้าหมาย (USD)"], 2) == -2.66    # as exported
    assert round(row[REFERENCE_COLUMN], 2) == 10.13            # Rₙ still live
    assert row[DELTA_COLUMN] == 0.0                            # export said -2.72
    assert round(row[ACTUAL_COLUMN], 2) == 12.88               # export said 10.16
    assert round(row[EXCESS_COLUMN], 2) == 0.03
    assert row["_meta"]["acted_price_next"] == pytest.approx(FILLED_PRICE)


def test_the_exported_numbers_are_reproduced_only_by_the_retired_branch():
    """−2.72 is the act branch on a PASS row. Nothing in main can produce it."""
    exported_delta = FIX_C * (PASS_PRICE / FILLED_PRICE - 1.0)
    assert round(exported_delta, 2) == -2.72
    assert round(CUMULATIVE_AFTER_FILL + exported_delta, 2) == 10.16


# --- 3. The audit that finds these rows without touching them ------------------

def _exported_rows() -> list[dict]:
    """The four committed rows exactly as exported: 17 columns, no fill fields."""
    raw = [
        ("READY_SELL", 340.08, 9.14492, 0.323, 3110.0, -110.0, 0.0, 0.0, 0.0, 0.0),
        ("READY_SELL", 341.54, 9.14492, 0.361, 3123.36, -123.36, 12.85, 12.88, 12.88, 0.03),
        ("PASS_THRESHOLD", 341.23, 8.78392, 0.0, 2997.34, 2.66, 10.13, -2.72, 10.16, 0.03),
        ("READY_SELL", 342.68, 8.78392, 0.03, 3010.12, -10.12, 22.89, 12.79, 22.95, 0.06),
    ]
    ids = ["0081de2771e6e03fe68c91c834e69aa9", "727e475cbfe68ea0eaaf9495a556892d",
           TARGET_RUN_ID, "798b5813468b095ff357b8ee8ee4a08f"]
    return [
        {"สถานะ": status, "DNA step": i, "ราคา Pₙ (USD)": price,
         "จำนวนถือครอง (หุ้น)": holdings, "จำนวนสั่ง (หุ้น)": qty,
         "มูลค่าพอร์ต (USD)": value, "ส่วนต่างเป้าหมาย (USD)": gap,
         REFERENCE_COLUMN: reference, DELTA_COLUMN: delta,
         ACTUAL_COLUMN: actual, EXCESS_COLUMN: excess,
         "run_id": run_id, "version": i + 1, "committed": True}
        for i, (run_id, (status, price, holdings, qty, value, gap,
                         reference, delta, actual, excess))
        in enumerate(zip(ids, raw))
    ]


def _audit(rows):
    return audit_rows(FIX_C, rows, tolerance=DISPLAY_PRICE_QUANTUM,
                      price_quantum=DISPLAY_PRICE_QUANTUM)


def test_the_audit_names_the_target_row_column_by_column():
    results = _audit(_exported_rows())
    target = next(r for r in results if r.run_id == TARGET_RUN_ID)

    assert target.executed is False
    assert target.ok is False
    assert REFERENCE_COLUMN not in target.mismatched      # Rₙ was always right
    assert set(target.mismatched) == {DELTA_COLUMN, ACTUAL_COLUMN, EXCESS_COLUMN}
    assert target.expected[DELTA_COLUMN] == 0.0
    assert target.stored[DELTA_COLUMN] == -2.72


def test_the_audit_clears_the_same_chain_once_the_fills_are_recorded():
    """With the fill evidence the split writes, three of four rows come out clean.

    Row 4's ΔAₙ stays flagged, and correctly: it was computed against P_acted =
    341.23, the PASS row's decision price, which the PASS row was never entitled
    to advance. That is the corruption propagating, not a second bug.
    """
    rows = _exported_rows()
    rows[1].update({"cashflow_status": "FINALIZED", "execution_price": FILLED_PRICE,
                    "execution_quantity": 0.361, "post_execution_holdings": PASS_HOLDINGS})
    results = _audit(rows)

    assert results[0].ok and results[1].ok
    target = results[2]
    assert target.run_id == TARGET_RUN_ID
    assert target.expected[DELTA_COLUMN] == 0.0
    assert target.expected[ACTUAL_COLUMN] == pytest.approx(12.88)
    assert target.expected[EXCESS_COLUMN] == pytest.approx(0.03, abs=2e-3)
    assert set(target.mismatched) == {DELTA_COLUMN, ACTUAL_COLUMN}
    assert DELTA_COLUMN in results[3].mismatched


def test_a_correct_chain_audits_clean():
    """The freeze, written the way main writes it, must not be flagged."""
    rows = _exported_rows()
    rows[1].update({"cashflow_status": "FINALIZED", "execution_price": FILLED_PRICE,
                    "execution_quantity": 0.361, "post_execution_holdings": PASS_HOLDINGS})
    rows[2].update({DELTA_COLUMN: 0.0, ACTUAL_COLUMN: 12.88, EXCESS_COLUMN: 0.03,
                    "cashflow_status": CASHFLOW_NO_ACTION})
    rows[3].update({DELTA_COLUMN: 0.0, ACTUAL_COLUMN: 12.88,
                    EXCESS_COLUMN: 0.03, REFERENCE_COLUMN: 22.85,
                    "cashflow_status": "PENDING_EXECUTION"})
    results = _audit(rows)

    assert [r.ok for r in results] == [True, True, True, True]
    assert frozen_row_violations(results) == []


def test_every_unfilled_status_is_audited_as_frozen():
    """READY, SUBMITTED, PENDING_DISPATCH, rejected, expired — all the same rule."""
    for status in ("READY_BUY", "READY_SELL", "PASS_DNA_ZERO", "PASS_THRESHOLD"):
        for cashflow_status in (None, "PENDING_EXECUTION", "NO_ACTION"):
            rows = _exported_rows()[:2]
            rows[1].update({"สถานะ": status, DELTA_COLUMN: 0.0,
                            ACTUAL_COLUMN: 0.0, EXCESS_COLUMN: 0.0})
            if cashflow_status:
                rows[1]["cashflow_status"] = cashflow_status
            results = _audit(rows)
            assert results[1].executed is False, (status, cashflow_status)
            assert results[1].ok is True, (status, cashflow_status)


def test_a_zero_quantity_fill_record_is_not_a_fill():
    rows = _exported_rows()[:2]
    rows[1].update({"execution_quantity": 0.0, "execution_price": FILLED_PRICE,
                    DELTA_COLUMN: 0.0, ACTUAL_COLUMN: 0.0, EXCESS_COLUMN: 0.0})
    results = _audit(rows)
    assert results[1].executed is False and results[1].ok is True


# --- 4. The fence: an older accounting may not write a newer chain -------------

def test_semantics_history_is_ordered_and_current_is_last():
    assert CASHFLOW_SEMANTICS == CASHFLOW_SEMANTICS_HISTORY[-1]
    assert CASHFLOW_SEMANTICS_HISTORY.index("gated_theoretical_v2") < \
        CASHFLOW_SEMANTICS_HISTORY.index("execution_confirmed_v1")


@pytest.mark.parametrize("stored", [None, "", CASHFLOW_SEMANTICS])
def test_a_chain_on_this_runtime_or_none_at_all_passes(stored):
    assert verify_cashflow_semantics({"cashflow_semantics": stored}) is None
    assert verify_cashflow_semantics(None) is None
    assert verify_cashflow_semantics({}) is None


def test_an_older_chain_is_migrated_forward_and_reported():
    reported = verify_cashflow_semantics(
        {"cashflow_semantics": "gated_theoretical_v2"})
    assert reported == "gated_theoretical_v2"


def test_a_newer_chain_refuses_this_runtime(monkeypatch):
    monkeypatch.setattr("lego_state.CASHFLOW_SEMANTICS", "gated_theoretical_v2")
    with pytest.raises(CashflowSemanticsDowngrade, match="เก่ากว่า"):
        verify_cashflow_semantics(
            {"cashflow_semantics": "execution_confirmed_v1"})


def test_an_unrecognised_semantics_fails_closed():
    with pytest.raises(CashflowSemanticsDowngrade, match="ไม่รู้จัก"):
        verify_cashflow_semantics({"cashflow_semantics": "some_branch_v9"})


def _seed_state(semantics: str) -> Config:
    cfg = _cfg()
    FAKE_DB.reference(f"{STATE_PATH}/{chain_key(cfg)}").set({
        "version": 2, "dna_step": 1, "p0": GENESIS_PRICE,
        "prev_price": FILLED_PRICE, "prev_actual": CUMULATIVE_AFTER_FILL,
        "prev_holdings": PASS_HOLDINGS, "cashflow_semantics": semantics,
        "updated_at": "2026-07-29T13:50:06Z",
        EXECUTION_STATE_KEY: {"last_action_price": FILLED_PRICE,
                              "actual_cumulative": CUMULATIVE_AFTER_FILL,
                              "finalized_seq": 1},
    })
    return cfg


def test_commit_refuses_a_downgrade_and_leaves_no_orphan_row(monkeypatch):
    cfg = _seed_state("execution_confirmed_v1")
    monkeypatch.setattr("lego_state.CASHFLOW_SEMANTICS", "gated_theoretical_v2")
    anchor = Anchor(version=2, dna_step=1, p0=GENESIS_PRICE,
                    prev_price=FILLED_PRICE, prev_actual=CUMULATIVE_AFTER_FILL,
                    prev_holdings=PASS_HOLDINGS)
    snapshot = _snapshot(PASS_PRICE, PASS_HOLDINGS)
    row = compute_row(cfg, snapshot, anchor, dna_step=2)

    with pytest.raises(CashflowSemanticsDowngrade):
        commit_final_row(cfg, snapshot, anchor, row)

    assert FAKE_DB.reference("webull_lego_rows").get() in (None, {})
    state = FAKE_DB.reference(f"{STATE_PATH}/{chain_key(cfg)}").get()
    assert state["version"] == 2                     # pointer untouched
    assert state[EXECUTION_STATE_KEY]["actual_cumulative"] == CUMULATIVE_AFTER_FILL


def test_finalizing_a_fill_refuses_a_downgrade(monkeypatch):
    cfg = _seed_state("execution_confirmed_v1")
    FAKE_DB.reference("webull_lego_rows/r1").set({
        REFERENCE_COLUMN: 10.13, DELTA_COLUMN: 0.0, ACTUAL_COLUMN: 12.88,
        EXCESS_COLUMN: 0.03, "committed": True, "chain_key": chain_key(cfg),
        "cashflow_status": "PENDING_EXECUTION"})
    monkeypatch.setattr("lego_state.CASHFLOW_SEMANTICS", "gated_theoretical_v2")

    with pytest.raises(CashflowSemanticsDowngrade):
        finalize_execution_fill(cfg, "r1", ExecutionFill(
            filled_price=PASS_PRICE, filled_quantity=0.3,
            holdings_after=PASS_HOLDINGS))

    cashflow = FAKE_DB.reference(f"{STATE_PATH}/{chain_key(cfg)}").get()[EXECUTION_STATE_KEY]
    assert cashflow["actual_cumulative"] == CUMULATIVE_AFTER_FILL
    assert cashflow["last_action_price"] == FILLED_PRICE


def test_a_forward_migration_is_announced_not_silent():
    cfg = _seed_state("gated_theoretical_v2")
    # read_anchor restarts Aₙ at zero across the boundary; that is the designed
    # behaviour, and the report is what keeps it from looking like corruption.
    anchor = Anchor(version=2, dna_step=1, p0=GENESIS_PRICE,
                    prev_price=FILLED_PRICE, prev_actual=0.0,
                    prev_holdings=PASS_HOLDINGS)
    snapshot = _snapshot(PASS_PRICE, PASS_HOLDINGS)
    row = compute_row(cfg, snapshot, anchor, dna_step=2)
    result = commit_final_row(cfg, snapshot, anchor, row)

    assert result["committed"] is True
    assert result["cashflow_semantics_migrated_from"] == "gated_theoretical_v2"
    state = FAKE_DB.reference(f"{STATE_PATH}/{chain_key(cfg)}").get()
    assert state["cashflow_semantics"] == CASHFLOW_SEMANTICS


def test_the_running_semantics_is_reported_on_every_committed_row(monkeypatch):
    """Runtime vs repository, answerable from the response Scheduler already calls."""
    for key, value in {
        "LEGO_SYMBOL": "AAPL", "LEGO_FIX_C": str(FIX_C), "LEGO_DIFF": str(DIFF),
        "LEGO_DNA_CODE": "bypass:100", "LEGO_DECIMAL_PRECISION": "5",
        "LEGO_SLOT_SECONDS": "1800", "LEGO_DNA_ORIGIN_UTC": "2026-07-23T18:00:00Z",
        "LEGO_DNA_CLOCK_MODE": "market", "FIREBASE_DB_URL": "https://x.firebaseio.com",
        "AUTO_SUBMIT": "false", "WEBULL_ENV": "UAT",
    }.items():
        monkeypatch.setenv(key, value)

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return SLOT.astimezone(tz) if tz else SLOT

    monkeypatch.setattr(main, "datetime", _Now)
    monkeypatch.setattr(main, "build_clients", lambda: (object(), object()))
    monkeypatch.setattr(main, "token_health", lambda: {"ok": True, "reasons": []})
    monkeypatch.setattr(main, "fetch_snapshot", lambda t, d, cfg: _snapshot(
        PASS_PRICE, PASS_HOLDINGS))

    body, code = main.lego_one_row(object())
    assert code == 200
    assert body["cashflow_semantics"] == "execution_confirmed_v1"
    assert "cashflow_semantics_migrated_from" not in body
