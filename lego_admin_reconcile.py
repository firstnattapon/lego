"""Fail-closed operator acknowledgement for a manually halted order chain.

This module is deliberately separate from the order worker.  It can read one
broker order and repair the *administrative fence* around that order; it cannot
preview, replace, cancel, place, or advance an order/ledger.  It recomputes the
persisted recurrence only as a read-only integrity proof before releasing money.

The default CLI mode is a dry run.  Applying a plan requires the exact phrase
printed by the dry run.  A short-lived RTDB lease serializes administrators,
while immutable audit phases and absorbing reconciliation ids make every step
safe to replay after a process crash.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import firebase_admin
from firebase_admin import credentials, db

from lego_one_row import (ACTUAL_COLUMN, COLUMN_ORDER, DELTA_COLUMN,
                          EXCESS_COLUMN, REFERENCE_COLUMN, Config)
from lego_orders import (EXECUTION_PRICE_FIELDS, FEE_FIELDS, TERMINAL_STATUSES,
                         normalize_status, summarize_order_result)
from lego_outbox import (DISPATCH_LOCK_PATH, OUTBOX_PATH, ROWS_PATH,
                         account_symbol_fence_key)
from lego_state import (CASHFLOW_FINALIZED, CASHFLOW_PENDING,
                        CASHFLOW_SEMANTICS, EXECUTION_STATE_KEY,
                        REALIZED_FIFO_SCHEMA_VERSION,
                        REALIZED_EVENT_ARCHIVE_PATH, REALIZED_PATH,
                        STATE_PATH, chain_key as strategy_chain_key,
                        config_hash, realized_open_legs_hash)
from webull_io import (build_clients, fetch_order_detail,
                       load_config, redact_sensitive_text,
                       runtime_identity_fingerprint)


UTC = timezone.utc
ADMIN_AUDIT_PATH = "webull_lego_admin_reconcile_audit"
DEFAULT_ADMIN_LEASE_SECONDS = 300
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
_QTY_TOLERANCE = 1e-9
_PRICE_TOLERANCE = 1e-8
_ROW_PRICE_COLUMN = COLUMN_ORDER[5]


class ReconcileRefusal(RuntimeError):
    """The evidence is not strong enough to release the money fence."""


class ReconcileIncomplete(RuntimeError):
    """The acknowledgement started safely but needs an idempotent replay."""


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _float(value: Any, *, allow_zero: bool = True) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0 or (not allow_zero and number <= 0):
        return None
    return number


def _finite_float(value: Any) -> float | None:
    """A finite signed number (A/ΔA/E/R may legitimately be negative)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _same_number(left: Any, right: Any, tolerance: float) -> bool:
    try:
        a = float(left)
        b = float(right)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(a) and math.isfinite(b)):
        return False
    return math.isclose(a, b, rel_tol=tolerance, abs_tol=tolerance)


def _strict_positive_int(value: Any) -> int | None:
    """Accept an integer spelling, but never bool/fractional/zero evidence."""
    if isinstance(value, bool):
        return None
    number = _float(value, allow_zero=False)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _strict_nonnegative_int(value: Any) -> int | None:
    """Accept zero/integer cursor spellings, never bool or fractions."""
    if isinstance(value, bool):
        return None
    number = _float(value)
    if number is None or number < 0 or not number.is_integer():
        return None
    return int(number)


def _quantity_text(value: float) -> str:
    """Canonical non-exponent spelling used in the mandatory phrase."""
    try:
        text = format(Decimal(str(value)).normalize(), "f")
    except (InvalidOperation, ValueError) as exc:  # pragma: no cover - defensive
        raise ReconcileRefusal("broker filled quantity is not canonical") from exc
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _valid_identifier(name: str, value: str) -> str:
    value = str(value or "")
    if not _IDENTIFIER.fullmatch(value):
        raise ReconcileRefusal(
            f"{name} must be 1-256 characters from A-Z, a-z, 0-9, _ . : -")
    return value


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False,
                     separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _admin_clear_recorded(lock: Any, event_id: str, run_id: str) -> bool:
    """Prove that this exact admin event durably cleared its chain fence.

    New locks retain one immutable marker per reconciliation event so a later
    event cannot erase crash-replay evidence for an older one.  The scalar
    marker remains readable only for locks written by the previous schema.
    """
    if not isinstance(lock, dict):
        return False
    events = lock.get("admin_reconciliation_events")
    if events is None:
        return hmac.compare_digest(
            str(lock.get("last_admin_reconciliation_id") or ""), event_id)
    if not isinstance(events, dict):
        return False
    marker = events.get(event_id)
    if not isinstance(marker, dict) or not hmac.compare_digest(
            str(marker.get("event_id") or ""), event_id):
        return False
    if marker.get("schema") == "admin_fence_clear_legacy_v1":
        # The previous schema recorded only the event id.  Migration preserves
        # exactly that proof strength instead of inventing a run association.
        return _parse_utc(marker.get("migrated_at")) is not None
    return (marker.get("schema") == "admin_fence_clear_v1"
            and hmac.compare_digest(str(marker.get("run_id") or ""), run_id)
            and _parse_utc(marker.get("cleared_at")) is not None)


def _strict_order_fields(detail: Any) -> dict:
    """Unwrap one order without silently choosing among multiple records."""
    current = detail
    if isinstance(current, list):
        if len(current) != 1 or not isinstance(current[0], dict):
            raise ReconcileRefusal("broker detail must contain exactly one order")
        current = current[0]
    if not isinstance(current, dict):
        raise ReconcileRefusal("broker detail is not an object")
    fields = dict(current)
    # SDK revisions have wrapped the same record as data, data.orders, and
    # items. Walk only known wrappers and still insist on exactly one order.
    for _depth in range(4):
        unwrapped = False
        for key in ("items", "orders", "data"):
            inner = fields.get(key)
            if isinstance(inner, list):
                if len(inner) != 1 or not isinstance(inner[0], dict):
                    raise ReconcileRefusal(
                        "broker detail contains an ambiguous order list")
                fields.pop(key, None)
                fields.update(inner[0])
                unwrapped = True
                break
            if isinstance(inner, dict):
                fields.pop(key, None)
                fields.update(inner)
                unwrapped = True
                break
        if not unwrapped:
            break
    return fields


