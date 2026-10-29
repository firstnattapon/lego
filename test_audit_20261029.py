"""Audit 2026-10-29 (docs/AUDIT_20261029_TH.md).

Revision lego-tick-uat-00056-zuw (main@de59904, candidate 852c11ec...), UAT sessions 2026-10-26,
2026-10-27 and 2026-10-28 (UBER), log 2026-10-25T08:44Z..2026-10-29T08:50Z. The numbers below are the
real ones, copied from the flight recorder (webull_lego_trace) and the fence document of the RTDB export.

F1  From 2026-10-27 13:45Z every order the broker accepted came back FAILED: 9 of 9 (SELL 1, BUY 8),
    Place HTTP 200 with a broker order id, Order Detail ``status: FAILED``, filled 0, commission 0 and
    no field that carries a reason. The last fill was 2026-10-26 17:45Z. Locked here: the payload of
    the FAILED SELL and of the last FILLED BUY is exactly what build_order_payload makes today (the
    builder has not changed since 2026-09-11), and the raw FAILED detail is read as a reasonless
    reject that moves no shares. Nothing in the order path is the cause; the broker has to say why.
F2  The broker-reject circuit halted at 14:32:06Z and probed six times on the UAT schedule
    (900 -> 1,800 -> 3,600 -> 7,200 -> 14,400 -> 14,400 s); every probe FAILED. Replayed below with the
    real times, it ends in the fence document the export holds. PROD never probes by itself.
F2a While halted, every committed row is written PASS_BROKER_REJECT_HALT whatever its gap (the design
    test_audit_20261021 locks). 30 of the 40 were trades the halt stopped; 10 had |gap| <= DIFF.

Nothing here touches a broker or a database: the RTDB is the in-memory fake of conftest.py.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

import broker_circuit as circuit
import lego_outbox as outbox
import webull_io
from conftest import FAKE_DB
from lego_one_row import Config
from lego_orders import summarize_order_result
from market_clock import session_bounds
from operational_health import complete_sessions_between

UTC = timezone.utc
IDENT = "ident-uat"
SYMBOL = "UBER"
SCOPE = outbox.account_symbol_fence_key(IDENT, SYMBOL)
CFG = Config("UBER", 10000, diff=25, dna_code="bypass:6500", strategy_id="shannon_demon_lego_v2",
             decimal_precision=2, quantity_increment=0.01)
HOLDINGS, FIX_C, DIFF = 151.3228, 10000.0, 25.0
WINDOW_END = datetime(2026, 11, 6, 21, 0, tzinfo=UTC)          # LEGO_TRADING_WINDOW_END of 00056


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolate():
    FAKE_DB.store.clear()
    yield
    FAKE_DB.store.clear()


# ------------------------------------------------------------------ F1: the order and its answer

# webull_lego_trace 2026-10-27/134504_e943fdd6 (FAILED) and 2026-10-26/174505_2ad4e920 (FILLED).
FAILED_SELL = {"client_order_id": "25726a67575e5f4f2417230175c61104", "combo_type": "NORMAL",
               "entrust_type": "QTY", "instrument_type": "EQUITY", "market": "US",
               "order_type": "MARKET", "quantity": "0.4", "side": "SELL",
               "support_trading_session": "CORE", "symbol": "UBER", "time_in_force": "DAY"}
FILLED_BUY = {"client_order_id": "e1c7cb7b483f77cc401f6e013b9b1c85", "combo_type": "NORMAL",
              "entrust_type": "QTY", "instrument_type": "EQUITY", "market": "US",
              "order_type": "MARKET", "quantity": "0.52", "side": "BUY",
              "support_trading_session": "CORE", "symbol": "UBER", "time_in_force": "DAY"}
FAILED_PLACE = {"client_order_id": FAILED_SELL["client_order_id"], "order_id": "038BTJEV5M80O0KF4PKO000000"}
FAILED_DETAIL = {
    "client_order_id": FAILED_SELL["client_order_id"], "combo_order_id": "038BTJEV5M80O0KF4PKO000000",
    "combo_type": "NORMAL",
    "orders": [{"client_order_id": FAILED_SELL["client_order_id"],
                "commission": {"actual_commission": "0", "receivable_commission": "0"},
                "entrust_type": "QTY", "filled_price": "0.000000", "filled_quantity": "0.000000",
                "instrument_type": "EQUITY", "order_id": "038BTJEV5M80O0KF4PKO000000",
                "order_type": "MARKET", "place_time": "2026-10-27T13:45:21.243Z",
                "place_time_at": "2026-10-27T13:45:21.243Z", "side": "SELL", "status": "FAILED",
                "support_trading_session": "CORE", "symbol": "UBER", "time_in_force": "DAY",
                "total_quantity": "0.400000"}]}
FILLED_PLACE = {"client_order_id": FILLED_BUY["client_order_id"], "order_id": "038BPA47V280O0KF4PKO000000"}
FILLED_DETAIL = {
    "client_order_id": FILLED_BUY["client_order_id"], "combo_order_id": "038BPA47V280O0KF4PKO000000",
    "combo_type": "NORMAL",
    "orders": [{"client_order_id": FILLED_BUY["client_order_id"],
                "commission": {"actual_commission": "0.34", "receivable_commission": "0.34"},
                "entrust_type": "QTY",
                "fees": [{"actual_value": "0.02", "receivable_value": "0.02", "type": "GST"}],
                "filled_price": "65.940000", "filled_quantity": "0.520000",
                "filled_time": "2026-10-26T17:45:18.884Z", "filled_time_at": "2026-10-26T17:45:18.884Z",
                "instrument_type": "EQUITY", "order_id": "038BPA47V280O0KF4PKO000000",
                "order_type": "MARKET", "place_time": "2026-10-26T17:45:18.065Z",
                "place_time_at": "2026-10-26T17:45:18.065Z", "side": "BUY", "status": "FILLED",
                "support_trading_session": "CORE", "symbol": "UBER", "time_in_force": "DAY",
                "total_quantity": "0.520000"}]}


@pytest.mark.parametrize("sent", [FAILED_SELL, FILLED_BUY], ids=["failed_sell", "filled_buy"])
def test_the_order_that_failed_and_the_one_that_filled_are_what_the_code_builds(sent):
    built = webull_io.build_order_payload(CFG, sent["side"], float(sent["quantity"]), sent["client_order_id"])
    assert built == [sent]


def test_the_failed_and_the_filled_order_differ_only_in_id_side_and_quantity():
    differ = {key for key in FAILED_SELL if FAILED_SELL[key] != FILLED_BUY[key]}
    assert differ == {"client_order_id", "side", "quantity"} and FAILED_SELL.keys() == FILLED_BUY.keys()


def test_the_raw_failed_detail_is_a_reject_without_reason_that_moves_no_shares():
    summary = summarize_order_result(FAILED_PLACE, FAILED_DETAIL)
    assert summary["status"] == "FAILED" and summary["broker_reason_missing"] is True
    assert summary["realized"] is False and summary["filled_quantity"] == "0.000000"
    assert summary["broker_order_id"] == "038BTJEV5M80O0KF4PKO000000"
    assert "reject_reason" not in summary and "broker_reject_code" not in summary


def test_the_last_fill_is_read_with_its_fee():
    summary = summarize_order_result(FILLED_PLACE, FILLED_DETAIL)
    assert summary["status"] == "FILLED" and summary["realized"] is True
    assert (summary["filled_quantity"], summary["filled_price"], summary["filled_fee"]) == \
        ("0.520000", "65.940000", "0.36")
    assert "broker_reason_missing" not in summary


# ---------------------------------------------------------- F2: the circuit, replayed with real times

# (run id, when the FAILED was recorded). The halted_at values are the halt_since of the tick log;
# the first two are when the order detail said FAILED (those did not halt, so nothing stores the time).
FAILED_ORDERS = [
    ("25726a67575e5f4f2417230175c61104", "2026-10-27T13:45:22"),
    ("fc4f62f60f6ced4d916b36795f9627b2", "2026-10-27T14:17:07"),
    ("12dd7c99cc92b1c810811d1807e4eee8", "2026-10-27T14:32:06.905818"),
    ("df5487cc2c73a76ecf1c77fd5fa94163", "2026-10-27T15:01:08.317620"),
    ("72599329ba8fd08f769d6beb3a022627", "2026-10-27T15:46:18.634306"),
    ("d43047456509794a7226e953bdac0580", "2026-10-27T17:32:05.813205"),
    ("1ed25d4799fb0d2d68ed2e5683980e36", "2026-10-27T19:45:21.506042"),
    ("7c929df336b2e09730ef09c3179e9772", "2026-10-28T13:46:28.013402"),
    ("a8f276d17a57ef0c8c0da1457cbeda42", "2026-10-28T18:00:21.176015"),
]
# READY slots the halt met, decided ~5 s into the slot, and whether the cooldown let a probe through.
# The last probe time is the exact last_probe_at of the export.
READY_DURING_HALT = [
    ("2026-10-27T14:45:05", False), ("2026-10-27T15:00:05", True), ("2026-10-27T15:30:05", False),
    ("2026-10-27T15:45:05", True), ("2026-10-27T16:00:05", False), ("2026-10-27T17:30:05", True),
    ("2026-10-27T18:30:05", False), ("2026-10-27T18:45:05", False), ("2026-10-27T19:00:05", False),
    ("2026-10-27T19:15:05", False), ("2026-10-27T19:45:05", True),
    ("2026-10-28T13:30:05", True), ("2026-10-28T17:45:05", False), ("2026-10-28T18:00:06.371090", True),
    ("2026-10-28T18:15:05", False), ("2026-10-28T19:45:05", False),
]


class _Clock(datetime):
    current = None

    @classmethod
    def now(cls, tz=None):
        return cls.current


def fence():
    return FAKE_DB.reference(f"{outbox.DISPATCH_LOCK_PATH}/{SCOPE}")


def record_failed(monkeypatch, run_id, when):
    """The terminal FAILED of the order the fence holds, stamped with the real time."""
    fence().update({"inflight_run_id": run_id})
    intent = {"run_id": run_id, "chain_key": "chain", "symbol": SYMBOL,
              "runtime_identity_fingerprint": IDENT}
    _Clock.current = at(when)
    with monkeypatch.context() as patch:
        patch.setattr(circuit, "datetime", _Clock)
        circuit.record_outcome(intent, {"status": "FAILED", "filled_quantity": "0.000000"})
    doc = fence().get()
    doc.pop("inflight_run_id", None)                    # the order is terminal: the fence is free again
    fence().set(doc)
    return circuit.status(IDENT, SYMBOL)


def try_probe(when, environment="UAT"):
    return circuit.try_half_open(IDENT, SYMBOL, environment=environment, now=at(when))


def replay(monkeypatch):
    """Every FAILED and every READY decision of 27-28 Oct in time order; returns the opened probes."""
    events = ([(at(when), "failed", run_id) for run_id, when in FAILED_ORDERS]
              + [(at(when), "ready", (when, opens)) for when, opens in READY_DURING_HALT])
    opened = []
    for _, kind, value in sorted(events, key=lambda event: event[0]):
        if kind == "failed":
            record_failed(monkeypatch, value, next(w for r, w in FAILED_ORDERS if r == value))
            continue
        when, opens = value
        if circuit.status(IDENT, SYMBOL).get("halted"):
            probe = try_probe(when)
            assert bool(probe) is opens, when
            if probe:
                opened.append((when, probe["probe_of_halt_id"][:8], probe["probe_total"]))
        else:
            # Half-open: the 13:30 probe was suppressed by quote_age_limit, so 13:45 is the probe.
            assert not opens, when
    return opened


def test_three_failed_in_a_row_halt_at_the_time_the_tick_log_shows(monkeypatch):
    for run_id, when in FAILED_ORDERS[:2]:
        state = record_failed(monkeypatch, run_id, when)
        assert state.get("halted") is not True
    state = record_failed(monkeypatch, *FAILED_ORDERS[2])
    assert state["halted"] is True and state["halt_id"] == FAILED_ORDERS[2][0]
    assert state["halted_at"] == "2026-10-27T14:32:06.905818+00:00"
    assert state["consecutive_broker_rejects"] == circuit.MAX_CONSECUTIVE_BROKER_REJECTS


def test_the_six_probes_opened_where_the_logs_say_and_only_there(monkeypatch):
    assert replay(monkeypatch) == [
        ("2026-10-27T15:00:05", "12dd7c99", 1), ("2026-10-27T15:45:05", "df5487cc", 2),
        ("2026-10-27T17:30:05", "72599329", 3), ("2026-10-27T19:45:05", "d4304745", 4),
        ("2026-10-28T13:30:05", "1ed25d47", 5), ("2026-10-28T18:00:06.371090", "7c929df3", 6)]


def test_the_replay_ends_in_the_fence_document_of_the_export(monkeypatch):
    replay(monkeypatch)
    state = dict(circuit.status(IDENT, SYMBOL))
    assert state.pop("probe_id")
    assert state == {
        "consecutive_broker_rejects": 3, "halt_id": "a8f276d17a57ef0c8c0da1457cbeda42", "halted": True,
        "halted_at": "2026-10-28T18:00:21.176015+00:00", "last_broker_status": "FAILED",
        "last_outcome_run_id": "a8f276d17a57ef0c8c0da1457cbeda42",
        "last_probe_at": "2026-10-28T18:00:06.371090+00:00",
        "probe_of_halt_id": "7c929df336b2e09730ef09c3179e9772", "probe_streak": 6, "probe_total": 6,
        "status": circuit.HALT, "updated_at": "2026-10-28T18:00:21.176015+00:00"}


def test_the_cooldowns_between_the_halts_follow_the_streak():
    halts = [at(when) for _, when in FAILED_ORDERS[2:]]
    earliest = [halt + timedelta(seconds=circuit.probe_cooldown_seconds(streak))
                for streak, halt in enumerate(halts)]
    assert [(e - h).total_seconds() for e, h in zip(earliest, halts)] == \
        [900, 1800, 3600, 7200, 14400, 14400, 14400]
    # 28 Oct 17:45:05 missed the cooldown of the 13:46:28 halt by 83 s; 18:00:06 was the next READY.
    assert earliest[5] - at("2026-10-28T17:45:05") == timedelta(seconds=83, microseconds=13402)
    assert earliest[6] == at("2026-10-28T22:00:21.176015")


def test_prod_never_lets_this_halt_test_the_broker(monkeypatch):
    replay(monkeypatch)
    session = "2026-10-29T13:45:05"                      # long after the last cooldown
    assert try_probe(session, environment="PROD") == {}
    assert circuit.status(IDENT, SYMBOL)["halted"] is True
    assert try_probe(session)["probe_total"] == 7       # UAT keeps probing, one order at a time


# ------------------------------------------------------------- F2a: what the halted rows would have been

# (market slot, P_n) of the 40 rows committed PASS_BROKER_REJECT_HALT; holdings 151.3228 throughout.
HALTED_ROWS = [
    ("2026-10-27:5", 65.8138), ("2026-10-27:7", 65.92), ("2026-10-27:8", 65.83), ("2026-10-27:10", 66.25),
    ("2026-10-27:11", 66.22), ("2026-10-27:12", 66.16), ("2026-10-27:13", 66.025), ("2026-10-27:14", 65.98),
    ("2026-10-27:15", 65.965), ("2026-10-27:17", 65.925), ("2026-10-27:18", 65.93), ("2026-10-27:19", 65.985),
    ("2026-10-27:20", 65.86), ("2026-10-27:21", 65.875), ("2026-10-27:22", 65.79), ("2026-10-27:23", 65.87),
    ("2026-10-27:24", 65.9751), ("2026-10-28:2", 65.811), ("2026-10-28:3", 65.27), ("2026-10-28:4", 65.24),
    ("2026-10-28:5", 64.96), ("2026-10-28:6", 64.83), ("2026-10-28:7", 64.825), ("2026-10-28:8", 64.935),
    ("2026-10-28:9", 64.955), ("2026-10-28:10", 65.3512), ("2026-10-28:11", 65.47), ("2026-10-28:12", 65.565),
    ("2026-10-28:13", 65.68), ("2026-10-28:14", 65.65), ("2026-10-28:15", 65.655), ("2026-10-28:16", 65.515),
    ("2026-10-28:17", 65.38), ("2026-10-28:19", 65.59), ("2026-10-28:20", 65.495), ("2026-10-28:21", 65.35),
    ("2026-10-28:22", 65.485), ("2026-10-28:23", 65.255), ("2026-10-28:24", 65.14), ("2026-10-28:25", 65.13),
]


def test_thirty_of_the_forty_halted_rows_were_trades_and_ten_would_have_passed():
    passed = [slot for slot, price in HALTED_ROWS if abs(HOLDINGS * price - FIX_C) <= DIFF]
    assert len(HALTED_ROWS) == 40 and len(HALTED_ROWS) - len(passed) == 30
    assert passed == ["2026-10-27:7", "2026-10-27:11", "2026-10-27:12", "2026-10-27:13", "2026-10-27:14",
                      "2026-10-27:15", "2026-10-27:17", "2026-10-27:18", "2026-10-27:19", "2026-10-27:24"]
    # 10-27:10 is the closest real one: gap +25.1355, a SELL the halt stopped.
    assert HOLDINGS * 66.25 - FIX_C == pytest.approx(25.1355, abs=1e-6)


# ------------------------------------------------------------------------ release window of 00056

@pytest.mark.parametrize("now,left", [("2026-10-27T13:44:05", 8), ("2026-10-28T20:20:34", 7),
                                      ("2026-10-29T08:50:05", 7)])
def test_the_window_has_the_sessions_the_tick_log_reports(now, left):
    assert complete_sessions_between(at(now), WINDOW_END) == left


def test_after_daylight_saving_the_last_session_closes_exactly_at_the_window_end():
    assert session_bounds(date(2026, 10, 30)) == (at("2026-10-30T13:30:00"), at("2026-10-30T20:00:00"))
    assert session_bounds(date(2026, 11, 2)) == (at("2026-11-02T14:30:00"), at("2026-11-02T21:00:00"))
    assert session_bounds(date(2026, 11, 6))[1] == WINDOW_END
