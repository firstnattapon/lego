"""Small allowlisted Cloud Logging events; never serialize broker payloads."""
import json
import os
from datetime import datetime, timezone
from lego_outbox import TERMINAL, normalize_status


def business_status(body: dict, http_status: int) -> str:
    if body.get("pipeline_status") == "AUTH_BACKOFF":
        return "AUTH_BACKOFF"
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
    if (decision.get("outbox_blocked") == "BROKER_REJECT_HALT"
            or any(item.get("broker_reject_halted") for item in results)
            or body.get("broker_reject_halted")):
        return "BROKER_REJECT_HALT"
    if any(normalize_status(item.get("broker_status") or item.get("status"))
           in {"FAILED", "REJECTED"} for item in results):
        return "BROKER_ORDER_FAILED"
    if any(item.get("token_preflight_blocked") for item in results):
        return "TOKEN_PREFLIGHT_BLOCKED"
    if any(item.get("status") == "AWAITING_BROKER_FEE" for item in results):
        return "WAITING_BROKER_FEE"
    if any(normalize_status(item.get("status")) not in TERMINAL for item in results):
        return "WAITING_RECONCILIATION"
    if decision.get("outbox_error"):
        return "OUTBOX_RECOVERY_PENDING"
    if decision.get("outbox_blocked") or decision.get("outbox_skipped"):
        return "INTENT_BLOCKED"
    remaining = decision.get("dna_steps_remaining")
    if type(remaining) is int and 0 <= remaining <= 10:
        return "DNA_LOW"
    return decision.get("pipeline_status") or body.get("pipeline_status", "UNKNOWN")


def emit_tick(body: dict, code: int) -> None:
    health = business_status(body, code)
    body["business_status"] = health
    decision = body.get("decision") or {}
    event = {
        "event": "lego_tick_completed", "timestamp": datetime.now(timezone.utc).isoformat(),
        "severity": ("ERROR" if health in {"ERROR", "FEE_OVERDUE", "MANUAL_RECONCILIATION_REQUIRED",
                                           "BROKER_REJECT_HALT", "BROKER_ORDER_FAILED", "TOKEN_PREFLIGHT_BLOCKED"}
                     else "WARNING" if health in {"AUTH_BACKOFF", "WAITING_BROKER_FEE", "WAITING_RECONCILIATION",
                                                  "OUTBOX_RECOVERY_PENDING", "INTENT_BLOCKED", "DNA_EXHAUSTED", "DNA_LOW"}
                     else "INFO"),
        "revision": os.environ.get("K_REVISION"),
        "candidate_hash": os.environ.get("LEGO_CANDIDATE_HASH"),
        "correlation_id": body.get("correlation_id"), "http_status": code,
        "pipeline_status": body.get("pipeline_status"), "business_status": health,
        "duration_ms": body.get("duration_ms"), "environment": body.get("environment"),
        "mode": body.get("mode"), "active": body.get("active"),
        "decision": {key: decision.get(key) for key in
                     ("run_id", "market_slot_id", "step", "status", "pipeline_status", "committed",
                      "dna_steps_remaining")},
        "execution": [],
        "errors": [],
    }
    # Include allowlisted broker metadata to diagnose repeated read failures.
    # Never copy free-form errors (or broker payloads) into logs.
    for phase_name, phase in (("tick", body), ("decision", decision),
                              ("recovery", body.get("recovery") or {}),
                              ("dispatch", body.get("dispatch") or {})):
        if phase.get("error"):
            details = _broker_fields(phase)
            event["errors"].append({
                "phase": phase_name,
                "type": phase.get("error_type") or phase.get("type") or "UnknownError",
                **({"broker_error": details} if details else {}),
            })
    for phase_name in ("recovery", "dispatch"):
        phase = body.get(phase_name) or {}
        for item in phase.get("results", []):
            if item.get("error"):
                details = _broker_fields(item)
                event["errors"].append({"phase": phase_name,
                                        "type": item.get("error_type") or "UnknownError",
                                        **({"broker_error": details} if details else {})})
            event["execution"].append({"phase": phase_name, **{
                key: item.get(key) for key in ("run_id", "status", "broker_status", "broker_fee_status",
                                             "cashflow_finalized", "fee_overdue", "fee_pending_age_seconds",
                                             "broker_reason_missing", "broker_reject_code",
                                             "broker_reject_halted", "token_preflight_blocked")}})
    print(json.dumps(event, ensure_ascii=False, allow_nan=False), flush=True)


def _broker_fields(phase: dict) -> dict:
    """Revalidate metadata at the log boundary, including persisted old records."""
    from types import SimpleNamespace
    from webull_io import broker_error_details
    details = phase.get("broker_error")
    if not isinstance(details, dict):
        return {}
    return broker_error_details(SimpleNamespace(
        http_status=details.get("http_status"), error_code=details.get("code"),
        request_id=details.get("request_id"), _lego_operation=details.get("operation")))
