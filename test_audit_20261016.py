"""Audit of revision lego-tick-uat-00047-qix (2026-10-16T13:30Z..15:04Z), docs/AUDIT_20261016_TH.md.

UAT half-open for the broker-reject circuit and the two findings the digest missed.
No live broker I/O: the RTDB is the in-memory fake of conftest.py and every instant is passed in.
Fixtures are synthetic. The incident they are shaped like: three UAT SELLs came back FAILED with no
reason, the third tripped BROKER_REJECT_HALT, and nothing cleared it for the rest of the session;
a day earlier every decision tick had failed from 14:57Z to the close on the broker's `positions`.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

import broker_circuit as circuit
import decision_service
import execution_service as execution
import flight_recorder as fr
import lego_outbox as outbox
import main
import operator_halt
import webull_io
from config import RuntimeConfig
from conftest import FAKE_DB
from test_broker_reject_safety import NOW as DECISION_NOW, decision_setup
from tools import trace_audit as ta

UTC = timezone.utc
IDENT = "ident-uat"
SYMBOL = "UBER"
SCOPE = outbox.account_symbol_fence_key(IDENT, SYMBOL)
HALTED_AT = datetime(2026, 10, 16, 14, 22, 27, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    FAKE_DB.store.clear()
    webull_io.reset_clients()
    execution.configure(main)
    decision_service.configure(main)
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    yield
    FAKE_DB.store.clear()
    webull_io.reset_clients()


# ------------------------------------------------------------------------------------ helpers

def fence():
    return FAKE_DB.reference(f"{outbox.DISPATCH_LOCK_PATH}/{SCOPE}")


def seed_halt(at=HALTED_AT, **fields):
    doc = {"halted": True, "status": circuit.HALT, "halt_id": "h1", "halted_at": at.isoformat(),
           "consecutive_broker_rejects": circuit.MAX_CONSECUTIVE_BROKER_REJECTS, **fields}
    fence().update({circuit.KEY: doc})


def state():
    return circuit.status(IDENT, SYMBOL)


def outcome(run_id, status="FAILED", quantity="0"):
    """One terminal broker result for the order the fence currently holds."""
    fence().update({"inflight_run_id": run_id})
    intent = {"run_id": run_id, "chain_key": "chain", "symbol": SYMBOL,
              "runtime_identity_fingerprint": IDENT}
    return circuit.record_outcome(intent, {"status": status, "filled_quantity": quantity})


def release_fence():
    doc = fence().get()
    doc.pop("inflight_run_id", None)
    fence().set(doc)


def open_at(seconds, environment="UAT"):
    return circuit.try_half_open(IDENT, SYMBOL, environment=environment,
                                 now=HALTED_AT + timedelta(seconds=seconds))


# ------------------------------------------------------------------- cooldown and eligibility

def test_cooldown_doubles_per_failed_probe_and_stops_at_four_hours():
    assert [circuit.probe_cooldown_seconds(n) for n in range(7)] == [
        900, 1800, 3600, 7200, 14400, 14400, 14400]
    for junk in (None, -3, "x", 1.9e99, True):
        assert circuit.probe_cooldown_seconds(junk) in (900, 1800, 14400)
    assert circuit.probe_cooldown_seconds(None) == circuit.probe_cooldown_seconds(-3) == 900


def test_the_policy_constants_are_pinned():
    assert circuit.AUTO_PROBE_ENVIRONMENTS == frozenset({"UAT"})
    assert (circuit.PROBE_BASE_SECONDS, circuit.PROBE_MAX_SECONDS) == (900, 14400)
    assert circuit.MAX_CONSECUTIVE_BROKER_REJECTS == 3


def test_uat_opens_at_the_cooldown_and_not_one_second_before():
    seed_halt()
    assert open_at(899) == {}
    assert state()["halted"] is True
    opened = open_at(900)
    assert opened["status"] == circuit.HALF_OPEN and opened["halted"] is False


def test_opening_leaves_the_counter_one_short_and_records_the_probe():
    seed_halt()
    opened = open_at(1000)
    assert opened["consecutive_broker_rejects"] == circuit.MAX_CONSECUTIVE_BROKER_REJECTS - 1
    assert opened["probe_of_halt_id"] == "h1" and opened["probe_total"] == 1
    assert opened["last_probe_at"] == (HALTED_AT + timedelta(seconds=1000)).isoformat()
    assert state() == opened


def test_a_second_call_is_a_no_op_once_the_circuit_is_open():
    seed_halt()
    assert open_at(1000)
    before = state()
    assert open_at(1001) == {}
    assert state() == before


@pytest.mark.parametrize("environment", ["PROD", "prod", "Production", "STAGING", "", None,
                                         "uat", " UAT", "UAT "])        # config.py stores exactly "UAT"
def test_only_uat_ever_opens_by_itself(environment):
    seed_halt()
    before = state()
    assert open_at(10 ** 6, environment=environment) == {}
    assert state() == before


@pytest.mark.parametrize("identity,symbol,now", [
    ("", SYMBOL, HALTED_AT + timedelta(hours=1)),
    (IDENT, "", HALTED_AT + timedelta(hours=1)),
    (IDENT, SYMBOL, None),
    (IDENT, SYMBOL, datetime(2026, 10, 16, 16, 0)),            # naive: no zone, no guess
    (IDENT, SYMBOL, "2026-10-16T16:00:00Z"),
])
def test_unusable_inputs_keep_the_halt(identity, symbol, now):
    seed_halt()
    before = state()
    assert circuit.try_half_open(identity, symbol, environment="UAT", now=now) == {}
    assert state() == before


REFUSALS = {
    "an unresolved order holds the fence": lambda: fence().update({"inflight_run_id": "r1"}),
    "an operator halt is set": lambda: fence().update({"operator_halt": {"halted": True, "halt_id": "o1"}}),
    "an operator halt audit is pending": lambda: fence().update(
        {"operator_halt": {"halted": False, "audit_pending_event": {"event_id": "e1"}}}),
    "a dispatch worker holds a live lease": lambda: fence().update(
        {"owner": "worker", "lease_until": (HALTED_AT + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")}),
    "the dispatch lease is unreadable": lambda: fence().update({"owner": "worker", "lease_until": "soon"}),
    "the lease has no end at all": lambda: fence().update({"owner": "worker"}),
}


@pytest.mark.parametrize("why", sorted(REFUSALS))
def test_something_else_owning_the_fence_keeps_the_halt(why):
    seed_halt()
    REFUSALS[why]()
    before = fence().get()
    assert open_at(7200) == {}, why
    assert fence().get() == before


def test_a_lease_that_has_ended_does_not_block_the_probe():
    seed_halt()
    fence().update({"owner": "worker",
                    "lease_until": (HALTED_AT + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")})
    assert open_at(1000)["status"] == circuit.HALF_OPEN


@pytest.mark.parametrize("halted_at", [None, "yesterday", "2026-10-16T14:22:27",
                                       (HALTED_AT + timedelta(days=30)).isoformat()])
def test_a_missing_unreadable_naive_or_future_halted_at_keeps_the_halt(halted_at):
    seed_halt(halted_at=halted_at)
    before = state()
    assert open_at(10 ** 6) == {}                    # 11.6 days after HALTED_AT: the last one is still ahead
    assert state() == before


def test_a_circuit_that_is_not_halted_is_left_alone():
    fence().update({circuit.KEY: {"halted": False, "consecutive_broker_rejects": 1}})
    before = state()
    assert open_at(10 ** 6) == {}
    assert state() == before
    assert circuit.try_half_open(IDENT, "NOPE", environment="UAT", now=HALTED_AT) == {}


@pytest.mark.parametrize("halted", ["true", "yes", 1])
def test_only_a_circuit_that_says_halted_true_is_opened(halted):
    seed_halt(halted=halted)            # hand-edited or damaged data is not a halt this code may lift
    before = state()
    assert open_at(10 ** 6) == {}
    assert state() == before


def test_a_circuit_without_a_halted_flag_is_left_alone():
    fence().update({circuit.KEY: {"status": circuit.HALT, "halt_id": "h1",
                                  "halted_at": HALTED_AT.isoformat()}})
    before = state()
    assert open_at(10 ** 6) == {}
    assert state() == before


def test_only_the_call_whose_write_opened_the_circuit_gets_the_probe(monkeypatch):
    """Two workers read the same halt; the second one's transaction finds the circuit already open."""
    seed_halt()
    assert open_at(1000)                                    # the other worker got there first
    first = state()
    real, looks = circuit._may_probe, []

    def stale_read_then_real(doc, now):
        looks.append(1)
        return True if len(looks) == 1 else real(doc, now)  # the plain read still saw the halt
    monkeypatch.setattr(circuit, "_may_probe", stale_read_then_real)
    assert open_at(1001) == {}
    assert len(looks) >= 2                                  # the transaction looked again ...
    assert state() == first                                 # ... and wrote nothing


