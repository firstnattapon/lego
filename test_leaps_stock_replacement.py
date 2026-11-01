"""LEAPS as stock replacement (LEGO_LEAPS): count LEAPS with the shares, sell only shares.

The operator holds deep-ITM LEAPS in place of part of the position (1 contract = 100
share-equivalents). LEGO_LEAPS tells the rebalancer about them:

* effective holdings = real broker shares + LEAPS, formed before anything else is computed;
  the decision values that position (value, gap) and column 7 shows it;
* a BUY buys real shares; a SELL can only come out of the real shares, and a SELL that needs
  at least all of them is PASS_SELL_EXCEEDS_REAL with its reason, never a truncated order;
* every guard keeps reading real shares (vanished position, drift, paper book, fill check):
  prev_holdings stays real, a fill's holdings_after is real + LEAPS like the ledger;
* changing LEGO_LEAPS mid-chain continues the same chain (P0, Aₙ, Eₙ carry on), the change is
  recorded on the row it first applies to, and an unsent intent of the old base is stopped;
* LEGO_LEAPS unset or 0 is bit-identical to the code before it (oracle below).

Only broker I/O is a double, as in test_paper_mode.
"""
from __future__ import annotations

import dataclasses
import math
import re
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal

import pytest

import decision_service
import execution_service as execution
import lego_admin_reconcile
import main
import ops
from config import STRATEGY_ID, ConfigurationError, load_runtime_config
from conftest import FAKE_DB
from lego_one_row import (ACTUAL_COLUMN, DELTA_COLUMN, EXCESS_COLUMN, PASS_DNA_ZERO,
                          PASS_MIN_ORDER, PASS_SELL_EXCEEDS_REAL, PASS_THRESHOLD,
                          READY_BUY, READY_SELL, REFERENCE_COLUMN, Anchor, Config,
                          Decision, ExecutionFill, HoldingsAnomaly, build_decision,
                          check_holdings_continuity, compute_row, effective_holdings,
                          position_vanished)
from lego_state import (EXECUTION_STATE_KEY, ROWS_PATH, STATE_PATH, chain_key,
                        commit_final_row, make_run_id)
from test_dispatch_overshoot import isolate  # noqa: F401  (autouse)
from test_fractional_quantity_incident import _frozen
from test_paper_mode import (DIFF, FIX_C, OPEN, Session, forbid_the_money_boundary,
                             paper_values, prices)
from tools import trace_audit

HOLDINGS = trace_audit.COL["holdings"]
STATUS, REASON, QTY = trace_audit.COL["status"], trace_audit.COL["reason"], trace_audit.COL["qty"]
VALUE, GAP = trace_audit.COL["value"], trace_audit.COL["gap"]
V2 = STRATEGY_ID
BASE_ENV = {"WEBULL_ACCOUNT_ID": "test-account", "LEGO_SYMBOL": "UBER",
            "LEGO_FIX_C": "3000", "LEGO_DIFF": "5"}


def v2(leaps=0.0, *, fix_c=3000.0, diff=5.0, dp=2, inc=0.01):
    return Config("UBER", fix_c, diff, "bypass:1000", V2, dp, inc, leaps_shares=leaps)


# ------------------------------------------------------------------ the pre-LEAPS oracle
def legacy_build_decision(cfg, price, holdings, signal):
    """build_decision exactly as it was before LEGO_LEAPS (main@f07f5f6): the oracle."""
    if not (math.isfinite(price) and price > 0):
        raise ValueError("price")
    if not (math.isfinite(holdings) and holdings >= 0):
        raise ValueError("holdings")
    if signal not in (0, 1):
        raise ValueError("signal")
    value = holdings * price
    gap = value - cfg.fix_c
    if signal == 0:
        return Decision(PASS_DNA_ZERO, "PASS", "", PASS_DNA_ZERO, 0.0, value, gap)
    if abs(gap) <= cfg.diff:
        return Decision(PASS_THRESHOLD, "PASS", "", PASS_THRESHOLD, 0.0, value, gap)
    if cfg.strategy_id.endswith("_v2"):
        raw = Decimal(str(abs(gap))) / Decimal(str(price))
        increment = Decimal(str(cfg.quantity_increment))
        qty = float((raw / increment).to_integral_value(rounding=ROUND_DOWN) * increment)
    else:
        qty = round(abs(gap) / price, cfg.decimal_precision)
    if gap > cfg.diff:
        quantum = Decimal(1).scaleb(-cfg.decimal_precision)
        qty = min(qty, float(Decimal(str(holdings)).quantize(quantum, rounding=ROUND_DOWN)))
    if cfg.strategy_id.endswith("_v2") and qty > 0 and qty < 1.0:
        if (Decimal(str(qty)) * Decimal(str(price))) < Decimal("1.0"):
            qty = 0.0
    if qty <= 0:
        reason = PASS_MIN_ORDER if cfg.strategy_id.endswith("_v2") else PASS_THRESHOLD
        return Decision(reason, "PASS", "", reason, 0.0, value, gap)
    if gap > cfg.diff:
        return Decision(READY_SELL, "TRIGGER_ACTION", "SELL", READY_SELL, qty, value, gap)
    return Decision(READY_BUY, "TRIGGER_ACTION", "BUY", READY_BUY, qty, value, gap)


