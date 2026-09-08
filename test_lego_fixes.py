"""test_lego_fixes.py — pure-function tests (ไม่ต้องมี secret/network)

รัน: python3 -m pytest test_lego_fixes.py -q
ครอบคลุม: decision band ตาม spec (ไม่มี clamp), recurrence gated_theoretical_v2
(ΔA เทียบราคา act ล่าสุด, แช่แข็งช่วง pass, smooth Eₙ — golden จาก gated demo CSV),
DNA golden, order payload ตาม decimal_precision, _extract_qty fail-closed,
submit gate, summarize/retry, Step 18 commit protocol (fake RTDB):
idempotent / stale-anchor / repair / semantics migration / pointer แช่แข็ง
"""
import math

import pytest

from dna_engine import DNAError, decode_dna
from lego_one_row import (Anchor, Config, DNAExhausted, PASS_DNA_ZERO,
                          PASS_THRESHOLD, READY_BUY, READY_SELL,
                          RowValidationError, COLUMN_ORDER, build_decision,
                          compute_recurrence, compute_row, dna_signal_for,
                          dna_step_for, validate_row_columns)
from lego_orders import (REALIZED_STATUSES, TERMINAL_STATUSES, SubmitGateError,
                         evaluate_submit_gate, order_confirmation_phrase,
                         summarize_order_result)
from webull_io import _extract_qty, _retry_transient, build_order_payload

CFG = Config(symbol="APLS", fix_c=1500.0, diff=60.0)


# ---- Step 8: decision band ตามสัญญา (invariant #4, #5) ---------------------
def test_decision_buy_spec():
    d = build_decision(CFG, price=10.0, holdings=100.0, signal=1)
    assert (d.status, d.action, d.side) == (READY_BUY, "TRIGGER_ACTION", "BUY")
    assert d.value == 1000.0 and d.gap == 500.0
    assert d.quantity == round(500.0 / 10.0, 5) == 50.0


def test_decision_sell_spec():
    # ค่าปกติยังเท่ากับ round(|gap|/Pₙ, dp)
    d = build_decision(CFG, price=12.0, holdings=150.0, signal=1)
    assert (d.status, d.side) == (READY_SELL, "SELL")
    assert d.gap == -300.0
    assert d.quantity == round(300.0 / 12.0, 5) == 25.0
    # qty < holdings เสมอ (คณิต: qty = holdings − FIX_C/Pₙ)
    assert d.quantity < 150.0


def test_sell_rounding_never_exceeds_fractional_holdings():
    """Broker precision must not turn a valid rebalance into an oversell."""
    cfg = Config(symbol="FFWM", fix_c=1000.0, decimal_precision=5)
    holdings = 0.000016
    d = build_decision(cfg, price=1_000_000_000.0,
                       holdings=holdings, signal=1)
    assert (d.status, d.side) == (READY_SELL, "SELL")
    assert d.quantity == 0.00001
    assert d.quantity <= holdings


def test_sell_smaller_than_one_quantity_tick_fails_closed():
    cfg = Config(symbol="FFWM", fix_c=100.0, decimal_precision=5)
    d = build_decision(cfg, price=1_000_000_000.0,
                       holdings=0.000006, signal=1)
    assert d.status == PASS_THRESHOLD
    assert d.quantity == 0.0 and d.side == ""


def test_decision_pass_threshold_band():
    d = build_decision(CFG, price=10.0, holdings=145.5, signal=1)   # gap=45 ≤ 60
    assert d.status == PASS_THRESHOLD and d.quantity == 0.0 and d.side == ""


def test_decision_gate_signal_zero_wins():
    d = build_decision(CFG, price=10.0, holdings=0.0, signal=0)     # gap เต็ม 1500
    assert d.status == PASS_DNA_ZERO and d.quantity == 0.0 and d.action == "PASS"


def test_decision_invalid_inputs_fail_closed():
    with pytest.raises(ValueError):
        build_decision(CFG, price=0.0, holdings=1.0, signal=1)
    with pytest.raises(ValueError):
        build_decision(CFG, price=float("nan"), holdings=1.0, signal=1)
    with pytest.raises(ValueError):
        build_decision(CFG, price=10.0, holdings=float("nan"), signal=1)
    with pytest.raises(ValueError):
        build_decision(CFG, price=10.0, holdings=-1.0, signal=1)
    with pytest.raises(ValueError):
        build_decision(CFG, price=10.0, holdings=1.0, signal=2)


def test_config_validation():
    with pytest.raises(ValueError):
        Config(symbol="A", fix_c=0.0)
    with pytest.raises(ValueError):
        Config(symbol="A", fix_c=1500.0, diff=float("inf"))
    with pytest.raises(ValueError):
        Config(symbol="A", fix_c=1500.0, decimal_precision=6)
    with pytest.raises(ValueError):
        Config(symbol="A", fix_c=1500.0, decimal_precision=-1)
    assert Config(symbol="A", fix_c=1500.0, decimal_precision=0).decimal_precision == 0