def test_another_account_or_symbol_is_never_opened():
    seed_halt()
    assert circuit.try_half_open("other-account", SYMBOL, environment="UAT",
                                 now=HALTED_AT + timedelta(hours=1)) == {}
    assert circuit.try_half_open(IDENT, "AAPL", environment="UAT",
                                 now=HALTED_AT + timedelta(hours=1)) == {}
    assert state()["halted"] is True


# --------------------------------------------------------------------------- what the probe decides

def test_a_failed_probe_halts_again_at_once_with_a_new_id_and_a_longer_wait():
    seed_halt()
    assert open_at(900)
    again = outcome("probe-1", "FAILED")
    assert again["halted"] is True and again["status"] == circuit.HALT
    assert again["halt_id"] == "probe-1" and again["probe_streak"] == 1
    assert again["consecutive_broker_rejects"] == 3
    release_fence()
    halted_at = datetime.fromisoformat(again["halted_at"])
    wait = circuit.probe_cooldown_seconds(1)
    assert wait == 1800
    assert circuit.try_half_open(IDENT, SYMBOL, environment="UAT",
                                 now=halted_at + timedelta(seconds=wait - 1)) == {}
    assert circuit.try_half_open(IDENT, SYMBOL, environment="UAT",
                                 now=halted_at + timedelta(seconds=wait))["status"] == circuit.HALF_OPEN


