"""The decision/execution split: who is allowed to move ΔAₙ, Aₙ and Eₙ.

lego_one_row decides and commits. It walks the DNA, prices the gap, and writes
the row — but a READY_BUY/READY_SELL is an *intent*, so the three cashflow
columns leave that row carried forward, exactly as a PASS row leaves them.

lego_order_worker executes and finalizes. Only after the broker confirms a fill
(cumulative filled quantity > 0) and the account position is read back changed
does it book ΔAₙ/Aₙ/Eₙ against the filled price — once, atomically, no matter how
many times it is retried or how many workers race for it.

Each test below names the case from the specification it pins.
"""
from __future__ import annotations

import math
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

import main
import webull_io
from conftest import FAKE_DB, FakeReference
from lego_one_row import (COLUMN_ORDER, ACTUAL_COLUMN, DELTA_COLUMN,
                          EXCESS_COLUMN, REFERENCE_COLUMN, ExecutionFill,
                          compute_row)
from lego_outbox import OUTBOX_PATH, list_actionable
from lego_state import (BROKER_CASHFLOW_PATH, CASHFLOW_FINALIZED, CASHFLOW_NO_ACTION,
                        CASHFLOW_PENDING, EXECUTION_STATE_KEY, STATE_PATH,
                        ExecutionFinalizeError, RuntimeIdentityMismatch,
                        chain_key, commit_final_row, execution_finalization,
                        finalize_execution_fill, read_anchor)

UTC = timezone.utc
SLOT_0 = datetime(2026, 7, 23, 18, 0, 5, tzinfo=UTC)      # ordinal 0 on a 30m grid
SLOT_1 = datetime(2026, 7, 23, 18, 30, 5, tzinfo=UTC)
SLOT_2 = datetime(2026, 7, 23, 19, 0, 5, tzinfo=UTC)
FIX_C = 3000.0


def _fixed_now(moment: datetime):
    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz else moment
    return _Now