# ---- Step 4–5: step, signal -------------------------------------------------
def test_dna_step_genesis_and_increment():
    assert dna_step_for(None) == 0
    a = Anchor(version=4, dna_step=4, p0=10.0, prev_price=12.0, prev_actual=0.0)
    assert dna_step_for(a) == 5


def test_dna_signal_exhausted_fail_closed():
    assert dna_signal_for("bypass:3", 2) == 1
    with pytest.raises(DNAExhausted):
        dna_signal_for("bypass:3", 3)


# ============================================================================
# Step 14–17: recurrence gated_theoretical_v2 (golden จาก gated demo CSV)
# ============================================================================

def test_recurrence_genesis_all_zero():
    r = compute_recurrence(CFG, price=10.0, anchor=None, signal=1)
    assert (r.R, r.dA, r.A, r.E) == (0.0, 0.0, 0.0, 0.0)
    assert r.acted_price_next == 10.0


def test_recurrence_act_row_price_formula_vs_last_acted():
    a = Anchor(version=1, dna_step=0, p0=10.0, prev_price=12.0, prev_actual=250.0)
    r = compute_recurrence(CFG, price=11.0, anchor=a, signal=1)
    assert r.R == pytest.approx(1500.0 * math.log(11.0 / 10.0))
    assert r.dA == pytest.approx(1500.0 * (11.0 / 12.0 - 1.0))
    assert r.A == pytest.approx(250.0 + r.dA)
    assert r.E == pytest.approx(r.A - r.R)
    assert r.acted_price_next == 11.0          # act -> P_acted เลื่อนเป็น Pₙ


def test_recurrence_pass_row_freezes_ledger_and_smooths_E():
    a = Anchor(version=1, dna_step=0, p0=10.0, prev_price=12.0, prev_actual=250.0)
    r = compute_recurrence(CFG, price=11.0, anchor=a, signal=0)
    assert r.dA == 0.0 and r.A == 250.0        # แช่แข็ง — ไม่ขยับตามราคา
    assert r.acted_price_next == 12.0          # pass -> P_acted ไม่เลื่อน
    # smooth Eₙ = A − FIX_C·ln(P_acted/P₀) ค้างค่า act ล่าสุด — ไม่ใช่ A − Rₙ
    assert r.E == pytest.approx(250.0 - 1500.0 * math.log(12.0 / 10.0))
    assert r.E != pytest.approx(r.A - r.R)


# golden จาก 094f5159 gated demo CSV (fix=1500, P₀=100)
def test_recurrence_csv_golden_pass_then_react():
    # รอบ 2 (act): ราคา 89.17306809, act ล่าสุด 96.81133514, A₁=−47.82997289
    a2 = Anchor(version=2, dna_step=1, p0=100.0,
                prev_price=96.81133514, prev_actual=-47.82997289)
    r2 = compute_recurrence(CFG, price=89.17306809, anchor=a2, signal=1)
    assert r2.dA == pytest.approx(-118.3477179, abs=1e-4)
    assert r2.A == pytest.approx(-166.1776908, abs=1e-4)
    assert r2.E == pytest.approx(5.708988114, abs=1e-4)

    # รอบ 3–9 pass (signal=0): A ค้าง, P_acted แช่ที่ 89.17306809, smooth E ค้าง
    a3 = Anchor(version=3, dna_step=2, p0=100.0,
                prev_price=89.17306809, prev_actual=-166.1776908)
    r3 = compute_recurrence(CFG, price=91.27812248, anchor=a3, signal=0)
    assert r3.dA == 0.0 and r3.A == pytest.approx(-166.1776908)
    assert r3.E == pytest.approx(5.708988114, abs=1e-4)     # smooth Eₙ ค้าง
    assert r3.acted_price_next == pytest.approx(89.17306809)

    # รอบ 10 act ใหม่: ΔA ก้อนเดียวเทียบราคาแช่แข็ง 89.17306809
    a10 = Anchor(version=10, dna_step=9, p0=100.0,
                 prev_price=89.17306809, prev_actual=-166.1776908)
    r10 = compute_recurrence(CFG, price=110.7252073, anchor=a10, signal=1)
    assert r10.dA == pytest.approx(362.533325, abs=1e-4)
    assert r10.A == pytest.approx(196.3556342, abs=1e-4)
    assert r10.E == pytest.approx(43.53362969, abs=1e-4)


def test_recurrence_csv_golden_second_gap_segment():
    # รอบ 23: act หลัง pass 19–22 (P_acted แช่ที่ 99.4981901, A=48.49871571)
    a = Anchor(version=23, dna_step=22, p0=100.0,
               prev_price=99.4981901, prev_actual=48.49871571)
    r = compute_recurrence(CFG, price=96.78323387, anchor=a, signal=1)
    assert r.dA == pytest.approx(-40.92973287, abs=1e-4)
    assert r.A == pytest.approx(7.568982834, abs=1e-4)
    assert r.E == pytest.approx(56.61359861, abs=1e-4)