def test_the_wait_keeps_growing_while_probes_keep_failing():
    seed_halt()
    now = HALTED_AT
    for streak in range(1, 6):
        now = now + timedelta(seconds=circuit.probe_cooldown_seconds(streak - 1))
        assert circuit.try_half_open(IDENT, SYMBOL, environment="UAT", now=now), streak
        failed = outcome(f"probe-{streak}", "FAILED")
        release_fence()
        assert failed["probe_streak"] == streak and failed["halt_id"] == f"probe-{streak}"
        now = datetime.fromisoformat(failed["halted_at"])
    assert circuit.probe_cooldown_seconds(5) == circuit.PROBE_MAX_SECONDS
    assert state()["probe_total"] == 5


def test_a_filled_probe_clears_the_status_the_counter_and_the_streak():
    seed_halt(probe_streak=3)
    assert open_at(circuit.PROBE_MAX_SECONDS)
    cleared = outcome("probe-ok", "FILLED", "4.5")
    assert cleared["halted"] is False and cleared["status"] == "READY"
    assert cleared["consecutive_broker_rejects"] == 0 and cleared["probe_streak"] == 0
    release_fence()
    # the next trouble starts from the short wait again
    for n in range(3):
        outcome(f"new-{n}", "FAILED")
        release_fence()
    fresh = state()
    assert fresh["halted"] is True and fresh["probe_streak"] == 0
    halted_at = datetime.fromisoformat(fresh["halted_at"])
    assert circuit.try_half_open(IDENT, SYMBOL, environment="UAT",
                                 now=halted_at + timedelta(seconds=900))["status"] == circuit.HALF_OPEN


@pytest.mark.parametrize("status", ["CANCELLED", "EXPIRED"])
def test_a_probe_that_ends_without_a_fill_neither_clears_nor_halts(status):
    seed_halt()
    assert open_at(900)
    after = outcome("probe-zero", status, "0")
    assert after["halted"] is False and after["status"] == circuit.HALF_OPEN
    assert after["consecutive_broker_rejects"] == 2 and "probe_streak" not in after


def test_a_failed_probe_with_a_partial_fill_still_counts_as_a_reject():
    seed_halt()
    assert open_at(900)
    again = outcome("probe-partial", "FAILED", "0.5")
    assert again["halted"] is True and again["probe_streak"] == 1


def test_an_old_callback_cannot_move_a_half_open_circuit():
    seed_halt()
    assert open_at(900)
    fence().update({"inflight_run_id": "current"})
    stale = {"run_id": "older", "chain_key": "chain", "symbol": SYMBOL,
             "runtime_identity_fingerprint": IDENT}
    circuit.record_outcome(stale, {"status": "FAILED", "filled_quantity": "0"})
    assert state()["halted"] is False and state()["consecutive_broker_rejects"] == 2


def test_a_circuit_that_never_opened_keeps_the_shape_it_always_had():
    outcome("a", "FAILED")
    release_fence()
    outcome("b", "FILLED", "1")
    shaped = state()
    assert shaped["consecutive_broker_rejects"] == 0
    assert "probe_streak" not in shaped and "status" not in shaped


def test_an_operator_reset_still_works_and_zeroes_the_streak():
    seed_halt()
    assert open_at(900)
    outcome("probe-1", "FAILED")
    release_fence()
    assert state()["probe_streak"] == 1
    circuit.reset(IDENT, SYMBOL, expected_halt_id="probe-1", reason="reviewed", apply=True)
    reset = state()
    assert reset["halted"] is False and reset["status"] == "READY" and reset["probe_streak"] == 0
    with pytest.raises(ValueError, match="changed"):
        circuit.reset(IDENT, SYMBOL, expected_halt_id="probe-1", reason="again", apply=True)


