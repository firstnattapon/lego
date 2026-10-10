"""Flight recorder: a per-tick forensic trace of LEGO actions, equations and Webull exchanges.

Why this exists. On 2026-10-08 a UAT order sat PENDING, its cancel was refused, and the
system halted itself eight hours later. Nobody could say why the DAY-expiry proof never
released the order: the reason was a ``logger.info`` that Cloud Logging never received
(0 of 1,860 ticks carried ``lego_order_worker actionable=``), ``lego_operation`` events
carry no request id, and the database held none of the broker's answers.

What it records, per tick, in ONE private RTDB document:
  * the LEGO flow ("ผัง"): every event carries a node id from :data:`NODES` and the
    envelope carries the path the tick took, e.g. ``D00>D05>D08[READY_BUY]>D10|W10>W11[…]``;
  * the equations ("สมการ"): inputs and outputs of the decision and of the fill-time
    recurrence, so an auditor can recompute them;
  * every Webull exchange: route, latency, HTTP status, ``X-Request-Id``, sanitized
    request, sanitized and projected response, error code and message;
  * every action around it: phases, warnings, outbox transitions, halts, errors.

Hard rules (each one is pinned by a test):
  * never raises, never prints, never mutates what it is given, never changes what the
    pipeline does. Cloud Logging stays payload-free (``tick_runtime`` / ``observability``
    promise that); payloads go only to ``webull_lego_trace`` which is not readable by
    clients (``database.rules.json``);
  * everything is sanitized with ``security_text`` (credentials, account identity) and
    bounded (per event, per tick, per day);
  * the final write happens after the tick's own log line and response are ready, is
    time-boxed, and is skipped when the tick has little budget left (a tick that sent or
    cancelled an order keeps its tail down to one second);
  * ``LEGO_TRACE_LEVEL=off`` is a full kill switch.

Environment (read at call time):
  LEGO_TRACE_LEVEL           notable (default) | all | off
  LEGO_TRACE_BODIES          full (default) | min   (min keeps only status, ids, hashes)
  LEGO_TRACE_RETENTION_DAYS  14
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

try:
    from firebase_admin import db
except ImportError:  # the NODES catalog stays importable where firebase_admin is not installed
    db = None

import security_text

logger = logging.getLogger(__name__)

UTC = timezone.utc
SCHEMA = 1
TRACE_PATH = "webull_lego_trace"
HEARTBEAT_PATH = "webull_lego_heartbeat"
DAYS_PATH = "webull_lego_trace_days"

MAX_EVENT_BYTES = 6144
MAX_TICK_BYTES = 98304
MAX_EVENTS = 200
FLUSH_MIN_BUDGET_SECONDS = 12.0
# A tick that sent or cancelled an order keeps its tail even when it ran long. The write is
# time-boxed to FLUSH_TIMEBOX_SECONDS and the tick deadline (35 s) sits 10 s inside Cloud
# Run's 45 s limit, so one second left is enough (worst case ~38 s). 2026-10-09 15:24 the one
# tick that overran lost exactly the part after Place: the order-detail error and the
# deferral that explained the order which then FAILED.
FLUSH_MIN_BUDGET_MUTATION_SECONDS = 1.0
FLUSH_TIMEBOX_SECONDS = 3.0
PRUNE_MIN_BUDGET_SECONDS = 10.0
PRUNE_EVERY_SECONDS = 3600.0
DEDUP_SECONDS = 900.0
HEARTBEAT_SECONDS = 300.0
WARN_EVERY_SECONDS = 600.0

# The flow chart. One catalog, one vocabulary: events, the tick ``path`` string,
# docs/FLIGHT_RECORDER_TH.md and the drift test all use these ids.
NODES: dict[str, dict] = {
    # --- decision path (main._run_tick -> decision_service.run_decision), LEGO Step 0-18
    "D00": {"group": "decision", "title": "config / clock / identity",
            "what": "load config, slot_seconds, clock mode, environment, runtime identity (Step 0)"},
    "D01": {"group": "decision", "title": "recover",
            "what": "read chain state, verify identity, materialize pending order intents"},
    "D02": {"group": "decision", "title": "session",
            "what": "regular session? closed -> PASS_MARKET_CLOSED"},
    "D03": {"group": "decision", "title": "slot consumed",
            "what": "slot already committed -> PASS_SLOT_CONSUMED"},
    "D04": {"group": "decision", "title": "capability",
            "what": "instrument capability: quantity precision/increment, fractionable"},
    "D05": {"group": "decision", "title": "clock",
            "what": "slot id, MarketOrdinal, legacy step, alignment error, effective DNA step (Step 4)"},
    "D06": {"group": "decision", "title": "snapshot",
            "what": "price Pn, holdings, quote time (Step 6-7)"},
    "D07": {"group": "decision", "title": "continuity",
            "what": "holdings continuity against the anchor"},
    "D08": {"group": "decision", "title": "equation",
            "what": "signal, Vn, gap, decision band, quantity, Rn, dAn, An, En (Step 5-17)"},
    "D09": {"group": "decision", "title": "gates",
            "what": "auto flag, broker-reject circuit, operator halt, money fence, preflight"},
    "D10": {"group": "decision", "title": "commit",
            "what": "commit_final_row: idempotency, stale anchor, slot guard, calendar guard (Step 18)"},
    "D11": {"group": "decision", "title": "outbox",
            "what": "put_intent / materialize the order intent"},
    # --- order worker (execution_service._run_order_worker)
    "W00": {"group": "worker", "title": "claim",
            "what": "dispatch lease, money fence, candidate intents"},
    "W10": {"group": "worker", "title": "reconcile",
            "what": "read order detail, build summary, validate broker evidence"},
    "W11": {"group": "worker", "title": "expiry proof",
            "what": "DAY-expiry proof: open orders + holdings -> blockers"},
    "W12": {"group": "worker", "title": "recovery",
            "what": "cancel / refused hold / manual (order_recovery.handle)"},
    "W13": {"group": "worker", "title": "settle",
            "what": "fill: realized ledger, broker cashflow, model ledger (dAn, An, En)"},
    "W20": {"group": "worker", "title": "dispatch guards",
            "what": "row committed, circuit, token, mutation permission, expiry, open orders"},
    "W21": {"group": "worker", "title": "quote safety",
            "what": "price drift, quote age, decision age, overshoot"},
    "W22": {"group": "worker", "title": "funding",
            "what": "preview, buying power, holdings"},
    "W23": {"group": "worker", "title": "submit gate",
            "what": "evaluate_submit_gate: payload, preview, confirmation phrase"},
    "W24": {"group": "worker", "title": "limits",
            "what": "execution limits reservation"},
    "W25": {"group": "worker", "title": "place",
            "what": "place witness, Place, first poll"},
    # --- cross-cutting
    "T00": {"group": "ops", "title": "tick", "what": "tick envelope"},
    "X00": {"group": "ops", "title": "error", "what": "caught exception"},
    "H00": {"group": "ops", "title": "halt", "what": "operator halt set / cleared"},
    "S01": {"group": "ops", "title": "transition", "what": "outbox intent status transition"},
}

_IDLE_BIZ = frozenset({"MARKET_CLOSED", "SLOT_CONSUMED"})
_STEADY = frozenset({"unresolved_intent", "halt", "biz_other", "warning", "webull_error"})
_REASON_BY_BIZ = {
    "ROW_COMMITTED": "row_committed",
    "WAITING_RECONCILIATION": "unresolved_intent",
    "RECONCILIATION_OVERDUE": "unresolved_intent",
    "WAITING_BROKER_FEE": "unresolved_intent",
    "FEE_OVERDUE": "unresolved_intent",
    "MANUAL_RECONCILIATION_REQUIRED": "halt",
    "OPERATOR_HALT": "halt",
    "BROKER_REJECT_HALT": "halt",
    "TICK_DEFERRED": "deferred",
    "ERROR": "error",
    "CONFIG_ERROR": "error",
}
_ENV_ALLOW = re.compile(r"^(LEGO_|WEBULL_ENV$|K_SERVICE$|K_REVISION$|LOG_EXECUTION_ID$)")
_ENV_DENY = re.compile(r"(?i)(secret|token|password|credential|account|app_?key|authoriz|binding)")
_SAFE_CHAIN = re.compile(r"[^A-Za-z0-9_\-]")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NO_BODY_ROUTES = ("/auth/", "/openapi/config")
_MUTATIONS = frozenset({"place", "cancel"})
_HIGH_PRIORITY = frozenset({"n", "eq", "er", "tr", "wn"})


class _Trace:
    """In-memory trace of the tick that is running right now."""

    def __init__(self, tick_id: str):
        self.tick = str(tick_id)
        self.t0 = time.monotonic()
        self.started = datetime.now(UTC)
        self.meta: dict = {}
        self.events: list[dict] = []
        self.bytes = 0
        self.seq = 0
        self.dropped = 0
        self.errors = 0
        self.flags: set[str] = set()
        self.warning_kinds: set[str] = set()
        self.runs: list[str] = []
        self.checkpointed = False
        self.budget = None          # callable -> seconds left in the tick (set by tick_scope)


_CURRENT: ContextVar[_Trace | None] = ContextVar("lego_flight_trace", default=None)

_BOOT_ID = uuid.uuid4().hex[:8]
_BOOT_AT = datetime.now(UTC)
_STATE_LOCK = threading.Lock()


def _fresh_stats() -> dict:
    return {"ticks": 0, "notable": 0, "written": 0, "skipped_dup": 0, "skipped_budget": 0,
            "write_errors": 0, "flush_timeouts": 0, "internal_errors": 0, "dropped_events": 0,
            "last_trace": None, "hb_mono": None, "warn_mono": None, "prune_mono": None,
            "booted": False}


_STATS: dict = _fresh_stats()
_LAST: dict[str, dict] = {}
_DAYS_SEEN: set[tuple[str, str]] = set()


def reset_state() -> None:
    """Forget module-level counters and dedup memory (tests; never needed in production)."""
    global _STATS
    with _STATE_LOCK:
        _STATS = _fresh_stats()
        _LAST.clear()
        _DAYS_SEEN.clear()


# --------------------------------------------------------------------------- config

def level() -> str:
    value = os.environ.get("LEGO_TRACE_LEVEL", "notable").strip().lower()
    return value if value in {"notable", "all", "off"} else "notable"


def bodies() -> str:
    value = os.environ.get("LEGO_TRACE_BODIES", "full").strip().lower()
    return value if value in {"full", "min"} else "full"


def retention_days() -> int:
    try:
        return max(1, int(os.environ.get("LEGO_TRACE_RETENTION_DAYS", "14")))
    except (TypeError, ValueError):
        return 14


# --------------------------------------------------------------------------- safety

def _count_error() -> None:
    try:
        trace = _CURRENT.get()
        if trace is not None:
            trace.errors += 1
        _STATS["internal_errors"] += 1
    except Exception:
        pass


def _safe(fn):
    """A recorder call must never be able to hurt the tick it is describing."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            _count_error()
            return None
    return wrapper


