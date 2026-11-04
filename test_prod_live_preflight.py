"""Account preflight of the PROD real-money release (tools/prod_live_preflight.py).

The release plan judges configuration; this judges the account it will trade, read-only,
right before the deploy: buying power, open orders, the instrument's fractional flag, the
type of every position row of the symbol, the LEAPS actually held (long calls x
multiplier, and when they expire), the price band of LEAPS against FIX_C and whether the
first order of a prefunded canary fits its caps. Numbers are the owner's release:
QBTS, FIX_C 1500, DIFF 50, LEAPS 100, fractional, at the late-October price 10.82.

T4 is the pure judgement as a table; T5 is the gathering, against SDK doubles that fail
the test if anything but a read is reached.
"""
from __future__ import annotations

import ast
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import webull_io
from conftest import FAKE_DB, FakeCall, FakeNamespace
from lego_orders import PROD, UAT
from tools import prod_live_preflight as pf

ROOT = Path(__file__).parent
WINDOW_END = datetime(2026, 11, 10, 21, 0, tzinfo=timezone.utc)


def params(**overrides):
    values = dict(symbol="QBTS", principal=Decimal("1500"), diff=Decimal("50"),
                  leaps=Decimal("100"), allow_fractional=True, window_end=WINDOW_END)
    values.update(overrides)
    return pf.Params(**values)


def call_leg(quantity="1", multiplier="100", expire="2028-01-21", option_type="CALL", symbol="QBTS"):
    return {"leg_id": "1", "symbol": symbol, "quantity": quantity, "option_type": option_type,
            "option_expire_date": expire, "option_exercise_price": "5.0",
            "option_contract_multiplier": multiplier, "option_contract_deliverable": multiplier,
            "expiration_type": "AM"}


def equity(quantity="38", symbol="QBTS", **extra):
    return {"position_id": "E1", "currency": "USD", "symbol": symbol, "instrument_type": "EQUITY",
            "quantity": quantity, "last_price": "10.82", "cost_price": "10.5",
            "unrealized_profit_loss": "0", **extra}


def option(legs=None, **extra):
    return {"position_id": "O1", "currency": "USD", "symbol": "QBTS", "instrument_type": "OPTION",
            "quantity": "1", "option_strategy": "SINGLE", "last_price": "7.0", "cost_price": "6.0",
            "unrealized_profit_loss": "0", "legs": [call_leg()] if legs is None else legs, **extra}


def facts(**overrides):
    values = {
        "environment": PROD, "endpoint_ok": True, "account_listed": True,
        "instrument": {"ok": True, "fractionable": True, "lot_size": "1",
                       "quantity_increment": "0.01", "decimal_precision": 2},
        "buying_power_usd": "1600.00",
        "positions": [equity(), option(), equity("3", symbol="UBER")],
        "open_orders": [], "history_probe": {"ok": True},
        "quote": {"price": "10.82", "quote_time": "2026-11-02T21:00:00.000Z"},
    }
    values.update(overrides)
    return values


def of(report, severity):
    return {f["id"] for f in report["findings"] if f["severity"] == severity}


def finding(report, id_):
    return next(f for f in report["findings"] if f["id"] == id_)


# ------------------------------------------------------------------ T4 the judgement
def test_a_prefunded_account_holding_its_leaps_passes_with_the_numbers_it_trades_on():
    report = pf.judge(facts(), params())
    assert report["ok"] is True and report["blocking"] == [], report["findings"]
    derived = report["derived"]
    assert derived["reference_price"] == "10.82"
    assert (derived["real_shares"], derived["leaps_declared"], derived["leaps_held"]) == ("38", "100", "100")
    assert derived["real_target"] == "38.63"                  # 1500 / 10.82 - 100
    assert derived["first_order"] is None                     # 138 x 10.82 - 1500 = -6.84: inside DIFF
    # Owner's rule 2026-11-04: the least that opens the next order. No first order here, so one
    # order at the canary cap (150 + 1 %); FIX_C + 1 % stays visible as a WARN threshold.
    assert derived["required_cash_usd"] == "151.50" and derived["own_cash_usd"] == "151.50"
    assert derived["principal_cash_usd"] == "1515.00"
    assert derived["caps"] == {"LEGO_MAX_ORDER_QUANTITY": "20", "LEGO_MAX_ORDER_NOTIONAL_USD": "150",
                               "LEGO_MAX_SESSION_ORDERS": "10"}
    assert derived["prefund_range"] == ["25", "52"] and derived["suggested_real_shares"] == "38"
    assert derived["price_band"] == {"buy_below": "14.5", "sell_below": "15"}
    assert derived["leaps_earliest_expiry"] == "2028-01-21"
    assert report["broker_mutations"] is False


