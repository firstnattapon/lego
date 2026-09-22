"""Offline, aggregate-only incident audit. Never imports SDKs or connects to RTDB.

Usage: python tools/audit_broker_rejects.py --database EXPORT.json --logs LOGS.json
Raw evidence remains local; output contains aggregate facts and source SHA256s.
"""
import argparse
from collections import Counter
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path


def documents(value):
    if isinstance(value, dict):
        return [v for v in value.values() if isinstance(v, dict)]
    return [v for v in (value or []) if isinstance(v, dict)]


def number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def audit(database, logs):
    orders = [item for chain in documents(database.get("webull_lego_order_outbox"))
              for item in documents(chain)]
    rows = documents(database.get("webull_lego_rows"))
    failed = [o for o in orders if o.get("status") in {"FAILED", "REJECTED"}]
    quantities = [number(o.get("quantity")) for o in orders]
    quantities = [q for q in quantities if q is not None]
    stamps = sorted(str(o["created_at"]) for o in orders if o.get("created_at"))
    steps = [r.get("DNA step") for r in rows if isinstance(r.get("DNA step"), (int, float))]
    http_errors = Counter()
    for order in orders:
        error = order.get("broker_error") or {}
        if isinstance(error.get("http_status"), int):
            http_errors[str(error["http_status"])] += 1
    # Capture business status hidden inside Cloud Logging textPayload as JSON.
    events = []
    for entry in logs:
        event = entry.get("jsonPayload")
        if not isinstance(event, dict):
            try:
                event = json.loads(entry.get("textPayload", ""))
            except (TypeError, ValueError):
                event = {}
        if isinstance(event, dict) and event.get("event") == "lego_tick_completed":
            events.append(event)
    return {
        "rows": len(rows), "orders": len(orders),
        "row_statuses": dict(Counter(r.get("สถานะ", "UNKNOWN") for r in rows)),
        "order_statuses": dict(Counter(o.get("status", "UNKNOWN") for o in orders)),
        "place_attempted": sum(o.get("place_attempted") is True for o in orders),
        "failed_orders": len(failed),
        "failed_explicit_zero_fill": sum(number(o.get("filled_quantity")) == 0 for o in failed),
        "failed_reason_missing": sum("reason not supplied" in str(o.get("terminal_reason", "")) for o in failed),
        "raw_detail_available": sum(bool(o.get("broker_raw_detail")) for o in orders),
        "fractional_quantities": sum(q != q.to_integral_value() for q in quantities),
        "quantity_range": [str(min(quantities)), str(max(quantities))] if quantities else [],
        "holding_values": sorted({str(r.get("จำนวนถือครอง (หุ้น)")) for r in rows}),
        "step_range": [min(steps), max(steps)] if steps else [],
        "created_at_range": [stamps[0], stamps[-1]] if stamps else [],
        "order_http_errors": dict(http_errors),
        "log_entries": len(logs),
        "log_severities": dict(Counter(l.get("severity", "DEFAULT") for l in logs)),
        "tick_events": len(events),
        "tick_business_statuses": dict(Counter(str(e.get("business_status")) for e in events)),
        "live_readiness": "NOT_PROVEN",
        "broker_rejection_cause": "UNDETERMINED_WITHOUT_BROKER_DETAIL_OR_SUPPORT",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--logs", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    raw_db, raw_logs = args.database.read_bytes(), args.logs.read_bytes()
    result = audit(json.loads(raw_db.decode("utf-8-sig")), json.loads(raw_logs.decode("utf-8-sig")))
    result["source_sha256"] = {"database": hashlib.sha256(raw_db).hexdigest(),
                               "logs": hashlib.sha256(raw_logs).hexdigest()}
    text = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf8")
    print(text)


if __name__ == "__main__":
    main()
