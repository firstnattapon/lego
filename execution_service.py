"""Execution orchestration for durable broker dispatch and recovery."""

import logging
import tick_runtime
from lego_outbox import acknowledge_audit
import math
import os
import time
import traceback
import uuid
import broker_circuit
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
                         DISPATCH_LOCK_PATH, account_symbol_fence_key,
                         begin_place_attempt,
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
                         StaleAnchorError, apply_broker_cashflow,
                         apply_realized_fill, chain_key,
                         chain_runtime_identity_is_verified, commit_final_row,
                         finalize_execution_fill, mark_order_intent_materialized,
                         pending_order_intents, read_anchor, read_chain_state,
                         repair_pending_intent_row, UNREAD_STATE,
                         update_order_audit,
                         verify_cashflow_semantics, verify_runtime_identity,
                         write_order_audit)
from market_clock import (MarketClockError, clock_mode, fallback_slot_id,
                          is_regular_session, resolve_dna_step, resolve_market_slot,
                          slot_seconds)
from webull_io import (IncompleteOpenOrdersError, build_clients,
                        build_order_payload, canonical_payload_hash,
                        environment_label,
                        fetch_buying_power, fetch_holdings, fetch_open_orders, fetch_order_detail,
                        fetch_snapshot, is_transient_exception, load_config,
                        market_category, place_market_order,
                        preview_market_order, preview_market_order_result,
                        redact_sensitive_text,
                        runtime_identity_fingerprint, token_health)
from decimal import Decimal
from webull_io import (validate_order_detail_identity, validate_place_response,
                       validate_preview_funding, FractionalGateError,
                       InstrumentCapability, evaluate_fractional_order_gate,
                       fetch_instrument_capability, is_fractional_quantity)