def test_reset_says_why_it_refuses_a_half_open_circuit():
    seed_halt()
    assert open_at(900)
    with pytest.raises(ValueError, match="half-open, not halted.*nothing to reset"):
        circuit.reset(IDENT, SYMBOL, expected_halt_id="h1", reason="reviewed", apply=True)
    with pytest.raises(ValueError, match="half-open"):
        circuit.reset(IDENT, SYMBOL, expected_halt_id="anything", reason="reviewed")        # dry run too


def test_a_circuit_that_is_still_cooling_down_costs_no_write(monkeypatch):
    seed_halt()
    writes = []
    original = type(FAKE_DB.reference("x")).transaction
    monkeypatch.setattr(type(FAKE_DB.reference("x")), "transaction",
                        lambda self, fn: writes.append(self.parts) or original(self, fn))
    assert open_at(60) == {} and open_at(899) == {}
    assert writes == []
    assert open_at(900) and len(writes) == 1


def test_the_place_fence_follows_the_circuit():
    seed_halt()
    claim = outbox.claim_chain_dispatch(SCOPE, "worker")
    assert outbox.fence_chain_dispatch(SCOPE, "probe-1", "worker", claim["claim_token"]) is None
    outbox.release_chain_dispatch(SCOPE, "worker", claim["claim_token"])
    assert open_at(900)
    claim = outbox.claim_chain_dispatch(SCOPE, "worker")
    fenced = outbox.fence_chain_dispatch(SCOPE, "probe-1", "worker", claim["claim_token"])
    assert fenced and fenced["inflight_run_id"] == "probe-1"
    assert outcome("probe-1", "FAILED")["halted"] is True
    assert outbox.clear_chain_dispatch_inflight(SCOPE, "probe-1", "worker", claim["claim_token"])
    assert outbox.fence_chain_dispatch(SCOPE, "probe-2", "worker", claim["claim_token"]) is None


# ------------------------------------------------------------------- the decision stage end to end

def decision_scope():
    return outbox.account_symbol_fence_key(webull_io.runtime_identity_fingerprint(), "TSLA")


def seed_decision_halt(minutes_ago, **fields):
    FAKE_DB.reference(f"{outbox.DISPATCH_LOCK_PATH}/{decision_scope()}").update({circuit.KEY: {
        "halted": True, "status": circuit.HALT, "halt_id": "h-old", "consecutive_broker_rejects": 3,
        "halted_at": (DECISION_NOW - timedelta(minutes=minutes_ago)).isoformat(), **fields}})


def decision_circuit():
    return circuit.status(webull_io.runtime_identity_fingerprint(), "TSLA")


def test_uat_decision_after_the_cooldown_sends_exactly_the_probe(monkeypatch):
    runtime, cfg = decision_setup(monkeypatch, allow_fractional=True)
    seed_decision_halt(20)
    body, code = decision_service.run_decision(None, runtime, cfg)
    assert code == 200 and body["committed"], body
    assert not body.get("outbox_blocked")
    assert outbox.read_intent(main.chain_key(cfg), body["run_id"]) is not None
    assert len(outbox.list_actionable(main.chain_key(cfg))) == 1
    opened = decision_circuit()
    assert opened["halted"] is False and opened["status"] == circuit.HALF_OPEN
    assert opened["consecutive_broker_rejects"] == 2 and opened["probe_of_halt_id"] == "h-old"
    warning = FAKE_DB.reference("webull_lego_warnings/BROKER_REJECT_AUTO_PROBE").get()
    assert warning["probe_total"] == 1 and warning["halt_id"] == "h-old"


def test_uat_decision_inside_the_cooldown_stays_halted(monkeypatch):
    runtime, cfg = decision_setup(monkeypatch, allow_fractional=True)
    seed_decision_halt(5)
    body, code = decision_service.run_decision(None, runtime, cfg)
    assert code == 200 and body["committed"], body
    assert body["outbox_blocked"] == circuit.HALT
    assert outbox.list_actionable(main.chain_key(cfg)) == []
    assert decision_circuit()["halted"] is True and "probe_total" not in decision_circuit()
    assert FAKE_DB.reference("webull_lego_warnings/BROKER_REJECT_AUTO_PROBE").get() is None


def test_a_slot_with_nothing_to_trade_does_not_spend_the_probe(monkeypatch):
    runtime, cfg = decision_setup(monkeypatch, allow_fractional=False, holdings=13)
    seed_decision_halt(600)
    body, code = decision_service.run_decision(None, runtime, cfg)
    assert code == 200 and body["committed"], body
    assert body["outbox_blocked"] == circuit.HALT
    assert decision_circuit()["halted"] is True and "probe_total" not in decision_circuit()


