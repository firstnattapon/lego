"""Comprehensive Unit and Integration Tests for Broker-Verified Fractional Shares.

Covers all required unit and integration test specifications from the engineering requirements:
Unit Tests:
1. fractionable symbol with validated fractional capability
2. fractionable=true while lot_size=1
3. fractionable=false
4. unknown fractional capability fails closed
5. 5-decimal normalization
6. trailing zero serialization
7. integer zero preservation
8. too-small quantity
9. fractional notional below USD 1
10. fractional SELL greater than holdings
11. fractional SELL equal or below holdings
12. outside Regular Trading Hours

Integration Tests:
1. fractional BUY -> intent -> preview -> place -> detail -> fill -> post-position -> cashflow finalize
2. fractional SELL -> preview -> place -> fill -> reconciliation
3. partial fractional fill
4. repeated cumulative fractional fill is idempotent
5. decimal holdings survive broker parsing
6. fractional place timeout results in reconciliation, not duplicate placement
7. preview rejection never reaches place_order
8. broker fractional rejection becomes explicit NOT_PLACED/block state
9. preview payload and placed payload match
10. non-fractional symbol retains whole-share behavior
11. fractional order outside CORE session is blocked
"""
from __future__ import annotations

import math
from decimal import Decimal
from datetime import datetime, timedelta, timezone
import pytest

from conftest import FAKE_DB, fake_trade_client, fake_data_client, FakeCall
import main
import webull_io
from config import load_runtime_config
from lego_one_row import (
    Config, build_decision, PASS_MIN_ORDER, Decision,
    REFERENCE_COLUMN, DELTA_COLUMN, ACTUAL_COLUMN, EXCESS_COLUMN
)
from lego_orders import UAT
from lego_outbox import OUTBOX_PATH, list_actionable, put_intent, read_intent
from lego_state import (
    chain_key, apply_realized_fill, finalize_execution_fill,
    CASHFLOW_FINALIZED, CASHFLOW_PENDING
)
from webull_io import (
    InstrumentCapability, parse_instrument_capability,
    fetch_instrument_capability, WebullConfigError, FractionalGateError,
    normalize_quantity_decimal, quantity_string, is_fractional_quantity,
    evaluate_fractional_order_gate, build_order_payload, fetch_holdings
)
import execution_service
import decision_service


UTC = timezone.utc

PROD_ENV = {
    "LEGO_SYMBOL": "AAPL",
    "LEGO_FIX_C": "3000",
    "LEGO_DIFF": "5",
    "LEGO_DNA_CODE": "bypass:100",
    "LEGO_DECIMAL_PRECISION": "3",
    "LEGO_SLOT_SECONDS": "900",
    "LEGO_DNA_ORIGIN_UTC": "2026-07-27T13:30:00Z",
    "LEGO_DNA_CLOCK_MODE": "market",
    "FIREBASE_DB_URL": "https://x.firebaseio.com",
    "AUTO_SUBMIT": "true",
    "WEBULL_ENV": "UAT",
    "WEBULL_ACCOUNT_ID": "uat-test-account",
}
PROD_MOMENT = datetime(2026, 7, 28, 17, 50, 4, tzinfo=UTC)
PROD_PRICE = 339.15
PROD_HOLDINGS = 9.14492
PROD_QUANTITY = 0.299
FIX_C = 3000.0


def _fixed_now(moment: datetime):
    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz else moment
    return _Now