def _remaining() -> float | None:
    """Seconds left in the running tick (None = unknown). tick_scope hands the clock in, so
    this module needs no import of tick_runtime and the import graph stays acyclic."""
    try:
        trace = _CURRENT.get()
        budget = trace.budget if trace is not None else None
        return budget() if budget is not None else None
    except Exception:
        return None


def _clean(value, **limits):
    return security_text.trace_clean(value, **limits)


def _size(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str))


def _sha(obj) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _chain_label(value) -> str:
    text = _SAFE_CHAIN.sub("_", str(value or "")).strip("_")
    return text[:64] or "_unbound"


# --------------------------------------------------------------------------- lifecycle

@_safe
def begin(tick_id: str, budget=None):
    """Start the trace of this tick; returns the token ``reset`` needs (None when off).

    ``budget`` is a zero-argument callable returning the seconds left in the tick.
    """
    if level() == "off":
        return None
    trace = _Trace(tick_id)
    trace.budget = budget
    return _CURRENT.set(trace)


def reset(token) -> None:
    try:
        if token is not None:
            _CURRENT.reset(token)
    except Exception:
        pass


def active() -> bool:
    return _CURRENT.get() is not None


@_safe
def bind(**meta) -> None:
    """Attach identity to the running trace (chain, symbol, environment, mode, slot...)."""
    trace = _CURRENT.get()
    if trace is None:
        return
    for key, value in meta.items():
        if value is not None:
            trace.meta[key] = _clean(value, max_nodes=30, max_str=120)


