"""Thin Cloud Functions HTTP boundary and backward-compatible test facade."""

import logging
import math
import os
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone

import firebase_admin
import functions_framework
from firebase_admin import credentials, db

from lego_archive import archive_terminal_records
from lego_one_row import (READY_BUY, READY_SELL, Config, DNAExhausted, ExecutionFill,
                          HoldingsAnomaly, build_decision,
                          check_holdings_continuity, compute_row, dna_step_for,
                          dna_steps_remaining, position_vanished)
from lego_orders import (TERMINAL_STATUSES, UAT, evaluate_submit_gate,
                         normalize_status, order_confirmation_phrase,
                         summarize_order_result)
from lego_outbox import (TERMINAL as OUTBOX_TERMINAL,
                         account_symbol_fence_key, begin_place_attempt,
                         claim_chain_dispatch, claim_intent,
                         clear_chain_dispatch_inflight, expire_unsent_before,
                         fence_chain_dispatch, list_actionable,
                         list_audit_pending, put_intent, read_committed_row,
                         read_intent, release_chain_dispatch,
                         release_intent_claim, update_intent)
from lego_preflight import DEFAULT_MIN_DNA_REMAINING, auto_submit_preflight
from lego_state import (CASHFLOW_SEMANTICS, CalendarDriftError,
                         CashflowSemanticsDowngrade, DNADriftError,
                         ExecutionFinalizeError, OrdinalRegression,
                         RuntimeIdentityError, SlotAlreadyConsumed,
                         StaleAnchorError, apply_realized_fill, chain_key,
                         chain_runtime_identity_is_verified, commit_final_row,
                         finalize_execution_fill, mark_order_intent_materialized,
                         pending_order_intents, read_anchor, read_chain_state,
                         repair_pending_intent_row, UNREAD_STATE,
                         update_order_audit,
                         verify_cashflow_semantics, verify_runtime_identity,
                         write_order_audit)
from market_clock import (MarketClockError, clock_mode, fallback_slot_id,
                          calendar_fingerprint, is_regular_session,
                          resolve_dna_step, resolve_market_slot,
                          slot_seconds)
from webull_io import (IncompleteOpenOrdersError, build_clients,
                        build_order_payload, environment_label,
                        fetch_holdings, fetch_open_orders, fetch_order_detail,
                        fetch_snapshot, is_transient_exception, load_config,
                        market_category, place_market_order,
                        preview_market_order, redact_sensitive_text,
                        runtime_identity_fingerprint, token_health)

logger = logging.getLogger(__name__)

ORDER_POLL_ATTEMPTS = 3
ORDER_POLL_DELAY_S = 2.0
UTC = timezone.utc
WARNINGS_PATH = "webull_lego_warnings"
ERRORS_PATH = "webull_lego_errors"
# Non-terminal: the broker has answered with a fill, but the position has not
# caught up yet, so the model ledger cannot be finalized on this tick. Kept out
# of lego_outbox.TERMINAL on purpose — the intent has to come back — and bounded
# below so an account whose position feed never moves cannot hold the queue.
AWAITING_FILL_CONFIRMATION = "AWAITING_FILL_CONFIRMATION"
AWAITING_BROKER_FEE = "AWAITING_BROKER_FEE"
DEFAULT_FILL_CONFIRM_MAX_ATTEMPTS = 5
DEFAULT_MAX_DISPATCH_PRICE_DRIFT_BPS = 100.0
DEFAULT_MAX_DISPATCH_QUOTE_AGE_SECONDS = 360.0
# A broker timestamp a fraction ahead of the worker can be ordinary clock skew.
# Anything farther ahead is not evidence about a quote that exists yet.
MAX_DISPATCH_FUTURE_SKEW_SECONDS = 5.0
RECONCILE_STATUSES = {
    "PLACING_UNKNOWN", "PLACING", "SUBMITTED", "UNKNOWN",
    "PARTIAL_FILLED", "PARTIALLY_FILLED", AWAITING_FILL_CONFIRMATION,
    AWAITING_BROKER_FEE,
}
# These statuses leave either the broker result or a strategy ledger requiring
# manual repair. They are queue-terminal to prevent retry churn, but never safe
# evidence for releasing the chain's money fence automatically.
MANUAL_CHAIN_TERMINAL = {
    "RECONCILE_ABANDONED", "CASHFLOW_FINALIZE_ERROR", "REALIZED_MATH_ERROR",
}
# These are produced only before the irreversible broker call.  They can clear
# without broker fill evidence, but only while the durable intent also says no
# place attempt ever started.
UNSENT_CHAIN_TERMINAL = {
    "EXPIRED_UNSENT", "SUPPRESSED_ACTIVE_ORDER", "SUPPRESSED_STATE_CHANGED",
    "NOT_PLACED", "UNSENT_ABORTED",
}