def bits(decision):
    """Every field, floats by repr: equal here means equal bit for bit."""
    return {k: (repr(v) if isinstance(v, float) else v)
            for k, v in dataclasses.asdict(decision).items()}


LEGACY_CONFIGS = [
    Config("APLS", 1500.0, 60.0, "21222524217299"),                       # the characterization CFG
    Config("UBER", 3000.0, 25.0, "bypass:1000", "shannon_demon_lego", 2),
    v2(), v2(dp=0, inc=1.0), v2(fix_c=10000.0, diff=25.0),
]
PRICES = [0.37, 1.0, 6.5, 19.99, 27.68, 65.49, 66.12, 331.25, 1000.0]
REAL = [0.0, 0.004, 0.1, 1.0, 8.776106, 45.83, 147.13455, 151.3228, 200.0, 10000.0]


# ------------------------------------------------------------------ LEGO_LEAPS in config
@pytest.mark.parametrize("raw", [None, "", "   ", "0", "0.0", "-0", " 0 "])
def test_unset_blank_or_zero_leaps_is_the_six_field_contract(raw):
    base = load_runtime_config(BASE_ENV)
    env = dict(BASE_ENV) if raw is None else {**BASE_ENV, "LEGO_LEAPS": raw}
    runtime = load_runtime_config(env)
    assert runtime.operator.leaps_shares == 0.0 and math.copysign(1, runtime.operator.leaps_shares) == 1
    assert runtime.operator.canonical() == base.operator.canonical()
    assert set(runtime.operator.canonical()) == {
        "symbol", "principal_usd", "diff_usd", "dna_fingerprint", "mode", "active"}
    assert runtime.operator.config_hash == base.operator.config_hash
    assert runtime.deployment.expected_release_binding == base.deployment.expected_release_binding


@pytest.mark.parametrize("raw,value", [("100", 100.0), ("12.5", 12.5), ("1e2", 100.0),
                                       (" 85.25 ", 85.25), ("200.000", 200.0)])
def test_leaps_is_a_share_count_and_part_of_what_a_release_authorizes(raw, value):
    base = load_runtime_config(BASE_ENV)
    runtime = load_runtime_config({**BASE_ENV, "LEGO_LEAPS": raw})
    assert runtime.operator.leaps_shares == value
    assert runtime.operator.canonical()["leaps_shares"] == format(value, ".17g")
    assert runtime.operator.config_hash != base.operator.config_hash
    assert runtime.deployment.expected_release_binding != base.deployment.expected_release_binding


@pytest.mark.parametrize("raw", ["-1", "-0.01", "nan", "NaN", "inf", "-inf", "abc", "1e400",
                                 "1,000", "100 shares"])
def test_anything_but_a_finite_count_is_refused_not_read_as_zero(raw):
    with pytest.raises(ConfigurationError):
        load_runtime_config({**BASE_ENV, "LEGO_LEAPS": raw})


def test_leaps_never_moves_the_chain_and_is_deployable():
    assert ops.DEPLOY_OVERRIDES["LEGO_LEAPS"] == "LEGO_LEAPS_OVERRIDE"
    assert chain_key(v2(100.0)) == chain_key(v2()) == chain_key(v2(12.5))
    for bad in (-1.0, float("nan"), float("inf"), True, "100", None):
        with pytest.raises((TypeError, ValueError)):
            v2(bad)
    assert v2(100).leaps_shares == 100.0 and isinstance(v2(100).leaps_shares, float)


# ------------------------------------------------------------------ LEAPS = 0 is bit-identical
def test_without_leaps_every_decision_is_the_pre_leaps_decision_bit_for_bit():
    checked = 0
    for cfg in LEGACY_CONFIGS:
        for price in PRICES:
            for real in REAL:
                for signal in (0, 1):
                    assert bits(build_decision(cfg, price, real, signal)) == bits(
                        legacy_build_decision(cfg, price, real, signal)), (cfg, price, real, signal)
                    checked += 1
    assert checked == len(LEGACY_CONFIGS) * len(PRICES) * len(REAL) * 2


