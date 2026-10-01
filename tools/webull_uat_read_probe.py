#!/usr/bin/env python3
"""Read-only Webull TH UAT probe, Python standard library only.

Run: python3 webull_uat_read_probe.py --output webull-uat-results.json
Credentials: WEBULL_ACCOUNT_ID / WEBULL_APP_KEY / WEBULL_APP_SECRET,
or interactive prompts. An optional existing WEBULL_ACCESS_TOKEN is supported.
No token creation, order preview, placement, cancellation, or deployment.

Signing and request contract verified against the official SDK source:
https://github.com/webull-inc/webull-openapi-python-sdk/tree/abe5668ce4bc11dd1e8a1aad944d9dbb239bf12d
webull/core/auth/composer/default_signature_composer.py
webull/core/auth/algorithm/sha_hmac256_new.py
This is an independent HTTP diagnostic, not a run of the LEGO strategy.
"""
import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

HOST = "th-api.uat.webullbroker.com"
PATHS = {
    "/openapi/config", "/trading/accounts/list",
    "/trading/assets/positions/list", "/app/subscriptions/list",
    "/market-data/stocks/snapshots/list",
}
SOURCE = "abe5668ce4bc11dd1e8a1aad944d9dbb239bf12d"


def signed_headers(path, params, key, secret, timestamp=None, nonce=None):
    if path not in PATHS:
        raise ValueError("Only the allowlisted read endpoints are permitted")
    headers = {
        "x-app-key": key,
        "x-timestamp": timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "x-signature-version": "1.0", "x-signature-algorithm": "HMAC-SHA256",
        "x-signature-nonce": nonce or str(uuid.uuid4()),
    }
    sign_params = {"host": HOST, **headers}
    for k, v in params.items():
        sign_params[k] = str(v) if k not in sign_params else str(sign_params[k]) + "&" + str(v)
    canonical = path + "&" + "&".join(str(k) + "=" + str(v) for k, v in sorted(sign_params.items()))
    to_sign = urllib.parse.quote(canonical, safe="")
    headers["x-signature"] = base64.b64encode(hmac.new((secret + "&").encode(), to_sign.encode(), hashlib.sha256).digest()).decode()
    headers.update({"x-version": "v3", "Accept": "application/json", "User-Agent": "Webull-TH-UAT-Read-Probe/1.0"})
    return headers


def clean_text(value, credentials):
    text = str(value)
    for secret in credentials.values():
        if secret:
            text = text.replace(str(secret), "<redacted>")
    text = re.sub(r"(?i)((?:x-signature|x-app-key|x-access-token|app_secret|token)[\s\"']*[:=][\s\"']*)[^\s\",}]+", r"\1<redacted>", text)
    return text[:400]