@pytest.mark.parametrize("buying_power,ok,short_of_principal", [
    ("0", False, None), ("151.49", False, None),            # cannot pay the next order: BLOCK
    ("151.50", True, True), ("1514.99", True, True),        # can, but not FIX_C + 1 %: WARN
    ("1515.00", True, False), ("1515.01", True, False)])
def test_buying_power_must_open_the_next_order_and_warns_below_fix_c(buying_power, ok, short_of_principal):
    report = pf.judge(facts(buying_power_usd=buying_power), params())
    assert report["ok"] is ok
    assert ("buying_power" in report["blocking"]) is (not ok)
    if short_of_principal is not None:
        assert ("buying_power_principal" in report["warnings"]) is short_of_principal


def test_a_first_buy_is_the_cash_the_release_needs_now():
    """No real shares, 100 LEAPS held, 10.09: the first BUY of 491.00 (+ 1 %) is what opens it."""
    flat = facts(positions=[option()], quote={"price": "10.09", "quote_time": None},
                 buying_power_usd="495.91")
    report = pf.judge(flat, params(funding="initial-funding"))
    assert report["derived"]["first_order"]["notional_usd"] == "491.00"
    assert report["derived"]["required_cash_usd"] == "495.91" and report["ok"] is True
    assert "its first BUY of 491.00 USD" in finding(report, "buying_power")["message"]
    short = pf.judge({**flat, "buying_power_usd": "495.90"}, params(funding="initial-funding"))
    assert short["blocking"] == ["buying_power"]


def test_an_open_order_of_the_symbol_blocks():
    order = {"symbol": "QBTS", "client_order_id": "manual-1", "side": "BUY", "status": "SUBMITTED"}
    report = pf.judge(facts(open_orders=[order]), params())
    assert report["blocking"] == ["open_orders"]


@pytest.mark.parametrize("instrument,allow,expected", [
    ({"ok": True, "fractionable": False, "lot_size": "1", "quantity_increment": "1",
      "decimal_precision": 0}, True, ("WARN", True)),
    ({"ok": True, "fractionable": False, "lot_size": "1", "quantity_increment": "1",
      "decimal_precision": 0}, False, ("INFO", True)),
    ({"ok": False, "reason": "INSTRUMENT_FRACTIONABLE_UNKNOWN"}, True, ("BLOCK", False)),
    ({"ok": False, "reason": "INSTRUMENT_NOT_TRADABLE"}, True, ("BLOCK", False)),
])
def test_the_instrument_profile_decides_fractional_or_whole_shares(instrument, allow, expected):
    report = pf.judge(facts(instrument=instrument), params(allow_fractional=allow))
    severity, ok = expected
    fid = "instrument" if not instrument["ok"] else "fractional"
    assert finding(report, fid)["severity"] == severity and report["ok"] is ok


@pytest.mark.parametrize("row", [
    {"position_id": "X", "symbol": "QBTS", "quantity": "1"},                          # no type
    {"position_id": "X", "symbol": "QBTS", "instrument_type": "WARRANT", "quantity": "1"},
    {"position_id": "X", "symbol": "QBTS", "instrument_type": None, "quantity": "1"},
])
def test_a_position_row_of_the_symbol_without_a_known_type_blocks(row):
    """_extract_qty counts an untyped row as shares: a LEAPS row would be counted twice."""
    report = pf.judge(facts(positions=[equity(), option(), row]), params())
    assert "positions_shape" in report["blocking"]