@pytest.fixture(autouse=True)
def env(monkeypatch):
    FAKE_DB.store.clear()
    for key, value in {
        "LEGO_SYMBOL": "AAPL", "LEGO_FIX_C": str(FIX_C), "LEGO_DIFF": "5",
        "LEGO_DNA_CODE": "bypass:100", "LEGO_DECIMAL_PRECISION": "2",
        "LEGO_SLOT_SECONDS": "1800", "LEGO_DNA_ORIGIN_UTC": "2026-07-23T18:00:00Z",
        "LEGO_DNA_CLOCK_MODE": "market", "FIREBASE_DB_URL": "https://x.firebaseio.com",
        "AUTO_SUBMIT": "true", "WEBULL_ENV": "UAT",
    }.items():
        monkeypatch.setenv(key, value)
    for key in ("LEGO_INLINE_ORDER_WORKER", "LEGO_DNA_LOW_WATERMARK",
                "LEGO_MARKET_HOLIDAYS", "LEGO_FILL_CONFIRM_MAX_ATTEMPTS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(main, "build_clients", lambda: (object(), object()))
    monkeypatch.setattr(main, "token_health", lambda: {"ok": True, "reasons": []})
    monkeypatch.setattr(main, "ORDER_POLL_DELAY_S", 0.0)   # no real waiting here


def _run(monkeypatch, moment: datetime, price: float, holdings: float):
    """One lego_one_row invocation — decision side only, no broker."""
    monkeypatch.setattr(main, "datetime", _fixed_now(moment))
    monkeypatch.setattr(main, "fetch_snapshot", lambda t, d, cfg: {
        "captured_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "quote_time": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "price": price, "holdings": holdings,
    })
    return main.lego_one_row(object())


def _stub_broker(monkeypatch, *, detail, holdings_after, place=None, preview=True,
                 default_fee=True):
    """The execution side: what the broker says, and what the position reads."""
    monkeypatch.setattr(main, "preview_market_order", lambda tc, o: preview)
    monkeypatch.setattr(main, "fetch_open_orders", lambda tc, s: [])
    monkeypatch.setattr(main, "place_market_order",
                        place or (lambda tc, o: {"order_status": "SUBMITTED"}))
    detail_fn = detail if callable(detail) else (lambda tc, r: detail)

    def broker_detail(tc, run_id):
        payload = dict(detail_fn(tc, run_id))
        if (default_fee and payload.get("filled_quantity") is not None
                and "filled_fee" not in payload):
            payload["filled_fee"] = 0.0  # explicitly known zero in these cases
        return payload

    monkeypatch.setattr(main, "fetch_order_detail", broker_detail)
    monkeypatch.setattr(main, "fetch_holdings",
                        holdings_after if callable(holdings_after)
                        else (lambda tc, cfg: float(holdings_after)))


def _cfg():
    return main.load_config()


def _state():
    return FAKE_DB.reference(f"{STATE_PATH}/{chain_key(_cfg())}").get()


def _cashflow():
    return _state()[EXECUTION_STATE_KEY]


def _row(run_id: str):
    return FAKE_DB.reference(f"webull_lego_rows/{run_id}").get()


def _intent(run_id: str):
    return FAKE_DB.reference(f"{OUTBOX_PATH}/{chain_key(_cfg())}/{run_id}").get()


def _work(limit: int = 3):
    return main._run_order_worker(_cfg(), limit=limit)["results"]


# --- Case 1: READY but not filled must not move Aₙ ---------------------------

def test_ready_row_commits_with_the_cashflow_carried_forward(monkeypatch):
    body, code = _run(monkeypatch, SLOT_0, 320.0, holdings=0.0)
    assert code == 200 and body["status"] == "READY_BUY"

    row = _row(body["run_id"])
    assert row["cashflow_status"] == CASHFLOW_PENDING
    assert row[DELTA_COLUMN] == 0.0
    assert row[ACTUAL_COLUMN] == 0.0
    # Rₙ is live on every row and did not move to the worker.
    assert row[REFERENCE_COLUMN] == 0.0                    # genesis: Pₙ = P₀
    assert _cashflow()["actual_cumulative"] == 0.0


def test_a_second_ready_row_still_does_not_move_the_cashflow(monkeypatch):
    """Two READY_* slots in a row: DNA time advances twice, Aₙ not at all."""
    _run(monkeypatch, SLOT_0, 320.0, holdings=0.0)
    body, _ = _run(monkeypatch, SLOT_1, 340.0, holdings=0.0)

    row = _row(body["run_id"])
    assert row["สถานะ"] == "READY_BUY"
    assert row[DELTA_COLUMN] == 0.0 and row[ACTUAL_COLUMN] == 0.0
    assert row[REFERENCE_COLUMN] == pytest.approx(FIX_C * math.log(340.0 / 320.0))
    anchor = read_anchor(_cfg())
    assert anchor.dna_step == 1                            # decision side moved
    assert anchor.prev_price == 320.0                      # P_acted did not
    assert anchor.prev_actual == 0.0


def test_pending_dispatch_and_submitted_are_not_acts(monkeypatch):
    """Case: SUBMITTED / PENDING_DISPATCH must never add to Aₙ."""
    body, _ = _run(monkeypatch, SLOT_0, 320.0, holdings=0.0)
    assert _intent(body["run_id"])["status"] == "PENDING_DISPATCH"
    assert _row(body["run_id"])[DELTA_COLUMN] == 0.0

    _stub_broker(monkeypatch, detail={"order_status": "SUBMITTED"}, holdings_after=0.0)
    result = _work()[0]
    assert result["status"] == "SUBMITTED"
    assert "cashflow_finalized" not in result
    assert _row(body["run_id"])[DELTA_COLUMN] == 0.0
    assert _row(body["run_id"])["cashflow_status"] == CASHFLOW_PENDING
    assert execution_finalization(_cfg(), body["run_id"]) is None


# --- Case 2: a confirmed fill finalizes, exactly once ------------------------

def test_filled_buy_finalizes_once_against_the_filled_price(monkeypatch):
    first, _ = _run(monkeypatch, SLOT_0, 320.0, holdings=0.0)      # genesis, P₀ = 320
    second, _ = _run(monkeypatch, SLOT_1, 330.0, holdings=8.0)
    run_id = second["run_id"]
    quantity = _row(run_id)["จำนวนสั่ง (หุ้น)"]
    # The broker fills at 331.25, not at the 330.0 the decision was made on.
    _stub_broker(monkeypatch, holdings_after=8.0 + quantity, detail={
        "order_status": "FILLED", "filled_quantity": quantity,
        "avg_filled_price": 331.25,
    })

    result = [r for r in _work() if r["run_id"] == run_id][0]
    assert result["status"] == "FILLED" and result["cashflow_finalized"] is True

    expected_dA = FIX_C * (331.25 / 320.0 - 1.0)
    row = _row(run_id)
    assert row["cashflow_status"] == CASHFLOW_FINALIZED
    assert row[DELTA_COLUMN] == pytest.approx(expected_dA)
    assert row[ACTUAL_COLUMN] == pytest.approx(expected_dA)
    assert row[EXCESS_COLUMN] == pytest.approx(expected_dA - row[REFERENCE_COLUMN])
    assert row["execution_price"] == 331.25
    # Rₙ still comes from the decision price — only ΔAₙ/Aₙ/Eₙ moved sides.
    assert row[REFERENCE_COLUMN] == pytest.approx(FIX_C * math.log(330.0 / 320.0))
    assert _row(first["run_id"])[DELTA_COLUMN] == 0.0     # untouched genesis row
    witness = _cashflow()["finalized_runs"][run_id]
    assert witness["previous_action_price"] == 320.0
    assert witness["previous_actual_cumulative"] == 0.0
    assert witness["delta_actual"] == pytest.approx(
        FIX_C * (witness["filled_price"] / witness["previous_action_price"] - 1.0))
    assert witness["actual_cumulative"] == pytest.approx(
        witness["previous_actual_cumulative"] + witness["delta_actual"])
    assert witness["excess"] == pytest.approx(
        witness["actual_cumulative"] - witness["reference"])

    anchor = read_anchor(_cfg())
    assert anchor.prev_price == 331.25                     # P_acted = fill price
    assert anchor.prev_actual == pytest.approx(expected_dA)


def test_filled_sell_finalizes_the_same_way(monkeypatch):
    _run(monkeypatch, SLOT_0, 320.0, holdings=9.375)                # PASS: gap = 0
    body, _ = _run(monkeypatch, SLOT_1, 340.0, holdings=9.375)
    assert body["status"] == "READY_SELL"
    run_id = body["run_id"]
    quantity = _row(run_id)["จำนวนสั่ง (หุ้น)"]
    _stub_broker(monkeypatch, holdings_after=9.375 - quantity, detail={
        "order_status": "FILLED", "filled_quantity": quantity,
        "avg_filled_price": 339.5,
    })

    assert [r for r in _work() if r["run_id"] == run_id][0]["cashflow_finalized"]
    assert _row(run_id)[DELTA_COLUMN] == pytest.approx(FIX_C * (339.5 / 320.0 - 1.0))


def test_worker_retry_never_recomputes_the_same_fill(monkeypatch):
    """Case: a worker retry must not book the cashflow twice."""
    _run(monkeypatch, SLOT_0, 320.0, holdings=0.0)
    body, _ = _run(monkeypatch, SLOT_1, 330.0, holdings=8.0)
    run_id = body["run_id"]
    quantity = _row(run_id)["จำนวนสั่ง (หุ้น)"]
    _stub_broker(monkeypatch, holdings_after=8.0 + quantity, detail={
        "order_status": "FILLED", "filled_quantity": quantity,
        "avg_filled_price": 331.25,
    })
    _work()
    booked = _cashflow()["actual_cumulative"]

    # Same fill offered again — through the module entry point, the way a retry
    # or a duplicated poll would arrive.
    again = finalize_execution_fill(
        _cfg(), run_id,
        ExecutionFill(filled_price=331.25, filled_quantity=quantity,
                      holdings_after=9.375 + quantity))
    assert again["applied"] is False
    assert _cashflow()["actual_cumulative"] == pytest.approx(booked)
    assert _cashflow()["finalized_seq"] == 1


def test_a_finalized_row_is_not_rebooked_after_its_history_is_pruned(monkeypatch):
    """finalized_runs is bounded; the row's own status is the fence behind it."""
    _run(monkeypatch, SLOT_0, 320.0, holdings=0.0)
    body, _ = _run(monkeypatch, SLOT_1, 330.0, holdings=8.0)
    run_id = body["run_id"]
    fill = ExecutionFill(filled_price=331.25, filled_quantity=1.09,
                         holdings_after=9.09)
    booked = finalize_execution_fill(_cfg(), run_id, fill)
    assert booked["applied"] is True

    # Age the run_id out of the bounded history, leaving only the row's record.
    state_ref = FAKE_DB.reference(f"{STATE_PATH}/{chain_key(_cfg())}")
    state = state_ref.get()
    state[EXECUTION_STATE_KEY].pop("finalized_runs")
    state_ref.set(state)

    replay = finalize_execution_fill(_cfg(), run_id, fill)
    assert replay["applied"] is False
    assert replay["delta_actual"] == pytest.approx(booked["delta_actual"])
    assert _cashflow()["actual_cumulative"] == pytest.approx(booked["actual_cumulative"])


def test_an_interrupted_row_write_is_repaired_by_the_next_attempt(monkeypatch):
    """The state transaction committed but the row patch never landed."""
    _run(monkeypatch, SLOT_0, 320.0, holdings=0.0)
    body, _ = _run(monkeypatch, SLOT_1, 330.0, holdings=8.0)
    run_id = body["run_id"]
    fill = ExecutionFill(filled_price=331.25, filled_quantity=1.09,
                         holdings_after=9.09)
    booked = finalize_execution_fill(_cfg(), run_id, fill)

    FAKE_DB.reference(f"webull_lego_rows/{run_id}").update({
        DELTA_COLUMN: 0.0, ACTUAL_COLUMN: 0.0, EXCESS_COLUMN: 0.0,
        "cashflow_status": CASHFLOW_PENDING})

    repair = finalize_execution_fill(_cfg(), run_id, fill)
    assert repair["applied"] is False                   # nothing booked twice
    row = _row(run_id)
    assert row["cashflow_status"] == CASHFLOW_FINALIZED
    assert row[DELTA_COLUMN] == pytest.approx(booked["delta_actual"])
    assert _cashflow()["finalized_seq"] == 1


# --- Case 3: no fill means no cashflow --------------------------------------

@pytest.mark.parametrize("status", ["REJECTED", "CANCELLED"])
def test_terminal_without_a_fill_leaves_delta_zero(monkeypatch, status):
    _run(monkeypatch, SLOT_0, 320.0, holdings=0.0)
    body, _ = _run(monkeypatch, SLOT_1, 330.0, holdings=9.375)
    run_id = body["run_id"]
    _stub_broker(monkeypatch, holdings_after=9.375, detail={
        "order_status": status, "filled_quantity": 0,
    })

    result = [r for r in _work() if r["run_id"] == run_id][0]
    assert result["status"] == status
    assert "cashflow_finalized" not in result

    row = _row(run_id)
    assert row[DELTA_COLUMN] == 0.0
    assert row[ACTUAL_COLUMN] == 0.0
    assert row["cashflow_status"] == CASHFLOW_PENDING       # never finalized
    anchor = read_anchor(_cfg())
    assert anchor.prev_price == 320.0 and anchor.prev_actual == 0.0
    assert execution_finalization(_cfg(), run_id) is None


def test_expired_unsent_intent_leaves_the_ledger_alone(monkeypatch):
    body, _ = _run(monkeypatch, SLOT_0, 320.0, holdings=0.0)
    # Move past the slot's execution window without ever sending.
    monkeypatch.setattr(main, "datetime", _fixed_now(SLOT_2))
    _stub_broker(monkeypatch, detail={}, holdings_after=0.0)
    _work()

    assert _intent(body["run_id"])["status"] == "EXPIRED_UNSENT"
    assert _row(body["run_id"])[DELTA_COLUMN] == 0.0
    assert _cashflow()["actual_cumulative"] == 0.0


def test_a_pass_row_carries_the_ledger_and_creates_no_intent(monkeypatch):
    """Case: PASS gives ΔAₙ = 0 and leaves Aₙ where the last fill put it."""
    _run(monkeypatch, SLOT_0, 320.0, holdings=0.0)
    body, _ = _run(monkeypatch, SLOT_1, 330.0, holdings=8.0)
    quantity = _row(body["run_id"])["จำนวนสั่ง (หุ้น)"]
    _stub_broker(monkeypatch, holdings_after=8.0 + quantity, detail={
        "order_status": "FILLED", "filled_quantity": quantity,
        "avg_filled_price": 331.25,
    })
    _work()
    booked = _cashflow()["actual_cumulative"]
    assert booked != 0.0

    # A slot inside the no-trade band: PASS_THRESHOLD, no intent, frozen ledger.
    passing, _ = _run(monkeypatch, SLOT_2, 330.0, holdings=FIX_C / 330.0)
    assert passing["status"] == "PASS_THRESHOLD"
    row = _row(passing["run_id"])
    assert row["cashflow_status"] == CASHFLOW_NO_ACTION
    assert row[DELTA_COLUMN] == 0.0
    assert row[ACTUAL_COLUMN] == pytest.approx(booked)
    # Eₙ on a pass is the smooth form, against the last executed price.
    assert row[EXCESS_COLUMN] == pytest.approx(
        booked - FIX_C * math.log(331.25 / 320.0))
    assert [i["run_id"] for i in list_actionable(chain_key(_cfg()))] == []


# --- Case 4: partial fills use the cumulative quantity, counted once ---------

def test_partial_fill_waits_then_finalizes_once_on_terminal_cumulative_values(
        monkeypatch):
    _run(monkeypatch, SLOT_0, 320.0, holdings=0.0)
    body, _ = _run(monkeypatch, SLOT_1, 330.0, holdings=8.0)
    run_id = body["run_id"]
    ordered = _row(run_id)["จำนวนสั่ง (หุ้น)"]
    half = round(ordered / 2, 2)

    _stub_broker(monkeypatch, holdings_after=8.0 + half, detail={
        "order_status": "PARTIAL_FILLED", "filled_quantity": half,
        "avg_filled_price": 331.0,
    })
    partial = [r for r in _work() if r["run_id"] == run_id][0]
    assert partial["status"] == "PARTIAL_FILLED"
    assert partial["cashflow_finalized"] is False
    assert partial["cashflow_waiting_for_terminal"] is True
    assert _row(run_id)[DELTA_COLUMN] == 0.0
    assert _row(run_id)["cashflow_status"] == CASHFLOW_PENDING
    assert _cashflow()["finalized_seq"] == 0

    # The rest fills; the poll reports the cumulative quantity again.
    _stub_broker(monkeypatch, holdings_after=8.0 + ordered, detail={
        "order_status": "FILLED", "filled_quantity": ordered,
        "avg_filled_price": 331.2,
    })
    _work()
    expected = FIX_C * (331.2 / 320.0 - 1.0)
    assert _row(run_id)[DELTA_COLUMN] == pytest.approx(expected)
    assert _row(run_id)["execution_price"] == pytest.approx(331.2)
    assert _row(run_id)["execution_quantity"] == pytest.approx(ordered)
    assert _cashflow()["actual_cumulative"] == pytest.approx(expected)
    assert _cashflow()["finalized_seq"] == 1
    assert _intent(run_id)["status"] == "FILLED"


@pytest.mark.parametrize("late_fee", [0.0, 0.25])
def test_terminal_unknown_fee_stays_pending_then_late_fee_is_delta_only(
        monkeypatch, late_fee):
    _run(monkeypatch, SLOT_0, 320.0, holdings=0.0)
    body, _ = _run(monkeypatch, SLOT_1, 330.0, holdings=8.0)
    run_id = body["run_id"]
    quantity = float(_intent(run_id)["quantity"])
    after = 8.0 + quantity
    _stub_broker(monkeypatch, holdings_after=after, default_fee=False, detail={
        "order_status": "FILLED", "filled_quantity": quantity,
        "avg_filled_price": 331.2,
    })
    pending = [r for r in _work() if r["run_id"] == run_id][0]
    assert pending["status"] == main.AWAITING_BROKER_FEE
    assert pending["broker_fee_status"] == "PENDING"
    assert pending["cashflow_finalized"] is True
    first_cash = pending["broker_cash_cumulative"]

    _stub_broker(monkeypatch, holdings_after=after, default_fee=False, detail={
        "order_status": "FILLED", "filled_quantity": quantity,
        "avg_filled_price": 331.2, "filled_fee": late_fee,
    })
    corrected = [r for r in _work() if r["run_id"] == run_id][0]
    assert corrected["status"] == "FILLED"
    assert corrected["broker_fee_status"] == "KNOWN"
    assert float(corrected["broker_cash_cumulative"]) == \
        pytest.approx(float(first_cash) - late_fee)
    event = FAKE_DB.reference(
        f"{BROKER_CASHFLOW_PATH}/{chain_key(_cfg())}/events/{run_id}").get()
    assert event["actual_fees"] == str(late_fee)
    assert list_actionable(chain_key(_cfg())) == []


@pytest.mark.parametrize("terminal_status", ["CANCELLED", "EXPIRED"])
def test_terminal_cancel_or_expiry_books_its_final_cumulative_partial_fill(
        monkeypatch, terminal_status):
    _run(monkeypatch, SLOT_0, 320.0, holdings=0.0)
    body, _ = _run(monkeypatch, SLOT_1, 330.0, holdings=8.0)
    run_id = body["run_id"]
    ordered = float(_intent(run_id)["quantity"])
    cumulative = round(ordered / 2, 2)

    _stub_broker(monkeypatch, holdings_after=8.0 + cumulative, detail={
        "order_status": terminal_status,
        "filled_quantity": cumulative,
        "avg_filled_price": 331.4,
    })
    result = [r for r in _work() if r["run_id"] == run_id][0]

    expected = FIX_C * (331.4 / 320.0 - 1.0)
    assert result["status"] == terminal_status
    assert result["cashflow_finalized"] is True
    assert _row(run_id)[DELTA_COLUMN] == pytest.approx(expected)
    assert _row(run_id)["execution_quantity"] == pytest.approx(cumulative)
    assert _cashflow()["actual_cumulative"] == pytest.approx(expected)
    assert _cashflow()["finalized_seq"] == 1


# --- Case 5: many workers, one finalization ---------------------------------

def test_concurrent_workers_finalize_exactly_once(monkeypatch):
    _run(monkeypatch, SLOT_0, 320.0, holdings=0.0)
    body, _ = _run(monkeypatch, SLOT_1, 330.0, holdings=9.375)
    run_id = body["run_id"]
    quantity = _row(run_id)["จำนวนสั่ง (หุ้น)"]

    lock = threading.RLock()
    original = FakeReference.transaction

    def atomic(self, fn):
        with lock:
            return original(self, fn)

    monkeypatch.setattr(FakeReference, "transaction", atomic)
    barrier = threading.Barrier(2)
    fill = ExecutionFill(filled_price=331.25, filled_quantity=quantity,
                         holdings_after=9.375 + quantity)

    def finalize(_):
        barrier.wait(timeout=5)
        return finalize_execution_fill(_cfg(), run_id, fill)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(finalize, range(2)))

    assert sorted(o["applied"] for o in outcomes) == [False, True]
    expected = FIX_C * (331.25 / 320.0 - 1.0)
    assert _cashflow()["actual_cumulative"] == pytest.approx(expected)
    assert _cashflow()["finalized_seq"] == 1
    assert _row(run_id)[DELTA_COLUMN] == pytest.approx(expected)


