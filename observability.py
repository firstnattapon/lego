"""Small allowlisted Cloud Logging events; never serialize broker payloads."""
import json
import os
import re
from datetime import datetime, timezone
from lego_outbox import TERMINAL, normalize_status


def request_trace(request) -> dict:
    """Accept only Cloud Trace syntax; never copy arbitrary request headers."""
    headers = getattr(request, "headers", {}) or {}
    value = headers.get("X-Cloud-Trace-Context", "")
    project = (os.environ.get("LEGO_TRACE_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")
               or os.environ.get("GCLOUD_PROJECT", ""))
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", project):
        return {}
    match = re.fullmatch(r"([0-9a-f]{32})/([0-9]{1,20})(?:;o=([01]))?", value)
    if not match or int(match[1], 16) == 0 or not 0 < int(match[2]) < 2**64:
        return {}
    return {"logging.googleapis.com/trace": f"projects/{project}/traces/{match[1]}",
            "logging.googleapis.com/spanId": format(int(match[2]), "016x"),
            "logging.googleapis.com/trace_sampled": match[3] == "1"}


def business_status(body: dict, http_status: int) -> str:
    if body.get("pipeline_status") == "AUTH_BACKOFF":
        return "AUTH_BACKOFF"
    decision = body.get("decision") or {}
    phases = [body.get("recovery") or {}, body.get("dispatch") or {}]
    results = [item for phase in phases for item in phase.get("results", [])]
    if any(item.get("needs_manual_check") for item in results) or any(
            phase.get("dispatch_blocked") for phase in phases):
        return "MANUAL_RECONCILIATION_REQUIRED"
    if (http_status >= 400 or any(phase.get("error") for phase in phases)
            or any(item.get("error") for item in results)):
        return "ERROR"
    if any(item.get("fee_overdue") for item in results):
        return "FEE_OVERDUE"
    if (decision.get("outbox_blocked") == "BROKER_REJECT_HALT"
            or any(item.get("broker_reject_halted") for item in results)
            or body.get("broker_reject_halted")):
        return "BROKER_REJECT_HALT"
    if any(normalize_status(item.get("broker_status") or item.get("status"))
           in {"FAILED", "REJECTED"} for item in results):
        return "BROKER_ORDER_FAILED"
    if any(item.get("token_preflight_blocked") for item in results):
        return "TOKEN_PREFLIGHT_BLOCKED"
    if any(item.get("execution_limit_blocked") for item in results):
        return "EXECUTION_LIMIT_BLOCKED"
    if any(item.get("reconciliation_overdue") for item in results):
        return "RECONCILIATION_OVERDUE"
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


def emit_tick(body: dict, code: int, *, request=None) -> None:
    health = business_status(body, code)
    body["business_status"] = health
    decision = body.get("decision") or {}
    event = {
        "event": "lego_tick_completed", "timestamp": datetime.now(timezone.utc).isoformat(),
        "severity": ("ERROR" if health in {"ERROR", "FEE_OVERDUE", "MANUAL_RECONCILIATION_REQUIRED",
                                           "BROKER_REJECT_HALT", "BROKER_ORDER_FAILED", "TOKEN_PREFLIGHT_BLOCKED", "EXECUTION_LIMIT_BLOCKED", "RECONCILIATION_OVERDUE"}
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
        **request_trace(request),
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
                                             "broker_reject_halted", "token_preflight_blocked",
                                             "execution_limit_blocked", "needs_manual_check",
                                             "reconciliation_overdue", "reconciliation_age_seconds")}})
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
