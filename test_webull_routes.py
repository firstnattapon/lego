"""The routes the pinned SDK really sends for every call webull_io makes.

webull_io names each broker call by its route (_OPERATION_BY_ROUTE). The name keys
the tick phase, the flight recorder's projection and broker_error_details. A route
the map does not know runs as a bare "sdk_request": the order-history fallback did,
because the map said /trading/orders/history/list while SDK 3.0.1 sends
/trading/orders/historical-orders/list, so a history page was traced whole, other
symbols' orders included, and its errors carried no operation.

These tests run the adapter's own functions on the SDK's own operation classes,
with only the HTTP exchange replaced, so an SDK upgrade that moves a route fails
here rather than in a reconcile.
"""
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from webull.data.quotes.market_data import MarketData
from webull.trade.trade.v2.account_info_v2 import AccountV2
from webull.trade.trade.v3.order_operation_v3 import OrderOperationV3

import flight_recorder
import webull_io
from lego_one_row import Config

CLIENT_ID = "c" * 32
CFG = Config(symbol="UBER", fix_c=3000.0, decimal_precision=2)


class _Exchange:
    """Stands in for ApiClient.get_response: keeps each request, answers {}."""

    def __init__(self, payload=None):
        self.requests = []
        self.payload = {} if payload is None else payload

    def get_response(self, request):
        self.requests.append(request)
        return SimpleNamespace(status_code=200, headers={}, json=lambda: self.payload)


def _clients(exchange):
    trade = SimpleNamespace(account_v2=AccountV2(exchange), order_v3=OrderOperationV3(exchange),
                            trade_instrument=SimpleNamespace(client=exchange))
    return trade, SimpleNamespace(market_data=MarketData(exchange))


def _since():
    return (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()


ADAPTER_CALLS = {
    "accounts": lambda t, d: webull_io.verify_account_access(t),
    "positions": lambda t, d: webull_io.fetch_holdings(t, CFG),
    "balance": lambda t, d: webull_io.fetch_buying_power(t),
    "instrument": lambda t, d: webull_io.fetch_instrument_capability(t, "UBER"),
    "preview": lambda t, d: webull_io.preview_market_order_result(
        t, webull_io.build_order_payload(CFG, "BUY", 1, CLIENT_ID)),
    "place": lambda t, d: webull_io.place_market_order(
        t, webull_io.build_order_payload(CFG, "BUY", 1, CLIENT_ID)),
    "order_detail": lambda t, d: webull_io.fetch_order_detail(t, CLIENT_ID),
    "cancel": lambda t, d: webull_io.cancel_order(t, CLIENT_ID),
    "open_orders": lambda t, d: webull_io.fetch_open_orders(t, "UBER"),
    "order_history": lambda t, d: webull_io.find_recent_order_by_client_id(
        t, CLIENT_ID, placed_at=_since()),
}


@pytest.mark.parametrize("operation", sorted(ADAPTER_CALLS))
def test_every_adapter_call_sends_a_route_the_map_names(monkeypatch, operation):
    monkeypatch.delenv("LEGO_MODE", raising=False)      # place_market_order refuses preview/paper
    exchange = _Exchange()
    trade, data = _clients(exchange)
    with suppress(Exception):                           # {} is no valid answer; only the request counts
        ADAPTER_CALLS[operation](trade, data)
    routes = [request.get_action_name() for request in exchange.requests]
    assert routes, f"{operation} sent no request"
    assert [webull_io._OPERATION_BY_ROUTE.get(route) for route in routes] == [operation], routes


def test_the_snapshot_read_sends_positions_then_the_snapshot_route():
    exchange = _Exchange({"data": []})                  # no position: holdings 0, then the quote
    trade, data = _clients(exchange)
    with suppress(Exception):
        webull_io.fetch_snapshot(trade, data, CFG)
    assert [webull_io._OPERATION_BY_ROUTE.get(r.get_action_name())
            for r in exchange.requests] == ["positions", "snapshot"]


def test_the_map_holds_only_routes_the_adapter_sends():
    assert set(webull_io._OPERATION_BY_ROUTE.values()) == set(ADAPTER_CALLS) | {"snapshot"}
    assert len(set(webull_io._OPERATION_BY_ROUTE.values())) == len(webull_io._OPERATION_BY_ROUTE)


def _history_through_the_adapter(monkeypatch, base):
    seen = []
    monkeypatch.setattr(flight_recorder, "webull_exchange",
                        lambda op, route, *a, **k: seen.append((op, route)))
    trade = SimpleNamespace(order_v3=OrderOperationV3(webull_io._bounded_api_class(base)()))
    return trade, seen


def test_order_history_is_traced_as_order_history(monkeypatch):
    class Base:
        def get_response(self, request):
            return SimpleNamespace(status_code=200, headers={}, json=lambda: {"data": []})

    trade, seen = _history_through_the_adapter(monkeypatch, Base)
    assert webull_io.find_recent_order_by_client_id(trade, CLIENT_ID, placed_at=_since()) is None
    assert seen == [("order_history", "/trading/orders/historical-orders/list")]


def test_a_refused_order_history_names_its_operation(monkeypatch):
    class Refused(Exception):
        http_status = 400
        error_code = "OPENAPI_PARAM_ERR"

    class Base:
        def get_response(self, request):
            raise Refused("start_time")

    trade, seen = _history_through_the_adapter(monkeypatch, Base)
    with pytest.raises(Refused) as caught:
        webull_io.find_recent_order_by_client_id(trade, CLIENT_ID, placed_at=_since())
    assert seen == [("order_history", "/trading/orders/historical-orders/list")]
    assert webull_io.broker_error_details(caught.value) == {
        "http_status": 400, "code": "OPENAPI_PARAM_ERR", "operation": "order_history",
        "retryable_read": False}