@_safe
def flag(name: str) -> None:
    trace = _CURRENT.get()
    if trace is not None:
        trace.flags.add(str(name))


@_safe
def snapshot() -> dict | None:
    """Copy of the running trace (tests and debugging)."""
    trace = _CURRENT.get()
    if trace is None:
        return None
    return {"tick": trace.tick, "meta": dict(trace.meta), "events": list(trace.events),
            "flags": sorted(trace.flags), "dropped": trace.dropped, "bytes": trace.bytes}


# --------------------------------------------------------------------------- events

def _fit(event: dict) -> dict:
    """Shrink an oversized event instead of losing it: bodies first, then everything."""
    if _size(event) <= MAX_EVENT_BYTES:
        return event
    for key in ("res", "req", "in", "out", "x"):
        if key in event and _size(event) > MAX_EVENT_BYTES:
            stub = {"truncated": True, "sha256": _sha(event[key]), "len": _size(event[key])}
            event = {**event, key: stub}
    if _size(event) > MAX_EVENT_BYTES:
        event = {k: event[k] for k in ("i", "t", "k", "n", "op", "rt", "st", "rid") if k in event}
        event["truncated"] = True
    return event


def _add(trace: _Trace, event: dict) -> None:
    event["i"] = trace.seq
    trace.seq += 1
    event["t"] = int((time.monotonic() - trace.t0) * 1000)
    event = _fit(event)
    size = _size(event)
    important = event.get("k") in _HIGH_PRIORITY or bool(event.get("err")) or bool(event.get("mut"))
    over = len(trace.events) >= MAX_EVENTS or trace.bytes + size > MAX_TICK_BYTES
    # Low-priority events stop at the soft cap; important ones may use the 25% headroom.
    if over and (not important or len(trace.events) >= int(MAX_EVENTS * 1.25)
                 or trace.bytes + size > MAX_TICK_BYTES * 1.25):
        trace.dropped += 1
        return
    trace.events.append(event)
    trace.bytes += size


@_safe
def node(node_id: str, tag: str | None = None, *, ok: bool = True, **facts) -> None:
    """One step of the LEGO flow chart was reached (``tag`` is the short verdict)."""
    trace = _CURRENT.get()
    if trace is None:
        return
    event = {"k": "n", "n": node_id, "ok": bool(ok)}
    if tag is not None:
        event["tag"] = str(tag)[:60]
    if facts:
        event["x"] = _clean(facts, max_nodes=120, max_str=200)
    _add(trace, event)
    run = facts.get("run_id")
    if isinstance(run, str) and run and run not in trace.runs and len(trace.runs) < 8:
        trace.runs.append(run)


@_safe
def equation(node_id: str, inputs: dict, outputs: dict, formula: str | None = None) -> None:
    """Equation inputs and outputs, so an auditor can recompute them."""
    trace = _CURRENT.get()
    if trace is None:
        return
    event = {"k": "eq", "n": node_id, "in": _clean(inputs, max_nodes=80, max_str=120),
             "out": _clean(outputs, max_nodes=120, max_str=160)}
    verdict = outputs.get("status") if isinstance(outputs, dict) else None
    if verdict:
        event["tag"] = str(verdict)[:60]
    if formula:
        event["f"] = str(formula)[:640]
    _add(trace, event)


