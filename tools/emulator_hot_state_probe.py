"""Measure production persistence functions against a real RTDB emulator."""
from __future__ import annotations

import json
import os
import sys
import uuid

import firebase_admin
from firebase_admin import db

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def encoded_bytes(value) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def main() -> None:
    if not os.environ.get("FIREBASE_DATABASE_EMULATOR_HOST"):
        raise SystemExit("FIREBASE_DATABASE_EMULATOR_HOST is required")
    project = os.environ.get("GCLOUD_PROJECT", "demo-lego-hot-state")
    firebase_admin.initialize_app(options={
        "databaseURL": f"https://{project}-default-rtdb.firebaseio.com",
        "projectId": project,
    })
    from lego_outbox import OUTBOX_PATH, list_actionable, list_audit_pending
    from lego_state import (REALIZED_EVENT_ARCHIVE_PATH,
                            REALIZED_HOT_EVENT_LIMIT, REALIZED_PATH,
                            apply_realized_fill)

    chain = f"AAPL_perf_{uuid.uuid4().hex[:10]}"
    docs = {}
    for index in range(1000):
        run_id = f"done-{index:04d}"
        docs[run_id] = {
            "run_id": run_id, "chain_key": chain, "status": "FILLED",
            "created_at": f"2026-01-{1 + index % 28:02d}T00:00:00Z",
            "updated_at": "2026-02-01T00:00:00Z",
            "filled_quantity": 1, "filled_price": 100,
            "broker_cashflow_recorded": True, "broker_fee_status": "KNOWN",
            "cashflow_finalized": True, "realized": True,
            "audit_pending": False,
        }
    for index in range(7):
        run_id = f"live-{index:02d}"
        docs[run_id] = {
            "run_id": run_id, "chain_key": chain,
            "status": "PENDING_DISPATCH",
            "created_at": f"2026-09-06T00:0{index}:00Z",
            "actionable_sort": f"2026-09-06T00:0{index}:00Z",
            "audit_pending": index < 2,
        }
    root = db.reference(f"{OUTBOX_PATH}/{chain}")
    try:
        root.set(docs)
        full = root.get()
        actionable = list_actionable(chain, limit=3)
        audit = list_audit_pending(chain, limit=1)

        for index in range(REALIZED_HOT_EVENT_LIMIT + 16):
            apply_realized_fill(
                chain, f"event-{index:04d}", "BUY", 1, 100, 0)
        realized_head = db.reference(f"{REALIZED_PATH}/{chain}").get()
        archived = db.reference(
            f"{REALIZED_EVENT_ARCHIVE_PATH}/{chain}").get() or {}
        seq_before = int(realized_head["applied_seq"])
        replay = apply_realized_fill(chain, "event-0000", "BUY", 1, 100, 0)
        seq_after = int(db.reference(
            f"{REALIZED_PATH}/{chain}/applied_seq").get())

        result = {
            "status": "PASS",
            "real_rtdb_emulator": True,
            "outbox_records": len(full),
            "outbox_full_bytes": encoded_bytes(full),
            "actionable_limit": 3,
            "actionable_records_returned": len(actionable),
            "actionable_bytes": encoded_bytes(actionable),
            "audit_limit": 1,
            "audit_records_returned": len(audit),
            "realized_hot_events": len(realized_head["applied_fills"]),
            "realized_hot_limit": REALIZED_HOT_EVENT_LIMIT,
            "realized_hot_bytes": encoded_bytes(realized_head),
            "realized_archived_events": len(archived),
            "old_replay_delta": replay["realized_delta"],
            "old_replay_seq_unchanged": seq_before == seq_after,
        }
        assert len(actionable) == 3 and len(audit) == 1
        assert len(realized_head["applied_fills"]) == REALIZED_HOT_EVENT_LIMIT
        assert len(archived) == 16
        assert replay["realized_delta"] == 0 and seq_before == seq_after
        print(json.dumps(result, sort_keys=True))
    finally:
        root.delete()
        db.reference(f"{REALIZED_PATH}/{chain}").delete()
        db.reference(f"{REALIZED_EVENT_ARCHIVE_PATH}/{chain}").delete()


if __name__ == "__main__":
    main()
