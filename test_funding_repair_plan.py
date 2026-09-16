import copy

import pytest

from tools.plan_initial_funding_repair import (A, DA, E, P, R, apply_to_copy,
                                              plan_repair)


def snapshot():
    offset = 5000 * (27.37 / 27.34 - 1)
    event = {"actual_cumulative": offset, "excess": offset, "delta_actual": offset,
             "previous_actual_cumulative": 0, "reference": 0,
             "previous_action_price": 27.34, "filled_price": 27.37,
             "filled_quantity": 182}
    audit = {**event, "status": "FILLED", "side": "BUY", "run_id": "run",
             "place_attempted": True}
    row = {"chain_key": "chain", "version": 1, "committed": True,
           "semantics": "execution_terminal_frozen_v2", "cashflow_status": "FINALIZED",
           "finalized_seq": 1, "ฝั่ง": "BUY", "จำนวนถือครอง (หุ้น)": 0,
           "execution_price": 27.37, "execution_quantity": 182, R: 0, P: 27.34,
           DA: offset, A: offset, E: offset,
           "มูลค่าพอร์ต (USD)": 0, "ส่วนต่างเป้าหมาย (USD)": 5000}
    return {
        "webull_lego_rows": {"run": row},
        "webull_lego_state": {"chain": {
            "version": 1, "last_run_id": "run", "prev_actual": offset,
            "prev_excess": offset, "prev_price": 27.37,
            "cashflow_semantics": "execution_terminal_frozen_v2",
            "execution_cashflow": {"finalized_seq": 1, "finalized_runs": {"run": event},
                                   "actual_cumulative": offset, "excess": offset,
                                   "last_action_price": 27.37}}},
        "webull_lego_order_outbox": {"chain": {"run": audit}},
        "webull_lego_order_audit": {"run": copy.deepcopy(audit)},
        "webull_lego_broker_cashflow": {"chain": {"events": {"run": {
            "side": "BUY", "cumulative_quantity": "182", "cumulative_notional": "4981.34"}}}},
        "webull_lego_realized": {"chain": {"realized": 0, "lots": [[182, 27.37]]}},
    }


def test_repair_plan_only_changes_model_and_cannot_overwrite_changed_source():
    source = snapshot()
    before = copy.deepcopy(source)
    plan = plan_repair(source, "chain")
    fixed = apply_to_copy(source, plan)
    assert source == before
    assert [fixed["webull_lego_rows"]["run"][c] for c in (DA, A, E)] == [0, 0, 0]
    for table in ("webull_lego_broker_cashflow", "webull_lego_realized"):
        assert fixed[table] == before[table]
    source["webull_lego_state"]["chain"]["version"] = 2
    with pytest.raises(ValueError, match="stale"):
        apply_to_copy(source, plan)


@pytest.mark.parametrize("mutation", [
    lambda d: d["webull_lego_state"]["chain"]["execution_cashflow"].update(finalized_seq=2),
    lambda d: d["webull_lego_rows"]["run"].update(version=2),
    lambda d: d["webull_lego_rows"]["run"].update({"จำนวนถือครอง (หุ้น)": 10}),
    lambda d: d["webull_lego_order_outbox"]["chain"]["run"].update(status="SUBMITTED"),
    lambda d: d["webull_lego_rows"]["run"].update({A: 100}),
    lambda d: d["webull_lego_broker_cashflow"]["chain"]["events"]["run"].update(cumulative_quantity=180),
    lambda d: d.update(webull_lego_order_dispatch_locks={"lock": {"claim_token": "active"}}),
])
def test_repair_refuses_incomplete_or_active_or_inconsistent_snapshots(mutation):
    source = snapshot()
    mutation(source)
    with pytest.raises(ValueError):
        plan_repair(source, "chain")


def test_unrebased_chain_can_be_repaired_after_writer_semantics_upgrade():
    source = snapshot()
    source["webull_lego_state"]["chain"]["cashflow_semantics"] = "execution_terminal_funding_v3"
    source["webull_lego_rows"]["run"]["semantics"] = "execution_terminal_funding_v3"
    assert apply_to_copy(source, plan_repair(source, "chain"))["webull_lego_rows"]["run"][A] == 0
