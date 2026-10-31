"""Audit 2026-10-31 (docs/AUDIT_20261031_TH.md).

Revision lego-tick-prod-00003-dob (main@ed793b3), PROD with LEGO_MODE=paper, UBER, session
2026-10-30, log 13:25Z..20:44Z: 440 ticks, 440 HTTP 200, 26 slots, 18 intents and 16 paper fills,
tools.trace_audit 0 P0. The offsets below are the real ones: the start of every positions read
(log time - duration_ms) in the 17 order ticks that read positions three times, in seconds after
the first read of the tick.

F1  Four of those ticks drew 429 TOO_MANY_REQUESTS on GET /trading/assets/positions/list, each
    on the third read, which started 2.967, 3.188, 3.478 and 3.513 s after the first. Every first
    and second read was served, and so was every third read that started 3.576 s or later (once
    at 3.499 s: the boundary is ~15 ms wide in these timestamps). Webull documents 10/30s; the
    gateway allows about two starts per 3.5 s, and a refused attempt costs nothing: the retries,
    3.26-3.68 s after the second read, were all served. The 2 s retry back-off absorbed the four,
    at the last read before the paper fill. webull_io now paces the route instead (_pace).
F2  Not defects: both SUPPRESSED_STATE_CHANGED re-priced across DIFF (side_changed_or_pass), and
    the token expiry was moved by Webull itself (the audit has the numbers).
F3  The deployed paper settings run to Preview without any real-money acknowledgement, and their
    release authorization cannot carry LEGO_MODE=trade.

Nothing here touches a broker or a database: the gateway is a double and the clock is fake.
"""
from __future__ import annotations

import json
import random

import pytest
from webull.core.exception.exceptions import ServerException

import tick_runtime
import webull_io
from config import load_runtime_config
from test_prod_live_gate import authorized, env

POSITIONS = "/trading/assets/positions/list"
# HH:MM of the tick -> (second read, third read), seconds after the first read.
SERVED = {
    "13:30": (2.098, 3.641), "13:45": (2.368, 4.165), "14:15": (2.546, 3.949),
    "14:30": (3.894, 5.582), "14:45": (2.150, 3.589), "15:15": (2.183, 3.743),
    "15:30": (2.581, 3.682), "15:45": (3.615, 5.319), "16:15": (2.191, 3.702),
    "17:45": (2.224, 3.499), "18:00": (3.297, 4.672), "19:00": (2.287, 3.576),
    "19:45": (1.934, 3.586),
}
REFUSED = {"14:00": (1.970, 3.188), "16:00": (2.328, 3.478),
           "17:00": (2.003, 3.513), "18:45": (1.873, 2.967)}
RETRIED = {"14:00": 5.335, "16:00": 5.635, "17:00": 5.686, "18:45": 5.129}   # all served
TICKS = {**SERVED, **REFUSED}


class Clock:
    def __init__(self):
        self.now = 50_000.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class TokenBucket:
    """`capacity` starts at once, one more every `refill` s; a refused attempt costs nothing."""

    def __init__(self, refill, capacity=2):
        self.refill, self.capacity = refill, capacity
        self.tokens, self.at = float(capacity), None

    def admit(self, now):
        if self.at is not None:
            self.tokens = min(self.capacity, self.tokens + (now - self.at) / self.refill)
        self.at = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


class SlidingWindow:
    """At most `limit` served starts in any `seconds`."""

    def __init__(self, seconds, limit=2):
        self.seconds, self.limit, self.served = seconds, limit, []

    def admit(self, now):
        if sum(1 for start in self.served if now - start < self.seconds) >= self.limit:
            return False
        self.served.append(now)
        return True


class Response:
    status_code = 200
    headers = {}

    def json(self):
        return {}


class Request:
    def __init__(self, action):
        self.action = action

    def get_action_name(self):
        return self.action

    def set_connect_timeout(self, value):
        self.connect = value

    def set_read_timeout(self, value):
        self.read = value


class Gateway:
    """The Webull side of the SDK boundary: throttles positions/list, records every attempt."""

    def __init__(self, clock, limit=None):
        self.clock, self.limit, self.attempts = clock, limit, []

    def client(self):
        gateway = self

        class Base:
            def get_response(self, request):
                action = request.get_action_name()
                served = (action != POSITIONS or gateway.limit is None
                          or gateway.limit.admit(gateway.clock.now))
                gateway.attempts.append((action, gateway.clock.now, served))
                if not served:
                    raise ServerException("TOO_MANY_REQUESTS", "Too many requests",
                                          http_status=429, request_id="audit-20261031-429")
                return Response()

        return webull_io._bounded_api_class(Base)()


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(webull_io.time, "monotonic", fake.monotonic)
    monkeypatch.setattr(webull_io.time, "sleep", fake.sleep)
    return fake


def replay(clock, limit, offsets):
    """One tick: each read is sent at its logged offset; returns (start offset, served) per read."""
    gateway = Gateway(clock, limit)
    sdk = gateway.client()
    clock.now += 900.0                       # next slot: whatever the last tick left has drained
    first = clock.now
    for offset in offsets:
        clock.now = max(clock.now, first + offset)
        try:
            sdk.get_response(Request(POSITIONS))
        except ServerException as exc:
            assert exc.http_status == 429 and webull_io.is_transient_exception(exc)
    return [(round(at - first, 3), served) for _action, at, served in gateway.attempts]


def test_without_the_pace_the_logged_429s_come_back(clock, monkeypatch):
    monkeypatch.setattr(webull_io, "_PACED_ROUTES", {})
    for tick, (second, third) in REFUSED.items():
        reads = replay(clock, TokenBucket(refill=3.52), (0.0, second, third, RETRIED[tick]))
        assert reads == [(0.0, True), (second, True), (third, False), (RETRIED[tick], True)], tick
    assert clock.slept == []