def test_recurrence_smooth_E_never_decreases_on_act():
    # ทุก segment ที่ act: E เพิ่ม ≥ 0 เพราะ x − 1 ≥ ln x
    a = Anchor(version=1, dna_step=0, p0=100.0, prev_price=100.0, prev_actual=0.0)
    prev_E = 0.0
    price, acted, A = 100.0, 100.0, 0.0
    for p in (96.8, 89.2, 110.7, 106.9, 79.5, 121.1):
        anc = Anchor(version=1, dna_step=0, p0=100.0, prev_price=acted, prev_actual=A)
        r = compute_recurrence(CFG, price=p, anchor=anc, signal=1)
        assert r.E >= prev_E - 1e-9
        prev_E, acted, A = r.E, p, r.A
    assert prev_E >= 0.0


def test_recurrence_bad_inputs_fail_closed():
    a = Anchor(version=1, dna_step=0, p0=0.0, prev_price=12.0, prev_actual=0.0)
    with pytest.raises(ValueError):
        compute_recurrence(CFG, price=11.0, anchor=a, signal=1)
    good = Anchor(version=1, dna_step=0, p0=10.0, prev_price=12.0, prev_actual=0.0)
    with pytest.raises(ValueError):
        compute_recurrence(CFG, price=11.0, anchor=good, signal=2)


# ---- compute_row: สัญญา 17 คอลัมน์ -----------------------------------------
SNAP = {"captured_at": "2026-07-20T14:30:00Z", "price": 12.0, "holdings": 150.0}


def test_compute_row_17_columns_exact_order():
    row = compute_row(CFG, SNAP, anchor=None)
    assert [k for k in row if k != "_meta"] == COLUMN_ORDER
    assert row["สถานะ"] == READY_SELL and row["DNA step"] == 0
    assert row["Rₙ อ้างอิง (USD)"] == 0.0                     # แถว genesis
    assert row["_meta"]["p0_next"] == 12.0
    assert row["_meta"]["acted_price_next"] == 12.0


def test_compute_row_pass_dna_zero_freezes_acted_price():
    cfg = Config(symbol="APLS", fix_c=1500.0, diff=0.0,
                 dna_code="26021034252903219354832053493")     # slot 1 = 0
    a = Anchor(version=1, dna_step=0, p0=100.0, prev_price=100.0, prev_actual=0.0)
    snap = {"captured_at": "2026-07-20T15:00:00Z", "price": 130.0, "holdings": 15.0}
    row = compute_row(cfg, snap, anchor=a)
    assert row["สถานะ"] == PASS_DNA_ZERO
    assert row["ΔAₙ ต่อสเต็ป (USD)"] == 0.0
    assert row["Aₙ สะสม (USD)"] == 0.0
    assert row["_meta"]["acted_price_next"] == 100.0          # แช่แข็ง
    # smooth E: A − FIX_C·ln(P_acted/P₀) = 0 (P_acted = P₀)
    assert row["Eₙ ส่วนเกินสะสม (USD)"] == pytest.approx(0.0)
    # Rₙ ยังวิ่งตามราคา (Reference คิดทุกแถว)
    assert row["Rₙ อ้างอิง (USD)"] == pytest.approx(1500.0 * math.log(130.0 / 100.0))


def test_validate_row_columns_fail_closed():
    row = compute_row(CFG, SNAP, anchor=None)
    bad = {k: v for k, v in row.items() if k != "ฝั่ง"}
    with pytest.raises(RowValidationError):
        validate_row_columns(bad)


# ---- DNA golden (ตรง skill / ตัว encode) -----------------------------------
def test_dna_stream_golden_champion():
    d = decode_dna("26021034252903219354832053493")
    assert len(d) == 60 and sum(d) == 25
    assert d[:20] == [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 1, 1, 0, 1, 1, 1]
    assert d[0] == 1
    assert decode_dna("26021034252903219354832053493") == d    # deterministic


def test_dna_bypass_forms():
    assert decode_dna("bypass:4") == [1, 1, 1, 1]
    assert decode_dna("[1, 4]") == [1, 1, 1, 1]


def test_dna_bad_specs_fail_closed():
    for bad in ("seed:425:60", "", "abc", "[2, 4]", "bypass:0", None, 42):
        with pytest.raises(DNAError):
            decode_dna(bad)
    with pytest.raises(DNAError):
        decode_dna("26033003425")      # rate 300 = 300% -> DNAError
    with pytest.raises(DNAError):      # a rejected code must never be cached
        decode_dna("abc")


def test_decode_is_cached_but_callers_get_independent_lists():
    first = decode_dna("bypass:4")
    second = decode_dna("bypass:4")
    assert first == second and first is not second
    first[1] = 0                        # mutating one caller must not leak
    assert decode_dna("bypass:4") == [1, 1, 1, 1]


