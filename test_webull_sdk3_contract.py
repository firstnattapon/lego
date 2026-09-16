"""Lock the official Webull 3.0.1 Thailand request routes used by the money path."""
import webull
from webull.core.http.initializer.config.bean.query_config_request import GetConfigRequest
from webull.core.http.initializer.token.bean.check_token_request import CheckTokenRequest
from webull.core.http.initializer.token.bean.create_token_request import CreateTokenRequest
from webull.data.request.get_snapshot_request import GetSnapshotRequest
from webull.trade.request.v2.get_account_balance_request import AccountBalanceRequest
from webull.trade.request.v2.get_account_list_request import GetAccountListRequest
from webull.trade.request.v2.get_account_positions_request import AccountPositionsRequest
from webull.trade.request.v3.get_order_detail_request import OrderDetailRequest
from webull.trade.request.v3.get_order_open_request import OrderOpenRequest
from webull.trade.request.v3.place_order_request import PlaceOrderRequest
from webull.trade.request.v3.preview_order_request import PreviewOrderRequest


def _route(request):
    return request.get_action_name(), request.get_version(), request.get_method()


def test_pinned_sdk_version_is_current_route_contract():
    assert webull.__version__ == "3.0.1"


def test_auth_and_account_routes_match_thailand_contract():
    assert _route(GetConfigRequest()) == ("/openapi/config", "v3", "GET")
    assert _route(CreateTokenRequest()) == ("/auth/tokens/create", "v3", "POST")
    assert _route(CheckTokenRequest()) == ("/auth/tokens/check", "v3", "POST")
    assert _route(GetAccountListRequest()) == ("/trading/accounts/list", "v3", "GET")
    assert _route(AccountBalanceRequest()) == ("/trading/assets/balances/get", "v3", "GET")
    assert _route(AccountPositionsRequest()) == ("/trading/assets/positions/list", "v3", "GET")


def test_order_and_market_data_routes_are_not_legacy_openapi_paths():
    assert _route(PreviewOrderRequest()) == ("/trading/orders/preview", "v3", "POST")
    assert _route(PlaceOrderRequest()) == ("/trading/orders/place", "v3", "POST")
    assert _route(OrderDetailRequest()) == ("/trading/orders/get", "v3", "GET")
    assert _route(GetSnapshotRequest()) == ("/market-data/stocks/snapshots/list", "v3", "GET")
    assert _route(OrderOpenRequest()) == ("/trading/orders/open-orders/list", "v2", "GET")
