"""Order gates, broker-result normalization, and realized matched-cycle math."""
from __future__ import annotations

import math
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from lego_one_row import READY_BUY, READY_SELL

UAT = "Test (UAT)"
PROD = "Production"


class SubmitGateError(RuntimeError):
    pass


def _confirmation_phrase(side, quantity, symbol, step) -> str:
    """One canonical spelling so two independent sources can be compared.

    Numbers are normalized because the two sides do not travel the same way: one
    is held in memory, the other round-trips through RTDB, which may hand a whole
    float back as an int. Comparing 25.0 with 25 would block a perfectly good
    order, so the phrase — not the caller — decides the spelling.
    """
    return f"CONFIRM {side} {float(quantity)} {symbol} STEP {int(step)}"


def order_confirmation_phrase(row: dict) -> str:
    """Side, quantity, symbol and step of one row, in one comparable line."""
    m = row["_meta"]
    return _confirmation_phrase(m["side"], m["quantity"], row["สินทรัพย์"], m["step"])


def evaluate_submit_gate(environment: str, row: dict, preview_ok: bool,
                         confirmation_input: str, committed: bool, *,
                         release_authorized: bool | None = None) -> None:
    """Refuse anything that is not exactly what the engine committed.

    `row` must be the committed row and `confirmation_input` the phrase of the
    intent about to be sent — two sources that travelled separately. Generating
    both from the same object makes the last check compare a value with itself,
    which is how it silently stopped being a check at all.
    """
    if environment not in {UAT, PROD}:
        raise SubmitGateError(f"environment ไม่รองรับ: {environment}")
    # None retains the old pure-function test contract. The deployed v2
    # entrypoint always supplies a boolean derived from an account/environment/
    # candidate-bound deployment profile.
    if environment == PROD and release_authorized is not True:
        raise SubmitGateError(
            "Production ต้องมี release/account authorization binding — ห้าม submit")
    if release_authorized is False:
        raise SubmitGateError(
            "release/account authorization binding ไม่ผ่าน — ห้าม submit")
    if not committed:
        raise SubmitGateError("แถวยังไม่ persist — ห้าม submit")
    if row["สถานะ"] not in (READY_BUY, READY_SELL):
        raise SubmitGateError(f"สถานะ {row['สถานะ']} ไม่ใช่ READY_* — ไม่ส่ง")
    m = row["_meta"]
    if not (m["quantity"] and m["quantity"] > 0):
        raise SubmitGateError("quantity <= 0 — ไม่ส่ง")
    if not preview_ok:
        raise SubmitGateError("preview ไม่ผ่าน — ไม่ส่ง")
    # Meaningful only when the caller passes a phrase from a *different* source
    # than `row`; the dispatcher passes the outbox intent's phrase and `row` is
    # rebuilt from the committed RTDB row.
    if confirmation_input != order_confirmation_phrase(row):
        raise SubmitGateError(
            f"confirmation phrase ไม่ตรงกับแถวที่ commit ไว้ — ไม่ส่ง "
            f"(intent={confirmation_input!r} committed={order_confirmation_phrase(row)!r})")


REALIZED_STATUSES = {"FILLED", "PARTIAL_FILLED", "PARTIALLY_FILLED"}
TERMINAL_STATUSES = {"FILLED", "CANCELLED", "FAILED", "REJECTED", "EXPIRED"}
EXECUTION_PRICE_FIELDS = (
    "average_filled_price", "avg_filled_price", "average_fill_price",
    "avg_fill_price", "filled_price", "fill_price", "executed_price",
    "execution_price",
)
FEE_FIELDS = ("transaction_fee", "filled_fee", "execution_fee", "commission", "fee")


def normalize_status(raw) -> str:
    return str(raw or "").strip().upper().replace(" ", "_")


def _order_fields(detail) -> dict:
    if isinstance(detail, list):
        detail = detail[0] if detail else {}
    if not isinstance(detail, dict):
        return {}
    for key in ("items", "orders", "data"):
        inner = detail.get(key)
        if isinstance(inner, list) and inner and isinstance(inner[0], dict):
            merged = dict(detail)
            merged.update(inner[0])
            return merged
        if isinstance(inner, dict):
            merged = dict(detail)
            merged.update(inner)
            return merged
    return detail


def _coalesce_decimal_string(
        fields: dict, names: tuple[str, ...],
        minimum_exclusive: Decimal | None = None) -> str | None:
    """Return a validated broker number without a binary-float round trip.

    Broker quantity, average price, and fee are accounting facts.  Keeping the
    canonical Decimal spelling here lets the broker-cashflow ledger consume the
    exact value while legacy model/display code may opt into ``float`` later.
    """
    for name in names:
        value = fields.get(name)
        if value is None or value == "" or isinstance(value, bool):
            continue
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError):
            continue
        if not number.is_finite():
            continue
        if minimum_exclusive is not None and not (number > minimum_exclusive):
            continue
        if minimum_exclusive is None and number < 0:
            continue
        return str(number)
    return None


