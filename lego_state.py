"""RTDB persistence, durable pending-order outbox, and realized fill ledger."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import uuid
from datetime import datetime, timezone

from firebase_admin import db

from bounded_fifo import (MATCH_PAGES_PER_CALL_V3, MIGRATION_PAGES_PER_CALL_V3,
                          PROJECTION_LIMIT_V3,
                          SCHEMA_VERSION as FIFO_SCHEMA_VERSION, make_page,
                          page_key, validate_page)
from dna_engine import dna_fingerprint
from lego_one_row import (ACTUAL_COLUMN, DELTA_COLUMN, EXCESS_COLUMN,
                          REFERENCE_COLUMN, Anchor, Config, ExecutionFill,
                          finalize_recurrence, validate_row_columns)
from lego_orders import apply_fill, normalize_open_legs, normalize_status
from ledger_v2 import BrokerCashflow, decimal
from market_clock import calendar_fingerprint, market_ordinal_for_slot_id
from webull_io import redact_sensitive_text

ROWS_PATH = "webull_lego_rows"
STATE_PATH = "webull_lego_state"
AUDIT_PATH = "webull_lego_order_audit"
REALIZED_PATH = "webull_lego_realized"
REALIZED_EVENT_ARCHIVE_PATH = "webull_lego_realized_events"
REALIZED_HOT_EVENT_LIMIT = 64
REALIZED_LOT_PAGE_PATH = "webull_lego_realized_lot_pages"
REALIZED_FIFO_SCHEMA_VERSION = FIFO_SCHEMA_VERSION
BROKER_CASHFLOW_PATH = "webull_lego_broker_cashflow"
# Every cashflow accounting this chain family has ever written, oldest first.
# The order is the whole point: each name means a different thing by "Aₙ", so a
# chain may be carried *forward* through this list — read_anchor restarts its
# baseline at the boundary — but never backwards.
#
# Backwards is not hypothetical. A revision that predates the split books a
# decision (or, further back, every row) as an act, and Cloud Run keeps an old
# revision serving until something takes its traffic away. Pointed at a chain
# that has already moved to execution-confirmed accounting, it writes ΔAₙ on
# PASS rows and drags P_acted forward with them, and every guard in this module
# lets it: the version increments, the slot is fresh, the calendar and the DNA
# still match. Only the semantics name disagrees, so that name is the fence.
CASHFLOW_SEMANTICS_HISTORY = (
    "cycle_realized_v1",        # ΔAₙ from FIFO-matched broker cycles
    "gated_theoretical_v2",     # a READY_* decision advanced Aₙ by itself
    "execution_confirmed_v1",   # only a broker-confirmed fill advances Aₙ
)
CASHFLOW_SEMANTICS = "execution_confirmed_v1"
V2_CASHFLOW_SEMANTICS = "execution_terminal_frozen_v2"
ALL_CASHFLOW_SEMANTICS = CASHFLOW_SEMANTICS_HISTORY + (V2_CASHFLOW_SEMANTICS,)


def cashflow_semantics_for(cfg: Config) -> str:
    return V2_CASHFLOW_SEMANTICS if cfg.strategy_id.endswith("_v2") else CASHFLOW_SEMANTICS

# Execution cashflow state, owned by lego_order_worker and nested under its own
# key so the decision pointer (version, dna_step, p0, slot_id, market_ordinal)
# and the money that has actually moved are never written by the same author.
EXECUTION_STATE_KEY = "execution_cashflow"
# How many finalized run_ids to remember. Only re-finalization of the *same*
# run_id has to be refused, and an intent leaves the actionable queue the moment
# it finalizes, so the window only has to outlive one intent — this outlives
# weeks of them.
FINALIZED_RUN_HISTORY = 200

# Row-level provenance, stored beside run_id/version and never as an 18th column.
CASHFLOW_NO_ACTION = "NO_ACTION"           # PASS row: nothing to execute
CASHFLOW_PENDING = "PENDING_EXECUTION"     # READY_*: waiting for a broker fill
CASHFLOW_FINALIZED = "FINALIZED"           # fill confirmed, ΔAₙ/Aₙ/Eₙ written


def realized_open_legs_hash(open_legs) -> str:
    """Canonical witness for one valid FIFO ledger state after an applied fill."""
    if not isinstance(open_legs, dict) or any(
            key not in {"buys", "sells"} for key in open_legs):
        raise ValueError("realized open_legs ต้องเป็น buys/sells object")
    canonical = {"buys": [], "sells": []}
    for side in ("buys", "sells"):
        legs = open_legs.get(side, [])
        if not isinstance(legs, list):
            raise ValueError("realized open_legs แต่ละฝั่งต้องเป็น list")
        for leg in legs:
            if not isinstance(leg, (list, tuple)) or len(leg) != 3:
                raise ValueError("realized leg ต้องเป็น [quantity, price, fee_per_share]")
            if any(isinstance(value, bool) for value in leg):
                raise ValueError("realized leg ห้ามใช้ bool")
            try:
                quantity, price, fee_per_share = map(float, leg)
            except (TypeError, ValueError) as exc:
                raise ValueError("realized leg ต้องเป็นตัวเลข") from exc
            if (not all(math.isfinite(value) for value in
                        (quantity, price, fee_per_share))
                    or quantity <= 1e-9 or price <= 0 or fee_per_share < 0):
                raise ValueError("realized leg อยู่นอกช่วงที่อนุญาต")
            canonical[side].append([quantity, price, fee_per_share])
    if canonical["buys"] and canonical["sells"]:
        raise ValueError("FIFO ledger ห้ามมี buy/sell open legs พร้อมกัน")
    raw = json.dumps(canonical, sort_keys=True, ensure_ascii=False,
                     separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class StaleAnchorError(RuntimeError):
    pass


class SlotAlreadyConsumed(RuntimeError):
    pass


class CalendarDriftError(RuntimeError):
    """The market calendar no longer reproduces this chain's committed slots."""


class DNADriftError(RuntimeError):
    """The same dna_code no longer decodes to the gate array this chain traded.

    Same family as CalendarDriftError: an input the chain was built on changed
    underneath it, so continuing would trade a different strategy under the same
    name. Fail closed and let a human decide.
    """


class OrdinalRegression(RuntimeError):
    """A later commit resolved to an ordinal at or before the chain's last one.

    market_ordinal(t) must reproduce the bar index the DNA was trained on, so it
    may only move forward. Letting it move back would replay an older gate on a
    newer slot, which is a corrupt chain, not a retry.
    """


class RuntimeIdentityError(RuntimeError):
    """The persisted chain cannot safely be used by this account/environment."""


class RuntimeIdentityMismatch(RuntimeIdentityError):
    """The chain was created under another opaque runtime identity."""


class CashflowSemanticsDowngrade(RuntimeError):
    """An older cashflow accounting tried to write a chain that moved past it.

    Same family as CalendarDriftError and DNADriftError — an input the chain was
    built on changed underneath it — except the input here is the running code.
    """


class ExecutionFinalizeError(RuntimeError):
    """A confirmed fill could not be booked against this chain's cashflow."""


class _Idempotent(Exception):
    pass


def config_hash(cfg: Config) -> str:
    fields = {"s": cfg.strategy_id, "sym": cfg.symbol, "fix": cfg.fix_c,
              "diff": cfg.diff, "dna": cfg.dna_code}
    # Broker lot/precision are execution capabilities, not strategy identity.
    # They may be discovered after cold start and must never move decision,
    # recovery and dispatch onto different chains. Legacy identity is preserved.
    if not cfg.strategy_id.endswith("_v2"):
        fields["dp"] = cfg.decimal_precision
    payload = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def chain_key(cfg: Config) -> str:
    return f"{cfg.symbol}_{config_hash(cfg)}"


def verify_runtime_identity(state: dict | None,
                            runtime_identity: str | None) -> bool:
    """Guard a chain against another account/environment; adopt a legacy one.

    A chain written before this guard existed carries no fingerprint. Refusing it
    buys nothing — the account that wrote it is unknowable either way — while a
    hard failure would stop the DNA clock every slot until an operator noticed.
    So a missing fingerprint is adopted (the caller stamps it on the next commit)
    and reported; only a fingerprint that is present and different is a real
    cross-account collision, and that still fails closed.

    Returns True when this is the first adoption, so the caller can say it out
    loud once. Never logs or persists the raw broker account id.
    """
    if not state or runtime_identity is None:
        return False
    stored = state.get("runtime_identity_fingerprint")
    if not stored:
        return True
    if not hmac.compare_digest(str(stored), str(runtime_identity)):
        raise RuntimeIdentityMismatch(
            "runtime identity ไม่ตรงกับ chain ที่บันทึกไว้ "
            "(account/environment คนละชุด; account id ไม่ถูกเปิดเผย)")
    return False


