"""Decision orchestration: snapshot to atomic row and recoverable intent."""

import logging
import tick_runtime
import math
import os
import time
import traceback
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import firebase_admin
import functions_framework
from firebase_admin import credentials, db

from lego_archive import archive_terminal_records
from lego_one_row import (READY_BUY, READY_SELL, DNAExhausted, ExecutionFill,
                          HoldingsAnomaly, build_decision,
                          check_holdings_continuity, compute_row, dna_step_for,
                          dna_steps_remaining, position_vanished)
from lego_orders import (TERMINAL_STATUSES, UAT, evaluate_submit_gate,
                         normalize_status, order_confirmation_phrase,
                         summarize_order_result)
from lego_outbox import (TERMINAL as OUTBOX_TERMINAL, begin_place_attempt,
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
from lego_state import cashflow_semantics_for
from market_clock import (MarketClockError, clock_mode, fallback_slot_id,
                          is_regular_session, resolve_dna_step, resolve_market_slot,
                          slot_seconds)
from webull_io import (IncompleteOpenOrdersError, build_clients,
                        build_order_payload, environment_label,
                        fetch_holdings, fetch_open_orders, fetch_order_detail,
                        fetch_snapshot, is_transient_exception, load_config,
                        market_category, place_market_order,
                        preview_market_order, redact_sensitive_text,
                        runtime_identity_fingerprint, token_health)
from webull_io import broker_error_details, fetch_instrument_capability
from config import RuntimeConfig
from execution_service import (_announce_identity_adoption, _error_text,
                               _init_firebase, _iso, _min_dna_remaining,
                               _record_warning, _recover_pending_order_intents,
                               _run_order_worker)

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
DEFAULT_FILL_CONFIRM_MAX_ATTEMPTS = 5
DEFAULT_MAX_DISPATCH_PRICE_DRIFT_BPS = 100.0
DEFAULT_MAX_DISPATCH_QUOTE_AGE_SECONDS = 360.0
# A broker timestamp a fraction ahead of the worker can be ordinary clock skew.
# Anything farther ahead is not evidence about a quote that exists yet.
MAX_DISPATCH_FUTURE_SKEW_SECONDS = 5.0
RECONCILE_STATUSES = {
    "PLACING_UNKNOWN", "PLACING", "SUBMITTED", "UNKNOWN",
    "PARTIAL_FILLED", "PARTIALLY_FILLED", AWAITING_FILL_CONFIRMATION,
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
    "NOT_PLACED",
}



_DECISION_PRIVATE_DEPS = [
    '_announce_identity_adoption', '_error_text', '_init_firebase',
    '_iso', '_min_dna_remaining', '_record_warning',
    '_recover_pending_order_intents', '_run_order_worker',
]
_INJECTABLE = ['AWAITING_FILL_CONFIRMATION', 'CASHFLOW_SEMANTICS', 'CalendarDriftError', 'CashflowSemanticsDowngrade', 'DEFAULT_FILL_CONFIRM_MAX_ATTEMPTS', 'DEFAULT_MAX_DISPATCH_PRICE_DRIFT_BPS', 'DEFAULT_MAX_DISPATCH_QUOTE_AGE_SECONDS', 'DEFAULT_MIN_DNA_REMAINING', 'DNADriftError', 'DNAExhausted', 'ERRORS_PATH', 'ExecutionFill', 'ExecutionFinalizeError', 'HoldingsAnomaly', 'IncompleteOpenOrdersError', 'MANUAL_CHAIN_TERMINAL', 'MAX_DISPATCH_FUTURE_SKEW_SECONDS', 'MarketClockError', 'ORDER_POLL_ATTEMPTS', 'ORDER_POLL_DELAY_S', 'OUTBOX_TERMINAL', 'OrdinalRegression', 'READY_BUY', 'READY_SELL', 'RECONCILE_STATUSES', 'RuntimeIdentityError', 'SlotAlreadyConsumed', 'StaleAnchorError', 'TERMINAL_STATUSES', 'UAT', 'UNREAD_STATE', 'UNSENT_CHAIN_TERMINAL', 'UTC', 'WARNINGS_PATH', 'apply_realized_fill', 'archive_terminal_records', 'auto_submit_preflight', 'begin_place_attempt', 'build_clients', 'build_decision', 'build_order_payload', 'chain_key', 'chain_runtime_identity_is_verified', 'check_holdings_continuity', 'claim_chain_dispatch', 'claim_intent', 'clear_chain_dispatch_inflight', 'clock_mode', 'commit_final_row', 'compute_row', 'credentials', 'datetime', 'db', 'dna_step_for', 'dna_steps_remaining', 'environment_label', 'evaluate_submit_gate', 'expire_unsent_before', 'fallback_slot_id', 'fence_chain_dispatch', 'fetch_holdings', 'fetch_open_orders', 'fetch_order_detail', 'fetch_snapshot', 'finalize_execution_fill', 'firebase_admin', 'functions_framework', 'is_regular_session', 'is_transient_exception', 'list_actionable', 'list_audit_pending', 'load_config', 'logger', 'logging', 'mark_order_intent_materialized', 'market_category', 'math', 'normalize_status', 'order_confirmation_phrase', 'os', 'pending_order_intents', 'place_market_order', 'position_vanished', 'preview_market_order', 'put_intent', 'read_anchor', 'read_chain_state', 'read_committed_row', 'read_intent', 'redact_sensitive_text', 'release_chain_dispatch', 'release_intent_claim', 'repair_pending_intent_row', 'resolve_dna_step', 'resolve_market_slot', 'runtime_identity_fingerprint', 'slot_seconds', 'summarize_order_result', 'time', 'timedelta', 'timezone', 'token_health', 'traceback', 'update_intent', 'update_order_audit', 'uuid', 'verify_cashflow_semantics', 'verify_runtime_identity', 'write_order_audit'] + _DECISION_PRIVATE_DEPS

def configure(deps) -> None:
    """Inject composition-root dependencies without importing main."""
    target = globals()
    for name in _INJECTABLE:
        if hasattr(deps, name):
            target[name] = getattr(deps, name)


def _outbox_intent(cfg, row: dict, snapshot: dict, slot, decision_time: datetime,
                   *, typed_v2: bool = False) -> dict:
    margin = (15 if typed_v2 else int(os.environ.get(
        "LEGO_ORDER_EXPIRY_MARGIN_SECONDS", "15")))
    expires_at = max(slot.slot_start_utc, slot.slot_end_utc - timedelta(seconds=margin))
    return {
        "status": "PENDING_DISPATCH",
        "row_status": row["สถานะ"], "side": row["_meta"]["side"],
        "quantity": row["_meta"]["quantity"], "symbol": cfg.symbol,
        "step": row["DNA step"], "signal": row["DNA signal"],
        "decision_price": snapshot["price"],
        "decision_holdings": snapshot["holdings"],
        "decision_time": _iso(decision_time),
        "created_at": snapshot["captured_at"],
        "slot_id": slot.slot_id,
        "slot_start_utc": _iso(slot.slot_start_utc),
        "slot_end_utc": _iso(slot.slot_end_utc),
        "expires_at": _iso(expires_at),
        "strategy_config": {
            "symbol": cfg.symbol,
            "fix_c": cfg.fix_c,
            "diff": cfg.diff,
            "dna_code": cfg.dna_code,
            "strategy_id": cfg.strategy_id,
            "decimal_precision": cfg.decimal_precision,
            "quantity_increment": cfg.quantity_increment,
        },
    }


def run_decision(request, runtime: RuntimeConfig | None = None, cfg_override=None):
    """Commit the current model slot first. Order failures never block DNA time."""
    _init_firebase()
    decision_time = datetime.now(UTC)

    try:
        # Mandatory: the slot grid must match the timeframe the DNA was trained
        # on, and the clock mode decides which step the row gets. A missing or
        # unsupported value is a deploy error, not a runtime one, so all three are
        # resolved up front instead of surfacing later as an engine failure —
        # the market category included, since a typo there reaches the broker as
        # a query parameter and comes back as an unhelpful snapshot error.
        cfg = cfg_override if cfg_override is not None else load_config()
        slot_seconds()
        mode = clock_mode()
        market_category()
        env = environment_label()
        runtime_identity = runtime_identity_fingerprint()
    except (KeyError, MarketClockError, ValueError) as exc:
        return {"status": "CONFIG_ERROR", "committed": False,
                "pipeline_status": "CONFIG_ERROR",
                "error": _error_text(exc, with_type=False)}, 500

    try:
        # Recovery and the anchor read both need this chain's state, so it is
        # fetched once here and passed to both instead of once each.
        state = read_chain_state(cfg)
        _announce_identity_adoption(
            verify_runtime_identity(state, runtime_identity), runtime_identity)
        _recover_pending_order_intents(cfg, runtime_identity, state=state)
    except RuntimeIdentityError as exc:
        return {"status": "CONFIG_ERROR", "committed": False,
                "pipeline_status": "CONFIG_ERROR",
                "error": _error_text(exc, with_type=False)}, 500

    # One calendar for every path: the same session rules the ordinal uses, so a
    # holiday or early close also blocks a degraded (clock-less) commit.
    if not is_regular_session(decision_time):
        return {"status": "PASS_MARKET_CLOSED", "committed": False,
                "pipeline_status": "MARKET_CLOSED"}, 200

    try:
        # Typed v2 chain identity excludes the operational lot size. A consumed
        # slot therefore needs no broker capability/position/quote request.
        # Recovery above and the tick's dispatch phase still process its intent.
        if runtime is not None:
            from lego_state import consumed_slot_state
            existing_slot = resolve_market_slot(decision_time)
            if existing_slot is not None:
                consumed = consumed_slot_state(
                    cfg, existing_slot.slot_id,
                    runtime_identity=runtime_identity, state=state)
                if consumed is not None:
                    return {
                        "status": "PASS_SLOT_CONSUMED", "committed": False,
                        "pipeline_status": "SLOT_CONSUMED",
                        "run_id": consumed.get("last_run_id"),
                        "market_slot_id": existing_slot.slot_id,
                        "step": consumed["dna_step"],
                        "dna_steps_remaining": dna_steps_remaining(
                            cfg.dna_code, consumed["dna_step"]),
                    }, 200

        # Resolve the broker's quantity contract before any model anchor is
        # interpreted for this decision. v2 chain identity deliberately excludes
        # this operational capability, while the exact resolved values are
        # snapshotted into the intent for cross-revision recovery.
        trade_client, data_client = build_clients()
        if runtime is not None:
            capability = fetch_instrument_capability(trade_client, cfg.symbol)
            cfg = replace(
                cfg,
                quantity_increment=float(capability.quantity_increment),
                # Broker decimals may be padded (1.00000000 is one share).
                # Count significant fractional places, not transport padding;
                # Config still rejects genuinely unsupported precision > 5.
                decimal_precision=max(
                    0, -capability.quantity_increment.normalize().as_tuple().exponent),
            )
        # Before the model is touched: if this revision's accounting is behind
        # the chain's, nothing it computes afterwards is worth writing. Existing
        # execution ledgers are retained; only pre-execution ledgers reset.
        running_semantics = cashflow_semantics_for(cfg)
        semantics_migrated_from = verify_cashflow_semantics(state, running_semantics)
        if semantics_migrated_from:
            stored_execution = (state or {}).get("execution_cashflow") or {}
            preserved = stored_execution.get("last_action_price") is not None
            baseline_note = (
                "คง execution baseline เดิม; การแก้ประวัติต้องผ่าน reconciliation"
                if preserved else "รีเซ็ต baseline Aₙ เป็น 0 สำหรับ ledger เดิมก่อนแยก execution")
            _record_warning(
                "cashflow_semantics_migration",
                f"chain เคยใช้ cashflow semantics '{semantics_migrated_from}' "
                f"แต่ runtime นี้เป็น '{running_semantics}' — {baseline_note}",
                {"from": semantics_migrated_from, "to": running_semantics,
                 "baseline_action": "preserved" if preserved else "reset",
                 "chain_key": chain_key(cfg)})
        anchor = read_anchor(cfg, runtime_identity=runtime_identity, state=state)
        legacy_step = dna_step_for(anchor)
        slot = None
        clock_error = None
        try:
            slot = resolve_market_slot(decision_time)
            if slot is None:
                return {"status": "PASS_MARKET_CLOSED", "committed": False,
                        "pipeline_status": "MARKET_CLOSED"}, 200
            effective_step, alignment_error = resolve_dna_step(legacy_step, slot)
        except MarketClockError as exc:
            if mode == "market":
                raise
            effective_step, alignment_error = legacy_step, None
            clock_error = str(exc)

        # The token dies of old age silently: nothing in the SDK renews it, and
        # recovery needs a human to approve 2FA within 300 seconds. build_clients
        # refreshes it while it is still valid; this reports what is left so the
        # cases it cannot fix by itself — an ephemeral token dir, a token already
        # gone — are visible days before they stop the chain.
        health = token_health()
        token_warning = None if health["ok"] else "; ".join(health["reasons"])
        if token_warning:
            _record_warning("webull_token", token_warning, {
                k: v for k, v in health.items()
                if k in ("token_dir", "ephemeral_token_dir", "status",
                         "expires_at", "days_left") and v is not None})
        snapshot = fetch_snapshot(trade_client, data_client, cfg)
        # Reaching this line is live proof that the token can sign a trade
        # request: fetch_snapshot goes through account_v2.get_account_position on
        # the very same authenticated ApiClient that place_order will use, and any
        # auth failure would have left this block by exception instead. Recorded
        # here, next to the call that earns it, so a later reordering cannot leave
        # the claim standing without the evidence behind it.
        token_proved_live = True
        # Before the row exists: a snapshot that lost the position would make
        # gap = fix_c, the largest order possible, and committing it would also
        # write prev_holdings = 0 and disarm this check for every commit after.
        if (runtime is not None
                or os.environ.get(
                    "LEGO_ALLOW_ZERO_HOLDINGS", "false").lower() != "true"):
            check_holdings_continuity(anchor, float(snapshot["holdings"]))
        if slot:
            slot_id = slot.slot_id
        else:
            # Degraded clock still gets a one-commit-per-slot key so a scheduler
            # retry cannot consume the same slot twice.
            slot_id = fallback_slot_id(snapshot["captured_at"])
            mode = f"{mode}:degraded"
        row = compute_row(cfg, snapshot, anchor, dna_step=effective_step)

        # The deployed v2 entrypoint supplies the typed runtime and therefore
        # has one explicit gate. Calling this legacy handler directly keeps the
        # compatibility behaviour for migration tests, but it is no longer a
        # deployed function.
        auto = (
            runtime.allows_new_broker_mutation
            if runtime is not None
            else os.environ.get("AUTO_SUBMIT", "false").lower() == "true"
        )
        # Warn while extending the DNA is still possible; also one of the
        # preflight inputs, so it is resolved once and read twice.
        remaining = dna_steps_remaining(cfg.dna_code, row["DNA step"])
        outbox_error = None
        outbox_skipped = None
        outbox_blocked = None
        preflight = None
        pending_intent = None

        # Evaluate a candidate before the state transaction so the exact payload
        # can be stored in that same transaction.  row_durable=True here means
        # "activate only if commit succeeds"; no broker/outbox write happens yet.
        if auto and row["สถานะ"] in (READY_BUY, READY_SELL):
            preflight = auto_submit_preflight(
                auto_submit=auto, environment=env, row=row, row_durable=True,
                slot=slot, token=health, dna_remaining=remaining,
                min_dna_remaining=_min_dna_remaining(
                    typed_v2=runtime is not None),
                token_proved_live=token_proved_live,
                **({"release_authorized": runtime.allows_new_broker_mutation}
                   if runtime is not None else {}))
            if preflight["ok"]:
                pending_intent = _outbox_intent(
                    cfg, row, snapshot, slot, decision_time,
                    typed_v2=runtime is not None)
                pending_intent["runtime_identity_fingerprint"] = runtime_identity

        # A rejected commit leaves no outbox candidate.  A successful state
        # transaction carries a deterministic recovery marker, closing the crash
        # window between advancing DNA and materializing the private outbox.
        result = commit_final_row(
            cfg, snapshot, anchor, row, slot_id=slot_id, clock_mode=mode,
            market_ordinal=None if slot is None else slot.market_ordinal,
            runtime_identity=runtime_identity,
            pending_intent=pending_intent)

        if preflight is None:
            pass                                    # nothing to submit this slot
        elif preflight["ok"]:
            try:
                put_intent(chain_key(cfg), result["run_id"], pending_intent)
                mark_order_intent_materialized(
                    cfg, result["run_id"], runtime_identity=runtime_identity)
            except Exception as exc:
                outbox_error = _error_text(exc)
                _record_warning(
                    "outbox_recovery",
                    "committed row ยัง materialize เข้า outbox ไม่สำเร็จ — "
                    "marker ยังอยู่และจะลองใหม่",
                    {"run_id": result["run_id"],
                     "error_type": type(exc).__name__},
                )
        else:
            # Blocked, and said out loud on both channels. Deciding this silently
            # was the worst of both worlds: the row still read READY_BUY with
            # committed=true and no error field, so a dashboard showed a working
            # bot that had never sent a single order.
            message = preflight["message"]
            if preflight["field"] == "outbox_skipped":
                outbox_skipped = message
            else:
                outbox_blocked = message
            extra = {"run_id": result["run_id"], "row_status": row["สถานะ"],
                     "clock_mode": mode, "blocked_by": preflight["blocked_by"]}
            if preflight["hint"]:
                extra["hint"] = preflight["hint"]
            _record_warning(preflight["warning_kind"], message, extra)

        out = {
            "status": row["สถานะ"], "committed": result["committed"],
            "idempotent": result.get("idempotent", False),
            "run_id": result["run_id"], "version": result.get("version"),
            "step": row["DNA step"], "signal": row["DNA signal"],
            "model_acted": row["_meta"]["acted"],
            "pipeline_status": "ROW_COMMITTED",
            # The running accounting, on every successful row. Comparing the
            # deployed revision against main used to need Cloud Run access; this
            # is the same answer from the response Cloud Scheduler already calls.
            "cashflow_semantics": cashflow_semantics_for(cfg),
            "clock_mode": mode,
            "legacy_step": legacy_step,
            "market_step": None if slot is None else slot.market_ordinal,
            "alignment_error": alignment_error,
            "market_slot_id": None if slot is None else slot.slot_id,
        }
        if semantics_migrated_from:
            out["cashflow_semantics_migrated_from"] = semantics_migrated_from
        if clock_error:
            out["clock_warning"] = clock_error
        if token_warning:
            out["token_warning"] = token_warning
        if outbox_error:
            out["outbox_error"] = outbox_error
        if outbox_skipped:
            out["outbox_skipped"] = outbox_skipped
        if outbox_blocked:
            out["outbox_blocked"] = outbox_blocked
            out["outbox_blocked_checks"] = preflight["blocked_by"]
        # The deployed typed runtime reports headroom on every slot for alerts.
        # Keep the legacy response shape for non-deployed migration consumers.
        if runtime is not None or remaining <= int(
                os.environ.get("LEGO_DNA_LOW_WATERMARK", "10")):
            out["dna_steps_remaining"] = remaining
        # One line per slot in Cloud Logging. The response body says all of this
        # already, but nothing reads it: the caller is Cloud Scheduler, which
        # keeps the status code and throws the body away. Without this a chain
        # deciding READY_SELL every slot and sending nothing looks exactly like a
        # chain trading normally — 200, no error, no output.
        logger.info(
            "lego_one_row slot=%s step=%s status=%s committed=%s "
            "order_intent=%s blocked_by=%s",
            out.get("market_slot_id"), row["DNA step"], row["สถานะ"],
            result["committed"],
            "created" if (preflight and preflight["ok"] and not outbox_error)
            else "none",
            (preflight or {}).get("blocked_by") or [])

        # Off by default: dispatching inline adds broker latency to the DNA
        # invocation, which raises the odds of a scheduler timeout+retry.
        if (runtime is None
                and os.environ.get(
                    "LEGO_INLINE_ORDER_WORKER", "false").lower() == "true"):
            try:
                out["order_worker"] = _run_order_worker(cfg, limit=1)
            except Exception as exc:
                out["order_worker"] = {
                    "processed": 0, "error": _error_text(exc)}
        return out, 200

    except RuntimeIdentityError as exc:
        return {"status": "CONFIG_ERROR", "committed": False,
                "pipeline_status": "CONFIG_ERROR",
                "error": _error_text(exc, with_type=False)}, 500
    except SlotAlreadyConsumed as exc:
        return {"status": "PASS_SLOT_CONSUMED", "committed": False,
                "pipeline_status": "SLOT_CONSUMED", "note": str(exc)}, 200
    except StaleAnchorError as exc:
        return {"status": "STALE_ANCHOR", "committed": False,
                "pipeline_status": "STALE_ANCHOR", "note": str(exc)}, 409
    except CalendarDriftError as exc:
        return {"status": "CALENDAR_DRIFT", "committed": False,
                "pipeline_status": "CALENDAR_DRIFT", "note": str(exc)}, 409
    except OrdinalRegression as exc:
        return {"status": "ORDINAL_REGRESSION", "committed": False,
                "pipeline_status": "ORDINAL_REGRESSION", "note": str(exc)}, 409
    except DNADriftError as exc:
        return {"status": "DNA_DRIFT", "committed": False,
                "pipeline_status": "DNA_DRIFT", "note": str(exc)}, 409
    except CashflowSemanticsDowngrade as exc:
        # 409 for the same reason as the drift guards: the request is fine, the
        # chain is fine, and this deployment is the thing that must not proceed.
        return {"status": "CASHFLOW_SEMANTICS_DOWNGRADE", "committed": False,
                "pipeline_status": "CASHFLOW_SEMANTICS_DOWNGRADE",
                "note": str(exc), "runtime_cashflow_semantics": cashflow_semantics_for(cfg),
                "hint": "ตรวจว่า Cloud Run revision ไหนยังรับ traffic อยู่ "
                        "และ scheduler ยิงไปที่ URL ของ revision ใด"}, 409
    except HoldingsAnomaly as exc:
        return {"status": "HOLDINGS_ANOMALY", "committed": False,
                "pipeline_status": "HOLDINGS_ANOMALY", "note": str(exc)}, 409
    except DNAExhausted as exc:
        # The DNA finishing is an expected end state, not a fault: bypass:100 on
        # a 30m grid lasts about eight trading days. Without this clause it fell
        # through to the generic handler and answered 500 on every slot forever,
        # pushing an error record each time and firing any 5xx alert with nothing
        # actionable in it. 200 with its own status says what happened and what
        # to do, and the scheduler stops treating it as an outage.
        return {"status": "PASS_DNA_EXHAUSTED", "committed": False,
                "pipeline_status": "DNA_EXHAUSTED", "note": str(exc),
                "hint": "ต่ออายุด้วย LEGO_DNA_CODE ที่ยาวขึ้น (chain ใหม่) "
                        "หรือหยุด scheduler ของ chain นี้"}, 200
    except Exception as exc:
        try:
            db.reference(ERRORS_PATH).push({
                "error": _error_text(exc, with_type=False),
                "type": type(exc).__name__,
                "broker_error": broker_error_details(exc),
                "trace": redact_sensitive_text(traceback.format_exc())[:2000],
                "at": datetime.now(UTC).isoformat(),
                "correlation_id": tick_runtime.correlation_id(),
            })
        except Exception:
            pass
        code = 503 if is_transient_exception(exc) else 500
        return {"status": "ERROR", "committed": False,
                "pipeline_status": "SNAPSHOT_OR_ENGINE_ERROR",
                "error": _error_text(exc, with_type=False),
                "type": type(exc).__name__,
                "broker_error": broker_error_details(exc)}, code

