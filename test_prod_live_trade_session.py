"""A whole PROD trade session of the owner's release, against a broker double (audit 2026-11-03, T7).

Paper never crosses Place: this runs the real-money path itself — decision, outbox, the PROD
real-money gate (release authorization + acknowledgement), caps and session quota, Preview,
funding check, the chain fence, the durable Place marker, Place, Order Detail, the position read
back after the fill and the 17-column ledger — with WEBULL_ENV=PROD LEGO_MODE=trade
LEGO_ACTIVE=true LEGO_SYMBOL=QBTS LEGO_FIX_C=1500 LEGO_ALLOW_FRACTIONAL=true LEGO_DIFF=50
LEGO_LEAPS=100 and the canary caps 20 shares / 150 USD / 10 orders. Only broker I/O is doubled:
Place fills at the dispatch quote, Order Detail answers in the shape UAT returned on 26 Oct
(test_audit_20261029.FILLED_DETAIL), and the account's real QBTS shares move by the fill.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import decision_service
import execution_service as execution
import lego_outbox as outbox
import main
import observability
import release_horizon as rh
import webull_io
from config import load_runtime_config
from conftest import FAKE_DB, fake_trade_client
from lego_one_row import PASS_SELL_EXCEEDS_REAL, READY_BUY, READY_SELL, Config
from lego_state import ROWS_PATH, STATE_PATH, chain_key
from test_dispatch_overshoot import isolate  # noqa: F401  (autouse)
from test_fractional_quantity_incident import _frozen
from test_paper_mode import OPEN, paper_values
from tools import trace_audit
from webull_io import parse_instrument_capability

UTC = timezone.utc
PROD_DB = "https://lego-firebase-prod.asia-southeast1.firebasedatabase.app"
FIX_C, DIFF, LEAPS = 1500.0, 50.0, 100.0
SLIP = Decimal("0.01")                       # the quote moves a cent between decision and dispatch
HOLDINGS, STATUS = trace_audit.COL["holdings"], trace_audit.COL["status"]
QBTS_PROFILE = {"data": [{"symbol": "QBTS", "status": "OC", "currency": "USD",
                          "category": "US_STOCK", "lot_size": 1, "fractionable": True}]}


def trade_values(**overrides):
    # LEGO_MAX_SESSION_BUY_ORDERS defaults to the session cap here, so the sessions below
    # are bound by the 10 orders alone; the BUY cap has sessions of its own further down.
    caps = {"LEGO_MAX_ORDER_QUANTITY": "20", "LEGO_MAX_ORDER_NOTIONAL_USD": "150",
            "LEGO_MAX_SESSION_ORDERS": "10", "LEGO_MAX_SESSION_BUY_ORDERS": "10", **overrides}
    values = paper_values(
        LEGO_SYMBOL="QBTS", LEGO_FIX_C="1500", LEGO_DIFF="50", LEGO_LEAPS="100",
        LEGO_MODE="trade", LEGO_ACTIVE="true", LEGO_ALLOW_FRACTIONAL="true",
        FIREBASE_DB_URL=PROD_DB, **caps)
    assert load_runtime_config(values).allows_new_broker_mutation      # the binding alone
    assert rh.assess(rh.inputs_from_runtime(load_runtime_config(values)))["ok"]
    return values


def path(start, signs, step="0.04"):
    """Prices moving by *step* per slot ('0' = unchanged): 4 % of ~1,500 USD is a ~60 USD
    gap, above DIFF 50 and well inside the 150 USD canary cap."""
    factor = {"+": Decimal(1) + Decimal(step), "-": Decimal(1) - Decimal(step), "0": Decimal(1)}
    out = [Decimal(start)]
    for sign in signs:
        out.append((out[-1] * factor[sign]).quantize(Decimal("0.0001")))
    return out


class TradeSession:
    """Slots of one regular session on the real-money path; the broker double fills every Place."""

    def __init__(self, monkeypatch, *, real, buying_power="1600.00", price="10.82", values=None):
        webull_io.reset_clients()
        execution.configure(main)
        decision_service.configure(main)
        self.monkeypatch = monkeypatch
        self.real = Decimal(real)                      # the account's real QBTS shares
        self.buying_power = Decimal(buying_power)
        self.price = Decimal(price)
        values = values or trade_values()
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        self.runtime = load_runtime_config(values)
        assert self.runtime.allows_new_broker_mutation
        self.cfg = Config("QBTS", FIX_C, DIFF, "bypass:1000", self.runtime.operator.strategy_id,
                          leaps_shares=LEAPS)
        self.placed, self.details, self.violations = [], {}, []
        self.client = fake_trade_client(preview=self._preview, place=self._place,
                                        order_detail=self._detail)
        token = {"ok": True, "ready": True, "status": "NORMAL", "secret_configured": True,
                 "token_storage": "SECRET_MANAGER",
                 "expires_at": (datetime.now(UTC) + timedelta(days=14)).isoformat()}
        capability = parse_instrument_capability(QBTS_PROFILE, "QBTS")
        monkeypatch.setattr(decision_service, "build_clients", lambda: (object(), object()))
        monkeypatch.setattr(decision_service, "fetch_instrument_capability", lambda *_a: capability)
        monkeypatch.setattr(decision_service, "token_health", lambda: token)
        monkeypatch.setattr(decision_service, "fetch_snapshot",
                            lambda *_a: self._quote(self.decided_at, self.price))
        self.token = token

    # -- the broker double
    def _quote(self, at, price):
        return {"price": float(price), "holdings": float(self.real),
                "captured_at": at.isoformat(), "quote_time": at.isoformat()}

    def _preview(self, _account, orders):
        cost = (Decimal(orders[0]["quantity"]) * (self.price + SLIP)).quantize(Decimal("0.01"))
        return {"estimated_cost": str(cost),
                "estimated_transaction_fee": str((cost * Decimal("0.0011")).quantize(Decimal("0.01")))}

    def _place(self, _account, orders):
        order = orders[0]
        cid, qty, side = order["client_order_id"], Decimal(order["quantity"]), order["side"]
        price = self.price + SLIP
        if side == "SELL" and not qty < self.real:
            self.violations.append(f"SELL {qty} with {self.real} real shares")
        if not re.fullmatch(r"\d+(\.\d{1,2})?", order["quantity"]):
            self.violations.append(f"quantity {order['quantity']} has more than 2 decimals")
        self.placed.append({"side": side, "quantity": qty, "price": price,
                            "real_before": self.real, "notional": qty * price, "order": order})
        self.real = self.real + qty if side == "BUY" else self.real - qty
        self.buying_power += -(qty * price) if side == "BUY" else qty * price
        broker_id = f"QBTS{len(self.placed):022d}"
        fee = (qty * price * Decimal("0.0011")).quantize(Decimal("0.01"))
        self.details[cid] = {
            "client_order_id": cid, "combo_order_id": broker_id, "combo_type": "NORMAL",
            "orders": [{"client_order_id": cid,
                        "commission": {"actual_commission": str(fee), "receivable_commission": str(fee)},
                        "entrust_type": "QTY",
                        "fees": [{"actual_value": "0.00", "receivable_value": "0.00", "type": "GST"}],
                        "filled_price": f"{price:.6f}", "filled_quantity": f"{qty:.6f}",
                        "instrument_type": "EQUITY", "order_id": broker_id, "order_type": "MARKET",
                        "side": side, "status": "FILLED", "support_trading_session": "CORE",
                        "symbol": "QBTS", "time_in_force": "DAY", "total_quantity": f"{qty:.6f}"}]}
        return {"client_order_id": cid, "order_id": broker_id}

    def _detail(self, _account, client_order_id):
        return self.details[client_order_id]

    # -- one slot
    def decide(self, index, price):
        self.price = Decimal(str(price))
        start = OPEN + timedelta(minutes=15 * index)
        self.decided_at, self.dispatched_at = start + timedelta(seconds=5), start + timedelta(seconds=20)
        self.monkeypatch.setattr(decision_service, "datetime", _frozen(self.decided_at))
        body, code = decision_service.run_decision(None, self.runtime, self.cfg)
        assert code == 200, body
        return body

    def dispatch(self):
        patch = self.monkeypatch.setattr
        patch(execution, "build_clients", lambda: (self.client, object()))
        patch(execution, "fetch_open_orders", lambda *_a: [])
        patch(execution, "fetch_snapshot", lambda *_a: self._quote(self.dispatched_at, self.price + SLIP))
        patch(execution, "fetch_buying_power", lambda *_a: self.buying_power)
        patch(execution, "fetch_holdings", lambda *_a: float(self.real))
        patch(execution, "token_health", lambda: self.token)
        patch(execution, "datetime", _frozen(self.dispatched_at))
        patch(outbox, "datetime", _frozen(self.dispatched_at))
        return execution._run_order_worker(self.cfg, limit=1, runtime=self.runtime)

    def run(self, prices):
        return [(self.decide(index, price), self.dispatch()) for index, price in enumerate(prices)]

    # -- what the production database holds
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


def audited(session):
    rows, intents, state = session.rows(), session.intents(), session.state()
    p0 = float(state["p0"])
    rows_report = trace_audit.check_rows(rows, fix_c=FIX_C, diff=DIFF, p0=p0)
    fills_report = trace_audit.check_fills(state, rows, intents, {}, fix_c=FIX_C, p0=p0)
    assert rows_report["mismatch"]["count"] == 0, rows_report["mismatch"]
    assert fills_report["mismatch"]["count"] == 0, fills_report["mismatch"]
    return rows, intents, state


# ------------------------------------------------------------------ a whole session, prefunded
def test_a_prefunded_qbts_session_trades_real_shares_around_100_leaps(monkeypatch):
    session = TradeSession(monkeypatch, real="38.63")              # FIX_C / 10.82 - 100
    prices = path("10.82", "+00-00+00-00-00+00+00-000")             # 26 slots, 8 moves of 4 %
    session.run(prices)
    rows, intents, state = audited(session)
    assert session.violations == []
    assert len(rows) == len(prices)
    sides = [p["side"] for p in session.placed]
    assert {"BUY", "SELL"} <= set(sides) and len(session.placed) >= 6
    # One Place per READY row, each with its own client order id, all filled and booked.
    ready = [r for r in rows if r[STATUS] in (READY_BUY, READY_SELL)]
    assert len(session.placed) == len(ready) == len(intents)
    assert len({p["order"]["client_order_id"] for p in session.placed}) == len(session.placed)
    assert all(i["status"] == "FILLED" and i["place_attempted"] for i in intents.values())
    assert all(r["cashflow_status"] == "FINALIZED" for r in ready)
    for placed in session.placed:
        order = placed["order"]
        assert (order["symbol"], order["order_type"], order["time_in_force"], order["entrust_type"],
                order["support_trading_session"]) == ("QBTS", "MARKET", "DAY", "QTY", "CORE")
        assert placed["quantity"] <= 20 and placed["notional"] <= Decimal("150") * Decimal("1.01")
    # Column 7 is real + LEAPS on every row; the chain's position is the real shares.
    for row in rows:
        assert row["leaps_shares"] == LEAPS
        assert row[HOLDINGS] == float(Decimal(str(row["real_holdings"])) + Decimal("100"))
        if row["cashflow_status"] == "FINALIZED":
            assert row["post_execution_holdings"] == float(
                Decimal(str(row["post_execution_real_holdings"])) + Decimal("100"))
    assert state["prev_holdings"] == pytest.approx(float(session.real))
    assert 0 < session.real and all(p["real_before"] > 0 for p in session.placed)
    # The broker's real position moved by exactly the fills.
    assert session.real == Decimal("38.63") + sum(
        p["quantity"] if p["side"] == "BUY" else -p["quantity"] for p in session.placed)


def test_a_volatile_session_stops_at_the_canary_order_cap_and_pages(monkeypatch):
    """16 READY slots in one session: 10 orders go out, the rest are blocked by the cap (R8)."""
    session = TradeSession(monkeypatch, real="38.63")
    prices = path("10.82", "+-+--+-++-+-+--+-++-+--+-")
    outs = [out for _body, out in session.run(prices)]
    rows, intents, _state = audited(session)
    assert session.violations == []
    ready = [r for r in rows if r[STATUS] in (READY_BUY, READY_SELL)]
    assert len(ready) == 16 and len(session.placed) == 10            # LEGO_MAX_SESSION_ORDERS
    filled = [i for i in intents.values() if i["status"] == "FILLED"]
    blocked = [i for i in intents.values() if i.get("execution_limit_blocked") is True]
    assert len(filled) == 10 and len(blocked) == 6
    assert all(not i.get("place_attempted") for i in blocked)        # never reached Place
    statuses = [observability.business_status({"dispatch": out}, 200) for out in outs]
    assert statuses.count("EXECUTION_LIMIT_BLOCKED") == 6
    assert observability.severity_for("EXECUTION_LIMIT_BLOCKED") == "ERROR"
    assert session.real > 0


# ------------------------------------------- the BUY cap and the clip (2026-11-07)
def test_a_falling_session_buys_three_times_then_reports_the_spent_buy_quota(monkeypatch):
    """LEGO_MAX_SESSION_BUY_ORDERS=3: BUY 4 onward stops before its Preview, a SELL still goes."""
    session = TradeSession(monkeypatch, real="38.63",
                           values=trade_values(LEGO_MAX_SESSION_BUY_ORDERS="3"))
    prices = path("10.82", "-----+++")                               # 5 falls of 4 %, then up
    outs = [out for _body, out in session.run(prices)]
    rows, intents, _state = audited(session)
    assert session.violations == []
    sides = [p["side"] for p in session.placed]
    assert sides.count("BUY") == 3 and "SELL" in sides
    capped = [i for i in intents.values() if i.get("session_buy_cap_reached") is True]
    # Falls 4 and 5 and the first rise (still below FIX_C - DIFF) ask for a BUY; all three wait.
    assert len(capped) == 3 and all(i["status"] == "NOT_PLACED" and i["side"] == "BUY" for i in capped)
    assert all(not i.get("place_attempted") and not i.get("preview_result") for i in capped)
    assert all(not i.get("execution_limit_blocked") for i in intents.values())
    assert len(session.client.order_v3.preview_order.calls) == len(session.placed)
    statuses = [observability.business_status({"dispatch": out}, 200) for out in outs]
    assert statuses.count("SESSION_BUY_CAP_REACHED") == 3
    assert observability.severity_for("SESSION_BUY_CAP_REACHED") == "WARNING"
    import alerting
    assert "SESSION_BUY_CAP_REACHED" not in alerting.ACTION_EVENTS
    counter = FAKE_DB.reference(outbox.DISPATCH_LOCK_PATH).get() or {}
    session_doc = next(doc["execution_session"] for doc in counter.values()
                       if isinstance(doc, dict) and "execution_session" in doc)
    assert session_doc["buy_count"] == 3 and session_doc["count"] == len(session.placed)


def test_a_gap_above_the_notional_cap_is_clipped_and_closed_by_the_next_slot(monkeypatch):
    """The owner's rule (ก): a 220-ish USD gap buys 148.5 now and the rest next slot."""
    session = TradeSession(monkeypatch, real="38.63")
    prices = path("10.82", "-0", step="0.15")                        # one fall of 15 %, then flat
    bodies = [body for body, _out in session.run(prices)]
    rows, intents, _state = audited(session)                          # trace_audit agrees
    assert session.violations == []
    first, second = session.placed[0], session.placed[1]
    assert first["side"] == second["side"] == "BUY"
    assert Decimal("148") <= first["quantity"] * Decimal(str(prices[1])) <= Decimal("148.52")
    assert first["notional"] <= Decimal("150")                         # the dispatch cap held
    assert rows[1]["clipped_from_quantity"] > float(first["quantity"])
    assert rows[1]["order_caps"] == {"quantity": "20", "notional_usd": "150", "price_margin": "0.01"}
    assert "clipped_from_quantity" not in rows[2]                     # the rest fits the caps
    assert bodies[1]["order_clipped"]["from_quantity"] == rows[1]["clipped_from_quantity"]
    assert "order_clipped_large_gap" not in bodies[1]
    clipped = next(i for i in intents.values() if i.get("clipped_from_quantity"))
    assert clipped["order_caps"] == rows[1]["order_caps"] and clipped["status"] == "FILLED"
    assert all(not i.get("execution_limit_blocked") for i in intents.values())
    # The two orders together close the gap: the position is back within DIFF of FIX_C.
    value = (session.real + 100) * prices[-1]
    assert abs(value - Decimal("1500")) <= Decimal("50")


