"""AUTO_SUBMIT readiness gate — validation only, never submission.

The deploy checklist used to live outside the code: a human was expected to move
the token off /tmp, set LEGO_DNA_ORIGIN_UTC, switch the clock to `market` and
check the DNA headroom *before* flipping AUTO_SUBMIT. Nothing enforced any of
it, so the flag alone decided whether a committed row became an order intent —
every unmet condition was a silent fail-open.

This module turns that checklist into the thing that answers the question. It is
deliberately pure: no RTDB, no broker, no environment reads of its own, no
writes. It cannot raise either — an input it cannot read is a *failed check*,
because a validator that throws would hand the caller an error path that skips
the gate entirely.
"""
from __future__ import annotations

from lego_one_row import READY_BUY, READY_SELL
from lego_orders import UAT

AUTO_SUBMIT_BLOCKED_WARNING = "auto_submit_blocked"
DEGRADED_CLOCK_WARNING = "degraded_clock_no_order"

# Kept verbatim from the branch this gate replaced: a degraded clock is the one
# blocked condition that already had a response field, a warning kind and a
# dashboard reading it, so it keeps all three.
DEGRADED_CLOCK_MESSAGE = ("degraded clock: ไม่มี slot window จึงคำนวณ expires_at ไม่ได้ "
                          "— แถวนี้ commit แล้วแต่ไม่มีการสร้าง order intent")
DEGRADED_CLOCK_HINT = ("ตั้ง LEGO_DNA_ORIGIN_UTC (find_origin.py) "
                       "แล้วเปิด LEGO_DNA_CLOCK_MODE=market")

BLOCKED_FIELD = "outbox_blocked"
SKIPPED_FIELD = "outbox_skipped"
DEFAULT_MIN_DNA_REMAINING = 1

# Declaration order is also report order: the first failing check names the
# response field, the warning kind and the message, so a degraded clock keeps
# answering exactly as it did before even when something else is wrong too.
CHECK_IDS = (
    "auto_submit_enabled",
    "environment_uat",
    "row_durable",
    "row_actionable",
    "clock_not_degraded",
    "step_matches_market_ordinal",
    "token_ready",
    "dna_headroom",
)


def _check(check_id: str, ok: bool, reason: str = "", *, field: str = BLOCKED_FIELD,
           warning_kind: str = AUTO_SUBMIT_BLOCKED_WARNING, hint: str = "",
           applicable: bool = True) -> dict:
    return {
        "id": check_id,
        "ok": bool(ok),
        "applicable": bool(applicable),
        "reason": "" if ok else reason,
        "field": field,
        "warning_kind": warning_kind,
        "hint": hint,
    }


def _row_status(row) -> str:
    try:
        return str(row.get("สถานะ") or "")
    except Exception:                      # noqa: BLE001 - unreadable row fails the check
        return ""


def _row_quantity(row) -> float:
    """0.0 for anything unreadable, which the quantity check refuses."""
    try:
        quantity = float(row["_meta"]["quantity"])
    except Exception:                      # noqa: BLE001 - see module docstring
        return 0.0
    return quantity if quantity == quantity and abs(quantity) != float("inf") else 0.0


def _row_step(row):
    try:
        return int(row["DNA step"])
    except Exception:                      # noqa: BLE001 - see module docstring
        return None


def _int_or_none(value):
    try:
        return int(value)
    except Exception:                      # noqa: BLE001 - see module docstring
        return None