def _first(fields: dict, names: tuple[str, ...]) -> Any:
    for name in names:
        value = fields.get(name)
        if value not in (None, ""):
            return value
    return None


def broker_evidence(detail: Any, *, expected_run_id: str,
                    expected_account_id: str) -> dict:
    """Normalize only explicit evidence; absent fill quantity never means zero."""
    fields = _strict_order_fields(detail)
    summary = summarize_order_result({}, fields)
    status = normalize_status(summary.get("status")) or "UNKNOWN"
    raw_quantity = _first(fields, ("filled_quantity", "filled_qty"))
    quantity = _float(raw_quantity)
    raw_price = _first(fields, EXECUTION_PRICE_FIELDS)
    price = (_float(raw_price, allow_zero=False)
             if raw_price not in (None, "") else None)
    raw_fee = _first(fields, FEE_FIELDS)
    fee = _float(raw_fee) if raw_fee not in (None, "") else None
    broker_id = str(_first(fields, (
        "order_id", "orderId", "broker_order_id", "brokerOrderId")) or "")
    client_order_id = str(_first(fields, (
        "client_order_id", "clientOrderId", "client_order_no",
        "client_order_number")) or "")
    account_id = str(_first(fields, (
        "account_id", "accountId", "account_no", "account_number")) or "")
    side = normalize_status(_first(fields, ("side", "order_side")))
    symbol = str(_first(fields, ("symbol", "ticker")) or "")

    blockers: list[str] = []
    if status not in TERMINAL_STATUSES:
        blockers.append(f"broker_status_not_terminal:{status}")
    if raw_quantity is None:
        blockers.append("broker_filled_quantity_missing")
    elif quantity is None:
        blockers.append("broker_filled_quantity_invalid")
    if not broker_id:
        blockers.append("broker_order_id_missing")
    else:
        try:
            _valid_identifier("broker order id", broker_id)
        except ReconcileRefusal:
            blockers.append("broker_order_id_invalid")
    if client_order_id and not hmac.compare_digest(client_order_id, expected_run_id):
        blockers.append("broker_client_order_id_mismatch")
    if account_id and not hmac.compare_digest(account_id, expected_account_id):
        blockers.append("broker_account_mismatch")
    if status == "FILLED" and quantity == 0:
        blockers.append("filled_status_with_zero_quantity")
    if quantity is not None and quantity > 0 and price is None:
        blockers.append("broker_filled_price_missing")
    if raw_fee not in (None, "") and fee is None:
        blockers.append("broker_filled_fee_invalid")

    return {
        "status": status,
        "filled_quantity": quantity,
        "filled_price": price,
        "filled_fee": fee,
        "broker_id": broker_id,
        "client_order_id": client_order_id,
        "account_id_present": bool(account_id),
        "side": side,
        "symbol": symbol,
        "blockers": blockers,
        "detail_hash": _canonical_hash(fields),
    }


def _active_lease_blockers(doc: dict, *, now_utc: datetime,
                           owner_field: str, until_field: str,
                           label: str, allowed_owner: str = "") -> list[str]:
    owner = str(doc.get(owner_field) or "")
    until_raw = doc.get(until_field)
    if not owner:
        return [f"{label}_lease_without_owner"] if until_raw else []
    until = _parse_utc(until_raw)
    if until is None:
        return [f"{label}_owner_lease_unparseable"]
    if allowed_owner and hmac.compare_digest(owner, allowed_owner):
        return ([] if until > now_utc else
                [f"{label}_owner_lease_expired_during_apply"])
    return [f"{label}_owner_lease_active"] if until > now_utc else []


def _row_reference_proof(cfg: Config, state: dict, row: dict) \
        -> tuple[list[str], float | None]:
    """Prove the row's quoted Rₙ from current config, P₀ and row quote."""
    blockers: list[str] = []
    p0 = _float(state.get("p0"), allow_zero=False)
    quote_price = _float(row.get(_ROW_PRICE_COLUMN), allow_zero=False)
    reference = _finite_float(row.get(REFERENCE_COLUMN))
    if p0 is None:
        blockers.append("state_p0_invalid")
    if quote_price is None:
        blockers.append("row_quote_price_invalid")
    if reference is None:
        blockers.append("row_reference_invalid")
    if p0 is not None and quote_price is not None and reference is not None:
        expected = cfg.fix_c * math.log(quote_price / p0)
        if not _same_number(reference, expected, _PRICE_TOLERANCE):
            blockers.append("row_reference_equation_mismatch")
    return blockers, reference