def test_an_untyped_leaps_row_alone_is_not_read_as_one_share():
    """No stock row: _extract_qty alone would answer 1 real share from the LEAPS row."""
    untyped_leaps = {k: v for k, v in option().items() if k != "instrument_type"}
    import webull_io as io
    assert io._extract_qty([untyped_leaps], "QBTS") == 1.0          # what the runtime would read
    report = pf.judge(facts(positions=[untyped_leaps]), params())
    assert "positions_shape" in report["blocking"] and "real_shares" not in report["derived"]


def test_untyped_rows_of_other_symbols_do_not_matter():
    report = pf.judge(facts(positions=[equity(), option(), {"symbol": "AAPL", "quantity": "1"}]), params())
    assert report["ok"] is True


@pytest.mark.parametrize("payload", [{"error_code": "SYSTEM_ERROR"}, {"unexpected": []}, None,
                                     [equity(), equity("2")]])
def test_an_unreadable_positions_answer_blocks(payload):
    assert "positions_shape" in pf.judge(facts(positions=payload), params())["blocking"]


@pytest.mark.parametrize("legs,held", [
    ([call_leg("2")], "200"),
    ([call_leg(), call_leg(expire="2028-06-16")], "200"),
    ([call_leg("1", multiplier="10")], "10"),
])
def test_leaps_held_are_long_calls_times_the_multiplier(legs, held):
    report = pf.judge(facts(positions=[equity(), option(legs=legs)]), params())
    assert report["derived"]["leaps_held"] == held
    assert ("leaps_held" in report["blocking"]) is (Decimal(held) < 100)


@pytest.mark.parametrize("legs", [[call_leg("-1")], [call_leg(option_type="PUT")],
                                  [call_leg(symbol="UBER")]])
def test_short_calls_puts_and_other_underlyings_are_not_leaps(legs):
    report = pf.judge(facts(positions=[equity(), option(legs=legs)]), params())
    assert report["derived"]["leaps_held"] == "0"
    assert "leaps_held" in report["blocking"]
    confirmed = pf.judge(facts(positions=[equity(), option(legs=legs)]), params(leaps_confirmed=True))
    assert "leaps_held" in confirmed["blocking"]       # read and found short: no confirmation overrides


@pytest.mark.parametrize("positions", [
    [equity()],                                          # no option row at all
    [equity(), option(legs=[])],                         # an option row without legs
    [equity(), {**option(), "legs": None}],
    [equity(), option(legs=[{**call_leg(), "option_contract_multiplier": None}])],
    [equity(), option(legs=[{**call_leg(), "option_expire_date": "21/01/2028"}])],
])
def test_leaps_that_cannot_be_read_block_unless_the_owner_confirms_them(positions):
    report = pf.judge(facts(positions=positions), params())
    assert "leaps_held" in report["blocking"]
    confirmed = pf.judge(facts(positions=positions), params(leaps_confirmed=True))
    assert "leaps_held" not in confirmed["blocking"] and finding(confirmed, "leaps_held")["severity"] == "WARN"


@pytest.mark.parametrize("expire,severity", [("2026-11-06", "BLOCK"), ("2026-11-10", "BLOCK"),
                                             ("2026-12-15", "WARN"), ("2028-01-21", "INFO")])
def test_leaps_must_outlive_the_window(expire, severity):
    report = pf.judge(facts(positions=[equity(), option(legs=[call_leg(expire=expire)])]), params())
    assert finding(report, "leaps_expiry")["severity"] == severity


def test_no_declared_leaps_skips_every_leaps_check():
    report = pf.judge(facts(positions=[equity("138")]), params(leaps=Decimal("0")))
    assert not {f["id"] for f in report["findings"]} & {"leaps_held", "leaps_expiry", "leaps_value"}
    assert report["ok"] is True and report["derived"]["real_target"] == "138.63"


