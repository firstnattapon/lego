"""Read-only, fail-closed audit of an RTDB export before a writer cutover.

This is an offline gate, not proof of the broker's current order/position state.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path


OUTBOX_PATHS = ("webull_lego_order_outbox", "webull_lego_outbox")
SAFE_UNSENT = {
    "EXPIRED_UNSENT", "SUPPRESSED_ACTIVE_ORDER", "SUPPRESSED_STATE_CHANGED",
    "NOT_PLACED", "UNSENT_ABORTED",
}
FENCE_FIELDS = ("inflight_run_id", "fenced_run_id", "place_fence", "claim_token")
BROKER_MARKERS = ("placed_at", "broker_order_id", "broker_id", "order_id",
                  "place_fence")


def safe_unsent(intent: dict, status: str) -> bool:
    if status not in SAFE_UNSENT or intent.get("place_attempted") is True:
        return False
    if "place_attempted" in intent and intent["place_attempted"] is not False:
        return False
    if (intent.get("needs_manual_check") or intent.get("audit_pending")
            or any(intent.get(key) for key in BROKER_MARKERS)):
        return False
    filled = intent.get("filled_quantity")
    if filled is None:
        return True
    try:
        quantity = Decimal(str(filled))
        return quantity.is_finite() and quantity == 0
    except (InvalidOperation, ValueError):
        return False


def audit_export(path: Path) -> dict:
    raw = path.read_bytes()
    root = json.loads(raw)
    if not isinstance(root, dict):
        raise ValueError("RTDB export root must be a JSON object")
    present_paths = [name for name in OUTBOX_PATHS if name in root]
    issues = []
    if not present_paths:
        issues.append("order outbox node missing")
    blocked = []
    total = 0
    for path in present_paths:
        outbox = root[path]
        if not isinstance(outbox, dict):
            issues.append(f"{path} must be an object")
            continue
        for chain_key, intents in outbox.items():
            if not isinstance(intents, dict):
                issues.append(f"{path}/{chain_key} must be an object")
                continue
            for run_id, intent in intents.items():
                if not isinstance(intent, dict):
                    issues.append(f"{path}/{chain_key}/{run_id} must be an object")
                    continue
                total += 1
                status = str(intent.get("status") or "UNKNOWN").strip().upper()
                if not safe_unsent(intent, status):
                    blocked.append({
                        "path": path, "chain_key": str(chain_key),
                        "run_id": str(run_id), "status": status,
                    })
    fences = []
    locks = root.get("webull_lego_order_dispatch_locks", {})
    if not isinstance(locks, dict):
        issues.append("webull_lego_order_dispatch_locks must be an object")
    else:
        for key, lock in locks.items():
            if not isinstance(lock, dict):
                issues.append(f"dispatch lock {key} must be an object")
            elif any(lock.get(field) for field in FENCE_FIELDS):
                fences.append(str(key))
    return {
        "export_sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "outbox_paths": present_paths,
        "outbox_intents": total,
        "attempted_or_unresolved": blocked,
        "active_fences": fences,
        "issues": issues,
        "cutover_safe": bool(present_paths) and not (blocked or fences or issues),
        "rule": "No broker attempt, unresolved intent, or active fence may cross cutover; verify broker state separately",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path)
    args = parser.parse_args()
    result = audit_export(args.export.resolve())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["cutover_safe"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