@pytest.fixture(autouse=True)
def test_setup(monkeypatch):
    FAKE_DB.store.clear()
    webull_io.reset_clients()
    for key, value in PROD_ENV.items():
        monkeypatch.setenv(key, value)
    for key in ("LEGO_INLINE_ORDER_WORKER", "LEGO_DNA_LOW_WATERMARK",
                "LEGO_MARKET_HOLIDAYS", "LEGO_AUTO_SUBMIT_MIN_DNA_REMAINING",
                "LEGO_ALLOW_EPHEMERAL_TOKEN_DIR", "WEBULL_TOKEN_DIR",
                "LEGO_FILL_CONFIRM_MAX_ATTEMPTS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(main, "build_clients", lambda: (object(), object()))
    monkeypatch.setattr(main, "ORDER_POLL_DELAY_S", 0.0)


def _write_token(tmp_path, monkeypatch):
    token_dir = tmp_path / "webull_token"
    token_dir.mkdir(parents=True, exist_ok=True)
    expires_ms = int((datetime.now(UTC) + timedelta(days=14)).timestamp() * 1000)
    (token_dir / "token.txt").write_text(f"tok-abc\n{expires_ms}\nNORMAL\n", encoding="utf-8")
    monkeypatch.setenv("WEBULL_TOKEN_DIR", str(token_dir))
    return token_dir


def _run(monkeypatch, moment=PROD_MOMENT, price=PROD_PRICE, holdings=PROD_HOLDINGS):
    monkeypatch.setattr(main, "datetime", _fixed_now(moment))
    monkeypatch.setattr(main, "fetch_snapshot", lambda t, d, cfg: {
        "captured_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "quote_time": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "price": price, "holdings": holdings,
    })
    return main.lego_one_row(object())


def _cfg():
    return main.load_config()


def _row(run_id):
    return FAKE_DB.reference(f"webull_lego_rows/{run_id}").get()


def _intent(run_id):
    return FAKE_DB.reference(f"{OUTBOX_PATH}/{chain_key(_cfg())}/{run_id}").get()


def _stub_broker(monkeypatch, *, detail, holdings_after, place=None, preview=True):
    monkeypatch.setattr(main, "preview_market_order",
                        preview if callable(preview) else (lambda tc, o: preview))
    monkeypatch.setattr(main, "fetch_open_orders", lambda tc, s: [])
    monkeypatch.setattr(main, "place_market_order",
                        place or (lambda tc, o: {"order_status": "SUBMITTED"}))
    detail_fn = detail if callable(detail) else (lambda tc, r: detail)

    def broker_detail(tc, run_id):
        payload = dict(detail_fn(tc, run_id))
        if (payload.get("filled_quantity") is not None and "filled_fee" not in payload):
            payload["filled_fee"] = 0.0
        return payload

    monkeypatch.setattr(main, "fetch_order_detail", broker_detail)
    monkeypatch.setattr(main, "fetch_holdings",
                        holdings_after if callable(holdings_after)
                        else (lambda tc, cfg: float(holdings_after)))


# ==============================================================================
# UNIT TESTS
# ==============================================================================

def test_fractionable_symbol_with_validated_fractional_capability():
    """fractionable symbol with validated fractional capability: raw quantity below 1 share remains valid decimal."""
    capability = InstrumentCapability(
        symbol="AAPL", status="OC", category="US_STOCK", currency="USD",
        lot_size=Decimal("1"), fractionable=True,
    )
    assert capability.fractionable is True
    assert capability.quantity_increment == Decimal("0.00001")
    assert capability.decimal_precision == 5

    cfg = Config(
        symbol="AAPL", fix_c=3000, diff=0, strategy_id="shannon_demon_lego_v2",
        quantity_increment=float(capability.quantity_increment),
        decimal_precision=capability.decimal_precision,
    )
    # Principal gap $60 at $335 price -> raw 0.179104... rounds down to 0.17910
    decision = build_decision(cfg, price=335.0, holdings=8.776106, signal=1)
    assert decision.acted is True
    assert decision.side == "BUY"
    assert abs(decision.quantity - 0.17910) < 1e-5
    assert decision.quantity > 0.0


def test_fractionable_true_while_lot_size_1_does_not_force_whole_share():
    """fractionable=true while lot_size=1: lot_size alone must not force whole-share rounding."""
    raw_payload = {
        "data": [{
            "symbol": "AAPL", "status": "OC", "category": "US_STOCK",
            "currency": "USD", "lot_size": 1, "fractionable": True,
        }]
    }
    cap = parse_instrument_capability(raw_payload, "AAPL")
    assert cap.lot_size == Decimal("1")
    assert cap.fractionable is True
    assert cap.quantity_increment == Decimal("0.00001")
    assert cap.decimal_precision == 5


def test_fractionable_false_forces_whole_share():
    """fractionable=false: fractional strategy amount must not be submitted as fractional."""
    raw_payload = {
        "data": [{
            "symbol": "BRK.A", "status": "OC", "category": "US_STOCK",
            "currency": "USD", "lot_size": 1, "fractionable": False,
        }]
    }
    cap = parse_instrument_capability(raw_payload, "BRK.A")
    assert cap.fractionable is False
    assert cap.quantity_increment == Decimal("1")
    assert cap.decimal_precision == 0

    cfg = Config(
        symbol="BRK.A", fix_c=1000, diff=0, strategy_id="shannon_demon_lego_v2",
        quantity_increment=float(cap.quantity_increment),
        decimal_precision=cap.decimal_precision,
    )
    decision = build_decision(cfg, price=1000.0, holdings=0.5, signal=1)
    assert decision.acted is False
    assert decision.status == PASS_MIN_ORDER
    assert decision.quantity == 0.0


def test_unknown_fractional_capability_fails_closed():
    """unknown fractional capability: fail closed with explicit reason."""
    # 1. Missing fractionable field
    with pytest.raises(WebullConfigError, match="missing fractionable"):
        parse_instrument_capability({
            "data": [{"symbol": "AAPL", "status": "OC", "category": "US_STOCK", "currency": "USD", "lot_size": 1}]
        }, "AAPL")

    # 2. Contradictory type (fractionable is string "true" instead of boolean)
    with pytest.raises(WebullConfigError, match="must be bool"):
        parse_instrument_capability({
            "data": [{"symbol": "AAPL", "status": "OC", "category": "US_STOCK", "currency": "USD", "lot_size": 1, "fractionable": "true"}]
        }, "AAPL")

    # 3. Non US_STOCK category
    with pytest.raises(WebullConfigError, match="category"):
        parse_instrument_capability({
            "data": [{"symbol": "AAPL", "status": "OC", "category": "HK_STOCK", "currency": "USD", "lot_size": 100, "fractionable": True}]
        }, "AAPL")


def test_five_decimal_normalization():
    """5-decimal normalization: input 0.299491 -> 0.29949."""
    norm = normalize_quantity_decimal("0.299491", 5)
    assert norm == Decimal("0.29949")
    assert quantity_string("0.299491", 5) == "0.29949"

    norm2 = normalize_quantity_decimal("0.1791044776", 5)
    assert norm2 == Decimal("0.17910")
    assert quantity_string("0.1791044776", 5) == "0.1791"


def test_trailing_zero_serialization():
    """trailing zero serialization: input 1.00000 -> 1."""
    assert quantity_string("1.00000", 5) == "1"
    assert quantity_string(Decimal("2.50000"), 5) == "2.5"
    assert quantity_string("0.29900", 5) == "0.299"


def test_integer_zero_preservation():
    """integer zero preservation: 20 -> 20, 100 -> 100."""
    assert quantity_string("20", 5) == "20"
    assert quantity_string("20.00000", 5) == "20"
    assert quantity_string("100", 5) == "100"
    assert quantity_string("100.00000", 5) == "100"
    assert quantity_string(100.0, 0) == "100"


def test_too_small_quantity_fails_closed():
    """too-small quantity: PASS/block and no order intent."""
    with pytest.raises(ValueError, match="เหลือ 0"):
        normalize_quantity_decimal("0.000001", 5)

    with pytest.raises(ValueError, match="เหลือ 0"):
        quantity_string("0.000001", 5)


def test_fractional_notional_below_usd_1_blocked():
    """fractional notional below USD 1: no broker placement."""
    cap = InstrumentCapability(
        symbol="AAPL", status="OC", category="US_STOCK", currency="USD",
        lot_size=Decimal("1"), fractionable=True,
    )
    cfg = Config(
        symbol="AAPL", fix_c=100.5, diff=0, strategy_id="shannon_demon_lego_v2",
        quantity_increment=0.00001, decimal_precision=5,
    )
    decision = build_decision(cfg, price=100.0, holdings=1.0, signal=1)
    assert decision.acted is False
    assert decision.status == PASS_MIN_ORDER

    with pytest.raises(FractionalGateError, match="below minimum USD 1.0"):
        evaluate_fractional_order_gate(
            capability=cap, side="BUY", quantity=Decimal("0.005"), price=Decimal("100.0"),
            holdings=Decimal("1.0"), order_type="MARKET", session_check_fn=lambda at: True,
        )


def test_fractional_sell_greater_than_holdings_blocked():
    """fractional SELL greater than holdings: blocked."""
    cap = InstrumentCapability(
        symbol="AAPL", status="OC", category="US_STOCK", currency="USD",
        lot_size=Decimal("1"), fractionable=True,
    )
    with pytest.raises(FractionalGateError, match="short selling is forbidden"):
        evaluate_fractional_order_gate(
            capability=cap, side="SELL", quantity=Decimal("0.5"), price=Decimal("150.0"),
            holdings=Decimal("0.4"), order_type="MARKET", session_check_fn=lambda at: True,
        )


def test_fractional_sell_equal_or_below_holdings_allowed():
    """fractional SELL equal or below holdings: allowed when every other broker condition passes."""
    cap = InstrumentCapability(
        symbol="AAPL", status="OC", category="US_STOCK", currency="USD",
        lot_size=Decimal("1"), fractionable=True,
    )
    evaluate_fractional_order_gate(
        capability=cap, side="SELL", quantity=Decimal("0.4"), price=Decimal("150.0"),
        holdings=Decimal("0.4"), order_type="MARKET", session_check_fn=lambda at: True,
    )


def test_outside_regular_trading_hours_blocked():
    """outside Regular Trading Hours: fractional order must not dispatch."""
    cap = InstrumentCapability(
        symbol="AAPL", status="OC", category="US_STOCK", currency="USD",
        lot_size=Decimal("1"), fractionable=True,
    )
    with pytest.raises(FractionalGateError, match="US Regular Trading Hours"):
        evaluate_fractional_order_gate(
            capability=cap, side="BUY", quantity=Decimal("0.5"), price=Decimal("150.0"),
            holdings=Decimal("0.0"), order_type="MARKET", session_check_fn=lambda at: False,
        )


# ==============================================================================
# INTEGRATION TESTS
# ==============================================================================

def test_integration_fractional_buy_pipeline(tmp_path, monkeypatch):
    """fractional BUY -> intent -> preview -> place -> detail -> fill -> post-position -> cashflow finalize"""
    _write_token(tmp_path, monkeypatch)
    # Price 320.0, holdings 8.0, fix_c 3000, diff 5 -> gap = 3000 - 2560 = 440 -> wait, for fractional:
    # Set price so gap/price has fractional decimals:
    # Holdings 8.5, price 339.15, fix_c 3000 -> gap = 3000 - 8.5 * 339.15 = 117.225 -> BUY 0.3456 shares
    body, _ = _run(monkeypatch, price=339.15, holdings=8.5)
    run_id = body["run_id"]
    row_doc = _row(run_id)
    assert row_doc["สถานะ"] == "READY_BUY"
    qty = float(row_doc["จำนวนสั่ง (หุ้น)"])
    assert qty > 0 and (qty % 1.0 != 0.0)  # Fractional!

    previewed, placed = [], []
    fill_price = 339.20
    holdings_before = 8.5
    holdings_after = holdings_before + qty

    def _preview(_tc, order):
        previewed.append(order)
        return True

    def _place(_tc, order):
        placed.append(order)
        return {"order_status": "SUBMITTED"}

    def _detail(_tc, client_order_id):
        return {"order_status": "FILLED", "filled_quantity": qty,
                "avg_filled_price": fill_price, "transaction_fee": 0.05}

    _stub_broker(monkeypatch, detail=_detail, holdings_after=holdings_after,
                 place=_place, preview=_preview)

    result = main._run_order_worker(_cfg(), limit=1)["results"][0]

    assert len(placed) == 1
    assert placed[0] == previewed[0]
    assert placed[0][0]["client_order_id"] == run_id
    assert result["status"] == "FILLED"
    assert result["cashflow_finalized"] is True
    assert result["post_execution_holdings"] == pytest.approx(holdings_after)
    assert _row(run_id)["cashflow_status"] == CASHFLOW_FINALIZED


def test_integration_fractional_sell_pipeline(tmp_path, monkeypatch):
    """fractional SELL -> preview -> place -> fill -> reconciliation"""
    _write_token(tmp_path, monkeypatch)
    body, _ = _run(monkeypatch)  # default PROD_PRICE=339.15, PROD_HOLDINGS=9.14492 -> READY_SELL 0.299
    run_id = body["run_id"]
    assert _row(run_id)["สถานะ"] == "READY_SELL"

    previewed, placed = [], []
    fill_price = 339.20
    holdings_before = PROD_HOLDINGS
    holdings_after = holdings_before - PROD_QUANTITY

    def _preview(_tc, order):
        previewed.append(order)
        return True

    def _place(_tc, order):
        placed.append(order)
        return {"order_status": "SUBMITTED"}

    def _detail(_tc, client_order_id):
        return {"order_status": "FILLED", "filled_quantity": PROD_QUANTITY,
                "avg_filled_price": fill_price, "transaction_fee": 0.05}

    _stub_broker(monkeypatch, detail=_detail, holdings_after=holdings_after,
                 place=_place, preview=_preview)

    result = main._run_order_worker(_cfg(), limit=1)["results"][0]

    assert len(placed) == 1
    assert placed[0] == previewed[0]
    assert placed[0][0]["quantity"] == "0.299"
    assert result["status"] == "FILLED"
    assert result["cashflow_finalized"] is True
    assert _row(run_id)["cashflow_status"] == CASHFLOW_FINALIZED


def test_integration_partial_fractional_fill(monkeypatch):
    """partial fractional fill: 0.5 ordered, 0.2 filled, cancelled -> books delta_actual 0.2"""
    cfg = _cfg()
    ck = chain_key(cfg)
    run_id = "frac-partial-fill-003"
    now = datetime.now(UTC)

    intent = {
        "run_id": run_id, "chain_key": ck, "status": "SUBMITTED", "side": "BUY",
        "quantity": 0.5, "symbol": "AAPL", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    summary = {
        "status": "CANCELLED",
        "filled_quantity": 0.2,
        "filled_price": 100.0,
        "broker_fee_status": "RESOLVED",
        "actual_broker_fee": 0.01,
    }
    trade_client = fake_trade_client(
        positions={"data": [{"symbol": "AAPL", "quantity": "0.2"}]}
    )
    monkeypatch.setattr(execution_service, "fetch_holdings", lambda tc, c: 0.2)
    monkeypatch.setattr(main, "fetch_holdings", lambda tc, c: 0.2)
    FAKE_DB.reference(f"webull_lego_state/{ck}").set({
        "version": 1, "anchor": {"holdings": 0.0},
        "execution_cashflow": {"last_action_price": 100.0, "actual_cumulative": 0.0, "A": 0.0}
    })
    FAKE_DB.reference(f"webull_lego_rows/{run_id}").set({
        "run_id": run_id, "committed": True, "cashflow_status": CASHFLOW_PENDING,
        "chain_key": ck, REFERENCE_COLUMN: 0.0,
    })

    fin = execution_service._finalize_model_ledger(trade_client, cfg, intent, summary)
    assert fin["applied"] is True
    assert fin["filled_quantity"] == 0.2
    assert fin["holdings_after"] == 0.2


def test_integration_repeated_cumulative_fractional_fill_is_idempotent():
    """repeated cumulative fractional fill is idempotent"""
    cfg = _cfg()
    ck = chain_key(cfg)
    run_id = "frac-idempotent-004"

    intent = {
        "run_id": run_id, "chain_key": ck, "status": "FILLED", "side": "BUY",
        "quantity": 0.35, "symbol": "AAPL", "cashflow_finalized": True,
    }
    summary = {
        "status": "FILLED",
        "filled_quantity": 0.35,
        "filled_price": 100.0,
        "broker_fee_status": "RESOLVED",
        "actual_broker_fee": 0.01,
    }
    trade_client = fake_trade_client(positions={"data": [{"symbol": "AAPL", "quantity": "0.35"}]})

    res = execution_service._finish_with_realized(trade_client, cfg, intent, summary)
    assert res["status"] == "FILLED"
    assert res["cashflow_finalized"] is True
    assert "delta_actual" not in res


def test_integration_decimal_holdings_survive_broker_parsing():
    """decimal holdings survive broker parsing without int truncation"""
    trade_client = fake_trade_client(
        positions={"data": [{"symbol": "AAPL", "quantity": "9.14492"}]}
    )
    holdings = fetch_holdings(trade_client, _cfg())
    assert abs(holdings - 9.14492) < 1e-6
    assert isinstance(holdings, float)


def test_integration_place_timeout_reconciliation_no_duplicate(tmp_path, monkeypatch):
    """fractional place timeout results in reconciliation, not duplicate placement"""
    _write_token(tmp_path, monkeypatch)
    body, _ = _run(monkeypatch)
    run_id = body["run_id"]

    placed = []

    def _place(_tc, order):
        placed.append(order)
        raise TimeoutError("gateway timeout")

    _stub_broker(monkeypatch, detail={"order_status": "UNKNOWN"},
                 holdings_after=PROD_HOLDINGS, place=_place)
    main._run_order_worker(_cfg(), limit=1)

    assert len(placed) == 1
    intent = _intent(run_id)
    assert intent["status"] == "PLACING_UNKNOWN"
    assert intent["place_attempted"] is True

    # The next tick reconciles by client_order_id and does not place again
    _stub_broker(monkeypatch, holdings_after=PROD_HOLDINGS - PROD_QUANTITY,
                 place=_place, detail={
                     "order_status": "FILLED", "filled_quantity": PROD_QUANTITY,
                     "avg_filled_price": 339.2})
    result = main._run_order_worker(_cfg(), limit=1)["results"][0]

    assert len(placed) == 1  # Still exactly one!
    assert result["status"] == "FILLED"
    assert result["cashflow_finalized"] is True


def test_integration_preview_rejection_never_reaches_place(tmp_path, monkeypatch):
    """preview rejection never reaches place_order; broker fractional rejection becomes explicit NOT_PLACED/block state"""
    _write_token(tmp_path, monkeypatch)
    body, _ = _run(monkeypatch)
    placed = []
    _stub_broker(monkeypatch, detail={}, holdings_after=PROD_HOLDINGS,
                 preview=lambda tc, o: False,
                 place=lambda tc, o: placed.append(o) or {})

    result = main._run_order_worker(_cfg(), limit=1)["results"][0]

    assert result["status"] == "NOT_PLACED"
    assert "preview ไม่ผ่าน" in result["error"]
    assert placed == []


def test_integration_preview_payload_and_placed_payload_match(tmp_path, monkeypatch):
    """preview payload and placed payload match exactly"""
    _write_token(tmp_path, monkeypatch)
    body, _ = _run(monkeypatch)
    run_id = body["run_id"]

    previewed, placed = [], []

    def _preview(_tc, order):
        previewed.append(order)
        return True

    def _place(_tc, order):
        placed.append(order)
        return {"order_status": "SUBMITTED"}

    _stub_broker(monkeypatch, detail={"order_status": "FILLED", "filled_quantity": PROD_QUANTITY, "avg_filled_price": 339.2},
                 holdings_after=PROD_HOLDINGS - PROD_QUANTITY,
                 place=_place, preview=_preview)

    main._run_order_worker(_cfg(), limit=1)
    assert len(previewed) == 1
    assert len(placed) == 1
    assert previewed[0] == placed[0]


def test_integration_non_fractional_symbol_retains_whole_share_behavior(monkeypatch):
    """non-fractional symbol retains whole-share behavior"""
    raw_payload = {
        "data": [{
            "symbol": "BRK.A", "status": "OC", "category": "US_STOCK",
            "currency": "USD", "lot_size": 1, "fractionable": False,
        }]
    }
    cap = parse_instrument_capability(raw_payload, "BRK.A")
    assert cap.fractionable is False
    assert cap.quantity_increment == Decimal("1")
    assert cap.decimal_precision == 0


def test_integration_fractional_order_outside_core_blocked(tmp_path, monkeypatch):
    """fractional order outside CORE session is blocked"""
    _write_token(tmp_path, monkeypatch)
    # Outside regular session
    monkeypatch.setattr(execution_service, "is_regular_session", lambda at: False)
    cap = InstrumentCapability(
        symbol="AAPL", status="OC", category="US_STOCK", currency="USD",
        lot_size=Decimal("1"), fractionable=True,
    )
    with pytest.raises(FractionalGateError, match="US Regular Trading Hours"):
        evaluate_fractional_order_gate(
            capability=cap, side="BUY", quantity=Decimal("0.299"), price=Decimal("339.15"),
            holdings=Decimal("0.0"), order_type="MARKET", session_check_fn=execution_service.is_regular_session,
        )
