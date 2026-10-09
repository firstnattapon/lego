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
    if (decision.get("outbox_blocked") == "OPERATOR_HALT"
            or any(item.get("operator_halt_blocked") for item in results)):
        return "OPERATOR_HALT"
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
    if (any(phase.get("open_order_blocked") for phase in phases)
            or any(item.get("status") == "SUPPRESSED_ACTIVE_ORDER" for item in results)):
        return "OPEN_ORDER_BLOCKED"
    if body.get("pipeline_status") == "TICK_DEFERRED" and (
            body.get("deferred_reason") == "tick_deadline"
            or any(item.get("deferred_reason") == "tick_deadline" for item in results)
            or any(p.get("deferred_reason") == "tick_deadline" for p in phases)):
        return "TICK_DEFERRED"
    if any(item.get("status") == "AWAITING_BROKER_FEE" for item in results):
        return "WAITING_BROKER_FEE"
    if any(normalize_status(item.get("status")) not in TERMINAL for item in results):
        return "WAITING_RECONCILIATION"
    if decision.get("outbox_error"):
        return "OUTBOX_RECOVERY_PENDING"
    if decision.get("outbox_blocked") or decision.get("outbox_skipped"):
        return "INTENT_BLOCKED"
    health = body.get("operational_health") or {}
    if health.get("release_expired"):
        return "RELEASE_EXPIRED"
    if health.get("dna_exhausted") or decision.get("pipeline_status") == "DNA_EXHAUSTED":
        return "DNA_EXHAUSTED"
    # After the finished horizons (those need a new release anyway), before the
    # soft warnings that would otherwise hide it for the last 48 hours of a release.
    if health.get("orders_blocked_by_release"):
        return "RELEASE_UNAUTHORIZED"
    if health.get("release_expiring"):
        return "RELEASE_EXPIRING"
    if health.get("token_warning"):
        return "TOKEN_EXPIRY_WARNING"
    if health.get("dna_low"):
        return "DNA_LOW"
    remaining = decision.get("dna_steps_remaining")
    if type(remaining) is int and 0 <= remaining <= 10:
        return "DNA_LOW"
    return decision.get("pipeline_status") or body.get("pipeline_status", "UNKNOWN")


ERROR_STATUSES = frozenset({
    "ERROR", "FEE_OVERDUE", "MANUAL_RECONCILIATION_REQUIRED", "BROKER_REJECT_HALT",
    "BROKER_ORDER_FAILED", "TOKEN_PREFLIGHT_BLOCKED", "EXECUTION_LIMIT_BLOCKED",
    "RECONCILIATION_OVERDUE", "RELEASE_UNAUTHORIZED"})
WARNING_STATUSES = frozenset({
    "AUTH_BACKOFF", "OPERATOR_HALT", "WAITING_BROKER_FEE", "WAITING_RECONCILIATION",
    "OUTBOX_RECOVERY_PENDING", "INTENT_BLOCKED", "DNA_LOW", "TOKEN_EXPIRY_WARNING",
    "RELEASE_EXPIRING", "TICK_DEFERRED", "OPEN_ORDER_BLOCKED"})
# A finished horizon is a steady state that repeats every minute until an operator
# deploys the next release. It is alerted by the horizon webhook and the
# horizon-policy (alerting.notify_tick, tools/monitoring_config), not by one
# WARNING line per tick: 2,748 of them in four days buried the real warnings.
NOTICE_STATUSES = frozenset({"RELEASE_EXPIRED", "DNA_EXHAUSTED"})


# A halted account must never be silent, yet a paused tick repeats every minute, so
# the steady state stays an INFO heartbeat. Only the first PAUSED_PAGE_SECONDS of each
# hour of halt age are ERROR: the page when the halt begins and an hourly reminder
# reach `severity>=ERROR` (the health policy of tools/monitoring_config.py). 2026-10-05
# a halt began at 17:09 and was INFO for 3.5 hours, including its first tick. Ticks come
# ~60 s apart with jitter, so the window is three ticks wide: with +-5 s jitter and 1% of
# ticks lost, a 60 s window missed a page or reminder in 85% of simulated 8-hour halts,
# 180 s in none of 4,000.
PAUSED_PAGE_SECONDS = 180
PAUSED_REMINDER_PERIOD_SECONDS = 3600


def severity_for(health: str, *, paused: bool = False,
                 halt_age_seconds: float | None = None) -> str:
    if paused and health == "MANUAL_RECONCILIATION_REQUIRED":
        if (halt_age_seconds is not None
                and max(0.0, halt_age_seconds) % PAUSED_REMINDER_PERIOD_SECONDS
                < PAUSED_PAGE_SECONDS):
            return "ERROR"
        return "INFO"
    if health in ERROR_STATUSES:
        return "ERROR"
    if health in WARNING_STATUSES:
        return "WARNING"
    if health in NOTICE_STATUSES:
        return "NOTICE"
    return "INFO"


def _halt_age_seconds(halt_since, now: datetime) -> float:
    """Seconds since the halt began. Unreadable means unknown, and unknown pages (0)."""
    try:
        since = datetime.fromisoformat(str(halt_since).replace("Z", "+00:00"))
        if since.tzinfo is None:
            return 0.0
        return max(0.0, (now - since).total_seconds())
    except (TypeError, ValueError):
        return 0.0


def emit_tick(body: dict, code: int, *, request=None, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    health = business_status(body, code)
    body["business_status"] = health
    decision = body.get("decision") or {}
    phases = [body.get("recovery") or {}, body.get("dispatch") or {}]
    paused = any(phase.get("reconciliation_paused") for phase in phases)
    halt_since = next((phase.get("halt_since") for phase in phases if phase.get("halt_since")), None)
    event = {
        "event": "lego_tick_completed", "timestamp": now.isoformat(),
        "severity": severity_for(health, paused=paused,
                                 halt_age_seconds=_halt_age_seconds(halt_since, now) if paused else None),
        "revision": os.environ.get("K_REVISION"),
        "candidate_hash": os.environ.get("LEGO_CANDIDATE_HASH"),
        "correlation_id": body.get("correlation_id"), "http_status": code,
        "pipeline_status": body.get("pipeline_status"), "business_status": health,
        "duration_ms": body.get("duration_ms"), "environment": body.get("environment"),
        "mode": body.get("mode"), "active": body.get("active"),
        "decision": {key: decision.get(key) for key in
                     ("run_id", "market_slot_id", "step", "status", "pipeline_status", "committed",
                      "dna_steps_remaining")},
        "operational_health": body.get("operational_health") or {},
        "open_order_blockers": [{key: phase.get(key) for key in
                                  ("open_order_count", "open_order_observed_at", "open_order_fingerprints")}
                                 for phase in phases if phase.get("open_order_blocked")],
        "execution": [],
        "reconciliation_paused": paused,
        "halt_since": halt_since,
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
                                             "order_contract_anomaly",
                                             "broker_reject_halted", "token_preflight_blocked",
                                             "execution_limit_blocked", "operator_halt_blocked",
                                             "needs_manual_check",
                                             "reconciliation_overdue", "reconciliation_age_seconds",
                                             "cancel_requested_at", "cancel_confirmed_at", "cancel_attempt_count",
                                             "cancel_last_error_code", "cancel_refused_at",
                                             "expiry_released", "expiry_released_at",
                                             "expiry_proof_blockers", "expiry_proof_checked_at",
                                             "open_order_count", "open_order_observed_at", "open_order_fingerprints")}})
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