# --- holdings come from the broker, never from the ordered quantity ----------

def test_post_execution_holdings_are_read_back_not_assumed(monkeypatch):
    """The broker's own position wins even when it disagrees with the order."""
    _run(monkeypatch, SLOT_0, 320.0, holdings=0.0)
    body, _ = _run(monkeypatch, SLOT_1, 330.0, holdings=8.0)
    run_id = body["run_id"]
    quantity = _row(run_id)["จำนวนสั่ง (หุ้น)"]
    # A number the ordered quantity cannot produce: another leg settled too.
    broker_holdings = 8.0 + quantity + 3.5
    _stub_broker(monkeypatch, holdings_after=broker_holdings, detail={
        "order_status": "FILLED", "filled_quantity": quantity,
        "avg_filled_price": 331.25,
    })
    _work()

    assert _row(run_id)["post_execution_holdings"] == pytest.approx(broker_holdings)
    assert _state()["prev_holdings"] == pytest.approx(broker_holdings)


def test_a_fill_whose_position_never_moves_defers_then_stops_asking(monkeypatch):
    """Fail closed, then bound it: an unconfirmable fill must not book a
    cashflow, and must not hold a dispatch slot forever either."""
    monkeypatch.setenv("LEGO_FILL_CONFIRM_MAX_ATTEMPTS", "2")
    _run(monkeypatch, SLOT_0, 320.0, holdings=0.0)
    body, _ = _run(monkeypatch, SLOT_1, 330.0, holdings=9.375)
    run_id = body["run_id"]
    _stub_broker(monkeypatch, holdings_after=9.375, detail={
        "order_status": "FILLED", "filled_quantity": 1.0,
        "avg_filled_price": 331.25,
    })

    first = [r for r in _work() if r["run_id"] == run_id][0]
    assert first["status"] == main.AWAITING_FILL_CONFIRMATION
    assert first["cashflow_finalized"] is False
    assert _row(run_id)[DELTA_COLUMN] == 0.0
    assert list_actionable(chain_key(_cfg()))                 # still asking

    second = [r for r in _work() if r["run_id"] == run_id][0]
    assert second["status"] == "FILLED" and second["needs_manual_check"] is True
    assert _row(run_id)[DELTA_COLUMN] == 0.0                  # still never booked
    assert list_actionable(chain_key(_cfg())) == []           # out of the queue
    assert FAKE_DB.reference(
        "webull_lego_warnings/cashflow_unconfirmed").get()["count"] == 1