# ---- order payload: quantity ตาม decimal_precision -------------------------
def test_payload_quantity_respects_precision():
    assert build_order_payload(CFG, "BUY", 25.0, "x")[0]["quantity"] == "25"
    assert build_order_payload(CFG, "SELL", 0.25, "x")[0]["quantity"] == "0.25"
    cfg0 = Config(symbol="APLS", fix_c=1500.0, decimal_precision=0)
    assert build_order_payload(cfg0, "BUY", 3.0, "x")[0]["quantity"] == "3"
    assert build_order_payload(CFG, "BUY", 1.23456, "x")[0]["quantity"] == "1.23456"


def test_payload_fixed_fields():
    o = build_order_payload(CFG, "BUY", 1.0, "cid123")[0]
    assert (o["order_type"], o["time_in_force"], o["entrust_type"]) == ("MARKET", "DAY", "QTY")
    assert o["client_order_id"] == "cid123" and o["symbol"] == "APLS"


# ---- _extract_qty: fail closed เมื่อ shape ไม่รู้จัก ------------------------
def test_extract_qty_known_shapes():
    assert _extract_qty([{"symbol": "APLS", "quantity": "4.5"}], "APLS") == 4.5
    assert _extract_qty({"positions": [{"symbol": "APLS", "quantity": 2}]}, "APLS") == 2.0
    assert _extract_qty({"items": [{"symbol": "TSLA", "quantity": 9}]}, "APLS") == 0.0
    assert _extract_qty({"data": []}, "APLS") == 0.0
    assert _extract_qty({"positions": []}, "APLS") == 0.0      # "ไม่มีหุ้น" ที่ถูกต้อง
    assert _extract_qty([], "APLS") == 0.0


def test_extract_qty_unknown_shape_fail_closed():
    # holdings=0 ปลอม -> READY_BUY ซ้ำทั้งก้อน — ต้อง raise ไม่ใช่คืน 0
    with pytest.raises(ValueError):
        _extract_qty({"unexpected": []}, "APLS")
    with pytest.raises(ValueError):
        _extract_qty(None, "APLS")


# ---- submit gate (invariant #9) --------------------------------------------
def _ready_row():
    return compute_row(CFG, SNAP, anchor=None)                 # READY_SELL qty 25


def test_gate_blocks_production_even_with_preview():
    with pytest.raises(SubmitGateError):
        evaluate_submit_gate("Production", _ready_row(), True,
                             order_confirmation_phrase(_ready_row()), committed=True)


def test_gate_blocks_uncommitted_and_bad_phrase_and_preview():
    row = _ready_row()
    phrase = order_confirmation_phrase(row)
    with pytest.raises(SubmitGateError):
        evaluate_submit_gate("Test (UAT)", row, True, phrase, committed=False)
    with pytest.raises(SubmitGateError):
        evaluate_submit_gate("Test (UAT)", row, False, phrase, committed=True)
    with pytest.raises(SubmitGateError):
        evaluate_submit_gate("Test (UAT)", row, True, "CONFIRM WRONG", committed=True)
    evaluate_submit_gate("Test (UAT)", row, True, phrase, committed=True)   # ผ่าน


# ---- summarize_order_result (invariant #10) --------------------------------
def test_place_only_no_status_is_unknown():
    s = summarize_order_result({"client_order_id": "abc"})
    assert s["status"] == "UNKNOWN"
    assert s["realized"] is False


def test_detail_flat_filled():
    s = summarize_order_result({}, {"order_status": "FILLED", "filled_quantity": "1.5"})
    assert s["status"] == "FILLED"
    assert s["realized"] is True
    assert s["filled_quantity"] == "1.5"


def test_detail_partial_filled_with_space_from_sdk_enum():
    # SDK enum จริงคือ "PARTIAL FILLED" (มีช่องว่าง) — normalize แล้วนับ realized
    s = summarize_order_result({}, {"order_status": "PARTIAL FILLED"})
    assert s["status"] == "PARTIAL_FILLED"
    assert s["realized"] is True


def test_a_fill_counts_even_when_the_order_ends_somewhere_else():
    """A DAY order that fills 30 of 70 and is cancelled at the close reports
    CANCELLED — those 30 shares are still real and still ours."""
    s = summarize_order_result({}, {"order_status": "CANCELLED",
                                    "filled_quantity": "30",
                                    "avg_filled_price": "12.5"})
    assert s["status"] == "CANCELLED" and s["realized"] is True
    assert s["filled_quantity"] == "30" and s["filled_price"] == "12.5"


@pytest.mark.parametrize("status", ["CANCELLED", "EXPIRED", "REJECTED"])
def test_an_order_that_never_filled_realizes_nothing(status):
    s = summarize_order_result({}, {"order_status": status, "filled_quantity": "0"})
    assert s["realized"] is False


def test_broker_expiry_leaves_the_dispatch_queue():
    """EXPIRED is terminal at the broker; leaving it actionable starves the
    outbox exactly the way an unresolvable reconcile did."""
    from lego_outbox import TERMINAL as OUTBOX_TERMINAL
    assert TERMINAL_STATUSES <= OUTBOX_TERMINAL
    assert "EXPIRED" in OUTBOX_TERMINAL          # broker's own, not EXPIRED_UNSENT
    assert "PARTIAL_FILLED" not in OUTBOX_TERMINAL


