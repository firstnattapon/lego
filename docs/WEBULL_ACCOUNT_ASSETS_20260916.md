# Webull account assets / HTTP 417 incident

The supplied RTDB error export records `HTTP 417 / OPENAPI_SYSTEM_ERROR` during
`fetch_snapshot -> fetch_holdings -> account_v2.get_account_position`. The error
occurs before price retrieval or new order submission. The export alone does
not prove whether the upstream failure was caused by routing, the account, or
a Webull service incident.

## Contract mismatch fixed

The pinned SDK 2.0.15 still constructs these legacy account requests, while the
current Thai API reference linked from `llms.txt` documents different paths:

| Read | SDK 2.0.15 path / version | Adapter now uses |
| --- | --- | --- |
| Positions | `/openapi/assets/positions` / v2 | `/trading/assets/positions/list` / v3 |
| Balance | `/openapi/assets/balance` / v2 | `/trading/assets/balances/get` / v3 |

The adapter uses `ApiRequest` through the same authenticated SDK client, retaining
the configured UAT/production host, signer, token, and request deadline. Only GET
reads are retried; each retry constructs a new request. No legacy endpoint
fallback or zero-holdings fallback is used.

Sources checked on 2026-09-16:

- [Webull documentation index](https://developer.webull.co.th/apis/llms.txt)
- [Account positions reference](https://developer.webull.co.th/apis/docs/reference/trade-api/account-position.md)
- [Account balance reference](https://developer.webull.co.th/apis/docs/reference/trade-api/account-balance.md)

The account guide still shows the `account_v2` SDK convenience methods. This
change follows the explicit HTTP reference paths and `x-version: v3`; it does
not infer that all v2 endpoints are unsupported.

## Failure behavior

Commit `4978cf8` on main already classifies `OPENAPI_SYSTEM_ERROR` as transient.
Regression coverage now exercises the real pinned SDK exception and request
classes: recovery after 417, bounded retry exhaustion, other 417 business errors,
401/403, tick deadlines, and exactly one Place attempt when Place raises 417.

Position parsing now rejects null/malformed lists, missing or invalid quantities,
non-finite/negative quantities, and duplicate equity matches. A valid empty list
still means zero holdings. Explicit OPTION records are not counted as equity.
This prevents an unknown position from silently becoming an initial-funding BUY.
Handler tests verify failures do not commit a row or create an order intent.

## Rollout verification

After review/merge, deploy the new revision and run the existing read-only smoke
command with the intended account credentials loaded securely:

```bash
AUTO_SUBMIT=false python lego_broker_smoke.py
```

Local regression tests use fake broker responses, not live account access. A
successful test suite cannot establish that the upstream 417 has cleared. If
417 persists after deployment, retain the redacted request ID and timestamp for
Webull support; do not treat failed position reads as zero or retry Place blindly.