def call(path, params, credentials, timeout=10):
    headers = signed_headers(path, params, credentials["app_key"], credentials["app_secret"])
    if credentials.get("token"):
        headers["x-access-token"] = credentials["token"]
    url = "https://" + HOST + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers=headers, method="GET")
    result = {"endpoint": path, "symbol": params.get("symbols"), "attempts": 1}
    started = time.monotonic()
    payload = None
    try:
        try:
            response = urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            result["http_status"] = response.code
            raw = response.read(1_000_001)
            result["response_bytes"] = len(raw)
            result["request_id"] = response.headers.get("X-Request-Id")
        if len(raw) > 1_000_000:
            result["error"] = "Response exceeds probe limit"
        else:
            try:
                payload = json.loads(raw.decode("utf-8"))
                result["response_type"] = type(payload).__name__
                if isinstance(payload, dict):
                    for field in ("error_code", "error_msg", "code", "message", "request_id"):
                        if field in payload and payload[field] is not None:
                            result[field] = clean_text(payload[field], credentials)
            except (UnicodeError, ValueError):
                result["response_type"] = "non_json"
                result["error"] = "Non-JSON response; cannot attribute it to Webull"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        result["transport_error"] = type(exc).__name__
        result["detail"] = clean_text(exc, credentials)
    result["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
    result["api_success"] = result.get("http_status") == 200 and payload is not None and not (
        isinstance(payload, dict) and any(payload.get(k) for k in ("error_code", "error", "errorCode")))
    return result, payload


def entries(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "positions", "items", "accounts"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return None


def quote_summary(payload, symbol):
    rows = entries(payload)
    if rows is None and isinstance(payload, dict):
        rows = [payload.get(symbol, payload)]
    matches = [x for x in (rows or []) if isinstance(x, dict) and str(x.get("symbol", "")).upper() == symbol]
    # An unnamed single-record result is accepted by the LEGO adapter too.
    if not matches and len(rows or []) == 1 and isinstance(rows[0], dict) and not rows[0].get("symbol"):
        matches = rows
    out = {"matching_records": len(matches), "valid_quote": False}
    if len(matches) == 1:
        row = matches[0]
        price = next((row[k] for k in ("last", "lastPrice", "price", "close") if row.get(k)), None)
        stamp = next((row[k] for k in ("last_trade_time", "lastTradeTime", "trade_time") if row.get(k)), None)
        try:
            price_float = float(price)
            stamp_float = float(stamp)
            now_ms = datetime.now(timezone.utc).timestamp() * 1000
            age = (now_ms - stamp_float) / 1000
            out.update({"price": price, "last_trade_time": stamp, "quote_age_seconds": round(age, 1),
                        "valid_quote": 0 < price_float < float("inf") and stamp_float >= 100_000_000_000 and -5 <= age <= 360})
        except (ValueError, TypeError, OverflowError):
            pass
    return out


def run_probe(credentials, timeout=10):
    report = {"started_utc": datetime.now(timezone.utc).isoformat(), "host": HOST,
              "mode": "read_only", "sdk_source_ref": SOURCE,
              "automatic_retries": 0, "checks": []}

    def check(name, path, params=None):
        r, data = call(path, params or {}, credentials, timeout)
        r["check"] = name
        report["checks"].append(r)
        print(name + ": " + json.dumps(r, ensure_ascii=False), flush=True)
        return r, data

    r, data = check("config", "/openapi/config")
    if not r["api_success"] or not isinstance(data, dict) or type(data.get("token_check_enabled")) is not bool:
        report["conclusion"] = (
            "AUTH_REJECTED: config returned 401; check credentials, environment and request signing"
            if r.get("http_status") == 401 else
            "CONFIG_NOT_VERIFIED: credentials/API health cannot be assessed from this environment")
        return report
    r["token_check_enabled"] = data["token_check_enabled"]
    if data["token_check_enabled"] and not credentials.get("token"):
        report["conclusion"] = "TOKEN_REQUIRED: provide an existing verified token; probe will not create one"
        return report
    time.sleep(1)
    r, data = check("accounts", "/trading/accounts/list")
    rows = entries(data)
    matched = rows is not None and any(isinstance(x, dict) and str(x.get("account_id")) == credentials["account_id"] for x in rows)
    r["expected_account_found"] = matched
    if not r["api_success"] or not matched:
        report["conclusion"] = "ACCOUNT_NOT_VERIFIED: check credentials/account ID and response contract"
        return report
    time.sleep(1)
    r, data = check("positions", "/trading/assets/positions/list", {"account_id": credentials["account_id"]})
    position_rows = entries(data)
    positions_verified = bool(r["api_success"] and position_rows is not None
                              and all(isinstance(x, dict) for x in position_rows))
    r["position_count"] = len(position_rows) if position_rows is not None else None
    r["positions_verified"] = positions_verified
    report["account_data_verified"] = positions_verified
    if r.get("http_status") in (401, 403):
        report["conclusion"] = "POSITIONS_AUTH_BLOCKED"
        return report
    time.sleep(1)
    r, data = check("subscriptions", "/app/subscriptions/list")
    r["subscription_count"] = len(entries(data)) if entries(data) is not None else None
    # An available route or a subscription count does not establish entitlement
    # for this category/symbol. A 404 establishes neither absence nor presence.
    report["market_data_permissions_verified"] = False
    if r.get("http_status") == 404:
        r["route_status"] = "NOT_FOUND_FOR_THIS_REQUEST"
    for symbol in ("UBER", "AAPL", "TSLA"):
        time.sleep(1)
        r, data = check("snapshot_" + symbol, "/market-data/stocks/snapshots/list", {"symbols": symbol, "category": "US_STOCK"})
        if r["api_success"]:
            r.update(quote_summary(data, symbol))
        if r.get("http_status") == 401:
            break
    quotes = [x for x in report["checks"] if x["check"].startswith("snapshot_")]
    uber = next((x for x in quotes if x["symbol"] == "UBER"), {})
    controls = [x for x in quotes if x["symbol"] in ("AAPL", "TSLA")]
    if uber.get("http_status") == 500 and any(x["api_success"] for x in controls):
        report["conclusion"] = "UBER_SPECIFIC_500: control symbols responded successfully; refer request IDs to Webull"
    elif len(quotes) == 3 and all(x.get("http_status") == 500 and x.get("response_type") == "dict" for x in quotes):
        report["conclusion"] = "SNAPSHOT_500_ALL_SYMBOLS: all three snapshot requests returned 500; underlying cause and data entitlement are unverified"
    elif len(quotes) == 3 and all(x["api_success"] and x.get("valid_quote") for x in quotes):
        report["conclusion"] = (
            "FRESH_SNAPSHOTS_OK_NOW: account reads and three fresh quotes passed; subscriptions, strategy and order execution are not validated"
            if positions_verified else
            "SNAPSHOTS_OK_POSITIONS_UNVERIFIED: fresh quotes passed, but positions did not; overall read probe has not passed")
    elif any(x.get("http_status") == 403 for x in quotes):
        report["conclusion"] = "SNAPSHOT_ACCESS_DENIED: inspect authentication headers, credentials and market-data permissions"
    else:
        report["conclusion"] = "INCONCLUSIVE: inspect individual statuses, transport errors, and quote validity"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="Optional redacted JSON report")
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 30:
        parser.error("--timeout must be between 1 and 30 seconds")
    creds = {
        "account_id": os.environ.get("WEBULL_ACCOUNT_ID") or input("Account ID: ").strip(),
        "app_key": os.environ.get("WEBULL_APP_KEY") or getpass.getpass("App Key (hidden): ").strip(),
        "app_secret": os.environ.get("WEBULL_APP_SECRET") or getpass.getpass("App Secret (hidden): ").strip(),
        "token": os.environ.get("WEBULL_ACCESS_TOKEN", ""),
    }
    if any(not creds[k] for k in ("account_id", "app_key", "app_secret")):
        parser.error("Account ID, App Key and App Secret are required")
    report = run_probe(creds, args.timeout)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as file:
            file.write(text + "\n")
    print(text)
    return 0 if report["conclusion"].startswith("FRESH_SNAPSHOTS_OK_NOW") else 2


if __name__ == "__main__":
    raise SystemExit(main())