def test_without_leaps_rows_fills_and_holdings_are_the_same_objects():
    for real in REAL:
        assert effective_holdings(real, 0.0) is real
        assert ExecutionFill.from_broker(66.14, 0.45, real) == ExecutionFill(66.14, 0.45, real)
    snapshot = {"captured_at": "2026-10-05T13:30:05Z", "price": 65.49, "holdings": 45.83}
    anchor = Anchor(version=3, dna_step=2, p0=65.0, prev_price=65.2, prev_actual=1.5,
                    prev_holdings=45.83, prev_excess=0.25)
    for cfg in LEGACY_CONFIGS:
        row = compute_row(cfg, snapshot, anchor, dna_step=3)
        assert row[HOLDINGS] == 45.83
        assert not {"real_holdings", "leaps_shares", "effective_holdings",
                    "leaps_shares_changed_from"} & set(row["_meta"])
        legacy = legacy_build_decision(cfg, 65.49, 45.83, row["DNA signal"])
        assert (row[STATUS], row[QTY], repr(row[VALUE]), repr(row[GAP])) == (
            legacy.status, legacy.quantity, repr(legacy.value), repr(legacy.gap))


def test_without_leaps_the_row_and_the_state_carry_no_leaps_key():
    cfg = v2()
    snapshot = {"captured_at": "2026-10-05T13:30:05Z", "price": 65.49, "holdings": 45.83}
    result = commit_final_row(cfg, snapshot, None, compute_row(cfg, snapshot, None, dna_step=0))
    row = FAKE_DB.reference(f"{ROWS_PATH}/{result['run_id']}").get()
    state = FAKE_DB.reference(f"{STATE_PATH}/{chain_key(cfg)}").get()
    leaps_keys = {"real_holdings", "leaps_shares", "leaps_shares_changed_from",
                  "post_execution_real_holdings", "funding_baseline_cancelled_by_leaps"}
    assert not leaps_keys & set(row) and "leaps_shares" not in state
    assert not {"leaps_shares_changed_from", "funding_baseline_cancelled"} & set(result)


# ------------------------------------------------------------------ the decision with LEAPS
def test_leaps_buy_values_real_plus_leaps_and_buys_real_shares():
    cfg = v2(100.0)
    decision = build_decision(cfg, 20.0, 10.0, 1)                   # 10 real + 100 LEAPS
    assert (decision.status, decision.side, decision.quantity) == (READY_BUY, "BUY", 40.0)
    assert (decision.value, decision.gap) == (2200.0, -800.0)       # 110 x 20, against 3000
    row = compute_row(cfg, {"captured_at": "2026-10-05T13:30:05Z", "price": 20.0,
                            "holdings": 10.0}, None, dna_step=0)
    assert (row[HOLDINGS], row[VALUE], row[GAP], row[QTY]) == (110.0, 2200.0, -800.0, 40.0)
    assert {k: row["_meta"][k] for k in ("real_holdings", "leaps_shares", "effective_holdings")} == {
        "real_holdings": 10.0, "leaps_shares": 100.0, "effective_holdings": 110.0}
    assert "leaps_shares_changed_from" not in row["_meta"]          # genesis: nothing changed


def test_a_sell_beyond_the_real_shares_passes_with_its_reason_and_is_not_cut_down():
    decision = build_decision(v2(100.0), 40.0, 5.0, 1)              # 105 x 40 = 4200 vs 3000
    assert (decision.status, decision.action, decision.side) == (PASS_SELL_EXCEEDS_REAL, "PASS", "")
    assert decision.quantity == 0.0 and not decision.acted
    assert (decision.value, decision.gap) == (4200.0, 1200.0)
    assert decision.reason.startswith(PASS_SELL_EXCEEDS_REAL + ":")
    assert "ต้องขาย 30 หุ้น" in decision.reason and "หุ้นจริง 5 หุ้น" in decision.reason
    assert "LEAPS 100" in decision.reason and "ไม่ตัดเหลือ" in decision.reason
    # The same position held as 105 real shares is an ordinary SELL of 30.
    plain = build_decision(v2(), 40.0, 105.0, 1)
    assert (plain.status, plain.quantity) == (READY_SELL, 30.0)
    row = compute_row(v2(100.0), {"captured_at": "2026-10-05T13:30:05Z", "price": 40.0,
                                  "holdings": 5.0}, None, dna_step=0)
    assert (row[STATUS], row["คำสั่ง"], row[QTY], row[REASON]) == (
        PASS_SELL_EXCEEDS_REAL, "PASS", 0.0, decision.reason)
    assert row["_meta"]["acted"] is False and row["_meta"]["execution_pending"] is False