def test_partial_realized_but_not_terminal():
    # partial = ของเข้าพอร์ตแล้วบางส่วน (realized) แต่ order ยังไม่จบ — ต้องตามต่อ
    assert "PARTIAL_FILLED" in REALIZED_STATUSES
    assert "PARTIAL_FILLED" not in TERMINAL_STATUSES
    assert "FILLED" in TERMINAL_STATUSES and "CANCELLED" in TERMINAL_STATUSES


def test_detail_nested_in_items():
    detail = {"items": [{"order_status": "FAILED", "reason": "insufficient buying power"}]}
    s = summarize_order_result({}, detail)
    assert s["status"] == "FAILED"
    assert s["realized"] is False
    assert s["reject_reason"] == "insufficient buying power"


def test_detail_nested_list_response():
    s = summarize_order_result({}, [{"status": "CANCELLED"}])
    assert s["status"] == "CANCELLED"
    assert s["realized"] is False


def test_submitted_not_realized():
    s = summarize_order_result({}, {"order_status": "SUBMITTED"})
    assert s["status"] == "SUBMITTED"
    assert s["realized"] is False
    assert "SUBMITTED" not in TERMINAL_STATUSES


def test_summary_extracts_execution_price_and_fee():
    s = summarize_order_result({}, {
        "order_status": "FILLED", "filled_quantity": "1.5",
        "average_filled_price": "109.37", "commission": "0.35"})
    assert s["filled_price"] == "109.37"
    assert s["filled_fee"] == "0.35"


def test_summary_never_uses_quote_as_execution_price():
    s = summarize_order_result({}, {
        "order_status": "FILLED", "filled_quantity": "1.5",
        "last_price": "111.11", "price": "111.11"})
    assert "filled_price" not in s          # quote ตอนตัดสินใจพิสูจน์เงินจริงไม่ได้


def test_summary_ignores_invalid_execution_values():
    s = summarize_order_result({}, {
        "order_status": "FILLED", "filled_price": "abc", "commission": "-1"})
    assert "filled_price" not in s and "filled_fee" not in s


# ---- _retry_transient ------------------------------------------------------
class _Transient(Exception):
    def __init__(self, http_status=504):
        self.http_status = http_status


def test_retry_succeeds_on_third_attempt(monkeypatch):
    monkeypatch.setattr("webull_io.time.sleep", lambda s: None)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _Transient(504)
        return "ok"

    assert _retry_transient(fn) == "ok"
    assert calls["n"] == 3


def test_retry_exhausted_raises_last(monkeypatch):
    monkeypatch.setattr("webull_io.time.sleep", lambda s: None)

    def fn():
        raise _Transient(504)

    with pytest.raises(_Transient):
        _retry_transient(fn)


def test_non_transient_raises_immediately():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise ValueError("signature mismatch")

    with pytest.raises(ValueError):
        _retry_transient(fn)
    assert calls["n"] == 1


def test_gateway_timeout_error_code_without_http_status(monkeypatch):
    monkeypatch.setattr("webull_io.time.sleep", lambda s: None)

    class _GwTimeout(Exception):
        error_code = "GATEWAY_TIMEOUT"

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 2:
            raise _GwTimeout()
        return "ok"

    assert _retry_transient(fn) == "ok"


# ---- fake RTDB สำหรับทดสอบ Step 18 -----------------------------------------
class _FakeRef:
    def __init__(self, store: dict, path: str):
        self._store, self._path = store, path.strip("/")

    def _node(self, create=False):
        cur = self._store
        parts = self._path.split("/")
        for p in parts[:-1]:
            if p not in cur:
                if not create:
                    return None, parts[-1]
                cur[p] = {}
            cur = cur[p]
        return cur, parts[-1]

    def get(self):
        parent, leaf = self._node()
        if parent is None:
            return None
        v = parent.get(leaf)
        return None if v == {} else v

    def set(self, value):
        parent, leaf = self._node(create=True)
        parent[leaf] = value

    def update(self, fields):
        parent, leaf = self._node(create=True)
        parent.setdefault(leaf, {}).update(fields)

    def delete(self):
        parent, leaf = self._node()
        if parent is not None:
            parent.pop(leaf, None)

    def transaction(self, fn):
        self.set(fn(self.get()))


@pytest.fixture
def fake_db(monkeypatch):
    import lego_state
    store: dict = {}
    monkeypatch.setattr(lego_state.db, "reference",
                        lambda path: _FakeRef(store, path))
    return store


def _round(cfg, t, price, holdings):
    """หนึ่งรอบ scheduler เหมือน main.lego_one_row (ไม่มี network)"""
    from lego_state import commit_final_row, read_anchor
    anchor = read_anchor(cfg)
    snap = {"captured_at": t, "price": price, "holdings": holdings}
    row = compute_row(cfg, snap, anchor)
    res = commit_final_row(cfg, snap, anchor, row)
    return row, res


