"""Real RTDB concurrency: one durable cancel right; all other workers query only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    if not os.environ.get("FIREBASE_DATABASE_EMULATOR_HOST", "").startswith(("localhost:", "127.0.0.1:")):
        raise SystemExit("local emulator required")
    import firebase_admin
    from firebase_admin import db
    import lego_outbox as outbox
    import order_recovery as recovery
    import transition_audit as audit
    from recovery_policy import RecoveryPolicy
    project = os.environ.get("GCLOUD_PROJECT", "demo-lego-firebase")
    firebase_admin.initialize_app(options={"projectId": project,
        "databaseURL": f"https://{project}-default-rtdb.firebaseio.com"})
    nonce = uuid.uuid4().hex
    chain, rid, identity = "cancel_" + nonce, nonce, "identity_" + nonce
    scope = outbox.account_symbol_fence_key(identity, "UBER")
    policy = RecoveryPolicy("cancel")
    now = datetime.now(timezone.utc)
    try:
        outbox.put_intent(chain, rid, {"status": "SUBMITTED", "symbol": "UBER",
            "runtime_identity_fingerprint": identity, "place_attempted": True,
            "cancel_policy": policy.snapshot(), "cancel_policy_hash": policy.fingerprint})
        claim = outbox.claim_chain_dispatch(scope, "worker", lease_seconds=120)
        outbox.fence_chain_dispatch(scope, rid, "worker", claim["claim_token"], intent_chain_key=chain)
        intent = outbox.claim_intent(chain, rid, "worker", lease_seconds=120)
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _: recovery.begin_cancel(intent, claim, policy, now), range(16)))
        assert sum(r is not None for r in results) == 1
        saved = outbox.read_intent(chain, rid)
        assert saved["cancel_attempt_count"] == 1 and saved["status"] == "CANCEL_REQUESTED"
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: audit.replay(chain, rid), range(8)))
        events = db.reference(f"{audit.PATH}/{chain}/{rid}").get()
        assert isinstance(events, dict) and len(events) == 2
        assert not outbox.read_intent(chain, rid).get("transition_pending")
        print(json.dumps({"status": "PASS", "real_rtdb_emulator": True,
                          "concurrent_workers": 16, "cancel_rights": 1, "immutable_events": 2}))
    finally:
        for path in (f"{outbox.OUTBOX_PATH}/{chain}", f"{outbox.DISPATCH_LOCK_PATH}/{scope}", f"{audit.PATH}/{chain}"):
            db.reference(path).delete()


if __name__ == "__main__":
    main()