def _positive_fill_blockers(*, cfg: Config, chain_key: str, run_id: str,
                             intent: dict,
                             state: dict, row: dict, realized: dict,
                             evidence: dict) -> list[str]:
    blockers: list[str] = []
    row_reference_blockers, row_reference = _row_reference_proof(cfg, state, row)
    blockers.extend(row_reference_blockers)
    qty = evidence["filled_quantity"]
    price = evidence["filled_price"]
    if intent.get("cashflow_finalized") is not True:
        blockers.append("intent_cashflow_not_finalized")
    if intent.get("realized") is not True:
        blockers.append("intent_realized_not_finalized")
    if not _same_number(intent.get("filled_quantity"), qty, _QTY_TOLERANCE):
        blockers.append("intent_broker_quantity_mismatch")
    if not _same_number(intent.get("filled_price"), price, _PRICE_TOLERANCE):
        blockers.append("intent_broker_price_mismatch")

    if not isinstance(row, dict) or row.get("committed") is not True:
        blockers.append("committed_row_missing")
    else:
        if row.get("chain_key") not in (None, chain_key):
            blockers.append("row_chain_mismatch")
        if str(row.get("run_id") or run_id) != run_id:
            blockers.append("row_run_id_mismatch")
        if row.get("cashflow_status") != CASHFLOW_FINALIZED:
            blockers.append("row_cashflow_not_finalized")
        if not _same_number(row.get("execution_quantity"), qty, _QTY_TOLERANCE):
            blockers.append("row_execution_quantity_mismatch")
        if not _same_number(row.get("execution_price"), price, _PRICE_TOLERANCE):
            blockers.append("row_execution_price_mismatch")

    cashflow = (state.get(EXECUTION_STATE_KEY) or {}) if isinstance(state, dict) else {}
    final = (cashflow.get("finalized_runs") or {}).get(run_id)
    if not isinstance(final, dict):
        blockers.append("model_cashflow_finalization_missing")
    else:
        if not _same_number(final.get("filled_quantity"), qty, _QTY_TOLERANCE):
            blockers.append("model_cashflow_quantity_mismatch")
        if not _same_number(final.get("filled_price"), price, _PRICE_TOLERANCE):
            blockers.append("model_cashflow_price_mismatch")
        final_holdings = _float(final.get("holdings_after"))
        if final_holdings is None:
            blockers.append("model_cashflow_holdings_invalid")
        else:
            if not _same_number(row.get("post_execution_holdings"),
                                final_holdings, _QTY_TOLERANCE):
                blockers.append("row_post_execution_holdings_mismatch")
            if not _same_number(state.get("prev_holdings"), final_holdings,
                                _QTY_TOLERANCE):
                blockers.append("state_prev_holdings_mismatch")
        for row_key, final_key in (
                (DELTA_COLUMN, "delta_actual"),
                (ACTUAL_COLUMN, "actual_cumulative"),
                (EXCESS_COLUMN, "excess")):
            if not _same_number(row.get(row_key), final.get(final_key),
                                _PRICE_TOLERANCE):
                blockers.append(f"model_cashflow_{final_key}_mismatch")

        final_reference = _finite_float(final.get("reference"))
        previous_price = _float(
            final.get("previous_action_price"), allow_zero=False)
        previous_actual = _finite_float(
            final.get("previous_actual_cumulative"))
        final_delta = _finite_float(final.get("delta_actual"))
        final_actual = _finite_float(final.get("actual_cumulative"))
        final_excess = _finite_float(final.get("excess"))
        if final_reference is None:
            blockers.append("model_cashflow_reference_invalid")
        elif row_reference is None or not _same_number(
                final_reference, row_reference, _PRICE_TOLERANCE):
            blockers.append("model_cashflow_reference_mismatch")
        if previous_price is None:
            blockers.append("model_cashflow_previous_action_price_invalid")
        if previous_actual is None:
            blockers.append("model_cashflow_previous_actual_invalid")
        if final_delta is None:
            blockers.append("model_cashflow_delta_actual_invalid")
        if final_actual is None:
            blockers.append("model_cashflow_actual_cumulative_invalid")
        if final_excess is None:
            blockers.append("model_cashflow_excess_invalid")
        if previous_price is not None and final_delta is not None:
            expected_delta = cfg.fix_c * (price / previous_price - 1.0)
            if not _same_number(final_delta, expected_delta, _PRICE_TOLERANCE):
                blockers.append("model_cashflow_delta_equation_mismatch")
        if (previous_actual is not None and final_delta is not None
                and final_actual is not None
                and not _same_number(final_actual,
                                     previous_actual + final_delta,
                                     _PRICE_TOLERANCE)):
            blockers.append("model_cashflow_actual_equation_mismatch")
        if (final_actual is not None and final_reference is not None
                and final_excess is not None
                and not _same_number(final_excess,
                                     final_actual - final_reference,
                                     _PRICE_TOLERANCE)):
            blockers.append("model_cashflow_excess_equation_mismatch")

        # The dispatch lock has prevented every later order on this chain.  A
        # positive-fill acknowledgement is therefore safe only when this exact
        # run is also the durable ledger head, not merely an entry in history.
        # This closes a corrupt-state path where releasing the fence would let
        # the next recurrence start from top-level values that disagree with the
        # row/finalized_runs witness we just inspected.
        final_seq = _strict_positive_int(final.get("seq"))
        if final_seq is None:
            blockers.append("model_cashflow_finalized_run_seq_invalid")
        if str(cashflow.get("last_finalized_run_id") or "") != run_id:
            blockers.append("model_cashflow_latest_run_mismatch")
        top_seq = _strict_positive_int(cashflow.get("finalized_seq"))
        if final_seq is None or top_seq != final_seq:
            blockers.append("model_cashflow_finalized_seq_mismatch")
        if not _same_number(cashflow.get("actual_cumulative"),
                            final.get("actual_cumulative"), _PRICE_TOLERANCE):
            blockers.append("model_cashflow_actual_cumulative_mismatch")
        if not _same_number(cashflow.get("last_action_price"),
                            final.get("filled_price"), _PRICE_TOLERANCE):
            blockers.append("model_cashflow_last_action_price_mismatch")
        if not _same_number(state.get("prev_actual"),
                            final.get("actual_cumulative"), _PRICE_TOLERANCE):
            blockers.append("state_prev_actual_mismatch")
        if not _same_number(state.get("prev_price"), final.get("filled_price"),
                            _PRICE_TOLERANCE):
            blockers.append("state_prev_price_mismatch")
        finalized_at = str(final.get("at") or "")
        if not finalized_at:
            blockers.append("model_cashflow_finalized_at_missing")
        elif str(row.get("cashflow_finalized_at") or "") != finalized_at:
            blockers.append("row_cashflow_finalized_at_mismatch")

        # Every current run after the first must chain from the immediately
        # preceding finalized record, which is always retained beside it in the
        # bounded history. This catches a self-consistent but detached rewrite of
        # previous_action_price/previous_actual_cumulative.
        if final_seq is not None and final_seq > 1:
            prior_records = [record for other_run, record in
                             (cashflow.get("finalized_runs") or {}).items()
                             if other_run != run_id and isinstance(record, dict)
                             and _strict_positive_int(record.get("seq"))
                             == final_seq - 1]
            if len(prior_records) != 1:
                blockers.append("model_cashflow_previous_run_missing_or_ambiguous")
            else:
                prior = prior_records[0]
                if not _same_number(previous_price, prior.get("filled_price"),
                                    _PRICE_TOLERANCE):
                    blockers.append("model_cashflow_previous_price_mismatch")
                if not _same_number(previous_actual,
                                    prior.get("actual_cumulative"),
                                    _PRICE_TOLERANCE):
                    blockers.append("model_cashflow_previous_actual_mismatch")

    applied = (realized.get("applied_fills") or {}).get(run_id) \
        if isinstance(realized, dict) else None
    if not isinstance(applied, dict):
        blockers.append("realized_ledger_fill_missing")
    else:
        if not _same_number(applied.get("quantity"), qty, _QTY_TOLERANCE):
            blockers.append("realized_ledger_quantity_mismatch")
        if not _same_number(applied.get("average_price"), price,
                            _PRICE_TOLERANCE):
            blockers.append("realized_ledger_price_mismatch")
        if normalize_status(applied.get("side")) != normalize_status(intent.get("side")):
            blockers.append("realized_ledger_side_mismatch")
        broker_fee = evidence.get("filled_fee")
        intent_fee_raw = intent.get("filled_fee")
        intent_fee = (_float(intent_fee_raw)
                      if intent_fee_raw not in (None, "") else None)
        if intent_fee_raw not in (None, "") and intent_fee is None:
            blockers.append("intent_filled_fee_invalid")
        expected_fee = (broker_fee if broker_fee is not None
                        else intent_fee if intent_fee is not None else 0.0)
        if not _same_number(applied.get("fee"), expected_fee, _PRICE_TOLERANCE):
            blockers.append("realized_ledger_fee_mismatch")
        # apply_realized_fill writes these witnesses in the same RTDB
        # transaction as FIFO open_legs and cumulative P&L.  Merely finding an
        # applied_fills entry is insufficient: a crash/corrupt write with a
        # missing ledger head would make the next opposite-side fill match the
        # wrong lots after this chain fence is released.
        applied_seq = _strict_positive_int(applied.get("seq"))
        top_applied_seq = _strict_positive_int(realized.get("applied_seq"))
        if applied_seq is None:
            blockers.append("realized_ledger_fill_seq_invalid")
        if applied_seq is None or top_applied_seq != applied_seq:
            blockers.append("realized_ledger_applied_seq_mismatch")
        if str(realized.get("last_event_id") or "") != run_id:
            blockers.append("realized_ledger_latest_event_mismatch")
        if not _same_number(applied.get("cumulative_realized_after"),
                            realized.get("cumulative_realized"),
                            _PRICE_TOLERANCE):
            blockers.append("realized_ledger_cumulative_mismatch")
        if not _same_number(applied.get("realized_delta"),
                            applied.get("realized_delta"), _PRICE_TOLERANCE):
            blockers.append("realized_ledger_event_delta_invalid")
        open_legs = realized.get("open_legs")
        if not isinstance(open_legs, dict):
            blockers.append("realized_ledger_open_legs_missing")
        else:
            try:
                open_legs_hash = realized_open_legs_hash(open_legs)
            except (TypeError, ValueError):
                blockers.append("realized_ledger_open_legs_invalid")
            else:
                if not hmac.compare_digest(
                        str(applied.get("open_legs_after_hash") or ""),
                        open_legs_hash):
                    blockers.append("realized_ledger_open_legs_mismatch")
        if int(realized.get("schema_version", 0) or 0) \
                == REALIZED_FIFO_SCHEMA_VERSION:
            read_cursor = realized.get("fifo_read_cursor")
            write_cursor = realized.get("fifo_write_cursor")
            if not isinstance(read_cursor, dict) or not isinstance(write_cursor, dict):
                blockers.append("realized_fifo_cursor_missing")
            else:
                for side_key in ("buys", "sells"):
                    read_value = _strict_nonnegative_int(read_cursor.get(side_key))
                    write_value = _strict_nonnegative_int(write_cursor.get(side_key))
                    if (read_value is None or write_value is None
                            or read_value > write_value):
                        blockers.append("realized_fifo_cursor_invalid")
                        break
            if realized.get("active_matching_event_id"):
                blockers.append("realized_fifo_matching_pending")
    return blockers