def _dA(row):
    return row["ΔAₙ ต่อสเต็ป (USD)"]


def _A(row):
    return row["Aₙ สะสม (USD)"]


# ---- Step 18: commit protocol (invariant #7, #8) ---------------------------
def test_commit_genesis_then_idempotent_replay(fake_db):
    from lego_state import commit_final_row, read_anchor
    row = compute_row(CFG, SNAP, anchor=None)

    r1 = commit_final_row(CFG, SNAP, None, row)
    assert r1["committed"] is True and r1["version"] == 1
    rows = fake_db["webull_lego_rows"]
    assert rows[r1["run_id"]]["committed"] is True

    # replay snapshot เดิม -> no-op ไม่สร้างแถวใหม่ (invariant #7)
    r2 = commit_final_row(CFG, SNAP, None, row)
    assert r2["idempotent"] is True and len(rows) == 1

    a = read_anchor(CFG)
    assert a.version == 1 and a.dna_step == 0
    assert a.p0 == 12.0 and a.prev_price == 12.0


def test_commit_stale_anchor_fail_closed(fake_db):
    from lego_state import StaleAnchorError, commit_final_row
    row0 = compute_row(CFG, SNAP, anchor=None)
    commit_final_row(CFG, SNAP, None, row0)

    # anchor เก่า (version 0 ไม่มีจริง) + snapshot ใหม่ -> StaleAnchorError + ไม่มี orphan
    stale = Anchor(version=0, dna_step=0, p0=12.0, prev_price=12.0, prev_actual=0.0)
    snap2 = {**SNAP, "captured_at": "2026-07-20T15:00:00Z", "price": 13.0}
    row2 = compute_row(CFG, snap2, anchor=stale)
    with pytest.raises(StaleAnchorError):
        commit_final_row(CFG, snap2, stale, row2)
    assert len(fake_db["webull_lego_rows"]) == 1               # orphan ถูกลบแล้ว


def test_commit_advances_the_decision_pointer_but_never_the_cashflow(fake_db):
    """A committed READY_* moves DNA time and nothing else.

    P_acted and Aₙ belong to lego_order_worker now, so the second commit leaves
    them exactly where genesis put them until a fill is finalized.
    """
    from lego_state import commit_final_row, read_anchor
    row0 = compute_row(CFG, SNAP, anchor=None)
    commit_final_row(CFG, SNAP, None, row0)
    a1 = read_anchor(CFG)

    snap2 = {**SNAP, "captured_at": "2026-07-20T15:00:00Z", "price": 13.0}
    row2 = compute_row(CFG, snap2, anchor=a1)
    assert row2["สถานะ"] == READY_SELL          # a decision, not an execution
    r = commit_final_row(CFG, snap2, a1, row2)
    assert r["version"] == 2

    a2 = read_anchor(CFG)
    assert a2.dna_step == 1 and a2.p0 == 12.0
    assert a2.prev_price == 12.0                # P_acted แช่แข็งจนกว่าจะมี fill
    assert a2.prev_actual == 0.0


def test_finalized_fill_advances_the_cashflow_pointer(fake_db):
    from lego_one_row import ExecutionFill
    from lego_state import commit_final_row, finalize_execution_fill, read_anchor
    row0 = compute_row(CFG, SNAP, anchor=None)
    commit_final_row(CFG, SNAP, None, row0)
    a1 = read_anchor(CFG)

    snap2 = {**SNAP, "captured_at": "2026-07-20T15:00:00Z", "price": 13.0}
    row2 = compute_row(CFG, snap2, anchor=a1)
    r = commit_final_row(CFG, snap2, a1, row2)

    out = finalize_execution_fill(
        CFG, r["run_id"],
        ExecutionFill(filled_price=13.0, filled_quantity=25.0, holdings_after=125.0))
    assert out["applied"] is True
    # ΔA = 1500×(13/12 − 1) เท่ากับสมการเดิม แต่คิดหลัง broker ยืนยัน fill
    assert out["delta_actual"] == pytest.approx(1500.0 * (13.0 / 12.0 - 1.0))

    a2 = read_anchor(CFG)
    assert a2.dna_step == 1 and a2.p0 == 12.0     # decision pointer ไม่ถูกแตะ
    assert a2.version == 2
    assert a2.prev_price == 13.0                  # P_acted = ราคาที่ fill จริง
    assert a2.prev_actual == pytest.approx(1500.0 * (13.0 / 12.0 - 1.0))
    assert a2.prev_holdings == pytest.approx(125.0)
    doc = fake_db["webull_lego_rows"][r["run_id"]]
    assert doc["cashflow_status"] == "FINALIZED"
    assert doc["ΔAₙ ต่อสเต็ป (USD)"] == pytest.approx(out["delta_actual"])


