"""The flight recorder against the real pipeline.

Two promises, both about the question the 2026-10-08 incident could not answer:

* a decision leaves its equation behind with every input, so an auditor can recompute the
  row that was committed; and
* an order that the DAY-expiry proof will not release says *which* condition is unmet --
  in the private trace, on the intent, and (as codes only) in the public warning -- instead of
  a ``logger.info`` that Cloud Logging never received.

The harnesses are the ones the pipeline's own suites use (the decision/execution cashflow
suite and the 2026-10-06 refused-cancel replay); only what is recorded is new here.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

import pytest

import decision_service
import flight_recorder as fr
import main
import observability
import tick_runtime
from conftest import FAKE_DB
from lego_state import STATE_PATH, chain_key
from test_continuous_runtime import setup_tick
from test_execution_confirmed_cashflow import (  # noqa: F401  (env is an autouse fixture)
    SLOT_0, SLOT_1, _cfg, _intent, _row, _run, _stub_broker, _work, env)
from test_incident_20261006 import PAST_MARGIN, _clock, _fenced_runs, _zombie
from test_main_pipeline import _fixed_now
from test_readiness_audit import typed_decision  # noqa: F401  (fixture)

TICK = "c0ffee00c0ffee00c0ffee00c0ffee00"


def _traced(call, tick=TICK):
    """Run ``call`` inside a tick scope; return its result and the events recorded meanwhile."""
    with tick_runtime.tick_scope(tick):
        result = call()
        return result, fr.snapshot()["events"]


def _node(events, node_id, tag=None):
    return next(e for e in events if e.get("n") == node_id and (tag is None or e.get("tag") == tag))


def _written():
    docs = FAKE_DB.reference(fr.TRACE_PATH).get() or {}
    return [doc for chain in docs.values() for day in chain.values() for doc in day.values()]


# ------------------------------------------------------------------------------ the decision

def test_the_decision_walks_the_flow_chart_and_its_equation_recomputes(monkeypatch):
    _traced(lambda: _run(monkeypatch, SLOT_0, 320.0, holdings=0.0), tick="a" * 32)       # genesis
    (body, code), events = _traced(lambda: _run(monkeypatch, SLOT_1, 330.0, holdings=8.0))
    assert code == 200 and body["status"] == "READY_BUY"
    steps = [e["n"] for e in events if e["k"] in ("n", "eq")]
    assert steps == ["D00", "D01", "D04", "D05", "D06", "D07", "D08", "D09", "D10", "D11"]

    equation = _node(events, "D08")
    given, out = equation["in"], equation["out"]
    value = given["holdings"] * given["P_n"]
    gap = value - given["FIX_C"]
    assert out["V_n"] == pytest.approx(value) and out["gap"] == pytest.approx(gap)
    assert (out["status"], out["side"]) == ("READY_BUY", "BUY") and gap < -given["DIFF"]
    assert out["qty"] == round(abs(gap) / given["P_n"], given["dp"])
    assert out["R_n"] == pytest.approx(given["FIX_C"] * math.log(given["P_n"] / given["P0"]))
    assert out["dA"] == 0 and out["A_n"] == given["A_prev"]     # a decision books nothing; the fill does
    assert out["acted"] is True and out["acted_price_next"] == given["P_acted"] == 320.0
    assert "FIX_C*ln(P/P0)" in equation["f"]

    row = _row(body["run_id"])                                   # what was committed is what was recorded
    for key, column in (("status", "สถานะ"), ("qty", "จำนวนสั่ง (หุ้น)"), ("V_n", "มูลค่าพอร์ต (USD)"),
                        ("gap", "ส่วนต่างเป้าหมาย (USD)"), ("R_n", "Rₙ อ้างอิง (USD)"),
                        ("dA", "ΔAₙ ต่อสเต็ป (USD)"), ("A_n", "Aₙ สะสม (USD)"),
                        ("E_n", "Eₙ ส่วนเกินสะสม (USD)")):
        assert out[key] == row[column], key
    assert _node(events, "D10")["x"]["run_id"] == body["run_id"] == _node(events, "D11")["x"]["run_id"]
    assert _node(events, "D06")["x"]["price"] == 330.0 and _node(events, "D05")["x"]["market_ordinal"] == 1


def test_the_genesis_row_records_that_it_has_no_anchor_yet(monkeypatch):
    (body, _), events = _traced(lambda: _run(monkeypatch, SLOT_0, 320.0, holdings=0.0))
    given = _node(events, "D08")["in"]
    assert given["genesis"] is True and given["P0"] is None and given["A_prev"] is None
    assert _node(events, "D08")["out"]["R_n"] == 0 and body["step"] == 0


def test_the_commit_guard_that_refuses_a_slot_is_recorded_after_the_equation(monkeypatch):
    _run(monkeypatch, SLOT_0, 320.0, holdings=0.0)
    (body, code), events = _traced(lambda: _run(monkeypatch, SLOT_0, 320.0, holdings=0.0))
    assert code == 200 and body["pipeline_status"] == "SLOT_CONSUMED"
    refused = _node(events, "D10")
    assert refused["tag"] == "SLOT_CONSUMED" and refused["ok"] is False
    assert [e["n"] for e in events if e["k"] in ("n", "eq")][-2:] == ["D09", "D10"]
    assert _node(events, "D08")["out"]["status"] == "READY_BUY"       # decided, then refused at Step 18


@pytest.mark.parametrize("field,node_id,tag", [
    (None, "D03", "PASS_SLOT_CONSUMED"),
    ("dna_fingerprint", "D08", "DNA_DRIFT"),
    ("calendar_fingerprint", "D05", "CALENDAR_DRIFT"),
    ("runtime_identity_fingerprint", "D01", "IDENTITY_ERROR")])
def test_every_refusal_of_the_decision_stops_at_its_own_node(typed_decision, field, node_id, tag):
    run, cfg, first, calls = typed_decision
    if field:
        FAKE_DB.reference(f"{STATE_PATH}/{chain_key(cfg)}").update({field: "changed"})
    (body, code), events = _traced(run)
    stop = [e for e in events if e.get("n") == node_id][-1]
    assert stop["tag"] == tag and stop["ok"] is (node_id == "D03")
    assert events[-1] is stop or events[-1]["n"] == node_id            # nothing was recorded after it
    assert not any(e.get("n") in {"D10", "D11"} for e in events)       # and nothing was committed


def test_a_v2_decision_floors_to_the_increment_and_the_trace_says_so(typed_decision, monkeypatch):
    run, cfg, first, calls = typed_decision
    moment = datetime(2026, 9, 14, 13, 45, 5, tzinfo=timezone.utc)
    stamp = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(decision_service, "datetime", _fixed_now(moment))
    monkeypatch.setattr(decision_service, "fetch_snapshot", lambda *a: {
        "price": 260.0, "holdings": 9, "captured_at": stamp, "quote_time": stamp})
    (body, code), events = _traced(run)
    assert code == 200 and body["status"] == "READY_BUY"
    given, out = _node(events, "D08")["in"], _node(events, "D08")["out"]
    gap = given["holdings"] * given["P_n"] - given["FIX_C"]
    assert given["strategy"].endswith("_v2") and given["inc"] == 1.0
    assert out["gap"] == pytest.approx(gap) == pytest.approx(-660.0)
    assert out["qty"] == math.floor(abs(gap) / given["P_n"] / given["inc"]) * given["inc"] == 2.0
    assert out["qty"] != round(abs(gap) / given["P_n"], given["dp"])    # not the legacy rounding


# ------------------------------------------------------------- the incident of 2026-10-08

def _listed(monkeypatch, run_id):
    monkeypatch.setattr(main, "fetch_open_orders", lambda tc, s: [
        {"client_order_id": run_id, "symbol": "AAPL", "status": "PENDING"}])


def _unreadable(monkeypatch, _run_id):
    def down(tc, symbol):
        raise TimeoutError("open orders unavailable")
    monkeypatch.setattr(main, "fetch_open_orders", down)


@pytest.mark.parametrize("holdings,arrange,blockers,facts", [
    (9.0, None, ["holdings_changed"], {"holdings": 9.0, "decision_holdings": 8.0, "listed": False}),
    (8.0, _listed, ["order_still_listed_open"], {"listed": True, "open_orders": 1}),
    (8.0, _unreadable, ["reads_failed:TimeoutError"], {"reason": "TimeoutError: open orders unavailable"}),
])
def test_an_unmet_expiry_proof_names_the_condition_in_trace_intent_and_warning(
        monkeypatch, holdings, arrange, blockers, facts):
    run_id, pending, _ = _zombie(monkeypatch, "cancel_expire")
    _clock(monkeypatch, PAST_MARGIN)
    _stub_broker(monkeypatch, holdings_after=holdings, detail=pending)
    if arrange:
        arrange(monkeypatch, run_id)

    results, events = _traced(_work)

    held = results[0]
    assert held["status"] == "CANCEL_UNKNOWN" and _fenced_runs() == {run_id}    # behaviour is unchanged
    assert held["expiry_proof_blockers"] == blockers                      # the worker result says it
    stored = _intent(run_id)
    assert stored["expiry_proof_blockers"] == blockers                    # so does the intent
    assert datetime.fromisoformat(stored["expiry_proof_checked_at"])

    proof = _node(events, "W11")                                          # and the private trace
    assert proof["ok"] is False and blockers[0] in proof["tag"]
    detail = proof["x"]
    assert detail["run_id"] == run_id
    for key, expected in facts.items():
        assert expected == detail[key] or (key == "reason" and expected in detail[key]), (key, detail)
    if "blockers" in detail:
        assert detail["blockers"] == blockers
    warned = [e for e in events if e["k"] == "wn" and e["kind"] == "expiry_proof_pending"]
    assert warned and warned[0]["x"]["blockers"] == blockers

    public = FAKE_DB.reference("webull_lego_warnings/expiry_proof_pending").get()
    assert public and public["blockers"] == blockers                      # codes only, readable by anyone
    assert not {"holdings", "decision_holdings", "open_orders", "listed", "tolerance"} & set(public)


def test_a_proven_release_is_recorded_with_the_transition(monkeypatch):
    run_id, pending, _ = _zombie(monkeypatch, "cancel_expire")
    _clock(monkeypatch, PAST_MARGIN)
    _stub_broker(monkeypatch, holdings_after=8.0, detail=pending)
    results, events = _traced(_work)
    assert results[0]["status"] == "EXPIRED"
    assert _node(events, "W11")["tag"] == "released"
    moved = [e for e in events if e["k"] == "tr" and e["run"] == run_id]
    assert moved and moved[-1]["to"] == "EXPIRED" and moved[-1]["from"] == "CANCEL_UNKNOWN"
    assert not _intent(run_id).get("expiry_proof_blockers")


def test_blockers_from_an_earlier_tick_do_not_outlive_the_release(monkeypatch):
    run_id, pending, _ = _zombie(monkeypatch, "cancel_expire")
    _clock(monkeypatch, PAST_MARGIN)
    _stub_broker(monkeypatch, holdings_after=9.0, detail=pending)
    assert _work()[0]["expiry_proof_blockers"] == ["holdings_changed"]
    assert _intent(run_id)["expiry_proof_blockers"] == ["holdings_changed"]
    _stub_broker(monkeypatch, holdings_after=8.0, detail=pending)         # the position is back where it was
    assert _work()[0]["status"] == "EXPIRED"
    stored = _intent(run_id)
    assert stored["status"] == "EXPIRED" and not stored.get("expiry_proof_blockers")
    assert datetime.fromisoformat(stored["expiry_proof_checked_at"])


def test_the_worker_nodes_of_a_held_order_read_as_a_route_through_the_chart(monkeypatch):
    run_id, pending, _ = _zombie(monkeypatch, "cancel_expire")
    _clock(monkeypatch, PAST_MARGIN)
    _stub_broker(monkeypatch, holdings_after=9.0, detail=pending)
    cfg = _cfg()
    with tick_runtime.tick_scope(TICK):
        fr.bind(chain_key=main.chain_key(cfg), symbol="AAPL", env="Test (UAT)", mode="trade")
        results = _work()
        body = {"pipeline_status": "TICK_OK", "recovery": {"results": results}}
        observability.emit_tick(body, 200)
        outcome = fr.finish(body, 200)
    assert outcome["written"] is True and "unresolved_intent" in outcome["reasons"]
    (doc,) = _written()
    assert doc["path"].startswith("W00[claimed]>W10[") and "W11[holdings_changed]" in doc["path"]
    assert doc["res"][0]["expiry_proof_blockers"] == ["holdings_changed"]
    assert doc["runs"] == [run_id] and doc["chain"] == main.chain_key(cfg)


# --------------------------------------------------------------------- the deployed entrypoint

def test_lego_tick_writes_one_private_record_of_a_held_order_and_returns_the_same_body(
        monkeypatch, capsys):
    held = {"results": [{"run_id": "old", "status": "CANCEL_UNKNOWN", "broker_status": "PENDING",
                         "expiry_proof_blockers": ["holdings_changed"],
                         "expiry_proof_checked_at": "2026-10-08T21:30:07+00:00"}]}
    setup_tick(monkeypatch, held)
    monkeypatch.setenv("WEBULL_ENV", "UAT")
    body, code = main.lego_tick(None)
    assert code == 200 and body["business_status"] == "WAITING_RECONCILIATION"
    printed = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert [e["event"] for e in printed] == ["lego_tick_completed"]       # the recorder never prints
    assert printed[0]["execution"][0]["expiry_proof_blockers"] == ["holdings_changed"]

    (doc,) = _written()
    assert doc["tick"] == body["correlation_id"] and doc["http"] == 200
    assert doc["biz"] == "WAITING_RECONCILIATION" and doc["env"] == "UAT" and doc["mode"] == "trade"
    assert doc["sym"] == "AAPL" and doc["why"] == ["boot", "unresolved_intent"]
    assert doc["res"][0]["expiry_proof_blockers"] == ["holdings_changed"]
    assert doc["boot"]["env"]["WEBULL_ENV"] == "UAT" and "WEBULL_ACCOUNT_ID" not in doc["boot"]["env"]
    beat = FAKE_DB.reference(f"{fr.HEARTBEAT_PATH}/{doc['chain']}").get()
    assert beat["last_trace"].endswith(f"{doc['at'][11:13]}{doc['at'][14:16]}{doc['at'][17:19]}_{doc['tick'][:8]}")

    # The same unresolved order a moment later adds nothing new to read.
    main.lego_tick(None)
    assert len(_written()) == 1 and fr._STATS["skipped_dup"] == 1


def test_a_recorder_that_cannot_write_never_changes_the_tick(monkeypatch, capsys):
    setup_tick(monkeypatch, {"results": [{"run_id": "old", "status": "CANCEL_UNKNOWN"}]})
    expected, expected_code = main.lego_tick(None)
    capsys.readouterr()
    FAKE_DB.store.clear()
    fr.reset_state()

    class Unreachable:                  # only the recorder's database fails; the tick's own works
        @staticmethod
        def reference(path):
            raise ConnectionError("rtdb unreachable")

    monkeypatch.setattr(fr, "db", Unreachable)
    body, code = main.lego_tick(None)
    assert code == expected_code and body["business_status"] == expected["business_status"]
    assert body["pipeline_status"] == expected["pipeline_status"]
    printed = [line for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert len(printed) == 1 and json.loads(printed[0])["event"] == "lego_tick_completed"