@pytest.mark.parametrize("price,severity", [("14.49", "INFO"), ("14.60", "WARN"), ("15.00", "BLOCK"),
                                            ("15.20", "BLOCK")])
def test_the_live_price_is_judged_against_the_leaps_band(price, severity):
    report = pf.judge(facts(quote={"price": price, "quote_time": "2026-11-02T21:00:00Z"},
                            positions=[equity("1"), option()]), params())
    assert finding(report, "leaps_value")["severity"] == severity


@pytest.mark.parametrize("real,ok,side", [
    ("0", False, "BUY"), ("24", False, "BUY"), ("25", True, "BUY"), ("38", True, None),
    ("52", True, "SELL"), ("53", False, "SELL"), ("120", False, "SELL"),
])
def test_the_first_order_of_a_prefunded_canary_must_fit_its_caps(real, ok, side):
    report = pf.judge(facts(positions=[equity(real), option()]), params())
    assert ("first_order" in report["blocking"]) is (not ok)
    first = report["derived"]["first_order"]
    assert (first or {}).get("side") == side
    if not ok:
        message = finding(report, "first_order")["message"]
        assert "25" in message and "52" in message and "38" in message


def test_a_flat_account_is_told_how_many_shares_to_buy_first():
    report = pf.judge(facts(positions=[option()]), params())
    first = report["derived"]["first_order"]
    assert (first["side"], first["notional_usd"]) == ("BUY", "418.00")
    assert report["blocking"] == ["first_order"]


@pytest.mark.parametrize("change,blocked", [
    ({"environment": UAT}, "environment"),
    ({"endpoint_ok": False}, "endpoint"),
    ({"account_listed": False}, "account"),
    ({"history_probe": {"ok": False, "error": "WebullConfigError"}}, "order_history_route"),
    ({"quote": {"price": None}}, "quote"),
    ({"quote": {"price": "0"}}, "quote"),
    ({"buying_power_usd": None}, "buying_power"),
])
def test_every_unproven_fact_blocks(change, blocked):
    assert blocked in pf.judge(facts(**change), params())["blocking"]


# ------------------------------------------------------------------ T5 gathering, read-only
class Response:
    def __init__(self, payload, status_code=200):
        self._payload, self.status_code = payload, status_code

    def json(self):
        return self._payload


class ReadOnlyOrders:
    """Open orders and order history only: any other order_v3 member fails the test."""

    def __init__(self, open_orders=None):
        self.list_order_open = FakeCall({"data": open_orders or []})
        self.list_order_history = FakeCall({"data": []})

    def __getattr__(self, name):
        raise AssertionError(f"the preflight reached order_v3.{name}")


QBTS_PROFILE = {"data": [{"symbol": "QBTS", "status": "OC", "currency": "USD", "lot_size": "1",
                          "category": "US_STOCK", "fractionable": True}]}


@pytest.fixture
def broker(monkeypatch):
    trade = FakeNamespace(
        account_v2=FakeNamespace(
            get_account_list=lambda: Response([{"account_id": "codex-test-account"}]),
            get_account_position=FakeCall([equity(), option()]),
            get_account_balance=FakeCall({"account_currency_assets": [
                {"currency": "USD", "cash_balance": "1700", "market_value": "411",
                 "buying_power": "1600.00", "unrealized_profit_loss": "0"}]})),
        order_v3=ReadOnlyOrders())
    data = object()
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "codex-test-account")
    monkeypatch.setattr(pf, "environment_label", lambda: PROD)
    monkeypatch.setattr(pf, "build_clients", lambda: (trade, data))
    monkeypatch.setattr(pf, "clients_endpoint", lambda t, d: webull_io.PROD_ENDPOINT)
    monkeypatch.setattr(pf, "fetch_instrument_capability",
                        lambda t, symbol: webull_io.parse_instrument_capability(QBTS_PROFILE, symbol))
    monkeypatch.setattr(pf, "fetch_snapshot", lambda t, d, cfg: {
        "price": 10.82, "quote_time": "2026-11-02T21:00:00.000Z",
        "captured_at": "2026-11-03T02:00:00Z", "holdings": 38.0})
    return trade