def test_commit_pass_row_freezes_acted_price(fake_db):
    # DNA champion: slot 0 = 1 (act), slot 1 = 0 (pass) -> pointer ต้องแช่แข็ง
    cfg = Config(symbol="APLS", fix_c=1500.0, diff=0.0,
                 dna_code="26021034252903219354832053493")
    from lego_state import read_anchor
    _round(cfg, "2026-07-20T14:30:00Z", 100.0, 15.0)
    r2, _ = _round(cfg, "2026-07-20T15:00:00Z", 130.0, 15.0)
    assert r2["สถานะ"] == PASS_DNA_ZERO
    assert _dA(r2) == 0.0 and _A(r2) == 0.0
    a = read_anchor(cfg)
    assert a.prev_price == 100.0               # แช่แข็งที่ราคา act ล่าสุด (genesis)
    assert a.prev_actual == 0.0
    assert a.dna_step == 1                     # step ยังเดิน +1 (กิน slot)


def test_gated_chain_pass_then_react_one_big_step(fake_db):
    # act(100) -> pass(110) -> pass(95) -> act(120): ΔA ก้อนเดียวเทียบ 100
    cfg = Config(symbol="APLS", fix_c=1500.0, diff=0.0, dna_code="[1, 10]")
    from lego_state import read_anchor
    import lego_one_row as lor
    # บังคับ pass ด้วย monkey DNA: ใช้ dna_code จริงไม่ได้เพราะ bypass = act หมด
    # -> จำลองผ่าน anchor ตรง ๆ (pure) แทน
    _round(cfg, "2026-07-20T14:30:00Z", 100.0, 15.0)           # genesis act
    a1 = read_anchor(cfg)
    # pass 2 แถว (จำลอง signal=0 ผ่าน compute_recurrence โดยตรง — pure)
    r_p1 = lor.compute_recurrence(cfg, 110.0, a1, signal=0)
    a2 = Anchor(version=2, dna_step=2, p0=100.0,
                prev_price=r_p1.acted_price_next, prev_actual=r_p1.A)
    r_p2 = lor.compute_recurrence(cfg, 95.0, a2, signal=0)
    a3 = Anchor(version=3, dna_step=3, p0=100.0,
                prev_price=r_p2.acted_price_next, prev_actual=r_p2.A)
    r_act = lor.compute_recurrence(cfg, 120.0, a3, signal=1)
    assert r_act.dA == pytest.approx(1500.0 * (120.0 / 100.0 - 1.0))   # เทียบ 100 ไม่ใช่ 95
    assert r_act.A == pytest.approx(300.0)
    assert r_act.E == pytest.approx(300.0 - 1500.0 * math.log(1.2))


def test_commit_rejects_malformed_row(fake_db):
    from lego_state import commit_final_row
    row = compute_row(CFG, SNAP, anchor=None)
    bad = {k: v for k, v in row.items() if k != "ฝั่ง"}
    with pytest.raises(RowValidationError):
        commit_final_row(CFG, SNAP, None, bad)


def test_repair_pending_marks_committed(fake_db):
    from lego_state import commit_final_row
    row0 = compute_row(CFG, SNAP, anchor=None)
    r1 = commit_final_row(CFG, SNAP, None, row0)
    # จำลอง crash ระหว่างขั้น 2)-3): state advance แล้วแต่ row ค้าง committed=False
    fake_db["webull_lego_rows"][r1["run_id"]]["committed"] = False

    from lego_state import read_anchor
    a1 = read_anchor(CFG)
    snap2 = {**SNAP, "captured_at": "2026-07-20T15:00:00Z", "price": 13.0}
    commit_final_row(CFG, snap2, a1, compute_row(CFG, snap2, anchor=a1))
    assert fake_db["webull_lego_rows"][r1["run_id"]]["committed"] is True


def test_retry_replay_idempotent_no_double_count(fake_db):
    from lego_state import chain_key, commit_final_row, read_anchor
    _round(CFG, "2026-07-20T14:30:00Z", 100.0, 15.0)
    anchor = read_anchor(CFG)
    snap = {"captured_at": "2026-07-20T15:00:00Z", "price": 110.0, "holdings": 15.0}
    row = compute_row(CFG, snap, anchor)
    first = commit_final_row(CFG, snap, anchor, row)
    retry = commit_final_row(CFG, snap, anchor, row)
    assert first["committed"] is True and retry.get("idempotent") is True
    state = fake_db["webull_lego_state"][chain_key(CFG)]
    # ไม่มี fill -> cashflow ไม่ขยับ (ทั้งครั้งแรกและ replay), version ไม่เดินซ้ำ
    assert state["prev_actual"] == 0.0
    assert state["prev_price"] == pytest.approx(100.0)
    assert state["version"] == first["version"]