def make_run_id(ck: str, anchor_version: int | None, snapshot: dict) -> str:
    raw = (f"{ck}|{anchor_version}|{snapshot['captured_at']}|"
           f"{snapshot['price']}|{snapshot.get('holdings', 0)}")
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def verify_calendar_continuity(state: dict | None) -> None:
    """Fail closed when the calendar would re-phase an existing chain.

    Two independent checks: the stored fingerprint (slot size, origin, declared
    holidays, rules version) and a recompute of the last committed slot id.
    """
    if not state:
        return
    stored = state.get("calendar_fingerprint")
    if stored and stored != calendar_fingerprint():
        raise CalendarDriftError(
            f"calendar/slot config เปลี่ยน: fingerprint {stored} -> {calendar_fingerprint()} "
            "(DNA จะเลื่อน phase ถาวร) ต้องเริ่ม chain ใหม่หรือคืนค่าเดิม")
    slot_id = state.get("slot_id")
    ordinal = state.get("market_ordinal")
    if not slot_id or ordinal is None or str(slot_id).startswith("epoch:"):
        return
    recomputed = market_ordinal_for_slot_id(str(slot_id))
    if recomputed != int(ordinal):
        raise CalendarDriftError(
            f"slot {slot_id} เคย commit เป็น ordinal {int(ordinal)} แต่คำนวณใหม่ได้ {recomputed}")


def verify_dna_continuity(cfg: Config, state: dict | None) -> None:
    """Fail closed when the same dna_code stops decoding to the traded array.

    Runs on every commit, not only clock-resolved ones: the gate array decides
    the row regardless of which slot it landed on. Chains written before the
    field simply have nothing to compare — the next commit records it.
    """
    if not state:
        return
    stored = state.get("dna_fingerprint")
    if stored and stored != dna_fingerprint(cfg.dna_code):
        raise DNADriftError(
            f"dna_code เดิมแต่ decode ได้ gate array คนละชุด: {stored} -> "
            f"{dna_fingerprint(cfg.dna_code)} (มักเกิดจาก numpy เปลี่ยนเวอร์ชัน) "
            "— chain นี้จะกลายเป็นคนละกลยุทธ์ ต้องคืน numpy เดิมหรือเริ่ม chain ใหม่")


def cashflow_semantics_rank(name: str | None) -> int | None:
    """Position of *name* in CASHFLOW_SEMANTICS_HISTORY, or None if unknown."""
    if not name:
        return None
    try:
        return ALL_CASHFLOW_SEMANTICS.index(str(name))
    except ValueError:
        return None


def verify_cashflow_semantics(
        state: dict | None, target_semantics: str | None = None) -> str | None:
    """Fail closed when older accounting tries to write a newer chain.

    Returns the name being migrated *from* when this runtime is legitimately
    ahead of the chain, so the caller can say the baseline reset out loud once —
    read_anchor drops Aₙ to zero at that boundary, and a silent reset looks
    exactly like the corruption this guard exists to stop. Returns None when
    there is nothing to report: an absent state, a chain written before the
    field existed, or a chain already on this runtime's semantics.

    An unrecognised name is refused rather than adopted. It can only come from
    code this deployment does not have, and guessing which side of the split it
    sits on is precisely the guess that produces an unexplainable ledger.
    """
    target_semantics = CASHFLOW_SEMANTICS if target_semantics is None else target_semantics
    if not state:
        return None
    stored = state.get("cashflow_semantics")
    if not stored or str(stored) == target_semantics:
        return None
    stored_rank = cashflow_semantics_rank(stored)
    if stored_rank is None:
        raise CashflowSemanticsDowngrade(
            f"chain ใช้ cashflow semantics '{stored}' ที่ deployment นี้ไม่รู้จัก "
            f"(รู้จัก: {', '.join(ALL_CASHFLOW_SEMANTICS)}) — "
            "อาจมี revision อื่นเขียน RTDB อยู่ ต้องตรวจ deployment ก่อน")
    if stored_rank > cashflow_semantics_rank(target_semantics):
        raise CashflowSemanticsDowngrade(
            f"chain เดินไปถึง '{stored}' แล้ว แต่ runtime นี้เป็น "
            f"'{target_semantics}' ซึ่งเก่ากว่า — revision เก่าห้ามเขียนทับ "
            "ledger ที่ใหม่กว่า (ตรวจว่า revision ไหนยังรับ traffic อยู่)")
    return str(stored)


class _Unread:
    """Distinguishes 'caller passed no state' from a real empty/absent state."""

    def __repr__(self) -> str:                # pragma: no cover - debug aid only
        return "UNREAD_STATE"


UNREAD_STATE = _Unread()


def read_chain_state(cfg: Config) -> dict | None:
    """The one place a caller fetches this chain's state document.

    Every guard on the read path needs the same document, so callers fetch it
    once and hand it down instead of paying a round trip per guard.
    """
    return db.reference(f"{STATE_PATH}/{chain_key(cfg)}").get()


def _resolve_state(cfg: Config, state) -> dict | None:
    return read_chain_state(cfg) if state is UNREAD_STATE else state


def execution_cashflow(state: dict, target_semantics: str = CASHFLOW_SEMANTICS) -> dict:
    """This chain's confirmed cashflow: the only source of P_acted and Aₙ.

    Both values move on a broker-confirmed fill and on nothing else, so they
    live under their own key with the worker as sole author. A chain written
    before the split still holds them in the flat prev_price/prev_actual
    fields; they are read once here as the seed and stored under the new key by
    the next commit. prev_actual only survives that migration when the chain
    already used this module's semantics — the previous name counted a decision
    as an act, so its Aₙ is a different quantity and must not be chained onto.
    """
    stored = (state or {}).get(EXECUTION_STATE_KEY)
    if isinstance(stored, dict) and stored.get("last_action_price") is not None:
        return {
            "last_action_price": float(stored["last_action_price"]),
            "actual_cumulative": float(stored.get("actual_cumulative", 0.0) or 0.0),
            "excess": float(stored.get("excess", 0.0) or 0.0),
            "r_basis": float(stored.get("r_basis", 0.0) or 0.0),
        }
    same_semantics = state.get("cashflow_semantics") == target_semantics
    return {
        "last_action_price": float(state["prev_price"]),
        "actual_cumulative": float(state["prev_actual"]) if same_semantics else 0.0,
        "excess": float(state.get("prev_excess", 0.0) or 0.0) if same_semantics else 0.0,
        "r_basis": float(state.get("r_basis", 0.0) or 0.0) if same_semantics else 0.0,
    }


def read_anchor(cfg: Config, *, runtime_identity: str | None = None,
                state=UNREAD_STATE) -> Anchor | None:
    state = _resolve_state(cfg, state)
    if not state:
        return None
    verify_runtime_identity(state, runtime_identity)
    ph = state.get("prev_holdings")
    target_semantics = cashflow_semantics_for(cfg)
    cashflow = execution_cashflow(state, target_semantics)
    return Anchor(
        version=int(state["version"]),
        dna_step=int(state["dna_step"]),
        p0=float(state["p0"]),
        prev_price=cashflow["last_action_price"],
        prev_actual=cashflow["actual_cumulative"],
        prev_holdings=None if ph is None else float(ph),
        prev_excess=(cashflow["excess"]
                     if target_semantics == V2_CASHFLOW_SEMANTICS else None),
    )


def _repair_pending_row(state: dict | None) -> None:
    if not state:
        return
    rid = state.get("last_run_id")
    if not rid:
        return
    ref = db.reference(f"{ROWS_PATH}/{rid}")
    doc = ref.get()
    if doc is not None and doc.get("committed") is False:
        ref.update({"committed": True})


def consumed_slot_state(cfg: Config, slot_id: str, *, runtime_identity: str,
                        state=UNREAD_STATE) -> dict | None:
    """Recognize a committed slot before broker I/O, retaining continuity guards.

    This is only a read/row-repair optimization. New slots still go through the
    authoritative commit transaction, and execution recovery runs independently.
    Repair the last row's commit flag after a crash just as commit_final_row does.
    """
    current = _resolve_state(cfg, state)
    verify_runtime_identity(current, runtime_identity)
    verify_dna_continuity(cfg, current)
    verify_cashflow_semantics(current, cashflow_semantics_for(cfg))
    verify_calendar_continuity(current)
    if current and current.get("slot_id") == slot_id:
        _repair_pending_row(current)
        return current
    return None


