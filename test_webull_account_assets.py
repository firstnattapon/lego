"""Thai account assets contract and incident recovery, with no broker network IO."""
from decimal import Decimal

import pytest
from webull.core.exception.exceptions import ServerException

import tick_runtime
import webull_io
from conftest import fake_data_client, fake_trade_client
from lego_one_row import Config

CFG = Config(symbol="PFE", fix_c=3000)
POSITIONS = [{"symbol": "PFE", "instrument_type": "EQUITY", "quantity": "7.25"}]
BALANCE = {"account_currency_assets": [{"currency": "USD", "buying_power": "1234.56"}]}


def system_error():
    return ServerException("OPENAPI_SYSTEM_ERROR", "System error.", http_status=417, request_id="test-request")


@pytest.mark.parametrize("reader,method,expected", [
    (lambda trade: webull_io.fetch_holdings(trade, CFG), "get_account_position", 7.25),
    (webull_io.fetch_buying_power, "get_account_balance", Decimal("1234.56")),
])
def test_account_assets_use_sdk3_account_methods(monkeypatch, reader, method, expected):
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "  test-account  ")
    trade = fake_trade_client(positions=POSITIONS, balance=BALANCE)
    assert reader(trade) == expected
    assert getattr(trade.account_v2, method).calls == [(("test-account",), {})]


@pytest.mark.parametrize("asset,method", [("positions", "get_account_position"), ("balance", "get_account_balance")])
def test_system_error_retries_safe_reads(monkeypatch, asset, method):
    sleeps = []
    monkeypatch.setattr(webull_io.time, "sleep", sleeps.append)
    attempts = {"n": 0}
    def flaky(*_args, **_kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise system_error()
        return POSITIONS if asset == "positions" else BALANCE
    trade = fake_trade_client(**{asset: flaky})
    if asset == "positions":
        assert webull_io.fetch_holdings(trade, CFG) == 7.25
    else:
        assert webull_io.fetch_buying_power(trade) == Decimal("1234.56")
    assert sleeps == [2.0, 4.0]
    assert len(getattr(trade.account_v2, method).calls) == 3


def test_exhausted_417_never_reaches_market_data_or_place(monkeypatch):
    sleeps = []
    monkeypatch.setattr(webull_io.time, "sleep", sleeps.append)
    error = system_error()
    trade = fake_trade_client(positions=error)
    data = fake_data_client(snapshot={"last": 25})
    with pytest.raises(ServerException) as caught:
        webull_io.fetch_snapshot(trade, data, CFG)
    assert caught.value is error
    assert len(trade.account_v2.get_account_position.calls) == 3
    assert sleeps == [2.0, 4.0]
    assert data.market_data.get_snapshot.calls == []
    assert trade.order_v3.place_order.calls == []


@pytest.mark.parametrize("code,status", [("OPENAPI_PARAM_ERR", 417), ("UNAUTHORIZED", 401), ("FORBIDDEN", 403)])
def test_business_and_auth_errors_do_not_retry(monkeypatch, code, status):
    sleeps = []
    monkeypatch.setattr(webull_io.time, "sleep", sleeps.append)
    trade = fake_trade_client(positions=ServerException(code, http_status=status))
    with pytest.raises(ServerException):
        webull_io.fetch_holdings(trade, CFG)
    assert len(trade.account_v2.get_account_position.calls) == 1
    assert sleeps == []


def test_417_on_place_is_never_retried(monkeypatch):
    sleeps = []
    monkeypatch.setattr(webull_io.time, "sleep", sleeps.append)
    trade = fake_trade_client(place=system_error())
    with pytest.raises(ServerException):
        webull_io.place_market_order(trade, [{"client_order_id": "test-order"}])
    assert len(trade.order_v3.place_order.calls) == 1
    assert sleeps == []


def test_account_retry_respects_tick_deadline(monkeypatch):
    sleeps = []
    monkeypatch.setattr(webull_io.time, "sleep", sleeps.append)
    trade = fake_trade_client(positions=system_error())
    with tick_runtime.tick_scope("account-retry", seconds=3):
        with pytest.raises(tick_runtime.TickDeadlineExceeded):
            webull_io.fetch_holdings(trade, CFG)
    assert len(trade.account_v2.get_account_position.calls) == 1
    assert sleeps == []


@pytest.mark.parametrize("payload", [
    {"positions": None}, {"items": False}, {"data": ""}, {"positions": 0}, {"data": {}},
    {"error_code": "OPENAPI_SYSTEM_ERROR", "positions": []}, [None], [{}], [{"symbol": ""}],
    [{"symbol": 123}], [{"symbol": "PFE"}], [{"symbol": "PFE", "quantity": None}],
    [{"symbol": "PFE", "quantity": True}], [{"symbol": "PFE", "quantity": ""}],
    [{"symbol": "PFE", "quantity": "NaN"}], [{"symbol": "PFE", "quantity": "Infinity"}],
    [{"symbol": "PFE", "quantity": "1e9999"}], [{"symbol": "PFE", "quantity": "-1"}],
    POSITIONS * 2, [{"symbol": "PFE", "quantity": "1", "instrument_type": "UNKNOWN"}],
])
def test_unknown_holdings_never_become_zero(payload):
    trade = fake_trade_client(positions=payload)
    data = fake_data_client(snapshot={"last": 25})
    with pytest.raises(ValueError, match="fail closed"):
        webull_io.fetch_snapshot(trade, data, CFG)
    assert len(trade.account_v2.get_account_position.calls) == 1
    assert data.market_data.get_snapshot.calls == []


@pytest.mark.parametrize("payload,expected", [
    ([], 0), ({"positions": []}, 0), ([{"symbol": "OTHER", "quantity": "3"}], 0),
    ([{"symbol": "PFE", "quantity": "0"}], 0), (POSITIONS, 7.25),
    ([{"symbol": "PFE", "instrument_type": "OPTION", "quantity": "99"}] + POSITIONS, 7.25),
])
def test_valid_positions_preserve_quantity_and_ignore_options(payload, expected):
    assert webull_io.fetch_holdings(fake_trade_client(positions=payload), CFG) == expected


def test_blank_account_fails_before_request(monkeypatch):
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", " ")
    trade = fake_trade_client(positions=POSITIONS)
    with pytest.raises(webull_io.WebullConfigError, match="WEBULL_ACCOUNT_ID"):
        webull_io.fetch_holdings(trade, CFG)
    assert trade.account_v2.get_account_position.calls == []
