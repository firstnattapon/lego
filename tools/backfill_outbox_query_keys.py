"""One-time migration for outbox records created before bounded query keys.

Dry-run by default. Run with ``--apply`` under an authorized admin identity
before deploying the bounded-query worker over an existing RTDB dataset.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, db

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lego_outbox import OUTBOX_PATH, TERMINAL, normalize_status


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    database_url = os.environ.get("FIREBASE_DB_URL", "").strip()
    if not database_url.startswith("https://"):
        raise SystemExit("FIREBASE_DB_URL HTTPS is required")
    if not firebase_admin._apps:
        firebase_admin.initialize_app(
            credentials.ApplicationDefault(), {"databaseURL": database_url})

    root = db.reference(OUTBOX_PATH)
    chains = root.get() or {}
    scanned = changed = 0
    for chain_key, intents in chains.items():
        if not isinstance(intents, dict):
            continue
        updates = {}
        for run_id, payload in intents.items():
            if not isinstance(payload, dict):
                continue
            scanned += 1
            status = normalize_status(payload.get("status"))
            expected = (None if status in TERMINAL else str(
                payload.get("slot_start_utc")
                or payload.get("created_at") or run_id))
            if payload.get("actionable_sort") != expected:
                updates[f"{run_id}/actionable_sort"] = expected
                changed += 1
        if args.apply and updates:
            db.reference(f"{OUTBOX_PATH}/{chain_key}").update(updates)
    print(json.dumps({
        "mode": "APPLY" if args.apply else "DRY_RUN",
        "chains": len(chains), "records_scanned": scanned,
        "records_changed": changed,
    }, sort_keys=True))


if __name__ == "__main__":
    main()

