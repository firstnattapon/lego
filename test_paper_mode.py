"""LEGO_MODE=paper: preview that books the fill a market order would have had.

Everything up to the preview cut is preview's: every read, a real Preview of BUY and SELL
alike, and every check. There the worker books a paper fill through the same
finalize_execution_fill a broker fill goes through, on a chain of its own; the next decision
decides on those paper holdings, so the 17-column ledger runs. Place is never called, and the
real account must not move (docs/PROD_OBSERVE_TH.md). Only broker I/O is a double.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import decision_service
import execution_limits
import execution_service as execution
import lego_outbox as outbox
import main
import observability
import operator_halt
import release_horizon as rh
import webull_io
from config import PAPER_STRATEGY_ID, STRATEGY_ID, load_runtime_config
from conftest import FAKE_DB, fake_trade_client
from lego_one_row import ACTUAL_COLUMN, DELTA_COLUMN, EXCESS_COLUMN, Config
from lego_outbox import DISPATCH_LOCK_PATH, account_symbol_fence_key, read_intent
from lego_state import ROWS_PATH, STATE_PATH, chain_key
from test_dispatch_overshoot import isolate  # noqa: F401  (autouse)
from test_fractional_quantity_incident import _frozen, _profile
from tools import trace_audit
from webull_io import parse_instrument_capability

UTC = timezone.utc
OPEN = datetime(2026, 10, 5, 13, 30, tzinfo=UTC)          # a Monday regular session
PAPER_DB = "https://lego-firebase-paper.asia-southeast1.firebasedatabase.app"
FIX_C, DIFF = 3000.0, 25.0
HOLDINGS = trace_audit.COL["holdings"]
SIDE = trace_audit.COL["side"]
# 25 one-percent moves: 26 slots, one whole regular session (13:30-20:00 UTC).
SIGNS = "++---++++-----+++--+++--+"
DISPATCH_SLIP = Decimal("0.02")             # the quote moves a little between decision and dispatch


def paper_values(**overrides):
    values = {
        "LEGO_SYMBOL": "UBER", "LEGO_FIX_C": "3000", "LEGO_DIFF": "25",
        "WEBULL_ENV": "PROD", "WEBULL_ACCOUNT_ID": "test-account",
        "LEGO_DNA_CODE": "bypass:1000", "LEGO_DNA_CLOCK_MODE": "market",
        "LEGO_SLOT_SECONDS": "900", "LEGO_DNA_ORIGIN_UTC": "2026-10-05T13:30:00Z",
        "LEGO_MODE": "paper", "LEGO_ACTIVE": "true", "LEGO_CANDIDATE_HASH": "candidate",
        "LEGO_ALLOW_FRACTIONAL": "true", "FIREBASE_DB_URL": PAPER_DB,
        # The funding caps the PROD preview release carried, 2 orders per session included:
        # paper reserves no quota, so a whole session of fills passes under them.
        "LEGO_MAX_ORDER_QUANTITY": "60", "LEGO_MAX_ORDER_NOTIONAL_USD": "3750",
        "LEGO_MAX_SESSION_ORDERS": "2", "LEGO_TRADING_WINDOW_END": "2030-01-01T00:00:00Z",
        "LEGO_SESSION_KEY_MODE": "market_day",
    }
    values.update(overrides)
    values["LEGO_RELEASE_AUTHORIZATION"] = load_runtime_config(
        values).deployment.expected_release_binding
    return values


def prices(start=Decimal("65.49")):
    out = [start]
    for sign in SIGNS:
        factor = Decimal("1.01") if sign == "+" else Decimal("0.99")
        out.append((out[-1] * factor).quantize(Decimal("0.01")))
    return out


class BrokerError(Exception):
    """What the SDK raises for an HTTP error answer."""

    def __init__(self, http_status, error_code=""):
        super().__init__(f"HTTP {http_status} {error_code}".strip())
        self.http_status, self.error_code = http_status, error_code


class Session:
    """Paper slots of one regular session; the broker double answers the way PROD did.

    The real account holds ``real`` shares throughout unless a test moves it. Preview answers
    BUY and SELL with the cost and the 0.107% fee PROD charged on 29 Oct; ``sell_answer``
    replaces the SELL answer with a refusal (a body with an error, or an exception).
    """

    def __init__(self, monkeypatch, *, sell_answer=None, real=0.0):
        webull_io.reset_clients()
        execution.configure(main)
        decision_service.configure(main)
        self.monkeypatch, self.sell_answer, self.real = monkeypatch, sell_answer, real
        self.real_at_dispatch = None
        values = paper_values()
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        self.runtime = load_runtime_config(values)
        self.cfg = Config("UBER", FIX_C, DIFF, "bypass:1000", self.runtime.operator.strategy_id)
        self.price = Decimal("65.49")
        self.client = fake_trade_client(preview=self._preview, place={"order_id": "never"})
        token = {"ok": True, "ready": True, "status": "NORMAL", "secret_configured": True,
                 "token_storage": "SECRET_MANAGER",
                 "expires_at": (datetime.now(UTC) + timedelta(days=14)).isoformat()}
        capability = parse_instrument_capability(_profile(), "UBER")
        monkeypatch.setattr(decision_service, "build_clients", lambda: (object(), object()))
        monkeypatch.setattr(decision_service, "fetch_instrument_capability", lambda *_a: capability)
        monkeypatch.setattr(decision_service, "token_health", lambda: token)
        monkeypatch.setattr(decision_service, "fetch_snapshot",
                            lambda *_a: self._quote(self.decided_at, self.price, self.real))
        self.token = token

    def _patch_worker(self):
        # main's facade re-injects its own dependencies into execution_service on every call
        # (main._execution_call), the decision's included: the doubles go in right before use.
        patch = self.monkeypatch.setattr
        patch(execution, "build_clients", lambda: (self.client, object()))
        patch(execution, "fetch_open_orders", lambda *_a: [])
        patch(execution, "fetch_snapshot", lambda *_a: self._quote(
            self.dispatched_at, self.price + DISPATCH_SLIP,
            self.real if self.real_at_dispatch is None else self.real_at_dispatch))
        patch(execution, "fetch_buying_power", lambda *_a: Decimal("4969.56"))
        patch(execution, "token_health", lambda: self.token)
        patch(execution, "datetime", _frozen(self.dispatched_at))
        patch(outbox, "datetime", _frozen(self.dispatched_at))

    @staticmethod
    def _quote(at, price, real):
        return {"price": float(price), "holdings": real,
                "captured_at": at.isoformat(), "quote_time": at.isoformat()}

    def _preview(self, _account, orders):
        order = orders[0]
        if order["side"] == "SELL" and self.sell_answer is not None:
            if isinstance(self.sell_answer, Exception):
                raise self.sell_answer
            return self.sell_answer
        cost = (Decimal(order["quantity"]) * (self.price + DISPATCH_SLIP)).quantize(Decimal("0.01"))
        return {"estimated_cost": str(cost),
                "estimated_transaction_fee": str((cost * Decimal("0.00107")).quantize(Decimal("0.01")))}

    def decide(self, index, price):
        self.price = Decimal(str(price))
        start = OPEN + timedelta(minutes=15 * index)
        self.decided_at, self.dispatched_at = start + timedelta(seconds=5), start + timedelta(seconds=20)
        self.monkeypatch.setattr(decision_service, "datetime", _frozen(self.decided_at))
        body, code = decision_service.run_decision(None, self.runtime, self.cfg)
        assert code == 200, body
        return body

    def dispatch(self):
        self._patch_worker()
        return execution._run_order_worker(self.cfg, limit=1, runtime=self.runtime)

    def slot(self, index, price):
        return self.decide(index, price), self.dispatch()

    def run(self, path):
        return [self.slot(index, price) for index, price in enumerate(path)]

    # -- what the paper database holds
    @property
    def ck(self):
        return chain_key(self.cfg)

    def rows(self):
        rows = [r for r in (FAKE_DB.reference(ROWS_PATH).get() or {}).values()
                if isinstance(r, dict) and r.get("chain_key") == self.ck]
        return sorted(rows, key=lambda r: r["version"])

    def intents(self):
        return {k: v for k, v in (FAKE_DB.reference(
            f"webull_lego_order_outbox/{self.ck}").get() or {}).items() if isinstance(v, dict)}

    def state(self):
        return FAKE_DB.reference(f"{STATE_PATH}/{self.ck}").get() or {}


def refuse(*_args, **_kwargs):
    raise AssertionError("paper reached the money boundary")


def forbid_the_money_boundary(monkeypatch):
    monkeypatch.setattr(execution, "fence_chain_dispatch", refuse)
    monkeypatch.setattr(execution, "begin_place_attempt", refuse)
    monkeypatch.setattr(execution_limits, "reserve_attempt", refuse)


def halt_of(symbol):
    return operator_halt.status(webull_io.runtime_identity_fingerprint(), symbol)


def first_sell(session, path):
    """Run slots until the first SELL decision and return its index."""
    for index, price in enumerate(path):
        body = session.decide(index, price)
        if body["status"] == "READY_SELL":
            return index
        session.dispatch()
    raise AssertionError("the price path produced no SELL")


# ------------------------------------------------------------------ a whole session
def test_a_paper_session_runs_the_17_column_ledger_and_never_places(monkeypatch):
    session = Session(monkeypatch)
    forbid_the_money_boundary(monkeypatch)
    session.run(prices())

    rows, intents, state = session.rows(), session.intents(), session.state()
    assert len(rows) == len(SIGNS) + 1
    assert all(row["paper"] is True for row in rows) and state["paper"] is True
    fills = [i for i in intents.values() if i.get("paper_fill")]
    sides = {i["side"] for i in fills}
    assert sides == {"BUY", "SELL"} and len(fills) > 2           # more than the 2-order session cap
    assert all(i["status"] == "UNSENT_ABORTED" and not i.get("place_attempted") for i in fills)
    assert session.client.order_v3.place_order.calls == []
    assert len(session.client.order_v3.preview_order.calls) == len(fills) == len(intents)

    # The position walks by exactly the fills, and every READY row is finalized.
    for row, following in zip(rows, rows[1:]):
        if row["cashflow_status"] == "FINALIZED":
            assert following[HOLDINGS] == pytest.approx(row["post_execution_holdings"])
        else:
            assert following[HOLDINGS] == pytest.approx(row[HOLDINGS])
    assert all(r["cashflow_status"] == "FINALIZED" for r in rows if r["สถานะ"].startswith("READY"))
    assert state["prev_holdings"] == pytest.approx(rows[-1]["post_execution_holdings"]
                                                   if rows[-1]["cashflow_status"] == "FINALIZED"
                                                   else rows[-1][HOLDINGS])

    # The first fill is the initial funding: P_acted seeded from its own price, dA = A = E = 0.
    funding = rows[0]
    assert funding["initial_funding"] is True and funding[SIDE] == "BUY" and funding[HOLDINGS] == 0
    assert (funding[DELTA_COLUMN], funding[ACTUAL_COLUMN], funding[EXCESS_COLUMN]) == (0.0, 0.0, 0.0)
    assert funding["execution_price"] == pytest.approx(65.49 + 0.02)
    assert any(r[DELTA_COLUMN] != 0 for r in rows[1:])                      # the ledger moves

    # The repository's independent audit agrees with every row and every fill.
    p0 = float(state["p0"])
    rows_report = trace_audit.check_rows(rows, fix_c=FIX_C, diff=DIFF, p0=p0)
    fills_report = trace_audit.check_fills(state, rows, intents, {}, fix_c=FIX_C, p0=p0)
    assert rows_report["mismatch"]["count"] == 0, rows_report["mismatch"]
    assert fills_report["mismatch"]["count"] == 0 and fills_report["n"] == len(fills), fills_report
    assert not trace_audit.check_ledger(state, {}, fills_report)["problems"]

    # error 0, no fence and no quota.
    assert FAKE_DB.reference("webull_lego_warnings").get() is None
    assert FAKE_DB.reference("webull_lego_errors").get() is None
    scope = account_symbol_fence_key(webull_io.runtime_identity_fingerprint(), "UBER")
    fence = FAKE_DB.reference(f"{DISPATCH_LOCK_PATH}/{scope}").get() or {}
    assert not fence.get("inflight_run_id") and "execution_session" not in fence
    assert state["paper_broker_holdings"] == 0.0 and halt_of("UBER").get("halted") is not True


def test_paper_holdings_live_on_a_chain_no_other_mode_can_continue(monkeypatch):
    session = Session(monkeypatch)
    real = Config("UBER", FIX_C, DIFF, "bypass:1000", STRATEGY_ID)
    assert session.cfg.strategy_id == PAPER_STRATEGY_ID and session.ck != chain_key(real)
    session.slot(0, Decimal("65.49"))
    assert FAKE_DB.reference(f"{STATE_PATH}/{chain_key(real)}").get() is None


# --------------------------------------------------------------------------- SELL
def test_a_paper_sell_is_previewed_like_a_buy_and_gets_its_real_fee(monkeypatch):
    session = Session(monkeypatch)
    first_sell(session, prices())
    result = session.dispatch()["results"][0]
    sent = session.client.order_v3.preview_order.calls[-1][0][1][0]
    assert sent["side"] == "SELL" and result["status"] == "UNSENT_ABORTED"
    assert result["paper_fill"]["fee_estimate"] not in (None, "") and "sell_preview" not in result
    stored = read_intent(session.ck, result["run_id"])
    assert stored["preview_result"]["estimated_transaction_fee"] == result["paper_fill"]["fee_estimate"]


@pytest.mark.parametrize("answer", [
    {"error_code": "POSITION_NOT_ENOUGH", "error": "no position to sell"},   # HTTP 200, refused in the body
    BrokerError(417, "POSITION_NOT_ENOUGH"),                                    # refused as an HTTP error
])
def test_a_paper_sell_the_broker_refuses_is_recorded_and_filled_without_an_error(monkeypatch, capsys, answer):
    session = Session(monkeypatch, sell_answer=answer)
    forbid_the_money_boundary(monkeypatch)
    first_sell(session, prices())
    worker = session.dispatch()
    result = worker["results"][0]
    assert result["status"] == "UNSENT_ABORTED" and result["paper_fill"]["quantity"] > 0
    assert result["sell_preview"]["refused"] is True and result["paper_fill"]["fee_estimate"] is None
    stored = read_intent(session.ck, result["run_id"])
    assert stored["sell_preview"]["refused"] is True and "last_error" not in stored
    assert stored["cashflow_finalized"] is True
    row = FAKE_DB.reference(f"{ROWS_PATH}/{result['run_id']}").get()
    assert row["cashflow_status"] == "FINALIZED" and row[SIDE] == "SELL"
    assert FAKE_DB.reference("webull_lego_warnings").get() is None
    assert session.client.order_v3.place_order.calls == []
    observability.emit_tick({"pipeline_status": "TICK_OK", "decision": {}, "recovery": {"results": []},
                             "dispatch": worker}, 200)
    event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert event["severity"] not in {"ERROR", "WARNING"}
    assert event["execution"][0]["sell_preview"]["refused"] is True


def test_a_paper_sell_the_broker_cannot_answer_waits_like_a_buy(monkeypatch):
    monkeypatch.setattr(webull_io.time, "sleep", lambda *_a: None)    # the SDK retry's back-off
    session = Session(monkeypatch, sell_answer=BrokerError(503, "SERVICE_UNAVAILABLE"))
    first_sell(session, prices())
    result = session.dispatch()["results"][0]
    assert result["status"] == "PENDING_DISPATCH"
    stored = read_intent(session.ck, result["run_id"])
    assert stored["gate_defer_count"] == 1 and "paper_fill" not in stored
    row = FAKE_DB.reference(f"{ROWS_PATH}/{result['run_id']}").get()
    assert row["cashflow_status"] == "PENDING_EXECUTION"


# ------------------------------------------------------------------- the real account
def test_a_real_position_that_moves_between_decisions_halts_paper(monkeypatch):
    session = Session(monkeypatch)
    session.slot(0, Decimal("65.49"))
    session.real = 1.0                                   # someone bought a real share
    body = session.decide(1, Decimal("66.15"))
    assert body["status"] == "PASS_OPERATOR_HALT"
    halt = halt_of("UBER")
    assert halt["halted"] is True and halt["set_by"] == execution.PREVIEW_TRIPWIRE_OPERATOR
    assert len(session.intents()) == 1                   # nothing new was created


def test_a_real_position_that_moves_before_dispatch_halts_paper(monkeypatch):
    session = Session(monkeypatch)
    session.decide(0, Decimal("65.49"))
    session.real_at_dispatch = 1.0
    result = session.dispatch()["results"][0]
    assert result["status"] == "SUPPRESSED_STATE_CHANGED" and result["preview_tripwire"] is True
    assert halt_of("UBER")["halted"] is True
    assert session.client.order_v3.preview_order.calls == []        # stopped before the Preview
    assert session.state().get("execution_cashflow", {}).get("finalized_seq", 0) == 0


# ----------------------------------------------------------------------- recovery
def test_a_booked_paper_fill_that_lost_its_close_is_closed_on_that_booking(monkeypatch):
    session = Session(monkeypatch)
    session.decide(0, Decimal("65.49"))
    closes = []
    real_done = execution._paper_done

    def crash_once(*args, **kwargs):
        closes.append(1)
        if len(closes) == 1:
            raise RuntimeError("the worker died after booking, before the close")
        return real_done(*args, **kwargs)

    monkeypatch.setattr(execution, "_paper_done", crash_once)
    with pytest.raises(RuntimeError):
        session.dispatch()
    run_id = next(iter(session.intents()))
    assert read_intent(session.ck, run_id)["status"] == "PENDING_DISPATCH"
    assert session.state()["execution_cashflow"]["finalized_seq"] == 1
    result = session.dispatch()["results"][0]
    assert result["status"] == "UNSENT_ABORTED" and result["paper_fill"]["quantity"] > 0
    assert session.state()["execution_cashflow"]["finalized_seq"] == 1     # booked once
    assert len(session.client.order_v3.preview_order.calls) == 1           # no second Preview


# ------------------------------------------------------------ config, locks and logs
def test_paper_needs_the_release_but_never_the_real_money_acknowledgement():
    runtime = load_runtime_config(paper_values())
    assert runtime.operator.paper and runtime.operator.preview_only
    assert runtime.allows_new_broker_mutation and runtime.prod_live_gate_open is False
    inputs = rh.inputs_from_runtime(runtime)
    assert inputs.trading and not inputs.live
    assert set(rh.prod_live_acks(inputs, runtime.deployment.expected_release_binding).values()) == {None}
    trade = load_runtime_config({**paper_values(), "LEGO_MODE": "trade"})
    assert trade.deployment.release_is_authorized is False          # the binding includes the mode


def test_place_refuses_under_paper_before_any_broker_call(monkeypatch):
    monkeypatch.setenv("LEGO_MODE", "paper")
    client = fake_trade_client(place={"order_id": "never"})
    with pytest.raises(webull_io.WebullConfigError, match="never places"):
        webull_io.place_market_order(client, [{"client_order_id": "a" * 32}])
    assert client.order_v3.place_order.calls == []


def test_the_tick_log_carries_the_paper_fill(capsys):
    fill = {"price": 65.51, "quantity": 45.8, "holdings_after": 45.8, "fee_estimate": "3.21",
            "price_source": "last_trade_after_preview"}
    body = {"pipeline_status": "TICK_OK", "decision": {}, "recovery": {"results": []},
            "dispatch": {"results": [{"run_id": "r", "status": "UNSENT_ABORTED", "paper": True,
                                      "preview_only": True, "preview_fee_pct": "0.1070",
                                      "paper_fill": fill}]}}
    observability.emit_tick(body, 200)
    event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert event["execution"][0]["paper"] is True and event["execution"][0]["paper_fill"] == fill
    assert event["severity"] not in {"ERROR", "WARNING"}


def test_the_fill_audits_know_an_initial_funding_books_nothing():
    """A funding BUY seeds P_acted and books dA = A = E = 0, whatever its price was."""
    witness = {"seq": 1, "filled_price": 65.51, "previous_action_price": 65.49,
               "previous_actual_cumulative": 0.0, "reference": 0.0, "delta_actual": 0.0,
               "actual_cumulative": 0.0, "excess": 0.0, "holdings_after": 45.8,
               "filled_quantity": 45.8, "initial_funding": True}
    state = {"p0": 65.49, "execution_cashflow": {
        "finalized_runs": {"r": witness}, "actual_cumulative": 0.0, "excess": 0.0,
        "last_action_price": 65.51}}
    intents = {"r": {"side": "BUY", "decision_holdings": 0.0}}
    report = trace_audit.check_fills(state, [], intents, {}, fix_c=FIX_C, p0=65.49)
    assert report["mismatch"]["count"] == 0, report
    traced = {"checked": 0, "mismatch": trace_audit._bucket()}
    trace_audit._check_equation_event({"n": "W13", "in": {
        "FIX_C": FIX_C, "P_fill": 65.51, "P_acted": 65.49, "A_prev": 0.0},
        "out": {"delta_actual": 0.0, "actual_cumulative": 0.0, "excess": 0.0, "reference": 0.0,
                "initial_funding": True}}, traced)
    assert traced["checked"] == 1 and traced["mismatch"]["count"] == 0
    # Without the flag the same numbers are still a mismatch: the check did not go soft.
    report = trace_audit.check_fills(
        {**state, "execution_cashflow": {**state["execution_cashflow"], "finalized_runs": {
            "r": {**witness, "initial_funding": False}}}}, [], intents, {}, fix_c=FIX_C, p0=65.49)
    assert report["mismatch"]["count"] > 0
