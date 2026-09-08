# Webull contract sources (v2 candidate)

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