def _zero_fill_blockers(*, cfg: Config, run_id: str, intent: dict, state: dict,
                        row: dict, realized: dict) -> list[str]:
    blockers: list[str] = []
    reference_blockers, _row_reference = _row_reference_proof(cfg, state, row)
    blockers.extend(reference_blockers)
    prior_qty = intent.get("filled_quantity")
    if prior_qty not in (None, "") and (_float(prior_qty) is None
                                         or float(prior_qty) > _QTY_TOLERANCE):
        blockers.append("intent_claims_positive_fill")
    if intent.get("cashflow_finalized") is True:
        blockers.append("zero_fill_intent_cashflow_is_finalized")
    if intent.get("realized") is True:
        blockers.append("zero_fill_intent_realized_is_finalized")
    if isinstance(row, dict):
        if row.get("cashflow_status") != CASHFLOW_PENDING:
            blockers.append("zero_fill_row_not_pending")
        if (row.get("execution_quantity") not in (None, "")
                or row.get("execution_price") not in (None, "")):
            blockers.append("zero_fill_row_has_execution_evidence")
    cashflow = (state.get(EXECUTION_STATE_KEY) or {}) if isinstance(state, dict) else {}
    last_action_price = _float(
        cashflow.get("last_action_price"), allow_zero=False)
    actual_cumulative = _finite_float(cashflow.get("actual_cumulative"))
    p0 = _float(state.get("p0"), allow_zero=False)
    row_delta = _finite_float(row.get(DELTA_COLUMN))
    row_actual = _finite_float(row.get(ACTUAL_COLUMN))
    row_excess = _finite_float(row.get(EXCESS_COLUMN))
    if last_action_price is None:
        blockers.append("zero_fill_last_action_price_invalid")
    if actual_cumulative is None:
        blockers.append("zero_fill_actual_cumulative_invalid")
    if row_delta is None or not _same_number(row_delta, 0.0, _PRICE_TOLERANCE):
        blockers.append("zero_fill_delta_not_frozen")
    if (row_actual is None or actual_cumulative is None
            or not _same_number(row_actual, actual_cumulative,
                                _PRICE_TOLERANCE)):
        blockers.append("zero_fill_actual_not_frozen")
    if (row_excess is None or actual_cumulative is None
            or last_action_price is None or p0 is None):
        blockers.append("zero_fill_excess_invalid")
    else:
        expected_excess = (
            actual_cumulative
            - cfg.fix_c * math.log(last_action_price / p0)
        )
        if not _same_number(row_excess, expected_excess, _PRICE_TOLERANCE):
            blockers.append("zero_fill_excess_not_frozen")
    if not _same_number(state.get("prev_price"), last_action_price,
                        _PRICE_TOLERANCE):
        blockers.append("zero_fill_state_prev_price_mismatch")
    if not _same_number(state.get("prev_actual"), actual_cumulative,
                        _PRICE_TOLERANCE):
        blockers.append("zero_fill_state_prev_actual_mismatch")
    if isinstance((cashflow.get("finalized_runs") or {}).get(run_id), dict):
        blockers.append("zero_fill_model_cashflow_exists")
    applied = (realized.get("applied_fills") or {}).get(run_id) \
        if isinstance(realized, dict) else None
    if isinstance(applied, dict):
        blockers.append("zero_fill_realized_ledger_exists")
    return blockers