def pick(obj, *keys) -> dict:
    """Selected keys of a mapping, ``{}`` for anything else. Safe to call from the money path."""
    try:
        return {key: obj[key] for key in keys if isinstance(obj, dict) and key in obj}
    except Exception:
        return {}


DECISION_FORMULA = (
    "V=holdings*P; gap=V-FIX_C; signal=0 -> PASS_DNA_ZERO; |gap|<=DIFF -> PASS_THRESHOLD; "
    "qty=floor(|gap|/P to the increment) when the strategy ends _v2, else round(|gap|/P, dp); "
    "gap>DIFF is a SELL capped at holdings rounded down to dp, otherwise a BUY; _v2 and "
    "qty*P<1 -> PASS_MIN_ORDER; qty<=0 -> PASS; R=FIX_C*ln(P/P0). The decision row books no dA "
    "(acted=False): dA=0, A=A_prev, E=frozen. At fill: dA=FIX_C*(P_fill/P_acted-1), "
    "A=A_prev+dA, E=A-R.")


@_safe
def decision_equation(cfg, snapshot, anchor, row, step) -> None:
    """D08: inputs and outputs of the decision equations (LEGO Step 5-17), for recomputation."""
    meta = row.get("_meta") or {}
    inputs = {
        "P_n": snapshot.get("price"), "holdings": snapshot.get("holdings"),
        "FIX_C": cfg.fix_c, "DIFF": cfg.diff, "dna_step": step, "signal": row.get("DNA signal"),
        "P0": getattr(anchor, "p0", None), "P_acted": getattr(anchor, "prev_price", None),
        "A_prev": getattr(anchor, "prev_actual", None),
        "E_prev": getattr(anchor, "prev_excess", None),
        "prev_holdings": getattr(anchor, "prev_holdings", None),
        "dp": cfg.decimal_precision, "inc": cfg.quantity_increment,
        "strategy": cfg.strategy_id, "genesis": anchor is None,
    }
    outputs = {
        "status": row.get("สถานะ"), "action": row.get("คำสั่ง"), "side": row.get("ฝั่ง"),
        "reason": row.get("เหตุผล"), "qty": row.get("จำนวนสั่ง (หุ้น)"),
        "V_n": row.get("มูลค่าพอร์ต (USD)"), "gap": row.get("ส่วนต่างเป้าหมาย (USD)"),
        "R_n": row.get("Rₙ อ้างอิง (USD)"), "dA": row.get("ΔAₙ ต่อสเต็ป (USD)"),
        "A_n": row.get("Aₙ สะสม (USD)"), "E_n": row.get("Eₙ ส่วนเกินสะสม (USD)"),
        "acted": meta.get("acted"), "p0_next": meta.get("p0_next"),
        "acted_price_next": meta.get("acted_price_next"),
    }
    equation("D08", inputs, outputs, DECISION_FORMULA)


@_safe
def decision_gates(auto, outbox_blocked, circuit, fence, stop, preflight, row) -> None:
    """D09: why this row may, or may not, become an order intent."""
    node("D09", outbox_blocked or ("auto" if auto else "observe"), auto=bool(auto),
         outbox_blocked=outbox_blocked,
         circuit_halted=pick(circuit, "halted").get("halted"),
         consecutive_rejects=pick(circuit, "consecutive_broker_rejects").get(
             "consecutive_broker_rejects"),
         inflight_run_id=pick(fence, "inflight_run_id").get("inflight_run_id"),
         operator_halt=bool(pick(stop, "halted").get("halted")),
         preflight_ok=pick(preflight, "ok").get("ok"),
         blocked_by=pick(preflight, "blocked_by").get("blocked_by"),
         row_status=row.get("สถานะ"))


FILL_FORMULA = (
    "dA=FIX_C*(P_fill/P_acted-1); A=A_prev+dA; R=FIX_C*ln(P_decision/P0) less the funding "
    "offset; E=A-R; holdings_after must have moved by the filled quantity.")


@_safe
def fill_equation(cfg, intent, summary, finalized) -> None:
    """W13: the fill-time recurrence, so dA/A/E can be recomputed from the recorded inputs."""
    inputs = {
        "FIX_C": getattr(cfg, "fix_c", None), "side": intent.get("side"),
        "qty": summary.get("filled_quantity"), "P_fill": summary.get("filled_price"),
        "fee": summary.get("filled_fee"), "P_decision": intent.get("decision_price"),
        "holdings_decision": intent.get("decision_holdings"),
        "P_acted": finalized.get("previous_action_price"),
        "A_prev": finalized.get("previous_actual_cumulative"),
    }
    outputs = {key: finalized.get(key) for key in (
        "applied", "seq", "delta_actual", "actual_cumulative", "excess", "reference",
        "holdings_after", "funding_reference_offset")}
    outputs["status"] = "applied" if finalized.get("applied") else "already_applied"
    equation("W13", inputs, outputs, FILL_FORMULA)