def commit_final_row(cfg: Config, snapshot: dict, anchor: Anchor | None, row: dict,
                     *, slot_id: str | None = None, market_ordinal: int | None = None,
                     clock_mode: str | None = None,
                     runtime_identity: str | None = None,
                     pending_intent: dict | None = None) -> dict:
    """Commit one row and advance the DNA pointer.

    Advances the *decision* pointer only. The execution cashflow (P_acted, Aₙ)
    is carried through untouched, read from `current` inside the transaction so
    a fill finalized by the worker between this caller's anchor read and its
    commit is preserved rather than overwritten with the pre-fill value.

    Order execution is not part of this transaction: intents live in the
    outbox, so a broker failure can never roll back a committed slot. The row
    keeps exactly the original 17 columns; slot provenance is stored alongside
    run_id/version as metadata, never as a new column.

    Guards, all fail closed: replayed run_id (idempotent no-op), stale anchor,
    already-consumed slot, calendar drift, and an ordinal that does not move
    forward. Degraded 'epoch:*' slots carry no ordinal, so they skip the last one.
    """
    validate_row_columns(row)
    ck = chain_key(cfg)
    anchor_version = None if anchor is None else anchor.version
    run_id = make_run_id(ck, anchor_version, snapshot)
    expected_version = 1 if anchor is None else anchor.version + 1
    row_ref = db.reference(f"{ROWS_PATH}/{run_id}")
    state_ref = db.reference(f"{STATE_PATH}/{ck}")
    meta = row["_meta"]
    state_before = state_ref.get()
    verify_runtime_identity(state_before, runtime_identity)
    verify_dna_continuity(cfg, state_before)
    target_semantics = cashflow_semantics_for(cfg)
    migrated_from = verify_cashflow_semantics(state_before, target_semantics)
    if slot_id is not None:
        verify_calendar_continuity(state_before)
    _repair_pending_row(state_before)

    existing = row_ref.get()
    if existing is not None and existing.get("committed"):
        return {"committed": False, "idempotent": True, "run_id": run_id,
                "version": existing.get("version")}

    doc = {k: v for k, v in row.items() if k != "_meta"}
    doc.update({
        "run_id": run_id,
        "chain_key": ck,
        "version": expected_version,
        "committed": False,
        "semantics": target_semantics,
        "instrument_capability": {
            "quantity_increment": format(
                float(cfg.quantity_increment), ".17g"),
            "decimal_precision": int(cfg.decimal_precision),
        },
        # Outside the 17 columns, like run_id and market_slot_id: says whether
        # the three cashflow columns are final or still waiting on a fill.
        "cashflow_status": CASHFLOW_PENDING
                           if meta.get("execution_pending", meta.get("acted"))
                           else CASHFLOW_NO_ACTION,
    })
    if target_semantics == V2_CASHFLOW_SEMANTICS:
        doc.update({
            "schema_version": 2,
            "ledger_version_at_observation": target_semantics,
            "E_mark_at_observation": float(meta["e_mark"]),
        })
    if slot_id is not None:
        doc["market_slot_id"] = slot_id
    if market_ordinal is not None:
        doc["market_ordinal"] = int(market_ordinal)
    if clock_mode is not None:
        doc["clock_mode"] = clock_mode
    row_ref.set(doc)

    def txn(current):
        current = current or None
        verify_runtime_identity(current, runtime_identity)
        capability_contract = {
            "quantity_increment": format(
                float(cfg.quantity_increment), ".17g"),
            "decimal_precision": int(cfg.decimal_precision),
        }
        prior_capability = ((current or {}).get("instrument_capability")
                            if current else None)
        if (cfg.strategy_id.endswith("_v2") and prior_capability is not None
                and prior_capability != capability_contract):
            raise RuntimeIdentityError(
                "instrument capability เปลี่ยนจาก state ที่ผูกไว้ — "
                "ต้องตรวจ lot-size migration ก่อน commit")
        # Re-checked inside the transaction, not only above: the write this
        # fences against is another revision's, and it can land between the read
        # at the top of this function and the moment this transaction runs.
        verify_cashflow_semantics(current, target_semantics)
        if current is None:
            if anchor_version is not None:
                raise StaleAnchorError("state ว่างแต่ anchor ไม่ใช่ genesis")
        else:
            if current.get("last_run_id") == run_id:
                raise _Idempotent()
            if anchor_version != current.get("version"):
                raise StaleAnchorError(
                    f"stale anchor: anchor.version={anchor_version} "
                    f"state.version={current.get('version')}")
            if slot_id is not None and current.get("slot_id") == slot_id:
                raise SlotAlreadyConsumed(f"slot {slot_id} commit ไปแล้ว")
            last_ordinal = current.get("market_ordinal")
            if market_ordinal is not None and last_ordinal is not None \
                    and int(market_ordinal) <= int(last_ordinal):
                raise OrdinalRegression(
                    f"market_ordinal ต้องเดินหน้า: chain อยู่ที่ {int(last_ordinal)} "
                    f"แต่ slot นี้ได้ {int(market_ordinal)} — DNA เดินถอยไม่ได้")

        # Read inside the transaction, so a fill the worker finalized after this
        # caller read its anchor is carried forward, not rolled back. Only an
        # absent cashflow is seeded — genesis, or the first commit of a chain
        # written before the split, where meta holds what read_anchor resolved.
        cashflow = dict((current or {}).get(EXECUTION_STATE_KEY) or {})
        if cashflow.get("last_action_price") is None:
            cashflow.update({
                "last_action_price": float(meta["acted_price_next"]),
                "actual_cumulative": float(meta["actual_next"]),
            })
            if target_semantics == V2_CASHFLOW_SEMANTICS:
                cashflow.update({
                    "excess": float(meta["excess_next"]),
                    "r_basis": 0.0,
                })
            cashflow.setdefault("finalized_seq", 0)

        next_state = {
            "version": expected_version,
            "dna_step": int(meta["step"]),
            "p0": float(meta["p0_next"]),
            EXECUTION_STATE_KEY: cashflow,
            # Mirrors of the cashflow above, kept for readers written before the
            # split. Sourced from the cashflow and never from this decision, so
            # a READY_* row cannot move them.
            "prev_price": float(cashflow["last_action_price"]),
            "prev_actual": float(cashflow["actual_cumulative"]),
            "prev_holdings": float(snapshot.get("holdings", 0.0) or 0.0),
            "last_run_id": run_id,
            "updated_at": snapshot["captured_at"],
            "config_hash": config_hash(cfg),
            "dna_fingerprint": dna_fingerprint(cfg.dna_code),
            "symbol": cfg.symbol,
            "cashflow_semantics": target_semantics,
            "instrument_capability": capability_contract,
        }
        if target_semantics == V2_CASHFLOW_SEMANTICS:
            next_state.update({
                "prev_excess": float(cashflow.get("excess", 0.0) or 0.0),
                "r_basis": float(cashflow.get("r_basis", 0.0) or 0.0),
                "schema_version": 2,
            })
        if runtime_identity is not None:
            next_state["runtime_identity_fingerprint"] = runtime_identity
        pending = dict((current or {}).get("pending_order_intents") or {})
        if pending_intent is not None:
            pending[run_id] = dict(pending_intent)
        if pending:
            next_state["pending_order_intents"] = pending
        if slot_id is not None:
            next_state["slot_id"] = slot_id
        if slot_id is not None and not slot_id.startswith("epoch:"):
            next_state["calendar_fingerprint"] = calendar_fingerprint()
        elif current and current.get("calendar_fingerprint"):
            # A degraded commit makes no claim about the calendar, so it must not
            # pin a new one — but dropping the chain's existing fingerprint would
            # disarm the drift guard for every commit after it, exactly when the
            # clock has just proven unreliable. Carry it forward, same reason as
            # market_ordinal below.
            next_state["calendar_fingerprint"] = current["calendar_fingerprint"]
        if market_ordinal is not None:
            next_state["market_ordinal"] = int(market_ordinal)
        elif current and current.get("market_ordinal") is not None:
            # A degraded commit resolves no ordinal, but dropping the chain's
            # last one would disarm the regression guard for every commit after
            # it — exactly when the clock has just proven unreliable. Carrying
            # the old value forward under-reports by the degraded slots, which
            # still catches a genuine walk backwards.
            next_state["market_ordinal"] = int(current["market_ordinal"])
        if clock_mode is not None:
            next_state["clock_mode"] = clock_mode
        return next_state

    try:
        state_ref.transaction(txn)
    except _Idempotent:
        row_ref.update({"committed": True})
        return {"committed": False, "idempotent": True,
                "run_id": run_id, "version": expected_version}
    except (StaleAnchorError, SlotAlreadyConsumed, OrdinalRegression,
            CashflowSemanticsDowngrade):
        row_ref.delete()
        raise

    row_ref.update({"committed": True})
    result = {"committed": True, "run_id": run_id, "version": expected_version,
              "market_slot_id": slot_id, "market_ordinal": market_ordinal}
    if migrated_from is not None:
        # read_anchor has already restarted Aₙ at zero for this commit. Saying so
        # is the point: a baseline reset and a corrupted ledger look identical in
        # the 17 columns, and only one of them is supposed to happen.
        result["cashflow_semantics_migrated_from"] = migrated_from
    return result


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_committed_row_for_chain(cfg: Config, run_id: str) -> dict:
    """The committed row a fill belongs to, refusing anything else."""
    doc = db.reference(f"{ROWS_PATH}/{run_id}").get()
    if not isinstance(doc, dict) or doc.get("committed") is not True:
        raise ExecutionFinalizeError(
            f"row {run_id} ยังไม่ committed — ห้าม finalize cashflow")
    ck = chain_key(cfg)
    if doc.get("chain_key") not in (None, ck):
        raise ExecutionFinalizeError(
            f"row {run_id} เป็นของ chain {doc.get('chain_key')} ไม่ใช่ {ck}")
    reference = doc.get(REFERENCE_COLUMN)
    if reference is None or not math.isfinite(float(reference)):
        raise ExecutionFinalizeError(f"row {run_id} ไม่มี Rₙ ที่ใช้ได้ — finalize ไม่ได้")
    return doc