def test_with_the_pace_no_logged_tick_draws_a_429(clock, monkeypatch):
    strict = 3.6        # stricter than the gateway: it refuses third reads the log shows served
    with monkeypatch.context() as unpaced:
        unpaced.setattr(webull_io, "_PACED_ROUTES", {})
        too_strict = {tick for tick, (second, third) in SERVED.items()
                      if not replay(clock, TokenBucket(refill=strict), (0.0, second, third))[2][1]}
    assert too_strict == {"14:45", "17:45", "19:00", "19:45"}
    for make in (lambda: TokenBucket(refill=strict), lambda: SlidingWindow(seconds=strict)):
        waits = {}
        for tick, (second, third) in TICKS.items():
            clock.slept.clear()
            reads = replay(clock, make(), (0.0, second, third))
            assert reads == [(0.0, True), (second, True), (max(third, 4.0), True)], tick
            waits[tick] = sum(clock.slept)
            assert waits[tick] == pytest.approx(max(0.0, 4.0 - third)), tick
        assert max(waits.values()) == pytest.approx(1.033)               # 18:45
        assert sum(1 for wait in waits.values() if wait) == 13


@pytest.mark.parametrize("seed", range(5))
def test_any_schedule_keeps_three_starts_4s_apart_and_9_per_30s(clock, seed):
    rng = random.Random(20261031 + seed)
    gateway = Gateway(clock)
    sdk = gateway.client()
    for _ in range(30):
        clock.now += rng.uniform(0.0, 3.0)
        sdk.get_response(Request(POSITIONS))
    starts = [at for _action, at, _served in gateway.attempts]
    assert len(starts) == 30 and starts == sorted(starts)
    assert min(later - earlier for earlier, later in zip(starts, starts[2:])) >= 4.0 - 1e-9
    assert max(sum(1 for s in starts if a <= s <= a + 30.0) for a in starts) <= 9   # Webull: 10/30s


def test_only_positions_is_paced(clock):
    gateway = Gateway(clock)
    sdk = gateway.client()
    for route in webull_io._OPERATION_BY_ROUTE:
        if route != POSITIONS:
            for _ in range(10):
                sdk.get_response(Request(route))
    assert clock.slept == [] and len(gateway.attempts) == 10 * (len(webull_io._OPERATION_BY_ROUTE) - 1)


def test_a_wait_the_tick_cannot_afford_is_refused_before_anything_is_sent(clock, monkeypatch):
    monkeypatch.setattr(tick_runtime, "remaining", lambda: 3.0)
    gateway = Gateway(clock)
    sdk = gateway.client()
    sdk.get_response(Request(POSITIONS))
    sdk.get_response(Request(POSITIONS))
    due = dict(webull_io._PACE_DUE)
    with pytest.raises(tick_runtime.TickDeadlineExceeded):
        sdk.get_response(Request(POSITIONS))                 # needs 4 s, the tick has 3
    assert clock.slept == [] and len(gateway.attempts) == 2 and webull_io._PACE_DUE == due


def test_a_wait_is_logged_as_its_own_operation(clock, capsys):
    sdk = Gateway(clock).client()
    for _ in range(3):
        sdk.get_response(Request(POSITIONS))
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()
              if line.startswith("{")]
    paced = [e for e in events if e.get("operation") == "pace_positions"]
    assert [e["outcome"] for e in paced] == ["ok"] and paced[0]["event"] == "lego_operation"
    assert paced[0]["severity"] == "INFO" and paced[0]["duration_ms"] == pytest.approx(4000.0)
    assert sum(1 for e in events if e.get("operation") == "sdk_positions") == 3


# YAML.txt of lego-tick-prod-00003-dob, apart from the account and the candidate.
DEPLOYED_PAPER = dict(
    LEGO_MODE="paper", LEGO_ACTIVE="true", LEGO_SYMBOL="UBER", LEGO_FIX_C="3000",
    LEGO_DIFF="5", LEGO_ALLOW_FRACTIONAL="true", LEGO_MAX_ORDER_QUANTITY="60",
    LEGO_MAX_ORDER_NOTIONAL_USD="3750", LEGO_MAX_SESSION_ORDERS="2",
    LEGO_TRADING_WINDOW_END="2026-11-05T21:00:00Z", LEGO_SESSION_KEY_MODE="market_day",
    LEGO_STALE_ORDER_ACTION="hold", LEGO_STALE_ORDER_SECONDS="300",
    LEGO_CANCEL_CONFIRM_GRACE_SECONDS="120", LEGO_MAX_CANCEL_MUTATIONS_PER_ORDER="1")


def test_the_deployed_paper_release_runs_to_preview_without_a_real_money_ack(tmp_path):
    runtime = load_runtime_config(authorized(env(tmp_path, **DEPLOYED_PAPER)))
    assert (runtime.deployment.environment, runtime.operator.symbol) == ("PROD", "UBER")
    assert runtime.operator.paper and runtime.operator.preview_only
    assert runtime.deployment.allow_fractional is True
    assert runtime.deployment.release_is_authorized and not runtime.prod_live_gate_open
    assert runtime.allows_new_broker_mutation


def test_the_paper_authorization_cannot_carry_trade(tmp_path):
    paper = authorized(env(tmp_path, **DEPLOYED_PAPER))
    switched = load_runtime_config({**paper, "LEGO_MODE": "trade"})
    assert not switched.deployment.release_is_authorized
    assert not switched.allows_new_broker_mutation
    own = load_runtime_config(authorized({**paper, "LEGO_MODE": "trade"}))   # its own binding, no ack
    assert own.deployment.release_is_authorized and not own.prod_live_gate_open
    assert not own.allows_new_broker_mutation