# Fields whose change on an outbox intent is worth an event even when ``status`` stays put.
_TRANSITION_WATCH = ("broker_status", "filled_quantity", "cancel_attempt_count",
                     "cancel_last_error_code", "needs_manual_check", "expiry_proof_blockers",
                     "terminal_reason", "place_attempted")


@_safe
def transition_from(chain_key, run_id, before, after, fields) -> None:
    """S01: record an outbox intent write only when its status or a watched field changed."""
    if _CURRENT.get() is None or not isinstance(after, dict):
        return
    old = before if isinstance(before, dict) else {}
    changed = {key: fields[key] for key in _TRANSITION_WATCH
               if isinstance(fields, dict) and key in fields and fields[key] != old.get(key)}
    if old.get("status") == after.get("status") and not changed:
        return
    transition(chain_key, run_id, old.get("status"), after.get("status"), changed)


@_safe
def phase(operation: str, witness, outcome: str, ms: float, remaining_ms, annotations=None) -> None:
    """A timed phase of the tick. ``sdk_*`` phases are skipped: the exchange event has them."""
    trace = _CURRENT.get()
    if trace is None or str(operation).startswith("sdk_"):
        return
    event = {"k": "ph", "op": str(operation)[:40], "w": witness, "o": outcome,
             "ms": round(float(ms), 1), "rem": remaining_ms}
    if annotations:
        event.update(_clean(annotations, max_nodes=10, max_str=80))
    _add(trace, event)


@_safe
def warning(kind: str, message: str, extra: dict | None = None) -> None:
    trace = _CURRENT.get()
    if trace is None:
        return
    trace.flags.add("warning")
    trace.warning_kinds.add(str(kind)[:60])
    event = {"k": "wn", "kind": str(kind)[:60], "msg": security_text.redact_sensitive_text(message)[:300]}
    if extra:
        event["x"] = _clean(extra, max_nodes=60, max_str=160)
    _add(trace, event)


@_safe
def transition(chain_key: str, run_id: str, before: str, after: str, changed: dict | None = None) -> None:
    """An outbox intent changed status."""
    trace = _CURRENT.get()
    if trace is None:
        return
    event = {"k": "tr", "n": "S01", "run": str(run_id), "from": str(before), "to": str(after)}
    if changed:
        event["chg"] = _clean(changed, max_nodes=40, max_str=120)
    _add(trace, event)
    if run_id and run_id not in trace.runs and len(trace.runs) < 8:
        trace.runs.append(str(run_id))


@_safe
def error(where: str, exc: BaseException, node_id: str = "X00") -> None:
    trace = _CURRENT.get()
    if trace is None:
        return
    trace.flags.add("error")
    event = {"k": "er", "n": node_id, "w": str(where)[:80], "type": type(exc).__name__,
             "msg": security_text.redact_sensitive_text(exc)[:300]}
    _add(trace, event)


# --------------------------------------------------------------------------- Webull exchanges

def _as_list(payload) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "items", "orders", "positions", "list"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _legs(rows: list) -> list[dict]:
    """Flatten grouped v3 order responses into their legs without raising."""
    legs: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        group = row.get("items") if isinstance(row.get("items"), list) else row.get("orders")
        if isinstance(group, list) and group:
            legs.extend(leg for leg in group if isinstance(leg, dict))
        else:
            legs.append(row)
    return legs


def _same_symbol(row: dict, symbol: str | None) -> bool:
    if not symbol:
        return True
    return str(row.get("symbol") or "").strip().upper() == str(symbol).strip().upper()


def _project(op: str, symbol: str | None, payload):
    """Keep what an audit needs from a response; drop other symbols' data."""
    if op == "positions":
        rows = [r for r in _as_list(payload) if isinstance(r, dict)]
        mine = [r for r in rows if _same_symbol(r, symbol)]
        return {"count": len(rows), "rows": [_clean(r, max_nodes=40, max_str=80) for r in mine[:3]]}
    if op in {"open_orders", "order_history"}:
        legs = _legs(_as_list(payload))
        mine = [r for r in legs if _same_symbol(r, symbol)]
        nxt = isinstance(payload, dict) and bool(payload.get("pagination_key"))
        return {"count": len(legs), "next": nxt,
                "rows": [_clean(r, max_nodes=60, max_str=80) for r in mine[:10]]}
    if op == "accounts":
        rows = _as_list(payload)
        return {"count": len(rows) if rows else (1 if isinstance(payload, dict) and payload else 0)}
    if op == "balance" and isinstance(payload, dict):
        assets = payload.get("account_currency_assets")
        if isinstance(assets, list):
            usd = [a for a in assets if isinstance(a, dict)
                   and str(a.get("currency") or "").upper() == "USD"]
            return {"usd": [_clean(a, max_nodes=40, max_str=80) for a in usd[:1]],
                    "currencies": len(assets)}
    if op in {"snapshot", "instrument"}:
        rows = [r for r in (_as_list(payload) or ([payload] if isinstance(payload, dict) else []))
                if isinstance(r, dict)]
        mine = [r for r in rows if _same_symbol(r, symbol)]
        return {"count": len(rows), "rows": [_clean(r, max_nodes=60, max_str=80) for r in mine[:2]]}
    return _clean(payload, max_nodes=200, max_str=240)