def test_prod_decision_never_opens_the_circuit(monkeypatch):
    runtime, cfg = decision_setup(monkeypatch, allow_fractional=True)
    prod = dataclasses.replace(
        runtime, deployment=dataclasses.replace(runtime.deployment, environment="PROD"))
    # Let PROD reach the circuit check at all: the release gates are not what is under test.
    monkeypatch.setattr(RuntimeConfig, "allows_new_broker_mutation", property(lambda self: True))
    seed_decision_halt(60 * 24 * 30)
    body, code = decision_service.run_decision(None, prod, cfg)
    assert code == 200 and body["committed"], body
    assert body["outbox_blocked"] == circuit.HALT
    assert outbox.list_actionable(main.chain_key(cfg)) == []
    assert decision_circuit()["halted"] is True and "probe_total" not in decision_circuit()


def test_an_operator_halt_is_not_overridden_by_the_probe(monkeypatch):
    runtime, cfg = decision_setup(monkeypatch, allow_fractional=True)
    operator_halt.set_halt(webull_io.runtime_identity_fingerprint(), "TSLA", operator="alice",
                           reason="incident review", apply=True)
    seed_decision_halt(60)
    body, code = decision_service.run_decision(None, runtime, cfg)
    assert code == 200 and body["committed"], body
    assert body["outbox_blocked"] == "OPERATOR_HALT"
    assert outbox.list_actionable(main.chain_key(cfg)) == []
    assert decision_circuit()["halted"] is True


def test_observe_mode_does_not_probe(monkeypatch):
    runtime, cfg = decision_setup(monkeypatch, allow_fractional=True)
    observe = dataclasses.replace(
        runtime, operator=dataclasses.replace(runtime.operator, active=False))
    seed_decision_halt(60)
    body, code = decision_service.run_decision(None, observe, cfg)
    assert code == 200 and body["committed"], body
    assert decision_circuit()["halted"] is True and "probe_total" not in decision_circuit()


def test_the_recorder_notes_the_circuit_status_and_streak(monkeypatch):
    seen = []
    monkeypatch.setattr(fr, "node", lambda *args, **fields: seen.append((args, fields)))
    fr.decision_gates(True, None, {"halted": False, "status": circuit.HALF_OPEN, "probe_streak": 2,
                                   "consecutive_broker_rejects": 2}, {}, {}, None, {"สถานะ": "READY_SELL"})
    (args, fields), = seen
    assert args[0] == "D09" and fields["circuit_status"] == circuit.HALF_OPEN
    assert fields["probe_streak"] == 2 and fields["circuit_halted"] is False


# ------------------------------------------------- the audit digest: standing halt, long outage

LOG_T0 = datetime(2026, 10, 16, 13, 30, tzinfo=UTC)
POSITIONS_417 = [{"phase": "decision", "type": "ServerException", "broker_error": {
    "http_status": 417, "operation": "positions", "code": "OPENAPI_SYSTEM_ERROR"}}]