def test_a_sell_of_exactly_every_real_share_passes_too():
    # 30 real + 75 LEAPS at 40: the rebalance needs exactly the 30 real shares. Selling them
    # would leave a real position of 0, which the vanished-position guard reads as a broker
    # that lost the symbol.
    decision = build_decision(v2(75.0), 40.0, 30.0, 1)
    assert decision.status == PASS_SELL_EXCEEDS_REAL and decision.quantity == 0.0
    # Rounding up to every real share counts as well (legacy round(), 0 places): the exact
    # need is 2.6 of 3 real shares, the rounded order would be all 3.
    legacy = Config("UBER", 14.0, 0.0, "bypass:1000", "shannon_demon_lego", 0, leaps_shares=1.0)
    rounded = build_decision(legacy, 10.0, 3.0, 1)
    assert rounded.status == PASS_SELL_EXCEEDS_REAL and "ต้องขาย 3 หุ้น" in rounded.reason


def test_a_sell_within_the_real_shares_is_ordinary_and_capped_at_real():
    decision = build_decision(v2(100.0, fix_c=2500.0), 20.0, 50.0, 1)   # 150 x 20 = 3000
    assert (decision.status, decision.side, decision.quantity) == (READY_SELL, "SELL", 25.0)
    # Fractional: 151.3228 real + 100 LEAPS at 66.12 against 16000 sells 9.33 of the real shares.
    fractional = build_decision(v2(100.0, fix_c=16000.0), 66.12, 151.3228, 1)
    assert (fractional.status, fractional.quantity) == (READY_SELL, 9.33)
    assert fractional.quantity < 151.32                                # the real ceiling


def test_the_sell_ceiling_is_the_real_position_where_it_still_binds():
    # Increment 0.5 with 0 broker places: the floored order (9.5) is below the 9.7 real shares
    # but above what 0 places can express of them (9). The ceiling is the real position.
    cfg = v2(100.0, fix_c=1001.0, dp=0, inc=0.5)
    decision = build_decision(cfg, 10.0, 9.7, 1)                     # 109.7 x 10 = 1097, need 9.6
    assert (decision.status, decision.quantity) == (READY_SELL, 9.0)


def test_zero_real_shares_with_leaps():
    cfg = v2(100.0)
    snapshot = {"captured_at": "2026-10-05T13:30:05Z", "price": 40.0, "holdings": 0.0}
    check_holdings_continuity(None, 0.0)                               # genesis
    check_holdings_continuity(Anchor(1, 0, 40.0, 40.0, 0.0, prev_holdings=0.0), 0.0)
    above = compute_row(cfg, snapshot, None, dna_step=0)              # LEAPS alone above FIX_C
    assert (above[STATUS], above[HOLDINGS], above[QTY]) == (PASS_SELL_EXCEEDS_REAL, 100.0, 0.0)
    below = compute_row(cfg, {**snapshot, "price": 20.0}, None, dna_step=0)
    assert (below[STATUS], below[HOLDINGS], below[QTY]) == (READY_BUY, 100.0, 50.0)


def test_fractional_policy():
    # The sum is exact: no binary neighbour of 251.3228 or 0.3 reaches the ledger.
    assert repr(effective_holdings(151.3228, 100.0)) == "251.3228"
    assert effective_holdings(0.1, 0.2) == 0.3 != 0.1 + 0.2
    assert effective_holdings(147.13455, 12.5) == 159.63455
    # Whole shares (LEGO_ALLOW_FRACTIONAL=false: increment 1, 0 places) with fractional LEAPS.
    whole = v2(12.5, fix_c=2000.0, dp=0, inc=1.0)
    buy = build_decision(whole, 50.0, 10.0, 1)                       # 22.5 x 50 = 1125
    assert (buy.status, buy.quantity) == (READY_BUY, 17.0)            # floor(17.5)
    sell = build_decision(whole, 100.0, 10.0, 1)                     # 22.5 x 100 = 2250
    assert (sell.status, sell.quantity) == (READY_SELL, 2.0)          # floor(2.5)
    assert float(buy.quantity).is_integer() and float(sell.quantity).is_integer()
    # The v2 minimum order still applies on top of LEAPS: 0.01 share x 50 USD < 1 USD.
    tiny = build_decision(v2(100.0, fix_c=5250.6, diff=0.5), 50.0, 5.0, 1)
    assert (tiny.status, tiny.quantity) == (PASS_MIN_ORDER, 0.0)