def test_a_gap_of_several_caps_is_clipped_and_pages_a_person(monkeypatch):
    session = TradeSession(monkeypatch, real="38.63")
    prices = path("10.82", "-", step="0.35")                         # 1500 -> ~975: gap ~525
    body, out = session.run(prices)[1]
    rows, intents, _state = audited(session)
    assert body["order_clipped_large_gap"] is True
    placed = session.placed[0]
    assert placed["side"] == "BUY" and placed["quantity"] == 20      # the share cap binds first
    assert observability.business_status({"decision": body, "dispatch": out}, 200) == (
        "ORDER_CLIPPED_LARGE_GAP")
    assert observability.severity_for("ORDER_CLIPPED_LARGE_GAP") == "ERROR"
    import alerting
    assert "ORDER_CLIPPED_LARGE_GAP" in alerting.ACTION_EVENTS


def test_inside_the_band_no_buy_and_above_fifteen_no_order_at_all(monkeypatch):
    session = TradeSession(monkeypatch, real="7.14", price="14.00")  # FIX_C / 14 - 100
    prices = [Decimal(p) for p in ("14.00", "14.60", "14.90", "15.10", "15.40", "14.40", "13.90")]
    session.run(prices)
    rows, _intents, _state = audited(session)
    assert session.violations == []
    statuses = [r[STATUS] for r in rows]
    assert statuses[1] == READY_SELL                                 # 107.14 x 14.60: sell real only
    assert statuses[3] == statuses[4] == PASS_SELL_EXCEEDS_REAL      # LEAPS alone >= FIX_C
    assert all(r[STATUS] != READY_BUY for r in rows if r["ราคา Pₙ (USD)"] >= 14.5)
    assert statuses[6] == READY_BUY                                  # back below 14.50
    assert [p["side"] for p in session.placed] == ["SELL", "BUY"]
    assert session.real > 0


def test_a_buy_the_account_cannot_pay_is_refused_before_place_and_pages(monkeypatch):
    session = TradeSession(monkeypatch, real="38.63", buying_power="10.00")
    session.decide(0, Decimal("10.82"))
    session.dispatch()
    body = session.decide(1, Decimal("10.3872"))                    # -4 %: a ~60 USD BUY
    assert body["status"] == READY_BUY
    out = session.dispatch()
    assert session.placed == []                                      # Place never reached
    result = out["results"][0]
    assert result["status"] == "NOT_PLACED" and "buying power" in result["error"]
    business = observability.business_status({"dispatch": out}, 200)
    assert business == "ERROR" and observability.severity_for(business) == "ERROR"