def finalize_execution_fill(cfg: Config, run_id: str, fill: ExecutionFill, *,
                            runtime_identity: str | None = None) -> dict:
    """Book ΔAₙ/Aₙ/Eₙ for one broker-confirmed fill — once, atomically.

    This is the whole point of the split: the engine committed the row with the
    cashflow carried forward, and only here — with a filled price, a cumulative
    filled quantity and the holdings read back after execution — does the model
    ledger move.

    Idempotent on run_id, which is also the client_order_id, so a worker retry,
    a second poll of a partial fill, and a second worker instance racing the
    first all reduce to the first result. The state transaction is the fence:
    whoever loses sees the run_id already recorded and applies nothing. The row
    patch is repeated on that losing path too, because the failure it recovers
    from is a transaction that committed while the row write did not.

    Touches no field of the decision pointer: version, dna_step, p0, slot_id and
    market_ordinal all stay exactly where the engine left them, so finalizing
    can never present a stale anchor or consume a slot.
    """
    if not isinstance(fill, ExecutionFill):
        raise TypeError("fill ต้องเป็น ExecutionFill จาก broker จริง")
    run_id = str(run_id)
    ck = chain_key(cfg)
    row_doc = read_committed_row_for_chain(cfg, run_id)
    reference_R = float(row_doc[REFERENCE_COLUMN])
    # The row's own record of having been finalized. Second fence behind
    # finalized_runs, and the one that still holds after the run_id ages out of
    # that bounded history: a poll of a very old fill then reads as the no-op it
    # is, instead of booking the same ΔAₙ a second time.
    row_already_final = row_doc.get("cashflow_status") == CASHFLOW_FINALIZED
    state_ref = db.reference(f"{STATE_PATH}/{ck}")
    outcome: dict = {}

    def txn(current):
        if not isinstance(current, dict) or not current:
            raise ExecutionFinalizeError(
                "ไม่พบ chain state — finalize fill ไม่ได้")
        state = dict(current)
        verify_runtime_identity(state, runtime_identity)
        # A fill is the one write that is *supposed* to move Aₙ, which makes it
        # the worst one to let an out-of-date accounting perform: it would chain
        # a new ΔAₙ onto an Aₙ that means something else.
        target_semantics = cashflow_semantics_for(cfg)
        verify_cashflow_semantics(state, target_semantics)
        cashflow = dict(state.get(EXECUTION_STATE_KEY) or {})
        if cashflow.get("last_action_price") is None:
            cashflow.update(execution_cashflow(state, target_semantics))
        finalized = dict(cashflow.get("finalized_runs") or {})
        already = finalized.get(run_id)
        if isinstance(already, dict):
            outcome.update({"applied": False, **already})
            return state                       # absorbing: never counted twice
        if row_already_final:
            outcome.update({
                "applied": False,
                "delta_actual": float(row_doc[DELTA_COLUMN]),
                "actual_cumulative": float(row_doc[ACTUAL_COLUMN]),
                "excess": float(row_doc[EXCESS_COLUMN]),
                "reference": reference_R,
                "filled_price": float(row_doc.get("execution_price")
                                      or fill.filled_price),
                "filled_quantity": float(row_doc.get("execution_quantity")
                                         or fill.filled_quantity),
                "holdings_after": float(row_doc.get("post_execution_holdings")
                                        if row_doc.get("post_execution_holdings")
                                        is not None else fill.holdings_after),
                "at": str(row_doc.get("cashflow_finalized_at") or ""),
            })
            return state

        result = finalize_recurrence(
            cfg, fill,
            last_action_price=float(cashflow["last_action_price"]),
            actual_cumulative=float(cashflow.get("actual_cumulative", 0.0) or 0.0),
            reference_R=reference_R)
        previous_actual_cumulative = float(
            cashflow.get("actual_cumulative", 0.0) or 0.0)
        seq = int(cashflow.get("finalized_seq", 0) or 0) + 1
        record = {
            "delta_actual": result.dA,
            "actual_cumulative": result.A,
            "excess": result.E,
            "reference": reference_R,
            "previous_action_price": float(cashflow["last_action_price"]),
            "previous_actual_cumulative": previous_actual_cumulative,
            "filled_price": float(fill.filled_price),
            "filled_quantity": float(fill.filled_quantity),
            "holdings_after": float(fill.holdings_after),
            "seq": seq,
            "at": _utc_stamp(),
        }
        finalized[run_id] = record
        if len(finalized) > FINALIZED_RUN_HISTORY:
            ordered = sorted(finalized.items(),
                             key=lambda kv: int((kv[1] or {}).get("seq", 0) or 0))
            finalized = dict(ordered[-FINALIZED_RUN_HISTORY:])
        cashflow.update({
            "last_action_price": result.acted_price_next,
            "actual_cumulative": result.A,
            "finalized_seq": seq,
            "last_finalized_run_id": run_id,
            "finalized_runs": finalized,
            "updated_at": record["at"],
        })
        if target_semantics == V2_CASHFLOW_SEMANTICS:
            cashflow.update({"excess": result.E, "r_basis": reference_R})
        state[EXECUTION_STATE_KEY] = cashflow
        state["prev_price"] = result.acted_price_next
        state["prev_actual"] = result.A
        if target_semantics == V2_CASHFLOW_SEMANTICS:
            state["prev_excess"] = result.E
            state["r_basis"] = reference_R
        # The post-execution reading, from the broker, replacing the decision's.
        state["prev_holdings"] = float(fill.holdings_after)
        outcome.update({"applied": True, **record})
        return state

    state_ref.transaction(txn)
    if not outcome:                            # pragma: no cover - defensive
        raise ExecutionFinalizeError("finalize transaction ไม่ได้คืนผลลัพธ์")

    # Always written, including on the idempotent path: the one crash this
    # repeats through is a committed transaction whose row patch never landed,
    # and rewriting the recorded numbers is exactly the repair.
    row_patch = {
        DELTA_COLUMN: outcome["delta_actual"],
        ACTUAL_COLUMN: outcome["actual_cumulative"],
        EXCESS_COLUMN: outcome["excess"],
        "cashflow_status": CASHFLOW_FINALIZED,
        "execution_price": outcome["filled_price"],
        "execution_quantity": outcome["filled_quantity"],
        "post_execution_holdings": outcome["holdings_after"],
        "cashflow_finalized_at": outcome["at"],
    }
    if cashflow_semantics_for(cfg) == V2_CASHFLOW_SEMANTICS:
        row_patch.update({
            "R_basis": outcome["reference"],
            "finalized_seq": outcome.get("seq"),
        })
    db.reference(f"{ROWS_PATH}/{run_id}").update(row_patch)
    return {"run_id": run_id, "chain_key": ck, **outcome}


def execution_finalization(cfg: Config, run_id: str, *,
                           state=UNREAD_STATE) -> dict | None:
    """What was booked for *run_id*, or None if this chain never finalized it."""
    state = _resolve_state(cfg, state) or {}
    cashflow = state.get(EXECUTION_STATE_KEY) or {}
    record = (cashflow.get("finalized_runs") or {}).get(str(run_id))
    return dict(record) if isinstance(record, dict) else None


