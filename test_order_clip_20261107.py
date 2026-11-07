"""An order above the per-order caps is clipped to them (owner's rule (ก), 2026-11-07).

Before: a decision whose quantity or notional broke LEGO_MAX_ORDER_QUANTITY /
LEGO_MAX_ORDER_NOTIONAL_USD became NOT_PLACED at dispatch, slot after slot, while the gap
kept growing. Now the decision itself is clipped to the caps (BUY and SELL alike) and the
next slots rebalance the rest:

    09:30  short 220 USD -> buy only ~148.5 (clipped)  -> short ~70 USD
    09:45  short  70 USD -> buy 70                      -> back at FIX_C

Covered here: every order that fitted the caps is sized bit for bit as before, the clip
target leaves room for the 1 % dispatch drift, the chain identity does not move, the
dispatch re-check rebuilds the same clipped decision, intents written before clipping are
re-checked as before, and tools/trace_audit recomputes a clipped row.
"""
from __future__ import annotations

import itertools
from decimal import Decimal

import pytest

import execution_service as execution
from execution_limits import ExecutionLimits
from lego_one_row import (CLIP_PRICE_MARGIN, PASS_SELL_EXCEEDS_REAL, READY_BUY, READY_SELL,
                          Config, OrderCaps, build_decision, clip_quantity, compute_row)
from lego_state import chain_key, config_hash
from test_dispatch_overshoot import CFG, NOW, STAMP, isolate  # noqa: F401  (autouse)
from tools import trace_audit

QBTS = Config("QBTS", 1500.0, 50.0, "bypass:6500", "shannon_demon_lego_v2",
              decimal_precision=2, quantity_increment=0.01)
CAPS = OrderCaps(Decimal("20"), Decimal("150"))          # the QBTS canary: 20 shares / 150 USD


def shares_for(value_usd, price):
    """Real shares worth *value_usd* at *price* (no LEAPS)."""
    return float((Decimal(str(value_usd)) / Decimal(str(price))).quantize(Decimal("0.01")))


# ------------------------------------------------------------------ the owner's example
def test_a_220_usd_gap_buys_148_5_now_and_the_70_usd_rest_next_slot():
    price = 10.0
    holdings = shares_for(1500 - 220, price)                   # 128 shares: short 220 USD
    first = build_decision(QBTS, price, holdings, 1, CAPS)
    assert (first.status, first.side) == (READY_BUY, "BUY")
    assert first.quantity == 14.85                             # 148.51 / 10, rounded down
    assert first.quantity * price <= 150 / 1.01 + 1e-9
    assert build_decision(QBTS, price, holdings, 1).quantity == 22.0   # unclipped: 220 USD
    after = holdings + first.quantity                          # filled: short ~71.5 USD
    second = build_decision(QBTS, price, after, 1, CAPS)
    assert second.quantity == build_decision(QBTS, price, after, 1).quantity   # fits: untouched
    assert abs((after + second.quantity) * price - 1500) <= QBTS.diff


def test_a_sell_above_the_cap_is_clipped_the_same_way():
    price = 10.0
    holdings = shares_for(1500 + 400, price)                   # 400 USD above FIX_C
    sell = build_decision(QBTS, price, holdings, 1, CAPS)
    assert (sell.status, sell.side, sell.quantity) == (READY_SELL, "SELL", 14.85)


def test_the_share_cap_binds_when_it_is_tighter_than_the_notional_cap():
    price = 5.0                                                # 20 shares = 100 USD < 148.5
    holdings = shares_for(1500 - 400, price)
    assert build_decision(QBTS, price, holdings, 1, CAPS).quantity == 20.0


# ------------------------------------------------------------------ nothing else moves
@pytest.mark.parametrize("price,gap", list(itertools.product(
    (0.37, 1.0, 7.03, 9.99, 10.82, 14.6, 27.68, 148.0),
    (-149.99, -140.0, -60.0, -50.01, 0.0, 50.01, 60.0, 140.0, 149.99))))
def test_every_order_inside_the_caps_is_sized_exactly_as_before(price, gap):
    holdings = (1500 + gap) / price
    plain = build_decision(QBTS, price, holdings, 1)
    capped = build_decision(QBTS, price, holdings, 1, OrderCaps(Decimal("1000000"), Decimal("150")))
    if Decimal(str(plain.quantity)) * Decimal(str(price)) <= 150:
        assert capped == plain