def evaluate_reconciliation(*, cfg: Config, chain_key: str, run_id: str,
                            runtime_identity: str, account_id: str,
                            lock: dict | None, intent: dict | None,
                            state: dict | None, row: dict | None,
                            realized: dict | None, broker_detail: Any,
                            now_utc: datetime | None = None,
                            allowed_owner: str = "",
                            dispatch_key: str | None = None) -> dict:
    """Pure, serializable reconciliation decision used by CLI and tests."""
    now_utc = (now_utc or _now()).astimezone(UTC)
    chain_key = _valid_identifier("chain key", chain_key)
    run_id = _valid_identifier("run id", run_id)
    evidence = broker_evidence(
        broker_detail, expected_run_id=run_id, expected_account_id=account_id)
    lock = dict(lock or {})
    intent = dict(intent or {})
    state = dict(state or {})
    row = dict(row or {})
    realized = dict(realized or {})
    blockers = list(evidence["blockers"])
    expected_chain_key = strategy_chain_key(cfg)
    expected_config_hash = config_hash(cfg)
    if not hmac.compare_digest(chain_key, expected_chain_key):
        blockers.append("current_config_chain_mismatch")

    reconciliation = intent.get("admin_reconciliation")
    reconciled_event = (str(reconciliation.get("event_id") or "")
                        if isinstance(reconciliation, dict) else "")

    if not intent:
        blockers.append("outbox_intent_missing")
    else:
        if str(intent.get("chain_key") or "") != chain_key:
            blockers.append("intent_chain_mismatch")
        if str(intent.get("run_id") or "") != run_id:
            blockers.append("intent_run_id_mismatch")
        client_id = str(intent.get("client_order_id") or "")
        if client_id and client_id != run_id:
            blockers.append("intent_client_order_id_mismatch")
        if not hmac.compare_digest(
                str(intent.get("runtime_identity_fingerprint") or ""),
                runtime_identity):
            blockers.append("intent_runtime_identity_mismatch")
    if not state:
        blockers.append("chain_state_missing")
    else:
        if not hmac.compare_digest(
                str(state.get("runtime_identity_fingerprint") or ""),
                runtime_identity):
            blockers.append("state_runtime_identity_mismatch")
        if not hmac.compare_digest(str(state.get("config_hash") or ""),
                                   expected_config_hash):
            blockers.append("state_config_hash_mismatch")
        if str(state.get("symbol") or "").upper() != cfg.symbol.upper():
            blockers.append("state_symbol_mismatch")
        if str(state.get("cashflow_semantics") or "") != CASHFLOW_SEMANTICS:
            blockers.append("state_cashflow_semantics_mismatch")

    inflight = str(lock.get("inflight_run_id") or "")
    replay_candidate = bool(reconciled_event and not inflight)
    if inflight and inflight != run_id:
        blockers.append("wrong_inflight_run")
    elif not inflight and not replay_candidate:
        blockers.append("inflight_run_missing")
    blockers.extend(_active_lease_blockers(
        lock, now_utc=now_utc, owner_field="owner", until_field="lease_until",
        label="dispatch", allowed_owner=allowed_owner))
    blockers.extend(_active_lease_blockers(
        intent, now_utc=now_utc, owner_field="claim_owner",
        until_field="claim_until", label="intent"))

    if row:
        if row.get("committed") is not True:
            blockers.append("source_row_not_committed")
        if row.get("chain_key") not in (None, chain_key):
            blockers.append("row_chain_mismatch")
        if str(row.get("run_id") or run_id) != run_id:
            blockers.append("row_run_id_mismatch")
        if str(row.get("สินทรัพย์") or "").upper() != cfg.symbol.upper():
            blockers.append("row_symbol_mismatch")
        if str(row.get("semantics") or "") != CASHFLOW_SEMANTICS:
            blockers.append("row_cashflow_semantics_mismatch")
    else:
        blockers.append("source_row_missing")

    qty = evidence.get("filled_quantity")
    if qty is not None and qty > _QTY_TOLERANCE:
        blockers.extend(_positive_fill_blockers(
            cfg=cfg, chain_key=chain_key, run_id=run_id, intent=intent, state=state,
            row=row, realized=realized, evidence=evidence))
    elif qty is not None:
        blockers.extend(_zero_fill_blockers(
            cfg=cfg, run_id=run_id, intent=intent, state=state, row=row,
            realized=realized))

    if evidence.get("side") and normalize_status(intent.get("side")) != evidence["side"]:
        blockers.append("broker_side_mismatch")
    if evidence.get("symbol") and str(intent.get("symbol") or "") != evidence["symbol"]:
        blockers.append("broker_symbol_mismatch")

    event_payload = {
        "chain_key": chain_key,
        "dispatch_key": dispatch_key or chain_key,
        "run_id": run_id,
        "runtime_identity_fingerprint": runtime_identity,
        "broker_id": evidence.get("broker_id"),
        "broker_status": evidence.get("status"),
        "filled_quantity": qty,
        "filled_price": evidence.get("filled_price"),
        "filled_fee": evidence.get("filled_fee"),
        "broker_detail_hash": evidence.get("detail_hash"),
    }
    event_id = _canonical_hash(event_payload)
    replay = replay_candidate and hmac.compare_digest(reconciled_event, event_id)
    if replay_candidate and not replay:
        blockers.append("replay_evidence_mismatch")
    if replay and not _admin_clear_recorded(lock, event_id, run_id):
        blockers.append("replay_lock_marker_missing")

    quantity_text = _quantity_text(qty) if qty is not None else "?"
    confirmation = (
        f"ACK RECONCILED {chain_key} {run_id} {evidence.get('broker_id') or '?'} "
        f"{evidence.get('status') or 'UNKNOWN'} {quantity_text}")
    # Preserve order while eliminating duplicates; stable diagnostics matter in
    # an incident transcript.
    blockers = list(dict.fromkeys(blockers))
    return {
        "allowed": not blockers,
        "replay": replay and not blockers,
        "dry_run": True,
        "chain_key": chain_key,
        "run_id": run_id,
        "runtime_identity_fingerprint": runtime_identity,
        "lock_generation": int(lock.get("generation", 0) or 0),
        "intent_hash": _canonical_hash(intent),
        "event_id": event_id,
        "confirmation_phrase": confirmation,
        "broker_evidence": {k: v for k, v in evidence.items()
                            if k not in {"account_id_present", "blockers"}},
        "blockers": blockers,
    }


