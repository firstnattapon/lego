"""Read-only audit of an RTDB JSON export before v2 cutover."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ATTEMPTED = {
    "PLACING_UNKNOWN", "PLACING", "SUBMITTED", "UNKNOWN",
    "PARTIAL_FILLED", "PARTIALLY_FILLED", "AWAITING_FILL_CONFIRMATION",
}


def audit_export(path: Path) -> dict:
    raw = path.read_bytes()
    root = json.loads(raw)
    if not isinstance(root, dict):
        raise ValueError("RTDB export root must be a JSON object")
    outbox = root.get("webull_lego_outbox") or {}
    if not isinstance(outbox, dict):
        raise ValueError("webull_lego_outbox must be an object")
    attempted = []
    total = 0
    for chain_key, intents in outbox.items():
        if not isinstance(intents, dict):
            continue
        for run_id, intent in intents.items():
            if not isinstance(intent, dict):
                continue
            total += 1
            status = str(intent.get("status") or "UNKNOWN").upper()
            if status in ATTEMPTED or bool(intent.get("place_attempted")):
                attempted.append({
                    "chain_key": str(chain_key), "run_id": str(run_id),
                    "status": status,
                })
    return {
        "export_sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "outbox_intents": total,
        "attempted_or_unresolved": attempted,
        "cutover_safe": not attempted,
        "rule": "cutover waits until every attempted/unknown order is reconciled",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path)
    args = parser.parse_args()
    print(json.dumps(audit_export(args.export.resolve()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