def test_a_sell_that_reads_back_zero_shares_never_finalizes(monkeypatch):
    """The vanished-position guard must survive on the execution side too.

    A rebalance SELL targets value fix_c and always leaves shares behind, so a
    post-execution read of exactly zero is a positions response that lost the
    symbol. Booking it would move P_acted and write prev_holdings = 0, disarming
    the guard for every slot after it.
    """
    monkeypatch.setenv("LEGO_FILL_CONFIRM_MAX_ATTEMPTS", "1")
    _run(monkeypatch, SLOT_0, 320.0, holdings=9.375)                # PASS: gap = 0
    body, _ = _run(monkeypatch, SLOT_1, 340.0, holdings=9.375)
    assert body["status"] == "READY_SELL"
    _stub_broker(monkeypatch, holdings_after=0.0, detail={
        "order_status": "FILLED", "filled_quantity": 0.55,
        "avg_filled_price": 339.5,
    })

    result = [r for r in _work() if r["run_id"] == body["run_id"]][0]
    assert result["cashflow_finalized"] is False
    assert result["needs_manual_check"] is True
    assert _row(body["run_id"])[DELTA_COLUMN] == 0.0
    assert _cashflow()["actual_cumulative"] == 0.0
    assert _state()["prev_holdings"] == 9.375          # guard still armed