def inspect_reconciliation(chain_key: str, run_id: str, trade_client, *,
                           now_utc: datetime | None = None,
                           allowed_owner: str = "") -> dict:
    """Read RTDB plus one exact broker order and return a dry-run plan."""
    identity = runtime_identity_fingerprint()
    cfg = load_config()
    account_id = os.environ.get("WEBULL_ACCOUNT_ID", "").strip()
    detail = fetch_order_detail(trade_client, run_id)
    dispatch_key = account_symbol_fence_key(identity, cfg.symbol)
    lock = db.reference(f"{DISPATCH_LOCK_PATH}/{dispatch_key}").get()
    if not isinstance(lock, dict):
        # Rollout compatibility: an incident created by the pre-account-fence
        # worker remains administrable until that legacy lock is migrated.
        legacy_lock = db.reference(
            f"{DISPATCH_LOCK_PATH}/{chain_key}").get()
        if isinstance(legacy_lock, dict):
            dispatch_key, lock = chain_key, legacy_lock
    elif not lock.get("inflight_run_id"):
        legacy_lock = db.reference(
            f"{DISPATCH_LOCK_PATH}/{chain_key}").get()
        if isinstance(legacy_lock, dict) and legacy_lock.get("inflight_run_id"):
            dispatch_key, lock = chain_key, legacy_lock
    realized = db.reference(f"{REALIZED_PATH}/{chain_key}").get() or {}
    archived_fill = db.reference(
        f"{REALIZED_EVENT_ARCHIVE_PATH}/{chain_key}/{run_id}").get()
    if isinstance(archived_fill, dict):
        realized = dict(realized)
        applied = dict(realized.get("applied_fills") or {})
        applied.setdefault(run_id, archived_fill)
        realized["applied_fills"] = applied
    return evaluate_reconciliation(
        cfg=cfg, chain_key=chain_key, run_id=run_id, runtime_identity=identity,
        account_id=account_id,
        lock=lock,
        intent=db.reference(f"{OUTBOX_PATH}/{chain_key}/{run_id}").get(),
        state=db.reference(f"{STATE_PATH}/{chain_key}").get(),
        row=db.reference(f"{ROWS_PATH}/{run_id}").get(),
        realized=realized, broker_detail=detail, now_utc=now_utc,
        allowed_owner=allowed_owner, dispatch_key=dispatch_key)


def _lease_seconds() -> int:
    try:
        value = int(os.environ.get(
            "LEGO_ADMIN_RECONCILE_LEASE_SECONDS",
            str(DEFAULT_ADMIN_LEASE_SECONDS)))
    except (TypeError, ValueError) as exc:
        raise ReconcileRefusal(
            "LEGO_ADMIN_RECONCILE_LEASE_SECONDS must be an integer") from exc
    if not 30 <= value <= 1800:
        raise ReconcileRefusal(
            "LEGO_ADMIN_RECONCILE_LEASE_SECONDS must be between 30 and 1800")
    return value


def _reserve(plan: dict, *, now_utc: datetime) -> tuple[str, str, int]:
    token = uuid.uuid4().hex
    owner = f"admin-reconcile:{token}"
    expected_generation = int(plan["lock_generation"])
    next_generation = expected_generation + 1
    lease_until = _stamp(now_utc + timedelta(seconds=_lease_seconds()))
    ref = db.reference(
        f"{DISPATCH_LOCK_PATH}/{plan.get('dispatch_key') or plan['chain_key']}")

    def txn(current):
        if not isinstance(current, dict):
            return current
        doc = dict(current)
        if int(doc.get("generation", 0) or 0) != expected_generation:
            return doc
        if str(doc.get("inflight_run_id") or "") != plan["run_id"]:
            return doc
        active = _parse_utc(doc.get("lease_until"))
        if doc.get("owner") and (active is None or active > now_utc):
            return doc
        doc.update({
            "owner": owner,
            "claim_token": token,
            "generation": next_generation,
            "claimed_at": _stamp(now_utc),
            "lease_until": lease_until,
            "admin_reconciliation_id": plan["event_id"],
        })
        return doc

    result = ref.transaction(txn)
    if not isinstance(result, dict) or result.get("claim_token") != token:
        raise ReconcileRefusal(
            "dispatch fence changed or another owner/admin won; run dry-run again")
    return token, owner, next_generation


def _release_reservation(plan: dict, token: str) -> None:
    ref = db.reference(
        f"{DISPATCH_LOCK_PATH}/{plan.get('dispatch_key') or plan['chain_key']}")

    def txn(current):
        if not isinstance(current, dict) or current.get("claim_token") != token:
            return current
        doc = dict(current)
        doc.update({"owner": "", "claim_token": "", "lease_until": ""})
        if doc.get("admin_reconciliation_id") == plan["event_id"]:
            doc.pop("admin_reconciliation_id", None)
        return doc

    ref.transaction(txn)


def _audit_payload(plan: dict, operator: str, phase: str,
                   now_utc: datetime) -> dict:
    evidence = plan["broker_evidence"]
    return {
        "schema": "admin_reconcile_v1",
        "event_id": plan["event_id"],
        "phase": phase,
        "at": _stamp(now_utc),
        "operator": operator,
        "chain_key": plan["chain_key"],
        "run_id": plan["run_id"],
        "runtime_identity_fingerprint": plan["runtime_identity_fingerprint"],
        "broker_id": evidence["broker_id"],
        "broker_status": evidence["status"],
        "filled_quantity": evidence["filled_quantity"],
        "filled_price": evidence.get("filled_price"),
        "confirmation_sha256": hashlib.sha256(
            plan["confirmation_phrase"].encode("utf-8")).hexdigest(),
    }


