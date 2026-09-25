"""Race operator halt against the dispatch fence on real RTDB transactions."""
from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import firebase_admin

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> None:
    if not os.environ.get("FIREBASE_DATABASE_EMULATOR_HOST"):
        raise SystemExit("FIREBASE_DATABASE_EMULATOR_HOST is required")
    project = os.environ.get("GCLOUD_PROJECT", "demo-lego-firebase")
    firebase_admin.initialize_app(options={
        "databaseURL": f"https://{project}-default-rtdb.firebaseio.com",
        "projectId": project,
    })
    from lego_outbox import (account_symbol_fence_key, claim_chain_dispatch,
                             clear_chain_dispatch_inflight,
                             fence_chain_dispatch, release_chain_dispatch)
    import operator_halt

    for index in range(8):
        identity = f"emulator-halt-{uuid.uuid4().hex}"
        scope = account_symbol_fence_key(identity, "TSLA")
        worker, run = "worker", f"run-{index}"
        claim = claim_chain_dispatch(scope, worker)
        assert claim is not None
        barrier = threading.Barrier(2)

        def halt():
            barrier.wait()
            return operator_halt.set_halt(
                identity, "TSLA", operator="alice", reason="emulator race",
                apply=True)

        def fence():
            barrier.wait()
            return fence_chain_dispatch(scope, run, worker, claim["claim_token"])

        with ThreadPoolExecutor(max_workers=2) as pool:
            halt_future = pool.submit(halt)
            fence_future = pool.submit(fence)
            halted, fenced = halt_future.result(), fence_future.result()
        assert not (halted["inflight"] is False and fenced is not None), (
            "a new fence was admitted after the halt transaction")
        assert operator_halt.status(identity, "TSLA")["halted"] is True
        if fenced is not None:
            assert clear_chain_dispatch_inflight(
                scope, run, worker, claim["claim_token"])
        release_chain_dispatch(scope, worker, claim["claim_token"])
        operator_halt.clear_halt(
            identity, "TSLA", expected_halt_id=halted["halt_id"],
            operator="bob", reason="emulator reviewed", apply=True)
        assert operator_halt.status(identity, "TSLA")["halted"] is False
        next_claim = claim_chain_dispatch(scope, "next-worker")
        assert fence_chain_dispatch(
            scope, f"next-{index}", "next-worker", next_claim["claim_token"])
    print(json.dumps({"status": "PASS", "races": 8}))


if __name__ == "__main__":
    main()
