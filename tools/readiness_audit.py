"""Offline, redacted snapshot/log audit. This cannot certify current broker state.

Usage: python -m tools.readiness_audit --export private.json --logs logs.json
       --candidate HASH --revision REVISION --output release_evidence/ACCEPTANCE.json
Exit 1 means FAIL/BLOCKED; missing evidence is never PASS. No network or writes
to the source evidence. The output intentionally excludes account/order IDs.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path

from tools.migration_audit import audit_export, safe_unsent


def number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def read_json(path):
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def build_report(export, logs, candidate, revision):
    root, export_hash = read_json(export)
    entries, logs_hash = read_json(logs)
    if not isinstance(root, dict) or not isinstance(entries, list):
        raise ValueError("expected RTDB object and Cloud Logging array")
    issues = Counter()
    statuses = Counter()
    checks = []

    def check(name, ok, detail):
        checks.append({"criterion": name, "status": "PASS" if ok else "FAIL", "detail": detail})

    def mapping(value):
        if not isinstance(value, dict):
            issues["malformed_or_missing_node"] += 1
            return {}
        return value

    intents = []
    for chain, records in mapping(root.get("webull_lego_order_outbox")).items():
        for run, raw in mapping(records).items():
            intent = mapping(raw)
            intents.append((chain, run, intent))
    audit = mapping(root.get("webull_lego_order_audit"))
    cash = mapping(root.get("webull_lego_broker_cashflow", {}))
    state = mapping(root.get("webull_lego_state", {}))
    realized = mapping(root.get("webull_lego_realized", {}))
    ids = Counter()
    matched = 0
    filled = 0
    mirror_fields = ("status", "filled_quantity", "filled_price", "filled_fee",
                     "broker_fee_status", "cashflow_finalized", "place_attempted")
    for chain, run, intent in intents:
        status = str(intent.get("status") or "UNKNOWN").upper()
        statuses[status] += 1
        ids[str(intent.get("client_order_id") or run)] += 1
        mirror = audit.get(run)
        version = intent.get("audit_revision")
        if (isinstance(mirror, dict) and type(version) is int and version >= 1
                and type(mirror.get("audit_revision")) is int
                and version == mirror.get("audit_revision")
                and not intent.get("audit_pending")
                and all(intent.get(key) == mirror.get(key) for key in mirror_fields)):
            matched += 1
        else:
            issues["mirror_missing_stale_or_mismatched"] += 1
        if intent.get("needs_manual_check") or status in {
                "RECONCILE_ABANDONED", "CASHFLOW_FINALIZE_ERROR", "REALIZED_MATH_ERROR"}:
            issues["manual_reconciliation_required"] += 1
        qty = number(intent.get("filled_quantity", 0))
        if qty is None or qty < 0:
            issues["invalid_fill_quantity"] += 1
            continue
        if not safe_unsent(intent, status) and status not in {"FILLED", "CANCELLED", "FAILED", "REJECTED", "EXPIRED"}:
            issues["unresolved_execution"] += 1
        if status == "FILLED" and qty == 0:
            issues["filled_without_quantity"] += 1
        if qty > 0:
            filled += 1
            fee, price = number(intent.get("filled_fee")), number(intent.get("filled_price"))
            if (fee is None or fee < 0 or price is None or price <= 0
                    or intent.get("broker_fee_status") != "KNOWN"
                    or intent.get("cashflow_finalized") is not True):
                issues["fill_fee_or_finalization_incomplete"] += 1
            event = mapping(mapping(cash.get(chain, {})).get("events", {})).get(run, {})
            event = mapping(event)
            if (qty != number(event.get("cumulative_quantity"))
                    or fee != number(event.get("actual_fees"))
                    or intent.get("side") != event.get("side")
                    or price is None
                    or qty * price != number(event.get("cumulative_notional"))):
                issues["cashflow_witness_mismatch"] += 1
            if fee is not None and price is not None:
                sign = 1 if intent.get("side") == "SELL" else -1
                if sign * qty * price - fee != number(event.get("cash_cumulative")):
                    issues["cashflow_arithmetic_mismatch"] += 1
            flow = mapping(mapping(state.get(chain, {})).get("execution_cashflow", {}))
            applied = mapping(mapping(realized.get(chain, {})).get("applied_fills", {}))
            if run not in mapping(flow.get("finalized_runs", {})) or run not in applied:
                issues["model_or_realized_witness_missing"] += 1
    if any(count > 1 for count in ids.values()):
        issues["duplicate_client_order_identity"] += 1
    locks = mapping(root.get("webull_lego_order_dispatch_locks", {}))
    unresolved = sum(bool(mapping(lock).get("inflight_run_id")) for lock in locks.values())
    check("snapshot_integrity", bool(intents) and not issues,
          {"intents": len(intents), "statuses": dict(statuses), "mirrors_matched": matched,
           "positive_fills": filled, "issues": dict(issues)})
    check("money_fences_resolved", unresolved == 0, {"unresolved_fences": unresolved})

    ticks, requests = [], []
    malformed = 0
    for entry in entries:
        if not isinstance(entry, dict):
            malformed += 1
            continue
        payload = entry.get("jsonPayload", {})
        if not isinstance(payload, dict):
            malformed += 1
            continue
        if payload.get("event") == "lego_tick_completed":
            ticks.append((entry, payload))
        if "httpRequest" in entry:
            requests.append(entry)
    correlations = [p.get("correlation_id") for _, p in ticks]
    required = ("correlation_id", "business_status", "revision", "candidate_hash", "timestamp")
    check("tick_fields", bool(ticks) and not malformed
          and all(all(p.get(k) for k in required) and type(p.get("http_status")) is int
                  and number(p.get("duration_ms")) is not None for _, p in ticks),
          {"ticks": len(ticks), "malformed_entries": malformed,
           "business_statuses": dict(Counter(str(p.get("business_status")) for _, p in ticks))})
    check("unique_tick_correlations", bool(ticks) and all(correlations)
          and len(set(correlations)) == len(correlations), {"ticks": len(ticks)})
    def trace(entry, payload=None):
        return (entry.get("trace") or (payload or {}).get("logging.googleapis.com/trace"),
                entry.get("spanId") or (payload or {}).get("logging.googleapis.com/spanId"))
    event_traces = Counter(trace(e, p) for e, p in ticks)
    request_traces = Counter(trace(e) for e in requests)
    check("request_tick_pairing", bool(ticks) and bool(requests)
          and all(all(key) and count == 1 for key, count in event_traces.items())
          and event_traces == request_traces,
          {"requests": len(requests), "ticks": len(ticks),
           "tick_traces_present": sum(all(trace(e, p)) for e, p in ticks)})
    check("candidate_revision_binding", bool(candidate) and bool(revision) and bool(ticks)
          and all(p.get("candidate_hash") == candidate and p.get("revision") == revision for _, p in ticks),
          {"expected_candidate": candidate, "expected_revision": revision})
    # A snapshot has no complete transition history, current broker witness,
    # operator approval, or proof of deployed IAM/alerts. Never manufacture GO.
    for criterion in ("current_broker_orders_positions_cash_fees", "complete_transition_history",
                      "exact_candidate_ci_emulator", "deployed_identity_config_image_binding",
                      "uat_buy_sell_restart_timeout_soak", "alert_delivery_and_operator_drill",
                      "prod_observe_and_canary_authorization"):
        checks.append({"criterion": criterion, "status": "BLOCKED",
                       "detail": "requires independently captured operational evidence"})
    return {"schema_version": 1, "status": "BLOCKED", "real_money_ready": False,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "evidence": {"export_sha256": export_hash, "logs_sha256": logs_hash},
            "scope": "offline snapshot and supplied log window; not broker certification",
            "cutover_safe": audit_export(export)["cutover_safe"], "checks": checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--logs", type=Path, required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output and args.output.resolve() in {args.export.resolve(), args.logs.resolve()}:
        parser.error("output must not overwrite input evidence")
    try:
        report = build_report(args.export, args.logs, args.candidate, args.revision)
    except (OSError, ValueError, TypeError) as exc:
        report = {"status": "BLOCKED", "real_money_ready": False,
                  "error_type": type(exc).__name__, "reason": "invalid or unreadable evidence"}
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