def stamp(minutes, seconds=0):
    return (LOG_T0 + timedelta(minutes=minutes, seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def tick(minutes, business="SLOT_CONSUMED", pipeline="TICK_OK", http=200, severity="INFO",
         decision=None, errors=None):
    return {"timestamp": stamp(minutes), "severity": severity,
            "jsonPayload": {"event": "lego_tick_completed", "business_status": business,
                            "pipeline_status": pipeline, "http_status": http,
                            "decision": decision or {}, "errors": errors or [], "duration_ms": 900.0,
                            "correlation_id": f"c{minutes}"}}


def failing(start, count):
    return [tick(start + i, business="ERROR", pipeline="TICK_DECISION_ERROR", http=503,
                 severity="ERROR", errors=POSITIONS_417) for i in range(count)]


def trace_doc(minutes, offset=-13):
    return {"at": stamp(minutes, offset), "pipe": "TICK_DECISION_ERROR", "biz": "ERROR", "http": 503,
            "events": [{"i": 0, "k": "n", "n": "D00"},
                       {"i": 1, "k": "wb", "op": "positions", "st": 417, "ms": 900.0, "t": 100,
                        "err": {"code": "OPENAPI_SYSTEM_ERROR"}}]}


def trace_db(minutes, day="2026-10-16"):
    return {"webull_lego_trace": {"CHAIN_x": {day: {f"k{m:04d}": trace_doc(m) for m in minutes}}}}


def codes(report, code):
    return [f for f in report["findings"] if f["code"] == code]


def halted_run(count=45, *, still=True):
    entries = [tick(0, business="ROW_COMMITTED", decision={"status": "READY_SELL"})]
    entries += [tick(i, business="BROKER_REJECT_HALT", severity="ERROR", decision={
        "status": "PASS_BROKER_REJECT_HALT" if i % 15 == 0 else "PASS_SLOT_CONSUMED"})
        for i in range(1, count + 1)]
    if not still:
        entries.append(tick(count + 1, business="ROW_COMMITTED", decision={"status": "READY_SELL"}))
    return entries


def test_a_standing_halt_in_the_logs_is_a_p1_finding():
    report = ta.build_report(logs=halted_run())
    halt = report["logs"]["ticks"]["broker_halt"]
    assert halt == {"ticks": 45, "since": stamp(1), "last": stamp(45), "slots_blocked": 3, "standing": True}
    (finding,) = codes(report, "BROKER_REJECT_HALT_ACTIVE")
    assert finding["level"] == "P1"
    assert "45 ticks and 3 slots" in finding["text"] and "ops.py reset-reject-halt" in finding["text"]


def test_a_halt_seen_only_in_the_logs_does_not_print_unknowns():
    (finding,) = codes(ta.build_report(logs=halted_run()), "BROKER_REJECT_HALT_ACTIVE")
    assert "None" not in finding["text"] and "since 2026-10-16T13:31:00Z" in finding["text"]


def test_a_halt_that_cleared_before_the_last_tick_is_not_reported_as_standing():
    report = ta.build_report(logs=halted_run(still=False))
    assert report["logs"]["ticks"]["broker_halt"]["standing"] is False
    assert codes(report, "BROKER_REJECT_HALT_ACTIVE") == []


def test_no_halt_ticks_means_no_halt_summary_and_no_finding():
    report = ta.build_report(logs=[tick(i) for i in range(10)])
    assert report["logs"]["ticks"]["broker_halt"] is None
    assert codes(report, "BROKER_REJECT_HALT_ACTIVE") == []


def halted_db(**fields):
    circuit_doc = {"halted": True, "halt_id": "ab" * 16, "halted_at": "2026-10-16T14:22:27.739883+00:00",
                   "consecutive_broker_rejects": 3, "status": "BROKER_REJECT_HALT", **fields}
    return {"webull_lego_state": {"CHAIN_x": {"updated_at": stamp(0), "version": 1, "dna_step": 1,
                                              "slot_id": "2026-10-16:0", "p0": 50.0, "prev_holdings": 1}},
            "webull_lego_order_dispatch_locks": {"SCOPE_synthetic_0123456789": {
                "broker_reject_circuit": circuit_doc}}}


def test_a_standing_halt_in_the_database_names_the_halt_and_the_probe_state():
    report = ta.build_report(rtdb=halted_db(probe_streak=2, probe_total=3,
                                            last_probe_at="2026-10-16T14:45:30+00:00"))
    (halt,) = report["rtdb"]["chains"][0]["orders"]["broker_circuits"]
    assert halt["state"] == "HALTED" and halt["halt"] == ("ab" * 16)[:8] and halt["since"] == "2026-10-16T14:22:27Z"
    assert (halt["probes"], halt["probe_streak"]) == (3, 2) and halt["last_probe_at"] == "2026-10-16T14:45:30Z"
    (finding,) = codes(report, "BROKER_REJECT_HALT_ACTIVE")
    assert finding["level"] == "P1" and "probes 3, failed-probe streak 2" in finding["text"]
    assert "since 2026-10-16T14:22:27Z" in finding["text"]


def test_a_circuit_that_is_not_halted_is_not_a_halt_finding():
    report = ta.build_report(rtdb=halted_db(halted=False, status="READY"))
    assert report["rtdb"]["chains"][0]["orders"]["broker_circuits"] == []
    assert codes(report, "BROKER_REJECT_HALT_ACTIVE") == [] and codes(report, "BROKER_REJECT_HALF_OPEN") == []


def test_a_half_open_circuit_is_an_info_finding_and_never_a_halt():
    report = ta.build_report(rtdb=halted_db(halted=False, status="HALF_OPEN", probe_total=2, probe_streak=1,
                                            last_probe_at="2026-10-16T14:45:30+00:00"))
    (c,) = report["rtdb"]["chains"][0]["orders"]["broker_circuits"]
    assert c["state"] == "HALF_OPEN" and (c["probes"], c["probe_streak"]) == (2, 1)
    (finding,) = codes(report, "BROKER_REJECT_HALF_OPEN")
    assert finding["level"] == "INFO" and "Nothing to reset" in finding["text"]
    assert codes(report, "BROKER_REJECT_HALT_ACTIVE") == []
    assert "BROKER-REJECT HALF-OPEN" in ta.render(report)


def test_the_audit_tool_and_the_circuit_agree_on_the_half_open_name():
    assert circuit.HALF_OPEN == "HALF_OPEN"
    assert ta.build_report(rtdb=halted_db(halted=False, status=circuit.HALF_OPEN))["rtdb"]["chains"][0][
        "orders"]["broker_circuits"][0]["state"] == "HALF_OPEN"


def test_every_standing_halt_gets_its_own_finding_even_without_an_id():
    db = halted_db()
    db["webull_lego_order_dispatch_locks"]["SCOPE_second_9876543210"] = {"broker_reject_circuit": {
        "halted": True, "consecutive_broker_rejects": 3, "halted_at": "2026-10-16T14:40:00+00:00"}}
    db["webull_lego_state"]["CHAIN_y"] = dict(db["webull_lego_state"]["CHAIN_x"])
    report = ta.build_report(rtdb=db)
    findings = codes(report, "BROKER_REJECT_HALT_ACTIVE")
    assert len(findings) == 2                                  # two chains list both locks: still one finding each
    assert sorted(f["evidence"]["halt"] or "" for f in findings) == ["", ("ab" * 16)[:8]]


def test_the_database_and_the_logs_report_one_standing_halt_not_two():
    report = ta.build_report(logs=halted_run(), rtdb=halted_db())
    assert len(codes(report, "BROKER_REJECT_HALT_ACTIVE")) == 1


@pytest.mark.parametrize("count,level", [(12, "P2"), (30, "P2"), (31, "P1"), (200, "P1")])
def test_a_run_of_failing_decisions_is_graded_by_how_long_it_lasted(count, level):
    report = ta.build_report(logs=failing(0, count))
    (finding,) = codes(report, "DECISION_OUTAGE")
    assert finding["level"] == level
    assert f"{count} decision ticks failed in a row" in finding["text"]
    assert "op=positions code=OPENAPI_SYSTEM_ERROR http=417" in finding["text"]


def test_a_run_of_failures_with_no_broker_error_is_not_blamed_on_the_broker():
    quiet = [tick(i, business="ERROR", pipeline="TICK_DECISION_ERROR", http=500, severity="ERROR")
             for i in range(40)]
    (finding,) = codes(ta.build_report(logs=quiet), "DECISION_OUTAGE")
    assert finding["level"] == "P1"
    assert "not the broker's weather" in finding["text"] and "resumes by itself" not in finding["text"]


def test_a_short_run_is_left_to_the_ordinary_broker_error_findings():
    report = ta.build_report(logs=failing(0, 10))                 # 9 min between first and last
    assert codes(report, "DECISION_OUTAGE") == []
    assert report["logs"]["outages"][0]["ticks"] == 10 and report["logs"]["outages"][0]["seconds"] == 540


def test_one_healthy_tick_ends_a_run_and_a_new_one_starts():
    report = ta.build_report(logs=failing(0, 12) + [tick(12)] + failing(13, 12))
    assert [w["ticks"] for w in report["logs"]["outages"]] == [12, 12]
    assert len(codes(report, "DECISION_OUTAGE")) == 2
    joined = ta.build_report(logs=failing(0, 12) + failing(12, 12))      # back to back: one run
    assert [w["ticks"] for w in joined["logs"]["outages"]] == [24]


def test_failures_on_every_other_tick_are_not_an_outage():
    report = ta.build_report(logs=[tick(i, business="ERROR", pipeline="TICK_DECISION_ERROR", http=503,
                                        severity="ERROR", errors=POSITIONS_417) if i % 2 == 0 else tick(i)
                                   for i in range(60)])
    assert all(w["ticks"] == 1 for w in report["logs"]["outages"])
    assert codes(report, "DECISION_OUTAGE") == []


def test_a_run_takes_its_cause_from_the_first_tick_that_names_one():
    nameless = tick(0, business="ERROR", pipeline="TICK_DECISION_ERROR", http=409, severity="ERROR")
    report = ta.build_report(logs=[nameless] + failing(1, 20))
    (finding,) = codes(report, "DECISION_OUTAGE")
    assert "op=positions code=OPENAPI_SYSTEM_ERROR http=417" in finding["text"]
    assert "not the broker's weather" not in finding["text"]


def test_a_run_keeps_the_first_cause_when_a_later_tick_names_another():
    place_504 = [{"phase": "decision", "type": "ServerException", "broker_error": {
        "http_status": 504, "operation": "place_order", "code": "GATEWAY_TIMEOUT"}}]
    later = [tick(10 + i, business="ERROR", pipeline="TICK_DECISION_ERROR", http=503, severity="ERROR",
                  errors=place_504 if i % 2 else []) for i in range(20)]
    (finding,) = codes(ta.build_report(logs=failing(0, 10) + later), "DECISION_OUTAGE")
    assert "op=positions code=OPENAPI_SYSTEM_ERROR http=417" in finding["text"]
    assert "GATEWAY_TIMEOUT" not in finding["text"]


def test_a_tick_seen_by_the_log_and_by_the_recorder_counts_once():
    report = ta.build_report(logs=failing(0, 1), rtdb=trace_db([0]))
    (run,) = ta.outage_runs(report["logs"]["failing_ticks"] + report["rtdb"]["traces"]["failing_ticks"])
    assert run["ticks"] == 1 and run["sources"] == ["logs", "traces"]


def test_the_slots_lost_are_the_run_length_in_fifteen_minute_slots():
    report = ta.build_report(logs=failing(0, 61))                         # 60 minutes first to last
    assert report["logs"]["outages"][0]["slots"] == 4


def test_the_flight_recorder_finds_the_outage_even_when_the_logs_are_not_given():
    report = ta.build_report(rtdb=trace_db(range(0, 303), day="2026-10-15"))
    (window,) = report["rtdb"]["traces"]["outages"]
    assert window["ticks"] == 303 and window["sources"] == ["traces"]
    assert (window["op"], window["code"], window["http"]) == ("positions", "OPENAPI_SYSTEM_ERROR", 417)
    (finding,) = codes(report, "DECISION_OUTAGE")
    assert finding["level"] == "P1" and "about 20 slots" in finding["text"]


def test_logs_and_traces_that_see_the_same_ticks_are_not_added_together():
    report = ta.build_report(logs=failing(0, 12), rtdb=trace_db(range(0, 12)))
    (finding,) = codes(report, "DECISION_OUTAGE")
    assert "12 decision ticks failed in a row" in finding["text"]
    assert finding["evidence"]["sources"] == ["logs", "traces"]


def error_records(minutes, offset=12):
    """webull_lego_errors: one record per caught exception, stamped when it was caught (tick start + ~12 s)."""
    return {"webull_lego_errors": {f"e{m:04d}": {"at": stamp(m, offset - 13), "type": "ServerException", "broker_error": {
        "operation": "positions", "code": "OPENAPI_SYSTEM_ERROR", "http_status": 417}} for m in minutes}}


def test_error_records_fill_the_hole_where_the_recorder_skipped_a_trace():
    holes = [m for m in range(0, 40) if m != 20]                 # one trace is missing, the errors node has all 40
    traces_only = ta.build_report(rtdb=trace_db(holes))
    assert [w["ticks"] for w in traces_only["rtdb"]["traces"]["outages"]] == [20, 19]
    both = ta.build_report(rtdb={**trace_db(holes), **error_records(range(0, 40))})
    (finding,) = codes(both, "DECISION_OUTAGE")
    assert "40 decision ticks failed in a row" in finding["text"]
    assert "op=positions code=OPENAPI_SYSTEM_ERROR http=417" in finding["text"]


def test_the_error_records_alone_are_enough_to_see_an_outage():
    report = ta.build_report(rtdb={"webull_lego_state": {}, **error_records(range(0, 15))})
    assert report["rtdb"]["error_records"]["n"] == 15
    (finding,) = codes(report, "DECISION_OUTAGE")
    assert finding["evidence"]["sources"] == ["errors"] and finding["level"] == "P2"


def test_error_records_without_a_time_are_ignored_and_since_applies():
    db = {"webull_lego_errors": {"a": {"at": "garbage"}, "b": "nonsense", "c": {"at": stamp(1)}, "d": {"at": stamp(50)}}}
    assert [t["at"] for t in ta._error_record_ticks(db)] == [stamp(1), stamp(50)]
    assert [t["at"] for t in ta._error_record_ticks(db, ta.parse_ts(stamp(10)))] == [stamp(50)]


def test_a_run_that_one_source_ends_and_the_other_continues_is_one_run():
    report = ta.build_report(logs=failing(0, 6), rtdb=trace_db(range(6, 12)))
    (finding,) = codes(report, "DECISION_OUTAGE")
    assert "12 decision ticks failed in a row" in finding["text"]
    assert finding["evidence"]["sources"] == ["logs", "traces"]


def test_the_digest_says_when_nobody_was_told():
    quiet = ta.build_report(logs=failing(0, 40))
    assert "no ALERT_WEBHOOK_* is configured" in codes(quiet, "DECISION_OUTAGE")[0]["text"]


def test_the_rendered_digest_shows_the_halt_and_the_outages():
    report = ta.build_report(logs=halted_run() + failing(60, 40), rtdb={**halted_db(), **trace_db(range(60, 100))})
    text = ta.render(report)
    assert "broker-reject halt: 45 ticks" in text and "decision outage" in text
    assert "BROKER-REJECT HALT abababab since 2026-10-16T14:22:27Z" in text
    assert "BROKER_REJECT_HALT_ACTIVE" in text and "DECISION_OUTAGE" in text
