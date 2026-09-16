# Vendored Webull SDK wheel

`webull_openapi_python_sdk-3.0.1-1lego-py3-none-any.whl` is rebuilt from the
official Webull `3.0.1` PyPI wheel.

The old runtime was 2.0.15. Webull Thailand now documents the current account,
authentication, order and market-data contracts and tells Python SDK users to
upgrade the official SDK. SDK 3.0.1 contains those route/version changes as one
coherent client instead of requiring local endpoint shims.

The rebuild changes **dependency metadata only**. Every file under `webull/` is
byte-for-byte identical to the official 3.0.1 wheel; only the two cryptography
`Requires-Dist` lines in `METADATA` change and wheel `RECORD` is regenerated.

- official 3.0.1 wheel SHA-256: `35abe4ecc65bc80f9de075a83b057a393d6991bae2a00417fc691aa05602694e`
- patched wheel SHA-256: `80d631d2decba3680e4cd5c3b8b7898301667397080460186f128e943a361e7a`
- runtime package file count: `299`
- aggregate SHA-256 of all `webull/` runtime files: `867063412d0c5acae34cfee9406d52521a3cb2ad8b682a61a06ee517e3ee3552`
- license/notice from the Apache-2.0 upstream wheel remain inside the wheel
- `test_vendored_webull_wheel.py` validates RECORD, constraints and runtime provenance

Do not replace this wheel without the complete CI suite and a read-only UAT
broker smoke test. Never retry `place_order` after an ambiguous broker response.