def test_trace_audit_recomputes_every_leaps_decision_like_the_engine():
    checked = 0
    for leaps in (0.0, 0.5, 12.5, 20.0, 100.0, 300.0):
        for dp, inc in ((2, 0.01), (0, 1.0)):
            for fix_c, diff in ((3000.0, 5.0), (16000.0, 25.0)):
                cfg = v2(leaps, fix_c=fix_c, diff=diff, dp=dp, inc=inc)
                for price in PRICES:
                    for real in REAL:
                        for signal in (0, 1):
                            engine = build_decision(cfg, price, real, signal)
                            audit = trace_audit.recompute_decision({
                                "P_n": price, "holdings": real, "FIX_C": fix_c, "DIFF": diff,
                                "signal": signal, "strategy": V2, "dp": dp, "inc": inc,
                                "leaps_shares": leaps})
                            assert (audit["V_n"], audit["gap"]) == (engine.value, engine.gap)
                            assert (audit["status"], audit["side"], audit["qty"]) == (
                                engine.status, engine.side, engine.quantity), (cfg, price, real)
                            checked += 1
    assert checked == 6 * 2 * 2 * len(PRICES) * len(REAL) * 2
    assert PASS_SELL_EXCEEDS_REAL in trace_audit.DECISION_STATUSES


# ------------------------------------------------------------------ guards stay on real shares
def test_the_vanished_position_guard_compares_real_shares():
    held = Anchor(4, 3, 65.0, 65.0, 0.0, prev_holdings=5.0, prev_leaps_shares=100.0)
    with pytest.raises(HoldingsAnomaly):
        check_holdings_continuity(held, 0.0)    # 0 real while 100 LEAPS are held: still refused
    assert position_vanished(5.0, 0.0) and not position_vanished(0.0, 0.0)


def switch(session, monkeypatch, leaps, **overrides):
    """Deploy the session again with LEGO_LEAPS=leaps (and any other override)."""
    values = paper_values(LEGO_LEAPS=str(leaps), **overrides)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    session.runtime = load_runtime_config(values)
    session.cfg = Config("UBER", FIX_C, DIFF, "bypass:1000", session.runtime.operator.strategy_id,
                         leaps_shares=session.runtime.operator.leaps_shares)
    return session


def test_a_vanished_real_position_is_refused_while_leaps_are_held(monkeypatch):
    session = switch(Session(monkeypatch, real=5.0), monkeypatch, 100, LEGO_MODE="preview")
    first = session.decide(0, Decimal("65.49"))       # 105 x 65.49 against 3000: SELL > 5 real
    assert first["status"] == PASS_SELL_EXCEEDS_REAL and first["effective_holdings"] == 105.0
    assert session.state()["prev_holdings"] == 5.0    # real, never 105
    session.real = 0.0                                # a positions response that lost the symbol
    session.decided_at = OPEN + timedelta(minutes=15, seconds=5)
    monkeypatch.setattr(decision_service, "datetime", _frozen(session.decided_at))
    body, code = decision_service.run_decision(None, session.runtime, session.cfg)
    assert code == 409 and body["status"] == "HOLDINGS_ANOMALY"   # effective would read 100


# ------------------------------------------------------------------ fills: ledger real + LEAPS
def _filled_chain(monkeypatch, *, real_after, leaps=100.0):
    cfg = v2(leaps)
    snapshot = {"captured_at": "2026-10-05T13:30:05Z", "price": 20.0, "holdings": 10.0}
    row = compute_row(cfg, snapshot, None, dna_step=0)
    assert row[STATUS] == READY_BUY and row[QTY] == 40.0
    run_id = commit_final_row(cfg, snapshot, None, row)["run_id"]
    monkeypatch.setattr(execution, "fetch_holdings", lambda *_a: real_after)
    intent = {"run_id": run_id, "side": "BUY", "decision_holdings": 10.0}
    summary = {"filled_quantity": 40.0, "filled_price": 20.0}
    return cfg, run_id, intent, summary