def _write_audit_phase(plan: dict, operator: str, phase: str,
                       now_utc: datetime) -> None:
    """Append a phase once. Existing audit evidence can never be overwritten."""
    payload = _audit_payload(plan, operator, phase, now_utc)
    path = (f"{ADMIN_AUDIT_PATH}/{plan['chain_key']}/{plan['run_id']}/"
            f"{plan['event_id']}/{phase.lower()}")
    ref = db.reference(path)

    def txn(current):
        if current is None:
            return payload
        # ``at`` and ``operator`` identify the administrator who first wrote
        # this immutable phase. A crash replay by another administrator must
        # preserve (not replace) that evidence, while accepting it as the same
        # operation when every substantive field still agrees.
        immutable_keys = set(payload) - {"at", "operator"}
        if (not isinstance(current, dict)
                or set(current) != set(payload)
                or any(current.get(key) != payload.get(key)
                       for key in immutable_keys)):
            raise ReconcileRefusal("immutable admin audit collision")
        return current

    ref.transaction(txn)


def _ack_intent(plan: dict, token: str, operator: str,
                now_utc: datetime) -> bool:
    ref = db.reference(
        f"{OUTBOX_PATH}/{plan['chain_key']}/{plan['run_id']}")
    marker = {
        "schema": "admin_reconcile_v1",
        "event_id": plan["event_id"],
        "operator": operator,
        "at": _stamp(now_utc),
        "broker_id": plan["broker_evidence"]["broker_id"],
        "broker_status": plan["broker_evidence"]["status"],
        "filled_quantity": plan["broker_evidence"]["filled_quantity"],
        "runtime_identity_fingerprint": plan["runtime_identity_fingerprint"],
        # A durable transaction result, unlike a callback closure side effect,
        # proves whether this invocation won when Firebase retries callbacks.
        "apply_token": token,
    }

    def txn(current):
        if not isinstance(current, dict):
            return current
        doc = dict(current)
        existing = doc.get("admin_reconciliation")
        if isinstance(existing, dict):
            if existing.get("event_id") != plan["event_id"]:
                return doc
            return doc
        if _canonical_hash(doc) != plan["intent_hash"]:
            return doc
        if (str(doc.get("run_id") or "") != plan["run_id"]
                or str(doc.get("chain_key") or "") != plan["chain_key"]):
            return doc
        doc["admin_reconciliation"] = marker
        # Keep the manual guard until the chain fence itself is durably clear.
        # Otherwise a normal worker could clear it after this admin lease expires
        # and leave the immutable COMPLETED audit phase unrecoverably missing.
        doc["admin_reconciliation_pending"] = True
        # Defense in depth: even an intent that entered reconciliation directly
        # from SUBMITTED must remain ineligible for the normal worker's terminal
        # fence-clear path if this admin process crashes before COMPLETED audit.
        doc["needs_manual_check"] = True
        doc["status"] = plan["broker_evidence"]["status"]
        doc["broker_status"] = plan["broker_evidence"]["status"]
        doc["filled_quantity"] = plan["broker_evidence"]["filled_quantity"]
        if plan["broker_evidence"].get("filled_price") is not None:
            doc["filled_price"] = plan["broker_evidence"]["filled_price"]
        doc.pop("claim_owner", None)
        doc.pop("claim_until", None)
        doc.update({
            "audit_pending": True,
            "terminal_reason": "operator reconciled against terminal broker evidence",
            "updated_at": _stamp(now_utc),
        })
        return doc

    result = ref.transaction(txn)
    if not isinstance(result, dict):
        raise ReconcileIncomplete("outbox intent disappeared after reservation")
    existing = result.get("admin_reconciliation")
    if not isinstance(existing, dict) or existing.get("event_id") != plan["event_id"]:
        raise ReconcileIncomplete("outbox changed after dry-run; fence remains safe")
    return existing.get("apply_token") == token


def _finalize_intent_ack(plan: dict, operator: str,
                         now_utc: datetime) -> bool:
    """Remove the manual halt only after the lock records this exact clear.

    This is intentionally replayable. A crash after the cross-path fence clear
    leaves ``admin_reconciliation_pending`` on the intent; the next dry-run sees
    the lock's absorbing event marker and finishes this transaction without ever
    reopening the money boundary ambiguously.
    """
    lock = db.reference(
        f"{DISPATCH_LOCK_PATH}/{plan.get('dispatch_key') or plan['chain_key']}").get()
    if (not isinstance(lock, dict) or lock.get("inflight_run_id")
            or not _admin_clear_recorded(
                lock, plan["event_id"], plan["run_id"])):
        raise ReconcileIncomplete(
            "dispatch fence clear marker is missing; keep manual halt and replay")
    ref = db.reference(
        f"{OUTBOX_PATH}/{plan['chain_key']}/{plan['run_id']}")

    def txn(current):
        if not isinstance(current, dict):
            return current
        doc = dict(current)
        marker = doc.get("admin_reconciliation")
        if (not isinstance(marker, dict)
                or marker.get("event_id") != plan["event_id"]):
            return doc
        if (doc.get("admin_reconciled") is True
                and not doc.get("admin_reconciliation_pending")
                and not doc.get("needs_manual_check")
                and not doc.get("cashflow_abandoned")):
            return doc
        doc["admin_reconciled"] = True
        doc.pop("admin_reconciliation_pending", None)
        doc.pop("needs_manual_check", None)
        doc.pop("cashflow_abandoned", None)
        doc.update({
            "audit_pending": True,
            "admin_reconciled_by": operator,
            "admin_reconciled_at": _stamp(now_utc),
            "updated_at": _stamp(now_utc),
        })
        return doc

    result = ref.transaction(txn)
    if (not isinstance(result, dict) or result.get("admin_reconciled") is not True
            or (result.get("admin_reconciliation") or {}).get("event_id")
            != plan["event_id"]):
        raise ReconcileIncomplete(
            "fence cleared but intent acknowledgement did not finalize; replay")
    return True


