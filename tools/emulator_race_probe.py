"""Concurrent RTDB emulator proof for the irreversible order path.

Run only through ``firebase emulators:exec``. This process intentionally avoids
pytest/conftest so it imports the real firebase-admin SDK.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import firebase_admin
from firebase_admin import db

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def parallel(fn, values):
    with ThreadPoolExecutor(max_workers=len(values)) as pool:
        return list(pool.map(fn, values))


def main() -> None:
    if not os.environ.get("FIREBASE_DATABASE_EMULATOR_HOST"):
        raise SystemExit("FIREBASE_DATABASE_EMULATOR_HOST is required")
    project = os.environ.get("GCLOUD_PROJECT", "demo-lego-race")
    firebase_admin.initialize_app(options={
        "databaseURL": f"https://{project}-default-rtdb.firebaseio.com",
        "projectId": project,
    })

    from lego_outbox import (DISPATCH_LOCK_PATH, OUTBOX_PATH,
                             account_symbol_fence_key, begin_place_attempt,
                             claim_chain_dispatch, claim_intent,
                             fence_chain_dispatch, put_intent,
                             release_chain_dispatch)

    nonce = uuid.uuid4().hex[:12]
    chain_old = f"AAPL_old_{nonce}"
    chain_new = f"AAPL_new_{nonce}"
    run_id = uuid.uuid4().hex
    workers = [f"worker-{i}" for i in range(16)]
    now = datetime.now(timezone.utc)
    scope = account_symbol_fence_key(f"emulator-account-{nonce}", "AAPL")

    try:
        created = parallel(
            lambda i: put_intent(chain_old, run_id, {
                "status": "PENDING_DISPATCH",
                "created_at": "2026-09-06T00:00:00Z",
                "writer": i,
            }), range(16))
        stored = db.reference(f"{OUTBOX_PATH}/{chain_old}/{run_id}").get()
        assert stored and all(doc["writer"] == stored["writer"] for doc in created)

        claims = parallel(
            lambda worker: claim_intent(
                chain_old, run_id, worker, now_utc=now, lease_seconds=60),
            workers)
        winners = [claim for claim in claims if claim is not None]
        assert len(winners) == 1, f"intent claim winners={len(winners)}"
        winner = winners[0]

        # Same valid owner/generation, deliberately raced. Only the invocation
        # whose unique transaction token lands may cross place_order.
        place_starts = parallel(
            lambda _: begin_place_attempt(
                chain_old, run_id, str(winner["claim_owner"]),
                int(winner["claim_generation"])),
            range(16))
        assert sum(item is not None for item in place_starts) == 1

        dispatch_claims = parallel(
            lambda worker: claim_chain_dispatch(
                scope, worker, now_utc=now, lease_seconds=60), workers)
        dispatch_winners = [claim for claim in dispatch_claims if claim is not None]
        assert len(dispatch_winners) == 1, (
            f"account-symbol dispatch winners={len(dispatch_winners)}")
        dispatch = dispatch_winners[0]
        fenced = fence_chain_dispatch(
            scope, run_id, str(dispatch["owner"]), str(dispatch["claim_token"]),
            intent_chain_key=chain_old, now_utc=now, lease_seconds=1)
        assert fenced and fenced["inflight_chain_key"] == chain_old
        release_chain_dispatch(scope, str(dispatch["owner"]),
                               str(dispatch["claim_token"]))

        successor = claim_chain_dispatch(
            scope, "new-config-worker",
            now_utc=now + timedelta(seconds=2), lease_seconds=60)
        assert successor is not None
        assert successor["inflight_run_id"] == run_id
        assert successor["inflight_chain_key"] == chain_old
        # A different config chain cannot overwrite the old ambiguity.
        assert fence_chain_dispatch(
            scope, uuid.uuid4().hex, "new-config-worker",
            str(successor["claim_token"]), intent_chain_key=chain_new,
            now_utc=now + timedelta(seconds=2), lease_seconds=60) is None

        print(json.dumps({
            "status": "PASS",
            "real_rtdb_emulator": True,
            "concurrency": len(workers),
            "intent_create_winners": 1,
            "intent_claim_winners": len(winners),
            "place_fence_winners": sum(item is not None for item in place_starts),
            "dispatch_claim_winners": len(dispatch_winners),
            "cross_config_inflight_chain": successor["inflight_chain_key"],
        }, sort_keys=True))
    finally:
        db.reference(f"{OUTBOX_PATH}/{chain_old}").delete()
        db.reference(f"{OUTBOX_PATH}/{chain_new}").delete()
        db.reference(f"{DISPATCH_LOCK_PATH}/{scope}").delete()


if __name__ == "__main__":
    main()

