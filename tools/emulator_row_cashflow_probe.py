"""Prove a fill/decision race and crashed row patch on the real RTDB emulator.

Only the simulated fill facts are synthetic. State and row transactions use
production code; this probe never calls Webull or a production database.
"""
from __future__ import annotations

import json
import os
import sys
import uuid

import firebase_admin
from firebase_admin import db

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> None:
    if not os.environ.get("FIREBASE_DATABASE_EMULATOR_HOST"):
        raise SystemExit("FIREBASE_DATABASE_EMULATOR_HOST is required")
    project = os.environ.get("GCLOUD_PROJECT", "demo-lego-row-cashflow")
    firebase_admin.initialize_app(options={
        "databaseURL": f"https://{project}-default-rtdb.firebaseio.com",
        "projectId": project,
    })

    import lego_state
    from lego_one_row import (ACTUAL_COLUMN, DELTA_COLUMN, EXCESS_COLUMN,
                              REFERENCE_COLUMN, Config, ExecutionFill, compute_row)
    from lego_state import ROWS_PATH, STATE_PATH, chain_key, commit_final_row

    cfg = Config(symbol=f"RC{uuid.uuid4().hex[:8].upper()}", fix_c=3000.0,
                 diff=5.0, dna_code="bypass:100",
                 strategy_id="shannon_demon_lego_v2", decimal_precision=2)
    ck = chain_key(cfg)
    run_ids: list[str] = []

    def commit(at: str, price: float, holdings: float, anchor):
        snapshot = {"captured_at": at, "price": price, "holdings": holdings}
        row = compute_row(cfg, snapshot, anchor)
        result = commit_final_row(cfg, snapshot, anchor, row)
        run_ids.append(result["run_id"])
        return result, snapshot, row

    try:
        commit("2026-09-04T14:00:05Z", 320.0, 9.375, None)
        prior, _snapshot, _row = commit(
            "2026-09-04T14:30:05Z", 330.0, 9.375,
            lego_state.read_anchor(cfg))
        stale_anchor = lego_state.read_anchor(cfg)
        lego_state.finalize_execution_fill(
            cfg, prior["run_id"],
            ExecutionFill(filled_price=331.25, filled_quantity=1.0,
                          holdings_after=8.375))
        cashflow = db.reference(
            f"{STATE_PATH}/{ck}/{lego_state.EXECUTION_STATE_KEY}").get()
        raced, _snapshot, _row = commit(
            "2026-09-04T15:00:05Z", 332.0, 8.375, stale_anchor)
        raced_row = db.reference(f"{ROWS_PATH}/{raced['run_id']}").get()
        assert raced_row[DELTA_COLUMN] == 0.0
        assert raced_row[ACTUAL_COLUMN] == cashflow["actual_cumulative"]
        assert raced_row[EXCESS_COLUMN] == cashflow["excess"]
        assert abs(raced_row["E_mark_at_observation"] -
                   (cashflow["actual_cumulative"] -
                    raced_row[REFERENCE_COLUMN])) < 1e-8

        anchor = lego_state.read_anchor(cfg)
        snapshot = {"captured_at": "2026-09-04T15:30:05Z", "price": 333.0,
                    "holdings": 8.375}
        row = compute_row(cfg, snapshot, anchor)
        original_repair = lego_state._repair_pending_row

        def crash_after_state(state):
            if state and state.get("version") == 4:
                raise RuntimeError("simulated crash before row patch")
            return original_repair(state)

        lego_state._repair_pending_row = crash_after_state
        try:
            try:
                commit_final_row(cfg, snapshot, anchor, row)
            except RuntimeError as exc:
                assert str(exc) == "simulated crash before row patch"
            else:
                raise AssertionError("expected simulated crash")
        finally:
            lego_state._repair_pending_row = original_repair
        state = db.reference(f"{STATE_PATH}/{ck}").get()
        crashed_run = state["last_run_id"]
        run_ids.append(crashed_run)
        assert db.reference(f"{ROWS_PATH}/{crashed_run}").get()["committed"] is False
        replay = commit_final_row(cfg, snapshot, anchor, row)
        assert replay["idempotent"] is True
        repaired = db.reference(f"{ROWS_PATH}/{crashed_run}").get()
        assert repaired["committed"] is True
        assert repaired[ACTUAL_COLUMN] == state["last_row_cashflow_observation"][
            "fields"][ACTUAL_COLUMN]
        print(json.dumps({"status": "PASS", "real_rtdb_emulator": True,
                          "fill_decision_race": True,
                          "crash_replay": True,
                          "webull_calls": 0}, sort_keys=True))
    finally:
        for run_id in set(run_ids):
            db.reference(f"{ROWS_PATH}/{run_id}").delete()
        db.reference(f"{STATE_PATH}/{ck}").delete()


if __name__ == "__main__":
    main()