from config import RuntimeConfig

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
REALIZED_MATCHING_PENDING = "REALIZED_MATCHING_PENDING"
DEFAULT_FILL_CONFIRM_MAX_ATTEMPTS = 5
DEFAULT_MAX_DISPATCH_PRICE_DRIFT_BPS = 100.0
DEFAULT_MAX_DISPATCH_QUOTE_AGE_SECONDS = 360.0
# Webull sandbox market data can be delayed by 15 minutes. This allowance is
# selected only by the v2 UAT deployment; it never extends decision lifetime.
UAT_QUOTE_DELAY_SECONDS = 900.0
# A broker timestamp a fraction ahead of the worker can be ordinary clock skew.
# Anything farther ahead is not evidence about a quote that exists yet.
MAX_DISPATCH_FUTURE_SKEW_SECONDS = 5.0
RECONCILE_STATUSES = {
    "PLACING_UNKNOWN", "PLACING", "PENDING", "SUBMITTED", "UNKNOWN",
    "PARTIAL_FILLED", "PARTIALLY_FILLED", AWAITING_FILL_CONFIRMATION,
    AWAITING_BROKER_FEE, REALIZED_MATCHING_PENDING,
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



_INJECTABLE = ['AWAITING_FILL_CONFIRMATION', 'CASHFLOW_SEMANTICS', 'CalendarDriftError', 'CashflowSemanticsDowngrade', 'DEFAULT_FILL_CONFIRM_MAX_ATTEMPTS', 'DEFAULT_MAX_DISPATCH_PRICE_DRIFT_BPS', 'DEFAULT_MAX_DISPATCH_QUOTE_AGE_SECONDS', 'DEFAULT_MIN_DNA_REMAINING', 'DNADriftError', 'DNAExhausted', 'ERRORS_PATH', 'ExecutionFill', 'ExecutionFinalizeError', 'HoldingsAnomaly', 'IncompleteOpenOrdersError', 'MANUAL_CHAIN_TERMINAL', 'MAX_DISPATCH_FUTURE_SKEW_SECONDS', 'MarketClockError', 'ORDER_POLL_ATTEMPTS', 'ORDER_POLL_DELAY_S', 'OUTBOX_TERMINAL', 'OrdinalRegression', 'READY_BUY', 'READY_SELL', 'RECONCILE_STATUSES', 'RuntimeIdentityError', 'SlotAlreadyConsumed', 'StaleAnchorError', 'TERMINAL_STATUSES', 'UAT', 'UNREAD_STATE', 'UNSENT_CHAIN_TERMINAL', 'UTC', 'WARNINGS_PATH', 'apply_realized_fill', 'archive_terminal_records', 'auto_submit_preflight', 'begin_place_attempt', 'build_clients', 'build_decision', 'build_order_payload', 'chain_key', 'chain_runtime_identity_is_verified', 'check_holdings_continuity', 'claim_chain_dispatch', 'claim_intent', 'clear_chain_dispatch_inflight', 'clock_mode', 'commit_final_row', 'compute_row', 'credentials', 'datetime', 'db', 'dna_step_for', 'dna_steps_remaining', 'environment_label', 'evaluate_submit_gate', 'expire_unsent_before', 'fallback_slot_id', 'fence_chain_dispatch', 'fetch_holdings', 'fetch_open_orders', 'fetch_order_detail', 'fetch_snapshot', 'finalize_execution_fill', 'firebase_admin', 'functions_framework', 'is_regular_session', 'is_transient_exception', 'list_actionable', 'list_audit_pending', 'load_config', 'logger', 'logging', 'mark_order_intent_materialized', 'market_category', 'math', 'normalize_status', 'order_confirmation_phrase', 'os', 'pending_order_intents', 'place_market_order', 'position_vanished', 'preview_market_order', 'put_intent', 'read_anchor', 'read_chain_state', 'read_committed_row', 'read_intent', 'redact_sensitive_text', 'release_chain_dispatch', 'release_intent_claim', 'repair_pending_intent_row', 'resolve_dna_step', 'resolve_market_slot', 'runtime_identity_fingerprint', 'slot_seconds', 'summarize_order_result', 'time', 'timedelta', 'timezone', 'token_health', 'traceback', 'update_intent', 'update_order_audit', 'uuid', 'verify_cashflow_semantics', 'verify_runtime_identity', 'write_order_audit']

def configure(deps) -> None:
    """Inject the HTTP facade dependencies for deterministic tests/wiring."""
    target = globals()
    for name in _INJECTABLE:
        if hasattr(deps, name):
            target[name] = getattr(deps, name)


class FillNotConfirmed(RuntimeError):
    """The broker reports a fill the account position has not shown yet.

    Not an error about the order — the order is fine. It says only that the two
    witnesses required before the model ledger may move (a cumulative filled
    quantity, and a position read back after execution) do not yet agree, so
    this tick must wait rather than book a cashflow on the broker's word alone.
    """


class RealizedMathError(RuntimeError):
    """The broker confirmed a fill, but our incremental realized math refused it.

    A different question from 'does this order exist?': the order does exist and
    is filled. Only the ledger update failed, and asking the broker again cannot
    change that, so it must not spend the reconcile budget.
    """


def _init_firebase():
    if not firebase_admin._apps:
        firebase_admin.initialize_app(
            credentials.ApplicationDefault(),
            {"databaseURL": os.environ["FIREBASE_DB_URL"], "httpTimeout": 5},
        )


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _error_text(exc: Exception, *, with_type: bool = True) -> str:
    text = redact_sensitive_text(exc)
    return f"{type(exc).__name__}: {text}" if with_type else text


def _execution_summary(place_response, detail):
    summary = summarize_order_result(place_response, detail)
    from lego_orders import REALIZED_STATUSES
    if summary["status"] not in (TERMINAL_STATUSES | REALIZED_STATUSES |
                                  {"PENDING", "SUBMITTED"}):
        summary["status"] = "UNKNOWN"
    return summary


def _poll_order_status(trade_client, client_order_id: str, place_res: dict,
                       expected_symbol: str | None = None) -> dict:
    detail = None
    for i in range(ORDER_POLL_ATTEMPTS):
        if i:
            tick_runtime.require_budget(ORDER_POLL_DELAY_S + 2.0)
            time.sleep(ORDER_POLL_DELAY_S)
        detail = fetch_order_detail(trade_client, client_order_id)
        if expected_symbol is not None:
            validate_order_detail_identity(detail, client_order_id=client_order_id,
                                           symbol=expected_symbol)
        summary = _execution_summary(place_res, detail)
        if normalize_status(summary.get("status")) in TERMINAL_STATUSES:
            return summary
    return _execution_summary(place_res, detail)


def _record_warning(kind: str, message: str, extra: dict | None = None) -> None:
    """Make a silent skip alertable without growing without bound.

    One node per kind, updated in place with a counter, instead of a push per
    slot: a degraded clock repeats every slot forever, so pushing a record each
    time would recreate the unbounded path this system is already trimming.
    Never raises — a warning that breaks the run it is warning about is worse
    than no warning.

    Also emitted to the log, because RTDB was the only place it went. A blocked
    order left three traces and an operator could reach none of them: the
    response body, which Cloud Scheduler discards; a single counter node nobody
    watches; and nothing at all in Cloud Logging, where the run showed HTTP 200
    in 0.3s. Diagnosing this took a CSV export of the row table. One log line per
    warning ends that — `kind` is greppable and `extra` carries blocked_by.
    """
    try:
        ref = db.reference(f"{WARNINGS_PATH}/{kind}")
        now = _iso(datetime.now(UTC))
        log_token = uuid.uuid4().hex

        def txn(current):
            doc = dict(current or {})
            changed = doc.get("message") != message[:500] or any(
                doc.get(k) != v for k, v in (extra or {}).items())
            last_logged = doc.get("last_logged_at")
            try:
                age = (datetime.now(UTC) - datetime.fromisoformat(
                    str(last_logged).replace("Z", "+00:00"))).total_seconds()
            except (ValueError, TypeError):
                age = 600
            if changed or age >= 600:
                doc["last_logged_at"] = now
                doc["last_log_token"] = log_token
            doc["count"] = int(doc.get("count", 0) or 0) + 1
            doc.setdefault("first_at", now)
            doc.update({"last_at": now, "message": message[:500], **(extra or {})})
            return doc

        written = ref.transaction(txn)
        if written and written.get("last_log_token") == log_token:
            logger.warning("lego warning kind=%s %s %s", kind, message,
                           redact_sensitive_text(extra) if extra else "")
    except Exception:
        logger.warning("lego warning kind=%s %s %s", kind, message,
                       redact_sensitive_text(extra) if extra else "")


def _apply_realized_if_available(intent: dict, summary: dict) -> dict:
    # summarize_order_result decides this from the filled quantity, not from the
    # status alone, so a fill that ends CANCELLED or EXPIRED is still accounted.
    if not summary.get("realized"):
        return summary
    qty = summary.get("filled_quantity")
    price = summary.get("filled_price")
    if qty is None or price is None:
        raise RealizedMathError(
            "fill confirmed but quantity/price unavailable — needs manual check")
    fee_known = summary.get("filled_fee") is not None
    try:
        broker_cashflow = apply_broker_cashflow(
            intent["chain_key"], intent["run_id"], intent["side"],
            cumulative_qty=qty, average_price=price,
            actual_fee=summary.get("filled_fee") if fee_known else None,
        )
        if not fee_known:
            # Strategy/model accounting is deliberately separate. It must not
            # silently turn an unavailable broker fee into a real zero.
            return {
                **summary,
                **broker_cashflow,
                "realized": False,
                "realized_pending_fee": True,
            }
        realized = apply_realized_fill(
            intent["chain_key"], intent["run_id"], intent["side"],
            cumulative_qty=float(qty), price=float(price),
            cumulative_fee=float(summary["filled_fee"]),
        )
    except ValueError as exc:
        # Raised by apply_realized_fill/apply_fill when the incremental fill
        # numbers cannot be made sense of. Kept distinct from every other failure
        # here, all of which mean 'we could not reach or read the broker'.
        raise RealizedMathError(str(exc)) from exc
    out = dict(summary)
    out.update(broker_cashflow)
    out["realized_pending_fee"] = False
    out["fee_overdue"] = False
    out.update(realized)
    if realized.get("matching_pending"):
        out["realized"] = False
    return out


def _persist(chain_key_: str, run_id: str, fields: dict) -> dict:
    """Keep the outbox authoritative and make an interrupted audit repairable."""
    durable = update_intent(chain_key_, run_id, {**fields, "audit_pending": True})
    _mirror_order_audit(chain_key_, run_id, durable)
    return {"run_id": run_id, **fields, "status": durable.get("status", fields.get("status"))}


def _mirror_order_audit(chain_key_: str, run_id: str, fields: dict) -> None:
    """Best-effort audit mirror; the outbox marker makes failure recoverable."""
    try:
        update_order_audit(run_id, _audit_fields(fields))
    except Exception as exc:
        _record_warning(
            "order_audit_repair",
            "outbox อัปเดตแล้วแต่ audit ยังไม่สำเร็จ — worker จะซ่อมซ้ำ",
            {"run_id": run_id, "error_type": type(exc).__name__},
        )
    else:
        # If this clear fails the marker safely remains and the repair pass writes
        # the same audit payload again.
        try:
            acknowledge_audit(chain_key_, run_id, int(fields.get("audit_revision", 0)))
        except Exception as exc:
            _record_warning(
                "order_audit_repair",
                "audit เขียนแล้วแต่ล้าง repair marker ไม่สำเร็จ — "
                "รอบถัดไปเขียนซ้ำได้อย่างปลอดภัย",
                {"run_id": run_id, "error_type": type(exc).__name__},
            )


_AUDIT_INTERNAL_FIELDS = {
    "audit_pending", "claim_owner", "claim_until", "claim_generation",
    "place_fence",
    "broker_raw_detail",
}


def _audit_fields(intent: dict) -> dict:
    fields = {k: v for k, v in intent.items() if k not in _AUDIT_INTERNAL_FIELDS}
    if not intent.get("place_attempted") and normalize_status(intent.get("status")) in UNSENT_CHAIN_TERMINAL:
        # Older versions incorrectly used decision time as placed_at. Removing
        # it is justified only for a durable, never-attempted terminal intent.
        fields["placed_at"] = None
    return fields


def _repair_pending_audits(chain_key_: str) -> int:
    repaired = 0
    for intent in list_audit_pending(chain_key_):
        tick_runtime.require_budget(5.0)
        run_id = str(intent["run_id"])
        fields = _audit_fields(intent)
        try:
            update_order_audit(run_id, fields)
            if acknowledge_audit(chain_key_, run_id, int(intent.get("audit_revision", 0))):
                repaired += 1
        except Exception as exc:
            _record_warning(
                "order_audit_repair",
                "audit repair ยังไม่สำเร็จ — เก็บ marker ไว้ลองรอบถัดไป",
                {"run_id": run_id, "error_type": type(exc).__name__},
            )
    return repaired


def _announce_identity_adoption(adopted: bool, runtime_identity: str) -> None:
    """Say once that a pre-guard chain was bound to this account/environment."""
    if not adopted:
        return
    _record_warning(
        "runtime_identity_adopted",
        "chain เดิมไม่มี runtime identity — ผูกกับ account/environment ปัจจุบัน "
        "อัตโนมัติ ตรวจว่า WEBULL_ACCOUNT_ID และ WEBULL_ENV ถูกต้อง",
        {"identity_prefix": runtime_identity[:8]},
    )


def _recover_pending_order_intents(cfg, runtime_identity: str,
                                   state=UNREAD_STATE) -> int:
    """Materialize state-transaction markers into the idempotent private outbox."""
    recovered = 0
    for run_id, payload in pending_order_intents(
            cfg, runtime_identity=runtime_identity, state=state).items():
        try:
            # The state transaction is the commit proof. Repair its final row
            # patch before exposing the intent to a worker that correctly
            # refuses any source row still marked committed=False.
            repair_pending_intent_row(
                cfg, run_id, runtime_identity=runtime_identity, state=state)
            put_intent(chain_key(cfg), run_id, payload)
            mark_order_intent_materialized(
                cfg, run_id, runtime_identity=runtime_identity)
            recovered += 1
        except RuntimeIdentityError:
            raise
        except Exception as exc:
            _record_warning(
                "outbox_recovery",
                "committed row ยัง materialize เข้า outbox ไม่สำเร็จ — "
                "marker ยังอยู่และจะลองใหม่",
                {"run_id": run_id, "error_type": type(exc).__name__},
            )
    return recovered


def _persist_summary(intent: dict, summary: dict) -> None:
    summary = dict(summary)
    if normalize_status(summary.get("status")) in {"FAILED", "REJECTED"}:
        summary["terminal_reason"] = "broker order failed: " + str(
            summary.get("reject_reason") or "reason not supplied by broker")
    _persist(intent["chain_key"], intent["run_id"],
             {**summary, "status": normalize_status(summary.get("status"))})


def _persist_error(chain_key_: str, run_id: str, status: str, exc: Exception,
                   extra: dict | None = None) -> dict:
    from webull_io import broker_error_details
    err = _error_text(exc)
    details = broker_error_details(exc)
    _persist(chain_key_, run_id,
             {"status": status, "last_error": err[:500],
              "broker_error": details, **(extra or {})})
    return {"run_id": run_id, "status": status, "error": err,
            "error_type": type(exc).__name__, "broker_error": details}


def _intent_is_v2(intent: dict) -> bool:
    strategy = (intent.get("strategy_config") or {}).get("strategy_id")
    return str(strategy or "").endswith("_v2")


def _reconcile_max_attempts(intent: dict | None = None) -> int:
    if intent is not None and _intent_is_v2(intent):
        return 20
    return max(1, int(os.environ.get("LEGO_RECONCILE_MAX_ATTEMPTS", "20")))


def _min_dna_remaining(*, typed_v2: bool = False) -> int:
    """How much DNA must be left before a new order may be opened.

    Never raises: an unreadable value is a deploy typo, and refusing to answer
    would abort the row instead of the order, which inverts the priority the
    whole pipeline is built on.
    """
    if typed_v2:
        return DEFAULT_MIN_DNA_REMAINING
    try:
        return int(os.environ.get("LEGO_AUTO_SUBMIT_MIN_DNA_REMAINING",
                                  str(DEFAULT_MIN_DNA_REMAINING)))
    except (TypeError, ValueError):
        return DEFAULT_MIN_DNA_REMAINING


def _persist_reconcile_failure(intent: dict, exc: Exception) -> dict:
    """Bound the reconcile loop so one unresolvable order cannot jam the outbox.

    PLACING_UNKNOWN is deliberately non-terminal: an order we failed to confirm
    may still exist at the broker, so the worker must ask again. But nothing
    counted the asking. An order the broker never accepted answers 'UNKNOWN'
    forever, and because list_actionable serves oldest-first up to
    LEGO_ORDER_WORKER_LIMIT, three such intents starve every later decision —
    the DNA keeps committing rows while no order is ever sent again.

    Past the bound the outbox stops asking and says so. The open question is not
    dropped: the order audit keeps run_id, last_error and needs_manual_check,
    and the dashboard already renders that table.
    """
    ck, run_id = intent["chain_key"], intent["run_id"]
    from webull_io import is_auth_blocked
    if isinstance(exc, tick_runtime.TickDeadlineExceeded) or is_auth_blocked(exc):
        # No unsuccessful broker query occurred. Preserve the durable status
        # and reconcile budget (particularly FILLED awaiting its late fee).
        current = read_intent(ck, run_id) or intent
        return {"run_id": run_id, "status": current.get("status", "PLACING_UNKNOWN"),
                "deferred_reason": "auth_backoff" if is_auth_blocked(exc) else "tick_deadline",
                "error": _error_text(exc)}
    attempts = int(intent.get("reconcile_attempts", 0) or 0) + 1
    # last_error is overwritten every tick, and by the time a human reads it the
    # useful message ("insufficient buying power") has been buried under the
    # generic one. Keep the first failure, which is the one that explains why.
    extra = {"reconcile_attempts": attempts}
    if not intent.get("first_error"):
        extra["first_error"] = _error_text(exc)[:500]
    if attempts < _reconcile_max_attempts(intent):
        return _persist_error(ck, run_id, "PLACING_UNKNOWN", exc, extra)
    return _persist_error(ck, run_id, "RECONCILE_ABANDONED", exc, {
        **extra,
        "needs_manual_check": True,
        "terminal_reason": (f"broker ไม่ยืนยันสถานะครบ {attempts} ครั้ง — "
                            "ต้องเช็คที่ broker เองว่า order นี้มีจริงหรือไม่"),
    })


def _persist_realized_math_error(intent: dict, summary: dict, exc: Exception) -> dict:
    """End an intent whose order filled but whose realized math did not.

    This used to share the reconcile path, which reads 'we do not know whether
    this order exists'. Here we do know: the broker answered, and the answer says
    filled. Retrying cannot fix arithmetic, so it ends immediately instead of
    spending attempts, and the broker's own numbers are kept on the audit so the
    person reconciling can see the fill was real and only the ledger is behind.
    """
    fields = {
        "status": "REALIZED_MATH_ERROR",
        "needs_manual_check": True,
        "realized": False,
        "broker_status": normalize_status(summary.get("status")),
        "last_error": _error_text(exc)[:500],
        "terminal_reason": ("broker ยืนยัน fill แล้ว แต่คำนวณ realized ไม่ได้ — "
                            "ห้ามส่ง order ซ้ำ ต้องกระทบยอด realized ledger เอง"),
    }
    for key in ("filled_quantity", "filled_price", "filled_fee", "reject_reason"):
        if key in summary:
            fields[key] = summary[key]
    _persist(intent["chain_key"], intent["run_id"], fields)
    _record_warning("realized_math_error",
                    "order fill ยืนยันแล้วแต่คำนวณ realized ไม่ได้ — ต้องกระทบยอดเอง",
                    {"run_id": intent["run_id"], "chain_key": intent["chain_key"]})
    return {"run_id": intent["run_id"], **fields}


def _holdings_drift_tolerance(*, typed_v2: bool = False) -> float:
    """Below this a holdings difference is noise, not a position that moved."""
    if typed_v2:
        return 0.000001
    try:
        return abs(float(os.environ.get("LEGO_HOLDINGS_DRIFT_TOLERANCE",
                                        "0.000001")))
    except (TypeError, ValueError):
        return 0.000001


def _fill_confirm_max_attempts(intent: dict | None = None) -> int:
    if intent is not None and _intent_is_v2(intent):
        return DEFAULT_FILL_CONFIRM_MAX_ATTEMPTS
    try:
        return max(1, int(os.environ.get("LEGO_FILL_CONFIRM_MAX_ATTEMPTS",
                                         str(DEFAULT_FILL_CONFIRM_MAX_ATTEMPTS))))
    except (TypeError, ValueError):
        return DEFAULT_FILL_CONFIRM_MAX_ATTEMPTS


def _positive_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 and number == number and number != float("inf") else None


def _nonnegative_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _chain_fence_can_clear(intent: dict | None) -> bool:
    """Whether an outbox terminal is safe for a later order on this chain."""
    if not isinstance(intent, dict):
        return False
    status = normalize_status(intent.get("status"))
    if status not in OUTBOX_TERMINAL or status in MANUAL_CHAIN_TERMINAL:
        return False
    if (intent.get("needs_manual_check") or intent.get("cashflow_abandoned")
            or intent.get("admin_reconciliation_pending")):
        return False
    if status in UNSENT_CHAIN_TERMINAL:
        return (intent.get("place_attempted") is not True
                and not intent.get("broker_id")
                and not intent.get("order_id"))

    # Every broker terminal needs an explicit finite cumulative quantity.  A
    # missing/NaN/negative value is not evidence of zero fill.  Positive partial
    # fills (including CANCELLED/EXPIRED) must be durable in *both* ledgers before
    # a later recurrence may use the chain; FILLED with zero is contradictory.
    quantity = _nonnegative_float(intent.get("filled_quantity"))
    if quantity is None:
        return False
    if quantity > 0:
        return (_positive_float(intent.get("filled_price")) is not None
                and intent.get("broker_cashflow_recorded") is True
                and intent.get("broker_fee_status") == "KNOWN"
                and intent.get("cashflow_finalized") is True
                and intent.get("realized") is True)
    if status == "FILLED":
        return False
    return (intent.get("cashflow_finalized") is not True
            and intent.get("realized") is not True)


def _holdings_moved(side: str, before: float, after: float, tolerance: float) -> bool:
    """Did the position move the way this side of the trade requires?

    Direction, not just difference: a BUY whose position went *down* between the
    decision and the fill is somebody else's trade landing in the same account,
    and booking our ΔAₙ against it would be inventing a cashflow.
    """
    if side == "BUY":
        return after > before + tolerance
    if side == "SELL":
        return after < before - tolerance
    return False


def _finalize_model_ledger(trade_client, cfg, intent: dict, summary: dict) -> dict:
    """Book ΔAₙ/Aₙ/Eₙ for this row, once the broker proves the shares moved.

    Both inputs come from the broker and neither from what we asked for: the
    cumulative filled quantity and price of this client_order_id, and the
    position read back after execution. The ordered quantity is never used to
    guess the resulting holdings, and the decision price is never used in place
    of the filled price.
    """
    quantity = _positive_float(summary.get("filled_quantity"))
    price = _positive_float(summary.get("filled_price"))
    if quantity is None or price is None:
        raise RealizedMathError(
            "fill confirmed but quantity/price unavailable — needs manual check")
    try:
        holdings_after = fetch_holdings(trade_client, cfg)
    except Exception as exc:
        # Could not read the position — a network blip, or a positions response
        # this adapter refuses to interpret. Neither says anything about the
        # fill, so it defers on the bounded budget instead of ending the intent
        # the way an arithmetic failure does.
        raise FillNotConfirmed(
            f"อ่าน holdings หลัง fill ไม่สำเร็จ: {_error_text(exc)}") from exc
    before = float(intent.get("decision_holdings", 0.0) or 0.0)
    # Same rule as the pre-decision guard, applied to the same reading: a
    # rebalance SELL targets value fix_c and always leaves shares behind, so a
    # position that reads exactly zero is a positions response that lost the
    # symbol, not a confirmation. Booking it would move P_acted onto that fill
    # *and* write prev_holdings = 0, which disarms the guard for every slot
    # after it — the compounding this system already refuses on the way in.
    if ((cfg.strategy_id.endswith("_v2")
         or os.environ.get(
             "LEGO_ALLOW_ZERO_HOLDINGS", "false").lower() != "true")
            and position_vanished(before, holdings_after)):
        raise FillNotConfirmed(
            f"chain เคยถือ {before} หุ้น แต่ post-execution holdings อ่านได้ 0 — "
            "อาจเป็น positions response ที่ไม่ครบ จึงยังไม่ finalize cashflow")
    if not _holdings_moved(str(intent.get("side", "")).upper(), before,
                           holdings_after, _holdings_drift_tolerance(
                               typed_v2=cfg.strategy_id.endswith("_v2"))):
        raise FillNotConfirmed(
            f"broker แจ้ง filled {quantity} แต่ holdings หลังส่งยังเป็น {holdings_after} "
            f"(ตอนตัดสินใจ {before}) — ยังไม่ยืนยันว่าจำนวนถือครองเปลี่ยน")
    return finalize_execution_fill(
        cfg, str(intent["run_id"]),
        ExecutionFill(filled_price=price, filled_quantity=quantity,
                      holdings_after=holdings_after),
        runtime_identity=intent.get("runtime_identity_fingerprint"))


def _defer_fill_confirmation(intent: dict, summary: dict, exc: Exception) -> dict:
    """Come back for a fill whose position has not landed yet — but not forever.

    The intent stays actionable under its own non-terminal status so the next
    tick asks the broker again. Past the bound it stops asking and carries the
    broker's own status out of the queue with needs_manual_check, for the same
    reason RECONCILE_ABANDONED exists: an intent that can never resolve must not
    keep a dispatch slot from every decision behind it.
    """
    attempts = int(intent.get("fill_confirm_attempts", 0) or 0) + 1
    broker_status = normalize_status(summary.get("status"))
    fields = {
        **summary,
        "broker_status": broker_status,
        "cashflow_finalized": False,
        "fill_confirm_attempts": attempts,
        "last_error": _error_text(exc)[:500],
    }
    if attempts < _fill_confirm_max_attempts(intent):
        fields["status"] = AWAITING_FILL_CONFIRMATION
        _persist(intent["chain_key"], intent["run_id"], fields)
        return {"run_id": intent["run_id"], **fields}
    fields.update({
        "status": broker_status,
        "cashflow_abandoned": True,
        "needs_manual_check": True,
        "terminal_reason": (f"broker แจ้ง fill แต่ holdings ไม่ยืนยันครบ {attempts} ครั้ง "
                            "— ΔAₙ/Aₙ/Eₙ ของแถวนี้ยังไม่ finalize ต้องกระทบยอดเอง"),
    })
    _persist(intent["chain_key"], intent["run_id"], fields)
    _record_warning(
        "cashflow_unconfirmed",
        "fill ยืนยันจาก broker แล้วแต่ holdings ไม่ขยับ — model ledger ยังไม่ finalize",
        {"run_id": intent["run_id"], "chain_key": intent["chain_key"]})
    return {"run_id": intent["run_id"], **fields}


def _persist_cashflow_error(intent: dict, summary: dict, exc: Exception) -> dict:
    """End an intent whose fill is real but whose model ledger refused it.

    Same shape as _persist_realized_math_error and for the same reason: the
    broker has answered, so re-sending would duplicate a filled order. Retrying
    cannot fix arithmetic or a missing chain state, so it ends here and the
    ledger gap stays visible instead of being retried forever.
    """
    fields = {
        **summary,
        "status": "CASHFLOW_FINALIZE_ERROR",
        "broker_status": normalize_status(summary.get("status")),
        "cashflow_finalized": False,
        "cashflow_abandoned": True,
        "needs_manual_check": True,
        "last_error": _error_text(exc)[:500],
        "terminal_reason": ("fill จริงแต่ finalize ΔAₙ/Aₙ/Eₙ ไม่ได้ — "
                            "ห้ามส่ง order ซ้ำ ต้องกระทบยอด model ledger เอง"),
    }
    _persist(intent["chain_key"], intent["run_id"], fields)
    _record_warning("cashflow_finalize_error",
                    "fill ยืนยันแล้วแต่ finalize model ledger ไม่ได้ — ต้องกระทบยอดเอง",
                    {"run_id": intent["run_id"], "chain_key": intent["chain_key"]})
    return {"run_id": intent["run_id"], **fields}


def _finish_with_realized(trade_client, cfg, intent: dict, summary: dict) -> dict:
    """The broker has answered; the only failure left belongs to us.

    Two ledgers move here and they are independent: the realized ledger from
    matched broker legs, and the 17-column model ledger this function finalizes
    from the same confirmed fill. A decision never reaches either one.
    """
    # Diagnostics stay private. Record the breaker while this run still owns
    # the money fence, BEFORE any terminal outbox write can remove recovery.
    summary = dict(summary)
    raw = summary.pop("broker_raw_detail", None)
    if raw is not None:
        update_intent(intent["chain_key"], intent["run_id"], {"broker_raw_detail": raw})
    circuit = broker_circuit.record_outcome(intent, summary)
    if circuit.get("halted"):
        summary["broker_reject_halted"] = True
        import alerting
        alerting.notify(broker_circuit.HALT,
                        str(intent.get("runtime_identity_fingerprint")) + cfg.symbol,
                        symbol=cfg.symbol, count=circuit.get("consecutive_broker_rejects"))
    try:
        summary = _apply_realized_if_available(intent, summary)
    except RealizedMathError as exc:
        return _persist_realized_math_error(intent, summary, exc)
    if summary.get("matching_pending"):
        summary = {
            **summary,
            "broker_status": normalize_status(summary.get("status")),
            "status": REALIZED_MATCHING_PENDING,
            "realized": False,
        }
        _persist_summary(intent, summary)
        return {"run_id": intent["run_id"], **summary}
    # Acted is a cumulative filled quantity above zero and nothing else — not the
    # status, which is only a label on top of it. So a DAY order that fills 30 of
    # 70 and is cancelled at the close still finalizes those 30, while a REJECTED
    # or CANCELLED order that moved no shares leaves ΔAₙ = 0 and Aₙ exactly where
    # the last confirmed fill left it. (_apply_realized_if_available has already
    # refused a claimed fill with no readable quantity or price.)
    filled = _positive_float(summary.get("filled_quantity"))
    # Broker fill quantities and average prices are cumulative. A partial
    # snapshot can still change, while finalize_execution_fill is deliberately
    # absorbing by run_id. Wait for a terminal status so the one model-ledger
    # booking uses the final cumulative values. CANCELLED/EXPIRED with a positive
    # cumulative fill still acted and therefore follows this terminal branch.
    broker_terminal = normalize_status(summary.get("status")) in TERMINAL_STATUSES
    if (filled is not None and broker_terminal
            and not intent.get("cashflow_abandoned")
            and not intent.get("cashflow_finalized")):
        try:
            finalized = _finalize_model_ledger(trade_client, cfg, intent, summary)
        except FillNotConfirmed as exc:
            return _defer_fill_confirmation(intent, summary, exc)
        except RealizedMathError as exc:
            return _persist_realized_math_error(intent, summary, exc)
        except (ExecutionFinalizeError, ValueError, TypeError) as exc:
            return _persist_cashflow_error(intent, summary, exc)
        summary = {
            **summary,
            "cashflow_finalized": True,
            "cashflow_waiting_for_terminal": False,
            "cashflow_applied_now": finalized["applied"],
            "delta_actual": finalized["delta_actual"],
            "actual_cumulative": finalized["actual_cumulative"],
            "excess": finalized["excess"],
            "post_execution_holdings": finalized["holdings_after"],
        }
    elif (filled is not None and broker_terminal
          and intent.get("cashflow_finalized")):
        # A terminal fill can re-enter only to collect a late broker fee. Do
        # not make that correction depend on another, later position snapshot.
        summary = {
            **summary,
            "cashflow_finalized": True,
            "cashflow_waiting_for_terminal": False,
        }
    elif filled is not None and not broker_terminal:
        summary = {
            **summary,
            "cashflow_finalized": False,
            "cashflow_waiting_for_terminal": True,
        }
    if (filled is not None and broker_terminal
            and summary.get("broker_fee_status") == "PENDING"):
        now = datetime.now(UTC)
        since = str(intent.get("fee_pending_since") or intent.get("created_at") or _iso(now))
        try:
            age = max(0.0, (now - datetime.fromisoformat(since.replace("Z", "+00:00"))).total_seconds())
        except (ValueError, TypeError):
            since, age = _iso(now), 0.0
        summary = {
            **summary,
            "broker_status": normalize_status(summary.get("status")),
            "status": AWAITING_BROKER_FEE,
            "realized": False,
            "realized_pending_fee": True,
            "fee_pending_since": since,
            "fee_pending_age_seconds": round(age, 3),
            "fee_overdue": age >= 900,
        }
        if age >= 900:
            _record_warning("broker_fee_overdue",
                            "terminal fill รอ actual fee เกิน 15 นาที — ตรวจ broker detail; fence ยังคงอยู่",
                            {"run_id": intent["run_id"], "chain_key": intent["chain_key"],
                             "fee_pending_since": since})
    _persist_summary(intent, summary)
    return {"run_id": intent["run_id"], **summary}


def _pending_row_shape(intent: dict) -> dict:
    """What the outbox intent says it wants to send."""
    return {
        "สถานะ": intent["row_status"],
        "สินทรัพย์": intent["symbol"],
        "_meta": {
            "side": intent["side"],
            "quantity": float(intent["quantity"]),
            "step": int(intent["step"]),
        },
    }


def _committed_row_shape(doc: dict) -> dict:
    """What the engine actually committed — the gate's source of truth."""
    return {
        "สถานะ": doc.get("สถานะ"),
        "สินทรัพย์": doc.get("สินทรัพย์"),
        "_meta": {
            "side": doc.get("ฝั่ง"),
            "quantity": float(doc.get("จำนวนสั่ง (หุ้น)") or 0.0),
            "step": int(doc.get("DNA step") or 0),
        },
    }


def _stop(chain_key_: str, run_id: str, status: str, extra: dict | None = None,
          **reported) -> dict:
    """Close an unsent intent and leave a repairable execution audit."""
    _persist(chain_key_, run_id, {"status": status, **(extra or {})})
    return {"run_id": run_id, "status": status, **reported}


def _nonnegative_finite_env(name: str, default: float) -> float:
    """Read a safety limit without letting NaN/inf disable its comparison."""
    raw = os.environ.get(name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} ต้องเป็นตัวเลข finite และ >= 0") from exc
    if not (math.isfinite(value) and value >= 0):
        raise ValueError(f"{name} ต้องเป็นตัวเลข finite และ >= 0")
    return value


def _parse_utc(value, field: str) -> datetime:
    """Parse an outbox timestamp as an aware UTC datetime, or fail closed."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} ต้องเป็น ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} ต้องมี timezone")
    return parsed.astimezone(UTC)


def _dispatch_quote_safety(cfg, intent: dict, fresh: dict, *,
                           now_utc: datetime | None = None,
                           max_price_drift_bps: float | None = None,
                           max_quote_age_seconds: float | None = None,
                           max_decision_age_seconds: float | None = None) -> dict:
    """Revalidate a committed intent against the quote immediately before send.

    A MARKET order may fill away from either quote, so tiny moves are expected.
    The guard nevertheless refuses a direction change, a stale intent that would
    now overshoot by more than the strategy's economic deadband, or a quote
    beyond the configured drift/age budget. It never rewrites the committed row
    or silently changes quantity.
    """
    now_utc = now_utc or datetime.now(UTC)
    if now_utc.tzinfo is None:
        raise ValueError("now_utc ต้องมี timezone")
    max_price_drift_bps = (
        _nonnegative_finite_env(
            "LEGO_MAX_DISPATCH_PRICE_DRIFT_BPS",
            DEFAULT_MAX_DISPATCH_PRICE_DRIFT_BPS)
        if max_price_drift_bps is None else float(max_price_drift_bps)
    )
    max_quote_age_seconds = (
        _nonnegative_finite_env(
            "LEGO_MAX_DISPATCH_QUOTE_AGE_SECONDS",
            DEFAULT_MAX_DISPATCH_QUOTE_AGE_SECONDS)
        if max_quote_age_seconds is None else float(max_quote_age_seconds)
    )
    if not (math.isfinite(max_price_drift_bps)
            and max_price_drift_bps >= 0):
        raise ValueError("max_price_drift_bps ต้อง finite และ >= 0")
    if not (math.isfinite(max_quote_age_seconds)
            and max_quote_age_seconds >= 0):
        raise ValueError("max_quote_age_seconds ต้อง finite และ >= 0")
    max_decision_age_seconds = (
        max_quote_age_seconds if max_decision_age_seconds is None
        else float(max_decision_age_seconds))
    if not (math.isfinite(max_decision_age_seconds)
            and max_decision_age_seconds >= 0):
        raise ValueError("max_decision_age_seconds ต้อง finite และ >= 0")

    try:
        decision_price = float(intent["decision_price"])
        decision_holdings = float(intent["decision_holdings"])
        requested_quantity = float(intent["quantity"])
        fresh_price = float(fresh["price"])
        fresh_holdings = float(fresh["holdings"])
        signal = int(intent["signal"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("intent/snapshot ขาด dispatch provenance ที่ตรวจสอบได้") from exc
    finite_values = (decision_price, decision_holdings, requested_quantity,
                     fresh_price, fresh_holdings)
    if not all(math.isfinite(value) for value in finite_values):
        raise ValueError("dispatch provenance ต้องเป็นตัวเลข finite")
    if (decision_price <= 0 or fresh_price <= 0 or decision_holdings < 0
            or fresh_holdings < 0 or requested_quantity <= 0
            or signal not in (0, 1)):
        raise ValueError("dispatch provenance อยู่นอกช่วงที่อนุญาต")

    decision_time = _parse_utc(
        intent.get("decision_time") or intent.get("created_at"),
        "decision_time/created_at")
    quote_time = _parse_utc(fresh.get("quote_time"), "quote_time")
    now_utc = now_utc.astimezone(UTC)
    decision_delta_seconds = (now_utc - decision_time).total_seconds()
    quote_delta_seconds = (now_utc - quote_time).total_seconds()
    decision_age_seconds = max(0.0, decision_delta_seconds)
    quote_age_seconds = max(0.0, quote_delta_seconds)
    price_drift_bps = abs(fresh_price / decision_price - 1.0) * 10_000.0
    original = build_decision(cfg, decision_price, decision_holdings, signal)
    current = build_decision(cfg, fresh_price, fresh_holdings, signal)
    intent_side = str(intent.get("side") or "").strip().upper()
    quantum = 10.0 ** (-cfg.decimal_precision)
    quantity_tolerance = max(1e-12, quantum / 2.0)
    # Subtract in decimal space before valuing the excess: binary float
    # subtraction can turn an exact budget boundary into a false rejection.
    dispatch_safe_quantity = current.quantity if current.acted else 0.0
    overshoot_quantity = max(
        Decimal("0"),
        Decimal(str(requested_quantity)) - Decimal(str(dispatch_safe_quantity)))
    dispatch_price = Decimal(str(fresh_price))
    overshoot_notional_usd = overshoot_quantity * dispatch_price
    rounding_notional_usd = Decimal(str(quantity_tolerance)) * dispatch_price
    max_overshoot_notional_usd = Decimal(str(cfg.diff)) + rounding_notional_usd

    reasons: list[str] = []
    if (not original.acted or original.side != intent_side
            or abs(original.quantity - requested_quantity) > quantity_tolerance):
        reasons.append("intent_decision_mismatch")
    if not current.acted or current.side != intent_side:
        reasons.append("side_changed_or_pass")
    # Keep the committed quantity when its excess value fits the same USD
    # deadband used by the strategy. A material excess still needs a new slot.
    # This allowance never applies to provenance or direction checks above.
    if current.acted and overshoot_notional_usd > max_overshoot_notional_usd:
        reasons.append("quantity_would_overshoot")
    if price_drift_bps > max_price_drift_bps:
        reasons.append("price_drift_limit")
    if decision_delta_seconds < -MAX_DISPATCH_FUTURE_SKEW_SECONDS:
        reasons.append("decision_time_in_future")
    if quote_delta_seconds < -MAX_DISPATCH_FUTURE_SKEW_SECONDS:
        reasons.append("quote_time_in_future")
    if decision_age_seconds > max_decision_age_seconds:
        reasons.append("decision_age_limit")
    if quote_age_seconds > max_quote_age_seconds:
        reasons.append("quote_age_limit")

    return {
        "ok": not reasons,
        "reasons": reasons,
        "decision_price": decision_price,
        "dispatch_price": fresh_price,
        "price_drift_bps": price_drift_bps,
        "max_price_drift_bps": max_price_drift_bps,
        "decision_time": decision_time.isoformat().replace("+00:00", "Z"),
        "quote_time": quote_time.isoformat().replace("+00:00", "Z"),
        "decision_age_seconds": decision_age_seconds,
        "quote_age_seconds": quote_age_seconds,
        "max_quote_age_seconds": max_quote_age_seconds,
        "max_decision_age_seconds": max_decision_age_seconds,
        "intent_side": intent_side,
        "dispatch_side": current.side if current.acted else "PASS",
        "intent_quantity": requested_quantity,
        "dispatch_safe_quantity": dispatch_safe_quantity,
        "overshoot_quantity": float(overshoot_quantity),
        "overshoot_notional_usd": float(overshoot_notional_usd),
        "max_overshoot_notional_usd": float(max_overshoot_notional_usd),
        "quantity_tolerance": quantity_tolerance,
        "rounding_notional_usd": float(rounding_notional_usd),
    }


def _reject_unsafe_dispatch_quote(chain_key_: str, run_id: str,
                                  quote_safety: dict, *, phase: str,
                                  extra: dict | None = None) -> dict:
    """Persist one quote-guard failure with phase-labelled evidence."""
    evidence = {
        key: value for key, value in quote_safety.items() if key != "ok"
    }
    evidence["dispatch_check_phase"] = phase
    evidence.update(extra or {})
    if "intent_decision_mismatch" in evidence["reasons"]:
        # The durable intent itself no longer agrees with the decision it claims
        # to represent. This is invalid provenance, not a normal market move.
        return _persist_error(
            chain_key_, run_id, "NOT_PLACED",
            ValueError(
                "confirmation phrase/outbox intent ไม่ตรงกับ committed decision"),
            {"terminal_reason": "intent decision provenance mismatch",
             **evidence})
    return _stop(
        chain_key_, run_id, "SUPPRESSED_STATE_CHANGED",
        {"terminal_reason": "dispatch blocked: " + ", ".join(evidence["reasons"]),
         **evidence},
        state_change_reasons=evidence["reasons"],
        price_drift_bps=evidence["price_drift_bps"])


def _dispatch_or_reconcile_one(trade_client, data_client, cfg, intent: dict,
                               dispatch_claim: dict | None = None,
                               runtime: RuntimeConfig | None = None) -> dict:
    run_id = intent["run_id"]
    ck = intent["chain_key"]
    status = normalize_status(intent.get("status"))

    committed_row = read_committed_row(run_id)
    if committed_row is None:
        return _stop(ck, run_id, "NOT_PLACED",
                     {"terminal_reason": "source row was not committed"})

    if status in RECONCILE_STATUSES:
        try:
            detail = fetch_order_detail(trade_client, run_id)
            if runtime is not None:
                validate_order_detail_identity(
                    detail, client_order_id=run_id, symbol=cfg.symbol)
            summary = _execution_summary({}, detail)
            if normalize_status(summary.get("status")) == "UNKNOWN":
                raise RuntimeError("broker order detail still UNKNOWN")
        except Exception as exc:
            # Everything inside this try is 'can we reach and read the broker?'.
            # The realized and model ledgers are applied outside it so their
            # failures are not reported as an unresolved order.
            return _persist_reconcile_failure(intent, exc)
        return _finish_with_realized(trade_client, cfg, intent, summary)

    if status != "PENDING_DISPATCH":
        return {"run_id": run_id, "status": status}

    identity = intent.get("runtime_identity_fingerprint") or runtime_identity_fingerprint()
    circuit = broker_circuit.status(identity, cfg.symbol)
    if circuit.get("halted"):
        return _stop(ck, run_id, "NOT_PLACED", {
            "terminal_reason": broker_circuit.HALT, "broker_reject_halted": True},
            broker_reject_halted=True)
    if runtime is not None:
        from webull_io import new_order_token_block
        token_block = new_order_token_block(token_health(), runtime.deployment.environment)
        if token_block:
            return _stop(ck, run_id, "NOT_PLACED", {
                "terminal_reason": token_block, "token_preflight_blocked": True},
                token_preflight_blocked=True)
        if (not runtime.deployment.allow_fractional
                and is_fractional_quantity(intent["quantity"])):
            return _stop(ck, run_id, "NOT_PLACED", {
                "terminal_reason": "LEGO_ALLOW_FRACTIONAL=false blocks committed fractional intent"})

    # Pause/observe stops only an order that has not crossed the irreversible
    # boundary. Attempted/unknown/submitted orders take the reconcile branch
    # above regardless of this setting.
    if runtime is not None and not runtime.allows_new_broker_mutation:
        return _stop(
            ck, run_id, "UNSENT_ABORTED",
            {"terminal_reason": "mode/active/release authorization blocks new mutation"},
        )

    expiry = datetime.fromisoformat(str(intent["expires_at"]).replace("Z", "+00:00"))
    if datetime.now(UTC) >= expiry:
        return _stop(ck, run_id, "EXPIRED_UNSENT")

    try:
        open_orders = fetch_open_orders(trade_client, cfg.symbol)
    except IncompleteOpenOrdersError as exc:
        return _persist_error(
            ck, run_id, "PENDING_DISPATCH", exc,
            {"pagination_complete": False},
        )
    if open_orders:
        return _stop(ck, run_id, "SUPPRESSED_ACTIVE_ORDER",
                     {"terminal_reason": f"{len(open_orders)} active broker order(s)"})

    fresh = fetch_snapshot(trade_client, data_client, cfg)
    try:
        if runtime is not None:
            tolerance = 0.000001
            max_price_drift_bps = DEFAULT_MAX_DISPATCH_PRICE_DRIFT_BPS
            max_quote_age_seconds = DEFAULT_MAX_DISPATCH_QUOTE_AGE_SECONDS
            max_decision_age_seconds = DEFAULT_MAX_DISPATCH_QUOTE_AGE_SECONDS
            if runtime.deployment.environment == "UAT":
                max_quote_age_seconds += UAT_QUOTE_DELAY_SECONDS
        else:
            tolerance = _nonnegative_finite_env(
                "LEGO_HOLDINGS_DRIFT_TOLERANCE", 0.000001)
            max_price_drift_bps = _nonnegative_finite_env(
                "LEGO_MAX_DISPATCH_PRICE_DRIFT_BPS",
                DEFAULT_MAX_DISPATCH_PRICE_DRIFT_BPS)
            max_quote_age_seconds = _nonnegative_finite_env(
                "LEGO_MAX_DISPATCH_QUOTE_AGE_SECONDS",
                DEFAULT_MAX_DISPATCH_QUOTE_AGE_SECONDS)
            max_decision_age_seconds = max_quote_age_seconds
    except ValueError as exc:
        # A deploy setting can be corrected, so keep the intent retryable.  In
        # particular, NaN must not turn ``drift > tolerance`` into False.
        return _persist_error(
            ck, run_id, "PENDING_DISPATCH", exc,
            {"configuration_error": True})
    try:
        decision_holdings = float(intent["decision_holdings"])
        fresh_holdings = float(fresh["holdings"])
        if not (math.isfinite(decision_holdings)
                and math.isfinite(fresh_holdings)
                and decision_holdings >= 0 and fresh_holdings >= 0):
            raise ValueError("holdings ต้อง finite และ >= 0")
    except (KeyError, TypeError, ValueError) as exc:
        return _persist_error(
            ck, run_id, "NOT_PLACED",
            ValueError("holdings provenance ตรวจสอบไม่ได้"),
            {"terminal_reason": "invalid holdings provenance"})
    drift = abs(fresh_holdings - decision_holdings)
    if drift > tolerance:
        return _stop(ck, run_id, "SUPPRESSED_STATE_CHANGED",
                     {"holdings_drift": drift, "dispatch_holdings": fresh["holdings"]},
                     holdings_drift=drift)

    try:
        quote_safety = _dispatch_quote_safety(
            cfg, intent, fresh,
            max_price_drift_bps=max_price_drift_bps,
            max_quote_age_seconds=max_quote_age_seconds,
            max_decision_age_seconds=max_decision_age_seconds)
    except ValueError as exc:
        # Unlike a deploy setting, incomplete/tampered intent provenance cannot
        # heal on retry and must not occupy the queue forever.
        return _persist_error(
            ck, run_id, "NOT_PLACED", exc,
            {"terminal_reason": "invalid dispatch provenance"})
    if not quote_safety["ok"]:
        return _reject_unsafe_dispatch_quote(
            ck, run_id, quote_safety, phase="pre_preview")

    env = environment_label()
    # Audit-only on purpose: the outbox already holds every one of these fields
    # from put_intent, and begin_place_attempt below is the authoritative outbox
    # write for this step. Mirroring them here too would spend three extra RTDB
    # transactions on the hot money path to restate what is already there.
    write_order_audit(run_id, {
        "run_id": run_id, "chain_key": ck, "side": intent["side"],
        "quantity": float(intent["quantity"]), "symbol": cfg.symbol,
        "environment": env, "status": "PENDING_DISPATCH", "realized": False,
        "created_at": intent["created_at"],
        "audit_revision": int(intent.get("audit_revision", 0)),
        "dispatch_quote_check": {**quote_safety, "dispatch_check_phase": "pre_preview"},
    })
    try:
        # Building the payload is part of the gate: a quantity that cannot be
        # expressed at this precision must end the intent, not abort the whole
        # worker run and leave the other intents of this tick unprocessed.
        if is_fractional_quantity(intent["quantity"]):
            capability = None
            if "instrument_capability" in intent and isinstance(intent["instrument_capability"], dict):
                cap_data = intent["instrument_capability"]
                try:
                    capability = InstrumentCapability(
                        symbol=cap_data["symbol"],
                        status=cap_data.get("status", "OC"),
                        category=cap_data.get("category", "US_STOCK"),
                        currency=cap_data.get("currency", "USD"),
                        lot_size=int(cap_data.get("lot_size", 1)),
                        fractionable=bool(cap_data.get("fractionable", False)),
                        quantity_increment=Decimal(str(cap_data["quantity_increment"])) if cap_data.get("quantity_increment") else None,
                        decimal_precision=int(cap_data["decimal_precision"]) if cap_data.get("decimal_precision") is not None else None,
                        minimum_quantity_if_authoritative=Decimal(str(cap_data["minimum_quantity_if_authoritative"])) if cap_data.get("minimum_quantity_if_authoritative") else None,
                        minimum_notional_usd_if_authoritative=Decimal(str(cap_data["minimum_notional_usd_if_authoritative"])) if cap_data.get("minimum_notional_usd_if_authoritative") else None,
                        capability_source=cap_data.get("capability_source", "intent_provenance"),
                    )
                except Exception:
                    capability = None
            if capability is None and (runtime is not None or hasattr(trade_client, "trade_instrument")):
                try:
                    capability = fetch_instrument_capability(trade_client, cfg.symbol)
                except Exception as cap_exc:
                    raise FractionalGateError(
                        f"unable to verify instrument capability for fractional order: {cap_exc}") from cap_exc

            if capability is not None:
                evaluate_fractional_order_gate(
                    capability=capability,
                    side=intent["side"],
                    quantity=intent["quantity"],
                    price=fresh["price"],
                    holdings=fresh["holdings"],
                    order_type="MARKET",
                    at=datetime.now(UTC),
                    session_check_fn=is_regular_session,
                )
            elif runtime is not None:
                raise FractionalGateError("fractional capability unknown: fail closed")

        order = build_order_payload(cfg, intent["side"], float(intent["quantity"]), run_id)
        if runtime is not None:
            preview_result = preview_market_order_result(
                trade_client, order, strict=True)
            funding = validate_preview_funding(
                side=intent["side"], quantity=intent["quantity"],
                holdings=fresh["holdings"], preview=preview_result,
                buying_power=(fetch_buying_power(trade_client)
                              if str(intent["side"]).upper() == "BUY" else None),
            )
            preview_ok = True
            update_intent(ck, run_id, {
                "order_payload": order,
                "payload_hash": canonical_payload_hash(order),
                "preview_result": preview_result,
                "funding_check": funding,
                "preview_at": _iso(datetime.now(UTC)),
                "state": "PREVIEWED",
            })
        else:
            preview_ok = preview_market_order(trade_client, order)
        # Two independent witnesses: the gate judges the committed row, the
        # phrase comes from the intent about to be sent. They are only equal
        # while the outbox still agrees with what the engine decided.
        evaluate_submit_gate(
            env, _committed_row_shape(committed_row), preview_ok,
            order_confirmation_phrase(_pending_row_shape(intent)),
            committed=True,
            release_authorized=(
                None if runtime is None
                else runtime.deployment.release_is_authorized
            ),
        )
    except Exception as exc:
        return _persist_error(ck, run_id, "NOT_PLACED", exc)

    # Preview is a network call and can take long enough for both price and
    # position/open orders to change. Fetch independent evidence again after it returns;
    # this is the snapshot that guards the durable chain fence and broker call.
    if runtime is not None:
        try:
            final_open_orders = fetch_open_orders(trade_client, cfg.symbol)
        except IncompleteOpenOrdersError as exc:
            return _persist_error(
                ck, run_id, "PENDING_DISPATCH", exc,
                {"pagination_complete": False,
                 "dispatch_check_phase": "post_preview"})
        if final_open_orders:
            return _stop(
                ck, run_id, "SUPPRESSED_ACTIVE_ORDER",
                {"terminal_reason":
                 f"{len(final_open_orders)} active broker order(s) after Preview",
                 "dispatch_check_phase": "post_preview"})
    try:
        final_fresh = fetch_snapshot(trade_client, data_client, cfg)
    except Exception as exc:
        return _persist_error(
            ck, run_id, "PENDING_DISPATCH", exc,
            {"dispatch_check_phase": "post_preview_refetch"})
    try:
        final_holdings = float(final_fresh["holdings"])
        if not (math.isfinite(final_holdings) and final_holdings >= 0):
            raise ValueError("holdings ต้อง finite และ >= 0")
    except (KeyError, TypeError, ValueError) as exc:
        return _persist_error(
            ck, run_id, "NOT_PLACED",
            ValueError("holdings provenance ตรวจสอบไม่ได้"),
            {"terminal_reason": "invalid holdings provenance",
             "dispatch_check_phase": "post_preview"})
    final_holdings_drift = abs(final_holdings - decision_holdings)
    if final_holdings_drift > tolerance:
        return _stop(
            ck, run_id, "SUPPRESSED_STATE_CHANGED",
            {"holdings_drift": final_holdings_drift,
             "dispatch_holdings": final_fresh["holdings"],
             "dispatch_check_phase": "post_preview",
             "terminal_reason": "fresh holdings invalidated committed intent"},
            holdings_drift=final_holdings_drift)
    try:
        final_quote_safety = _dispatch_quote_safety(
            cfg, intent, final_fresh,
            max_price_drift_bps=max_price_drift_bps,
            max_quote_age_seconds=max_quote_age_seconds,
            max_decision_age_seconds=max_decision_age_seconds)
    except ValueError as exc:
        return _persist_error(
            ck, run_id, "NOT_PLACED", exc,
            {"terminal_reason": "invalid dispatch provenance",
             "dispatch_check_phase": "post_preview"})
    if not final_quote_safety["ok"]:
        return _reject_unsafe_dispatch_quote(
            ck, run_id, final_quote_safety, phase="post_preview")

    # The per-intent claim below prevents a duplicate client_order_id.  This
    # second, chain-wide fence prevents two workers that claimed different
    # run_ids from both crossing the money boundary after observing the same
    # empty open-order snapshot.
    if not dispatch_claim:
        return _persist_error(
            ck, run_id, "PENDING_DISPATCH",
            RuntimeError("ไม่มี chain dispatch lease — ห้าม place order"))
    dispatch_scope = str(dispatch_claim.get("dispatch_scope_key") or ck)
    tick_runtime.require_budget(12.0)
    try:
        fenced = fence_chain_dispatch(
            dispatch_scope, run_id, str(dispatch_claim.get("owner") or ""),
            str(dispatch_claim.get("claim_token") or ""),
            intent_chain_key=ck,
            lease_seconds=120 if runtime is not None else None)
    except TypeError as exc:
        # Compatibility for injected pre-v2 test doubles only. Production's
        # implementation accepts intent_chain_key and persists it atomically.
        if "intent_chain_key" not in str(exc):
            raise
        fenced = fence_chain_dispatch(
            dispatch_scope, run_id, str(dispatch_claim.get("owner") or ""),
            str(dispatch_claim.get("claim_token") or ""))
    if fenced is None:
        # Another generation owns the chain now.  Leave the intent actionable;
        # that owner will process it, and a stale worker must not make this
        # recoverable hand-off terminal.
        return {"run_id": run_id, "status": "PENDING_DISPATCH",
                "dispatch_fence_lost": True}
    # The outer worker may clear this durable run fence only after it reads a
    # safe terminal status back from the authoritative outbox document.
    dispatch_claim.update(fenced)

    # fence_chain_dispatch is another remote transaction. Recompute age from
    # the broker's source timestamp after it returns so a delayed fence cannot
    # carry an expired quote across the irreversible boundary.
    try:
        deadline_safety = _dispatch_quote_safety(
            cfg, intent, final_fresh,
            max_price_drift_bps=max_price_drift_bps,
            max_quote_age_seconds=max_quote_age_seconds,
            max_decision_age_seconds=max_decision_age_seconds)
    except ValueError as exc:
        return _persist_error(
            ck, run_id, "NOT_PLACED", exc,
            {"terminal_reason": "dispatch evidence expired before place",
             "dispatch_check_phase": "pre_place_deadline"})
    if not deadline_safety["ok"]:
        return _reject_unsafe_dispatch_quote(
            ck, run_id, deadline_safety, phase="pre_place_deadline")

    if tick_runtime.remaining() is not None and tick_runtime.remaining() < 10.0:
        return _stop(ck, run_id, "NOT_PLACED", {
            "terminal_reason": "tick budget exhausted before Place; no broker attempt"})
    if runtime is not None:
        token_block = new_order_token_block(token_health(), runtime.deployment.environment)
        if token_block:
            return _stop(ck, run_id, "NOT_PLACED", {
                "terminal_reason": token_block, "token_preflight_blocked": True},
                token_preflight_blocked=True)
    started = begin_place_attempt(
        ck, run_id, str(intent.get("claim_owner") or ""),
        int(intent.get("claim_generation", 0) or 0))
    if started is None:
        # The lease expired and another generation won before this worker reached
        # the irreversible call.  Do not place; that winner owns reconciliation.
        return {"run_id": run_id, "status": normalize_status(intent.get("status"))}
    _mirror_order_audit(ck, run_id, started)
    try:
        place_res = place_market_order(trade_client, order)
        if runtime is not None:
            validate_place_response(place_res, run_id)
        summary = _poll_order_status(trade_client, run_id, place_res,
                                     expected_symbol=cfg.symbol if runtime is not None else None)
    except Exception as exc:
        # Same open question as a failed reconcile — "does this order exist?" —
        # so it draws on the same bounded budget.
        return _persist_reconcile_failure(intent, exc)
    return _finish_with_realized(trade_client, cfg, intent, summary)


def _config_for_intent(cfg, intent: dict):
    """Recover using the immutable strategy contract that created the intent."""
    snapshot = intent.get("strategy_config")
    intent_chain = str(intent.get("chain_key") or "")
    if isinstance(snapshot, dict):
        try:
            resolved = Config(**snapshot)
        except (TypeError, ValueError) as exc:
            raise RuntimeIdentityError("outbox strategy_config ไม่ถูกต้อง") from exc
        if chain_key(resolved) != intent_chain:
            raise RuntimeIdentityError("outbox strategy_config ไม่ตรงกับ chain_key")
        return resolved
    if chain_key(cfg) != intent_chain:
        raise RuntimeIdentityError(
            "legacy outbox คนละ chain และไม่มี strategy_config สำหรับ recovery")
    return cfg


def _run_order_worker(cfg, limit: int = 3,
                      runtime_identity: str | None = None,
                      runtime: RuntimeConfig | None = None) -> dict:
    runtime_identity = runtime_identity or runtime_identity_fingerprint()
    ck = chain_key(cfg)
    tick_runtime.require_budget()
    dispatch_scope = account_symbol_fence_key(runtime_identity, cfg.symbol)
    # Both guards below read the same document, so read it once and hand it down.
    state = read_chain_state(cfg)
    state_verified = chain_runtime_identity_is_verified(
        cfg, runtime_identity, state=state)
    _announce_identity_adoption(
        verify_runtime_identity(state, runtime_identity), runtime_identity)
    _recover_pending_order_intents(cfg, runtime_identity, state=state)
    _repair_pending_audits(ck)
    expired = expire_unsent_before(ck, datetime.now(UTC))
    if expired:
        _repair_pending_audits(ck)
    results = []
    worker_id = uuid.uuid4().hex
    candidates = list_actionable(ck, limit=limit)
    if candidates and not state_verified:
        raise RuntimeIdentityError(
            "พบ outbox แต่ไม่พบ chain state สำหรับยืนยัน account/environment")
    dispatch_claim = claim_chain_dispatch(
        dispatch_scope, worker_id,
        lease_seconds=120 if runtime is not None else None)
    if dispatch_claim is None:
        logger.info("lego_order_worker chain dispatch lease busy chain_key=%s", ck)
        return {"processed": 0, "actionable": len(candidates),
                "expired_unsent": expired, "dispatch_locked": True,
                "results": []}
    try:
        dispatch_claim["dispatch_scope_key"] = dispatch_scope
        # One-release migration: workers deployed before the account+symbol
        # fence stored an unresolved run below the strategy chain key. Adopt
        # that fence before looking at new work so an in-flight broker call
        # cannot disappear during the rollout.
        if dispatch_scope != ck and not dispatch_claim.get("inflight_run_id"):
            legacy_dispatch = db.reference(
                f"{DISPATCH_LOCK_PATH}/{ck}").get() or {}
            legacy_inflight = (str(legacy_dispatch.get("inflight_run_id") or "")
                               if isinstance(legacy_dispatch, dict) else "")
            if legacy_inflight:
                adopted = fence_chain_dispatch(
                    dispatch_scope, legacy_inflight, worker_id,
                    str(dispatch_claim.get("claim_token") or ""),
                    intent_chain_key=str(
                        legacy_dispatch.get("inflight_chain_key") or ck),
                    lease_seconds=120 if runtime is not None else None)
                if adopted is None:
                    return {"processed": 0, "actionable": len(candidates),
                            "expired_unsent": expired,
                            "dispatch_locked": True, "results": []}
                dispatch_claim.update(adopted)
        # The owner lease may expire, but the run that may already have crossed
        # place_order does not. A successor is allowed to reconcile only that
        # run until the outbox proves a safe terminal result.
        inflight_run_id = str(dispatch_claim.get("inflight_run_id") or "")
        if inflight_run_id:
            inflight_chain = str(dispatch_claim.get("inflight_chain_key") or ck)
            inflight = read_intent(inflight_chain, inflight_run_id)
            if inflight is None:
                return {
                    "processed": 0,
                    "actionable": len(candidates),
                    "expired_unsent": expired,
                    "dispatch_blocked": True,
                    "dispatch_inflight_run_id": inflight_run_id,
                    "dispatch_block_reason": "inflight outbox intent is missing",
                    "results": [],
                }
            inflight_status = normalize_status(inflight.get("status"))
            if _chain_fence_can_clear(inflight):
                # Repair an older terminal record before releasing its fence.
                broker_circuit.record_outcome(inflight, inflight)
                if not clear_chain_dispatch_inflight(
                        dispatch_scope, inflight_run_id, worker_id,
                        str(dispatch_claim.get("claim_token") or "")):
                    return {
                        "processed": 0,
                        "actionable": len(candidates),
                        "expired_unsent": expired,
                        "dispatch_locked": True,
                        "results": [],
                    }
                for key in ("inflight_run_id", "place_fence",
                            "inflight_chain_key", "fenced_run_id", "fenced_at"):
                    dispatch_claim.pop(key, None)
            elif inflight_status in OUTBOX_TERMINAL:
                # Queue-terminal can still mean broker ambiguity or a broken
                # strategy ledger. Keep the fence until a human reconciles it.
                return {
                    "processed": 0,
                    "actionable": len(candidates),
                    "expired_unsent": expired,
                    "dispatch_blocked": True,
                    "dispatch_inflight_run_id": inflight_run_id,
                    "dispatch_block_reason": "execution or ledger needs manual reconciliation",
                    "results": [],
                }
            else:
                if inflight_status == "PENDING_DISPATCH":
                    # Crash between the durable chain fence and the per-intent
                    # PLACING_UNKNOWN write: make the ambiguity explicit before
                    # an expired prior worker can resume its place path.
                    inflight = update_intent(inflight_chain, inflight_run_id, {
                        "status": "PLACING_UNKNOWN",
                        "place_attempted": True,
                        "recovered_chain_fence": True,
                    })
                candidates = [inflight]

        if not candidates:
            # The account+symbol fence must be inspected even when the current
            # config chain is idle: it may point at an unresolved intent from a
            # previous config chain. Only after that check is it safe to avoid
            # the broker authentication calls for a genuinely idle tick.
            logger.info("lego_order_worker actionable=0 expired_unsent=%d "
                        "chain_key=%s — no order intent to dispatch", expired, ck)
            return {"processed": 0, "actionable": 0,
                    "expired_unsent": expired, "results": []}

        trade_client, data_client = build_clients()
        for intent in candidates:
            tick_runtime.require_budget()
            intent_chain = str(intent.get("chain_key") or ck)
            claimed = claim_intent(
                intent_chain, intent["run_id"], worker_id,
                lease_seconds=120 if runtime is not None else None)
            if claimed is None:
                continue
            try:
                intent_identity = claimed.get("runtime_identity_fingerprint")
                if (intent_identity is not None
                        and str(intent_identity) != runtime_identity):
                    raise RuntimeIdentityError(
                        "outbox runtime identity ไม่ตรงกับ worker "
                        "(account/environment คนละชุด)")
                intent_cfg = _config_for_intent(cfg, claimed)
                result = _dispatch_or_reconcile_one(
                    trade_client, data_client, intent_cfg, claimed, dispatch_claim,
                    runtime=runtime)
                results.append(result)
            finally:
                release_intent_claim(
                    intent_chain, intent["run_id"], worker_id)

            current_inflight = str(dispatch_claim.get("inflight_run_id") or "")
            if not current_inflight:
                continue
            current_inflight_chain = str(
                dispatch_claim.get("inflight_chain_key") or intent_chain)
            authoritative = read_intent(
                current_inflight_chain, current_inflight)
            if (authoritative is not None
                    and _chain_fence_can_clear(authoritative)
                    and clear_chain_dispatch_inflight(
                        dispatch_scope, current_inflight, worker_id,
                        str(dispatch_claim.get("claim_token") or ""))):
                for key in ("inflight_run_id", "place_fence",
                            "inflight_chain_key", "fenced_run_id", "fenced_at"):
                    dispatch_claim.pop(key, None)
                continue
            # Broker-unknown and manual ledger terminals preserve the run fence.
            # Later intents wait until both execution and accounting are safe.
            break
    finally:
        release_chain_dispatch(
            dispatch_scope, worker_id,
            str(dispatch_claim.get("claim_token") or ""))
    logger.info("lego_order_worker actionable=%d processed=%d expired_unsent=%d "
                "statuses=%s", len(candidates), len(results), expired,
                [r.get("status") for r in results])
    return {"processed": len(results), "actionable": len(candidates),
            "expired_unsent": expired, "results": results}


def run_http(request, deps) -> tuple[dict, int]:
    """Translate an HTTP worker invocation into execution orchestration."""
    deps._init_firebase()
    try:
        cfg = deps.load_config()
        deps.environment_label()
        runtime_identity = deps.runtime_identity_fingerprint()
        limit = int(deps.os.environ.get("LEGO_ORDER_WORKER_LIMIT", "3"))
        deps._nonnegative_finite_env("LEGO_HOLDINGS_DRIFT_TOLERANCE", 0.000001)
        deps._nonnegative_finite_env(
            "LEGO_MAX_DISPATCH_PRICE_DRIFT_BPS",
            deps.DEFAULT_MAX_DISPATCH_PRICE_DRIFT_BPS)
        deps._nonnegative_finite_env(
            "LEGO_MAX_DISPATCH_QUOTE_AGE_SECONDS",
            deps.DEFAULT_MAX_DISPATCH_QUOTE_AGE_SECONDS)
    except (KeyError, ValueError) as exc:
        return {
            "pipeline_status": "CONFIG_ERROR",
            "error": deps._error_text(exc, with_type=False),
        }, 500
    try:
        return {
            "pipeline_status": "ORDER_WORKER_OK",
            **deps._run_order_worker(
                cfg, limit, runtime_identity=runtime_identity),
        }, 200
    except deps.RuntimeIdentityError as exc:
        return {
            "pipeline_status": "CONFIG_ERROR",
            "error": deps._error_text(exc, with_type=False),
        }, 500
    except Exception as exc:
        return {"pipeline_status": "ORDER_WORKER_ERROR",
                "error": deps._error_text(exc)}, 503