def _clear_fence(plan: dict, token: str, now_utc: datetime) -> bool:
    ref = db.reference(
        f"{DISPATCH_LOCK_PATH}/{plan.get('dispatch_key') or plan['chain_key']}")
    clear_marker = uuid.uuid4().hex
    event_marker = {
        "schema": "admin_fence_clear_v1",
        "event_id": plan["event_id"],
        "run_id": plan["run_id"],
        "cleared_at": _stamp(now_utc),
    }

    def txn(current):
        if not isinstance(current, dict):
            return current
        doc = dict(current)
        if (doc.get("claim_token") != token
                or str(doc.get("inflight_run_id") or "") != plan["run_id"]
                or doc.get("admin_reconciliation_id") != plan["event_id"]):
            return doc
        raw_events = doc.get("admin_reconciliation_events")
        if raw_events is not None and not isinstance(raw_events, dict):
            return doc
        events = dict(raw_events or {})
        if raw_events is None:
            legacy_event = str(doc.get("last_admin_reconciliation_id") or "")
            if legacy_event:
                if re.fullmatch(r"[0-9a-f]{64}", legacy_event) is None:
                    return doc
                if legacy_event != plan["event_id"]:
                    events[legacy_event] = {
                        "schema": "admin_fence_clear_legacy_v1",
                        "event_id": legacy_event,
                        "migrated_at": _stamp(now_utc),
                    }
        existing = events.get(plan["event_id"])
        if existing is not None and existing != event_marker:
            return doc
        events[plan["event_id"]] = existing or event_marker
        for key in ("inflight_run_id", "place_fence", "fenced_run_id", "fenced_at",
                    "admin_reconciliation_id"):
            doc.pop(key, None)
        doc.update({
            "owner": "",
            "claim_token": "",
            "lease_until": "",
            "last_clear_token": clear_marker,
            "last_cleared_run_id": plan["run_id"],
            "last_admin_reconciliation_id": plan["event_id"],
            "admin_reconciliation_events": events,
            "cleared_at": _stamp(now_utc),
        })
        return doc

    result = ref.transaction(txn)
    return (isinstance(result, dict)
            and result.get("last_clear_token") == clear_marker
            and _admin_clear_recorded(
                result, plan["event_id"], plan["run_id"])
            and not result.get("inflight_run_id"))


def acknowledge_reconciliation(plan: dict, confirmation: str, trade_client, *,
                               operator: str,
                               now_utc: datetime | None = None) -> dict:
    """Apply one authorized acknowledgement; never calls a broker mutation."""
    now_utc = (now_utc or _now()).astimezone(UTC)
    operator = str(operator or "").strip()
    if not operator or len(operator) > 128:
        raise ReconcileRefusal("operator is required and must be <= 128 characters")
    if not plan.get("allowed"):
        raise ReconcileRefusal("dry-run plan is refused: " + ", ".join(plan["blockers"]))
    if not hmac.compare_digest(str(confirmation), plan["confirmation_phrase"]):
        raise ReconcileRefusal("confirmation phrase does not match exactly")
    if plan.get("replay"):
        _finalize_intent_ack(plan, operator, now_utc)
        _write_audit_phase(plan, operator, "COMPLETED", now_utc)
        return {"applied": False, "replay_noop": True,
                "fence_cleared": True, "event_id": plan["event_id"]}

    token, owner, _generation = _reserve(plan, now_utc=now_utc)
    try:
        # Re-read every witness after taking the administrative lease.  This is
        # the optimistic second check that closes the dry-run/apply TOCTOU gap.
        refreshed = inspect_reconciliation(
            plan["chain_key"], plan["run_id"], trade_client,
            now_utc=now_utc, allowed_owner=owner)
        if (not refreshed.get("allowed")
                or refreshed.get("event_id") != plan["event_id"]
                or refreshed.get("confirmation_phrase") != plan["confirmation_phrase"]):
            raise ReconcileRefusal(
                "evidence changed after reservation: "
                + ", ".join(refreshed.get("blockers") or ["event changed"]))
        _write_audit_phase(refreshed, operator, "AUTHORIZED", now_utc)
        applied_now = _ack_intent(refreshed, token, operator, now_utc)
        _write_audit_phase(refreshed, operator, "APPLIED", now_utc)
        if not _clear_fence(refreshed, token, now_utc):
            raise ReconcileIncomplete(
                "intent acknowledged but dispatch fence did not clear; replay after lease expiry")
        _finalize_intent_ack(refreshed, operator, now_utc)
        _write_audit_phase(refreshed, operator, "COMPLETED", now_utc)
        return {"applied": applied_now, "replay_noop": not applied_now,
                "fence_cleared": True, "event_id": refreshed["event_id"]}
    except ReconcileRefusal:
        _release_reservation(plan, token)
        raise
    # ReconcileIncomplete deliberately preserves the lease and inflight fence.
    # Its expiry permits a safe replay, but no normal worker can cross it now.


def _init_firebase() -> None:
    if not firebase_admin._apps:
        firebase_admin.initialize_app(
            credentials.ApplicationDefault(),
            {"databaseURL": os.environ["FIREBASE_DB_URL"]})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect and acknowledge one durable LEGO chain halt")
    parser.add_argument("--chain-key", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--apply", action="store_true",
                        help="write acknowledgement; default is dry-run")
    parser.add_argument("--confirmation", default="",
                        help="exact ACK RECONCILED ... phrase from dry-run")
    parser.add_argument("--operator", default=os.environ.get(
        "LEGO_ADMIN_OPERATOR", os.environ.get("USERNAME", "")))
    return parser


def cli(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _init_firebase()
        trade_client, _data_client = build_clients()
        plan = inspect_reconciliation(args.chain_key, args.run_id, trade_client)
        if not args.apply:
            print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if plan["allowed"] else 2
        result = acknowledge_reconciliation(
            plan, args.confirmation, trade_client, operator=args.operator)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:  # keep SDK/RTDB exception repr and secrets off stderr
        print(json.dumps({
            "ok": False,
            "error_type": type(exc).__name__,
            "error": redact_sensitive_text(exc),
        }, ensure_ascii=False),
              file=sys.stderr)
        return 3


if __name__ == "__main__":  # pragma: no cover - exercised by operator
    raise SystemExit(cli())