def test_an_unreadable_position_defers_instead_of_ending_the_intent(monkeypatch):
    """Failing to read the position says nothing about the fill, so it must not
    be reported as a ledger that refused the arithmetic."""
    _run(monkeypatch, SLOT_0, 320.0, holdings=0.0)
    body, _ = _run(monkeypatch, SLOT_1, 330.0, holdings=8.0)

    def unreadable(_tc, _cfg):
        raise ValueError("positions response shape ไม่รู้จัก — fail closed")

    _stub_broker(monkeypatch, holdings_after=unreadable, detail={
        "order_status": "FILLED", "filled_quantity": 1.09,
        "avg_filled_price": 331.25,
    })

    result = [r for r in _work() if r["run_id"] == body["run_id"]][0]
    assert result["status"] == main.AWAITING_FILL_CONFIRMATION
    assert "needs_manual_check" not in result          # still worth another ask
    assert _row(body["run_id"])[DELTA_COLUMN] == 0.0


def test_a_buy_whose_position_fell_is_not_a_confirmation(monkeypatch):
    """Direction matters: a BUY confirmed by a *smaller* position is somebody
    else's trade landing in the same account."""
    assert main._holdings_moved("BUY", 9.0, 10.0, 1e-6) is True
    assert main._holdings_moved("BUY", 9.0, 8.0, 1e-6) is False
    assert main._holdings_moved("SELL", 9.0, 8.0, 1e-6) is True
    assert main._holdings_moved("SELL", 9.0, 10.0, 1e-6) is False
    assert main._holdings_moved("BUY", 9.0, 9.0, 1e-6) is False


