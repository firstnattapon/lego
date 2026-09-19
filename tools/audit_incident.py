"""Read local evidence and emit aggregate facts only; never publish raw exports."""
import argparse
from collections import Counter
import hashlib
import importlib.util
import json
import sys
from pathlib import Path


def audit(log_path, database_path, reader_root):
    logs = json.loads(Path(log_path).read_text(encoding="utf-8"))
    database = json.loads(Path(database_path).read_text(encoding="utf-8"))
    spec = importlib.util.spec_from_file_location("audit_reader", Path(reader_root) / "lego_dash_core.py")
    reader = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(Path(reader_root).resolve()))
    try:
        spec.loader.exec_module(reader)
    finally:
        sys.path.pop(0)
    rows = reader.rows_to_df(database.get("webull_lego_rows"))
    checks, ok = reader.integrity_report(rows)
    errors = list(database.get("webull_lego_errors", {}).values())
    orders = list(database.get("webull_lego_order_audit", {}).values())
    ticks = [entry["jsonPayload"] for entry in logs
             if entry.get("jsonPayload", {}).get("event") == "lego_tick_completed"]
    return {
        "input_sha256": {"logs": hashlib.sha256(Path(log_path).read_bytes()).hexdigest(),
                         "database": hashlib.sha256(Path(database_path).read_bytes()).hexdigest()},
        "log_records": len(logs), "tick_events": len(ticks),
        "first_log_utc": min(entry["timestamp"] for entry in logs),
        "last_log_utc": max(entry["timestamp"] for entry in logs),
        "log_severity": dict(Counter(entry.get("severity", "UNSPECIFIED") for entry in logs)),
        "error_nodes": len(errors),
        "unauthorized_error_nodes": sum("UNAUTHORIZED" in json.dumps(entry) for entry in errors),
        "error_types": dict(Counter(entry.get("type", "UNKNOWN") for entry in errors)),
        "committed_rows": len(rows),
        "observed_holdings": sorted(set(float(value) for value in rows["จำนวนถือครอง (หุ้น)"])),
        "order_statuses": dict(Counter(entry.get("status", "UNKNOWN") for entry in orders)),
        "failed_orders_missing_reason": sum(entry.get("status") == "FAILED" and not entry.get("reject_reason") for entry in orders),
        "persisted_equations_pass": bool(ok),
        "equation_checks": [{"id": entry["ข้อ"], "passed": bool(entry["ผ่าน"])}
                            for entry in checks.to_dict(orient="records")],
        "scope": "Supplied historical snapshots only; not a live broker reconciliation or live approval",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--reader-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.logs, args.database, args.reader_root), indent=2, allow_nan=False))