def evaluate_auto_submit_preflight(*, auto_submit, environment, row, row_durable,
                                   slot, token, dna_remaining,
                                   min_dna_remaining: int = DEFAULT_MIN_DNA_REMAINING,
                                   token_proved_live: bool = False) -> dict:
    """Every condition that must hold before a committed row may become an order.

    Returns a report; raises nothing the caller has to handle. `ok` is True only
    when every applicable check passed, so a report that could not be built at
    all still reads as blocked.

    `token_proved_live` is the caller's evidence that this same token already
    signed an authenticated broker request in this invocation. Default False, so
    a caller that has no such evidence keeps the strict reading.
    """
    checks: list[dict] = []

    checks.append(_check(
        "auto_submit_enabled", auto_submit is True,
        "AUTO_SUBMIT ไม่ได้เปิด — ไม่สร้าง order intent"))

    checks.append(_check(
        "environment_uat", environment == UAT,
        f"ส่ง order ได้เฉพาะ {UAT}; ปัจจุบัน={environment}"))

    checks.append(_check(
        "row_durable", row_durable is True,
        "แถวยัง persist ไม่สำเร็จ — ห้ามสร้าง order intent จากแถวที่ยังไม่ committed"))

    status, quantity = _row_status(row), _row_quantity(row)
    checks.append(_check(
        "row_actionable", status in (READY_BUY, READY_SELL) and quantity > 0,
        f"สถานะ {status or 'ไม่ทราบ'} quantity {quantity} ไม่ใช่ decision ที่ส่งได้"))

    checks.append(_check(
        "clock_not_degraded", slot is not None, DEGRADED_CLOCK_MESSAGE,
        field=SKIPPED_FIELD, warning_kind=DEGRADED_CLOCK_WARNING,
        hint=DEGRADED_CLOCK_HINT))

    # The fail-open this gate exists for: in shadow mode the row's step comes
    # from anchor + 1, which drifts from the bar index the DNA was trained on the
    # moment the scheduler misses a slot. Committing such a row is fine — it is
    # shadow — but sending a real order on it is trading a different slot than
    # the backtest did. Not evaluable without a slot, and the check above already
    # holds the gate closed in that case, so it is reported as not applicable
    # rather than silently passed.
    if slot is None:
        checks.append(_check(
            "step_matches_market_ordinal", False,
            "ไม่มี slot จึงเทียบ market ordinal ไม่ได้ (ติดที่ clock_not_degraded แล้ว)",
            applicable=False))
    else:
        step = _row_step(row)
        ordinal = _int_or_none(getattr(slot, "market_ordinal", None))
        checks.append(_check(
            "step_matches_market_ordinal",
            step is not None and ordinal is not None and step == ordinal,
            f"DNA step {step} ไม่ตรง market ordinal {ordinal} — "
            "order จะไปตกคนละ slot กับที่ DNA เทรนมา"))

    # `ready`, not `ok`: token_health's `ok` also carries the durability warning
    # for a token dir that cannot survive a container recycle, and on Cloud
    # Functions that is every deployment. Gating orders on it made this check
    # impossible to pass, so committed READY_BUY/READY_SELL rows never became
    # intents and the position never moved. `ready` drops that one warning only
    # when the operator accepted it; a health report without the key (an older
    # caller, a test double) still reads `ok` and keeps the strict behaviour.
    token = token if isinstance(token, dict) else {}
    token_reasons = token.get("reasons") or []
    if not isinstance(token_reasons, (list, tuple)):
        token_reasons = [str(token_reasons)]
    token_usable = token["ready"] if "ready" in token else token.get("ok")
    # ...and `ready` alone was still unsatisfiable in practice, because the flag
    # that relaxes it is off by default while /tmp is the only writable path
    # Cloud Functions offers. That left the deployed default in exactly the state
    # this gate exists to prevent: every slot committing READY_SELL with the
    # order blocked on a token that had just authenticated two calls in the same
    # invocation. So a local-file verdict yields to live proof — the caller says
    # the token signed a real broker request moments ago, which answers this
    # check's question directly and better than any file inspection can.
    #
    # `live_proof_supersedable` is what token_health offers now; it covers the
    # durability warning and one more finding that turned out to be equally
    # unanswerable from disk — no token file at all. On a broker app with token
    # checking disabled the SDK never writes one and never will, so that reason
    # alone shut the gate permanently and the position never moved.
    # `durability_risk_only` is still honoured on its own so a health report from
    # before the wider field existed keeps working unchanged.
    #
    # Deliberately narrow either way: both flags are False the moment any reason
    # is the broker's or the file's own verdict on the token, so a rejected or
    # expiring token still blocks no matter what the caller proved.
    if (token_usable is not True and token_proved_live is True
            and (token.get("durability_risk_only") is True
                 or token.get("live_proof_supersedable") is True)):
        token_usable = True
    checks.append(_check(
        "token_ready", token_usable is True,
        "token ยังไม่พร้อม: " + ("; ".join(str(r) for r in token_reasons)
                                 or "token_health ไม่ได้บอกว่า ok")))

    remaining = _int_or_none(dna_remaining)
    minimum = _int_or_none(min_dna_remaining)
    minimum = DEFAULT_MIN_DNA_REMAINING if minimum is None else minimum
    checks.append(_check(
        "dna_headroom", remaining is not None and remaining >= minimum,
        f"DNA เหลือ {remaining} step (ต้อง >= {minimum}) — "
        "chain ใกล้หมดอายุ ไม่ควรเปิด order ใหม่"))

    failed = [c for c in checks if c["applicable"] and not c["ok"]]
    primary = failed[0] if failed else None
    return {
        "ok": not failed,
        "checks": checks,
        "blocked_by": [c["id"] for c in failed],
        "reasons": [c["reason"] for c in failed],
        "message": primary["reason"] if primary else "",
        "field": primary["field"] if primary else "",
        "warning_kind": primary["warning_kind"] if primary else "",
        "hint": primary["hint"] if primary else "",
    }


def auto_submit_preflight(**kwargs) -> dict:
    """evaluate_auto_submit_preflight with the last fail-closed wrapper.

    The evaluator is already written not to raise; this exists so that a future
    edit which forgets that rule blocks the order instead of propagating an
    exception into the caller's error path.
    """
    try:
        return evaluate_auto_submit_preflight(**kwargs)
    except Exception as exc:               # noqa: BLE001 - see docstring
        reason = f"preflight ล้มเหลว: {type(exc).__name__}: {exc}"
        return {
            "ok": False,
            "checks": [],
            "blocked_by": ["preflight_error"],
            "reasons": [reason],
            "message": reason,
            "field": BLOCKED_FIELD,
            "warning_kind": AUTO_SUBMIT_BLOCKED_WARNING,
            "hint": "",
        }

