# Webull contract sources

Retrieved from the official Webull Thailand developer site on 2026-09-05.
The adapter uses the pinned SDK for signing; these hashes bind the reviewed
Markdown/OpenAPI contracts without storing credentials or broker responses.

| Contract | Official URL | SHA-256 |
|---|---|---|
| SDK | https://developer.webull.co.th/apis/docs/sdk.md | `614ef29ba9978573a181ebdf04f6bd321c8874ae386669d1d90359342c13f866` |
| Token | https://developer.webull.co.th/apis/docs/authentication/token.md | `9d444fd3ed265563f3beea58be4236ace6659aad691a8aa475a9ce14b007154e` |
| Account balance | https://developer.webull.co.th/apis/docs/reference/trade-api/account-balance.md | `3da176c01de05fa4c3bddeaebcb76489756e40d6b58fa806094cdd1e8e807c43` |
| Preview | https://developer.webull.co.th/apis/docs/reference/trade-api/common-order-preview.md | `6850ee736225fd8d8ed3a00158a90f4226bc077a4644dd8e04ffe83967b071e3` |
| Place | https://developer.webull.co.th/apis/docs/reference/trade-api/common-order-place.md | `7d9cd45bf1cbdb9d609f1678cbca6ed1ca7bd91b5bc948611154999d66b990ba` |
| Detail | https://developer.webull.co.th/apis/docs/reference/trade-api/order-detail.md | `ddf0eb802ea83ce084dcd625e6d07784853f841dab0074ef915fea70cb2aa58` |

The documented balance contract exposes `account_currency_assets[].buying_power`.
The position contract exposes `quantity`, not a separate stock `sellable_quantity`;
therefore the v2 pre-Place rule conservatively requires a complete empty
open-order scan and limits SELL to the freshly read position quantity.

## Open Orders route update (2026-09-24 review)

The pinned Webull 3.0.1 wheel exposes two methods for the same path. The old
`get_order_open(account_id, page_size, last_client_order_id)` uses `x-version=v2`.
The active adapter now calls `list_order_open(account_id, pagination_key)` using
`x-version=v3` and requires the `{data: [...], pagination_key: ...}` response.
It does not infer completion from page length. An absent cursor ends the scan;
malformed/repeated cursors and unreadable group legs block new orders.

This route change follows the [official 2026-09-05 cursor migration](https://developer.webull.com/apis/docs/changelog/)
and the [Thailand Open Orders reference](https://developer.webull.co.th/apis/docs/reference/trade-api/order-open/).
It has SDK contract and local parser tests; it still needs read-only UAT
validation on the deployed candidate. The [Order Detail reference](https://developer.webull.co.th/apis/docs/reference/trade-api/order-detail/)
remains the exact-client-ID recovery source for an attempted order.
