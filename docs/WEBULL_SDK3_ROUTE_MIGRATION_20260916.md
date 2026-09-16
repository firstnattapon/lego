# Webull SDK 3.0.1 route migration — 2026-09-16

## Incident

After PR #5, UAT continued to return `HTTP 417 / OPENAPI_SYSTEM_ERROR` on every
scheduled holdings read and the safe-read retry exhausted all attempts. One slot
also timed out. Replacing only the account positions path therefore did not fix
the broker contract mismatch.

## Migration

The application was still running Webull SDK 2.0.15. Current Webull Thailand
documentation tells Python SDK users to upgrade the official SDK. SDK 3.0.1
carries the current request routes/version headers for authentication, accounts,
orders and market data as one coherent client.

This change removes the local account-assets route shim and lets SDK 3.0.1 build
those requests, while locking the critical route contracts in tests.

## Safety kept

- `OPENAPI_SYSTEM_ERROR` retries only through the safe read/preview wrapper.
- `place_market_order()` remains exactly one broker submission attempt.
- malformed holdings fail closed rather than becoming zero.
- the vendored wheel keeps official 3.0.1 runtime bytes unchanged; only
  dependency metadata and RECORD are rebuilt for `cryptography==50.0.0`.

## Required after deploy

Run the read-only broker smoke with `AUTO_SUBMIT=false`. It must prove account
visibility, positions, open orders and a market snapshot before trade mode is
re-enabled.