def test_gather_reads_the_account_and_never_writes_or_mutates(broker):
    before = json.dumps(FAKE_DB.store, sort_keys=True, default=str)
    report = pf.run(params())
    assert report["ok"] is True, report["findings"]
    assert report["derived"]["buying_power_usd"] == "1600.00"
    assert report["derived"]["real_shares"] == "38" and report["derived"]["leaps_held"] == "100"
    assert json.dumps(FAKE_DB.store, sort_keys=True, default=str) == before
    assert broker.order_v3.list_order_history.calls                 # the reconcile route was proven
    assert "codex-test-account" not in json.dumps(report)


def test_an_open_order_read_from_the_broker_blocks(broker):
    broker.order_v3.list_order_open = FakeCall({"data": [
        {"symbol": "QBTS", "client_order_id": "manual-1", "side": "BUY"}]})
    assert pf.run(params())["blocking"] == ["open_orders"]


def test_a_uat_environment_is_never_read(monkeypatch, broker):
    monkeypatch.setattr(pf, "environment_label", lambda: UAT)
    monkeypatch.setattr(pf, "build_clients", lambda: (_ for _ in ()).throw(AssertionError("built")))
    report = pf.run(params())
    assert "environment" in report["blocking"] and report["ok"] is False


def test_a_failure_is_a_block_with_no_secret_in_it(monkeypatch, broker):
    def refuse():
        raise webull_io.WebullConfigError("PROD requires hydrated token; app_secret=abc123 x-access-token: zzz")
    monkeypatch.setattr(pf, "build_clients", refuse)
    report = pf.run(params())
    assert report["ok"] is False and report["blocking"] == ["preflight_unavailable"]
    text = json.dumps(report)
    assert "WebullConfigError" in text and "abc123" not in text and "zzz" not in text


def test_the_cli_prints_json_and_exits_by_the_verdict(monkeypatch, capsys, broker):
    code = pf.main(["--symbol", "QBTS", "--fix-c", "1500", "--diff", "50", "--leaps", "100",
                    "--allow-fractional", "true", "--window-sessions", "5"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["ok"] is True and out["schema"] == pf.SCHEMA
    assert out["window_end_utc"].endswith("Z")
    broker.account_v2.get_account_balance = FakeCall({"account_currency_assets": [
        {"currency": "USD", "cash_balance": "10", "market_value": "0", "buying_power": "10",
         "unrealized_profit_loss": "0"}]})
    assert pf.main(["--symbol", "QBTS", "--fix-c", "1500", "--diff", "50", "--leaps", "100",
                    "--allow-fractional", "true"]) == 1


@pytest.mark.parametrize("argv", [
    ["--symbol", "QBTS", "--fix-c", "0", "--diff", "50", "--leaps", "100", "--allow-fractional", "true"],
    ["--symbol", "QBTS", "--fix-c", "1500", "--diff", "-1", "--leaps", "100", "--allow-fractional", "true"],
    ["--symbol", "QBTS", "--fix-c", "1500", "--diff", "50", "--leaps", "nan", "--allow-fractional", "true"],
    ["--symbol", "QBTS", "--fix-c", "1500", "--diff", "50", "--leaps", "100", "--allow-fractional", "yes"],
    ["--symbol", "", "--fix-c", "1500", "--diff", "50", "--leaps", "100", "--allow-fractional", "true"],
])
def test_the_cli_refuses_a_malformed_release(argv, capsys):
    assert pf.main(argv) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_the_tool_imports_no_order_mutation_and_no_firebase():
    tree = ast.parse((ROOT / "tools" / "prod_live_preflight.py").read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    forbidden = {"place_market_order", "cancel_order", "preview_market_order", "preview_market_order_result",
                 "replace_order", "firebase_admin", "main", "execution_service", "lego_outbox",
                 "decision_service", "order_recovery"}
    assert not names & forbidden, names & forbidden