# --- the guards the split must not weaken ------------------------------------

def test_finalizing_never_touches_the_decision_pointer(monkeypatch):
    _run(monkeypatch, SLOT_0, 320.0, holdings=0.0)
    body, _ = _run(monkeypatch, SLOT_1, 330.0, holdings=9.375)
    before = {k: v for k, v in _state().items()
              if k in ("version", "dna_step", "p0", "slot_id", "market_ordinal",
                       "calendar_fingerprint", "dna_fingerprint", "last_run_id",
                       "clock_mode", "config_hash")}

    finalize_execution_fill(
        _cfg(), body["run_id"],
        ExecutionFill(filled_price=331.25, filled_quantity=1.0, holdings_after=10.375))

    after = {k: _state()[k] for k in before}
    assert after == before


def test_a_later_commit_preserves_a_fill_finalized_in_between(monkeypatch):
    """The lost-update race: the engine reads its anchor, the worker finalizes,
    and the engine then commits. The finalized cashflow must survive."""
    _run(monkeypatch, SLOT_0, 320.0, holdings=0.0)
    body, _ = _run(monkeypatch, SLOT_1, 330.0, holdings=9.375)
    cfg = _cfg()

    stale_anchor = read_anchor(cfg)                    # read before the fill
    finalize_execution_fill(
        cfg, body["run_id"],
        ExecutionFill(filled_price=331.25, filled_quantity=1.0, holdings_after=10.375))
    booked = _cashflow()["actual_cumulative"]

    snapshot = {"captured_at": "2026-07-23T19:00:05Z", "price": 332.0,
                "holdings": 10.375}
    row = compute_row(cfg, snapshot, stale_anchor)
    commit_final_row(cfg, snapshot, stale_anchor, row, slot_id="2026-07-23:11",
                     market_ordinal=2, clock_mode="market")

    assert _cashflow()["actual_cumulative"] == pytest.approx(booked)
    assert _cashflow()["last_action_price"] == 331.25
    assert _state()["prev_actual"] == pytest.approx(booked)


