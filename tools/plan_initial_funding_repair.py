"""Offline, fail-closed repair plan for a complete one-fill funding incident.

Never connects to Firebase or Webull. Produces expected-before values and leaf
updates for review. A live executor must stop writers, export afresh, re-plan,
verify the whole source hash and apply the leaf update atomically. Do NOT import
the snapshot over a live database. Multiple fills require a separate replay.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from pathlib import Path


POLICY = "initial_funding_zero_v1"
SEMANTICS = "execution_terminal_funding_v3"
SUPPORTED_SEMANTICS = {"execution_terminal_frozen_v2", SEMANTICS}
DA = "ΔAₙ ต่อสเต็ป (USD)"
A = "Aₙ สะสม (USD)"
E = "Eₙ ส่วนเกินสะสม (USD)"
R = "Rₙ อ้างอิง (USD)"
P = "ราคา Pₙ (USD)"


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def require(ok, message):
    if not ok:
        raise ValueError(message)


def close(value, expected):
    try:
        return math.isfinite(float(value)) and math.isclose(
            float(value), expected, rel_tol=1e-10, abs_tol=1e-8)
    except (TypeError, ValueError):
        return False


def plan_repair(source, chain):
    """Narrow incident repair: first committed row BUY from flat, one fill only."""
    source_hash = fingerprint(source)
    state = source["webull_lego_state"][chain]
    cash = state["execution_cashflow"]
    require(state.get("cashflow_semantics") in SUPPORTED_SEMANTICS,
            "only frozen-v2 or unrebased funding-v3 snapshots are supported")
    require(not cash.get("model_baseline_policy"), "baseline already migrated")
    require(cash.get("finalized_seq") == 1, "requires exactly one finalized fill")
    records = cash["finalized_runs"]
    require(len(records) == 1, "requires complete single-fill history")
    run, record = next(iter(records.items()))
    rows = sorted(((key, row) for key, row in source["webull_lego_rows"].items()
                   if row.get("chain_key") == chain), key=lambda pair: pair[1]["version"])
    require([row["version"] for _, row in rows] == list(range(1, state["version"] + 1)),
            "requires every committed row from version 1 through state.version")
    require(rows[0][0] == run and state.get("last_run_id") == rows[-1][0],
            "row/state pointers mismatch")
    first = rows[0][1]
    require(first.get("ฝั่ง") == "BUY" and first.get("จำนวนถือครอง (หุ้น)") == 0,
            "first fill must be BUY from flat")
    require(first.get("cashflow_status") == "FINALIZED" and first.get("finalized_seq") == 1,
            "missing terminal funding evidence")
    require(close(first[R], 0) and close(record.get("reference"), 0)
            and close(record.get("previous_actual_cumulative"), 0), "nonzero funding origin")
    price = float(first["execution_price"])
    qty = float(first["execution_quantity"])
    principal = float(first["มูลค่าพอร์ต (USD)"]) + float(first["ส่วนต่างเป้าหมาย (USD)"])
    require(price > 0 and qty > 0 and principal > 0, "invalid funding facts")
    correction = principal * (price / float(first[P]) - 1)
    require(close(record.get("previous_action_price"), float(first[P]))
            and close(record.get("filled_price"), price)
            and close(record.get("filled_quantity"), qty)
            and close(cash.get("last_action_price"), price), "fill basis mismatch")
    for node in (record, cash):
        require(close(node.get("actual_cumulative"), correction)
                and close(node.get("excess"), correction), "state ledger mismatch")
    require(close(record.get("delta_actual"), correction), "record delta mismatch")
    require(close(state.get("prev_actual"), correction)
            and close(state.get("prev_excess"), correction)
            and close(state.get("prev_price"), price), "flat state mirrors mismatch")
    require(not state.get("pending_order_intents"), "pending intent recovery exists")
    outbox = source["webull_lego_order_outbox"][chain]
    require(run in outbox, "missing funding outbox")
    for item in outbox.values():
        require(item.get("status") in {"FILLED", "SUPPRESSED_STATE_CHANGED", "EXPIRED_UNSENT",
                                       "REJECTED", "CANCELLED", "CANCELED"},
                "unresolved order; reconcile before repairing")
        require(item.get("run_id") == run or not item.get("place_attempted"),
                "another broker attempt requires a broader replay")
    for lock in source.get("webull_lego_order_dispatch_locks", {}).values():
        require(not lock.get("inflight_run_id") and not lock.get("claim_token"),
                "active dispatch lock; snapshot must be quiescent")
    for _, row in rows:
        require(row.get("committed") is True and row.get("semantics") in SUPPORTED_SEMANTICS,
                "uncommitted or unsupported-semantics row")
        require(close(row[A], correction) and close(row[E], correction), "row ledger mismatch")
        require(close(row[DA], correction if row is first else 0), "row delta mismatch")
        require(row is first or row.get("cashflow_status") != "FINALIZED", "multiple fills")
    # Broker truth must match before changing the model. Its cash, fee and FIFO
    # records are deliberately excluded from every update below.
    broker = source["webull_lego_broker_cashflow"][chain]["events"][run]
    require(broker.get("side") == "BUY" and close(broker.get("cumulative_quantity"), qty)
            and close(broker.get("cumulative_notional"), qty * price), "broker cashflow mismatch")
    updates, expected = {}, {}
    def put(path, value):
        node = source
        for part in path.split("/"):
            node = node.get(part) if isinstance(node, dict) else None
        if node != value:
            expected[path], updates[path] = node, value
    for key, row in rows:
        path = f"webull_lego_rows/{key}"
        for col in (DA, A, E):
            put(f"{path}/{col}", 0.0)
        for name, value in {"semantics": SEMANTICS, "ledger_version_at_observation": SEMANTICS,
                            "model_baseline_policy": POLICY, "funding_reference_offset": 0.0,
                            "R_basis": 0.0}.items():
            put(f"{path}/{name}", value)
        # Observation E_mark on the funding row predates the erroneous fill.
        put(f"{path}/E_mark_at_observation", -float(row[R]))
    for name, value in {"initial_funding": True, "previous_action_price": float(first[P]),
                        "previous_actual_cumulative": 0.0}.items():
        put(f"webull_lego_rows/{run}/{name}", value)
    root = f"webull_lego_state/{chain}"
    put(f"{root}/cashflow_semantics", SEMANTICS)
    for name in ("prev_actual", "prev_excess", "r_basis"):
        put(f"{root}/{name}", 0.0)
    for name, value in {"actual_cumulative": 0.0, "excess": 0.0, "r_basis": 0.0,
                        "model_baseline_policy": POLICY, "funding_baseline_pending": False,
                        "funding_reference_offset": 0.0}.items():
        put(f"{root}/execution_cashflow/{name}", value)
    for name, value in {"delta_actual": 0.0, "actual_cumulative": 0.0, "excess": 0.0,
                        "initial_funding": True, "model_baseline_policy": POLICY,
                        "funding_reference_offset": 0.0}.items():
        put(f"{root}/execution_cashflow/finalized_runs/{run}/{name}", value)
    for path in (f"webull_lego_order_audit/{run}", f"webull_lego_order_outbox/{chain}/{run}"):
        node = source
        for part in path.split("/"):
            node = node[part]
        require(node.get("status") == "FILLED" and node.get("side") == "BUY"
                and close(node.get("filled_price"), price) and close(node.get("filled_quantity"), qty),
                "audit/outbox fill mismatch")
        for name in ("delta_actual", "actual_cumulative", "excess"):
            require(close(node.get(name), correction), "audit/outbox model mismatch")
            put(f"{path}/{name}", 0.0)
    return {"format": "offline_initial_funding_repair_v1", "source_sha256": source_hash,
            "chain_key": chain, "run_id": run, "model_offset_removed": correction,
            "rows_checked": len(rows), "expected_values": expected, "updates": updates,
            "requires": "Pause all writers; fresh export; same whole-source hash; atomic leaf update. Never import a stale full snapshot."}


def apply_to_copy(source, plan):
    """Offline verification only. Reject even a one-field change since planning."""
    require(fingerprint(source) == plan["source_sha256"], "stale repair source")
    result = copy.deepcopy(source)
    for path, value in plan["updates"].items():
        parts = path.split("/")
        node = result
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        require(node.get(parts[-1]) == plan["expected_values"][path], "before-value mismatch")
        node[parts[-1]] = value
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path)
    parser.add_argument("chain")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    source = json.loads(args.export.read_text(encoding="utf-8-sig"))
    plan = plan_repair(source, args.chain)
    apply_to_copy(source, plan)
    args.output.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"OFFLINE ONLY: {plan['rows_checked']} rows; offset {plan['model_offset_removed']:.8f}; {len(plan['updates'])} leaf updates")


if __name__ == "__main__":
    main()