@pytest.mark.parametrize("signal", [0, 1])
def test_pass_rows_are_untouched_by_the_caps(signal):
    for holdings in (shares_for(1500, 10), shares_for(1530, 10)):   # inside DIFF
        assert build_decision(QBTS, 10.0, holdings, signal, CAPS) == build_decision(
            QBTS, 10.0, holdings, signal)
    zero = build_decision(QBTS, 10.0, shares_for(1000, 10), 0, CAPS)
    assert zero.status != READY_BUY and zero.quantity == 0.0


def test_the_caps_are_not_part_of_the_chain_identity():
    """OrderCaps is an argument, never a Config field: config_hash and chain_key stay put."""
    assert "caps" not in Config.__dataclass_fields__ and "order_caps" not in Config.__dataclass_fields__
    row = compute_row(QBTS, {"price": 10.0, "holdings": 128.0, "captured_at": STAMP,
                             "quote_time": STAMP}, None, dna_step=0, caps=CAPS)
    assert row["_meta"]["quantity"] == 14.85
    assert chain_key(QBTS) == f"QBTS_{config_hash(QBTS)}"


def test_whole_shares_that_cannot_fit_keep_the_old_refusal():
    whole = Config("BIG", 1500.0, 50.0, "bypass:6500", "shannon_demon_lego_v2",
                   decimal_precision=0, quantity_increment=1.0)
    price = 200.0                                              # one share > 148.5 USD
    holdings = 5.0                                             # 1000 USD: short 500
    plain, capped = build_decision(whole, price, holdings, 1), build_decision(whole, price, holdings, 1, CAPS)
    assert capped == plain and plain.quantity == 2.0           # dispatch refuses it, as it always did


def test_the_legacy_rounding_strategy_is_clipped_down_too():
    legacy = Config("QBTS", 1500.0, 50.0, "bypass:6500", "shannon_demon_lego",
                    decimal_precision=2, quantity_increment=0.01)
    assert clip_quantity(legacy, 22.0, 10.0, CAPS) == 14.85


def test_a_leaps_sell_that_needs_more_than_the_real_shares_stays_a_pass():
    """The SELL-exceeds-real test runs on the unclipped quantity, as before the clip."""
    leaps = Config("QBTS", 1500.0, 50.0, "bypass:6500", "shannon_demon_lego_v2",
                   decimal_precision=2, quantity_increment=0.01, leaps_shares=100.0)
    decision = build_decision(leaps, 16.0, 5.0, 1, CAPS)       # (100 + 5) x 16 = 1680: SELL 11.25 > 5
    assert decision.status == PASS_SELL_EXCEEDS_REAL
    assert decision == build_decision(leaps, 16.0, 5.0, 1)


# ------------------------------------------------------------------ the dispatch boundary
def test_the_clip_target_survives_the_largest_dispatch_drift():
    limits = ExecutionLimits.parse(("20", "150", "10", "2030-01-01T00:00:00Z", "3"))
    for price in (Decimal("0.37"), Decimal("7.03"), Decimal("10.82"), Decimal("33.3333")):
        qty = clip_quantity(QBTS, 1000.0, float(price), CAPS)
        worst = price * (1 + CLIP_PRICE_MARGIN)                 # +100 bps, the dispatch maximum
        limits.check(str(qty), str(worst), now=NOW)             # never raises


def intent(cfg, price, holdings, caps):
    decision = build_decision(cfg, price, holdings, 1, caps)
    out = {"run_id": "c" * 32, "chain_key": chain_key(cfg), "status": "PENDING_DISPATCH",
           "row_status": decision.status, "symbol": cfg.symbol, "side": decision.side,
           "quantity": decision.quantity, "step": 0, "signal": 1, "decision_price": price,
           "decision_holdings": holdings, "decision_time": STAMP, "created_at": STAMP}
    if caps is not None:
        out["order_caps"] = caps.snapshot()
    return out


