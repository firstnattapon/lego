"""Small allowlisted Cloud Logging events; never serialize broker payloads."""
import json
import os
from datetime import datetime, timezone
from lego_outbox import TERMINAL, normalize_status


def business_status(body: dict, http_status: int) -> str:
    decision = body.get("decision") or {}
    phases = [body.get("recovery") or {}, body.get("dispatch") or {}]
    results = [item for phase in phases for item in phase.get("results", [])]
    if (http_status >= 400 or any(phase.get("error") for phase in phases)
            or any(item.get("error") for item in results)):
        return "ERROR"
    if any(item.get("fee_overdue") for item in results):
        return "FEE_OVERDUE"
    if any(item.get("needs_manual_check") for item in results) or any(
            phase.get("dispatch_blocked") for phase in phases):
        return "MANUAL_RECONCILIATION_REQUIRED"
    if any(item.get("status") == "AWAITING_BROKER_FEE" for item in results):
        return "WAITING_BROKER_FEE"
    if any(normalize_status(item.get("status")) not in TERMINAL for item in results):
        return "WAITING_RECONCILIATION"
    if decision.get("outbox_error"):
        return "OUTBOX_RECOVERY_PENDING"
    if decision.get("outbox_blocked") or decision.get("outbox_skipped"):
        return "INTENT_BLOCKED"
    return decision.get("pipeline_status") or body.get("pipeline_status", "UNKNOWN")


def emit_tick(body: dict, code: int) -> None:
    health = business_status(body, code)
    body["business_status"] = health
    decision = body.get("decision") or {}
    event = {
        "event": "lego_tick_completed", "timestamp": datetime.now(timezone.utc).isoformat(),
        "severity": ("ERROR" if health in {"ERROR", "FEE_OVERDUE", "MANUAL_RECONCILIATION_REQUIRED"}
                     else "WARNING" if health in {"WAITING_BROKER_FEE", "WAITING_RECONCILIATION",
                                                  "OUTBOX_RECOVERY_PENDING", "INTENT_BLOCKED", "DNA_EXHAUSTED"}
                     else "INFO"),
        "revision": os.environ.get("K_REVISION"),
        "candidate_hash": os.environ.get("LEGO_CANDIDATE_HASH"),
        "correlation_id": body.get("correlation_id"), "http_status": code,
        "pipeline_status": body.get("pipeline_status"), "business_status": health,
        "duration_ms": body.get("duration_ms"), "environment": body.get("environment"),
        "mode": body.get("mode"), "active": body.get("active"),
        "decision": {key: decision.get(key) for key in
                     ("run_id", "market_slot_id", "step", "status", "pipeline_status", "committed")},
        "execution": [],
    }
    for phase_name in ("recovery", "dispatch"):
        phase = body.get(phase_name) or {}
        for item in phase.get("results", []):
            event["execution"].append({"phase": phase_name, **{
                key: item.get(key) for key in ("run_id", "status", "broker_status", "broker_fee_status",
                                             "cashflow_finalized", "fee_overdue", "fee_pending_age_seconds")}})
    print(json.dumps(event, ensure_ascii=False, allow_nan=False), flush=True)
