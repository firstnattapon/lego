"""Summarize actual local validation without certifying broker acceptance.

Usage: python -m tools.build_release_evidence --validation PRIVATE_VALIDATION.json
       --output release_evidence/RELEASE-local.json

Input uses the command records captured by tools/verify_final_local.py. Logs
must be relative to the validation file and retain their original SHA-256.
Historical acceptance is never overwritten; no static PASS IDs or test counts.
This verifies local evidence integrity, not its authenticity or live trading.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path

from tools.candidate_manifest import build_manifest, digest


REQUIRED = ("dependency_install", "backend", "emulator", "compile",
            "pip_check", "pip_audit", "secret_scan")
EXTERNAL = ("incident_lineage_and_terminal_accounting", "deployment_identity",
            "uat_buy_sell_two_sessions_restart_rollover", "alert_delivery",
            "prod_observe_two_sessions", "controlled_live_authorization",
            "live_fills_fees_positions_cash")


def _timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("evidence timestamps require timezone")
    return parsed


def build(validation, manifest, evidence_dir):
    """Missing evidence is BLOCKED; contradictory evidence is FAIL."""
    if (validation.get("candidate_hash") != manifest["candidate_hash"]
            or validation.get("candidate_unchanged") is not True):
        raise ValueError("validation candidate missing, changed or mismatched")
    records = validation.get("commands", [])
    if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
        raise ValueError("commands must be a list of records")
    identifiers = [r.get("id") for r in records]
    if any(not isinstance(i, str) or not i for i in identifiers):
        raise ValueError("command ID required")
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("duplicate validation command IDs")
    records = {r["id"]: r for r in records}
    required = REQUIRED + (("reader",) if manifest["scope"] == "backend-and-reader" else ())
    checks = []
    root = Path(evidence_dir).resolve()
    for name in required:
        record = records.get(name)
        item = {"criterion": name, "required": True, "status": "BLOCKED"}
        checks.append(item)
        if record is None:
            item["reason"] = "validation command not captured"
            continue
        try:
            relative = Path(record["log"])
            path = (root / relative).resolve()
            if relative.is_absolute() or not path.is_relative_to(root):
                raise ValueError("log outside evidence directory")
            if not path.is_file() or not path.stat().st_size:
                item["reason"] = "raw command log missing or empty"
                continue
            if digest(path) != record["log_sha256"]:
                raise ValueError("raw command log hash mismatch")
            start, end = _timestamp(record["started_utc"]), _timestamp(record["finished_utc"])
            if start > end or end > datetime.now(timezone.utc):
                raise ValueError("invalid command time range")
            if not record.get("command") or type(record["exit_code"]) is not int:
                raise ValueError("command and integer exit code required")
            item.update(status="PASS" if record["exit_code"] == 0 else "FAIL",
                        exit_code=record["exit_code"], artifact_path=relative.as_posix(),
                        artifact_sha256=record["log_sha256"],
                        started_utc=record["started_utc"], finished_utc=record["finished_utc"])
        except (KeyError, TypeError, AttributeError, ValueError, OSError):
            item.update(status="FAIL", reason="invalid or changed command evidence")
    counts = Counter(item["status"] for item in checks)
    local = "FAIL" if counts["FAIL"] else "BLOCKED" if counts["BLOCKED"] else "PASS"
    return {"schema": "lego_local_release_evidence_v2",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "candidate_hash": manifest["candidate_hash"],
            "dependency_lock_hash": manifest["dependency_lock_hash"],
            "scope": "local command evidence integrity; not signed CI or broker acceptance",
            "local_status": local, "release_state": "NO_GO", "real_money_ready": False,
            "summary": {key.lower(): counts[key] for key in ("PASS", "FAIL", "BLOCKED")},
            "checks": checks,
            "external_gates": [{"criterion": gate, "required": True, "status": "BLOCKED"}
                               for gate in EXTERNAL]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    validation = json.loads(args.validation.read_text(encoding="utf-8"))
    report = build(validation, build_manifest(), args.validation.parent)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({key: report[key] for key in
                      ("local_status", "release_state", "real_money_ready")}))
    return 0 if report["local_status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
