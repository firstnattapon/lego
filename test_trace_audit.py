"""tools.trace_audit: the digest an auditor (or an AI) starts from.

Every fixture here is SYNTHETIC: a small chain built from first principles with its own
arithmetic, shaped like the exports of 2026-10-08 (rows, finalized fills, an unresolved order, a
halt, a Cloud Run service). No account, order, log or key from a real run is in this file.

The decision formulas in the tool mirror lego_one_row.py. The parity tests below pin that mirror
to the production code, so changing a formula there cannot silently blind the audit.
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import flight_recorder as fr
import lego_one_row
import main as lego_main
import tick_runtime
from conftest import FAKE_DB
from test_execution_confirmed_cashflow import (  # noqa: F401  (env is an autouse fixture)
    _intent as stored_intent, _stub_broker, _work, env)
from test_incident_20261006 import PAST_MARGIN, _clock, _zombie
from tools import trace_audit as ta

UTC = timezone.utc
COL = ta.COL
FIX_C, DIFF, P0 = 10000.0, 25.0, 50.0
CHAIN = "TESTCO_a1b2c3d4e5f6"
T0 = datetime(2026, 10, 8, 13, 30, tzinfo=UTC)


def at(minutes):
    return (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_id(version):
    return hashlib.sha1(f"run-{version}".encode()).hexdigest()[:32]


# version, price, holdings at the decision, status, qty, side, fill=(price, fee) or None
PLAN = [
    (1, 50.00, 200.00, "PASS_THRESHOLD", 0.0, "", None),
    (2, 49.50, 200.00, "READY_BUY", 2.02, "BUY", (49.52, 1.07)),
    (3, 49.60, 202.02, "PASS_THRESHOLD", 0.0, "", None),
    (4, 51.00, 202.02, "READY_SELL", 5.94, "SELL", (51.02, 3.05)),
    (5, 51.10, 196.08, "PASS_THRESHOLD", 0.0, "", None),
    (6, 50.95, 196.08, "PASS_THRESHOLD", 0.0, "", None),
    (7, 49.90, 196.08, "READY_BUY", 4.32, "BUY", (49.93, 2.31)),
    (8, 49.90, 200.40, "PASS_THRESHOLD", 0.0, "", None),
    (9, 49.95, 200.40, "PASS_THRESHOLD", 0.0, "", None),
    (10, 49.20, 200.40, "READY_BUY", 2.85, "BUY", None),                # placed, never resolved
    (11, 49.00, 200.40, "PASS_RECOVERY_BLOCKED", 0.0, "", None),
    (12, 49.10, 200.40, "PASS_RECOVERY_BLOCKED", 0.0, "", None),
]
STUCK_BROKER_ID = "BRK-SYNTHETIC-0000000000000000"
STUCK_HALT = "ab" * 16


def make_world(*, stuck=True):
    """The RTDB export of a small chain, computed with plain arithmetic."""
    rows, finalized, applied, intents = {}, {}, {}, {}
    a_prev, p_acted, seq, realized_total = 0.0, P0, 0, 0.0
    for version, price, holdings, status, qty, side, fill in (PLAN if stuck else PLAN[:9]):
        value = holdings * price
        row = {
            COL["time"]: at(version * 15), COL["symbol"]: "TEST", COL["status"]: status, COL["step"]: 500 + version,
            COL["signal"]: 1, COL["price"]: price, COL["holdings"]: holdings,
            COL["action"]: "TRIGGER_ACTION" if qty else "PASS", COL["side"]: side, COL["reason"]: status,
            COL["qty"]: qty, COL["value"]: value, COL["gap"]: value - FIX_C,
            COL["R"]: FIX_C * math.log(price / P0), COL["dA"]: 0, COL["A"]: a_prev, COL["E"]: 0.0,
            "run_id": run_id(version), "chain_key": CHAIN, "version": version, "committed": True,
            "cashflow_status": "NO_ACTION" if not qty else "PENDING_EXECUTION",
            "instrument_capability": {"quantity_increment": "0.01", "decimal_precision": 2},
        }
        if fill:
            seq += 1
            p_fill, fee = fill
            delta = FIX_C * (p_fill / p_acted - 1.0)
            cumulative = a_prev + delta
            reference = FIX_C * math.log(price / P0)
            after = round(holdings + qty if side == "BUY" else holdings - qty, 6)
            finalized[run_id(version)] = {
                "seq": seq, "at": at(version * 15 + 1), "filled_price": p_fill, "filled_quantity": qty,
                "holdings_after": after, "previous_action_price": p_acted, "previous_actual_cumulative": a_prev,
                "delta_actual": delta, "actual_cumulative": cumulative, "reference": reference,
                "excess": cumulative - reference}
            row.update({COL["dA"]: delta, COL["A"]: cumulative, COL["E"]: cumulative - reference,
                        "cashflow_status": "FINALIZED", "finalized_seq": seq})
            delta_realized = -0.62 if side == "SELL" else 0.0
            realized_total += delta_realized
            applied[run_id(version)] = {"seq": seq, "side": side, "quantity": qty, "average_price": p_fill, "fee": fee,
                                        "realized_delta": delta_realized, "cumulative_realized_after": realized_total}
            a_prev, p_acted = cumulative, p_fill
        rows[run_id(version)] = row
        if qty:
            filled = bool(fill)
            intents[run_id(version)] = {
                "run_id": run_id(version), "client_order_id": run_id(version), "chain_key": CHAIN, "symbol": "TEST",
                "side": side, "quantity": qty, "decision_holdings": holdings, "created_at": at(version * 15),
                "status": "FILLED" if filled else "MANUAL_RECONCILIATION_REQUIRED",
                "cancel_policy": {"action": "cancel_expire", "stale_seconds": 300}}
    stuck_id = run_id(10)
    if stuck:
        intents[stuck_id].update({
            "broker_order_id": STUCK_BROKER_ID, "broker_status": "PENDING", "placed_at": at(152), "needs_manual_check": True,
            "manual_since": at(150 + 480), "cancel_attempt_count": 1, "cancel_refused_at": at(158),
            "cancel_last_error_code": "CANCEL_REFUSED_HOLD_EXPIRED", "cancel_confirmation_deadline": at(150 + 480),
            "filled_quantity": "0.000000", "updated_at": at(150 + 481)})
    state = {"updated_at": at(200), "version": len(rows), "dna_step": 500 + len(rows), "slot_id": "2026-10-08:11", "p0": P0,
             "cashflow_semantics": "execution_terminal_funding_v3", "clock_mode": "market", "prev_holdings": 200.4,
             "execution_cashflow": {"finalized_seq": seq, "actual_cumulative": a_prev, "last_action_price": p_acted,
                                    "excess": finalized[run_id(7)]["excess"], "finalized_runs": finalized}}
    db = {
        "webull_lego_state": {CHAIN: state}, "webull_lego_rows": rows,
        "webull_lego_order_outbox": {CHAIN: intents},
        "webull_lego_realized": {CHAIN: {"applied_seq": seq, "cumulative_realized": realized_total,
                                         "applied_fills": applied}},
        "webull_lego_execution_transitions": {CHAIN: {stuck_id: {
            f"r_{n:012d}": {"at": at(150 + n), "revision": n, "state": s}
            for n, s in enumerate([{"status": "PENDING_DISPATCH"}, {"status": "PLACING_UNKNOWN"},
                                   {"status": "PENDING", "broker_status": "PENDING"},
                                   {"status": "CANCEL_UNKNOWN", "broker_status": "PENDING",
                                    "cancel_last_error_code": "CANCEL_REFUSED_NOT_OPERABLE"},
                                   {"status": "MANUAL_RECONCILIATION_REQUIRED", "broker_status": "PENDING",
                                    "needs_manual_check": True}], start=1)}}},
        "webull_lego_order_dispatch_locks": {"SCOPE_synthetic_0123456789": {
            "inflight_run_id": stuck_id, "fenced_run_id": stuck_id, "claimed_at": at(300),
            "operator_halt": {"halted": True, "halt_id": STUCK_HALT, "reason": "CANCEL_REFUSED_HOLD_EXPIRED",
                              "set_at": at(150 + 480), "set_by": "system:order-recovery"}}},
        "webull_lego_operator_halt_audit": {"SCOPE_synthetic_0123456789": {"evt1": {
            "action": "HALT", "at": at(150 + 480), "operator": "system:order-recovery",
            "reason": "CANCEL_REFUSED_HOLD_EXPIRED", "halt_id": STUCK_HALT}}},
    }
    if not stuck:
        for name in ("webull_lego_order_dispatch_locks", "webull_lego_operator_halt_audit",
                     "webull_lego_execution_transitions"):
            db[name] = {}
    return db


YAML = """apiVersion: serving.knative.dev/v1
kind: Service
metadata:
  name: lego-tick-uat
  generation: 7