def summarize_order_result(place_response: dict, detail: dict | None = None) -> dict:
    fields = _order_fields(detail) if detail else {}
    status = normalize_status(
        fields.get("order_status") or fields.get("status")
        or (place_response or {}).get("order_status")
        or (place_response or {}).get("status"))
    filled = _coalesce_decimal_string(
        fields, ("filled_quantity", "filled_qty"))
    filled_number = Decimal(filled) if filled is not None else None
    # Shares moved or they did not; the status is a label on top of that. Keying
    # this on the status set alone lost every fill that ended somewhere else: a
    # DAY order that fills 30 of 70 and is cancelled at the close reports
    # CANCELLED with filled_quantity=30, and those 30 real shares never reached
    # the realized ledger. Double counting is not the risk — apply_realized_fill
    # stores the cumulative quantity per order, so seeing the same 30 again is a
    # zero-sized delta.
    realized = status in REALIZED_STATUSES or bool(
        filled_number is not None and filled_number > 0)
    out = {
        "status": status or "UNKNOWN",
        "realized": realized,
        "note": "realized ใช้เฉพาะ fill จริง; model ledger แยกจาก broker ledger",
    }
    if filled is not None:
        out["filled_quantity"] = filled
    price = _coalesce_decimal_string(
        fields, EXECUTION_PRICE_FIELDS, minimum_exclusive=Decimal("0"))
    if price is not None:
        out["filled_price"] = price
    fee = _actual_fee(fields)
    if fee is not None:
        out["filled_fee"] = fee
    reason = (fields.get("reason") or fields.get("message")
              or fields.get("error_msg") or fields.get("reject_reason"))
    if reason:
        out["reject_reason"] = str(reason)
    return out


def _actual_fee(fields: dict) -> str | None:
    """Normalize actual cumulative fees, never receivables or preview estimates.

    The current TH/global contract has a commission object and a fees array.
    Partial breakdowns stay unknown: a missing component is not a zero. Legacy
    scalar totals remain supported for the pinned legacy endpoint.
    """
    commission = fields.get("commission")
    if not isinstance(commission, dict) and "fees" not in fields:
        return _coalesce_decimal_string(fields, FEE_FIELDS)
    if not isinstance(commission, dict) or not isinstance(fields.get("fees"), list):
        return None
    actual = _coalesce_decimal_string(commission, ("actual_commission",))
    if actual is None:
        return None
    total = Decimal(actual)
    seen = set()
    for component in fields["fees"]:
        if not isinstance(component, dict):
            return None
        kind = component.get("type")
        value = _coalesce_decimal_string(component, ("actual_value",))
        if not isinstance(kind, str) or not kind or kind in seen or value is None:
            return None
        seen.add(kind)
        total += Decimal(value)
    # A legacy total alongside a complete breakdown must agree; otherwise there
    # is no unambiguous accounting fact to book.
    legacy = _coalesce_decimal_string(fields, FEE_FIELDS)
    if legacy is not None and Decimal(legacy) != total:
        return None
    return str(total)


_LEG_EPS = 1e-9


def empty_open_legs() -> dict:
    return {"buys": [], "sells": []}


def _as_sequence(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value[k] for k in sorted(value, key=lambda x: int(x) if str(x).isdigit() else str(x))]
    return []


def normalize_open_legs(raw) -> dict:
    raw = raw or {}
    out = empty_open_legs()
    for side in ("buys", "sells"):
        for leg in _as_sequence(raw.get(side)):
            if not isinstance(leg, (list, tuple)) or len(leg) < 3:
                continue
            q, p, fee_ps = float(leg[0]), float(leg[1]), float(leg[2])
            if q > _LEG_EPS and p > 0 and fee_ps >= 0:
                out[side].append([q, p, fee_ps])
    return out


def apply_fill(open_legs: dict | None, side: str, qty: float, price: float,
               fee: float = 0.0) -> tuple[dict, float]:
    """Apply one incremental broker fill and realize only matched closed quantity.

    Returns (new_open_legs, realized_delta). Fees are allocated per share and
    deducted only when both legs are matched.
    """
    side = str(side).upper()
    if side not in ("BUY", "SELL"):
        raise ValueError("side ต้อง BUY หรือ SELL")
    if not (math.isfinite(qty) and qty > 0):
        raise ValueError("fill qty ต้อง finite และ > 0")
    if not (math.isfinite(price) and price > 0):
        raise ValueError("fill price ต้อง finite และ > 0")
    if not (math.isfinite(fee) and fee >= 0):
        raise ValueError("fill fee ต้อง finite และ >= 0")
    legs = normalize_open_legs(deepcopy(open_legs))
    fee_ps = fee / qty
    remaining = qty
    realized = 0.0
    opposite = legs["sells"] if side == "BUY" else legs["buys"]
    while remaining > _LEG_EPS and opposite:
        oq, op, ofps = opposite[0]
        matched = min(remaining, oq)
        if side == "BUY":
            realized += (op - price) * matched
        else:
            realized += (price - op) * matched
        realized -= (ofps + fee_ps) * matched
        remaining -= matched
        if oq - matched <= _LEG_EPS:
            opposite.pop(0)
        else:
            opposite[0][0] = oq - matched
    if remaining > _LEG_EPS:
        target = legs["buys"] if side == "BUY" else legs["sells"]
        target.append([remaining, price, fee_ps])
    return legs, realized