def test_a_fill_books_real_plus_leaps_and_keeps_the_chain_on_real_shares(monkeypatch):
    cfg, run_id, intent, summary = _filled_chain(monkeypatch, real_after=50.0)
    finalized = execution._finalize_model_ledger(None, cfg, intent, summary)
    assert (finalized["holdings_after"], finalized["real_holdings_after"],
            finalized["leaps_shares"]) == (150.0, 50.0, 100.0)
    state = FAKE_DB.reference(f"{STATE_PATH}/{chain_key(cfg)}").get()
    row = FAKE_DB.reference(f"{ROWS_PATH}/{run_id}").get()
    assert state["prev_holdings"] == 50.0                     # real: the next guard compares this
    assert (row["post_execution_holdings"], row["post_execution_real_holdings"]) == (150.0, 50.0)
    record = state[EXECUTION_STATE_KEY]["finalized_runs"][run_id]
    assert (record["holdings_after"], record["real_holdings_after"]) == (150.0, 50.0)
    # The admin repair tool agrees with all three, and would catch a state holding real + LEAPS.
    kwargs = dict(cfg=cfg, chain_key=chain_key(cfg), run_id=run_id,
                  intent={**intent, "place_attempted": True, "cashflow_finalized": True,
                          "realized": True, "filled_quantity": 40.0, "filled_price": 20.0,
                          "symbol": "UBER", "quantity": 40.0},
                  row=row, realized={}, broker_cashflow=None,
                  evidence={"filled_quantity": 40.0, "filled_price": 20.0, "filled_fee": 0.0,
                            "side": "BUY"})
    blockers = lego_admin_reconcile._positive_fill_blockers(state=state, **kwargs)
    assert not {"state_prev_holdings_mismatch", "row_post_execution_holdings_mismatch",
                "model_cashflow_holdings_invalid"} & set(blockers)
    wrong = {**state, "prev_holdings": 150.0}
    assert "state_prev_holdings_mismatch" in lego_admin_reconcile._positive_fill_blockers(
        state=wrong, **kwargs)
    # The independent audit walks the fill on real shares too.
    report = trace_audit.check_fills(state, [row], {run_id: intent}, {}, fix_c=3000.0, p0=20.0)
    assert not [m for m in report["mismatch"]["items"] if m["field"] == "position_walk"]


@pytest.mark.parametrize("real_after", [0.0, 150.0, 49.0])
def test_a_fill_is_confirmed_on_real_shares_only(monkeypatch, real_after):
    # 0: the position vanished; 150: a reading that already includes the LEAPS; 49: not the fill.
    cfg, _run_id, intent, summary = _filled_chain(monkeypatch, real_after=real_after)
    with pytest.raises(execution.FillNotConfirmed):
        execution._finalize_model_ledger(None, cfg, intent, summary)


# ------------------------------------------------------------------ paper sessions, end to end
def _assert_ledger_continuous(rows, state):
    """No jump: Aₙ moves only by booked ΔAₙ, a frozen Eₙ stays, Rₙ keeps one P₀."""
    p0, carried_a, carried_e = float(state["p0"]), 0.0, None
    for row in rows:
        assert row[REFERENCE_COLUMN] == pytest.approx(FIX_C * math.log(row["ราคา Pₙ (USD)"] / p0))
        if row["cashflow_status"] == "FINALIZED":
            assert row[ACTUAL_COLUMN] == pytest.approx(carried_a + row[DELTA_COLUMN], abs=1e-9)
            carried_a, carried_e = row[ACTUAL_COLUMN], row[EXCESS_COLUMN]
        else:
            assert row[DELTA_COLUMN] == 0.0 and row[ACTUAL_COLUMN] == pytest.approx(carried_a)
            if carried_e is not None:
                assert row[EXCESS_COLUMN] == pytest.approx(carried_e)


def _audit_clean(session):
    rows, intents, state = session.rows(), session.intents(), session.state()
    p0 = float(state["p0"])
    rows_report = trace_audit.check_rows(rows, fix_c=FIX_C, diff=DIFF, p0=p0)
    fills_report = trace_audit.check_fills(state, rows, intents, {}, fix_c=FIX_C, p0=p0)
    assert rows_report["mismatch"]["count"] == 0, rows_report["mismatch"]
    assert fills_report["mismatch"]["count"] == 0, fills_report["mismatch"]
    return rows, intents, state