@pytest.mark.parametrize("drift", [-0.0099, -0.003, 0.0, 0.003, 0.0099])
def test_the_dispatch_recheck_rebuilds_the_clipped_decision(drift):
    price, holdings = 10.0, 128.0
    committed = intent(QBTS, price, holdings, CAPS)
    fresh = {"price": price * (1 + drift), "holdings": holdings, "quote_time": STAMP}
    result = execution._dispatch_quote_safety(QBTS, committed, fresh, now_utc=NOW,
                                              max_price_drift_bps=100)
    assert result["ok"], result["reasons"]
    assert result["intent_quantity"] == 14.85


def test_without_the_stored_caps_a_clipped_intent_is_refused_by_the_recheck():
    """A worker that rebuilt the decision unclipped would see another quantity: it refuses."""
    committed = intent(QBTS, 10.0, 128.0, CAPS)
    committed.pop("order_caps")
    fresh = {"price": 10.0, "holdings": 128.0, "quote_time": STAMP}
    result = execution._dispatch_quote_safety(QBTS, committed, fresh, now_utc=NOW, max_price_drift_bps=100)
    assert "intent_decision_mismatch" in result["reasons"]


def test_an_intent_written_before_clipping_is_rechecked_exactly_as_before():
    committed = intent(CFG, 27.68, 181, None)
    assert "order_caps" not in committed
    fresh = {"price": 27.6843, "holdings": 181, "quote_time": STAMP}
    result = execution._dispatch_quote_safety(CFG, committed, fresh, now_utc=NOW, max_price_drift_bps=100)
    assert result["ok"], result["reasons"]


@pytest.mark.parametrize("raw", ["x", {"quantity": "20"}, {"quantity": "20", "notional_usd": "x",
                                                            "price_margin": "0.01"},
                                 {"quantity": "0", "notional_usd": "150", "price_margin": "0.01"}])
def test_malformed_stored_caps_fail_the_recheck_closed(raw):
    with pytest.raises(ValueError):
        OrderCaps.from_snapshot(raw)


def test_the_snapshot_round_trips():
    assert OrderCaps.from_snapshot(CAPS.snapshot()) == CAPS
    assert OrderCaps.from_snapshot(None) is None
    assert CAPS.snapshot() == {"quantity": "20", "notional_usd": "150", "price_margin": "0.01"}


def test_a_gap_above_three_caps_is_large():
    assert not CAPS.large_gap(450.0) and CAPS.large_gap(450.01) and CAPS.large_gap(-451.0)


# ------------------------------------------------------------------ the row and its audit
def test_only_a_clipped_row_carries_the_clip_and_its_caps():
    snap = {"price": 10.0, "holdings": 128.0, "captured_at": STAMP, "quote_time": STAMP}
    clipped = compute_row(QBTS, snap, None, dna_step=0, caps=CAPS)
    assert clipped["_meta"]["clipped_from_quantity"] == 22.0
    assert clipped["_meta"]["order_caps"] == CAPS.snapshot()
    assert clipped["จำนวนสั่ง (หุ้น)"] == 14.85 and clipped["ส่วนต่างเป้าหมาย (USD)"] == pytest.approx(-220.0)
    fits = compute_row(QBTS, {**snap, "holdings": 143.0}, None, dna_step=0, caps=CAPS)
    assert "clipped_from_quantity" not in fits["_meta"] and "order_caps" not in fits["_meta"]
    plain = compute_row(QBTS, {**snap, "holdings": 143.0}, None, dna_step=0)
    assert fits == plain


@pytest.mark.parametrize("price,holdings", [(10.0, 128.0), (10.0, 190.0), (5.0, 220.0), (7.03, 138.63)])
def test_trace_audit_recomputes_a_clipped_decision(price, holdings):
    engine = build_decision(QBTS, price, holdings, 1, CAPS)
    audit = trace_audit.recompute_decision({
        "P_n": price, "holdings": holdings, "FIX_C": 1500.0, "DIFF": 50.0, "signal": 1, "P0": None,
        "dp": 2, "inc": 0.01, "strategy": "x_v2", "order_caps": CAPS.snapshot()})
    assert (audit["status"], audit["side"]) == (engine.status, engine.side)
    assert audit["qty"] == pytest.approx(engine.quantity, abs=1e-12)