import sys

import decision_service
import execution_service
import archive_service
from config import ConfigurationError, load_runtime_config

FillNotConfirmed = execution_service.FillNotConfirmed
RealizedMathError = execution_service.RealizedMathError

def _facade():
    return sys.modules[__name__]


def _execution_call(name, *args, **kwargs):
    execution_service.configure(_facade())
    return getattr(execution_service, name)(*args, **kwargs)


def _init_firebase(*args, **kwargs):
    return _execution_call('_init_firebase', *args, **kwargs)


def _iso(*args, **kwargs):
    return _execution_call('_iso', *args, **kwargs)


def _error_text(*args, **kwargs):
    return _execution_call('_error_text', *args, **kwargs)


def _poll_order_status(*args, **kwargs):
    return _execution_call('_poll_order_status', *args, **kwargs)


def _record_warning(*args, **kwargs):
    return _execution_call('_record_warning', *args, **kwargs)


def _apply_realized_if_available(*args, **kwargs):
    return _execution_call('_apply_realized_if_available', *args, **kwargs)


def _persist(*args, **kwargs):
    return _execution_call('_persist', *args, **kwargs)


def _mirror_order_audit(*args, **kwargs):
    return _execution_call('_mirror_order_audit', *args, **kwargs)


def _repair_pending_audits(*args, **kwargs):
    return _execution_call('_repair_pending_audits', *args, **kwargs)


def _announce_identity_adoption(*args, **kwargs):
    return _execution_call('_announce_identity_adoption', *args, **kwargs)


def _recover_pending_order_intents(*args, **kwargs):
    return _execution_call('_recover_pending_order_intents', *args, **kwargs)


def _persist_summary(*args, **kwargs):
    return _execution_call('_persist_summary', *args, **kwargs)


def _persist_error(*args, **kwargs):
    return _execution_call('_persist_error', *args, **kwargs)


def _reconcile_max_attempts(*args, **kwargs):
    return _execution_call('_reconcile_max_attempts', *args, **kwargs)


def _min_dna_remaining(*args, **kwargs):
    return _execution_call('_min_dna_remaining', *args, **kwargs)


def _persist_reconcile_failure(*args, **kwargs):
    return _execution_call('_persist_reconcile_failure', *args, **kwargs)


def _persist_realized_math_error(*args, **kwargs):
    return _execution_call('_persist_realized_math_error', *args, **kwargs)


def _holdings_drift_tolerance(*args, **kwargs):
    return _execution_call('_holdings_drift_tolerance', *args, **kwargs)


def _fill_confirm_max_attempts(*args, **kwargs):
    return _execution_call('_fill_confirm_max_attempts', *args, **kwargs)


def _positive_float(*args, **kwargs):
    return _execution_call('_positive_float', *args, **kwargs)


def _nonnegative_float(*args, **kwargs):
    return _execution_call('_nonnegative_float', *args, **kwargs)


def _chain_fence_can_clear(*args, **kwargs):
    return _execution_call('_chain_fence_can_clear', *args, **kwargs)


def _holdings_moved(*args, **kwargs):
    return _execution_call('_holdings_moved', *args, **kwargs)


def _finalize_model_ledger(*args, **kwargs):
    return _execution_call('_finalize_model_ledger', *args, **kwargs)


def _defer_fill_confirmation(*args, **kwargs):
    return _execution_call('_defer_fill_confirmation', *args, **kwargs)


def _persist_cashflow_error(*args, **kwargs):
    return _execution_call('_persist_cashflow_error', *args, **kwargs)


def _finish_with_realized(*args, **kwargs):
    return _execution_call('_finish_with_realized', *args, **kwargs)


def _pending_row_shape(*args, **kwargs):
    return _execution_call('_pending_row_shape', *args, **kwargs)


def _committed_row_shape(*args, **kwargs):
    return _execution_call('_committed_row_shape', *args, **kwargs)


def _stop(*args, **kwargs):
    return _execution_call('_stop', *args, **kwargs)


def _nonnegative_finite_env(*args, **kwargs):
    return _execution_call('_nonnegative_finite_env', *args, **kwargs)


def _parse_utc(*args, **kwargs):
    return _execution_call('_parse_utc', *args, **kwargs)


def _dispatch_quote_safety(*args, **kwargs):
    return _execution_call('_dispatch_quote_safety', *args, **kwargs)


def _reject_unsafe_dispatch_quote(*args, **kwargs):
    return _execution_call('_reject_unsafe_dispatch_quote', *args, **kwargs)


