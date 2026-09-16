"""Race first-fill finalization against the real local RTDB transaction engine."""
import json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor

import firebase_admin
from firebase_admin import db

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    host = os.environ.get("FIREBASE_DATABASE_EMULATOR_HOST", "")
    if not host.startswith(("127.0.0.1:", "localhost:")):
        raise SystemExit("local RTDB emulator required")
    project = os.environ.get("GCLOUD_PROJECT", "demo-lego-funding")
    if not project.startswith("demo-"):
        raise SystemExit("demo project required")
    firebase_admin.initialize_app(options={
        "databaseURL": f"https://{project}-default-rtdb.firebaseio.com", "projectId": project})
    from lego_one_row import ACTUAL_COLUMN, DELTA_COLUMN, EXCESS_COLUMN, Config, ExecutionFill, compute_row
    from lego_state import ROWS_PATH, STATE_PATH, chain_key, commit_final_row, finalize_execution_fill
    cfg = Config("RACE" + uuid.uuid4().hex[:8], 5000, 25, "bypass:500",
                 strategy_id="shannon_demon_lego_v2", decimal_precision=0)
    snapshot = {"captured_at": "2026-09-15T14:16:09Z", "price": 27.34, "holdings": 0}
    row = compute_row(cfg, snapshot, None, dna_step=133)
    run = commit_final_row(cfg, snapshot, None, row)["run_id"]
    try:
        fill = ExecutionFill(27.37, 182, 182)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: finalize_execution_fill(cfg, run, fill), range(8)))
        assert sum(result["applied"] for result in results) == 1
        state = db.reference(f"{STATE_PATH}/{chain_key(cfg)}").get()
        stored = db.reference(f"{ROWS_PATH}/{run}").get()
        assert state["execution_cashflow"]["finalized_seq"] == 1
        assert state["prev_price"] == 27.37
        assert [stored[col] for col in (DELTA_COLUMN, ACTUAL_COLUMN, EXCESS_COLUMN)] == [0, 0, 0]
        assert stored["initial_funding"] is True
        print(json.dumps({"status": "PASS", "workers": 8, "applied": 1,
                          "real_rtdb_emulator": True, "funding_ledger": [0, 0, 0]}))
    finally:
        db.reference(f"{ROWS_PATH}/{run}").delete()
        db.reference(f"{STATE_PATH}/{chain_key(cfg)}").delete()


if __name__ == "__main__":
    main()
