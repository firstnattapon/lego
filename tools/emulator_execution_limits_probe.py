"""Local emulator proof: concurrent retries consume one slot, not many."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    host = os.environ.get("FIREBASE_DATABASE_EMULATOR_HOST", "")
    if not host.startswith(("127.0.0.1:", "localhost:")):
        raise SystemExit("local emulator required")
    import firebase_admin
    from firebase_admin import db
    from execution_limits import ExecutionLimits, ExecutionLimitError, reserve_attempt
    from lego_outbox import (DISPATCH_LOCK_PATH, claim_chain_dispatch,
                             fence_chain_dispatch, clear_chain_dispatch_inflight)
    project = os.environ.get("GCLOUD_PROJECT", "demo-lego-firebase")
    firebase_admin.initialize_app(options={"projectId": project,
        "databaseURL": f"https://{project}-default-rtdb.firebaseio.com"})
    scope = "limit-probe-" + uuid.uuid4().hex
    ref = db.reference(f"{DISPATCH_LOCK_PATH}/{scope}")
    limits = ExecutionLimits.parse(("10", "1000", "1",
        (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()))
    key = str(limits.end.timestamp())
    try:
        claim = claim_chain_dispatch(scope, "worker")
        assert fence_chain_dispatch(scope, "first", "worker", claim["claim_token"])
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _: reserve_attempt(scope, claim, "first", limits, key), range(16)))
        assert all(r["reservation_count"] == 1 for r in results)
        assert clear_chain_dispatch_inflight(scope, "first", "worker", claim["claim_token"])
        assert fence_chain_dispatch(scope, "second", "worker", claim["claim_token"])
        try:
            reserve_attempt(scope, claim, "second", limits, key)
        except ExecutionLimitError:
            pass
        else:
            raise AssertionError("second order exceeded session budget")
        assert ref.get()["execution_session"]["count"] == 1
        print(json.dumps({"status": "PASS", "real_rtdb_emulator": True,
                          "concurrent_retries": 16, "reservations": 1, "second_order_blocked": True}))
    finally:
        ref.delete()


if __name__ == "__main__":
    main()
