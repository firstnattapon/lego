"""Initial funding is a model baseline, not a return on uninvested cash."""
import math
from decimal import Decimal

import pytest

from conftest import FAKE_DB
from ledger_v2 import FUNDING_BASELINE_POLICY, FrozenLedger
from lego_one_row import (ACTUAL_COLUMN, DELTA_COLUMN, EXCESS_COLUMN, Config,
                          ExecutionFill, compute_row)
from lego_state import (EXECUTION_STATE_KEY, ROWS_PATH, STATE_PATH,
                        ExecutionFinalizeError, chain_key, commit_final_row,
                        finalize_execution_fill, read_anchor, verify_cashflow_semantics,
                        CashflowSemanticsDowngrade, LEGACY_V2_CASHFLOW_SEMANTICS)


@pytest.fixture(autouse=True)
def clean():
    FAKE_DB.store.clear()
    yield
    FAKE_DB.store.clear()


def config():
    return Config("XYZ", 5000, 25, "bypass:500",
                  strategy_id="shannon_demon_lego_v2", decimal_precision=0)


def commit(cfg, price, holdings, step):
    snapshot = {"captured_at": f"2026-09-15T14:{step - 130:02d}:00Z",
                "price": price, "holdings": holdings}
    anchor = read_anchor(cfg)
    row = compute_row(cfg, snapshot, anchor, dna_step=step)
    return commit_final_row(cfg, snapshot, anchor, row)["run_id"]


def row(run):
    return FAKE_DB.reference(f"{ROWS_PATH}/{run}").get()


def state_ref(cfg):
    return FAKE_DB.reference(f"{STATE_PATH}/{chain_key(cfg)}")


def test_first_flat_buy_at_nonzero_dna_step_is_zero_then_uses_fill_basis():
    cfg = config()
    run = commit(cfg, 27.34, 0, 133)
    fill = ExecutionFill(27.37, 182, 182)
    first = finalize_execution_fill(cfg, run, fill)
    assert first["initial_funding"] is True
    assert [row(run)[c] for c in (DELTA_COLUMN, ACTUAL_COLUMN, EXCESS_COLUMN)] == [0, 0, 0]
    assert read_anchor(cfg).prev_price == 27.37
    assert finalize_execution_fill(cfg, run, fill)["applied"] is False
    passed = commit(cfg, 27.4, 182, 134)
    assert [row(passed)[c] for c in (DELTA_COLUMN, ACTUAL_COLUMN, EXCESS_COLUMN)] == [0, 0, 0]
    second = commit(cfg, 28, 182, 135)
    result = finalize_execution_fill(cfg, second, ExecutionFill(28.02, 3, 179))
    expected = 5000 * (28.02 / 27.37 - 1)
    assert result["delta_actual"] == pytest.approx(expected)
    assert result["excess"] == pytest.approx(expected - 5000 * math.log(28 / 27.34))
    assert result["seq"] == 2


def test_first_fill_after_unfilled_decisions_sets_reference_offset_once():
    cfg = config()
    commit(cfg, 27.34, 0, 133)  # no confirmed execution
    run = commit(cfg, 27.5, 0, 134)
    result = finalize_execution_fill(cfg, run, ExecutionFill(27.53, 181, 181))
    assert result["delta_actual"] == result["actual_cumulative"] == result["excess"] == 0
    assert result["funding_reference_offset"] == pytest.approx(5000 * math.log(27.5 / 27.34))
    next_run = commit(cfg, 28, 181, 135)
    second = finalize_execution_fill(cfg, next_run, ExecutionFill(28, 2, 179))
    assert second["reference"] == pytest.approx(5000 * math.log(28 / 27.5))


def test_existing_position_is_not_mistaken_for_initial_funding():
    cfg = config()
    run = commit(cfg, 27.34, 190, 133)
    result = finalize_execution_fill(cfg, run, ExecutionFill(27.37, 7, 183))
    assert result["delta_actual"] == pytest.approx(5.48646671543529)
    assert "initial_funding" not in result


def test_legacy_chain_is_not_silently_reset():
    cfg = config()
    run = commit(cfg, 27.34, 0, 133)
    state = state_ref(cfg).get()
    for key in ("model_baseline_policy", "funding_baseline_pending", "funding_reference_offset"):
        state[EXECUTION_STATE_KEY].pop(key)
    state_ref(cfg).set(state)
    result = finalize_execution_fill(cfg, run, ExecutionFill(27.37, 182, 182))
    assert result["actual_cumulative"] == pytest.approx(5.48646671543529)


def test_row_retry_after_bounded_history_eviction_preserves_basis_and_seq():
    cfg = config()
    commit(cfg, 27.34, 0, 133)
    run = commit(cfg, 27.5, 0, 134)
    fill = ExecutionFill(27.53, 181, 181)
    finalize_execution_fill(cfg, run, fill)
    before = row(run)
    state = state_ref(cfg).get()
    state[EXECUTION_STATE_KEY]["finalized_runs"] = {}
    state_ref(cfg).set(state)
    assert finalize_execution_fill(cfg, run, fill)["applied"] is False
    assert row(run) == before
    assert state_ref(cfg).get() == state


def test_crash_after_state_commit_repairs_row_without_rebooking(monkeypatch):
    from conftest import FakeReference
    cfg = config()
    run = commit(cfg, 27.34, 0, 133)
    original = FakeReference.update
    def crash(self, fields):
        if "cashflow_finalized_at" in fields:
            raise OSError("simulated row write failure")
        return original(self, fields)
    monkeypatch.setattr(FakeReference, "update", crash)
    fill = ExecutionFill(27.37, 182, 182)
    with pytest.raises(OSError):
        finalize_execution_fill(cfg, run, fill)
    monkeypatch.setattr(FakeReference, "update", original)
    assert finalize_execution_fill(cfg, run, fill)["applied"] is False
    assert row(run)[ACTUAL_COLUMN] == 0
    assert row(run)["model_baseline_policy"] == FUNDING_BASELINE_POLICY
    assert state_ref(cfg).get()[EXECUTION_STATE_KEY]["finalized_seq"] == 1


def test_inconsistent_funding_marker_fails_closed():
    cfg = config()
    commit(cfg, 27.34, 0, 133)
    run = commit(cfg, 28, 190, 134)  # unexplained external holdings
    with pytest.raises(ExecutionFinalizeError, match="funding evidence"):
        finalize_execution_fill(cfg, run, ExecutionFill(28, 10, 180))


def test_decimal_reference_model_agrees_on_funding_initialization():
    ledger, event = FrozenLedger.genesis("27.34").finalize_terminal_fill(
        principal=5000, fill_price="27.37", decision_r_basis=0, initial_funding=True)
    assert event["delta_A"] == ledger.A == ledger.E == Decimal(0)
    assert ledger.p_acted == Decimal("27.37")
    with pytest.raises(ValueError):
        ledger.finalize_terminal_fill(principal=5000, fill_price=28,
                                     decision_r_basis=0, initial_funding=True)


def test_old_accounting_cannot_write_after_v3_chain_upgrade():
    cfg = config()
    commit(cfg, 27.34, 0, 133)
    with pytest.raises(CashflowSemanticsDowngrade):
        verify_cashflow_semantics(state_ref(cfg).get(), LEGACY_V2_CASHFLOW_SEMANTICS)
