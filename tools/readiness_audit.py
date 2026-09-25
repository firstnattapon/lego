"""Offline, redacted snapshot/log audit. This cannot certify current broker state.

Usage: python -m tools.readiness_audit --export private.json --logs logs.json
       --candidate HASH --revision REVISION --output release_evidence/ACCEPTANCE.json
Exit 1 means FAIL/BLOCKED; missing evidence is never PASS. No network or writes
to the source evidence. The output intentionally excludes account/order IDs.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re

from lego_orders import normalize_status
from tools.migration_audit import audit_export, safe_unsent


def number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def same_number(left, right, tolerance):
    a, b = number(left), number(right)
    return a is not None and b is not None and abs(a - b) <= Decimal(tolerance)


def read_json(path):
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


_HTTP_LATENCY = re.compile(r"\d+(?:\.\d+)?s\Z")
_PAIRING_CLOCK_TOLERANCE = timedelta(seconds=2)


def _utc_timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _cloud_run_scope(entry):
    resource = entry.get("resource")
    if not isinstance(resource, dict) or resource.get("type") != "cloud_run_revision":
        return None
    labels = resource.get("labels")
    if not isinstance(labels, dict):
        return None
    scope = tuple(labels.get(name) for name in (
        "project_id", "location", "service_name", "revision_name"))
    return scope if all(isinstance(part, str) and part for part in scope) else None


def _log_trace(entry, payload=None):
    value = entry.get("trace") or (payload or {}).get("logging.googleapis.com/trace")
    return value if isinstance(value, str) and value.strip() else None


def pair_request_ticks(ticks, requests):
    """Prove a one-to-one Cloud Run request/tick link without equating span IDs.

    Cloud Run's request log and the application log are different spans of the
    same trace. A shared trace is useful evidence only when it is unique in
    each set, both records belong to the same service revision, and the tick
    occurred during the HTTP request's recorded lifetime.
    """
    problems = Counter()
    tick_by_trace, request_by_trace = defaultdict(list), defaultdict(list)
    for entry, payload in ticks:
        trace = _log_trace(entry, payload)
        if trace is None:
            problems["tick_trace_missing"] += 1
        else:
            tick_by_trace[trace].append((entry, payload))
    for entry in requests:
        trace = _log_trace(entry)
        if trace is None:
            problems["request_trace_missing"] += 1
        else:
            request_by_trace[trace].append(entry)
    problems["duplicate_tick_traces"] = sum(
        len(group) - 1 for group in tick_by_trace.values() if len(group) > 1)
    problems["duplicate_request_traces"] = sum(
        len(group) - 1 for group in request_by_trace.values() if len(group) > 1)
    problems["ticks_without_request"] = len(tick_by_trace.keys() - request_by_trace.keys())
    problems["requests_without_tick"] = len(request_by_trace.keys() - tick_by_trace.keys())

    matched = 0
    for trace in tick_by_trace.keys() & request_by_trace.keys():
        tick_group, request_group = tick_by_trace[trace], request_by_trace[trace]
        if len(tick_group) != 1 or len(request_group) != 1:
            continue
        tick_entry, payload = tick_group[0]
        request_entry = request_group[0]
        tick_scope = _cloud_run_scope(tick_entry)
        request_scope = _cloud_run_scope(request_entry)
        if (tick_scope is None or request_scope is None or tick_scope != request_scope
                or payload.get("revision") != tick_scope[-1]):
            problems["service_or_revision_mismatch"] += 1
            continue
        tick_time = _utc_timestamp(tick_entry.get("timestamp"))
        request_time = _utc_timestamp(request_entry.get("timestamp"))
        http = request_entry.get("httpRequest")
        latency_text = http.get("latency") if isinstance(http, dict) else None
        if (tick_time is None or request_time is None
                or not isinstance(latency_text, str)
                or not _HTTP_LATENCY.fullmatch(latency_text)):
            problems["request_window_missing"] += 1
            continue
        try:
            request_end = request_time + timedelta(
                seconds=float(Decimal(latency_text[:-1])))
        except (ValueError, OverflowError):
            problems["request_window_missing"] += 1
            continue
        if not request_time <= tick_time <= request_end + _PAIRING_CLOCK_TOLERANCE:
            problems["tick_outside_request_window"] += 1
            continue
        if (type(payload.get("http_status")) is not int
                or type(http.get("status")) is not int
                or payload["http_status"] != http["status"]):
            problems["http_status_mismatch"] += 1
            continue
        matched += 1
    problems = {name: count for name, count in problems.items() if count}
    return (bool(ticks) and bool(requests) and matched == len(ticks) == len(requests)
            and not problems), {"requests": len(requests), "ticks": len(ticks),
                               "matched": matched, "issues": problems}


def link_committed_rows(rows, ticks, requests):
    """Match committed decision rows to tick logs inside the supplied window.

    Rows outside the request window are historical context, not proof that the
    provided log export is incomplete. A missing row for a commit event inside
    the export is always a failure.
    """
    problems = Counter()
    committed = Counter()
    matching = set()
    for _entry, payload in ticks:
        decision = payload.get("decision")
        if not isinstance(decision, dict) or decision.get("committed") is not True:
            continue
        run = str(decision.get("run_id") or "")
        committed[run] += 1
        row = rows.get(run)
        if (not run or not isinstance(row, dict) or row.get("committed") is not True
                or str(row.get("run_id")) != run
                or row.get("สถานะ") != decision.get("status")
                or row.get("DNA step") != decision.get("step")
                or row.get("market_slot_id") != decision.get("market_slot_id")):
            problems["commit_event_row_missing_or_mismatched"] += 1
        else:
            matching.add(run)
    problems["duplicate_commit_event"] = sum(
        count - 1 for count in committed.values() if count > 1)

    starts, ends = [], []
    for entry in requests:
        start = _utc_timestamp(entry.get("timestamp"))
        http = entry.get("httpRequest")
        latency = http.get("latency") if isinstance(http, dict) else None
        if start is None or not isinstance(latency, str) or not _HTTP_LATENCY.fullmatch(latency):
            continue
        try:
            end = start + timedelta(seconds=float(Decimal(latency[:-1])))
        except (ValueError, OverflowError):
            continue
        starts.append(start)
        ends.append(end)
    scoped = 0
    if starts:
        first, last = min(starts), max(ends)
        for run, row in rows.items():
            if not isinstance(row, dict) or row.get("committed") is not True:
                continue
            observed = _utc_timestamp(row.get("เวลา (UTC)"))
            if observed is None:
                problems["committed_row_time_missing"] += 1
            elif first <= observed <= last:
                scoped += 1
                if run not in matching:
                    problems["scoped_row_commit_event_missing"] += 1
    else:
        problems["request_window_missing"] += 1
    problems = {name: count for name, count in problems.items() if count}
    return not problems and bool(starts), {
        "commit_events": sum(committed.values()),
        "rows_in_request_window": scoped,
        "matched_rows": len(matching),
        "issues": problems,
    }


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
    rows = mapping(root.get("webull_lego_rows"))
    cash = mapping(root.get("webull_lego_broker_cashflow", {}))
    state = mapping(root.get("webull_lego_state", {}))
    realized = mapping(root.get("webull_lego_realized", {}))
    ids = Counter()
    matched = 0
    filled = 0
    mirror_fields = ("status", "filled_quantity", "filled_price", "filled_fee",
                     "broker_fee_status", "cashflow_finalized", "place_attempted",
                     "needs_manual_check", "order_contract_anomaly",
                     "broker_reason_missing")
    for chain, run, intent in intents:
        status = normalize_status(intent.get("status") or "UNKNOWN")
        statuses[status] += 1
        ids[str(intent.get("client_order_id") or run)] += 1
        row = rows.get(run)
        if not isinstance(row, dict) or row.get("committed") is not True:
            issues["committed_decision_row_missing"] += 1
            row = {}
        elif (str(row.get("run_id")) != run or row.get("chain_key") != chain
              or row.get("สถานะ") != intent.get("row_status")
              or row.get("ฝั่ง") != intent.get("side")
              or row.get("สินทรัพย์") != intent.get("symbol")
              or number(row.get("จำนวนสั่ง (หุ้น)")) != number(intent.get("quantity"))):
            issues["intent_decision_row_mismatch"] += 1
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
        if intent.get("order_contract_anomaly"):
            issues["order_contract_anomaly"] += 1
        if status in {"FAILED", "REJECTED"} and intent.get("broker_reason_missing") is True:
            issues["broker_rejection_reason_missing"] += 1
        # A broker-terminal order with no quantity is unresolved evidence, not
        # proof of a zero fill. Only an intent never sent may omit that field.
        unsent = safe_unsent(intent, status)
        qty = number(intent.get("filled_quantity", 0 if unsent else None))
        if qty is None or qty < 0:
            issues["invalid_fill_quantity"] += 1
            continue
        if not unsent and status not in {"FILLED", "CANCELLED", "FAILED", "REJECTED", "EXPIRED"}:
            issues["unresolved_execution"] += 1
        if status == "FILLED" and qty == 0:
            issues["filled_without_quantity"] += 1
        if qty > 0:
            filled += 1
            payload = intent.get("order_payload")
            order = payload[0] if isinstance(payload, list) and len(payload) == 1 \
                and isinstance(payload[0], dict) else None
            submitted = number(order.get("quantity")) if order else None
            intended = number(intent.get("quantity"))
            if submitted is None or submitted <= 0 or intended is None or intended <= 0:
                issues["submitted_quantity_unverifiable"] += 1
            else:
                if submitted != intended:
                    issues["intent_payload_quantity_mismatch"] += 1
                if qty > submitted:
                    issues["fill_exceeds_submitted_quantity"] += 1
            if (order is None or order.get("client_order_id") !=
                    str(intent.get("client_order_id") or run)
                    or order.get("side") != intent.get("side")
                    or order.get("symbol") != intent.get("symbol")):
                issues["submitted_order_identity_mismatch"] += 1
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
            final = mapping(flow.get("finalized_runs", {})).get(run)
            realized_fill = applied.get(run)
            if not isinstance(final, dict) or not isinstance(realized_fill, dict):
                issues["model_or_realized_witness_missing"] += 1
            elif (not same_number(final.get("filled_quantity"), qty, "1e-9")
                  or not same_number(final.get("filled_price"), price, "1e-8")
                  or not same_number(realized_fill.get("quantity"), qty, "1e-9")
                  or not same_number(realized_fill.get("average_price"), price, "1e-8")
                  or not same_number(realized_fill.get("fee"), fee, "1e-8")
                  or realized_fill.get("side") != intent.get("side")
                  or row.get("cashflow_status") != "FINALIZED"
                  or not same_number(row.get("execution_quantity"), qty, "1e-9")
                  or not same_number(row.get("execution_price"), price, "1e-8")):
                issues["model_or_realized_witness_mismatch"] += 1
    if any(count > 1 for count in ids.values()):
        issues["duplicate_client_order_identity"] += 1
    locks = mapping(root.get("webull_lego_order_dispatch_locks", {}))
    lock_docs = [mapping(lock) for lock in locks.values()]
    halt_docs = [mapping(lock.get("operator_halt", {})) for lock in lock_docs]
    halt_audit = mapping(root.get("webull_lego_operator_halt_audit", {}))
    for scope, lock in locks.items():
        halt = mapping(mapping(lock).get("operator_halt", {}))
        if not halt:
            continue
        events = mapping(halt_audit.get(scope, {}))
        halt_id = halt.get("halt_id")
        actions = {event.get("action") for event in events.values()
                   if isinstance(event, dict) and event.get("scope") == scope
                   and event.get("halt_id") == halt_id}
        expected = {"HALT"} if halt.get("halted") else {"HALT", "CLEAR"}
        if not halt_id or not expected.issubset(actions):
            issues["operator_halt_audit_history_missing"] += 1
        last = halt.get("last_audit_event_id")
        if not last or not isinstance(events.get(last), dict):
            issues["operator_halt_last_audit_witness_missing"] += 1
    unresolved = sum(bool(lock.get("inflight_run_id")) for lock in lock_docs)
    operator_halts = sum(bool(halt.get("halted")) for halt in halt_docs)
    halt_audits_pending = sum(bool(halt.get("audit_pending_event"))
                              for halt in halt_docs)
    check("snapshot_integrity", bool(intents) and not issues,
          {"intents": len(intents), "statuses": dict(statuses), "mirrors_matched": matched,
           "positive_fills": filled, "issues": dict(issues)})
    check("money_fences_resolved",
          unresolved == operator_halts == halt_audits_pending == 0,
          {"unresolved_fences": unresolved, "operator_halts": operator_halts,
           "operator_halt_audits_pending": halt_audits_pending})

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
    pairing_ok, pairing_detail = pair_request_ticks(ticks, requests)
    check("request_tick_pairing", pairing_ok, pairing_detail)
    lineage_ok, lineage_detail = link_committed_rows(rows, ticks, requests)
    check("committed_row_log_lineage", lineage_ok, lineage_detail)
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