def test_a_paper_session_with_leaps_trades_real_shares_around_the_leaps(monkeypatch):
    session = switch(Session(monkeypatch), monkeypatch, 20)
    forbid_the_money_boundary(monkeypatch)
    session.run(prices())
    rows, intents, state = _audit_clean(session)
    fills = [i for i in intents.values() if i.get("paper_fill")]
    assert {i["side"] for i in fills} == {"BUY", "SELL"} and len(fills) > 2
    assert session.client.order_v3.place_order.calls == []
    for row in rows:
        assert row["leaps_shares"] == 20.0
        assert row[HOLDINGS] == float(Decimal(str(row["real_holdings"])) + Decimal("20"))
        assert row[STATUS] != PASS_SELL_EXCEEDS_REAL                   # 20 LEAPS < FIX_C
        if row["cashflow_status"] == "FINALIZED":
            assert row["post_execution_holdings"] == float(
                Decimal(str(row["post_execution_real_holdings"])) + Decimal("20"))
    for intent in intents.values():                                    # sized and re-checked
        assert intent["strategy_config"]["leaps_shares"] == 20.0       # on the decision's base
        assert intent["decision_holdings"] == pytest.approx(           # the paper (real) shares
            next(r for r in rows if r["run_id"] == intent["run_id"])["real_holdings"])
        assert re.fullmatch(r"[0-9a-f]{32}", intent["client_order_id"])
        assert intent["client_order_id"] == intent["run_id"]
        assert intent["status"] == "UNSENT_ABORTED" and intent.get("paper_fill")
    # The chain's position is the paper book: real shares, never real + LEAPS.
    last = rows[-1]
    assert state["prev_holdings"] == pytest.approx(
        last["post_execution_real_holdings"] if last["cashflow_status"] == "FINALIZED"
        else last["real_holdings"])
    assert state["leaps_shares"] == 20.0
    # A flat paper book plus LEAPS is not flat: no initial-funding baseline, ordinary fills.
    assert rows[0]["real_holdings"] == 0.0 and rows[0][HOLDINGS] == 20.0
    assert rows[0].get("initial_funding") is not True
    _assert_ledger_continuous(rows, state)


def test_a_paper_session_whose_leaps_alone_exceed_fix_c_never_sells(monkeypatch):
    session = switch(Session(monkeypatch), monkeypatch, 100)
    forbid_the_money_boundary(monkeypatch)
    session.run(prices()[:6])
    rows, intents, state = _audit_clean(session)
    assert [r[STATUS] for r in rows] == [PASS_SELL_EXCEEDS_REAL] * 6 and intents == {}
    assert all(r[HOLDINGS] == 100.0 and r["real_holdings"] == 0.0 for r in rows)
    assert all("หุ้นจริง 0 หุ้น" in r[REASON] for r in rows)
    assert state["prev_holdings"] == 0.0
    assert session.client.order_v3.preview_order.calls == []


def test_changing_leaps_mid_chain_is_a_recorded_rollover_not_a_rebase(monkeypatch):
    session = Session(monkeypatch)
    forbid_the_money_boundary(monkeypatch)
    path = prices()
    plan = [0.0] * 8 + [20.0] * 10 + [0.0] * 8                       # 0 -> 20 -> 0
    keys = set()
    for index, (price, leaps) in enumerate(zip(path, plan)):
        switch(session, monkeypatch, leaps)
        keys.add(session.ck)
        session.slot(index, price)
    rows, _intents, state = _audit_clean(session)
    assert len(keys) == 1 and len(rows) == len(path)                  # one chain throughout
    assert [r["version"] for r in rows] == list(range(1, len(path) + 1))
    on, off = rows[8], rows[18]
    assert (on["leaps_shares_changed_from"], on["leaps_shares"]) == (0.0, 20.0)
    assert (off["leaps_shares_changed_from"], off["leaps_shares"]) == (20.0, 0.0)
    assert sum("leaps_shares_changed_from" in r for r in rows) == 2   # recorded exactly twice
    assert all("leaps_shares" not in r for r in rows[:8] + rows[19:])  # back to the plain shape
    assert "leaps_shares" not in state
    # The new base applies from the switching row on: it values real + 20 right away.
    assert on[HOLDINGS] == float(Decimal(str(on["real_holdings"])) + 20)
    assert on[STATUS] == READY_SELL and off[STATUS] == READY_BUY      # rebalanced around LEAPS
    warning = FAKE_DB.reference("webull_lego_warnings/leaps_shares_changed").get()
    assert warning["count"] == 2 and (warning["from"], warning["to"]) == (20.0, 0.0)
    _assert_ledger_continuous(rows, state)


