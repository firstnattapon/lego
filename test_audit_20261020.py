"""Audit 2026-10-20 (docs/AUDIT_20261020_TH.md).

Origin: revision lego-tick-uat-00048-kap (main@e039b48), UAT session 2026-10-19 13:30Z-20:47Z and the
RTDB export of 2026-10-20T02:46Z. Every number below is a measurement from that evidence.

S1  the flight recorder dropped the ticks that explain an order. It wrote a notable tick only with 12 s of
    tick budget left (1 s only for a tick that sent or cancelled an order), and the ticks that matter are
    exactly the ones that end because the budget ran out: 3 TICK_DEFERRED ticks (1.85 s, 2.64 s and 11.58 s
    left) and 3 of the 4 instrument-outage ticks (3.86 s, 2.81 s and 6.20 s left) never reached
    webull_lego_trace (heartbeat skipped_budget = 6). 23 of the 26 committed rows had a recorded equation.
S2  a standing BROKER_REJECT_HALT was an ERROR line on every tick (391 of 438 ticks, 48 of them after the
    close). The monitoring health policy pages on severity>=ERROR, so it would page every five minutes all
    night. It now follows the MANUAL_RECONCILIATION_REQUIRED cadence: the first 180 s of each hour of halt.
N1  tools/trace_audit counted none of this: it now says when the recorder skipped ticks, when a committed row
    has no recorded equation, and when the log export ends long before the database's last heartbeat.
N3  deploy/continuous-uat.sh could not run the whole-share control (it hard-coded LEGO_ALLOW_FRACTIONAL=true),
    and that control is a one-way door for a chain: 2 places -> 0 places is a tightening, the way back is not.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import flight_recorder as fr
import main
import observability
from conftest import FAKE_DB
from lego_outbox import DISPATCH_LOCK_PATH, account_symbol_fence_key
from lego_state import _capability_is_tightening
from test_continuous_runtime import setup_tick
from test_flight_recorder import body, tick, traces
from test_trace_audit import CHAIN, at, make_world, run_id, tick_entry
from tools import trace_audit as ta

ROOT = Path(__file__).parent
UTC = timezone.utc


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    FAKE_DB.store.clear()
    for name in ("LEGO_TRACE_LEVEL", "LEGO_TRACE_BODIES", "LEGO_TRACE_RETENTION_DAYS"):
        monkeypatch.delenv(name, raising=False)
    yield
    FAKE_DB.store.clear()


# --------------------------------------------------------------------------------------------
# S1: a tick the recorder decided to keep is written unless less than one second is left.
# Seconds left when each tick ended, from lego_operation.remaining_budget_ms in the Cloud Run log.
# --------------------------------------------------------------------------------------------

DEFERRED = [("13:49", 1.85), ("14:45", 2.64), ("15:30", 11.58)]
OUTAGE = [("13:45", 3.86), ("13:46", 2.81), ("13:47", 6.20)]


@pytest.mark.parametrize("label,left", DEFERRED)
def test_a_tick_deferred_for_lack_of_budget_still_reaches_the_trace(monkeypatch, label, left):
    monkeypatch.setattr(fr, "_remaining", lambda: left)
    with tick():
        fr.node("D08", "READY_SELL")
        fr.node("D10", "committed")
        result = fr.finish(body(pipeline_status="TICK_DEFERRED", business_status="TICK_DEFERRED"), 200)
    assert result["written"] is True and "deferred" in result["reasons"], label
    assert len(traces()) == 1 and fr._STATS["skipped_budget"] == 0


@pytest.mark.parametrize("label,left", OUTAGE)
def test_a_tick_lost_to_a_broker_outage_still_reaches_the_trace(monkeypatch, label, left):
    monkeypatch.setattr(fr, "_remaining", lambda: left)
    with tick():
        result = fr.finish(body(pipeline_status="TICK_DECISION_ERROR", business_status="ERROR"), 503)
    assert result["written"] is True and "error" in result["reasons"], label
    assert len(traces()) == 1 and fr._STATS["skipped_budget"] == 0


def test_a_committed_row_keeps_its_equation_with_three_seconds_left(monkeypatch):
    monkeypatch.setattr(fr, "_remaining", lambda: 3.0)
    with tick():
        fr.node("D08", "READY_SELL")
        result = fr.finish(body(), 200)                              # ROW_COMMITTED
    assert result["written"] is True and "row_committed" in result["reasons"]


def test_an_unresolved_order_tick_and_a_probe_warning_are_kept_with_three_seconds_left(monkeypatch):
    monkeypatch.setattr(fr, "_remaining", lambda: 3.0)
    with tick():
        assert fr.finish(body(business_status="WAITING_RECONCILIATION"), 200)["written"] is True
    fr.reset_state()
    FAKE_DB.store.clear()
    with tick():
        fr.flag("warning")                      # what _record_warning("BROKER_REJECT_AUTO_PROBE") sets
        result = fr.finish(body(business_status="BROKER_REJECT_HALT"), 200)
    assert result["written"] is True and "warning" in result["reasons"]


def test_a_standing_halt_tick_below_twelve_seconds_is_still_skipped(monkeypatch):
    """The 15-minute overnight repeat of a halt is not evidence: it keeps the old floor."""
    monkeypatch.setattr(fr, "_remaining", lambda: 3.0)
    with tick():
        fr.node("D02", "MARKET_CLOSED")
        result = fr.finish(body(business_status="BROKER_REJECT_HALT"), 200)
    assert result["skipped"] == "budget" and traces() == {}
    assert fr._STATS["skipped_budget"] == 1


@pytest.mark.parametrize("business", ["TICK_DEFERRED", "ERROR", "ROW_COMMITTED"])
def test_nothing_is_written_with_less_than_one_second_left(monkeypatch, business):
    monkeypatch.setattr(fr, "_remaining", lambda: 0.5)
    with tick():
        result = fr.finish(body(business_status=business), 200)
    assert result["skipped"] == "budget" and traces() == {}


def test_an_idle_tick_still_needs_twelve_seconds_for_its_heartbeat(monkeypatch):
    fr._STATS["booted"] = True                                       # no "boot" reason: a plain idle tick
    monkeypatch.setattr(fr, "_remaining", lambda: 3.0)
    with tick():
        assert fr.finish(body(business_status="SLOT_CONSUMED"), 200)["skipped"] == "idle"
    assert FAKE_DB.reference(fr.HEARTBEAT_PATH).get() is None
    monkeypatch.setattr(fr, "_remaining", lambda: 20.0)
    with tick():
        fr.finish(body(business_status="SLOT_CONSUMED"), 200)
    assert FAKE_DB.reference(fr.HEARTBEAT_PATH).get()


def test_the_evidence_reasons_are_reasons_the_recorder_can_actually_emit():
    emitted = set(fr._REASON_BY_BIZ.values()) | {"mutation", "error", "webull_error", "warning"}
    assert fr._EVIDENCE_REASONS <= emitted
    assert {"mutation", "row_committed", "deferred", "error"} <= fr._EVIDENCE_REASONS
    assert not {"halt", "boot", "level_all", "biz_other"} & fr._EVIDENCE_REASONS


def test_the_floor_for_evidence_stays_inside_the_cloud_run_limit():
    # deadline 35 s (tick_runtime.tick_scope) + one time-boxed write must stay under the 45 s timeout.
    assert 0 < fr.FLUSH_MIN_BUDGET_MUTATION_SECONDS < fr.FLUSH_MIN_BUDGET_SECONDS
    assert 35.0 - fr.FLUSH_MIN_BUDGET_MUTATION_SECONDS + fr.FLUSH_TIMEBOX_SECONDS < 45.0


# --------------------------------------------------------------------------------------------
# S2: a standing broker-reject halt pages when it begins and once an hour after, not every minute.
# 2026-10-19 the halt of c703f30b began at 19:00:19Z; the other 390 halted ticks were ERROR too.
# --------------------------------------------------------------------------------------------

HALT_AT = datetime(2026, 10, 19, 19, 0, 19, tzinfo=UTC)


def halt_event(capsys, offset, halted_at=HALT_AT.isoformat(), **over):
    payload = {"pipeline_status": "TICK_OK", "broker_reject_halted": True,
               "decision": {"pipeline_status": "MARKET_CLOSED"}, **over}
    if halted_at is not ...:
        payload["broker_reject_halted_at"] = halted_at
    observability.emit_tick(payload, 200, now=HALT_AT + timedelta(seconds=offset))
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("offset,severity", [
    (1, "ERROR"), (60, "ERROR"), (179, "ERROR"), (180, "INFO"), (1000, "INFO"), (3599, "INFO"),
    (3600, "ERROR"), (3779, "ERROR"), (3780, "INFO"), (7230, "ERROR"), (86400 + 30, "ERROR"), (86400 + 600, "INFO")])
def test_a_standing_broker_halt_pages_when_it_begins_and_every_hour_after(capsys, offset, severity):
    event = halt_event(capsys, offset)
    assert event["business_status"] == "BROKER_REJECT_HALT"
    assert event["severity"] == severity
    assert event["halt_since"] == HALT_AT.isoformat() and event["reconciliation_paused"] is False


@pytest.mark.parametrize("halted_at", [None, "", "garbage", "2026-10-19T19:00:19", 17.5,
                                       "2026-10-20T19:00:19+00:00", ...])
def test_an_unreadable_or_future_halt_time_pages_instead_of_going_quiet(capsys, halted_at):
    """Unknown means unknown: the old behaviour (ERROR on every tick) is the safe fallback."""
    event = halt_event(capsys, 900, halted_at=halted_at)
    assert event["business_status"] == "BROKER_REJECT_HALT" and event["severity"] == "ERROR"


def test_the_whole_night_pages_three_ticks_an_hour_not_every_tick(capsys):
    ticks, errors, moment = 0, 0, HALT_AT + timedelta(seconds=45)
    expected = 0
    while moment < HALT_AT + timedelta(hours=7, minutes=43):         # 19:00:19Z -> 02:43Z, one tick a minute
        offset = (moment - HALT_AT).total_seconds()
        event = halt_event(capsys, offset)
        ticks += 1
        errors += event["severity"] == "ERROR"
        expected += offset % 3600 < 180
        moment += timedelta(seconds=60)
    assert ticks == 463 and errors == expected
    assert 3 <= errors <= 3 * 8 and errors / ticks < 0.06           # was 463 of 463


def test_other_error_statuses_keep_their_severity(capsys):
    failed = {"pipeline_status": "TICK_OK", "dispatch": {"results": [{"status": "FAILED", "broker_status": "FAILED"}]}}
    observability.emit_tick(failed, 200, now=HALT_AT + timedelta(seconds=900))
    event = json.loads(capsys.readouterr().out)
    assert event["business_status"] == "BROKER_ORDER_FAILED" and event["severity"] == "ERROR"
    assert observability.severity_for("BROKER_REJECT_HALT") == "ERROR"           # no age known: page
    assert observability.severity_for("OPERATOR_HALT") == "WARNING"
    assert observability.severity_for("MANUAL_RECONCILIATION_REQUIRED") == "ERROR"
    assert observability.severity_for("MANUAL_RECONCILIATION_REQUIRED", paused=True) == "INFO"


def seed_halt(halted_at, *, identity="identity", symbol="AAPL"):
    FAKE_DB.reference(f"{DISPATCH_LOCK_PATH}/{account_symbol_fence_key(identity, symbol)}").set({
        "broker_reject_circuit": {"halted": True, "status": "BROKER_REJECT_HALT", "halted_at": halted_at,
                                  "halt_id": "h" * 32, "consecutive_broker_rejects": 3}})


@pytest.mark.parametrize("age,severity", [(10, "ERROR"), (600, "INFO"), (3600 + 10, "ERROR")])
def test_the_tick_carries_when_the_halt_began_and_logs_the_cadence(monkeypatch, capsys, age, severity):
    setup_tick(monkeypatch)
    halted_at = (datetime.now(UTC) - timedelta(seconds=age)).isoformat()
    seed_halt(halted_at)
    result, code = main.lego_tick(None)
    event = json.loads(capsys.readouterr().out)
    assert code == 200 and result["broker_reject_halted"] is True
    assert result["broker_reject_halted_at"] == halted_at
    assert event["business_status"] == "BROKER_REJECT_HALT" and event["halt_since"] == halted_at
    assert event["severity"] == severity


def test_a_circuit_that_is_not_halted_reports_no_halt_time(monkeypatch, capsys):
    setup_tick(monkeypatch)
    result, _ = main.lego_tick(None)
    assert result["broker_reject_halted"] is False and result.get("broker_reject_halted_at") is None
    assert json.loads(capsys.readouterr().out)["business_status"] != "BROKER_REJECT_HALT"


def test_a_half_open_circuit_keeps_its_old_halted_at_out_of_the_tick(monkeypatch, capsys):
    """try_half_open and reset leave halted_at in the document; the tick must not call that a standing halt."""
    setup_tick(monkeypatch)
    FAKE_DB.reference(f"{DISPATCH_LOCK_PATH}/{account_symbol_fence_key('identity', 'AAPL')}").set({
        "broker_reject_circuit": {"halted": False, "status": "HALF_OPEN", "halted_at": HALT_AT.isoformat(),
                                  "halt_id": "h" * 32, "probe_streak": 4}})
    result, _ = main.lego_tick(None)
    assert result["broker_reject_halted"] is False and result["broker_reject_halted_at"] is None
    event = json.loads(capsys.readouterr().out)
    assert event["business_status"] != "BROKER_REJECT_HALT" and event["halt_since"] is None


# --------------------------------------------------------------------------------------------
# N1: the digest says when the evidence itself is incomplete.
# --------------------------------------------------------------------------------------------

PATH_WITH_EQUATION = "W00[claimed]|D00[market]>D01>D05[market]>D06>D07[ok]>D08[READY_BUY]>D09[auto]>D10[committed]"
PATH_WITHOUT = "W00[claimed]|D00[market]>D01>D02[MARKET_CLOSED]|W00[claimed]"


def trace_doc(version, *, path=PATH_WITH_EQUATION):
    return {"v": 1, "tick": f"{version:032x}", "at": at(version * 15), "http": 200, "pipe": "TICK_OK",
            "biz": "ROW_COMMITTED", "why": ["row_committed"], "path": path, "runs": [run_id(version)],
            "events": [], "n": 0, "dropped": 0, "partial": False}


def recorder_world(*, traced=range(1, 10), skipped=0, beat_at=None, path=PATH_WITH_EQUATION):
    db = make_world(stuck=False)                                    # rows v1..v9
    day = {f"{133000 + v:06d}_{v:08x}": trace_doc(v, path=path) for v in traced}
    db["webull_lego_trace"] = {CHAIN: {"2026-10-08": day}}
    db["webull_lego_heartbeat"] = {CHAIN: {"at": beat_at or at(150), "boot_at": at(-600), "ticks": 4926,
                                           "written": 172, "skipped_budget": skipped}}
    return db


def finding(report, code):
    return [f for f in report["findings"] if f["code"] == code]


def test_a_committed_row_without_a_recorded_equation_is_reported():
    report = ta.build_report(rtdb=recorder_world(traced=[v for v in range(1, 10) if v not in (4, 7)]))
    (gap,) = finding(report, "TRACE_GAP_ROWS")
    assert gap["level"] == "P2" and "2 committed rows" in gap["text"]
    assert "4" in gap["text"] and "7" in gap["text"]


def test_complete_traces_report_no_gap():
    assert finding(ta.build_report(rtdb=recorder_world()), "TRACE_GAP_ROWS") == []


def test_rows_older_than_the_first_trace_are_not_a_gap():
    """Older rows predate the recorder or its retention: nothing was dropped."""
    assert finding(ta.build_report(rtdb=recorder_world(traced=range(5, 10))), "TRACE_GAP_ROWS") == []


def test_a_build_that_records_no_equations_cannot_have_a_gap():
    assert finding(ta.build_report(rtdb=recorder_world(path=PATH_WITHOUT)), "TRACE_GAP_ROWS") == []


def test_a_single_order_view_does_not_pretend_to_know_the_gap():
    world = recorder_world(traced=[v for v in range(1, 10) if v != 4])
    assert finding(ta.build_report(rtdb=world, run=run_id(2)[:8]), "TRACE_GAP_ROWS") == []


def test_skipped_ticks_in_the_heartbeat_are_reported():
    (skipped,) = finding(ta.build_report(rtdb=recorder_world(skipped=6)), "RECORDER_SKIPPED_TICKS")
    assert skipped["level"] == "P2" and "6" in skipped["text"]
    assert finding(ta.build_report(rtdb=recorder_world(skipped=0)), "RECORDER_SKIPPED_TICKS") == []


def test_a_log_export_that_ends_long_before_the_last_heartbeat_is_reported():
    logs = [tick_entry(m) for m in range(0, 8)]                      # the export ends at minute 7
    (short,) = finding(ta.build_report(logs=logs, rtdb=recorder_world(beat_at=at(7 + 360))), "LOG_EXPORT_SHORT")
    assert short["level"] == "INFO" and "6.0 h" in short["text"]
    assert finding(ta.build_report(logs=logs, rtdb=recorder_world(beat_at=at(7 + 5))), "LOG_EXPORT_SHORT") == []
    assert finding(ta.build_report(rtdb=recorder_world(beat_at=at(7 + 360))), "LOG_EXPORT_SHORT") == []


# --------------------------------------------------------------------------------------------
# N3: deploy/continuous-uat.sh can run the whole-share control; the chain only moves one way.
# --------------------------------------------------------------------------------------------

def run_policy_script(tmp_path, **env):
    """The real script, with its final `exec bash deploy/cloudshell-all-in-one.sh` replaced by an echo."""
    text = (ROOT / "deploy" / "continuous-uat.sh").read_text()
    marker = "exec bash deploy/cloudshell-all-in-one.sh"
    assert text.rstrip().endswith(marker)
    script = tmp_path / "continuous-uat.sh"
    script.write_text(text.replace(marker, 'printf "%s" "${LEGO_ALLOW_FRACTIONAL_OVERRIDE}"'))
    base = {"PATH": os.environ["PATH"], "EXPECTED_CANDIDATE_HASH": "candidate",
            "LEGO_RELEASE_AUTHORIZATION_OVERRIDE": "authorization", "LEGO_TRADING_WINDOW_END_OVERRIDE": "2026-10-30T20:00:00Z"}
    return subprocess.run(["bash", str(script)], env={**base, **env}, capture_output=True, text=True)


def test_the_uat_script_keeps_fractional_orders_unless_told_otherwise(tmp_path):
    done = run_policy_script(tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout == "true"


def test_the_uat_script_runs_the_whole_share_control_when_asked(tmp_path):
    done = run_policy_script(tmp_path, LEGO_ALLOW_FRACTIONAL_OVERRIDE="false")
    assert done.returncode == 0, done.stderr
    assert done.stdout == "false"


def test_the_uat_script_still_demands_the_release_values(tmp_path):
    text = (ROOT / "deploy" / "continuous-uat.sh").read_text()
    for required in ("EXPECTED_CANDIDATE_HASH", "LEGO_RELEASE_AUTHORIZATION_OVERRIDE", "LEGO_TRADING_WINDOW_END_OVERRIDE"):
        assert f': "${{{required}:?' in text


def test_whole_share_is_a_tightening_and_the_way_back_is_refused():
    """The shape the chain state stores (lego_state.commit_final_row): increment as '.17g', precision as int."""
    fractional = {"quantity_increment": "0.01", "decimal_precision": 2}
    whole = {"quantity_increment": "1", "decimal_precision": 0}
    assert _capability_is_tightening(fractional, whole) is True
    assert _capability_is_tightening(whole, fractional) is False
