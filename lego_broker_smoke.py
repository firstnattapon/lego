"""Redacted Webull connectivity and Preview smoke test.

This command has no Firebase dependency and deliberately imports no broker
mutation helper.  It proves authentication, account visibility, positions,
open-order queries, market data, and (when explicitly requested in UAT) Preview.
"""
from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import sys
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from lego_orders import UAT
from webull_io import (PROD_ENDPOINT, UAT_ENDPOINT, build_clients,
                       build_order_payload, clients_endpoint,
                       environment_label, fetch_holdings, fetch_open_orders,
                       fetch_snapshot, load_config, preview_market_order,
                       redact_sensitive_text)


SCHEMA = "lego_broker_smoke_v1"


class SmokeRefusal(RuntimeError):
    """The requested smoke would cross its fail-closed safety boundary."""


def _contains_account_id(value: Any, expected: str) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = "".join(
                character for character in str(key).lower()
                if character.isalnum())
            if (normalized_key in {"accountid", "accountno", "accountnumber"}
                    and not isinstance(item, (dict, list, tuple))
                    and hmac.compare_digest(str(item), expected)):
                return True
            if _contains_account_id(item, expected):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_account_id(item, expected) for item in value)
    return False


def _positive_quantity(value: str) -> float:
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SmokeRefusal("preview quantity must be a finite positive number") from exc
    if not quantity.is_finite() or quantity <= 0:
        raise SmokeRefusal("preview quantity must be a finite positive number")
    number = float(quantity)
    if not math.isfinite(number) or number <= 0:
        raise SmokeRefusal("preview quantity is outside the supported range")
    return number


def run_smoke(*, preview_side: str | None = None,
              preview_quantity: str | None = None) -> dict:
    """Run broker reads and, optionally, one non-submitting UAT Preview."""
    if os.environ.get("AUTO_SUBMIT", "").strip().lower() != "false":
        raise SmokeRefusal(
            "AUTO_SUBMIT must be explicitly false during an interactive broker smoke")
    if bool(preview_side) != bool(preview_quantity):
        raise SmokeRefusal(
            "preview side and preview quantity must be provided together")

    environment = environment_label()
    if preview_side and environment != UAT:
        raise SmokeRefusal("Preview smoke is allowed only in Test (UAT)")

    configured_account = os.environ.get("WEBULL_ACCOUNT_ID", "").strip()
    if not configured_account:
        raise SmokeRefusal("WEBULL_ACCOUNT_ID is required")

    config = load_config()
    trade_client, data_client = build_clients()
    expected_endpoint = (
        UAT_ENDPOINT if environment == UAT else PROD_ENDPOINT)
    if not hmac.compare_digest(
            str(clients_endpoint(trade_client, data_client) or ""),
            expected_endpoint):
        raise SmokeRefusal(
            "authenticated client endpoint does not match the validated environment")
    account_response = trade_client.account_v2.get_account_list()
    account_status = getattr(account_response, "status_code", None)
    if account_status != 200:
        raise SmokeRefusal(
            f"account list returned unexpected HTTP status {account_status!r}")
    account_payload = account_response.json()
    if not _contains_account_id(account_payload, configured_account):
        raise SmokeRefusal(
            "configured account is not present in authenticated account list")

    holdings = fetch_holdings(trade_client, config)
    if not math.isfinite(holdings) or holdings < 0:
        raise SmokeRefusal("broker holdings are not finite and non-negative")
    open_orders = fetch_open_orders(trade_client, config.symbol)
    if not isinstance(open_orders, list):
        raise SmokeRefusal("open-orders query returned an unknown shape")
    snapshot = fetch_snapshot(trade_client, data_client, config)
    if (not math.isfinite(float(snapshot.get("price", 0)))
            or float(snapshot.get("price", 0)) <= 0
            or not snapshot.get("quote_time")):
        raise SmokeRefusal("market snapshot is missing safe price/time evidence")

    checks = {
        "auto_submit_disabled": True,
        "account_list_http_200": True,
        "configured_account_present": True,
        "holdings_read": True,
        "open_orders_read": True,
        "snapshot_price_and_time": True,
        "preview_requested": bool(preview_side),
    }
    report = {
        "schema": SCHEMA,
        "environment": environment,
        "ok": True,
        "checks": checks,
        "broker_mutations": False,
    }

    if preview_side:
        side = str(preview_side).upper()
        if side not in {"BUY", "SELL"}:
            raise SmokeRefusal("preview side must be BUY or SELL")
        quantity = _positive_quantity(str(preview_quantity))
        if side == "SELL" and quantity > holdings + 1e-9:
            raise SmokeRefusal(
                "SELL Preview quantity exceeds current broker holdings")
        client_order_id = ("legosmoke" + uuid.uuid4().hex)[:32]
        payload = build_order_payload(
            config, side, quantity, client_order_id)
        preview_ok = bool(preview_market_order(trade_client, payload))
        checks["preview_accepted"] = preview_ok
        report["ok"] = preview_ok

    return report


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise SmokeRefusal("invalid command-line arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        description=(
            "Webull read-only connectivity smoke; optional Preview is UAT-only."
        ))
    parser.add_argument("--preview-side", choices=("BUY", "SELL"))
    parser.add_argument("--preview-quantity")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        report = run_smoke(
            preview_side=args.preview_side,
            preview_quantity=args.preview_quantity,
        )
    except Exception as exc:
        report = {
            "schema": SCHEMA,
            "ok": False,
            "checks": {},
            "broker_mutations": False,
            "error_type": type(exc).__name__,
            "error": redact_sensitive_text(str(exc)),
        }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 2


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    sys.exit(main())