def test_an_unsent_intent_of_the_old_leaps_is_stopped_and_funding_closes(monkeypatch):
    session = Session(monkeypatch)
    forbid_the_money_boundary(monkeypatch)
    first = session.decide(0, Decimal("65.49"))                       # flat: initial funding
    assert first["status"] == READY_BUY
    assert session.state()[EXECUTION_STATE_KEY]["funding_baseline_pending"] is True
    switch(session, monkeypatch, 20)                                  # deployed before dispatch
    session.dispatch()
    intent = session.intents()[first["run_id"]]
    assert intent["status"] == "SUPPRESSED_STATE_CHANGED"
    assert intent["terminal_reason"] == "LEGO_LEAPS changed since decision"
    assert (intent["decision_leaps_shares"], intent["dispatch_leaps_shares"]) == (0.0, 20.0)
    assert session.client.order_v3.preview_order.calls == []          # stopped before Preview
    second = session.decide(1, Decimal("65.49"))                      # the new base decides
    assert second["leaps_shares_changed_from"] == 0.0
    assert second["funding_baseline_cancelled"]["by"] == "LEGO_LEAPS"
    cashflow = session.state()[EXECUTION_STATE_KEY]
    assert cashflow["funding_baseline_pending"] is False
    assert cashflow["funding_baseline_cancelled"]["run_id"] == second["run_id"]
    row = FAKE_DB.reference(f"{ROWS_PATH}/{second['run_id']}").get()
    assert row["funding_baseline_cancelled_by_leaps"] is True
    assert FAKE_DB.reference("webull_lego_warnings/leaps_funding_baseline_cancelled").get()
    session.dispatch()                                                # an ordinary fill now
    row = FAKE_DB.reference(f"{ROWS_PATH}/{second['run_id']}").get()
    assert row["cashflow_status"] == "FINALIZED" and row["initial_funding"] is False
    assert row["post_execution_holdings"] == float(
        Decimal(str(row["post_execution_real_holdings"])) + 20)
    _audit_clean(session)


def test_the_tick_log_carries_the_leaps_split_only_when_used(capsys):
    import json

    import observability
    body = {"pipeline_status": "TICK_OK", "decision": {
        "run_id": "r" * 32, "status": READY_BUY, "pipeline_status": "ROW_COMMITTED"}}
    observability.emit_tick(body, 200)
    plain = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert set(plain["decision"]) == {"run_id", "market_slot_id", "step", "status",
                                      "pipeline_status", "committed", "dna_steps_remaining"}
    split = {"leaps_shares": 20.0, "real_holdings": 25.8, "effective_holdings": 45.8,
             "leaps_shares_changed_from": 0.0}
    observability.emit_tick({**body, "decision": {**body["decision"], **split}}, 200)
    event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert {key: event["decision"][key] for key in split} == split


# ------------------------------------------------------------------ order identity and wiring
def test_run_id_and_client_order_id_keep_their_format_and_inputs():
    snapshot = {"captured_at": "2026-10-05T13:30:05Z", "price": 65.49, "holdings": 10.0}
    plain, leaps = make_run_id(chain_key(v2()), 3, snapshot), make_run_id(chain_key(v2(100.0)), 3, snapshot)
    assert plain == leaps and re.fullmatch(r"[0-9a-f]{32}", leaps)   # real holdings, same chain


def test_the_tick_hands_the_leaps_to_the_decision_and_the_worker(monkeypatch):
    env = {"LEGO_SYMBOL": "AAPL", "LEGO_FIX_C": "3000", "LEGO_DIFF": "25", "WEBULL_ENV": "UAT",
           "WEBULL_ACCOUNT_ID": "test", "LEGO_MODE": "trade", "LEGO_ACTIVE": "true",
           "LEGO_CANDIDATE_HASH": "candidate", "LEGO_LEAPS": "100"}
    env["LEGO_RELEASE_AUTHORIZATION"] = load_runtime_config(env).deployment.expected_release_binding
    runtime = load_runtime_config(env)
    seen = []
    monkeypatch.setattr(main, "load_runtime_config", lambda: runtime)
    monkeypatch.setattr(execution, "_init_firebase", lambda: None)
    monkeypatch.setattr(main, "runtime_identity_fingerprint", lambda: "identity")
    monkeypatch.setattr(execution, "_run_order_worker",
                        lambda cfg, **_kw: seen.append(cfg) or {"results": [], "processed": 0})
    monkeypatch.setattr(main.decision_service, "run_decision",
                        lambda _request, runtime=None, cfg_override=None: (
                            seen.append(cfg_override)
                            or ({"pipeline_status": "ROW_COMMITTED", "run_id": "new"}, 200)))
    monkeypatch.setattr(main, "archive_terminal_records", lambda **_kw: {})
    _body, code = main.lego_tick(None)
    assert code == 200 and len(seen) >= 2
    assert all(cfg.leaps_shares == 100.0 for cfg in seen)
