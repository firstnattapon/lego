"""Prove rejection receipt idempotency with real concurrent RTDB transactions."""
import json
import os
from pathlib import Path
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor

import firebase_admin
from firebase_admin import db

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    host = os.environ.get("FIREBASE_DATABASE_EMULATOR_HOST", "")
    if not host.startswith(("127.0.0.1:", "localhost:")):
        raise SystemExit("local RTDB emulator required")
    project = os.environ.get("GCLOUD_PROJECT", "demo-lego-firebase")
    firebase_admin.initialize_app(options={
        "databaseURL": f"https://{project}-default-rtdb.firebaseio.com", "projectId": project})
    import broker_circuit as circuit
    from lego_outbox import (DISPATCH_LOCK_PATH, account_symbol_fence_key,
                             claim_chain_dispatch, fence_chain_dispatch)
    identity = "emulator-reject-" + uuid.uuid4().hex
    scope = account_symbol_fence_key(identity, "TSLA")
    ref = db.reference(f"{DISPATCH_LOCK_PATH}/{scope}")
    try:
        for n in range(3):
            run = str(n)
            ref.update({"inflight_run_id": run})
            intent = {"runtime_identity_fingerprint": identity, "symbol": "TSLA", "run_id": run}
            with ThreadPoolExecutor(max_workers=16) as pool:
                list(pool.map(lambda _: circuit.record_outcome(
                    intent, {"status": "FAILED", "filled_quantity": "0"}), range(16)))
            assert circuit.status(identity, "TSLA")["consecutive_broker_rejects"] == n + 1
        ref.update({"inflight_run_id": None})
        claim = claim_chain_dispatch(scope, "worker")
        assert fence_chain_dispatch(scope, "fourth", "worker", claim["claim_token"]) is None
        assert circuit.status(identity, "TSLA")["halted"]
        print(json.dumps({"status": "PASS", "real_rtdb_emulator": True,
                          "concurrency": 16, "distinct_rejects": 3,
                          "replayed_callbacks": 48, "fourth_order_fenced": True}))
    finally:
        ref.delete()


if __name__ == "__main__":
    main()