def pending_order_intents(cfg: Config, *,
                          runtime_identity: str | None = None,
                          state=UNREAD_STATE) -> dict[str, dict]:
    """Durable intent payloads committed atomically with the state pointer."""
    state = _resolve_state(cfg, state) or {}
    verify_runtime_identity(state, runtime_identity)
    raw = state.get("pending_order_intents") or {}
    return {
        str(run_id): dict(payload)
        for run_id, payload in raw.items()
        if isinstance(payload, dict)
    }


def repair_pending_intent_row(cfg: Config, run_id: str, *,
                              runtime_identity: str | None = None,
                              state=UNREAD_STATE) -> dict:
    """Finish the row patch proven committed by a transaction intent marker.

    ``commit_final_row`` writes the row as ``committed=False``, atomically moves
    the state pointer together with ``pending_order_intents[run_id]``, then
    patches the row true.  A process can die between the last two operations.
    Recovery must repair that row *before* materializing its outbox entry;
    otherwise the dispatcher sees an uncommitted source and absorbs the real
    intent as ``NOT_PLACED``.
    """
    resolved = _resolve_state(cfg, state) or {}
    verify_runtime_identity(resolved, runtime_identity)
    run_id = str(run_id)
    pending = resolved.get("pending_order_intents") or {}
    if run_id not in pending or not isinstance(pending.get(run_id), dict):
        raise ExecutionFinalizeError(
            f"state has no committed pending-intent marker for row {run_id}")

    ck = chain_key(cfg)
    ref = db.reference(f"{ROWS_PATH}/{run_id}")
    doc = ref.get()
    if not isinstance(doc, dict):
        raise ExecutionFinalizeError(
            f"pending-intent marker exists but row {run_id} is missing")
    if doc.get("chain_key") != ck:
        raise ExecutionFinalizeError(
            f"pending-intent row {run_id} belongs to another chain")
    if doc.get("run_id") != run_id:
        raise ExecutionFinalizeError(
            f"pending-intent row id mismatch for {run_id}")
    if doc.get("committed") is not True:
        ref.update({"committed": True})
        doc = {**doc, "committed": True}
    return doc


def chain_runtime_identity_is_verified(
        cfg: Config, runtime_identity: str | None, *, state=UNREAD_STATE) -> bool:
    """Return whether a state exists after applying the identity guard."""
    state = _resolve_state(cfg, state)
    if not state:
        return False
    verify_runtime_identity(state, runtime_identity)
    return True


def mark_order_intent_materialized(cfg: Config, run_id: str, *,
                                   runtime_identity: str | None = None) -> None:
    """Clear a recovery marker only after idempotent outbox creation succeeds."""
    ref = db.reference(f"{STATE_PATH}/{chain_key(cfg)}")

    def txn(current):
        if not isinstance(current, dict) or not current:
            raise RuntimeIdentityError(
                "state หายระหว่าง materialize outbox — หยุดเพื่อไม่สร้าง state ไม่ครบ")
        state = dict(current or {})
        verify_runtime_identity(state, runtime_identity)
        pending = dict(state.get("pending_order_intents") or {})
        pending.pop(run_id, None)
        if pending:
            state["pending_order_intents"] = pending
        else:
            state.pop("pending_order_intents", None)
        if runtime_identity is not None and not state.get("runtime_identity_fingerprint"):
            state["runtime_identity_fingerprint"] = runtime_identity
        return state

    ref.transaction(txn)


_AUDIT_SECRET_FIELDS = {
    "app_key", "app_secret", "access_token", "x-signature",
    "x-access-token", "x-app-key", "account_id", "webull_account_id",
    "authorization",
}


def _redact_audit_payload(payload: dict) -> dict:
    def clean(value):
        if isinstance(value, str):
            return redact_sensitive_text(value)
        if isinstance(value, dict):
            return {
                key: clean(item)
                for key, item in value.items()
                if str(key).lower() not in _AUDIT_SECRET_FIELDS
            }
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    return {
        key: clean(value)
        for key, value in payload.items()
        if str(key).lower() not in _AUDIT_SECRET_FIELDS
    }


def write_order_audit(event_id: str, payload: dict) -> None:
    update_order_audit(event_id, payload)


def update_order_audit(event_id: str, fields: dict) -> None:
    safe = _redact_audit_payload(fields)
    def txn(current):
        current = dict(current or {})
        if ("audit_revision" in safe and int(safe["audit_revision"])
                < int(current.get("audit_revision", 0))):
            return current
        current.update(safe)
        return current
    db.reference(f"{AUDIT_PATH}/{event_id}").transaction(txn)


def pending_audits(terminal_statuses: set[str], limit: int = 20) -> dict:
    all_audits = db.reference(AUDIT_PATH).get() or {}
    rows: list[tuple[str, dict]] = []
    for event_id, payload in all_audits.items():
        if not isinstance(payload, dict):
            continue
        if normalize_status(payload.get("status")) in terminal_statuses:
            continue
        rows.append((event_id, payload))
    rows.sort(key=lambda item: str(item[1].get("placed_at") or item[1].get("created_at") or ""))
    return dict(rows[:limit])


def apply_broker_cashflow(ck: str, event_id: str, side: str,
                          cumulative_qty: object, average_price: object,
                          actual_fee: object | None) -> dict:
    """Persist exact broker cash movement independently of strategy P&L.

    Webull values are cumulative per order. Decimal strings preserve the
    broker's values without a float round trip, and ``actual_fees=None`` stays
    explicitly pending until the broker publishes the fee. A late fee update
    therefore applies only the fee delta and never repeats the filled notional.
    """
    ref = db.reference(
        f"{BROKER_CASHFLOW_PATH}/{ck}/events/{event_id}")
    apply_token = uuid.uuid4().hex

    def txn(current):
        doc = dict(current or {})
        prior_fee = doc.get("actual_fees")
        prior = BrokerCashflow(
            cumulative_quantity=decimal(
                doc.get("cumulative_quantity", "0"),
                name="stored cumulative quantity", positive=False),
            cumulative_notional=decimal(
                doc.get("cumulative_notional", "0"),
                name="stored cumulative notional", positive=False),
            actual_fees=(
                None if prior_fee is None
                else decimal(prior_fee, name="stored actual fees", positive=False)),
            cash_delta=decimal(
                doc.get("cash_cumulative", "0"),
                name="stored cash cumulative"),
        )
        updated, delta = prior.apply_cumulative(
            side=side, quantity=cumulative_qty,
            average_price=average_price, actual_fees=actual_fee)
        fee_became_known = (
            prior.actual_fees is None and updated.actual_fees is not None)
        changed = fee_became_known or any(delta[name] != 0 for name in (
            "delta_quantity", "delta_notional", "delta_fee"))
        if not changed and doc:
            return doc
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {
            "event_id": str(event_id),
            "chain_key": str(ck),
            "side": str(side).upper(),
            "cumulative_quantity": str(updated.cumulative_quantity),
            "cumulative_notional": str(updated.cumulative_notional),
            "actual_fees": (
                None if updated.actual_fees is None
                else str(updated.actual_fees)),
            "fee_status": (
                "PENDING" if updated.actual_fees is None else "KNOWN"),
            "cash_cumulative": str(updated.cash_delta),
            "last_delta_quantity": str(delta["delta_quantity"]),
            "last_delta_notional": str(delta["delta_notional"]),
            "last_delta_fee": str(delta["delta_fee"]),
            "last_broker_cash_delta": str(delta["broker_cash_delta"]),
            "last_apply_token": apply_token,
            "updated_at": now,
        }

    result = ref.transaction(txn) or {}
    return {
        "broker_cashflow_recorded": bool(result),
        "broker_fee_status": str(result.get("fee_status") or "PENDING"),
        "broker_cash_cumulative": result.get("cash_cumulative"),
        "broker_cashflow_applied_now": (
            result.get("last_apply_token") == apply_token),
    }




def _fifo_page_ref(ck: str, side_key: str, sequence: int, generation=None):
    prefix = f"generations/{generation}/" if generation else ""
    return db.reference(
        f"{REALIZED_LOT_PAGE_PATH}/{ck}/{prefix}{side_key}/{page_key(sequence)}")


def _write_immutable_fifo_page(ck: str, page: dict, generation=None) -> dict:
    ref = _fifo_page_ref(ck, str(page["side"]), int(page["sequence"]), generation)

    def txn(current):
        if current is None:
            return page
        if current != page:
            raise ValueError("FIFO lot page immutable collision")
        return current

    stored = ref.transaction(txn)
    return validate_page(
        stored, sequence=int(page["sequence"]), side_key=str(page["side"]))