def test_pending_audits_includes_partial_and_placing(fake_db):
    from lego_state import pending_audits, write_order_audit
    write_order_audit("e1", {"status": "PARTIAL_FILLED", "realized": True})
    write_order_audit("e2", {"status": "PLACING", "realized": False})
    write_order_audit("e3", {"status": "FILLED", "realized": True})
    write_order_audit("e4", {"status": "NOT_PLACED", "realized": False})
    pend = pending_audits(TERMINAL_STATUSES | {"NOT_PLACED"})
    assert set(pend) == {"e1", "e2"}     # partial ต้องถูกตามต่อ, terminal/local-final ไม่เอา


def test_write_order_audit_redacts_secrets(fake_db):
    from lego_state import write_order_audit
    write_order_audit("e9", {"status": "PLACING", "app_secret": "S", "access_token": "T"})
    doc = fake_db["webull_lego_order_audit"]["e9"]
    assert "app_secret" not in doc and "access_token" not in doc


def test_semantics_migration_old_state_resets_baseline(fake_db):
    # state เก่า (legacy price-formula / cycle_realized_v1): ความหมาย Aₙ ต่างกัน
    # — ห้ามลากมาต่อ -> baseline Aₙ รีเซ็ต 0, chain เดินต่อ ไม่ restart DNA
    from lego_state import CASHFLOW_SEMANTICS, chain_key, read_anchor
    fake_db["webull_lego_state"] = {chain_key(CFG): {
        "version": 7, "dna_step": 6, "p0": 100.0, "prev_price": 104.0,
        "prev_actual": 999.99, "last_run_id": "legacy", "slot": None,
        "cashflow_semantics": "cycle_realized_v1",
    }}
    a = read_anchor(CFG)
    assert a.prev_actual == 0.0                            # baseline เริ่มใหม่
    assert a.version == 7 and a.dna_step == 6              # chain เดินต่อ
    assert a.prev_price == 104.0                           # ใช้เป็น P_acted ตั้งต้น
    r8, _ = _round(CFG, "2026-07-20T15:00:00Z", 104.0, 15.0)
    assert r8["DNA step"] == 7 and _A(r8) == pytest.approx(0.0)
    state = fake_db["webull_lego_state"][chain_key(CFG)]
    assert state["cashflow_semantics"] == CASHFLOW_SEMANTICS


def test_read_anchor_prev_holdings_none_safe(monkeypatch):
    import lego_state

    from lego_state import CASHFLOW_SEMANTICS

    state = {"version": 3, "dna_step": 2, "p0": 333.74,
             "prev_price": 326.51, "prev_actual": -43.56,
             "cashflow_semantics": CASHFLOW_SEMANTICS}

    class _Ref:
        def get(self):
            return state

    monkeypatch.setattr(lego_state.db, "reference", lambda path: _Ref())
    cfg = Config(symbol="AAPL", fix_c=2000.0, diff=10.0)

    a = lego_state.read_anchor(cfg)
    assert a.prev_holdings is None       # state เก่าไม่มี field -> None
    assert a.prev_actual == pytest.approx(-43.56)   # semantics ตรง -> ไม่รีเซ็ต

    state["prev_holdings"] = 4.61492
    a = lego_state.read_anchor(cfg)
    assert a.prev_holdings == pytest.approx(4.61492)


def test_row_doc_tagged_with_semantics(fake_db):
    _round(CFG, "2026-07-20T14:30:00Z", 100.0, 15.0)
    from lego_state import CASHFLOW_SEMANTICS
    doc = next(iter(fake_db["webull_lego_rows"].values()))
    assert doc["semantics"] == CASHFLOW_SEMANTICS == "execution_confirmed_v1"


# --- vanished-position predicate and gate-array fingerprint ------------------

def test_holdings_continuity_only_refuses_a_vanished_position():
    from lego_one_row import Anchor, HoldingsAnomaly, check_holdings_continuity

    def anchor(prev_holdings):
        return Anchor(version=1, dna_step=1, p0=100.0, prev_price=100.0,
                      prev_actual=0.0, prev_holdings=prev_holdings)

    check_holdings_continuity(None, 0.0)              # genesis: no reference yet
    check_holdings_continuity(anchor(None), 0.0)      # state written before the field
    check_holdings_continuity(anchor(0.0), 0.0)       # flat stays flat
    check_holdings_continuity(anchor(15.0), 4.0)      # a sell is ordinary
    check_holdings_continuity(anchor(15.0), 15.0)
    with pytest.raises(HoldingsAnomaly):
        check_holdings_continuity(anchor(15.0), 0.0)


def test_dna_fingerprint_tracks_the_array_not_the_code():
    from dna_engine import dna_fingerprint, decode_dna

    # Same array from two different spellings of bypass -> same fingerprint.
    assert decode_dna("bypass:50") == decode_dna("[1, 50]")
    assert dna_fingerprint("bypass:50") == dna_fingerprint("[1, 50]")
    # Different arrays -> different fingerprints.
    assert dna_fingerprint("bypass:50") != dna_fingerprint("bypass:51")
    assert dna_fingerprint("26021034252903219354832053493") != dna_fingerprint("bypass:26")
    assert len(dna_fingerprint("bypass:50")) == 16