def _dispatch_or_reconcile_one(*args, **kwargs):
    return _execution_call('_dispatch_or_reconcile_one', *args, **kwargs)


def _run_order_worker(*args, **kwargs):
    return _execution_call('_run_order_worker', *args, **kwargs)


def _outbox_intent(*args, **kwargs):
    decision_service.configure(_facade())
    return decision_service._outbox_intent(*args, **kwargs)


def lego_one_row(request):
    decision_service.configure(_facade())
    return decision_service.run_decision(request)


def lego_order_worker(request):
    return execution_service.run_http(request, _facade())


def lego_archive_worker(request):
    return archive_service.run_archive(_facade())


def _untrusted_overrides(request) -> list[str]:
    """Names a caller tried to use to escape the deployment-bound identity."""
    try:
        payload = request.get_json(silent=True) if request is not None else None
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        return []
    forbidden = {
        "environment", "host", "endpoint", "account_id", "database_url",
        "project_id", "release_authorization", "symbol",
    }
    return sorted(forbidden.intersection(payload))


@functions_framework.http
def lego_tick(request):
    """The only deployed HTTP entrypoint: recover, decide, dispatch, housekeep."""
    started = time.monotonic()
    correlation_id = uuid.uuid4().hex
    overrides = _untrusted_overrides(request)
    if overrides:
        return {
            "pipeline_status": "UNTRUSTED_REQUEST_OVERRIDE",
            "correlation_id": correlation_id,
            "rejected_fields": overrides,
        }, 400
    try:
        runtime = load_runtime_config()
        execution_service._init_firebase()
        # The immutable DNA bundle is the clock source of truth. Environment
        # variables remain an internal compatibility boundary for market_clock,
        # not extra operator settings.
        os.environ["LEGO_SLOT_SECONDS"] = str(
            runtime.operator.dna_bundle.interval_seconds)
        if runtime.operator.dna_bundle.origin_utc:
            os.environ["LEGO_DNA_ORIGIN_UTC"] = runtime.operator.dna_bundle.origin_utc
        expected_calendar = runtime.operator.dna_bundle.calendar_fingerprint
        if expected_calendar and expected_calendar != calendar_fingerprint():
            raise ConfigurationError(
                "dna_bundle.calendar_fingerprint ไม่ตรงกับ deployed market clock")
        cfg = Config(
            symbol=runtime.operator.symbol,
            fix_c=runtime.operator.principal_usd,
            diff=runtime.operator.diff_usd,
            dna_code=runtime.operator.dna_bundle.dna_code,
            strategy_id="shannon_demon_lego_v2",
        )
        runtime_identity = runtime_identity_fingerprint()
    except (ConfigurationError, KeyError, ValueError) as exc:
        return {
            "pipeline_status": "CONFIG_ERROR",
            "correlation_id": correlation_id,
            "error": _error_text(exc, with_type=False),
        }, 500

    # Recovery is intentionally first and independent of active/mode. An
    # already-attempted order can move money while the strategy is paused.
    try:
        recovery = execution_service._run_order_worker(
            cfg, limit=3, runtime_identity=runtime_identity, runtime=runtime)
    except Exception as exc:
        return {
            "pipeline_status": "RECOVERY_ERROR",
            "correlation_id": correlation_id,
            "error": _error_text(exc),
        }, 503

    decision, decision_code = decision_service.run_decision(
        request, runtime=runtime, cfg_override=cfg)
    dispatch = None
    if decision_code < 500 and runtime.allows_new_broker_mutation:
        try:
            dispatch = execution_service._run_order_worker(
                cfg, limit=1, runtime_identity=runtime_identity, runtime=runtime)
        except Exception as exc:
            dispatch = {"pipeline_status": "ORDER_WORKER_ERROR", "error": _error_text(exc)}

    archive = None
    elapsed = time.monotonic() - started
    if elapsed < 30:
        try:
            archive = archive_terminal_records(
                days=30, limit=500,
                chain_key_=chain_key(cfg),
                dispatch_key_=account_symbol_fence_key(
                    runtime_identity, cfg.symbol))
        except Exception as exc:
            archive = {"status": "ARCHIVE_DEFERRED", "error": _error_text(exc)}

    return {
        "pipeline_status": "TICK_OK" if decision_code < 500 else "TICK_DECISION_ERROR",
        "correlation_id": correlation_id,
        "environment": runtime.deployment.environment,
        "mode": runtime.operator.mode,
        "active": runtime.operator.active,
        "new_mutations_authorized": runtime.allows_new_broker_mutation,
        "recovery": recovery,
        "decision": decision,
        "dispatch": dispatch,
        "archive": archive,
        "duration_ms": round((time.monotonic() - started) * 1000, 3),
    }, decision_code