def _stable_order_facts(payload) -> dict:
    """Fields that identify an order's state, for change detection (no timestamps)."""
    node_ = payload
    if isinstance(payload, dict):
        group = _legs(_as_list(payload)) if _as_list(payload) else []
        node_ = group[0] if group else payload
    if not isinstance(node_, dict):
        return {}
    facts = {}
    for out, keys in (("st", ("order_status", "status")),
                      ("fq", ("filled_quantity", "filled_qty", "cum_filled_quantity")),
                      ("tq", ("total_quantity", "quantity", "qty"))):
        for key in keys:
            if node_.get(key) is not None:
                facts[out] = str(node_[key])[:40]
                break
    return facts


@_safe
def webull_exchange(op: str, route: str, method: str, ms: float, *, status=None,
                    request_id=None, query=None, body=None, payload=None,
                    error_info: dict | None = None) -> None:
    """One Webull SDK call: what was sent, what came back, how long it took."""
    trace = _CURRENT.get()
    if trace is None:
        return
    symbol = trace.meta.get("symbol")
    event: dict = {"k": "wb", "op": str(op)[:30], "rt": str(route)[:80], "m": str(method)[:8],
                   "ms": round(float(ms), 1)}
    if status is not None:
        event["st"] = status
    if request_id:
        event["rid"] = str(request_id)[:64]
    if op in _MUTATIONS:
        event["mut"] = True
        trace.flags.add("mutation")
    skip_body = any(marker in str(route) for marker in _NO_BODY_ROUTES)
    if error_info:
        event["err"] = _clean(error_info, max_nodes=10, max_str=240)
        trace.flags.add("webull_error")
    elif isinstance(status, int) and status >= 400:
        trace.flags.add("webull_error")
    if not skip_body:
        if payload is not None:
            event["sha"] = _sha(payload)
            event["len"] = _size(payload)
        if bodies() == "full":
            request_part = {}
            if query:
                request_part["q"] = _clean(query, max_nodes=30, max_str=100)
            if body:
                request_part["b"] = _clean(body, max_nodes=80, max_str=120)
            if request_part:
                event["req"] = request_part
            if payload is not None:
                event["res"] = _project(op, symbol, payload)
        facts = _stable_order_facts(payload) if op == "order_detail" and payload is not None else {}
        if facts:
            event["fact"] = facts
    _add(trace, event)


# --------------------------------------------------------------------------- envelope + flush