def migrate_realized_open_legs(
        ck: str, *, dry_run: bool = False,
        max_pages: int = MIGRATION_PAGES_PER_CALL_V3) -> dict:
    """Resume conversion of one legacy open-leg array into immutable pages.

    The legacy array remains authoritative until every page has been written
    and verified. The final head transaction checks its content hash before
    switching cursors, so interruption can leave only harmless duplicate pages.
    """
    if max_pages < 1:
        raise ValueError("max_pages must be >= 1")
    ref = db.reference(f"{REALIZED_PATH}/{ck}")
    state = ref.get() or {}
    if not isinstance(state, dict):
        raise ValueError("realized ledger head invalid")
    if int(state.get("schema_version", 0) or 0) == FIFO_SCHEMA_VERSION:
        return {"complete": True, "migrated": False, "remaining": 0}

    legacy = normalize_open_legs(state.get("open_legs"))
    if legacy["buys"] and legacy["sells"]:
        raise ValueError("legacy FIFO ledger has both buy and sell open legs")
    side_key = "buys" if legacy["buys"] else "sells"
    lots = legacy[side_key]
    source_hash = realized_open_legs_hash(legacy)
    checkpoint = state.get("fifo_migration") or {}
    if checkpoint and checkpoint.get("source_hash") != source_hash:
        raise ValueError("legacy FIFO migration source changed")
    next_index = int(checkpoint.get("next_index", 0) or 0)
    plan = {
        "schema_from": int(state.get("schema_version", 2) or 2),
        "schema_to": FIFO_SCHEMA_VERSION,
        "side": side_key,
        "source_hash": source_hash,
        "total_lots": len(lots),
        "next_index": next_index,
        "remaining": max(0, len(lots) - next_index),
        "total_quantity": sum(float(leg[0]) for leg in lots),
        "total_allocated_fee": sum(
            float(leg[0]) * float(leg[2]) for leg in lots),
        "cumulative_realized": float(
            state.get("cumulative_realized", 0.0) or 0.0),
    }
    if dry_run:
        return {**plan, "complete": len(lots) == next_index, "dry_run": True}

    # Claim a migration epoch before writing pages. Rollback cancels this epoch
    # atomically; stale writers can leave only unlinked immutable pages.
    expected_epoch = int(state.get("fifo_migration_epoch", 0) or 0)
    proposed_generation = uuid.uuid4().hex if lots else None

    def begin_txn(current):
        current = dict(current or {})
        if int(current.get("schema_version", 0) or 0) == FIFO_SCHEMA_VERSION:
            return current
        if int(current.get("fifo_migration_epoch", 0) or 0) != expected_epoch:
            raise ValueError("FIFO migration cancelled; retry from current head")
        if realized_open_legs_hash(normalize_open_legs(current.get("open_legs"))) != source_hash:
            raise ValueError("legacy FIFO migration source changed before claim")
        active = current.get("fifo_migration") or {}
        if active and active.get("source_hash") != source_hash:
            raise ValueError("FIFO migration source identity mismatch")
        if not active:
            current["fifo_migration"] = {
                "source_hash": source_hash, "side": side_key,
                "total_lots": len(lots), "next_index": 0,
                "generation": proposed_generation, "epoch": expected_epoch,
            }
        return current

    started = ref.transaction(begin_txn) or {}
    if int(started.get("schema_version", 0) or 0) == FIFO_SCHEMA_VERSION:
        return {"complete": True, "migrated": False, "remaining": 0}
    checkpoint = started["fifo_migration"]
    generation = checkpoint.get("generation")
    next_index = int(checkpoint.get("next_index", 0) or 0)
    stop = min(len(lots), next_index + int(max_pages))
    for index in range(next_index, stop):
        quantity, price, fee_per_share = map(float, lots[index])
        page = make_page(
            sequence=index, side_key=side_key, quantity=quantity, price=price,
            fee_per_share=fee_per_share, event_id=f"legacy:{index}")
        _write_immutable_fifo_page(ck, page, generation)

    def checkpoint_txn(current):
        current = dict(current or {})
        if int(current.get("schema_version", 0) or 0) == FIFO_SCHEMA_VERSION:
            return current
        active = current.get("fifo_migration") or {}
        if (int(current.get("fifo_migration_epoch", 0) or 0) != expected_epoch
                or active.get("source_hash") != source_hash
                or active.get("generation") != generation):
            raise ValueError("FIFO migration cancelled before checkpoint")
        current_legacy = normalize_open_legs(current.get("open_legs"))
        if realized_open_legs_hash(current_legacy) != source_hash:
            raise ValueError("legacy FIFO migration source changed during commit")
        committed_stop = max(stop, int(active.get("next_index", 0) or 0))
        current["fifo_migration"] = {
            "generation": generation, "epoch": expected_epoch,
            "source_hash": source_hash,
            "side": side_key,
            "total_lots": len(lots),
            "next_index": committed_stop,
            "updated_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
        }
        if committed_stop == len(lots):
            current.update({
                "schema_version": FIFO_SCHEMA_VERSION,
                "fifo_page_generation": generation,
                "ledger_sequence": int(current.get("ledger_sequence", 0) or 0),
                "fifo_read_cursor": {"buys": 0, "sells": 0},
                "fifo_write_cursor": {
                    "buys": len(lots) if side_key == "buys" else 0,
                    "sells": len(lots) if side_key == "sells" else 0,
                },
                "fifo_head_remaining": None,
                "active_matching_event_id": None,
                "matching_progress": None,
                "projection_repair_cursor": 0,
                "open_legs": {
                    "buys": legacy["buys"][:PROJECTION_LIMIT_V3],
                    "sells": legacy["sells"][:PROJECTION_LIMIT_V3],
                },
            })
            current["fifo_migration"]["complete"] = True
            current["fifo_migration"]["completed_at"] = datetime.now(
                timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return current

    result = ref.transaction(checkpoint_txn) or {}
    complete = int(result.get("schema_version", 0) or 0) == FIFO_SCHEMA_VERSION
    committed_stop = int((result.get("fifo_migration") or {}).get("next_index", stop))
    return {
        **plan,
        "complete": complete,
        "migrated": complete,
        "next_index": committed_stop,
        "remaining": max(0, len(lots) - committed_stop),
    }


def rollback_realized_open_legs_migration(ck: str) -> dict:
    """Cancel a migration atomically; retain unlinked pages for safe recovery.

    Never delete pages from a snapshot read: a concurrent finalization may
    already have linked them. A later migration uses a separate generation.
    """
    ref = db.reference(f"{REALIZED_PATH}/{ck}")
    rollback_token = uuid.uuid4().hex

    def txn(current):
        current = dict(current or {})
        if int(current.get("schema_version", 0) or 0) == FIFO_SCHEMA_VERSION:
            raise ValueError("FIFO v3 migration already finalized; schema rollback is not safe")
        migration = dict(current.get("fifo_migration") or {})
        if not migration:
            return current
        current_legacy = normalize_open_legs(current.get("open_legs"))
        if realized_open_legs_hash(current_legacy) != migration.get("source_hash"):
            raise ValueError("legacy FIFO source changed during rollback")
        current.pop("fifo_migration", None)
        current["fifo_migration_epoch"] = int(current.get("fifo_migration_epoch", 0) or 0) + 1
        current["fifo_rollback_token"] = rollback_token
        return current

    result = ref.transaction(txn) or {}
    return {"rolled_back": result.get("fifo_rollback_token") == rollback_token,
            "pages_deleted": 0}


def _archive_fifo_witnesses_before_claim(
        ck: str, event_id: str, hot_applied: dict) -> set[str]:
    archived: set[str] = set()
    if event_id in hot_applied or len(hot_applied) < REALIZED_HOT_EVENT_LIMIT:
        return archived
    overflow = len(hot_applied) - REALIZED_HOT_EVENT_LIMIT + 1
    victims = sorted(
        hot_applied.items(),
        key=lambda item: int((item[1] or {}).get("seq", 0) or 0),
    )[:overflow]
    for victim_id, record in victims:
        victim_ref = db.reference(
            f"{REALIZED_EVENT_ARCHIVE_PATH}/{ck}/{victim_id}")

        def txn(current, record=dict(record)):
            current = dict(current or {})
            if int(current.get("seq", 0) or 0) > int(record.get("seq", 0) or 0):
                return current
            return record

        stored = victim_ref.transaction(txn) or {}
        if stored != record:
            raise ValueError("realized witness archive content/version mismatch")
        archived.add(str(victim_id))
    return archived


def apply_realized_fill(ck: str, event_id: str, side: str,
                        cumulative_qty: float, price: float,
                        cumulative_fee: float = 0.0, *,
                        _fault_hook=None) -> dict:
    """Apply cumulative fill through a bounded, resumable immutable FIFO.

    At most ``MATCH_PAGES_PER_CALL_V3`` matching steps run per call, each reading
    one lot and at most one projection-refill page (16 direct reads total). If a single
    fill spans more pages, ``matching_pending`` remains true and the caller must
    keep the account-symbol fence while a later worker invocation resumes it.
    """
    cumulative_qty = float(cumulative_qty)
    price = float(price)
    cumulative_fee = float(cumulative_fee or 0.0)
    side = str(side).upper()
    requested_cumulative = (cumulative_qty, price, cumulative_fee, side)
    if side not in {"BUY", "SELL"}:
        raise ValueError("side ต้อง BUY หรือ SELL")
    if not all(math.isfinite(value) for value in
               (cumulative_qty, price, cumulative_fee)):
        raise ValueError("cumulative fill/price/fee ต้องเป็น finite")
    if cumulative_qty < 0 or cumulative_fee < 0:
        raise ValueError("cumulative fill/fee ติดลบไม่ได้")
    if cumulative_qty > 1e-9 and price <= 0:
        raise ValueError("fill price ต้อง > 0 เมื่อ quantity เป็นบวก")

    def fault(point: str) -> None:
        if _fault_hook is not None:
            _fault_hook(point)

    migration = migrate_realized_open_legs(
        ck, max_pages=REALIZED_HOT_EVENT_LIMIT)
    if not migration["complete"]:
        return {
            "realized_delta": 0.0,
            "realized_cumulative": 0.0,
            "open_legs": {"buys": [], "sells": []},
            "matching_pending": True,
            "matching_reason": "FIFO_MIGRATION_PENDING",
            "migration_remaining": migration["remaining"],
        }

    ref = db.reference(f"{REALIZED_PATH}/{ck}")
    before = ref.get() or {}
    hot_applied = dict(before.get("applied_fills") or {})
    archived_prev = db.reference(
        f"{REALIZED_EVENT_ARCHIVE_PATH}/{ck}/{event_id}").get()
    if not isinstance(archived_prev, dict):
        archived_prev = None
    archived_victims = _archive_fifo_witnesses_before_claim(
        ck, event_id, hot_applied)
    if archived_victims:
        fault("after_archive_before_eviction")
    apply_token = uuid.uuid4().hex
    busy = {"value": False}

    def claim_txn(current):
        state = dict(current or {})
        active = state.get("active_matching_event_id")
        if active and active != event_id:
            busy["value"] = True
            return state
        applied = dict(state.get("applied_fills") or {})
        prev = dict(applied.get(event_id) or archived_prev or {})
        prev_qty = float(prev.get("quantity", 0.0) or 0.0)
        prev_fee = float(prev.get("fee", 0.0) or 0.0)
        prev_avg = float(prev.get("average_price", prev.get("price", 0.0)) or 0.0)
        delta_qty = cumulative_qty - prev_qty
        delta_fee = cumulative_fee - prev_fee
        if delta_qty < -1e-9:
            raise ValueError("cumulative filled quantity ถอยหลังไม่ได้")
        if delta_fee < -1e-9:
            raise ValueError("cumulative filled fee ถอยหลังไม่ได้")
        delta_qty = max(0.0, delta_qty)
        delta_fee = max(0.0, delta_fee)
        if (delta_qty <= 1e-9 and prev_qty > 1e-9
                and abs(price - prev_avg) > 1e-9):
            raise ValueError(
                "average fill price เปลี่ยนโดย quantity ไม่เพิ่ม — ต้องตรวจด้วยมือ")
        if active == event_id:
            progress = dict(state.get("matching_progress") or {})
            actual = (
                float(progress.get("cumulative_qty", -1)),
                float(progress.get("average_price", -1)),
                float(progress.get("cumulative_fee", -1)),
                str(progress.get("side", "")),
            )
            # Broker cumulative facts may advance while a bounded checkpoint
            # still has work. Finish that immutable checkpoint first, then let
            # the next invocation apply only the newly observed increment.
            if (side != actual[3] or cumulative_qty < actual[0] - 1e-9
                    or cumulative_fee < actual[2] - 1e-9
                    or cumulative_qty * price < actual[0] * actual[1] - 1e-9
                    or (abs(cumulative_qty - actual[0]) <= 1e-9
                        and abs(price - actual[1]) > 1e-9)):
                raise ValueError("broker cumulative facts regress against active FIFO checkpoint")
            return state
        if delta_qty <= 1e-9 and delta_fee <= 1e-9:
            return state
        previous_notional = prev_qty * prev_avg
        cumulative_notional = cumulative_qty * price
        delta_price = (
            (cumulative_notional - previous_notional) / delta_qty
            if delta_qty > 1e-9 else price)
        if delta_qty > 1e-9 and delta_price <= 0:
            raise ValueError("incremental fill price ต้อง > 0")
        revision = int(state.get("ledger_sequence", 0) or 0) + 1
        accumulated = -delta_fee if delta_qty <= 1e-9 else 0.0
        if delta_qty <= 1e-9:
            cumulative = float(state.get("cumulative_realized", 0.0) or 0.0)
            state["cumulative_realized"] = cumulative + accumulated
        state.update({
            "active_matching_event_id": event_id,
            "matching_progress": {
                "event_id": event_id,
                "side": side,
                "cumulative_qty": cumulative_qty,
                "average_price": price,
                "cumulative_fee": cumulative_fee,
                "remaining_quantity": delta_qty,
                "incremental_price": delta_price,
                "fee_per_share": delta_fee / delta_qty if delta_qty > 1e-9 else 0.0,
                "accumulated_realized_delta": accumulated,
                "previous_event_realized": float(
                    prev.get("realized_delta", 0.0) or 0.0),
                "revision": revision,
            },
            "ledger_sequence": revision,
            "updated_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
        })
        return state

    claimed = ref.transaction(claim_txn) or {}
    fault("after_event_checkpoint")
    if busy["value"]:
        return {
            "realized_delta": 0.0,
            "realized_cumulative": float(
                claimed.get("cumulative_realized", 0.0) or 0.0),
            "open_legs": claimed.get("open_legs") or {"buys": [], "sells": []},
            "matching_pending": True,
            "matching_reason": "FIFO_OTHER_EVENT_ACTIVE",
        }
    if claimed.get("active_matching_event_id") != event_id:
        return {
            "realized_delta": 0.0,
            "realized_cumulative": float(
                claimed.get("cumulative_realized", 0.0) or 0.0),
            "open_legs": claimed.get("open_legs") or {"buys": [], "sells": []},
            "matching_pending": False,
        }

    checkpoint = claimed["matching_progress"]
    cumulative_qty = float(checkpoint["cumulative_qty"])
    price = float(checkpoint["average_price"])
    cumulative_fee = float(checkpoint["cumulative_fee"])
    side = str(checkpoint["side"])

    own_key = "buys" if side == "BUY" else "sells"
    opposite_key = "sells" if side == "BUY" else "buys"

    for _step in range(MATCH_PAGES_PER_CALL_V3):
        snapshot = ref.get() or {}
        if snapshot.get("active_matching_event_id") != event_id:
            break
        progress = dict(snapshot.get("matching_progress") or {})
        remaining = float(progress.get("remaining_quantity", 0.0) or 0.0)
        revision = int(progress.get("revision", 0) or 0)
        if remaining <= 1e-9:
            break
        read_cursors = dict(snapshot.get("fifo_read_cursor") or {})
        write_cursors = dict(snapshot.get("fifo_write_cursor") or {})
        read_index = int(read_cursors.get(opposite_key, 0) or 0)
        write_index = int(write_cursors.get(opposite_key, 0) or 0)

        if read_index >= write_index:
            own_index = int(write_cursors.get(own_key, 0) or 0)
            page = _write_immutable_fifo_page(ck, make_page(
                sequence=own_index, side_key=own_key, quantity=remaining,
                price=float(progress["incremental_price"]),
                fee_per_share=float(progress["fee_per_share"]),
                event_id=event_id), snapshot.get("fifo_page_generation"))
            fault("after_page_write_before_head")
            step_token = uuid.uuid4().hex

            def append_txn(current):
                state = dict(current or {})
                op = dict(state.get("matching_progress") or {})
                if (state.get("active_matching_event_id") != event_id
                        or int(op.get("revision", -1)) != revision):
                    return state
                cursors = dict(state.get("fifo_write_cursor") or {})
                if int(cursors.get(own_key, 0) or 0) != own_index:
                    return state
                validate_page(page, sequence=own_index, side_key=own_key)
                cursors[own_key] = own_index + 1
                projection = normalize_open_legs(state.get("open_legs"))
                if len(projection[own_key]) < PROJECTION_LIMIT_V3:
                    projection[own_key].append([
                        page["quantity"], page["price"],
                        page["fee_per_share"]])
                op["remaining_quantity"] = 0.0
                op["revision"] = revision + 1
                op["last_page_sequence"] = own_index
                op["last_page_hash"] = page["content_hash"]
                state.update({
                    "fifo_write_cursor": cursors,
                    "open_legs": projection,
                    "matching_progress": op,
                    "ledger_sequence": int(
                        state.get("ledger_sequence", 0) or 0) + 1,
                    "last_matching_step_token": step_token,
                    "updated_at": datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"),
                })
                return state

            ref.transaction(append_txn)
            fault("after_page_head_commit")
            continue

        page = validate_page(
            _fifo_page_ref(ck, opposite_key, read_index,
                           snapshot.get("fifo_page_generation")).get(),
            sequence=read_index, side_key=opposite_key)
        head_remaining = snapshot.get("fifo_head_remaining")
        if (isinstance(head_remaining, dict)
                and head_remaining.get("side") == opposite_key
                and int(head_remaining.get("sequence", -1)) == read_index):
            lot_quantity = float(head_remaining.get("quantity", 0.0) or 0.0)
        else:
            lot_quantity = float(page["quantity"])
        matched = min(remaining, lot_quantity)
        incoming_price = float(progress["incremental_price"])
        incoming_fee_ps = float(progress["fee_per_share"])
        if side == "BUY":
            realized_delta = (float(page["price"]) - incoming_price) * matched
        else:
            realized_delta = (incoming_price - float(page["price"])) * matched
        realized_delta -= (
            float(page["fee_per_share"]) + incoming_fee_ps) * matched
        if not math.isfinite(realized_delta):
            raise ValueError("realized FIFO delta must be finite")
        snapshot_projection = normalize_open_legs(snapshot.get("open_legs"))
        refill_page = None
        if lot_quantity - matched <= 1e-9:
            refill_index = read_index + max(
                1, len(snapshot_projection[opposite_key]))
            if refill_index < write_index:
                refill_page = validate_page(
                    _fifo_page_ref(ck, opposite_key, refill_index,
                                   snapshot.get("fifo_page_generation")).get(),
                    sequence=refill_index, side_key=opposite_key)
        step_token = uuid.uuid4().hex

        def match_txn(current):
            state = dict(current or {})
            op = dict(state.get("matching_progress") or {})
            if (state.get("active_matching_event_id") != event_id
                    or int(op.get("revision", -1)) != revision):
                return state
            cursors = dict(state.get("fifo_read_cursor") or {})
            if int(cursors.get(opposite_key, 0) or 0) != read_index:
                return state
            current_head = state.get("fifo_head_remaining")
            current_quantity = (
                float(current_head.get("quantity", 0.0) or 0.0)
                if isinstance(current_head, dict)
                and current_head.get("side") == opposite_key
                and int(current_head.get("sequence", -1)) == read_index
                else float(page["quantity"]))
            if abs(current_quantity - lot_quantity) > 1e-9:
                return state
            leftover = lot_quantity - matched
            projection = normalize_open_legs(state.get("open_legs"))
            if projection[opposite_key]:
                if leftover <= 1e-9:
                    projection[opposite_key].pop(0)
                else:
                    projection[opposite_key][0][0] = leftover
            elif leftover > 1e-9:
                projection[opposite_key].append([
                    leftover, page["price"], page["fee_per_share"]])
            if (leftover <= 1e-9 and refill_page is not None
                    and len(projection[opposite_key]) < PROJECTION_LIMIT_V3):
                projection[opposite_key].append([
                    refill_page["quantity"], refill_page["price"],
                    refill_page["fee_per_share"]])
            if leftover <= 1e-9:
                cursors[opposite_key] = read_index + 1
                next_head = None
            else:
                next_head = {
                    "side": opposite_key,
                    "sequence": read_index,
                    "quantity": leftover,
                    "page_hash": page["content_hash"],
                }
            op["remaining_quantity"] = max(0.0, remaining - matched)
            op["accumulated_realized_delta"] = float(
                op.get("accumulated_realized_delta", 0.0) or 0.0
            ) + realized_delta
            op["revision"] = revision + 1
            op["last_page_sequence"] = read_index
            op["last_page_hash"] = page["content_hash"]
            cumulative = float(state.get("cumulative_realized", 0.0) or 0.0)
            state.update({
                "fifo_read_cursor": cursors,
                "fifo_head_remaining": next_head,
                "open_legs": projection,
                "matching_progress": op,
                "cumulative_realized": cumulative + realized_delta,
                "ledger_sequence": int(
                    state.get("ledger_sequence", 0) or 0) + 1,
                "last_matching_step_token": step_token,
                "updated_at": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"),
            })
            return state

        ref.transaction(match_txn)
        fault("after_match_head_commit")

    finalized_token = uuid.uuid4().hex
    fault("before_event_finalize")

    def finalize_txn(current):
        state = dict(current or {})
        if state.get("active_matching_event_id") != event_id:
            return state
        op = dict(state.get("matching_progress") or {})
        if float(op.get("remaining_quantity", 0.0) or 0.0) > 1e-9:
            return state
        applied = dict(state.get("applied_fills") or {})
        applied_seq = int(state.get("applied_seq", 0) or 0) + 1
        event_delta = float(op.get("accumulated_realized_delta", 0.0) or 0.0)
        event_realized = float(op.get("previous_event_realized", 0.0) or 0.0) \
            + event_delta
        projection = normalize_open_legs(state.get("open_legs"))
        applied[event_id] = {
            "quantity": float(op["cumulative_qty"]),
            "fee": float(op["cumulative_fee"]),
            "average_price": float(op["average_price"]),
            "side": str(op["side"]),
            "realized_delta": event_realized,
            "cumulative_realized_after": float(
                state.get("cumulative_realized", 0.0) or 0.0),
            "open_legs_after_hash": realized_open_legs_hash(projection),
            "seq": applied_seq,
        }
        if len(applied) > REALIZED_HOT_EVENT_LIMIT:
            victims = sorted(
                applied.items(),
                key=lambda item: int((item[1] or {}).get("seq", 0) or 0),
            )[:len(applied) - REALIZED_HOT_EVENT_LIMIT]
            if any(str(victim_id) not in archived_victims
                   for victim_id, _record in victims):
                raise ValueError(
                    "realized hot witness changed during archive rotation")
            for victim_id, _record in victims:
                applied.pop(victim_id, None)
        state.update({
            "applied_fills": applied,
            "applied_seq": applied_seq,
            "last_realized_delta": event_delta,
            "last_event_id": event_id,
            "last_apply_token": apply_token,
            "last_finalize_token": finalized_token,
            "active_matching_event_id": None,
            "matching_progress": None,
            "projection_repair_cursor": min(
                int((state.get("fifo_read_cursor") or {}).get(own_key, 0) or 0),
                int((state.get("fifo_write_cursor") or {}).get(own_key, 0) or 0)),
            "ledger_sequence": int(state.get("ledger_sequence", 0) or 0) + 1,
            "updated_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
        })
        return state

    result = ref.transaction(finalize_txn) or {}
    fault("after_event_finalize")
    witness = (result.get("applied_fills") or {}).get(event_id) or {}
    newer_facts_waiting = (
        float(witness.get("quantity", 0) or 0) < requested_cumulative[0] - 1e-9
        or float(witness.get("fee", 0) or 0) < requested_cumulative[2] - 1e-9)
    pending = result.get("active_matching_event_id") == event_id or newer_facts_waiting
    applied_this_call = result.get("last_apply_token") == apply_token
    progress = dict(result.get("matching_progress") or {})
    return {
        "realized_delta": (
            float(result.get("last_realized_delta", 0.0) or 0.0)
            if applied_this_call else 0.0),
        "realized_cumulative": float(
            result.get("cumulative_realized", 0.0) or 0.0),
        "open_legs": result.get("open_legs") or {"buys": [], "sells": []},
        "matching_pending": pending,
        "matching_reason": ("FIFO_NEWER_FACTS_PENDING" if newer_facts_waiting
                            and not result.get("active_matching_event_id")
                            else "FIFO_MATCHING_PENDING" if pending else None),
        "matching_remaining_quantity": float(
            progress.get("remaining_quantity", 0.0) or 0.0) if pending else 0.0,
        "matching_work_limit": MATCH_PAGES_PER_CALL_V3,
    }