def test_finalize_refuses_an_uncommitted_row(monkeypatch):
    _run(monkeypatch, SLOT_0, 320.0, holdings=0.0)
    body, _ = _run(monkeypatch, SLOT_1, 330.0, holdings=9.375)
    FAKE_DB.reference(f"webull_lego_rows/{body['run_id']}").update({"committed": False})
    with pytest.raises(ExecutionFinalizeError):
        finalize_execution_fill(
            _cfg(), body["run_id"],
            ExecutionFill(filled_price=331.25, filled_quantity=1.0,
                          holdings_after=10.375))


def test_finalize_refuses_a_foreign_runtime_identity(monkeypatch):
    _run(monkeypatch, SLOT_0, 320.0, holdings=0.0)
    body, _ = _run(monkeypatch, SLOT_1, 330.0, holdings=9.375)
    with pytest.raises(RuntimeIdentityMismatch):
        finalize_execution_fill(
            _cfg(), body["run_id"],
            ExecutionFill(filled_price=331.25, filled_quantity=1.0,
                          holdings_after=10.375),
            runtime_identity="another-account-fingerprint")
    assert _cashflow()["actual_cumulative"] == 0.0


def test_finalize_refuses_a_zero_quantity_fill(monkeypatch):
    """Acted is cumulative_filled_quantity > 0 and nothing else."""
    _run(monkeypatch, SLOT_0, 320.0, holdings=0.0)
    body, _ = _run(monkeypatch, SLOT_1, 330.0, holdings=9.375)
    with pytest.raises(ValueError):
        finalize_execution_fill(
            _cfg(), body["run_id"],
            ExecutionFill(filled_price=331.25, filled_quantity=0.0,
                          holdings_after=9.375))
    assert _cashflow()["actual_cumulative"] == 0.0