def _timebox(fn, seconds: float):
    """Run ``fn`` on a helper thread and stop waiting after ``seconds``."""
    box: dict = {}

    def run():
        try:
            fn()
        except Exception as exc:  # reported, never raised
            box["err"] = exc

    worker = threading.Thread(target=run, name="lego-trace-write", daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        return "timeout", None
    return ("error", box["err"]) if "err" in box else ("ok", None)


def _warn_once(reason: str) -> None:
    now = time.monotonic()
    last = _STATS.get("warn_mono")
    if last is None or now - last >= WARN_EVERY_SECONDS:
        _STATS["warn_mono"] = now
        logger.warning("lego trace unavailable reason=%s", reason)


def _env_snapshot() -> dict:
    snap = {}
    for name, value in os.environ.items():
        if _ENV_ALLOW.match(name) and not _ENV_DENY.search(name):
            snap[name] = security_text.redact_sensitive_text(value)[:200]
    return dict(sorted(snap.items()))


def _path_of(trace: _Trace) -> str:
    """The route this tick took through the flow chart, e.g. ``D00>D05>D08[READY_BUY]|W10>W11[x]``."""
    tokens: list[str] = []
    group = None
    for event in trace.events:
        node_id = event.get("n")
        if not node_id or node_id == "S01":
            continue
        info = NODES.get(node_id)
        kind = info["group"] if info else "ops"
        token = node_id + (f"[{event['tag']}]" if event.get("tag") else "")
        if tokens and tokens[-1] == token:
            continue
        if kind in {"decision", "worker"}:
            if group is not None and kind != group:
                tokens.append("|")
            group = kind
        tokens.append(token)
    return ">".join(tokens).replace(">|>", "|")[:400]


def _results(body: dict) -> list[dict]:
    items = []
    for name in ("recovery", "dispatch"):
        phase_ = body.get(name) if isinstance(body, dict) else None
        for item in (phase_ or {}).get("results", []) if isinstance(phase_, dict) else []:
            if isinstance(item, dict):
                items.append({"ph": name, **{k: item.get(k) for k in (
                    "run_id", "status", "broker_status", "needs_manual_check",
                    "cancel_attempt_count", "cancel_last_error_code", "cancel_refused_at",
                    "reconciliation_overdue", "reconciliation_age_seconds",
                    "expiry_proof_blockers", "expiry_proof_checked_at", "expiry_released",
                    "deferred_reason", "filled_quantity") if item.get(k) is not None}})
    return items


def _signature(trace: _Trace, body: dict, pipe, biz, results: list[dict]) -> str:
    unstable = {"reconciliation_age_seconds", "expiry_proof_checked_at", "cancel_refused_at"}
    stable_results = [{k: v for k, v in item.items() if k not in unstable} for item in results]
    errors = sorted({(e.get("op"), (e.get("err") or {}).get("code"), (e.get("err") or {}).get("http"))
                     for e in trace.events if e.get("k") == "wb" and e.get("err")}, key=str)
    details = sorted([json.dumps(e.get("fact"), sort_keys=True) for e in trace.events
                      if e.get("k") == "wb" and e.get("fact")])
    decision = (body.get("decision") or {}) if isinstance(body, dict) else {}
    return _sha({"pipe": pipe, "biz": biz, "dec": decision.get("status"), "res": stable_results,
                 "wn": sorted(trace.warning_kinds), "we": errors, "od": details})


def _reasons(trace: _Trace, biz, pipe, first_of_boot: bool) -> set[str]:
    reasons = set(trace.flags)
    if biz not in _IDLE_BIZ:
        reasons.add(_REASON_BY_BIZ.get(str(biz), "biz_other"))
    if "ERROR" in str(pipe or ""):
        reasons.add("error")
    if first_of_boot:
        reasons.add("boot")
    if level() == "all":
        reasons.add("level_all")
    return reasons


def _request_ids(request) -> dict:
    out = {}
    try:
        headers = getattr(request, "headers", None) or {}
        ctx = str(headers.get("X-Cloud-Trace-Context", ""))
        match = re.match(r"^([0-9a-f]{32})/", ctx)
        if match:
            out["trace"] = match.group(1)
        execution = headers.get("Function-Execution-Id")
        if isinstance(execution, str) and execution:
            out["exec"] = execution[:32]
    except Exception:
        pass
    return out


def _build_envelope(trace: _Trace, body: dict, code, pipe, biz, results, reasons, request,
                    first_of_boot: bool, partial: bool) -> dict:
    decision = (body.get("decision") or {}) if isinstance(body, dict) else {}
    env = {
        "v": SCHEMA, "tick": trace.tick, "at": _iso(trace.started),
        "dur_ms": int((time.monotonic() - trace.t0) * 1000), "http": code,
        "pipe": pipe, "biz": biz,
        "rev": os.environ.get("K_REVISION"), "cand": os.environ.get("LEGO_CANDIDATE_HASH"),
        "git": os.environ.get("LEGO_GIT_COMMIT"),
        "env": trace.meta.get("env"), "mode": trace.meta.get("mode"), "sym": trace.meta.get("symbol"),
        "chain": trace.meta.get("chain_key"),
        "slot": decision.get("market_slot_id"), "step": decision.get("step"),
        "runs": list(trace.runs), "path": _path_of(trace), "why": sorted(reasons),
        "res": _clean(results, max_nodes=100, max_str=120) if results else None,
        "n": len(trace.events), "bytes": trace.bytes, "dropped": trace.dropped,
        "rec_err": trace.errors, "partial": bool(partial), "level": level(), "bodies": bodies(),
        "boot_id": _BOOT_ID, "events": trace.events,
    }
    env.update({f"cloud_{k}": v for k, v in _request_ids(request).items()})
    if first_of_boot:
        env["boot"] = {"id": _BOOT_ID, "at": _iso(_BOOT_AT), "env": _env_snapshot()}
    return {k: v for k, v in env.items() if v is not None}


def _heartbeat(trace: _Trace, pipe, biz, code, key) -> dict:
    stats = _STATS
    return {k: v for k, v in {
        "at": _iso(datetime.now(UTC)), "tick": trace.tick, "pipe": pipe, "biz": biz, "http": code,
        "rev": os.environ.get("K_REVISION"), "cand": os.environ.get("LEGO_CANDIDATE_HASH"),
        "git": os.environ.get("LEGO_GIT_COMMIT"), "env": trace.meta.get("env"),
        "mode": trace.meta.get("mode"), "sym": trace.meta.get("symbol"),
        "boot_id": _BOOT_ID, "boot_at": _iso(_BOOT_AT), "level": level(),
        "ticks": stats["ticks"], "notable": stats["notable"], "written": stats["written"],
        "skipped_dup": stats["skipped_dup"], "skipped_budget": stats["skipped_budget"],
        "write_errors": stats["write_errors"], "flush_timeouts": stats["flush_timeouts"],
        "internal_errors": stats["internal_errors"], "dropped_events": stats["dropped_events"],
        "last_trace": key or stats["last_trace"],
    }.items() if v is not None}


def _persist(chain: str, day: str, key: str | None, envelope: dict | None, heartbeat: dict | None) -> None:
    if envelope is not None:
        db.reference(f"{TRACE_PATH}/{chain}/{day}/{key}").set(envelope)
        if (chain, day) not in _DAYS_SEEN:
            db.reference(f"{DAYS_PATH}/{chain}/{day}").set(True)
            _DAYS_SEEN.add((chain, day))
    if heartbeat is not None:
        db.reference(f"{HEARTBEAT_PATH}/{chain}").set(heartbeat)


def _write(chain, day, key, envelope, heartbeat) -> str:
    status, err = _timebox(lambda: _persist(chain, day, key, envelope, heartbeat), FLUSH_TIMEBOX_SECONDS)
    if status == "timeout":
        _STATS["flush_timeouts"] += 1
        _warn_once("timeout")
    elif status == "error":
        _STATS["write_errors"] += 1
        _warn_once(type(err).__name__)
    return status


@_safe
def checkpoint() -> None:
    """Write what is known so far (called right after Place/Cancel, before anything can crash)."""
    trace = _CURRENT.get()
    if trace is None or level() == "off":
        return
    left = _remaining()
    if left is not None and left < FLUSH_MIN_BUDGET_SECONDS - 4:
        return
    chain = _chain_label(trace.meta.get("chain_key"))
    day = trace.started.strftime("%Y-%m-%d")
    key = f"{trace.started.strftime('%H%M%S')}_{trace.tick[:8]}"
    envelope = _build_envelope(trace, {}, None, None, None, [], {"mutation"}, None, False, True)
    if _write(chain, day, key, envelope, None) == "ok":
        trace.checkpointed = True


@_safe
def finish(body: dict, code, request=None) -> dict | None:
    """Decide whether this tick is worth keeping and write it (time-boxed, best effort)."""
    trace = _CURRENT.get()
    if trace is None or level() == "off":
        return None
    body = body if isinstance(body, dict) else {}
    pipe = body.get("pipeline_status")
    biz = body.get("business_status")      # emit_tick sets it before finish() runs
    results = _results(body)
    _STATS["ticks"] += 1
    _STATS["dropped_events"] += trace.dropped
    first_of_boot = not _STATS["booted"]
    reasons = _reasons(trace, biz, pipe, first_of_boot)
    notable = bool(reasons)
    chain = _chain_label(trace.meta.get("chain_key"))
    now_mono = time.monotonic()
    result = {"written": False, "key": None, "reasons": sorted(reasons), "skipped": None,
              "events": len(trace.events), "bytes": trace.bytes}
    heartbeat_due = (_STATS["hb_mono"] is None or now_mono - _STATS["hb_mono"] >= HEARTBEAT_SECONDS)
    if not notable:
        if heartbeat_due and _budget_ok():
            _STATS["hb_mono"] = now_mono
            _write(chain, trace.started.strftime("%Y-%m-%d"), None, None,
                   _heartbeat(trace, pipe, biz, code, None))
        result["skipped"] = "idle"
        return result
    _STATS["notable"] += 1
    signature = _signature(trace, body, pipe, biz, results)
    last = _LAST.get(chain)
    if (reasons <= _STEADY and last and last["sig"] == signature
            and now_mono - last["mono"] < DEDUP_SECONDS):
        _STATS["skipped_dup"] += 1
        result["skipped"] = "duplicate"
        if heartbeat_due and _budget_ok():
            _STATS["hb_mono"] = now_mono
            _write(chain, trace.started.strftime("%Y-%m-%d"), None, None,
                   _heartbeat(trace, pipe, biz, code, None))
        return result
    if not _budget_ok(mutation="mutation" in reasons):
        _STATS["skipped_budget"] += 1
        result["skipped"] = "budget"
        return result
    day = trace.started.strftime("%Y-%m-%d")
    key = f"{trace.started.strftime('%H%M%S')}_{trace.tick[:8]}"
    envelope = _build_envelope(trace, body, code, pipe, biz, results, reasons, request,
                               first_of_boot, False)
    # Counters in the heartbeat describe the write we are about to make.
    _STATS["written"] += 1
    _STATS["last_trace"] = f"{chain}/{day}/{key}"
    heartbeat = _heartbeat(trace, pipe, biz, code, f"{chain}/{day}/{key}")
    status = _write(chain, day, key, envelope, heartbeat)
    if status != "ok":
        _STATS["written"] -= 1
        result["skipped"] = "write_" + status
        return result
    _STATS["booted"] = True
    _STATS["hb_mono"] = now_mono
    _LAST[chain] = {"sig": signature, "mono": now_mono}
    result.update(written=True, key=f"{chain}/{day}/{key}")
    return result


def _budget_ok(mutation: bool = False) -> bool:
    left = _remaining()
    floor = FLUSH_MIN_BUDGET_MUTATION_SECONDS if mutation else FLUSH_MIN_BUDGET_SECONDS
    return left is None or left >= floor


@_safe
def prune(chain_key: str, now: datetime | None = None) -> str | None:
    """Delete the oldest expired day bucket (at most one per call, at most hourly)."""
    if level() == "off":
        return None
    mono = time.monotonic()
    last = _STATS.get("prune_mono")
    if last is not None and mono - last < PRUNE_EVERY_SECONDS:
        return None
    left = _remaining()
    if left is not None and left < PRUNE_MIN_BUDGET_SECONDS:
        return None
    _STATS["prune_mono"] = mono
    chain = _chain_label(chain_key)
    days = db.reference(f"{DAYS_PATH}/{chain}").get() or {}
    if not isinstance(days, dict):
        return None
    cutoff = ((now or datetime.now(UTC)).astimezone(UTC).date()
              - timedelta(days=retention_days())).isoformat()
    expired = sorted(d for d in days if isinstance(d, str) and _DAY.match(d) and d < cutoff)
    if not expired:
        return None
    day = expired[0]

    def delete():
        db.reference(f"{TRACE_PATH}/{chain}/{day}").delete()
        db.reference(f"{DAYS_PATH}/{chain}/{day}").delete()

    status, err = _timebox(delete, FLUSH_TIMEBOX_SECONDS)
    if status != "ok":
        _warn_once("prune_" + (type(err).__name__ if err else status))
        return None
    _DAYS_SEEN.discard((chain, day))
    return day