spec:
  template:
    metadata:
      name: lego-tick-uat-00007-xyz
      annotations:
        autoscaling.knative.dev/maxScale: 1
    spec:
      containerConcurrency: 1
      timeoutSeconds: 45
      containers:
      - name: worker
        env:
        - name: FIREBASE_DB_URL
          value: >-
            https://example-rtdb.invalid
        - name: LEGO_DIFF
          value: 25
        - name: LEGO_FIX_C
          value: 10000
        - name: LEGO_CANDIDATE_HASH
          value: deadbeefcafe
        - name: LEGO_GIT_COMMIT
          value: 0123456789abcdef
        - name: LEGO_RELEASE_AUTHORIZATION
          value: RELEASE-BINDING-SENTINEL
        - name: LEGO_STALE_ORDER_ACTION
          value: cancel_expire
        - name: LEGO_TRADING_WINDOW_END
          value: '2026-10-12T20:00:00Z'
        - name: WEBULL_ENV
          value: UAT
        - name: WEBULL_ACCOUNT_ID
          valueFrom:
            secretKeyRef:
              key: latest
              name: ACCOUNT-SECRET-SENTINEL
        resources:
          limits:
            cpu: 0.3333
            memory: 512Mi
"""


# ------------------------------------------------------------------------- parity with production

def test_row_columns_are_the_production_contract():
    assert list(COL.values()) == list(lego_one_row.COLUMN_ORDER)
    assert (COL["R"], COL["dA"], COL["A"], COL["E"]) == (
        lego_one_row.REFERENCE_COLUMN, lego_one_row.DELTA_COLUMN, lego_one_row.ACTUAL_COLUMN, lego_one_row.EXCESS_COLUMN)
    assert ta.DECISION_STATUSES >= {lego_one_row.PASS_DNA_ZERO, lego_one_row.PASS_THRESHOLD, lego_one_row.PASS_MIN_ORDER,
                                    lego_one_row.READY_BUY, lego_one_row.READY_SELL}


def test_the_decision_mirror_matches_the_production_decision_everywhere():
    prices = [0.93, 41.37, 49.5, 68.99, 260.0, 333.4, 1523.55]
    holdings = [0.0, 0.37, 9.0, 144.9528, 200.0, 1.234567]
    checked = 0
    for fix_c, diff, strategy, (dp, inc), signal in itertools.product(
            (3000.0, 10000.0), (0.0, 25.0), ("shannon_demon_lego", "shannon_demon_lego_v2"),
            ((0, 1.0), (2, 0.01), (1, 0.1), (5, 1.0)), (0, 1)):
        cfg = lego_one_row.Config("X", fix_c, diff, strategy_id=strategy, decimal_precision=dp, quantity_increment=inc)
        for price, held in itertools.product(prices, holdings):
            real = lego_one_row.build_decision(cfg, price, held, signal)
            mine = ta.recompute_decision({"P_n": price, "holdings": held, "FIX_C": fix_c, "DIFF": diff, "signal": signal,
                                          "strategy": strategy, "dp": dp, "inc": inc, "P0": 50.0})
            assert (mine["status"], mine["side"]) == (real.status, real.side), (cfg, price, held, signal)
            assert mine["qty"] == pytest.approx(real.quantity, abs=1e-12), (cfg, price, held, signal)
            assert mine["V_n"] == pytest.approx(real.value) and mine["gap"] == pytest.approx(real.gap)
            checked += 1
    assert checked == 2688


def test_the_fill_and_reference_mirrors_match_the_production_recurrence():
    cfg = lego_one_row.Config("X", FIX_C, DIFF)
    anchor = lego_one_row.Anchor(version=3, dna_step=9, p0=P0, prev_price=49.52, prev_actual=-96.0)
    produced = lego_one_row.compute_recurrence(cfg, 51.02, anchor, acted=True)
    mine = ta.recompute_fill(FIX_C, 51.02, anchor.prev_price, anchor.prev_actual, produced.R)
    assert mine["delta_actual"] == pytest.approx(produced.dA)
    assert mine["actual_cumulative"] == pytest.approx(produced.A)
    assert mine["excess"] == pytest.approx(produced.E)
    live = lego_one_row.compute_recurrence(cfg, 49.0, anchor, acted=False)
    assert ta.recompute_decision({"P_n": 49.0, "holdings": 200.0, "FIX_C": FIX_C, "DIFF": DIFF, "signal": 1,
                                  "P0": P0})["R_n"] == pytest.approx(live.R)


# ------------------------------------------------------------------------------ a clean chain

def test_a_consistent_chain_passes_every_check():
    report = ta.build_report(rtdb=make_world())
    (chain,) = report["rtdb"]["chains"]
    rows = chain["rows"]
    assert rows["n"] == 12 and rows["mismatch"]["count"] == 0
    assert rows["fix_c"] == FIX_C and rows["rules"] == {"floor_v2": 10} and rows["gated"] == {"PASS_RECOVERY_BLOCKED": 2}
    assert rows["diff"]["source"] == "inferred from the rows"
    assert rows["diff"]["lower"] == pytest.approx(20.192, abs=1e-3) and rows["diff"]["upper"] == pytest.approx(100.0)
    assert rows["versions"] == {"first": 1, "last": 12, "contiguous": True} and rows["steps"]["gaps"] == []
    assert chain["fills"]["n"] == 3 and chain["fills"]["mismatch"]["count"] == 0
    assert [f["seq"] for f in chain["fills"]["fills"]] == [1, 2, 3]
    assert all(abs(f["reference_gap"]) < 1e-9 for f in chain["fills"]["fills"])
    assert chain["ledger"]["problems"] == [] and chain["ledger"]["net_position_change"] == pytest.approx(0.4)
    assert chain["economics"]["n"] == 3 and chain["economics"]["fee_pct"]["median"] == pytest.approx(1.07, abs=0.02)
    assert chain["economics"]["cumulative_realized"] == pytest.approx(-0.62)


def test_a_given_diff_must_fit_the_decisions_the_rows_made():
    fine = ta.build_report(rtdb=make_world(), diff=DIFF)["rtdb"]["chains"][0]["rows"]
    assert fine["diff"]["source"] == "config" and fine["mismatch"]["count"] == 0
    wrong = ta.build_report(rtdb=make_world(), diff=10.0)["rtdb"]["chains"][0]["rows"]
    assert any(m["field"] == "diff_vs_rows" for m in wrong["mismatch"]["items"])


def test_the_findings_name_the_stuck_order_the_halt_and_the_unknown_proof():
    report = ta.build_report(rtdb=make_world())
    codes = [f["code"] for f in report["findings"]]
    assert codes[:3] == ["ORDER_UNRESOLVED", "EXPIRY_PROOF_UNKNOWN", "OPERATOR_HALT"] or {
        "ORDER_UNRESOLVED", "EXPIRY_PROOF_UNKNOWN", "OPERATOR_HALT"} <= set(codes[:4])
    by = {f["code"]: f for f in report["findings"]}
    assert run_id(10)[:8] in by["ORDER_UNRESOLVED"]["text"] and "CANCEL_REFUSED_HOLD_EXPIRED" in by["ORDER_UNRESOLVED"]["text"]
    assert "2 slots" in by["OPERATOR_HALT"]["text"] and STUCK_HALT[:8] in by["OPERATOR_HALT"]["text"]
    assert "FEE_DRAG" in by and by["FEE_DRAG"]["level"] == "P1" and "NO_TRACES" in by
    levels = [f["level"] for f in report["findings"]]
    assert levels == sorted(levels, key=ta.LEVELS.index)
    assert report["summary"]["P0"] >= 3 and report["as_of"] == at(480 + 150 + 1) or report["as_of"] >= at(300)


def test_proof_blockers_written_on_the_intent_replace_the_unknown():
    world = make_world()
    stuck = world["webull_lego_order_outbox"][CHAIN][run_id(10)]
    stuck.update({"expiry_proof_blockers": ["order_still_listed_open", "holdings_changed"],
                  "expiry_proof_checked_at": at(700)})
    report = ta.build_report(rtdb=world)
    by = {f["code"]: f for f in report["findings"]}
    assert "EXPIRY_PROOF_UNKNOWN" not in by
    assert "order_still_listed_open, holdings_changed" in by["EXPIRY_PROOF_BLOCKED"]["text"]
    text = ta.render(report)
    assert "expiry proof blockers ['order_still_listed_open', 'holdings_changed']" in text


def test_a_healthy_export_has_no_findings_worse_than_info():
    report = ta.build_report(rtdb=make_world(stuck=False))
    assert report["summary"]["P0"] == 0, report["findings"]


@pytest.mark.parametrize("tamper,field,code", [
    (lambda w: w["webull_lego_rows"][run_id(2)].update({COL["qty"]: 2.03}), "decision", "ROW_EQUATION_MISMATCH"),
    (lambda w: w["webull_lego_rows"][run_id(4)].update({COL["value"]: 10308.02}), "V_n", "ROW_EQUATION_MISMATCH"),
    (lambda w: w["webull_lego_rows"][run_id(6)].update({COL["R"]: 1.0}), "R_n", "ROW_EQUATION_MISMATCH"),
    (lambda w: w["webull_lego_rows"][run_id(3)].update({COL["dA"]: 5.0}), "dA_before_fill", "ROW_EQUATION_MISMATCH"),
    (lambda w: w["webull_lego_rows"][run_id(11)].update({COL["qty"]: 2.0}), "gated_qty", "ROW_EQUATION_MISMATCH"),
    (lambda w: w["webull_lego_rows"][run_id(5)].update({COL["status"]: "READY_SELL", COL["side"]: "SELL"}),
     "decision", "ROW_EQUATION_MISMATCH"),
    (lambda w: w["webull_lego_state"][CHAIN]["execution_cashflow"]["finalized_runs"][run_id(4)].update({"delta_actual": 999.0}),
     "delta_actual", "FILL_EQUATION_MISMATCH"),
    (lambda w: w["webull_lego_state"][CHAIN]["execution_cashflow"]["finalized_runs"][run_id(7)].update({"previous_actual_cumulative": 1.0}),
     "chain_A", "FILL_EQUATION_MISMATCH"),
    (lambda w: w["webull_lego_state"][CHAIN]["execution_cashflow"]["finalized_runs"][run_id(7)].update({"holdings_after": 200.5}),
     "position_walk", "FILL_EQUATION_MISMATCH"),
    (lambda w: w["webull_lego_state"][CHAIN]["execution_cashflow"].update({"last_action_price": 1.0}),
     "state_last_action_price", "FILL_EQUATION_MISMATCH"),
])
def test_a_tampered_value_is_found_and_named(tamper, field, code):
    world = make_world()
    tamper(world)
    report = ta.build_report(rtdb=world)
    chain = report["rtdb"]["chains"][0]
    found = chain["rows"]["mismatch"]["items"] + chain["fills"]["mismatch"]["items"]
    assert any(m["field"] == field for m in found), found
    finding = next(f for f in report["findings"] if f["code"] == code)
    assert finding["level"] == "P0"


def test_a_recent_order_is_in_flight_and_is_not_a_page():
    world = make_world(stuck=False)
    world["webull_lego_order_outbox"][CHAIN][run_id(7)].update({"status": "SUBMITTED", "created_at": at(195)})
    report = ta.build_report(rtdb=world)
    by = {f["code"]: f for f in report["findings"]}
    assert by["ORDER_IN_FLIGHT"]["level"] == "P2" and "ORDER_UNRESOLVED" not in by and report["summary"]["P0"] == 0
    world["webull_lego_order_outbox"][CHAIN][run_id(7)]["created_at"] = at(100)         # the same order, 100 minutes on
    by = {f["code"]: f for f in ta.build_report(rtdb=world)["findings"]}
    assert by["ORDER_UNRESOLVED"]["level"] == "P0" and "EXPIRY_PROOF_UNKNOWN" not in by    # no refused cancel: no proof question


def test_a_row_missing_its_inputs_is_reported_not_a_crash():
    world = make_world()
    del world["webull_lego_rows"][run_id(3)][COL["gap"]]
    world["webull_lego_rows"][run_id(5)][COL["price"]] = "n/a"
    items = ta.build_report(rtdb=world)["rtdb"]["chains"][0]["rows"]["mismatch"]["items"]
    assert {(m["version"], m["field"]) for m in items} >= {(3, "inputs"), (5, "inputs")}


def test_ledger_breaks_are_reported():
    world = make_world()
    world["webull_lego_realized"][CHAIN]["cumulative_realized"] = -5.0
    world["webull_lego_realized"][CHAIN]["applied_seq"] = 2
    problems = ta.build_report(rtdb=world)["rtdb"]["chains"][0]["ledger"]["problems"]
    assert "sum(realized_delta) != cumulative_realized" in problems
    assert "applied_seq != number of applied fills" in problems


def test_missing_rows_and_uncommitted_rows_are_findings():
    world = make_world()
    del world["webull_lego_rows"][run_id(5)]
    world["webull_lego_rows"][run_id(8)]["committed"] = False
    codes = {f["code"] for f in ta.build_report(rtdb=world)["findings"]}
    assert {"ROWS_NOT_CONTIGUOUS", "ROWS_UNCOMMITTED"} <= codes


# ----------------------------------------------------------------------------- the order timeline

def test_the_unresolved_order_has_a_timeline_and_short_ids():
    report = ta.build_report(rtdb=make_world())
    (order,) = report["rtdb"]["chains"][0]["orders"]["unresolved"]
    assert order["run"] == run_id(10)[:8] and order["broker_id"] == STUCK_BROKER_ID[:8]
    assert [s["status"] for s in order["timeline"]] == [
        "PENDING_DISPATCH", "PLACING_UNKNOWN", "PENDING", "CANCEL_UNKNOWN", "MANUAL_RECONCILIATION_REQUIRED"]
    assert order["timeline"][3]["cancel_error"] == "CANCEL_REFUSED_NOT_OPERABLE"
    (halt,) = report["rtdb"]["chains"][0]["orders"]["halts"]
    assert halt["halt"] == STUCK_HALT[:8] and halt["set_by"] == "system:order-recovery"
    text = ta.render(report)
    assert STUCK_BROKER_ID not in text and run_id(10) not in text and "UNRESOLVED " + run_id(10)[:8] in text


def test_run_filter_keeps_only_that_orders_story():
    report = ta.build_report(rtdb=make_world(), run=run_id(2)[:8])
    orders = report["rtdb"]["chains"][0]["orders"]
    assert orders["n"] == 1 and orders["unresolved"] == []


# --------------------------------------------------------------------------------- Cloud Logging

def tick_entry(minutes, *, severity="INFO", business="ROW_COMMITTED", pipeline="TICK_OK", ms=800.0, errors=None,
               instance="inst-aaaaaaaaaaaaaaaa", revision="lego-tick-uat-00007-xyz", **extra):
    stamp = at(minutes)
    return {"timestamp": stamp, "severity": severity, "resource": {"labels": {"revision_name": revision}},
            "labels": {"instanceId": instance},
            "jsonPayload": {"event": "lego_tick_completed", "timestamp": stamp, "severity": severity, "http_status": 200,
                            "pipeline_status": pipeline, "business_status": business, "duration_ms": ms,
                            "correlation_id": hashlib.sha1(stamp.encode()).hexdigest()[:32], "errors": errors or [],
                            "environment": "UAT", "mode": "trade", **extra}}


def op_entry(minutes, phase, ms, outcome="ok", **extra):
    return {"timestamp": at(minutes), "labels": {"instanceId": "inst-aaaaaaaaaaaaaaaa"},
            "jsonPayload": {"event": "lego_operation", "phase": phase, "operation": phase, "outcome": outcome,
                            "duration_ms": ms, **extra}}


def test_the_log_digest_finds_gaps_error_runs_slow_ticks_and_broker_errors():
    entries = [tick_entry(m) for m in range(0, 8)]
    entries += [tick_entry(m) for m in range(20, 23)]                                   # a 13 minute silence
    entries += [tick_entry(m, severity="ERROR", business="RECONCILIATION_OVERDUE", pipeline="TICK_DEFERRED",
                           errors=[{"phase": "recovery", "type": "TimeoutError",
                                    "broker_error": {"operation": "positions", "code": None, "http_status": 504,
                                                     "request_id": "req-0000-aaaa-1111"}}]) for m in range(23, 26)]
    entries += [tick_entry(26, ms=38000.0, business="TICK_DEFERRED"), tick_entry(27)]
    entries += [op_entry(1, "sdk_positions", 900.0), op_entry(2, "sdk_positions", 4200.0, "error",
                                                              error_code="OPENAPI_TIMEOUT", http_status=504,
                                                              request_id="req-0000-bbbb-2222"),
                op_entry(3, "sdk_cancel", 300.0, "error", error_code="OPENAPI_ORDER_CANNOT_OPERATE", http_status=417,
                         request_id="req-0000-cccc-3333"), op_entry(4, "place", 1200.0)]
    entries += [{"timestamp": at(1), "httpRequest": {"status": 200, "latency": "1.250s"}, "labels": {"instanceId": "inst-b"}}]
    for minute in (4, 9):                                    # the SDK's own text lines, as Cloud Logging shows them
        dump = (f'2026-10-08 13:{30 + minute}:11,000 webull.core.client ERROR ServerException occurred. Request:{{\n'
                '  "_action_name": "/trading/assets/positions/list",\n  "_method": "GET"\n}')
        entries += [{"timestamp": at(minute), "textPayload": dump},
                    {"timestamp": at(minute), "textPayload": 'get_response exception. {\n  "error_code": "GATEWAY_TIMEOUT",\n'
                     f'  "error_msg": "",\n  "http_status": 504,\n  "request_id": "req-text-{minute}"\n}}'}]
    entries += [{"timestamp": at(5), "textPayload": "Retrying (Retry(total=9)) after connection broken"}]
    logs = ta.analyze_logs(entries)
    t = logs["ticks"]
    assert t["n"] == 16 and t["by_severity"]["ERROR"] == 3 and t["gap_count"] == 1 and t["gaps"][0]["seconds"] == 780
    assert [r["n"] for r in t["error_runs"]] == [3] and t["error_runs"][0]["types"] == {"TimeoutError": 3}
    assert t["near_budget"] == 1 and t["slowest"][0]["ms"] == 38000.0 and t["interval_s"] == 60.0
    assert [(s["status"], s["n"]) for s in t["segments"]] == [
        ("ROW_COMMITTED", 11), ("RECONCILIATION_OVERDUE", 3), ("TICK_DEFERRED", 1), ("ROW_COMMITTED", 1)]
    assert len(logs["instances"]) == 2 and list(logs["revisions"]) == ["lego-tick-uat-00007-xyz"]
    assert logs["http"]["by_status"] == {200: 1} and logs["http"]["latency_s"]["max"] == 1.25
    ops = logs["webull"]["operations"]
    assert ops["sdk_positions"]["errors"] == 1 and ops["sdk_positions"]["codes"] == {"OPENAPI_TIMEOUT": 1}
    assert ops["sdk_cancel"]["http"] == {417: 1} and ops["sdk_cancel"]["request_ids"] == ["req-0000-cccc-3333"]
    (broker,) = logs["webull"]["errors"]
    assert (broker["op"], broker["http"], broker["n"], broker["request_ids"]) == ("positions", 504, 3, ["req-0000-aaaa-1111"])
    (sdk,) = logs["webull"]["sdk_errors"]
    assert (sdk["route"], sdk["code"], sdk["http"], sdk["n"], sdk["request_ids"]) == (
        "/trading/assets/positions/list", "GATEWAY_TIMEOUT", 504, 2, ["req-text-4", "req-text-9"])
    codes = {f["code"] for f in ta.build_findings({"logs": logs})}
    assert {"TICK_GAPS", "TICK_NEAR_BUDGET", "TICKS_DEFERRED", "WEBULL_ERRORS", "WEBULL_SDK_ERRORS"} <= codes


def test_since_drops_older_logs():
    entries = [tick_entry(m) for m in range(0, 10)]
    assert ta.analyze_logs(entries, since=ta.parse_ts(at(5)))["ticks"]["n"] == 5


def test_timestamps_with_nanoseconds_and_offsets_parse():
    assert ta.parse_ts("2026-10-07T13:30:08.994599123Z") == datetime(2026, 10, 7, 13, 30, 8, 994599, tzinfo=UTC)
    assert ta.parse_ts("2026-10-09T01:00:00+07:00") == datetime(2026, 10, 8, 18, 0, tzinfo=UTC)
    assert ta.parse_ts("garbage") is None and ta.parse_ts(None) is None and ta.parse_ts("") is None


# --------------------------------------------------------------------------------- the deployment

def test_the_service_yaml_is_read_without_a_yaml_library_and_without_secrets():
    service = ta.parse_service_yaml(YAML)
    assert service["revision"] == "lego-tick-uat-00007-xyz" and service["generation"] == "7"
    assert (service["timeout_s"], service["concurrency"], service["cpu"], service["memory"], service["max_scale"]) == (
        45.0, 1.0, "0.3333", "512Mi", 1.0)
    assert service["env"]["FIREBASE_DB_URL"] == "https://example-rtdb.invalid"          # folded scalar
    assert service["env"]["LEGO_TRADING_WINDOW_END"] == "2026-10-12T20:00:00Z"
    assert "WEBULL_ACCOUNT_ID" not in service["env"] and service["secret_env"] == ["WEBULL_ACCOUNT_ID"]


def test_the_deploy_section_flags_missing_alerts_the_window_and_hides_secrets():
    logs = [tick_entry(m, severity="ERROR", business="RECONCILIATION_OVERDUE") for m in range(3)]
    report = ta.build_report(logs=logs, rtdb=make_world(), service_text=YAML)
    deploy = report["deploy"]
    assert deploy["alert_webhook_configured"] is False and deploy["window_days_left"] == pytest.approx(4.2, abs=0.6)
    assert deploy["env"]["LEGO_RELEASE_AUTHORIZATION"] == "<set>" and "WEBULL_ACCOUNT_ID" not in deploy["env"]
    by = {f["code"]: f for f in report["findings"]}
    assert "ALERTING_NOT_CONFIGURED" in by and "3 ERROR ticks" in by["ALERTING_NOT_CONFIGURED"]["text"]
    assert by["RELEASE_WINDOW"]["level"] == "P1" and "UAT_ONLY" in by
    assert report["rtdb"]["chains"][0]["rows"]["diff"]["source"] == "config"            # LEGO_DIFF from the YAML
    for secret in ("RELEASE-BINDING-SENTINEL", "ACCOUNT-SECRET-SENTINEL", "example-rtdb"):
        assert secret not in ta.render(report) and secret not in json.dumps(report, default=str)


@pytest.mark.parametrize("head,dirty,hashes,expected", [
    ("0123456789abcdef", False, {"backend_only": "deadbeefcafe"}, ("matches", None)),
    ("0123456789abcdef", False, {"backend_only": "x", "with_reader": "deadbeefcafe"}, ("matches", None)),
    ("0123456789abcdef", False, {"backend_only": "x", "with_reader": "y"}, ("CANDIDATE_MISMATCH", "P1")),
    ("fffffffffffffff0", False, {"backend_only": "x"}, ("CANDIDATE_NOT_COMPARED", "INFO")),
    ("0123456789abcdef", True, {"backend_only": "x"}, ("CANDIDATE_NOT_COMPARED", "INFO")),
])
def test_the_candidate_hash_is_compared_only_when_the_checkout_is_the_deployed_commit(
        monkeypatch, head, dirty, hashes, expected):
    monkeypatch.setattr(ta, "git_state", lambda repo: (head, dirty))
    monkeypatch.setattr(ta, "repo_candidate_hashes", lambda repo: hashes)
    report = ta.build_report(service_text=YAML, repo=".")
    code, level = expected
    if code == "matches":
        assert report["deploy"]["candidate_matches_repo"] is True
        assert not {"CANDIDATE_MISMATCH", "CANDIDATE_NOT_COMPARED"} & {f["code"] for f in report["findings"]}
    else:
        assert any(f["code"] == code and f["level"] == level for f in report["findings"])


def test_the_repo_hash_has_a_backend_only_variant(tmp_path, monkeypatch):
    from tools import candidate_manifest as manifest
    root = Path(__file__).resolve().parent
    monkeypatch.setattr(manifest, "STREAMLIT", tmp_path / "no-reader")
    expected = manifest.build_manifest()["candidate_hash"]
    monkeypatch.undo()
    hashes = ta.repo_candidate_hashes(root)
    assert hashes["backend_only"] == expected
    assert manifest.ROOT == root and manifest.STREAMLIT == root.parent / "lego-firebase-streamlit"     # restored


# ---------------------------------------------------------- the flight recorder -> the digest

TICK = "feedfacefeedfacefeedfacefeedface"


def test_a_recorded_incident_reads_back_as_the_cause_with_the_brokers_request_id(monkeypatch):
    """The acceptance case: write the trace with the real pipeline, then read it back from the export."""
    run, pending, _ = _zombie(monkeypatch, "cancel_expire")
    _clock(monkeypatch, PAST_MARGIN)
    _stub_broker(monkeypatch, holdings_after=9.0, detail=pending)                  # holdings moved
    cfg = lego_main.load_config()
    with tick_runtime.tick_scope(TICK):
        fr.bind(chain_key=lego_main.chain_key(cfg), symbol="AAPL", env="UAT", mode="trade")
        fr.webull_exchange(
            "cancel", "/trading/orders/cancel", "POST", 118.0, status=417, request_id="req-synthetic-417",
            body={"client_order_id": run}, error_info={"code": "OPENAPI_ORDER_CANNOT_OPERATE", "http": 417,
                                                       "msg": "The current status cannot be modified.",
                                                       "type": "ServerException"})
        results = _work()
        body = {"pipeline_status": "TICK_OK", "recovery": {"results": results}}
        from observability import emit_tick
        emit_tick(body, 200)
        assert fr.finish(body, 200)["written"] is True

    export = copy.deepcopy(FAKE_DB.store)
    report = ta.build_report(rtdb=export)
    traces = report["rtdb"]["traces"]
    assert traces["n"] == 1 and traces["proof"][0]["blockers"] == ["holdings_changed"]
    assert traces["proof"][0]["holdings"] == 9.0 and traces["proof"][0]["run"] == run[:8]
    (exchange,) = traces["exchanges"]
    assert (exchange["op"], exchange["status"], exchange["error"], exchange["request_ids"], exchange["mutation"]) == (
        "cancel", 417, "OPENAPI_ORDER_CANNOT_OPERATE", ["req-synthetic-417"], True)
    by = {f["code"]: f for f in report["findings"]}
    assert "holdings_changed" in by["EXPIRY_PROOF_BLOCKED"]["text"] and "EXPIRY_PROOF_UNKNOWN" not in by
    text = ta.render(report)
    assert "W11" in text and "holdings_changed" in text and "req-synthetic-417" in text
    assert "W00[claimed]" in traces["ticks"][0]["path"] and "W11[holdings_changed]" in traces["ticks"][0]["path"]
    only = ta.build_report(rtdb=export, run=run[:8])
    assert only["rtdb"]["traces"]["n"] == 1
    assert ta.build_report(rtdb=export, run="00000000")["rtdb"]["traces"]["n"] == 0


def test_recorded_decision_and_fill_equations_are_recomputed_from_the_trace():
    good = {"k": "eq", "n": "D08",
            "in": {"P_n": 49.5, "holdings": 200.0, "FIX_C": FIX_C, "DIFF": DIFF, "signal": 1, "P0": P0, "dp": 2, "inc": 0.01,
                   "strategy": "shannon_demon_lego_v2", "genesis": False, "A_prev": -96.0},
           "out": {"status": "READY_BUY", "side": "BUY", "qty": 2.02, "V_n": 9900.0, "gap": -100.0,
                   "R_n": FIX_C * math.log(49.5 / P0), "A_n": -96.0}}
    fill = {"k": "eq", "n": "W13",
            "in": {"FIX_C": FIX_C, "P_fill": 51.02, "P_acted": 49.52, "A_prev": -96.0},
            "out": {"delta_actual": FIX_C * (51.02 / 49.52 - 1), "actual_cumulative": -96.0 + FIX_C * (51.02 / 49.52 - 1),
                    "reference": 198.0, "excess": -96.0 + FIX_C * (51.02 / 49.52 - 1) - 198.0}}

    def check(*events):
        doc = {"at": at(1), "tick": TICK, "events": [{**e, "i": n, "t": 0} for n, e in enumerate(events)], "n": len(events)}
        return ta.analyze_traces({"webull_lego_trace": {"c": {"2026-10-08": {"k": doc}}}})["equations"]

    assert check(good, fill) == {"checked": 2, "mismatch": {"count": 0, "items": []}}
    bad_decision = copy.deepcopy(good)
    bad_decision["out"]["qty"] = 2.03
    bad_fill = copy.deepcopy(fill)
    bad_fill["out"]["excess"] += 1.0
    result = check(bad_decision, bad_fill)
    assert result["mismatch"]["count"] == 2
    assert {(m["node"], m["field"]) for m in result["mismatch"]["items"]} == {("D08", "qty"), ("W13", "excess")}


def test_since_limits_the_recorded_traces_too():
    def doc(minutes):
        return {"at": at(minutes), "tick": f"t{minutes:02d}" + "0" * 20, "events": [], "n": 0}

    db = {"webull_lego_trace": {"c": {"2026-10-08": {"a": doc(1), "b": doc(30), "c": doc(60)}}}}
    assert ta.analyze_traces(db)["n"] == 3
    assert ta.analyze_traces(db, since=ta.parse_ts(at(30)))["n"] == 2
    report = ta.analyze_rtdb({**make_world(), **db}, since=ta.parse_ts(at(31)))
    assert report["traces"]["n"] == 1


def test_recorder_health_counters_become_a_finding():
    db = {"webull_lego_trace": {}, "webull_lego_heartbeat": {"chain": {"at": at(1), "write_errors": 2, "flush_timeouts": 1}}}
    findings = ta.build_findings({"rtdb": {"chains": [], "traces": ta.analyze_traces(db)}, "logs": {}, "deploy": {}})
    assert any(f["code"] == "RECORDER_UNHEALTHY" and "write_errors" in f["text"] for f in findings)


# ------------------------------------------------------------------------------------------ live

def test_live_reads_the_heartbeat_and_the_newest_traces_and_follow_prints_each_once():
    day = "2026-10-08"
    for n in range(4):
        FAKE_DB.reference(f"webull_lego_trace/c1/{day}/{100000 + n}_tick{n:04d}").set({
            "at": at(n), "tick": f"tick{n:04d}" + "0" * 24, "http": 200, "pipe": "TICK_OK", "biz": "ROW_COMMITTED",
            "why": ["row_committed"], "path": "D00>D05>D08>D10", "n": 4, "events": []})
    FAKE_DB.reference(f"webull_lego_trace_days/c1/{day}").set(True)
    FAKE_DB.reference("webull_lego_heartbeat/c1").set({"at": at(3), "ticks": 40, "written": 4, "write_errors": 0})
    reader = lambda path: FAKE_DB.reference(path).get()                       # noqa: E731
    live = ta.read_live(reader, last=2)
    assert [t["tick"] for t in live["ticks"]] == ["tick0002", "tick0003"] and live["heartbeat"]["c1"]["ticks"] == 40
    text = ta.render_live(live)
    assert "heartbeat c1" in text and "D00>D05>D08>D10" in text
    printed = []
    rounds = iter(range(3))

    def sleep(_):
        if next(rounds) == 0:                                               # a new trace arrives between polls
            FAKE_DB.reference(f"webull_lego_trace/c1/{day}/100009_tick0009").set({
                "at": at(9), "tick": "tick0009" + "0" * 24, "http": 200, "pipe": "TICK_OK", "biz": "ROW_COMMITTED",
                "why": ["row_committed"], "path": "D00", "n": 1, "events": []})

    ta.follow(reader, last=2, sleep=sleep, out=printed.append, rounds=3)
    joined = "\n".join(printed)
    assert joined.count("tick0003") == 1 and joined.count("tick0009") == 1 and joined.count("tick0002") == 1
    assert ta.read_live(lambda path: None) == ta.analyze_traces({})


# ------------------------------------------------------------------------------------------- CLI

def test_the_cli_writes_a_digest_json_and_an_exit_code(tmp_path, capsys):
    paths = {}
    for name, value in (("logs.json", [tick_entry(m) for m in range(3)]), ("rtdb.json", make_world()),
                        ("clean.json", make_world(stuck=False))):
        paths[name] = tmp_path / name
        paths[name].write_text(json.dumps(value), encoding="utf-8")
    service = tmp_path / "service.yaml"
    service.write_text(YAML, encoding="utf-8")

    assert ta.main(["--logs", str(paths["logs.json"]), "--rtdb", str(paths["rtdb.json"]), "--yaml", str(service)]) == 0
    text = capsys.readouterr().out
    assert text.startswith("AUDIT DIGEST") and "ORDER_UNRESOLVED" in text and "== inputs" in text
    assert hashlib.sha256(paths["rtdb.json"].read_bytes()).hexdigest()[:16] in text

    assert ta.main(["--rtdb", str(paths["rtdb.json"]), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["P0"] >= 3

    assert ta.main(["--rtdb", str(paths["rtdb.json"]), "--fail-on", "P0"]) == 1
    capsys.readouterr()
    assert ta.main(["--rtdb", str(paths["clean.json"]), "--fail-on", "P0"]) == 0
    capsys.readouterr()
    with pytest.raises(SystemExit) as usage:
        ta.main([])
    assert usage.value.code == 2


def test_the_tool_is_read_only_and_standard_library_only():
    source = Path(ta.__file__).read_text(encoding="utf-8")
    for forbidden in (".set(", ".delete(", ".push(", ".transaction(", "import requests", "urllib", "socket", "http.client", "open(",
                      "write_text", "write_bytes", "subprocess.Popen", "os.system"):
        assert forbidden not in source, forbidden
    assert source.count(".reference(") == 1 and ".reference(path).get()" in source      # the one live read
    imports = {line.split()[1].split(".")[0] for line in source.splitlines() if line.startswith(("import ", "from "))}
    assert imports <= {"__future__", "argparse", "hashlib", "json", "math", "re", "statistics", "sys", "time",
                       "collections", "datetime", "decimal", "pathlib"}