def test_the_17_columns_survive_finalization(monkeypatch):
    _run(monkeypatch, SLOT_0, 320.0, holdings=0.0)
    body, _ = _run(monkeypatch, SLOT_1, 330.0, holdings=9.375)
    before = [k for k in _row(body["run_id"]) if k in COLUMN_ORDER]

    finalize_execution_fill(
        _cfg(), body["run_id"],
        ExecutionFill(filled_price=331.25, filled_quantity=1.0, holdings_after=10.375))

    row = _row(body["run_id"])
    assert before == COLUMN_ORDER
    assert [k for k in row if k in COLUMN_ORDER] == COLUMN_ORDER
    assert set(row) - set(COLUMN_ORDER) == {
        "run_id", "chain_key", "version", "committed", "semantics",
        "market_slot_id", "market_ordinal", "clock_mode", "cashflow_status",
            "execution_price", "execution_quantity", "post_execution_holdings",
            "cashflow_finalized_at", "instrument_capability"}


def test_fetch_holdings_reads_the_position_without_market_data(monkeypatch):
    """Confirming a fill must not depend on a market-data subscription.

    fetch_snapshot answers 403 when the OpenAPI quote entitlement is missing —
    a condition with nothing to do with whether the order filled — so the
    post-execution read goes to the position endpoint alone.
    """
    from conftest import fake_data_client, fake_trade_client

    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "account-under-test")
    data = fake_data_client(snapshot=AssertionError("market data must not be called"))
    trade = fake_trade_client(
        positions={"positions": [{"symbol": "AAPL", "quantity": "12.5"}]})

    assert webull_io.fetch_holdings(trade, _cfg()) == 12.5
    assert data.market_data.get_snapshot.calls == []
